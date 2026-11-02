"""Server-side changesets, immutable trees, graph queries and three-way integration.

Uses the existing object store and revision log. Creating a changeset does not
move a ref or materialize a server working copy.
"""

import difflib
import io
import json
from collections import deque
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.domain import WorkspaceError
from app.filesystem import safe_relative
from app.snapshots import FileEntry
from app.sync import PublishRequest, Revision, changes, validate_tree


class ChangeRequest(BaseModel):
    base_revision: UUID
    files: dict[str, FileEntry] = Field(default_factory=dict)
    deleted: list[str] = Field(default_factory=list)
    directories_added: list[str] = Field(default_factory=list)
    directories_deleted: list[str] = Field(default_factory=list)
    message: str = ""


class RefRequest(BaseModel):
    expected: UUID | None = None
    revision_id: UUID


class IntegrationRequest(BaseModel):
    left: UUID
    right: UUID
    message: str = ""


def delta_response(base, target):
    delta = changes(base, target)
    return {
        **target.model_dump(mode="json", exclude={"files", "directories"}),
        "base_revision": str(base.revision_id),
        "files": {n: target.files[n].model_dump() for n in delta["added"] + delta["modified"]},
        "deleted": delta["deleted"],
        "directories_added": sorted(set(target.directories) - set(base.directories)),
        "directories_deleted": sorted(set(base.directories) - set(target.directories)),
    }


def apply_delta(base, data):
    if str(base.revision_id) != str(data["base_revision"]):
        raise WorkspaceError("stale_base", "Delta baseline does not match")
    files = dict(base.files)
    for name in data["deleted"]:
        files.pop(name, None)
    files.update({n: FileEntry.model_validate(e) for n, e in data["files"].items()})
    dirs = (set(base.directories) - set(data["directories_deleted"])) | set(
        data["directories_added"]
    )
    return Revision.model_validate(
        {
            **{k: v for k, v in data.items() if k in Revision.model_fields},
            "files": files,
            "directories": sorted(dirs),
        }
    )


class SourceControl:
    def __init__(self, sync):
        self.sync, self.repo, self.cache = sync, sync.repo, sync.cache
        with self.repo.connect() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS source_refs (
                project_id TEXT, name TEXT, revision_id TEXT NOT NULL,
                PRIMARY KEY(project_id,name))""")

    def create_repository(self, name):
        safe_relative(name)
        project = self.repo.source(f"objects:{uuid4()}/{name}")
        revision = Revision(
            revision_id=uuid4(),
            project_id=project,
            created_at=datetime.now(UTC).isoformat(),
            files={},
            directories=[],
            message="Initialize object-only repository",
        )
        with self.repo.connect() as db, db:
            self.sync.store_revision(revision, db)
            db.execute("INSERT INTO object_projects VALUES (?)", (str(project),))
            db.execute(
                "INSERT INTO sync_heads VALUES (?,?,?)",
                (str(project), str(revision.revision_id), "{}"),
            )
        return {"project_id": project, "revision_id": revision.revision_id, "name": name}

    def revision(self, project, identity):
        revision = self.sync.revision(identity)
        if revision.project_id != project:
            raise WorkspaceError("project_mismatch", "Revision belongs to another project", 404)
        return revision

    def commit(self, project, request, author="local"):
        base = self.revision(project, request.base_revision)
        data = request.model_dump(mode="json")
        data.update(
            revision_id=str(uuid4()),
            project_id=str(project),
            parent=str(base.revision_id),
            created_at=datetime.now(UTC).isoformat(),
            author=author,
            merge_parent=None,
        )
        revision = apply_delta(base, data)
        directories = set(revision.directories)
        for name in revision.files:
            directories.update(
                p.as_posix() for p in safe_relative(name).parents if p.as_posix() != "."
            )
        revision = revision.model_copy(update={"directories": sorted(directories)})
        validate_tree(revision.files, revision.directories, self.sync.manager.settings)
        for entry in request.files.values():
            if not self.cache.has(entry.sha256, entry.size):
                raise WorkspaceError("object_missing", "Upload changed objects before committing")
        self.sync.store_revision(revision)
        return revision

    def update_ref(self, project, name, request):
        safe_relative(name)
        self.revision(project, request.revision_id)
        with self.repo.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT revision_id FROM source_refs WHERE project_id=? AND name=?",
                (str(project), name),
            ).fetchone()
            current = row[0] if row else None
            if current != (str(request.expected) if request.expected else None):
                raise WorkspaceError("stale_ref", "Ref changed; reload and retry")
            db.execute(
                "INSERT OR REPLACE INTO source_refs VALUES (?,?,?)",
                (str(project), name, str(request.revision_id)),
            )
        return {"name": name, "revision_id": request.revision_id}

    def ancestors(self, project, identity):
        distances, queue = {}, deque([(identity, 0)])
        while queue:
            identity, distance = queue.popleft()
            if identity in distances:
                continue
            revision = self.revision(project, identity)
            distances[identity] = distance
            queue.extend((p, distance + 1) for p in (revision.parent, revision.merge_parent) if p)
        return distances

    def merge_base(self, project, left, right):
        a, b = self.ancestors(project, left), self.ancestors(project, right)
        common = a.keys() & b.keys()
        # Exclude ancestors of other common ancestors: only best bases qualify.
        best = set(common)
        for candidate in common:
            best.difference_update(set(self.ancestors(project, candidate)) - {candidate})
        if not best:
            raise WorkspaceError("unrelated_history", "No common ancestor")
        if len(best) != 1:
            raise WorkspaceError(
                "ambiguous_merge_base", "Multiple merge bases require explicit resolution"
            )
        return next(iter(best))

    def history(self, project, identity, path=None, limit=100):
        if path:
            safe_relative(path)
        result = []
        while identity and len(result) < limit:
            revision = self.revision(project, identity)
            parent = self.revision(project, revision.parent) if revision.parent else None
            if path is None or revision.files.get(path) != (
                parent.files.get(path) if parent else None
            ):
                result.append(revision.model_dump(mode="json", exclude={"files", "directories"}))
            identity = revision.parent
        return result

    def diff(self, project, left, right, path=None):
        a, b = self.revision(project, left), self.revision(project, right)
        result = changes(a, b)
        if path:
            safe_relative(path)
            try:
                before, after = self.content(a.files.get(path)), self.content(b.files.get(path))
                result["patch"] = "".join(
                    difflib.unified_diff(
                        before.decode("utf-8").splitlines(True),
                        after.decode("utf-8").splitlines(True),
                        fromfile=f"{left}/{path}",
                        tofile=f"{right}/{path}",
                    )
                )
            except UnicodeDecodeError:
                result["binary"] = True
        return result

    def content(self, entry):
        if entry is None:
            return b""
        if not self.cache.has(entry.sha256, entry.size):
            raise WorkspaceError("object_missing", "Historical file content is unavailable", 404)
        return self.cache.path(entry.sha256).read_bytes()

    def merge_file(self, base, left, right):
        if left == right or right == base:
            return left
        if left == base:
            return right
        if base is None or left is None or right is None:
            raise ValueError("add/add or delete/modify conflict")
        if left.executable != right.executable:
            raise ValueError("mode conflict")
        original = self.content(base).decode("utf-8").splitlines(True)
        edits = []
        for entry in (left, right):
            lines = self.content(entry).decode("utf-8").splitlines(True)
            edits.append(
                [
                    (i, j, lines[x:y])
                    for op, i, j, x, y in difflib.SequenceMatcher(
                        None, original, lines, autojunk=False
                    ).get_opcodes()
                    if op != "equal"
                ]
            )
        combined = list(edits[0])
        for edit in edits[1]:
            if edit in combined:
                continue
            a, b, _ = edit
            for c, d, _ in combined:
                if max(a, c) < min(b, d) or (a == b and c <= a <= d) or (c == d and a <= c <= b):
                    raise ValueError("overlapping line edits")
            combined.append(edit)
        for a, b, replacement in sorted(combined, key=lambda e: (e[0], e[1]), reverse=True):
            original[a:b] = replacement
        payload = "".join(original).encode("utf-8")
        identity, size = self.cache.put(io.BytesIO(payload), self.sync.manager.settings.max_bytes)
        return FileEntry(sha256=identity, size=size, executable=left.executable)

    def integrate(self, project, request, rebase=False, author="local"):
        left, right = self.revision(project, request.left), self.revision(project, request.right)
        base = self.revision(project, self.merge_base(project, request.left, request.right))
        if rebase:
            # Replay each first-parent commit, preserving its individual message.
            chain, cursor = [], left
            while cursor.revision_id != base.revision_id:
                if cursor.merge_parent:
                    raise WorkspaceError(
                        "merge_rebase", "Rebasing merge commits requires explicit resolution"
                    )
                chain.append(cursor)
                cursor = self.revision(project, cursor.parent)
            target = right
            for original in reversed(chain):
                parent = self.revision(project, original.parent)
                target = self._integrate_trees(
                    project, parent, target, original, False, original.message, author
                )
            return target
        return self._integrate_trees(project, base, left, right, True, request.message, author)

    def _integrate_trees(self, project, base, left, right, merge, message, author):
        files, conflicts = {}, []
        for name in sorted(base.files.keys() | left.files.keys() | right.files.keys()):
            try:
                entry = self.merge_file(
                    base.files.get(name), left.files.get(name), right.files.get(name)
                )
                if entry is not None:
                    files[name] = entry
            except (ValueError, UnicodeDecodeError):
                conflicts.append(name)
        if conflicts:
            raise WorkspaceError("merge_conflict", "Conflicting paths: " + ", ".join(conflicts))
        dirs = (set(left.directories) | (set(right.directories) - set(base.directories))) - (
            set(base.directories) - set(right.directories)
        )
        # Keep ancestors of retained files even when the other side deleted the directory.
        for name in files:
            dirs.update(p.as_posix() for p in safe_relative(name).parents if p.as_posix() != ".")
        validate_tree(files, sorted(dirs), self.sync.manager.settings)
        revision = Revision(
            revision_id=uuid4(),
            project_id=project,
            parent=left.revision_id,
            merge_parent=right.revision_id if merge else None,
            created_at=datetime.now(UTC).isoformat(),
            files=files,
            directories=sorted(dirs),
            message=message,
            author=author,
        )
        self.sync.store_revision(revision)
        return revision

    def blame(self, project, identity, path):
        safe_relative(path)
        chain, cursor = [], identity
        while cursor:
            revision = self.revision(project, cursor)
            chain.append(revision)
            cursor = revision.parent
        if path not in chain[0].files:
            raise WorkspaceError("file_missing", "Path is absent at this revision", 404)
        previous, labels = [], []
        for revision in reversed(chain):
            try:
                lines = self.content(revision.files.get(path)).decode("utf-8").splitlines(True)
            except UnicodeDecodeError as error:
                raise WorkspaceError(
                    "binary_file", "Line blame requires UTF-8 text", 422
                ) from error
            next_labels = [str(revision.revision_id)] * len(lines)
            for op, a, b, c, d in difflib.SequenceMatcher(
                None, previous, lines, autojunk=False
            ).get_opcodes():
                if op == "equal":
                    next_labels[c:d] = labels[a:b]
            previous, labels = lines, next_labels
        return [
            {"line": i + 1, "revision_id": label, "text": line}
            for i, (label, line) in enumerate(zip(labels, previous, strict=True))
        ]

    def tree(self, project, identity, prefix=""):
        revision = self.revision(project, identity)
        if prefix:
            safe_relative(prefix)

        # Immutable directory objects can be served by every cache tier.
        def build(directory):
            start = directory + "/" if directory else ""
            files = {
                n[len(start) :]: e.model_dump()
                for n, e in revision.files.items()
                if n.startswith(start) and "/" not in n[len(start) :]
            }
            children = {
                n[len(start) :]
                for n in revision.directories
                if n.startswith(start) and "/" not in n[len(start) :]
            }
            trees = {name: build(start + name) for name in sorted(children)}
            payload = json.dumps(
                {"files": files, "trees": trees}, sort_keys=True, separators=(",", ":")
            ).encode()
            return self.cache.put(io.BytesIO(payload), self.sync.manager.settings.max_bytes)[0]

        if prefix and prefix not in revision.directories:
            raise WorkspaceError("directory_missing", "Directory is absent", 404)
        return {"revision_id": identity, "tree": build(prefix), "prefix": prefix}

    def land(self, project, identity, expected):
        revision = self.revision(project, identity)
        return self.sync.publish(
            project,
            PublishRequest(
                base_revision=expected, files=revision.files, directories=revision.directories
            ),
            candidate=revision,
        )
