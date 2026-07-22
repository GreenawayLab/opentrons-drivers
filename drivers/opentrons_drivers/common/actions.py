import time
from typing import cast
from opentrons_drivers.common.custom_types import ActionFn
from opentrons_drivers.common.methods import LIQUID_METHODS
from opentrons_drivers.common.custom_types import CoreWell, StaticCtx, JSONType
from opentrons.protocol_api.labware import Well
from opentrons.types import Point, Location
from opentrons.protocol_api.instrument_context import InstrumentContext
import opentrons_drivers.common.helpers as help

#---------- Registries of possible functions ----------

ACTION_REGISTRY: dict[str, ActionFn] = {}  # is exported
register_action = help.make_registry_decorator(ACTION_REGISTRY)

"""
    All exportable functions must have the same base signature:
    ctx: StaticCtx, arg: dict[str, JSONType]

    ctx contains the system state: plates, amounts of liquids, pipettes, etc.
    arg is an argument to the function: what to do with this action.

    It is a function's responsibility to unwrap the ctx and the arg.

    All exportable functions must return True upon completion.

"""

@register_action("transfer_execution")
def transfer_execution(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """
    Perform a liquid transfer and update bookkeeping.

    Modes:
        1. Stock → Core:
            - source == ["substance_name"]
            - Validates stock volume, updates `stock_amounts` and `core_amounts`.

        2. Core → Core:
            - source == ["core_plate", "well_label"]
            - Validates both wells, updates `core_amounts`.

    Parameters:
        ctx (StaticCtx): Device state (pipettes, volumes, plates).
        arg (dict[str, JSONType]): Instruction arguments.
            - source: list[str]
            - receiver: list[str]
            - amount: float
            - method: str = "_liquid_transfer"
            - pipette_mount: str = "left"
            - swell_time: float = 0.0
            - swell_cycle: int = 1
            - tip_cycle: tuple[bool, bool] = [True, True]
            - ...plus any method-specific kwargs

    Returns:
        bool: True upon successful transfer.
    """

    # shared hardware and tables
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    core_amounts = ctx["core_amounts"]
    stock_amounts = ctx["stock_amounts"]

    # strongly typed payload 
    source   = cast(list[str],  arg["source"])
    receiver = cast(list[str],  arg["receiver"])
    amount   = cast(float,      arg["amount"])

    method = cast(str, arg.get("method", "liquid_transfer"))
    tips_raw = arg.get("tip_cycle", (True, True))

    if not (
        isinstance(tips_raw, (list, tuple))
        and len(tips_raw) == 2
        and all(isinstance(x, bool) for x in tips_raw)
    ):
        raise ValueError("tip_cycle must be a tuple/list of two booleans")

    tip_on, tip_off = tips_raw

    swell_time = cast(float, arg.get("swell_time", 0.0))
    swell_cycle = cast(int, arg.get("swell_cycle", 1))

    # extra kwargs for specialised methods
    extra = {
    k: v for k, v in arg.items()
    if k not in {
        "source", "receiver", "amount", "method",
        "pipette_mount", "swell_time", "swell_cycle", "tip_cycle"
                }
            }

    # get low-level transfer function
    transfer_fn = LIQUID_METHODS.get(method)
    if transfer_fn is None:
        raise ValueError(f"Unknown transfer method '{method}'. "
                         f"Available: {list(LIQUID_METHODS)}")

    # prep tip
    if tip_on:
        pipette.pick_up_tip()

    # receiver objects
    recv_data: CoreWell = core_amounts[receiver[0]][receiver[1]]
    recv_well: Well     = recv_data["position"]
    help.well_validation(core_amounts, receiver, amount, "receiver")

    # STOCK → CORE 
    if len(source) == 1:
        sub_name = source[0]

        help.stock_validation(stock_amounts, sub_name, amount, pipette.min_volume)

        stock_entry = stock_amounts[sub_name][0]
        stock_well: Well = stock_entry["position"]

        if swell_time > 0: # passive swell
            help.swell_tip(pipette, stock_amounts, core_amounts, [sub_name], seconds=swell_time)

        if swell_cycle > 1: # active swell
            help.swell_tip(pipette, stock_amounts, core_amounts, [sub_name], cycles=swell_cycle)

        transfer_fn(pipette=pipette,
                    to=recv_well,
                    fr=stock_well,
                    amount=amount,
                    **extra)

        # bookkeeping
        stock_entry["volume"]   -= amount
        recv_data["volume"]     += amount
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        recv_data["substance"][timestamp] = (sub_name, stock_well.well_name, amount)

    # CORE → CORE 
    elif len(source) == 2:
        src_data: CoreWell = core_amounts[source[0]][source[1]]
        src_well: Well     = src_data["position"]

        help.well_validation(core_amounts, source,   amount, "source")

        if swell_time > 0: # passive swell
            help.swell_tip(pipette, stock_amounts, core_amounts, source, seconds=swell_time)

        if swell_cycle > 1: # active swell
            help.swell_tip(pipette, stock_amounts, core_amounts, source, cycles=swell_cycle)

        transfer_fn(pipette=pipette,
                    to=recv_well,
                    fr=src_well,
                    amount=amount,
                    **extra)

        # bookkeeping
        src_data["volume"]  -= amount
        recv_data["volume"] += amount
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        recv_data["substance"][timestamp] = (source[0], source[1], amount)
        src_data["substance"][timestamp]  = (receiver[0], receiver[1], -amount)

    else:
        raise ValueError("`source` must be ['substance'] or ['plate', 'well'].")

    if tip_off:
        pipette.drop_tip()

    state = ctx["system_state"]
    # receiver always defines the new "location"
    state["plate"] = receiver[0]
    state["well"] = receiver[1]
    state["last_action"] = "transfer"
    state["timestamp"] = time.time()

    return True

@register_action("sampler_action")
def sampler_action(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    pip = ctx["pipettes"][arg.get("sampler_mount", "left")]
    state = ctx["system_state"]

    mode = arg.get("mode")
    if mode not in {"scan", "wash", "lift"}:
        raise ValueError(f"Unknown sampler mode: {mode}")

    # ---------- Helper: safe lift ----------
    def safe_lift():
        plate = state.get("plate")
        well = state.get("well")
        mode_prev = state.get("last_action")

        if plate is None or well is None:
        # try to find the first available core position
            core = ctx["core_amounts"]
            if core:
                first_plate = next(iter(core.keys()))
                first_well = next(iter(core[first_plate].keys()))
                pos = core[first_plate][first_well]["position"]
                pip.move_to(pos.top(50))
            return

        if mode_prev == "wash":
            wash = ctx["stock_amounts"]["wash_solv"][0]["position"]
            pip.move_to(wash.top(50))
            return
        else:
            pos = ctx["core_amounts"][plate][well]["position"]
            pip.move_to(pos.top(50))
            return

    # ---------- LIFT ----------
    if mode == "lift":
        safe_lift()

        state["last_action"] = "lift"
        state["timestamp"] = time.time()
        return True

    # ---------- WASH ----------
    elif mode == "wash":
        amount = float(arg["amount"])
        wells = ctx["stock_amounts"]["wash_solv"]

        if wells[0]["volume"] < amount:
            raise RuntimeError("Wash solvent insufficient")

        safe_lift()
        pos = wells[0]["position"]
        pip.move_to(pos.top(30))

        wells[0]["volume"] -= amount

        state["plate"] = None
        state["well"]  = None
        state["last_action"] = "wash"
        state["last_args"] = arg
        state["timestamp"] = time.time()

        return True

    # ---------- SCAN ----------
    elif mode == "scan":
        plate = cast(str, arg["plate"])
        well  = cast(str, arg["well"])

        safe_lift()

        pos = ctx["core_amounts"][plate][well]["position"]
        pip.move_to(pos.top(30))

        state["plate"] = plate
        state["well"]  = well
        state["last_action"] = "scan"
        state["last_args"] = arg
        state["timestamp"] = time.time()

        return True

@register_action("test_action")
def test_action(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """
    Smoke test: move pipette to deck-safe coordinates and back, then home.
    
    Proves: HTTP → slot → protocol thread → Opentrons API → motors path
    is alive end-to-end. Does NOT touch tips, wells, or any labware.

    Payload (all optional, keyword-only):
        pipette_mount : "left" | "right"  (default "left")
        x, y, z       : float             (default 200, 150, 150 — deck-safe)
        dx            : float             (default 20.0 — visible nudge)
        skip_home     : bool              (default False — set True to leave
                                           the pipette at the final move
                                           position, e.g. for chained tests)
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))

    pipettes = ctx["pipettes"]
    if pipette_mount not in pipettes:
        raise RuntimeError(
            f"No pipette mounted on '{pipette_mount}'. "
            f"Available mounts: {sorted(pipettes.keys())}"
        )
    pipette: InstrumentContext = pipettes[pipette_mount]

    x = float(arg.get("x", 200.0))
    y = float(arg.get("y", 150.0))
    z = float(arg.get("z", 50.0))
    dx = float(arg.get("dx", 20.0))
    skip_home = bool(arg.get("skip_home", False))

    pipette.move_to(Location(Point(x=x,      y=y, z=z), None))
    pipette.move_to(Location(Point(x=x + dx, y=y, z=z), None))

    if not skip_home:
        pipette.home()

    return True

# ---------- Calibration and setup actions ----------

@register_action("reset_tipracks")
def reset_tipracks(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Return any held tip and reset tip tracking so a protocol starts full.

    Calibration picks up a tip to check the tip position over a well. This
    returns that tip to its slot (return_tip, so the rack stays physically full)
    and then resets tip tracking, so the run begins from tip position one with
    the rack both physically and logically full.

    Parameters:
        arg["pipette_mount"] (str): defaults to "left".
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    if pipette.has_tip:
        pipette.return_tip()
    pipette.reset_tipracks()
    return True


@register_action("set_offset")
def set_offset(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Apply a deck offset to one specific plate, overwriting its previous one.

    Offsets belong to the labware: set_offset replaces rather than accumulates,
    so the calibration UI sends the full current value for that plate each time
    and re-moves to see the effect with no drift.

    Parameters:
        arg["plate"] (str): the core or stock plate name.
        arg["x"], arg["y"], arg["z"] (float): offset in millimetres, default 0.
    """
    plate_name = cast(str, arg["plate"])
    robot = ctx["robot"]
    plate = robot.core_plates.get(plate_name) or robot.stock_plates.get(plate_name)
    if plate is None:
        raise ValueError(f"unknown plate '{plate_name}' for calibration")
    plate.set_offset(
        x=cast(float, arg.get("x", 0.0)),
        y=cast(float, arg.get("y", 0.0)),
        z=cast(float, arg.get("z", 0.0)),
    )
    return True


@register_action("calibration_tiprack")
def calibration_tiprack(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Try to pick up a tip from a tiprack then return it, to check its offset.

    The pickup itself is the test: if the pipette misaligns with the rack the
    tip will not seat cleanly. The user watches, adjusts the rack offset with
    set_tiprack_offset, and retries until pickup is clean. Any previously held
    tip is returned first.

    Parameters:
        arg["rack_index"] (int): index into the loaded tip racks, default 0.
        arg["well"] (str): which tip to try, default "A1". A central well is a
            representative check.
        arg["pipette_mount"] (str): defaults to "left".
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    rack_index = cast(int, arg.get("rack_index", 0))
    well_label = cast(str, arg.get("well", "A1"))

    robot = ctx["robot"]
    racks = robot.support_plates
    if rack_index >= len(racks):
        raise ValueError(f"no tiprack at index {rack_index} (have {len(racks)})")
    rack = racks[rack_index]
    if pipette.has_tip:
        pipette.return_tip()
    pipette.pick_up_tip(rack[well_label])
    pipette.return_tip()
    return True


@register_action("set_tiprack_offset")
def set_tiprack_offset(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Apply a deck offset to one tiprack, overwriting its previous one.

    Tipracks have no name in the config, so they are addressed by index, unlike
    core and stock plates. Absolute, like set_offset.

    Parameters:
        arg["rack_index"] (int): index into the loaded tip racks, default 0.
        arg["x"], arg["y"], arg["z"] (float): offset in millimetres, default 0.
    """
    rack_index = cast(int, arg.get("rack_index", 0))
    robot = ctx["robot"]
    racks = robot.support_plates
    if rack_index >= len(racks):
        raise ValueError(f"no tiprack at index {rack_index} (have {len(racks)})")
    racks[rack_index].set_offset(
        x=cast(float, arg.get("x", 0.0)),
        y=cast(float, arg.get("y", 0.0)),
        z=cast(float, arg.get("z", 0.0)),
    )
    return True


@register_action("calibration_plate")
def calibration_plate(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Pick up a tip and visit a plate's three corners, then return the tip.

    Visits A1 (top left), the bottom-left corner, and the top-right corner, so
    the three points let the user judge the plate plane under the current
    offset. The user adjusts with set_offset and retries until happy. Corners
    are computed from the labware, so the caller needs no well labels.

    Parameters:
        arg["plate"] (str): core or stock plate name.
        arg["rack_index"] (int): tiprack to draw the tip from, default 0.
        arg["tip_well"] (str): which tip to use, default "A1".
        arg["pipette_mount"] (str): defaults to "left".
        arg["clearance"] (float): millimetres above each well top, default 0.
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    plate_name = cast(str, arg["plate"])
    rack_index = cast(int, arg.get("rack_index", 0))
    tip_well = cast(str, arg.get("tip_well", "A1"))
    clearance = cast(float, arg.get("clearance", 0.0))

    robot = ctx["robot"]
    plate = robot.core_plates.get(plate_name) or robot.stock_plates.get(plate_name)
    if plate is None:
        raise ValueError(f"unknown plate '{plate_name}' for calibration")
    racks = robot.support_plates
    if rack_index >= len(racks):
        raise ValueError(f"no tiprack at index {rack_index} (have {len(racks)})")
    rack = racks[rack_index]

    columns = plate.columns()
    corners: list[Well] = [columns[0][0], columns[0][-1], columns[-1][0]]

    if pipette.has_tip:
        pipette.return_tip()
    pipette.pick_up_tip(rack[tip_well])
    for well in corners:
        pipette.move_to(well.top(clearance))
    pipette.return_tip()
    return True