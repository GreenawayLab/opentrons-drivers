from opentrons import protocol_api
from collections import defaultdict
import json
from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Labware
from typing import Dict, List, cast
from opentrons_drivers.common.custom_types import StockWell, CoreWell, BaseConfig, PlateInfo
from pathlib import Path


class Opentrons:
    """BaseRobot class that stores state and hardware of the machine"""

    def __init__(self, protocol: protocol_api.ProtocolContext, 
                 base_config: BaseConfig) -> None:
        """
        Initialize the robot with base configuration of the hardware and layout.

        Parameters
        ----------
        protocol : ProtocolContext
            Opentrons API object for interacting with the robot.
        
        base_config : BaseConfig
            Hardware and layout configuration for Opentrons (pipettes, labware, etc.).
        """
        self.protocol = protocol
        self.base_config = base_config

        self.core_plates: Dict[str, Labware] = {}  # Plates where substances are mixed
        self.support_plates: List[Labware] = []  # Tipracks, etc.
        self.stock_amounts: Dict[str, List[StockWell]] = defaultdict(list)  # Stock well information
        self.core_amounts: Dict[str, Dict[str, CoreWell]] = defaultdict(dict)  # Core well information
        self.pipettes: Dict[str, InstrumentContext] = {}  # Pipette objects

        # Set gantry speeds
        for ax in ["X", "Y", "Z"]:
            self.protocol.max_speeds[ax] = base_config.get(f"gantry_speed_{ax}", 400)

        # Load core plates (returns plates + fills core_amounts)
        self._load_assigned_plates("core_plates", is_stock=False)

        # Load stock plates (ONLY fills stock_amounts, no plate objects)
        self._load_assigned_plates("stock_plates", is_stock=True)

        # Load pipettes
        pipettes = self.base_config["pipettes"]
        for mount, pipette in pipettes.items():
            unit = protocol.load_instrument(pipette['model'], mount=mount)
            unit.swelled = None # type: ignore[attr-defined]
            self.pipettes[mount] = unit

    def _load_assigned_plates(self, name: str, is_stock: bool) -> None:
        """
        Load plates from configuration and update system state.

        This method loads either:
        - **core plates**, which are represented in both `core_plates` and `core_amounts`,
        - or **stock plates**, which populate the `stock_amounts` dictionary.

        All plates are defined in the base config under the given `name`, 
        and are expected to reference either a labware type string (stock plates)
        or a custom definition file (core plates).

        Resulting Structures
        ---------------------
        * self.core_amounts:
            Dict[plate_name, Dict[well_name, CoreWell]]
            Each well contains:
                • position: Well object
                • volume: float
                • substance: {"initial": Optional[str]}
                • max_volume: float

        * self.stock_amounts:
            Dict[substance_name, List[StockWell]]
            Each StockWell contains:
                • position: Well object
                • volume: float

        Parameters
        -----------
        name : str
            The key in `self.base_config` pointing to the plate assignment dictionary.

        is_stock : bool
            Whether to treat the plates as stock (True) or core (False).
            This affects both how the plate is parsed and where it is stored.
        """
        # TODO: refactor this method to simplify the logic
        assigned_data = cast(Dict[str, PlateInfo], self.base_config.get(name, {}))

        for plate_name, plate_info in assigned_data.items():
            offset = plate_info.get("offset", {})
            if plate_name.startswith("tiprack_"):
                plate = self.protocol.load_labware(plate_info["type"], location=plate_info["place"])
                plate.set_offset(x=offset.get('x', 0), y=offset.get('y', 0), z=offset.get('z', 0))
                self.support_plates.append(plate)
                continue

            # Load labware definition
            with open(Path('plates', plate_info["type"])) as labware_file:
                labware_def = json.load(labware_file)

            plate = self.protocol.load_labware_from_definition(labware_def=labware_def, location=plate_info["place"])
            plate.set_offset(x=offset.get('x', 0), y=offset.get('y', 0), z=offset.get('z', 0))

            # Ensure all wells have substance and amount values
            well_defaults: Dict[str, CoreWell] = {
                                well: {
                                    "substance": {"initial": None},
                                    "volume": 0,
                                    **({"position": plate[well], "max_volume": plate_info["max_volume"]} if not is_stock else {})
                                }
                                for well in labware_def["wells"]
                            }
            if plate_info.get("content"):
                for well, well_data in plate_info["content"].items():
                    well_defaults[well]["volume"] = well_data["volume"]
                    well_defaults[well]["substance"] = {"initial": well_data["substance"]}

                    if not is_stock:
                        well_defaults[well]["max_volume"] = plate_info["max_volume"]
                        well_defaults[well]["position"] = plate[well]

            # Store well data
            if is_stock:
                for well, data in well_defaults.items():
                    key = cast(str, data["substance"]["initial"])
                    self.stock_amounts[key].append(
                        {"position": plate[well], "volume": data["volume"]}
                    )
            else:
                self.core_amounts[plate_name] = well_defaults  
                self.core_plates[plate_name] = plate  

    