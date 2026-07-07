"""Wire models for manual protocols and the dry-run report.

These mirror ``opentrons_drivers.common.custom_types.BaseConfig`` field for
field, expressed in pydantic so the backend can validate a UI-submitted
config and the simulator can read typed fields. The shape is the single
source of truth: the *same* config object is sent to the agent launch and
handed to the checker. Nothing here imports ``opentrons`` — ``BaseConfig``
is plain data, and the one opentrons-importing dependency (its home module)
is deliberately not reached into.

Keep this in lockstep with the agent-side ``BaseConfig`` via a shared
example fixture; the two type-expressions describe one JSON contract.

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
    """

    model: str


class BaseConfig(BaseModel):
    """Hardware/deck configuration authored in the UI.

    The one object used for both agent launch and simulation.

    :param pipettes: Mount to pipette info.
    :param core_plates: Plate name to plate info (destinations; ``tiprack_*``
        names are support labware and carry no liquid accounting).
    :param stock_plates: Plate name to plate info (sources).
    """

    pipettes: dict[str, PipetteInfo]
    core_plates: dict[str, PlateInfo]
    stock_plates: dict[str, PlateInfo]

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
    :param final_core: Core well volumes (µL) after the last step.
    """

    ok: bool
    verdicts: list[StepVerdict]
    final_stocks: dict[str, float]
    final_core: dict[str, dict[str, float]]


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