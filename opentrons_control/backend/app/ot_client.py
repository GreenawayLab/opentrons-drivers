"""
Async HTTP client for an Opentrons agent.

A single :class:`OTClient` instance corresponds to one running agent on
one OT. It hides the agent's REST surface behind typed coroutines:
readiness check, action submission, status polling, and abort.

The client does not own any session or routing concept; callers supply
the agent's base URL. It is safe to construct one per session and discard
it when the session ends.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from dataclasses import dataclass
import opentrons_control.backend.app.custom_types as ct
from opentrons_control.backend.app.global_variables import HEALTHY_STATUSES
import httpx


@dataclass(frozen=True)
class JobSnapshot:
    """
    Point-in-time view of the agent's slot for a given job_id.

    Mirrors the dict shape returned by the agent and pins down the field
    set so callers don't have to remember the wire format.
    """
    job_id: Optional[str]
    action: Optional[str]
    status: Optional[str]
    error: Optional[str]
    result: ct.JSONType
    submitted_at: Optional[float]
    finished_at: Optional[float]

    @classmethod
    def from_dict(cls, d: ct.JobSnapshotDict) -> "JobSnapshot":
        return cls(
            job_id=d.get("job_id"),
            action=d.get("action"),
            status=d.get("status"),
            error=d.get("error"),
            result=d.get("result"),
            submitted_at=d.get("submitted_at"),
            finished_at=d.get("finished_at"),
        )

    @property
    def is_terminal(self) -> bool:
        """True when no further state transitions are expected for this job."""
        return self.status in ("complete", "failed")
    

class OTClient:
    """
    Async client for a single Opentrons agent.

    Wraps an ``httpx.AsyncClient`` bound to the agent's base URL. Methods
    are thin: each one corresponds to a single HTTP call and translates
    transport-level results into typed responses or exceptions.

    Polling helpers (:meth:`wait_until_ready`, :meth:`wait_for_job`) are
    provided as convenience layers over the single-call methods.

    Use as an async context manager to ensure the underlying connection
    pool is closed::

        async with OTClient("http://10.0.0.3:9000") as client:
            await client.wait_until_ready()
            snapshot = await client.submit_action("transfer_liquid", {...})
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "OTClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def is_ready(self) -> bool:
        """
        Return True if the agent is fully initialised and accepting jobs.

        A connection error counts as not-ready (the agent process may not
        be listening yet). Any HTTP error other than 200/503 is propagated
        as :class:`OTClientError`.
        """
        try:
            r = await self._http.get("/health")
        except httpx.HTTPError:
            return False

        if r.status_code == 503:
            return False
        if r.status_code != 200:
            raise ct.OTClientError(f"unexpected /health status {r.status_code}: {r.text}")

        body = r.json()
        return body.get("status") in HEALTHY_STATUSES

    async def wait_until_ready(
        self,
        *,
        timeout: float = 180.0,
        interval: float = 2.0,
    ) -> None:
        """
        Poll :meth:`is_ready` until the agent reports ready or the timeout
        elapses.

        Raises :class:`AgentUnreachable` on timeout. ``interval`` is the
        delay between polls; ``timeout`` is the overall wall-clock budget.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if await self.is_ready():
                return
            if asyncio.get_event_loop().time() >= deadline:
                raise ct.AgentUnreachable(
                    f"agent at {self._base_url} did not become ready within {timeout}s"
                )
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Action submission and polling
    # ------------------------------------------------------------------

    async def submit_action(
        self,
        action: str,
        payload: dict[str, ct.JSONType],
    ) -> JobSnapshot:
        """
        Submit a single action and return the agent's initial snapshot.

        On success the slot transitions to ``queued`` and the returned
        snapshot carries the new ``job_id``. Use :meth:`get_job` or
        :meth:`wait_for_job` to follow the job to completion.

        Raises
        ------
        AgentBusy
            The slot is currently occupied by another job.
        AgentNotReady
            Hardware initialisation has not completed.
        AgentBadRequest
            The action/payload was rejected as malformed.
        AgentUnreachable
            The agent could not be contacted.
        """
        try:
            r = await self._http.post(
                "/actions",
                json={"action": action, "payload": payload},
            )
        except httpx.HTTPError as e:
            raise ct.AgentUnreachable(str(e)) from e

        if r.status_code == 202:
            return JobSnapshot.from_dict(r.json())
        if r.status_code == 409:
            raise ct.AgentBusy(r.json())
        if r.status_code == 503:
            raise ct.AgentNotReady(r.text)
        if r.status_code == 400:
            raise ct.AgentBadRequest(r.json())
        raise ct.OTClientError(f"unexpected /actions status {r.status_code}: {r.text}")

    async def get_job(self, job_id: str) -> JobSnapshot:
        """
        Return a snapshot of the slot for ``job_id``.

        Raises :class:`JobNotFound` if the agent does not recognise the id
        (typically because the slot has since been overwritten by another
        submission).
        """
        try:
            r = await self._http.get(f"/actions/{job_id}")
        except httpx.HTTPError as e:
            raise ct.AgentUnreachable(str(e)) from e

        if r.status_code == 200:
            return JobSnapshot.from_dict(r.json())
        if r.status_code == 404:
            raise ct.JobNotFound(job_id)
        raise ct.OTClientError(f"unexpected /actions/{{id}} status {r.status_code}: {r.text}")

    async def get_current(self) -> JobSnapshot:
        """Return a snapshot of whatever job currently occupies the slot."""
        try:
            r = await self._http.get("/actions/current")
        except httpx.HTTPError as e:
            raise ct.AgentUnreachable(str(e)) from e

        if r.status_code == 200:
            return JobSnapshot.from_dict(r.json())
        raise ct.OTClientError(f"unexpected /actions/current status {r.status_code}: {r.text}")

    async def wait_for_job(
        self,
        job_id: str,
        *,
        interval: float = 1.5,
    ) -> JobSnapshot:
        """
        Poll :meth:`get_job` until the snapshot reaches a terminal status.

        Returns the terminal snapshot. Does not impose a timeout: actions
        can legitimately take hours. Callers that need a deadline should
        wrap the call with :func:`asyncio.wait_for`.
        """
        while True:
            snapshot = await self.get_job(job_id)
            if snapshot.is_terminal:
                return snapshot
            await asyncio.sleep(interval)

    # ------------------------------------------------------------------
    # Abort
    # ------------------------------------------------------------------

    async def abort(self) -> None:
        """
        Tell the agent to terminate itself.

        The agent acknowledges with 202 and then exits its process. The
        next call against this client will fail with :class:`AgentUnreachable`,
        which is the expected outcome.

        A transport-level failure during the abort call is treated as
        success: if the agent is already gone, the desired state is
        already achieved.
        """
        try:
            r = await self._http.post("/abort")
        except httpx.HTTPError:
            return

        if r.status_code in (200, 202):
            return
        raise ct.OTClientError(f"unexpected /abort status {r.status_code}: {r.text}")