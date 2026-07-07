"""
Routing proxy for the Opentrons control plane.

The proxy is the only network ingress reachable by external clients. It holds
no business logic and no durable state: every request is forwarded to one of
three places.

Session lifecycle (``/sessions``)
    Forwarded to the backend, which owns session state and drives the SSH
    bootstrap of agents.

Action surface (``/actions``)
    Authenticated by a session token in the ``Authorization`` header. The proxy
    looks the token up against the backend, resolves it to an agent base URL,
    and forwards the request body verbatim.

Human console (``/``, ``/login``, ``/logout``, ``/admin/*``, ``/user/*``,
``/static/*``)
    Forwarded to the frontend, which renders HTML by calling the backend's JSON
    API. These are the only browser-facing paths; the forward is cookie-aware
    so the session cookie survives the hop in both directions.

Everything else is refused with 404. That allowlist is load-bearing: it is what
keeps the backend's ``/internal/*``, ``/api/*``, ``/manual/*`` and ``/robots``
surfaces unreachable from outside, since a naive catch-all would proxy them
straight to an unauthenticated control plane.

Configuration is via environment variables:

``BACKEND_URL``
    Base URL of the backend API. Defaults to ``http://backend:8000``.
``FRONTEND_URL``
    Base URL of the frontend renderer. Defaults to ``http://frontend:8000``.
``PROXY_TIMEOUT``
    Per-request timeout (seconds) for outbound calls. Defaults to ``200`` to
    accommodate the long-poll on session creation while the OT agent boots.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response


# -------------------- Configuration --------------------


BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://frontend:8000").rstrip("/")
PROXY_TIMEOUT = float(os.environ.get("PROXY_TIMEOUT", "200"))


#: Path prefixes the proxy will forward to the frontend. Anything not matched
#: here (and not matched by an explicit route below) is refused with 404.
_HUMAN_PREFIXES = ("/login", "/logout", "/register", "/admin", "/user", "/static")


def _is_human_route(path: str) -> bool:
    """True if ``path`` is a browser-facing route the frontend should render."""
    if path == "/":
        return True
    return any(path == p or path.startswith(p + "/") for p in _HUMAN_PREFIXES)


# -------------------- App lifespan --------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Hold a long-lived async HTTP client for the session/action forwarders."""
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

    Returns the route descriptor. A non-active session is rejected with 410 Gone
    so the client distinguishes "this was a valid session but isn't anymore"
    from "this token never existed" (404).
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
    Forward a request to ``url`` and mirror the response back verbatim.

    Preserves the status code, body bytes, and upstream content-type. Used for
    the session and action surfaces, which are token-based and carry no cookies.
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


async def _forward_human(request: Request, path: str) -> Response:
    """
    Forward a browser request to the frontend, cookie-aware.

    Passes the ``Cookie`` header inward and relays ``Set-Cookie`` and
    ``Location`` back out, without following redirects (the browser does that,
    so the address bar tracks the real navigation). A fresh client is used per
    call so no ``Set-Cookie`` from a login response is retained in a shared jar
    and replayed onto another user's request.
    """
    headers: dict[str, str] = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    cookie = request.headers.get("cookie")
    if cookie:
        headers["cookie"] = cookie

    body = await request.body()

    async with httpx.AsyncClient(timeout=PROXY_TIMEOUT, follow_redirects=False) as client:
        try:
            r = await client.request(
                request.method,
                f"{FRONTEND_URL}{path}",
                content=body,
                headers=headers,
                params=request.query_params,
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"frontend unreachable: {e}")

    out = Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type"),
    )
    for value in r.headers.get_list("set-cookie"):
        out.headers.append("set-cookie", value)
    location = r.headers.get("location")
    if location:
        out.headers["location"] = location
    return out


# -------------------- Session lifecycle --------------------


@app.post("/sessions")
async def create_session(request: Request) -> Response:
    """
    Create a new session.

    Forwarded verbatim to ``POST /internal/sessions`` on the backend. The
    response contains the session token the client uses on subsequent action
    requests. Blocks for the duration of the agent bootstrap on the OT
    (typically 60-90 seconds); clients should use a generous timeout.
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

    Maps to ``POST /internal/sessions/{token}/abort`` on the backend, which
    marks the session aborting, signals the agent to terminate, and releases the
    robot lock.
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
    """Liveness probe for the proxy itself. Does not check upstreams."""
    return {"status": "ok"}


# -------------------- Human console (catch-all, registered last) --------------


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def human_console(full_path: str, request: Request) -> Response:
    """
    Forward browser traffic to the frontend, or refuse it.

    Registered last so the explicit session/action/health routes match first.
    Only allowlisted human paths are forwarded; everything else (notably the
    backend's ``/internal/*``, ``/api/*``, ``/manual/*`` and ``/robots``) is
    refused, keeping the control plane unreachable from outside.
    """
    path = "/" + full_path
    if not _is_human_route(path):
        raise HTTPException(status_code=404, detail="not found")
    return await _forward_human(request, path)