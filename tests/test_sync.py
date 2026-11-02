from uuid import UUID

import pytest

from app.domain import WorkspaceError
from app.manager import WorkspaceManager
from app.sync import SyncService
from app.sync_client import SyncClient


def test_cached_checkout_and_noop_pull_reads_zero_files(sync_world):
    app, _, project, user, requests = sync_world
    manager, service, client = user("user1")
    first = client.checkout(project, "first")
    assert first["downloaded_files"] == 2
    requests.clear()
    second = client.checkout(project, "second")
    assert second["downloaded_files"] == 0 and second["cache_hits"] == 2
    assert not any("/objects/" in path for _, path, _ in requests)
    workspace = first["workspace"]
    before = (workspace.path / "utils.py").stat().st_mtime_ns
    requests.clear()
    pulled = client.pull(workspace.workspace_id)
    assert pulled["downloaded_files"] == pulled["files_read"] == pulled["updated_files"] == 0
    assert (workspace.path / "utils.py").stat().st_mtime_ns == before
    assert not any("/objects/" in path for _, path, _ in requests)
    assert client.status(workspace.workspace_id)["files_read"] == 0
    # Working files are independent of cached bytes and each other.
    (workspace.path / "main.py").write_text("changed")
    assert (second["workspace"].path / "main.py").read_text() != "changed"


def test_push_pull_transfers_only_changed_files_and_deletions(sync_world):
    app, _, project, user, requests = sync_world
    one, _, client1 = user("user1")
    two, _, client2 = user("user2")
    a = client1.checkout(project, "a")["workspace"]
    b = client2.checkout(project, "b")["workspace"]
    unchanged_mtime = (b.path / "utils.py").stat().st_mtime_ns
    payload = "changed locally\n"
    (a.path / "main.py").write_text(payload)
    expected_bytes = (a.path / "main.py").stat().st_size
    requests.clear()
    status = client1.status(a.workspace_id)
    assert status["modified"] == ["main.py"] and status["files_read"] == 1
    assert client1.status(a.workspace_id)["files_read"] == 0
    pushed = client1.push(a.workspace_id)
    assert pushed["uploaded_files"] == 1 and pushed["uploaded_bytes"] == expected_bytes
    assert pushed["files_read"] == 0
    assert (app.state.manager.settings.project_path / "main.py").read_text() == payload
    uploads = [(method, path, size) for method, path, size in requests if method == "PUT"]
    assert len(uploads) == 1 and uploads[0][2] == expected_bytes
    requests.clear()
    pulled = client2.pull(b.workspace_id)
    assert pulled["downloaded_files"] == pulled["updated_files"] == 1
    assert (b.path / "main.py").read_text() == payload
    assert (b.path / "utils.py").stat().st_mtime_ns == unchanged_mtime
    assert client2.status(b.workspace_id)["modified"] == []
    (a.path / "main.py").unlink()
    pushed = client1.push(a.workspace_id)
    assert pushed["uploaded_files"] == 0 and pushed["deleted"] == ["main.py"]
    pulled = client2.pull(b.workspace_id)
    assert pulled["downloaded_files"] == 0 and not (b.path / "main.py").exists()


def test_dirty_pull_and_stale_push_preserve_local_edits(sync_world):
    app, _, project, user, _ = sync_world
    _, _, client1 = user("user1")
    _, _, client2 = user("user2")
    a = client1.checkout(project, "a")["workspace"]
    b = client2.checkout(project, "b")["workspace"]
    (a.path / "main.py").write_text("user1")
    (b.path / "main.py").write_text("user2")
    client1.push(a.workspace_id)
    with pytest.raises(WorkspaceError) as error:
        client2.push(b.workspace_id)
    assert error.value.code == "stale_base"
    with pytest.raises(WorkspaceError) as error:
        client2.pull(b.workspace_id)
    assert error.value.code == "local_changes"
    assert (b.path / "main.py").read_text() == "user2"
    assert (app.state.manager.settings.project_path / "main.py").read_text() == "user1"


def test_local_cloud_edits_refresh_cache_only_for_changed_content(sync_world):
    app, _, project, user, _ = sync_world
    _, _, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    source = app.state.manager.settings.project_path
    (source / "extra.py").write_text("new")
    revision, stats = app.state.sync.head(project)
    assert stats["files_read"] == 1
    assert app.state.sync.head(project)[1]["files_read"] == 0
    pulled = client.pull(workspace.workspace_id)
    assert pulled["downloaded_files"] == 1
    assert (workspace.path / "extra.py").read_text() == "new"


def test_push_reuses_identical_content_and_handles_file_directory_changes(sync_world):
    app, _, project, user, requests = sync_world
    _, _, first = user("user1")
    _, _, second = user("user2")
    a = first.checkout(project, "a")["workspace"]
    b = second.checkout(project, "b")["workspace"]
    (a.path / "duplicate.py").write_bytes((a.path / "main.py").read_bytes())
    assert first.push(a.workspace_id)["uploaded_files"] == 0
    assert second.pull(b.workspace_id)["downloaded_files"] == 0
    (a.path / "main.py").unlink()
    (a.path / "main.py").mkdir()
    (a.path / "main.py" / "nested.py").write_text("nested")
    first.push(a.workspace_id)
    second.pull(b.workspace_id)
    assert (b.path / "main.py" / "nested.py").read_text() == "nested"


def test_adopt_existing_zip_workspace_without_redownloading(sync_world):
    app, http, project, user, requests = sync_world
    from app.client import download

    snapshot = app.state.snapshots.create(project)
    manager, service, client = user("user1")
    workspace = download(
        manager,
        "http://localhost",
        UUID(snapshot["snapshot_id"]),
        "legacy",
        "admin-token",
        transport=client.transport,
    )
    requests.clear()
    status = client.status(workspace.workspace_id)
    assert status["modified"] == []
    assert not any("/objects/" in path for _, path, _ in requests)
    (workspace.path / "main.py").write_text("legacy edit")
    result = client.push(workspace.workspace_id)
    assert result["uploaded_files"] == 1


def test_pull_rollback_on_metadata_failure(sync_world, monkeypatch):
    app, _, project, user, _ = sync_world
    manager, service, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    original = (workspace.path / "main.py").read_text()
    (app.state.manager.settings.project_path / "main.py").write_text("cloud-new")

    def fail(*args, **kwargs):
        raise OSError("metadata write failed")

    monkeypatch.setattr(client, "save", fail)
    with pytest.raises(OSError):
        client.pull(workspace.workspace_id)
    assert (workspace.path / "main.py").read_text() == original
    assert not list(service.updater.jobs.iterdir())


def test_cache_persists_across_client_restart(sync_world):
    app, _, project, user, requests = sync_world
    manager, service, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    settings, transport = manager.settings, client.transport
    manager.close()
    restarted = WorkspaceManager(settings)
    try:
        client2 = SyncClient(
            SyncService(restarted), "http://localhost", "admin-token", transport=transport
        )
        requests.clear()
        result = client2.checkout(project, "b")
        assert result["downloaded_files"] == 0
        assert client2.status(workspace.workspace_id)["files_read"] == 0
    finally:
        restarted.close()


def test_container_seed_directories_are_writable(tmp_path):
    from app.container import writable_source_directories

    source = tmp_path / "seed"
    source.mkdir()
    nested = source / "nested"
    nested.mkdir()
    nested.chmod(0o555)
    source.chmod(0o555)
    try:
        writable_source_directories(source)
        assert source.stat().st_mode & 0o200
        assert nested.stat().st_mode & 0o200
    finally:
        source.chmod(0o755)
        nested.chmod(0o755)


def test_retry_after_push_acknowledgement_failure(sync_world, monkeypatch):
    app, _, project, user, _ = sync_world
    _, _, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    (workspace.path / "main.py").write_text("new bytes")
    original = client.save

    def fail(*args, **kwargs):
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(client, "save", fail)
    with pytest.raises(OSError):
        client.push(workspace.workspace_id)
    monkeypatch.setattr(client, "save", original)
    retry = client.push(workspace.workspace_id)
    assert retry["already_current"] is True and retry["uploaded_files"] == 0
    assert client.status(workspace.workspace_id)["modified"] == []


def test_partial_pull_is_rolled_back_after_restart(sync_world):
    app, _, project, user, _ = sync_world
    manager, service, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    old = service.revision(workspace.base_revision)
    original = (workspace.path / "main.py").read_text()
    (app.state.manager.settings.project_path / "main.py").write_text("remote bytes")
    new, _ = app.state.sync.head(project)
    with client.http() as http:
        client.fetch(http, new)
    service.updater.apply(workspace.path, old, new, f"workspace-{workspace.workspace_id}")
    assert (workspace.path / "main.py").read_text() == "remote bytes"
    settings = manager.settings
    manager.close()
    restarted = WorkspaceManager(settings)
    try:
        recovered = SyncService(restarted)
        assert (workspace.path / "main.py").read_text() == original
        assert not list(recovered.updater.jobs.iterdir())
    finally:
        restarted.close()


def test_corrupt_local_cache_is_refetched(sync_world):
    app, _, project, user, _ = sync_world
    manager, service, client = user("user1")
    workspace = client.checkout(project, "a")["workspace"]
    revision = service.revision(workspace.base_revision)
    entry = revision.files["main.py"]
    service.cache.path(entry.sha256).write_bytes(b"x" * entry.size)
    result = client.checkout(project, "repaired")
    assert result["downloaded_files"] == 1
    assert (result["workspace"].path / "main.py").read_bytes() == (
        workspace.path / "main.py"
    ).read_bytes()
