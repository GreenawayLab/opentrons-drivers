from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from opentrons_control.backend.app.settings.deps import templates
from opentrons_control.backend.app.settings.security import (
    create_token,
    dashboard_for,
    get_current_user_redirect,
    verify_password,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import fetch_one

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request})


@router.post("/login")
def login(
    request: Request,
    name: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = fetch_one(db, "users/get_by_name.sql", {"name": name})
    if not user or not verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid username or password."},
            status_code=401,
        )
    token = create_token(user["id"])
    response = RedirectResponse(url=dashboard_for(user["role"]), status_code=302)
    response.set_cookie("access_token", token, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("access_token")
    return response


@router.get("/dashboard")
def dashboard(user=Depends(get_current_user_redirect)):
    return RedirectResponse(url=dashboard_for(user.role), status_code=302)