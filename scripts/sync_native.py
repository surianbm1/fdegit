"""Refresh generated native project copies, backing up every changed existing file."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.filesystem import check_path, digest
from app.manager import WorkspaceManager


def main():
    source = Path(__file__).resolve().parents[1]
    base = Path(os.environ["LOCALAPPDATA"]) / "AgentWorkspaceManager" / "environments"
    files = [
        source / name
        for name in (
            "README.md",
            "PLAN.md",
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
            ".dockerignore",
            "Dockerfile",
            "compose.yaml",
        )
    ]
    for pattern in (
        "app/*.py",
        "app/*.md",
        "tests/*.py",
        "tests/*.md",
        "docs/*.md",
        "scripts/*.py",
        "examples/sample-app/*",
        "deploy/Caddyfile",
    ):
        files.extend(p for p in source.glob(pattern) if p.is_file())
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    summary = []
    for name in ("cloud", "user1", "user2", "user3"):
        environment = base / name
        settings = Settings.load(environment / "environment.json")
        destination = settings.project_path
        if destination != (environment / "sources" / "filegit").resolve():
            raise ValueError("Unexpected native project destination")
        backup = environment / "backups" / stamp
        copied = 0
        for original in files:
            relative = original.relative_to(source)
            target = destination / relative
            check_path(target)
            if target.exists():
                if digest(original) == digest(target):
                    continue
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, target)
            assert digest(original) == digest(target)
            copied += 1
        manager = WorkspaceManager(settings)
        manager.close()
        summary.append(
            {
                "environment": name,
                "updated_files": copied,
                "project": str(destination),
                "backup": str(backup) if backup.exists() else None,
            }
        )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
