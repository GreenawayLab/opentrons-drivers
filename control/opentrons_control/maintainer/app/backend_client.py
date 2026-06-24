"""
HTTP client for the backend.

The maintainer is a pure HTTP client of the backend: it asks for git
credentials and it hands over a built wheel plus an instruction (version +
target robots) for execution. It never imports backend code and never touches
a robot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from opentrons_control.maintainer.app.config import BACKEND_URL
from opentrons_control.maintainer.app.config import BACKEND_TIMEOUT
from opentrons_control.maintainer.app.config import GIT_CREDENTIAL_PATH
from opentrons_control.maintainer.app.config import INSTALL_PATH


class BackendError(RuntimeError):
    """Raised when a backend call fails or returns a non-200 status."""


async def fetch_git_credential() -> bytes:
    """Fetch the git deploy key from the backend vault. Returns raw bytes."""
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
            r = await client.get(f"{BACKEND_URL}{GIT_CREDENTIAL_PATH}")
    except httpx.HTTPError as e:
        raise BackendError(f"backend unreachable: {e}") from e
    if r.status_code != 200:
        raise BackendError(
            f"credential fetch returned {r.status_code}: {r.text}"
        )
    return r.content


async def send_install(
    wheel: Path,
    version: str,
    robot_ids: list[str],
) -> dict[str, Any]:
    """Send the wheel + instruction to the backend executor and return its report.

    :param wheel: Local path to the wheel to install.
    :param version: Version label for the instruction.
    :param robot_ids: Target robots; empty list means "all available" on the
        backend side.
    :returns: The backend's parsed JSON report (``{"version", "results"}``).
    :raises BackendError: on transport failure or a non-200 status.
    """
    data = {"version": version, "robot_ids": ",".join(robot_ids)}
    files = {"wheel": (wheel.name, wheel.read_bytes(), "application/octet-stream")}
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as client:
            r = await client.post(f"{BACKEND_URL}{INSTALL_PATH}", data=data, files=files)
    except httpx.HTTPError as e:
        raise BackendError(f"backend unreachable: {e}") from e
    if r.status_code != 200:
        raise BackendError(f"install returned {r.status_code}: {r.text}")
    return r.json()