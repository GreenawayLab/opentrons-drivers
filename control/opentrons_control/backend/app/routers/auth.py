"""
Authentication API.

Credentials are validated here and a session cookie is minted on success. The
cookie carries an httpOnly JWT and travels back to the browser unchanged
through the frontend and proxy, so it has no Domain attribute and scopes to the
edge origin the browser actually talks to.

Registration redeems a single-use invite: it creates the account the invite
authorises and signs the user in the same way login does (id-only token). The
user insert and invite consume run in one transaction, and the consume is
atomic (UPDATE ... WHERE used_by IS NULL RETURNING), so a race for the same
code has exactly one winner and rolls the loser's insert back.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import (
    CurrentUser,
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute_returning, fetch_one

router = APIRouter(prefix="/api/auth")

MIN_PASSWORD_LEN = 8


class LoginRequest(BaseModel):
    name: str
    password: str


class RegisterRequest(BaseModel):
    name: str
    password: str
    code: str


class Identity(BaseModel):
    name: str
    role: str


@router.post("/login", response_model=Identity)
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)) -> Identity:
    user = fetch_one(db, "users/get_by_name.sql", {"name": req.name})
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid username or password")
    token = create_token(user["id"])
    response.set_cookie("access_token", token, httponly=True, samesite="lax")
    return Identity(name=user["name"], role=user["role"])


@router.post("/register", response_model=Identity)
def register(req: RegisterRequest, response: Response, db: Session = Depends(get_db)) -> Identity:
    """Create an account from an invite code and start its session."""
    invite = fetch_one(db, "invites/get_unused.sql", {"code": req.code})
    if invite is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid or already-used invite code")
    if len(req.password) < MIN_PASSWORD_LEN:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"password must be at least {MIN_PASSWORD_LEN} characters",
        )
    if fetch_one(db, "users/get_by_name.sql", {"name": req.name}) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"the name '{req.name}' is taken")

    user = execute_returning(
        db,
        "users/insert.sql",
        {"name": req.name, "role": invite["target_role"], "password_hash": hash_password(req.password)},
        commit=False,
    )
    consumed = execute_returning(
        db, "invites/consume.sql", {"code": req.code, "user_id": user["id"]}, commit=False
    )
    if not consumed:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="that code was just used; ask for a new one")
    db.commit()

    token = create_token(user["id"])
    response.set_cookie("access_token", token, httponly=True, samesite="lax")
    return Identity(name=req.name, role=invite["target_role"])


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie("access_token")
    return {"status": "logged out"}


@router.get("/me", response_model=Identity)
def me(user: CurrentUser = Depends(get_current_user)) -> Identity:
    return Identity(name=user.name, role=user.role)