# Project memory — 2026-11-02

## Established decisions
- Four environments: cloud, user1, user2, user3.
- Active filesystem MVP: push.cmd/push.sh publishes changed files to the shared cloud tree/cache; pull.cmd/pull.sh explicitly refreshes another user's files. No automatic pull while editing or on folder open.
- SHA-256 whole-file cache with per-user baselines and metadata scan fingerprints; unchanged file contents are reused. Same-path conflicts are preserved/reported. No transparent missing-file materialization.
- Native copies live below `%LOCALAPPDATA%/AgentWorkspaceManager/environments/<name>/sources/filegit`. The filesystem MVP does not require FastAPI or Docker.
- The older compose.yaml FastAPI source-control stack remains separate. It supports revisions, objects, refs and explicit sparse materialization; it is not a website deployment service.
- User wants a simple chat-oriented interface for nontechnical users. UI framework selection and agent integration are deferred. No Puck-style visual builder requested.
- Local Docker only for now. No GCP account or remote deployment configured.

## Local publishing prototype
Run `start-local-cloud.cmd` with Docker Desktop running. UI: http://localhost:8090 ; published website: http://localhost:8090/live/ .

`compose.local-cloud.yaml` runs a separate publisher container. Its persistent named volume holds `/data/users/user1..3/index.html`, immutable `/data/releases/<id>/index.html`, and an atomically replaced `/data/current.json` live pointer. Publish saves the selected draft and creates a fixed release. Pull copies the current published page into another draft and backs up its previous saved content. Editing a draft does not change the live release.

This is an isolated static HTML demo, not connected to the existing native filegit working copies. It does not yet build multi-file/framework apps, execute an agent, deploy one container per app, implement Cloud Run autoscaling/scale-to-zero, or perform remote deployment. Its API accepts one HTML page, not arbitrary project uploads. Existing command-line source push/pull behavior is unchanged.

## Next integration work
Connect the chosen chat agent to an isolated project workspace; support complete app artifacts and build validation; publish pinned revisions from the existing source cache; run each resulting image as a separate service; add deployment status/history/rollback and later real Cloud Run deployment. Keep source push distinct from website Publish.

## Prior performance evidence
Local filesystem benchmark: 100 files of 16 KiB, 50 changed; five trials, median push 1.3923 s and pull 0.9329 s. Includes engine setup, excludes Python startup, warm disk cache. Not a network or production scaling result.
