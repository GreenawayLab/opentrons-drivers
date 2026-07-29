"""One entry point for writing to the events (surveillance) log.

Every call opens its own short-lived session rather than borrowing the caller's.
That is deliberate: an audit record must persist independently of whatever
transaction the caller is in (we want "launch attempted" on disk even if the
launch then fails and the request rolls back), and it lets the exact same
function be used from a request handler and from a detached background task (the
run executor, the bootstrap finalizer) that has no request-scoped session.

log_event NEVER raises. A surveillance log that can break the operation it is
observing is worse than no log, so any failure is swallowed and logged to the
application logger instead of propagating.
"""

from __future__ import annotations

import json
from logging import getLogger
from typing import Any, Optional

from opentrons_control.backend.app.db.db_session import SessionLocal
from opentrons_control.backend.app.db.runner import execute_returning

logger = getLogger(__name__)


def log_event(
    *,
    kind: str,
    status: Optional[str] = None,
    source: Optional[str] = None,
    user_id: Optional[int] = None,
    actor: Optional[str] = None,
    robot_id: Optional[str] = None,
    plan_id: Optional[int] = None,
    plan_name: Optional[str] = None,
    config_id: Optional[int] = None,
    run_id: Optional[str] = None,
    session_token: Optional[str] = None,
    message: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Append one row to the events log; on any failure, log and swallow.

    :param kind: What happened (launch, ready, running, complete, failed,
        aborted, cancelled, config_saved, plan_saved, deleted, ...). Free text.
    :param status: The run/session status at the moment of the event.
    :param source: "manual" (a user via the UI) or "auto" (an external client).
        Stored in ``detail`` so no schema column is needed.
    :param user_id: The acting user's id (manual runs); None for automated.
    :param actor: Human/agent identity - the user name for manual, the caller's
        client_id for automated. Denormalised so the event survives row deletion.
    :param robot_id: Robot name the event concerns.
    :param plan_id: Pinned plan id, if any (nulled on plan deletion; plan_name
        is kept alongside so the event stays legible).
    :param plan_name: Plan name, denormalised.
    :param config_id: Pinned config id, if any.
    :param run_id: In-process run id, to stitch an event to its (ephemeral) run.
    :param session_token: Session token, for the same correlation on the auto path.
    :param message: Human-readable detail or error text.
    :param detail: Extra structured context; merged with ``source``.
    """
    payload: dict[str, Any] = dict(detail or {})
    if source is not None:
        payload.setdefault("source", source)

    session = SessionLocal()
    try:
        execute_returning(
            session,
            "events/insert.sql",
            {
                "kind": kind,
                "status": status,
                "user_id": user_id,
                "actor": actor,
                "robot_id": robot_id,
                "plan_id": plan_id,
                "plan_name": plan_name,
                "config_id": config_id,
                "run_id": run_id,
                "session_token": session_token,
                "message": message,
                "detail": json.dumps(payload),
            },
            commit=True,
        )
    except Exception:  # noqa: BLE001 - surveillance must never break the caller
        logger.exception("failed to record event kind=%s run_id=%s", kind, run_id)
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        session.close()