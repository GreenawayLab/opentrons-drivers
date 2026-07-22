"""
Backend HTTP API.

Exposes control-plane endpoints consumed by the proxy and by lab-internal
clients. Three endpoint groups:

``/internal/...``
    Session lifecycle endpoints called by the proxy on behalf of external
    clients, plus the driver-update executor called (via the maintainer) for
    fleet installs. Cover session creation, routing lookup, abort, a debug
    view, and wheel install. These are not authenticated at this layer;
    access control lives at the proxy edge (which refuses ``/internal/*`` from
    outside entirely).

``/manual/...``
    Endpoints triggered by internal lab tooling for backend-driven runs.
    Currently a stub; the slicing pipeline that turns an uploaded
    instruction document into a queue of actions lives outside this
    module.

Auth and admin management are mounted from the routers package as a JSON API
under ``/api``. Rendering lives in a separate frontend service that consumes
that API. The module exposes no top-level FastAPI instance: callers construct
the app via :func:`create_app`, passing in a fully-resolved robot registry.
This keeps configuration loading out of the library proper.

This module is excluded from strict no Any typing by mypy because it is
somehow conflicting with the pydantic BaseModel.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import AsyncIterator, Dict, Mapping, Optional, Any
from pydantic import BaseModel, Field

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from sqlalchemy.orm import Session

from opentrons_control.backend.app.launcher import launch_session
from opentrons_control.backend.app.ot_client import OTClient
from opentrons_control.backend.app.robot_sessions import (
    Robot,
    Session as RobotSession,
    SessionRegistry,
)
from opentrons_control.backend.app.routers import auth, admin, deck, user
from opentrons_control.backend.app import update
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.vault import get_secret
import opentrons_control.backend.app.settings.custom_types as ct
import opentrons_control.backend.app.settings.global_variables as gv
from opentrons_control.backend.app.generator import plan_to_protocol
from opentrons_control.backend.app.simulator import simulate
from opentrons_control.backend.app.run import (
    Executor,
    Run,
    assemble_launch_files,
    freeze_stream,
    new_run_id,
    register,
)
from opentrons_control.backend.app.run import get as get_executor
from opentrons_control.backend.app.protocol_model import BaseConfig
from opentrons_control.backend.app.db.runner import fetch_one
from opentrons_control.backend.app.security import CurrentUser, get_current_user


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


class DeployStarted(BaseModel):
    """Returned immediately when a deploy job is accepted."""

    job_id: str


class OpenRunRequest(BaseModel):
    plan_id: int
    robot_id: str


class DeployStatus(BaseModel):
    """Snapshot of a deploy job, polled until ``state`` is ``"done"``."""

    job_id: str
    version: str
    state: str
    results: Dict[str, str]



# -------------------- Helpers --------------------


def _session_to_details(session: RobotSession) -> SessionDetailsResponse:
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
    The release runs in a ``finally`` so an unexpected agent response never
    leaves the session wedged in ``aborting`` with the robot lock held.
    """
    session = registry.mark_aborting(token, message="abort requested")
    try:
        if session.agent_base_url is not None:
            async with OTClient(session.agent_base_url) as client:
                await client.abort()
    finally:
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
    # JSON API: auth and admin management
    # ------------------------------------------------------------------

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(deck.router)
    app.include_router(user.router)

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
    # Internal: driver-update executor (maintainer-facing)
    # ------------------------------------------------------------------

    @app.post("/internal/update", response_model=DeployStarted)
    async def execute_update(
        version: str = Form(...),
        robot_ids: str = Form(""),
        wheel: UploadFile = File(...),
    ) -> DeployStarted:
        """
        Accept a driver install and run it in the background.

        The backend holds no wheel store; the maintainer owns versioned wheels
        and hands the backend a single wheel to install. ``robot_ids`` is a
        comma-separated target list; empty means every currently-available
        robot. The wheel upload is read in full and the target set validated
        here (so a bad robot id fails fast), then the install runs as a
        background job and this returns a ``job_id`` immediately. Poll
        ``/internal/update/status/{job_id}`` for per-robot progress.
        """
        data = await wheel.read()
        if not data:
            raise HTTPException(status_code=400, detail="empty wheel upload")
        if len(data) > gv.MAX_WHEEL_BYTES:
            raise HTTPException(status_code=413, detail="wheel exceeds size limit")
        if not wheel.filename:
            raise HTTPException(status_code=400, detail="wheel upload must include a filename")

        ids = [r.strip() for r in robot_ids.split(",") if r.strip()]
        try:
            targets = update.resolve_targets(registry, ids)
        except ct.UnknownRobot as e:
            raise HTTPException(
                status_code=404, detail=f"unknown robot in target set: {e}"
            )

        try:
            job_id = update.start_install_job(
                registry, data, wheel.filename, version, targets
            )
        except update.UpdateError as e:
            raise HTTPException(status_code=400, detail=str(e))

        return DeployStarted(job_id=job_id)

    @app.get("/internal/update/status/{job_id}", response_model=DeployStatus)
    async def update_status(job_id: str) -> DeployStatus:
        """Return a deploy job's per-robot progress, or 404 if unknown.

        A 404 means the job id isn't known — either never existed or lost to a
        backend restart (the job store is in-memory and transitional). The
        caller should treat that as "redeploy", never as success.
        """
        job = update.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="unknown job")
        return DeployStatus(**update.job_status(job))

    @app.get("/internal/update/token")
    async def get_git_token(db: Session = Depends(get_db)) -> Response:
        """
        Return the git access token for the maintainer, if configured.

        The token (a read-only PAT) lives encrypted in the vault and is
        decrypted only in memory. A 404 means none is configured, which the
        maintainer treats as "public repo" and fetches unauthenticated.
        Reachable only on the internal network (the proxy refuses
        ``/internal/*`` from outside).
        """
        try:
            token = get_secret(db, gv.GIT_TOKEN_SECRET)
        except KeyError:
            raise HTTPException(
                status_code=404, detail="git token not configured"
            )
        return Response(content=token, media_type="text/plain")

    # ------------------------------------------------------------------
    # Manual runs: open -> (calibrate) -> start, with abort / pause / resume
    # ------------------------------------------------------------------

    @app.post("/runs", status_code=201)
    async def open_run(
        req: OpenRunRequest,
        user: CurrentUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        """Freeze a plan, gate it on the checker, and book plus boot the robot.

        Lands the run in ``ready`` with an open session and a dormant executor.
        Nothing drives yet: the user may calibrate offsets against the open
        agent, then call /start to attach the driver, or /cancel to back out.
        """
        plan = fetch_one(db, "action_plans/get.sql", {"id": req.plan_id})
        if plan is None:
            raise HTTPException(status_code=404, detail=f"unknown plan {req.plan_id}")
        cfg_row = fetch_one(db, "deck_configs/get.sql", {"id": plan["config_id"]})
        if cfg_row is None:
            raise HTTPException(status_code=404, detail=f"unknown config {plan['config_id']}")
        config = BaseConfig.model_validate(cfg_row["config"])

        # freeze: expand to commands with how, then gate on the checker so no
        # invalid or incomplete plan can reach a robot
        protocol, incomplete, gen_errors = plan_to_protocol(config, plan["steps"], name=plan["name"])
        if incomplete:
            raise HTTPException(status_code=409, detail="plan is incomplete; finish it before running")
        if gen_errors:
            raise HTTPException(status_code=409, detail=gen_errors[0])
        report = simulate(protocol)
        if not report.ok:
            first = next((e for v in report.verdicts for e in v.errors), "plan does not pass the check")
            raise HTTPException(status_code=409, detail=first)
        stream = freeze_stream(protocol.steps)

        # ship the labware defs the config references that live in the labware
        # table; standard opentrons labware is resolved on the robot and skipped
        labware_defs: dict[str, Any] = {}
        for plate in {**config.core_plates, **config.stock_plates}.values():
            row = fetch_one(db, "labware/get.sql", {"name": plate.type})
            if row is not None:
                labware_defs[plate.type] = row["definition"]
        files = assemble_launch_files(cfg_row["config"], labware_defs)

        run_id = new_run_id()
        try:
            session = await launch_session(
                registry,
                robot_id=req.robot_id,
                protocol_name=plan["name"],
                mode="manual",
                files=files,
                client_id=run_id,
            )
        except ct.UnknownRobot:
            raise HTTPException(status_code=404, detail=f"unknown robot {req.robot_id!r}")
        except ct.RobotBusy as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ct.FileFormatError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ct.BootstrapFailed as e:
            raise HTTPException(status_code=502, detail=str(e))

        # dormant executor: constructed with the open session but not started, so
        # calibration can run first. The token releases the robot on teardown.
        token = session.token
        the_run = Run(run_id=run_id, robot_id=req.robot_id, stream=stream, token=token)
        executor = Executor(the_run, session.agent_base_url, on_teardown=lambda: registry.release(token))
        register(executor)
        return {"run_id": run_id, "token": token, "status": the_run.status, "total": the_run.total}

    def _run_or_404(run_id: str) -> Executor:
        ex = get_executor(run_id)
        if ex is None:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return ex

    @app.post("/runs/{run_id}/start")
    async def start_run(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        """Attach the driver and begin executing, after any calibration."""
        ex = _run_or_404(run_id)
        ex.start()
        return ex.status()

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        """Back out of a ready run that was never started, releasing the robot."""
        ex = _run_or_404(run_id)
        ex.cancel()
        return ex.status()

    @app.post("/runs/{run_id}/abort")
    async def abort_run(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        ex = _run_or_404(run_id)
        ex.abort()
        return ex.status()

    @app.post("/runs/{run_id}/pause")
    async def pause_run(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        ex = _run_or_404(run_id)
        ex.pause()
        return ex.status()

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        ex = _run_or_404(run_id)
        ex.resume()
        return ex.status()

    @app.get("/runs/{run_id}")
    async def run_status(run_id: str, user: CurrentUser = Depends(get_current_user)) -> dict[str, Any]:
        return _run_or_404(run_id).status()

    return app