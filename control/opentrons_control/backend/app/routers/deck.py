"""Deck-config authoring API.

Mounted as a sub-router of the user router, so these routes live under
``/api/user/deck/*`` (labware and configs). Two resources back the
manual-protocol setup UI:

* **labware** — opentrons labware-definition JSON, stored once and referenced
  by filename from a config's ``PlateInfo.type``. The launcher stages these
  into the agent's ``plates/`` bucket, so a config is only runnable if every
  custom (``.json``) plate it names exists here.
* **deck_configs** — a saved ``BaseConfig`` (pipettes, core and stock plates)
  authored in the UI. The one object used for both agent launch and the
  simulation check; stored whole so there is no second structure to drift.

Referential integrity is enforced at both ends: a config cannot be saved
while it names custom labware absent from the library, and labware cannot be
deleted while a saved config still references it.

Gating: any signed-in user may read/author labware and configs. Deleting
labware is admin-only (users add to the shared library but never prune it).
Uploads may replace an existing definition — a deliberate choice — but every
write records who did it and when (``created_by`` is the original author,
``updated_by``/``updated_at`` the last writer), so a replace is traceable
rather than silent.

Config ownership is not yet enforced: any user can update or delete any
config by id. ``created_by`` is recorded as the hook for the ownership rules
that land with user/group management.
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
    has_permission,
)
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import (
    execute,
    execute_returning,
    fetch,
    fetch_one,
)
from opentrons_control.backend.app.protocol_model import (
    BaseConfig,
    custom_labware_refs,
    labware_wells,
)

router = APIRouter(prefix="/deck")


# -------------------- wire models --------------------


class LabwareSummary(BaseModel):
    """A labware library entry for listing."""

    name: str
    well_count: int
    created_at: Any


class LabwareDetail(BaseModel):
    """A labware entry with its full definition and derived well list."""

    name: str
    definition: dict[str, Any]
    wells: list[str]


class SaveLabwareRequest(BaseModel):
    """Upload payload for a labware definition.

    :param name: Filename used as the reference in ``PlateInfo.type``.
    :param definition: The parsed labware-definition JSON.
    """

    name: str
    definition: dict[str, Any]

class ConfigSummary(BaseModel):
    id: int
    name: str
    version: int
    created_at: Any
    owner_name: str | None = None
    origin_owner_name: str | None = None
    origin_name: str | None = None
    origin_version: int | None = None


class ConfigDetail(BaseModel):
    id: int
    owner: int
    owner_name: str
    name: str
    version: int
    config: BaseConfig
    origin_owner_name: str | None
    origin_name: str | None
    origin_version: int | None


class ConfigOrigin(BaseModel):
    owner_name: str
    name: str
    version: int


class VersionInfo(BaseModel):
    id: int
    version: int
    created_at: Any


class SaveConfigRequest(BaseModel):
    name: str
    config: BaseConfig
    origin: ConfigOrigin | None = None


class StandardUnitInfo(BaseModel):
    name: str
    category: str
    created_by_name: str | None


class AddStandardUnitRequest(BaseModel):
    name: str
    category: Literal["module", "pipette"]


def _gate(user: CurrentUser, db: Session, permission: str) -> None:
    """Allow admins unconditionally, otherwise require the named permission."""
    if user.role != "admin" and not has_permission(db, user.id, permission):
        raise HTTPException(status_code=403, detail=f"you lack the '{permission}' permission")


# -------------------- labware --------------------


@router.get("/labware", response_model=list[LabwareSummary])
def list_labware(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[LabwareSummary]:
    """List the labware library."""
    return [
        LabwareSummary(
            name=r["name"], well_count=r["well_count"], created_at=r["created_at"]
        )
        for r in fetch(db, "labware/list.sql")
    ]


@router.get("/labware/{name}", response_model=LabwareDetail)
def get_labware(
    name: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LabwareDetail:
    """Return one labware definition and its well labels."""
    row = fetch_one(db, "labware/get.sql", {"name": name})
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown labware '{name}'")
    definition = row["definition"]
    return LabwareDetail(name=row["name"], definition=definition, wells=labware_wells(definition))


@router.post("/labware")
def save_labware(
    req: SaveLabwareRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Validate and store (or replace) a labware definition.

    Requires the add_labware permission (admins bypass).

    A replace is allowed; the write is stamped with the actor and time so the
    trace of who changed a shared definition is never lost.
    """
    _gate(user, db, "add_labware")
    if not req.name.endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="labware name must end with .json (it is referenced as a filename)",
        )
    try:
        labware_wells(req.definition)  # shape check
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    execute(
        db,
        "labware/upsert.sql",
        {"name": req.name, "definition": json.dumps(req.definition), "actor": user.id},
    )
    return {"status": "saved", "name": req.name}


# Deleting labware is an admin activity and lives in the admin router
# (DELETE /api/labware/{name}); users add to the shared library but never prune it.


# -------------------- standard units --------------------


@router.get("/standard-units", response_model=list[StandardUnitInfo])
def list_standard_units(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[StandardUnitInfo]:
    """List standard modules and pipette strings for the deck pickers."""
    return [StandardUnitInfo(**r) for r in fetch(db, "standard_units/list.sql")]


@router.post("/standard-units")
def add_standard_unit(
    req: AddStandardUnitRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    """Add a standard module or pipette string. Requires add_labware (admins bypass)."""
    _gate(user, db, "add_labware")
    if fetch_one(db, "standard_units/get.sql", {"name": req.name}) is not None:
        raise HTTPException(status_code=409, detail=f"'{req.name}' already exists")
    execute(
        db,
        "standard_units/insert.sql",
        {"name": req.name, "category": req.category, "created_by": user.id},
    )
    return {"status": "added", "name": req.name}


# -------------------- deck configs (versioned, owned) --------------------


@router.get("/configs/mine", response_model=list[ConfigSummary])
def list_my_configs(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ConfigSummary]:
    """List the latest version of each of the caller's own config families."""
    return [ConfigSummary(**r) for r in fetch(db, "deck_configs/list_mine.sql", {"owner": user.id})]


@router.get("/configs/others", response_model=list[ConfigSummary])
def list_other_configs(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ConfigSummary]:
    """List the latest version of every other user's config families."""
    return [ConfigSummary(**r) for r in fetch(db, "deck_configs/list_others.sql", {"owner": user.id})]


@router.get("/configs/{config_id}", response_model=ConfigDetail)
def get_config(
    config_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ConfigDetail:
    """Load one config version. Any user, so a peer's config can be adopted."""
    row = fetch_one(db, "deck_configs/get.sql", {"id": config_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown config {config_id}")
    return ConfigDetail(
        id=row["id"], owner=row["owner"], owner_name=row["owner_name"],
        name=row["name"], version=row["version"],
        config=BaseConfig.model_validate(row["config"]),
        origin_owner_name=row["origin_owner_name"],
        origin_name=row["origin_name"], origin_version=row["origin_version"],
    )


@router.get("/configs/{config_id}/versions", response_model=list[VersionInfo])
def config_versions(
    config_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[VersionInfo]:
    """List every version in the family the given config belongs to."""
    return [VersionInfo(**r) for r in fetch(db, "deck_configs/versions.sql", {"id": config_id})]


@router.post("/configs")
def save_config(
    req: SaveConfigRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Save a config as a new version.

    Requires add_config (admins bypass). Every save writes a new row. If the
    caller already has a family with this name the version bumps and the family
    origin is preserved. Otherwise it is version 1, carrying the fork origin if
    one was passed (which the caller sets only when adopting a peer's config).
    """
    _gate(user, db, "add_config")
    available = {r["name"] for r in fetch(db, "labware/list.sql")}
    missing = custom_labware_refs(req.config) - available
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"config references unknown labware: {', '.join(sorted(missing))}",
        )

    # well-existence: every filled well must belong to the labware definition
    for plates in (req.config.core_plates, req.config.stock_plates):
        for pname, plate in plates.items():
            if not plate.content or not plate.type.endswith(".json"):
                continue
            lw = fetch_one(db, "labware/get.sql", {"name": plate.type})
            if lw is None:
                continue
            try:
                def_wells = set(labware_wells(lw["definition"]))
            except ValueError:
                continue
            bad = sorted(set(plate.content) - def_wells)
            if bad:
                raise HTTPException(
                    status_code=422,
                    detail=f"plate '{pname}' uses wells not in {plate.type}: {', '.join(bad)}",
                )

    latest = fetch_one(db, "deck_configs/latest_for_family.sql", {"owner": user.id, "name": req.name})
    if latest is not None:
        if req.origin is not None:
            raise HTTPException(
                status_code=409,
                detail=f"you already own a config named '{req.name}', rename this adoption",
            )
        version = latest["version"] + 1
        origin = (latest["origin_owner_name"], latest["origin_name"], latest["origin_version"])
    else:
        version = 1
        origin = (
            (req.origin.owner_name, req.origin.name, req.origin.version)
            if req.origin is not None
            else (None, None, None)
        )

    row = execute_returning(
        db,
        "deck_configs/insert.sql",
        {
            "owner": user.id, "name": req.name, "version": version,
            "config": req.config.model_dump_json(),
            "origin_owner_name": origin[0], "origin_name": origin[1], "origin_version": origin[2],
        },
    )
    return {"status": "saved", "id": row["id"] if row else None, "name": req.name, "version": version}


def _owned_or_admin(db: Session, config_id: int, user: CurrentUser) -> dict[str, Any]:
    """Fetch a config, 404 if unknown, 403 unless the caller owns it or is admin."""
    row = fetch_one(db, "deck_configs/get.sql", {"id": config_id})
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown config {config_id}")
    if row["owner"] != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="you can only delete your own configs")
    return row


@router.delete("/configs/{config_id}")
def delete_config_version(
    config_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete one version (owner or admin). Forks keep their heritage snapshot."""
    _owned_or_admin(db, config_id, user)
    execute(db, "deck_configs/delete_version.sql", {"id": config_id})
    return {"status": "deleted", "id": config_id}


@router.delete("/configs/{config_id}/family")
def delete_config_family(
    config_id: int,
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Delete every version of the config's family (owner or admin)."""
    row = _owned_or_admin(db, config_id, user)
    execute(db, "deck_configs/delete_family.sql", {"owner": row["owner"], "name": row["name"]})
    return {"status": "deleted family", "name": row["name"]}