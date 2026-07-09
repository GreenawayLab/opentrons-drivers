"""Liquid-handling method registry.

Methods map to driver functions in the drivers repo, so which ones manual users
may select is an admin decision after reviewing the code. Listing is open to any
authenticated user (the actions editor needs it); adding and removing are
admin-only. basic_liquid_transfer is the seeded default and cannot be removed.

A method's params is the hyperparameter spec the admin publishes: a list of
{name, units?, min?, max?} that the frontend turns into fillable fields when the
method is selected. Mounted under the user router, so routes live at
``/api/user/methods``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import (
    CurrentUser,
    get_current_user,
    require_admin,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute, fetch

router = APIRouter(prefix="/methods")

DEFAULT_METHOD = "basic_liquid_transfer"


class Param(BaseModel):
    name: str
    dtype: Literal["int", "float", "bool"] = "float"
    units: str | None = None
    min: float | None = None
    max: float | None = None


class MethodInfo(BaseModel):
    name: str
    params: list[Param]


class SaveMethodRequest(BaseModel):
    name: str
    params: list[Param] = []


@router.get("", response_model=list[MethodInfo])
def list_methods(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MethodInfo]:
    """List the methods a manual user may select, with their hyperparameter specs."""
    return [MethodInfo(name=r["name"], params=r["params"]) for r in fetch(db, "methods/list.sql", {})]


@router.post("")
def save_method(
    req: SaveMethodRequest,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Publish or update a method's hyperparameter spec (admin only)."""
    execute(
        db,
        "methods/upsert.sql",
        {"name": req.name, "params": json.dumps([p.model_dump() for p in req.params]), "created_by": user.id},
    )
    return {"status": "saved", "name": req.name}


@router.delete("/{name}")
def delete_method(
    name: str,
    user: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Remove a method (admin only). The default method cannot be removed."""
    if name == DEFAULT_METHOD:
        raise HTTPException(status_code=400, detail="the default method cannot be removed")
    execute(db, "methods/delete.sql", {"name": name})
    return {"status": "deleted", "name": name}