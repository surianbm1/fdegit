"""Resolve generated local Docker state outside the source checkout."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime_root():
    configured = os.environ.get("AWM_RUNTIME_DIR")
    env_file = ROOT / ".env"
    if not configured and env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("AWM_RUNTIME_DIR="):
                configured = line.split("=", 1)[1].strip().strip('"')
    if configured:
        return Path(configured).resolve()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "AgentWorkspaceManager" / "docker"
