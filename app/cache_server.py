"""Disposable bounded HTTP object-cache tier, sharing the source API protocol."""

import io
import os
import threading
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.cache import ObjectCache


def create_cache_app(upstream, root, max_bytes=256 * 1024 * 1024, transport=None):
    root.mkdir(parents=True, exist_ok=True)
    cache = ObjectCache(root)
    lock = threading.RLock()
    app = FastAPI(title="Workspace regional object cache")
    stats = {"hits": 0, "misses": 0, "upstream_requests": 0}

    @app.get("/health")
    def health():
        return {"status": "ok", "tier": "regional", **stats}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def proxy(path: str, request: Request):
        # Clients keep their existing project credentials across the tier.
        authorization = request.headers.get("authorization", "")
        if not authorization:
            return JSONResponse({"code": "unauthorized", "message": "Bearer token required"}, 401)
        payload = await request.body()
        return await run_in_threadpool(
            forward,
            path,
            request.method,
            authorization,
            dict(request.query_params),
            request.headers.get("content-type", "application/octet-stream"),
            payload,
        )

    def forward(path, method, authorization, query, content_type, payload):
        identity = path.rsplit("/", 1)[-1]
        immutable = method == "GET" and "/objects/" in path and len(identity) == 64
        # Scope cache grants to this process and bearer/project after a successful origin read.
        import hashlib

        project_path = path.split("/objects/", 1)[0]
        grant = (hashlib.sha256(authorization.encode()).hexdigest(), project_path)
        with lock:
            if immutable and cache.has(identity) and grant not in app.state.grants:
                try:
                    with httpx.Client(
                        base_url=upstream, timeout=120, trust_env=False, transport=transport
                    ) as client:
                        allowed = client.get("/projects", headers={"Authorization": authorization})
                    project = project_path.split("/")[-1]
                    if allowed.status_code != 200 or not any(
                        p["project_id"] == project for p in allowed.json()
                    ):
                        return JSONResponse(
                            {"code": "forbidden", "message": "Project access required"}, 403
                        )
                    app.state.grants.add(grant)
                except (httpx.HTTPError, ValueError, KeyError):
                    return JSONResponse(
                        {"code": "upstream_unavailable", "message": "Cache upstream unavailable"},
                        502,
                    )
            if immutable and grant in app.state.grants and cache.has(identity):
                stats["hits"] += 1
                return Response(
                    cache.path(identity).read_bytes(), media_type="application/octet-stream"
                )
            if immutable:
                stats["misses"] += 1
            stats["upstream_requests"] += 1
            # Fixed upstream only: no fallback bypass on upstream failure.
            try:
                with httpx.Client(
                    base_url=upstream, timeout=120, trust_env=False, transport=transport
                ) as client:
                    response = client.request(
                        method,
                        "/" + path,
                        params=query,
                        headers={
                            "Authorization": authorization,
                            "Content-Type": content_type,
                        },
                        content=payload,
                    )
            except httpx.HTTPError:
                return JSONResponse(
                    {"code": "upstream_unavailable", "message": "Cache upstream unavailable"}, 502
                )
            if immutable and response.status_code == 200:
                cache.put(
                    io.BytesIO(response.content), max(len(response.content), max_bytes), identity
                )
                app.state.grants.add(grant)
                cache.prune(max_bytes)
            return Response(
                response.content,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )

    app.state.grants = set()
    app.state.cache = cache
    return app


def main():
    app = create_cache_app(
        os.environ["AWM_UPSTREAM"],
        Path("/data/cache"),
        int(os.environ.get("AWM_CACHE_BYTES", 256 * 1024 * 1024)),
    )
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
