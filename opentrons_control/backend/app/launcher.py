"""
End-to-end launch orchestration for a single session.

A launch turns a session-creation request into a running agent on a robot.
It is the only place that bridges the in-memory session registry, the SSH
bootstrap helpers, and the agent's HTTP client.

The launch flow:

1. Acquire the robot lock and register the session in ``starting`` status.
2. Materialise each named bucket of files into a temporary directory on
   the backend filesystem.
3. SSH-prepare the launch directory on the OT, SCP each bucket into its
   matching subdirectory (``postbox/``, ``plates/``, etc.), and start the
   agent process.
4. Poll the agent's ``/health`` until it reports ready.
5. Transition the session to ``active`` and return it.

On any failure between steps 2 and 4 the session is marked ``failed``,
the robot lock is released, and :class:`BootstrapFailed` is raised. The
caller does not need to perform additional cleanup.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Mapping, Optional, Any

from opentrons_control.backend.app.bootstrap import OTBootstrap, SSHError
from opentrons_control.backend.app.ot_client import OTClient
from opentrons_control.backend.app.sessions import (
    Session,
    SessionRegistry,
)
import opentrons_control.backend.app.custom_types as ct
from opentrons_control.backend.app.global_variables import DEFAULT_READINESS_TIMEOUT

# -------------------- File materialisation --------------------


@asynccontextmanager
async def _materialise_buckets(
    buckets: Mapping[str, Mapping[str, ct.JSONType]],
) -> AsyncIterator[dict[str, list[Path]]]:
    """
    Stage one or more named buckets of files inside a temporary directory.

    ``buckets`` is a mapping of subdirectory name (e.g. ``"postbox"``,
    ``"plates"``) to a mapping of filename to JSON-serialisable content.
    Each bucket becomes its own subdirectory under the temp root.

    Yields a mapping of subdirectory name to the list of staged file
    paths within it. The temporary directory is removed on context exit
    regardless of success.

    Only ``.json`` filenames are accepted in this revision. Any other
    suffix raises :class:`FileFormatError` before any state transition.
    """
    with tempfile.TemporaryDirectory(prefix="launch_") as tmp:
        tmp_path = Path(tmp)
        staged: dict[str, list[Path]] = {}

        for subdir, files in buckets.items():
            bucket_dir = tmp_path / subdir
            bucket_dir.mkdir()
            bucket_paths: list[Path] = []

            for name, content in files.items():
                suffix = Path(name).suffix.lower()
                if suffix != ".json":
                    raise ct.FileFormatError(
                        f"{subdir}/{name}: only .json is supported"
                    )

                target = bucket_dir / name
                with target.open("w", encoding="utf-8") as f:
                    json.dump(content, f, indent=2)
                bucket_paths.append(target)

            staged[subdir] = bucket_paths

        yield staged


# -------------------- Launch flow --------------------


async def launch_session(
    registry: SessionRegistry,
    *,
    robot_id: str,
    protocol_name: str,
    mode: ct.Mode,
    files: Mapping[str, Mapping[str, ct.JSONType]],
    client_id: Optional[str] = None,
    readiness_timeout: float = DEFAULT_READINESS_TIMEOUT,
) -> Session:
    """
    Drive a new session from acquisition through to ``active`` status.

    Parameters
    ----------
    registry :
        The session registry that owns lock state.
    robot_id :
        Target robot. Must be known to ``registry``.
    protocol_name :
        Human-friendly protocol name; used in directory paths on the OT.
    mode :
        ``manual`` for backend-driven runs, ``auto`` for external clients.
    files :
        Mapping of subdirectory name to a mapping of filename to
        JSON-serialisable content. Each subdirectory is materialised on
        the OT under the launch directory. Typical keys are ``postbox``
        (containing ``base_config.json``) and ``plates`` (containing
        labware definitions referenced by base_config).
    client_id :
        External client identifier, if any. ``None`` for manual runs.
    readiness_timeout :
        Wall-clock budget for the agent to become healthy.

    Returns
    -------
    Session
        The newly active session.

    Raises
    ------
    UnknownRobot, RobotBusy
        Propagated from :meth:`SessionRegistry.acquire`.
    FileFormatError
        A file entry cannot be serialised. Raised before any state
        transition or network I/O.
    BootstrapFailed
        Any failure after the session was acquired. The session is left
        cleaned up before the exception is raised.
    """
    robot = registry.get_robot(robot_id)
    session = await registry.acquire(
        robot_id,
        protocol_name=protocol_name,
        mode=mode,
        client_id=client_id,
    )

    bootstrap = OTBootstrap(
        host=robot.host,
        user=robot.user,
        key_path=robot.key_path,
        protocol_name=protocol_name,
        launch_id=session.launch_id,
    )

    loop = asyncio.get_running_loop()

    try:
        async with _materialise_buckets(files) as staged:
            await loop.run_in_executor(None, bootstrap.prepare_dir)
            for subdir, paths in staged.items():
                await loop.run_in_executor(
                    None, bootstrap.upload_files_to, subdir, paths
                )
            await loop.run_in_executor(None, bootstrap.start_agent)

        async with OTClient(robot.agent_base_url) as client:
            await client.wait_until_ready(timeout=readiness_timeout)

    except (SSHError, ct.OTClientError, OSError) as e:
        registry.mark_failed(session.token, message=str(e))
        registry.release(session.token)
        raise ct.BootstrapFailed(str(e)) from e

    return registry.mark_active(session.token, robot.agent_base_url)