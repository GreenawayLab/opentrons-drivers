"""
Frontend rendering service.

Owns the browser-facing HTML and nothing else: no database, no secrets, no
auth logic. Pages are rendered by calling the backend JSON API (relaying the
user's session cookie through unchanged) and, for driver updates, by calling
the maintainer service. Authentication decisions belong to the backend; this
service only translates backend status codes into what a browser should see
(the login page on 401/403, the dashboard on success).

Configuration is via environment variables:

``BACKEND_URL``
    Base URL of the backend API. Defaults to ``http://backend:8000``.
``MAINTAINER_URL``
    Base URL of the maintainer service. Defaults to ``http://maintainer:8000``.
``BACKEND_TIMEOUT`` / ``MAINTAINER_TIMEOUT``
    Per-request timeouts (seconds). The maintainer timeout is generous: a build
    pulls + compiles a wheel, and a deploy blocks until every targeted robot
    has finished its pip install.
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from opentrons_control.frontend.app.deps import templates


BACKEND_URL = os.environ.get("BACKEND_URL", "http://backend:8000").rstrip("/")
BACKEND_TIMEOUT = float(os.environ.get("BACKEND_TIMEOUT", "200"))
MAINTAINER_URL = os.environ.get("MAINTAINER_URL", "http://maintainer:8000").rstrip("/")
MAINTAINER_TIMEOUT = float(os.environ.get("MAINTAINER_TIMEOUT", "600"))


_ROLE_DASHBOARDS = {
    "admin": "/admin/dashboard",
    "user": "/user/dashboard",
}


def dashboard_for(role: str) -> str:
    """Map a role to its landing page. Unknown roles fall back to login."""
    return _ROLE_DASHBOARDS.get(role, "/login")


app = FastAPI(title="opentrons-control-frontend")


# ------------------------------------------------------------------
# Chokepoints: every upstream call goes through one of these.
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

    The single place that propagates the ``Cookie`` header, so no handler can
    forget the inbound leg. A fresh client per call avoids a shared cookie jar
    replaying one user's session onto the next. Transport failures propagate as
    ``httpx.RequestError`` and become a 502 page via the handler below.
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


async def call_maintainer(
    method: str,
    path: str,
    *,
    json: Optional[dict[str, Any]] = None,
) -> httpx.Response:
    """
    Call the maintainer service.

    The maintainer is unauthenticated and internal-only; access is gated here by
    an admin check before any call is made. No cookie is forwarded. A fresh
    client per call, generous timeout for build/deploy.
    """
    async with httpx.AsyncClient(
        timeout=MAINTAINER_TIMEOUT,
        follow_redirects=False,
    ) as client:
        return await client.request(method, f"{MAINTAINER_URL}{path}", json=json)


def _relay_set_cookie(backend_resp: httpx.Response, out: Response) -> None:
    """Copy any ``Set-Cookie`` headers from the backend response onto ``out``."""
    for value in backend_resp.headers.get_list("set-cookie"):
        out.headers.append("set-cookie", value)


async def _admin_or_redirect(
    request: Request,
) -> tuple[Optional[dict[str, Any]], Optional[Response]]:
    """
    Resolve the current user and require the admin role.

    Returns ``(user, None)`` for an admin, or ``(None, redirect)`` pointing at
    the login page (not signed in) or the user's own dashboard (wrong role).
    """
    me = await call_backend(request, "GET", "/api/auth/me")
    if me.status_code != 200:
        return None, RedirectResponse(url="/login", status_code=303)
    user = me.json()
    if user["role"] != "admin":
        return None, RedirectResponse(url=dashboard_for(user["role"]), status_code=303)
    return user, None

async def _user_or_redirect(
    request: Request,
) -> tuple[Optional[dict[str, Any]], Optional[Response]]:
    """
    Resolve the current user; any signed-in role is allowed.

    Returns ``(user, None)`` when signed in, or ``(None, redirect)`` to the
    login page when not.
    """
    me = await call_backend(request, "GET", "/api/auth/me")
    if me.status_code != 200:
        return None, RedirectResponse(url="/login", status_code=303)
    return me.json(), None

# ------------------------------------------------------------------
# Auth pages
# ------------------------------------------------------------------


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
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
    _relay_set_cookie(backend_resp, out)
    return out


@app.post("/logout")
async def logout(request: Request) -> Response:
    backend_resp = await call_backend(request, "POST", "/api/auth/logout")
    out = RedirectResponse(url="/login", status_code=303)
    _relay_set_cookie(backend_resp, out)
    return out


# ------------------------------------------------------------------
# Admin: robots
# ------------------------------------------------------------------


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect

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
    await call_backend(request, "POST", "/api/robots/reload")
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/robots/{robot_id}/delete")
async def delete_robot(request: Request, robot_id: str) -> Response:
    resp = await call_backend(request, "DELETE", f"/api/robots/{robot_id}")
    if resp.status_code in (401, 403):
        return RedirectResponse(url="/login", status_code=303)
    await call_backend(request, "POST", "/api/robots/reload")
    return RedirectResponse(url="/admin/dashboard", status_code=303)


# ------------------------------------------------------------------
# Admin: driver updates (via the maintainer)
# ------------------------------------------------------------------


async def _render_updates(
    request: Request,
    user: dict[str, Any],
    *,
    build_result: Optional[str] = None,
    deploy_job_id: Optional[str] = None,
    deploy_version: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    """Fetch the state the updates page needs and render it."""
    robots_resp = await call_backend(request, "GET", "/api/robots")
    robots = robots_resp.json() if robots_resp.status_code == 200 else []

    token_resp = await call_backend(request, "GET", "/api/git-token")
    token_set = token_resp.json().get("set", False) if token_resp.status_code == 200 else False

    try:
        versions_resp = await call_maintainer("GET", "/versions")
        versions = versions_resp.json() if versions_resp.status_code == 200 else []
        maintainer_down = versions_resp.status_code != 200
    except httpx.RequestError:
        versions = []
        maintainer_down = True

    return templates.TemplateResponse(
        request,
        "admin/updates.html",
        {
            "user": user,
            "robots": robots,
            "versions": versions,
            "token_set": token_set,
            "maintainer_down": maintainer_down,
            "build_result": build_result,
            "deploy_job_id": deploy_job_id,
            "deploy_version": deploy_version,
            "error": error,
        },
    )


@app.get("/admin/methods", response_class=HTMLResponse)
async def admin_methods(request: Request) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "admin/methods.html", {"user": user})


@app.get("/admin/updates", response_class=HTMLResponse)
async def updates_page(request: Request) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect
    return await _render_updates(request, user)


@app.post("/admin/updates/build", response_class=HTMLResponse)
async def updates_build(request: Request) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect

    try:
        resp = await call_maintainer("POST", "/build")
    except httpx.RequestError as e:
        return await _render_updates(request, user, error=f"Maintainer unreachable: {e}")

    if resp.status_code != 200:
        return await _render_updates(
            request, user, error=f"Build failed ({resp.status_code}): {resp.text}"
        )
    return await _render_updates(request, user, build_result=resp.json().get("version"))


@app.post("/admin/updates/deploy", response_class=HTMLResponse)
async def updates_deploy(
    request: Request,
    version: str = Form(...),
    robot_ids: list[str] = Form([]),
) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect

    try:
        resp = await call_maintainer(
            "POST", "/deploy", json={"version": version, "robot_ids": robot_ids}
        )
    except httpx.RequestError as e:
        return await _render_updates(request, user, error=f"Maintainer unreachable: {e}")

    if resp.status_code != 200:
        return await _render_updates(
            request, user, error=f"Deploy failed ({resp.status_code}): {resp.text}"
        )
    return await _render_updates(
        request,
        user,
        deploy_job_id=resp.json().get("job_id"),
        deploy_version=version,
    )


@app.get("/admin/updates/status/{job_id}")
async def updates_status(request: Request, job_id: str) -> Response:
    """JSON proxy the deploy panel polls for per-robot progress.

    Admin-gated like the rest of the page; forwards the maintainer's status
    (which forwards the backend's). Returns the status JSON as-is so the
    client-side poller can render per-robot rows until ``state == "done"``.
    """
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        resp = await call_maintainer("GET", f"/deploy/status/{job_id}")
    except httpx.RequestError as e:
        return JSONResponse({"error": f"maintainer unreachable: {e}"}, status_code=502)
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.post("/admin/git-token")
async def set_git_token(request: Request, token: str = Form(...)) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect
    await call_backend(request, "POST", "/api/git-token", json={"token": token})
    return RedirectResponse(url="/admin/updates", status_code=303)



# ------------------------------------------------------------------
# User pages
# ------------------------------------------------------------------


@app.get("/user/dashboard", response_class=HTMLResponse)
async def user_dashboard(request: Request) -> Response:
    user, redirect = await _user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "user/dashboard.html", {"user": user})


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
        "user/error.html",
        {"code": 502, "message": "The backend is unreachable. Try again in a moment."},
        status_code=502,
    )


@app.exception_handler(404)
async def not_found(request: Request, exc: Exception) -> Response:
    return templates.TemplateResponse(
        request,
        "user/error.html",
        {"code": 404, "message": "That page does not exist."},
        status_code=404,
    )


# ---- register (PUBLIC — add "/register" to the proxy allow-list) ----

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> Response:
    return templates.TemplateResponse(request, "auth/register.html", {})


@app.post("/register")
async def register_submit(request: Request) -> Response:
    form = await request.form()
    r = await call_backend(request, "POST", "/api/auth/register", json={
        "name": form.get("name", ""),
        "password": form.get("password", ""),
        "code": form.get("code", ""),
    })
    if r.status_code == 200:
        # invites only mint 'user', so a fresh account always lands here
        resp = RedirectResponse(url="/user/dashboard", status_code=303)
        _relay_set_cookie(r, resp)
        return resp
    detail = "Registration failed."
    try:
        detail = r.json().get("detail", detail)
    except Exception:
        pass
    return templates.TemplateResponse(
        request, "auth/register.html", {"error": detail}, status_code=r.status_code
    )


# ---- user: deck configuration ----

@app.get("/user/configs", response_class=HTMLResponse)
async def configs_page(request: Request) -> Response:
    user, redirect = await _user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "user/configs.html", {"user": user})


@app.put("/user/draft/{kind}")
async def draft_save(request: Request, kind: str) -> Response:
    r = await call_backend(request, "PUT", f"/api/user/draft/{kind}", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/draft/{kind}")
async def draft_get(request: Request, kind: str) -> Response:
    r = await call_backend(request, "GET", f"/api/user/draft/{kind}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/draft/{kind}")
async def draft_delete(request: Request, kind: str) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/draft/{kind}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/methods")
async def methods_list(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/methods")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/methods")
async def methods_save(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/methods", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/methods/{name}")
async def methods_delete(request: Request, name: str) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/methods/{name}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/robots")
async def robots_for_user(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/robots")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/runs")
async def runs_open(request: Request) -> Response:
    r = await call_backend(request, "POST", "/runs", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/runs/{run_id}")
async def runs_status(request: Request, run_id: str) -> Response:
    r = await call_backend(request, "GET", f"/runs/{run_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/runs/{run_id}/{action}")
async def runs_control(request: Request, run_id: str, action: str) -> Response:
    # whitelist the control verbs so the relay cannot reach arbitrary backend paths
    if action not in ("start", "cancel", "abort", "pause", "resume"):
        return JSONResponse({"detail": f"unknown run action {action}"}, status_code=404)
    r = await call_backend(request, "POST", f"/runs/{run_id}/{action}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/plans/mine")
async def plans_mine(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/plans/mine")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/plans/others")
async def plans_others(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/plans/others")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/plans/{plan_id}/versions")
async def plans_versions(request: Request, plan_id: int) -> Response:
    r = await call_backend(request, "GET", f"/api/user/plans/{plan_id}/versions")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/plans/{plan_id}")
async def plans_get(request: Request, plan_id: int) -> Response:
    r = await call_backend(request, "GET", f"/api/user/plans/{plan_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/plans/check")
async def plans_check(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/plans/check", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/plans")
async def plans_save(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/plans/", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/plans/{plan_id}/family")
async def plans_delete_family(request: Request, plan_id: int) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/plans/{plan_id}/family")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/plans/{plan_id}")
async def plans_delete_version(request: Request, plan_id: int) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/plans/{plan_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/actions", response_class=HTMLResponse)
async def actions_page(request: Request) -> Response:
    user, redirect = await _user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "user/actions.html", {"user": user})


@app.get("/user/library", response_class=HTMLResponse)
async def library_page(request: Request) -> Response:
    user, redirect = await _user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "user/library.html", {"user": user})


@app.get("/user/deck", response_class=HTMLResponse)
async def deck_page(request: Request) -> Response:
    user, redirect = await _user_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "user/deck.html", {"user": user})


@app.get("/user/deck/labware")
async def deck_list_labware(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/deck/labware")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/labware/{name}")
async def deck_get_labware(request: Request, name: str) -> Response:
    r = await call_backend(request, "GET", f"/api/user/deck/labware/{name}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/deck/labware")
async def deck_save_labware(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/deck/labware", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/standard-units")
async def deck_list_units(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/deck/standard-units")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/deck/standard-units")
async def deck_add_unit(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/deck/standard-units", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/configs")
async def deck_list_configs(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/deck/configs")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/configs/mine")
async def deck_configs_mine(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/deck/configs/mine")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/configs/others")
async def deck_configs_others(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/user/deck/configs/others")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/configs/{config_id}")
async def deck_get_config(request: Request, config_id: int) -> Response:
    r = await call_backend(request, "GET", f"/api/user/deck/configs/{config_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/user/deck/configs")
async def deck_save_config(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/user/deck/configs", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/user/deck/configs/{config_id}/versions")
async def deck_config_versions(request: Request, config_id: int) -> Response:
    r = await call_backend(request, "GET", f"/api/user/deck/configs/{config_id}/versions")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/deck/configs/{config_id}/family")
async def deck_delete_family(request: Request, config_id: int) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/deck/configs/{config_id}/family")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/user/deck/configs/{config_id}")
async def deck_delete_config(request: Request, config_id: int) -> Response:
    r = await call_backend(request, "DELETE", f"/api/user/deck/configs/{config_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


# ---- admin: user management (page + relays; no create — invites onboard) ----

@app.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request) -> Response:
    user, redirect = await _admin_or_redirect(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "admin/users.html", {"user": user})


@app.get("/admin/users/list")
async def users_list(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/users")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/admin/users/{user_id}")
async def users_deactivate(request: Request, user_id: int) -> Response:
    r = await call_backend(request, "DELETE", f"/api/users/{user_id}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.get("/admin/users/{user_id}/permissions")
async def users_perms_list(request: Request, user_id: int) -> Response:
    r = await call_backend(request, "GET", f"/api/users/{user_id}/permissions")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/admin/users/{user_id}/permissions")
async def users_perms_grant(request: Request, user_id: int) -> Response:
    r = await call_backend(request, "POST", f"/api/users/{user_id}/permissions", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/admin/users/{user_id}/permissions/{permission}")
async def users_perms_revoke(request: Request, user_id: int, permission: str) -> Response:
    r = await call_backend(request, "DELETE", f"/api/users/{user_id}/permissions/{permission}")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/admin/users/{user_id}/password")
async def users_reset_password(request: Request, user_id: int) -> Response:
    r = await call_backend(request, "POST", f"/api/users/{user_id}/password", json=await request.json())
    return JSONResponse(r.json(), status_code=r.status_code)


# ---- admin: invites ----

@app.get("/admin/invites")
async def invites_list(request: Request) -> Response:
    r = await call_backend(request, "GET", "/api/invites")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.post("/admin/invites")
async def invites_issue(request: Request) -> Response:
    r = await call_backend(request, "POST", "/api/invites")
    return JSONResponse(r.json(), status_code=r.status_code)


@app.delete("/admin/invites/{code}")
async def invites_revoke(request: Request, code: str) -> Response:
    r = await call_backend(request, "DELETE", f"/api/invites/{code}")
    return JSONResponse(r.json(), status_code=r.status_code)