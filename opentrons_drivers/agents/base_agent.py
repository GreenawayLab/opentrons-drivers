from __future__ import annotations
from opentrons import protocol_api
from typing import Dict
from opentrons_drivers.common.custom_types import StaticCtx, JSONType
from opentrons_drivers.common.base_opentrons import Opentrons
from opentrons_drivers.common.actions import ACTION_REGISTRY
from pathlib import Path
import json
import time
import traceback

class Agent():
    """Base class for agents that extends Opentrons functionality."""

    def __init__(self, protocol: protocol_api.ProtocolContext, 
                 base_config: Dict[str, str], 
                 agent_config: Dict[str, str]):
        """Initialize the BaseAgent with a protocol context and base configuration."""
        self._write_status("operating")
        self.agent_config = agent_config
        self.robot = Opentrons(protocol, base_config)
        self.static_ctx: StaticCtx = dict(
                                core_amounts=self.robot.core_amounts,
                                stock_amounts=self.robot.stock_amounts,
                                pipettes=self.robot.pipettes,
                                         )

    def _invoke(self, func_name: str, ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
        """Invoke a registered action function with the given context and arguments."""
        try:
            func = ACTION_REGISTRY[func_name]
        except KeyError:
            raise ValueError(f"Unknown action function '{func_name}'. Available: {list(ACTION_REGISTRY.keys())}")
        return func(ctx=ctx, arg=arg)

    def _parse_payload(self, path: Path):
        """Parse the payload from a file and return it as a dictionary."""
        with open(path, "r") as file:
            content = file.read()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in {path}: {content}")
    
    def _write_status(self, status: str, error: Exception | None = None) -> None:
        """Writes agent status and potential error trace to a JSON file."""
        content = {
            "status": status,
            "error": None if error is None else traceback.format_exc()
        }
        with open("status.json", "w") as f:
            json.dump(content, f, indent=2)

    def monitor(self, watch_dir: str="."):
        """Launch the server-like behaviour of the agent"""
        while True:
            for trigger, action in self.agent_config():
                fp = Path(watch_dir, trigger)
                if fp.exists():
                    self._write_status("operating") 
                    try:
                        payload = self._parse_payload(fp)
                        fp.unlink()
                        result = self._invoke(action, self.static_ctx, payload)
                        if result:
                            self._write_status("complete") 
                    except Exception as e:
                        self._write_status("operating", error=e) 
            time.sleep(1)