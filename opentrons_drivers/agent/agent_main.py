from opentrons import protocol_api
from opentrons_drivers.agent.base_agent import Agent
from pathlib import Path
import json

with open(Path('postbox', 'task.json')) as f:
    task = json.load(f)
    to_do = Path('protocols', task['who'], task['protocol'])
    with open(Path(to_do, 'base_config.json')) as bc_file:
        base_config = json.load(bc_file)
    with open(Path(to_do, 'agent_config.json')) as ac_file:
        agent_config = json.load(ac_file)

def run(protocol: protocol_api.ProtocolContext) -> None:
    """Function triggered by the systemd unit opentrons_execute"""
    ot = Agent(protocol=protocol, 
               base_config=base_config,
               agent_config=agent_config)
    
    ot.monitor()