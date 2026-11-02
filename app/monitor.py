"""Filesystem notifications refresh cached status; publication stays explicit."""

import logging
import threading

from watchfiles import watch


class WorkspaceMonitor:
    def __init__(self, sync, client_factory=None):
        self.sync, self.client_factory = sync, client_factory
        self.stop_event = threading.Event()
        self.thread = None
        self.batches = 0
        self.last_error = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="workspace-monitor", daemon=True)
        self.thread.start()
        return self

    def run(self):
        settings = self.sync.manager.settings
        roots = [settings.workspace_root, settings.project_path]
        for batch in watch(*roots, stop_event=self.stop_event, debounce=200, step=50):
            try:
                self.refresh([path for _, path in batch])
                self.batches += 1
                self.last_error = None
            except Exception as error:
                self.last_error = str(error)
                logging.getLogger(__name__).warning("Filesystem cache refresh failed: %s", error)

    def refresh(self, paths):
        from pathlib import Path
        from uuid import UUID

        settings = self.sync.manager.settings
        paths = [Path(path) for path in paths]
        if any(p.is_relative_to(settings.project_path) for p in paths):
            project = self.sync.repo.source(str(settings.project_path))
            self.sync.head(project)
        if self.client_factory:
            identities = set()
            for path in paths:
                if path.is_relative_to(settings.workspace_root):
                    relative = path.relative_to(settings.workspace_root)
                    if relative.parts:
                        try:
                            identities.add(UUID(relative.parts[0]))
                        except ValueError:
                            pass
            client = self.client_factory()
            for identity in identities:
                workspace = self.sync.manager.repo.get(identity)
                if workspace and workspace.remote_url and workspace.status in ("READY", "STOPPED"):
                    client.status(identity)

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
