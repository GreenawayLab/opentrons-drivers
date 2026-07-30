"""Expand a plan's steps into the atomic transfer_execution stream.

The plan stores intent (add_stock and move_core with wells, edges, and volume
cells). The simulator and the agent consume atomic transfer_execution steps.
This generator is the bridge: it walks the ordered steps, resolves fill_to
against a running per-well volume seeded from the config, and emits one transfer
per receiver well. Wells or edges with no volume yet are reported as incomplete
rather than emitted, so the checker can tell unfinished from wrong.

fill_to resolves to target minus the running volume in that well at the point
the step runs, so ordering is load bearing. A negative result (the well is
already at or above the target) is emitted as is and caught downstream by the
simulator's amount > 0 rule, keeping one source of truth for validity.

Multichannel: the channel count comes from the pinned config
(``config.pipettes[mount].channels``, baked at save), and the column membership
from the receiving/aspirating plate's labware definition (``labware_defs``). A
step whose pipette has channels > 1 emits, per selected column head, one transfer
carrying the resolved ``wells`` (and ``source_wells`` for a core move) so the
checker sees the exact column the agent will drive. The running ledger fans out
over the whole column so fill_to ordering stays correct. fill_to is refused with
a multichannel, since a column takes one uniform amount, not a per-well top-up.
"""

from __future__ import annotations

import re
from typing import Any

from opentrons_control.backend.app.protocol_model import (
    BaseConfig,
    ManualProtocol,
    Step,
    resolve_column,
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


def _ordered_wells(wells: list[str], order: dict[str, Any] | None) -> list[str]:
    """Return an add_stock step's wells in execution order.

    ``order`` is the ordering descriptor ``{axis, reverse, custom}``. When custom,
    the stored well list *is* the order (an arbitrary hand-picked sequence, e.g.
    from ctrl-click selection or a JSON paste), so it is walked untouched.
    Otherwise the wells are sorted lexicographically by the chosen axis
    (``"row"`` = row-major, ``"col"`` = column-major) and direction. Ordering is
    load bearing: fill_to resolves against the running ledger at the point a well
    is filled, so traversal is a semantic input, not presentation. An absent
    descriptor is row-major forward, so plans authored before ordering existed
    expand identically.

    :param wells: The step's selected wells, in selection order.
    :param order: The ordering descriptor, or None for the default.
    :returns: The wells in the order transfers should be emitted.
    """
    order = order or {}
    if order.get("custom"):
        return list(wells)
    axis = order.get("axis", "row")
    reverse = bool(order.get("reverse", False))

    def key(w: str) -> tuple[Any, Any]:
        m = re.fullmatch(r"([A-Za-z]+)(\d+)", w)
        if m is None:
            return (w, 0)
        row, col = m.group(1), int(m.group(2))
        return (row, col) if axis == "row" else (col, row)

    return sorted(wells, key=key, reverse=reverse)


def plan_to_protocol(
    config: BaseConfig,
    steps: list[dict[str, Any]],
    name: str = "check",
    drivers_version: str = "check",
    labware_defs: dict[str, dict[str, Any]] | None = None,  # deprecated: ignored, columns derive from the well label
) -> tuple[ManualProtocol, list[str], list[str]]:
    """Expand plan steps into a transfer_execution protocol plus incomplete notes.

    :param config: The pinned deck config (plates, stock content, capacities,
        pipette channel counts).
    :param steps: The plan's ordered step envelopes (add_stock or move_core).
    :param labware_defs: plate name -> labware definition, needed only to resolve
        multichannel columns; single-channel plans work without it.
    :returns: A ManualProtocol of atomic transfers, a list of incomplete notes
        (wells or transfers with no substance or volume yet), and a list of hard
        generation errors (a fill_to below the well's current volume, an
        unresolvable column, or fill_to under a multichannel pipette).
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

        # module ops carry no transfers: emit one module_action and move on. The
        # module method name and its params ride in `how`, mirroring how a
        # transfer step carries its liquid method. Reserved keys are written after
        # the params spread so a stray param cannot shadow them.
        if kind == "module_op":
            module = step.get("module")
            method = how.get("method")
            params = how.get("params") or {}
            if not module:
                incomplete.append(f"step {i + 1}: module op has no module")
            elif not method:
                incomplete.append(f"step {i + 1}: module op has no method")
            else:
                out.append(Step(
                    action="module_action",
                    payload={**params, "module": module, "method": method},
                ))
            continue

        method = how.get("method") or DEFAULT_METHOD
        params = how.get("params") or {}
        pipette = how.get("pipette") or "auto"
        tip_policy = how.get("tip") or "fresh"
        return_tip = bool(how.get("return_tip", False))

        # resolve the pipette and its channel count up front: the channel count
        # drives both the column fan-out and the fill_to guard below.
        mount = default_mount if pipette == "auto" else pipette
        pip_info = config.pipettes.get(mount)
        channels = pip_info.channels if pip_info is not None else 1

        # each transfer: (source, receiver, amount, recv_wells, src_wells|None)
        transfers: list[tuple[list[Any], list[Any], float, list[str], list[str] | None]] = []

        if kind == "add_stock":
            plate = step.get("dest_plate")
            assignments = step.get("assignments") or {}
            running = core.setdefault(plate, {})
            for well in _ordered_wells(step.get("wells") or [], step.get("order")):
                a = assignments.get(well) or {}
                substance = a.get("substance")
                cell = a.get("volume")
                if not substance:
                    incomplete.append(f"step {i + 1}: {plate}/{well} has no substance")
                    continue
                if cell is None:
                    incomplete.append(f"step {i + 1}: {plate}/{well} has no volume")
                    continue

                is_fill_to = isinstance(cell, dict) and cell.get("mode") == "fill_to"
                if is_fill_to and channels > 1:
                    errors.append(
                        f"step {i + 1}: {plate}/{well} uses fill_to with a {channels}-channel "
                        f"pipette, but a column takes one uniform amount, not a per-well top-up"
                    )
                    continue

                if is_fill_to:
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

                try:
                    recv_wells = resolve_column(well, channels)
                except ValueError as exc:
                    errors.append(f"step {i + 1}: {exc}")
                    continue

                transfers.append(([substance], [plate, well], amount, recv_wells, None))
                for w in recv_wells:
                    running[w] = running.get(w, 0.0) + amount

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

                try:
                    recv_wells = resolve_column(dst, channels)
                    src_wells = resolve_column(src, channels)
                except ValueError as exc:
                    errors.append(f"step {i + 1}: {exc}")
                    continue
                if len(recv_wells) != len(src_wells):
                    errors.append(
                        f"step {i + 1}: source column ({len(src_wells)}) and receiver column "
                        f"({len(recv_wells)}) differ in length"
                    )
                    continue

                transfers.append(([s_plate, src], [r_plate, dst], amount, recv_wells, src_wells))
                for w in recv_wells:
                    r_running[w] = r_running.get(w, 0.0) + amount
                for w in src_wells:
                    s_running[w] = s_running.get(w, 0.0) - amount

        # tag the step's transfers to the agent's transfer_execution contract:
        # method by name, tip_cycle as [pickup, drop], a resolved pipette_mount,
        # and the method hyperparameters spread as top-level extras (the agent
        # collects non-reserved top-level keys and passes them to the method).
        # Reserved keys are written last so a stray param cannot shadow them.
        # For a multichannel op, the resolved column travels as `wells` (and
        # `source_wells` for a core move) so the checker sees the exact column;
        # single-channel emits neither, keeping those payloads byte-identical.
        flags = _tip_flags(len(transfers), tip_policy)
        for (source, receiver, amount, recv_wells, src_wells), flag in zip(transfers, flags):
            payload: dict[str, Any] = {
                **params,
                "source": source,
                "receiver": receiver,
                "amount": amount,
                "method": method,
                "pipette_mount": mount,
                "tip_cycle": [flag["pickup"], flag["drop"]],
                "return_tip": return_tip,
            }
            if channels > 1:
                payload["wells"] = recv_wells
                if src_wells is not None:
                    payload["source_wells"] = src_wells
            out.append(Step(action="transfer_execution", payload=payload))

    return ManualProtocol(name=name, drivers_version=drivers_version, config=config, steps=out), incomplete, errors