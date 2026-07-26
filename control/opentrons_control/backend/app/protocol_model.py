"""Wire models for manual protocols and the dry-run report.

These mirror ``opentrons_drivers.common.custom_types.BaseConfig`` field for
field, expressed in pydantic so the backend can validate a UI-submitted
config and the simulator can read typed fields. The shape is the single
source of truth: the *same* config object is sent to the agent launch and
handed to the checker. Nothing here imports ``opentrons`` — ``BaseConfig``
is plain data, and the one opentrons-importing dependency (its home module)
is deliberately not reached into.

Keep this in lockstep with the agent-side ``BaseConfig`` via a shared
example fixture; the two type-expressions describe one JSON contract. The
one intentional divergence is ``PipetteInfo.channels``: it is baked here so
the checker and generator can reason off-robot, and the agent ignores it
(it reads ``pipette.channels`` from the live instrument), so the agent-side
TypedDict does not carry it.

Steps stay action-agnostic (``action`` + ``payload``) so a later ``delay``
or ``pause`` is a new payload shape, not a schema change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class PlateContent(BaseModel):
    """Initial contents of a single well.

    :param substance: Substance name (stock) or label (core).
    :param volume: Starting volume in µL.
    """

    substance: str
    volume: float


class PlateInfo(BaseModel):
    """Declarative plate configuration; mirrors the agent PlateInfo.

    :param type: Labware JSON filename or built-in load name.
    :param place: Deck slot, e.g. ``"5"``.
    :param max_volume: Per-well capacity in µL; absent for tipracks.
    :param offset: x/y/z deck offset.
    :param content: Optional per-well initial fill.
    """

    type: str
    place: str
    max_volume: float | None = None
    offset: dict[str, float] = Field(default_factory=dict)
    content: dict[str, PlateContent] = Field(default_factory=dict)
    on_module: str | None = None

    @model_validator(mode="after")
    def _content_within_max(self) -> "PlateInfo":
        """Reject any well filled above the plate's set max volume."""
        if self.max_volume is not None:
            for well, c in self.content.items():
                if c.volume > self.max_volume:
                    raise ValueError(
                        f"well {well} volume {c.volume} exceeds the plate max_volume {self.max_volume}"
                    )
        return self


class PipetteInfo(BaseModel):
    """Pipette mount configuration.

    :param model: Opentrons model string, e.g. ``p300_single_gen2``.
    :param channels: Channel count (1, 8, or 96), baked at config save so the
        checker and generator can reason without a live pipette. Defaults to 1
        so configs authored before this field existed validate as single-channel.
    """

    model: str
    channels: int = 1

    @field_validator("channels")
    @classmethod
    def _known_channels(cls, v: int) -> int:
        """Reject a channel count that is not a real pipette layout."""
        if v not in (1, 8, 96):
            raise ValueError(f"channels must be 1, 8, or 96, got {v}")
        return v


class ModuleInfo(BaseModel):
    """A hardware module on the deck (heater-shaker, temperature module, etc.).

    :param type: Opentrons module load name, e.g. ``heaterShakerModuleV1``.
    :param place: Deck slot the module occupies.
    :param adapter: Optional adapter load name loaded onto the module; a
        heater-shaker requires a thermal adapter before labware can sit on it.
    """

    type: str
    place: str
    adapter: str | None = None


class BaseConfig(BaseModel):
    """Hardware/deck configuration authored in the UI.

    The one object used for both agent launch and simulation.

    :param pipettes: Mount to pipette info.
    :param core_plates: Plate name to plate info (destinations; ``tiprack_*``
        names are support labware and carry no liquid accounting).
    :param stock_plates: Plate name to plate info (sources).
    :param modules: Module name to module info (heater-shaker etc.). A plate
        placed on a module sets its ``on_module`` to the module's name.
    """

    pipettes: dict[str, PipetteInfo]
    core_plates: dict[str, PlateInfo]
    stock_plates: dict[str, PlateInfo]
    modules: dict[str, ModuleInfo] = Field(default_factory=dict)

    @field_validator("pipettes")
    @classmethod
    def _at_least_one_pipette(cls, v: dict[str, PipetteInfo]) -> dict[str, PipetteInfo]:
        """Reject a deck with no usable pipette (empty dict or all-blank models)."""
        if not any(p.model.strip() for p in v.values()):
            raise ValueError("at least one pipette must be specified")
        return v


class Step(BaseModel):
    """One protocol step; ``payload`` mirrors the agent action arg dict.

    :param action: Registry name, e.g. ``transfer_execution``.
    :param payload: Action arguments as sent to the agent. ``Any`` at the
        wire boundary is deliberate — do not narrow to an alias.
    """

    action: str
    payload: dict[str, Any]


class ManualProtocol(BaseModel):
    """A version-pinned manual protocol ready to simulate or run.

    :param name: Human-facing protocol/template name.
    :param drivers_version: ``opentrons_drivers`` wheel version this was
        authored against; the runner refuses a mismatch.
    :param config: The deck/hardware config, authored in the UI.
    :param steps: Ordered steps; row order is execution order.
    """

    name: str
    drivers_version: str
    config: BaseConfig
    steps: list[Step]


class StepVerdict(BaseModel):
    """Per-step outcome of a dry run.

    :param index: Zero-based step index.
    :param ok: True when the step raised no errors.
    :param errors: Hard failures that would abort a real run.
    :param warnings: Non-fatal notes (e.g. an unaccounted action).
    """

    index: int
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SimReport(BaseModel):
    """Whole-sequence dry-run result.

    :param ok: True when every step is ok.
    :param verdicts: Per-step verdicts in execution order.
    :param final_stocks: Stock volumes (µL) per substance after the last step.
    :param final_core: Per-well composition (substance -> µL) after the last step.
    """

    ok: bool
    verdicts: list[StepVerdict]
    final_stocks: dict[str, float]
    final_core: dict[str, dict[str, dict[str, float]]]


# ---------------------------------------------------------------------------
# Pure helpers shared by the deck-config API (kept here so referential-
# integrity logic has one home and is testable without the web layer).
# ---------------------------------------------------------------------------


def labware_wells(definition: dict[str, Any]) -> list[str]:
    """Return the well labels of a labware definition.

    :param definition: A parsed opentrons labware-definition JSON.
    :raises ValueError: If it has no non-empty ``wells`` object — the field
        the agent reads at launch, so its absence means this isn't usable
        labware.
    """
    wells = definition.get("wells")
    if not isinstance(wells, dict) or not wells:
        raise ValueError("not a labware definition: missing a non-empty 'wells' object")
    return list(wells.keys())


def custom_labware_refs(config: BaseConfig) -> set[str]:
    """Return every custom (``.json``) labware filename a config references.

    Built-in load names (non-``.json`` types, e.g. a standard tiprack) are
    excluded: the agent loads those by name and they need no library entry.

    :param config: The deck config to inspect.
    """
    refs: set[str] = set()
    for plate in (*config.core_plates.values(), *config.stock_plates.values()):
        if plate.type.endswith(".json"):
            refs.add(plate.type)
    return refs


# ---------------------------------------------------------------------------
# Pipette channel derivation and multichannel column resolution.
#
# Channels is a pure function of the pipette load name; deriving it here gives
# the deck API one place to compute it (at pipette add-time and at config save)
# and the generator a geometry-free way to expand a column. resolve_column
# mirrors the agent's on-robot resolution so the checker replays the identical
# atomic stream the agent executes.
# ---------------------------------------------------------------------------


#: Whole-token map, matched against the underscore-split load name. Whole tokens
#: (not substrings) so "8channel" never matches inside "96channel". Covers both
#: conventions: OT-2 (single/multi) and Flex (Nchannel).
_CHANNEL_TOKENS: dict[str, int] = {
    "single": 1,
    "1channel": 1,
    "multi": 8,
    "8channel": 8,
    "96channel": 96,
}


def pipette_channels(model: str) -> int:
    """Derive the channel count from an opentrons pipette load name.

    Two naming conventions are in use: OT-2 (``p300_single_gen2``,
    ``p300_multi_gen2``) and Flex (``flex_1channel_1000``, ``flex_8channel_1000``,
    ``flex_96channel_1000``). The name is split on underscores and matched against
    a fixed token map, so an unrecognised name fails loudly here rather than being
    guessed at.

    :param model: The pipette load name.
    :returns: 1, 8, or 96.
    :raises ValueError: if no channel token is present in the name.
    """
    tokens = set(model.lower().split("_"))
    for token, n in _CHANNEL_TOKENS.items():
        if token in tokens:
            return n
    raise ValueError(f"cannot determine channel count from pipette model '{model}'")


def labware_columns(definition: dict[str, Any]) -> list[list[str]]:
    """Return the labware's columns as lists of well labels, top to bottom.

    Reads the definition's ``ordering`` — the opentrons field that lists each
    column's wells in physical order — which is exactly the column membership a
    multichannel pipette needs, and gives it without an opentrons import.

    :param definition: A parsed opentrons labware-definition JSON.
    :raises ValueError: if ``ordering`` is missing or malformed.
    """
    ordering = definition.get("ordering")
    if (
        not isinstance(ordering, list)
        or not ordering
        or not all(isinstance(col, list) and col for col in ordering)
    ):
        raise ValueError("labware definition has no usable 'ordering' to derive columns from")
    return [list(col) for col in ordering]


def resolve_column(anchor: str, channels: int, definition: dict[str, Any]) -> list[str]:
    """Return the wells a pipette engages when it targets ``anchor``.

    Single-channel returns just the anchor. A multichannel returns the column
    whose head (row A) is ``anchor``; that column must hold exactly ``channels``
    wells. This mirrors the agent's on-robot resolution — same row-A anchor rule,
    same exact-length rule — so the checker replays the identical stream the agent
    executes rather than approximating it.

    :param anchor: The well targeted (a column head for a multichannel).
    :param channels: The pipette channel count.
    :param definition: The receiving/aspirating plate's labware definition.
    :returns: The engaged wells, top to bottom.
    :raises ValueError: if a multichannel anchor is not a column head, or its
        column does not hold exactly ``channels`` wells.
    """
    if channels == 1:
        return [anchor]
    for column in labware_columns(definition):
        if column[0] == anchor:
            if len(column) != channels:
                raise ValueError(
                    f"a {channels}-channel pipette needs a column of {channels} wells, "
                    f"but the column headed by {anchor} has {len(column)}"
                )
            return column
    raise ValueError(
        f"a {channels}-channel pipette must target a column head (row A); "
        f"'{anchor}' heads no column of this labware"
    )