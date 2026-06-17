"""
Backend startup.

Builds the robot registry from the database and serves the FastAPI app.
"""

from __future__ import annotations

import logging
import os
from typing import Dict

import uvicorn

from opentrons_control.backend.app.api import create_app
from opentrons_control.backend.app.robot_sessions import Robot
from opentrons_control.backend.app.vault import materialize_key
from opentrons_control.backend.app.db.db_session import SessionLocal
from opentrons_control.backend.app.db.runner import fetch

logger = logging.getLogger(__name__)


def load_robots() -> Dict[str, Robot]:
    """Read enabled robots from the database and resolve their SSH keys."""
    robots: Dict[str, Robot] = {}
    db = SessionLocal()
    try:
        for row in fetch(db, "robots/list_enabled.sql"):
            key_name = row["key_name"]
            if not key_name:
                logger.warning("robot %s has no key assigned; skipping", row["robot_id"])
                continue
            robots[row["robot_id"]] = Robot(
                id=row["robot_id"],
                host=row["host"],
                user=row["ssh_user"],
                key_path=materialize_key(db, key_name),
                agent_port=row["agent_port"],
            )
    finally:
        db.close()
    logger.info("loaded %d robot(s) from database", len(robots))
    return robots


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(load_robots())
    uvicorn.run(
        app,
        host=os.environ.get("BACKEND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BACKEND_PORT", "8000")),
    )


if __name__ == "__main__":
    main()