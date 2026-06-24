"""
Admin robot-management API.

Returns and mutates robot configuration as JSON. A pasted SSH key is stored
encrypted under ``<robot_id>_key`` and linked to the robot; a blank key on save
leaves any existing link untouched (handled by the upsert). ``has_key`` is
computed per robot so the frontend can show key status without a second call.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import CurrentUser, require_admin
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute, fetch
from opentrons_control.backend.app.vault import put_secret

router = APIRouter(prefix="/api")


class RobotInfo(BaseModel):
    robot_id: str
    host: str
    ssh_user: str
    agent_port: int
    key_name: str | None
    enabled: bool
    has_key: bool


class SaveRobotRequest(BaseModel):
    robot_id: str
    host: str
    ssh_user: str = "root"
    agent_port: int = 9000
    ssh_key: str = ""


@router.get("/robots", response_model=list[RobotInfo])
def list_robots(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[RobotInfo]:
    robots = fetch(db, "robots/list_all.sql")
    secret_names = {s["name"] for s in fetch(db, "secrets/list.sql")}
    return [
        RobotInfo(
            robot_id=r["robot_id"],
            host=r["host"],
            ssh_user=r["ssh_user"],
            agent_port=r["agent_port"],
            key_name=r["key_name"],
            enabled=r["enabled"],
            has_key=bool(r["key_name"]) and r["key_name"] in secret_names,
        )
        for r in robots
    ]


@router.post("/robots")
def save_robot(
    req: SaveRobotRequest,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    key_name = None
    if req.ssh_key.strip():
        key_name = f"{req.robot_id}_key"
        put_secret(db, key_name, req.ssh_key.encode(), kind="ssh_key")

    execute(
        db,
        "robots/upsert.sql",
        {
            "robot_id": req.robot_id,
            "host": req.host,
            "ssh_user": req.ssh_user,
            "agent_port": req.agent_port,
            "key_name": key_name,
        },
    )
    return {"status": "saved", "robot_id": req.robot_id}


@router.delete("/robots/{robot_id}")
def delete_robot(
    robot_id: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    execute(db, "robots/delete.sql", {"robot_id": robot_id})
    return {"status": "deleted", "robot_id": robot_id}