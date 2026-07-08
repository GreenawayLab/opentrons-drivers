"""Draft store: unsaved editor state, one per user per kind.

A draft is a JSONB blob the deck or actions editor parks explicitly (a button,
never autosaved). It carries no validation and no versioning because a draft is
allowed to be broken. One config draft and one plan draft per user; a real save
into the versioned entity deletes the matching draft, and the user can discard
one outright. Mounted under the user router, so routes live at
``/api/user/draft/{kind}``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import CurrentUser, get_current_user
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import execute, fetch_one

router = APIRouter(prefix="/draft")

Kind = Literal["config", "plan"]


class SaveDraftRequest(BaseModel):
    """The full editor state to park. Shape is the editor's own, unvalidated."""

    content: dict[str, Any]


class DraftInfo(BaseModel):
    content: dict[str, Any]
    updated_at: Any


@router.put("/{kind}")
def save_draft(
    kind: Kind,
    req: SaveDraftRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Upsert the caller's draft for this kind. Overwrites any existing one."""
    execute(
        db,
        "drafts/upsert.sql",
        {"user_id": user.id, "kind": kind, "content": json.dumps(req.content)},
    )
    return {"status": "saved", "kind": kind}


@router.get("/{kind}", response_model=DraftInfo | None)
def get_draft(
    kind: Kind,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DraftInfo | None:
    """Return the caller's draft for this kind, or null if none is parked."""
    row = fetch_one(db, "drafts/get.sql", {"user_id": user.id, "kind": kind})
    if row is None:
        return None
    return DraftInfo(content=row["content"], updated_at=row["updated_at"])


@router.delete("/{kind}")
def delete_draft(
    kind: Kind,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Discard the caller's draft for this kind (also called on a real save)."""
    execute(db, "drafts/delete.sql", {"user_id": user.id, "kind": kind})
    return {"status": "deleted", "kind": kind}