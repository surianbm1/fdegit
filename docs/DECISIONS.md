# Architecture decisions

Status: implemented baseline, October 29, 2026. The later cloud-to-local workflow adds immutable ZIP snapshots, checksum validation, bearer authentication, project grants, and a local Docker deployment. Each Docker service is still a single-process local service with its own database and data volume.

The subsequent cache request adds content-addressed file objects shared across each environment's workspaces, stat-indexed change scans, latest manifests and file-level push/pull. Immutable blobs are deduplicated; editable working files are independent copies. See CACHE_SYNC.md. The remaining decision table preserves the original provisioning baseline.

| Decision | Reason |
| --- | --- |
| Local, single-user, loopback service | Keep the first release bounded and easy to verify |
| One API process; serialized lifecycle mutations | Make initial concurrency behavior explicit |
| Ordinary file copies, without hard links | Independent writable contents with straightforward semantics |
| SQLite metadata and local working directories | Small operational footprint |
| API -> lifecycle service -> metadata repository / filesystem materializer | Keep HTTP, persistence and filesystem responsibilities separate |
| UUID directories and registered ownership | Keep display names and request paths out of deletion targets |
| Staging, lifecycle failure states and startup reconciliation | Recover when filesystem and database operations are interrupted |
| Runtime storage outside the synced repository | Keep databases and mutable workspaces out of OneDrive source syncing |
| Source and state roots are disjoint | Prevent recursive copying and accidental source deletion |
| ACTIVE is metadata only | Agent execution belongs to a future phase |

Independent copies provide file independence. OS process isolation requires a separate execution boundary in a later phase.

The complete lifecycle, HTTP behavior, exclusions and correctness gates are specified in ../PLAN.md. Optimize storage only after measuring the copy baseline. Future object storage holds immutable source content; local materialization remains a separate responsibility.
