"""
Admin API.

Robots: returns and mutates robot configuration as JSON. A pasted SSH key is
stored encrypted under ``<robot_id>_key`` and linked to the robot; a blank key
on save leaves any existing link untouched (handled by the upsert). ``has_key``
is computed per robot so the frontend can show key status without a second call.

Git token: a write-only setter and a boolean status for the single access token
the maintainer uses to fetch the drivers source (never returns the token).

Accounts: admin-only user management (list, role, deactivate, password reset)
and single-use invite codes for onboarding. Account *creation* is not here — new
users self-register by redeeming an invite (routers/auth.py); admins only issue
codes, and codes only ever mint the 'user' role (an admin code would be a
shareable bearer credential; admins are made by promoting a user). Labware
pruning also lives here: users add to the shared library, admins delete from it.

Guards: an admin cannot deactivate their own account or demote/deactivate the
last active admin, so the box can never be locked out of itself.
"""

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import CurrentUser, hash_password, require_admin, VALID_PERMISSIONS
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute, fetch, fetch_one
from opentrons_control.backend.app.vault import put_secret
from opentrons_control.backend.app.protocol_model import BaseConfig, custom_labware_refs
import opentrons_control.backend.app.settings.global_variables as gv

router = APIRouter(prefix="/api")

MIN_PASSWORD_LEN = 8
INVITE_TTL_DAYS = 14


# -------------------- models: robots + git token --------------------


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


class GitTokenStatus(BaseModel):
    set: bool


class SetGitTokenRequest(BaseModel):
    token: str


# -------------------- models: accounts --------------------


class UserSummary(BaseModel):
    id: int
    name: str
    role: str
    active: bool
    created_at: Any


class SetPasswordRequest(BaseModel):
    password: str


class InviteInfo(BaseModel):
    code: str
    target_role: str
    created_at: Any
    used_at: Any
    created_by_name: str | None
    used_by_name: str | None


# -------------------- robots --------------------


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


# -------------------- git token --------------------


@router.get("/git-token", response_model=GitTokenStatus)
def git_token_status(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> GitTokenStatus:
    """Report whether the git token is configured. Never returns the token."""
    names = {s["name"] for s in fetch(db, "secrets/list.sql")}
    return GitTokenStatus(set=gv.GIT_TOKEN_SECRET in names)


@router.post("/git-token")
def set_git_token(
    req: SetGitTokenRequest,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Set or replace the git access token (write-only)."""
    token = req.token.strip()
    if not token:
        raise HTTPException(status_code=400, detail="empty token")
    put_secret(db, gv.GIT_TOKEN_SECRET, token.encode(), kind="git_token")
    return {"status": "saved"}


# -------------------- labware (admin-only delete) --------------------


@router.delete("/labware/{name}")
def delete_labware(
    name: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Delete labware, refusing while a saved config still references it."""
    users_of = [
        c["name"]
        for c in fetch(db, "deck_configs/all_with_config.sql")
        if name in custom_labware_refs(BaseConfig.model_validate(c["config"]))
    ]
    if users_of:
        raise HTTPException(
            status_code=409,
            detail=f"labware '{name}' is in use by config(s): {', '.join(users_of)}",
        )
    execute(db, "labware/delete.sql", {"name": name})
    return {"status": "deleted", "name": name}


# -------------------- accounts: management --------------------


def _active_admin_count(db: Session) -> int:
    """Return the number of admins that are not soft-deleted."""
    row = fetch_one(db, "users/count_active_admins.sql")
    return row["n"] if row else 0


def _active_target(db: Session, user_id: int) -> dict[str, Any]:
    """Fetch a user by id, 404 if unknown or already inactive."""
    row = fetch_one(db, "users/get_full.sql", {"user_id": user_id})
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="no such active user")
    return row


@router.get("/users", response_model=list[UserSummary])
def list_users(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[UserSummary]:
    """List all accounts, active first."""
    return [
        UserSummary(
            id=r["id"],
            name=r["name"],
            role=r["role"],
            active=r["deleted_at"] is None,
            created_at=r["created_at"],
        )
        for r in fetch(db, "users/list.sql")
    ]


@router.delete("/users/{user_id}")
def deactivate_user(
    user_id: int,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Soft-delete a user; cannot remove yourself or the last active admin."""
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="cannot deactivate your own account")
    target = _active_target(db, user_id)
    if target["role"] == "admin" and _active_admin_count(db) <= 1:
        raise HTTPException(status_code=409, detail="cannot deactivate the last active admin")
    execute(db, "users/soft_delete.sql", {"user_id": user_id})
    return {"status": "deactivated", "id": user_id}


@router.post("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    req: SetPasswordRequest,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Set a new password for a user."""
    if len(req.password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"password must be at least {MIN_PASSWORD_LEN} characters",
        )
    _active_target(db, user_id)
    execute(
        db,
        "users/set_password.sql",
        {"user_id": user_id, "password_hash": hash_password(req.password)},
    )
    return {"status": "password reset", "id": user_id}


# -------------------- accounts: invites --------------------


@router.post("/invites")
def issue_invite(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Issue a single-use invite for a new 'user' account."""
    code = secrets.token_urlsafe(12)
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)
    execute(
        db,
        "invites/create.sql",
        {"code": code, "target_role": "user", "created_by": user.id, "expires_at": expires_at},
    )
    return {"status": "issued", "code": code, "target_role": "user"}


@router.get("/invites", response_model=list[InviteInfo])
def list_invites(
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[InviteInfo]:
    """List all invites, newest first, with issuer and redeemer names."""
    return [InviteInfo(**r) for r in fetch(db, "invites/list.sql")]


@router.delete("/invites/{code}")
def revoke_invite(
    code: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Revoke an unused invite (used invites are kept as a record)."""
    execute(db, "invites/revoke.sql", {"code": code})
    return {"status": "revoked", "code": code}


# -------------------- standard units (admin delete) --------------------


@router.delete("/standard-units/{name}")
def delete_standard_unit(
    name: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Delete a standard module or pipette string (admin only)."""
    execute(db, "standard_units/delete.sql", {"name": name})
    return {"status": "deleted", "name": name}


# -------------------- permissions --------------------


class GrantPermissionRequest(BaseModel):
    permission: str


@router.get("/users/{user_id}/permissions")
def list_user_permissions(
    user_id: int,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[str]:
    """List a user's granted permissions."""
    _active_target(db, user_id)
    return [r["permission"] for r in fetch(db, "permissions/list_for_user.sql", {"user_id": user_id})]


@router.post("/users/{user_id}/permissions")
def grant_permission(
    user_id: int,
    req: GrantPermissionRequest,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Grant a permission to a non-admin user."""
    if req.permission not in VALID_PERMISSIONS:
        raise HTTPException(status_code=400, detail=f"unknown permission '{req.permission}'")
    target = _active_target(db, user_id)
    if target["role"] == "admin":
        raise HTTPException(status_code=400, detail="admins already have every permission")
    execute(db, "permissions/grant.sql", {"user_id": user_id, "permission": req.permission})
    return {"status": "granted", "id": user_id, "permission": req.permission}


@router.delete("/users/{user_id}/permissions/{permission}")
def revoke_permission(
    user_id: int,
    permission: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Revoke a permission from a user."""
    execute(db, "permissions/revoke.sql", {"user_id": user_id, "permission": permission})
    return {"status": "revoked", "id": user_id, "permission": permission}