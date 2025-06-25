from opentrons import protocol_api
from collections import defaultdict
import json
from pprint import pprint

class Opentrons:
    """TODO: add docstring."""

    def __init__(self, protocol: protocol_api.ProtocolContext, base_config: dict[str, str]) -> None:
        """TODO: add docstring."""
        self.protocol = protocol
        self.base_config = base_config
        # TODO: type hint for these attributes
        self.core_plates = {}  # Plates where substances are mixed
        self.support_plates = []  # Tipracks
        self.max_volumes = {}
        self.stock_amounts = defaultdict(list)  # Stock well information
        self.core_amounts = defaultdict(list)  # Core well information

        with open("error.txt", "w+") as error_file:
            error_file.write("None")

        with open("error.txt", "w+") as status_file:
            status_file.write("operating")

        with open("where.txt", "w+") as file:
            file.write(" \n")
            file.write(" ")

        # Set gantry speeds
        for ax in ["X", "Y", "Z"]:
            self.protocol.max_speeds[ax] = 350

        # Load core plates (returns plates + fills core_amounts)
        self.load_assigned_plates("core_assigned.json", is_stock=False)

        # Load stock plates (ONLY fills stock_amounts, no plate objects)
        self.load_assigned_plates("stock_assigned.json", is_stock=True)

        self.scan_pipette = protocol.load_instrument("p300_single_gen2", mount="left")

    def load_assigned_plates(self, filename: str, is_stock: bool) -> None:
        """Load assigned plates from JSON, ensuring missing well values are filled.

        Resulting dict is like { 'sub_0': [{'position':object of opentron plates, 'amount': 5000}],
                                 'sub_1': [{'position':object of opentron plates, 'amount': 4999}] }
        """
        # TODO: refactor this method to simplify the logic
        with open(filename) as assigned_file:
            assigned_data = json.load(assigned_file)

        for plate_name, plate_info in assigned_data.items():
            if plate_name.startswith("tiprack_"):
                plate = self.protocol.load_labware(plate_info["type"], location=plate_info["place"])
                plate.set_offset(x=0.7, y=1.4, z=0.2)
                self.support_plates.append(plate)
                continue

            # Load labware definition
            with open(plate_info["type"]) as labware_file:
                labware_def = json.load(labware_file)

            plate = self.protocol.load_labware_from_definition(labware_def=labware_def, location=plate_info["place"])

            if not is_stock:
                plate.set_offset(x=-1.0, y=3.5, z=0.0)

            # Get max volume for core plates
            if not is_stock and "max_volume" in plate_info:
                self.max_volumes[plate_name] = plate_info["max_volume"]

            # Ensure all wells have substance and amount values
            well_defaults = {well: {"substance": {"initial": None}, "amount": 0} for well in labware_def["wells"]}
            if plate_info.get("content"):
                for well, well_data in plate_info["content"].items():
                    well_defaults[well]["amount"] = well_data["amount"]
                    well_defaults[well]["substance"] = {"initial": well_data["substance"]}

            # Store well data
            if is_stock:
                for well, data in well_defaults.items():
                    self.stock_amounts[data["substance"]["initial"]].append(
                        {"position": plate[well], "amount": data["amount"]}
                    )
            else:
                self.core_amounts[plate_name] = well_defaults  # Stores plate data
                self.core_plates[plate_name] = plate  # Stores plate objects

    def stock_validation(self, what: str, amt: float) -> None:
        """This function checks that if stocks are addressed we have a sufficient amount of whatever is needed."""
        min_vol = 200
        approved = False

        while not approved:
            try:
                substance = self.stock_amounts[what][0]
            except:  # NOQA  # TODO: specify the exception type
                pprint(self.stock_amounts)
                raise RuntimeError(f"Substance {what} not found in deck.")

            if amt > (substance["amount"] - min_vol):
                print(
                    f"Amount of {what} needed is greater than the amount in the well.\n"
                    f"Well {substance['position']} is now out of scope. \n"
                    f"Trying again by moving to the next well containing {what}."
                )
                # Change the well plate to move from
                self.stock_amounts[what].pop(0)
                if len(self.stock_amounts[what]) == 0:
                    self.protocol.home()
                    raise RuntimeError(f"No more {what} left on the deck.")
            else:
                approved = True