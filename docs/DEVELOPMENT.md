# Native development

From the repository:

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA 'AgentWorkspaceManager/development/.venv'
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check app scripts tests
```

uv.lock records dependency versions. The shared development virtual environment is outside OneDrive; use one source checkout at a time with it. HTTPX is a runtime dependency for cloud downloads. Current Starlette emits a TestClient deprecation warning for HTTPX; tests still pass. HTTPX2 migration is a dependency-maintenance follow-up.

## Native setup and server

```powershell
$descriptor = "$env:LOCALAPPDATA/AgentWorkspaceManager/environments/user1/environment.json"
$runtime = "$env:LOCALAPPDATA/AgentWorkspaceManager/docker"
uv run --locked workspace-manager --environment $descriptor
uv run --locked workspace-manager --environment $descriptor --initialize
uv run --locked workspace-manager --environment $descriptor --serve --credentials "$runtime/secrets/user1/credentials.json"
```

The legacy application_configured field is informational. Without --serve the setup command does not start an API. Do not run two processes against one native state root. Docker uses separate named volumes. If Docker occupies the native descriptor's port, stop that container or select another port in a separate descriptor.

For a native server to support POST /downloads, set AWM_CLOUD_URL and AWM_CLOUD_TOKEN_FILE before startup. Non-loopback HTTP additionally requires AWM_ALLOW_HTTP=1.

## Native download

With the API for that native state directory stopped:

```powershell
uv run --locked workspace-manager --environment $descriptor --server http://localhost:8000 --snapshot '<snapshot UUID>' --name 'native-task' --token-file "$runtime/secrets/user1/cloud.token"
```

For local HTTPS use `--server https://localhost:8443 --ca-file "$runtime/local-ca.crt"`. A running API owns its state root; use its /downloads route instead of a competing native client for the same state.

## Full checks

```powershell
uv run --locked python scripts/benchmark.py
uv run --locked python scripts/verify_docker.py
```

Benchmarks create temporary fixtures and measure serialized creation with 1, 5 and 20 coexisting copies, not parallel throughput. Verification exercises HTTP, TLS, named volumes and container restart. JSON reports contain identifiers/results without credentials.

References: [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/), [Python ZIP handling](https://docs.python.org/3/library/zipfile.html), [SQLite partial unique indexes](https://www.sqlite.org/partialindex.html).
