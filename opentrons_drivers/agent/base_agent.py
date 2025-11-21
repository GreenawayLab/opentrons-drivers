from __future__ import annotations
from opentrons import protocol_api
from typing import Dict
from opentrons_drivers.common.custom_types import StaticCtx, JSONType, BaseConfig, AgentConfig
from opentrons_drivers.common.base_opentrons import Opentrons
from opentrons_drivers.common.actions import ACTION_REGISTRY
from pathlib import Path
import json
import time
import traceback

class Agent():

    def __init__(self, protocol: protocol_api.ProtocolContext, 
                 base_config: BaseConfig, 
                 agent_config: AgentConfig) -> None:
        """
        Initialize the agent with protocol context, robot config, and agent task map.

        Parameters
        ----------
        protocol : ProtocolContext
            Opentrons API object for interacting with the robot.
        
        base_config : BaseConfig
            Hardware and layout configuration for Opentrons (pipettes, labware, etc.).
        
        agent_config : AgentConfig
            Dictionary mapping filenames (triggers) to registered action names.
        """
        self._write_status("operating")
        self.agent_config = agent_config
        self.robot = Opentrons(protocol, base_config)
        self.static_ctx: StaticCtx = {
                                'core_amounts':self.robot.core_amounts,
                                'stock_amounts':self.robot.stock_amounts,
                                'pipettes':self.robot.pipettes,
                                        }

    def _invoke(self, func_name: str, ctx: StaticCtx, arg: Dict[str, JSONType]) -> bool:
        """
        Invoke a registered action function.

        Parameters
        ----------
        func_name : str
            The name of the registered function in `ACTION_REGISTRY`.

        ctx : StaticCtx
            System state including pipettes, volumes, etc.

        arg : dict[str, JSONType]
            Arguments to pass into the registered function.

        Returns
        -------
        bool
            True if the action completed successfully.

        Raises
        ------
        ValueError
            If the function name is not in the registry.
        """
        try:
            func = ACTION_REGISTRY[func_name]
        except KeyError:
            raise ValueError(f"Unknown action function '{func_name}'. Available: {list(ACTION_REGISTRY.keys())}")
        return func(ctx, arg)

    def _parse_payload(self, path: Path) -> Dict[str, JSONType]:
        """
        Load and parse a task payload from disk.

        Parameters
        ----------
        path : Path
            Path to the file containing the payload (JSON expected).

        Returns
        -------
        dict[str, JSONType]
            The parsed payload dictionary.

        Raises
        ------
        ValueError
            If the file contents are not valid JSON.
        """
        with open(path, "r") as file:
            content = file.read()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in {path}: {content}")
    
    def _write_status(self, status: str, error: Exception | None = None) -> None:
        """
        Write agent status and error trace to `postbox/status.json`.

        Parameters
        ----------
        status : str
            Current status string, e.g. "operating" or "complete".

        error : Exception | None, optional
            An error object to capture the traceback (default is None).
        """
        content = {
            "status": status,
            "error": None if error is None else traceback.format_exc()
        }
        with open(r"postbox/status.json", "w") as f:
            json.dump(content, f, indent=2)

    def monitor(self, watch_dir: str="postbox") -> None:
        """
        Start monitoring for task files and executing mapped actions.

        Monitors the `watch_dir` for any trigger files defined in the `agent_config`.
        On detection:
        - Parses payload,
        - Executes mapped action,
        - Logs success or failure in `status.json`.

        Parameters
        ----------
        watch_dir : str, optional
            Folder path to monitor for task trigger files (default is "postbox").
        """
        while True:
            for trigger, action in self.agent_config.items():
                action = str(action) #TODO: agent_config should have .items() analogy
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