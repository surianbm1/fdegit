"""Immutable snapshot publication and bounded, portable archive extraction."""

import json
import os
import stat
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain import WorkspaceError
from app.filesystem import check_path, digest, excluded, inventory, safe_relative


class FileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    executable: bool = False


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID
    project_id: UUID
    status: str = "READY"
    created_at: str
    copy_policy: str = "default-v1"
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive_bytes: int = Field(ge=0)
    files: dict[str, FileEntry]
    directories: list[str]


def extract(archive: Path, metadata: Snapshot, destination: Path, settings) -> None:
    if metadata.status != "READY":
        raise WorkspaceError("snapshot_not_ready", "Snapshot is not ready")
    if (
        archive.stat().st_size != metadata.archive_bytes
        or digest(archive) != metadata.archive_sha256
    ):
        raise WorkspaceError("checksum_mismatch", "Archive checksum or length mismatch", 422)
    if len(metadata.files) + len(metadata.directories) > settings.max_files:
        raise WorkspaceError("copy_limit", "Too many snapshot entries", 413)
    if sum(entry.size for entry in metadata.files.values()) > settings.max_bytes:
        raise WorkspaceError("copy_limit", "Snapshot exceeds size limit", 413)
    paths = set()
    file_keys = {name.casefold() for name in metadata.files}
    for name in [*metadata.files, *metadata.directories]:
        relative = safe_relative(name)
        if any(excluded(part) for part in relative.parts):
            raise WorkspaceError("excluded_file", "Snapshot includes an excluded file", 422)
        if name.casefold() in paths:
            raise WorkspaceError("path_collision", "Duplicate snapshot path", 422)
        paths.add(name.casefold())
        if any(
            parent.as_posix().casefold() in file_keys
            for parent in relative.parents
            if parent.as_posix() != "."
        ):
            raise WorkspaceError("path_collision", "File/directory collision", 422)
    check_path(destination)
    destination.mkdir(exist_ok=False)
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) != len(metadata.files) or {e.filename for e in entries} != set(
            metadata.files
        ):
            raise WorkspaceError("invalid_archive", "Archive does not match manifest", 422)
        for entry in entries:
            mode = entry.external_attr >> 16
            if (
                entry.is_dir()
                or stat.S_ISLNK(mode)
                or entry.flag_bits & 1
                or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            ):
                raise WorkspaceError("invalid_archive", "Unsupported archive entry", 422)
            expected = metadata.files[entry.filename]
            if entry.file_size != expected.size:
                raise WorkspaceError("invalid_archive", "Entry size differs from manifest", 422)
            target = destination / entry.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with package.open(entry) as source, target.open("xb") as output:
                while chunk := source.read(1024 * 1024):
                    count += len(chunk)
                    if count > expected.size:
                        raise WorkspaceError("copy_limit", "Archive entry exceeds size", 413)
                    output.write(chunk)
            if count != expected.size or digest(target) != expected.sha256:
                raise WorkspaceError("checksum_mismatch", "File checksum mismatch", 422)
            if expected.executable:
                target.chmod(target.stat().st_mode | 0o111)
        for name in metadata.directories:
            (destination / name).mkdir(parents=True, exist_ok=True)


class SnapshotService:
    def __init__(self, manager):
        self.manager = manager
        self.repo = manager.repo
        self.root = manager.settings.snapshot_root
        self.recover()

    def archive_path(self, snapshot_id: UUID) -> Path:
        path = self.root / f"{UUID(str(snapshot_id))}.zip"
        check_path(path)
        return path

    def update(self, metadata):
        with self.repo.connect() as db, db:
            db.execute(
                "UPDATE snapshots SET metadata=? WHERE snapshot_id=?",
                (json.dumps(metadata), metadata["snapshot_id"]),
            )

    def recover(self):
        with self.repo.connect() as db:
            rows = list(db.execute("SELECT metadata FROM snapshots"))
        for row in rows:
            metadata = json.loads(row[0])
            if metadata["status"] == "CREATING":
                identity = UUID(metadata["snapshot_id"])
                try:
                    self.manager.fs.remove(identity)
                    for path in (
                        self.archive_path(identity),
                        self.archive_path(identity).with_suffix(".part"),
                    ):
                        path.unlink(missing_ok=True)
                    metadata.update(status="ERROR", error="Interrupted snapshot creation")
                except OSError:
                    metadata.update(status="ERROR", error="Snapshot cleanup failed; retry manually")
                self.update(metadata)

    def get(self, snapshot_id: UUID) -> dict:
        metadata = self.repo.snapshot(snapshot_id)
        if metadata["status"] != "READY":
            raise WorkspaceError("snapshot_not_ready", "Snapshot is not available")
        return metadata

    def create(self, project_id: UUID) -> dict:
        with self.manager.lock:
            source = self.manager.fs.source(self.repo.project_path(project_id))
            identity = uuid4()
            archive = self.archive_path(identity)
            partial = archive.with_suffix(".part")
            metadata = {
                "snapshot_id": str(identity),
                "project_id": str(project_id),
                "status": "CREATING",
            }
            self.repo.save_snapshot(metadata)
            try:
                self.manager.fs.copy(source, identity)
                stage = self.manager.fs.stage(identity)
                entries = inventory(stage, self.manager.settings)
                directories = []
                for directory, dirs, _ in os.walk(stage):
                    for name in dirs:
                        relative = (Path(directory) / name).relative_to(stage).as_posix()
                        safe_relative(relative)
                        directories.append(relative)
                if len(entries) + len(directories) > self.manager.settings.max_files:
                    raise WorkspaceError("copy_limit", "Too many snapshot entries", 413)
                with zipfile.ZipFile(partial, "x", compression=zipfile.ZIP_DEFLATED) as package:
                    for name in entries:
                        package.write(stage / name, arcname=name)
                metadata = Snapshot(
                    snapshot_id=identity,
                    project_id=project_id,
                    created_at=datetime.now(UTC).isoformat(),
                    archive_bytes=partial.stat().st_size,
                    archive_sha256=digest(partial),
                    files={
                        name: FileEntry(
                            size=entry["size"],
                            sha256=entry["sha256"],
                            executable=entry["executable"],
                        )
                        for name, entry in entries.items()
                    },
                    directories=directories,
                ).model_dump(mode="json")
                partial.rename(archive)
                self.manager.fs.remove(identity)
                self.update(metadata)
                return metadata
            except Exception as error:
                metadata = {
                    "snapshot_id": str(identity),
                    "project_id": str(project_id),
                    "status": "ERROR",
                    "error": str(error),
                }
                try:
                    self.manager.fs.remove(identity)
                    partial.unlink(missing_ok=True)
                    archive.unlink(missing_ok=True)
                except Exception as cleanup_error:
                    metadata["error"] += f"; cleanup failed: {cleanup_error}"
                self.update(metadata)
                raise
