"""
Authentication API.

Credentials are validated here and a session cookie is minted on success. The
cookie carries an httpOnly JWT and travels back to the browser unchanged
through the frontend and proxy, so it has no Domain attribute and scopes to the
edge origin the browser actually talks to.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import (
    CurrentUser,
    create_token,
    get_current_user,
    verify_password,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import fetch_one

router = APIRouter(prefix="/api/auth")


class LoginRequest(BaseModel):
    name: str
    password: str


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


@router.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie("access_token")
    return {"status": "logged out"}


@router.get("/me", response_model=Identity)
def me(user: CurrentUser = Depends(get_current_user)) -> Identity:
    return Identity(name=user.name, role=user.role)