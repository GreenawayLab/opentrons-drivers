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