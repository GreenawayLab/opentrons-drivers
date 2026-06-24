"""
Frontend rendering service.

Owns the browser-facing HTML and nothing else: no database, no secrets, no
auth logic. Pages are rendered by calling the backend JSON API and relaying the
user's session cookie through unchanged. Authentication decisions belong to the
backend; this service only translates backend status codes into what a browser
should see (the login page on 401/403, the dashboard on success).

Configuration is via environment variables:

``BACKEND_URL``
    Base URL of the backend API. Defaults to ``http://backend:8000``.
``BACKEND_TIMEOUT``
    Per-request timeout (seconds) for calls to the backend. Defaults to ``200``
    to match the proxy's budget for the long-poll on session creation, though
    none of the human routes here are that slow.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.deps import templates


BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "200"))


_ROLE_DASHBOARDS = {
    "admin": "/admin/dashboard",
    "user": "/user/dashboard",
}


def dashboard_for(role: str) -> str:
    """Map a role to its landing page. Unknown roles fall back to login."""
    return _ROLE_DASHBOARDS.get(role, "/login")


app = FastAPI(title="opentrons-control-frontend")


# ------------------------------------------------------------------
# The chokepoint: every backend call goes through here.
# ------------------------------------------------------------------


async def call_backend(
    request: Request,
    method: str,
    path: str,
    *,
    json: Optional[dict[str, Any]] = None,
) -> httpx.Response:
    """
    Call the backend, forwarding the browser's session cookie inward.

    This is the single place that propagates the ``Cookie`` header, so no
    individual handler can forget the inbound leg. Relaying any ``Set-Cookie``
    back to the browser is the caller's concern (only login and logout need it).

    A fresh client is used per call deliberately: a shared client keeps a cookie
    jar that stores ``Set-Cookie`` from responses and would replay one user's
    session cookie onto the next user's request. Forwarding the header by hand
    and discarding the jar each time removes that cross-request bleed entirely.

    Transport failures (a down or unreachable backend) propagate as
    ``httpx.RequestError`` and are turned into a 502 page by the handler below,
    rather than surfacing as an unhandled 500.
    """
    headers: dict[str, str] = {}
    cookie = request.headers.get("cookie")
    if cookie:
        headers["cookie"] = cookie

    async with httpx.AsyncClient(
        timeout=BACKEND_TIMEOUT,
        follow_redirects=False,
    ) as client:
        return await client.request(method, f"{BACKEND_URL}{path}", json=json, headers=headers)


def _relay_set_cookie(backend_resp: httpx.Response, out: Response) -> None:
    """Copy any ``Set-Cookie`` headers from the backend response onto ``out``."""
    for value in backend_resp.headers.get_list("set-cookie"):
        out.headers.append("set-cookie", value)


# ------------------------------------------------------------------
# Auth pages
# ------------------------------------------------------------------


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    # Already signed in? Skip the form and go where the role belongs.
    me = await call_backend(request, "GET", "/api/auth/me")
    if me.status_code == 200:
        return RedirectResponse(url=dashboard_for(me.json()["role"]), status_code=303)
    return templates.TemplateResponse(request, "auth/login.html")


@app.post("/login")
async def login(
    request: Request,
    name: str = Form(...),
    password: str = Form(...),
) -> Response:
    backend_resp = await call_backend(
        request, "POST", "/api/auth/login", json={"name": name, "password": password}
    )
    if backend_resp.status_code != 200:
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"error": "Invalid username or password."},
            status_code=401,
        )

    role = backend_resp.json()["role"]
    out = RedirectResponse(url=dashboard_for(role), status_code=303)
    _relay_set_cookie(backend_resp, out)  # the session cookie travels to the browser
    return out


@app.post("/logout")
async def logout(request: Request) -> Response:
    backend_resp = await call_backend(request, "POST", "/api/auth/logout")
    out = RedirectResponse(url="/login", status_code=303)
    _relay_set_cookie(backend_resp, out)  # carries the cookie-clear back to the browser
    return out


# ------------------------------------------------------------------
# Admin pages
# ------------------------------------------------------------------


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Response:
    me = await call_backend(request, "GET", "/api/auth/me")
    if me.status_code != 200:
        return RedirectResponse(url="/login", status_code=303)

    user = me.json()
    if user["role"] != "admin":
        return RedirectResponse(url=dashboard_for(user["role"]), status_code=303)

    robots_resp = await call_backend(request, "GET", "/api/robots")
    if robots_resp.status_code != 200:
        return HTMLResponse(
            f"backend error fetching robots ({robots_resp.status_code})",
            status_code=502,
        )

    return templates.TemplateResponse(
        request,
        "admin/dashboard.html",
        {"user": user, "robots": robots_resp.json()},
    )


@app.post("/admin/robots")
async def save_robot(
    request: Request,
    robot_id: str = Form(...),
    host: str = Form(...),
    ssh_user: str = Form("root"),
    agent_port: int = Form(9000),
    ssh_key: str = Form(""),
) -> Response:
    resp = await call_backend(
        request,
        "POST",
        "/api/robots",
        json={
            "robot_id": robot_id,
            "host": host,
            "ssh_user": ssh_user,
            "agent_port": agent_port,
            "ssh_key": ssh_key,
        },
    )
    if resp.status_code in (401, 403):
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/robots/{robot_id}/delete")
async def delete_robot(request: Request, robot_id: str) -> Response:
    resp = await call_backend(request, "DELETE", f"/api/robots/{robot_id}")
    if resp.status_code in (401, 403):
        return RedirectResponse(url="/login", status_code=303)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


# ------------------------------------------------------------------
# User pages
# ------------------------------------------------------------------


@app.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard(request: Request) -> Response:
    me = await call_backend(request, "GET", "/api/auth/me")
    if me.status_code != 200:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(request, "user/dashboard.html", {"user": me.json()})


# ------------------------------------------------------------------
# Health and error pages
# ------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.exception_handler(httpx.RequestError)
async def backend_unreachable(request: Request, exc: httpx.RequestError) -> Response:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"code": 502, "message": "The backend is unreachable. Try again in a moment."},
        status_code=502,
    )


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> Response:
    return templates.TemplateResponse(
        request,
        "error.html",
        {"code": 404, "message": "That page does not exist."},
        status_code=404,
    )