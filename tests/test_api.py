import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
READER = {"Authorization": "Bearer user1-token"}


def test_api_auth_lifecycle_and_restart(settings_factory, credentials):
    settings = settings_factory()
    app = create_app(settings, credentials)
    with TestClient(app) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200
        assert "Workspace" in client.get("/openapi.json").json()["components"]["schemas"]
        assert (
            client.post(
                "/downloads", headers=ADMIN, json={"snapshot_id": str(uuid4()), "name": "   "}
            ).status_code
            == 422
        )
        assert client.get("/projects").status_code == 401
        assert (
            client.get("/projects", headers={"Authorization": "Bearer invalid"}).status_code == 401
        )
        assert client.get("/workspaces/not-uuid", headers=ADMIN).status_code == 422
        assert client.get(f"/workspaces/{uuid4()}", headers=ADMIN).status_code == 404
        body = {"name": "first", "source_path": str(settings.project_path)}
        assert client.post("/workspaces", headers=READER, json=body).status_code == 403
        response = client.post("/workspaces", headers=ADMIN, json=body)
        assert response.status_code == 201, response.text
        workspace = response.json()
        identity = workspace["workspace_id"]
        assert client.post("/workspaces", headers=ADMIN, json=body).status_code == 409
        assert client.get(f"/workspaces/{identity}", headers=READER).status_code == 404
        assert client.get("/workspaces", headers=READER).json() == []
        assert client.post(f"/workspaces/{identity}/stop", headers=ADMIN).status_code == 409
        assert (
            client.post(f"/workspaces/{identity}/start", headers=ADMIN).json()["status"] == "ACTIVE"
        )
        assert client.delete(f"/workspaces/{identity}", headers=ADMIN).status_code == 409
        assert client.post(f"/workspaces/{identity}/stop", headers=ADMIN).status_code == 200
        assert len(client.get("/workspaces?limit=1", headers=ADMIN).json()) == 1
    with TestClient(create_app(settings, credentials)) as client:
        assert client.get(f"/workspaces/{identity}", headers=ADMIN).json()["status"] == "STOPPED"
        assert client.delete(f"/workspaces/{identity}", headers=ADMIN).status_code == 204
        assert client.delete(f"/workspaces/{identity}", headers=ADMIN).status_code == 204
        assert client.get("/workspaces", headers=ADMIN).json() == []
        assert len(client.get("/workspaces?include_deleted=true", headers=ADMIN).json()) == 1


def test_snapshot_project_permissions_and_revocation(settings_factory, credentials):
    app = create_app(settings_factory(), credentials)
    with TestClient(app) as client:
        project = client.get("/projects", headers=ADMIN).json()[0]["project_id"]
        assert client.get("/projects", headers=READER).json() == []
        assert client.post(f"/projects/{project}/snapshots", headers=READER).status_code == 403
        response = client.post(f"/projects/{project}/snapshots", headers=ADMIN)
        assert response.status_code == 201, response.text
        snapshot = response.json()["snapshot_id"]
        assert client.get(f"/snapshots/{snapshot}", headers=READER).status_code == 404
        principals = json.loads(credentials.read_text())
        principals[1]["projects"] = [project]
        credentials.write_text(json.dumps(principals))
        assert len(client.get("/projects", headers=READER).json()) == 1
        assert client.get(f"/snapshots/{snapshot}", headers=READER).status_code == 200
        assert client.get(f"/snapshots/{snapshot}/download", headers=READER).status_code == 200
        assert len(client.get(f"/projects/{project}/snapshots", headers=READER).json()) == 1
        principals[1]["token_sha256"] = hashlib.sha256(b"rotated").hexdigest()
        credentials.write_text(json.dumps(principals))
        assert client.get("/projects", headers=READER).status_code == 401
        assert (
            client.post(
                "/downloads", headers=ADMIN, json={"snapshot_id": snapshot, "name": "x"}
            ).status_code
            == 409
        )


def test_reader_can_only_control_own_workspace(settings_factory, credentials):
    app = create_app(settings_factory(), credentials)
    with TestClient(app) as client:
        manager = app.state.manager
        workspace = manager.reserve(
            "owned", manager.repo.source(str(manager.settings.project_path)), owner="user1"
        )
        manager.fs.copy(manager.settings.project_path, workspace.workspace_id)
        manager.finish(workspace)
        identity = workspace.workspace_id
        assert client.get(f"/workspaces/{identity}", headers=READER).status_code == 200
        assert client.post(f"/workspaces/{identity}/start", headers=READER).status_code == 200
        assert client.post(f"/workspaces/{identity}/stop", headers=READER).status_code == 200
        assert client.delete(f"/workspaces/{identity}", headers=READER).status_code == 204


def test_project_registration_is_admin_only_and_scoped(settings_factory, credentials):
    settings = settings_factory()
    source = settings.allowed_source_root / "another"
    source.mkdir()
    payload = {"source_path": str(source)}
    with TestClient(create_app(settings, credentials)) as client:
        assert client.post("/projects", headers=READER, json=payload).status_code == 403
        response = client.post("/projects", headers=ADMIN, json=payload)
        assert response.status_code == 200 and response.json()["name"] == "another"
        assert len(client.get("/projects", headers=ADMIN).json()) == 2
        assert (
            client.post(
                "/projects", headers=ADMIN, json={"source_path": str(settings.state_root)}
            ).status_code
            == 403
        )
