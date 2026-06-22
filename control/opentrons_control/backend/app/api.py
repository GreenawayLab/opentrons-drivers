"""
Backend HTTP API.

Exposes control-plane endpoints consumed by the proxy and by lab-internal
clients. Two endpoint groups:

``/internal/...``
    Session lifecycle endpoints called by the proxy on behalf of external
    clients. Cover session creation, routing lookup, abort, and a debug
    view. These are not authenticated at this layer; access control lives
    at the proxy edge.

``/manual/...``
    Endpoints triggered by internal lab tooling for backend-driven runs.
    Currently a stub; the slicing pipeline that turns an uploaded
    instruction document into a queue of actions lives outside this
    module.

The module exposes no top-level FastAPI instance. Callers construct the
app via :func:`create_app`, passing in a fully-resolved robot registry.
This keeps configuration loading (and the secret handling that comes with
it) out of the library proper.

This module is excluded from strict no Any typing by mypy because it is 
somehow conflicting with the pydantic BaseModel.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator, Dict, Mapping, Optional, Any
from pydantic import BaseModel, Field

from fastapi import FastAPI, HTTPException

from opentrons_control.backend.app.launcher import launch_session
from opentrons_control.backend.app.ot_client import OTClient
from opentrons_control.backend.app.sessions import (
    Robot,
    Session,
    SessionRegistry
)
import opentrons_control.backend.app.custom_types as ct


logger = logging.getLogger(__name__)



class CreateSessionRequest(BaseModel):
    """Payload accepted by ``POST /internal/sessions``."""

    robot_id: str
    protocol_name: str
    mode: ct.Mode = "auto"
    files: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    client_id: Optional[str] = None


class CreateSessionResponse(BaseModel):
    token: str
    robot_id: str
    launch_id: str
    status: str


class RouteResponse(BaseModel):
    """Routing view returned to the proxy."""

    robot_id: str
    agent_base_url: str
    status: str


class SessionDetailsResponse(BaseModel):
    token: str
    robot_id: str
    launch_id: str
    protocol_name: str
    mode: str
    status: str
    agent_base_url: Optional[str]
    client_id: Optional[str]
    created_at: float
    message: Optional[str]


class RobotInfoResponse(BaseModel):
    id: str
    host: str
    agent_port: int



# -------------------- Helpers --------------------


def _session_to_details(session: Session) -> SessionDetailsResponse:
    return SessionDetailsResponse(**asdict(session))


async def _abort_session(
    registry: SessionRegistry,
    token: str,
) -> None:
    """
    Drive a session through ``aborting`` to release.

    Marks the session as aborting, signals the agent to terminate, and
    releases the lock. Transport-level failures on the agent abort call
    are treated as success because the desired end state is "agent gone".
    """
    session = registry.mark_aborting(token, message="abort requested")
    if session.agent_base_url is not None:
        async with OTClient(session.agent_base_url) as client:
            await client.abort()
    registry.release(token)


# -------------------- App factory --------------------


def create_app(robots: Mapping[str, Robot]) -> FastAPI:
    """
    Build a FastAPI app bound to a concrete robot registry.

    Parameters
    ----------
    robots :
        Mapping of robot_id to :class:`Robot`. The caller is responsible
        for loading these from whatever source applies (config file,
        secrets manager, env, etc.) and resolving any indirection such as
        key-name to key-path.
    """
    registry = SessionRegistry(dict(robots))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.registry = registry
        logger.info("backend api started with %d robot(s)", len(robots))
        yield
        for session in list(registry.all_sessions()):
            try:
                await _abort_session(registry, session.token)
            except Exception:
                logger.exception(
                    "failed to abort session %s during shutdown", session.token
                )

    app = FastAPI(title="opentrons-control-backend", lifespan=lifespan)

    # ------------------------------------------------------------------
    # Health and metadata
    # ------------------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/robots", response_model=list[RobotInfoResponse])
    async def list_robots() -> list[RobotInfoResponse]:
        return [
            RobotInfoResponse(id=r.id, host=r.host, agent_port=r.agent_port)
            for r in robots.values()
        ]

    # ------------------------------------------------------------------
    # Internal: session lifecycle (proxy-facing)
    # ------------------------------------------------------------------

    @app.post(
        "/internal/sessions",
        response_model=CreateSessionResponse,
        status_code=201,
    )
    async def create_session(
        req: CreateSessionRequest,
    ) -> CreateSessionResponse:
        try:
            session = await launch_session(
                registry,
                robot_id=req.robot_id,
                protocol_name=req.protocol_name,
                mode=req.mode,
                files=req.files,  
                client_id=req.client_id,
            )
        except ct.UnknownRobot:
            raise HTTPException(status_code=404, detail=f"unknown robot {req.robot_id!r}")
        except ct.RobotBusy as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ct.FileFormatError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ct.BootstrapFailed as e:
            raise HTTPException(status_code=502, detail=str(e))

        return CreateSessionResponse(
            token=session.token,
            robot_id=session.robot_id,
            launch_id=session.launch_id,
            status=session.status,
        )

    @app.get(
        "/internal/sessions/{token}",
        response_model=RouteResponse,
    )
    async def get_route(token: str) -> RouteResponse:
        try:
            target = registry.route(token)
        except ct.UnknownSession:
            raise HTTPException(status_code=404, detail="unknown session")
        return RouteResponse(
            robot_id=target.robot_id,
            agent_base_url=target.agent_base_url,
            status=target.status,
        )

    @app.get(
        "/internal/sessions/{token}/details",
        response_model=SessionDetailsResponse,
    )
    async def get_details(token: str) -> SessionDetailsResponse:
        try:
            session = registry.get(token)
        except ct.UnknownSession:
            raise HTTPException(status_code=404, detail="unknown session")
        return _session_to_details(session)

    @app.post("/internal/sessions/{token}/abort", status_code=200)
    async def abort_session(token: str) -> dict[str, str]:
        try:
            registry.get(token)
        except ct.UnknownSession:
            raise HTTPException(status_code=404, detail="unknown session")
        await _abort_session(registry, token)
        return {"status": "aborted"}

    # ------------------------------------------------------------------
    # Manual protocols (stub)
    # ------------------------------------------------------------------

    @app.post("/manual/protocols", status_code=501)
    async def submit_manual_protocol() -> dict[str, str]:
        """
        Submit a manual protocol payload for backend-driven execution.

        The slicing pipeline that converts an uploaded instruction document
        into a queue of actions is not part of this module and is not yet
        implemented. The endpoint exists so the URL surface is stable.
        """
        raise HTTPException(
            status_code=501,
            detail="manual protocol submission is not yet implemented",
        )

    return app