"""
Driver-package install executor.

The backend is a pure executor for driver updates: it receives one pre-built
``opentrons_drivers`` wheel plus an instruction (a version label and a set of
target robots), installs the wheel over the existing SSH transport, and reports
the per-robot outcome. It keeps no wheel store of its own — versioned wheels and
version history live in the maintainer container.

Deploys run as background jobs. ``start_install_job`` stages the wheel, registers
an in-memory job, spawns the install as an asyncio task, and returns a job id
immediately; the install reports progress into the job store, which the status
endpoint polls. This keeps the request short (no minutes-long blocking POST) so
the proxy never times out and the browser never hangs.

The job store is **in-memory and transitional**: a backend restart drops it,
which is intended — better to lose a finished job and redeploy than to persist a
transient one. It is touched only from the event loop (request handlers and the
per-robot coroutines all run there; the blocking install is offloaded to worker
threads but results are written back on the loop), so it needs no lock.

Install mechanic
----------------
The robot is network-isolated and already has ``opentrons`` (the wheel's only
dependency) in its system image, so the wheel must install **without** reaching
a package index — ``--no-deps`` is mandatory; without it pip tries to resolve
``opentrons`` from PyPI and hangs forever. The install is a single, atomic pip
invocation:

    upload wheel  →  <python> -m pip install --force-reinstall --no-deps <wheel>  →
    remove the wheel

``--force-reinstall`` replaces the package in one step (no separate uninstall),
so a failed install can never strand the robot with the old package removed and
nothing in its place. A failed install returns non-zero, surfaces as an
SSHError, and is reported against that robot; the others are unaffected.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from opentrons_control.backend.app.bootstrap import SSHClient, robot_pip
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

    The robot is network-isolated, so the install must never reach a package
    index. ``opentrons`` (the wheel's only dependency) is already present in
    the system image, so the wheel is installed with ``--no-deps`` — without
    it, pip tries to resolve ``opentrons`` from PyPI and hangs forever on the
    air-gapped robot. ``--force-reinstall`` does it in a single pip invocation
    (no separate uninstall step), so a failure can never leave the robot with
    the old package removed and nothing installed.

    :param robot: Target robot connection details.
    :param wheel: Local path to the wheel to install.
    The interpreter is detected per robot rather than trusting a bare ``pip``
    command, which is absent on some robot images.

    :param version: Version label, used only for the returned status string.
    :returns: A short status string, e.g. ``"installed 0.2.0"``.
    :raises SSHError: if the upload or the pip install fails.
    """
    ssh = SSHClient(host=robot.host, user=robot.user, key_path=robot.key_path)
    remote = f"{gv.WHEEL_STAGING_DIR}/{wheel.name}"
    q_remote = shlex.quote(remote)
    q_dir = shlex.quote(gv.WHEEL_STAGING_DIR)

    ssh.run(f"mkdir -p {q_dir}", timeout=30)
    ssh.upload(wheel, remote)
    ssh.run(
        f"{robot_pip(ssh)} install --force-reinstall --no-deps {q_remote}",
        timeout=300,
    )
    ssh.run(f"rm -f {q_remote}", timeout=30)
    return f"installed {version}"


# -------------------- Job store (in-memory, event-loop-only) --------------------


@dataclass
class _Job:
    """A single deploy job. Created and mutated only on the event loop."""

    job_id: str
    version: str
    #: robot_id -> "running" | "installed X" | "failed: ..." | "skipped: busy"
    results: dict[str, str]
    state: str = "running"  # "running" | "done"
    started_at: float = field(default_factory=time.time)


_JOBS: dict[str, _Job] = {}
_MAX_JOBS = 50


def _prune() -> None:
    """Bound the store: drop the oldest jobs beyond ``_MAX_JOBS``."""
    if len(_JOBS) > _MAX_JOBS:
        stale = sorted(_JOBS, key=lambda j: _JOBS[j].started_at)[: len(_JOBS) - _MAX_JOBS]
        for jid in stale:
            _JOBS.pop(jid, None)


def get_job(job_id: str) -> _Job | None:
    """Return the job, or None if unknown (e.g. lost to a restart)."""
    return _JOBS.get(job_id)


def job_status(job: _Job) -> dict[str, object]:
    """Serialise a job for the status endpoint."""
    return {
        "job_id": job.job_id,
        "version": job.version,
        "state": job.state,
        "results": dict(job.results),
    }


# -------------------- Target resolution --------------------


def resolve_targets(registry: SessionRegistry, robot_ids: list[str]) -> list[str]:
    """Resolve the target set, validating every id up front.

    An empty list means "all registered robots". Any explicit id must be known,
    so a typo fails fast in the request rather than silently inside a job.

    :raises ct.UnknownRobot: if any id is not registered.
    """
    if not robot_ids:
        return registry.robot_ids()
    for robot_id in robot_ids:
        registry.get_robot(robot_id)
    return list(robot_ids)


# -------------------- Install orchestration --------------------


async def _install_one(
    registry: SessionRegistry,
    robot_id: str,
    wheel: Path,
    version: str,
    job: _Job,
) -> None:
    """Acquire, install, record — for a single robot. Updates ``job`` in place.

    Runs on the event loop; the blocking install is offloaded to a worker
    thread, and the result is written back into the job here on the loop. A
    robot whose lock is already held is recorded as skipped, not waited on.
    """
    try:
        session = await registry.acquire(
            robot_id, protocol_name=f"update-{version}", mode="update"
        )
    except ct.RobotBusy:
        job.results[robot_id] = "skipped: busy"
        return

    loop = asyncio.get_running_loop()
    try:
        status = await loop.run_in_executor(
            None, install_wheel_on_robot, registry.get_robot(robot_id), wheel, version
        )
        job.results[robot_id] = status
    except Exception as exc:  # reported per robot, never fatal to the job
        job.results[robot_id] = f"failed: {exc}"
    finally:
        registry.release(session.token)


async def _run_job(
    registry: SessionRegistry,
    job: _Job,
    wheel: Path,
    tmpdir: Path,
) -> None:
    """Background task: install on every target, then finalise and clean up."""
    try:
        targets = list(job.results)
        await asyncio.gather(
            *(_install_one(registry, rid, wheel, job.version, job) for rid in targets)
        )
    finally:
        job.state = "done"
        shutil.rmtree(tmpdir, ignore_errors=True)


def start_install_job(
    registry: SessionRegistry,
    data: bytes,
    filename: str,
    version: str,
    targets: list[str],
) -> str:
    """Stage the wheel, register a job, and spawn the install in the background.

    Returns immediately with the new job id; the install runs as an asyncio task
    and reports progress into the job store (polled via the status endpoint).
    The staged wheel lives in a private temp dir for the life of the job and is
    removed when it finishes. Must be called from within the event loop (it
    schedules a task).

    :raises UpdateError: if ``filename`` is not a valid wheel filename.
    """
    name = _safe_wheel_name(Path(filename).name)
    tmpdir = Path(tempfile.mkdtemp(prefix="wheel-"))
    wheel = tmpdir / name
    wheel.write_bytes(data)

    job = _Job(
        job_id=uuid4().hex,
        version=version,
        results={rid: "running" for rid in targets},
    )
    _JOBS[job.job_id] = job
    _prune()

    asyncio.create_task(_run_job(registry, job, wheel, tmpdir))
    return job.job_id