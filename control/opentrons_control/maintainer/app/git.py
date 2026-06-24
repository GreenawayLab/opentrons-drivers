"""
Fetch the drivers source via a read-only git deploy key.

The deploy key is fetched from the backend (which reads it from the vault),
written to a tmpfs file for the duration of the clone, used via
``GIT_SSH_COMMAND``, and removed afterwards. The source is cloned fresh each
build (shallow), so there is no incremental-pull state to go stale.

The key is materialised here exactly as the backend materialises the robot SSH
keys (CRLF-normalised, trailing newline, mode 0600), but from raw bytes rather
than from the vault — the maintainer has no database of its own.

NOTE: ``materialize_key`` below is the maintainer-side twin of
``vault.materialize_key`` in the backend. The two packages are independent by
design (no cross-imports), so the normalisation rule is duplicated
deliberately — if you change it here, change it there too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from opentrons_control.maintainer.app.config import DRIVERS_SUBDIR
from opentrons_control.maintainer.app.config import GIT_REF
from opentrons_control.maintainer.app.config import KEYS_DIR
from opentrons_control.maintainer.app.config import REPO_URL
from opentrons_control.maintainer.app.config import SOURCE_DIR


class GitError(RuntimeError):
    """Raised when the source cannot be fetched."""


def materialize_key(data: bytes, path: Path) -> Path:
    """Write secret ``data`` to ``path`` as a private (0600) key file.

    Carriage returns are stripped and a trailing newline ensured, since ssh
    rejects keys with CRLF endings or a missing final newline. The parent
    directory is created if missing.

    :param data: Raw key bytes (from the backend credential endpoint).
    :param path: Destination file path, typically under a tmpfs directory.
    :returns: ``path``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    if not normalized.endswith(b"\n"):
        normalized += b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(normalized)
    return path


def clone_source(credential: bytes) -> Path:
    """Clone the repo at GIT_REF and return the drivers project directory.

    :param credential: Raw private deploy-key bytes from the backend.
    :returns: Path to ``<SOURCE_DIR>/<DRIVERS_SUBDIR>`` (the drivers project).
    :raises GitError: if REPO_URL is unset, the clone fails, or the expected
        drivers project is not present in the checkout.
    """
    if not REPO_URL:
        raise GitError("REPO_URL is not configured")

    key_path = materialize_key(credential, Path(KEYS_DIR) / "git_deploy")
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": (
            f"ssh -i {key_path} -o StrictHostKeyChecking=no -o BatchMode=yes"
        ),
    }
    src = Path(SOURCE_DIR)
    try:
        if src.exists():
            shutil.rmtree(src)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", GIT_REF, REPO_URL, str(src)],
            check=True,
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        raise GitError(f"git clone failed: {exc}") from exc
    finally:
        # The key never outlives the clone.
        key_path.unlink(missing_ok=True)

    project = src / DRIVERS_SUBDIR
    if not (project / "pyproject.toml").exists():
        raise GitError(f"no pyproject.toml at {project}; check DRIVERS_SUBDIR")
    return project