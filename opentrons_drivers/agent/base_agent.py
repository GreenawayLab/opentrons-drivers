"""
HTTP-driven Opentrons agent.

Threading model
---------------
This module's design hinges on three tiers of threads with strict role
separation. Every class and method here exists to support that separation,
so it's worth being explicit upfront:

1. **Protocol thread** — the thread that opentrons_execute hands to run().
   This is the ONLY thread allowed to touch the Opentrons protocol context,
   pipettes, or labware. The Opentrons API is not thread-safe and must be
   driven from a single thread. This thread runs the Agent.serve() loop,
   which polls the slot every poll_interval seconds and executes any queued
   job in-place.

2. **Server thread** — a daemon thread spawned in Agent.__init__. Its only
   job is to run ThreadingHTTPServer.serve_forever(), which sits in a
   blocking accept() loop on the listening socket. When a client connects,
   the server thread spawns a handler thread (see below) and immediately
   goes back to accept(). The server thread itself never reads request
   bodies or writes responses — it only dispatches.

3. **Handler threads** — short-lived, one spawned per incoming connection
   by the ThreadingHTTPServer. Each runs Handler.do_GET or do_POST,
   processes the request, sends the response, and exits. Handler threads
   read from and write to the agent's job slot under a lock. They never
   call into the protocol context.

The slot
--------
The "slot" is a single dict, `Agent._slot`, guarded by `Agent._lock`. It
holds at most one job at a time and tracks its lifecycle through a
`status` field:

    None         → no job has ever been submitted
    "queued"     → submitted via POST, not yet picked up by serve()
    "running"    → picked up; robot.invoke is executing
    "complete"   → finished successfully
    "failed"     → finished with an exception or falsy return

Threads communicate ONLY through the slot. There is no other shared state
between handler threads and the protocol thread. The protocol thread polls
the slot's status; handler threads write to it (POST) or read from it
(GET).

Submitting a new job while the slot is "queued" or "running" returns 409.
Submitting after "complete" or "failed" overwrites the slot.

Why a polling loop instead of a Condition variable
--------------------------------------------------
A Condition or Event would let the protocol thread sleep until a job
arrives, eliminating the 200ms latency floor. We use a sleep-poll instead
because (a) 200ms latency is invisible at human-orchestration timescales,
(b) the polling loop is trivially debuggable (no synchronization
primitives beyond the lock), and (c) it leaves room to add periodic
maintenance work to the loop later (e.g. watchdog checks, idle-timeout
cleanup) without restructuring.
"""

from __future__ import annotations
from opentrons import protocol_api
from typing import Dict, Optional, Any
from opentrons_drivers.common.custom_types import StaticCtx, BaseConfig
from opentrons_drivers.common.base_opentrons import Opentrons
from opentrons_drivers.agent.http_handler import Handler
from http.server import ThreadingHTTPServer
from threading import Lock, Thread
from pathlib import Path
import json
import time
import uuid
import traceback


class Agent:
    """
    HTTP-driven Opentrons agent. See module docstring for the threading
    model and slot lifecycle.

    Endpoints
    ---------
    GET  /health             → 200 once hardware init is complete, 503 before.
    POST /actions            → submit an action; 202 on accept, 409 if busy,
                               400 on bad input, 503 if not ready.
                               Body: {"action": str, "payload": dict}
                               Action name is dispatched directly through
                               ACTION_REGISTRY; unknown names raise during
                               execution and surface as a "failed" job.
    GET  /actions/<job_id>   → poll a specific job's status.
    GET  /actions/current    → status of the currently running (or last) job.
    """

    # Statuses that indicate the slot is occupied and rejecting new submissions.
    _BUSY = ("queued", "running")

    def __init__(
        self,
        protocol: protocol_api.ProtocolContext,
        base_config: BaseConfig,
        host: str = "0.0.0.0",
        port: int = 9000,
    ) -> None:
        """
        Initialize the agent and bring up the HTTP server.

        Runs on the protocol thread. Blocks for ~60-80s while hardware
        initializes; during that window, /health is unreachable (the server
        isn't running yet, so callers get connection-refused).

        Parameters
        ----------
        protocol :
            Active Opentrons protocol context. Owned by this thread.
        base_config :
            Hardware/layout configuration consumed by Opentrons.
        host, port :
            HTTP server bind address. Default ``0.0.0.0:9000``.
            ``0.0.0.0`` means "listen on every network interface" (LAN +
            loopback), not "localhost only" — that would be ``127.0.0.1``.
        """
        # Mark "process up but not yet ready" before anything that could
        # fail. If init crashes, agent_main.py's outer except overwrites
        # this with a "crashed" record.
        self._write_status("starting")

        # Boot hardware. Slow (~60-80s). Until it returns, the HTTP server
        # is not running yet, so callers probing /health get connection
        # refused — the correct signal for "not up yet".
        self.robot = Opentrons(protocol, base_config)
        self.static_ctx: StaticCtx = {
            "core_amounts": self.robot.core_amounts,
            "stock_amounts": self.robot.stock_amounts,
            "pipettes": self.robot.pipettes,
            "system_state": {},
        }

        # ---- Shared state between protocol thread and handler threads ----
        # The lock protects every read and write of `_slot`. Held briefly
        # in submit(), job_view(), current_job_view(), _claim_queued(), and
        # the finalization step of _execute(). NEVER held across hardware
        # calls or network I/O — that would block status polls for as long
        # as a job runs.
        self._lock = Lock()
        self._slot: Dict[str, Any] = {
            "job_id": None,
            "action": None,
            "payload": None,
            "status": None,        # None | "queued" | "running" | "complete" | "failed"
            "error": None,
            "result": None,
            "submitted_at": None,
            "finished_at": None,
        }
        self._ready = True

        # ---- Spin up the HTTP server on a daemon thread ----
        # Handler is instantiated fresh per request by ThreadingHTTPServer
        # and has no way to receive constructor arguments, so we bind the
        # agent reference via class attribute. Set this BEFORE starting
        # the server thread so handlers are guaranteed to see it.
        Handler.agent = self
        self._server = ThreadingHTTPServer((host, port), Handler)

        # Daemon thread so it doesn't keep the process alive if the
        # protocol thread ever exits. In normal operation the protocol
        # thread runs forever; this matters only for crash paths.
        self._server_thread = Thread(
            target=self._server.serve_forever,
            name="agent-http",
            daemon=True,
        )
        self._server_thread.start()

        self._write_status("ready")

    # ------------------------------------------------------------------
    # Public API consumed by Handler (called from handler threads)
    # ------------------------------------------------------------------

    def is_ready(self) -> bool:
        """Cheap readiness probe used by /health and POST gating."""
        return self._ready

    def submit(self, action: str, payload: dict) -> tuple[bool, dict]:
        """
        Try to claim the job slot.

        Called from a handler thread. Acquires the lock briefly, checks
        whether the slot is free, and either writes the new job into it or
        rejects. Does NOT trigger execution — the protocol thread will
        notice the new "queued" status on its next serve() tick (within
        poll_interval seconds) and pick the work up itself.

        Returns
        -------
        (accepted, info) :
            (True,  {"job_id": ..., "status": "queued"}) on accept,
            (False, {"error": ..., "current_job_id": ..., ...}) on reject.
        """
        with self._lock:
            if self._slot["status"] in self._BUSY:
                return False, {
                    "error": "agent is busy",
                    "current_job_id": self._slot["job_id"],
                    "current_action": self._slot["action"],
                }

            job_id = uuid.uuid4().hex
            self._slot.update({
                "job_id": job_id,
                "action": action,
                "payload": payload,
                "status": "queued",
                "error": None,
                "result": None,
                "submitted_at": time.time(),
                "finished_at": None,
            })
            return True, {"job_id": job_id, "status": "queued"}

    def job_view(self, job_id: str) -> Optional[dict]:
        """
        Return a snapshot of the slot if its job_id matches, else None.

        Called from a handler thread. The snapshot is a fresh dict copied
        under the lock, so the handler can serialize it without further
        synchronization.
        """
        with self._lock:
            if self._slot["job_id"] != job_id:
                return None
            return self._snapshot_locked()

    def current_job_view(self) -> dict:
        """Return a snapshot of whatever is currently in the slot."""
        with self._lock:
            if self._slot["job_id"] is None:
                return {
                    "job_id": None,
                    "action": None,
                    "status": "idle",
                    "error": None,
                    "result": None,
                }
            return self._snapshot_locked()

    # ------------------------------------------------------------------
    # Main loop (runs on the protocol thread)
    # ------------------------------------------------------------------

    def serve(self, poll_interval: float = 0.2) -> None:
        """
        Block forever, executing submitted jobs on the calling thread.

        MUST be called on the same thread that owns the Opentrons protocol
        context (i.e. the thread opentrons_execute calls run() on). This
        is the ONLY thread that ever drives hardware.

        The loop is deliberately simple: peek at the slot, run anything
        queued, otherwise nap. Latency between submission and start is
        bounded by poll_interval. We accept this for simplicity over a
        Condition-based wakeup; see module docstring.
        """
        while True:
            job = self._claim_queued()
            if job is None:
                time.sleep(poll_interval)
                continue
            self._execute(job)

    def _claim_queued(self) -> Optional[Dict[str, Any]]:
        """
        Atomically flip the slot's status from "queued" → "running" and
        return the work to do.

        Runs on the protocol thread. If no job is queued, returns None;
        the caller naps and tries again. Holding the lock across the read,
        the check, and the write ensures that even if a handler thread
        were to (hypothetically, in some future design) try to also flip
        the status, exactly one of the two would succeed.
        """
        with self._lock:
            if self._slot["status"] != "queued":
                return None
            self._slot["status"] = "running"
            return {
                "action": self._slot["action"],
                "payload": self._slot["payload"],
            }

    def _execute(self, job: Dict[str, Any]) -> None:
        """
        Run a single job to completion on the protocol thread.

        The lock is intentionally NOT held during robot.invoke. That call
        can take minutes; if we held the lock, every status poll would
        block for the duration of the action, which defeats the entire
        purpose of having a status endpoint. Handlers polling mid-action
        will see status="running" and the slot is otherwise consistent.

        On completion (or exception), we re-acquire the lock for the
        finalization writes so they appear atomically to any concurrent
        polling handler.
        """
        try:
            ok = self.robot.invoke(job["action"], self.static_ctx, job["payload"])
            with self._lock:
                self._slot["status"] = "complete" if ok else "failed"
                self._slot["error"] = None if ok else "action returned a falsy value"
                self._slot["finished_at"] = time.time()
        except Exception:
            tb = traceback.format_exc()
            with self._lock:
                self._slot["status"] = "failed"
                self._slot["error"] = tb
                self._slot["finished_at"] = time.time()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _snapshot_locked(self) -> dict:
        """
        Build a snapshot dict from the slot's current contents.

        Caller MUST hold self._lock. Returns a fresh dict so the caller
        can release the lock and continue working with the snapshot
        without further synchronization concerns.
        """
        s = self._slot
        return {
            "job_id": s["job_id"],
            "action": s["action"],
            "status": s["status"],
            "error": s["error"],
            "result": s["result"],
            "submitted_at": s["submitted_at"],
            "finished_at": s["finished_at"],
        }

    def _write_status(self, status: str, error: Optional[BaseException] = None) -> None:
        """
        Write a coarse-grained status to disk.

        Used only for boot-time diagnostics and crash detection (writes
        "starting", "ready", and indirectly "crashed" via agent_main.py).
        The hot path of running jobs is reflected via GET /actions/<id>,
        not here.
        """
        content = {
            "status": status,
            "error": None if error is None else traceback.format_exc(),
        }
        try:
            Path("postbox").mkdir(parents=True, exist_ok=True)
            with open("postbox/status.json", "w") as f:
                json.dump(content, f, indent=2)
        except OSError:
            # Don't take down the agent because we couldn't write a log.
            pass