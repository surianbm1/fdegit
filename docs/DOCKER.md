# Local Docker operation

The recommended flow now uses /checkouts, /status, /push and /pull with shared file objects. See CACHE_SYNC.md. The full ZIP snapshot commands below remain supported for export and backwards compatibility. Generated cloud user tokens now have project writer grants so they can push; existing token bytes are preserved by prepare_docker.py.

## Service control

```powershell
python scripts/prepare_docker.py
docker compose up -d --build --force-recreate --wait
docker compose ps
docker compose logs --tail 30 cloud
docker compose stop
docker compose start
```

`docker compose down` removes containers/network while preserving named volumes. Rebuild images after code changes; volumes retain edits, metadata and snapshots. Only cloud builds the shared image, avoiding a race among four services publishing the same image tag.

Application containers run as non-root users with read-only image filesystems, private writable data volumes, dropped capabilities and no privilege escalation. User services mount only their own credential hashes and cloud reader token. Host admin tokens are not mounted.

## Publish and download

From PowerShell in the repository:

```powershell
$runtime = Join-Path $env:LOCALAPPDATA 'AgentWorkspaceManager/docker'
$cloudHeaders = @{ Authorization = 'Bearer ' + (Get-Content -Raw "$runtime/secrets/cloud/admin.token").Trim() }
$userHeaders = @{ Authorization = 'Bearer ' + (Get-Content -Raw "$runtime/secrets/user1/admin.token").Trim() }
$projects = Invoke-RestMethod 'http://localhost:8000/projects' -Headers $cloudHeaders
$projectId = $projects[0].project_id
$snapshot = Invoke-RestMethod -Method Post "http://localhost:8000/projects/$projectId/snapshots" -Headers $cloudHeaders
$body = @{ snapshot_id = $snapshot.snapshot_id; name = 'my-task' } | ConvertTo-Json
$workspace = Invoke-RestMethod -Method Post 'http://localhost:8001/downloads' -Headers $userHeaders -ContentType 'application/json' -Body $body
$workspace
```

Response contains workspace_id, source_id, name, READY status, a UUID path beneath `/data/state/workspaces`, snapshot_id, remote_url, owner and UTC timestamps. base_revision remains null. Repeat with user2/user3 tokens and ports 8002/8003 for independent copies. The server-side `/downloads` request cannot choose an arbitrary URL: it uses the configured cloud origin and token.

```powershell
$id = $workspace.workspace_id
Invoke-RestMethod -Method Post "http://localhost:8001/workspaces/$id/start" -Headers $userHeaders
Invoke-RestMethod -Method Post "http://localhost:8001/workspaces/$id/stop" -Headers $userHeaders
docker compose cp "user1:$($workspace.path)" ./downloaded-workspace
```

The last command exports an independent host folder. Edits to the exported folder do not automatically update its container counterpart. DELETE a stopped workspace's API route to remove its managed files. Repeated deletion succeeds. Names are unique after Unicode normalization/case folding among non-deleted records.

Error responses contain code and message. Invalid transitions/duplicate names return 409, disallowed paths 403, malformed requests 422, and unknown resources 404. Failed operations retain error metadata.

To add another cloud project, copy it beneath `/data/sources` and POST `/projects` with `{"source_path":"/data/sources/project-name"}` as cloud admin. Add its UUID to reader grants before offering it to users. The default sample grant does not authorize every project.

## HTTPS and credentials

The gateway serves `https://localhost:8443` using a local CA. Export its public root certificate:

```powershell
docker compose cp gateway:/data/caddy/pki/authorities/local/root.crt "$runtime/local-ca.crt"
```

Verification trusts this certificate explicitly without disabling verification or modifying the system trust store. Browsers may show a certificate warning until you choose to trust this local CA. Native downloads accept `--ca-file`. User containers use explicitly trusted HTTP on the private bridge; host HTTP ports are loopback-only. This is a local testing deployment, not an Internet-facing configuration.

Server credential JSON contains SHA-256 digests, principal names, roles, project grants and optional ISO-8601 expiry timestamps. Authentication compares digests in constant time. JSON changes revoke tokens without a server restart. To rotate, generate a new high-entropy token and replace its corresponding digest; update the mounted user's cloud.token for cloud readers. Plain tokens live outside the checkout; do not print them to shared logs. Preserve credentials when backing up data volumes.

## Recovery

Schema v1/v2 migrate to v3 for ownership, provenance, snapshots, cached manifests and workspace sync indexes. One process holds an advisory lock per state root and serializes lifecycle mutations. Startup cleans registered interrupted copies/downloads, retries interrupted deletion, resolves sync journals and records failures. Unknown folders are never adopted/deleted automatically. Cleanup failures preserve details. No automatic cache retention policy is implemented.
