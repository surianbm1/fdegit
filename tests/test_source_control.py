import io
import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.cache_server import create_cache_app
from app.domain import WorkspaceError
from app.monitor import WorkspaceMonitor
from app.snapshots import FileEntry
from app.source_control import ChangeRequest, IntegrationRequest, RefRequest, SourceControl
from app.sync import SyncService


@pytest.fixture
def source(manager):
    sync = SyncService(manager)
    project = manager.repo.source(str(manager.settings.project_path))
    base = sync.head(project)[0]
    return SourceControl(sync), project, base


def entry(sc, text):
    digest, size = sc.cache.put(io.BytesIO(text.encode()), 1_000_000)
    return FileEntry(sha256=digest, size=size, executable=False)


def commit(sc, project, base, files, message="edit"):
    return sc.commit(
        project,
        ChangeRequest(
            base_revision=base.revision_id,
            files={n: entry(sc, text) for n, text in files.items()},
            message=message,
        ),
        "tester",
    )


def test_changesets_are_incremental_persistent_and_do_not_move_head(source):
    sc, project, base = source
    revision = commit(sc, project, base, {"main.py": "new\n"})
    assert sc.sync.head(project)[0] == base
    assert sc.sync.manager.settings.project_path.joinpath("main.py").read_text() != "new\n"
    with sc.repo.connect() as db:
        data = json.loads(
            db.execute(
                "SELECT metadata FROM sync_revisions WHERE revision_id=?",
                (str(revision.revision_id),),
            ).fetchone()[0]
        )
    assert "files" not in data and list(data["delta"]["files"]) == ["main.py"]
    assert (
        SourceControl(SyncService(sc.sync.manager)).revision(project, revision.revision_id)
        == revision
    )


def test_long_delta_chain_and_path_history(source):
    sc, project, base = source
    revision = base
    for i in range(30):
        revision = commit(sc, project, revision, {"main.py": f"value={i}\n"})
    assert sc.revision(project, revision.revision_id) == revision
    assert len(sc.history(project, revision.revision_id, "main.py", 5)) == 5
    assert len(sc.history(project, revision.revision_id, "utils.py")) == 1
    assert (
        "+value=29" in sc.diff(project, base.revision_id, revision.revision_id, "main.py")["patch"]
    )


def test_merge_disjoint_files_and_cas_land_preserves_ancestry(source):
    sc, project, base = source
    left = commit(sc, project, base, {"main.py": "left\n"})
    right = commit(sc, project, base, {"utils.py": "right\n"})
    merged = sc.integrate(
        project, IntegrationRequest(left=left.revision_id, right=right.revision_id)
    )
    assert merged.merge_parent == right.revision_id
    assert merged.files["main.py"] == left.files["main.py"]
    assert merged.files["utils.py"] == right.files["utils.py"]
    assert sc.merge_base(project, left.revision_id, right.revision_id) == base.revision_id
    sc.land(project, left.revision_id, base.revision_id)
    landed = sc.land(project, merged.revision_id, left.revision_id)
    assert landed.revision_id == merged.revision_id and landed.merge_parent == right.revision_id
    assert sc.sync.manager.settings.project_path.joinpath("utils.py").read_text() == "right\n"


def test_text_merge_nonoverlapping_and_conflict(source):
    sc, project, seed = source
    base = commit(sc, project, seed, {"main.py": "one\ntwo\nthree\n"})
    left = commit(sc, project, base, {"main.py": "ONE\ntwo\nthree\n"})
    right = commit(sc, project, base, {"main.py": "one\ntwo\nTHREE\n"})
    merged = sc.integrate(
        project, IntegrationRequest(left=left.revision_id, right=right.revision_id)
    )
    assert sc.content(merged.files["main.py"]) == b"ONE\ntwo\nTHREE\n"
    conflict = commit(sc, project, base, {"main.py": "different\ntwo\nthree\n"})
    with pytest.raises(WorkspaceError, match="Conflicting paths"):
        sc.integrate(project, IntegrationRequest(left=left.revision_id, right=conflict.revision_id))
    assert sc.sync.head(project)[0] == seed


def test_rebase_replays_each_commit(source):
    sc, project, base = source
    one = commit(sc, project, base, {"main.py": "one\n"}, "first")
    two = commit(sc, project, one, {"main.py": "two\n"}, "second")
    target = commit(sc, project, base, {"utils.py": "target\n"})
    result = sc.integrate(
        project, IntegrationRequest(left=two.revision_id, right=target.revision_id), True
    )
    parent = sc.revision(project, result.parent)
    assert parent.parent == target.revision_id
    assert parent.message == "first" and result.message == "second"
    assert result.files["utils.py"] == target.files["utils.py"]
    assert result.files["main.py"] == two.files["main.py"]


def test_blame_unchanged_lines_keep_original_revision(source):
    sc, project, seed = source
    base = commit(sc, project, seed, {"main.py": "one\ntwo\nthree\n"})
    changed = commit(sc, project, base, {"main.py": "one\nTWO\nthree\n"})
    lines = sc.blame(project, changed.revision_id, "main.py")
    assert [line["revision_id"] for line in lines] == [
        str(base.revision_id),
        str(changed.revision_id),
        str(base.revision_id),
    ]


def test_ref_compare_and_swap_one_concurrent_winner(source):
    sc, project, base = source
    sc.update_ref(project, "main", RefRequest(revision_id=base.revision_id))
    revisions = [commit(sc, project, base, {"main.py": str(i)}) for i in range(2)]

    def update(revision):
        try:
            sc.update_ref(
                project,
                "main",
                RefRequest(expected=base.revision_id, revision_id=revision.revision_id),
            )
            return True
        except WorkspaceError as error:
            assert error.code == "stale_ref"
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(update, revisions)) == [False, True]


def test_tree_objects_are_stable_and_content_addressed(source):
    sc, project, base = source
    result = sc.tree(project, base.revision_id)
    assert sc.tree(project, base.revision_id) == result
    tree = json.loads(sc.cache.path(result["tree"]).read_bytes())
    assert set(tree["files"]) == {"main.py", "utils.py"}
    assert json.loads(sc.cache.path(tree["trees"]["empty"]).read_bytes()) == {
        "files": {},
        "trees": {},
    }


def test_sparse_push_pull_materialize_and_empty_checkout(sync_world):
    app, _, project, user, _ = sync_world
    _, _, client = user("user1")
    result = client.checkout(project, "sparse", ["main.py"])
    workspace = result["workspace"]
    assert result["downloaded_files"] == 1 and not (workspace.path / "utils.py").exists()
    assert client.status(workspace.workspace_id)["deleted"] == []
    (workspace.path / "main.py").write_text("sparse edit")
    client.push(workspace.workspace_id)
    source = app.state.manager.settings.project_path
    assert (source / "utils.py").exists()
    (source / "utils.py").write_text("cloud update")
    pulled = client.pull(workspace.workspace_id)
    assert pulled["downloaded_files"] == 0 and not (workspace.path / "utils.py").exists()
    materialized = client.materialize(workspace.workspace_id, ["utils.py"])
    assert materialized["materialized_files"] == 1
    assert (workspace.path / "utils.py").read_text() == "cloud update"
    empty = client.checkout(project, "empty-lazy", [])
    assert empty["downloaded_files"] == 0 and not list(empty["workspace"].path.iterdir())


def test_delta_wire_contains_only_changed_file(sync_world):
    app, http, project, user, _ = sync_world
    _, _, client = user("user1")
    workspace = client.checkout(project, "delta")["workspace"]
    base = workspace.base_revision
    (workspace.path / "main.py").write_text("delta bytes")
    client.push(workspace.workspace_id)
    response = http.get(
        f"/projects/{project}/delta?base={base}", headers={"Authorization": "Bearer admin-token"}
    )
    assert response.status_code == 200
    assert list(response.json()["files"]) == ["main.py"]
    assert "directories" not in response.json()


def test_prune_preserves_unsent_edits_and_rollback_baseline(sync_world):
    _, _, project, user, _ = sync_world
    _, service, client = user("user1")
    workspace = client.checkout(project, "pin")["workspace"]
    (workspace.path / "main.py").write_text("unsent edit")
    client.status(workspace.workspace_id)
    orphan, _ = service.cache.put(io.BytesIO(b"unused cache object"), 100)
    result = client.prune_cache(0)
    assert result["over_limit_pinned"] and not service.cache.has(orphan)
    assert client.status(workspace.workspace_id)["files_read"] == 0
    assert client.push(workspace.workspace_id)["uploaded_files"] == 1


def test_monitor_refreshes_cloud_and_dirty_workspace(sync_world):
    app, _, project, user, _ = sync_world
    _, service, client = user("user1")
    workspace = client.checkout(project, "watch")["workspace"]
    path = workspace.path / "main.py"
    path.write_text("watch edit")
    WorkspaceMonitor(service, lambda: client).refresh([str(path)])
    assert client.status(workspace.workspace_id)["files_read"] == 0
    source = app.state.manager.settings.project_path / "main.py"
    source.write_text("cloud watch edit")
    WorkspaceMonitor(app.state.sync).refresh([str(source)])
    assert app.state.sync.head(project)[1]["files_read"] == 0


def test_cache_tier_hits_eviction_and_upstream_failure(tmp_path):
    import hashlib

    payload = b"shared object"
    digest = hashlib.sha256(payload).hexdigest()
    calls = []

    def origin(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=payload)

    app = create_cache_app("http://cloud", tmp_path / "cache", 1000, httpx.MockTransport(origin))
    headers = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        path = f"/projects/example/objects/{digest}"
        assert client.get(path, headers=headers).content == payload
        assert client.get(path, headers=headers).content == payload
        assert len(calls) == 1 and client.get("/health").json()["hits"] == 1
        app.state.cache.prune(0)
        assert client.get(path, headers=headers).content == payload
        assert len(calls) == 2


def test_source_api_commit_history_and_auth(sync_world):
    app, http, project, _, _ = sync_world
    headers = {"Authorization": "Bearer admin-token"}
    base = app.state.sync.head(project)[0]
    sc = SourceControl(app.state.sync)
    blob = entry(sc, "API write\n")
    response = http.post(
        f"/projects/{project}/changesets",
        headers=headers,
        json={"base_revision": str(base.revision_id), "files": {"main.py": blob.model_dump()}},
    )
    assert response.status_code == 201
    revision = UUID(response.json()["revision_id"])
    assert http.get(f"/projects/{project}/history/{revision}", headers=headers).status_code == 200
    assert (
        http.get(f"/projects/{project}/blame/{revision}?path=main.py", headers=headers).status_code
        == 200
    )
    assert http.get(f"/projects/{project}/refs").status_code == 401


def test_object_only_repository_never_materializes_a_server_tree(source):
    sc, _, _ = source
    created = sc.create_repository("api-only")
    project = created["project_id"]
    base = sc.sync.head(project)[0]
    revision = commit(sc, project, base, {"nested/file.txt": "object only\n"})
    assert "nested" in revision.directories
    sc.land(project, revision.revision_id, base.revision_id)
    assert sc.sync.head(project)[0] == revision
    assert not sc.repo.project_path(project).exists()
    tree = sc.tree(project, revision.revision_id)
    assert "nested" in json.loads(sc.cache.path(tree["tree"]).read_bytes())["trees"]


def test_sparse_expansion_rolls_back_on_metadata_failure(sync_world, monkeypatch):
    _, _, project, user, _ = sync_world
    _, _, client = user("user1")
    workspace = client.checkout(project, "expand-fail", ["main.py"])["workspace"]
    (workspace.path / "main.py").write_text("keep unsent edit")

    def fail(*args, **kwargs):
        raise OSError("metadata failure")

    monkeypatch.setattr(client, "scan_cache", fail)
    with pytest.raises(OSError):
        client.materialize(workspace.workspace_id, ["utils.py"])
    assert not (workspace.path / "utils.py").exists()
    assert client.selected(workspace.workspace_id) == ["main.py"]
    assert (workspace.path / "main.py").read_text() == "keep unsent edit"


def test_merge_delete_modify_conflict_and_binary_conflict(source):
    sc, project, base = source
    deleted = sc.commit(project, ChangeRequest(base_revision=base.revision_id, deleted=["main.py"]))
    edited = commit(sc, project, base, {"main.py": "changed"})
    with pytest.raises(WorkspaceError, match="Conflicting paths"):
        sc.integrate(
            project, IntegrationRequest(left=deleted.revision_id, right=edited.revision_id)
        )
    raw, size = sc.cache.put(io.BytesIO(b"\xff\xfe"), 10)
    binary = sc.commit(
        project,
        ChangeRequest(
            base_revision=base.revision_id,
            files={"main.py": FileEntry(sha256=raw, size=size, executable=False)},
        ),
    )
    with pytest.raises(WorkspaceError, match="Conflicting paths"):
        sc.integrate(project, IntegrationRequest(left=binary.revision_id, right=edited.revision_id))


def test_sparse_expansion_restart_recovers_uncommitted_journal(sync_world):
    _, _, project, user, _ = sync_world
    _, service, client = user("user1")
    workspace = client.checkout(project, "expand-crash", ["main.py"])["workspace"]
    base = service.revision(workspace.base_revision)
    old, new = client.project(base, ["main.py"]), client.project(base, ["main.py", "utils.py"])
    with client.http() as http:
        client.fetch(http, new, store=False)
    service.updater.apply(
        workspace.path, old, new, f"expand-{workspace.workspace_id}", marker=["main.py", "utils.py"]
    )
    assert (workspace.path / "utils.py").exists()
    service.recover()
    assert not (workspace.path / "utils.py").exists()


def test_cache_tier_reuses_content_across_users(tmp_path):
    import hashlib

    payload = b"cross user cache"
    digest = hashlib.sha256(payload).hexdigest()
    calls = []

    def origin(request):
        calls.append(request.url.path)
        if request.url.path == "/projects":
            return httpx.Response(200, json=[{"project_id": "example"}])
        return httpx.Response(200, content=payload)

    app = create_cache_app("http://cloud", tmp_path / "tier", 1000, httpx.MockTransport(origin))
    with TestClient(app) as client:
        for token in ("one", "two", "three"):
            response = client.get(
                f"/projects/example/objects/{digest}", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.content == payload
        assert sum("/objects/" in path for path in calls) == 1
        assert client.get("/health").json()["hits"] == 2


def test_cache_tier_does_not_bypass_failed_upstream(tmp_path):
    def fail(request):
        raise httpx.ConnectError("offline", request=request)

    app = create_cache_app(
        "http://cloud", tmp_path / "offline", transport=httpx.MockTransport(fail)
    )
    with TestClient(app) as client:
        response = client.get(
            "/projects/p/objects/" + "a" * 64, headers={"Authorization": "Bearer test"}
        )
        assert response.status_code == 502


def test_checkout_fetches_only_missing_directory_objects(sync_world):
    app, _, project, user, requests = sync_world
    root = app.state.manager.settings.project_path
    for name in ("one", "two"):
        (root / name).mkdir()
        (root / name / "file.txt").write_text(name)
    _, _, client = user("user1")
    first = client.checkout(project, "tree-first")
    assert first["downloaded_trees"] == 4  # root, empty, one, two
    requests.clear()
    warm = client.checkout(project, "tree-warm")
    assert warm["downloaded_trees"] == warm["metadata_bytes"] == 0
    assert not any(path.endswith("/head") or "/objects/" in path for _, path, _ in requests)
    (root / "one" / "file.txt").write_text("updated one")
    changed = client.checkout(project, "tree-changed")
    assert changed["downloaded_trees"] == 2  # root and one; unchanged subtrees reused
    assert changed["downloaded_files"] == 1
