"""Backend-driven execution of a manual plan on one robot.

Two responsibilities:

* assemble the file buckets the agent boots from (postbox/base_config.json and
  the labware definitions the config references, under plates/), and
* drive the generated atomic stream over the agent's HTTP port, one action at a
  time: submit a transfer_execution, poll the slot to a terminal state, submit
  the next.

Runs are tracked in an in-memory store, fire-and-poll like the deploy pipeline.
There is no durability: a backend restart loses in-flight run state. That is the
deliberate scope for manual single-protocol testing. When unattended multi-robot
runs arrive, this store moves to Postgres with a persisted per-run cursor and a
startup reconciliation pass, none of which is load bearing yet.

The launcher and agent-client imports are deferred into the functions that use
them, so importing this module pulls no SSH or HTTP dependency and the file
assembly stays unit-testable in isolation.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from opentrons_control.backend.app.protocol_model import ManualProtocol

_POLL_SECONDS = 0.5


def assemble_launch_files(
    config_dict: dict[str, Any],
    labware_defs: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Build the file buckets launch_session ships to the robot.

    :param config_dict: The pinned config as a plain dict; becomes
        postbox/base_config.json, which the agent loads at boot.
    :param labware_defs: name to labware-definition dict for every labware the
        config references; each becomes a file under plates/.
    :returns: A buckets mapping shaped for launch_session (subdir to files).
    """
    return {
        "postbox": {"base_config.json": config_dict},
        "plates": dict(labware_defs),
    }


@dataclass
class Run:
    """In-memory state of one backend-driven run."""

    run_id: str
    robot_id: str
    total: int
    done: int = 0
    status: str = "starting"  # starting | running | complete | failed
    error: str | None = None
    token: str | None = None


_RUNS: dict[str, Run] = {}


def get_run(run_id: str) -> Run | None:
    """Return the run by id, or None if unknown (never existed or lost to restart)."""
    return _RUNS.get(run_id)


def run_status(run: Run) -> dict[str, Any]:
    """A JSON-safe status projection for the poll endpoint."""
    return {
        "run_id": run.run_id,
        "robot_id": run.robot_id,
        "total": run.total,
        "done": run.done,
        "status": run.status,
        "error": run.error,
    }


async def _drive(registry: Any, run: Run, protocol: "ManualProtocol") -> None:
    """Walk the atomic stream, one submit-and-poll per transfer.

    A failed action stops the run at that step. Any transport failure (agent
    unreachable, bootstrap gap) fails the run with the exception message rather
    than leaving it stuck in a non-terminal state.
    """
    from opentrons_control.backend.app.ot_client import OTClient

    robot = registry.get_robot(run.robot_id)
    try:
        async with OTClient(robot.agent_base_url) as client:
            await client.wait_until_ready()
            run.status = "running"
            for i, step in enumerate(protocol.steps):
                snap = await client.submit_action(step.action, step.payload)
                while not snap.is_terminal:
                    await asyncio.sleep(_POLL_SECONDS)
                    snap = await client.get_job(snap.job_id)
                if snap.status == "failed":
                    run.status = "failed"
                    run.error = f"step {i + 1} ({step.action}) failed on the robot"
                    return
                run.done = i + 1
            run.status = "complete"
    except Exception as exc:  # agent unreachable, bootstrap gap, transport error
        run.status = "failed"
        run.error = str(exc)


async def start_run(
    registry: Any,
    robot_id: str,
    protocol_name: str,
    protocol: "ManualProtocol",
    files: dict[str, dict[str, Any]],
    mode: str = "manual",
    client_id: str | None = None,
) -> Run:
    """Bootstrap the agent for this protocol, then drive its stream in the background.

    Returns immediately with a Run in starting/running state; poll run_status.
    The caller owns permission and validity gating: only a plan the checker
    passed should reach here.
    """
    from opentrons_control.backend.app.launcher import launch_session

    session = await launch_session(
        registry,
        robot_id=robot_id,
        protocol_name=protocol_name,
        mode=mode,
        files=files,
        client_id=client_id,
    )
    run = Run(
        run_id=uuid.uuid4().hex,
        robot_id=robot_id,
        total=len(protocol.steps),
        token=session.token,
    )
    _RUNS[run.run_id] = run
    asyncio.create_task(_drive(registry, run, protocol))
    return run