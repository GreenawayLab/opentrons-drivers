import time
from typing import Callable, Any
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
    amts = help.liquid_batching(pipette, amount, reserve=airgap)
    if airgap < 0:
        raise ValueError(f"airgap must be non-negative, got {airgap}")
    if amts and airgap + max(amts) > pipette.max_volume:
        raise ValueError(
            f"airgap {airgap} uL + aspirate {max(amts)} uL exceeds tip capacity "
            f"{pipette.max_volume} uL; reduce the airgap or the per-transfer volume"
        )
    for a in amts:
        pipette.aspirate(a, fr)
        pipette.air_gap(airgap)
        pipette.dispense(location=to.top(z=1))
        pipette.blow_out(location=to.top(z=1))


@register_liquid_method("advanced_liquid_transfer")
def advanced_liquid_transfer(pipette: InstrumentContext,
                    to: Well, 
                    fr: Well, 
                    amount: float, 
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

        if not (0.0 <= airgap <= 1.0):
            raise ValueError(
                f"advanced_liquid_transfer airgap is a FRACTION 0-1 (portion of "
                f"spare tip capacity), got {airgap}. Note basic_liquid_transfer "
                f"airgap is a VOLUME in uL - do not pass a volume here."
            )
        amts = help.liquid_batching(pipette, amount)
        for a in amts:
            can = pipette.max_volume - a
            # clamp non-negative and within remaining capacity: a negative air_gap
            # (e.g. from an out-of-range airgap) faults the hardware and crashes the
            # agent with no catchable error.
            initial_ag = max(0.0, min(can * 0.3 * airgap, can))
            midway_ag = max(0.0, min(can * 0.15 * (1 - airgap), can - initial_ag))
            pipette.aspirate(a, fr.bottom(z=2), flow_rate=asprate) 
            [pipette.touch_tip(fr) for _ in range(touchtip)] 
            pipette.air_gap(initial_ag) 
            pipette.move_to(help.midpoint(fr,to)) 
            pipette.air_gap(midway_ag, in_place = True) # type: ignore[call-arg]
            pipette.dispense(a, location=to.top(z=1), flow_rate=disrate) 
            [pipette.blow_out(location=to.top(z=1)) for _ in range(blowout)]
            [pipette.touch_tip(to) for _ in range(touchtip)]
        
        pipette.move_to(fr.top(z=5))


@register_liquid_method("semi_advanced_liquid_transfer")
def semi_advanced_liquid_transfer(pipette: InstrumentContext,
                    to: Well,
                    fr: Well,
                    amount: float,
                    airgap: float,
                    midgap: float,
                    touchtip: int,
                    blowout: int,
                    asprate: float,
                    disrate: float
                    ) -> None:
    """Like advanced_liquid_transfer, but the two air gaps are explicit volumes.

    A copy of advanced_liquid_transfer with two deliberate differences, kept as a
    separate method so the shared advanced_liquid_transfer is untouched:

      * ``airgap`` and ``midgap`` are direct volumes in uL (the initial and the
        midway air gap), not a fraction of spare capacity. The caller sets exactly
        how much air to draw.
      * No final move back to the source well at the end.

    Args:
        to (Well): Destination well.
        fr (Well): Source well.
        amount (float): Volume in uL to transfer.
        airgap (float): Initial air gap volume in uL (drawn after aspirating).
        midgap (float): Midway air gap volume in uL (drawn in transit).
        touchtip (int): Number of touch-tip cycles.
        blowout (int): Number of blowout cycles.
        asprate (float): Aspiration rate.
        disrate (float): Dispense rate.

    Returns:
        None
    """
    if airgap < 0 or midgap < 0:
        raise ValueError(f"airgap and midgap must be non-negative, got {airgap}, {midgap}")
    amts = help.liquid_batching(pipette, amount, reserve=airgap + midgap)
    if amts and airgap + midgap + max(amts) > pipette.max_volume:
        raise ValueError(
            f"airgap {airgap} + midgap {midgap} + aspirate {max(amts)} uL exceeds "
            f"tip capacity {pipette.max_volume} uL; reduce a value"
        )
    for a in amts:
        pipette.aspirate(a, fr.bottom(z=2), flow_rate=asprate)
        [pipette.touch_tip(fr) for _ in range(touchtip)]
        pipette.air_gap(airgap)
        pipette.move_to(help.midpoint(fr, to))
        pipette.air_gap(midgap, in_place=True)  # type: ignore[call-arg]
        pipette.dispense(a, location=to.top(z=1), flow_rate=disrate)
        [pipette.blow_out(location=to.top(z=1)) for _ in range(blowout)]
        [pipette.touch_tip(to) for _ in range(touchtip)]
    # no final move back to the source well


@register_liquid_method("viscous_liquid_transfer")
def viscous_liquid_transfer(pipette: InstrumentContext, 
                             to: Well, fr: Well, 
                             amount: float, 
                             flow_rate: float) -> None:
    """
    Transfer method for viscous liquids.

    Slows down aspiration and dispense speeds, includes touch tips.

    Parameters:
        pipette (InstrumentContext): Pipette to use.
        to (Well): Target well.
        fr (Well): Source well.
        amount (float): Volume to transfer.
        flow_rate (float): Aspiration/dispense flow rate multiplier.

    Returns:
        None
    """
    amts = help.liquid_batching(pipette, amount)
    for a in amts:
        pipette.move_to(fr.bottom(z=3))
        time.sleep(10)
        pipette.aspirate(a, fr.bottom(z=3), flow_rate=flow_rate)
        [pipette.touch_tip(radius=0.5, speed=30, v_offset=-50) for _ in range(3)]
        pipette.move_to(fr.top())
        time.sleep(10)
        [pipette.touch_tip(radius=1, speed=30, v_offset=-10) for _ in range(3)]
        pipette.dispense(a, location=to.top(z=1), flow_rate=flow_rate)
        pipette.touch_tip(radius=1, speed=400, v_offset=-5)
        pipette.dispense(a, location=fr.top(z=-1), flow_rate=flow_rate)
        pipette.blow_out(location=fr.top(z=1))


#---------- Module methods ----------

MODULE_METHODS: dict[str, Callable[..., object]] = {}
register_module_method = help.make_registry_decorator(MODULE_METHODS)

"""
    All module methods must have the same base signature:
    module: Any (the loaded module context), protocol: Any (the ProtocolContext)

    module is the loaded module object (from StaticCtx["modules"]); protocol is
    needed for protocol.delay (protocol.pause is forbidden - it deadlocks the
    agent thread). Anything else is passed as keyword arguments (rpm, minutes,
    etc.) drawn from the step's method params.

    Types are Any because the module-context union is opentrons-version-specific;
    this is a hardware wire boundary.
"""

@register_module_method("heater_shaker_shake")
def heater_shaker_shake(module: Any, protocol: Any, rpm: float, minutes: float) -> None:
    """
    Latch, shake at a set speed for a set time, then stop and release.

    The heater-shaker will only shake with the labware latch closed (a hardware
    safety), so the latch is closed first and opened again at the end so the
    plate can be accessed or removed. protocol.delay holds for the shake duration
    (protocol.pause is never used - it deadlocks the agent's protocol thread).

    :param module: The loaded heater-shaker module context.
    :param protocol: The active ProtocolContext (for delay).
    :param rpm: Target shake speed in RPM.
    :param minutes: How long to shake.
    """
    module.close_labware_latch()
    module.set_and_wait_for_shake_speed(rpm)
    protocol.delay(minutes=minutes)
    module.deactivate_shaker()
    module.open_labware_latch()


@register_module_method("heater_shaker_latch")
def heater_shaker_latch(module: Any, protocol: Any, action: str = "open") -> None:
    """
    Open or close the heater-shaker labware latch.

    :param module: The loaded heater-shaker module context.
    :param protocol: The active ProtocolContext (unused, kept for a uniform signature).
    :param action: ``"open"`` (default) or ``"close"``.
    """
    if action == "close":
        module.close_labware_latch()
    else:
        module.open_labware_latch()