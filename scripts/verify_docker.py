"""Exercise the running Docker topology without logging credentials.

Creates uniquely named demo workspaces; preserves user1 edits for inspection.
"""

import json
import ssl
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from runtime_paths import ROOT, runtime_root

RUNTIME = runtime_root()
PORTS = {"cloud": 8000, "user1": 8001, "user2": 8002, "user3": 8003}


def compose(*args):
    return subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()


def token(name, filename="admin.token"):
    return (RUNTIME / "secrets" / name / filename).read_text().strip()


def call(name, method, path, **kwargs):
    headers = {"Authorization": f"Bearer {token(name)}"}
    response = httpx.request(
        method,
        f"http://127.0.0.1:{PORTS[name]}{path}",
        headers=headers,
        timeout=120,
        trust_env=False,
        **kwargs,
    )
    response.raise_for_status()
    return response.json() if response.content else None


def read_file(name, path):
    return compose(
        "exec",
        "-T",
        name,
        "python",
        "-c",
        "from pathlib import Path; import sys; print(Path(sys.argv[1]).read_text(),end='')",
        path,
    )


def main():
    checks = []
    for name, port in PORTS.items():
        response = httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False)
        assert response.status_code == 200 and response.json()["environment"] == name
    checks.append("All four API health endpoints return 200")
    assert httpx.get("http://127.0.0.1:8000/projects", trust_env=False).status_code == 401
    checks.append("Anonymous project access is rejected with 401")
    project = call("cloud", "GET", "/projects")[0]
    project_id = project["project_id"]
    response = httpx.post(
        f"http://127.0.0.1:8000/projects/{project_id}/snapshots",
        headers={"Authorization": f"Bearer {token('user1', 'cloud.token')}"},
        trust_env=False,
    )
    assert response.status_code == 403
    checks.append("Cloud reader cannot publish snapshots (403)")
    snapshot = call("cloud", "POST", f"/projects/{project_id}/snapshots")
    snapshot_id = snapshot["snapshot_id"]
    label = "demo-" + uuid4().hex[:8]
    workspaces = {}
    original = read_file("cloud", "/data/sources/sample-app/main.py")
    for name in ("user1", "user2", "user3"):
        workspace = call(
            name, "POST", "/downloads", json={"snapshot_id": snapshot_id, "name": label}
        )
        assert workspace["snapshot_id"] == snapshot_id and workspace["status"] == "READY"
        assert read_file(name, workspace["path"] + "/main.py") == original
        workspaces[name] = workspace
    checks.append("Three users downloaded and verified the same immutable snapshot")
    first = workspaces["user1"]
    changed = original + "\n# Independent user1 edit\n"
    compose(
        "exec",
        "-T",
        "user1",
        "python",
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
        first["path"] + "/main.py",
        changed,
    )
    assert read_file("cloud", "/data/sources/sample-app/main.py") == original
    for name in ("user2", "user3"):
        assert read_file(name, workspaces[name]["path"] + "/main.py") == original
    checks.append("Editing user1 leaves cloud, user2 and user3 unchanged")
    identity = first["workspace_id"]
    for action in ("start", "stop", "start", "stop"):
        call("user1", "POST", f"/workspaces/{identity}/{action}")
    compose("restart", "user1")
    for attempt in range(40):
        try:
            response = httpx.get("http://127.0.0.1:8001/health", timeout=2, trust_env=False)
            if response.status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("user1 did not restart")
    assert call("user1", "GET", f"/workspaces/{identity}")["status"] == "STOPPED"
    assert read_file("user1", first["path"] + "/main.py") == changed.strip()
    checks.append("Lifecycle and a real container restart preserve user1 edits and metadata")
    disposable = call(
        "user1", "POST", "/downloads", json={"snapshot_id": snapshot_id, "name": label + "-delete"}
    )
    call("user1", "DELETE", f"/workspaces/{disposable['workspace_id']}")
    call("user1", "DELETE", f"/workspaces/{disposable['workspace_id']}")
    assert call("user1", "GET", f"/workspaces/{disposable['workspace_id']}")["status"] == "DELETED"
    assert read_file("cloud", "/data/sources/sample-app/main.py") == original
    for name in ("user2", "user3"):
        assert read_file(name, workspaces[name]["path"] + "/main.py") == original
    checks.append("Scoped, repeatable deletion leaves other users and cloud unchanged")
    ca = RUNTIME / "local-ca.crt"
    compose("cp", "gateway:/data/caddy/pki/authorities/local/root.crt", str(ca))
    context = ssl.create_default_context(cafile=str(ca))
    with httpx.Client(verify=context, trust_env=False) as client:
        assert client.get("https://localhost:8443/health").status_code == 200
        response = client.get(
            "https://localhost:8443/projects",
            headers={"Authorization": f"Bearer {token('user1', 'cloud.token')}"},
        )
        assert response.status_code == 200 and response.json()[0]["project_id"] == project_id
    checks.append("HTTPS gateway certificate and project access verified using local CA")
    report = {
        "verified_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "snapshot_id": snapshot_id,
        "project_id": project_id,
        "workspaces": workspaces,
        "tls_ca": str(ca),
        "result": "PASS",
    }
    (RUNTIME / "verification.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps(
            {"result": "PASS", "checks": checks, "report": str(RUNTIME / "verification.json")},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
