"""
End-to-end launch orchestration for a single session.

A launch turns a session-creation request into a running agent on a robot.
It is the only place that bridges the in-memory session registry, the SSH
bootstrap helpers, and the agent's HTTP client.

The launch flow:

1. Acquire the robot lock and register the session in ``starting`` status.
2. Materialise the postbox payload into a temporary directory on the
   backend filesystem.
3. SSH-prepare the launch directory on the OT, SCP all materialised files
   into its postbox, and start the agent process.
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
from typing import AsyncIterator, Mapping, Optional

from opentrons_drivers.common.custom_types import JSONType
from opentrons_control.backend.app.bootstrap import OTBootstrap, SSHError
from opentrons_control.backend.app.ot_client import OTClient, OTClientError
from opentrons_control.backend.app.sessions import (
    Mode,
    Session,
    SessionRegistry,
)


#: Wall-clock budget for the agent to report ready after launch. Hardware
#: boot typically takes 60-80 seconds; the headroom covers slow USB
#: enumeration and pipette discovery.
DEFAULT_READINESS_TIMEOUT = 180.0


class BootstrapFailed(RuntimeError):
    """
    Raised when a session cannot reach the ``active`` status.

    The session is left cleaned up (marked ``failed``, lock released)
    before the exception propagates.
    """


class PostboxFormatError(ValueError):
    """Raised when a postbox entry cannot be serialised as written."""


# -------------------- Postbox materialisation --------------------


@asynccontextmanager
async def _materialise_postbox(
    payload: Mapping[str, JSONType],
) -> AsyncIterator[list[Path]]:
    """
    Stage a postbox payload as files inside a temporary directory.

    Each entry's key is used verbatim as the filename. The serialisation
    format is inferred from the extension; only ``.json`` is supported.
    Values are serialised with :func:`json.dump`.

    The temporary directory is removed on context exit regardless of
    success.
    """
    with tempfile.TemporaryDirectory(prefix="postbox_") as tmp:
        tmp_path = Path(tmp)
        files: list[Path] = []

        for name, content in payload.items():
            suffix = Path(name).suffix.lower()
            if suffix != ".json":
                raise PostboxFormatError(
                    f"postbox entry {name!r}: only .json is supported"
                )

            target = tmp_path / name
            with target.open("w", encoding="utf-8") as f:
                json.dump(content, f, indent=2)
            files.append(target)

        yield files


# -------------------- Launch flow --------------------


async def launch_session(
    registry: SessionRegistry,
    *,
    robot_id: str,
    protocol_name: str,
    mode: Mode,
    postbox: Mapping[str, JSONType],
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
    postbox :
        Files to land in the agent's postbox directory before the agent
        starts. Keys are filenames; values are JSON-serialisable content.
        The launcher does not interpret the contents.
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
    PostboxFormatError
        A postbox entry cannot be serialised. Raised before any state
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
        async with _materialise_postbox(postbox) as files:
            await loop.run_in_executor(None, bootstrap.prepare_dir)
            await loop.run_in_executor(
                None, bootstrap.upload_postbox_files, files
            )
            await loop.run_in_executor(None, bootstrap.start_agent)

        async with OTClient(robot.agent_base_url) as client:
            await client.wait_until_ready(timeout=readiness_timeout)

    except (SSHError, OTClientError, OSError) as e:
        registry.mark_failed(session.token, message=str(e))
        registry.release(session.token)
        raise BootstrapFailed(str(e)) from e

    return registry.mark_active(session.token, robot.agent_base_url)