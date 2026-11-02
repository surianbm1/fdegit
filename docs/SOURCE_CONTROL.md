# Source-control extensions

Implemented in the existing workspace manager, using the same object store,
SQLite database, project IDs and user workspaces. No replacement project or
second copy of the application is required.

The reference is Anthropic's public design article:
https://www.anthropic.com/research/redesigning-source-control
It is an architectural description, not a complete protocol or implementation
specification. This project does not claim complete Scion parity.

## What runs locally

| Capability | Implementation |
| --- | --- |
| Incremental metadata | `/delta` and `/push-delta` exchange changed entries and directory additions/deletions. Normal workspace push/pull now uses them. |
| Changeset storage | New revisions store first-parent deltas when the parent is available; initial imports/checkpoints keep a full manifest. Existing full records remain readable. |
| Server commits | `/changesets` creates an immutable revision without moving a ref or writing a working directory. |
| Graph and history | Revision retrieval, first-parent/path history, merge-base queries, file diff and UTF-8 line blame. |
| Merge and rebase | Three-way file and nonoverlapping text-line merge. Rebase replays each first-parent commit. Conflicts return 409 without modifying working files or refs. |
| Atomic refs | Named refs use compare-and-swap; one of two concurrent updates with the same expected value succeeds. |
| Landing | `/land` publishes an existing changeset whose first parent is the current project head, preserving its identity and merge parent. |
| Sparse working copies | Checkout accepts selected paths. `paths: []` creates an empty working directory. Unselected files are not downloaded or treated as deletions. |
| Explicit on-demand access | `/materialize` fetches and expands selected paths, with journaled rollback/recovery. Ordinary OS reads do not trigger downloads. |
| Directory objects | Checkout recursively requests missing immutable SHA-256 directory objects. Unchanged cached subtrees transfer no metadata bytes; trees use the same object endpoints as blobs. |
| Node cache | Each user API process serves as the node's shared workspace/cache manager. Workspaces on that node reuse objects. |
| Regional cache | A fifth Docker service serves immutable objects at localhost:8010 and fetches misses only from cloud. User metadata calls keep their existing cloud origin. |
| Cache bounds | Client cache eviction defaults to 256 MiB, pinning live baselines, unsent edits and recovery journals. Regional eviction has no pinned working copies. `AWM_CACHE_BYTES` configures each limit. |
| Filesystem notifications | `watchfiles` monitors source/workspace changes when `AWM_WATCH=1` (enabled in Docker). Notifications refresh cached status, not publish edits. |
| Object-only repositories | `/repositories` creates a repository stored as objects and metadata, without a materialized server source tree. Existing filesystem-backed projects keep updating their cloud files. |

## API usage

Authorize in http://localhost:8000/docs with the existing cloud admin token;
use the corresponding user admin token at ports 8001–8003 for workspace calls.

To create a sparse workspace on a user service:

```json
{"project_id":"<project UUID>","name":"sparse-task","paths":["src","README.md"]}
```

Send that body to `POST /checkouts`. Omit `paths` for the original full working
copy behavior. To fetch additional paths, send `{"paths":["tests"]}` to
`POST /workspaces/<workspace UUID>/materialize`. Native CLI supports the same
selection via `--paths src README.md` and `--operation materialize`.

For a server-only commit:

1. Upload new file contents to `PUT /projects/<project>/objects/<sha256>`.
2. Call `POST /projects/<project>/changesets` with:

```json
{
  "base_revision":"<revision UUID>",
  "files":{"src/main.py":{"sha256":"<hash>","size":123,"executable":false}},
  "deleted":[],
  "directories_added":[],
  "directories_deleted":[],
  "message":"Describe the change"
}
```

3. Review with `/history/<revision>`, `/diff?left=<base>&right=<new>` or
   `/blame/<revision>?path=src/main.py`.
4. Merge with `POST /merge` and `{"left":"<revision>","right":"<revision>"}`.
   Rebase uses the same body at `/rebase`: replay left onto right.
5. Move a named ref with `PUT /refs/<name>` and
   `{"expected":"<old revision>","revision_id":"<new revision>"}`. Use
   `expected: null` for creation. Refs are separate pointers, not filesystem
   project-head updates.
6. To publish to the project's head/source tree, call
   `POST /land/<revision>?expected=<current head>`. Rebase onto that head first
   if the revision's first parent is different.

All project-relative routes above start with `/projects/<project UUID>`.
`POST /repositories` takes `{"name":"api-only-project"}` and returns the
initial project/revision IDs for an object-only repository. Existing user tokens
still need a grant for a newly created project.

`GET /monitor` reports watcher state/errors. `POST /cache/prune?max_bytes=...`
on a user service reports eviction and whether pinned contents exceed the budget.
Pinned data is never discarded merely to satisfy the budget. Automatic eviction
runs after workspace save operations; large in-flight transfers can temporarily
exceed the limit. Regional grants and LRU recency are process-local; object bytes
survive container restarts. Cache misses fail if the configured upstream is down;
there is no bypass to another tier.

## Remaining differences from Scion

- SQLite and the lifecycle lock still serialize local writes. This is not a
  distributed, horizontally scalable metadata service or a lock-free commit log.
- Initial checkout uses recursive content-addressed tree negotiation and reuses
  cached directory objects. It still walks the full metadata tree for a baseline;
  sparse selection currently reduces blob transfer rather than metadata traversal.
- Tree generation, graph queries, blame and delta reconstruction use ordinary
  computation over revision data. Scion's derived indices, skip lists and
  change-proportional performance have not been implemented or benchmarked here.
- Materialization is explicit. Transparent read-triggered virtualization needs
  an OS filesystem adapter (such as FUSE/WinFsp), which is not implemented.
- Blame follows the first-parent chain; it does not trace moved/copied lines or
  preserve second-parent attribution through arbitrary merges. Binary conflicts,
  overlapping edits and ambiguous merge bases require explicit resolution.
- Rebase rejects merge commits. There is no interactive conflict-resolution UI,
  Git import/export, content-compressed authoritative storage, distributed HA,
  cache seeding snapshots, or automatic authoritative history garbage collection.
- Watch notifications schedule a stat scan of the affected workspace. Explicit
  sync still verifies metadata for correctness after missed events or restarts.
- The regional prototype buffers objects in memory while forwarding them and
  serializes requests. It does not have Scion's streaming or fleet throughput.

These are outstanding engineering items, not features completed by the local
implementation. No Scion-equivalent speed, scale or complete parity is claimed.

## Verification

Run `python -m pytest -q`, then with Docker services running:

```text
python scripts/verify_cache.py
python scripts/verify_source_control.py
```

The first script verifies actual file synchronization and container restart.
The second verifies server commits/merge/rebase/query APIs, sparse materialization,
cross-user regional cache hits and running filesystem monitors. Reports live
outside the repository under the configured runtime root.
