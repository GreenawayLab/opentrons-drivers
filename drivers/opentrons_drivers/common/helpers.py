from opentrons.protocol_api.instrument_context import InstrumentContext
from opentrons.types import Point, Location
from opentrons.protocol_api.labware import Well
from typing import Dict, List
from opentrons_drivers.common.custom_types import StockWell, CoreWell
from typing import Dict, Callable, TypeVar
import time
import re

F = TypeVar("F", bound=Callable[..., object])

def make_registry_decorator(registry: Dict[str, F]) -> Callable[[str], Callable[[F], F]]: # type: ignore[misc]
    """
    Return a decorator factory that registers functions into a given mapping.

    The returned decorator takes a string key and, when applied to a function,
    stores that function in the provided registry under the specified name.
    This is used to form registries of liquid_methods and actions to be triggered.

    Parameters
    ----------
    registry : Dict[str, F]
        A dictionary that will collect functions keyed by the names supplied
        to the generated decorator.

    Returns
    -------
    Callable[[str], Callable[[F], F]]
        A decorator factory. Calling it with a name returns a decorator that
        registers a function under that name.
    """
    def register(name: str) -> Callable[[F], F]: # type: ignore[misc]
        def decorator(fn: F) -> F: # type: ignore[misc]
            registry[name] = fn
            return fn
        return decorator
    return register

#---------- Liquid transfer low-level functions ----------

def liquid_batching(pipette: InstrumentContext, amt: float, reserve: float = 0.0) -> List[float]:
    """
    Split a large transfer volume into pipette-sized batches.

    Parameters:
        pipette (InstrumentContext): The pipette being used.
        amt (float): Total volume to transfer.
        reserve (float): Volume in uL to keep free in the tip on every aspirate -
            e.g. air gaps drawn on top of each chunk. Chunks are sized to
            working_volume - reserve, so a chunk plus its air gaps never overflows
            tip. Defaults to 0 (the original full-capacity chunking).

    Returns:
        List[float]: A list of individual volumes to transfer in sequence.
    """
    # Size against the WORKING volume, not max_volume. max_volume is the nominal
    # pipette figure (e.g. 300 for a p300); the real aspirate ceiling for the
    # attached tip is lower (opentrons holds back ~10%, so a "300" tip may only
    # take 270). Using max_volume overfills and raises InvalidAspirateVolumeError
    # on the robot. get_working_volume is the exact number opentrons enforces;
    # fall back to max_volume only if the (private) accessor is unavailable.
    try:
        usable = float(pipette._core.get_working_volume())
    except Exception:  # noqa: BLE001 - fall back to the nominal max
        usable = float(pipette.max_volume)
    max_vol = usable - reserve
    if max_vol <= 0:
        raise ValueError(
            f"reserved volume {reserve} uL leaves no room in a "
            f"{usable} uL working tip"
        )
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


#---------- Multichannel addressing helpers ----------
#
# The one invariant behind every action: a multichannel pipette addresses
# a whole COLUMN anchored at row A. Command it to any other row and the front
# nozzle lands on target while the rest hang off the labware edge and crash. So
# every action that aims the pipette at a specific well must, for channels > 1,
# target row A and understand it is touching the whole column, not one well. A
# single-channel pipette (channels == 1) is unrestricted, so these helpers are
# no-ops for it and every single-channel path is preserved exactly.


def _split_label(label: str) -> tuple[str, str]:
    """Split a well label into (row_letters, column_digits), e.g. 'A1' -> ('A', '1').

    :param label: A well label such as ``"A1"`` or ``"H12"``.
    :returns: The row-letter prefix and the column-digit suffix.
    :raises RuntimeError: if the label is not <letters><digits>.
    """
    parsed = re.fullmatch(r"([A-Za-z]+)(\d+)", label)
    if parsed is None:
        raise RuntimeError(f"cannot parse well label '{label}'")
    return parsed.group(1), parsed.group(2)


def require_multichannel_anchor(label: str, channels: int) -> None:
    """Reject a multichannel pipette aimed at a non-row-A well.

    A no-op for single-channel pipettes. For a multichannel one, the target must
    be a row-A anchor or the motion would put most nozzles off the labware.

    :param label: The well label the pipette is being aimed at.
    :param channels: The pipette channel count (``pipette.channels``).
    :raises RuntimeError: if channels > 1 and the label is not in row A.
    """
    if channels > 1:
        row, _ = _split_label(label)
        if row != "A":
            raise RuntimeError(
                f"a {channels}-channel pipette must target row A, but got well '{label}'"
            )


def resolve_column(anchor: Well, channels: int) -> list[Well]:
    """Return the physical wells a pipette engages when it targets ``anchor``.

    A single-channel pipette (``channels == 1``) touches only the anchor well, so
    the anchor is returned unchanged and every single-channel path is preserved
    exactly. A multi-channel pipette addresses a whole column anchored at row A:
    the returned list is that column, top to bottom, and must contain exactly
    ``channels`` wells.

    Only plates whose column length equals the channel count are supported here.
    A 384-well plate addresses every other well (a different resolution), and any
    other mismatch means an incompatible pipette or plate reached the agent, so
    both raise rather than being silently mis-resolved into a crash on the deck.

    :param anchor: The well the transfer targets. For a multi-channel pipette this
        must be a row-A well.
    :param channels: The pipette channel count, i.e. ``pipette.channels``.
    :returns: The ``channels`` wells engaged, in physical top-to-bottom order.
    :raises RuntimeError: if a multi-channel anchor is not in row A, or its column
        does not hold exactly ``channels`` wells.
    """
    if channels == 1:
        return [anchor]

    require_multichannel_anchor(anchor.well_name, channels)
    _, column_name = _split_label(anchor.well_name)

    column = anchor.parent.columns_by_name()[column_name]
    if len(column) != channels:
        raise RuntimeError(
            f"a {channels}-channel pipette needs a column of {channels} wells, but "
            f"column {column_name} of '{anchor.parent}' has {len(column)}"
        )
    return column