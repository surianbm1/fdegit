# Filesystem MVP

From a native user1/user2/user3 project folder, save your files and run
` .\push.cmd ` to publish to the native cloud project. Other users run
` .\pull.cmd ` when ready to update their working files. `publish.cmd` remains
an alias for push. Git Bash users can run `bash push.sh` and `bash pull.sh`.
Docker and FastAPI are not involved in these commands.
The older HTTP API remains a separate implementation.

The four folders are under `%LOCALAPPDATA%/AgentWorkspaceManager/environments`,
each at `<environment>/sources/filegit`. The common immutable SHA-256 cache,
per-user baselines, stat indexes, lock and recovery journals are under
`%LOCALAPPDATA%/AgentWorkspaceManager/filesystem-sync`.

Only changed file contents are read after a warm scan. Missing/new objects are
cached once. Added/modified/deleted files are published. Other working folders
stay unchanged until explicit pull; opening a folder does not update it.
If a pull encounters conflicts, it applies none of the incoming file updates.
Empty-directory-only changes are not
versioned in this small MVP.

Conflicting saved edits are preserved and reported by path. A stale publish of
the same path fails; edits on unrelated paths can publish. The tool cannot see
unsaved VS Code buffers: save or discard editor changes before publishing.

Initial linking uses the current cloud tree as the common baseline. Differences
already present in a user's source folder are considered local edits. The demo
publishes only `cache-sync-demo.txt`, leaving other existing edits unpublished.
The live evidence is `filesystem-sync/verification.json`. Windows launchers use
the existing shared Python environment and the implementation in the central
filegit project; those paths are specific to this machine.
