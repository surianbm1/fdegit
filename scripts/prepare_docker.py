"""Generate local Docker credentials without printing bearer tokens.

Existing credentials are preserved. Delete/revoke explicitly to rotate them.
"""

import hashlib
import json
import secrets
import shutil
from uuid import NAMESPACE_URL, uuid5

from runtime_paths import ROOT, runtime_root


def main():
    runtime = runtime_root()
    base = runtime / "secrets"
    legacy = ROOT / ".runtime" / "docker" / "secrets"
    if legacy.exists() and not base.exists():
        shutil.copytree(legacy, base)
    env_file = ROOT / ".env"
    content = env_file.read_text() if env_file.exists() else ""
    if "AWM_RUNTIME_DIR=" not in content:
        env_file.write_text(content.rstrip() + f'\nAWM_RUNTIME_DIR="{runtime.as_posix()}"\n')
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    project_id = str(uuid5(NAMESPACE_URL, "/data/sources/sample-app"))
    cloud_principals = []
    for name in ("cloud", "user1", "user2", "user3"):
        folder = base / name
        folder.mkdir(exist_ok=True)
        folder.chmod(0o700)
        admin_path = folder / "admin.token"
        if not admin_path.exists():
            admin_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
        admin_path.chmod(0o600)
        admin = {
            "name": name,
            "role": "admin",
            "projects": [],
            "token_sha256": hashlib.sha256(admin_path.read_bytes()).hexdigest(),
        }
        if name == "cloud":
            cloud_principals.append(admin)
        else:
            credential_path = folder / "credentials.json"
            if not credential_path.exists():
                credential_path.write_text(json.dumps([admin], indent=2), encoding="utf-8")
            remote_token = folder / "cloud.token"
            if not remote_token.exists():
                remote_token.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            cloud_principals.append(
                {
                    "name": name,
                    "role": "writer",
                    "projects": [project_id],
                    "token_sha256": hashlib.sha256(remote_token.read_bytes()).hexdigest(),
                }
            )
    cloud_credentials = base / "cloud" / "credentials.json"
    if not cloud_credentials.exists():
        cloud_credentials.write_text(json.dumps(cloud_principals, indent=2), encoding="utf-8")
    else:
        # The generated demo users now support push as well as pull. Preserve tokens/grants.
        existing = json.loads(cloud_credentials.read_text())
        generated = {row["name"]: row for row in cloud_principals}
        for row in existing:
            expected = generated.get(row["name"])
            if (
                expected
                and row["name"] != "cloud"
                and row["token_sha256"] == expected["token_sha256"]
                and row["role"] == "reader"
            ):
                row["role"] = "writer"
        cloud_credentials.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"Docker credentials ready under {base}; tokens were not printed.")


if __name__ == "__main__":
    main()
