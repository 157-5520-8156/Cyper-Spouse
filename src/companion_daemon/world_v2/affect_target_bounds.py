"""Pinned numeric authority for model-authored Affect targets.

The character model owns whether an Affect component exists and which legal
target it selects.  This module exposes only the lower bound that the installed
projection/reducer contracts can accept at one exact ledger cursor.  It never
clamps a target or selects an emotion on the model's behalf.
"""

from __future__ import annotations

from typing import Literal, Mapping, Sequence

from pydantic import Field, model_validator

from .schema_core import FrozenModel
from .schemas import LedgerProjection


AffectDimension = Literal[
    "hurt",
    "anger",
    "sadness",
    "loneliness",
    "anxiety",
    "resentment",
    "warmth",
    "joy",
]

AFFECT_DIMENSIONS: tuple[AffectDimension, ...] = (
    "anger",
    "anxiety",
    "hurt",
    "joy",
    "loneliness",
    "resentment",
    "sadness",
    "warmth",
)

# These selectors are the exact policies emitted by both current Affect
# adapters. Keeping their installed numbers beside the provider contract
# prevents the prompt and compiler from drifting apart.
STANDARD_DECAY_OBJECT_REF = "policy:decay:standard"
STANDARD_DECAY_SCHEMA_VERSION = "affect-decay.1"
STANDARD_DECAY_HALF_LIFE_SECONDS = 3_600
STANDARD_DECAY_FLOOR_BP = 300
STANDARD_DECAY_DELAY_SECONDS = 120
STANDARD_RESIDUE_OBJECT_REF = "policy:residue:standard"
STANDARD_RESIDUE_SCHEMA_VERSION = "affect-residue.1"
STANDARD_RESIDUE_BP = 500


class AffectTargetDimensionLowerBound(FrozenModel):
    """One dimension's reducer-acceptable minimum at the pinned cursor."""

    dimension: AffectDimension
    baseline_bp: int = Field(ge=0, le=10_000)
    installed_decay_floor_bp: int = Field(ge=0, le=10_000)
    installed_residue_bp: int = Field(ge=0, le=10_000)
    minimum_target_intensity_bp: int = Field(ge=0, le=10_000)
    baseline_calibration_revision: int | None = Field(default=None, ge=0)
    baseline_policy_version: str | None = Field(default=None, min_length=1, max_length=128)
    baseline_basis_hash: str | None = Field(default=None, min_length=64, max_length=64)

    @model_validator(mode="after")
    def minimum_and_baseline_source_are_exact(self) -> "AffectTargetDimensionLowerBound":
        expected = max(
            self.baseline_bp,
            self.installed_decay_floor_bp,
            self.installed_residue_bp,
        )
        if self.minimum_target_intensity_bp != expected:
            raise ValueError("affect target minimum does not match its numeric bases")
        source = (
            self.baseline_calibration_revision,
            self.baseline_policy_version,
            self.baseline_basis_hash,
        )
        if any(value is None for value in source) and any(value is not None for value in source):
            raise ValueError("affect baseline source binding is partial")
        if self.baseline_bp and all(value is None for value in source):
            raise ValueError("a nonzero affect baseline requires its projection source binding")
        return self


class AffectTargetLowerBounds(FrozenModel):
    """Complete lower-bound manifest bound to one exact ModelInput cursor."""

    contract_version: Literal["affect-target-lower-bounds.1"] = (
        "affect-target-lower-bounds.1"
    )
    source_world_revision: int = Field(ge=0)
    source_deliberation_revision: int = Field(ge=0)
    source_ledger_sequence: int = Field(ge=0)
    bounds: tuple[AffectTargetDimensionLowerBound, ...] = Field(
        min_length=len(AFFECT_DIMENSIONS),
        max_length=len(AFFECT_DIMENSIONS),
    )

    @model_validator(mode="after")
    def covers_each_supported_dimension_once(self) -> "AffectTargetLowerBounds":
        dimensions = tuple(item.dimension for item in self.bounds)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("affect target lower bounds repeat a dimension")
        if set(dimensions) != set(AFFECT_DIMENSIONS):
            raise ValueError("affect target lower bounds must cover every supported dimension")
        return self

    def minimum_for(self, dimension: str) -> int:
        match = next((item for item in self.bounds if item.dimension == dimension), None)
        if match is None:
            raise ValueError(f"affect target bound is missing dimension={dimension}")
        return match.minimum_target_intensity_bp


class AffectTargetBelowMinimumError(ValueError):
    """A model selected a numeric target the pinned reducers cannot accept."""

    def __init__(self, *, dimension: str, selected: int, minimum: int) -> None:
        self.dimension = dimension
        self.selected = selected
        self.minimum = minimum
        super().__init__(
            "affect_target_below_pinned_minimum:"
            f"dimension={dimension}:selected={selected}:minimum={minimum}"
        )


def lower_bounds_from_projection(projection: LedgerProjection) -> AffectTargetLowerBounds:
    """Derive the complete standard-policy manifest from an exact projection."""

    baselines = {item.dimension: item for item in projection.affect_baselines}
    bounds: list[AffectTargetDimensionLowerBound] = []
    for dimension in AFFECT_DIMENSIONS:
        baseline = baselines.get(dimension)
        baseline_bp = baseline.baseline_bp if baseline is not None else 0
        bounds.append(
            AffectTargetDimensionLowerBound(
                dimension=dimension,
                baseline_bp=baseline_bp,
                installed_decay_floor_bp=STANDARD_DECAY_FLOOR_BP,
                installed_residue_bp=STANDARD_RESIDUE_BP,
                minimum_target_intensity_bp=max(
                    baseline_bp,
                    STANDARD_DECAY_FLOOR_BP,
                    STANDARD_RESIDUE_BP,
                ),
                baseline_calibration_revision=(
                    baseline.calibration_revision if baseline is not None else None
                ),
                baseline_policy_version=(
                    baseline.policy_version if baseline is not None else None
                ),
                baseline_basis_hash=(
                    baseline.last_calibration_basis_hash if baseline is not None else None
                ),
            )
        )
    return AffectTargetLowerBounds(
        source_world_revision=projection.world_revision,
        source_deliberation_revision=projection.deliberation_revision,
        source_ledger_sequence=projection.ledger_sequence,
        bounds=tuple(bounds),
    )


def validate_model_authored_targets(
    components: Sequence[Mapping[str, object]],
    bounds: AffectTargetLowerBounds | None,
) -> None:
    """Reject, but never alter, a target below its pinned effective minimum."""

    if bounds is None:
        # Historical/offline ModelInput values predate this provider contract.
        # Their immutable delta/replay path remains governed by the compiler.
        return
    for component in components:
        dimension = component.get("dimension")
        selected = component.get("target_intensity_bp")
        if not isinstance(dimension, str) or isinstance(selected, bool) or not isinstance(
            selected, int
        ):
            continue
        minimum = bounds.minimum_for(dimension)
        if selected < minimum:
            raise AffectTargetBelowMinimumError(
                dimension=dimension,
                selected=selected,
                minimum=minimum,
            )


def target_reselection_instruction(error: AffectTargetBelowMinimumError) -> str:
    """Name only the hard numeric failure and return choice to the same model."""

    return (
        "The previous complete JSON selected an Affect target outside the pinned numeric "
        "authority: "
        f"dimension={error.dimension}, selected={error.selected}, minimum={error.minimum}. "
        "Using the original pinned context, return one complete replacement JSON object. "
        "You own whether to choose no_change, another legal dimension, or any target at or "
        "above that dimension's supplied minimum. Do not merely clamp the old number. "
        "Return JSON only."
    )


__all__ = [
    "AFFECT_DIMENSIONS",
    "AffectDimension",
    "AffectTargetBelowMinimumError",
    "AffectTargetDimensionLowerBound",
    "AffectTargetLowerBounds",
    "STANDARD_DECAY_DELAY_SECONDS",
    "STANDARD_DECAY_FLOOR_BP",
    "STANDARD_DECAY_HALF_LIFE_SECONDS",
    "STANDARD_DECAY_OBJECT_REF",
    "STANDARD_DECAY_SCHEMA_VERSION",
    "STANDARD_RESIDUE_BP",
    "STANDARD_RESIDUE_OBJECT_REF",
    "STANDARD_RESIDUE_SCHEMA_VERSION",
    "lower_bounds_from_projection",
    "target_reselection_instruction",
    "validate_model_authored_targets",
]
