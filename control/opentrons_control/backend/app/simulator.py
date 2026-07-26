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
excluded here. Multichannel makes that contract load-bearing: the agent
derives the touched column from the live pipette (``pipette.channels``),
while this checker has no pipette object and is *told* the wells instead
(see below), so a golden vector for a one-column multichannel op that both
must agree on is exactly the drift guard to keep.

Accounting rules:
    * ``amount`` is per destination well.
    * A transfer touches one well (single-channel) or a column of N wells
      (multichannel). The wells are carried explicitly in the payload —
      ``wells`` for the receiver, ``source_wells`` for a core source — and
      default to the single anchor well when absent, so single-channel plans
      are unchanged. The checker never derives geometry; it reads the resolved
      wells the frontend produced, mirroring the agent that derives them from
      the live pipette.
    * Stock -> core: one aspiration feeds every destination, so the stock
      depletes by ``amount * n_wells`` and each destination fills by ``amount``.
    * Core -> core: paired columns — channel i moves ``amount`` from source
      well i (carrying its composition) into destination well i.
    * A destination exceeding ``max_volume`` errors.
Unknown actions pass through with a warning, keeping dispatch agnostic.

Known gaps (flagged, not silently approximated):
    * Stock is tracked as a per-substance total; multi-well stocks with
      per-well aspiration limits are not modelled (fine for single-well /
      reservoir stocks). A multichannel draw is likewise treated as coming
      from that per-substance total, not a specific stock column.
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


def _wells_from(payload: dict[str, Any], key: str, anchor: str) -> list[str]:
    """Return the physical wells an op touches: an explicit list, or the anchor.

    A single-channel op carries no ``key`` list and falls back to just the anchor
    well, so single-channel accounting is unchanged. A multichannel op carries the
    frontend-resolved column under ``key`` — the checker has no pipette object and
    no labware geometry, so it is told the wells rather than deriving them. A range
    label (containing ``":"``) is rejected: expansion happens upstream, never here,
    which keeps the checker geometry-free.

    :param payload: The transfer_execution payload.
    :param key: ``"wells"`` (receiver) or ``"source_wells"`` (core source).
    :param anchor: The single well to fall back to when ``key`` is absent.
    :raises SimError: if the list is malformed or a label is an unexpanded range.
    """
    wells = payload.get(key)
    if wells is None:
        wells = [anchor]
    if not isinstance(wells, list) or not wells or not all(isinstance(w, str) for w in wells):
        raise SimError(f"'{key}' must be a non-empty list of well labels")
    for w in wells:
        if ":" in w:
            raise SimError(f"well range '{w}' must be expanded before the simulator")
    return wells


def _well_total(comp: dict[str, float]) -> float:
    """Total µL in a well across all substances."""
    return sum(comp.values())


def _apply_transfer(state: SimState, payload: dict[str, Any], v: StepVerdict) -> None:
    """Apply one ``transfer_execution`` payload to state, recording errors on ``v``.

    Handles both a single-well transfer and a multichannel column: the touched
    wells come from ``payload["wells"]`` / ``payload["source_wells"]`` when present
    and default to the anchor wells otherwise, so a single-channel payload behaves
    exactly as before.
    """
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

    dst_plate = receiver[0]
    if dst_plate not in state.core:
        v.errors.append(f"unknown core plate '{dst_plate}'")
        return
    try:
        dests = _wells_from(payload, "wells", receiver[1])
    except SimError as exc:
        v.errors.append(str(exc))
        return

    cap = state.cap.get(dst_plate)

    # ---- STOCK -> CORE: one aspiration of a reagent fans out over the column ----
    if len(source) == 1:
        sub = source[0]
        if sub not in state.stocks:
            v.errors.append(f"unknown stock '{sub}'")
            return
        total = amount * len(dests)
        if state.stocks[sub] - total < 0:
            v.errors.append(
                f"stock '{sub}' short: need {total:g} µL, have {state.stocks[sub]:g} µL"
            )
            return
        state.stocks[sub] -= total
        for well in dests:
            comp = state.core[dst_plate].setdefault(well, {})
            comp[sub] = comp.get(sub, 0.0) + amount
            new_total = _well_total(comp)
            if cap is not None and new_total > cap:
                v.errors.append(f"{dst_plate}·{well} overfills: {new_total:g} > {cap:g} µL")

    # ---- CORE -> CORE: paired columns, channel i moves source i into dest i ----
    elif len(source) == 2:
        s_plate = source[0]
        if s_plate not in state.core:
            v.errors.append(f"unknown core plate '{s_plate}'")
            return
        try:
            srcs = _wells_from(payload, "source_wells", source[1])
        except SimError as exc:
            v.errors.append(str(exc))
            return
        if len(srcs) != len(dests):
            v.errors.append(
                f"source has {len(srcs)} well(s) but receiver has {len(dests)}; "
                f"a column transfer pairs them one to one"
            )
            return

        # each pair is independent: a channel aspirates `amount` from its own
        # source well (carrying that well's composition) into its own dest well.
        # A short source is recorded and that pair skipped, so the report lists
        # every short well rather than stopping at the first.
        for s_well, d_well in zip(srcs, dests):
            src_comp = state.core[s_plate].get(s_well, {})
            have = _well_total(src_comp)
            if have < amount:
                v.errors.append(
                    f"{s_plate}·{s_well} short: need {amount:g} µL, have {have:g} µL"
                )
                continue
            frac = amount / have
            dst_comp = state.core[dst_plate].setdefault(d_well, {})
            for sub_name, vol in list(src_comp.items()):
                take = vol * frac
                src_comp[sub_name] = vol - take
                dst_comp[sub_name] = dst_comp.get(sub_name, 0.0) + take
            new_total = _well_total(dst_comp)
            if cap is not None and new_total > cap:
                v.errors.append(f"{dst_plate}·{d_well} overfills: {new_total:g} > {cap:g} µL")

    else:
        v.errors.append("source must be [substance] or [plate, well]")
        return


def _apply_module(state: SimState, payload: dict[str, Any], v: StepVerdict) -> None:
    """Account for a ``module_action`` step: it moves no liquid.

    Module operations (shake, latch, delay) have no effect on stock or well
    volumes, so there is nothing to check or update. Registering an explicit
    no-op keeps a legitimate module step from surfacing as an ``action not
    simulated`` warning, while an unknown action still warns.
    """
    return None


_ACCOUNTERS = {
    "transfer_execution": _apply_transfer,
    "module_action": _apply_module,
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