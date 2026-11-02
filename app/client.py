"""Authenticated cloud-to-local download with verified staging and provenance."""

import json
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.domain import WorkspaceError
from app.snapshots import Snapshot, extract


def validate_server(url: str, allow_http=False) -> str:
    parsed = urlsplit(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if (
        parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or not parsed.hostname
        or parsed.scheme not in ("http", "https")
        or (parsed.scheme == "http" and not (local or allow_http))
    ):
        raise WorkspaceError(
            "invalid_server", "Use HTTPS or an explicitly trusted local HTTP server", 422
        )
    return url.rstrip("/")


def download(
    manager,
    server: str,
    snapshot_id: UUID,
    name: str,
    token: str,
    *,
    allow_http=False,
    transport=None,
    verify=True,
):
    server = validate_server(server, allow_http)
    headers = {"Authorization": f"Bearer {token.strip()}"}
    with (
        manager.lock,
        httpx.Client(
            base_url=server,
            headers=headers,
            timeout=120,
            follow_redirects=False,
            transport=transport,
            verify=verify,
            trust_env=False,
        ) as client,
    ):
        # Bound JSON metadata independently of file size.
        with client.stream("GET", f"/snapshots/{snapshot_id}") as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > min(64 * 1024 * 1024, manager.settings.max_files * 1024 + 16384):
                    raise WorkspaceError("copy_limit", "Snapshot manifest too large", 413)
        metadata = Snapshot.model_validate(json.loads(data))
        if metadata.snapshot_id != snapshot_id:
            raise WorkspaceError("snapshot_mismatch", "Server returned another snapshot", 422)
        if metadata.archive_bytes > manager.settings.max_bytes + manager.settings.max_files * 1024:
            raise WorkspaceError("copy_limit", "Archive exceeds configured limits", 413)
        source_id = manager.repo.source(f"remote:{server}/projects/{metadata.project_id}")
        workspace = manager.reserve(
            name, source_id, manager.settings.environment, snapshot_id, server
        )
        archive = manager.settings.staging_root / f"{workspace.workspace_id}.download"
        try:
            with client.stream("GET", f"/snapshots/{snapshot_id}/download") as response:
                response.raise_for_status()
                count = 0
                with archive.open("xb") as output:
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > metadata.archive_bytes:
                            raise WorkspaceError(
                                "copy_limit", "Download exceeds expected length", 413
                            )
                        output.write(chunk)
            extract(archive, metadata, manager.fs.stage(workspace.workspace_id), manager.settings)
            archive.unlink()
            return manager.finish(workspace)
        except Exception as error:
            try:
                archive.unlink(missing_ok=True)
            finally:
                manager.failed(workspace, error)
            raise
