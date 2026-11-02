"""Local, static-site publishing simulator. Run with compose.local-cloud.yaml."""
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("LOCAL_CLOUD_ROOT", "/data"))
ROOT.mkdir(parents=True, exist_ok=True)
RELEASES = ROOT / "releases"
RELEASES.mkdir(exist_ok=True)
lock = threading.Lock()
INITIAL = '<!doctype html><html><head><meta charset="utf-8"><title>My website</title></head><body style="font-family:system-ui;padding:10%;background:#eff6ff"><h1>Hello from my workspace</h1><p>Edit your draft, then publish it to the local cloud.</p></body></html>'
for user in ("user1", "user2", "user3"):
    folder = ROOT / "users" / user
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / "index.html").exists():
        (folder / "index.html").write_text(INITIAL, encoding="utf-8")

app = FastAPI(title="Local cloud publisher")


class Draft(BaseModel):
    html: str = Field(min_length=1, max_length=1_000_000)


def draft_path(user):
    if user not in ("user1", "user2", "user3"):
        raise HTTPException(404, "Unknown workspace")
    return ROOT / "users" / user / "index.html"


def atomic(path, content):
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    temp.replace(path)


def current():
    path = ROOT / "current.json"
    return json.loads(path.read_text()) if path.exists() else None


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/state/{user}")
def state(user: str):
    with lock:
        return {"html": draft_path(user).read_text(encoding="utf-8"), "deployment": current()}


@app.put("/api/draft/{user}")
def save(user: str, draft: Draft):
    with lock:
        atomic(draft_path(user), draft.html)
    return {"saved": True}


@app.post("/api/publish/{user}")
def publish(user: str, draft: Draft):
    with lock:
        atomic(draft_path(user), draft.html)
        release = uuid.uuid4().hex
        folder = RELEASES / release
        folder.mkdir()
        (folder / "index.html").write_text(draft.html, encoding="utf-8")
        result = {"release": release, "user": user,
                  "sha256": hashlib.sha256(draft.html.encode()).hexdigest(),
                  "url": "/live/", "snapshot": f"/releases/{release}/index.html"}
        atomic(ROOT / "current.json", json.dumps(result))
        return result


@app.post("/api/pull/{user}")
def pull(user: str):
    with lock:
        path = draft_path(user)
        deployed = current()
        if not deployed:
            raise HTTPException(409, "Publish a website first")
        backup = path.with_name(f"before-pull-{uuid.uuid4().hex}.html")
        backup.write_bytes(path.read_bytes())
        atomic(path, (RELEASES / deployed["release"] / "index.html").read_text(encoding="utf-8"))
        return {"backup": backup.name}


@app.get("/live/", response_class=HTMLResponse)
def live():
    deployed = current()
    if not deployed:
        raise HTTPException(404, "No published website yet")
    return HTMLResponse((RELEASES / deployed["release"] / "index.html").read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def ui():
    return (Path(__file__).parent / "local_cloud_ui.html").read_text(encoding="utf-8")


app.mount("/releases", StaticFiles(directory=RELEASES), name="releases")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
