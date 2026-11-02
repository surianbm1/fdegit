# Implementation checklist

## Workspace preparation

- [x] Inspect the repository and preserve the validated plan.
- [x] Create documentation and application/test directory placeholders.
- [x] Preserve the original proposal as reference material.
- [x] Establish separate local source and runtime directories.
- [x] Exclude runtime data, credentials, environments and generated files from Git.

## 1. Contract and scaffold

- [x] Add pyproject.toml and a reproducible dependency setup.
- [x] Implement validated configuration with disjoint source and state roots.
- [x] Define schemas, errors, lifecycle transitions and repository interfaces.
- [x] Define the SQLite schema, normalized name uniqueness and source identity.

## 2. Safe copying

- [x] Implement canonical source validation and Windows reparse-point handling.
- [x] Implement exclusions, byte/file limits, staging and independent copies.
- [x] Verify changes and deletions in A cannot affect B or the source.

## 3. Persistence and recovery

- [x] Reserve metadata before copying; finalize after directory publication.
- [x] Implement scoped deletion, tombstones and preserved failure details.
- [x] Reconcile interrupted creation/deletion at startup.
- [x] Verify failures at database/filesystem boundaries and operation races.

## 4. API

- [x] Implement create, list, get, start, stop, delete and health routes.
- [x] Add consistent HTTP errors, pagination and OpenAPI examples.
- [x] Keep blocking filesystem operations off the async event loop.

## 5. Acceptance

- [x] Complete every correctness gate in PLAN.md.
- [x] Run the A/B isolation demonstration with lifecycle operations and restart.
- [x] Measure copy time, storage growth and cleanup for documented fixtures.
- [x] Document installation, examples, verified test results and limitations.

The local implementation is complete. See VERIFICATION.md for measured results and known limits.

## Added cloud-to-local phases

- [x] Publish immutable snapshots with manifests and SHA-256 checksums.
- [x] Add authenticated project/snapshot/download APIs.
- [x] Add bounded downloads, safe extraction and local provenance.
- [x] Verify cloud and all three users through real HTTP and container restart.
- [x] Deploy four local Docker environments with distinct volumes and credentials.
- [x] Verify a local HTTPS gateway using its CA certificate.

## Incremental cache requested next

- [x] Store immutable file objects once per environment and share across its workspaces.
- [x] Persist manifests and stat indexes; read working-file contents only when changed.
- [x] Download only missing objects for checkout/pull.
- [x] Upload only missing changed objects for push and update cloud filesystem/cache/head.
- [x] Transfer additions/deletions and reused content without unnecessary file bytes.
- [x] Adopt existing ZIP workspaces without re-downloading unchanged contents.
- [x] Recover interrupted pulls/publication and retry acknowledged-late pushes.
- [x] Verify warm-cache, one-file push/pull, deletion and restart through real Docker HTTP.

Future work: dashboard if needed, cache retention/eviction, automatic merging if needed, and a Git-worktree comparison. Internet hosting was replaced by local Docker at the user's request. See CACHE_SYNC.md for the current incremental workflow.

## Source-control extension

- [x] Incremental metadata on push/pull and delta revision storage.
- [x] Server changesets, history, diff, blame, merge/rebase and CAS refs.
- [x] Sparse checkout and explicit journaled materialization.
- [x] Immutable directory objects and object-only repository API.
- [x] Node cache pinning/eviction, regional cache and filesystem notifications.
- [ ] Distributed metadata, lock-free writes and scalable derived indices.
- [x] Recursive tree negotiation as the default checkout protocol.
- [ ] Sparse metadata traversal without a full metadata baseline.
- [ ] Transparent OS read-triggered virtualization.
- [ ] Streaming/compressed storage and fleet-scale performance verification.
