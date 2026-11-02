"""Publish a native environment's current project with one command."""

import json
import os
from pathlib import Path

from app.cache import scan
from app.config import Settings
from app.manager import WorkspaceManager
from app.source_control import delta_response
from app.sync import Revision, SyncService
from app.sync_client import SyncClient


def upload(client, http, project, files):
    entries = {entry.sha256: entry for entry in files.values()}
    missing = client.response(
        http.post(f"/projects/{project}/objects/missing", json={"hashes": list(entries)})
    )["missing"]
    for identity in missing:
        with client.service.cache.path(identity).open("rb") as stream:
            client.response(http.put(f"/projects/{project}/objects/{identity}", content=stream))
    return len(missing)


def publish(folder):
    root = Path(os.environ["LOCALAPPDATA"]) / "AgentWorkspaceManager"
    environments = root / "environments"
    selected = None
    for name in ("user1", "user2", "user3"):
        settings = Settings.load(environments / name / "environment.json")
        if folder.resolve() == settings.project_path.resolve():
            selected = settings
    if selected is None:
        raise ValueError("Run publish from the user1, user2 or user3 project folder")
    cloud_settings = Settings.load(environments / "cloud" / "environment.json")
    token = (root / "docker" / "secrets" / "cloud" / "admin.token").read_text().strip()
    cloud = WorkspaceManager(cloud_settings)
    local = None
    try:
        # Serialize first-time linking across users. This creates a dedicated filegit
        # repository; the Docker sample-app project is never used as the target.
        service = SyncService(cloud)
        client = SyncClient(service, "http://localhost:8000", token)
        link = cloud_settings.state_root / "publish-project.json"
        with client.http() as http:
            if link.exists():
                project = json.loads(link.read_text())
            else:
                created = client.response(http.post("/repositories", json={"name": "filegit"}))
                base = client.manifest(http, f"/projects/{created['project_id']}/head")
                files, dirs, _, _ = scan(cloud_settings.project_path, service.cache, cloud_settings)
                seed = Revision.model_validate(
                    {**base.model_dump(), "files": files, "directories": dirs}
                )
                upload(client, http, base.project_id, seed.files)
                delta = delta_response(base, seed)
                payload = {
                    k: delta[k]
                    for k in (
                        "base_revision",
                        "files",
                        "deleted",
                        "directories_added",
                        "directories_deleted",
                    )
                }
                client.response(http.post(f"/projects/{base.project_id}/push-delta", json=payload))
                seed = client.manifest(http, f"/projects/{base.project_id}/head")
                project = {"project_id": str(base.project_id), "seed": seed.model_dump(mode="json")}
                temporary = link.with_suffix(".tmp")
                temporary.write_text(json.dumps(project))
                temporary.replace(link)
        local = WorkspaceManager(selected)
        service = SyncService(local)
        client = SyncClient(service, "http://localhost:8000", token)
        state = selected.state_root / "publish-baseline.json"
        base = (
            Revision.model_validate_json(state.read_text())
            if state.exists()
            else Revision.model_validate(project["seed"])
        )
        with client.http() as http:
            files, dirs, _, _ = scan(selected.project_path, service.cache, selected)
            current = Revision.model_validate(
                {**base.model_dump(), "files": files, "directories": dirs}
            )
            head = client.manifest(http, f"/projects/{base.project_id}/head")
            if head.files == current.files and head.directories == current.directories:
                result, count = head, 0
            else:
                if head.revision_id != base.revision_id:
                    raise ValueError(
                        "Cloud changed since your baseline. Integrate cloud changes before publishing; nothing was overwritten."
                    )
                delta = delta_response(base, current)
                count = upload(
                    client, http, base.project_id, {n: current.files[n] for n in delta["files"]}
                )
                payload = {
                    k: delta[k]
                    for k in (
                        "base_revision",
                        "files",
                        "deleted",
                        "directories_added",
                        "directories_deleted",
                    )
                }
                client.response(http.post(f"/projects/{base.project_id}/push-delta", json=payload))
                result = client.manifest(http, f"/projects/{base.project_id}/head")
            temporary = state.with_suffix(".tmp")
            temporary.write_text(result.model_dump_json())
            temporary.replace(state)
            print(
                f"Published {selected.environment} to cloud filegit: {count} file objects uploaded."
            )
            print(f"Revision: {result.revision_id}")
    finally:
        if local:
            local.close()
        cloud.close()


if __name__ == "__main__":
    try:
        publish(Path.cwd())
    except Exception as error:
        raise SystemExit(f"Publish failed: {error}") from error
