import time
from typing import Callable
from opentrons.protocol_api.labware import Well
from opentrons.protocol_api.instrument_context import InstrumentContext
import opentrons_drivers.common.helpers as help

LIQUID_METHODS: dict[str, Callable[..., object]] = {}
register_liquid_method = help.make_registry_decorator(LIQUID_METHODS)

"""
    All liquid transfer methods must have the same base signature:
    pipette: InstrumentContext, to: Well, fr: Well, amount: float
    
    Anything else can be passed as keyword arguments
    e.g. rate, iterations, etc. as **kwargs

"""
@register_liquid_method("basic_liquid_transfer")
def basic_liquid_transfer(pipette: InstrumentContext, 
                     to: Well, fr: Well, 
                     amount: float, 
                     airgap: float = 20) -> None:
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
        pipette.air_gap(airgap)
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
