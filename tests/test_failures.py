import hashlib
import json
import sqlite3
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.client import download
from app.domain import CreateWorkspace, Status, WorkspaceError
from app.filesystem import safe_relative
from app.main import create_app
from app.manager import WorkspaceManager
from app.snapshots import SnapshotService


def test_source_change_detected(manager, monkeypatch):
    import app.filesystem as filesystem

    original = filesystem.shutil.copyfile

    def mutate(source, target, **kwargs):
        result = original(source, target, **kwargs)
        source.write_text("changed during copy")
        return result

    monkeypatch.setattr(filesystem.shutil, "copyfile", mutate)
    with pytest.raises(WorkspaceError, match="Source changed"):
        manager.create(CreateWorkspace(name="change", source_path=manager.settings.project_path))
    assert manager.repo.list()[0].status == Status.ERROR
    assert not list(manager.settings.staging_root.iterdir())


def test_deletion_failure_is_retryable(manager, monkeypatch):
    workspace = manager.create(
        CreateWorkspace(name="delete", source_path=manager.settings.project_path)
    )
    original = manager.fs.remove

    def fail(identity):
        raise PermissionError("injected permission error")

    monkeypatch.setattr(manager.fs, "remove", fail)
    with pytest.raises(PermissionError):
        manager.action(workspace.workspace_id, "delete")
    assert manager.get(workspace.workspace_id).status == Status.ERROR
    monkeypatch.setattr(manager.fs, "remove", original)
    assert manager.action(workspace.workspace_id, "delete").status == Status.DELETED


def test_cleanup_failure_records_original_error(manager, monkeypatch):
    def copy(source, identity):
        raise OSError("original copy failure")

    def remove(identity):
        raise PermissionError("cleanup failure")

    monkeypatch.setattr(manager.fs, "copy", copy)
    monkeypatch.setattr(manager.fs, "remove", remove)
    with pytest.raises(OSError, match="original copy failure"):
        manager.create(CreateWorkspace(name="fail", source_path=manager.settings.project_path))
    row = manager.repo.list()[0]
    assert "original copy failure" in row.error and "cleanup failure" in row.error


@pytest.mark.parametrize("failure", ["disconnect", "oversized", "wrong_hash"])
def test_download_failure_cleanup(manager, failure):
    service = SnapshotService(manager)
    metadata = service.create(manager.repo.source(str(manager.settings.project_path)))
    snapshot_id = UUID(metadata["snapshot_id"])
    archive = service.archive_path(snapshot_id).read_bytes()
    if failure == "wrong_hash":
        metadata["archive_sha256"] = "0" * 64

    def serve(request):
        if not request.url.path.endswith("/download"):
            return httpx.Response(200, json=metadata)
        if failure == "disconnect":
            raise httpx.ReadError("injected disconnect")
        return httpx.Response(
            200, content=archive + b"extra" if failure == "oversized" else archive
        )

    with pytest.raises((WorkspaceError, httpx.ReadError)):
        download(
            manager,
            "http://localhost",
            snapshot_id,
            "bad",
            "token",
            transport=httpx.MockTransport(serve),
        )
    assert manager.repo.list()[0].status == Status.ERROR
    assert not list(manager.settings.staging_root.iterdir())


def test_interrupted_download_cleanup(manager):
    row = manager.reserve("partial", manager.repo.source("remote:https://example.test/project"))
    partial = manager.settings.staging_root / f"{row.workspace_id}.download"
    partial.write_bytes(b"partial download")
    settings = manager.settings
    manager.close()
    restarted = WorkspaceManager(settings)
    try:
        assert restarted.get(row.workspace_id).status == Status.ERROR
        assert not partial.exists()
    finally:
        restarted.close()


def test_snapshot_metadata_commit_failure_recovery(manager, monkeypatch):
    service = SnapshotService(manager)
    original = service.update

    def fail(metadata):
        if metadata["status"] == "READY":
            raise sqlite3.OperationalError("metadata commit failed")
        return original(metadata)

    monkeypatch.setattr(service, "update", fail)
    with pytest.raises(sqlite3.OperationalError):
        service.create(manager.repo.source(str(manager.settings.project_path)))
    assert not list(manager.settings.snapshot_root.glob("*.zip"))
    assert not list(manager.settings.staging_root.iterdir())


def test_expired_token_fails_closed(settings_factory, credentials):
    rows = json.loads(credentials.read_text())
    rows.append(
        {
            "name": "expired",
            "role": "admin",
            "projects": [],
            "token_sha256": hashlib.sha256(b"expired").hexdigest(),
            "expires_at": "2000-01-01T00:00:00Z",
        }
    )
    credentials.write_text(json.dumps(rows))
    with TestClient(create_app(settings_factory(), credentials)) as client:
        assert (
            client.get("/projects", headers={"Authorization": "Bearer expired"}).status_code == 401
        )


def test_case_collision_policy_and_entry_limit(manager):
    for name in ("a/../b", "NUL", "folder/file.", "a\x00b"):
        with pytest.raises(WorkspaceError):
            safe_relative(name)
    from app.filesystem import inventory

    settings = manager.settings.model_copy(update={"max_files": 1})
    with pytest.raises(WorkspaceError) as error:
        inventory(settings.project_path, settings)
    assert error.value.status_code == 413


def test_snapshot_survives_server_restart(settings_factory, credentials):
    settings = settings_factory()
    headers = {"Authorization": "Bearer admin-token"}
    with TestClient(create_app(settings, credentials)) as client:
        project_id = client.get("/projects", headers=headers).json()[0]["project_id"]
        snapshot = client.post(f"/projects/{project_id}/snapshots", headers=headers).json()
    with TestClient(create_app(settings, credentials)) as client:
        identity = snapshot["snapshot_id"]
        assert client.get(f"/snapshots/{identity}", headers=headers).json() == snapshot
        assert client.get(f"/snapshots/{identity}/download", headers=headers).status_code == 200
        assert client.get(f"/snapshots/{uuid4()}", headers=headers).status_code == 404
