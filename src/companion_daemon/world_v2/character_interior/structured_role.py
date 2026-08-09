"""One structured, source-bound character author for every interior purpose.

Purpose contracts in this module describe only wire shape and capability
closure.  They do not encode a motive, tone, social policy, or preferred
choice.  Provider and parsing failures remain technical failures; this faculty
never discovers a second author or manufactures a character result.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    field_validator,
    model_validator,
)

from companion_daemon.llm import model_call_scope

from ..model_completion import ChatCompletionModel
from ..character_outcome_contract import CharacterLifeDirectionDraft
from ..proposal_envelope import AspirationTransitionPayload
from ..schema_core import canonicalize_json_value
from ..structured_completion import complete_json_object
from ..schemas import MemoryCueKind, MemoryRetentionRationale
from .author_identity import character_semantic_author_identity
from .contracts import (
    InteriorAffectOpenTransition,
    InteriorAffectSupersedeTransition,
    InteriorAffectUpdateTransition,
    InteriorAffectTransition,
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
)
from .experience_transitions import (
    ExperienceTransitionCapability,
    ExperienceTransitionDraft,
    validate_experience_transition_draft,
)
from .ports import (
    _InteriorRoleRequest,
    _InteriorRoleResult,
    _RoleResultContractError,
)
from .structured_role_tool_contract import (
    StructuredRoleToolContract,
    StructuredRoleToolContracts,
)


_FACET_NAMES = (
    "private_self",
    "selective_memory",
    "appraisal_affect",
    "emotional_continuity",
    "subjective_relationship",
    "aspirations_conflicts",
    "autonomous_impulses",
    "expression_stance",
)

_EXPRESSION_RECONSIDERATION_DISPOSITIONS = frozenset(
    {"continue", "cancel", "defer", "merge", "supersede", "new_beat"}
)


def _canonical(value: object) -> str:
    return json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class StructuredRoleResultError(_RoleResultContractError):
    """A model-authored result that violates the structural role contract."""

    def __init__(
        self,
        code: str,
        *,
        detail: str,
        response_hash: str | None = None,
    ) -> None:
        super().__init__(
            code,
            detail=detail,
            response_hash=response_hash,
        )


PayloadValidator = Callable[[Mapping[str, object], frozenset[str]], None]


@dataclass(frozen=True, slots=True)
class PurposeDecisionContract:
    """Extensible structural contract for one authorized purpose.

    ``validator`` may check only syntax and the offered capability set.  The
    role model remains the sole owner of the semantic choice.
    """

    purpose: str
    payload_contract: str
    capability_kind: str | None = None
    offered_token_fields: tuple[str, ...] = ()
    selected_token_required: bool = False
    decision_required: bool = False
    proposals_allowed: bool = True
    proposal_type: str | None = None
    proposal_payload_contract: str | None = None
    validator: PayloadValidator | None = None

    def __post_init__(self) -> None:
        if not self.purpose or not self.payload_contract:
            raise ValueError("purpose decision contract identity is incomplete")
        if self.selected_token_required and self.capability_kind is None:
            raise ValueError("selected-token contract needs a capability kind")
        if (self.proposal_type is None) != (self.proposal_payload_contract is None):
            raise ValueError("typed proposal contract identity is incomplete")


def _validate_media_selection_payload(
    payload: Mapping[str, object],
    offered_tokens: frozenset[str],
) -> None:
    """Close only the media wire shape, never the character's preference."""

    decision = payload.get("decision")
    if decision == "no_op" and set(payload) == {"decision"}:
        return
    selected_token = payload.get("selected_token")
    if decision == "select":
        if not isinstance(selected_token, str) or not selected_token:
            raise StructuredRoleResultError(
                "selected_token_required",
                detail=_FAILURE_DETAILS["selected_token_required"],
            )
        if selected_token not in offered_tokens:
            raise StructuredRoleResultError(
                "selected_token_not_offered",
                detail=_FAILURE_DETAILS["selected_token_not_offered"],
            )
        if set(payload) == {"decision", "selected_token"}:
            return
    raise ValueError("media selection must be no_op or select exactly one offered token")


def _validate_reconsideration_payload(
    payload: Mapping[str, object],
    offered_tokens: frozenset[str],
) -> None:
    if set(payload) != {"disposition"} or payload.get("disposition") not in offered_tokens:
        raise ValueError("expression reconsideration disposition is outside the offered capability")
    if payload.get("disposition") not in _EXPRESSION_RECONSIDERATION_DISPOSITIONS:
        raise ValueError("expression reconsideration needs one supported disposition")


class _ExpressionReconsiderationPayload(BaseModel):
    """Canonical role-authored disposition for an interrupted expression."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    disposition: Literal[
        "continue",
        "cancel",
        "defer",
        "merge",
        "supersede",
        "new_beat",
    ]


_MemoryBasisPointInteger = Annotated[int, Field(ge=0, le=10_000)]
_MemoryBasisPointNumber = Annotated[float, Field(ge=0, le=10_000)]


class _MemorySaliencePayload(BaseModel):
    """Provider-facing salience values without reducer-owned matrix metadata."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    autobiographical_relevance_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    relationship_relevance_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    emotional_residue_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    unfinished_business_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    recurrence_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    novelty_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    future_utility_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber
    world_continuity_bp: _MemoryBasisPointInteger | _MemoryBasisPointNumber


class _MemoryRetentionNoChangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    retain: Literal[False]


class _MemoryRetentionPayloadValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    retain: Literal[True]
    cue_kind: MemoryCueKind
    retention_rationales: list[MemoryRetentionRationale] = Field(
        min_length=1,
        max_length=8,
    )
    salience: _MemorySaliencePayload

    @model_validator(mode="after")
    def rationales_are_unique(self) -> "_MemoryRetentionPayloadValue":
        if len(self.retention_rationales) != len(set(self.retention_rationales)):
            raise ValueError("memory retention rationales must be unique")
        return self


class _MemoryRetentionPayload(
    RootModel[_MemoryRetentionNoChangePayload | _MemoryRetentionPayloadValue]
):
    """Canonical retain/no-change wire shared by fact and experience memory."""


def _validate_proactive_payload(
    payload: Mapping[str, object],
    _offered_tokens: frozenset[str],
) -> None:
    # Import locally so the deep Module's generic role contract does not make
    # proactive scheduling a dependency of every CharacterInterior import.
    from ..expression_draft import normalize_expression_draft_wire
    from ..proactive_action import ProactiveDraft

    if "private_turn_state" in payload:
        raise ValueError("proactive private_turn_state is supplied by the same InnerTurn summary")
    normalized = normalize_expression_draft_wire(
        {
            **dict(payload),
            "private_turn_state": {
                "inner_state_summary": "structural validation only",
                "attended_source_refs": [],
            },
        }
    )
    ProactiveDraft.model_validate_json(_canonical(normalized), strict=True)


def _validate_life_choice_payload(
    payload: Mapping[str, object],
    _offered_tokens: frozenset[str],
) -> None:
    if set(payload) != {"completion"} or not isinstance(payload.get("completion"), dict):
        raise ValueError("life choice needs exactly one complete JSON completion object")
    if "recall_request" in payload["completion"]:
        raise ValueError("nested Life recall is unavailable; use the outer recall_request status")


def _normalized_life_development_completion(
    payload: Mapping[str, object],
    *,
    manifest: _InteriorCapabilityManifest,
    response_hash: str | None = None,
) -> dict[str, object]:
    """Close one Life choice over the exact trusted capability envelope.

    The business runtime must not discover a domain error after the InnerTurn
    has completed and then manufacture a second opportunity.  Keep the full
    shape and cross-field authority check beside the structured role so the
    ordinary CharacterInterior correction lifecycle owns the only reselection.
    """

    from ..life_development_draft import (
        LifeDevelopmentDraftError,
        LifeDevelopmentPossibilityDraft,
        parse_character_choice,
    )
    from ..schemas import DueWindow

    completion = payload.get("completion")
    capability = manifest.payload
    external_opportunity = capability.get("external_opportunity")
    executable_envelope = capability.get("executable_envelope")
    active_aspirations = capability.get("active_aspiration_source_refs", [])
    try:
        if not isinstance(completion, dict):
            raise ValueError("life choice completion must be one JSON object")
        if not isinstance(external_opportunity, dict):
            raise ValueError("life choice capability lacks its external opportunity")
        if not isinstance(executable_envelope, dict):
            raise ValueError("life choice capability lacks its executable envelope")
        if not isinstance(active_aspirations, list) or any(
            not isinstance(item, str) or not item for item in active_aspirations
        ):
            raise ValueError("life choice aspiration authority is malformed")
        offered = LifeDevelopmentPossibilityDraft.model_validate_json(
            _canonical(external_opportunity),
        )
        offered_window = DueWindow.model_validate_json(
            _canonical(
                {
                    "opens_at": executable_envelope.get("opens_at"),
                    "closes_at": executable_envelope.get("closes_at"),
                }
            )
        )
        participant_refs = executable_envelope.get("participant_refs")
        if participant_refs != list(offered.entity_refs):
            raise ValueError("life choice executable participants changed the offered opportunity")
        parsed = parse_character_choice(
            raw=_canonical(completion),
            offered=offered,
            offered_window=offered_window,
            active_aspiration_source_refs=tuple(active_aspirations),
        )
    except LifeDevelopmentDraftError as exc:
        raise StructuredRoleResultError(
            exc.code,
            detail=exc.detail,
            response_hash=response_hash,
        ) from exc
    except (TypeError, ValueError) as exc:
        raise StructuredRoleResultError(
            "role_result_schema_invalid",
            detail=str(exc),
            response_hash=response_hash,
        ) from exc
    return parsed.model_dump(mode="json")


def _validate_activity_lifecycle_payload(
    payload: Mapping[str, object],
    offered_tokens: frozenset[str],
) -> None:
    decision = payload.get("decision")
    if decision == "no_op":
        if set(payload) != {"decision"}:
            raise ValueError("activity lifecycle no_op may contain only decision")
        return
    if decision != "select" or set(payload) != {"decision", "selected_token"}:
        raise ValueError("activity lifecycle must select one offered token or no_op")
    selected = payload.get("selected_token")
    if not isinstance(selected, str) or selected not in offered_tokens:
        raise ValueError("activity lifecycle selected an unavailable token")


def _validate_outcome_selection_payload(
    payload: Mapping[str, object],
    offered_tokens: frozenset[str],
) -> None:
    """Close one consequential choice over the exact observed alternatives."""

    if set(payload) != {"selected_token", "character_life_direction"}:
        raise ValueError(
            "outcome selection needs one selected token and an optional character direction"
        )
    selected = payload.get("selected_token")
    if not isinstance(selected, str) or not selected:
        raise StructuredRoleResultError(
            "selected_token_required",
            detail=_FAILURE_DETAILS["selected_token_required"],
        )
    if selected not in offered_tokens:
        raise StructuredRoleResultError(
            "selected_token_not_offered",
            detail=_FAILURE_DETAILS["selected_token_not_offered"],
        )
    direction = payload.get("character_life_direction")
    if direction is None:
        return
    from ..character_outcome_contract import CharacterLifeDirectionDraft
    from ..schemas import BiographicalCoordinateReplacement

    parsed = CharacterLifeDirectionDraft.model_validate(direction, strict=True)
    BiographicalCoordinateReplacement.create(
        coordinate_ref=parsed.coordinate_ref,
        summary=parsed.summary,
        context_tags=parsed.context_tags,
        replaces_context_tag_prefixes=parsed.replaces_context_tag_prefixes,
        privacy_class=parsed.privacy_class,
    )


class _OutcomeSelectionPayload(BaseModel):
    """Canonical role-authored outcome choice payload.

    The offered candidate set and the optional direction capability are
    specialized by the transport compiler.  This model only defines the
    complete semantic wire; it does not select a candidate or a direction.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    selected_token: str = Field(min_length=1, max_length=512)
    character_life_direction: CharacterLifeDirectionDraft | None = None


class _ActivityLifecyclePayload(BaseModel):
    """Canonical role-authored activity opening choice payload."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    decision: Literal["no_op", "select"]
    selected_token: str | None = None

    @model_validator(mode="after")
    def choice_shape_is_closed(self) -> "_ActivityLifecyclePayload":
        if self.decision == "no_op" and self.selected_token is not None:
            raise ValueError("activity no_op cannot carry a selected token")
        if self.decision == "select" and not self.selected_token:
            raise ValueError("activity select requires a selected token")
        return self


def _validate_memory_retention_payload(
    payload: Mapping[str, object],
    _offered_tokens: frozenset[str],
) -> None:
    # This is the installed Memory wire/matrix boundary, not a preference for
    # retention or forgetting.  Both choices remain authored by the role.
    from ..fact_memory_draft import materialize_fact_memory_draft

    materialize_fact_memory_draft(_canonical(dict(payload)))


class _PrivateImpressionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    proposal_type: Literal["private_impression_transition"]
    decision: Literal["retain", "consolidate", "supersede"]
    predecessor_refs: list[str] = Field(default_factory=list, max_length=32)
    source_refs: list[str] = Field(min_length=1, max_length=48)
    reflection_summary: str = Field(min_length=1, max_length=1_200)
    confidence_bp: int = Field(ge=0, le=10_000)
    expiry_condition: Literal[
        "until_appraisal_contradicted",
        "until_counter_evidence",
        "until_relationship_stage_changes",
        "one_month_without_support",
    ]

    @model_validator(mode="after")
    def transition_shape_is_closed(self) -> "_PrivateImpressionProposal":
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("private impression source refs must be unique")
        if len(self.predecessor_refs) != len(set(self.predecessor_refs)):
            raise ValueError("private impression predecessor refs must be unique")
        if self.decision == "retain" and self.predecessor_refs:
            raise ValueError("retain cannot retire an existing impression")
        if self.decision != "retain" and not self.predecessor_refs:
            raise ValueError("consolidate/supersede require predecessor refs")
        if set(self.predecessor_refs) - set(self.source_refs):
            raise ValueError("private impression predecessors must also be selected sources")
        return self


class _WorldStimulusMeaningCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    meaning: str = Field(min_length=1, max_length=128)
    confidence: int = Field(ge=0, le=10_000)

    @field_validator("meaning")
    @classmethod
    def meaning_is_canonical_free_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("appraisal meaning cannot have surrounding whitespace")
        return value


class _WorldStimulusRelationshipDeltas(BaseModel):
    """A bounded suggestion, never direct relationship mutation authority."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    trust_bp: int = Field(ge=-10_000, le=10_000)
    closeness_bp: int = Field(ge=-10_000, le=10_000)
    respect_bp: int = Field(ge=-10_000, le=10_000)
    reliability_bp: int = Field(ge=-10_000, le=10_000)
    mutuality_bp: int = Field(ge=-10_000, le=10_000)
    repair_confidence_bp: int = Field(ge=-10_000, le=10_000)


class _WorldStimulusRelationshipSignal(BaseModel):
    """Optional relationship reading authored in the same private turn."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    subject_ref: str = Field(min_length=1, max_length=512)
    signal_code: str = Field(min_length=1, max_length=128)
    confidence_bp: int = Field(ge=1, le=10_000)
    persistence: Literal["session", "durable"]
    rationale_code: str = Field(min_length=1, max_length=128)
    suggested_deltas: _WorldStimulusRelationshipDeltas


class _WorldStimulusAppraisalResult(BaseModel):
    """Wire closure only; the role still owns whether and how it appraises."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    proposal_type: Literal["world_stimulus_appraisal_result"]
    decision: Literal["no_change", "activate"]
    brief_rationale: str = Field(min_length=1, max_length=240)
    behavior_tendency: str = Field(min_length=1, max_length=128)
    stance: str = Field(min_length=1, max_length=128)
    display_strategy: str = Field(min_length=1, max_length=128)
    confidence: int = Field(ge=0, le=10_000)
    meaning_candidates: list[_WorldStimulusMeaningCandidate] | None = Field(
        default=None,
        min_length=1,
        max_length=16,
    )
    attribution: (
        Literal["user", "companion", "npc", "situation", "third_party", "unknown"] | None
    ) = None
    severity: int | None = Field(default=None, ge=0, le=10_000)
    expiry: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def canonicalize_rationale(cls, value: object) -> object:
        # The role model frequently names the private reason "rationale"
        # instead of the wire's "brief_rationale".  Accept that exact alias.
        if isinstance(value, dict) and "rationale" in value and "brief_rationale" not in value:
            value = dict(value)
            value["brief_rationale"] = value.pop("rationale")
        # The model sometimes omits the semantic decision while still
        # carrying appraisal fields; derive it deterministically from the
        # carried content instead of failing the whole turn.
        if (
            isinstance(value, dict)
            and "decision" not in value
            and any(
                value.get(field) is not None
                for field in (
                    "meaning_candidates",
                    "affect_transition",
                    "relationship_signal",
                    "aspiration_transition",
                    "experience_transition",
                )
            )
        ):
            value = dict(value)
            value["decision"] = "activate"
        # The model sometimes echoes its attended sources into the proposal;
        # that binding already lives on the outer envelope, so drop the echo.
        if isinstance(value, dict) and "source_refs" in value:
            value = dict(value)
            value.pop("source_refs", None)
        return value

    @field_validator("expiry", mode="before")
    @classmethod
    def parse_iso_expiry(cls, value: object) -> object:
        # The role model naturally emits ISO-8601 strings; the wire demands a
        # timezone-aware datetime object.  Parse that exact representation so
        # an otherwise valid appraisal never fails solely on serialization.
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
        return value

    affect_transition: InteriorAffectTransition | None = None
    relationship_signal: _WorldStimulusRelationshipSignal | None = None
    aspiration_transition: AspirationTransitionPayload | None = None
    experience_transition: ExperienceTransitionDraft | None = None

    @model_validator(mode="after")
    def decision_closes_appraisal_shape(self) -> "_WorldStimulusAppraisalResult":
        appraisal_fields = (
            self.meaning_candidates,
            self.attribution,
            self.severity,
        )
        if self.decision == "activate":
            if any(item is None for item in appraisal_fields):
                raise ValueError("activate requires one complete appraisal")
            assert self.meaning_candidates is not None
            meanings = [item.meaning for item in self.meaning_candidates]
            if len(meanings) != len(set(meanings)):
                raise ValueError("appraisal meanings must be unique")
            if sum(item.confidence for item in self.meaning_candidates) <= 0:
                raise ValueError("appraisal meaning weights cannot all be zero")
        elif (
            any(item is not None for item in appraisal_fields)
            or self.expiry is not None
            or self.affect_transition is not None
            or self.relationship_signal is not None
        ):
            raise ValueError("no_change cannot smuggle an appraisal")
        return self


_BUILTIN_CONTRACTS = (
    PurposeDecisionContract(
        purpose="media_selection",
        payload_contract="character-interior-media-selection-decision.1",
        capability_kind="media_selection",
        offered_token_fields=("offered_tokens", "candidates"),
        selected_token_required=False,
        proposals_allowed=False,
        validator=_validate_media_selection_payload,
    ),
    PurposeDecisionContract(
        purpose="external_perception_attention",
        payload_contract="external-perception-attention-decision.1",
        capability_kind="external_perception_attention",
        # External attention is deliberately zero-or-many.  The specialized
        # validator below closes every chosen candidate/revision/channel over
        # the exact source-bound window without turning this into a single
        # token behavior choice.
        selected_token_required=False,
        proposals_allowed=False,
    ),
    PurposeDecisionContract(
        purpose="qq_attachment_perception",
        payload_contract="character-interior-qq-attachment-perception-decision.1",
        capability_kind="qq_attachment_perception",
        offered_token_fields=(
            "offered_tokens",
            "attachments",
            "attachment_refs",
        ),
        selected_token_required=True,
        proposals_allowed=False,
    ),
    PurposeDecisionContract(
        purpose="proactive_contact",
        payload_contract="character-interior-proactive-contact-decision.1",
        capability_kind="proactive_contact",
        proposals_allowed=False,
        validator=_validate_proactive_payload,
    ),
    PurposeDecisionContract(
        purpose="expression_reconsideration",
        payload_contract="character-interior-expression-reconsideration-decision.1",
        capability_kind="expression_reconsideration",
        offered_token_fields=("allowed_dispositions",),
        proposals_allowed=False,
        validator=_validate_reconsideration_payload,
    ),
    PurposeDecisionContract(
        purpose="private_impression_reflection",
        payload_contract="character-interior-private-impression-decision.1",
        capability_kind="private_impression_reflection",
        offered_token_fields=("offered_tokens", "reflection_sources"),
        proposal_type="private_impression_transition",
        proposal_payload_contract="character-interior-private-impression-transition.1",
    ),
    PurposeDecisionContract(
        purpose="world_stimulus_appraisal",
        payload_contract="character-interior-world-stimulus-appraisal-decision.1",
        capability_kind="world_stimulus_appraisal",
        proposal_type="world_stimulus_appraisal_result",
        proposal_payload_contract="character-interior-world-stimulus-appraisal-result.1",
    ),
    PurposeDecisionContract(
        purpose="life_development_choice",
        payload_contract="character-interior-life-development-choice.1",
        capability_kind="life_development_choice",
        decision_required=True,
        proposals_allowed=False,
        validator=_validate_life_choice_payload,
    ),
    PurposeDecisionContract(
        purpose="activity_lifecycle_choice",
        payload_contract="character-interior-activity-lifecycle-choice.1",
        capability_kind="activity_lifecycle_choice",
        offered_token_fields=("offered_tokens",),
        decision_required=True,
        proposals_allowed=False,
        validator=_validate_activity_lifecycle_payload,
    ),
    PurposeDecisionContract(
        purpose="outcome_selection",
        payload_contract="character-interior-outcome-selection-decision.1",
        capability_kind="outcome_selection",
        offered_token_fields=("offered_tokens", "candidates"),
        selected_token_required=True,
        decision_required=True,
        proposals_allowed=False,
        validator=_validate_outcome_selection_payload,
    ),
    PurposeDecisionContract(
        purpose="fact_memory_retention",
        payload_contract="character-interior-fact-memory-retention.1",
        capability_kind="fact_memory_retention",
        decision_required=True,
        proposals_allowed=False,
        validator=_validate_memory_retention_payload,
    ),
    PurposeDecisionContract(
        purpose="experience_memory_retention",
        payload_contract="character-interior-experience-memory-retention.1",
        capability_kind="experience_memory_retention",
        decision_required=True,
        proposals_allowed=False,
        validator=_validate_memory_retention_payload,
    ),
    PurposeDecisionContract(
        purpose="memory_withdrawal_review",
        payload_contract="character-interior-memory-withdrawal-review.1",
        capability_kind="memory_withdrawal_review",
        offered_token_fields=("offered_tokens",),
        selected_token_required=True,
        decision_required=True,
        proposals_allowed=False,
    ),
)


class _WireDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    source_refs: list[str] = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def refs_are_unique(self) -> "_WireDecision":
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("decision source refs must be unique")
        if any(not item for item in self.source_refs):
            raise ValueError("decision source refs cannot be empty")
        return self


class _WireRoleResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["transition", "no_change", "decision", "silent", "recall_request"]
    summary: str = Field(min_length=1, max_length=1_024)
    attended_source_refs: list[str] = Field(default_factory=list, max_length=32)
    decision: _WireDecision | None = None
    recall_query: str | None = Field(default=None, min_length=1, max_length=1_024)
    proposals: list[dict[str, Any]] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def terminal_shape_matches_status(self) -> "_WireRoleResult":
        if not self.summary.strip():
            raise ValueError("role result summary cannot be blank")
        if len(self.attended_source_refs) != len(set(self.attended_source_refs)):
            raise ValueError("role attention refs must be unique")
        if self.status == "decision" and self.decision is None:
            raise ValueError("decision status requires decision")
        if self.status != "decision" and self.decision is not None:
            raise ValueError("only decision status may contain decision")
        if self.status == "recall_request" and self.recall_query is None:
            raise ValueError("recall_request requires recall_query")
        if self.status != "recall_request" and self.recall_query is not None:
            raise ValueError("only recall_request may contain recall_query")
        if self.status == "recall_request" and self.proposals:
            raise ValueError("recall_request cannot contain proposals")
        return self


class _ExternalAttentionSelection(BaseModel):
    """Wire-only shape; semantic closure is checked against the manifest."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    candidate_ref: str = Field(min_length=1, max_length=1_024)
    exact_signal_revision_refs: list[str] = Field(min_length=1, max_length=32)
    selected_channel_ref: str = Field(min_length=1, max_length=1_024)
    subjective_summary: str = Field(min_length=1, max_length=8_000)
    epistemic_notes: str = Field(default="", max_length=4_000)
    attended_context_refs: list[str] = Field(default_factory=list, max_length=64)
    privacy_class: Literal["public", "shareable", "personal", "private", "withhold"] | None = None

    @model_validator(mode="after")
    def refs_are_unique(self) -> "_ExternalAttentionSelection":
        if len(self.exact_signal_revision_refs) != len(set(self.exact_signal_revision_refs)):
            raise ValueError("external attention revision refs must be unique")
        if len(self.attended_context_refs) != len(set(self.attended_context_refs)):
            raise ValueError("external attention Context refs must be unique")
        return self


_FAILURE_DETAILS = {
    "role_result_not_text": "The provider result was not JSON text.",
    "role_result_not_json": "The provider result was not one valid JSON object.",
    "role_result_not_object": "The JSON root was not an object.",
    "role_result_schema_invalid": "The object did not match the complete role-result schema.",
    "required_tool_choice_unsupported": (
        "The selected provider does not support the required purpose tool."
    ),
    "phase_status_invalid": "The selected status is unavailable in this phase.",
    "attended_source_unpinned": "An attended source ref was absent from the pinned snapshot.",
    "decision_source_unpinned": "A decision source ref was absent from the pinned snapshot or manifest.",
    "capability_manifest_required": "This purpose needs its source-bound capability manifest.",
    "capability_kind_mismatch": "The capability kind does not match this purpose contract.",
    "capability_manifest_missing_offered_tokens": (
        "The capability manifest did not contain an offered token set."
    ),
    "selected_token_required": "The decision payload did not name one selected token.",
    "selected_token_not_offered": "The selected token was not an offered token.",
    "media_selection_source_unclosed": (
        "The selected media candidate's source refs were not all cited by the decision."
    ),
    "payload_contract_reserved": "The role payload attempted to replace its trusted contract.",
    "purpose_proposals_not_allowed": (
        "This capability decision cannot submit domain proposals; return proposals as []."
    ),
    "unsupported_purpose_contract": "No structural contract was registered for this capability purpose.",
    "repeated_recall_request": "Selective recall was already completed for this inner turn.",
    "world_stimulus_affect_target_outside_capability": (
        "An Affect target was absent from, or below, the exact cursor-pinned numeric "
        "capability. Choose no immediate Affect or author a complete legal replacement."
    ),
    "world_stimulus_relationship_subject_outside_capability": (
        "The relationship signal named a subject not offered by this exact capability."
    ),
    "world_stimulus_aspiration_source_outside_capability": (
        "The aspiration transition must cite the current stimulus and only supplied "
        "aspiration authority sources."
    ),
    "world_stimulus_aspiration_target_outside_capability": (
        "The aspiration transition targets no active aspiration in the supplied capability."
    ),
    "world_stimulus_experience_transition_outside_capability": (
        "The long-horizon transition must select one exact offered head/source, revision, "
        "operation, and complete evidence closure. Goal creation is unavailable until a "
        "content authority exists."
    ),
    "external_attention_result_shape_invalid": (
        "External attention must return one zero-or-many selections array with complete fields."
    ),
    "external_attention_duplicate_candidate": (
        "The same external candidate was selected more than once."
    ),
    "external_attention_candidate_not_offered": (
        "A selected external candidate was absent from this frozen window."
    ),
    "external_attention_revision_not_offered": (
        "A selected signal revision was absent from its offered candidate."
    ),
    "external_attention_channel_not_offered": (
        "A selected channel was absent from its offered candidate."
    ),
    "external_attention_channel_inaccessible": (
        "The selected channel cannot access one of the selected signal revisions."
    ),
    "external_attention_context_unpinned": (
        "An attended context ref was absent from the canonical inner-life snapshot."
    ),
    "external_attention_decision_source_unclosed": (
        "The decision did not cite every committed channel-authority proof."
    ),
    "external_attention_privacy_invalid": (
        "The live/shadow privacy field did not match this deployment capability."
    ),
    "external_attention_live_notes_required": (
        "A live perception selection needs non-empty epistemic notes."
    ),
    "external_attention_nonquotable_reproduced": (
        "The authored reading reproduced protected text from a non-quotable source."
    ),
}


class StructuredCharacterRoleFaculty:
    """The single injected character author behind ``CharacterInterior``."""

    name = "structured-character-role"
    VERSION = "structured-character-role.1"
    requires_author_lineage = True

    @property
    def author_identity(self) -> Mapping[str, object]:
        """Stable route identity bound into every InnerTurn id."""

        return {
            **self._semantic_author_identity,
            "name": self.name,
            "version": self.VERSION,
        }

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        model_id: str,
        model_version: str | None = None,
        temperature: float = 0.8,
        purpose_contracts: Sequence[PurposeDecisionContract] = (),
    ) -> None:
        if not callable(getattr(model, "complete", None)):
            raise TypeError("structured character role needs one completion model")
        if not model_id or len(model_id) > 256:
            raise ValueError("structured character role model id is invalid")
        if not 0 <= temperature <= 2:
            raise ValueError("structured character role temperature is invalid")
        contracts: dict[str, PurposeDecisionContract] = {
            item.purpose: item for item in _BUILTIN_CONTRACTS
        }
        for item in purpose_contracts:
            if item.purpose in contracts:
                raise ValueError(f"duplicate purpose decision contract: {item.purpose}")
            contracts[item.purpose] = item
        self._model = model
        self._model_id = model_id
        self._model_version = model_version or str(getattr(model, "model_version", model_id))
        self._semantic_author_identity = character_semantic_author_identity(
            model_id=self._model_id,
            model_version=self._model_version,
        )
        self._temperature = temperature
        self._contracts = contracts
        if bool(getattr(model, "supports_required_tool_choice", False)):
            # Pay canonical Pydantic schema/ref-closure cost at composition,
            # outside the first provider-entry and TTFT budgets.
            StructuredRoleToolContracts().precompile()
        # FacultyRegistry uses this frozen declaration for startup topology.
        # Unknown or unreviewed purposes never fall through to a generic role
        # surface; tests may add an explicit fixture-only contract.
        self.purposes = tuple(sorted(contracts))

    async def experience(self, request: _InteriorRoleRequest) -> Mapping[str, object]:
        if request.phase != "experience":
            raise ValueError("experience faculty received a non-experience request")
        return await self._complete(request)

    async def consider(self, request: _InteriorRoleRequest) -> Mapping[str, object]:
        if request.phase != "consider":
            raise ValueError("consider faculty received a non-consider request")
        return await self._complete(request)

    async def _complete(self, request: _InteriorRoleRequest) -> Mapping[str, object]:
        contract = self._resolve_contract(request)
        messages = self._messages(request, contract=contract)
        tool_contract = self._tool_contract(request)
        request_json = _canonical(
            self._request_identity_value(messages=messages, tool_contract=tool_contract)
        )
        request_hash = _hash_text(request_json)
        with model_call_scope("world_v2_character_interior"):
            raw = await complete_json_object(
                self._model,
                messages,
                temperature=self._temperature,
                tools=(list(tool_contract.provider_tools) if tool_contract is not None else None),
                tool_choice=(
                    tool_contract.provider_tool_choice if tool_contract is not None else None
                ),
            )
        try:
            result, response_hash = self._parse_and_validate(
                raw,
                request=request,
                contract=contract,
            )
        except StructuredRoleResultError as exc:
            import logging

            logging.getLogger(__name__).warning(
                "structured role validation rejected purpose=%s code=%s output_len=%d tail=%r detail=%s",
                request.purpose,
                exc.code,
                len(raw),
                raw[-120:],
                getattr(exc, "detail", str(exc))[:500],
            )
            raise
        model_call_id = self._model_call_id(
            request=request,
            request_hash=request_hash,
        )
        parent_model_call_id = None
        if request.correction_ordinal == 1:
            initial = request.model_copy(
                update={
                    "correction_ordinal": 0,
                    "correction_failure_code": None,
                }
            )
            initial_contract = self._resolve_contract(initial)
            initial_messages = self._messages(initial, contract=initial_contract)
            initial_tool_contract = self._tool_contract(initial)
            initial_hash = _hash_text(
                _canonical(
                    self._request_identity_value(
                        messages=initial_messages,
                        tool_contract=initial_tool_contract,
                    )
                )
            )
            parent_model_call_id = self._model_call_id(
                request=initial,
                request_hash=initial_hash,
            )
        lineage = _InteriorAuthorLineage(
            model_id=self._model_id,
            model_version=self._model_version,
            model_call_id=model_call_id,
            request_hash=request_hash,
            response_hash=response_hash,
            attempt_ordinal=request.correction_ordinal,
            parent_model_call_id=parent_model_call_id,
        )
        normalized_decision = self._normalize_decision(
            result.decision,
            request=request,
            contract=contract,
        )
        normalized = _InteriorRoleResult(
            status=result.status,
            summary=result.summary,
            attended_source_refs=tuple(result.attended_source_refs),
            decision=normalized_decision,
            recall_query=result.recall_query,
            proposals=self._normalize_proposals(
                result.proposals,
                request=request,
                contract=contract,
            ),
            author_lineage=lineage,
        )
        return normalized.model_dump(mode="python")

    def _tool_contract(
        self,
        request: _InteriorRoleRequest,
    ) -> StructuredRoleToolContract | None:
        if request.purpose not in {
            "proactive_contact",
            "world_stimulus_appraisal",
            "private_impression_reflection",
            "outcome_selection",
            "activity_lifecycle_choice",
            "life_development_choice",
            "expression_reconsideration",
            "fact_memory_retention",
            "experience_memory_retention",
        }:
            return None
        if not bool(getattr(self._model, "supports_required_tool_choice", False)):
            raise StructuredRoleResultError(
                "required_tool_choice_unsupported",
                detail=_FAILURE_DETAILS["required_tool_choice_unsupported"],
            )
        manifest = request.capability_manifest
        if manifest is None:
            raise StructuredRoleResultError(
                "capability_manifest_required",
                detail=_FAILURE_DETAILS["capability_manifest_required"],
            )
        try:
            compiler = StructuredRoleToolContracts()
            if request.purpose == "proactive_contact":
                return compiler.proactive_contact(
                    capability_payload=manifest.payload,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose == "world_stimulus_appraisal":
                return compiler.world_stimulus_appraisal(
                    capability_payload=manifest.payload,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose == "private_impression_reflection":
                return compiler.private_impression_reflection(
                    capability_payload=manifest.payload,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose == "activity_lifecycle_choice":
                return compiler.activity_lifecycle_choice(
                    capability_payload=manifest.payload,
                    source_refs=manifest.source_refs,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose == "life_development_choice":
                return compiler.life_development_choice(
                    capability_payload=manifest.payload,
                    source_refs=manifest.source_refs,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose == "expression_reconsideration":
                return compiler.expression_reconsideration(
                    capability_payload=manifest.payload,
                    source_refs=manifest.source_refs,
                    recall_allowed=not request.recall_completed,
                )
            if request.purpose in {
                "fact_memory_retention",
                "experience_memory_retention",
            }:
                return compiler.memory_retention(
                    purpose=request.purpose,
                    capability_payload=manifest.payload,
                    source_refs=manifest.source_refs,
                    recall_allowed=not request.recall_completed,
                )
            return compiler.outcome_selection(
                capability_payload=manifest.payload,
                source_refs=manifest.source_refs,
                recall_allowed=not request.recall_completed,
            )
        except (TypeError, ValueError) as exc:
            raise StructuredRoleResultError(
                "role_result_schema_invalid",
                detail=f"{request.purpose} forced-tool contract is invalid: {exc}",
            ) from exc

    @staticmethod
    def _request_identity_value(
        *,
        messages: list[dict[str, str]],
        tool_contract: StructuredRoleToolContract | None,
    ) -> object:
        if tool_contract is None:
            return messages
        return {
            "messages": messages,
            "tools": list(tool_contract.provider_tools),
            "tool_choice": tool_contract.provider_tool_choice,
            "tool_contract_identity": (tool_contract.identity.request_identity_material()),
        }

    def _messages(
        self,
        request: _InteriorRoleRequest,
        *,
        contract: PurposeDecisionContract,
    ) -> list[dict[str, str]]:
        allowed_statuses = sorted(self._allowed_statuses(request, contract=contract))
        snapshot = request.snapshot.model_view()
        capability = self._capability_view(request.capability_manifest)
        user_payload: dict[str, object] = {
            "inner_turn": {
                "inner_turn_id": request.inner_turn_id,
                "phase": request.phase,
                "subject_ref": request.subject_ref,
                "trigger_ref": request.trigger_ref,
                "purpose": request.purpose,
                "context_note": request.context_note,
                "subject_source_refs": list(request.subject_source_refs),
            },
            # This is the canonical, cursor-pinned environment presented to
            # the character.  It is not the character's instant private self:
            # that short-lived semantic state is authored by this very turn
            # and returned through ``summary``/``attended_source_refs``.
            "inner_life_snapshot": snapshot,
            "eight_facets": list(_FACET_NAMES),
            "selective_recall": {
                "available": not request.recall_completed,
                "result_status": "recall_request",
            },
            "capability_manifest": capability,
            "purpose_contract": self._contract_view(contract),
            "wire_contract": {
                "allowed_statuses": allowed_statuses,
                "fields": [
                    "status",
                    "summary",
                    "attended_source_refs",
                    "decision",
                    "recall_query",
                    "proposals",
                ],
                "decision_shape": {
                    "source_refs": "one_or_more_pinned_refs",
                    "payload": "one_json_object",
                },
                # Keep the transport envelope unambiguous for providers that
                # support JSON mode but not a provider-enforced JSON Schema.
                # This is a placement contract only: the values below are
                # placeholders, never a suggested character decision.
                "placement_rules": {
                    "generic_decision": (
                        "When status is decision, decision MUST be an object with exactly "
                        "source_refs (a non-empty list of supplied refs) and payload (one "
                        "purpose-specific JSON object). Put every purpose field inside "
                        "decision.payload; do not put timing, retention, life, or appraisal "
                        "fields directly inside decision."
                    ),
                    "typed_proposal": (
                        "When this purpose contract declares a proposal_type, keep decision "
                        "null and put the complete purpose-specific object in proposals. "
                        "Do not put the proposal's semantic decision string in the outer "
                        "decision field."
                    ),
                },
                "shape_example": {
                    "generic_decision": {
                        "status": "decision",
                        "summary": "<one short role-authored instant private self>",
                        "attended_source_refs": ["<one supplied pinned source ref>"],
                        "decision": {
                            "source_refs": ["<one supplied pinned source ref>"],
                            "payload": {"<purpose-specific field>": "<role-authored value>"},
                        },
                        "recall_query": None,
                        "proposals": [],
                    },
                    "typed_proposal": {
                        "status": "<transition or no_change as allowed>",
                        "summary": "<one short role-authored instant private self>",
                        "attended_source_refs": ["<one supplied pinned source ref>"],
                        "decision": None,
                        "recall_query": None,
                        "proposals": ["<one complete purpose-specific proposal object>"],
                    },
                },
                "summary_semantics": (
                    "one short instant private self: what currently matters, feels salient, "
                    "or is wanted/avoided; no hidden chain-of-thought and no invented fact"
                ),
            },
        }
        if request.correction_ordinal == 1:
            code = request.correction_failure_code or "role_result_schema_invalid"
            user_payload["correction"] = {
                "ordinal": 1,
                "failure_code": code,
                "failure_detail": request.correction_failure_detail
                or _FAILURE_DETAILS.get(code, code),
                "scope": "return_a_complete_new_result_for_the_same_pinned_request",
            }
        return [
            {
                "role": "system",
                "content": (
                    "You are the sole semantic author of this character's private deliberation. "
                    "The cursor-pinned inner_life_snapshot contains eight source-bound facets; "
                    "it is context, not a pre-authored instant private self or a behavior program. "
                    "Form the character's own instant motive, "
                    "attention, feeling, stance, and choice from that context. There is no preferred "
                    "conversational outcome, motive, tone, or action. Selective recall is optional "
                    "when available. The capability manifest describes only choices the system can "
                    "carry out. Return exactly one JSON object with the supplied wire fields, never "
                    "Markdown. For a generic decision, use the nested shape "
                    "decision:{source_refs:[...],payload:{...}} and put all purpose fields in "
                    "payload. For a typed-proposal purpose, keep decision null and put the "
                    "complete proposal in proposals. Never flatten a purpose payload into the "
                    "outer decision field. Cite only supplied source refs and capability tokens. Do not return "
                    "author audit fields; the trusted boundary adds those after the provider call."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    user_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]

    def _parse_and_validate(
        self,
        raw: object,
        *,
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
    ) -> tuple[_WireRoleResult, str]:
        if not isinstance(raw, str):
            raise StructuredRoleResultError(
                "role_result_not_text",
                detail=_FAILURE_DETAILS["role_result_not_text"],
            )
        response_hash = _hash_text(raw)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StructuredRoleResultError(
                "role_result_not_json",
                detail=_FAILURE_DETAILS["role_result_not_json"],
                response_hash=response_hash,
            ) from exc
        if not isinstance(decoded, dict):
            raise StructuredRoleResultError(
                "role_result_not_object",
                detail=_FAILURE_DETAILS["role_result_not_object"],
                response_hash=response_hash,
            )
        decoded = self._normalize_provider_wire_shape(
            decoded,
            request=request,
            contract=contract,
        )
        try:
            result = _WireRoleResult.model_validate(decoded)
        except ValidationError as exc:
            raise StructuredRoleResultError(
                "role_result_schema_invalid",
                detail=exc.json(include_url=False),
                response_hash=response_hash,
            ) from exc
        allowed = self._allowed_statuses(request, contract=contract)
        if result.status not in allowed:
            self._raise("phase_status_invalid", response_hash=response_hash)
        if result.status == "recall_request" and request.recall_completed:
            self._raise("repeated_recall_request", response_hash=response_hash)
        if not contract.proposals_allowed and result.proposals:
            self._raise("purpose_proposals_not_allowed", response_hash=response_hash)
        self._validate_proposals(
            result,
            request=request,
            contract=contract,
            response_hash=response_hash,
        )
        if set(result.attended_source_refs) - set(request.snapshot.source_refs):
            self._raise("attended_source_unpinned", response_hash=response_hash)
        if result.decision is not None:
            visible_refs = set(request.snapshot.source_refs)
            if request.capability_manifest is not None:
                visible_refs.update(request.capability_manifest.source_refs)
            if set(result.decision.source_refs) - visible_refs:
                self._raise("decision_source_unpinned", response_hash=response_hash)
            self._validate_decision_payload(
                result.decision.payload,
                decision_source_refs=frozenset(result.decision.source_refs),
                request=request,
                contract=contract,
                response_hash=response_hash,
            )
        return result, response_hash

    @staticmethod
    def _normalize_provider_wire_shape(
        decoded: dict[str, object],
        *,
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
    ) -> dict[str, object]:
        """Repair only an unambiguous provider transport wrapper.

        DeepSeek's JSON mode occasionally emits a complete purpose payload in
        the generic ``decision`` slot while omitting the envelope.  This
        adapter only moves an object when its own explicit source binding is
        already present; it must never promote attention refs or invent
        summary, refs, timing, silence, or any semantic field.  Proposal contracts have the inverse legacy
        shape: a duplicate semantic decision string appears beside an already
        complete typed proposal.  It is safe to discard that duplicate only
        when it exactly agrees with the typed proposal.
        """

        normalized = dict(decoded)
        raw_decision = normalized.get("decision")
        proposals = normalized.get("proposals")

        # Some providers use the generic status name for an experience turn
        # even though they supplied the complete typed proposal.  The typed
        # proposal itself is the semantic evidence; mapping only this phase
        # label keeps the proposal intact and does not select an outcome.
        if (
            request.phase == "experience"
            and normalized.get("status") == "decision"
            and contract.proposal_type is not None
            and isinstance(proposals, list)
            and len(proposals) == 1
        ):
            normalized["status"] = "transition"

        if isinstance(raw_decision, dict):
            if (
                contract.proposal_type is not None
                and isinstance(proposals, list)
                and not proposals
                and raw_decision.get("proposal_type") == contract.proposal_type
            ):
                # The typed proposal was placed in the generic slot.  Move
                # that same object to its declared collection without
                # changing any semantic value.
                normalized["proposals"] = [raw_decision]
                normalized["decision"] = None
                if request.phase == "experience":
                    normalized["status"] = "transition"
                return normalized
            # Already canonical.  Do not reinterpret a host-shaped or
            # otherwise partially wrapped object; strict validation should
            # explain the exact missing/extra field to the one correction.
            if "source_refs" in raw_decision or "payload" in raw_decision:
                if (
                    contract.purpose == "life_development_choice"
                    and isinstance(raw_decision.get("payload"), dict)
                    and "completion" not in raw_decision["payload"]
                ):
                    # Life providers sometimes use the generic envelope but
                    # place the complete character-choice object directly in
                    # ``payload``.  Keep the exact authored object and add
                    # only the purpose's declared transport key.
                    normalized["decision"] = {
                        **raw_decision,
                        "payload": {"completion": raw_decision["payload"]},
                    }
                return normalized
            # A bare decision has no explicit evidence binding.  Attention
            # refs are a separate model-authored signal and cannot be promoted
            # into decision evidence by the host; strict validation must send
            # this result through the bounded same-author correction instead.
            return normalized

        if isinstance(raw_decision, str) and contract.proposal_type is not None:
            proposals = normalized.get("proposals")
            if isinstance(proposals, list) and len(proposals) == 1:
                proposal = proposals[0]
                if (
                    isinstance(proposal, dict)
                    and proposal.get("proposal_type") == contract.proposal_type
                    and proposal.get("decision") == raw_decision
                ):
                    # Preserve the complete typed proposal and remove only
                    # its redundant primitive echo from the generic slot.
                    normalized["decision"] = None
        return normalized

    @staticmethod
    def _allowed_statuses(
        request: _InteriorRoleRequest,
        *,
        contract: PurposeDecisionContract,
    ) -> set[str]:
        if request.phase == "experience":
            return {"transition", "no_change", "recall_request"}
        if contract.decision_required or request.purpose in {
            "media_selection",
            "proactive_contact",
            "expression_reconsideration",
        }:
            # ``no_op`` is the character's explicit choice inside the media
            # decision payload.  A generic silent status would discard the
            # capability binding and make that choice unauditable.
            return {"decision", "recall_request"}
        return {"decision", "silent", "recall_request"}

    @classmethod
    def _validate_proposals(
        cls,
        result: _WireRoleResult,
        *,
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
        response_hash: str,
    ) -> None:
        if contract.proposal_type is None:
            return
        if contract.proposal_type == "world_stimulus_appraisal_result":
            if len(result.proposals) != 1:
                cls._raise("role_result_schema_invalid", response_hash=response_hash)
            try:
                proposal = _WorldStimulusAppraisalResult.model_validate(result.proposals[0])
            except ValidationError as exc:
                raise StructuredRoleResultError(
                    "role_result_schema_invalid",
                    detail=exc.json(include_url=False),
                    response_hash=response_hash,
                ) from exc
            expected_status = (
                "transition"
                if proposal.decision == "activate"
                or proposal.aspiration_transition is not None
                or proposal.experience_transition is not None
                else "no_change"
            )
            if result.status != expected_status:
                cls._raise("role_result_schema_invalid", response_hash=response_hash)
            manifest = request.capability_manifest
            if manifest is None or manifest.capability_kind != "world_stimulus_appraisal":
                cls._raise("capability_manifest_required", response_hash=response_hash)
            affect_capability = manifest.payload.get("affect_target_lower_bounds")
            raw_bounds = (
                affect_capability.get("bounds") if isinstance(affect_capability, dict) else None
            )
            minima = (
                {
                    item.get("dimension"): item.get("minimum_target_intensity_bp")
                    for item in raw_bounds
                    if isinstance(item, dict)
                    and isinstance(item.get("dimension"), str)
                    and isinstance(item.get("minimum_target_intensity_bp"), int)
                }
                if isinstance(raw_bounds, list)
                else {}
            )
            affect_transition = proposal.affect_transition
            raw_heads = manifest.payload.get("active_affect_heads")
            heads = (
                {
                    item.get("episode_id"): item
                    for item in raw_heads
                    if isinstance(item, dict) and isinstance(item.get("episode_id"), str)
                }
                if isinstance(raw_heads, list)
                else {}
            )
            if affect_transition is not None:
                invalid = False
                if isinstance(affect_transition, InteriorAffectOpenTransition):
                    invalid = not minima or any(
                        target.dimension not in minima
                        or target.target_intensity_bp < minima[target.dimension]
                        for target in affect_transition.component_targets
                    )
                else:
                    head = heads.get(affect_transition.episode_id)
                    invalid = head is None
                    if not invalid and isinstance(
                        affect_transition, InteriorAffectUpdateTransition
                    ):
                        assert isinstance(head, dict)
                        components = {
                            item.get("component_id"): item
                            for item in head.get("components", [])
                            if isinstance(item, dict) and isinstance(item.get("component_id"), str)
                        }
                        invalid = any(
                            (offered := components.get(target.component_id)) is None
                            or offered.get("dimension") != target.dimension
                            or target.target_intensity_bp
                            < offered.get("minimum_target_intensity_bp", 10_001)
                            for target in affect_transition.component_targets
                        )
                    elif not invalid and isinstance(
                        affect_transition, InteriorAffectSupersedeTransition
                    ):
                        invalid = not minima or any(
                            target.dimension not in minima
                            or target.target_intensity_bp < minima[target.dimension]
                            for target in affect_transition.component_targets
                        )
                if invalid:
                    cls._raise(
                        "world_stimulus_affect_target_outside_capability",
                        response_hash=response_hash,
                    )
            offered_subjects = manifest.payload.get("relationship_subject_refs")
            relationship_subjects = (
                {item for item in offered_subjects if isinstance(item, str)}
                if isinstance(offered_subjects, list)
                else set()
            )
            if (
                proposal.relationship_signal is not None
                and proposal.relationship_signal.subject_ref not in relationship_subjects
            ):
                cls._raise(
                    "world_stimulus_relationship_subject_outside_capability",
                    response_hash=response_hash,
                )
            if proposal.aspiration_transition is not None:
                current_source_refs = set(manifest.source_refs)
                raw_aspirations = manifest.payload.get("active_aspirations")
                active = (
                    {
                        item.get("aspiration_id"): item
                        for item in raw_aspirations
                        if isinstance(item, dict)
                        and isinstance(item.get("aspiration_id"), str)
                        and isinstance(item.get("authority_source_ref"), str)
                    }
                    if isinstance(raw_aspirations, list)
                    else {}
                )
                offered_source_refs = (
                    current_source_refs
                    | {str(item["authority_source_ref"]) for item in active.values()}
                    | {
                        str(item["planted_event_ref"])
                        for item in active.values()
                        if isinstance(item.get("planted_event_ref"), str)
                    }
                )
                transition = proposal.aspiration_transition
                selected_sources = set(transition.source_refs)
                if not current_source_refs.issubset(
                    selected_sources
                ) or not selected_sources.issubset(offered_source_refs):
                    cls._raise(
                        "world_stimulus_aspiration_source_outside_capability",
                        response_hash=response_hash,
                    )
                if transition.operation == "plant":
                    if transition.aspiration_id is not None:
                        cls._raise(
                            "world_stimulus_aspiration_target_outside_capability",
                            response_hash=response_hash,
                        )
                else:
                    selected = active.get(transition.aspiration_id)
                    if (
                        selected is None
                        or selected["authority_source_ref"] not in selected_sources
                        or selected.get("planted_event_ref") not in selected_sources
                    ):
                        cls._raise(
                            "world_stimulus_aspiration_target_outside_capability",
                            response_hash=response_hash,
                        )
            if proposal.experience_transition is not None:
                raw_capability = manifest.payload.get("experience_transitions")
                try:
                    transition_capability = ExperienceTransitionCapability.model_validate_json(
                        _canonical(raw_capability)
                    )
                    validate_experience_transition_draft(
                        proposal.experience_transition,
                        capability=transition_capability,
                    )
                except (TypeError, ValueError, ValidationError) as exc:
                    raise StructuredRoleResultError(
                        "world_stimulus_experience_transition_outside_capability",
                        detail=str(exc),
                        response_hash=response_hash,
                    ) from exc
            return
        if result.status == "no_change" and not result.proposals:
            return
        if result.status != "transition" or len(result.proposals) != 1:
            cls._raise("role_result_schema_invalid", response_hash=response_hash)
        manifest = request.capability_manifest
        if manifest is None:
            cls._raise("capability_manifest_required", response_hash=response_hash)
        raw_proposal = result.proposals[0]
        if isinstance(raw_proposal, dict):
            # The private-impression capability offers the model short,
            # position-stable tokens ("s0", "s1", ...) instead of very long
            # source refs.  Map any short tokens back to the real refs before
            # validation so the rest of the pipeline only ever sees real
            # source identities; the mapping is deterministic from capsule
            # source order.
            raw_token_map = manifest.payload.get("token_map")
            if isinstance(raw_token_map, dict):

                def _map_refs(refs: object) -> object:
                    if not isinstance(refs, list):
                        return refs
                    mapped: list[object] = []
                    for item in refs:
                        if isinstance(item, str) and item in raw_token_map:
                            mapped.append(raw_token_map[item])
                        else:
                            mapped.append(item)
                    return mapped

                mapped_proposal = dict(raw_proposal)
                for field in ("source_refs", "predecessor_refs"):
                    if field in mapped_proposal:
                        mapped_proposal[field] = _map_refs(mapped_proposal[field])
                raw_proposal = mapped_proposal
        try:
            proposal = _PrivateImpressionProposal.model_validate(raw_proposal)
        except ValidationError as exc:
            raise StructuredRoleResultError(
                "role_result_schema_invalid",
                detail=exc.json(include_url=False),
                response_hash=response_hash,
            ) from exc
        raw_sources = manifest.payload.get("reflection_sources")
        offered = (
            {
                item.get("source_ref")
                for item in raw_sources
                if isinstance(item, dict) and isinstance(item.get("source_ref"), str)
            }
            if isinstance(raw_sources, list)
            else set()
        )
        if not offered:
            raw_tokens = manifest.payload.get("offered_tokens")
            offered = (
                {item for item in raw_tokens if isinstance(item, str)}
                if isinstance(raw_tokens, list)
                else set()
            )
        if set(proposal.source_refs) - offered:
            cls._raise("selected_token_not_offered", response_hash=response_hash)
        if set(proposal.predecessor_refs) - offered:
            cls._raise("selected_token_not_offered", response_hash=response_hash)
        anchor_refs = manifest.payload.get("anchor_source_refs")
        anchors = (
            {item for item in anchor_refs if isinstance(item, str)}
            if isinstance(anchor_refs, list)
            else set()
        )
        if anchors and not (set(proposal.source_refs) & anchors):
            cls._raise("selected_token_required", response_hash=response_hash)

    def _resolve_contract(self, request: _InteriorRoleRequest) -> PurposeDecisionContract:
        exact = self._contracts.get(request.purpose)
        if exact is None:
            raise StructuredRoleResultError(
                "unsupported_purpose_contract",
                detail=_FAILURE_DETAILS["unsupported_purpose_contract"],
            )
        contract = exact
        if contract.capability_kind is None:
            if request.capability_manifest is not None and contract.purpose == "generic":
                raise StructuredRoleResultError(
                    "unsupported_purpose_contract",
                    detail=_FAILURE_DETAILS["unsupported_purpose_contract"],
                )
            return contract
        manifest = request.capability_manifest
        if manifest is None:
            raise StructuredRoleResultError(
                "capability_manifest_required",
                detail=_FAILURE_DETAILS["capability_manifest_required"],
            )
        if manifest.capability_kind != contract.capability_kind:
            raise StructuredRoleResultError(
                "capability_kind_mismatch",
                detail=_FAILURE_DETAILS["capability_kind_mismatch"],
            )
        return contract

    def _validate_decision_payload(
        self,
        payload: Mapping[str, object],
        *,
        decision_source_refs: frozenset[str],
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
        response_hash: str,
    ) -> None:
        if "contract" in payload:
            self._raise("payload_contract_reserved", response_hash=response_hash)
        if request.purpose == "external_perception_attention":
            self._validate_external_attention_payload(
                payload,
                decision_source_refs=decision_source_refs,
                request=request,
                response_hash=response_hash,
            )
            return
        offered = self._offered_tokens(request.capability_manifest, contract=contract)
        if contract.selected_token_required:
            token = payload.get("selected_token")
            if not isinstance(token, str) or not token:
                self._raise("selected_token_required", response_hash=response_hash)
            if token not in offered:
                self._raise("selected_token_not_offered", response_hash=response_hash)
        if contract.validator is not None:
            try:
                contract.validator(payload, offered)
            except StructuredRoleResultError:
                raise
            except (TypeError, ValueError) as exc:
                raise StructuredRoleResultError(
                    "role_result_schema_invalid",
                    detail=str(exc),
                    response_hash=response_hash,
                ) from exc
        if request.purpose == "life_development_choice":
            manifest = request.capability_manifest
            assert manifest is not None
            _normalized_life_development_completion(
                payload,
                manifest=manifest,
                response_hash=response_hash,
            )
        if request.purpose == "outcome_selection":
            manifest = request.capability_manifest
            assert manifest is not None
            direction_value = payload.get("character_life_direction")
            if (
                manifest.payload.get("allow_character_life_direction") is False
                and direction_value is not None
            ):
                raise StructuredRoleResultError(
                    "role_result_schema_invalid",
                    detail="this outcome capability does not authorize a life direction",
                    response_hash=response_hash,
                )
            if direction_value is not None:
                try:
                    from ..character_outcome_contract import CharacterLifeDirectionDraft

                    direction = CharacterLifeDirectionDraft.model_validate(
                        direction_value,
                        strict=True,
                    )
                    raw_coordinates = manifest.payload.get("current_coordinates")
                    coordinates = raw_coordinates if isinstance(raw_coordinates, list) else []
                    for coordinate in coordinates:
                        if not isinstance(coordinate, dict):
                            raise ValueError("current coordinate is malformed")
                        prefixes = coordinate.get("replaces_context_tag_prefixes")
                        coordinate_ref = coordinate.get("coordinate_ref")
                        if not isinstance(prefixes, list) or not isinstance(coordinate_ref, str):
                            raise ValueError("current coordinate is malformed")
                        if (
                            set(prefixes) & set(direction.replaces_context_tag_prefixes)
                            and coordinate_ref != direction.coordinate_ref
                        ):
                            raise ValueError(
                                "character direction must preserve the overlapping coordinate ref"
                            )
                except (TypeError, ValueError) as exc:
                    raise StructuredRoleResultError(
                        "role_result_schema_invalid",
                        detail=str(exc),
                        response_hash=response_hash,
                    ) from exc
        if request.purpose == "media_selection" and payload.get("decision") == "select":
            manifest = request.capability_manifest
            assert manifest is not None
            selected_token = payload.get("selected_token")
            raw_candidates = manifest.payload.get("candidates")
            candidates = raw_candidates if isinstance(raw_candidates, list) else []
            selected = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict) and item.get("token") == selected_token
                ),
                None,
            )
            raw_refs = selected.get("source_refs") if isinstance(selected, dict) else []
            candidate_refs = (
                {ref for ref in raw_refs if isinstance(ref, str) and ref}
                if isinstance(raw_refs, list)
                else set()
            )
            if candidate_refs - decision_source_refs:
                self._raise(
                    "media_selection_source_unclosed",
                    response_hash=response_hash,
                )

    @classmethod
    def _validate_external_attention_payload(
        cls,
        payload: Mapping[str, object],
        *,
        decision_source_refs: frozenset[str],
        request: _InteriorRoleRequest,
        response_hash: str,
    ) -> None:
        manifest = request.capability_manifest
        if manifest is None:
            cls._raise("capability_manifest_required", response_hash=response_hash)
        capability = manifest.payload
        raw_candidates = capability.get("candidates")
        deployment_mode = capability.get("deployment_mode")
        if not isinstance(raw_candidates, list) or deployment_mode not in {"shadow", "live"}:
            cls._raise(
                "external_attention_result_shape_invalid",
                response_hash=response_hash,
            )
        candidates: dict[str, Mapping[str, object]] = {}
        for raw_candidate in raw_candidates:
            if not isinstance(raw_candidate, dict):
                cls._raise(
                    "external_attention_result_shape_invalid",
                    response_hash=response_hash,
                )
            candidate_ref = raw_candidate.get("candidate_ref")
            candidate_token = raw_candidate.get("candidate_token")
            if (
                not isinstance(candidate_ref, str)
                or candidate_token != candidate_ref
                or candidate_ref in candidates
            ):
                cls._raise(
                    "external_attention_result_shape_invalid",
                    response_hash=response_hash,
                )
            candidates[candidate_ref] = raw_candidate
        raw_selections = payload.get("selections")
        if not isinstance(raw_selections, list) or set(payload) != {"selections"}:
            cls._raise(
                "external_attention_result_shape_invalid",
                response_hash=response_hash,
            )
        maximum = 12 if deployment_mode == "live" else 16
        if len(raw_selections) > maximum:
            cls._raise(
                "external_attention_result_shape_invalid",
                response_hash=response_hash,
            )
        try:
            selections = tuple(
                _ExternalAttentionSelection.model_validate(item) for item in raw_selections
            )
        except ValidationError as exc:
            raise StructuredRoleResultError(
                "external_attention_result_shape_invalid",
                detail=exc.json(include_url=False),
                response_hash=response_hash,
            ) from exc
        seen: set[str] = set()
        snapshot_refs = set(request.snapshot.source_refs)
        durable = capability.get("durable_snapshots")
        durable_by_revision = (
            {
                item["signal_revision_ref"]: item
                for item in durable
                if isinstance(item, dict) and isinstance(item.get("signal_revision_ref"), str)
            }
            if isinstance(durable, list)
            else {}
        )
        for selection in selections:
            if selection.candidate_ref in seen:
                cls._raise(
                    "external_attention_duplicate_candidate",
                    response_hash=response_hash,
                )
            seen.add(selection.candidate_ref)
            candidate = candidates.get(selection.candidate_ref)
            if candidate is None:
                cls._raise(
                    "external_attention_candidate_not_offered",
                    response_hash=response_hash,
                )
            raw_revisions = candidate.get("exact_signal_revisions")
            offered_revisions = (
                {item for item in raw_revisions if isinstance(item, str)}
                if isinstance(raw_revisions, list)
                else set()
            )
            if not set(selection.exact_signal_revision_refs) <= offered_revisions:
                cls._raise(
                    "external_attention_revision_not_offered",
                    response_hash=response_hash,
                )
            raw_channels = candidate.get("accessible_channels")
            channels = (
                {
                    item["channel_ref"]: item
                    for item in raw_channels
                    if isinstance(item, dict) and isinstance(item.get("channel_ref"), str)
                }
                if isinstance(raw_channels, list)
                else {}
            )
            channel = channels.get(selection.selected_channel_ref)
            if channel is None:
                cls._raise(
                    "external_attention_channel_not_offered",
                    response_hash=response_hash,
                )
            raw_material = candidate.get("model_visible_material")
            revision_sources = (
                {
                    item["signal_revision_ref"]: item.get("source_id")
                    for item in raw_material
                    if isinstance(item, dict) and isinstance(item.get("signal_revision_ref"), str)
                }
                if isinstance(raw_material, list)
                else {}
            )
            accessible_sources = channel.get("accessible_source_ids")
            accessible = (
                {item for item in accessible_sources if isinstance(item, str)}
                if isinstance(accessible_sources, list)
                else set()
            )
            if any(
                revision_sources.get(revision) not in accessible
                for revision in selection.exact_signal_revision_refs
            ):
                cls._raise(
                    "external_attention_channel_inaccessible",
                    response_hash=response_hash,
                )
            if set(selection.attended_context_refs) - snapshot_refs:
                cls._raise(
                    "external_attention_context_unpinned",
                    response_hash=response_hash,
                )
            channel_proofs = channel.get("evidence_refs")
            # Signal revisions are sidecar capability tokens, already closed
            # over the manifest payload and (live) durable snapshot hashes.
            # Only committed channel authority refs belong in source_refs.
            required_sources: set[str] = set()
            if isinstance(channel_proofs, list):
                required_sources.update(item for item in channel_proofs if isinstance(item, str))
            if not required_sources <= decision_source_refs:
                cls._raise(
                    "external_attention_decision_source_unclosed",
                    response_hash=response_hash,
                )
            if deployment_mode == "live":
                if selection.privacy_class is None:
                    cls._raise(
                        "external_attention_privacy_invalid",
                        response_hash=response_hash,
                    )
                if not selection.epistemic_notes.strip():
                    cls._raise(
                        "external_attention_live_notes_required",
                        response_hash=response_hash,
                    )
                authored = selection.subjective_summary + "\n" + selection.epistemic_notes
                for revision_ref in selection.exact_signal_revision_refs:
                    source = durable_by_revision.get(revision_ref)
                    if (
                        source is not None
                        and source.get("may_quote") is False
                        and (
                            cls._reproduces_non_quotable_text(
                                output=authored,
                                source=source.get("headline"),
                                minimum_protected_characters=4,
                            )
                            or cls._reproduces_non_quotable_text(
                                output=authored,
                                source=source.get("licensed_summary"),
                            )
                        )
                    ):
                        cls._raise(
                            "external_attention_nonquotable_reproduced",
                            response_hash=response_hash,
                        )
            elif selection.privacy_class is not None:
                cls._raise(
                    "external_attention_privacy_invalid",
                    response_hash=response_hash,
                )

    @staticmethod
    def _reproduces_non_quotable_text(
        *,
        output: str,
        source: object,
        minimum_protected_characters: int = 8,
    ) -> bool:
        if not isinstance(source, str):
            return False
        normalize = lambda value: "".join(  # noqa: E731 - tiny local transform
            unicodedata.normalize("NFKC", value).casefold().split()
        )
        protected = normalize(source)
        candidate = normalize(output)
        if len(protected) < minimum_protected_characters:
            return False
        if len(protected) <= 32:
            return protected in candidate
        return any(
            protected[index : index + 32] in candidate for index in range(len(protected) - 31)
        )

    @staticmethod
    def _offered_tokens(
        manifest: _InteriorCapabilityManifest | None,
        *,
        contract: PurposeDecisionContract,
    ) -> frozenset[str]:
        if not contract.offered_token_fields:
            return frozenset()
        if manifest is None:
            raise StructuredRoleResultError(
                "capability_manifest_required",
                detail=_FAILURE_DETAILS["capability_manifest_required"],
            )
        tokens: set[str] = set()
        payload = manifest.payload
        for field in contract.offered_token_fields:
            raw_items = payload.get(field)
            if not isinstance(raw_items, list):
                continue
            for item in raw_items:
                if isinstance(item, str) and item:
                    tokens.add(item)
                elif isinstance(item, dict):
                    for key in (
                        "token",
                        "candidate_token",
                        "signal_token",
                        "attachment_token",
                        "attachment_ref",
                    ):
                        value = item.get(key)
                        if isinstance(value, str) and value:
                            tokens.add(value)
                            break
        if not tokens:
            raise StructuredRoleResultError(
                "capability_manifest_missing_offered_tokens",
                detail=_FAILURE_DETAILS["capability_manifest_missing_offered_tokens"],
            )
        return frozenset(tokens)

    @staticmethod
    def _normalize_decision(
        decision: _WireDecision | None,
        *,
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
    ) -> dict[str, object] | None:
        if decision is None:
            return None
        normalized: dict[str, object] = {
            "contract": "character-interior-purpose-decision.1",
            "purpose": request.purpose,
            "source_refs": list(decision.source_refs),
        }
        if request.capability_manifest is not None:
            normalized.update(
                {
                    "capability_ref": request.capability_manifest.capability_ref,
                    "capability_payload_hash": request.capability_manifest.payload_hash,
                }
            )
        payload = dict(decision.payload)
        if request.purpose == "life_development_choice":
            manifest = request.capability_manifest
            assert manifest is not None
            payload["completion"] = _normalized_life_development_completion(
                payload,
                manifest=manifest,
            )
        normalized["payload"] = {"contract": contract.payload_contract, **payload}
        return normalized

    @staticmethod
    def _normalize_proposals(
        proposals: Sequence[Mapping[str, object]],
        *,
        request: _InteriorRoleRequest,
        contract: PurposeDecisionContract,
    ) -> tuple[dict[str, object], ...]:
        if not proposals or contract.proposal_type is None:
            return tuple(dict(item) for item in proposals)
        manifest = request.capability_manifest
        assert manifest is not None
        if contract.proposal_type == "world_stimulus_appraisal_result":
            world_result = _WorldStimulusAppraisalResult.model_validate(proposals[0])
            value = world_result.model_dump(mode="json")
            value.pop("proposal_type")
            return (
                {
                    "contract": "character-interior-typed-proposal.1",
                    "proposal_type": contract.proposal_type,
                    "purpose": request.purpose,
                    "source_refs": list(manifest.source_refs),
                    "capability_ref": manifest.capability_ref,
                    "capability_payload_hash": manifest.payload_hash,
                    "payload": {
                        "contract": contract.proposal_payload_contract,
                        **value,
                    },
                },
            )
        proposal = _PrivateImpressionProposal.model_validate(proposals[0])
        # The private-impression capability hands the model short
        # position-stable tokens; map them back to the real source refs before
        # the proposal payload is persisted, so authority validation and the
        # reducer only ever see canonical source identities.
        manifest_raw_token_map = manifest.payload.get("token_map")
        if isinstance(manifest_raw_token_map, dict) and (
            any(item in manifest_raw_token_map for item in proposal.source_refs)
            or any(item in manifest_raw_token_map for item in proposal.predecessor_refs)
        ):
            proposal = proposal.model_copy(
                update={
                    "source_refs": [
                        manifest_raw_token_map.get(item, item) for item in proposal.source_refs
                    ],
                    "predecessor_refs": [
                        manifest_raw_token_map.get(item, item) for item in proposal.predecessor_refs
                    ],
                }
            )
        value = proposal.model_dump(mode="json")
        value.pop("proposal_type")
        return (
            {
                "contract": "character-interior-typed-proposal.1",
                "proposal_type": contract.proposal_type,
                "purpose": request.purpose,
                # Trusted evidence closure belongs to the capability, never
                # to model-selected reflection tokens inside the payload.
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": contract.proposal_payload_contract,
                    **value,
                },
            },
        )

    @staticmethod
    def _capability_view(
        manifest: _InteriorCapabilityManifest | None,
    ) -> dict[str, object]:
        if manifest is None:
            return {"availability": "unavailable"}
        return {
            "availability": "available",
            "capability_ref": manifest.capability_ref,
            "capability_kind": manifest.capability_kind,
            "payload_hash": manifest.payload_hash,
            "payload": dict(manifest.payload),
            "source_refs": list(manifest.source_refs),
        }

    @staticmethod
    def _contract_view(contract: PurposeDecisionContract) -> dict[str, object]:
        view: dict[str, object] = {
            "purpose": contract.purpose,
            "payload_contract": contract.payload_contract,
            "payload_is_open_json_object": True,
        }
        if contract.capability_kind is not None:
            view["capability_kind"] = contract.capability_kind
        if contract.selected_token_required:
            view["selected_token"] = "one token from capability_manifest.payload"
        view["proposals"] = "allowed" if contract.proposals_allowed else "must_be_empty"
        if contract.purpose == "media_selection":
            view["payload_schema"] = {
                "decision": "select|no_op",
                "selected_token": "required only for select; one offered token",
            }
        if contract.purpose == "external_perception_attention":
            view["payload_schema"] = {
                "selections": [
                    {
                        "candidate_ref": "one offered candidate_token",
                        "exact_signal_revision_refs": ["one or more revisions from that candidate"],
                        "selected_channel_ref": "one channel from that candidate",
                        "subjective_summary": "the character's own fallible reading",
                        "epistemic_notes": "uncertainty noticed in the evidence",
                        "attended_context_refs": ["optional canonical inner-life source refs"],
                        "privacy_class": "live only: public|shareable|personal|private|withhold",
                    }
                ],
                "selection_count": "zero_or_many",
            }
        if contract.purpose == "proactive_contact":
            view["payload_schema"] = {
                "timing_choice": "now|later|silent",
                "beats": "zero only for silent; otherwise one or more offered expression beats",
                "delay_seconds": "later only",
                "expires_after_seconds": "later only",
                "stance": "free text",
                "brief_rationale": "short free text",
                "impulse_summary": "free text",
                "confidence": "integer 0..10000",
                "world_claims": [
                    {
                        "claim_text": "one concrete claim",
                        "scope": (
                            "current_world|past_world|counterpart_history|"
                            "shared_history|stable_identity"
                        ),
                        "source_refs": ["one supplied matching pinned ref"],
                    }
                ],
                "world_claims_rule": (
                    "Use objects, never strings. Only factual claims need entries; "
                    "feelings and hypothetical impulses use no claim. Every grounded "
                    "claim cites at least one matching supplied source ref; use [] when "
                    "the beats contain no factual claim."
                ),
            }
        if contract.purpose == "expression_reconsideration":
            view["payload_schema"] = {
                "disposition": "continue|cancel|defer|merge|supersede|new_beat"
            }
        if contract.purpose == "private_impression_reflection":
            view["status_schema"] = {
                "no_change": "proposals must be []",
                "transition": "exactly one private_impression_transition proposal",
            }
            view["proposal_schema"] = {
                "proposal_type": "private_impression_transition",
                "decision": "retain|consolidate|supersede",
                "predecessor_refs": (
                    "selected existing-impression short tokens from "
                    "capability_manifest.payload.short_tokens"
                ),
                "source_refs": (
                    "selected offered short tokens from "
                    "capability_manifest.payload.short_tokens; at least one "
                    "anchor_short_tokens entry must be included"
                ),
                "cross_field_rules": (
                    "predecessor_refs are the exact offered short tokens, every "
                    "predecessor must also be listed in source_refs, retain has no "
                    "predecessors, and consolidate/supersede has at least one"
                ),
                "reflection_summary": "free tentative private reading",
                "confidence_bp": "integer 0..10000",
                "expiry_condition": "one offered lifecycle condition",
            }
        if contract.purpose == "world_stimulus_appraisal":
            view["status_schema"] = {
                "no_change": (
                    "one result proposal whose decision is no_change and whose "
                    "aspiration_transition and experience_transition are null"
                ),
                "transition": (
                    "one result proposal whose decision is activate or whose optional "
                    "aspiration_transition or experience_transition is present"
                ),
            }
            view["proposal_example_activate"] = {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": "activate",
                "brief_rationale": "This settled moment matters to me; I want to acknowledge it privately.",
                "behavior_tendency": "Stay quietly open to similar moments.",
                "stance": "Gently moved; this aligns with what I value.",
                "display_strategy": "Keep it private; no need to announce it.",
                "confidence": 7000,
                "meaning_candidates": [
                    {"meaning": "A small moment of genuine connection", "confidence": 6000},
                    {
                        "meaning": "A reminder that quiet shared experiences matter",
                        "confidence": 4000,
                    },
                ],
                "attribution": "situation",
                "severity": 4000,
                "expiry": "2026-08-09T12:00:00Z",
                "affect_transition": {
                    "operation": "open",
                    "component_targets": [{"dimension": "warmth", "target_intensity_bp": 6000}],
                },
                "relationship_signal": None,
                "aspiration_transition": None,
                "experience_transition": None,
            }
            view["proposal_example_no_change"] = {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": "no_change",
                "brief_rationale": "This happened but it does not change how I feel.",
                "behavior_tendency": "Continue as before.",
                "stance": "Unchanged.",
                "display_strategy": "No outward change.",
                "confidence": 5000,
                "meaning_candidates": None,
                "attribution": None,
                "severity": None,
                "expiry": None,
                "affect_transition": None,
                "relationship_signal": None,
                "aspiration_transition": None,
                "experience_transition": None,
            }
            view["proposal_schema"] = {
                "proposal_type": "world_stimulus_appraisal_result",
                "decision": (
                    "no_change means the character has no appraisal at all: "
                    "meaning_candidates, attribution, severity, expiry, "
                    "affect_transition, relationship_signal, aspiration_transition "
                    "and experience_transition must all be null; "
                    "activate means the character does appraise it: those fields "
                    "may be present. Pick exactly one. Return the exact "
                    "proposal_example_activate shape when activating, or the exact "
                    "proposal_example_no_change shape otherwise"
                ),
                "brief_rationale": "the character's short private reason (required)",
                "behavior_tendency": "free text (required, even for no_change)",
                "stance": "free text (required, even for no_change)",
                "display_strategy": "free text (required, even for no_change)",
                "confidence": "integer 0..10000",
                "meaning_candidates": (
                    'activate only: one or more objects, each with "meaning": a short '
                    "free-text tentative interpretation grounded in the supplied source "
                    'context, and "confidence": an integer 0..10000 private weight; '
                    "meanings must be unique and are not enum labels or facts about "
                    "another person"
                ),
                "attribution": "activate only: user|companion|npc|situation|third_party|unknown",
                "severity": "activate only: integer 0..10000",
                "expiry": "activate only: ISO-8601 datetime string or null",
                "affect_transition": {
                    "availability": "activate only; null means no Affect lifecycle change",
                    "operation": "open|update|resolve|supersede",
                    "open": (
                        "component_targets with unique dimension and target_intensity_bp; "
                        "dimension must be one exact token of hurt, anger, sadness, "
                        "loneliness, anxiety, resentment, warmth, joy; targets must "
                        "satisfy affect_target_lower_bounds"
                    ),
                    "update": (
                        "one episode_id from active_affect_heads plus component_targets "
                        "that name exact offered component_id, dimension and intensity"
                    ),
                    "resolve": (
                        "one episode_id from active_affect_heads and a free-text resolution_summary"
                    ),
                    "supersede": (
                        "one episode_id from active_affect_heads plus new successor "
                        "component_targets"
                    ),
                },
                "relationship_signal": (
                    "activate only: optional source-bound relationship signal for one "
                    "supplied relationship_subject_ref; null means no relationship change"
                ),
                "aspiration_transition": {
                    "availability": "optional for either appraisal decision",
                    "operation": "plant|reinforce|revise|abandon",
                    "aspiration_id": "null for plant; otherwise one supplied active id",
                    "text": "free text for plant/revise; otherwise null",
                    "privacy_class": "plant/revise only: public|shareable|personal|private|withhold",
                    "tension_summary": "optional free internal conflict, not a category",
                    "tension_source_refs": "sources supporting that tension or []",
                    "source_refs": "current stimulus plus any selected aspiration authority",
                    "reason_summary": "short free private reason",
                },
                "experience_transition": {
                    "availability": (
                        "optional one-of goal|thread|commitment|memory_candidate; null is "
                        "always allowed; select only exact heads/sources and operations "
                        "listed by capability_manifest.payload.experience_transitions"
                    ),
                    "goal": (
                        "pause|resume|abandon an offered head; creating a Goal is unavailable "
                        "because no Goal content authority is installed"
                    ),
                    "thread": "open from the current source, or update|resolve|cancel an offered head",
                    "commitment": (
                        "open only against an offered open Thread fulfillment contract, or "
                        "release an offered commitment head"
                    ),
                    "memory_candidate": (
                        "retain only the offered exact Fact, Experience, or terminal Thread source"
                    ),
                    "source_refs": "the exact closure required by the selected capability",
                    "reason_summary": "the character's free private reason, not a system motive code",
                },
            }
        if contract.purpose == "life_development_choice":
            view["payload_schema"] = {"completion": "one complete JSON object"}
        if contract.purpose == "activity_lifecycle_choice":
            view["payload_schema"] = {
                "decision": "select|no_op",
                "selected_token": "select only: one offered activity token",
            }
        if contract.purpose == "outcome_selection":
            view["payload_schema"] = {
                "selected_token": "exactly one offered outcome token",
                "character_life_direction": (
                    "null or one freely authored, structurally closed subjective direction"
                ),
            }
        if contract.purpose in {
            "fact_memory_retention",
            "experience_memory_retention",
        }:
            view["payload_schema"] = {
                "retain": "boolean",
                "when_retain_true": [
                    "cue_kind",
                    "retention_rationales",
                    "salience",
                ],
                "when_retain_false": "no other fields",
            }
        if contract.purpose == "memory_withdrawal_review":
            view["payload_schema"] = {"selected_token": "one offered retain|forget|revise token"}
        return view

    @staticmethod
    def _model_call_id(
        *,
        request: _InteriorRoleRequest,
        request_hash: str,
    ) -> str:
        identity = {
            "inner_turn_id": request.inner_turn_id,
            "phase": request.phase,
            "purpose": request.purpose,
            "attempt_ordinal": request.correction_ordinal,
            "request_hash": request_hash,
        }
        return "model-call:character-interior:" + _hash_text(_canonical(identity))

    @staticmethod
    def _raise(code: str, *, response_hash: str) -> None:
        import logging

        logging.getLogger(__name__).warning(
            "structured role wire rejected code=%s detail=%s",
            code,
            _FAILURE_DETAILS.get(code, "")[:300],
        )
        raise StructuredRoleResultError(
            code,
            detail=_FAILURE_DETAILS[code],
            response_hash=response_hash,
        )


__all__ = [
    "PurposeDecisionContract",
    "StructuredCharacterRoleFaculty",
    "StructuredRoleResultError",
]
