"""Content-addressed immutable file objects and stat-based working-copy scans."""

import hashlib
import os
import shutil
import stat
import tempfile
import time
from pathlib import Path

from app.domain import WorkspaceError
from app.filesystem import check_path, excluded, safe_relative
from app.snapshots import FileEntry


class ObjectCache:
    def __init__(self, state_root: Path):
        self.root = state_root / "objects"
        check_path(self.root)
        self.root.mkdir(exist_ok=True)
        self._verified = {}
        self._access = {}

    def path(self, sha256: str) -> Path:
        if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
            raise WorkspaceError("invalid_hash", "Expected a SHA-256 object ID", 422)
        path = self.root / sha256[:2] / sha256
        check_path(path)
        return path

    def has(self, sha256: str, size: int | None = None) -> bool:
        path = self.path(sha256)
        if not path.is_file() or (size is not None and path.stat().st_size != size):
            return False
        metadata = fingerprint(path)
        if self._verified.get(sha256) != metadata:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != sha256:
                return False
            self._verified[sha256] = metadata
        self._access[sha256] = time.time_ns()
        return True

    def prune(self, max_bytes: int, pinned=()):
        """Evict least recently used unpinned objects, never authoritative data."""
        pinned = set(pinned)
        objects = [p for p in self.root.glob("*/*") if p.is_file() and len(p.name) == 64]
        total = sum(p.stat().st_size for p in objects)
        removed, released = 0, 0
        for path in sorted(objects, key=lambda p: self._access.get(p.name, p.stat().st_mtime_ns)):
            if total <= max_bytes:
                break
            if path.name in pinned:
                continue
            size = path.stat().st_size
            path.unlink()
            self._verified.pop(path.name, None)
            self._access.pop(path.name, None)
            total -= size
            released += size
            removed += 1
        return {
            "bytes": total,
            "evicted_objects": removed,
            "evicted_bytes": released,
            "limit_bytes": max_bytes,
            "over_limit_pinned": total > max_bytes,
        }

    def put(self, stream, max_bytes: int, expected: str | None = None) -> tuple[str, int]:
        handle, temporary = tempfile.mkstemp(prefix="object-", dir=self.root)
        temporary = Path(temporary)
        try:
            hasher, total = hashlib.sha256(), 0
            with os.fdopen(handle, "wb") as output:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise WorkspaceError("copy_limit", "Object exceeds file limit", 413)
                    hasher.update(chunk)
                    output.write(chunk)
            identity = hasher.hexdigest()
            if expected is not None and expected != identity:
                raise WorkspaceError("checksum_mismatch", "Object hash mismatch", 422)
            target = self.path(identity)
            target.parent.mkdir(exist_ok=True)
            if not self.has(identity, total):
                temporary.replace(target)
            self._verified[identity] = fingerprint(target)
            return identity, total
        finally:
            temporary.unlink(missing_ok=True)

    def put_file(self, path: Path, max_bytes: int) -> tuple[str, int]:
        check_path(path)
        with path.open("rb") as stream:
            return self.put(stream, max_bytes)

    def materialize(self, entry: FileEntry, target: Path):
        if not self.has(entry.sha256, entry.size):
            raise WorkspaceError("object_missing", "Required object is not cached", 409)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Writable copies never share inodes with immutable cache objects.
        shutil.copyfile(self.path(entry.sha256), target)
        target.chmod(0o755 if entry.executable else 0o644)


def fingerprint(path: Path) -> list:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceError("unsupported_file", "Only regular files are supported", 422)
    return [
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_ino,
        bool(info.st_mode & 0o111),
    ]


def scan(root: Path, cache: ObjectCache, settings, previous_files=None, previous_index=None):
    """Read file contents only when metadata changed or the object is absent."""
    previous_files, previous_index = previous_files or {}, previous_index or {}
    files, index, directories, seen = {}, {}, [], set()
    total, read_files, read_bytes = 0, 0, 0
    check_path(root)
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            check_path(Path(directory) / name)
        dirs[:] = sorted(name for name in dirs if not excluded(name))
        for name in dirs + sorted(name for name in names if not excluded(name)):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            safe_relative(relative)
            if relative.casefold() in seen:
                raise WorkspaceError("path_collision", "Case-insensitive path collision", 422)
            seen.add(relative.casefold())
            if len(seen) > settings.max_files:
                raise WorkspaceError("copy_limit", "Too many entries", 413)
            if name in dirs:
                directories.append(relative)
                continue
            before = fingerprint(path)
            total += before[0]
            if total > settings.max_bytes:
                raise WorkspaceError("copy_limit", "Project exceeds size limit", 413)
            old = previous_files.get(relative)
            old = FileEntry.model_validate(old) if old else None
            if (
                old is not None
                and previous_index.get(relative) == before
                and cache.has(old.sha256, old.size)
            ):
                entry = old
            else:
                identity, size = cache.put_file(path, settings.max_bytes)
                read_files += 1
                read_bytes += size
                if fingerprint(path) != before:
                    raise WorkspaceError("source_changed", "File changed while scanning")
                entry = FileEntry(sha256=identity, size=size, executable=before[4])
            files[relative] = entry.model_dump()
            index[relative] = before
    return files, sorted(directories), index, {"files_read": read_files, "bytes_read": read_bytes}
