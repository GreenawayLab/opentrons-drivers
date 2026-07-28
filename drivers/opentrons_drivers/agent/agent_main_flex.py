"""
Flex entry point for the Opentrons HTTP agent.

opentrons parses ``requirements`` statically (``ast.literal_eval``), so
``robotType`` has to be a plain literal in the file ``opentrons_execute``
runs - it cannot be computed at import (a function call like
``os.environ.get(...)`` raises MalformedPythonProtocolError). This is the
Flex-literal twin of ``agent_main.py``: identical boot behaviour, only the
literal ``robotType`` differs. The launcher points ``opentrons_execute`` at
this file when the config's ``robot_type`` is ``"Flex"``.

All boot logic lives once in ``agent_main.run()``; this file only carries the
literal ``metadata`` / ``requirements`` opentrons reads statically, plus a thin
``run`` that delegates.
"""

from opentrons import protocol_api

from opentrons_drivers.agent.agent_main import run as _run

metadata = {
    "protocolName": "ot_agent_flex",
    "author": "Aleksandr Ostudin",
    "description": "Activate Flex based on HTTP requests",
}

requirements = {
    "robotType": "Flex",
    "apiLevel": "2.24",
}


def run(protocol: protocol_api.ProtocolContext) -> None:
    """Delegate to the shared agent boot in :func:`agent_main.run`."""
    _run(protocol)