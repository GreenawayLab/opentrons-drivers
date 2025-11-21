from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.types import Point, Location
from opentrons.protocol_api.labware import Well
from typing import Dict, List
from opentrons_drivers.common.custom_types import StockWell, CoreWell
import time

#---------- Liquid transfer low-level functions ----------

def liquid_batching(pipette: InstrumentContext, amt: float) -> List[float]:
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

#---------- High-level liquid transfer helpers (non-exportable) ----------

def stock_validation(stock_amounts: Dict[str, List[StockWell]], 
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

def well_validation(core_amounts: Dict[str, Dict[str, CoreWell]], 
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
                f"adding {amt}μL exceeds max {well_data['max_volume']}μL."
            )
    else:
        raise ValueError(f"Unknown validation role '{role}'. Expected 'source' or 'receiver'.")


def swell_tip(pipette: InstrumentContext, stock_amounts: Dict[str, List[StockWell]], 
              core_amounts: Dict[str, Dict[str, CoreWell]], with_what: list[str], 
              seconds: float=0, cycles: int=1) -> None:
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
    
    vol = pipette.max_volume*0.5
    
    if seconds == 0:  # active
        for _ in range(cycles):
            pipette.aspirate(vol, spot)
            time.sleep(10)
            pipette.move_to(spot.top())
            pipette.dispense(vol, location=spot)
            [pipette.blow_out(location=spot.top()) for _ in range(2)]
            pipette.swelled = name # type: ignore[attr-defined]
    else:  # passive
        pipette.aspirate(vol, spot)
        pipette.move_to(spot.bottom())
        time.sleep(seconds)  
        pipette.move_to(spot.top())
        pipette.dispense(vol, location=spot)
        pipette.blow_out(location=spot.top())
        pipette.swelled = with_what

def midpoint(fr: Well, to: Well) -> Location:
    """Calculate a safe midpoint above the deck between two wells.

    Args:
        fr (Well): Source well.
        to (Well): Destination well.

    Returns:
        Location: Absolute coordinates above the midpoint between the two wells.
    """
    source_coords = fr.top().point
    sourcex = source_coords.x
    sourcey = source_coords.y

    reciever_coords = to.top().point
    recieverx = reciever_coords.x
    recievery = reciever_coords.y

    # Midpoint calculation
    mid_x = (sourcex + recieverx) / 2
    mid_y = (sourcey + recievery) / 2
    mid_z = 130  # Fixed Z height for mid-point

    # Return as Location object
    mid = Point(mid_x, mid_y, mid_z)
    return Location(mid, None)
