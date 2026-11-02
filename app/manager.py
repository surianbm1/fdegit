"""Registered workspace operations with crash recovery and process-level ownership."""

import logging
import os
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.config import Settings
from app.domain import CreateWorkspace, Status, Workspace, WorkspaceError, transition
from app.filesystem import LocalFilesystem, check_path
from app.storage import SQLiteRepository, initialize_database

logger = logging.getLogger(__name__)


class StateLock:
    def __init__(self, path):
        check_path(path)
        self.stream = path.open("a+b")
        try:
            self.stream.seek(0, 2)
            if self.stream.tell() == 0:
                self.stream.write(b"0")
                self.stream.flush()
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise WorkspaceError("state_locked", "Another process owns this environment") from error

    def close(self):
        self.stream.close()


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.lock = threading.RLock()
        for path in (
            settings.state_root,
            settings.workspace_root,
            settings.staging_root,
            settings.snapshot_root,
        ):
            check_path(path)
            path.mkdir(parents=True, exist_ok=True)
        self.process_lock = StateLock(settings.state_root / "process.lock")
        try:
            initialize_database(settings.metadata_database)
            self.repo = SQLiteRepository(settings.metadata_database, settings.workspace_root)
            self.fs = LocalFilesystem(settings)
            self.repo.source(str(settings.project_path))
            self.recover()
        except Exception:
            self.close()
            raise

    def close(self):
        self.process_lock.close()

    def get(self, workspace_id: UUID, owner: str | None = None) -> Workspace:
        workspace = self.repo.get(workspace_id)
        if workspace is None or (owner is not None and workspace.owner != owner):
            raise WorkspaceError("workspace_missing", "Workspace not found", 404)
        return workspace

    def reserve(
        self, name: str, source_id: UUID, owner="local", snapshot_id=None, remote_url=None
    ) -> Workspace:
        # Reuse request validation without accepting an arbitrary output path.
        name = CreateWorkspace(name=name, source_path=self.settings.project_path).name
        now = datetime.now(UTC)
        workspace_id = uuid4()
        workspace = Workspace(
            workspace_id=workspace_id,
            source_id=source_id,
            name=name,
            status=Status.CREATING,
            path=self.fs.final(workspace_id),
            created_at=now,
            updated_at=now,
            owner=owner,
            snapshot_id=snapshot_id,
            remote_url=remote_url,
        )
        self.repo.reserve(workspace, "default-v1")
        return workspace

    def failed(self, workspace: Workspace, error: Exception):
        message = f"{type(error).__name__}: {error}"
        try:
            self.fs.remove(workspace.workspace_id)
        except Exception as cleanup_error:
            message += f"; cleanup failed: {cleanup_error}"
        try:
            self.repo.set_status(workspace.workspace_id, Status.CREATING, Status.ERROR, message)
        except Exception:
            logger.exception("Could not record failure; startup recovery will retry")

    def finish(self, workspace: Workspace) -> Workspace:
        self.fs.publish(workspace.workspace_id)
        self.repo.set_status(workspace.workspace_id, Status.CREATING, Status.READY)
        return self.get(workspace.workspace_id)

    def create(self, request: CreateWorkspace, owner="local") -> Workspace:
        with self.lock:
            source = self.fs.source(request.source_path)
            workspace = self.reserve(request.name, self.repo.source(str(source)), owner)
            try:
                self.fs.copy(source, workspace.workspace_id)
                return self.finish(workspace)
            except Exception as error:
                self.failed(workspace, error)
                raise

    def action(self, workspace_id: UUID, action: str, owner: str | None = None) -> Workspace:
        with self.lock:
            workspace = self.get(workspace_id, owner)
            target = transition(workspace.status, action)
            if workspace.status == target:
                return workspace
            self.repo.set_status(workspace_id, workspace.status, target)
            if target == Status.DELETING:
                try:
                    self.fs.remove(workspace_id)
                    self.repo.set_status(workspace_id, Status.DELETING, Status.DELETED)
                except Exception as error:
                    self.repo.set_status(
                        workspace_id, Status.DELETING, Status.ERROR, f"Deletion failed: {error}"
                    )
                    raise
            return self.get(workspace_id, owner)

    def recover(self):
        # Paginate to avoid a silent recovery cap. No rows are deleted during iteration.
        offset = 0
        while rows := self.repo.list(include_deleted=True, limit=100, offset=offset):
            for workspace in rows:
                try:
                    if workspace.status in (Status.CREATING, Status.DELETING):
                        self.fs.remove(workspace.workspace_id)
                        target = (
                            Status.ERROR if workspace.status == Status.CREATING else Status.DELETED
                        )
                        self.repo.set_status(
                            workspace.workspace_id,
                            workspace.status,
                            target,
                            "Interrupted creation" if target == Status.ERROR else None,
                        )
                    elif workspace.status in (Status.READY, Status.ACTIVE, Status.STOPPED):
                        check_path(workspace.path)
                        if not workspace.path.is_dir():
                            raise OSError("Workspace directory missing")
                except Exception as error:
                    self.repo.set_status(
                        workspace.workspace_id,
                        workspace.status,
                        Status.ERROR,
                        f"Recovery failed: {error}",
                    )
                    logger.warning("Workspace recovery failed: %s", workspace.workspace_id)
            offset += len(rows)
