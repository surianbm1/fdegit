# Verification results

Verified October 29, 2026. Deployment target: local Docker Desktop, as requested.

## Automated suite

104 tests passed on Windows with Python 3.13.5. Ruff lint and formatting checks passed. One dependency warning remains: Starlette's TestClient deprecates its HTTPX integration in favor of HTTPX2. No test failures are hidden or skipped for this warning.

Tests cover independent edits/deletions, empty directories, default exclusions, copy quotas, source mutation during copying, duplicate-name concurrency, single-process state ownership, lifecycle conflicts, tombstones, database migration/persistence, interrupted create/delete/download/snapshot recovery, Windows junctions, path traversal, ZIP entry collisions/links, manifest/archive/file checksums, permission and database failures, authentication, expiry, revocation, reader ownership and project grants.

## Full Docker flow

The trigger is scripts/verify_docker.py, which calls the real HTTP services and checks filesystem contents inside the containers. This product uses CLI/API entry points; no dashboard is implemented.

| Boundary | Evidence |
| --- | --- |
| Client -> API | Four health endpoints returned 200; anonymous project access returned 401 |
| Authentication/authorization | Cloud reader snapshot publication returned 403; allowed project reads succeeded |
| Source -> snapshot | A cloud sample-project snapshot was published with archive/file SHA-256 checksums |
| Snapshot -> local files | All three users downloaded the same snapshot into distinct volumes and UUID directories |
| Edit isolation | user1 edit left cloud, user2 and user3 source contents unchanged |
| Persistence | A real user1 container restart retained STOPPED metadata and the modified file |
| Deletion | A disposable user1 workspace was deleted twice successfully without affecting other users/cloud |
| HTTPS | Gateway certificate verification and authorized project access succeeded with its exported local CA |
| Native Windows downloads | user1/user2/user3 CLI clients downloaded the same Docker cloud snapshot over verified HTTPS into their separate native state directories; main.py SHA-256 matched the source |

Services run on localhost ports 8000 (cloud), 8001-8003 (users), and 8443 (HTTPS gateway). The gateway root certificate is trusted explicitly in verification; the system trust store is unchanged. JSON run evidence and current demo resource IDs are under the external runtime root in verification.json. Demo user1 is STOPPED with its preserved edit; user2 and user3 are READY.

native-downloads.json records the three native workspace locations. The original native code copies were refreshed with backups of replaced files, and their metadata databases migrated to schema v2.

## Copy baseline

Measured on Windows 10, Python 3.13.5, temporary native filesystem fixtures. Creation is serialized; 1, 5 and 20 copies coexist before deletion. These are file-content bytes, excluding SQLite/filesystem overhead. Caches may be warm, and the measurements ran alongside development/Docker activity. These numbers are a baseline, not a production performance claim or a Git comparison.

| Fixture | Files | Source size | Copies | Mean create seconds | Workspace bytes | Total delete seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| small | 12 | 48 KiB | 1 | 0.1584 | 49,152 | 0.0338 |
| small | 12 | 48 KiB | 5 | 0.1118 | 245,760 | 0.2407 |
| small | 12 | 48 KiB | 20 | 0.0959 | 983,040 | 0.5657 |
| medium | 100 | 1.5625 MiB | 1 | 2.0856 | 1,638,400 | 0.0964 |
| medium | 100 | 1.5625 MiB | 5 | 1.2883 | 8,192,000 | 0.5665 |
| medium | 100 | 1.5625 MiB | 20 | 1.3017 | 32,768,000 | 2.3054 |
| large | 400 | 12.5 MiB | 1 | 10.0890 | 13,107,200 | 0.3763 |
| large | 400 | 12.5 MiB | 5 | 5.6350 | 65,536,000 | 2.0712 |
| large | 400 | 12.5 MiB | 20 | 6.0388 | 262,144,000 | 8.4611 |

Storage grows linearly with ordinary copies. A separate Git-worktree comparison should precede investment in deduplication or custom version control. benchmark.json retains raw results in the external runtime directory.

## Limits and follow-up

The end-to-end download, isolation and incremental file synchronization behavior is verified. This does not establish real-world product demand, high availability or Internet-facing readiness. Authoritative retention/garbage collection, distributed scaling, transparent filesystem virtualization, agent execution and hostile-process sandboxing remain outside this implementation. Server-side merges and bounded client/regional cache eviction are now implemented; see SOURCE_CONTROL.md for precise limits.

## Incremental cache verification

scripts/verify_cache.py passed against all three live Docker user services:

- Warm-cache checkout downloaded zero file bytes.
- No-op pull read zero working-file contents, downloaded zero blobs and updated zero files.
- A changed main.py was scanned once, cached, and uploaded as one missing blob.
- Cloud source contents, object cache and latest manifest all reflected the push.
- user2/user3 fetched exactly one blob; unchanged file hashes and mtimes stayed intact.
- A new filename reusing an existing blob required zero file-content upload/download.
- Deletion required zero file-content upload/download.
- After restarting user2, no-op pull still read/downloaded/updated zero files.

cache-verification.json in the external runtime root records resource IDs and actual transfer counters. Additional unit tests cover cache corruption repair, legacy ZIP-workspace adoption without re-download, interrupted-pull rollback and retry after a lost push acknowledgement. The live test also exposed inherited read-only seed-directory permissions; container startup now makes its own seeded source directories writable before recovery, and a regression test covers this.

All three original native Windows clients were also verified against the Docker cloud over HTTPS: each adopted its existing downloaded workspace, fetched exactly one changed file, and then performed a no-op pull with zero working-file contents read and zero files downloaded. native-cache-verification.json records these counters.

SQLite is now schema v3. The original copy benchmark above remains the baseline for ordinary full workspace materialization, not a measurement of incremental transfer speed. Manifests still travel on no-op sync calls; zero file bytes does not mean zero HTTP traffic. See CACHE_SYNC.md for usage and current conflict behavior.

Current credential mounts point outside OneDrive. An automatic execution-policy check rejected removal of earlier generated credential copies from the Git-ignored .runtime/docker/secrets directory; those legacy copies were left in place. No detailed rejection reason was provided. They are not mounted by the running services.

## Source-control extension verification

The source-control extension adds incremental revision storage and metadata transfer, server changesets, history/diff/blame, merge/rebase, concurrent CAS refs, sparse expansion and recovery, immutable directory negotiation, object-only repositories, pinned cache eviction, filesystem notifications and cross-user regional cache reuse. Unit/integration coverage is in tests/test_source_control.py. The live Docker report source-control-verification.json records server operations, sparse materialization, all four monitors, and three users producing one regional miss plus two hits for the same new blob. See SOURCE_CONTROL.md for remaining parity and scaling work.
