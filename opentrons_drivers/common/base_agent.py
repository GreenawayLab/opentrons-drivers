from opentrons_drivers.common.base_opentrons import Opentrons
from inspect import signature
from pathlib import Path
import json
import time

class BaseAgent(Opentrons):
    """Base class for agents that extends Opentrons functionality."""

    def __init__(self, protocol, base_config, agent_config):
        """Initialize the BaseAgent with a protocol context and base configuration."""
        super().__init__(protocol, base_config)
        self._null_status()
        self.agent_config = agent_config
        self.static_ctx = dict(
                                core_plates=self.core_plates,
                                core_amounts=self.core_amounts,
                                stock_amounts=self.stock_amounts,
                                pipettes=self.pipettes,
                              )

    def _invoke(self, func, **context):
        needed = signature(func).parameters
        result = func(**{k: v for k, v in context.items() if k in needed})
        return result

    def _parse_payload(self, path: Path):
        """Parse the payload from a file and return it as a dictionary."""
        with open(path, "r") as file:
            content = file.read()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            raise ValueError(f"Invalid JSON in {path}: {content}")
    
    def _null_status(self) -> None:

        with open("error.txt", "w+") as error_file:
            error_file.write("None")

        with open("status.txt", "w+") as status_file:
            status_file.write("operating")

    def _set_status(self, status: bool) -> None:
        """Set the status of the agent."""
        with open("status.txt", "w+") as status_file:
            if status:
                status = "operating"
            else:
                status = "completed"
            status_file.write(status)

    def monitor(self, watch_dir="."):

        while True:
            for trigger, action in self.agent_config():
                fp = Path(watch_dir, trigger)
                if fp.exists():
                    self._set_status(True)
                    payload = self._parse_payload(fp)          
                    fp.unlink()                             
                    ctx = {**self.static_ctx, **payload}
                    result = self._invoke(action, **ctx)
                    if result:
                        self._set_status(False)

            time.sleep(1)