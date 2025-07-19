from opentrons import protocol_api
from opentrons_drivers.agents.base_agent import Agent

def run(protocol: protocol_api.ProtocolContext) -> None:
    """TODO: add docstring summary line."""
    ot = Agent(protocol=protocol)
    ot.monitor()