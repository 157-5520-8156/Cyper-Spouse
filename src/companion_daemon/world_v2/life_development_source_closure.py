"""Independent semantic truth closure for model-authored life development.

The World Author remains free to invent proposal-scoped environmental
possibilities.  This module has a deliberately narrower authority: it checks
only whether material presented as *existing* World truth is entailed by the
exact cited sources, whether factual prose escaped the claim declarations, and
whether an executable typed location contradicts the proposal it coordinates.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from .life_development_draft import (
    LifeDevelopmentCapabilityManifest,
    LifeDevelopmentPossibilityDraft,
)
from .schema_core import FrozenModel
from .schemas import WorldEvent


_REVIEW_CONTRACT = "life-development-source-closure-review.1"
_NOVEL_ORIGIN_CONTRACT = "life-development-novel-origin-review.2"


def _canonicalize_unique_string_set(value: object) -> object:
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        if len(value) != len(set(value)):
            raise ValueError("source-closure coordinates must be unique")
        return tuple(sorted(set(value)))
    return value


class LifeDevelopmentTypedLocationConflict(FrozenModel):
    """One exact prose coordinate that contradicts the typed execution place."""

    typed_location_ref: str = Field(min_length=1, max_length=512)
    prose_path: str = Field(min_length=1, max_length=256)
    conflicting_fragment: str = Field(min_length=1, max_length=1_000)


class LifeDevelopmentSourceClosureReview(FrozenModel):
    """One model-authored semantic adjudication with deterministic coordinates."""

    decision: Literal["supported", "unsupported"]
    unsupported_claim_ids: tuple[str, ...] = Field(default=(), max_length=24)
    undeclared_fact_fragments: tuple[str, ...] = Field(default=(), max_length=32)
    undeclared_fact_paths: tuple[str, ...] = Field(default=(), max_length=32)
    typed_location_conflicts: tuple[LifeDevelopmentTypedLocationConflict, ...] = Field(
        default=(),
        max_length=8,
    )
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator(
        "unsupported_claim_ids",
        "undeclared_fact_fragments",
        "undeclared_fact_paths",
        mode="before",
    )
    @classmethod
    def canonicalize_coordinates(cls, value: object) -> object:
        return _canonicalize_unique_string_set(value)

    @field_validator("typed_location_conflicts", mode="before")
    @classmethod
    def tupleize_location_coordinates(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def decision_matches_coordinates(self) -> "LifeDevelopmentSourceClosureReview":
        if any(
            not fragment.strip() for fragment in self.undeclared_fact_fragments
        ):
            raise ValueError("undeclared fact fragments cannot be blank")
        conflict_keys = tuple(
            (
                conflict.typed_location_ref,
                conflict.prose_path,
                conflict.conflicting_fragment,
            )
            for conflict in self.typed_location_conflicts
        )
        if len(conflict_keys) != len(set(conflict_keys)):
            raise ValueError("typed-location conflict coordinates must be unique")
        coordinates = (
            self.unsupported_claim_ids,
            self.undeclared_fact_fragments,
            self.undeclared_fact_paths,
            self.typed_location_conflicts,
        )
        if self.decision == "supported" and any(coordinates):
            raise ValueError("supported review cannot carry rejection coordinates")
        if self.decision == "unsupported" and not any(coordinates):
            raise ValueError("unsupported review requires at least one exact coordinate")
        return self


NovelOriginViolationKind = Literal[
    "retroactive_relationship_or_shared_history",
    "completed_character_experience",
    "existing_entity_or_fact_masquerading_as_novel",
    "imported_current_or_prior_prerequisite",
]


class LifeDevelopmentNovelOriginClaimFinding(FrozenModel):
    """One exact novel-claim coordinate rejected by the focused critic."""

    claim_id: str = Field(min_length=1, max_length=128)
    violation_kinds: tuple[NovelOriginViolationKind, ...] = Field(
        min_length=1,
        max_length=4,
    )
    exact_fragments: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("violation_kinds", "exact_fragments", mode="before")
    @classmethod
    def canonicalize_coordinates(cls, value: object) -> object:
        return _canonicalize_unique_string_set(value)

    @model_validator(mode="after")
    def coordinates_are_nonempty(self) -> "LifeDevelopmentNovelOriginClaimFinding":
        if any(not item.strip() for item in self.exact_fragments):
            raise ValueError("novel-origin claim fragments cannot be blank")
        return self


class LifeDevelopmentNovelOriginNpcFinding(FrozenModel):
    """One exact provisional-NPC coordinate rejected by the focused critic."""

    local_ref: str = Field(min_length=1, max_length=80)
    violation_kinds: tuple[NovelOriginViolationKind, ...] = Field(
        min_length=1,
        max_length=4,
    )
    exact_fragments: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("violation_kinds", "exact_fragments", mode="before")
    @classmethod
    def canonicalize_coordinates(cls, value: object) -> object:
        return _canonicalize_unique_string_set(value)

    @model_validator(mode="after")
    def coordinates_are_nonempty(self) -> "LifeDevelopmentNovelOriginNpcFinding":
        if any(not item.strip() for item in self.exact_fragments):
            raise ValueError("novel-origin NPC fragments cannot be blank")
        return self


class LifeDevelopmentOutcomePrerequisiteFinding(FrozenModel):
    """One exact outcome fragment that imports truth from before its branch."""

    prose_path: str = Field(
        pattern=r"^outcomes\.(0|[1-9][0-9]*)\.text$",
        max_length=256,
    )
    violation_kinds: tuple[NovelOriginViolationKind, ...] = Field(
        min_length=1,
        max_length=4,
    )
    exact_fragments: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("violation_kinds", "exact_fragments", mode="before")
    @classmethod
    def canonicalize_coordinates(cls, value: object) -> object:
        return _canonicalize_unique_string_set(value)

    @model_validator(mode="after")
    def coordinates_are_nonempty(
        self,
    ) -> "LifeDevelopmentOutcomePrerequisiteFinding":
        if any(not item.strip() for item in self.exact_fragments):
            raise ValueError("outcome-prerequisite fragments cannot be blank")
        external_origin_kinds = {
            "retroactive_relationship_or_shared_history",
            "existing_entity_or_fact_masquerading_as_novel",
            "imported_current_or_prior_prerequisite",
        }
        if not external_origin_kinds.intersection(self.violation_kinds):
            raise ValueError(
                "outcome-prerequisite findings must identify truth imported from "
                "outside the current proposal branch"
            )
        return self


class LifeDevelopmentNovelOriginReview(FrozenModel):
    """Independent model verdict over novel fact origin, not story quality."""

    decision: Literal["supported", "unsupported"]
    unsupported_claims: tuple[LifeDevelopmentNovelOriginClaimFinding, ...] = Field(
        default=(),
        max_length=24,
    )
    unsupported_provisional_npcs: tuple[
        LifeDevelopmentNovelOriginNpcFinding,
        ...,
    ] = Field(default=(), max_length=16)
    unsupported_outcome_prerequisites: tuple[
        LifeDevelopmentOutcomePrerequisiteFinding,
        ...,
    ] = Field(default=(), max_length=8)
    # Kept as an explicit empty legacy slot so committed `.1` supported reviews
    # remain decodable. Current-premise coverage belongs exclusively to the
    # general source reviewer in `.2`.
    undeclared_premise_fragments: tuple[str, ...] = Field(default=(), max_length=0)
    reason: str = Field(min_length=1, max_length=2_000)

    @field_validator(
        "unsupported_claims",
        "unsupported_provisional_npcs",
        "unsupported_outcome_prerequisites",
        mode="before",
    )
    @classmethod
    def tupleize_findings(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("undeclared_premise_fragments", mode="before")
    @classmethod
    def canonicalize_premise_coordinates(cls, value: object) -> object:
        return _canonicalize_unique_string_set(value)

    @model_validator(mode="after")
    def decision_matches_coordinates(self) -> "LifeDevelopmentNovelOriginReview":
        claim_ids = tuple(item.claim_id for item in self.unsupported_claims)
        npc_refs = tuple(item.local_ref for item in self.unsupported_provisional_npcs)
        outcome_paths = tuple(
            item.prose_path for item in self.unsupported_outcome_prerequisites
        )
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError("novel-origin claim findings must be unique")
        if len(npc_refs) != len(set(npc_refs)):
            raise ValueError("novel-origin NPC findings must be unique")
        if len(outcome_paths) != len(set(outcome_paths)):
            raise ValueError("outcome-prerequisite findings must use unique paths")
        coordinates = (
            self.unsupported_claims,
            self.unsupported_provisional_npcs,
            self.unsupported_outcome_prerequisites,
        )
        if self.decision == "supported" and any(coordinates):
            raise ValueError("supported novel-origin review cannot carry coordinates")
        if self.decision == "unsupported" and not any(coordinates):
            raise ValueError("unsupported novel-origin review requires exact coordinates")
        return self


class LifeDevelopmentSourceClosureError(ValueError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        violations: tuple[dict[str, str], ...] = (),
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.violations = violations


def _unwrap_review_transport_envelope(
    decoded: dict[str, object],
    *,
    error_code: str,
    review_label: str,
) -> dict[str, object]:
    """Decode the current strict-provider envelope without breaking old bytes."""

    if set(decoded) != {"review"}:
        return decoded
    review = decoded["review"]
    if not isinstance(review, dict):
        raise LifeDevelopmentSourceClosureError(
            error_code,
            f"{review_label} transport envelope must contain one review object",
        )
    return review


def _review_output_contract(
    *,
    contract: str,
    review_model: type[FrozenModel],
) -> dict[str, object]:
    """Describe the transport envelope and the semantic verdict invariant."""

    return {
        "contract": contract,
        "transport_envelope": {
            "required_root_key": "review",
            "additional_root_fields": False,
        },
        "review_schema": review_model.model_json_schema(mode="validation"),
        "decision_coordinate_authority": {
            "supported": "all_rejection_coordinate_arrays_empty",
            "unsupported": "at_least_one_rejection_coordinate_array_non_empty",
        },
    }


def parse_life_development_source_closure_review(
    *,
    raw: str,
    draft: LifeDevelopmentPossibilityDraft,
) -> LifeDevelopmentSourceClosureReview:
    """Decode one bounded reviewer result without inventing a local verdict."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16_000:
        raise LifeDevelopmentSourceClosureError(
            "invalid_source_closure_output",
            "source-closure review must be bounded JSON text",
        )
    json_text = raw.strip()
    if json_text.startswith("```") and json_text.endswith("```"):
        first_newline = json_text.find("\n")
        opening = json_text[:first_newline].strip().casefold()
        if first_newline > 0 and opening in {"```", "```json"}:
            json_text = json_text[first_newline + 1 : -3].strip()
    try:
        decoded = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise LifeDevelopmentSourceClosureError(
            "invalid_source_closure_json",
            "source-closure review is not valid JSON",
        ) from exc
    if not isinstance(decoded, dict):
        raise LifeDevelopmentSourceClosureError(
            "invalid_source_closure_shape",
            "source-closure review must be one JSON object",
        )
    review_value = _unwrap_review_transport_envelope(
        decoded,
        error_code="invalid_source_closure_shape",
        review_label="source-closure review",
    )
    try:
        review = LifeDevelopmentSourceClosureReview.model_validate_json(
            json.dumps(review_value, ensure_ascii=False, separators=(",", ":"))
        )
    except ValueError as exc:
        detail = "source-closure review violates its exact output contract"
        structured: tuple[dict[str, str], ...] = ()
        if isinstance(exc, ValidationError):
            violations = []
            machine = []
            for error in exc.errors(include_url=False, include_input=False):
                location = ".".join(str(part) for part in error["loc"]) or "<root>"
                violations.append(f"{location}: {error['msg']} [{error['type']}]")
                machine.append(
                    {
                        "path": location,
                        "message": str(error["msg"]),
                        "type": str(error["type"]),
                    }
                )
            if violations:
                detail = f"{detail}: {'; '.join(violations)}"
                structured = tuple(machine)
        raise LifeDevelopmentSourceClosureError(
            "invalid_source_closure_shape",
            detail[:8_000],
            violations=structured,
        ) from exc
    declared = {
        claim.claim_id
        for claim in draft.claim_declarations
        if claim.scope == "existing_world"
    }
    unknown = tuple(sorted(set(review.unsupported_claim_ids) - declared))
    if unknown:
        raise LifeDevelopmentSourceClosureError(
            "unknown_source_closure_claim",
            "unsupported_claim_ids contains ids absent from the reviewed draft: "
            + ", ".join(unknown),
        )
    prose = _general_source_prose_coordinates(draft)
    unknown_paths = tuple(
        path for path in review.undeclared_fact_paths if path not in prose
    )
    if unknown_paths:
        raise LifeDevelopmentSourceClosureError(
            "unknown_source_closure_path",
            "undeclared_fact_paths contains paths absent from the reviewed prose: "
            + ", ".join(unknown_paths),
        )
    missing_fragments = tuple(
        fragment
        for fragment in review.undeclared_fact_fragments
        if not any(fragment in value for value in prose.values())
    )
    exact_fragments = tuple(
        fragment
        for fragment in review.undeclared_fact_fragments
        if fragment not in missing_fragments
    )
    for conflict in review.typed_location_conflicts:
        if (
            draft.location_ref is None
            or conflict.typed_location_ref != draft.location_ref
        ):
            raise LifeDevelopmentSourceClosureError(
                "unknown_typed_location_coordinate",
                "typed-location conflict does not bind the draft's actual location_ref",
            )
        value = _typed_location_prose_coordinates(draft).get(
            conflict.prose_path
        )
        if value is None or conflict.conflicting_fragment not in value:
            raise LifeDevelopmentSourceClosureError(
                "unknown_typed_location_coordinate",
                "typed-location conflict path/fragment is absent from the reviewed prose",
            )
    if missing_fragments:
        has_exact_rejection_coordinate = bool(
            review.unsupported_claim_ids
            or exact_fragments
            or review.undeclared_fact_paths
            or review.typed_location_conflicts
        )
        if not has_exact_rejection_coordinate:
            raise LifeDevelopmentSourceClosureError(
                "unknown_source_closure_fragment",
                "undeclared_fact_fragments contains text absent from the reviewed prose: "
                + ", ".join(missing_fragments),
            )
        review = review.model_copy(
            update={"undeclared_fact_fragments": exact_fragments}
        )
    return review


def parse_life_development_novel_origin_review(
    *,
    raw: str,
    draft: LifeDevelopmentPossibilityDraft,
) -> LifeDevelopmentNovelOriginReview:
    """Decode a focused novel-origin verdict and verify every model coordinate."""

    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16_000:
        raise LifeDevelopmentSourceClosureError(
            "invalid_novel_origin_output",
            "novel-origin review must be bounded JSON text",
        )
    json_text = raw.strip()
    if json_text.startswith("```") and json_text.endswith("```"):
        first_newline = json_text.find("\n")
        opening = json_text[:first_newline].strip().casefold()
        if first_newline > 0 and opening in {"```", "```json"}:
            json_text = json_text[first_newline + 1 : -3].strip()
    try:
        decoded = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise LifeDevelopmentSourceClosureError(
            "invalid_novel_origin_json",
            "novel-origin review is not valid JSON",
        ) from exc
    if not isinstance(decoded, dict):
        raise LifeDevelopmentSourceClosureError(
            "invalid_novel_origin_shape",
            "novel-origin review must be one JSON object",
        )
    review_value = _unwrap_review_transport_envelope(
        decoded,
        error_code="invalid_novel_origin_shape",
        review_label="novel-origin review",
    )
    try:
        review = LifeDevelopmentNovelOriginReview.model_validate_json(
            json.dumps(review_value, ensure_ascii=False, separators=(",", ":"))
        )
    except ValueError as exc:
        detail = "novel-origin review violates its exact output contract"
        structured: tuple[dict[str, str], ...] = ()
        if isinstance(exc, ValidationError):
            violations = []
            machine = []
            for error in exc.errors(include_url=False, include_input=False):
                location = ".".join(str(part) for part in error["loc"]) or "<root>"
                violations.append(f"{location}: {error['msg']} [{error['type']}]")
                machine.append(
                    {
                        "path": location,
                        "message": str(error["msg"]),
                        "type": str(error["type"]),
                    }
                )
            if violations:
                detail = f"{detail}: {'; '.join(violations)}"
                structured = tuple(machine)
        raise LifeDevelopmentSourceClosureError(
            "invalid_novel_origin_shape",
            detail[:8_000],
            violations=structured,
        ) from exc

    novel_claims = {
        item.claim_id: item.summary
        for item in draft.claim_declarations
        if item.scope == "novel_world_generation"
    }
    for finding in review.unsupported_claims:
        summary = novel_claims.get(finding.claim_id)
        if summary is None:
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_claim",
                "focused critic claim finding is absent from novel declarations",
            )
        if any(fragment not in summary for fragment in finding.exact_fragments):
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_claim_fragment",
                "focused critic claim fragment is absent from the exact claim summary",
            )

    npc_summaries: dict[str, list[str]] = {}
    for outcome in draft.outcomes:
        for npc in outcome.provisional_npcs:
            npc_summaries.setdefault(npc.local_ref, []).append(npc.summary)
    for finding in review.unsupported_provisional_npcs:
        summaries = npc_summaries.get(finding.local_ref)
        if summaries is None:
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_npc",
                "focused critic NPC finding is absent from provisional NPCs",
            )
        if any(
            not any(fragment in summary for summary in summaries)
            for fragment in finding.exact_fragments
        ):
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_npc_fragment",
                "focused critic NPC fragment is absent from the exact NPC summaries",
            )
    outcome_text = {
        f"outcomes.{index}.text": outcome.text
        for index, outcome in enumerate(draft.outcomes)
    }
    for finding in review.unsupported_outcome_prerequisites:
        text = outcome_text.get(finding.prose_path)
        if text is None:
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_outcome_path",
                "focused critic outcome path is absent from the reviewed draft",
            )
        if any(fragment not in text for fragment in finding.exact_fragments):
            raise LifeDevelopmentSourceClosureError(
                "unknown_novel_origin_outcome_fragment",
                "focused critic outcome fragment is absent from the exact outcome text",
            )
    return review


def _general_source_prose_coordinates(
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, str]:
    """Return prose over which the general reviewer has negative authority."""

    values: dict[str, str] = {"premise": draft.premise}
    for outcome_index, outcome in enumerate(draft.outcomes):
        prefix = f"outcomes.{outcome_index}"
        for npc_index, npc in enumerate(outcome.provisional_npcs):
            values[f"{prefix}.provisional_npcs.{npc_index}.summary"] = npc.summary
        visual = outcome.visual_evidence
        if visual is None:
            continue
        if visual.activity_description is not None:
            values[f"{prefix}.visual_evidence.activity_description"] = (
                visual.activity_description
            )
        if visual.location is not None:
            for field in (
                "location_ref",
                "kind",
                "country",
                "region",
                "city",
                "publicness",
            ):
                value = getattr(visual.location, field)
                if value is not None:
                    values[f"{prefix}.visual_evidence.location.{field}"] = value
        if visual.environment is not None:
            for field in ("light", "weather", "structure", "region"):
                value = getattr(visual.environment, field)
                if value is not None:
                    values[f"{prefix}.visual_evidence.environment.{field}"] = value
        for object_index, item in enumerate(visual.objects):
            values[f"{prefix}.visual_evidence.objects.{object_index}.kind"] = item.kind
            values[
                f"{prefix}.visual_evidence.objects.{object_index}.description"
            ] = item.description
    return values


def _typed_location_prose_coordinates(
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, str]:
    """Expose semantic execution prose only to the typed-location boundary."""

    return {
        **_general_source_prose_coordinates(draft),
        **{
            f"outcomes.{index}.text": outcome.text
            for index, outcome in enumerate(draft.outcomes)
        },
    }


def life_development_source_closure_messages(
    *,
    context: dict[str, object],
    manifest: LifeDevelopmentCapabilityManifest,
    draft: LifeDevelopmentPossibilityDraft,
    cited_events: tuple[WorldEvent, ...],
) -> list[dict[str, str]]:
    """Compile the independent reviewer request from the exact pinned inputs."""

    existing_claim_refs = {
        ref
        for claim in draft.claim_declarations
        if claim.scope == "existing_world"
        for ref in claim.source_refs
    }
    event_material = [
        {
            "source_ref": event.event_id,
            "event_type": event.event_type,
            "logical_time": event.logical_time.isoformat(),
            "payload": event.payload(),
        }
        for event in cited_events
        if event.event_id in existing_claim_refs
    ]
    system = (
        "You are an independent semantic source-closure reviewer, not the World "
        "Author and not the Character Model. Judge only non-negotiable truth and "
        "coordinate authority. Do not judge whether a development is interesting, "
        "likely, tasteful, socially appropriate, emotionally fitting, or what the "
        "character should choose. The World Author is explicitly free to create "
        "proposal-scoped novel environmental events, adverse surprises, provisional "
        "NPCs, and scoped novel places under novel_world_generation without a source. "
        "Do not reject those merely because they are invented. Existing-world claims "
        "are different: every such claim must be semantically entailed by its exact "
        "cited source material; the presence of a source id is never sufficient. A "
        "ClockAdvanced event proves only its recorded time movement, not weather, a "
        "message, a person, a relationship, a venue, or an activity. A biographical "
        "event proves only its exact recorded biography/calendar/residence fields; "
        "residence context is not proof of current physical presence. "
        "Novel generation cannot be used to smuggle a prior friendship, known person, "
        "user/shared history, or completed character experience. Any existing named "
        "person participating in the possibility must be bound through manifest-listed "
        "entity_refs and exact existing-world evidence; a genuinely new person remains "
        "proposal-scoped and must use the provisional-NPC authority instead. A newly "
        "authored weather or environmental condition must be covered by its own "
        "novel_world_generation claim, not inferred from calendar context. Identify "
        "undeclared current facts only in premise, visual evidence, and provisional-NPC "
        "summaries. Outcome text has no negative coordinate in this general review lane: "
        "do not return an outcome text path or copy an outcome-only fragment into "
        "undeclared_fact_fragments. A separate focused critic reviews only imported "
        "current/prior prerequisites and retroactive history in outcome text. "
        "Branch-internal candidate actions, dialogue, feelings, replies, invitations, "
        "messages, and responses remain unsettled and are not source-closure failures. "
        "If a typed location_ref is "
        "present, it must be the execution coordinate of the proposed Plan or "
        "occurrence; other places may appear only as explicit background, origin, or "
        "hypothetical alternatives, not as a hidden destination. A proposal-scoped "
        "novel execution place remains valid only with no contradictory typed "
        "location and does not become a reusable location capability. For undeclared "
        "factual prose, prefer undeclared_fact_paths copied from the supplied parser "
        "coordinate catalogue. Use undeclared_fact_fragments only when a shorter "
        "coordinate is useful, and then copy a verbatim substring without quotation "
        "marks, a path prefix, or commentary. Return exactly one JSON object matching "
        "the supplied output contract, with the complete verdict inside its required "
        "review envelope."
    )
    request = {
        "review_contract": _REVIEW_CONTRACT,
        "reviewed_world_author_draft": draft.model_dump(mode="json"),
        "pinned_source_evidence": {
            "contract": "life-development-source-evidence.1",
            "pinned_world_context": context,
            "cited_committed_events": event_material,
            "capability_manifest": {
                **manifest.model_dump(mode="json"),
                "manifest_hash": manifest.manifest_hash,
            },
            "authority_note": (
                "Only the exact semantic content above may support existing_world "
                "claims. Opaque ids and broad event types add no unstated facts."
            ),
        },
        "review_dimensions": {
            "existing_world_entailment": "exact_cited_sources_only",
            "undeclared_factual_prose": "premise,provisional_npcs,visual_evidence",
            "outcome_text_authority": {
                "general_reviewer": "no_negative_coordinate_authority",
                "focused_novel_origin_critic": (
                    "imported_current_or_prior_prerequisites_and_retroactive_history_only"
                ),
                "branch_internal_candidate_action_dialogue_feeling": "allowed",
            },
            "novel_world_generation": {
                "proposal_scoped_environment": "allowed",
                "adverse_or_unfavorable_event": "allowed",
                "provisional_npc": "allowed",
                "scoped_novel_place": "allowed",
                "invented_prior_relationship_or_completed_history": "not_allowed",
            },
            "typed_location_consistency": (
                "typed execution coordinate must match the semantic proposal; reject "
                "only with typed_location_ref plus an exact prose_path and verbatim "
                "conflicting_fragment from that field"
            ),
        },
        "parser_coordinate_catalog": _source_closure_coordinate_catalog(draft),
        "output_contract": _review_output_contract(
            contract=_REVIEW_CONTRACT,
            review_model=LifeDevelopmentSourceClosureReview,
        ),
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def life_development_source_closure_correction_message(
    *,
    raw: str,
    error: LifeDevelopmentSourceClosureError,
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, str]:
    """Ask the same reviewer to repair only its invalid wire result."""

    return {
        "role": "user",
        "content": json.dumps(
            {
                "invalid_review_output": raw,
                "validation_failure": {
                    "code": error.code,
                    "detail": error.detail,
                    "violations": list(error.violations),
                },
                "review_contract": _REVIEW_CONTRACT,
                "parser_coordinate_catalog": _source_closure_coordinate_catalog(
                    draft
                ),
                "output_contract": _review_output_contract(
                    contract=_REVIEW_CONTRACT,
                    review_model=LifeDevelopmentSourceClosureReview,
                ),
                "instruction": (
                    "Return one complete replacement review for the identical draft and "
                    "pinned evidence. Prefer an exact undeclared_fact_path from the "
                    "catalogue over paraphrasing prose. Any undeclared_fact_fragment "
                    "must be copied verbatim without quotes, path labels, or commentary. "
                    "Do not change the World Author's proposal, invent evidence, or make "
                    "a style/motive judgement."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def _source_closure_coordinate_catalog(
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, object]:
    """Expose only deterministic coordinates already present in the draft."""

    prose_paths = sorted(_general_source_prose_coordinates(draft))
    typed_location_paths = sorted(_typed_location_prose_coordinates(draft))
    return {
        "unsupported_claim_ids": [
            item.claim_id
            for item in draft.claim_declarations
            if item.scope == "existing_world"
        ],
        "undeclared_fact_paths": prose_paths,
        "fragment_rule": (
            "copy_a_verbatim_substring_from_the_selected_path_without_quotes_or_commentary"
        ),
        "typed_location": {
            "typed_location_ref": draft.location_ref,
            "prose_paths": typed_location_paths,
        },
    }


def life_development_novel_origin_messages(
    *,
    context: dict[str, object],
    manifest: LifeDevelopmentCapabilityManifest,
    draft: LifeDevelopmentPossibilityDraft,
) -> list[dict[str, str]]:
    """Compile an independent hard-boundary review of novel fact origin."""

    system = (
        "You are an independent focused novel-origin critic, not the general "
        "source reviewer, World Author, or Character Model. Review only hard truth "
        "origin; never judge plot quality, likelihood, "
        "motive, mood, style, or whether the character should participate. A "
        "novel_world_generation claim may create a genuinely new current environmental "
        "contingency, scoped place, provisional stranger, first encounter, or new "
        "relationship starting point. It cannot retroactively create a prior "
        "friendship, classmate relationship, period of no contact, shared history, "
        "known person, recurring habit, or completed character experience. The same "
        "boundary applies to provisional-NPC summaries. Inspect each exact outcome "
        "text for facts imported from before that candidate branch: a current/prior "
        "external prerequisite, retroactive relationship or shared history, an "
        "already-completed character experience, or an existing entity/fact relabelled "
        "as novel. Ordinary events created inside the candidate branch—including "
        "actions, dialogue, invitations, replies, feelings, and subjective responses—"
        "remain unsettled and must not be rejected. Current premise, visual, NPC "
        "declaration coverage and typed location belong to the general reviewer, not "
        "this lane. Return only parser-verifiable "
        "coordinates: each unsupported novel claim uses its exact claim_id and "
        "verbatim fragments from that claim summary; each provisional NPC uses its "
        "exact local_ref and verbatim fragments from its summary; each imported "
        "outcome prerequisite uses an exact supplied outcomes.N.text prose_path and "
        "verbatim fragments from that one outcome. Return exactly one JSON object "
        "matching the supplied contract, with the complete verdict inside its required "
        "review envelope."
    )
    request = {
        "review_contract": _NOVEL_ORIGIN_CONTRACT,
        "reviewed_world_author_draft": draft.model_dump(mode="json"),
        "pinned_authority": {
            "pinned_world_context": context,
            "capability_manifest": {
                **manifest.model_dump(mode="json"),
                "manifest_hash": manifest.manifest_hash,
            },
        },
        "review_dimensions": {
            "novel_claim_origin": (
                "no_prior_relationship_shared_history_or_completed_experience"
            ),
            "provisional_npc_origin": "new_person_or_new_relationship_start_only",
            "outcome_prerequisites": {
                "reject": (
                    "imported_current_or_prior_fact_or_retroactive_history_outside_branch"
                ),
                "allow": (
                    "branch_internal_candidate_action_dialogue_feeling_or_response"
                ),
            },
            "current_premise_coverage": "delegated_to_general_source_reviewer",
            "character_behavior": "out_of_scope",
        },
        "parser_coordinate_catalog": _novel_origin_coordinate_catalog(draft),
        "output_contract": _review_output_contract(
            contract=_NOVEL_ORIGIN_CONTRACT,
            review_model=LifeDevelopmentNovelOriginReview,
        ),
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]


def life_development_novel_origin_correction_message(
    *,
    error: LifeDevelopmentSourceClosureError,
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, str]:
    """Ask the same focused critic to repair only an invalid wire result."""

    return {
        "role": "user",
        "content": json.dumps(
            {
                "validation_failure": {
                    "code": error.code,
                    "detail": error.detail,
                    "violations": list(error.violations),
                },
                "review_contract": _NOVEL_ORIGIN_CONTRACT,
                "parser_coordinate_catalog": _novel_origin_coordinate_catalog(
                    draft
                ),
                "output_contract": _review_output_contract(
                    contract=_NOVEL_ORIGIN_CONTRACT,
                    review_model=LifeDevelopmentNovelOriginReview,
                ),
                "instruction": (
                    "Return one complete replacement review for the identical draft "
                    "and pinned authority. Preserve the focused truth-origin boundary, "
                    "use only exact parser-verifiable coordinates from the supplied "
                    "catalogue, and do not judge or change the story. Branch-internal "
                    "candidate actions, dialogue, feelings, or responses are not "
                    "imported prerequisites."
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }


def _novel_origin_coordinate_catalog(
    draft: LifeDevelopmentPossibilityDraft,
) -> dict[str, object]:
    """Expose exact focused-review coordinates without assigning a verdict."""

    return {
        "novel_claim_ids": [
            item.claim_id
            for item in draft.claim_declarations
            if item.scope == "novel_world_generation"
        ],
        "provisional_npc_refs": sorted(
            {
                npc.local_ref
                for outcome in draft.outcomes
                for npc in outcome.provisional_npcs
            }
        ),
        "outcome_prerequisite_paths": [
            f"outcomes.{index}.text"
            for index, _outcome in enumerate(draft.outcomes)
        ],
        "fragment_rule": (
            "copy_verbatim_substrings_from_the_matching_claim_npc_or_outcome_path"
        ),
    }


__all__ = [
    "LifeDevelopmentOutcomePrerequisiteFinding",
    "LifeDevelopmentNovelOriginReview",
    "LifeDevelopmentSourceClosureError",
    "LifeDevelopmentSourceClosureReview",
    "life_development_novel_origin_correction_message",
    "life_development_novel_origin_messages",
    "life_development_source_closure_correction_message",
    "life_development_source_closure_messages",
    "parse_life_development_novel_origin_review",
    "parse_life_development_source_closure_review",
]
