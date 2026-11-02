"""Seed a private container data volume and run its local environment API."""

import os
import shutil
from pathlib import Path

import uvicorn

from app.config import Settings
from app.filesystem import check_path
from app.main import create_app


def writable_source_directories(source):
    # copytree copies image directory modes, which can be read-only on Windows build contexts.
    for directory, _, _ in os.walk(source, followlinks=False):
        path = Path(directory)
        check_path(path)
        path.chmod(path.stat().st_mode | 0o700)


def main():
    root = Path("/data")
    source = root / "sources" / "sample-app"
    if not source.exists():
        shutil.copytree(Path("/opt/awm/examples/sample-app"), source)
    writable_source_directories(source)
    state = root / "state"
    settings = Settings(
        environment=os.environ["AWM_NAME"],
        project_path=source,
        allowed_source_root=source.parent,
        state_root=state,
        workspace_root=state / "workspaces",
        staging_root=state / "staging",
        metadata_database=state / "metadata.sqlite3",
        planned_port=8000,
    )
    api = create_app(
        settings,
        Path("/run/secrets/credentials.json"),
        cloud_url=os.environ.get("AWM_CLOUD_URL"),
        cloud_token_file=Path("/run/secrets/cloud.token")
        if os.environ.get("AWM_CLOUD_URL")
        else None,
        allow_http=True,
    )
    uvicorn.run(api, host="0.0.0.0", port=8000, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
