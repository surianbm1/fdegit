"""Validate an environment and optionally initialize its empty metadata database."""

import argparse
import json
import os
import sqlite3
import ssl
from pathlib import Path

from app.config import Settings
from app.domain import WorkspaceError
from app.storage import initialize_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, type=Path, help="Descriptor JSON path")
    parser.add_argument("--initialize", action="store_true", help="Create runtime dirs and schema")
    parser.add_argument("--serve", action="store_true", help="Run the authenticated API")
    parser.add_argument("--credentials", type=Path, help="Hashed server credentials JSON")
    parser.add_argument("--container", action="store_true", help="Bind inside Docker only")
    parser.add_argument("--server", help="Cloud origin for download")
    parser.add_argument("--snapshot", help="Snapshot UUID to download")
    parser.add_argument("--name", help="Local workspace name")
    parser.add_argument("--token-file", type=Path, help="Cloud bearer token file")
    parser.add_argument("--allow-http", action="store_true", help="Trust private Docker HTTP")
    parser.add_argument("--ca-file", type=Path, help="Trusted CA for a private HTTPS cloud")
    parser.add_argument(
        "--operation",
        choices=("checkout", "status", "push", "pull", "materialize"),
        help="Use the incremental file cache",
    )
    parser.add_argument("--project", help="Cloud project UUID for cached checkout")
    parser.add_argument("--workspace", help="Local workspace UUID for status/push/pull")
    parser.add_argument(
        "--paths",
        nargs="*",
        help="Sparse checkout selection, or paths to materialize; empty means no files",
    )
    args = parser.parse_args()
    try:
        settings = Settings.load(args.environment)
        if args.operation:
            if not (args.server and args.token_file):
                parser.error("Cached operations require --server and --token-file")
            from uuid import UUID

            from fastapi.encoders import jsonable_encoder

            from app.manager import WorkspaceManager
            from app.sync import SyncService
            from app.sync_client import SyncClient

            manager = WorkspaceManager(settings)
            try:
                client = SyncClient(
                    SyncService(manager),
                    args.server,
                    args.token_file.read_text(),
                    allow_http=args.allow_http,
                    verify=ssl.create_default_context(cafile=str(args.ca_file))
                    if args.ca_file
                    else True,
                )
                if args.operation == "checkout":
                    if not (args.project and args.name):
                        parser.error("checkout requires --project and --name")
                    result = client.checkout(UUID(args.project), args.name, args.paths)
                else:
                    if not args.workspace:
                        parser.error("status/push/pull require --workspace")
                    if args.operation == "materialize":
                        result = client.materialize(UUID(args.workspace), args.paths or [])
                    else:
                        result = getattr(client, args.operation)(UUID(args.workspace))
                print(json.dumps(jsonable_encoder(result), indent=2))
            finally:
                manager.close()
            return
        if args.serve:
            if not args.credentials:
                parser.error("--serve requires --credentials")
            if args.container and not Path("/.dockerenv").exists():
                parser.error("--container is only supported inside Docker")
            import uvicorn

            from app.main import create_app

            api = create_app(
                settings,
                args.credentials,
                cloud_url=os.environ.get("AWM_CLOUD_URL"),
                cloud_token_file=Path(os.environ["AWM_CLOUD_TOKEN_FILE"])
                if os.environ.get("AWM_CLOUD_TOKEN_FILE")
                else None,
                allow_http=os.environ.get("AWM_ALLOW_HTTP") == "1",
            )
            uvicorn.run(
                api,
                host="0.0.0.0" if args.container else settings.host,
                port=settings.planned_port,
                workers=1,
                proxy_headers=False,
            )
            return
        if args.snapshot:
            if not (args.server and args.name and args.token_file):
                parser.error("--snapshot requires --server, --name, and --token-file")
            from uuid import UUID

            from app.client import download
            from app.manager import WorkspaceManager

            manager = WorkspaceManager(settings)
            try:
                workspace = download(
                    manager,
                    args.server,
                    UUID(args.snapshot),
                    args.name,
                    args.token_file.read_text().strip(),
                    allow_http=args.allow_http,
                    verify=ssl.create_default_context(cafile=str(args.ca_file))
                    if args.ca_file
                    else True,
                )
                print(workspace.model_dump_json(indent=2))
            finally:
                manager.close()
            return
        if args.initialize:
            for folder in (settings.state_root, settings.workspace_root, settings.staging_root):
                folder.mkdir(parents=True, exist_ok=True)
            initialize_database(settings.metadata_database)
    except (OSError, ValueError, sqlite3.Error, WorkspaceError) as error:
        parser.exit(1, f"Environment setup failed: {error}\n")
    print(
        json.dumps(
            {
                "environment": settings.environment,
                "configuration_valid": True,
                "database_initialized": args.initialize,
                "database": str(settings.metadata_database),
                "api_running": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
