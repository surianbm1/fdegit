"""SQLite persistence, migrations and storage contracts."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from app.domain import Status, Workspace, WorkspaceError, normalized_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    canonical_path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(source_id),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    base_revision TEXT CHECK (base_revision IS NULL),
    status TEXT NOT NULL CHECK (status IN
      ('CREATING','READY','ACTIVE','STOPPED','DELETING','DELETED','ERROR')),
    copy_policy TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS live_workspace_name
ON workspaces(normalized_name) WHERE status != 'DELETED';
"""


def initialize_database(path: Path) -> None:
    """Initialize/migrate schema to v2; reject unknown future versions."""
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            raise ValueError(f"Unsupported database schema version: {version}")
        connection.executescript(SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workspaces)")}
        for name, declaration in {
            "owner": "TEXT NOT NULL DEFAULT 'local'",
            "snapshot_id": "TEXT",
            "remote_url": "TEXT",
        }.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE workspaces ADD COLUMN {name} {declaration}")
        connection.execute("""CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES sources(source_id),
            metadata TEXT NOT NULL)""")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS sync_revisions (
                revision_id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sync_heads (
                project_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, stat_index TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS workspace_sync (
                workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
                project_id TEXT NOT NULL, base_revision TEXT NOT NULL,
                manifest TEXT NOT NULL, stat_index TEXT NOT NULL);
        """)
        connection.execute("PRAGMA user_version = 3")


class SQLiteRepository:
    def __init__(self, database: Path, workspace_root: Path):
        self.database = database
        self.workspace_root = workspace_root

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return closing(connection)

    def source(self, path: str) -> UUID:
        canonical = os.path.normcase(path)
        identity = uuid5(NAMESPACE_URL, canonical)
        with self.connect() as db, db:
            db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (str(identity), canonical))
            return UUID(
                db.execute(
                    "SELECT source_id FROM sources WHERE canonical_path=?", (canonical,)
                ).fetchone()[0]
            )

    def projects(self) -> list[dict]:
        with self.connect() as db:
            return [
                {"project_id": r["source_id"], "name": Path(r["canonical_path"]).name}
                for r in db.execute("SELECT * FROM sources ORDER BY canonical_path")
                if not r["canonical_path"].startswith("remote:")
            ]

    def project_path(self, project_id: UUID) -> Path:
        with self.connect() as db:
            row = db.execute(
                "SELECT canonical_path FROM sources WHERE source_id=?", (str(project_id),)
            ).fetchone()
        if row is None or row[0].startswith("remote:"):
            raise WorkspaceError("project_missing", "Project not found", 404)
        return Path(row[0])

    def reserve(self, workspace: Workspace, copy_policy: str) -> None:
        try:
            with self.connect() as db, db:
                db.execute(
                    """INSERT INTO workspaces
                  (workspace_id,source_id,name,normalized_name,status,copy_policy,created_at,
                   updated_at,owner,snapshot_id,remote_url) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(workspace.workspace_id),
                        str(workspace.source_id),
                        workspace.name,
                        normalized_name(workspace.name),
                        workspace.status,
                        copy_policy,
                        workspace.created_at.isoformat(),
                        workspace.updated_at.isoformat(),
                        workspace.owner,
                        str(workspace.snapshot_id) if workspace.snapshot_id else None,
                        workspace.remote_url,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "normalized_name" in str(error):
                raise WorkspaceError("duplicate_name", "Workspace name already exists") from error
            raise

    def decode(self, row) -> Workspace:
        fields = dict(row)
        for key in ("normalized_name", "copy_policy"):
            fields.pop(key)
        fields["path"] = self.workspace_root / fields["workspace_id"]
        with self.connect() as db:
            sync = db.execute(
                "SELECT base_revision FROM workspace_sync WHERE workspace_id=?",
                (fields["workspace_id"],),
            ).fetchone()
        if sync:
            fields["base_revision"] = sync[0]
        return Workspace.model_validate(fields)

    def get(self, workspace_id: UUID) -> Workspace | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM workspaces WHERE workspace_id=?", (str(workspace_id),)
            ).fetchone()
        return self.decode(row) if row else None

    def list(
        self, *, include_deleted=False, limit=100, offset=0, owner: str | None = None
    ) -> list[Workspace]:
        clauses, args = [], []
        if not include_deleted:
            clauses.append("status != 'DELETED'")
        if owner is not None:
            clauses.append("owner = ?")
            args.append(owner)
        query = "SELECT * FROM workspaces"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at,workspace_id LIMIT ? OFFSET ?"
        with self.connect() as db:
            return [self.decode(row) for row in db.execute(query, [*args, limit, offset])]

    def set_status(
        self, workspace_id: UUID, expected: Status, target: Status, error: str | None = None
    ) -> None:
        with self.connect() as db, db:
            cursor = db.execute(
                """UPDATE workspaces SET status=?,updated_at=?,error=?
                WHERE workspace_id=? AND status=?""",
                (target, datetime.now(UTC).isoformat(), error, str(workspace_id), expected),
            )
            if cursor.rowcount != 1:
                raise WorkspaceError("concurrent_change", "Workspace state changed")

    def save_snapshot(self, metadata: dict) -> None:
        with self.connect() as db, db:
            db.execute(
                "INSERT INTO snapshots VALUES (?,?,?)",
                (metadata["snapshot_id"], metadata["project_id"], json.dumps(metadata)),
            )

    def snapshot(self, snapshot_id: UUID) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT metadata FROM snapshots WHERE snapshot_id=?", (str(snapshot_id),)
            ).fetchone()
        if not row:
            raise WorkspaceError("snapshot_missing", "Snapshot not found", 404)
        return json.loads(row[0])

    def snapshots(self, project_id: UUID, limit=100, offset=0) -> list[dict]:
        with self.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT metadata FROM snapshots WHERE project_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                    (str(project_id), limit, offset),
                )
            ]


class MetadataRepository(Protocol):
    def reserve(self, workspace: Workspace, copy_policy: str) -> None: ...
    def get(self, workspace_id: UUID) -> Workspace | None: ...
    def list(self, *, include_deleted: bool, limit: int, offset: int) -> list[Workspace]: ...
    def set_status(
        self,
        workspace_id: UUID,
        expected: Status,
        target: Status,
        error: str | None = None,
    ) -> None: ...


class WorkspaceMaterializer(Protocol):
    def copy(self, source: Path, workspace_id: UUID) -> None: ...
    def publish(self, workspace_id: UUID) -> Path: ...
    def remove(self, workspace_id: UUID) -> None: ...
