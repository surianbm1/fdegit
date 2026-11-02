# Four-environment local setup

Prepared October 29, 2026. These native project folders are separate from the later Docker deployment. Docker runs cloud/user1/user2/user3 in individual containers with individual volumes; see DOCKER.md. Native folders run under the current Windows account and are not OS security boundaries.

Base directory: `C:\Users\surya\AppData\Local\AgentWorkspaceManager\environments`

| Environment | Project copy under base | Planned loopback port |
| --- | --- | --- |
| cloud | cloud\sources\filegit | 8000 |
| user1 | user1\sources\filegit | 8001 |
| user2 | user2\sources\filegit | 8002 |
| user3 | user3\sources\filegit | 8003 |

Each native environment has independent project files and its own `state\workspaces`, `state\staging`, and initialized `state\metadata.sqlite3`. scripts/sync_native.py refreshes generated project files and backs up changed existing files before replacing them.

Each environment also has an environment.json descriptor outside its project copy. Native CLI commands load and validate it. The local Docker deployment currently occupies ports 8000 through 8003, so stop the corresponding container or choose a free native port before starting a native API.

Copies contain ordinary independent files, without shared Git metadata, hard links, or automatic synchronization. Editing a user copy does not publish changes to cloud or to another user. Shared projects, change submission and synchronization require future application functionality.

The original `filegit` repository remains the development source. Environment project copies are an initial snapshot and will not automatically receive subsequent code changes.
