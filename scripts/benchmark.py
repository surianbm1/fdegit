"""Measure independent-copy cost on reproducible temporary fixtures."""

import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from runtime_paths import runtime_root

from app.config import Settings
from app.domain import CreateWorkspace
from app.manager import WorkspaceManager


def main():
    rows = []
    for label, count, size in (("small", 12, 4096), ("medium", 100, 16384), ("large", 400, 32768)):
        with tempfile.TemporaryDirectory(prefix="awm-benchmark-") as temporary:
            root = Path(temporary).resolve()
            source = root / "sources" / "fixture"
            source.mkdir(parents=True)
            for index in range(count):
                (source / f"file-{index:04}.txt").write_bytes(bytes([index % 251]) * size)
            state = root / "state"
            manager = WorkspaceManager(
                Settings(
                    environment="cloud",
                    project_path=source,
                    allowed_source_root=source.parent,
                    state_root=state,
                    workspace_root=state / "workspaces",
                    staging_root=state / "staging",
                    metadata_database=state / "metadata.sqlite3",
                    planned_port=8000,
                )
            )
            try:
                for concurrent_copies in (1, 5, 20):
                    timings, workspaces = [], []
                    for index in range(concurrent_copies):
                        start = time.perf_counter()
                        workspace = manager.create(
                            CreateWorkspace(
                                name=f"{label}-{concurrent_copies}-{index}", source_path=source
                            )
                        )
                        timings.append(time.perf_counter() - start)
                        workspaces.append(workspace)
                    used = sum(
                        p.stat().st_size
                        for w in workspaces
                        for p in w.path.rglob("*")
                        if p.is_file()
                    )
                    start = time.perf_counter()
                    for workspace in workspaces:
                        manager.action(workspace.workspace_id, "delete")
                    rows.append(
                        {
                            "fixture": label,
                            "source_files": count,
                            "source_bytes": count * size,
                            "coexisting_copies": concurrent_copies,
                            "mean_create_seconds": round(statistics.mean(timings), 4),
                            "max_create_seconds": round(max(timings), 4),
                            "workspace_bytes": used,
                            "total_delete_seconds": round(time.perf_counter() - start, 4),
                        }
                    )
            finally:
                manager.close()
    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "method": "Serialized creation; 1, 5, 20 coexisting ordinary copies; warm caches possible",
        "results": rows,
    }
    destination = runtime_root() / "benchmark.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
