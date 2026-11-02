"""Workspace contracts independent of HTTP and filesystem implementation."""

from enum import StrEnum
from pathlib import Path
from typing import Literal
from unicodedata import normalize
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class Status(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    DELETING = "DELETING"
    DELETED = "DELETED"
    ERROR = "ERROR"


def transition(status: Status, action: Literal["start", "stop", "delete"]) -> Status:
    targets = {
        "start": {
            Status.READY: Status.ACTIVE,
            Status.STOPPED: Status.ACTIVE,
            Status.ACTIVE: Status.ACTIVE,
        },
        "stop": {Status.ACTIVE: Status.STOPPED, Status.STOPPED: Status.STOPPED},
        "delete": {
            Status.READY: Status.DELETING,
            Status.STOPPED: Status.DELETING,
            Status.ERROR: Status.DELETING,
            Status.DELETED: Status.DELETED,
        },
    }
    target = targets.get(action, {}).get(status)
    if target is None:
        raise WorkspaceError("invalid_transition", f"Cannot {action} a {status} workspace")
    return target


def normalized_name(name: str) -> str:
    return normalize("NFKC", name).strip().casefold()


class CreateWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    source_path: Path

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("Name must contain printable, non-whitespace characters")
        return value


class Workspace(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    source_id: UUID
    name: str
    base_revision: UUID | None = None
    path: Path
    status: Status
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error: str | None = None
    owner: str = "local"
    snapshot_id: UUID | None = None
    remote_url: str | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str
