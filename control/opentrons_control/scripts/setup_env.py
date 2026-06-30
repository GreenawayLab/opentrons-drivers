#!/usr/bin/env python3
"""
Generate the local ``.env`` for the control-plane stack.

Idempotent by construction: it only ever *appends* keys that are missing, and
never rewrites or overwrites an existing line. Re-running is safe — in fact it
is meant to be called on every launch (e.g. from ``launch.sh``), generating
secrets on the first run and doing nothing thereafter.

Why idempotency is not optional here: ``FERNET_KEY`` is the vault's encryption
key. Every secret in the database (robot SSH keys, the git token) is encrypted
with it. Regenerating it would leave those rows intact but permanently
undecryptable — a silent, unrecoverable data loss. So this script generates it
once and then leaves it alone forever.

Secrets are generated with the standard library only (a Fernet key is just
url-safe base64 of 32 random bytes), so the host needs no third-party packages.

Run from the directory containing ``docker-compose.yml`` (or set ``ENV_FILE``).
"""

from __future__ import annotations

import base64
import os
import secrets
import stat
from pathlib import Path

ENV_PATH = Path(os.environ.get("ENV_FILE", ".env"))


def _fernet_key() -> str:
    """A valid Fernet key: url-safe base64 of 32 random bytes."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _hex_secret() -> str:
    return secrets.token_hex(32)


# Secrets this script generates if absent. The names must match the fields in
# backend/app/settings/config.py (pydantic Settings) and docker-compose.yml.
GENERATED: dict[str, "callable[[], str]"] = {
    "FERNET_KEY": _fernet_key,        # vault encryption — generate ONCE, never rotate here
    "SECRET_KEY": _hex_secret,        # JWT signing
    "POSTGRES_PASSWORD": _hex_secret,  # database
}

REQUIRED_USER = {
    "GITHUB_REPO": "owner/repo  (the monorepo the maintainer builds from)",
}


def _existing_keys(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def main() -> None:
    present = _existing_keys(ENV_PATH)

    to_append = [
        (name, gen()) for name, gen in GENERATED.items() if name not in present
    ]

    if to_append:
        # Ensure we start on a fresh line, then append only the missing keys.
        prefix = ""
        if ENV_PATH.exists() and ENV_PATH.stat().st_size > 0:
            if not ENV_PATH.read_text().endswith("\n"):
                prefix = "\n"
        with ENV_PATH.open("a", encoding="utf-8") as f:
            f.write(prefix)
            for name, value in to_append:
                f.write(f"{name}={value}\n")
        # Secrets live here: keep it owner-only.
        try:
            ENV_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass
        print(f"generated and appended: {', '.join(n for n, _ in to_append)}")
    else:
        print("all generated secrets already present; nothing changed")

    missing_user = [k for k in REQUIRED_USER if k not in present and k not in dict(to_append)]
    for key in missing_user:
        print(f"  ACTION NEEDED: set {key}= in {ENV_PATH}  ({REQUIRED_USER[key]})")

    print(f"{ENV_PATH} ready")


if __name__ == "__main__":
    main()