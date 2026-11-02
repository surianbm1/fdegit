"""Filesystem-only publishing with cached objects and conflict-safe fan-out."""

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.cache import ObjectCache, scan
from app.config import Settings
from app.sync import Revision, TreeUpdater, validate_tree


class LocalSync:
    def __init__(self, settings, state):
        self.settings = settings
        self.state = state
        state.mkdir(parents=True, exist_ok=True)
        self.cache = ObjectCache(state)
        self.updater = TreeUpdater(self.cache, state)
        self.record = state / "sync.json"

    @contextmanager
    def locked(self):
        with (self.state / "publish.lock").open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                if handle.read(1) == b"":
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                self.data = (
                    json.loads(self.record.read_text())
                    if self.record.exists()
                    else {"bases": {}, "scans": {}, "versions": {}}
                )
                for job in list(self.updater.jobs.iterdir()):
                    journal = job / "journal.json"
                    if not journal.exists():
                        continue
                    saved = json.loads(journal.read_text())
                    name = saved["marker"]["name"]
                    if self.data["versions"].get(name) == saved["new"]["revision_id"]:
                        self.updater.finish(job)
                    else:
                        self.updater.rollback(self.settings[name].project_path, job)
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def save(self):
        temporary = self.record.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data))
        temporary.replace(self.record)

    def read(self, name):
        old = self.data["scans"].get(name, {})
        files, dirs, index, stats = scan(
            self.settings[name].project_path,
            self.cache,
            self.settings[name],
            old.get("files"),
            old.get("index"),
        )
        self.data["scans"][name] = {"files": files, "index": index}
        return files, dirs, stats

    @staticmethod
    def revision(files, dirs):
        return Revision(
            revision_id=uuid4(),
            project_id=UUID(int=0),
            created_at=datetime.now(UTC).isoformat(),
            files=files,
            directories=dirs,
        )

    def write(self, name, before, after, dirs):
        directories = set(dirs)
        for path in after:
            directories.update(p.as_posix() for p in Path(path).parents if p.as_posix() != ".")
        # Remove directories that have become files.
        directories.difference_update(after)
        old = self.revision(before, dirs)
        new = self.revision(after, sorted(directories))
        validate_tree(new.files, new.directories, self.settings[name])
        job = self.updater.apply(
            self.settings[name].project_path, old, new, f"local-{uuid4()}", marker={"name": name}
        )
        try:
            self.data["versions"][name] = str(new.revision_id)
            self.save()
        except Exception:
            self.updater.rollback(self.settings[name].project_path, job)
            raise
        self.updater.finish(job)

    def initialize(self):
        cloud, _, _ = self.read("cloud")
        for name in self.settings:
            if name != "cloud":
                self.data["bases"].setdefault(name, dict(cloud))
        self.save()
        return cloud

    def refresh(self, name, cloud):
        current, dirs, _ = self.read(name)
        base = self.data["bases"][name]
        updated, next_base, conflicts = dict(current), dict(base), []
        for path in base.keys() | cloud.keys():
            old, remote, local = base.get(path), cloud.get(path), current.get(path)
            if remote == old:
                continue
            if local not in (old, remote):
                conflicts.append(path)
                continue
            if remote is None:
                updated.pop(path, None)
                next_base.pop(path, None)
            else:
                updated[path] = remote
                next_base[path] = remote
        if conflicts:
            return {"updated_files": 0, "conflicts": conflicts}
        if updated != current:
            self.write(name, current, updated, dirs)
        self.data["bases"][name] = next_base
        self.save()
        return {
            "updated_files": sum(
                current.get(p) != updated.get(p) for p in current.keys() | updated.keys()
            ),
            "conflicts": conflicts,
        }

    def run(self, name, operation="publish", paths=None):
        with self.locked():
            cloud = self.initialize()
            if operation == "pull":
                return self.refresh(name, cloud)
            current, _, stats = self.read(name)
            base = self.data["bases"][name]
            changed = {p for p in base.keys() | current.keys() if base.get(p) != current.get(p)}
            if paths is not None:
                changed.intersection_update(paths)
            conflicts = [p for p in changed if cloud.get(p) not in (base.get(p), current.get(p))]
            if conflicts:
                raise ValueError(
                    "Cloud conflicts; files preserved: " + ", ".join(sorted(conflicts))
                )
            target = dict(cloud)
            for path in changed:
                if path in current:
                    target[path] = current[path]
                else:
                    target.pop(path, None)
            _, dirs, _ = self.read("cloud")
            if target != cloud:
                self.write("cloud", cloud, target, dirs)
            for path in changed:
                if path in current:
                    base[path] = current[path]
                else:
                    base.pop(path, None)
            self.save()
            return {
                "published_files": len(changed),
                "scan": stats,
                "message": "Cloud updated. Other users must pull explicitly.",
            }


def main():
    import sys

    root = Path(os.environ["LOCALAPPDATA"]) / "AgentWorkspaceManager"
    settings = {
        name: Settings.load(root / "environments" / name / "environment.json")
        for name in ("cloud", "user1", "user2", "user3")
    }
    name = next(
        (
            name
            for name, config in settings.items()
            if config.project_path.resolve() == Path.cwd().resolve()
        ),
        None,
    )
    if name not in ("user1", "user2", "user3"):
        raise SystemExit("Run this command in a user project folder")
    try:
        result = LocalSync(settings, root / "filesystem-sync").run(
            name, "pull" if "pull" in sys.argv else "publish"
        )
        print(json.dumps(result, indent=2))
    except Exception as error:
        raise SystemExit(f"Sync failed: {error}") from error


if __name__ == "__main__":
    main()
