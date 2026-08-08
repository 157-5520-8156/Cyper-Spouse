"""Hard-boundary contracts for a CharacterInterior-owned life consequence."""

from __future__ import annotations

import json

from pydantic import Field, field_validator

from .schema_core import FrozenModel, PrivacyClass


class CharacterLifeDirectionDraft(FrozenModel):
    """A freely formed subjective direction, never offered by World Author."""

    coordinate_ref: str = Field(
        pattern=r"^biography:direction\.[a-z][a-z0-9._-]{0,53}$"
    )
    summary: str = Field(min_length=1, max_length=12_000)
    context_tags: tuple[str, ...] = Field(min_length=1, max_length=16)
    replaces_context_tag_prefixes: tuple[str, ...] = Field(min_length=1, max_length=8)
    privacy_class: PrivacyClass = "personal"

    @field_validator("context_tags", "replaces_context_tag_prefixes", mode="before")
    @classmethod
    def json_arrays_are_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("context_tags")
    @classmethod
    def values_are_subjective_directions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not item.startswith("direction.") for item in value):
            raise ValueError("character direction values must use a direction.* namespace")
        return value

    @field_validator("replaces_context_tag_prefixes")
    @classmethod
    def prefixes_are_subjective_directions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not item.startswith("direction.") for item in value):
            raise ValueError(
                "character direction replacements must use a direction.* namespace"
            )
        return value


def outcome_selection_audit_text(
    *,
    candidate_result_ref: str,
    adopt_proposed_life_direction: bool,
    character_life_direction: FrozenModel | None = None,
    candidate_matrix_hash: str,
    response_hash: str,
) -> str:
    """Canonical semantic binding stored beside the accepted choice."""

    material: dict[str, object] = {
        "adopt_proposed_life_direction": adopt_proposed_life_direction,
        "candidate_matrix_hash": candidate_matrix_hash,
        "candidate_result_ref": candidate_result_ref,
        "response_hash": response_hash,
    }
    if character_life_direction is not None:
        material["character_life_direction"] = character_life_direction.model_dump(
            mode="json", exclude={"descriptor_hash"}
        )
    return json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = ["CharacterLifeDirectionDraft", "outcome_selection_audit_text"]
