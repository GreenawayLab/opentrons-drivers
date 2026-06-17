from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from opentrons_control.backend.app.deps import templates
from opentrons_control.backend.app.security import CurrentUser, require_admin
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute, fetch
from opentrons_control.backend.app.vault import put_secret

router = APIRouter(prefix="/admin")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    robots = fetch(db, "robots/list_all.sql")
    secrets = fetch(db, "secrets/list.sql")
    secret_names = {s["name"] for s in secrets}
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "user": user,
            "robots": robots,
            "secrets": secrets,
            "secret_names": secret_names,
        },
    )


@router.post("/robots")
def save_robot(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    robot_id: str = Form(...),
    host: str = Form(...),
    ssh_user: str = Form("root"),
    agent_port: int = Form(9000),
    ssh_key: str = Form(""),
):
    # A pasted key is stored encrypted under "<robot_id>_key" and linked.
    # Blank key leaves any existing link untouched (handled by the upsert).
    key_name = None
    if ssh_key.strip():
        key_name = f"{robot_id}_key"
        put_secret(db, key_name, ssh_key.encode(), kind="ssh_key")

    execute(
        db,
        "robots/upsert.sql",
        {
            "robot_id": robot_id,
            "host": host,
            "ssh_user": ssh_user,
            "agent_port": agent_port,
            "key_name": key_name,
        },
    )
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@router.post("/robots/{robot_id}/delete")
def delete_robot(
    robot_id: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    execute(db, "robots/delete.sql", {"robot_id": robot_id})
    return RedirectResponse(url="/admin/dashboard", status_code=303)