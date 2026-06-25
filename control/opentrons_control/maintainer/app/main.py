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
from opentrons_control.maintainer.app.backend_client import send_install
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
    version: str
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
    """Send a stored wheel + instruction to the backend executor.

    ``robot_ids`` empty means "all available robots" (resolved on the backend).
    """
    wheel = wheel_for(req.version)
    if wheel is None:
        raise HTTPException(
            status_code=404, detail=f"no stored wheel for version {req.version!r}"
        )
    try:
        report = await send_install(wheel, req.version, req.robot_ids)
    except BackendError as e:
        raise HTTPException(status_code=502, detail=f"backend install failed: {e}")

    return DeployResponse(version=req.version, results=report.get("results", {}))