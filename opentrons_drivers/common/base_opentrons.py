from opentrons import protocol_api
from collections import defaultdict
import json
from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.protocol_api.labware import Labware
from typing import Dict, List, cast
from opentrons_drivers.common.custom_types import StaticCtx, JSONType
from opentrons_drivers.common.actions import ACTION_REGISTRY
from opentrons_drivers.common.custom_types import StockWell, CoreWell, BaseConfig, PlateInfo
from pathlib import Path


class Opentrons:
    """
    Unified Opentrons driver responsible for:
    - Loading labware (core plates, stock plates, tipracks)
    - Applying deck offsets
    - Building volume/substance bookkeeping tables
    - Loading pipettes and attaching tipracks
    """

    def __init__(self, protocol: protocol_api.ProtocolContext,
                 base_config: BaseConfig) -> None:
        """
        Create all labware, offsets, pipettes, and internal volume dictionaries
        as described in the supplied BaseConfig.

        Parameters
        ----------
        protocol : ProtocolContext
            Active Opentrons protocol context.
        base_config : BaseConfig
            Declarative configuration describing all labware, pipettes,
            offsets and initial well contents.
        """
        self.protocol = protocol
        self.base_config = base_config

        # Labware containers
        self.core_plates: Dict[str, Labware] = {}
        self.stock_plates: Dict[str, Labware] = {}
        self.support_plates: List[Labware] = []  # tipracks and any support labware

        # Internal bookkeeping
        self.core_amounts: Dict[str, Dict[str, CoreWell]] = defaultdict(dict)
        self.stock_amounts: Dict[str, List[StockWell]] = defaultdict(list)

        # Pipette references
        self.pipettes: Dict[str, InstrumentContext] = {}

        # Load all labware from BaseConfig
        self._init_assigned_plates("core_plates", is_stock=False)
        self._init_assigned_plates("stock_plates", is_stock=True)

        # Build well volume tables
        self._build_amounts_dicts("core_plates", is_stock=False)
        self._build_amounts_dicts("stock_plates", is_stock=True)

        # Load pipettes (all tipracks are already inside support_plates)
        self._init_pipettes()

    # ----------------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------------

    def set_offsets(self, offsets: Dict[str, Dict[str, float]]) -> None:
        """
        Apply updated deck offsets to existing labware objects.

        Parameters
        ----------
        offsets : Dict[str, Dict[str, float]]
            Mapping: plate_name → {"x": float, "y": float, "z": float}
        """
        for plate_name, off in offsets.items():
            if plate_name in self.core_plates:
                plate = self.core_plates[plate_name]
            elif plate_name in self.stock_plates:
                plate = self.stock_plates[plate_name]
            else:
                # Tipracks live inside support_plates, but we cannot address by name:
                # the user must specify tiprack offsets in BaseConfig only.
                continue

            plate.set_offset(
                x=off.get("x", 0.0),
                y=off.get("y", 0.0),
                z=off.get("z", 0.0),
            )

    def invoke(self, func_name: str, ctx: StaticCtx, arg: Dict[str, JSONType]) -> bool:
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

    # ----------------------------------------------------------------------
    # Internal: Labware creation
    # ----------------------------------------------------------------------

    def _init_assigned_plates(self, name: str, is_stock: bool) -> None:
        """
        Load all labware defined under `base_config[name]` and apply offsets.

        This includes:
        - Custom core plates (from custom JSON)
        - Custom stock plates (from custom JSON)
        - Tipracks (either built-in or custom JSON)
        """
        assigned = cast(Dict[str, PlateInfo], self.base_config.get(name, {}))

        for plate_name, plate_info in assigned.items():
            labware_type = plate_info["type"]
            deck_slot = plate_info["place"]
            offset = plate_info.get("offset", {"x": 0, "y": 0, "z": 0})

            # Tipracks typically use standard names but may use custom definitions
            if plate_name.startswith("tiprack_"):
                if labware_type.endswith(".json"):
                    with open(Path("plates", labware_type)) as f:
                        lw_def = json.load(f)
                    plate = self.protocol.load_labware_from_definition(
                        lw_def, deck_slot
                    )
                else:
                    plate = self.protocol.load_labware(
                        labware_name=labware_type, location=deck_slot
                    )

                plate.set_offset(
                    x=offset.get("x", 0.0),
                    y=offset.get("y", 0.0),
                    z=offset.get("z", 0.0),
                )
                self.support_plates.append(plate)
                continue

            # Core / stock plates always use custom JSON
            with open(Path("plates", labware_type)) as f:
                lw_def = json.load(f)

            plate = self.protocol.load_labware_from_definition(
                lw_def, deck_slot
            )

            plate.set_offset(
                x=offset.get("x", 0.0),
                y=offset.get("y", 0.0),
                z=offset.get("z", 0.0),
            )

            if is_stock:
                self.stock_plates[plate_name] = plate
            else:
                self.core_plates[plate_name] = plate

    # ----------------------------------------------------------------------
    # Internal: Pipettes
    # ----------------------------------------------------------------------

    def _init_pipettes(self) -> None:
        """
        Load pipettes as defined in BaseConfig and attach loaded tipracks.
        """
        pip_cfg = self.base_config.get("pipettes", {})
        for mount, info in pip_cfg.items():
            model = info["model"]
            unit = self.protocol.load_instrument(
                model,
                mount=mount,
                tip_racks=self.support_plates if self.support_plates else None,
            )
            unit.swelled = None  # required for compatibility with actions
            unit.max_volume = unit.max_volume * 0.8 # type: ignore[attr-defined]
            self.pipettes[mount] = unit

    # ----------------------------------------------------------------------
    # Internal: Bookkeeping dictionaries
    # ----------------------------------------------------------------------

    def _build_amounts_dicts(self, name: str, is_stock: bool) -> None:
        """
        Create `core_amounts` or `stock_amounts` dictionaries based on content
        definitions in BaseConfig.

        Core plates create one CoreWell per well.
        Stock plates group wells by initial substance name.
        """
        assigned = cast(Dict[str, PlateInfo], self.base_config.get(name, {}))

        for plate_name, plate_info in assigned.items():
            # Skip tipracks
            if plate_name.startswith("tiprack_"):
                continue

            with open(Path("plates", plate_info["type"])) as f:
                wells_def = json.load(f)["wells"]
            well_names = list(wells_def.keys())

            content = plate_info.get("content", {})

            # ------------------ Stock plates ------------------
            if is_stock:
                plate = self.stock_plates[plate_name]
                for well in well_names:
                    init = content.get(well, None)
                    if init is None:
                        continue

                    sub = init["substance"]
                    vol = init["volume"]

                    entry: StockWell = {
                        "position": plate[well],
                        "volume": float(vol),
                    }
                    self.stock_amounts[sub].append(entry)
                continue

            # ------------------ Core plates ------------------
            plate = self.core_plates[plate_name]
            max_volume = plate_info["max_volume"]

            well_structs: Dict[str, CoreWell] = {}

            for well in well_names:
                init = content.get(well, None)
                sub = init["substance"] if init else None
                vol = init["volume"] if init else 0.0

                well_structs[well] = {
                    "position": plate[well],
                    "volume": float(vol),
                    "substance": {"initial": sub},
                    "max_volume": max_volume,
                }

            self.core_amounts[plate_name] = well_structs
