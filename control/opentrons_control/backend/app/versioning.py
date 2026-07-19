"""Semver auto-classification for versioned entities.

A save's bump is derived by diffing the new state against the family head, not
declared by the user, so the version number never lies. The highest changed
axis wins and lower axes reset (standard semver). For deck configs:

* major  the labware and pipette SET: which plates and pipettes exist, their
         names and types, and the pipette models. A rename counts, because
         action steps reference plates by name.
* minor  positions and offsets of the same labware.
* patch  well contents (substances, volumes) and per-well max volume.

The comparators are pure functions on two BaseConfig objects. Ordering is by
axis severity, so a change that touches several axes is classified by the
highest one (contents plus a new plate is major, not patch).
"""

from __future__ import annotations

from typing import Any

from opentrons_control.backend.app.protocol_model import BaseConfig, PlateInfo

Version = tuple[int, int, int]


def bump(head: Version, axis: str) -> Version:
    """Return the next version for the given axis, resetting the lower axes."""
    major, minor, patch = head
    if axis == "major":
        return (major + 1, 0, 0)
    if axis == "minor":
        return (major, minor + 1, 0)
    if axis == "patch":
        return (major, minor, patch + 1)
    raise ValueError(f"unknown bump axis {axis!r}")


def _offset_tuple(plate: PlateInfo) -> tuple[float, float, float]:
    o = plate.offset or {}
    return (o.get("x", 0.0), o.get("y", 0.0), o.get("z", 0.0))


def _plates(config: BaseConfig):
    """Yield (role, name, plate) across core and stock plates."""
    for role, plates in (("core", config.core_plates), ("stock", config.stock_plates)):
        for name, plate in plates.items():
            yield role, name, plate


def _labware_sig(config: BaseConfig) -> frozenset:
    """The labware/pipette SET: plate (role, name, type) plus pipette (mount, model)."""
    plates = {(role, name, plate.type) for role, name, plate in _plates(config)}
    pipettes = {("pip", mount, pip.model) for mount, pip in config.pipettes.items()}
    return frozenset(plates | pipettes)


def _position_sig(config: BaseConfig) -> frozenset:
    """Positions and offsets of the same labware."""
    return frozenset(
        (role, name, plate.place, _offset_tuple(plate))
        for role, name, plate in _plates(config)
    )


def _content_sig(config: BaseConfig) -> frozenset:
    """Well contents and per-well max volume."""

    def plate_content(plate: PlateInfo) -> tuple:
        wells = tuple(sorted(
            (well, c.substance, c.volume) for well, c in (plate.content or {}).items()
        ))
        return (plate.max_volume, wells)

    return frozenset(
        (role, name, plate_content(plate)) for role, name, plate in _plates(config)
    )


def classify_config_change(old: BaseConfig, new: BaseConfig) -> str | None:
    """Return the highest changed axis, or None if the two configs are equivalent."""
    if _labware_sig(old) != _labware_sig(new):
        return "major"
    if _position_sig(old) != _position_sig(new):
        return "minor"
    if _content_sig(old) != _content_sig(new):
        return "patch"
    return None


def next_config_version(old: BaseConfig, new: BaseConfig, head: Version) -> Version | None:
    """The version a new config save should take, or None if nothing changed."""
    axis = classify_config_change(old, new)
    return None if axis is None else bump(head, axis)


# ============================ action plans ============================
# Plans version on the same three axes, classified from the ordered step list
# (dicts, the plan wire format):
#
# * major  the step SET and what each step does: kind, substance or plates, and
#          the target wells or transfer edges. Add, remove, or change what a
#          step does and it is major.
# * minor  the order of the steps and their methods (the how).
# * patch  the volumes.


def _step_identity(step: dict) -> tuple:
    """What a step is and does, independent of order and volumes.

    For add_stock the identity includes which substance is assigned to each
    well, since a single step may now dispense different substances.
    """
    kind = step.get("kind")
    if kind == "add_stock":
        a = step.get("assignments") or {}
        return (kind, step.get("dest_plate"),
                tuple(sorted((w, (a.get(w) or {}).get("substance")) for w in step.get("wells", []))))
    return (kind, step.get("source_plate"), step.get("receiver_plate"),
            tuple(sorted((e.get("src"), e.get("dst")) for e in step.get("edges", []))))


def _how_sig(step: dict) -> tuple:
    how = dict(step.get("how") or {})
    params = how.pop("params", None) or {}
    return (tuple(sorted(how.items())), tuple(sorted(params.items())))


def _plan_identity_set(steps: list) -> list:
    """Order-independent multiset of step identities (the major axis)."""
    return sorted(_step_identity(s) for s in steps)


def _plan_order_method_sig(steps: list) -> list:
    """Ordered identities plus each step's method (the minor axis)."""
    return [(_step_identity(s), _how_sig(s)) for s in steps]


def _cell_sig(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, dict):
        if v.get("mode") == "fill_to":
            return ("fill_to", v.get("target"))
        return ("value", v.get("value"))
    return ("value", v)


def _plan_volume_sig(steps: list) -> list:
    """Per-step volumes (the patch axis)."""
    out: list = []
    for s in steps:
        if s.get("kind") == "add_stock":
            a = s.get("assignments") or {}
            out.append(tuple(sorted((w, _cell_sig((a.get(w) or {}).get("volume"))) for w in s.get("wells", []))))
        else:
            out.append(tuple(e.get("volume") for e in s.get("edges", [])))
    return out


def classify_plan_change(old_steps: list, new_steps: list) -> str | None:
    """Return the highest changed axis for a plan, or None if equivalent."""
    if _plan_identity_set(old_steps) != _plan_identity_set(new_steps):
        return "major"
    if _plan_order_method_sig(old_steps) != _plan_order_method_sig(new_steps):
        return "minor"
    if _plan_volume_sig(old_steps) != _plan_volume_sig(new_steps):
        return "patch"
    return None


def next_plan_version(old_steps: list, new_steps: list, head: Version) -> Version | None:
    """The version a plan save should take, or None if nothing changed."""
    axis = classify_plan_change(old_steps, new_steps)
    return None if axis is None else bump(head, axis)