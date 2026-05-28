"""
Routing proxy for the Opentrons control plane.

The proxy is the only network ingress reachable by external clients. It
holds no business logic and no durable state: every request is either
forwarded to the backend (control plane) or routed to an agent on the
isolated robot subnet (data plane).

Two endpoint groups:

Session lifecycle (``/sessions``)
    Forwarded to the backend, which owns session state and drives the
    SSH bootstrap of agents.

Action surface (``/actions``)
    Authenticated by a session token in the ``Authorization`` header. The
    proxy looks the token up against the backend, resolves it to an
    agent base URL, and forwards the request body verbatim.

The backend is consulted on every action request; there is no caching in
this revision. Routing is correct at the cost of a small per-request
round trip to the backend.

Configuration is via environment variables:

``BACKEND_URL``
    Base URL of the backend API. Defaults to ``http://backend:8000``.
``PROXY_TIMEOUT``
    Per-request timeout (seconds) for outbound calls. Defaults to ``200``
    to accommodate the long-poll on session creation while the OT agent
    boots.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response


# -------------------- Configuration --------------------


BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "200"))


# -------------------- App lifespan --------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold a long-lived async HTTP client for outbound calls."""
    async with httpx.AsyncClient(timeout=PROXY_TIMEOUT) as client:
        app.state.http = client
        yield


app = FastAPI(title="opentrons-control-proxy", lifespan=lifespan)


# -------------------- Helpers --------------------


def _bearer(authorization: Optional[str]) -> str:
    """
    Extract the bearer token from an ``Authorization`` header.

    Raises ``HTTPException(401)`` if the header is missing or malformed.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="missing Authorization header")
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'")
    return parts[1]


async def _resolve_route(http: httpx.AsyncClient, token: str) -> dict[str, Any]:
    """
    Look up a session token against the backend.

    Returns the route descriptor. A non-active session is rejected with
    410 Gone so the client distinguishes "this was a valid session but
    isn't anymore" from "this token never existed" (404).
    """
    try:
        r = await http.get(f"{BACKEND_URL}/internal/sessions/{token}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"backend unreachable: {e}")

    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="unknown session")
    if r.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"backend returned {r.status_code} on route lookup",
        )

    route = r.json()
    if route.get("status") != "active":
        raise HTTPException(
            status_code=410,
            detail=f"session not routable (status={route.get('status')!r})",
        )
    return route


async def _forward(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
) -> Response:
    """
    Forward a request to ``url`` and mirror the response back to the
    caller verbatim.

    The proxy preserves the status code, the body bytes, and the
    upstream content-type. It does not inspect or transform the payload.
    """
    headers: dict[str, str] = {}
    if content_type is not None and body is not None:
        headers["Content-Type"] = content_type

    try:
        r = await http.request(method, url, content=body, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream unreachable: {e}")

    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
    )


# -------------------- Session lifecycle --------------------


@app.post("/sessions")
async def create_session(request: Request) -> Response:
    """
    Create a new session.

    Forwarded verbatim to ``POST /internal/sessions`` on the backend. The
    response contains the session token the client uses on subsequent
    action requests.

    This call blocks for the duration of the agent bootstrap on the OT
    (typically 60-90 seconds). Clients should use a generous timeout.
    """
    body = await request.body()
    return await _forward(
        request.app.state.http,
        "POST",
        f"{BACKEND_URL}/internal/sessions",
        body=body,
        content_type=request.headers.get("content-type", "application/json"),
    )


@app.delete("/sessions/{token}")
async def end_session(token: str, request: Request) -> Response:
    """
    Tear down a session.

    Maps to ``POST /internal/sessions/{token}/abort`` on the backend,
    which marks the session aborting, signals the agent to terminate,
    and releases the robot lock.
    """
    return await _forward(
        request.app.state.http,
        "POST",
        f"{BACKEND_URL}/internal/sessions/{token}/abort",
    )


# -------------------- Action surface --------------------


@app.post("/actions")
async def submit_action(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """Forward an action submission to the agent owning this session."""
    token = _bearer(authorization)
    route = await _resolve_route(request.app.state.http, token)
    body = await request.body()
    return await _forward(
        request.app.state.http,
        "POST",
        f"{route['agent_base_url']}/actions",
        body=body,
        content_type=request.headers.get("content-type", "application/json"),
    )


@app.get("/actions/current")
async def get_current(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """Return the agent's current slot view for the session's owner."""
    token = _bearer(authorization)
    route = await _resolve_route(request.app.state.http, token)
    return await _forward(
        request.app.state.http,
        "GET",
        f"{route['agent_base_url']}/actions/current",
    )


@app.get("/actions/{job_id}")
async def get_job(
    job_id: str,
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Response:
    """Return the agent's snapshot for a specific job_id."""
    token = _bearer(authorization)
    route = await _resolve_route(request.app.state.http, token)
    return await _forward(
        request.app.state.http,
        "GET",
        f"{route['agent_base_url']}/actions/{job_id}",
    )


# -------------------- Health --------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe for the proxy itself. Does not check the backend."""
    return {"status": "ok"}