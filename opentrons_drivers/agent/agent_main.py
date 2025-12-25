from opentrons import protocol_api
from opentrons_drivers.agent.base_agent import Agent
from pathlib import Path
import json

metadata = {
    "protocolName": "ot_agent",
    "author": "Aleksandr Ostudin",
    "description": "Activate OT based on query msg",
    "apiLevel": "2.13",
}

with open(Path('postbox', 'base_config.json')) as bc_file:
    base_config = json.load(bc_file)
with open(Path('postbox', 'agent_config.json')) as ac_file:
    agent_config = json.load(ac_file)

def run(protocol: protocol_api.ProtocolContext) -> None:
    """Function triggered by the systemd unit opentrons_execute"""
    ot = Agent(protocol=protocol, 
               base_config=base_config,
               agent_config=agent_config)
    
    ot.monitor()