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
from typing import Any, Dict

import uvicorn

from opentrons_control.backend.app.api import create_app
from opentrons_control.backend.app.sessions import Robot


logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = "/data/backend.json"


def load_robots(config: Dict[str, Any]) -> Dict[str, Robot]:
    """
    Build the :class:`Robot` mapping from a parsed configuration dict.

    Per-robot ``key_name`` entries are resolved against ``secrets.keys_dir``
    to produce absolute key paths. The library never sees raw key names.
    """
    keys_dir = Path(config["secrets"]["keys_dir"])
    robots: Dict[str, Robot] = {}

    for robot_id, entry in config["robots"].items():
        robots[robot_id] = Robot(
            id=robot_id,
            host=entry["host"],
            user=entry["user"],
            key_path=keys_dir / entry["key_name"],
            agent_port=entry.get("agent_port", 9000),
        )
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