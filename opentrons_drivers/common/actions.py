import time
from typing import cast
from typing import Dict, Callable, List
from opentrons_drivers.common.custom_types import ActionFn
from opentrons_drivers.common.custom_types import StockWell, CoreWell, StaticCtx, JSONType
from opentrons.protocol_api.labware import Well
from opentrons.protocol_api.instrument_context import InstrumentContext

#---------- Registries of possible functions ----------

ACTION_REGISTRY: Dict[str, ActionFn] = {} # is exported
LIQUID_METHODS: Dict[str, Callable] = {} # stays inside

def register_in(registry: Dict[str, Callable], name: str):
    def decorator(fn: Callable):
        registry[name] = fn
        return fn
    return decorator

def register_action(name: str):
    return register_in(ACTION_REGISTRY, name)

def register_liquid_method(name: str):
    return register_in(LIQUID_METHODS, name)

#---------- Liquid transfer low-level functions ----------

def _liquid_batching(pipette: InstrumentContext, amt: float) -> None:
    """This function universally receives from where to where move what.

    Assumes that the tip is on and everything is tip-top (hah).
    """
    max_vol = pipette.max_volume
    amts = [max_vol for _ in range(int(amt // max_vol))]
    res = amt % max_vol
    if res > 0:
        amts.append(res)
    # we have sliced the vol to discrete additions by max vols
    # e.g. 2.73ml -> [0.95, 0.95, 0.83]
    return amts

@register_liquid_method("_liquid_transfer")
def _liquid_transfer(pipette: InstrumentContext, to: Well, fr: Well, amount: float):
    amts = _liquid_batching(pipette, amount)
    for a in amts:
        pipette.aspirate(a, fr)
        pipette.air_gap(20)
        pipette.dispense(location=to.top(z=1))
        pipette.blow_out(location=to.top(z=1))

@register_liquid_method("_viscous_liquid_transfer")
def _viscous_liquid_transfer(pipette: InstrumentContext, to: Well, fr: Well, 
                             amount: float, rate: float):
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

#---------- High-level liquid transfer (exportable but mostly used inside) ----------

def stock_validation(stock_amounts: Dict[str, List[StockWell]], 
                     what: str, amt: float, min_vol) -> None:
        """This function checks that if stocks are addressed 
        they have a sufficient amount of whatever is needed."""
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

def well_validation(core_amounts: Dict[str, Dict[str, CoreWell]], 
                    plate_requested: list[str], amt: float, role: str) -> None:
    """
    Validates a well for use as source or receiver.

    Parameters:
        plate: list[str] — [plate_name, well_label]
        amt: float — the amount to move
        role: str — either "source" or "receiver"

    Raises:
        RuntimeError if validation fails.
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


def swell_tip(pipette: InstrumentContext, stock_amounts: Dict[str, List[StockWell]], 
              core_amounts: Dict[str, Dict[str, CoreWell]], with_what: list[str]) -> None:
    """Better wetting I guess so no drip and better precision.

    Sacred knowledge inherited from an elder, more developed civilization.
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
    pipette.swelled = name

#---------- Complex exportable functions ----------

@register_action("transfer_execution")
def transfer_execution(ctx: StaticCtx, arg: dict[str, JSONType]) -> bool:
    """
    Perform a liquid transfer and update bookkeeping.

    Two modes
    ----------
    1. Stock → Core
       * `source == ["substance_name"]`
       * Validates stock volume, updates `stock_amounts` and `core_amounts`.
    2. Core → Core
       * `source == ["core_plate", "well_label"]`
       * Validates both wells and updates `core_amounts`.

    Payload (`arg`)
    ---------------
    • source: list[str]                 (see above)
    • receiver: list[str]               (["core_plate", "well_label"])
    • amount: float                     (µL)
    • method: str (default "_liquid_transfer")
    • pipette_mount: str ("left"/"right", default "left")
    • swell: bool                       (wet tip, default True)
    • tip_cycle: bool                   (pick/drop tip, default True)
    • …anything else is forwarded to the low-level method as **extra
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

        well_validation(core_amounts, receiver, amount, "receiver")
        stock_validation(stock_amounts, sub_name, amount, pipette.min_volume)

        stock_entry = stock_amounts[sub_name][0]
        stock_well: Well = stock_entry["position"]

        if swell:
            swell_tip(pipette, stock_amounts, core_amounts, [sub_name])

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

        well_validation(core_amounts, source,   amount, "source")
        well_validation(core_amounts, receiver, amount, "receiver")

        if swell:
            swell_tip(pipette, stock_amounts, core_amounts, source)

        transfer_fn(pipette=pipette,
                    to=recv_well,
                    fr=src_well,
                    amount=amount,
                    **extra)

        # bookkeeping
        src_data["volume"]  -= amount
        recv_data["volume"] += amount
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        recv_data["substance"][timestamp] = (*source, amount)
        src_data["substance"][timestamp]  = (*receiver, -amount)

    else:
        raise ValueError("`source` must be ['substance'] or ['plate', 'well'].")

    if tip_cycle:
        pipette.drop_tip()

    return True
