"""
Driver-package install executor.

The backend is a pure executor for driver updates: it receives one
pre-built ``opentrons_drivers`` wheel plus an instruction (a version label
and a set of target robots), installs the wheel over the existing SSH
transport, and reports the per-robot outcome. It keeps no wheel store of
its own — versioned wheels and version history live in the maintainer
container. The received wheel is written to a temporary directory for the
duration of the install and discarded afterwards.

Install mechanic
----------------
The robot already has ``opentrons`` (the wheel's only dependency) in its
system image, so the local wheel installs with plain pip and no package
index. The sequence mirrors the manual workflow:

    upload wheel  →  pip uninstall opentrons_drivers (tolerant)  →
    pip install <wheel>  →  remove the wheel

A failed ``pip install`` returns non-zero, which surfaces as an SSHError and
is reported against that robot; the others are unaffected.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import tempfile
from pathlib import Path

from opentrons_control.backend.app.bootstrap import SSHClient
from opentrons_control.backend.app.robot_sessions import Robot, SessionRegistry
import opentrons_control.backend.app.settings.custom_types as ct
import opentrons_control.backend.app.settings.global_variables as gv


#: A valid wheel filename: wheel names are restricted to this charset by the
#: spec, and the name is interpolated into remote pip/rm commands, so this
#: guards against shell injection and against being handed a non-wheel.
_WHEEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.whl$")


class UpdateError(RuntimeError):
    """Raised for caller-facing update failures (e.g. a bad wheel filename)."""


def _safe_wheel_name(name: str) -> str:
    """Return ``name`` if it is a valid, safe wheel filename, else raise.

    :param name: Basename of the uploaded wheel.
    :raises UpdateError: if it is not a spec-shaped ``*.whl`` filename.
    """
    if not _WHEEL_RE.match(name):
        raise UpdateError(f"not a valid wheel filename: {name!r}")
    return name


# -------------------- On-robot install --------------------


def install_wheel_on_robot(robot: Robot, wheel: Path, version: str) -> str:
    """Install ``wheel`` on a single robot via pip. Returns a status string.

    ``opentrons`` (the wheel's only dependency) is already present on the
    robot, so the local wheel installs without any package index. Uninstall
    is tolerant of a first-ever install (nothing to remove).

    :param robot: Target robot connection details.
    :param wheel: Local path to the wheel to install.
    :param version: Version label, used only for the returned status string.
    :returns: A short status string, e.g. ``"installed 0.2.0"``.
    :raises SSHError: if the upload or the pip install fails.
    """
    ssh = SSHClient(host=robot.host, user=robot.user, key_path=robot.key_path)
    pkg = gv.DRIVERS_PACKAGE
    remote = f"{gv.WHEEL_STAGING_DIR}/{wheel.name}"
    q_remote = shlex.quote(remote)
    q_dir = shlex.quote(gv.WHEEL_STAGING_DIR)

    ssh.run(f"mkdir -p {q_dir}", timeout=30)
    ssh.upload(wheel, remote)
    ssh.run(
        f"{gv.ROBOT_PIP} uninstall -y {pkg} || true && "
        f"{gv.ROBOT_PIP} install {q_remote}",
        timeout=300,
    )
    ssh.run(f"rm -f {q_remote}", timeout=30)
    return f"installed {version}"


# -------------------- Orchestration --------------------


async def install_to(
    registry: SessionRegistry,
    wheel: Path,
    version: str,
    *,
    robot_ids: list[str],
) -> dict[str, str]:
    """Install ``wheel`` on each of ``robot_ids`` concurrently.

    Each robot is locked for the duration of its install via a
    ``mode="update"`` session, so no protocol run can start mid-install. A
    robot whose lock is already held (a session is running) is skipped and
    reported, not waited on.

    :param registry: Session registry owning the robot locks.
    :param wheel: Local path to the wheel to install.
    :param version: Version label the wheel reports.
    :param robot_ids: Robots to target. Each must be known to the registry.
    :returns: Mapping of robot_id to a status string or error text.
    :raises ct.UnknownRobot: if any id in ``robot_ids`` is not registered.
        Raised before any install starts, so a bad id never leaves the fleet
        half-updated.
    """
    # Validate the whole target set up front.
    for robot_id in robot_ids:
        registry.get_robot(robot_id)

    report: dict[str, str] = {}
    acquired: dict[str, str] = {}  # robot_id -> token, in target order

    for robot_id in robot_ids:
        try:
            session = await registry.acquire(
                robot_id, protocol_name=f"update-{version}", mode="update"
            )
            acquired[robot_id] = session.token
        except ct.RobotBusy:
            report[robot_id] = "skipped: busy"

    loop = asyncio.get_running_loop()
    try:
        results = await asyncio.gather(
            *(
                loop.run_in_executor(
                    None,
                    install_wheel_on_robot,
                    registry.get_robot(rid),
                    wheel,
                    version,
                )
                for rid in acquired
            ),
            return_exceptions=True,
        )
        for rid, res in zip(acquired, results):
            report[rid] = res if isinstance(res, str) else f"failed: {res}"
    finally:
        for token in acquired.values():
            registry.release(token)

    return report


async def execute_install(
    registry: SessionRegistry,
    data: bytes,
    filename: str,
    version: str,
    *,
    robot_ids: list[str],
) -> dict[str, str]:
    """Stage the received wheel in a tmpdir, install it, then discard it.

    This is the backend's executor entry point: the wheel bytes arrive in the
    request, are written to a temporary directory that exists only for the
    duration of the install, and the install proceeds from there. Nothing is
    persisted on the backend.

    :param registry: Session registry owning the robot locks.
    :param data: Raw wheel bytes from the instruction.
    :param filename: Wheel filename; only its basename is used.
    :param version: Version label for the returned report.
    :param robot_ids: Robots to target.
    :returns: Mapping of robot_id to a status string or error text.
    :raises UpdateError: if ``filename`` is not a valid wheel filename.
    :raises ct.UnknownRobot: if any target id is not registered.
    """
    name = _safe_wheel_name(Path(filename).name)
    with tempfile.TemporaryDirectory(prefix="wheel-") as tmp:
        wheel = Path(tmp) / name
        wheel.write_bytes(data)
        # The tmpdir is held open across the await, so the file survives until
        # every concurrent install has read it.
        return await install_to(registry, wheel, version, robot_ids=robot_ids)