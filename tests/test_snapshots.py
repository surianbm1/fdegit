import hashlib
import stat
import zipfile
from uuid import UUID, uuid4

import httpx
import pytest

from app.client import download, validate_server
from app.domain import Status, WorkspaceError
from app.filesystem import digest
from app.manager import WorkspaceManager
from app.snapshots import FileEntry, Snapshot, SnapshotService, extract


def test_snapshot_is_immutable_and_downloads_are_independent(manager, settings_factory):
    service = SnapshotService(manager)
    project_id = manager.repo.source(str(manager.settings.project_path))
    metadata = service.create(project_id)
    identity = UUID(metadata["snapshot_id"])
    archive = service.archive_path(identity)
    original = (manager.settings.project_path / "main.py").read_text()
    (manager.settings.project_path / "main.py").write_text("cloud edit after snapshot")
    assert ".env" not in metadata["files"]
    assert digest(archive) == metadata["archive_sha256"]

    def serve(request):
        assert request.headers["authorization"] == "Bearer test-token"
        if request.url.path.endswith("/download"):
            return httpx.Response(200, content=archive.read_bytes())
        return httpx.Response(200, json=metadata)

    users = []
    try:
        for name in ("user1", "user2", "user3"):
            local = WorkspaceManager(settings_factory(name))
            users.append(local)
            workspace = download(
                local,
                "http://127.0.0.1:8000",
                identity,
                "shared",
                "test-token",
                transport=httpx.MockTransport(serve),
            )
            assert workspace.status == Status.READY and workspace.snapshot_id == identity
            assert workspace.remote_url == "http://127.0.0.1:8000"
            assert (workspace.path / "main.py").read_text() == original
            assert (workspace.path / "empty").is_dir()
        first = users[0].repo.list()[0]
        (first.path / "main.py").write_text("user1 edit")
        for user in users[1:]:
            assert (user.repo.list()[0].path / "main.py").read_text() == original
        users[0].action(first.workspace_id, "delete")
        assert all(user.repo.list()[0].path.exists() for user in users[1:])
        assert (
            manager.settings.project_path / "main.py"
        ).read_text() == "cloud edit after snapshot"
    finally:
        for user in users:
            user.close()


def hostile_archive(tmp_path, filename="main.py", payload=b"hello", mode=None, directories=None):
    path = tmp_path / "hostile.zip"
    info = zipfile.ZipInfo(filename)
    if mode:
        info.external_attr = mode << 16
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(info, payload)
    metadata = Snapshot(
        snapshot_id=uuid4(),
        project_id=uuid4(),
        created_at="now",
        archive_sha256=digest(path),
        archive_bytes=path.stat().st_size,
        files={filename: FileEntry(size=len(payload), sha256=hashlib.sha256(payload).hexdigest())},
        directories=directories or [],
    )
    return path, metadata


@pytest.mark.parametrize(
    "filename",
    ["../escape", "/absolute", "C:/escape", "a\\escape", "CON.txt", "a./file", "a:stream", ".env"],
)
def test_unsafe_archive_paths_rejected(tmp_path, manager, filename):
    archive, metadata = hostile_archive(tmp_path, filename)
    destination = manager.fs.stage(uuid4())
    with pytest.raises(WorkspaceError):
        extract(archive, metadata, destination, manager.settings)
    assert not destination.exists()


def test_symlink_and_checksum_mismatch_rejected(tmp_path, manager):
    archive, metadata = hostile_archive(tmp_path, mode=stat.S_IFLNK | 0o777)
    with pytest.raises(WorkspaceError, match="Unsupported"):
        extract(archive, metadata, manager.fs.stage(uuid4()), manager.settings)
    metadata.archive_sha256 = "0" * 64
    with pytest.raises(WorkspaceError, match="checksum"):
        extract(archive, metadata, manager.fs.stage(uuid4()), manager.settings)


def test_file_hash_and_archive_length_fail_cleanup(manager, tmp_path):
    archive, metadata = hostile_archive(tmp_path)
    metadata.files["main.py"].sha256 = "0" * 64

    def serve(request):
        return (
            httpx.Response(200, content=archive.read_bytes())
            if request.url.path.endswith("/download")
            else httpx.Response(200, json=metadata.model_dump(mode="json"))
        )

    with pytest.raises(WorkspaceError, match="File checksum"):
        download(
            manager,
            "http://localhost",
            metadata.snapshot_id,
            "bad",
            "token",
            transport=httpx.MockTransport(serve),
        )
    row = manager.repo.list()[0]
    assert row.status == Status.ERROR
    assert not list(manager.settings.staging_root.iterdir())
    assert not row.path.exists()


def test_snapshot_crash_recovery(manager):
    identity = uuid4()
    project = manager.repo.source(str(manager.settings.project_path))
    manager.repo.save_snapshot(
        {"snapshot_id": str(identity), "project_id": str(project), "status": "CREATING"}
    )
    manager.fs.stage(identity).mkdir()
    partial = manager.settings.snapshot_root / f"{identity}.part"
    partial.write_bytes(b"partial")
    service = SnapshotService(manager)
    assert manager.repo.snapshot(identity)["status"] == "ERROR"
    assert not partial.exists() and not manager.fs.stage(identity).exists()
    with pytest.raises(WorkspaceError):
        service.get(identity)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "file:///tmp",
        "https://example.com/path",
        "https://example.com?q=1",
    ],
)
def test_untrusted_origins_rejected(url):
    with pytest.raises(WorkspaceError):
        validate_server(url)


def test_extraction_limits_and_collisions(tmp_path, manager):
    archive, metadata = hostile_archive(tmp_path, directories=["MAIN.py"])
    with pytest.raises(WorkspaceError, match="Duplicate"):
        extract(archive, metadata, manager.fs.stage(uuid4()), manager.settings)
    metadata.directories = []
    settings = manager.settings.model_copy(update={"max_bytes": 1})
    with pytest.raises(WorkspaceError, match="size limit"):
        extract(archive, metadata, manager.fs.stage(uuid4()), settings)
