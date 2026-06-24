"""
Session lifecycle and registry for backend-side ownership of running agents.

A :class:`Session` represents one active or pending run of an agent on one
robot. The :class:`SessionRegistry` owns the live set of sessions, enforces
per-robot exclusivity through asyncio locks, and is the authoritative
source the proxy queries to resolve a session token to a routing target.

Persistence is out of scope right now; the registry is purely in-memory at PoC stage. 
A backend restart loses all session state.

Driving a session through bootstrap is the launcher's concern, not this
module's. The registry exposes :meth:`acquire`, :meth:`mark_active`,
:meth:`mark_aborting`, :meth:`mark_failed`, and :meth:`release` as the
state transitions the launcher (and lifecycle owners) need.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
import opentrons_control.backend.app.settings.custom_types as ct
from pathlib import Path
from typing import Dict, Optional


# -------------------- Robot config --------------------


@dataclass(frozen=True)
class Robot:
    """
    Static connection details for one Opentrons unit.

    Attributes
    ----------
    id :
        Logical identifier used by clients and the registry.
    host :
        Network address of the OT on the internal subnet.
    user :
        SSH user, typically ``root``.
    key_path :
        Filesystem path to the SSH private key used to reach the OT.
    agent_port :
        TCP port the agent's HTTP server binds on the OT.
    """

    id: str
    host: str
    user: str
    key_path: Path
    agent_port: int = 9000

    @property
    def agent_base_url(self) -> str:
        """Base URL for HTTP traffic to this robot's agent."""
        return f"http://{self.host}:{self.agent_port}"


# -------------------- Session --------------------


@dataclass
class Session:
    """
    Authoritative state of a single agent run.

    A session binds a token to a robot for the duration of one launch.
    The token is the only identifier the proxy uses to route forwarded
    requests; the ``launch_id`` is the on-disk scope on the OT, fixed at
    session creation.
    """

    token: str
    robot_id: str
    launch_id: str
    protocol_name: str
    mode: ct.Mode
    status: ct.SessionStatus = "starting"
    agent_base_url: Optional[str] = None
    client_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    message: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in ct.TERMINAL_STATUSES


# -------------------- Routing view --------------------


@dataclass(frozen=True)
class RouteTarget:
    """
    Minimal projection of a session for the proxy's per-request lookups.

    The proxy needs only enough information to forward a request to the
    right agent. Everything else (mode, client_id, timestamps) stays
    inside the backend.
    """

    robot_id: str
    agent_base_url: str
    status: ct.SessionStatus


# -------------------- Registry --------------------


class SessionRegistry:
    """
    In-memory registry of active and pending sessions.

    Maintains one :class:`asyncio.Lock` per known robot. Lock acquisition
    is the gate that enforces single-occupancy: a robot's lock is held
    for the entire lifetime of any session targeting it, from
    :meth:`acquire` through :meth:`release`.

    The registry is async-safe but not thread-safe; all access must come
    from the same event loop.
    """

    def __init__(self, robots: Dict[str, Robot]):
        self._robots = robots
        self._sessions: Dict[str, Session] = {}
        self._robot_locks: Dict[str, asyncio.Lock] = {
            rid: asyncio.Lock() for rid in robots
        }
        # Maps robot_id to the token currently holding its lock. ``None``
        # when the robot is free.
        self._robot_to_token: Dict[str, Optional[str]] = {
            rid: None for rid in robots
        }

    # ------------------------------------------------------------------
    # Robot lookup
    # ------------------------------------------------------------------

    def get_robot(self, robot_id: str) -> Robot:
        """Return the :class:`Robot` config for ``robot_id``."""
        try:
            return self._robots[robot_id]
        except KeyError:
            raise ct.UnknownRobot(robot_id) from None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def acquire(
        self,
        robot_id: str,
        *,
        protocol_name: str,
        mode: ct.Mode,
        client_id: Optional[str] = None,
    ) -> Session:
        """
        Attempt to claim ``robot_id`` and register a new session.

        Returns the freshly created session in ``starting`` status. The
        caller is responsible for driving bootstrap and calling
        :meth:`mark_active` once the agent is reachable.

        Raises
        ------
        UnknownRobot
            ``robot_id`` is not in the registry.
        RobotBusy
            The robot's lock is held by another session.
        """
        if robot_id not in self._robot_locks:
            raise ct.UnknownRobot(robot_id)

        lock = self._robot_locks[robot_id]
        if lock.locked():
            raise ct.RobotBusy(robot_id)

        await lock.acquire()

        token = secrets.token_urlsafe(24)
        launch_id = time.strftime("%Y%m%d_%H%M%S")

        session = Session(
            token=token,
            robot_id=robot_id,
            launch_id=launch_id,
            protocol_name=protocol_name,
            mode=mode,
            status="starting",
            client_id=client_id,
        )
        self._sessions[token] = session
        self._robot_to_token[robot_id] = token
        return session

    def mark_active(self, token: str, agent_base_url: str) -> Session:
        """
        Transition a session from ``starting`` to ``active``.

        Called once the agent's ``/health`` reports ready and routing
        through the proxy is safe.
        """
        session = self._get(token)
        session.agent_base_url = agent_base_url
        session.status = "active"
        session.message = None
        return session

    def mark_aborting(self, token: str, *, message: Optional[str] = None) -> Session:
        """
        Mark a session as in the process of being torn down.

        The session remains registered until :meth:`release` is called.
        Routing lookups during this window return the session's status so
        the proxy can refuse further forwards.
        """
        session = self._get(token)
        session.status = "aborting"
        session.message = message
        return session

    def mark_failed(self, token: str, *, message: str) -> Session:
        """
        Mark a session as failed and surface a human-readable cause.

        Failure is a terminal status; the caller should follow this with
        :meth:`release` once cleanup is complete.
        """
        session = self._get(token)
        session.status = "failed"
        session.message = message
        return session

    def release(self, token: str) -> None:
        """
        Remove the session from the registry and free its robot lock.

        Safe to call from any terminal or near-terminal status. If the
        session is unknown the call is a no-op so cleanup paths can be
        written idempotently.
        """
        session = self._sessions.pop(token, None)
        if session is None:
            return

        if not session.is_terminal:
            session.status = "ended"

        robot_id = session.robot_id
        self._robot_to_token[robot_id] = None
        lock = self._robot_locks.get(robot_id)
        if lock is not None and lock.locked():
            lock.release()

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get(self, token: str) -> Session:
        """Return the session for ``token`` or raise :class:`UnknownSession`."""
        return self._get(token)

    def route(self, token: str) -> RouteTarget:
        """
        Return the proxy's routing view of a session.

        Raises :class:`UnknownSession` if the token is not registered or
        the session has no agent URL yet. The caller is expected to
        inspect the returned status and reject forwarding for
        non-``active`` sessions.
        """
        session = self._get(token)
        if session.agent_base_url is None:
            raise ct.UnknownSession(token)
        return RouteTarget(
            robot_id=session.robot_id,
            agent_base_url=session.agent_base_url,
            status=session.status,
        )

    def current_token_for(self, robot_id: str) -> Optional[str]:
        """Return the token currently bound to ``robot_id`` or ``None`` if free."""
        if robot_id not in self._robot_to_token:
            raise ct.UnknownRobot(robot_id)
        return self._robot_to_token[robot_id]

    def all_sessions(self) -> list[Session]:
        """Return a snapshot list of all live sessions."""
        return list(self._sessions.values())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get(self, token: str) -> Session:
        try:
            return self._sessions[token]
        except KeyError:
            raise ct.UnknownSession(token) from None