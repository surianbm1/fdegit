# Incremental file cache, push and pull

Each environment now keeps immutable SHA-256 file objects under `state/objects/<prefix>/<hash>`. Its workspaces share this cache. Working files remain ordinary writable copies, so edits cannot modify the cached object or another workspace.

Cloud stores the latest project manifest in SQLite. A manifest records file paths, hashes, lengths, executable flags and directories. A version UUID identifies that manifest. Cached blobs are reused across filenames, workspaces and versions. This implementation synchronizes whole changed files, not byte ranges within a file.

## Flow

1. Checkout asks cloud for its latest manifest and downloads only blobs absent from the local cache.
2. Further checkouts of the same version reuse those blobs without downloading file contents.
3. Status checks file size, modification/change times, inode and executable flag. File contents are read only when that fingerprint changed or a required object is missing/corrupt.
4. Push asks cloud which changed objects it lacks, uploads only those, applies changed paths to the cloud filesystem, and publishes the latest manifest.
5. Another user's pull compares its baseline to the latest cloud manifest, fetches only missing blobs and updates only affected working paths. Untouched files keep their mtimes.
6. Deletions travel as metadata. A new filename containing already-cached bytes transfers no file contents.

Push immediately updates the cloud cache, source tree and latest revision. Other environments refresh their remote baseline when they pull. Filesystem notifications refresh local cached status; publication remains explicit. Push/pull exchange incremental metadata, while initial checkout recursively requests missing immutable directory objects to assemble its metadata baseline. Zero downloaded_bytes means zero file-content bytes, not zero network traffic. See [SOURCE_CONTROL.md](SOURCE_CONTROL.md) for changesets, merges, sparse copies, monitoring and tiered cache APIs.

## API steps

Use cloud GET /projects to obtain the project UUID. In a user service's API docs, authorize with that user's local admin token and execute:

```text
POST /checkouts
{"project_id":"<cloud project UUID>","name":"cached-task"}

GET /workspaces/<workspace UUID>/status
POST /workspaces/<workspace UUID>/push
POST /workspaces/<workspace UUID>/pull
```

Checkout returns a workspace plus downloaded_files, downloaded_bytes and cache_hits. Push returns added/modified/deleted paths, uploaded_files and uploaded_bytes. Pull returns changed paths, updated_files, downloaded_files and downloaded_bytes. Transfer file counts count unique blobs, not necessarily filenames. files_read and bytes_read count working/source contents scanned, not cache integrity reads. Cached objects are integrity-checked on first use per process and after their stat metadata changes.

Use READY or STOPPED workspaces for synchronization. Stop an ACTIVE workspace first. The existing start/stop operations remain bookkeeping.

## Existing local copies

Existing ZIP-downloaded workspaces automatically attach their original snapshot as a baseline on their first status/push/pull. Matching local files seed the cache directly; unchanged contents are not downloaded again. If a local file differs from that original baseline, the baseline blob may be downloaded into the cache so it remains available for comparison/recovery, without replacing local edits. Existing local-only copies without a remote origin need a cached checkout to establish their remote project.

The old ZIP endpoints remain available for export/backward compatibility. Use /checkouts for the incremental path.

## Native Windows client

With the API for the native state directory stopped, from the repository:

```powershell
$env:UV_PROJECT_ENVIRONMENT = "$env:LOCALAPPDATA/AgentWorkspaceManager/development/.venv"
$descriptor = "$env:LOCALAPPDATA/AgentWorkspaceManager/environments/user1/environment.json"
$token = "$env:LOCALAPPDATA/AgentWorkspaceManager/docker/secrets/user1/cloud.token"
uv run --locked workspace-manager --environment $descriptor --server http://localhost:8000 --token-file $token --operation checkout --project '<project UUID>' --name 'cached-task'
uv run --locked workspace-manager --environment $descriptor --server http://localhost:8000 --token-file $token --operation status --workspace '<workspace UUID>'
uv run --locked workspace-manager --environment $descriptor --server http://localhost:8000 --token-file $token --operation push --workspace '<workspace UUID>'
uv run --locked workspace-manager --environment $descriptor --server http://localhost:8000 --token-file $token --operation pull --workspace '<workspace UUID>'
```

For local HTTPS use the same origin throughout and add --ca-file as described in DEVELOPMENT.md.

## Updates and recovery

Re-run `python scripts/prepare_docker.py` and rebuild/recreate the services. It preserves generated token bytes and gives the three generated cloud users write access to their existing project grants. No additional security system is introduced. Existing readers remain read-only unless explicitly granted writer/admin role. SQLite migrates v2 to v3, adding cached manifests, latest project pointers and workspace baseline/index records.

An out-of-date push returns 409 stale_base. A pull that would overwrite local edits returns 409 local_changes. Identical already-published contents can be adopted without retransmission, including retry after a lost acknowledgement. This version does not automatically merge divergent edits. Save those edits outside the workspace or restore the affected working files to the base before pulling, then reapply them.

Only affected paths are changed. A journal plus cached old objects provides rollback on failed publication or pull. Startup resolves interrupted journals using the committed cloud/workspace version pointer. Source trees must remain stable during synchronization. Client/regional caches now support bounded eviction; authoritative history is retained. See SOURCE_CONTROL.md for limits and pinning behavior.

## Verify actual transfers

```powershell
uv run --locked python scripts/verify_cache.py
```

The script exercises three live Docker users, warm-cache checkout, no-op pull, one-file push/pull, content reuse under a new filename, deletions and restart persistence. It leaves a uniquely named demo workspace per user, and updates the sample cloud main.py with a harmless comment. Its report is at `%LOCALAPPDATA%/AgentWorkspaceManager/docker/cache-verification.json` on this machine.
