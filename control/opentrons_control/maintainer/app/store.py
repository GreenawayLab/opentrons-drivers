"""
Versioned wheel store, owned by the maintainer.

Built wheels are kept under ``<WHEELS_DIR>/<version>/`` so a previously built
version can be re-deployed without rebuilding. The backend keeps no store of
its own; this is the single home for driver wheels and their history.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from opentrons_control.maintainer.app.config import WHEELS_DIR

PACKAGE = "opentrons_drivers"

#: Version tokens permitted as a directory name (guards re-deploy requests
#: whose version arrives from the frontend).
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


class StoreError(RuntimeError):
    """Raised for invalid version strings."""


def _safe_version(version: str) -> str:
    if not _VERSION_RE.match(version):
        raise StoreError(f"invalid version string: {version!r}")
    return version


def store_wheel(wheel: Path, version: str) -> Path:
    """Copy a freshly built wheel into the store. Returns its stored path."""
    _safe_version(version)
    dest_dir = Path(WHEELS_DIR) / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / wheel.name
    shutil.copy2(wheel, dest)
    return dest


def wheel_for(version: str) -> Path | None:
    """Return the stored wheel for ``version``, or None if not present."""
    _safe_version(version)
    dest_dir = Path(WHEELS_DIR) / version
    wheels = sorted(dest_dir.glob(f"{PACKAGE}-*.whl"))
    return wheels[-1] if wheels else None


def list_versions() -> list[str]:
    """Return the versions currently in the store, sorted."""
    root = Path(WHEELS_DIR)
    if not root.is_dir():
        return []
    return sorted(
        d.name
        for d in root.iterdir()
        if d.is_dir() and any(d.glob(f"{PACKAGE}-*.whl"))
    )