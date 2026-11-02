"""Bounded file copies and deletion scoped to owned UUID directories.

Designed for trusted local storage, not hostile processes sharing the OS account.
"""

import hashlib
import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.config import Settings
from app.domain import WorkspaceError

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".runtime",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
    ".ssh",
    ".aws",
}


def excluded(name: str) -> bool:
    lower = name.casefold()
    return (
        lower in EXCLUDED
        or lower == ".env"
        or lower.startswith(".env.")
        or lower.endswith((".pem", ".key", ".p12", ".pfx"))
    )


def check_path(path: Path) -> None:
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise WorkspaceError("unsafe_link", "Links and reparse points are not allowed", 403)


def safe_relative(name: str) -> PurePosixPath:
    """Portable path policy, including Windows aliases and reserved device names."""
    parts = name.split("/")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        not name
        or "\\" in name
        or any(
            not p
            or p in (".", "..")
            or p.endswith((".", " "))
            or any(ord(c) < 32 or c in ':<>"|?*' for c in p)
            or p.split(".")[0].upper() in reserved
            for p in parts
        )
    ):
        raise WorkspaceError("unsafe_path", "Unsupported or unsafe file path", 422)
    return PurePosixPath(name)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path, settings: Settings) -> dict[str, dict]:
    check_path(root)
    result = {}
    seen = set()
    total = 0
    entry_count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        # Reject links even in excluded entries rather than silently following them.
        for name in dirs + files:
            check_path(Path(directory) / name)
        dirs[:] = sorted(d for d in dirs if not excluded(d))
        for name in dirs:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            safe_relative(relative)
            key = relative.casefold()
            if key in seen:
                raise WorkspaceError("path_collision", "Case-insensitive path collision", 422)
            seen.add(key)
            entry_count += 1
        for name in sorted(files):
            if excluded(name):
                continue
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            safe_relative(relative)
            key = relative.casefold()
            if key in seen:
                raise WorkspaceError("path_collision", "Case-insensitive path collision", 422)
            seen.add(key)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise WorkspaceError("unsupported_file", "Only regular files are supported", 422)
            total += info.st_size
            entry_count += 1
            if total > settings.max_bytes or entry_count > settings.max_files:
                raise WorkspaceError("copy_limit", "Project exceeds configured copy limits", 413)
            result[relative] = {
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "sha256": digest(path),
                "executable": bool(info.st_mode & 0o111),
            }
        if entry_count > settings.max_files:
            raise WorkspaceError("copy_limit", "Project exceeds configured copy limits", 413)
    return result


class LocalFilesystem:
    def __init__(self, settings: Settings):
        self.settings = settings

    def source(self, requested: Path) -> Path:
        if not requested.is_absolute():
            raise WorkspaceError("disallowed_source", "Source path must be absolute", 403)
        check_path(requested)
        path = requested.resolve()
        if not path.is_relative_to(self.settings.allowed_source_root):
            raise WorkspaceError("disallowed_source", "Source is outside allowed root", 403)
        if not path.is_dir():
            raise WorkspaceError("source_missing", "Source directory not found", 404)
        return path

    def stage(self, workspace_id: UUID) -> Path:
        return self.settings.staging_root / str(UUID(str(workspace_id)))

    def final(self, workspace_id: UUID) -> Path:
        return self.settings.workspace_root / str(UUID(str(workspace_id)))

    def copy(self, source: Path, workspace_id: UUID) -> None:
        source = self.source(source)
        before = inventory(source, self.settings)
        stage = self.stage(workspace_id)
        check_path(stage)
        stage.mkdir(exist_ok=False)
        # Preserve empty directories without changing source permissions.
        for directory, dirs, _ in os.walk(source, followlinks=False):
            dirs[:] = [d for d in dirs if not excluded(d)]
            check_path(Path(directory))
            target = stage / Path(directory).relative_to(source)
            target.mkdir(parents=True, exist_ok=True)
        for relative, entry in before.items():
            path = source / relative
            check_path(path)
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target, follow_symlinks=False)
            if digest(target) != entry["sha256"]:
                raise WorkspaceError("source_changed", "Source changed during copying")
            if entry["executable"]:
                target.chmod(target.stat().st_mode | 0o111)
        if inventory(source, self.settings) != before:
            raise WorkspaceError("source_changed", "Source changed during copying")

    def publish(self, workspace_id: UUID) -> Path:
        stage, final = self.stage(workspace_id), self.final(workspace_id)
        check_path(stage)
        check_path(final)
        if final.exists():
            raise WorkspaceError("workspace_exists", "Workspace directory already exists")
        stage.rename(final)
        return final

    def remove(self, workspace_id: UUID) -> None:
        # Caller must verify registration. UUID-derived paths never come from stored strings.
        for path in (self.stage(workspace_id), self.final(workspace_id)):
            check_path(path)
            if path.exists():
                for directory, dirs, files in os.walk(path, followlinks=False):
                    for name in dirs + files:
                        check_path(Path(directory) / name)
                shutil.rmtree(path)
        download = self.settings.staging_root / f"{UUID(str(workspace_id))}.download"
        check_path(download)
        download.unlink(missing_ok=True)
