"""File-object sync: manifests, latest cloud versions and recoverable tree updates."""

import json
import os
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.cache import ObjectCache, fingerprint, scan
from app.domain import WorkspaceError
from app.filesystem import check_path, excluded, safe_relative
from app.snapshots import FileEntry, Snapshot


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: UUID
    project_id: UUID
    parent: UUID | None = None
    created_at: str
    files: dict[str, FileEntry]
    directories: list[str]
    merge_parent: UUID | None = None
    author: str = "local"
    message: str = ""


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_revision: UUID
    files: dict[str, FileEntry]
    directories: list[str] = Field(default_factory=list)


def validate_tree(files, directories, settings):
    if len(files) + len(directories) > settings.max_files:
        raise WorkspaceError("copy_limit", "Too many entries", 413)
    if sum(FileEntry.model_validate(e).size for e in files.values()) > settings.max_bytes:
        raise WorkspaceError("copy_limit", "Project exceeds size limit", 413)
    seen, file_keys = set(), {name.casefold() for name in files}
    for name in [*files, *directories]:
        path = safe_relative(name)
        if any(excluded(part) for part in path.parts):
            raise WorkspaceError("excluded_file", "Excluded path in manifest", 422)
        if name.casefold() in seen or any(
            parent.as_posix().casefold() in file_keys
            for parent in path.parents
            if parent.as_posix() != "."
        ):
            raise WorkspaceError("path_collision", "Manifest paths collide", 422)
        seen.add(name.casefold())


def changes(before, after):
    old, new = before.files, after.files
    return {
        "added": sorted(set(new) - set(old)),
        "modified": sorted(name for name in old.keys() & new.keys() if old[name] != new[name]),
        "deleted": sorted(set(old) - set(new)),
        "directories_changed": sorted(before.directories) != sorted(after.directories),
    }


def is_changed(delta):
    return any(delta.values())


def index_for(root, revision):
    return {name: fingerprint(root / name) for name in revision.files}


class TreeUpdater:
    """Journal only affected paths. Cached old objects provide rollback contents."""

    def __init__(self, cache, state_root):
        self.cache = cache
        self.jobs = state_root / "sync-jobs"
        self.jobs.mkdir(exist_ok=True)

    def apply(self, root: Path, old: Revision, new: Revision, identity: str, marker=None):
        check_path(root)
        job = self.jobs / identity
        if job.exists():
            raise WorkspaceError("sync_pending", "A previous sync needs recovery")
        job.mkdir()
        delta = changes(old, new)
        try:
            for name in delta["added"] + delta["modified"]:
                self.cache.materialize(new.files[name], job / "next" / name)
            # Persist intent before modifying working files.
            (job / "journal.json").write_text(
                json.dumps(
                    {
                        "old": old.model_dump(mode="json"),
                        "new": new.model_dump(mode="json"),
                        "marker": marker,
                    }
                )
            )
            self._remove(
                root, delta["deleted"] + delta["modified"], old.directories, new.directories
            )
            for directory in new.directories:
                check_path(root / directory)
                (root / directory).mkdir(parents=True, exist_ok=True)
            for name in delta["added"] + delta["modified"]:
                target = root / name
                check_path(target)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(job / "next" / name, target)
        except Exception:
            if (job / "journal.json").exists():
                self.rollback(root, job)
            else:
                shutil.rmtree(job)
            raise
        return job

    @staticmethod
    def _remove(root, files, old_dirs, new_dirs):
        for name in files:
            path = root / name
            check_path(path)
            if path.is_file():
                path.unlink()
        for directory in sorted(
            set(old_dirs) - set(new_dirs), key=lambda p: len(p.split("/")), reverse=True
        ):
            path = root / directory
            check_path(path)
            if path.is_dir():
                try:
                    path.rmdir()
                except OSError:
                    # Preserve ignored/untracked contents. A conflicting write will fail.
                    pass

    def rollback(self, root, job):
        data = json.loads((job / "journal.json").read_text())
        old, new = Revision.model_validate(data["old"]), Revision.model_validate(data["new"])
        delta = changes(old, new)
        self._remove(root, delta["added"] + delta["modified"], new.directories, old.directories)
        for directory in old.directories:
            (root / directory).mkdir(parents=True, exist_ok=True)
        for name in delta["deleted"] + delta["modified"]:
            self.cache.materialize(old.files[name], root / name)
        shutil.rmtree(job)

    def finish(self, job):
        shutil.rmtree(job)


class SyncService:
    def __init__(self, manager):
        self.manager, self.repo = manager, manager.repo
        self.cache = ObjectCache(manager.settings.state_root)
        self.updater = TreeUpdater(self.cache, manager.settings.state_root)
        with self.repo.connect() as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS object_projects (project_id TEXT PRIMARY KEY)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS sparse_workspaces (workspace_id TEXT PRIMARY KEY, paths TEXT NOT NULL)"
            )
        self.recover()

    def store_revision(self, revision: Revision, db=None):
        if db is None:
            with self.repo.connect() as connection, connection:
                self.store_revision(revision, connection)
        else:
            metadata = revision.model_dump(mode="json")
            if (
                revision.parent
                and db.execute(
                    "SELECT 1 FROM sync_revisions WHERE revision_id=?", (str(revision.parent),)
                ).fetchone()
            ):
                parent = self.revision(revision.parent, db)
                delta = changes(parent, revision)
                metadata.pop("files")
                metadata.pop("directories")
                metadata["delta"] = {
                    "files": {
                        n: revision.files[n].model_dump()
                        for n in delta["added"] + delta["modified"]
                    },
                    "deleted": delta["deleted"],
                    "directories_added": sorted(
                        set(revision.directories) - set(parent.directories)
                    ),
                    "directories_deleted": sorted(
                        set(parent.directories) - set(revision.directories)
                    ),
                }
            db.execute(
                "INSERT OR IGNORE INTO sync_revisions VALUES (?,?)",
                (str(revision.revision_id), json.dumps(metadata)),
            )

    def revision(self, identity: UUID, db=None) -> Revision:
        if db is None:
            with self.repo.connect() as connection:
                return self.revision(identity, connection)
        chain = []
        while True:
            row = db.execute(
                "SELECT metadata FROM sync_revisions WHERE revision_id=?", (str(identity),)
            ).fetchone()
            if not row:
                raise WorkspaceError("revision_missing", "Version is not cached", 404)
            data = json.loads(row[0])
            if "delta" not in data:
                result = Revision.model_validate(data)
                break
            chain.append(data)
            identity = data["parent"]
        for data in reversed(chain):
            delta = data.pop("delta")
            files = dict(result.files)
            for name in delta["deleted"]:
                files.pop(name, None)
            files.update(delta["files"])
            directories = (set(result.directories) - set(delta["directories_deleted"])) | set(
                delta["directories_added"]
            )
            result = Revision.model_validate(
                {**data, "files": files, "directories": sorted(directories)}
            )
        return result

    def head_row(self, project_id):
        with self.repo.connect() as db:
            return db.execute(
                "SELECT * FROM sync_heads WHERE project_id=?", (str(project_id),)
            ).fetchone()

    def head(self, project_id: UUID) -> tuple[Revision, dict]:
        with self.manager.lock:
            if self.object_only(project_id):
                row = self.head_row(project_id)
                return self.revision(UUID(row["revision_id"])), {"files_read": 0, "bytes_read": 0}
            source = self.manager.fs.source(self.repo.project_path(project_id))
            row = self.head_row(project_id)
            old = self.revision(UUID(row["revision_id"])) if row else None
            files, directories, index, stats = scan(
                source,
                self.cache,
                self.manager.settings,
                old.files if old else {},
                json.loads(row["stat_index"]) if row else {},
            )
            candidate = Revision(
                revision_id=uuid4(),
                project_id=project_id,
                parent=old.revision_id if old else None,
                created_at=datetime.now(UTC).isoformat(),
                files=files,
                directories=directories,
            )
            if old is not None and not is_changed(changes(old, candidate)):
                candidate = old
            with self.repo.connect() as db, db:
                self.store_revision(candidate, db)
                db.execute(
                    "INSERT OR REPLACE INTO sync_heads VALUES (?,?,?)",
                    (str(project_id), str(candidate.revision_id), json.dumps(index)),
                )
            return candidate, stats

    def object_only(self, project_id):
        with self.repo.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM object_projects WHERE project_id=?", (str(project_id),)
                ).fetchone()
                is not None
            )

    def from_snapshot(self, snapshot_id: UUID) -> Revision:
        with self.manager.lock:
            try:
                return self.revision(snapshot_id)
            except WorkspaceError:
                pass
            metadata = Snapshot.model_validate(self.repo.snapshot(snapshot_id))
            archive = self.manager.settings.snapshot_root / f"{snapshot_id}.zip"
            with zipfile.ZipFile(archive) as package:
                for name, entry in metadata.files.items():
                    if not self.cache.has(entry.sha256, entry.size):
                        with package.open(name) as stream:
                            self.cache.put(stream, self.manager.settings.max_bytes, entry.sha256)
            revision = Revision(
                revision_id=snapshot_id,
                project_id=metadata.project_id,
                created_at=metadata.created_at,
                files=metadata.files,
                directories=metadata.directories,
            )
            self.store_revision(revision)
            return revision

    def publish(self, project_id: UUID, request: PublishRequest, candidate=None):
        with self.manager.lock:
            old, _ = self.head(project_id)
            if old.revision_id != request.base_revision:
                raise WorkspaceError(
                    "stale_base", "Cloud changed; pull the latest files before pushing"
                )
            validate_tree(request.files, request.directories, self.manager.settings)
            for entry in request.files.values():
                if not self.cache.has(entry.sha256, entry.size):
                    raise WorkspaceError(
                        "object_missing", "Upload missing objects before publishing"
                    )
            revision = candidate or Revision(
                revision_id=uuid4(),
                project_id=project_id,
                parent=old.revision_id,
                created_at=datetime.now(UTC).isoformat(),
                files=request.files,
                directories=sorted(request.directories),
            )
            if revision.project_id != project_id or revision.parent != old.revision_id:
                raise WorkspaceError(
                    "stale_base", "Rebase the changeset onto the current head before landing"
                )
            delta = changes(old, revision)
            if not is_changed(delta) and candidate is None:
                return old
            if self.object_only(project_id):
                with self.repo.connect() as db, db:
                    self.store_revision(revision, db)
                    cursor = db.execute(
                        "UPDATE sync_heads SET revision_id=? WHERE project_id=? AND revision_id=?",
                        (str(revision.revision_id), str(project_id), str(old.revision_id)),
                    )
                    if cursor.rowcount != 1:
                        raise WorkspaceError("stale_base", "Cloud changed while publishing")
                return revision
            source = self.repo.project_path(project_id)
            job = self.updater.apply(source, old, revision, f"project-{project_id}")
            try:
                with self.repo.connect() as db, db:
                    self.store_revision(revision, db)
                    cursor = db.execute(
                        "UPDATE sync_heads SET revision_id=?,stat_index=? WHERE project_id=? AND revision_id=?",
                        (
                            str(revision.revision_id),
                            json.dumps(index_for(source, revision)),
                            str(project_id),
                            str(old.revision_id),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise WorkspaceError("stale_base", "Cloud changed while publishing")
            except Exception:
                self.updater.rollback(source, job)
                raise
            self.updater.finish(job)
            return revision

    def recover(self):
        for job in self.updater.jobs.iterdir():
            if not job.is_dir():
                continue
            check_path(job)
            journal = job / "journal.json"
            if not journal.exists():
                # Only UUID-labelled jobs generated by us are eligible for cleanup.
                if job.name.startswith(("project-", "workspace-", "expand-")):
                    UUID(job.name.split("-", 1)[1])
                    shutil.rmtree(job)
                continue
            data = json.loads(journal.read_text())
            new = Revision.model_validate(data["new"])
            if job.name.startswith("project-"):
                project = UUID(job.name.removeprefix("project-"))
                root = self.repo.project_path(project)
                row = self.head_row(project)
                committed = row is not None and row["revision_id"] == str(new.revision_id)
            elif job.name.startswith("expand-"):
                identity = UUID(job.name.removeprefix("expand-"))
                root = self.manager.get(identity).path
                with self.repo.connect() as db:
                    row = db.execute(
                        "SELECT paths FROM sparse_workspaces WHERE workspace_id=?", (str(identity),)
                    ).fetchone()
                committed = row is not None and json.loads(row[0]) == data["marker"]
            elif job.name.startswith("workspace-"):
                identity = UUID(job.name.removeprefix("workspace-"))
                root = self.manager.get(identity).path
                with self.repo.connect() as db:
                    row = db.execute(
                        "SELECT base_revision FROM workspace_sync WHERE workspace_id=?",
                        (str(identity),),
                    ).fetchone()
                committed = row is not None and row[0] == str(new.revision_id)
            else:
                continue
            if committed:
                self.updater.finish(job)
            else:
                self.updater.rollback(root, job)
