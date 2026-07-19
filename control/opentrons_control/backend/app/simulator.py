"""Opentrons-free dry run of a manual protocol's liquid bookkeeping.

Mirrors the agent-side transfer accounting — stock/core volume tracking,
sufficiency checks, and overfill checks — reading everything from the same
``BaseConfig`` the agent launches with. No ``opentrons`` import: per-well
capacity is inline on ``PlateInfo.max_volume`` and initial volumes on
``PlateInfo.content``, so the checker needs no hardware package and no
labware JSON.

This is the code path the ``check`` endpoint calls. It is *not* the code
path the OT executes; the anti-drift contract between them is a set of
golden test vectors, since they cannot share a process once opentrons is
excluded here.

Accounting rules (v1, single-channel):
    * ``amount`` is per destination well.
    * A stock source depletes by ``amount * n_destinations``.
    * A core source depletes its own well by that total.
    * Each destination fills by ``amount``; exceeding ``max_volume`` errors.
Unknown actions pass through with a warning, keeping dispatch agnostic.

Known v1 gaps (flagged, not silently approximated):
    * Stock is tracked as a per-substance total; multi-well stocks with
      per-well aspiration limits are not modelled (fine for single-well stocks).
    * ``min_residual`` is not in ``BaseConfig`` (a runtime concern), so it is 0.
    * Well existence is not validated here; the editor only offers wells the
      labware definition declares, so steps reference real wells by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from opentrons_control.backend.app.protocol_model import (
    BaseConfig,
    ManualProtocol,
    SimReport,
    Step,
    StepVerdict,
)



def _is_tiprack(name: str) -> bool:
    """Return True for support labware that carries no liquid accounting."""
    return name.startswith("tiprack_")


class SimError(Exception):
    """A step-level accounting failure surfaced as a verdict error."""


@dataclass
class SimState:
    """Mutable bookkeeping state threaded through a dry run.

    :param stocks: Substance to current µL (summed across its stock wells).
    :param core: ``plate -> well -> {substance -> current µL}`` (composition).
    :param cap: ``plate -> per-well capacity µL or None``.
    """

    stocks: dict[str, float]
    core: dict[str, dict[str, dict[str, float]]]
    cap: dict[str, float | None] = field(default_factory=dict)

    @classmethod
    def from_config(cls, config: BaseConfig) -> "SimState":
        """Build initial state from the authored ``BaseConfig``."""
        stocks: dict[str, float] = {}
        for name, plate in config.stock_plates.items():
            if _is_tiprack(name):
                continue
            for cell in plate.content.values():
                stocks[cell.substance] = stocks.get(cell.substance, 0.0) + cell.volume

        core: dict[str, dict[str, dict[str, float]]] = {}
        cap: dict[str, float | None] = {}
        for name, plate in config.core_plates.items():
            if _is_tiprack(name):
                continue
            core[name] = {w: {c.substance: c.volume} for w, c in plate.content.items()}
            cap[name] = plate.max_volume

        return cls(stocks=stocks, core=core, cap=cap)


def _expand_wells(ref: str) -> list[str]:
    """Return the wells a receiver ref denotes.

    The simulator consumes atomic single-well transfers. Well selection and any
    rectangular expansion happen upstream (the editor grid is sized from the
    labware definition, so it knows the real rows and columns; the generator
    emits one transfer per well). A range reaching here means a producer skipped
    that expansion, so it is a hard error rather than a guess against an assumed
    grid.
    """
    if ":" in ref:
        raise SimError(f"well range '{ref}' must be expanded before the simulator")
    return [ref]


def _well_total(comp: dict[str, float]) -> float:
    """Total µL in a well across all substances."""
    return sum(comp.values())


def _apply_transfer(state: SimState, payload: dict[str, Any], v: StepVerdict) -> None:
    """Apply one ``transfer_execution`` payload to state, recording errors on ``v``."""
    source = payload.get("source")
    receiver = payload.get("receiver")
    if not isinstance(source, list) or not isinstance(receiver, list):
        v.errors.append("source and receiver must be lists")
        return
    try:
        amount = float(payload["amount"])
    except (KeyError, TypeError, ValueError):
        v.errors.append("amount missing or not a number")
        return
    if amount <= 0:
        v.errors.append("amount must be > 0")
        return
    if len(receiver) != 2:
        v.errors.append("receiver must be [plate, well]")
        return

    dst_plate, dst_ref = receiver[0], receiver[1]
    if dst_plate not in state.core:
        v.errors.append(f"unknown core plate '{dst_plate}'")
        return
    try:
        dests = _expand_wells(dst_ref)
    except SimError as exc:
        v.errors.append(str(exc))
        return

    total = amount * len(dests)

    # ---- source depletion: work out the mix that lands in each destination ----
    if len(source) == 1:
        sub = source[0]
        if sub not in state.stocks:
            v.errors.append(f"unknown stock '{sub}'")
            return
        if state.stocks[sub] - total < 0:
            v.errors.append(
                f"stock '{sub}' short: need {total:g} µL, have {state.stocks[sub]:g} µL"
            )
            return
        state.stocks[sub] -= total
        per_well_mix = {sub: amount}
    elif len(source) == 2:
        s_plate, s_well = source
        if s_plate not in state.core:
            v.errors.append(f"unknown core plate '{s_plate}'")
            return
        src_comp = state.core[s_plate].get(s_well, {})
        have = _well_total(src_comp)
        if have < total:
            v.errors.append(
                f"{s_plate}·{s_well} short: need {total:g} µL, have {have:g} µL"
            )
            return
        # a well aspiration removes each substance in proportion to its share,
        # so the moved liquid carries the source well's composition
        frac = total / have
        removed: dict[str, float] = {}
        for sub_name, vol in list(src_comp.items()):
            take = vol * frac
            src_comp[sub_name] = vol - take
            removed[sub_name] = take
        per_well_mix = {sub_name: vol / len(dests) for sub_name, vol in removed.items()}
    else:
        v.errors.append("source must be [substance] or [plate, well]")
        return

    # ---- destination fill + overfill check ----
    cap = state.cap.get(dst_plate)
    for well in dests:
        comp = state.core[dst_plate].setdefault(well, {})
        for sub_name, vol in per_well_mix.items():
            comp[sub_name] = comp.get(sub_name, 0.0) + vol
        new_total = _well_total(comp)
        if cap is not None and new_total > cap:
            v.errors.append(f"{dst_plate}·{well} overfills: {new_total:g} > {cap:g} µL")


_ACCOUNTERS = {
    "transfer_execution": _apply_transfer,
}


def simulate(protocol: ManualProtocol) -> SimReport:
    """Dry-run a protocol and return a per-step report.

    Folds each step over an in-memory bookkeeping state. A step with errors
    still advances state where it can, so later steps are checked against
    realistic volumes rather than aborting the whole report.

    :param protocol: The version-pinned protocol to check.
    :returns: A :class:`SimReport` with per-step verdicts and final volumes.
    """
    state = SimState.from_config(protocol.config)
    verdicts: list[StepVerdict] = []

    for i, step in enumerate(protocol.steps):
        v = StepVerdict(index=i, ok=True)
        accounter = _ACCOUNTERS.get(step.action)
        if accounter is None:
            v.warnings.append(f"action '{step.action}' not simulated")
        else:
            accounter(state, step.payload, v)
        v.ok = not v.errors
        verdicts.append(v)

    return SimReport(
        ok=all(v.ok for v in verdicts),
        verdicts=verdicts,
        final_stocks=state.stocks,
        final_core=state.core,
    )