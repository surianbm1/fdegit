"""Exercise the local demo; preserves original drafts and leaves user1 published."""
import json
import subprocess
import urllib.request

BASE = "http://127.0.0.1:8090"


def request(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read().decode()
        return json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw


original = {u: request("/api/state/" + u)["html"] for u in ("user1", "user2", "user3")}
assert "Publish to cloud" in request("/")
try:
    first = request("/api/publish/user1", "POST", {"html": original["user1"]})
    assert request("/live/") == original["user1"]
    draft = original["user1"] + "<!-- unpublished edit -->"
    request("/api/draft/user1", "PUT", {"html": draft})
    assert request("/live/") == original["user1"]
    second = request("/api/publish/user1", "POST", {"html": draft})
    assert request("/live/") == draft
    assert request(first["snapshot"]) == original["user1"]
    assert request("/api/state/user2")["html"] == original["user2"]
    for user in ("user2", "user3"):
        assert request("/api/pull/" + user, "POST")["backup"]
        assert request("/api/state/" + user)["html"] == draft
    subprocess.run(["docker", "compose", "-f", "compose.local-cloud.yaml", "restart"], check=True)
    subprocess.run(["docker", "compose", "-f", "compose.local-cloud.yaml", "up", "-d", "--wait"], check=True)
    assert request("/live/") == draft
    assert request("/api/state/user1")["deployment"]["release"] == second["release"]
    print("PASS: UI served, publish, draft isolation, immutable releases, explicit pull for both users, restart persistence")
finally:
    for user, html in original.items():
        request("/api/draft/" + user, "PUT", {"html": html})
    request("/api/publish/user1", "POST", {"html": original["user1"]})
