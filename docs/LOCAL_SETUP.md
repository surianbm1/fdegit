# Local workspace layout

Prepared October 29, 2026. The service is implemented; Docker operation is described in DOCKER.md. This table preserves the original generic native layout; per-environment native roots are described in ENVIRONMENTS.md.

| Purpose | Location |
| --- | --- |
| Code and planning documents | C:\Users\surya\OneDrive\Documents\ChatGPT\filegit |
| Allowed source root for future copying | C:\Users\surya\AppData\Local\AgentWorkspaceManager\sources |
| Runtime state root | C:\Users\surya\AppData\Local\AgentWorkspaceManager\state |
| Independent editable workspaces | C:\Users\surya\AppData\Local\AgentWorkspaceManager\state\workspaces |
| Incomplete copies | C:\Users\surya\AppData\Local\AgentWorkspaceManager\state\staging |
| Planned metadata database | C:\Users\surya\AppData\Local\AgentWorkspaceManager\state\metadata.sqlite3 |

The generic database location above is unused. Each of the four native environments has an initialized database, and each Docker service has a separate volume-backed database. Source/runtime roots are disjoint and validated at startup.

Place a stable sample project beneath the allowed source root when testing begins. Keep that source unchanged while it is copied. Tests will instead create their own temporary directories.

Python, the Python launcher, uv and Git are available on PATH. The project requires Python 3.12 or later. The installed development environment is at %LOCALAPPDATA%/AgentWorkspaceManager/development/.venv.

Configuration and commands are documented in DEVELOPMENT.md and DOCKER.md. Generated Docker credentials/reports live at %LOCALAPPDATA%/AgentWorkspaceManager/docker; the checkout's ignored .env contains its path only.
