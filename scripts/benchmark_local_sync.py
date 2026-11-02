"""Time filesystem sync on 100 independent 16 KiB files without touching projects."""

import argparse
import json
import platform
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from runtime_paths import runtime_root

from app.config import Settings
from app.local_sync import LocalSync


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--changed", type=int, default=1, choices=range(1, 101))
    count = parser.parse_args().changed
    samples = []
    for trial in range(5):
        with tempfile.TemporaryDirectory(prefix="filegit-sync-benchmark-") as temporary:
            root = Path(temporary).resolve()
            settings = {}
            for name in ("cloud", "user1", "user2", "user3"):
                source = root / name / "sources" / "project"
                source.mkdir(parents=True)
                for index in range(100):
                    (source / f"file-{index:03}.txt").write_bytes(bytes([index]) * 16384)
                state = root / name / "state"
                settings[name] = Settings(
                    environment=name,
                    project_path=source,
                    allowed_source_root=source.parent,
                    state_root=state,
                    workspace_root=state / "workspaces",
                    staging_root=state / "staging",
                    metadata_database=state / "metadata.sqlite3",
                    planned_port=8000,
                )

            def measured(label, name, operation="publish"):
                start = time.perf_counter()
                # Each command creates a fresh cache instance, matching the CLI.
                result = LocalSync(settings, root / "shared").run(name, operation)
                elapsed = time.perf_counter() - start
                samples.append(
                    {"trial": trial + 1, "operation": label, "seconds": elapsed, "result": result}
                )
                return result

            measured("first_push_cache_initialization", "user1")
            measured("first_pull_scan", "user2", "pull")
            assert measured("unchanged_push", "user1")["published_files"] == 0
            assert measured("unchanged_pull", "user2", "pull")["updated_files"] == 0
            untouched = {
                i: (settings["user2"].project_path / f"file-{i:03}.txt").stat().st_mtime_ns
                for i in range(count, 100)
            }
            for index in range(count):
                changed = settings["user1"].project_path / f"file-{index:03}.txt"
                changed.write_bytes(bytes([index + 100]) * 16384)
            pushed = measured(f"{count}_files_push", "user1")
            assert pushed["published_files"] == pushed["scan"]["files_read"] == count
            pulled = measured(f"{count}_files_pull", "user2", "pull")
            assert pulled["updated_files"] == count
            for index in range(100):
                filename = f"file-{index:03}.txt"
                expected = bytes([index + 100 if index < count else index]) * 16384
                for name in ("cloud", "user1", "user2"):
                    assert (settings[name].project_path / filename).read_bytes() == expected
                assert (settings["user3"].project_path / filename).read_bytes() == bytes(
                    [index]
                ) * 16384
                if index in untouched:
                    assert (
                        settings["user2"].project_path / filename
                    ).stat().st_mtime_ns == untouched[index]
    summary = {}
    for label in dict.fromkeys(sample["operation"] for sample in samples):
        times = [s["seconds"] for s in samples if s["operation"] == label]
        summary[label] = {
            "median_seconds": round(statistics.median(times), 4),
            "min_seconds": round(min(times), 4),
            "max_seconds": round(max(times), 4),
        }
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "files": 100,
        "bytes_per_file": 16384,
        "total_bytes": 1638400,
        "trials": 5,
        "changed_files": count,
        "changed_bytes": count * 16384,
        "summary": summary,
        "samples": samples,
        "notes": "Local temporary disk; OS cache may be warm. Includes sync engine construction and work, excludes Python startup, fixture creation and cleanup. First push initializes cache/baselines; not an initial project download.",
    }
    output = runtime_root() / f"filesystem-benchmark-100-changed-{count}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2))
    print(json.dumps({"summary": summary, "report": str(output)}, indent=2))


if __name__ == "__main__":
    main()
