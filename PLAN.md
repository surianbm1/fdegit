# Agent Workspace Manager: validation and implementation plan

Status: implemented October 29, 2026, including the subsequently requested cloud-to-local snapshot workflow and local Docker deployment. See docs/VERIFICATION.md for evidence and limits. This document preserves the original design baseline.

## Assessment

Proceed with a local workspace manager MVP. Its initial value is repeatable provisioning, independent editable files, persistent lifecycle metadata, and reliable cleanup for multiple coding tasks. Product demand and performance benefits remain hypotheses to measure.

Anthropic's October 13, 2026 article describes Scion as its internal source-control system for an unusually large agent workload. Its design emphasizes server-side source-control operations, immutable content-addressed data, and sparse or on-demand working copies. Those ideas motivate the longer-term direction; ordinary full directory copies do not validate those scaling benefits.

Source: https://www.anthropic.com/research/redesigning-source-control

## Phase 1 scope

Keep the proposed Python 3.12+, FastAPI, Pydantic, SQLite, local filesystem and pytest stack. Implement the seven proposed API routes. No agent execution, revision history, merges, cloud storage, or dashboard in this phase.

Use the existing repository root rather than nesting another repository. Separate API schemas, lifecycle service, metadata repository, and workspace materializer. The materializer owns local file creation and deletion. A future object store supplies immutable content; it cannot directly replace the writable filesystem exposed to agents.

## Decisions to settle in the implementation contract

1. Isolation means independently copied file contents. Never create hard links between workspaces or their source. This is not an execution sandbox: processes using the same OS account can still reach other directories. Future untrusted execution needs a separate boundary.
2. Run as a trusted local, single-user service on loopback with one application process. Serialize lifecycle mutations initially. Do not claim protection against malicious same-user processes racing filesystem operations. Future multi-process serving needs cross-process coordination.
3. Require disjoint allowed-source and storage roots: neither root may contain the other. On Windows, reject symlinks, junctions, and other reparse points in traversed source paths and entries. Inspect workspace paths again before deletion; never follow links introduced after creation.
4. Use generated UUIDs for physical directory names. Metadata paths are derived from the configured root and UUID, never supplied as deletion targets by clients.
5. Generate a stable source_id through a small source registry keyed by canonical local path. Keep base_revision null; a path identifies a source location, not a reproducible version.
6. Require unique normalized workspace names among non-deleted records, enforced by SQLite. Return 409 for duplicates. Preserve the original display name.
7. Exclude .git, .env and .env.*, known private-key filenames, virtual environments, dependency directories, and generated caches by default. Explicitly allow example environment templates if desired. Document that filename exclusions are not a comprehensive secret detector. Record the applied copy policy.
8. Source contents must remain stable while copying. A full directory copy is not an atomic snapshot; document this limitation and reject detectable concurrent modifications. Do not promise reproducible bases until snapshot support exists.
9. Keep runtime files and SQLite outside the OneDrive-synced code repository, for example under a configurable local application-data directory.

## Lifecycle and recovery

Use CREATING, READY, ACTIVE, STOPPED, DELETING, DELETED, and ERROR states. Retain tombstone metadata after successful deletion, omit it from normal lists, and allow an explicit include_deleted filter.

- Creation: reserve metadata/name as CREATING; copy into a registered staging directory; rename to the final UUID directory on the same filesystem; mark READY.
- Start: READY or STOPPED becomes ACTIVE. This is bookkeeping and does not launch a process or enforce a file lock.
- Stop: ACTIVE becomes STOPPED; files remain untouched.
- Delete: allow READY, STOPPED, or ERROR; reject ACTIVE until stopped. Mark DELETING, remove only the registered workspace and its registered staging remnants, then mark DELETED. A repeated delete of a tombstoned UUID succeeds.
- Repeated start/stop in the target state succeeds; other unsupported transitions return 409.
- On failure, retain enough metadata to explain and retry cleanup. Preserve the original error if cleanup also fails.
- At startup, reconcile interrupted CREATING and DELETING operations using explicit recovery rules. Remove incomplete copies; never adopt unregistered directories automatically. Missing files for a live record become ERROR. ACTIVE remains metadata across service restarts, not proof of a running agent.

SQLite transactions and filesystem operations cannot form one atomic transaction. Recovery tests are part of the design, not optional polish.

## API contract

| Route | Result |
| --- | --- |
| POST /workspaces | 201 with READY workspace after bounded synchronous copying |
| GET /workspaces | 200, paginated metadata |
| GET /workspaces/{id} | 200 or 404 |
| POST /workspaces/{id}/start | 200 with ACTIVE metadata |
| POST /workspaces/{id}/stop | 200 with STOPPED metadata |
| DELETE /workspaces/{id} | 204 after successful removal |
| GET /health | Local readiness, including metadata-store availability |

Use 422 for malformed input/UUIDs, 404 for unknown sources or workspaces, 403 for disallowed paths, and 409 for lifecycle/name conflicts. Return a consistent machine-readable error code and message. Bound copying by configured file-count and byte limits; run blocking copy work outside the async event loop. Large asynchronous provisioning can be a later extension.

## Implementation milestones

1. Contract and scaffold: configuration, packaging, schemas, transition table, database schema and repository interfaces. Completion: configuration and lifecycle semantics are documented and testable.
2. Safe copying: source validation, exclusion policy, independent file copying, staging and bounded resource use. Completion: two copies can be modified independently without changing the source.
3. Persistence and recovery: metadata reservation, finalization, tombstones, failure recording and startup reconciliation. Completion: injected interruptions at each database/filesystem boundary recover deterministically.
4. API integration: seven routes, structured errors, OpenAPI examples and lifecycle serialization. Completion: create/list/get/start/stop/delete work through HTTP with persisted state after restart.
5. Acceptance and documentation: run the complete test suite, document Windows setup and limits, and capture copy-time/disk-use measurements. Completion: all correctness gates below pass and a reproducible demonstration is available.

## Correctness gates

- Create A and B from one fixture, then create/edit/delete files in A; B and the original source remain unchanged.
- Start, stop, restart A; edits remain. Stop and delete A; B and the source remain intact.
- Test traversal, sibling paths with similar prefixes, Windows case behavior, symlink/junction traversal, and links inserted into a workspace before deletion.
- Test duplicate-name races, conflicting lifecycle requests, missing source/workspace paths, and invalid UUIDs.
- Inject disk-full, permission, copy, rename, database-finalization, and deletion failures. Verify preserved error records and safe recovery.
- Restart after each provisioning/deletion boundary and confirm no incomplete workspace is returned as READY.
- Verify default exclusions and ensure no operation executes copied project code.

## Validation and next-phase decision

Measure creation latency, copied bytes, disk growth, and cleanup latency on representative small, medium and large fixtures. Include 1, 5 and 20 workspace scenarios. Publish the fixture sizes and environment; do not invent performance thresholds before measuring the baseline.

Validate user value by completing a real two-task workflow using the API and confirming that the workspace identities, preserved edits and cleanup are useful. Independent copies alone are not a differentiated product; reliable lifecycle management across tools is the initial hypothesis.

After Phase 1, compare copying against a standalone Git-worktree baseline before selecting optimizations. Keep Git integration outside this MVP. If copying is the bottleneck, investigate immutable source snapshots and copy-on-write materialization. If reviewing and returning changes is the bottleneck, prioritize diff/export or Git interoperability. Build custom history and merge semantics only after those needs are demonstrated. Introduce execution isolation before running untrusted agents, and authentication before remote exposure.
