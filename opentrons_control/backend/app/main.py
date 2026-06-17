"""
Backend startup harness.

Reads a JSON configuration file, resolves robot connection details into
:class:`Robot` instances, and starts the FastAPI app via uvicorn.

The harness is a reference wiring of the lib; replace or extend it as
needed for the deployment environment. The library itself (``api.py``)
does not load configuration: it accepts a fully-resolved robot mapping.

Configuration file shape::

    {
      "secrets": {
        "keys_dir": "/data/access"
      },
      "robots": {
        "ot-3": {
          "host": "10.0.0.3",
          "user": "root",
          "key_name": "ot3_id_ed25519",
          "agent_port": 9000
        }
      }
    }

Environment variables:

``BACKEND_CONFIG``
    Path to the configuration file. Defaults to ``/data/backend.json``.
``BACKEND_HOST`` / ``BACKEND_PORT``
    Bind address for uvicorn. Default to ``0.0.0.0`` and ``8000``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict

import uvicorn

from opentrons_control.backend.app.api import create_app
from opentrons_control.backend.app.robot_sessions import Robot
from opentrons_control.backend.app.settings.global_variables import DEFAULT_CONFIG_PATH
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
                logger.warning(
                    "robot %s has no key assigned; skipping", row["robot_id"]
                )
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

    config_path = Path(os.environ.get("BACKEND_CONFIG", DEFAULT_CONFIG_PATH))
    with config_path.open() as f:
        config = json.load(f)

    robots = load_robots(config)
    logger.info("loaded %d robot(s) from %s", len(robots), config_path)

    app = create_app(robots)

    uvicorn.run(
        app,
        host=os.environ.get("BACKEND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BACKEND_PORT", "8000")),
    )


if __name__ == "__main__":
    main()