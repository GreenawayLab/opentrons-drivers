"""Action-plan API: versioned, owned protocol plans pinned to a config version.

Mirrors the deck-config shape (list mine/others, get, versions, save, delete)
with the same semver machinery: a save auto-classifies its bump against the
family head, and editing a non-head version, a peer's plan, or saving under a
new name forks a new family with a heritage snapshot. A plan pins one config
version by id and stores its ordered steps as an opaque JSONB blob (the same
shape the actions editor serializes). Mounted under the user router, so routes
live at ``/api/user/plans/*``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import (
    CurrentUser,
    get_current_user,
    has_permission,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import (
    execute,
    execute_returning,
    fetch,
    fetch_one,
)
from opentrons_control.backend.app.versioning import bump, classify_plan_change
from opentrons_control.backend.app.protocol_model import BaseConfig
from opentrons_control.backend.app.generator import plan_to_protocol
from opentrons_control.backend.app.simulator import simulate

router = APIRouter(prefix="/plans")


# -------------------- models --------------------


class PlanSummary(BaseModel):
    id: int
    name: str
    major: int
    minor: int
    patch: int
    config_id: int
    created_at: Any
    owner_name: str | None = None
    origin_owner_name: str | None = None
    origin_name: str | None = None
    origin_major: int | None = None
    origin_minor: int | None = None
    origin_patch: int | None = None


class PlanDetail(BaseModel):
    id: int
    owner: int
    owner_name: str
    name: str
    major: int
    minor: int
    patch: int
    config_id: int
    steps: list[dict[str, Any]]
    origin_owner_name: str | None
    origin_name: str | None
    origin_major: int | None
    origin_minor: int | None
    origin_patch: int | None


class VersionInfo(BaseModel):
    id: int
    major: int
    minor: int
    patch: int
    created_at: Any


class SavePlanRequest(BaseModel):
    name: str
    config_id: int
    steps: list[dict[str, Any]]
    base_id: int | None = None


class CheckPlanRequest(BaseModel):
    config_id: int
    steps: list[dict[str, Any]]


# -------------------- helpers --------------------


def _gate(user: CurrentUser, db: Session, permission: str) -> None:
    """Allow admins unconditionally, otherwise require the named permission."""
    if user.role != "admin" and not has_permission(db, user.id, permission):
        raise HTTPException(status_code=403, detail=f"you lack the '{permission}' permission")


def _owned_or_admin(db: Session, plan_id: int, user: CurrentUser) -> dict[str, Any]:
    """Fetch a plan, 404 if unknown, 403 unless the caller owns it or is admin."""
    row = fetch_one(db, "action_plans/get.sql", {"id": plan_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown plan {plan_id}")
    if row["owner"] != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="you can only delete your own plans")
    return row


# -------------------- list / get --------------------


@router.get("/mine", response_model=list[PlanSummary])
def list_my_plans(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PlanSummary]:
    """List the latest version of each of the caller's own plan families."""
    return [PlanSummary(**r) for r in fetch(db, "action_plans/list_mine.sql", {"owner": user.id})]


@router.get("/others", response_model=list[PlanSummary])
def list_other_plans(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PlanSummary]:
    """List the latest version of every other user's plan families."""
    return [PlanSummary(**r) for r in fetch(db, "action_plans/list_others.sql", {"owner": user.id})]


@router.post("/check")
def check_plan(
    req: CheckPlanRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Dry-run the plan's steps against its pinned config.

    Expands the steps into the atomic transfer stream (resolving fill_to), runs
    the simulator, and returns a three-state verdict: incomplete (some volume is
    unset), invalid (an overflow, stock shortfall, or non-positive amount), or
    valid. Errors and warnings are flat lists of human-readable messages.
    """
    row = fetch_one(db, "deck_configs/get.sql", {"id": req.config_id})
    if row is None:
        raise HTTPException(status_code=400, detail=f"unknown config {req.config_id}")
    config = BaseConfig.model_validate(row["config"])
    protocol, incomplete = plan_to_protocol(config, req.steps)
    report = simulate(protocol)

    def _describe(ref: list) -> str:
        return ref[0] if len(ref) == 1 else f"{ref[0]}/{ref[1]}"

    # a per-transfer trace: what each atomic step does, and where it first breaks.
    # Once a transfer fails, later ones usually fail for the same reason (a stock
    # stays exhausted), so we surface the first failure and how far the run got
    # rather than repeating the consequence on every remaining transfer.
    trace: list[dict[str, Any]] = []
    first_error: dict[str, Any] | None = None
    for i, (step, verdict) in enumerate(zip(protocol.steps, report.verdicts)):
        payload = step.payload
        desc = f"{_describe(payload['source'])} -> {_describe(payload['receiver'])}"
        amount = payload.get("amount")
        err = verdict.errors[0] if verdict.errors else None
        entry: dict[str, Any] = {"n": i + 1, "desc": desc, "amount": amount, "ok": err is None}
        if err:
            entry["error"] = err
            if first_error is None:
                first_error = {"n": i + 1, "desc": desc, "amount": amount, "message": err}
        trace.append(entry)

    fail_count = sum(1 for e in trace if not e["ok"])
    status = "incomplete" if incomplete else ("valid" if report.ok else "invalid")
    return {
        "status": status,
        "total": len(protocol.steps),
        "trace": trace,
        "first_error": first_error,
        "fail_count": fail_count,
        "incomplete": incomplete,
    }


@router.get("/{plan_id}", response_model=PlanDetail)
def get_plan(
    plan_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlanDetail:
    """Load one plan version. Any user, so a peer's plan can be adopted."""
    row = fetch_one(db, "action_plans/get.sql", {"id": plan_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown plan {plan_id}")
    return PlanDetail(
        id=row["id"], owner=row["owner"], owner_name=row["owner_name"],
        name=row["name"], major=row["major"], minor=row["minor"], patch=row["patch"],
        config_id=row["config_id"], steps=row["steps"],
        origin_owner_name=row["origin_owner_name"], origin_name=row["origin_name"],
        origin_major=row["origin_major"], origin_minor=row["origin_minor"], origin_patch=row["origin_patch"],
    )


@router.get("/{plan_id}/versions", response_model=list[VersionInfo])
def plan_versions(
    plan_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VersionInfo]:
    """List every version in the family the given plan belongs to."""
    return [VersionInfo(**r) for r in fetch(db, "action_plans/versions.sql", {"id": plan_id})]


# -------------------- save --------------------


@router.post("/")
def save_plan(
    req: SavePlanRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save a plan version.

    Requires add_plan (admins bypass). base_id is the version the edit derives
    from. Editing the family head bumps the semver (axis auto-classified by
    diffing the steps). Editing a non-head version, a peer's plan, or saving
    under a new name forks a new family with a heritage snapshot. A blank new
    plan (no base_id) starts a fresh family at 1.0.0. The pinned config_id must
    reference an existing config version.
    """
    _gate(user, db, "add_plan")

    # the pinned config must exist (the plan borrows its plates and substances)
    if fetch_one(db, "deck_configs/get.sql", {"id": req.config_id}) is None:
        raise HTTPException(status_code=400, detail=f"unknown config {req.config_id} to pin")

    head = fetch_one(db, "action_plans/latest_for_family.sql", {"owner": user.id, "name": req.name})

    if req.base_id is None:
        if head is not None:
            raise HTTPException(
                status_code=409,
                detail=f"you already own a plan named '{req.name}', load it to version or pick a new name",
            )
        version = (1, 0, 0)
        origin = (None, None, None, None, None)
    else:
        base = fetch_one(db, "action_plans/get.sql", {"id": req.base_id})
        if base is None:
            raise HTTPException(status_code=404, detail=f"unknown base plan {req.base_id}")
        editing_head = (
            head is not None and base["id"] == head["id"]
            and base["owner"] == user.id and base["name"] == req.name
        )
        if editing_head:
            axis = classify_plan_change(base["steps"], req.steps)
            if axis is None:
                raise HTTPException(status_code=409, detail="no changes to save")
            version = bump((head["major"], head["minor"], head["patch"]), axis)
            origin = (
                head["origin_owner_name"], head["origin_name"],
                head["origin_major"], head["origin_minor"], head["origin_patch"],
            )
        else:
            if head is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"you already own a plan named '{req.name}', rename to fork it",
                )
            version = (1, 0, 0)
            origin = (base["owner_name"], base["name"], base["major"], base["minor"], base["patch"])

    row = execute_returning(
        db,
        "action_plans/insert.sql",
        {
            "owner": user.id, "name": req.name,
            "major": version[0], "minor": version[1], "patch": version[2],
            "config_id": req.config_id, "steps": json.dumps(req.steps),
            "origin_owner_name": origin[0], "origin_name": origin[1],
            "origin_major": origin[2], "origin_minor": origin[3], "origin_patch": origin[4],
        },
    )
    return {"status": "saved", "id": row["id"] if row else None, "name": req.name,
            "version": f"{version[0]}.{version[1]}.{version[2]}"}


# -------------------- delete --------------------


@router.delete("/{plan_id}")
def delete_plan_version(
    plan_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete one plan version (owner or admin)."""
    _owned_or_admin(db, plan_id, user)
    execute(db, "action_plans/delete_version.sql", {"id": plan_id})
    return {"status": "deleted", "id": plan_id}


@router.delete("/{plan_id}/family")
def delete_plan_family(
    plan_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete every version of the plan's family (owner or admin)."""
    row = _owned_or_admin(db, plan_id, user)
    execute(db, "action_plans/delete_family.sql", {"id": plan_id})
    return {"status": "deleted family", "name": row["name"]}