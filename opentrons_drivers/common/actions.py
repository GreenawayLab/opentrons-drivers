import time
from typing import cast
from typing import Dict, Callable, List, TypeVar
from opentrons_drivers.common.custom_types import ActionFn
from opentrons_drivers.common.custom_types import StockWell, CoreWell, StaticCtx, JSONType
from opentrons.protocol_api.labware import Well
from opentrons.protocol_api.instrument_context import InstrumentContext

#---------- Registries of possible functions ----------

ACTION_REGISTRY: Dict[str, ActionFn] = {} # is exported
LIQUID_METHODS: Dict[str, Callable[..., object]] = {} # type: ignore[misc]

F = TypeVar("F", bound=Callable[..., object]) # type: ignore[misc]

# Shared registry decorator factory
def make_registry_decorator(registry: Dict[str, F]) -> Callable[[str], Callable[[F], F]]: # type: ignore[misc]
    def register(name: str) -> Callable[[F], F]: # type: ignore[misc]
        def decorator(fn: F) -> F: # type: ignore[misc]
            registry[name] = fn
            return fn
        return decorator
    return register

register_action = make_registry_decorator(ACTION_REGISTRY)
register_liquid_method = make_registry_decorator(LIQUID_METHODS)

#---------- Liquid transfer low-level functions ----------

def _liquid_batching(pipette: InstrumentContext, amt: float) -> List[float]:
    """
    Split a large transfer volume into pipette-sized batches.

    Parameters:
        pipette (InstrumentContext): The pipette being used.
        amt (float): Total volume to transfer.

    Returns:
        List[float]: A list of individual volumes to transfer in sequence.
    """
    max_vol = pipette.max_volume
    amts = [max_vol for _ in range(int(amt // max_vol))]
    res = amt % max_vol
    if res > 0:
        amts.append(res)

    return amts

"""
    All liquid transfer methods must have the same base signature:
    pipette: InstrumentContext, to: Well, fr: Well, amount: float
    
    Anything else can be passed as keyword arguments
    e.g. rate, iterations, etc. as **kwargs

"""
@register_liquid_method("_liquid_transfer")
def _liquid_transfer(pipette: InstrumentContext, 
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
    amts = _liquid_batching(pipette, amount)
    for a in amts:
        pipette.aspirate(a, fr)
        pipette.air_gap(20)
        pipette.dispense(location=to.top(z=1))
        pipette.blow_out(location=to.top(z=1))

@register_liquid_method("_viscous_liquid_transfer")
def _viscous_liquid_transfer(pipette: InstrumentContext, 
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
    amts = _liquid_batching(pipette, amount)
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

#---------- High-level liquid transfer helpers (non-exportable) ----------

def _stock_validation(stock_amounts: Dict[str, List[StockWell]], 
                     what: str, amt: float, min_vol: float) -> None:
    """
    Validate that a stock well contains enough liquid for a transfer.

    Parameters:
        stock_amounts (Dict[str, List[StockWell]]): Current stock volume per substance.
        what (str): Name of the substance to draw.
        amt (float): Required volume.
        min_vol (float): Minimum residual volume required after draw.

    Raises:
        RuntimeError: If the stock is insufficient.
    """
    approved = False

    while not approved:
        try:
            substance = stock_amounts[what][0]
        except:  # NOQA  # TODO: specify the exception type
            print(stock_amounts)
            raise RuntimeError(f"Substance {what} not found in deck.")

        if amt > (substance["volume"] - min_vol):
            print(
                f"Volume of {what} needed is greater than the volume in the well.\n"
                f"Well {substance['position']} is now out of scope. \n"
                f"Trying again by moving to the next well containing {what}."
            )
            # Change the well plate to move from
            stock_amounts[what].pop(0)
            if len(stock_amounts[what]) == 0:
                raise RuntimeError(f"No more {what} left on the deck.")
        else:
            approved = True

def _well_validation(core_amounts: Dict[str, Dict[str, CoreWell]], 
                    plate_requested: list[str], amt: float, role: str) -> None:
    """
    Validate that a core well can send or receive a volume.

    Parameters:
        core_amounts (Dict[str, Dict[str, CoreWell]]): Volume info per well.
        plate_requested (list[str]): [plate_name, well_label].
        amt (float): Volume to move.
        role (str): "source" or "receiver".

    Raises:
        RuntimeError: If well volume is too low or overflows.
        ValueError: If role is unknown.
    """
    plate_name, well = plate_requested
    try:
        well_data = core_amounts[plate_name][well]
    except KeyError:
        raise RuntimeError(f"Core well {plate_name} {well} not found.")

    if role == "source":
        if well_data["volume"] < amt:
            raise RuntimeError(
                f"Insufficient volume in {plate_name} {well}. "
                f"Available: {well_data['volume']}μL, required: {amt}μL."
            )
    elif role == "receiver":
        if (well_data["volume"] + amt) > well_data["max_volume"]:
            raise RuntimeError(
                f"Overflow risk: {plate_name} {well} has {well_data['volume']}μL, "
                f"adding {amt}μL exceeds max {well_data["max_volume"]}μL."
            )
    else:
        raise ValueError(f"Unknown validation role '{role}'. Expected 'source' or 'receiver'.")


def _swell_tip(pipette: InstrumentContext, stock_amounts: Dict[str, List[StockWell]], 
              core_amounts: Dict[str, Dict[str, CoreWell]], with_what: list[str]) -> None:
    """
    Pre-wet the tip with the liquid to reduce dripping and improve accuracy.

    Parameters:
        pipette (InstrumentContext): Pipette to pre-wet.
        stock_amounts (Dict[str, List[StockWell]]): Available stock wells.
        core_amounts (Dict[str, Dict[str, CoreWell]]): Available core wells.
        with_what (list[str]): [substance] or [plate, well].

    Raises:
        ValueError: If input is not 1 or 2 parts.
    """
    if len(with_what) == 1:
        spot = stock_amounts[with_what[0]][0]["position"]
        name = with_what[0]
    elif len(with_what) == 2:
        spot = core_amounts[with_what[0]][with_what[1]]["position"]
        name = f"{with_what[0]}_{with_what[1]}"
    else:
        raise ValueError("Either use [stock_substance_name] or [core_plate_name, well].")
    
    vol = pipette.max_volume*0.3
    pipette.aspirate(vol, spot)
    time.sleep(10)
    pipette.move_to(spot.top())
    pipette.dispense(vol, location=spot)
    pipette.swelled = name # type: ignore[attr-defined]

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
            - swell: bool = True
            - tip_cycle: bool = True
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

    method = cast(str, arg.get("method", "_liquid_transfer"))
    swell  = cast(bool, arg.get("swell", True))
    tip_cycle = cast(bool, arg.get("tip_cycle", True))

    # extra kwargs for specialised methods
    extra = {k: v for k, v in arg.items()
             if k not in {"source", "receiver", "amount",
                          "method", "pipette_mount", "swell", "tip_cycle"}}

    # get low-level transfer function
    transfer_fn = LIQUID_METHODS.get(method)
    if transfer_fn is None:
        raise ValueError(f"Unknown transfer method '{method}'. "
                         f"Available: {list(LIQUID_METHODS)}")

    # prep tip
    if tip_cycle:
        pipette.pick_up_tip()

    # receiver objects
    recv_data: CoreWell = core_amounts[receiver[0]][receiver[1]]
    recv_well: Well     = recv_data["position"]

    # STOCK → CORE 
    if len(source) == 1:
        sub_name = source[0]

        _well_validation(core_amounts, receiver, amount, "receiver")
        _stock_validation(stock_amounts, sub_name, amount, pipette.min_volume)

        stock_entry = stock_amounts[sub_name][0]
        stock_well: Well = stock_entry["position"]

        if swell:
            _swell_tip(pipette, stock_amounts, core_amounts, [sub_name])

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

        _well_validation(core_amounts, source,   amount, "source")
        _well_validation(core_amounts, receiver, amount, "receiver")

        if swell:
            _swell_tip(pipette, stock_amounts, core_amounts, source)

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

    if tip_cycle:
        pipette.drop_tip()

    return True
