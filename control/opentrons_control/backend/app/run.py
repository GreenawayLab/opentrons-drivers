"""Executor: the live driver for one manual run.

Separation of concerns:

* A Run is frozen data plus live state: the ordered command stream and a cursor,
  status, and error slot. It is the record of what a run is and how far it got.
* An Executor is the live controller. It pushes one Run through one already-open
  session, one command at a time, and exposes a small control surface (abort,
  pause, resume) that acts only at command boundaries, because you cannot signal
  a moving pipette.

The executor is deliberately dumb about tips, calibration, and methods. It only
consumes a stream of {action, payload} commands. The stream is prepared and
frozen elsewhere: the freeze step prepends the tip-rack reset, so "reset first"
is data in the stream rather than executor logic. That keeps the executor a pure
consumer, fully testable against a fake agent client with no robot and no HTTP.

There is no durability. A backend restart loses in-flight executors. That is the
deliberate scope for an attended, single-PC, small-team deployment. If this ever
grows to unattended multi-robot fleets, runs move to Postgres with a persisted
cursor and a startup reconciliation pass, none of which is load bearing now.

The launcher and agent-client imports are deferred into the functions that use
them, so importing this module pulls no SSH or HTTP dependency and the file
assembly and control logic stay unit-testable in isolation.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from opentrons_control.backend.app.ot_client import OTClient
from opentrons_control.backend.app.protocol_model import Step

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


def freeze_stream(commands: list["Step"]) -> list["Step"]:
    """Prepend the tip-rack reset so a run always begins from a full, known rack.

    Calibration may pick up and return corner tips, which the API marks as used.
    Resetting the tip racks as the first command wipes that tracking, so the
    protocol starts from tip position one regardless of what calibration did.
    The reset is a stream command, not executor logic, keeping the executor
    entirely tip-agnostic.
    """
    return [Step(action="reset_tipracks", payload={})] + list(commands)


class Control(Enum):
    """The finite control vocabulary an executor understands at a boundary.

    New verbs (skip a command, insert a cleanup) are additive: a new member and
    a new branch in the drain, never a structural change to the loop.
    """

    ABORT = "abort"
    PAUSE = "pause"
    RESUME = "resume"


@dataclass
class Run:
    """Frozen command stream plus the live state one executor mutates."""

    run_id: str
    robot_id: str
    stream: list["Step"]
    cursor: int = 0
    status: str = "ready"  # ready | running | paused | complete | failed | aborted | cancelled
    error: str | None = None
    token: str | None = None

    @property
    def total(self) -> int:
        """Number of commands in the frozen stream, reset included."""
        return len(self.stream)


class Executor:
    """Live controller that drives one Run through one open session.

    Constructed with an already-open session (its agent URL) and a Run to drive.
    The control channel and its boundary-drain are intrinsic: the executor is
    born controllable, so the endpoints that abort or pause it are only a thin
    delivery layer on top of this queue.
    """

    def __init__(
        self,
        run: Run,
        agent_base_url: str,
        on_teardown: Callable[[], None] | None = None,
        # client_factory is a seam for the agent client: None builds the real
        # OTClient, tests and a future dry-run inject a fake with no robot
        client_factory: Callable[[str], Any] | None = None,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        self.run = run
        self._agent_base_url = agent_base_url
        self._on_teardown = on_teardown or (lambda: None)
        self._client_factory = client_factory
        self._poll = poll_seconds
        # two separate channels: run.stream is the frozen list of actions the
        # cursor walks, this queue carries only external control signals
        self._control: asyncio.Queue[Control] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    # ---- control surface: enqueue and return, never drive directly ----
    def abort(self) -> None:
        """Request a stop. Takes effect after the current command finishes."""
        self._control.put_nowait(Control.ABORT)

    def pause(self) -> None:
        """Request a pause. The run idles at the next boundary until resume."""
        self._control.put_nowait(Control.PAUSE)

    def resume(self) -> None:
        """Release a pause and continue from the current cursor."""
        self._control.put_nowait(Control.RESUME)

    def status(self) -> dict[str, Any]:
        """A JSON-safe projection of the run's live state for the poll endpoint."""
        return {
            "run_id": self.run.run_id,
            "robot_id": self.run.robot_id,
            "cursor": self.run.cursor,
            "total": self.run.total,
            "status": self.run.status,
            "error": self.run.error,
        }

    def start(self) -> None:
        """Spawn the drive loop as a background task and return immediately."""
        self._task = asyncio.create_task(self._drive())

    def cancel(self) -> None:
        """Abandon a ready run that was never started, releasing the session.

        Valid only before start: there is no driver to stop, only an open
        session to free. A started run stops via abort instead.
        """
        if self._task is None:
            self.run.status = "cancelled"
            self._on_teardown()

    # ---- the loop ----
    async def _drain_control(self) -> bool:
        """Handle pending control at a boundary. Returns True if the run must stop.

        Abort stops the run. Pause blocks here on the queue until resume or abort
        arrives, so a paused run is genuinely idle rather than spinning.
        """
        while not self._control.empty():
            msg = self._control.get_nowait()
            if msg is Control.ABORT:
                return True
            if msg is Control.PAUSE:
                self.run.status = "paused"
                while True:
                    later = await self._control.get()
                    if later is Control.ABORT:
                        return True
                    if later is Control.RESUME:
                        self.run.status = "running"
                        break
        return False

    async def one_shot(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Post a single action to the agent and wait for it to finish.

        For calibration against the open session before the run starts. Not for
        driving the protocol: once started, the drive loop owns the agent's slot.
        """
        async with self._client() as client:
            await client.wait_until_ready()
            snap = await client.submit_action(action, payload)
            while not snap.is_terminal:
                await asyncio.sleep(self._poll)
                snap = await client.get_job(snap.job_id)
        return {"action": action, "status": snap.status}

    def _client(self) -> Any:
        """The agent client for this run, real by default, injectable for tests."""
        if self._client_factory is not None:
            return self._client_factory(self._agent_base_url)
        return OTClient(self._agent_base_url)

    async def _drive(self) -> None:
        """Walk the frozen stream, one submit-poll-advance per command.

        A failed command stops the run at that cursor. Any transport failure
        fails the run rather than leaving it stuck. Teardown (releasing the
        session) always runs, whatever the terminal state.
        """
        try:
            async with self._client() as client:
                await client.wait_until_ready()
                self.run.status = "running"
                # while, not for: the cursor is external mutable state on the Run
                # (status reports it, resume would restart from it, control can
                # re-decide against it), so it cannot be a loop-internal counter
                while self.run.cursor < self.run.total:
                    if await self._drain_control():
                        self.run.status = "aborted"
                        return
                    cmd = self.run.stream[self.run.cursor]
                    snap = await client.submit_action(cmd.action, cmd.payload)
                    while not snap.is_terminal:
                        await asyncio.sleep(self._poll)
                        snap = await client.get_job(snap.job_id)
                    if snap.status == "failed":
                        self.run.status = "failed"
                        self.run.error = f"command {self.run.cursor + 1} ({cmd.action}) failed on the robot"
                        return
                    self.run.cursor += 1
                self.run.status = "complete"
        except Exception as exc:  # agent unreachable, bootstrap gap, transport error
            self.run.status = "failed"
            self.run.error = str(exc)
        finally:
            self._on_teardown()


# ---- executor registry: run_id -> Executor, sibling to the session registry ----
# Terminated executors are kept so status stays pollable after a run ends. For an
# attended small-team deployment there is no reason to prune them before restart.
_EXECUTORS: dict[str, Executor] = {}


def register(executor: Executor) -> None:
    """Make an executor discoverable by run_id for status and control."""
    _EXECUTORS[executor.run.run_id] = executor


def get(run_id: str) -> Executor | None:
    """Return the executor for a run, or None if unknown or lost to restart."""
    return _EXECUTORS.get(run_id)


def new_run_id() -> str:
    """A fresh opaque run identifier."""
    return uuid.uuid4().hex