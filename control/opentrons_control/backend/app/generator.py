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
) -> tuple[ManualProtocol, list[str]]:
    """Expand plan steps into a transfer_execution protocol plus incomplete notes.

    :param config: The pinned deck config (plates, stock content, capacities).
    :param steps: The plan's ordered step envelopes (add_stock or move_core).
    :returns: A ManualProtocol of atomic transfers, and a list of human-readable
        notes for wells or edges that have no volume yet (the incomplete set).
    """
    core = _seed_core(config)
    out: list[Step] = []
    incomplete: list[str] = []

    for i, step in enumerate(steps):
        kind = step.get("kind")

        if kind == "add_stock":
            substance = step.get("substance")
            plate = step.get("dest_plate")
            vols = step.get("volumes") or {}
            running = core.setdefault(plate, {})
            for well in step.get("wells") or []:
                cell = vols.get(well)
                if cell is None:
                    incomplete.append(f"step {i + 1}: {plate}/{well} has no volume")
                    continue
                if isinstance(cell, dict) and cell.get("mode") == "fill_to":
                    target = cell.get("target")
                    if target is None:
                        incomplete.append(f"step {i + 1}: {plate}/{well} fill_to has no target")
                        continue
                    amount = float(target) - running.get(well, 0.0)
                else:
                    amount = float(cell.get("value") if isinstance(cell, dict) else cell)
                out.append(Step(
                    action="transfer_execution",
                    payload={"source": [substance], "receiver": [plate, well], "amount": amount},
                ))
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
                out.append(Step(
                    action="transfer_execution",
                    payload={"source": [s_plate, src], "receiver": [r_plate, dst], "amount": amount},
                ))
                r_running[dst] = r_running.get(dst, 0.0) + amount
                s_running[src] = s_running.get(src, 0.0) - amount

    return ManualProtocol(name=name, drivers_version=drivers_version, config=config, steps=out), incomplete