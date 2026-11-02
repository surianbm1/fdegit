import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.manager import WorkspaceManager
from app.sync import SyncService
from app.sync_client import SyncClient


@pytest.fixture
def settings_factory(tmp_path):
    def factory(name="cloud", **limits):
        root = tmp_path / name
        source = root / "sources" / "sample-app"
        source.mkdir(parents=True)
        (source / "main.py").write_text("value = 'original'\n")
        (source / "utils.py").write_text("def helper(): return 1\n")
        (source / "empty").mkdir()
        (source / ".env").write_text("SECRET=do-not-copy")
        state = root / "state"
        return Settings(
            environment=name,
            project_path=source,
            allowed_source_root=source.parent,
            state_root=state,
            workspace_root=state / "workspaces",
            staging_root=state / "staging",
            metadata_database=state / "metadata.sqlite3",
            planned_port=8000,
            **limits,
        )

    return factory


@pytest.fixture
def manager(settings_factory):
    manager = WorkspaceManager(settings_factory())
    yield manager
    manager.close()


@pytest.fixture
def credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "admin",
                    "role": "admin",
                    "projects": [],
                    "token_sha256": hashlib.sha256(b"admin-token").hexdigest(),
                },
                {
                    "name": "user1",
                    "role": "reader",
                    "projects": [],
                    "token_sha256": hashlib.sha256(b"user1-token").hexdigest(),
                },
            ]
        )
    )
    return path


@pytest.fixture
def sync_world(settings_factory, credentials):
    cloud_settings = settings_factory()
    app = create_app(cloud_settings, credentials)
    with TestClient(app) as http:
        project = app.state.manager.repo.source(str(cloud_settings.project_path))
        requests = []

        def dispatch(request):
            data = request.read()
            requests.append((request.method, request.url.path, len(data)))
            response = http.request(
                request.method,
                request.url.raw_path.decode(),
                headers=dict(request.headers),
                content=data,
            )
            return httpx.Response(
                response.status_code, headers=dict(response.headers), content=response.content
            )

        users = []

        def user(name):
            manager = WorkspaceManager(settings_factory(name))
            users.append(manager)
            service = SyncService(manager)
            client = SyncClient(
                service, "http://localhost", "admin-token", transport=httpx.MockTransport(dispatch)
            )
            return manager, service, client

        yield app, http, project, user, requests
        for manager in users:
            manager.close()
