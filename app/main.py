"""Authenticated API for local workspaces and cloud project snapshots."""

import logging
import os
import sqlite3
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from app.client import download
from app.config import Settings
from app.domain import CreateWorkspace, ErrorResponse, Workspace, WorkspaceError
from app.manager import WorkspaceManager
from app.monitor import WorkspaceMonitor
from app.security import Credentials, Principal
from app.snapshots import Snapshot, SnapshotService
from app.source_api import source_router
from app.source_control import SourceControl
from app.sync import PublishRequest, Revision, SyncService
from app.sync_client import SyncClient

logger = logging.getLogger(__name__)


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: UUID
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return CreateWorkspace(name=value, source_path=Path(".")).name


class RegisterProject(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_path: Path


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    name: str = Field(min_length=1, max_length=100)
    paths: list[str] | None = None


class MaterializeRequest(BaseModel):
    paths: list[str]


class RepositoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MissingObjects(BaseModel):
    hashes: list[str] = Field(max_length=100_000)


def create_app(
    settings: Settings,
    credential_file: Path,
    *,
    cloud_url: str | None = None,
    cloud_token_file: Path | None = None,
    allow_http=False,
) -> FastAPI:
    credentials = Credentials(credential_file)

    @asynccontextmanager
    async def lifespan(app):
        manager = WorkspaceManager(settings)
        try:
            app.state.manager = manager
            app.state.snapshots = SnapshotService(manager)
            app.state.sync = SyncService(manager)
            app.state.monitor = WorkspaceMonitor(app.state.sync, sync_client if cloud_url else None)
            if os.environ.get("AWM_WATCH", "0") == "1":
                app.state.monitor.start()
            yield
        finally:
            if hasattr(app.state, "monitor"):
                app.state.monitor.close()
            manager.close()

    app = FastAPI(
        title="Agent Workspace Manager",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    bearer = HTTPBearer(auto_error=False)

    def authenticate(auth: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
        if auth is None or auth.scheme.casefold() != "bearer":
            raise WorkspaceError("unauthorized", "Bearer token required", 401)
        return credentials.authenticate(auth.credentials)

    @app.exception_handler(WorkspaceError)
    async def workspace_error(request: Request, error: WorkspaceError):
        headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else {}
        return JSONResponse(
            {"code": error.code, "message": str(error)},
            status_code=error.status_code,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error):
        return JSONResponse(
            {"code": "invalid_request", "message": "Request validation failed"}, status_code=422
        )

    async def operational_error(request: Request, error):
        logger.error("Operation failed: %s", type(error).__name__, exc_info=error)
        code = 502 if isinstance(error, httpx.HTTPError) else 500
        return JSONResponse(
            {"code": "operation_failed", "message": "Operation failed; see server logs"},
            status_code=code,
        )

    for error_type in (
        OSError,
        sqlite3.Error,
        httpx.HTTPError,
        zipfile.BadZipFile,
        ValidationError,
    ):
        app.add_exception_handler(error_type, operational_error)

    @app.get("/health")
    def health():
        try:
            with app.state.manager.repo.connect() as db:
                db.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return {"status": "ok", "environment": settings.environment}

    router = APIRouter(
        dependencies=[Depends(authenticate)],
        responses={
            code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 413, 422, 500, 502)
        },
    )

    @app.get("/docs", include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)

    @app.get("/openapi.json", include_in_schema=False)
    def schema():
        return app.openapi()

    @router.get("/projects")
    def projects(principal: Principal = Depends(authenticate)):
        return [
            p
            for p in app.state.manager.repo.projects()
            if principal.role == "admin" or p["project_id"] in principal.projects
        ]

    @router.post("/repositories", status_code=201)
    def create_repository(request: RepositoryRequest, principal: Principal = Depends(authenticate)):
        principal.admin()
        return SourceControl(app.state.sync).create_repository(request.name)

    @router.post("/projects")
    def register_project(request: RegisterProject, principal: Principal = Depends(authenticate)):
        principal.admin()
        manager = app.state.manager
        with manager.lock:
            source = manager.fs.source(request.source_path)
            return {"project_id": manager.repo.source(str(source)), "name": source.name}

    @router.get("/projects/{project_id}/head", response_model=Revision)
    def head(project_id: UUID, principal: Principal = Depends(authenticate)):
        principal.project(project_id)
        return app.state.sync.head(project_id)[0]

    @router.get("/snapshots/{snapshot_id}/revision", response_model=Revision)
    def snapshot_revision(snapshot_id: UUID, principal: Principal = Depends(authenticate)):
        metadata = app.state.snapshots.get(snapshot_id)
        principal.project(metadata["project_id"])
        return app.state.sync.from_snapshot(snapshot_id)

    @router.post("/projects/{project_id}/objects/missing")
    def missing(
        project_id: UUID, request: MissingObjects, principal: Principal = Depends(authenticate)
    ):
        principal.write(project_id)
        return {
            "missing": [
                identity
                for identity in dict.fromkeys(request.hashes)
                if not app.state.sync.cache.has(identity)
            ]
        }

    @router.get("/projects/{project_id}/objects/{sha256}")
    def get_object(project_id: UUID, sha256: str, principal: Principal = Depends(authenticate)):
        principal.project(project_id)
        path = app.state.sync.cache.path(sha256)
        if not path.is_file():
            raise WorkspaceError("object_missing", "File object is not cached", 404)
        return FileResponse(path, media_type="application/octet-stream")

    @router.put("/projects/{project_id}/objects/{sha256}")
    async def put_object(
        project_id: UUID,
        sha256: str,
        request: Request,
        principal: Principal = Depends(authenticate),
    ):
        principal.write(project_id)
        app.state.sync.cache.path(sha256)
        count = 0
        with tempfile.TemporaryFile() as stream:
            async for chunk in request.stream():
                count += len(chunk)
                if count > settings.max_bytes:
                    raise WorkspaceError("copy_limit", "Object exceeds size limit", 413)
                await run_in_threadpool(stream.write, chunk)
            stream.seek(0)
            identity, size = await run_in_threadpool(
                app.state.sync.cache.put, stream, settings.max_bytes, sha256
            )
        return {"sha256": identity, "size": size}

    @router.post("/projects/{project_id}/push", response_model=Revision)
    def push_project(
        project_id: UUID, request: PublishRequest, principal: Principal = Depends(authenticate)
    ):
        principal.write(project_id)
        return app.state.sync.publish(project_id, request)

    def sync_client():
        if not cloud_url or not cloud_token_file:
            raise WorkspaceError("cloud_not_configured", "Cloud connection is not configured")
        return SyncClient(
            app.state.sync, cloud_url, cloud_token_file.read_text(), allow_http=allow_http
        )

    @router.get("/monitor")
    def monitor_status(principal: Principal = Depends(authenticate)):
        principal.admin()
        monitor = app.state.monitor
        return {
            "running": bool(monitor.thread and monitor.thread.is_alive()),
            "batches": monitor.batches,
            "last_error": monitor.last_error,
        }

    @router.post("/cache/prune")
    def prune_cache(
        max_bytes: int = Query(268435456, ge=0), principal: Principal = Depends(authenticate)
    ):
        principal.admin()
        with app.state.manager.lock:
            return sync_client().prune_cache(max_bytes)

    @router.post("/checkouts", status_code=201)
    def checkout(request: CheckoutRequest, principal: Principal = Depends(authenticate)):
        principal.admin()
        return sync_client().checkout(request.project_id, request.name, request.paths)

    @router.post("/workspaces/{workspace_id}/materialize")
    def materialize(
        workspace_id: UUID,
        request: MaterializeRequest,
        principal: Principal = Depends(authenticate),
    ):
        principal.admin()
        return sync_client().materialize(workspace_id, request.paths)

    @router.get("/workspaces/{workspace_id}/status")
    def sync_status(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        principal.admin()
        return sync_client().status(workspace_id)

    @router.post("/workspaces/{workspace_id}/push")
    def push_workspace(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        principal.admin()
        return sync_client().push(workspace_id)

    @router.post("/workspaces/{workspace_id}/pull")
    def pull_workspace(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        principal.admin()
        return sync_client().pull(workspace_id)

    @router.post("/projects/{project_id}/snapshots", status_code=201, response_model=Snapshot)
    def create_snapshot(project_id: UUID, principal: Principal = Depends(authenticate)):
        principal.admin()
        principal.project(project_id)
        return app.state.snapshots.create(project_id)

    @router.get("/projects/{project_id}/snapshots", response_model=list[Snapshot])
    def list_snapshots(
        project_id: UUID,
        principal: Principal = Depends(authenticate),
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        principal.project(project_id)
        app.state.manager.repo.project_path(project_id)
        return [
            m
            for m in app.state.manager.repo.snapshots(project_id, limit, offset)
            if m["status"] == "READY"
        ]

    def allowed_snapshot(snapshot_id: UUID, principal: Principal):
        metadata = app.state.snapshots.get(snapshot_id)
        principal.project(metadata["project_id"])
        return metadata

    @router.get("/snapshots/{snapshot_id}", response_model=Snapshot)
    def get_snapshot(snapshot_id: UUID, principal: Principal = Depends(authenticate)):
        return allowed_snapshot(snapshot_id, principal)

    @router.get("/snapshots/{snapshot_id}/download")
    def get_download(snapshot_id: UUID, principal: Principal = Depends(authenticate)):
        allowed_snapshot(snapshot_id, principal)
        path = app.state.snapshots.archive_path(snapshot_id)
        if not path.is_file():
            raise WorkspaceError("snapshot_missing", "Snapshot archive missing", 404)
        return FileResponse(path, media_type="application/zip", filename=f"{snapshot_id}.zip")

    @router.post("/downloads", status_code=201, response_model=Workspace)
    def create_download(request: DownloadRequest, principal: Principal = Depends(authenticate)):
        principal.admin()
        if not cloud_url or not cloud_token_file:
            raise WorkspaceError("cloud_not_configured", "Cloud connection is not configured", 409)
        return download(
            app.state.manager,
            cloud_url,
            request.snapshot_id,
            request.name,
            cloud_token_file.read_text().strip(),
            allow_http=allow_http,
        )

    @router.post("/workspaces", status_code=201, response_model=Workspace)
    def create_workspace(request: CreateWorkspace, principal: Principal = Depends(authenticate)):
        principal.admin()
        return app.state.manager.create(request, principal.name)

    @router.get("/workspaces", response_model=list[Workspace])
    def list_workspaces(
        principal: Principal = Depends(authenticate),
        include_deleted: bool = False,
        limit: int = Query(100, ge=1, le=100),
        offset: int = Query(0, ge=0),
    ):
        return app.state.manager.repo.list(
            include_deleted=include_deleted,
            limit=limit,
            offset=offset,
            owner=None if principal.role == "admin" else principal.name,
        )

    @router.get("/workspaces/{workspace_id}", response_model=Workspace)
    def get_workspace(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        return app.state.manager.get(
            workspace_id, None if principal.role == "admin" else principal.name
        )

    @router.post("/workspaces/{workspace_id}/start", response_model=Workspace)
    def start(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        return app.state.manager.action(
            workspace_id, "start", None if principal.role == "admin" else principal.name
        )

    @router.post("/workspaces/{workspace_id}/stop", response_model=Workspace)
    def stop(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        return app.state.manager.action(
            workspace_id, "stop", None if principal.role == "admin" else principal.name
        )

    @router.delete("/workspaces/{workspace_id}", status_code=204)
    def delete(workspace_id: UUID, principal: Principal = Depends(authenticate)):
        app.state.manager.action(
            workspace_id, "delete", None if principal.role == "admin" else principal.name
        )
        return Response(status_code=204)

    app.include_router(router)
    app.include_router(source_router(app, authenticate))
    return app


def from_environment():
    return create_app(
        Settings.load(Path(os.environ["AWM_ENVIRONMENT"])),
        Path(os.environ["AWM_CREDENTIALS"]),
        cloud_url=os.environ.get("AWM_CLOUD_URL"),
        cloud_token_file=Path(os.environ["AWM_CLOUD_TOKEN_FILE"])
        if os.environ.get("AWM_CLOUD_TOKEN_FILE")
        else None,
        allow_http=os.environ.get("AWM_ALLOW_HTTP") == "1",
    )
