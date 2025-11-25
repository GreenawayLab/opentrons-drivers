from __future__ import annotations
from opentrons import protocol_api
from typing import Dict
from opentrons_drivers.common.custom_types import StaticCtx, JSONType, BaseConfig, AgentConfig
from opentrons_drivers.common.base_opentrons import Opentrons
from pathlib import Path
import json
import time
import traceback

class BasicProtocol():

    def __init__(self, protocol: protocol_api.ProtocolContext, 
                 base_config: BaseConfig) -> None:
        """
        Initialize the simple protocol with protocol context and robot configuration.
        On contrast to Agent, this class sequntially executes commands without monitoring a folder.

        Parameters
        ----------
        protocol : ProtocolContext
            Opentrons API object for interacting with the robot.
        
        base_config : BaseConfig
            Hardware and layout configuration for Opentrons (pipettes, labware, etc.).
        """
        self.robot = Opentrons(protocol, base_config)
        self.static_ctx: StaticCtx = {
                                'core_amounts': self.robot.core_amounts,
                                'stock_amounts': self.robot.stock_amounts,
                                'pipettes': self.robot.pipettes,
                                'system_state': {}
                                     }