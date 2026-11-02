"""Exercise source-control APIs, sparse files and tiered caches in running Docker."""

import hashlib
import json
from uuid import uuid4

import httpx
from verify_cache import RUNTIME, call, compose


def main():
    label = "source-" + uuid4().hex[:8]
    project = call("cloud", "GET", "/projects")[0]["project_id"]
    base = call("cloud", "GET", f"/projects/{project}/head")
    checks = []
    payload = f"# {label}\n".encode()
    digest = hashlib.sha256(payload).hexdigest()
    blob = call("cloud", "PUT", f"/projects/{project}/objects/{digest}", content=payload)
    blob["executable"] = False
    left = call(
        "cloud",
        "POST",
        f"/projects/{project}/changesets",
        json={
            "base_revision": base["revision_id"],
            "files": {f"{label}-left.py": blob},
            "message": "left",
        },
    )
    right = call(
        "cloud",
        "POST",
        f"/projects/{project}/changesets",
        json={
            "base_revision": base["revision_id"],
            "files": {f"{label}-right.py": blob},
            "message": "right",
        },
    )
    merged = call(
        "cloud",
        "POST",
        f"/projects/{project}/merge",
        json={"left": left["revision_id"], "right": right["revision_id"]},
    )
    assert merged["merge_parent"] == right["revision_id"]
    rebased = call(
        "cloud",
        "POST",
        f"/projects/{project}/rebase",
        json={"left": left["revision_id"], "right": right["revision_id"]},
    )
    assert rebased["parent"] == right["revision_id"]
    assert call("cloud", "GET", f"/projects/{project}/head")["revision_id"] == base["revision_id"]
    checks.append(
        "Server creates, merges and rebases changesets without changing the working source"
    )
    history = call("cloud", "GET", f"/projects/{project}/history/{merged['revision_id']}")
    assert len(history) >= 3
    blame = call(
        "cloud",
        "GET",
        f"/projects/{project}/blame/{left['revision_id']}",
        params={"path": f"{label}-left.py"},
    )
    assert blame[0]["revision_id"] == left["revision_id"]
    diff = call(
        "cloud",
        "GET",
        f"/projects/{project}/diff",
        params={"left": base["revision_id"], "right": left["revision_id"]},
    )
    assert diff["added"] == [f"{label}-left.py"]
    call(
        "cloud",
        "PUT",
        f"/projects/{project}/refs/{label}",
        json={"revision_id": merged["revision_id"]},
    )
    checks.append("History, blame, diff and atomic ref creation return consistent results")
    tree = call("cloud", "GET", f"/projects/{project}/revisions/{merged['revision_id']}/tree")
    assert len(tree["tree"]) == 64
    checks.append("Immutable content-addressed directory tree is available")
    lazy = call(
        "user3", "POST", "/checkouts", json={"project_id": project, "name": label, "paths": []}
    )
    workspace = lazy["workspace"]
    assert lazy["downloaded_files"] == 0
    count = compose(
        "exec",
        "-T",
        "user3",
        "python",
        "-c",
        "from pathlib import Path; import sys; print(len(list(Path(sys.argv[1]).iterdir())))",
        workspace["path"],
    )
    assert count == "0"
    expanded = call(
        "user3",
        "POST",
        f"/workspaces/{workspace['workspace_id']}/materialize",
        json={"paths": ["main.py"]},
    )
    assert expanded["materialized_files"] == 1
    checks.append("Empty sparse checkout downloads no files; explicit access materializes one file")
    cache_url = "http://127.0.0.1:8010"
    before = httpx.get(cache_url + "/health", trust_env=False).json()
    for name in ("user1", "user2", "user3"):
        token = (RUNTIME / "secrets" / name / "cloud.token").read_text().strip()
        response = httpx.get(
            f"{cache_url}/projects/{project}/objects/{digest}",
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
        )
        response.raise_for_status()
        assert response.content == payload
    after = httpx.get(cache_url + "/health", trust_env=False).json()
    assert after["misses"] - before["misses"] == 1
    assert after["hits"] - before["hits"] == 2
    checks.append("Three user credentials share one regional blob fetch and two cache hits")
    for name in ("cloud", "user1", "user2", "user3"):
        assert call(name, "GET", "/monitor")["running"]
    checks.append("Filesystem monitors run in all four environments")
    result = {"status": "PASS", "checks": checks, "cache": after, "workspace": workspace}
    output = RUNTIME / "source-control-verification.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"status": "PASS", "checks": checks, "report": str(output)}, indent=2))


if __name__ == "__main__":
    main()
