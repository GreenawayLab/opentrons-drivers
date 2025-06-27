import time
import os
from inspect import signature
import sys

def call_with_needed(fn, **kwargs):
    needed = signature(fn).parameters
    return fn(**{k: v for k, v in kwargs.items() if k in needed})

#---------- Liquid transfer low-level functions ----------

def _liquid_batching(pipette, amt: float) -> None:
    """This function universally receives from where to where move what.

    Assumes that the tip is on and everything is tip-top (hah).
    """
    max_vol = pipette.max_volume
    amts = [max_vol for i in range(int(amt // max_vol))]
    res = amt % max_vol
    if res > 0:
        amts.append(res)
    # we have sliced the vol to discrete additions by max vols
    # e.g. 2.73ml -> [0.95, 0.95, 0.83]
    return amts

def _liquid_transfer(pipette, to, fr, amount:float):
    amts = _liquid_batching(pipette, amount)
    for a in amts:
        pipette.aspirate(a, fr)
        pipette.air_gap(20)
        pipette.dispense(location=to.top(z=1))
        pipette.blow_out(location=to.top(z=1))

def _viscous_liquid_transfer(pipette, to, fr, amount:float, rate):
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

#---------- High-level liquid transfer ----------

def stock_validation(stock_amounts, what: str, amt: float, min_vol) -> None:
        """This function checks that if stocks are addressed we have a sufficient amount of whatever is needed."""
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

def well_validation(core_amounts, plate: list[str], amt: float, role: str) -> None:
    """
    Validates a well for use as source or receiver.

    Parameters:
        plate: list[str] — [plate_name, well_label]
        amt: float — the amount to move
        role: str — either "source" or "receiver"

    Raises:
        RuntimeError if validation fails.
    """
    plate_name, well = plate
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


def swell_tip(pipette, stock_amounts, core_plates, with_what: list[str]) -> None:
    """Better wetting I guess so no drip and better precision.

    Sacred knowledge inherited from an elder, more developed civilization.
    """
    if len(with_what) == 1:
        spot = stock_amounts[with_what[0]][0]["position"]
        name = with_what[0]
    elif len(with_what) == 2:
        spot = core_plates[with_what[0]][with_what[1]]
        name = f"{with_what[0]}_{with_what[1]}"
    else:
        raise ValueError("Either use [stock_substance_name] or [core_plate_name, well].")
    
    vol = pipette.max_volume*0.3
    pipette.aspirate(vol, spot)
    time.sleep(10)
    pipette.move_to(spot.top())
    pipette.dispense(vol, location=spot)
    pipette.swelled = name

def transfer_execution(pipettes, core_plates, 
                       core_amounts, stock_amounts, 
                       source: list[str], 
                       receiver: list[str], 
                       amount: float,
                       method: str = "_liquid_transfer",
                       pipette_mount: str = "left",
                       swell: bool = True,
                       tip_cycle: bool = True,
                       **extra) -> None:
    
    """Handles liquid transfers between wells.

    - From **stock** → Core well: Validates stock, updates `core_amounts`.
    - From **core well** → Another core well: Validates both source & receiver, updates `core_amounts`.
    """
    pipette = pipettes[pipette_mount]

    if tip_cycle:
        pipette.pick_up_tip()

    this_module = sys.modules[__name__]          # module where funcs live
    try:
        transfer_fn = getattr(this_module, method)
    except AttributeError:
        raise ValueError(
            f"Unknown transfer method '{method}'. "
            f"Defined methods: {[n for n in dir(this_module) if n.startswith('_') and 'transfer' in n]}"
        )
    
    # Receiver is always in a core plate
    to = core_plates[receiver[0]][receiver[1]]

    if len(source) == 1:
        # Stock transfer case
        well_validation(core_amounts, receiver, amount, "receiver")
        what = source[0]
        stock_validation(stock_amounts, what, amount, pipette.min_volume)
        substance = stock_amounts[what][0]
        fr = substance["position"]
        if swell:
            swell_tip(pipette, stock_amounts, core_plates, [what])

        call_with_needed(
        transfer_fn,
        pipette=pipette,
        to=to,
        fr=fr,
        amount=amount,
        **extra,
                        )

        # Update stock volume
        substance["volume"] -= amount

        # Update receiver well in core_amounts
        to["volume"] += amount

        # Track history with timestamp
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        to["substance"][timestamp] = (
            what,
            substance["position"].well_name,
            amount,
        )

    elif len(source) == 2:
        # Core-to-core transfer case
        fr = core_plates[source[0]][source[1]]

        well_validation(core_amounts, source, amount, "source")
        well_validation(core_amounts, receiver, amount, "receiver")

        if swell:
            swell_tip(pipette, stock_amounts, core_plates, source)
        
        call_with_needed(
        transfer_fn,
        pipette=pipette,
        to=to,
        fr=fr,
        amount=amount,
        **extra,
                        )

        # Update source and receiver well volumes

        fr["volume"] -= amount
        to["volume"] += amount

        # Track history with timestamp
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        to["substance"][timestamp] = (
                *source,
                amount,
            )
        fr["substance"][timestamp] = (
                *receiver,
                -amount,
            )
    else:
        raise ValueError(
                "Wrong transfer parameters. \n"
                "Either use a masked stock name (e.g. source=['alc_1']) \n"
                "or core coordinates (e.g. source=['core_0', 'A1'])."
            )
    if tip_cycle:
        pipette.drop_tip()
    
    return True