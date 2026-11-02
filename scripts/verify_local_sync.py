"""Publish only the isolated demonstration file and verify all native copies."""

import hashlib
import json
import os
from pathlib import Path

from app.config import Settings
from app.local_sync import LocalSync

root = Path(os.environ["LOCALAPPDATA"]) / "AgentWorkspaceManager"
settings = {
    name: Settings.load(root / "environments" / name / "environment.json")
    for name in ("cloud", "user1", "user2", "user3")
}
result = LocalSync(settings, root / "filesystem-sync").run("user1", paths=["cache-sync-demo.txt"])
copies = {}
for user in ("user2", "user3"):
    LocalSync(settings, root / "filesystem-sync").run(user, "pull")
for name, config in settings.items():
    path = config.project_path / "cache-sync-demo.txt"
    copies[name] = {
        "path": str(path),
        "contents": path.read_text(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
assert len({copy["sha256"] for copy in copies.values()}) == 1
report = {"status": "PASS", "publish": result, "copies": copies}
(root / "filesystem-sync" / "verification.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
