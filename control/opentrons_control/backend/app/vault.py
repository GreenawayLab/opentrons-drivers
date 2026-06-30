"""
Encrypted credential store.

Secrets live in the database as Fernet ciphertext and are only ever decrypted
in memory. An SSH key is written to a RAM-backed directory with private
permissions when a connection needs a file path; no plaintext reaches
persistent storage.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from opentrons_control.backend.app.settings.config import settings
from opentrons_control.backend.app.db.runner import execute, fetch_one

KEYS_DIR = Path("/run/keys")

_fernet = Fernet(settings.fernet_key.encode())


def get_secret(db: Session, name: str) -> bytes:
    """Return the decrypted bytes of a stored secret."""
    row = fetch_one(db, "secrets/get.sql", {"name": name})
    if row is None:
        raise KeyError(f"secret not found: {name}")
    return _fernet.decrypt(bytes(row["ciphertext"]))


def put_secret(db: Session, name: str, value: bytes, kind: str = "other") -> None:
    """Encrypt and store a secret, replacing any existing entry of that name."""
    token = _fernet.encrypt(value)
    execute(db, "secrets/put.sql", {"name": name, "ciphertext": token, "kind": kind})


def materialize_key(db: Session, name: str, dest_dir: Path = KEYS_DIR) -> Path:
    """
    Write a stored SSH key to a private file under dest_dir and return its path.

    Carriage returns are stripped and a trailing newline is ensured, since ssh
    rejects keys with CRLF line endings or a missing final newline. The file is
    created mode 0600.
    """
    raw = get_secret(db, name)
    normalized = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if not normalized.endswith(b"\n"):
        normalized += b"\n"

    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(normalized)
    return path