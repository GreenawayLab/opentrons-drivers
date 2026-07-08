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

from fastapi import APIRouter

from opentrons_control.backend.app.routers import deck, draft

router = APIRouter(prefix="/api/user")
router.include_router(deck.router)
router.include_router(draft.router)