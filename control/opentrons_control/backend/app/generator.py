"""Expand a plan's steps into the atomic transfer_execution stream.

The plan stores intent (add_stock and move_core with wells, edges, and volume
cells). The simulator and the agent consume atomic transfer_execution steps
(one source, one receiver, one amount). This generator is the bridge: it walks
the ordered steps, resolves fill_to against a running per-well volume seeded
from the config, and emits one transfer per receiver well. Wells or edges with
no volume yet are reported as incomplete rather than emitted, so the checker
can tell unfinished from wrong.

fill_to resolves to target minus the running volume in that well at the point
the step runs, so ordering is load bearing. A negative result (the well is
already at or above the target) is emitted as is and caught downstream by the
simulator's amount > 0 rule, keeping one source of truth for validity.
"""

from __future__ import annotations

from typing import Any

from opentrons_control.backend.app.protocol_model import (
    BaseConfig,
    ManualProtocol,
    Step,
)


DEFAULT_METHOD = "basic_liquid_transfer"


def _tip_flags(n: int, policy: str) -> list[dict[str, bool]]:
    """Per-transfer pickup/drop flags for a step's tip policy.

    reuse picks up once on the first transfer and drops on the last, holding the
    tip between. fresh uses a new tip for every transfer. A single-transfer step
    picks up and drops on that one transfer under either policy.
    """
    if policy == "reuse":
        return [{"pickup": i == 0, "drop": i == n - 1} for i in range(n)]
    return [{"pickup": True, "drop": True} for _ in range(n)]


def _seed_core(config: BaseConfig) -> dict[str, dict[str, float]]:
    """Running per-well volume for core plates, seeded from authored content."""
    core: dict[str, dict[str, float]] = {}
    for name, plate in config.core_plates.items():
        core[name] = {w: c.volume for w, c in plate.content.items()}
    return core


def plan_to_protocol(
    config: BaseConfig,
    steps: list[dict[str, Any]],
    name: str = "check",
    drivers_version: str = "check",
) -> tuple[ManualProtocol, list[str], list[str]]:
    """Expand plan steps into a transfer_execution protocol plus incomplete notes.

    :param config: The pinned deck config (plates, stock content, capacities).
    :param steps: The plan's ordered step envelopes (add_stock or move_core).
    :returns: A ManualProtocol of atomic transfers, a list of incomplete notes
        (wells or transfers with no substance or volume yet), and a list of hard
        generation errors (a fill_to below the well's current volume).
    """
    core = _seed_core(config)
    out: list[Step] = []
    incomplete: list[str] = []
    errors: list[str] = []

    # "auto" pipette resolves to the first configured mount. Volume-driven
    # selection across two pipettes is not modelled yet, so a two-pipette config
    # always lands on the first mount until that logic exists.
    default_mount = next(iter(config.pipettes), "left")

    for i, step in enumerate(steps):
        kind = step.get("kind")
        how = step.get("how") or {}
        method = how.get("method") or DEFAULT_METHOD
        params = how.get("params") or {}
        pipette = how.get("pipette") or "auto"
        tip_policy = how.get("tip") or "fresh"

        # collect this step's transfers as (source, receiver, amount), updating
        # the running ledger as we go so fill_to resolves against live volumes
        transfers: list[tuple[list[Any], list[Any], float]] = []

        if kind == "add_stock":
            plate = step.get("dest_plate")
            assignments = step.get("assignments") or {}
            running = core.setdefault(plate, {})
            for well in step.get("wells") or []:
                a = assignments.get(well) or {}
                substance = a.get("substance")
                cell = a.get("volume")
                if not substance:
                    incomplete.append(f"step {i + 1}: {plate}/{well} has no substance")
                    continue
                if cell is None:
                    incomplete.append(f"step {i + 1}: {plate}/{well} has no volume")
                    continue
                if isinstance(cell, dict) and cell.get("mode") == "fill_to":
                    target = cell.get("target")
                    if target is None:
                        incomplete.append(f"step {i + 1}: {plate}/{well} fill_to has no target")
                        continue
                    current = running.get(well, 0.0)
                    amount = float(target) - current
                    if amount < 0:
                        errors.append(
                            f"step {i + 1}: fill_to {float(target):g} µL in {plate}/{well} is below its "
                            f"current {current:g} µL, so it would need to remove liquid"
                        )
                        continue
                else:
                    amount = float(cell.get("value") if isinstance(cell, dict) else cell)
                transfers.append(([substance], [plate, well], amount))
                running[well] = running.get(well, 0.0) + amount

        elif kind == "move_core":
            s_plate = step.get("source_plate")
            r_plate = step.get("receiver_plate")
            s_running = core.setdefault(s_plate, {})
            r_running = core.setdefault(r_plate, {})
            for edge in step.get("edges") or []:
                vol = edge.get("volume")
                src, dst = edge.get("src"), edge.get("dst")
                if vol is None:
                    incomplete.append(f"step {i + 1}: transfer {src} to {dst} has no volume")
                    continue
                amount = float(vol)
                transfers.append(([s_plate, src], [r_plate, dst], amount))
                r_running[dst] = r_running.get(dst, 0.0) + amount
                s_running[src] = s_running.get(src, 0.0) - amount

        # tag the step's transfers to the agent's transfer_execution contract:
        # method by name, tip_cycle as [pickup, drop], a resolved pipette_mount,
        # and the method hyperparameters spread as top-level extras (the agent
        # collects non-reserved top-level keys and passes them to the method).
        # Reserved keys are written last so a stray param cannot shadow them.
        flags = _tip_flags(len(transfers), tip_policy)
        mount = default_mount if pipette == "auto" else pipette
        for (source, receiver, amount), flag in zip(transfers, flags):
            out.append(Step(
                action="transfer_execution",
                payload={
                    **params,
                    "source": source,
                    "receiver": receiver,
                    "amount": amount,
                    "method": method,
                    "pipette_mount": mount,
                    "tip_cycle": [flag["pickup"], flag["drop"]],
                },
            ))

    return ManualProtocol(name=name, drivers_version=drivers_version, config=config, steps=out), incomplete, errors