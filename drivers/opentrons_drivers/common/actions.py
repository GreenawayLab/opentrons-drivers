import time
from typing import cast
from opentrons_drivers.common.custom_types import ActionFn
from opentrons_drivers.common.methods import LIQUID_METHODS, MODULE_METHODS, MECHANICAL_METHODS
from opentrons_drivers.common.custom_types import CoreWell, StaticCtx, JSONType
from opentrons.protocol_api.labware import Labware, Well
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

    Works for any channel count. A single-channel pipette moves one source well to
    one receiver well (the original behaviour, preserved exactly). A multi-channel
    pipette targets a row-A anchor and engages the whole column: the same single
    ``transfer_fn`` call runs the motion (the pipette drives the column), and the
    bookkeeping fans out over the ``n = pipette.channels`` wells the column holds.
    ``amount`` is the per-well volume, applied uniformly to every well in the
    column, so a stock source depletes by ``amount * n``.

    Modes:
        1. Stock -> Core:
            - source == ["substance_name"]
            - Validates stock volume for the aggregate over all channels, then
              updates `stock_amounts` once and every receiver well.

        2. Core -> Core:
            - source == ["core_plate", "well_label"]
            - Validates both columns, then updates `core_amounts` per channel,
              pairing source well i to receiver well i.

    Parameters:
        ctx (StaticCtx): Device state (pipettes, volumes, plates).
        arg (dict[str, JSONType]): Instruction arguments.
            - source: list[str]
            - receiver: list[str]           (multi-channel: a row-A anchor)
            - amount: float                 (per receiver well)
            - method: str
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

    # How many physical wells one motion touches. 1 for single-channel (the
    # original path), 8 for an 8-channel, etc. The pipette object is authoritative,
    # so the payload never carries this.
    n = pipette.channels

    # strongly typed payload
    source   = cast(list[str],  arg["source"])
    receiver = cast(list[str],  arg["receiver"])
    amount   = cast(float,      arg["amount"])

    method = cast(str, arg.get("method", "basic_liquid_transfer"))
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
            "pipette_mount", "swell_time", "swell_cycle", "tip_cycle",
            "wells", "source_wells",
        }
    }

    # get low-level transfer function
    transfer_fn = LIQUID_METHODS.get(method)
    if transfer_fn is None:
        raise ValueError(f"Unknown transfer method '{method}'. "
                         f"Available: {list(LIQUID_METHODS)}")

    # prep tip (a multi-channel pick_up_tip grabs a whole column of tips)
    if tip_on:
        pipette.pick_up_tip()

    # Receiver: the anchor well drives the motion; the column it resolves to is
    # what the bookkeeping fans out over. For n == 1 this is just [anchor], so the
    # single validation call below is identical to the original.
    recv_data: CoreWell = core_amounts[receiver[0]][receiver[1]]
    recv_anchor: Well = recv_data["position"]
    recv_column = help.resolve_column(recv_anchor, n)
    for w in recv_column:
        help.well_validation(core_amounts, [receiver[0], w.well_name], amount, "receiver")

    # STOCK -> CORE
    if len(source) == 1:
        sub_name = source[0]

        # One motion aspirates `amount` per channel from a single stock well, so
        # that well must hold the aggregate. stock_validation rolls to the next
        # well of this substance if the front is short and never splits a draw,
        # which is exactly right for a column aspiration from one reservoir well.
        help.stock_validation(stock_amounts, sub_name, amount * n, pipette.min_volume)

        stock_entry = stock_amounts[sub_name][0]
        stock_well: Well = stock_entry["position"]

        if swell_time > 0:  # passive swell
            help.swell_tip(pipette, stock_amounts, core_amounts, [sub_name], seconds=swell_time)

        if swell_cycle > 1:  # active swell
            help.swell_tip(pipette, stock_amounts, core_amounts, [sub_name], cycles=swell_cycle)

        transfer_fn(pipette=pipette,
                    to=recv_anchor,
                    fr=stock_well,
                    amount=amount,
                    **extra)

        # bookkeeping: one aggregate stock debit, one credit per receiver well
        stock_entry["volume"] -= amount * n
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for w in recv_column:
            rd = core_amounts[receiver[0]][w.well_name]
            rd["volume"] += amount
            rd["substance"][timestamp] = (sub_name, stock_well.well_name, amount)

    # CORE -> CORE
    elif len(source) == 2:
        src_data: CoreWell = core_amounts[source[0]][source[1]]
        src_anchor: Well = src_data["position"]
        src_column = help.resolve_column(src_anchor, n)
        for w in src_column:
            help.well_validation(core_amounts, [source[0], w.well_name], amount, "source")

        if swell_time > 0:  # passive swell
            help.swell_tip(pipette, stock_amounts, core_amounts, source, seconds=swell_time)

        if swell_cycle > 1:  # active swell
            help.swell_tip(pipette, stock_amounts, core_amounts, source, cycles=swell_cycle)

        transfer_fn(pipette=pipette,
                    to=recv_anchor,
                    fr=src_anchor,
                    amount=amount,
                    **extra)

        # bookkeeping: channel i moves source-column well i into receiver-column
        # well i. Volumes are uniform across the column, so the pairing is by
        # physical order (columns_by_name returns both top-to-bottom).
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for sw, rw in zip(src_column, recv_column):
            sd = core_amounts[source[0]][sw.well_name]
            rd = core_amounts[receiver[0]][rw.well_name]
            sd["volume"] -= amount
            rd["volume"] += amount
            rd["substance"][timestamp] = (source[0], sw.well_name, amount)
            sd["substance"][timestamp] = (receiver[0], rw.well_name, -amount)

    else:
        raise ValueError("`source` must be ['substance'] or ['plate', 'well'].")

    if tip_off:
        pipette.drop_tip()

    state = ctx["system_state"]
    # the receiver anchor always defines the new "location"
    state["plate"] = receiver[0]
    state["well"] = receiver[1]
    state["last_action"] = "transfer"
    state["timestamp"] = time.time()

    return True


@register_action("mechanical_move")
def mechanical_move(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Dispatch a mechanical (gantry-move) routine by name.

    Mirrors how transfer_execution dispatches LIQUID_METHODS: resolve the pipette,
    then hand off to the registered MECHANICAL_METHODS routine. Adding a tool
    (probe, sampler, ...) is a new @register_mechanical_method - no edit here.
    """
    mount = cast(str, arg.get("mount", "left"))
    pip = ctx["pipettes"][mount]
    method = arg.get("method")
    fn = MECHANICAL_METHODS.get(method)  # type: ignore[arg-type]
    if fn is None:
        raise ValueError(
            f"Unknown mechanical method '{method}'. Available: {list(MECHANICAL_METHODS)}"
        )
    return bool(fn(pip, ctx, arg))



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

    NOTE verify on hardware: reset_tipracks() is documented from API v2.0, but
    confirm it exists in the pinned opentrons version. return_tip() is already
    used elsewhere in this module, so that half is safe.

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
    """Apply a deck offset to one specific core plate, overwriting its previous one.

    Offsets belong to the labware: set_offset replaces rather than accumulates,
    so the calibration UI sends the full current value for that plate each time
    and re-moves to see the effect with no drift. The plate is reached through
    core_amounts (a core plate's wells carry their Well position, whose parent is
    the labware), so no robot handle is needed.

    Parameters:
        arg["plate"] (str): the core plate name.
        arg["x"], arg["y"], arg["z"] (float): offset in millimetres, default 0.
    """
    plate_name = cast(str, arg["plate"])
    wells = ctx["core_amounts"].get(plate_name)
    if not wells:
        raise ValueError(f"unknown core plate '{plate_name}' for calibration")
    plate = next(iter(wells.values()))["position"].parent
    plate.set_offset(
        x=cast(float, arg.get("x", 0.0)),
        y=cast(float, arg.get("y", 0.0)),
        z=cast(float, arg.get("z", 0.0)),
    )
    return True


def _tiprack_in_slot(pipette: InstrumentContext, slot: str) -> Labware:
    """Return the attached tiprack sitting in a deck slot.

    Addressing by slot rather than by position in ``pipette.tip_racks`` because
    the list order depends on config iteration order, which JSONB does not
    preserve. A slot is what the chemist actually sees on the bench, so it is
    both stable and unambiguous.
    """
    for rack in pipette.tip_racks:
        if str(rack.parent) == str(slot):
            return rack
    occupied = ", ".join(str(r.parent) for r in pipette.tip_racks) or "none attached"
    raise ValueError(f"no tiprack in deck slot {slot} (tipracks are in slots: {occupied})")


@register_action("calibration_tiprack")
def calibration_tiprack(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Try to pick up a tip from a tiprack then return it, to check its offset.

    The pickup itself is the test: if the pipette misaligns with the rack the
    tip will not seat cleanly. The user watches, adjusts the rack offset with
    set_tiprack_offset, and retries until pickup is clean. Any previously held
    tip is returned first.

    A multichannel pipette picks up a whole column of tips anchored at ``well``,
    so ``well`` must be a row-A anchor, checked up front rather than crashed into.

    Parameters:
        arg["slot"] (str): deck slot holding the tiprack.
        arg["well"] (str): which tip to try, default "A1". A central well is a
            representative check.
        arg["pipette_mount"] (str): defaults to "left".
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    well_label = cast(str, arg.get("well", "A1"))
    help.require_multichannel_anchor(well_label, pipette.channels)

    rack = _tiprack_in_slot(pipette, cast(str, arg["slot"]))
    if pipette.has_tip:
        pipette.return_tip()
    pipette.pick_up_tip(rack[well_label])
    pipette.return_tip()
    return True


@register_action("set_tiprack_offset")
def set_tiprack_offset(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Apply a deck offset to one tiprack, overwriting its previous one.

    Tipracks are addressed by deck slot, not by config name, because the agent
    attaches them to the pipette as a list. Absolute, like set_offset.

    Parameters:
        arg["slot"] (str): deck slot holding the tiprack.
        arg["pipette_mount"] (str): defaults to "left".
        arg["x"], arg["y"], arg["z"] (float): offset in millimetres, default 0.
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    _tiprack_in_slot(pipette, cast(str, arg["slot"])).set_offset(
        x=cast(float, arg.get("x", 0.0)),
        y=cast(float, arg.get("y", 0.0)),
        z=cast(float, arg.get("z", 0.0)),
    )
    return True


@register_action("calibration_plate")
def calibration_plate(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """Pick up a tip (or a column of tips) and visit representative plate points.

    Single-channel: visits three corners — A1 (top left), the bottom-left corner,
    and the top-right corner — so the three points let the user judge the plate
    plane under the current offset.

    Multichannel: a column pipette cannot touch the bottom-left corner (its
    nozzles already span the column from a row-A anchor, and any non-row-A target
    hangs tips off the edge). Instead it visits the row-A anchor of the first,
    middle, and last columns. Each visit engages the whole column, so the vertical
    span is covered at three horizontal positions, which is the plane check a
    multichannel can actually perform.

    Tips are picked up the same way in both cases (a multichannel pick_up_tip
    grabs the whole tip column anchored at ``tip_well``, which must be row A).

    Parameters:
        arg["plate"] (str): core plate name.
        arg["tip_slot"] (str): deck slot of the tiprack to draw the tip from.
        arg["tip_well"] (str): which tip to use, default "A1".
        arg["pipette_mount"] (str): defaults to "left".
        arg["clearance"] (float): millimetres above each well top, default 0.
    """
    pipette_mount = cast(str, arg.get("pipette_mount", "left"))
    pipette: InstrumentContext = ctx["pipettes"][pipette_mount]
    plate_name = cast(str, arg["plate"])
    tip_well = cast(str, arg.get("tip_well", "A1"))
    clearance = cast(float, arg.get("clearance", 0.0))

    help.require_multichannel_anchor(tip_well, pipette.channels)

    wells = ctx["core_amounts"].get(plate_name)
    if not wells:
        raise ValueError(f"unknown core plate '{plate_name}' for calibration")
    plate = next(iter(wells.values()))["position"].parent
    rack = _tiprack_in_slot(pipette, cast(str, arg["tip_slot"]))

    columns = plate.columns()
    if pipette.channels == 1:
        # three point corners: top-left, bottom-left, top-right
        targets: list[Well] = [columns[0][0], columns[0][-1], columns[-1][0]]
    else:
        # first, middle, last columns, each at its row-A anchor; the column span
        # covers the vertical extent, so no separate bottom corner is visited
        mid = len(columns) // 2
        targets = [columns[0][0], columns[mid][0], columns[-1][0]]

    if pipette.has_tip:
        pipette.return_tip()
    pipette.pick_up_tip(rack[tip_well])
    for well in targets:
        pipette.move_to(well.top(clearance))
    pipette.return_tip()
    return True


@register_action("module_action")
def module_action(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """
    Dispatch a module operation to a registered MODULE_METHODS function.

    The module object is looked up by name in ctx["modules"] (loaded at boot),
    and the method by name in MODULE_METHODS. Method params ride as top-level
    keys and are forwarded as kwargs; "module" and "method" are reserved.
    protocol is handed through for methods that need protocol.delay.

    Parameters:
        ctx (StaticCtx): Device state (modules, protocol, system_state).
        arg (dict[str, JSONType]): Instruction arguments.
            - module: str            (name of a loaded module)
            - method: str            (a MODULE_METHODS key)
            - ...plus any method-specific kwargs (rpm, minutes, action, ...)

    Returns:
        bool: True upon completion.
    """
    module_name = cast(str, arg["module"])
    modules = ctx["modules"]
    if module_name not in modules:
        raise RuntimeError(
            f"No module '{module_name}' loaded. Available: {sorted(modules.keys())}"
        )
    module = modules[module_name]

    method = cast(str, arg["method"])
    fn = MODULE_METHODS.get(method)
    if fn is None:
        raise ValueError(
            f"Unknown module method '{method}'. Available: {list(MODULE_METHODS)}"
        )

    extra = {k: v for k, v in arg.items() if k not in {"module", "method"}}
    fn(module, ctx["protocol"], **extra)

    state = ctx["system_state"]
    state["last_action"] = "module"
    state["timestamp"] = time.time()
    return True