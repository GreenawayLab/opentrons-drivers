"""
Entry point for the Opentrons HTTP agent.

This file is what opentrons_execute hands a ProtocolContext to. It loads
the hardware config, instantiates the Agent (which boots hardware and
starts the HTTP server), and hands control to Agent.serve(), which blocks
forever processing one job at a time on this thread.

Any exception escaping this scope is treated as a fatal crash: we
best-effort write a "crashed" status record to disk so external observers
(e.g. someone SSH-ing in) can tell the difference between "agent process
not running" and "agent process running but mid-boot".
"""

from opentrons import protocol_api
from opentrons_drivers.agent.base_agent import Agent
from pathlib import Path
import json
import traceback

metadata = {
    "protocolName": "ot_agent",
    "author": "Aleksandr Ostudin",
    "description": "Activate OT based on HTTP requests",
    "apiLevel": "2.24",
}


def _write_crash(exc: BaseException) -> None:
    """Best-effort write of a crash record to status.json."""
    try:
        Path("postbox").mkdir(parents=True, exist_ok=True)
        with open("postbox/status.json", "w") as f:
            json.dump(
                {"status": "crashed", "error": traceback.format_exc()},
                f,
                indent=2,
            )
    except OSError:
        pass


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Function triggered by the systemd unit opentrons_execute."""
    try:
        # Config load is inside run() so that a missing or malformed
        # base_config.json (FileNotFoundError, JSONDecodeError) gets
        # caught by the crash handler below instead of dying silently
        # at import time before any status can be written.
        with open(Path("postbox", "base_config.json")) as bc_file:
            base_config = json.load(bc_file)

        ot = Agent(protocol=protocol, base_config=base_config)
        # serve() never returns under normal operation; it runs the
        # job-execution loop on this thread for the lifetime of the agent.
        ot.serve()
    except BaseException as e:  # noqa: BLE001
        _write_crash(e)
        raise