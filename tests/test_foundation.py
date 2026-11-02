import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.domain import CreateWorkspace, Status, WorkspaceError, normalized_name, transition
from app.storage import initialize_database


@pytest.fixture
def descriptor(tmp_path):
    source = tmp_path / "sources"
    project = source / "filegit"
    project.mkdir(parents=True)
    state = tmp_path / "state"
    return {
        "environment": "user1",
        "project_path": str(project),
        "allowed_source_root": str(source),
        "state_root": str(state),
        "workspace_root": str(state / "workspaces"),
        "staging_root": str(state / "staging"),
        "metadata_database": str(state / "metadata.sqlite3"),
        "planned_port": 8001,
    }


def test_descriptor_load_is_read_only_and_accepts_bom(tmp_path, descriptor):
    path = tmp_path / "environment.json"
    path.write_text(json.dumps(descriptor), encoding="utf-8-sig")
    settings = Settings.load(path)
    assert settings.environment == "user1"
    assert not settings.state_root.exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "0.0.0.0"),
        ("planned_port", 70000),
        ("max_bytes", 0),
        ("workspace_root", "relative"),
        ("environment", "unknown"),
    ],
)
def test_bad_configuration(descriptor, field, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({**descriptor, field: value})


@pytest.mark.parametrize("direction", ["state_inside_source", "source_inside_state", "equal"])
def test_overlapping_roots_rejected(descriptor, direction):
    source = descriptor["allowed_source_root"]
    if direction == "state_inside_source":
        descriptor["state_root"] = source + "/state"
    elif direction == "source_inside_state":
        descriptor["state_root"] = str(Path(source).parent)
    else:
        descriptor["state_root"] = source
    with pytest.raises(ValidationError, match="disjoint"):
        Settings.model_validate(descriptor)


def test_database_cannot_escape_state(descriptor, tmp_path):
    descriptor["metadata_database"] = str(tmp_path / "outside.sqlite3")
    with pytest.raises(ValidationError, match="metadata_database"):
        Settings.model_validate(descriptor)


@pytest.mark.parametrize(
    "status,action,target",
    [
        (Status.READY, "start", Status.ACTIVE),
        (Status.STOPPED, "start", Status.ACTIVE),
        (Status.ACTIVE, "start", Status.ACTIVE),
        (Status.ACTIVE, "stop", Status.STOPPED),
        (Status.STOPPED, "stop", Status.STOPPED),
        (Status.READY, "delete", Status.DELETING),
        (Status.ERROR, "delete", Status.DELETING),
        (Status.DELETED, "delete", Status.DELETED),
    ],
)
def test_supported_transitions(status, action, target):
    assert transition(status, action) == target


@pytest.mark.parametrize(
    "status,action",
    [
        (Status.ACTIVE, "delete"),
        (Status.READY, "stop"),
        (Status.DELETED, "start"),
        (Status.CREATING, "start"),
        (Status.DELETING, "delete"),
        (Status.ERROR, "start"),
    ],
)
def test_invalid_transitions(status, action):
    with pytest.raises(WorkspaceError) as error:
        transition(status, action)
    assert error.value.code == "invalid_transition"
    assert error.value.status_code == 409


def test_names_and_request_contract():
    assert normalized_name(" Payment-Agent ") == normalized_name("payment-agent")
    assert normalized_name("Ａgent") == normalized_name("agent")
    with pytest.raises(ValidationError):
        CreateWorkspace(name="   ", source_path="/example")
    with pytest.raises(ValidationError):
        CreateWorkspace(name="ok", source_path="/example", deletion_path="/anything")


def test_schema_persistence_and_live_name_constraint(tmp_path):
    database = tmp_path / "metadata.sqlite3"
    initialize_database(database)
    source_id = str(uuid4())
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO sources VALUES (?, ?)", (source_id, "/source"))

        def insert(name, status="READY"):
            db.execute(
                "INSERT INTO workspaces (workspace_id,source_id,name,normalized_name,status,"
                "copy_policy,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    source_id,
                    name,
                    normalized_name(name),
                    status,
                    "default-v1",
                    "2026-10-29T00:00:00Z",
                    "2026-10-29T00:00:00Z",
                ),
            )

        insert("Agent")
        with pytest.raises(sqlite3.IntegrityError):
            insert(" agent ")
        db.execute("UPDATE workspaces SET status = 'DELETED'")
        insert("agent")
        with pytest.raises(sqlite3.IntegrityError):
            insert("invalid", "UNKNOWN")
    initialize_database(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 2
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_future_database_version_rejected(tmp_path):
    database = tmp_path / "metadata.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="Unsupported"):
        initialize_database(database)
