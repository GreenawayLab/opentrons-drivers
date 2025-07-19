from opentrons import protocol_api
from collections import defaultdict
import json
from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Labware
from typing import Dict, List
from opentrons_drivers.common.custom_types import StockWell, CoreWell


class Opentrons:
    """BaseRobot class that stores state and hardware of the machine"""

    def __init__(self, protocol: protocol_api.ProtocolContext, 
                 base_config: Dict[str, str]) -> None:
        """Initialize the BaseRobot with a protocol context and base configuration."""
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
        self._load_assigned_plates("core_assigned.json", is_stock=False)

        # Load stock plates (ONLY fills stock_amounts, no plate objects)
        self._load_assigned_plates("stock_assigned.json", is_stock=True)

        # Load pipettes
        pipettes = self.base_config["pipettes"]
        for mount, pipette in pipettes.items():
            unit = protocol.load_instrument(pipette['name'], mount=mount)
            unit.swelled = None 
            unit.max_volume = pipette.get('max_volume', 1000)
            unit.min_volume = unit.max_volume * 0.1  # Default to 10% of max volume
            self.pipettes[mount] = unit

    def _load_assigned_plates(self, filename: str, is_stock: bool) -> None:
        """Load assigned plates from JSON, ensuring missing well values are filled.

        Resulting dict is like { 'sub_0': [{'position':object of opentron plates, 'amount': 5000}],
                                 'sub_1': [{'position':object of opentron plates, 'amount': 4999}] }
        """
        # TODO: refactor this method to simplify the logic
        with open(filename) as assigned_file:
            assigned_data = json.load(assigned_file)

        for plate_name, plate_info in assigned_data.items():
            offset = plate_info.get("offset", {})
            if plate_name.startswith("tiprack_"):
                plate = self.protocol.load_labware(plate_info["type"], location=plate_info["place"])
                plate.set_offset(x=offset.get('x', 0), y=offset.get('y', 0), z=offset.get('z', 0))
                self.support_plates.append(plate)
                continue

            # Load labware definition
            with open(plate_info["type"]) as labware_file:
                labware_def = json.load(labware_file)

            plate = self.protocol.load_labware_from_definition(labware_def=labware_def, location=plate_info["place"])
            plate.set_offset(x=offset.get('x', 0), y=offset.get('y', 0), z=offset.get('z', 0))

            # Ensure all wells have substance and amount values
            well_defaults = {
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
                    self.stock_amounts[data["substance"]["initial"]].append(
                        {"position": plate[well], "volume": data["volume"]}
                    )
            else:
                self.core_amounts[plate_name] = well_defaults  
                self.core_plates[plate_name] = plate  

    