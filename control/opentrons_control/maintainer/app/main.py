"""
Maintainer service.

The frontend (reached by the browser through the proxy) calls this service to
build and deploy the drivers wheel. The maintainer owns the wheel store, builds
the drivers subpackage from a fresh checkout, and hands the result plus an
instruction to the backend, which is the only component that touches a robot.

Surface
-------
POST /build              fetch source tarball (optional token from backend) +
                         build + store -> {"version": ...}
POST /deploy             send a stored wheel + instruction to the backend
                         {"version", "robot_ids"?} -> {"version", "results"}
GET  /versions           list stored versions
GET  /health             liveness
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from opentrons_control.maintainer.app.backend_client import BackendError
from opentrons_control.maintainer.app.backend_client import fetch_git_token
from opentrons_control.maintainer.app.backend_client import get_install_status
from opentrons_control.maintainer.app.backend_client import start_install
from opentrons_control.maintainer.app.builder import WheelBuildError
from opentrons_control.maintainer.app.builder import build_drivers_wheel
from opentrons_control.maintainer.app.source import SourceError
from opentrons_control.maintainer.app.source import fetch_source
from opentrons_control.maintainer.app.store import list_versions
from opentrons_control.maintainer.app.store import store_wheel
from opentrons_control.maintainer.app.store import wheel_for


app = FastAPI(title="opentrons-control-maintainer")


class BuildResponse(BaseModel):
    version: str


class DeployRequest(BaseModel):
    version: str
    robot_ids: list[str] = []


class DeployResponse(BaseModel):
    job_id: str


class DeployStatusResponse(BaseModel):
    job_id: str
    version: str
    state: str
    results: dict[str, str]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/versions", response_model=list[str])
async def versions() -> list[str]:
    """List the versions currently in the wheel store."""
    return list_versions()


@app.post("/build", response_model=BuildResponse)
async def build() -> BuildResponse:
    """Fetch the drivers source, build the wheel, and store it.

    Asks the backend for the (optional) git token, downloads the repo archive
    at the configured ref, extracts the drivers subtree, builds it, and files
    the wheel in the store. The blocking fetch/build steps run in a worker
    thread so the event loop is free.
    """
    loop = asyncio.get_running_loop()

    token = await fetch_git_token()

    with tempfile.TemporaryDirectory(prefix="maint-build-") as tmp:
        tmp_path = Path(tmp)
        try:
            project = await loop.run_in_executor(
                None, fetch_source, token, tmp_path / "src"
            )
        except SourceError as e:
            raise HTTPException(status_code=502, detail=f"source fetch failed: {e}")

        try:
            wheel, version = await loop.run_in_executor(
                None, build_drivers_wheel, project, tmp_path / "dist"
            )
        except WheelBuildError as e:
            raise HTTPException(status_code=500, detail=f"build failed: {e}")

        store_wheel(wheel, version)

    return BuildResponse(version=version)


@app.post("/deploy", response_model=DeployResponse)
async def deploy(req: DeployRequest) -> DeployResponse:
    """Hand a stored wheel + instruction to the backend; returns a job id.

    The backend runs the install in the background and reports progress; this
    returns immediately. ``robot_ids`` empty means "all available robots"
    (resolved on the backend). Poll ``/deploy/status/{job_id}`` for progress.
    """
    wheel = wheel_for(req.version)
    if wheel is None:
        raise HTTPException(
            status_code=404, detail=f"no stored wheel for version {req.version!r}"
        )
    try:
        started = await start_install(wheel, req.version, req.robot_ids)
    except BackendError as e:
        raise HTTPException(status_code=502, detail=f"backend deploy failed: {e}")

    return DeployResponse(job_id=started["job_id"])


@app.get("/deploy/status/{job_id}", response_model=DeployStatusResponse)
async def deploy_status(job_id: str) -> DeployStatusResponse:
    """Passthrough to the backend's deploy-job status.

    Stateless: the maintainer keeps no job state; it forwards the backend's
    per-robot progress so the frontend has a single place to poll.
    """
    try:
        status = await get_install_status(job_id)
    except BackendError as e:
        raise HTTPException(status_code=502, detail=f"status fetch failed: {e}")
    return DeployStatusResponse(**status)