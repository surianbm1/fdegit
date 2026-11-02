"""Hashed bearer credentials, explicit project grants, and admin operations."""

import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain import WorkspaceError


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    role: Literal["admin", "reader", "writer"]
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", exclude=True)
    projects: list[str] = Field(default_factory=list)
    expires_at: AwareDatetime | None = None

    def project(self, project_id):
        if self.role != "admin" and str(project_id) not in self.projects:
            raise WorkspaceError("project_missing", "Project not found", 404)

    def admin(self):
        if self.role != "admin":
            raise WorkspaceError("forbidden", "Administrator permission required", 403)

    def write(self, project_id):
        self.project(project_id)
        if self.role not in ("admin", "writer"):
            raise WorkspaceError("forbidden", "Project write permission required", 403)


class Credentials:
    def __init__(self, path: Path):
        self.path = path
        self.load()  # Fail closed at startup.

    def load(self):
        rows = json.loads(self.path.read_text(encoding="utf-8-sig"))
        principals = [Principal.model_validate(row) for row in rows]
        if not principals or len({p.name for p in principals}) != len(principals):
            raise ValueError("Credentials must contain unique principals")
        return principals

    def authenticate(self, token: str) -> Principal:
        candidate = hashlib.sha256(token.encode()).hexdigest()
        for principal in self.load():  # Revocation takes effect without a restart.
            if hmac.compare_digest(candidate, principal.token_sha256):
                if principal.expires_at and principal.expires_at <= datetime.now(UTC):
                    break
                return principal
        raise WorkspaceError("unauthorized", "Valid bearer token required", 401)
