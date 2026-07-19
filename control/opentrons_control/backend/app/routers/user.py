"""User-domain API aggregator.

Groups everything the user-facing surface calls under one ``/api/user`` prefix,
mirroring the frontend's ``/user/*`` pages. Feature areas live in their own
modules and mount here as sub-routers; ``deck`` (labware library + deck
configs) is the first. Protocols and run history will mount the same way.

Gating stays per-endpoint in each sub-router rather than blanket at this
level, because the surface is not uniformly user-only: a few operations (e.g.
pruning shared labware) are admin-gated exceptions living beside the
user-gated majority.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from opentrons_control.backend.app.security import CurrentUser, get_current_user
from opentrons_control.backend.app.db.db_session import get_db
from opentrons_control.backend.app.db.runner import fetch

from opentrons_control.backend.app.routers import deck, draft, method, plan

router = APIRouter(prefix="/api/user")
router.include_router(deck.router)
router.include_router(draft.router)
router.include_router(plan.router)
router.include_router(method.router)


@router.get("/robots")
def list_robots_for_user(
    user: CurrentUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Robot ids a user may target for a run, without any connection details."""
    return [{"robot_id": r["robot_id"], "enabled": r["enabled"]} for r in fetch(db, "robots/list_all.sql")]