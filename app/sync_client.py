"""Incremental cached checkout, status, push and pull operations."""

import io
import json
import os
from datetime import UTC, datetime
from uuid import UUID

import httpx

from app.cache import fingerprint, scan
from app.client import validate_server
from app.domain import Status, WorkspaceError
from app.filesystem import safe_relative
from app.snapshots import FileEntry
from app.source_control import apply_delta
from app.sync import Revision, changes, index_for, is_changed, validate_tree


class SyncClient:
    def __init__(
        self, service, server: str, token: str, *, allow_http=False, transport=None, verify=True
    ):
        self.service, self.manager = service, service.manager
        self.server = validate_server(server, allow_http)
        self.token, self.transport, self.verify = token.strip(), transport, verify

    def http(self):
        return httpx.Client(
            base_url=self.server,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=120,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
            verify=self.verify,
        )

    @staticmethod
    def response(response):
        if response.is_error:
            try:
                error = response.json()
            except ValueError:
                response.raise_for_status()
            raise WorkspaceError(
                error.get("code", "cloud_error"),
                error.get("message", "Cloud request failed"),
                response.status_code,
            )
        response.raise_for_status()
        return response.json()

    def manifest(self, http, path):
        revision = Revision.model_validate(self.response(http.get(path)))
        validate_tree(revision.files, revision.directories, self.manager.settings)
        return revision

    def remote_delta(self, http, base):
        data = self.response(
            http.get(f"/projects/{base.project_id}/delta", params={"base": str(base.revision_id)})
        )
        revision = apply_delta(base, data)
        validate_tree(revision.files, revision.directories, self.manager.settings)
        return revision

    def checkout_tree(self, http, project_id):
        header = self.response(http.get(f"/projects/{project_id}/checkout-head"))
        files, directories = {}, []
        fetched, metadata_bytes = 0, 0
        pending = [("", header.pop("tree"))]
        header.pop("prefix", None)
        while pending:
            prefix, identity = pending.pop()
            if not self.service.cache.has(identity):
                url = (
                    os.environ.get("AWM_OBJECT_URL", "").rstrip("/")
                    + f"/projects/{project_id}/objects/{identity}"
                )
                response = http.get(url)
                if response.is_error:
                    self.response(response)
                self.service.cache.put(
                    io.BytesIO(response.content), self.manager.settings.max_bytes, identity
                )
                fetched += 1
                metadata_bytes += len(response.content)
            tree = json.loads(self.service.cache.path(identity).read_bytes())
            for name, entry in tree["files"].items():
                if len(safe_relative(name).parts) != 1:
                    raise WorkspaceError(
                        "invalid_tree", "Tree entry must be an immediate child", 422
                    )
                files[prefix + name] = FileEntry.model_validate(entry)
            for name, child in tree["trees"].items():
                if len(safe_relative(name).parts) != 1:
                    raise WorkspaceError(
                        "invalid_tree", "Tree entry must be an immediate child", 422
                    )
                directories.append(prefix + name)
                pending.append((prefix + name + "/", child))
            if len(files) + len(directories) > self.manager.settings.max_files:
                raise WorkspaceError("copy_limit", "Tree contains too many entries", 413)
        revision = Revision.model_validate(
            {**header, "files": files, "directories": sorted(directories)}
        )
        validate_tree(revision.files, revision.directories, self.manager.settings)
        return revision, {"downloaded_trees": fetched, "metadata_bytes": metadata_bytes}

    def fetch(self, http, revision, store=True):
        downloaded, size, hits = 0, 0, 0
        unique = {entry.sha256: entry for entry in revision.files.values()}
        for identity, entry in unique.items():
            if self.service.cache.has(identity, entry.size):
                hits += 1
                continue
            # Stream into a temporary file rather than retaining whole projects in memory.
            import tempfile

            with tempfile.TemporaryFile() as stream:
                with http.stream(
                    "GET",
                    (
                        os.environ.get("AWM_OBJECT_URL", "").rstrip("/")
                        + f"/projects/{revision.project_id}/objects/{identity}"
                    ),
                ) as response:
                    if response.status_code != 200:
                        response.read()
                        self.response(response)
                    count = 0
                    for chunk in response.iter_bytes():
                        count += len(chunk)
                        if count > entry.size:
                            raise WorkspaceError(
                                "copy_limit", "Object exceeds manifest length", 413
                            )
                        stream.write(chunk)
                if count != entry.size:
                    raise WorkspaceError("checksum_mismatch", "Object length mismatch", 422)
                stream.seek(0)
                self.service.cache.put(stream, self.manager.settings.max_bytes, identity)
            downloaded += 1
            size += entry.size
        if store:
            self.service.store_revision(revision)
        return {"downloaded_files": downloaded, "downloaded_bytes": size, "cache_hits": hits}

    def save(self, workspace_id, revision, index, db=None):
        if db is None:
            with self.manager.repo.connect() as connection, connection:
                self.save(workspace_id, revision, index, connection)
            self.prune_cache()
        else:
            self.service.store_revision(revision, db)
            db.execute(
                "INSERT OR REPLACE INTO workspace_sync VALUES (?,?,?,?,?)",
                (
                    str(workspace_id),
                    str(revision.project_id),
                    str(revision.revision_id),
                    revision.model_dump_json(),
                    json.dumps(index),
                ),
            )

    def prune_cache(self, max_bytes=None):
        """Pin rollback baselines and unsent edits before automatic client eviction."""
        pinned = set()
        with self.manager.repo.connect() as db:
            rows = db.execute(
                "SELECT s.manifest,s.stat_index FROM workspace_sync s JOIN workspaces w ON s.workspace_id=w.workspace_id WHERE w.status != 'DELETED'"
            ).fetchall()
        for row in rows:
            pinned.update(e["sha256"] for e in json.loads(row[0])["files"].values())
            pinned.update(
                e["entry"]["sha256"] for e in json.loads(row[1]).values() if isinstance(e, dict)
            )
        for journal in self.service.updater.jobs.glob("*/journal.json"):
            data = json.loads(journal.read_text())
            for revision in (data["old"], data["new"]):
                pinned.update(e["sha256"] for e in revision["files"].values())
        limit = (
            max_bytes
            if max_bytes is not None
            else int(os.environ.get("AWM_CACHE_BYTES", 256 * 1024 * 1024))
        )
        return self.service.cache.prune(limit, pinned)

    def state(self, workspace_id):
        with self.manager.repo.connect() as db:
            row = db.execute(
                "SELECT * FROM workspace_sync WHERE workspace_id=?", (str(workspace_id),)
            ).fetchone()
        return row

    def attach(self, workspace_id):
        workspace = self.manager.get(workspace_id)
        if workspace.remote_url != self.server:
            raise WorkspaceError("wrong_cloud", "Workspace belongs to another cloud")
        row = self.state(workspace_id)
        if row:
            return row
        if workspace.snapshot_id is None:
            raise WorkspaceError("not_linked", "Use cached checkout for this local-only workspace")
        with self.http() as http:
            revision = self.manifest(http, f"/snapshots/{workspace.snapshot_id}/revision")
            # Adopt existing downloaded bytes instead of downloading them again.
            index = {}
            for name, entry in revision.files.items():
                path = workspace.path / name
                if path.is_file():
                    identity, size = self.service.cache.put_file(
                        path, self.manager.settings.max_bytes
                    )
                    if identity == entry.sha256 and size == entry.size:
                        index[name] = fingerprint(path)
            self.fetch(http, revision)
            self.save(workspace_id, revision, index)
        return self.state(workspace_id)

    def selected(self, workspace_id):
        with self.manager.repo.connect() as db:
            row = db.execute(
                "SELECT paths FROM sparse_workspaces WHERE workspace_id=?", (str(workspace_id),)
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def project(revision, paths):
        if paths is None:
            return revision

        def matches(name):
            return any(name == p or name.startswith(p + "/") for p in paths)

        files = {n: e for n, e in revision.files.items() if matches(n)}
        dirs = {n for n in revision.directories if matches(n)}
        for name in files:
            dirs.update(p.as_posix() for p in safe_relative(name).parents if p.as_posix() != ".")
        return revision.model_copy(update={"files": files, "directories": sorted(dirs)})

    def workspace_index(self, workspace, revision):
        return index_for(
            workspace.path, self.project(revision, self.selected(workspace.workspace_id))
        )

    def checkout(self, project_id: UUID, name: str, paths=None):
        with self.manager.lock, self.http() as http:
            if paths is not None:
                paths = sorted(set(safe_relative(p).as_posix() for p in paths))
            revision, tree_stats = self.checkout_tree(http, project_id)
            if revision.project_id != project_id:
                raise WorkspaceError("project_mismatch", "Cloud returned another project", 422)
            selected = self.project(revision, paths)
            # Store the full metadata baseline; only selected content is fetched.
            stats = self.fetch(http, selected, store=False)
            stats.update(tree_stats)
            self.service.store_revision(revision)
            source_id = self.manager.repo.source(f"remote:{self.server}/projects/{project_id}")
            workspace = self.manager.reserve(
                name, source_id, self.manager.settings.environment, remote_url=self.server
            )
            try:
                stage = self.manager.fs.stage(workspace.workspace_id)
                stage.mkdir()
                for directory in selected.directories:
                    (stage / directory).mkdir(parents=True, exist_ok=True)
                for path, entry in selected.files.items():
                    self.service.cache.materialize(entry, stage / path)
                self.manager.fs.publish(workspace.workspace_id)
                with self.manager.repo.connect() as db, db:
                    if paths is not None:
                        db.execute(
                            "INSERT INTO sparse_workspaces VALUES (?,?)",
                            (str(workspace.workspace_id), json.dumps(paths)),
                        )
                    self.save(
                        workspace.workspace_id, revision, index_for(workspace.path, selected), db
                    )
                    db.execute(
                        "UPDATE workspaces SET status='READY',updated_at=? WHERE workspace_id=?",
                        (datetime.now(UTC).isoformat(), str(workspace.workspace_id)),
                    )
                return {"workspace": self.manager.get(workspace.workspace_id), **stats}
            except Exception as error:
                self.manager.failed(workspace, error)
                raise

    def scan_workspace(self, workspace_id):
        workspace = self.manager.get(workspace_id)
        if workspace.status not in (Status.READY, Status.STOPPED):
            raise WorkspaceError("workspace_busy", "Stop the workspace before syncing")
        row = self.attach(workspace_id)
        base = Revision.model_validate_json(row["manifest"])
        cached_index = json.loads(row["stat_index"])
        cached_files = {
            name: item["entry"] if isinstance(item, dict) else base.files.get(name)
            for name, item in cached_index.items()
        }
        cached_stats = {
            name: item["stat"] if isinstance(item, dict) else item
            for name, item in cached_index.items()
        }
        files, directories, index, stats = scan(
            workspace.path, self.service.cache, self.manager.settings, cached_files, cached_stats
        )
        paths = self.selected(workspace_id)
        if paths is not None:
            selected = self.project(base, paths)
            unexpected = set(files) - set(
                self.project(base.model_copy(update={"files": files}), paths).files
            )
            if unexpected:
                raise WorkspaceError(
                    "outside_sparse",
                    "Materialize paths before editing outside the sparse selection",
                )
            files = {**{n: e for n, e in base.files.items() if n not in selected.files}, **files}
            directories = sorted(
                (set(base.directories) - set(selected.directories)) | set(directories)
            )
        current = base.model_copy(
            update={
                "files": {name: FileEntry.model_validate(entry) for name, entry in files.items()},
                "directories": directories,
            }
        )
        return workspace, base, current, index, stats

    def status(self, workspace_id):
        with self.manager.lock:
            workspace, base, current, index, stats = self.scan_workspace(workspace_id)
            # Cache the scanned hashes even for edits, so repeated status reads no content.
            # Persist current scan separately from baseline; dirty hashes must not masquerade as base.
            self.scan_cache(workspace_id, current, index)
            return {
                "workspace_id": workspace_id,
                "base_revision": base.revision_id,
                **changes(base, current),
                **stats,
            }

    def scan_cache(self, workspace_id, current, index, db=None):
        # Index includes both fingerprint and content entry; baseline remains immutable.
        encoded = {
            name: {"stat": value, "entry": current.files[name].model_dump()}
            for name, value in index.items()
        }
        if db is None:
            with self.manager.repo.connect() as connection, connection:
                return self.scan_cache(workspace_id, current, index, connection)
        db.execute(
            "UPDATE workspace_sync SET stat_index=? WHERE workspace_id=?",
            (json.dumps(encoded), str(workspace_id)),
        )

    def materialize(self, workspace_id, paths):
        """Expand a sparse working copy on demand without touching existing edits."""
        with self.manager.lock, self.http() as http:
            workspace, base, current, index, _ = self.scan_workspace(workspace_id)
            selected = self.selected(workspace_id)
            if selected is None:
                return {"downloaded_files": 0, "materialized_files": 0}
            paths = sorted(set(selected) | {safe_relative(p).as_posix() for p in paths})
            old = self.project(base, selected)
            new = self.project(base, paths)
            added = {n: e for n, e in new.files.items() if n not in old.files}
            for name in added:
                if (workspace.path / name).exists():
                    raise WorkspaceError("local_changes", "Expansion would overwrite local files")
            stats = self.fetch(http, new.model_copy(update={"files": added}), store=False)
            job = self.service.updater.apply(
                workspace.path, old, new, f"expand-{workspace_id}", marker=paths
            )
            try:
                index.update({n: fingerprint(workspace.path / n) for n in added})
                with self.manager.repo.connect() as db, db:
                    db.execute(
                        "UPDATE sparse_workspaces SET paths=? WHERE workspace_id=?",
                        (json.dumps(paths), str(workspace_id)),
                    )
                    self.scan_cache(workspace_id, current, index, db)
            except Exception:
                self.service.updater.rollback(workspace.path, job)
                raise
            self.service.updater.finish(job)
            return {**stats, "materialized_files": len(added), "paths": paths}

    def push(self, workspace_id):
        with self.manager.lock, self.http() as http:
            workspace, base, current, index, stats = self.scan_workspace(workspace_id)
            head = self.remote_delta(http, base)
            if head.revision_id != base.revision_id:
                if not is_changed(changes(current, head)):
                    # Retry after an interrupted response: cloud already has these bytes.
                    self.save(workspace_id, head, index)
                    return {
                        "workspace": self.manager.get(workspace_id),
                        "revision_id": head.revision_id,
                        **changes(base, head),
                        **stats,
                        "uploaded_files": 0,
                        "uploaded_bytes": 0,
                        "already_current": True,
                    }
                # Identical legacy snapshot content can safely adopt the initial cached head.
                if is_changed(changes(base, head)):
                    raise WorkspaceError("stale_base", "Cloud changed; pull before pushing")
                base = head
            delta = changes(base, current)
            uploaded, uploaded_bytes = 0, 0
            if is_changed(delta):
                changed_entries = {
                    current.files[name].sha256: current.files[name]
                    for name in delta["added"] + delta["modified"]
                }
                missing = self.response(
                    http.post(
                        f"/projects/{base.project_id}/objects/missing",
                        json={"hashes": list(changed_entries)},
                    )
                )["missing"]
                for identity in missing:
                    with self.service.cache.path(identity).open("rb") as stream:
                        self.response(
                            http.put(
                                f"/projects/{base.project_id}/objects/{identity}", content=stream
                            )
                        )
                    uploaded += 1
                    uploaded_bytes += changed_entries[identity].size
                new = apply_delta(
                    base,
                    self.response(
                        http.post(
                            f"/projects/{base.project_id}/push-delta",
                            json={
                                "base_revision": str(base.revision_id),
                                "files": {
                                    name: entry.model_dump()
                                    for name, entry in current.files.items()
                                    if name in delta["added"] + delta["modified"]
                                },
                                "deleted": delta["deleted"],
                                "directories_added": sorted(
                                    set(current.directories) - set(base.directories)
                                ),
                                "directories_deleted": sorted(
                                    set(base.directories) - set(current.directories)
                                ),
                            },
                        )
                    ),
                )
            else:
                new = head
            self.save(workspace_id, new, index)
            return {
                "workspace": self.manager.get(workspace_id),
                "revision_id": new.revision_id,
                **delta,
                **stats,
                "uploaded_files": uploaded,
                "uploaded_bytes": uploaded_bytes,
            }

    @staticmethod
    def manifest_response(data):
        return Revision.model_validate(data)

    def pull(self, workspace_id):
        with self.manager.lock, self.http() as http:
            workspace, base, current, index, stats = self.scan_workspace(workspace_id)
            new = self.remote_delta(http, base)
            local_delta = changes(base, current)
            remote_delta = changes(base, new)
            if is_changed(local_delta):
                if not is_changed(changes(current, new)):
                    self.save(workspace_id, new, index)
                    return {
                        "workspace": self.manager.get(workspace_id),
                        "revision_id": new.revision_id,
                        "downloaded_files": 0,
                        "downloaded_bytes": 0,
                        "cache_hits": 0,
                        "updated_files": 0,
                        "already_current": True,
                        **stats,
                    }
                if not is_changed(remote_delta):
                    self.scan_cache(workspace_id, current, index)
                    return {
                        "workspace": workspace,
                        "revision_id": base.revision_id,
                        "local_changes_preserved": True,
                        "downloaded_files": 0,
                        "downloaded_bytes": 0,
                        "cache_hits": 0,
                        "updated_files": 0,
                        **stats,
                    }
                raise WorkspaceError(
                    "local_changes", "Push or save local changes before pulling cloud updates"
                )
            paths = self.selected(workspace_id)
            selected_base, selected_new = self.project(base, paths), self.project(new, paths)
            transfer = self.fetch(http, selected_new, store=False)
            if is_changed(remote_delta):
                job = self.service.updater.apply(
                    workspace.path, selected_base, selected_new, f"workspace-{workspace_id}"
                )
                try:
                    self.save(workspace_id, new, self.workspace_index(workspace, new))
                except Exception:
                    self.service.updater.rollback(workspace.path, job)
                    raise
                self.service.updater.finish(job)
            else:
                self.save(workspace_id, new, index)
            return {
                "workspace": self.manager.get(workspace_id),
                "revision_id": new.revision_id,
                **remote_delta,
                **stats,
                **transfer,
                "updated_files": len(remote_delta["added"]) + len(remote_delta["modified"]),
            }
