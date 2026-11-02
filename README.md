# Agent Workspace Manager

Cloud/local file caching with incremental push and pull, independent editable workspaces, SQLite metadata and crash recovery. All services run locally in Docker. See [cache workflow and commands](docs/CACHE_SYNC.md).

## Start

With Docker Desktop running, from this folder:

```powershell
python scripts/prepare_docker.py
docker compose up -d --build --force-recreate --wait
```

| Service | API docs | Data volume |
| --- | --- | --- |
| cloud | http://localhost:8000/docs | cloud-data |
| user1 | http://localhost:8001/docs | user1-data |
| user2 | http://localhost:8002/docs | user2-data |
| user3 | http://localhost:8003/docs | user3-data |
| HTTPS cloud | https://localhost:8443/docs | Caddy certificate volumes |

All host ports bind to loopback. Each environment has its own storage and credentials. The same small sample project is seeded into each source root; user services download cloud snapshots into their own workspaces over the private Docker network.

Generated credentials and reports live under `%LOCALAPPDATA%/AgentWorkspaceManager/docker` on Windows, outside OneDrive. Preparation writes a Git-ignored `.env` containing only this location. Existing token bytes are preserved. Each `secrets/<environment>/admin.token` authenticates to that environment. Each generated user's `cloud.token` can pull and push the cloud sample project. Use the correct token with **Authorize** in API docs. Health, documentation and API schema are public; project data requires authentication.

## Verify

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA 'AgentWorkspaceManager/development/.venv'
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check app scripts tests
uv run --locked python scripts/verify_docker.py
uv run --locked python scripts/verify_cache.py
uv run --locked python scripts/benchmark.py
```

Docker verification publishes a snapshot, downloads it for three users, edits user1, checks the other copies, tests lifecycle operations and deletion, restarts a real container, and verifies HTTPS with the local CA. Demo workspaces remain for inspection. Each verification run creates a new uniquely named set.

See [Docker operation and API examples](docs/DOCKER.md), [native development](docs/DEVELOPMENT.md), and [verification results](docs/VERIFICATION.md).

## API

| Route | Purpose |
| --- | --- |
| GET /health | Readiness including metadata-store access |
| GET /projects | Visible projects |
| GET /projects/{id}/head | Latest file manifest; refresh changed cloud-source objects |
| POST /checkouts | Create a working copy using shared local cached objects |
| GET /workspaces/{id}/status | Scan only changed file contents |
| POST /workspaces/{id}/push | Upload missing file blobs and update cloud |
| POST /workspaces/{id}/pull | Fetch missing blobs and update changed paths |
| POST /projects | Register an allowed source directory (admin) |
| POST /projects/{id}/snapshots | Publish a snapshot (admin) |
| GET /projects/{id}/snapshots | List available snapshots |
| GET /snapshots/{id} | Manifest and checksums |
| GET /snapshots/{id}/download | Immutable ZIP download |
| POST /downloads | Download from the configured cloud (admin) |
| POST /workspaces | Independent local copy (admin) |
| GET /workspaces | List with pagination and optional tombstones |
| GET /workspaces/{id} | Metadata |
| POST /workspaces/{id}/start | Mark ACTIVE |
| POST /workspaces/{id}/stop | Mark STOPPED |
| DELETE /workspaces/{id} | Remove files; retain a tombstone |

Start/stop are metadata operations and do not execute agents. Readers may manage only their own workspaces and download explicitly granted projects. Admins manage the service they authenticate to.

## Code layout

`app/config.py` validates descriptors; `domain.py` defines schemas and transitions; `filesystem.py` copies and deletes; `storage.py` owns SQLite; `manager.py` owns lifecycle; `snapshots.py` handles ZIP exports; `cache.py` stores immutable objects and scans changed files; `sync.py` maintains manifests and recoverable tree updates; `sync_client.py` implements cached checkout/status/push/pull; `main.py` exposes FastAPI. CLI/container entry points and tests live in their corresponding directories.

## Current boundaries

This is a local Docker deployment with file-level incremental synchronization. It does not provide Internet hosting, Git history, automatic merges or agent execution. Other users update on pull; no background watcher is implemented. Source directories must remain stable while copying/syncing. File blobs are deduplicated within each environment, while editable workspaces remain full independent copies. Metadata manifests are exchanged on each sync; only missing file contents are transferred. Cache/snapshot retention is manual with no eviction/garbage collector yet.

Original Windows project copies remain under `%LOCALAPPDATA%/AgentWorkspaceManager/environments`. Docker volumes are a separate deployment, not bind mounts of those copies. See [validated plan](PLAN.md) and [implementation checklist](docs/TASKS.md).

## Source-control extensions

See [docs/SOURCE_CONTROL.md](docs/SOURCE_CONTROL.md) for incremental changesets, server history/diff/blame, merge/rebase, atomic refs, sparse materialization, filesystem monitoring, regional caching and object-only repositories. That document also lists the remaining differences from Scion; complete parity is not claimed.
