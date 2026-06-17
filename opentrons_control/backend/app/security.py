"""
Authentication and authorisation.

Passwords are hashed with PBKDF2-HMAC-SHA256. A session is a JWT carried in an
httpOnly cookie holding only the user id; role and name are read from the live
user row on every request, so a soft-deleted account loses access on its next
request and role changes take effect without re-login.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException, status
from jwt import DecodeError, ExpiredSignatureError, decode, encode
from sqlalchemy.orm import Session

from opentrons_control.backend.app.settings.config import settings
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import fetch_one

PBKDF2_ROUNDS = 260_000


def hash_password(plain: str) -> str:
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", plain.encode(), salt, PBKDF2_ROUNDS)
    return f"sha256${salt.hex()}${key.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    try:
        _, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", plain.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS)
    return hmac.compare_digest(actual, bytes.fromhex(hash_hex))


def create_token(user_id: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.token_expire_minutes)
    return encode({"sub": str(user_id), "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def _decode_token(token: str) -> dict | None:
    try:
        return decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except (DecodeError, ExpiredSignatureError):
        return None


class CurrentUser:
    def __init__(self, id: int, role: str, name: str):
        self.id = id
        self.role = role
        self.name = name


_ROLE_DASHBOARDS = {
    "admin": "/admin/dashboard",
    "user": "/user/dashboard",
}


def dashboard_for(role: str) -> str:
    return _ROLE_DASHBOARDS.get(role, "/login")


def _resolve_user(access_token: str | None, db: Session) -> CurrentUser | None:
    if not access_token:
        return None
    payload = _decode_token(access_token)
    if not payload:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    row = fetch_one(db, "users/get_by_id.sql", {"user_id": user_id})
    if not row:
        return None
    return CurrentUser(id=row["id"], role=row["role"], name=row["name"])


def get_current_user_redirect(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> CurrentUser:
    """For HTML routes: redirect to /login when unauthenticated."""
    user = _resolve_user(access_token, db)
    if not user:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})
    return user


def require_admin(user: CurrentUser = Depends(get_current_user_redirect)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": dashboard_for(user.role)})
    return user


def require_user(user: CurrentUser = Depends(get_current_user_redirect)) -> CurrentUser:
    if user.role != "user":
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": dashboard_for(user.role)})
    return user