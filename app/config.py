"""Explicit, read-only validation of local environment descriptors."""

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: Literal["cloud", "user1", "user2", "user3"]
    mode: Literal["local-simulation"] = "local-simulation"
    project_path: Path
    allowed_source_root: Path
    state_root: Path
    workspace_root: Path
    staging_root: Path
    metadata_database: Path
    host: Literal["127.0.0.1"] = "127.0.0.1"
    planned_port: int = Field(ge=1024, le=65535)
    application_configured: bool = False
    max_files: int = Field(default=100_000, gt=0)
    max_bytes: int = Field(default=1_073_741_824, gt=0)

    @property
    def snapshot_root(self) -> Path:
        return self.state_root / "snapshots"

    @field_validator(
        "project_path",
        "allowed_source_root",
        "state_root",
        "workspace_root",
        "staging_root",
        "metadata_database",
    )
    @classmethod
    def absolute_regular_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Paths must be absolute")
        for component in (value, *value.parents):
            if component.is_symlink() or component.is_junction():
                raise ValueError("Configured paths must not traverse links or junctions")
        return value.resolve()

    @model_validator(mode="after")
    def boundaries(self) -> Self:
        source, state = self.allowed_source_root, self.state_root
        if source.is_relative_to(state) or state.is_relative_to(source):
            raise ValueError("Source and state roots must be disjoint")
        if not self.project_path.is_relative_to(source):
            raise ValueError("Project must be beneath allowed source root")
        expected = {
            "workspace_root": state / "workspaces",
            "staging_root": state / "staging",
            "metadata_database": state / "metadata.sqlite3",
        }
        for field, path in expected.items():
            if getattr(self, field) != path:
                raise ValueError(f"{field} must be {path}")
        if not source.is_dir() or not self.project_path.is_dir():
            raise ValueError("Source root and project must exist as directories")
        return self

    @classmethod
    def load(cls, descriptor: Path) -> Self:
        # PowerShell-created descriptors may include a UTF-8 BOM.
        return cls.model_validate(json.loads(descriptor.read_text(encoding="utf-8-sig")))
