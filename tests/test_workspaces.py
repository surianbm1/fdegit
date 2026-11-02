import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.domain import CreateWorkspace, Status, WorkspaceError
from app.manager import WorkspaceManager


def request(manager, name="a"):
    return CreateWorkspace(name=name, source_path=manager.settings.project_path)


def test_isolation_lifecycle_deletion_and_persistence(manager):
    source = manager.settings.project_path
    a = manager.create(request(manager))
    b = manager.create(request(manager, "b"))
    assert a.source_id == b.source_id
    assert not (a.path / ".env").exists()
    assert (a.path / "empty").is_dir()
    (a.path / "main.py").write_text("changed")
    (a.path / "utils.py").unlink()
    (a.path / "new.txt").write_text("new")
    assert (b.path / "main.py").read_text() == (source / "main.py").read_text()
    assert (b.path / "utils.py").is_file() and (source / "utils.py").is_file()
    manager.action(a.workspace_id, "start")
    manager.action(a.workspace_id, "stop")
    manager.action(a.workspace_id, "start")
    with pytest.raises(WorkspaceError, match="Cannot delete"):
        manager.action(a.workspace_id, "delete")
    manager.action(a.workspace_id, "stop")
    settings = manager.settings
    manager.close()
    restarted = WorkspaceManager(settings)
    try:
        assert restarted.get(a.workspace_id).status == Status.STOPPED
        assert (a.path / "main.py").read_text() == "changed"
        restarted.action(a.workspace_id, "delete")
        restarted.action(a.workspace_id, "delete")
        assert not a.path.exists() and b.path.exists() and source.exists()
    finally:
        restarted.close()


def test_process_lock_and_unknown_delete(manager):
    with pytest.raises(WorkspaceError, match="Another process"):
        WorkspaceManager(manager.settings)
    unknown = uuid4()
    foreign = manager.fs.final(unknown)
    foreign.mkdir()
    with pytest.raises(WorkspaceError):
        manager.action(unknown, "delete")
    assert foreign.exists()


def test_duplicate_name_race(manager):
    def create(_):
        try:
            return manager.create(request(manager, "same")).status
        except WorkspaceError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, range(2)))
    assert sorted(results) == sorted([Status.READY, "duplicate_name"])


@pytest.mark.parametrize("kind", ["outside", "prefix", "missing", "traversal"])
def test_invalid_sources(manager, tmp_path, kind):
    source = manager.settings.project_path
    candidate = {
        "outside": tmp_path,
        "prefix": source.parent.with_name(source.parent.name + "-other"),
        "missing": source / "missing",
        "traversal": source / ".." / ".." / "..",
    }[kind]
    with pytest.raises(WorkspaceError):
        manager.create(CreateWorkspace(name="invalid", source_path=candidate))


def test_limits_and_copy_failure_cleanup(settings_factory, monkeypatch):
    settings = settings_factory(max_bytes=1)
    manager = WorkspaceManager(settings)
    try:
        with pytest.raises(WorkspaceError) as error:
            manager.create(request(manager))
        assert error.value.code == "copy_limit"
        row = manager.repo.list()[0]
        assert row.status == Status.ERROR
        assert not row.path.exists() and not manager.fs.stage(row.workspace_id).exists()

        def fail(source, identity):
            manager.fs.stage(identity).mkdir()
            raise OSError("disk full")

        monkeypatch.setattr(manager.fs, "copy", fail)
        with pytest.raises(OSError, match="disk full"):
            manager.create(request(manager, "failure"))
        assert all(w.status == Status.ERROR for w in manager.repo.list())
        assert not list(settings.staging_root.iterdir())
    finally:
        manager.close()


def test_recover_interrupted_create_delete_and_missing(manager):
    creating = manager.reserve(
        "interrupted", manager.repo.source(str(manager.settings.project_path))
    )
    manager.fs.copy(manager.settings.project_path, creating.workspace_id)
    manager.fs.publish(creating.workspace_id)  # Crash after rename, before metadata finalization.
    deleting = manager.create(request(manager, "deleting"))
    manager.repo.set_status(deleting.workspace_id, Status.READY, Status.DELETING)
    missing = manager.create(request(manager, "missing"))
    manager.fs.remove(missing.workspace_id)
    settings = manager.settings
    manager.close()
    restarted = WorkspaceManager(settings)
    try:
        assert restarted.get(creating.workspace_id).status == Status.ERROR
        assert not creating.path.exists()
        assert restarted.get(deleting.workspace_id).status == Status.DELETED
        assert restarted.get(missing.workspace_id).status == Status.ERROR
    finally:
        restarted.close()


def test_database_finalize_failure_leaves_recoverable_record(manager, monkeypatch):
    original = manager.repo.set_status

    def fail(*args, **kwargs):
        if args[2] == Status.READY:
            raise sqlite3.OperationalError("injected commit failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(manager.repo, "set_status", fail)
    with pytest.raises(sqlite3.OperationalError):
        manager.create(request(manager))
    row = manager.repo.list()[0]
    assert row.status == Status.ERROR and not row.path.exists()


def make_link(link, target):
    if __import__("os").name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(target, target_is_directory=True)


def test_junction_copy_and_delete_never_touch_target(manager, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep.txt").write_text("keep")
    link = manager.settings.project_path / "escape"
    make_link(link, external)
    with pytest.raises(WorkspaceError, match="Links"):
        manager.create(request(manager))
    link.unlink() if link.is_symlink() else link.rmdir()
    a = manager.create(request(manager, "safe"))
    link = a.path / "escape"
    make_link(link, external)
    with pytest.raises(WorkspaceError, match="Links"):
        manager.action(a.workspace_id, "delete")
    assert (external / "keep.txt").read_text() == "keep"
    link.unlink() if link.is_symlink() else link.rmdir()
    manager.action(a.workspace_id, "delete")
