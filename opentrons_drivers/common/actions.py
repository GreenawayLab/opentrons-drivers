import time
from typing import cast
from typing import Dict, Callable, TypeVar
from opentrons_drivers.common.custom_types import ActionFn
from opentrons_drivers.common.custom_types import CoreWell, StaticCtx, JSONType
from opentrons.protocol_api.labware import Well
from opentrons.protocol_api.instrument_context import InstrumentContext
import opentrons_drivers.common.helpers as help

#---------- Registries of possible functions ----------

ACTION_REGISTRY: dict[str, ActionFn] = {}  # is exported
LIQUID_METHODS: dict[str, Callable[..., object]] = {}

F = TypeVar("F", bound=Callable[..., object])

def make_registry_decorator(registry: Dict[str, F]) -> Callable[[str], Callable[[F], F]]: # type: ignore[misc]
    def register(name: str) -> Callable[[F], F]: # type: ignore[misc]
        def decorator(fn: F) -> F: # type: ignore[misc]
            registry[name] = fn
            return fn
        return decorator
    return register

register_action = make_registry_decorator(ACTION_REGISTRY)
register_liquid_method = make_registry_decorator(LIQUID_METHODS)


"""
    All liquid transfer methods must have the same base signature:
    pipette: InstrumentContext, to: Well, fr: Well, amount: float
    
    Anything else can be passed as keyword arguments
    e.g. rate, iterations, etc. as **kwargs

"""
@register_liquid_method("basic_liquid_transfer")
def basic_liquid_transfer(pipette: InstrumentContext, 
                     to: Well, fr: Well, 
                     amount: float) -> None:
    """
    Basic liquid transfer method.

    Parameters:
        pipette (InstrumentContext): Pipette to use.
        to (Well): Target well.
        fr (Well): Source well.
        amount (float): Total volume to transfer.

    Returns:
        None
    """
    amts = help.liquid_batching(pipette, amount)
    for a in amts:
        pipette.aspirate(a, fr)
        pipette.air_gap(20)
        pipette.dispense(location=to.top(z=1))
        pipette.blow_out(location=to.top(z=1))


@register_liquid_method("advanced_liquid_transfer")
def advanced_liquid_transfer(pipette: InstrumentContext,
                    to: Well, 
                    fr: Well, 
                    amt: float, 
                    airgap: float, 
                    touchtip: int, 
                    blowout: int, 
                    asprate: float, 
                    disrate: float
                    ) -> None:
        """Perform a liquid transfer between two wells.

        Handles chunking volumes greater than pipette capacity into multiple
        aspiration/dispense cycles.

        Args:
            to (Well): Destination well.
            fr (Well): Source well.
            amt (float): Volume in µL to transfer.
            airgap (float): Volume in µL of airgap.
            touchtip (int): Number of touch-tip cycles.
            blowout (int): Number of blowout cycles.
            asprate (float): Aspiration rate multiplier.
            disrate (float): Dispense rate multiplier.

        Returns:
            None
        """

        amts = help.liquid_batching(pipette, amt)
        for a in amts:
            can = pipette.max_volume - a
            initial_ag = min(can * 0.3 * airgap, can)
            midway_ag = min(can * 0.15 * (1-airgap), can-initial_ag)
            pipette.aspirate(a, fr.bottom(z=2), rate=asprate) 
            [pipette.touch_tip(fr) for _ in range(touchtip)] 
            pipette.air_gap(initial_ag) 
            pipette.move_to(help.midpoint(fr,to)) 
            pipette.air_gap(midway_ag, in_place = True) # type: ignore[call-arg]
            pipette.dispense(a, location=to.top(z=1), rate=disrate) 
            [pipette.blow_out(location=to.top(z=1)) for _ in range(blowout)]
            [pipette.touch_tip(to) for _ in range(touchtip)]
        
        pipette.move_to(fr.top(z=5))


@register_liquid_method("viscous_liquid_transfer")
def viscous_liquid_transfer(pipette: InstrumentContext, 
                             to: Well, fr: Well, 
                             amount: float, 
                             rate: float) -> None:
    """
    Transfer method for viscous liquids.

    Slows down aspiration and dispense speeds, includes touch tips.

    Parameters:
        pipette (InstrumentContext): Pipette to use.
        to (Well): Target well.
        fr (Well): Source well.
        amount (float): Volume to transfer.
        rate (float): Aspiration/dispense rate multiplier.

    Returns:
        None
    """
    amts = help.liquid_batching(pipette, amount)
    for a in amts:
        pipette.move_to(fr.bottom(z=3))
        time.sleep(10)
        pipette.aspirate(a, fr.bottom(z=3), rate=rate)
        [pipette.touch_tip(radius=0.5, speed=30, v_offset=-50) for _ in range(3)]
        pipette.move_to(fr.top())
        time.sleep(10)
        [pipette.touch_tip(radius=1, speed=30, v_offset=-10) for _ in range(3)]
        pipette.dispense(a, location=to.top(z=1), rate=rate)
        pipette.touch_tip(radius=1, speed=400, v_offset=-5)
        pipette.dispense(a, location=fr.top(z=-1), rate=rate)
        pipette.blow_out(location=fr.top(z=1))


#---------- Complex exportable functions ----------

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

    state = ctx.get("agent_state")
    if state is not None:
        # receiver always defines the new "location"
        state["plate"] = receiver[0]
        state["well"] = receiver[1]
        state["last_action"] = "transfer"
        state["timestamp"] = time.time()

    return True

@register_action("sampler_action")
def sampler_action(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    pip = ctx["pipettes"][arg.get("sampler_mount", "left")]
    state = ctx["agent_state"]

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
                pip.move_to(pos.top(100))

        if mode_prev == "wash":
            wash = ctx["stock_amounts"]["wash_solv"][0]["position"]
            pip.move_to(wash.top(100))
        else:
            pos = ctx["core_amounts"][plate][well]["position"]
            pip.move_to(pos.top(100))

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
        pip.move_to(pos.top(40))

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
        pip.move_to(pos.top(35))

        state["plate"] = plate
        state["well"]  = well
        state["last_action"] = "scan"
        state["last_args"] = arg
        state["timestamp"] = time.time()

        return True
