# Local Docker publishing

Start Docker Desktop, then run `start-local-cloud.cmd` from the repository root.

1. Open http://localhost:8090 and select user1.
2. Change the heading in Website draft. Preview updates immediately.
3. Click **Publish to cloud**, then **Open live website**.
4. Change the draft again without publishing: the live website stays at the published version.
5. Select user2 or user3 and click **Pull published version** to copy the live page into that user's draft. The previous saved draft is backed up; unsaved editor changes are discarded after the confirmation.

The FastAPI publisher and website run in one Docker container. Files persist in Docker volume `workspace-local-cloud_local-cloud-files`. Each publish creates a new release directory, then atomically switches the live pointer. Old snapshot URLs keep serving their original content. A failed request before the pointer switch leaves the previous live release active.

Stop: `docker compose -f compose.local-cloud.yaml stop`

Start again: `docker compose -f compose.local-cloud.yaml up -d --wait`

Verification: run `scripts/verify_local_cloud.py` with Python. This checks HTTP publishing, immutable snapshots, draft isolation, both users pulling and container restart persistence. It restores the original drafts and leaves user1's original draft published. It creates retained test releases and pull backups.

This prototype publishes one HTML page. Existing native source folders and push/pull are separate. Chat, framework builds, per-app runtime containers, automatic scale-to-zero and Google Cloud deployment remain future work.
