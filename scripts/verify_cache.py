"""Prove real Docker transfers reuse cached file objects and update only changed paths."""

import json
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


def call(name, method, path, **kwargs):
    token = (RUNTIME / "secrets" / name / "admin.token").read_text().strip()
    response = httpx.request(
        method,
        f"http://127.0.0.1:{PORTS[name]}{path}",
        timeout=120,
        headers={"Authorization": f"Bearer {token}"},
        trust_env=False,
        **kwargs,
    )
    response.raise_for_status()
    return response.json()


def inspect(name, path):
    return json.loads(
        compose(
            "exec",
            "-T",
            name,
            "python",
            "-c",
            "import json,hashlib,sys; from pathlib import Path; p=Path(sys.argv[1]); "
            "print(json.dumps({'hash':hashlib.sha256(p.read_bytes()).hexdigest(),"
            "'mtime_ns':p.stat().st_mtime_ns}))",
            path,
        )
    )


def main():
    label = "cached-" + uuid4().hex[:8]
    project = call("cloud", "GET", "/projects")[0]["project_id"]
    initial_head = call("cloud", "GET", f"/projects/{project}/head")
    static = next(name for name in initial_head["files"] if name != "main.py")
    users, transfers, checks = {}, {}, []
    for name in ("user1", "user2", "user3"):
        result = call(name, "POST", "/checkouts", json={"project_id": project, "name": label})
        users[name] = result["workspace"]
        transfers[f"{name}_checkout"] = {
            key: value for key, value in result.items() if key != "workspace"
        }
    repeat = call(
        "user1", "POST", "/checkouts", json={"project_id": project, "name": label + "-warm"}
    )
    assert repeat["downloaded_files"] == repeat["downloaded_bytes"] == 0
    checks.append("Second checkout uses local cache: zero file downloads")
    unchanged = {name: inspect(name, users[name]["path"] + "/" + static) for name in users}
    a = users["user1"]
    noop = call("user1", "POST", f"/workspaces/{a['workspace_id']}/pull")
    assert noop["files_read"] == noop["downloaded_files"] == noop["updated_files"] == 0
    checks.append("Unchanged pull reads zero working-file contents and downloads zero bytes")
    compose(
        "exec",
        "-T",
        "user1",
        "python",
        "-c",
        "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
        "p.write_bytes(p.read_bytes()+sys.argv[2].encode())",
        a["path"] + "/main.py",
        f"\n# {label}: pushed through the file cache\n",
    )
    status = call("user1", "GET", f"/workspaces/{a['workspace_id']}/status")
    # The filesystem monitor may have already cached the edit before this request.
    assert status["modified"] == ["main.py"] and status["files_read"] in (0, 1)
    assert call("user1", "GET", f"/workspaces/{a['workspace_id']}/status")["files_read"] == 0
    pushed = call("user1", "POST", f"/workspaces/{a['workspace_id']}/push")
    assert pushed["uploaded_files"] == 1 and pushed["files_read"] == 0
    transfers["push_one_file"] = {key: value for key, value in pushed.items() if key != "workspace"}
    checks.append("One edited file is read once, cached, and uploaded as one blob")
    expected = inspect("user1", a["path"] + "/main.py")["hash"]
    assert inspect("cloud", "/data/sources/sample-app/main.py")["hash"] == expected
    head = call("cloud", "GET", f"/projects/{project}/head")
    assert head["files"]["main.py"]["sha256"] == expected
    checks.append("Push updates cloud working files, latest manifest and immutable object cache")
    for name in ("user2", "user3"):
        result = call(name, "POST", f"/workspaces/{users[name]['workspace_id']}/pull")
        assert result["downloaded_files"] == result["updated_files"] == 1
        assert inspect(name, users[name]["path"] + "/main.py")["hash"] == expected
        assert inspect(name, users[name]["path"] + "/" + static) == unchanged[name]
        transfers[f"{name}_pull_one_file"] = {
            key: value for key, value in result.items() if key != "workspace"
        }
    checks.append("Other users fetch only the changed blob; unchanged file mtimes stay intact")
    extra = label + ".py"
    compose(
        "exec",
        "-T",
        "user1",
        "python",
        "-c",
        "from pathlib import Path; import sys; root=Path(sys.argv[1]); "
        "(root/sys.argv[2]).write_bytes((root/sys.argv[3]).read_bytes())",
        a["path"],
        extra,
        static,
    )
    duplicate = call("user1", "POST", f"/workspaces/{a['workspace_id']}/push")
    assert duplicate["uploaded_files"] == 0
    for name in ("user2", "user3"):
        result = call(name, "POST", f"/workspaces/{users[name]['workspace_id']}/pull")
        assert result["downloaded_files"] == 0 and result["updated_files"] == 1
    checks.append("New path with already-cached content transfers zero file bytes")
    compose(
        "exec",
        "-T",
        "user1",
        "python",
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).unlink()",
        a["path"] + "/" + extra,
    )
    deletion = call("user1", "POST", f"/workspaces/{a['workspace_id']}/push")
    assert deletion["uploaded_files"] == 0 and deletion["deleted"] == [extra]
    for name in ("user2", "user3"):
        result = call(name, "POST", f"/workspaces/{users[name]['workspace_id']}/pull")
        assert result["downloaded_files"] == 0 and result["deleted"] == [extra]
    checks.append("Deletions propagate through metadata with zero file-content transfer")
    compose("restart", "user2")
    for _ in range(40):
        try:
            if (
                httpx.get("http://127.0.0.1:8002/health", timeout=2, trust_env=False).status_code
                == 200
            ):
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("user2 did not restart")
    after = call("user2", "POST", f"/workspaces/{users['user2']['workspace_id']}/pull")
    assert after["downloaded_files"] == after["files_read"] == after["updated_files"] == 0
    checks.append("Cache and baseline survive a real container restart")
    report = {
        "result": "PASS",
        "verified_at": datetime.now(UTC).isoformat(),
        "checks": checks,
        "project_id": project,
        "initial_revision": initial_head["revision_id"],
        "latest_revision": call("cloud", "GET", f"/projects/{project}/head")["revision_id"],
        "workspaces": users,
        "transfers": transfers,
    }
    destination = RUNTIME / "cache-verification.json"
    destination.write_text(json.dumps(report, indent=2))
    print(json.dumps({"result": "PASS", "checks": checks, "report": str(destination)}, indent=2))


if __name__ == "__main__":
    main()
