"""Model-owned expression choice with deployment-owned capability materialization.

The model chooses *whether* and *how* to express itself from the supplied
situation.  This module owns the less interesting but security-sensitive work:
provider message binding, immutable payload bytes, dependency ordering and
relative due windows.  Platform profiles describe executable vocabulary; they
never prescribe which social response should be selected.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal, NamedTuple

from pydantic import Field, ValidationError, model_validator

from .biographical_claim_authority import (
    biographical_coordinate_authorities,
    biographical_parent_attention_refs,
)
from .deliberation import ModelInput
from .expression_cadence import (
    CADENCE_POLICY_VERSION,
    CadenceDraw,
    CadenceProfile,
    cadence_windows,
)
from .expression_payload_contract import QQ_REACTION_OPTIONS, QQ_STICKER_OPTIONS
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    ResponseExpectationAssessmentDraft,
    TypedChange,
    VariationProfile,
)
from .private_turn_state import (
    PrivateTurnState,
    validate_private_turn_state_sources,
)
from .schema_core import FrozenModel


ExpressionModality = Literal["text", "reaction", "sticker", "typing"]
TimingChoice = Literal["now", "later", "silent"]
TurnPosture = Literal["yield", "continue", "interject", "supersede"]
EXPRESSION_DELAY_MAX_SECONDS = 86_400
RESPONSE_EXPECTATION_WAIT_MAX_SECONDS = 86_400
# Compatibility only: older role-model prompts emitted one of these host-owned
# rhetorical labels. New prompts and schemas do not expose the taxonomy; the
# exact historical values are discarded before validation so old provider
# echoes converge on the same new proposal identity.
_LEGACY_EXPRESSION_BEAT_ROLES = frozenset(
    ("opening", "substantive", "challenge", "self_correction", "afterthought")
)
_EVENT_EVIDENCE_KIND = {
    "ObservationRecorded": "observed_message",
    "FactCommitted": "committed_fact",
    "FactCorrected": "committed_fact",
    "FactWithdrawn": "committed_fact",
    "ExperienceCommitted": "committed_experience",
    "WorldOccurrenceSettled": "settled_world_event",
    "ActivityPlanned": "active_plan",
    "ActivityStarted": "active_plan",
    "ActivityPaused": "active_plan",
    "ActivityResumed": "active_plan",
}


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class ExpressionOption(FrozenModel):
    """One executable token plus a model-facing semantic label."""

    option_id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=128)


class ExpressionDraftCapabilities(FrozenModel):
    """Deployment fact shared by prompt grammar and Acceptance grammar."""

    profile_id: str = Field(min_length=1, max_length=128)
    modalities: tuple[ExpressionModality, ...] = Field(min_length=1, max_length=4)
    reaction_options: tuple[ExpressionOption, ...] = ()
    sticker_options: tuple[ExpressionOption, ...] = ()
    max_beats: int = Field(default=8, ge=1, le=16)
    # Deferred settlement closes over the whole dependency-ordered plan.  The
    # model therefore owns the same one-to-many message-count choice for
    # ``later`` as it does for ``now``.
    max_later_beats: int = Field(default=8, ge=1, le=16)
    private_turn_state_mode: Literal["legacy_optional", "required"] = "legacy_optional"
    recorded_cadence_mode: Literal["off", "shadow", "on"] = "off"
    cadence_policy_version: Literal["expression-cadence.1"] = CADENCE_POLICY_VERSION

    @model_validator(mode="after")
    def option_sets_match_modalities(self) -> "ExpressionDraftCapabilities":
        if (
            self.modalities != tuple(dict.fromkeys(self.modalities))
            or "text" not in self.modalities
        ):
            raise ValueError("expression modalities must be unique and include text")
        if bool(self.reaction_options) != ("reaction" in self.modalities):
            raise ValueError("reaction modality and options must be installed together")
        if bool(self.sticker_options) != ("sticker" in self.modalities):
            raise ValueError("sticker modality and options must be installed together")
        for options in (self.reaction_options, self.sticker_options):
            ids = tuple(item.option_id for item in options)
            if len(ids) != len(set(ids)):
                raise ValueError("expression option ids must be unique")
        if self.max_later_beats > self.max_beats:
            raise ValueError("later beat limit cannot exceed the overall beat limit")
        return self

    @property
    def action_kinds(self) -> frozenset[str]:
        kinds = {"reply", "followup", "proactive_message"}
        kinds.update(item for item in self.modalities if item != "text")
        return frozenset(kinds)

    def prompt_value(self) -> dict[str, object]:
        value: dict[str, object] = {
            "profile_id": self.profile_id,
            "modalities": self.modalities,
            "reaction_options": tuple(
                item.model_dump(mode="json") for item in self.reaction_options
            ),
            "sticker_options": tuple(item.model_dump(mode="json") for item in self.sticker_options),
            "max_beats": self.max_beats,
            "max_later_beats": self.max_later_beats,
            # This is a current transport fact, not a social prescription.
            # Deferred reactions/stickers/typing lack future provider target
            # bindings and therefore are not executable yet.
            "later_modalities": ("text",),
            "private_turn_state_mode": self.private_turn_state_mode,
        }
        if self.recorded_cadence_mode != "off":
            value.update(
                {
                    "recorded_cadence_mode": self.recorded_cadence_mode,
                    "cadence_profiles": (
                        "rapid",
                        "conversational",
                        "hesitant",
                        "escalating",
                    ),
                    "cadence_policy_version": self.cadence_policy_version,
                }
            )
        return value


TEXT_ONLY_EXPRESSION_CAPABILITIES = ExpressionDraftCapabilities(
    profile_id="expression:http-text-only.1",
    modalities=("text",),
)

QQ_NAPCAT_EXPRESSION_CAPABILITIES = ExpressionDraftCapabilities(
    profile_id="expression:qq-napcat.1",
    modalities=("text", "reaction", "sticker", "typing"),
    reaction_options=tuple(
        ExpressionOption(option_id=option_id, label=label)
        for option_id, label in QQ_REACTION_OPTIONS
    ),
    # These labels describe platform glyphs, not situations in which they must
    # be used.  The model is free to select none of them.
    sticker_options=tuple(
        ExpressionOption(option_id=option_id, label=label)
        for option_id, label in QQ_STICKER_OPTIONS
    ),
)


def qq_expression_capabilities(
    adapter: str, *, recorded_cadence_mode: Literal["off", "shadow", "on"] = "off"
) -> ExpressionDraftCapabilities:
    """Return only modalities proven by the configured QQ transport dialect."""

    base = (
        QQ_NAPCAT_EXPRESSION_CAPABILITIES
        if adapter.strip().lower() == "napcat"
        else TEXT_ONLY_EXPRESSION_CAPABILITIES
    )
    return base.model_copy(
        update={
            "recorded_cadence_mode": recorded_cadence_mode,
            "private_turn_state_mode": "required",
        }
    )


class ExpressionBeatDraftChoice(FrozenModel):
    modality: ExpressionModality
    text: str | None = Field(default=None, min_length=1, max_length=4_096)
    reaction_id: str | None = Field(default=None, min_length=1, max_length=128)
    sticker_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def carries_only_its_modality_value(self) -> "ExpressionBeatDraftChoice":
        supplied = {
            "text": self.text is not None,
            "reaction": self.reaction_id is not None,
            "sticker": self.sticker_id is not None,
            "typing": not any(
                value is not None for value in (self.text, self.reaction_id, self.sticker_id)
            ),
        }
        if not supplied[self.modality] or sum(supplied.values()) != 1:
            raise ValueError("expression beat carries fields from another modality")
        return self


class ResponseExpectationDraft(FrozenModel):
    """Optional, semantic invitation to reply; never inferred from punctuation."""

    hoped_response: str = Field(min_length=1, max_length=128)
    pressure_bp: int = Field(ge=0, le=10_000)
    importance_bp: int = Field(ge=0, le=10_000)
    wait_seconds: int = Field(ge=30, le=RESPONSE_EXPECTATION_WAIT_MAX_SECONDS)
    expires_after_seconds: int = Field(ge=60, le=172_800)

    @model_validator(mode="after")
    def expiry_follows_wait(self) -> "ResponseExpectationDraft":
        if self.expires_after_seconds <= self.wait_seconds:
            raise ValueError("response expectation expiry must follow its wait")
        return self


class WorldClaimDraft(FrozenModel):
    """Model-declared autobiographical claim, checked against Context authority."""

    claim_text: str = Field(min_length=1, max_length=512)
    scope: Literal[
        "current_world",
        "past_world",
        "counterpart_history",
        "shared_history",
        "stable_identity",
        "subjective_or_hypothetical",
    ]
    source_refs: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def source_shape_matches_scope(self) -> "WorldClaimDraft":
        grounded = self.scope in {
            "current_world",
            "past_world",
            "counterpart_history",
            "shared_history",
        }
        if grounded and not self.source_refs:
            raise ValueError("world claim scope requires matching source refs")
        if self.scope == "subjective_or_hypothetical" and self.source_refs:
            raise ValueError("subjective world claim cannot cite source refs")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("world claim source refs must be unique")
        return self


class ExpressionDraft(FrozenModel):
    """Small model draft; no IDs, targets, provider parameters or budgets."""

    private_turn_state: PrivateTurnState | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    timing_choice: TimingChoice = "now"
    # A role-owned conversational posture.  It is an advisory protocol
    # coordinate for the current turn, not a host rule: the model may choose
    # to yield, continue, interject, or supersede a pending expression.
    turn_posture: TurnPosture | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    cadence: CadenceProfile = "conversational"
    beats: tuple[ExpressionBeatDraftChoice, ...] = Field(default=(), max_length=16)
    delay_seconds: int | None = Field(default=None, ge=1, le=EXPRESSION_DELAY_MAX_SECONDS)
    expires_after_seconds: int | None = Field(default=None, ge=2, le=172_800)
    stance: str = Field(min_length=1, max_length=128)
    brief_rationale: str = Field(min_length=1, max_length=240)
    # Free-form audit of what made this expression salient.  It deliberately
    # has no host-owned motive taxonomy; proactive turns require it while an
    # ordinary reply need not invent one merely to share the wire contract.
    impulse_summary: str | None = Field(default=None, min_length=1, max_length=240)
    confidence: int = Field(default=5_000, ge=0, le=10_000)
    variation_profile: VariationProfile | None = None
    response_expectation: ResponseExpectationDraft | None = None
    response_expectation_assessment: ResponseExpectationAssessmentDraft | None = None
    world_claims: tuple[WorldClaimDraft, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def timing_and_visible_expression_are_orthogonal_but_complete(self) -> "ExpressionDraft":
        visible_content_seen = any(beat.modality != "typing" for beat in self.beats)
        if self.turn_posture == "interject" and self.timing_choice != "now":
            raise ValueError("interject posture requires an immediate expression")
        if self.turn_posture == "yield" and self.timing_choice == "now":
            raise ValueError("yield posture cannot authorize an immediate expression")
        if self.timing_choice == "silent":
            if (
                self.beats
                or self.delay_seconds is not None
                or self.expires_after_seconds is not None
                or self.response_expectation is not None
            ):
                raise ValueError("silent expression cannot smuggle visible beats or a due window")
            return self
        if not self.beats or not visible_content_seen:
            raise ValueError("visible expression requires at least one beat")
        # Typing is a model-owned beat in the ordered expression, not a host
        # prefix. It may appear before or between visible beats, but it cannot
        # leave the counterpart with a terminal composing indicator. Since a
        # final visible beat follows the last typing beat, this one boundary
        # also proves that every earlier typing beat eventually resolves.
        if self.beats and self.beats[-1].modality == "typing":
            raise ValueError("typing beat must be followed by visible content")
        if self.timing_choice == "now":
            if self.delay_seconds is not None or self.expires_after_seconds is not None:
                raise ValueError("immediate expression cannot select a due window")
            return self
        if self.delay_seconds is None or self.expires_after_seconds is None:
            raise ValueError("later expression requires a relative due window")
        if self.expires_after_seconds <= self.delay_seconds:
            raise ValueError("later expression expiry must follow its opening delay")
        return self


def validate_expression_draft_capabilities(
    *,
    draft: ExpressionDraft,
    capabilities: ExpressionDraftCapabilities,
    provider_message_id: str | None,
) -> None:
    """Apply deployment capability facts uniformly to every expression lane.

    The author owns whether and how many beats to make.  This only checks the
    concrete transport vocabulary available for the already-selected plan.
    Deferred non-text effects have no installed future provider-binding
    contract, so both inbound and proactive paths reject them identically.
    """

    if len(draft.beats) > capabilities.max_beats:
        raise ValueError("expression draft exceeds the deployment beat limit")
    if draft.timing_choice == "later" and len(draft.beats) > capabilities.max_later_beats:
        raise ValueError("later expression exceeds the installed deferred-effect limit")
    if draft.timing_choice == "later" and any(item.modality != "text" for item in draft.beats):
        raise ValueError("later expression supports only the installed text modality")
    available = set(capabilities.modalities)
    if any(item.modality not in available for item in draft.beats):
        raise ValueError("expression modality is not available in this deployment")
    reaction_ids = {item.option_id for item in capabilities.reaction_options}
    sticker_ids = {item.option_id for item in capabilities.sticker_options}
    if any(
        item.reaction_id not in reaction_ids for item in draft.beats if item.modality == "reaction"
    ):
        raise ValueError("reaction option is not available in this deployment")
    if any(
        item.sticker_id not in sticker_ids for item in draft.beats if item.modality == "sticker"
    ):
        raise ValueError("sticker option is not available in this deployment")
    if any(item.modality == "reaction" for item in draft.beats) and not provider_message_id:
        raise ValueError("reaction requires a provider message binding")


def _context_item_source_tokens(item: dict[str, object]) -> set[str]:
    """Return only the source tokens carried by one provider-visible item."""

    tokens = {
        token
        for field in ("item_ref", "source_ref", "source_hash", "value_hash")
        for token in (item.get(field),)
        if isinstance(token, str)
    }
    # The compact provider view retains semantic source tokens inside an
    # item's value (notably for an audited Recall injection) while
    # deliberately stripping the heavier authority bindings. Those nested
    # refs are genuinely visible to the model and therefore may be named in
    # its turn-local attention state.
    item_value = item.get("value")
    if isinstance(item_value, dict):
        value_refs = item_value.get("source_refs", ())
        if isinstance(value_refs, list):
            tokens.update(ref for ref in value_refs if isinstance(ref, str))
    bindings = item.get("source_bindings", ())
    if isinstance(bindings, list):
        tokens.update(
            binding["ref"]
            for binding in bindings
            if isinstance(binding, dict) and isinstance(binding.get("ref"), str)
        )
    return tokens


def _slice_source_tokens(context: dict[str, object], *slice_names: str) -> set[str]:
    slices = context.get("slices")
    if not isinstance(slices, dict):
        return set()
    tokens: set[str] = set()
    for name in slice_names:
        slice_value = slices.get(name)
        if not isinstance(slice_value, dict) or slice_value.get("availability") != "available":
            continue
        refs = slice_value.get("source_refs", ())
        if isinstance(refs, list):
            tokens.update(ref for ref in refs if isinstance(ref, str))
        items = slice_value.get("items", ())
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            tokens.update(_context_item_source_tokens(item))
    return tokens


def _context_entity_identity_tokens(context: dict[str, object]) -> set[str]:
    """Collect subject addresses that are visible context, never fact proof.

    Context item identities normally point at a source-bearing fact, event, or
    dialogue record.  A few typed projections are instead keyed by the entity
    they describe (notably ``current_situation`` by ``actor_ref``).  Letting
    that address enter a world-claim capability means the bare identity of the
    companion can be cited as proof of any current occurrence.  Preserve these
    refs for attention while excluding them from claim authority.
    """

    identity_fields = frozenset(
        {
            "actor_ref",
            "speaker_ref",
            "subject_refs",
            "participant_refs",
        }
    )
    tokens: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for field, nested in value.items():
                if field in identity_fields:
                    if isinstance(nested, str):
                        tokens.add(nested)
                    elif isinstance(nested, (list, tuple)):
                        tokens.update(item for item in nested if isinstance(item, str))
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)

    visit(context)
    return tokens


def _slice_claim_authority_tokens(
    context: dict[str, object],
    *slice_names: str,
) -> set[str]:
    """Return semantic/proof tokens without promoting entity addresses."""

    return _slice_source_tokens(context, *slice_names) - _context_entity_identity_tokens(context)


def _recent_dialogue_authority_tokens(
    context: dict[str, object],
    *,
    epistemic_scope: Literal[
        "counterpart_report_only",
        "companion_expression_record",
    ],
) -> set[str]:
    """Select dialogue proof by its typed speaker rather than by the whole lane.

    A recent-dialogue slice intentionally interleaves both sides of the
    conversation.  Its aggregate source set therefore cannot authorize a
    counterpart-history claim: a companion's old expression proves only that
    expression, not a user fact or the companion's old motive.  Unknown or
    contradictory compatibility shapes fail closed while remaining available
    through the separate attention-token path.
    """

    slices = context.get("slices")
    recent_dialogue = slices.get("recent_dialogue") if isinstance(slices, dict) else None
    if not isinstance(recent_dialogue, dict) or recent_dialogue.get("availability") != "available":
        return set()
    items = recent_dialogue.get("items")
    if not isinstance(items, list):
        return set()
    actor_ref = context.get("actor_ref")
    actor_ref = actor_ref if isinstance(actor_ref, str) else None
    tokens: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        declared_scope = value.get("epistemic_scope")
        speaker = value.get("speaker")
        speaker_ref = value.get("speaker_ref")
        if isinstance(speaker_ref, str) and actor_ref is not None:
            if speaker_ref == actor_ref and speaker == "counterpart":
                continue
            if speaker_ref != actor_ref and speaker == "companion":
                continue
        if declared_scope in {
            "counterpart_report_only",
            "companion_expression_record",
        }:
            resolved_scope = declared_scope
        elif speaker in {"counterpart", "user"}:
            resolved_scope = "counterpart_report_only"
        elif speaker == "companion":
            resolved_scope = "companion_expression_record"
        else:
            continue
        if resolved_scope == epistemic_scope:
            tokens.update(_context_item_source_tokens(item))
    return tokens - _context_entity_identity_tokens(context)


def current_counterpart_report_source_refs(
    *,
    context: dict[str, object],
    request: ModelInput,
) -> frozenset[str]:
    """Return refs that identify only the exact report which opened this turn.

    The current Observation already forms mandatory proposal evidence.  This
    helper gives both the author and the source reviewer one shared identity
    for natural conversational uptake without turning the reported
    proposition into an objective World fact.  A recent-dialogue alias joins
    the set only when its typed speaker, actor, text, and Observation identity
    all match the verified trigger exactly.
    """

    trigger = request.trigger_message
    if trigger is None:
        return frozenset()
    refs = {
        request.trigger_ref,
        trigger.event_ref,
        trigger.observation_ref,
    }
    slices = context.get("slices")
    recent_dialogue = slices.get("recent_dialogue") if isinstance(slices, dict) else None
    if not isinstance(recent_dialogue, dict) or recent_dialogue.get("availability") != "available":
        return frozenset(refs)
    items = recent_dialogue.get("items")
    if not isinstance(items, list):
        return frozenset(refs)
    expected_dialogue_id = f"dialogue:observation:{trigger.observation_ref}"
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if not isinstance(value, dict):
            continue
        if (
            value.get("dialogue_id") != expected_dialogue_id
            or value.get("speaker") not in {"counterpart", "user"}
            or value.get("text") != trigger.text
        ):
            continue
        speaker_ref = value.get("speaker_ref")
        if speaker_ref is not None and speaker_ref != trigger.actor:
            continue
        item_refs = _context_item_source_tokens(item)
        if expected_dialogue_id not in item_refs:
            continue
        refs.update(item_refs)
        break
    return frozenset(refs)


def _world_life_occurrence_source_tokens(
    context: dict[str, object],
    *,
    include_active: bool,
) -> set[str]:
    """Return occurrence proof without promoting active life into past fact."""

    slices = context.get("slices")
    world_life = slices.get("world_life") if isinstance(slices, dict) else None
    if not isinstance(world_life, dict) or world_life.get("availability") != "available":
        return set()
    items = world_life.get("items")
    if not isinstance(items, list):
        return set()
    return {
        token
        for item in items
        if isinstance(item, dict)
        and not (
            isinstance(item.get("value"), dict)
            and item["value"].get("context_kind") == "biographical_context"
        )
        and (
            include_active
            or not (
                isinstance(item.get("value"), dict)
                and item["value"].get("context_kind") == "active_world_occurrence"
            )
        )
        for token in _context_item_source_tokens(item)
    } - _context_entity_identity_tokens(context)


def _active_world_occurrence_source_tokens(context: dict[str, object]) -> set[str]:
    """Expose only pinned active-occurrence claim capability.

    An empty result means that this provider-visible Context carries no
    authority for such a claim. It is not evidence that no occurrence exists
    outside the bounded Context.
    """

    slices = context.get("slices")
    world_life = slices.get("world_life") if isinstance(slices, dict) else None
    if not isinstance(world_life, dict) or world_life.get("availability") != "available":
        return set()
    items = world_life.get("items")
    if not isinstance(items, list):
        return set()
    return {
        token
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("value"), dict)
        and item["value"].get("context_kind") == "active_world_occurrence"
        for token in _context_item_source_tokens(item)
    } - _context_entity_identity_tokens(context)


def _biographical_coordinate_source_tokens(
    context: dict[str, object],
    *,
    scope: Literal["current_world"],
) -> set[str]:
    """Return only field/value-bound biography capabilities for one claim lane."""

    return {
        item.source_ref
        for item in biographical_coordinate_authorities(context)
        if item.scope == scope
    }


def world_claim_source_tokens(context: dict[str, object], *slice_names: str) -> set[str]:
    """Return the exact source tokens exposed by selected pinned Context lanes."""

    tokens = _slice_claim_authority_tokens(
        context,
        *(name for name in slice_names if name != "world_life"),
    )
    if "world_life" in slice_names:
        tokens.update(
            _world_life_occurrence_source_tokens(
                context,
                include_active=True,
            )
        )
    return tokens


def _all_context_source_tokens(context: dict[str, object]) -> set[str]:
    slices = context.get("slices")
    if not isinstance(slices, dict):
        return set()
    return _slice_source_tokens(
        context,
        *(name for name in slices if isinstance(name, str)),
    )


def _all_context_attention_tokens(context: dict[str, object]) -> set[str]:
    """Collect provider-visible attention aliases without granting claim authority."""

    tokens = _all_context_source_tokens(context)
    slices = context.get("slices")
    if not isinstance(slices, dict):
        return tokens
    for slice_value in slices.values():
        if not isinstance(slice_value, dict) or slice_value.get("availability") != "available":
            continue
        items = slice_value.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            refs = item.get("attention_source_refs") if isinstance(item, dict) else None
            if isinstance(refs, list):
                tokens.update(ref for ref in refs if isinstance(ref, str))
    return tokens


class SourceRefAliasTable(NamedTuple):
    """One immutable provider-call mapping from short aliases to pinned refs."""

    entries: tuple[tuple[str, str], ...]
    canonical_refs: frozenset[str]

    def prompt_value(self) -> dict[str, str]:
        return dict(self.entries)

    def alias_for(self, source_ref: str) -> str | None:
        return next(
            (alias for alias, canonical in self.entries if canonical == source_ref),
            None,
        )

    def expand(self, source_ref: str) -> str:
        # A canonical ref always wins, including the unlikely case where a
        # trusted source itself uses the reserved short-token shape.
        if source_ref in self.canonical_refs:
            return source_ref
        expanded = dict(self.entries).get(source_ref)
        if expanded is not None:
            return expanded
        if (
            len(source_ref) > 1
            and source_ref[0] in {"S", "T"}
            and source_ref[1:].isascii()
            and source_ref[1:].isdecimal()
        ):
            raise ValueError(f"unknown source-ref alias: {source_ref}")
        return source_ref


def _benefits_from_source_ref_alias(source_ref: str) -> bool:
    """Keep ordinary short fixture refs readable; abbreviate costly wire refs."""

    return len(source_ref) > 48 or (len(source_ref) > 28 and source_ref.count(":") >= 3)


def _is_pinned_time_source_ref(source_ref: str) -> bool:
    prefix = "pinned-time:sha256:"
    if not source_ref.startswith(prefix):
        return False
    digest = source_ref[len(prefix) :]
    return len(digest) == 64 and all(character in "0123456789abcdef" for character in digest)


def _pinned_time_source_refs(context: dict[str, object]) -> frozenset[str]:
    """Return valid temporal refs from the actual provider-visible time lane."""

    slices = context.get("slices")
    pinned_time = slices.get("pinned_time") if isinstance(slices, dict) else None
    if not isinstance(pinned_time, dict) or pinned_time.get("availability") != "available":
        return frozenset()
    items = pinned_time.get("items")
    if not isinstance(items, list):
        return frozenset()
    return frozenset(
        source_ref
        for item in items
        if isinstance(item, dict)
        for source_ref in (item.get("source_ref"),)
        if isinstance(source_ref, str) and _is_pinned_time_source_ref(source_ref)
    )


def build_source_ref_alias_table(
    *,
    request: ModelInput,
    stable_identity_source_refs: frozenset[str] = frozenset(),
    model_visible_context_json: str | None = None,
    existing: SourceRefAliasTable | None = None,
) -> SourceRefAliasTable:
    """Freeze deterministic aliases from exactly provider-visible authority.

    The verified current trigger is also provider-visible and may authorize a
    source-bound report of what the counterpart said. ``existing`` extends a
    mapping after bounded Recall without renumbering aliases already shown in
    the first provider call.
    """

    raw_context = (
        request.model_content_json
        if model_visible_context_json is None
        else model_visible_context_json
    )
    try:
        context = json.loads(raw_context)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("source-ref aliases require model-visible Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("source-ref aliases require a model-visible Context object")
    pinned_time_refs = _pinned_time_source_refs(context)
    # A Recall follow-up is sent in the same provider conversation, so refs
    # exposed by the first request remain visible even if compacting the
    # augmented Context evicts their duplicate slice item.
    canonical_refs = set(existing.canonical_refs if existing is not None else ())
    canonical_refs.update(stable_identity_source_refs)
    canonical_refs.update(_all_context_attention_tokens(context))
    canonical_refs.update(item.source_ref for item in biographical_coordinate_authorities(context))
    canonical_refs.update(item.ref_id for item in request.trigger_evidence)
    canonical_refs.add(request.trigger_ref)
    trigger = request.trigger_message
    if trigger is not None:
        canonical_refs.update((trigger.event_ref, trigger.observation_ref))
    canonical_refs = {ref for ref in canonical_refs if isinstance(ref, str) and ref}
    entries = list(existing.entries if existing is not None else ())
    if existing is not None:
        if any(canonical not in existing.canonical_refs for _, canonical in entries):
            raise ValueError("source-ref alias extension received an invalid frozen mapping")
    assigned = {canonical for _, canonical in entries}
    used_aliases = {alias for alias, _ in entries}
    # Pinned time is a derived convenience source that is present before a
    # character-requested Recall can augment the Context.  Giving it an S
    # ordinal would permanently consume S1 in the frozen first-call table and
    # silently reinterpret existing Recall follow-ups that already copied S1.
    # A dedicated temporal namespace keeps all pre-existing S aliases stable
    # while remaining short and copyable.
    temporal_ordinal = 1
    for source_ref in sorted(pinned_time_refs):
        if source_ref in assigned or source_ref not in canonical_refs:
            continue
        while (alias := f"T{temporal_ordinal}") in used_aliases or alias in canonical_refs:
            temporal_ordinal += 1
        entries.append((alias, source_ref))
        assigned.add(source_ref)
        used_aliases.add(alias)
        temporal_ordinal += 1
    ordinal = 1
    for source_ref in sorted(canonical_refs):
        if (
            source_ref in assigned
            or source_ref in pinned_time_refs
            or not _benefits_from_source_ref_alias(source_ref)
        ):
            continue
        while (alias := f"S{ordinal}") in used_aliases or alias in canonical_refs:
            ordinal += 1
        entries.append((alias, source_ref))
        assigned.add(source_ref)
        used_aliases.add(alias)
        ordinal += 1
    return SourceRefAliasTable(
        entries=tuple(entries),
        canonical_refs=frozenset(canonical_refs),
    )


def expand_expression_source_ref_aliases(
    value: dict[str, object],
    *,
    aliases: SourceRefAliasTable,
) -> dict[str, object]:
    """Expand only the two model-owned source-ref fields, never prose or IDs."""

    expanded = dict(value)
    raw_state = expanded.get("private_turn_state")
    if isinstance(raw_state, dict):
        refs = raw_state.get("attended_source_refs")
        if isinstance(refs, (list, tuple)):
            expanded["private_turn_state"] = {
                **raw_state,
                "attended_source_refs": [
                    aliases.expand(ref) if isinstance(ref, str) else ref for ref in refs
                ],
            }
    claims = expanded.get("world_claims")
    if isinstance(claims, (list, tuple)):
        expanded_claims: list[object] = []
        for claim in claims:
            if not isinstance(claim, dict):
                expanded_claims.append(claim)
                continue
            refs = claim.get("source_refs")
            if not isinstance(refs, (list, tuple)):
                expanded_claims.append(claim)
                continue
            expanded_claims.append(
                {
                    **claim,
                    "source_refs": [
                        aliases.expand(ref) if isinstance(ref, str) else ref for ref in refs
                    ],
                }
            )
        expanded["world_claims"] = expanded_claims
    return expanded


class PrivateTurnStateValidationError(ValueError):
    """Sanitized failure of the model-owned pre-expression causal boundary."""

    def __init__(self, *, code: str, field_path: str) -> None:
        self.code = code
        self.field_path = field_path
        super().__init__(
            f"private_turn_state validation failed code={self.code} path={self.field_path}"
        )


_PRIVATE_STATE_ERROR_CODES = {
    "extra_forbidden": "private_turn_state.unexpected_field",
    "literal_error": "private_turn_state.invalid_contract",
    "missing": "private_turn_state.missing_field",
    "string_too_long": "private_turn_state.string_too_long",
    "string_too_short": "private_turn_state.string_too_short",
    "string_type": "private_turn_state.invalid_type",
    "too_long": "private_turn_state.too_many_items",
    "tuple_type": "private_turn_state.invalid_type",
    "value_error": "private_turn_state.invalid_value",
}
_PRIVATE_STATE_FIELDS = {
    "contract",
    "inner_state_summary",
    "attended_source_refs",
}


def _private_state_validation_error(exc: ValidationError) -> PrivateTurnStateValidationError:
    errors = exc.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    first = errors[0] if errors else {}
    error_type = first.get("type")
    code = _PRIVATE_STATE_ERROR_CODES.get(
        error_type if isinstance(error_type, str) else "",
        "private_turn_state.invalid_field",
    )
    location = first.get("loc")
    field = (
        next(
            (item for item in location if isinstance(item, str) and item in _PRIVATE_STATE_FIELDS),
            None,
        )
        if isinstance(location, tuple)
        else None
    )
    path = "private_turn_state" + (f".{field}" if field is not None else "")
    return PrivateTurnStateValidationError(code=code, field_path=path)


def validate_expression_private_turn_state(
    *,
    value: dict[str, object],
    request: ModelInput,
    capabilities: ExpressionDraftCapabilities,
    stable_identity_source_refs: frozenset[str] = frozenset(),
    model_visible_context_json: str | None = None,
    source_ref_aliases: SourceRefAliasTable | None = None,
) -> PrivateTurnState | None:
    """Validate the model-owned turn state against the exact pinned turn.

    This deliberately performs no semantic interpretation.  It exists as a
    small shared boundary so a missing, malformed, or unpinned state cannot
    reach source review or a recall follow-up. JSON object member order is not
    evidence of when the model formed the state and is deliberately ignored.
    """

    required = capabilities.private_turn_state_mode == "required"
    aliases = source_ref_aliases or build_source_ref_alias_table(
        request=request,
        stable_identity_source_refs=stable_identity_source_refs,
        model_visible_context_json=model_visible_context_json,
    )
    value = expand_expression_source_ref_aliases(value, aliases=aliases)
    raw_state = value.get("private_turn_state")
    if raw_state is None:
        if required:
            raise PrivateTurnStateValidationError(
                code="private_turn_state.missing",
                field_path="private_turn_state",
            )
        return None
    try:
        state = (
            raw_state
            if isinstance(raw_state, PrivateTurnState)
            else PrivateTurnState.model_validate_json(_canonical_json(raw_state), strict=True)
        )
    except ValidationError as exc:
        raise _private_state_validation_error(exc) from None
    except (TypeError, ValueError):
        raise PrivateTurnStateValidationError(
            code="private_turn_state.invalid_shape",
            field_path="private_turn_state",
        ) from None
    trigger = request.trigger_message
    try:
        context = json.loads(
            request.model_content_json
            if model_visible_context_json is None
            else model_visible_context_json
        )
    except (TypeError, json.JSONDecodeError):
        raise PrivateTurnStateValidationError(
            code="private_turn_state.invalid_context",
            field_path="private_turn_state.attended_source_refs",
        ) from None
    allowed_attention_refs = {
        request.trigger_ref,
        *(item.ref_id for item in request.trigger_evidence),
        *stable_identity_source_refs,
    }
    # Proactive turns intentionally have no current inbound message.  Their
    # private state is still grounded in the exact pinned world context and
    # opportunity evidence; requiring a synthetic user observation here would
    # make that legitimate deliberation impossible.
    if trigger is not None:
        allowed_attention_refs.update((trigger.event_ref, trigger.observation_ref))
    if isinstance(context, dict):
        allowed_attention_refs.update(_all_context_attention_tokens(context))
        # The provider-visible biography manifest exposes exact, content-
        # addressed coordinates in addition to their broad parent item.  The
        # alias table derives the same refs from this exact pinned Context, so
        # they are valid attention provenance without granting any prefix-
        # based or free-form authority.
        allowed_attention_refs.update(
            item.source_ref for item in biographical_coordinate_authorities(context)
        )
    try:
        validate_private_turn_state_sources(
            state,
            allowed_source_refs=allowed_attention_refs,
        )
    except (TypeError, ValueError):
        raise PrivateTurnStateValidationError(
            code="private_turn_state.unpinned_source",
            field_path="private_turn_state.attended_source_refs",
        ) from None
    return state


def _has_response_expectation_advisory(context: dict[str, object]) -> bool:
    slices = context.get("slices")
    if not isinstance(slices, dict):
        return False
    advisories = slices.get("advisories")
    if not isinstance(advisories, dict):
        return False
    items = advisories.get("items")
    if not isinstance(items, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance(item.get("value"), dict)
        and item["value"].get("kind") == "response_expectation"
        for item in items
    )


def request_requires_response_expectation_assessment(request: ModelInput) -> bool:
    """Whether this exact pinned cognition must return the four-state judgement."""

    try:
        context = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(context, dict) and _has_response_expectation_advisory(context)


def _world_claim_source_refs_by_scope(
    *,
    context: dict[str, object],
    stable_identity_source_refs: frozenset[str],
    counterpart_message_source_refs: frozenset[str] = frozenset(),
) -> dict[str, set[str]]:
    private_stable_identity = {
        ref
        for ref in stable_identity_source_refs
        if not ref.startswith(
            (
                "identity-frame:shared-history:",
                "identity-frame:counterpart-history:",
            )
        )
    }
    private_shared_history = {
        ref
        for ref in stable_identity_source_refs
        if ref.startswith("identity-frame:shared-history:")
    }
    return {
        "current_world": _slice_claim_authority_tokens(
            context,
            "current_situation",
        )
        | _world_life_occurrence_source_tokens(
            context,
            include_active=True,
        )
        | _biographical_coordinate_source_tokens(context, scope="current_world"),
        "past_world": _world_life_occurrence_source_tokens(
            context,
            include_active=False,
        )
        | _slice_claim_authority_tokens(context, "recent_experiences"),
        "counterpart_history": _slice_claim_authority_tokens(
            context,
            "relevant_facts",
        )
        | _recent_dialogue_authority_tokens(
            context,
            epistemic_scope="counterpart_report_only",
        )
        | set(counterpart_message_source_refs),
        "shared_history": _slice_claim_authority_tokens(
            context,
            "recent_dialogue",
            "recent_experiences",
        )
        | private_shared_history,
        "stable_identity": _slice_claim_authority_tokens(context, "character_core")
        | private_stable_identity,
        "subjective_or_hypothetical": set(),
    }


def world_claim_source_refs_by_scope(
    *,
    context: dict[str, object],
    stable_identity_source_refs: frozenset[str] = frozenset(),
    counterpart_message_source_refs: frozenset[str] = frozenset(),
) -> dict[str, frozenset[str]]:
    """Compile the shared deterministic WorldClaim capability matrix.

    Interactive and proactive expression must not independently reinterpret a
    Context slice.  In particular, a broad biographical item is attention
    context while only its derived, exact pinned coordinates are claim
    authority.
    """

    return {
        scope: frozenset(refs)
        for scope, refs in _world_claim_source_refs_by_scope(
            context=context,
            stable_identity_source_refs=stable_identity_source_refs,
            counterpart_message_source_refs=counterpart_message_source_refs,
        ).items()
    }


def world_claim_source_ref_aliases_by_scope(
    *,
    request: ModelInput,
    stable_identity_source_refs: frozenset[str] = frozenset(),
    source_ref_aliases: SourceRefAliasTable | None = None,
    model_visible_context_json: str | None = None,
) -> dict[str, tuple[str, ...]]:
    """Compile the exact provider-visible alias vocabulary for each claim scope.

    This is the wire counterpart of :func:`world_claim_source_refs_by_scope`.
    It does not decide whether the role should make a claim; it prevents a
    strict corrective schema from advertising a ref under a semantic lane
    that the final materializer must reject.
    """

    context_json = (
        request.model_content_json
        if model_visible_context_json is None
        else model_visible_context_json
    )
    try:
        context = json.loads(context_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("world claim source aliases require Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("world claim source aliases require a Context object")
    aliases = source_ref_aliases or build_source_ref_alias_table(
        request=request,
        stable_identity_source_refs=stable_identity_source_refs,
        model_visible_context_json=context_json,
    )
    source_refs = world_claim_source_refs_by_scope(
        context=context,
        stable_identity_source_refs=stable_identity_source_refs,
        counterpart_message_source_refs=current_counterpart_report_source_refs(
            context=context,
            request=request,
        ),
    )
    return {
        scope: tuple(
            sorted(
                aliases.alias_for(source_ref) or source_ref
                for source_ref in refs
                if source_ref in aliases.canonical_refs
            )
        )
        for scope, refs in source_refs.items()
        if scope != "subjective_or_hypothetical"
    }


def single_report_epistemic_scope_boundary() -> dict[str, object]:
    """Describe what one occurrence report can prove, without choosing a response."""

    return {
        "boundary_kind": "fact_scope_only",
        "behavior_advice": False,
        "evidence_cardinality": "one_report_of_one_occurrence",
        "cannot_authorize": [
            "class_wide_assertion",
            "habitual_assertion",
            "generic_assertion",
            "typical_assertion",
            "frequency_assertion",
        ],
    }


def world_source_scope_boundary() -> dict[str, object]:
    """Describe which propositions belong to this ledger's source boundary."""

    return {
        "classification_owner": "inventory_and_source_authority_models",
        "host_keyword_or_surface_classifier": False,
        "source_closure_target": "specific_world_bound_actual_or_settled_proposition",
        "world_unbound_generalization": {
            "inventory_role": "world_unbound_generalization",
            "requires_pinned_world_source": False,
            "cannot_authorize_world_mutation": True,
            "scope": (
                "ordinary_background_or_phenomenological_generalization_whose_truth_"
                "does_not_depend_on_a_specific_world_entity_place_time_occurrence_or_history"
            ),
            "conversational_application": (
                "mentioning_or_applying_the_general_relation_to_an_attended_reported_"
                "scene_does_not_itself_assert_a_new_specific_scene_fact"
            ),
            "binding_test": (
                "classify_the_complete_semantic_commitment_not_the_presence_of_a_"
                "specific_scene_in_the_surrounding_conversation"
            ),
        },
        "unsettled_conjecture": {
            "inventory_role": "nonassertive_content",
            "requires_pinned_world_source": False,
            "scope": (
                "complete_utterance_keeps_a_specific_current_or_future_world_"
                "proposition_genuinely_unsettled"
            ),
            "epistemic_commitment": (
                "speaker_may_lean_toward_p_while_both_p_and_not_p_remain_"
                "compatible_with_the_complete_utterance"
            ),
        },
        "subjective_evaluation": {
            "requires_pinned_world_source": False,
            "scope": (
                "speaker_owned_evaluative_predicate_even_when_it_mentions_an_"
                "attended_specific_scene"
            ),
            "report_relative_composition": (
                "an_evaluation_may_take_descriptive_operands_from_exact_current_or_"
                "typed_dialogue_reports_while_preserving_their_report_only_status"
            ),
            "experiential_projection": (
                "a_subjective_prediction_about_how_a_condition_may_feel_sound_"
                "look_or_seem_does_not_settle_a_physical_result"
            ),
            "separate_descriptive_premise_still_requires_source": True,
        },
        "still_requires_source_closure": [
            "specific_current_or_past_user_companion_or_shared_world_fact",
            "specific_location_activity_bodily_state_person_occurrence_or_history",
            "entity_or_identifiable_group_bound_habitual_or_frequency_claim",
            "specific_world_state_presented_as_actual_or_settled_despite_a_hedge",
            "descriptive_premise_inside_an_evaluation_not_entailed_by_the_"
            "current_or_typed_dialogue_report",
        ],
    }


def expression_hard_boundary_manifest(
    *,
    request: ModelInput,
    stable_identity_source_refs: frozenset[str] = frozenset(),
    source_ref_aliases: SourceRefAliasTable | None = None,
) -> dict[str, object]:
    """Expose executable constraints without choosing the character's response.

    Cross-field validators and semantic source lanes are not fully expressible
    in the provider's generic JSON mode.  This compact manifest makes the
    already-enforced hard boundary machine-readable, so the role model can
    choose any motive or expression without guessing which exact source token
    belongs to which factual scope.
    """

    try:
        context = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("expression hard boundaries require Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("expression hard boundaries require a Context object")
    current_report_refs = current_counterpart_report_source_refs(
        context=context,
        request=request,
    )
    source_refs = _world_claim_source_refs_by_scope(
        context=context,
        stable_identity_source_refs=stable_identity_source_refs,
        counterpart_message_source_refs=current_report_refs,
    )
    aliases = source_ref_aliases or build_source_ref_alias_table(
        request=request,
        stable_identity_source_refs=stable_identity_source_refs,
    )
    claim_authority_refs = {source_ref for refs in source_refs.values() for source_ref in refs}
    attention_only_refs = sorted(
        aliases.alias_for(source_ref) or source_ref
        for source_ref in aliases.canonical_refs - claim_authority_refs
        if source_ref in _all_context_attention_tokens(context)
    )
    attention_only_aliases = {
        alias: canonical
        for alias, canonical in aliases.entries
        if canonical not in claim_authority_refs
        and canonical in _all_context_attention_tokens(context)
    }
    claim_authority_aliases = {
        alias: canonical
        for alias, canonical in aliases.entries
        if canonical in claim_authority_refs
    }

    def present_claim_refs(refs: set[str]) -> list[str]:
        return sorted(aliases.alias_for(ref) or ref for ref in refs)

    coordinate_authorities = biographical_coordinate_authorities(context)
    return {
        "contract": "expression-hard-boundaries.8",
        "private_turn_state": {
            "attended_source_refs": {
                "maximum_items": 8,
                "unique": True,
                "authority": "attention_provenance_only_not_world_fact_authority",
                "attention_only_not_fact_authority": attention_only_refs,
                "additional_attention_only_source_ref_aliases": (attention_only_aliases),
            },
            "epistemic_authority": {
                "character_private_mental_state": {
                    "source_required": False,
                    "covers": (
                        "present_and_immediate_retrospective_first_person_mental_continuity"
                    ),
                },
                "external_material_in_private_state": {
                    "world_authority": False,
                    "examples_are_non_exhaustive": [
                        "place",
                        "action_or_activity",
                        "other_person_or_their_mental_state",
                        "bodily_or_physical_status",
                        "world_occurrence_or_settled_history",
                    ],
                    "effect": (
                        "turn_local_audit_only; visible_or_durable_restatement_requires_"
                        "matching_output_seam_authority"
                    ),
                },
            },
        },
        "response_expectation": {
            "wait_seconds": {"minimum": 30, "maximum": 86_400},
            "expires_after_seconds": {"minimum": 60, "maximum": 172_800},
            "relation": "expires_after_seconds > wait_seconds",
        },
        "later": {
            "delay_seconds": {"minimum": 1, "maximum": 86_400},
            "expires_after_seconds": {"minimum": 2, "maximum": 172_800},
            "relation": "expires_after_seconds > delay_seconds",
        },
        **(
            {"single_report_epistemic_scope": single_report_epistemic_scope_boundary()}
            if request.trigger_message is not None
            else {}
        ),
        "world_source_scope": world_source_scope_boundary(),
        "world_claim_source_refs": {
            scope: sorted(aliases.alias_for(ref) or ref for ref in refs)
            for scope, refs in source_refs.items()
            if scope != "subjective_or_hypothetical"
        },
        "companion_life_authority_availability": {
            "authority": "pinned_claim_capability_only",
            "behavior_advice": False,
            "empty_semantics": "no_pinned_authority_available_not_event_did_not_happen",
            "current_situation_source_refs": present_claim_refs(
                _slice_claim_authority_tokens(context, "current_situation")
            ),
            "active_occurrence_source_refs": present_claim_refs(
                _active_world_occurrence_source_tokens(context)
            ),
            "committed_experience_source_refs": present_claim_refs(
                _slice_claim_authority_tokens(context, "recent_experiences")
            ),
        },
        "biographical_coordinate_authority": [
            {
                "source_ref": aliases.alias_for(item.source_ref) or item.source_ref,
                "scope": item.scope,
                "field_path": item.field_path,
                **({"logical_at": item.logical_at} if item.logical_at is not None else {}),
                "value": item.value,
            }
            for item in coordinate_authorities
        ],
        "biographical_parent_attention_only": sorted(
            aliases.alias_for(ref) or ref for ref in biographical_parent_attention_refs(context)
        ),
        **(
            {
                "current_counterpart_report_authority": {
                    "discourse_scope": "current_counterpart_report",
                    "epistemic_status": ("report_only_not_objective_truth_or_companion_experience"),
                    "reported_text": request.trigger_message.text,
                    "reporter_ref": request.trigger_message.actor,
                    "source_refs": sorted(
                        aliases.alias_for(ref) or ref for ref in current_report_refs
                    ),
                    "world_claim_required_for_direct_uptake": False,
                    "natural_uptake_without_attribution_phrase": True,
                    "does_not_authorize": [
                        "added_or_changed_subject_time_occurrence_or_status",
                        "added_detail_or_motive",
                        "objective_world_fact",
                        "companion_experience",
                        "durable_world_mutation",
                    ],
                }
            }
            if request.trigger_message is not None
            else {}
        ),
        "legacy_replay_only_world_claim_scopes": ["subjective_or_hypothetical"],
        "source_ref_aliases": claim_authority_aliases,
    }


def invalid_world_claim_source_indexes(
    *,
    draft: ExpressionDraft,
    request: ModelInput,
    stable_identity_source_refs: frozenset[str] = frozenset(),
) -> tuple[int, ...]:
    """Return claims whose cited refs are outside their exact semantic lane.

    This is a mechanical capability check, not a semantic judgment about the
    prose.  It is shared with the independent source reviewer so a reviewer
    cannot accidentally bless an attention-only token as World authority.
    """

    try:
        context = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("world claim validation requires Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("world claim validation requires a Context object")
    allowed = _world_claim_source_refs_by_scope(
        context=context,
        stable_identity_source_refs=stable_identity_source_refs,
        counterpart_message_source_refs=current_counterpart_report_source_refs(
            context=context,
            request=request,
        ),
    )
    return tuple(
        index
        for index, claim in enumerate(draft.world_claims)
        if claim.scope == "subjective_or_hypothetical"
        or not set(claim.source_refs).issubset(allowed[claim.scope])
    )


def _validate_world_claims(
    *,
    draft: ExpressionDraft,
    request: ModelInput,
    stable_identity_source_refs: frozenset[str] = frozenset(),
) -> None:
    private_stable_identity = {
        ref
        for ref in stable_identity_source_refs
        if not ref.startswith(
            (
                "identity-frame:shared-history:",
                "identity-frame:counterpart-history:",
            )
        )
    }
    invalid_source_indexes = frozenset(
        invalid_world_claim_source_indexes(
            draft=draft,
            request=request,
            stable_identity_source_refs=stable_identity_source_refs,
        )
    )
    for index, claim in enumerate(draft.world_claims):
        if claim.scope == "subjective_or_hypothetical":
            raise ValueError(
                "legacy subjective world-claim scope cannot be authored; "
                "subjective feelings and hypotheticals use no world_claim item"
            )
        if claim.scope == "stable_identity" and private_stable_identity and not claim.source_refs:
            raise ValueError(
                "stable identity world claim requires the exact private identity source ref"
            )
        if index in invalid_source_indexes:
            raise ValueError(
                "world claim cites authority outside its semantic source lane: "
                f"scope={claim.scope} claim={claim.claim_text[:80]!r}"
            )


def _world_claim_evidence(
    *,
    draft: ExpressionDraft,
    request: ModelInput,
    non_ledger_source_refs: frozenset[str] = frozenset(),
) -> tuple[ProposalEvidenceRef, ...]:
    cited = {
        ref
        for claim in draft.world_claims
        if claim.scope != "subjective_or_hypothetical"
        for ref in claim.source_refs
        if ref not in non_ledger_source_refs
    }
    if not cited:
        return ()
    context = json.loads(request.model_content_json)
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        raise ValueError("world claim evidence requires Context slices")
    coordinate_parents = {
        item.source_ref: item.parent_item_ref
        for item in biographical_coordinate_authorities(context)
    }
    derived_coordinate_refs = cited & set(coordinate_parents)
    expanded_cited = set(cited - derived_coordinate_refs)
    world_life = slices.get("world_life")
    world_life_items = (
        world_life.get("items")
        if isinstance(world_life, dict) and world_life.get("availability") == "available"
        else None
    )
    if isinstance(world_life_items, list):
        parent_refs = {coordinate_parents[ref] for ref in derived_coordinate_refs}
        for item in world_life_items:
            if not isinstance(item, dict):
                continue
            parent_ref = item.get("item_ref") or item.get("source_ref")
            if parent_ref not in parent_refs:
                continue
            bindings = item.get("source_bindings")
            if not isinstance(bindings, list):
                continue
            expanded_cited.update(
                ref
                for binding in bindings
                if isinstance(binding, dict)
                for ref in (binding.get("ref"),)
                if isinstance(ref, str) and ref
            )
    candidates: dict[str, set[tuple[str, str, int, str]]] = {}
    for lane in slices.values():
        if not isinstance(lane, dict):
            continue
        items = lane.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            bindings = item.get("source_bindings")
            if not isinstance(bindings, list):
                continue
            for binding in bindings:
                if not isinstance(binding, dict):
                    continue
                ref = binding.get("ref")
                source_kind = binding.get("source_kind")
                authority_type = binding.get("authority_type")
                revision = binding.get("source_world_revision")
                immutable_hash = binding.get("immutable_hash")
                if (
                    isinstance(ref, str)
                    and ref in expanded_cited
                    and isinstance(source_kind, str)
                    and isinstance(authority_type, str)
                    and isinstance(revision, int)
                    and not isinstance(revision, bool)
                    and isinstance(immutable_hash, str)
                    and len(immutable_hash) == 64
                ):
                    candidates.setdefault(ref, set()).add(
                        (source_kind, authority_type, revision, immutable_hash)
                    )
    evidence: list[ProposalEvidenceRef] = []
    for ref in sorted(expanded_cited):
        matches = candidates.get(ref, set())
        if not matches:
            # Older Context profiles expose a projection item_ref/value hash
            # without its event binding. The existing semantic-lane check
            # remains authoritative for those replay-compatible tokens. New
            # recall documents always carry bindings and therefore enter the
            # proposal evidence closure below.
            continue
        if len(matches) != 1:
            raise ValueError("world claim source has ambiguous authority binding")
        source_kind, authority_type, revision, immutable_hash = next(iter(matches))
        if source_kind == "execution_receipt":
            kind = "settled_external_result"
        elif source_kind == "committed_event":
            kind = _EVENT_EVIDENCE_KIND.get(authority_type, "committed_world_event")
        else:
            raise ValueError("world claim source is not immutable event authority")
        evidence.append(
            ProposalEvidenceRef(
                ref_id=ref,
                evidence_kind=kind,
                source_world_revision=revision,
                immutable_hash="sha256:" + immutable_hash,
            )
        )
    return tuple(evidence)


def is_world_claim_violation(violation: str) -> bool:
    """Recognize a claim-bookkeeping failure attached to an otherwise sound draft."""

    return "world claim" in violation or "world_claims" in violation


def _normalize_world_claim_aliases(value: dict[str, object]) -> dict[str, object]:
    """Repair one unambiguous field-name echo without loosening validation.

    Models regularly echo the prompt phrase "exact source_refs" as a literal
    ``exact_source_refs`` key.  The meaning is identical and the strict
    schema would otherwise collapse a fully valid reply into the recovery
    lane, so only this exact alias is renamed — any other extra key still
    fails closed.
    """

    claims = value.get("world_claims")
    if not isinstance(claims, list):
        return value
    repaired = []
    changed = False
    for claim in claims:
        if isinstance(claim, dict) and "exact_source_refs" in claim and "source_refs" not in claim:
            claim = {
                ("source_refs" if key == "exact_source_refs" else key): item
                for key, item in claim.items()
            }
            changed = True
        repaired.append(claim)
    if not changed:
        return value
    return {**value, "world_claims": repaired}


def _normalize_cadence_alias(value: dict[str, object]) -> dict[str, object]:
    """Repair one unambiguous field-name echo without loosening validation.

    The prompt asks the model to "choose one bounded cadence intent", and
    models regularly echo that phrase as a literal ``cadence_intent`` key.
    The meaning is identical to the schema's ``cadence`` field, so only this
    exact alias is renamed — any other extra key still fails closed.
    """

    if "cadence_intent" in value and "cadence" not in value:
        return {
            ("cadence" if key == "cadence_intent" else key): item for key, item in value.items()
        }
    return value


def _normalize_later_envelope(value: dict[str, object]) -> dict[str, object]:
    """Losslessly flatten one exact provider echo of the later contract.

    The public schema exposes ``delay_seconds`` and ``expires_after_seconds``
    at the draft root, but JSON models sometimes group those same two fields
    under a literal ``later`` object copied from the prompt heading. Promote
    only that exact one-to-one envelope. Unknown fields, non-later timing,
    complete redundant envelopes, and conflicting root values remain present
    so strict Pydantic validation fails closed.
    """

    nested = value.get("later")
    fields = ("delay_seconds", "expires_after_seconds")
    if (
        value.get("timing_choice") != "later"
        or not isinstance(nested, dict)
        or set(nested) != set(fields)
        or all(field in value for field in fields)
        or any(field in value and value[field] != nested[field] for field in fields)
    ):
        return value
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if key != "later":
            normalized[key] = item
            continue
        for field in fields:
            normalized.setdefault(field, nested[field])
    return normalized


def normalize_expression_draft_wire(value: dict[str, object]) -> dict[str, object]:
    """Normalize only exact, lossless provider wire aliases before parsing."""

    value = _normalize_later_envelope(value)
    value = _normalize_world_claim_aliases(value)
    value = _normalize_cadence_alias(value)
    beats = value.get("beats")
    if not isinstance(beats, (list, tuple)):
        return value
    normalized_beats: list[object] = []
    changed = False
    for item in beats:
        if (
            isinstance(item, dict)
            and "role" in item
            and item["role"] in _LEGACY_EXPRESSION_BEAT_ROLES
        ):
            normalized_item = dict(item)
            normalized_item.pop("role")
            normalized_beats.append(normalized_item)
            changed = True
        else:
            normalized_beats.append(item)
    if not changed:
        return value
    normalized = dict(value)
    normalized["beats"] = normalized_beats
    return normalized


class ExpressionPlanBeatMaterialization(NamedTuple):
    beat_values: list[dict[str, object]]
    intents: list[ProposalActionIntent]


def materialize_expression_plan_beats(
    *,
    draft: ExpressionDraft,
    identity: str,
    namespace: str,
    change_id: str,
    target: str,
    provider_message_id: str | None,
    effective_windows: tuple[tuple[datetime, datetime] | None, ...],
    suggested_windows: tuple[tuple[datetime, datetime] | None, ...] | None = None,
    include_shadow_delay_window: bool = False,
    text_now_action_kind: str = "reply",
) -> ExpressionPlanBeatMaterialization:
    """Materialize ordered immutable beats for any ExpressionDraft caller.

    Callers retain their own proposal/source identities; this shared deep
    module owns only the transport-neutral sequence, payload bytes and
    dependency chain.  That keeps inbound and proactive plans from drifting.
    """

    if len(effective_windows) != len(draft.beats):
        raise ValueError("expression beat windows must match the selected beat count")
    if suggested_windows is not None and len(suggested_windows) != len(draft.beats):
        raise ValueError("expression shadow windows must match the selected beat count")
    beat_values: list[dict[str, object]] = []
    intents: list[ProposalActionIntent] = []
    previous_beat_id: str | None = None
    previous_intent_id: str | None = None
    for position, choice in enumerate(draft.beats, start=1):
        body, content_type, action_kind = _payload_for(
            choice=choice,
            timing_choice=draft.timing_choice,
            provider_message_id=provider_message_id,
        )
        if choice.modality == "text" and draft.timing_choice == "now":
            action_kind = text_now_action_kind
        beat_id = f"beat:{namespace}:{identity}:{position}"
        intent_id = f"intent:{namespace}:{identity}:{position}"
        payload_ref = f"payload:{namespace}:{identity}:{position}"
        payload_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        effective_window = effective_windows[position - 1]
        suggested_window = suggested_windows[position - 1] if suggested_windows else None
        delay_value = (
            {
                "not_before": effective_window[0].isoformat(),
                "expires_at": effective_window[1].isoformat(),
            }
            if effective_window is not None
            else None
        )
        beat_value: dict[str, object] = {
            "beat_id": beat_id,
            "inline_text": body,
            "materialized_payload_ref": payload_ref,
            "payload_hash": payload_hash,
            "content_type": content_type,
            "dependency_beat_ids": [previous_beat_id] if previous_beat_id else [],
            "delay_window": delay_value,
            "cancel_policy": "cancel-before-dispatch",
            "reconsider_policy": "reconsider-on-new-observation",
            "merge_policy": "model-reconsider",
        }
        if include_shadow_delay_window:
            beat_value["shadow_delay_window"] = (
                {
                    "not_before": suggested_window[0].isoformat(),
                    "expires_at": suggested_window[1].isoformat(),
                }
                if suggested_window is not None
                else None
            )
        beat_values.append(beat_value)
        intents.append(
            ProposalActionIntent(
                intent_id=intent_id,
                kind=action_kind,
                layer="external_action",
                target=target,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                causal_change_id=change_id,
                beat_ref=beat_id,
                dependencies=(previous_intent_id,) if previous_intent_id else (),
                due_window=effective_window,
            )
        )
        previous_beat_id, previous_intent_id = beat_id, intent_id
    return ExpressionPlanBeatMaterialization(beat_values=beat_values, intents=intents)


def materialize_expression_draft(
    *,
    value: dict[str, object],
    request: ModelInput,
    capabilities: ExpressionDraftCapabilities,
    cadence_draws: tuple[CadenceDraw, ...] | None = None,
    stable_identity_source_refs: frozenset[str] = frozenset(),
    private_state_context_json: str | None = None,
    source_ref_aliases: SourceRefAliasTable | None = None,
) -> DecisionProposal:
    """Bind one model choice to the verified trigger and immutable effects."""

    trigger = request.trigger_message
    if trigger is None:
        raise ValueError("ExpressionDraft requires a verified current message")
    aliases = source_ref_aliases or build_source_ref_alias_table(
        request=request,
        stable_identity_source_refs=stable_identity_source_refs,
        model_visible_context_json=private_state_context_json,
    )
    value = expand_expression_source_ref_aliases(value, aliases=aliases)
    validate_expression_private_turn_state(
        value=value,
        request=request,
        capabilities=capabilities,
        stable_identity_source_refs=stable_identity_source_refs,
        model_visible_context_json=private_state_context_json,
        source_ref_aliases=aliases,
    )
    value = normalize_expression_draft_wire(value)
    # JSON arrays are the natural wire representation of immutable tuples.
    # Field validators remain strict about every scalar and cross-field rule.
    draft = ExpressionDraft.model_validate_json(_canonical_json(value), strict=True)
    _validate_world_claims(
        draft=draft,
        request=request,
        stable_identity_source_refs=stable_identity_source_refs,
    )
    validate_expression_draft_capabilities(
        draft=draft,
        capabilities=capabilities,
        provider_message_id=trigger.platform_message_id,
    )
    cadence_draws = request.recorded_cadence_draws if cadence_draws is None else cadence_draws
    cadence_draws = tuple(item for item in cadence_draws if item.beat_position <= len(draft.beats))
    cadence_draw_refs = tuple(dict.fromkeys(item.draw_ref for item in cadence_draws))
    if cadence_draw_refs and not set(cadence_draw_refs).issubset(request.recorded_draw_refs):
        raise ValueError("cadence draws are not bound to the model request")
    if (
        capabilities.recorded_cadence_mode == "on"
        and draft.timing_choice == "now"
        and len(draft.beats) > 1
        and len(cadence_draws) != len(draft.beats) - 1
    ):
        raise ValueError("on cadence requires one recorded draw per subsequent beat")

    identity_draft = draft.model_dump(mode="json")
    # PrivateTurnState is proposal audit only.  It must never mint a second
    # set of TypedChange/ExpressionPlan/Beat/Intent identities for the same
    # model-selected visible effect.
    identity_draft.pop("private_turn_state", None)
    if capabilities.recorded_cadence_mode == "off":
        identity_draft.pop("cadence", None)
    identity = _digest(
        {
            "contract": (
                "expression-draft-materialization.1"
                if capabilities.recorded_cadence_mode == "off"
                else "expression-draft-materialization.3"
            ),
            "capability_profile": capabilities.profile_id,
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "reply_target": trigger.reply_target,
            "draft": identity_draft,
            # When upstream RandomAuthority supplies a draw, its immutable ref
            # participates in proposal identity and remains in ModelInput audit.
            "recorded_draw_refs": request.recorded_draw_refs,
            **(
                {
                    "cadence_draw_refs": cadence_draw_refs,
                    "cadence_policy_version": capabilities.cadence_policy_version,
                    "recorded_cadence_mode": capabilities.recorded_cadence_mode,
                }
                if capabilities.recorded_cadence_mode != "off"
                else {}
            ),
        }
    )
    proposal_id = f"proposal:expression:{identity}"
    evidence = (
        ProposalEvidenceRef(
            ref_id=trigger.observation_ref,
            evidence_kind="observed_message",
            source_world_revision=trigger.source_world_revision,
            immutable_hash=trigger.event_payload_hash,
        ),
        *_world_claim_evidence(
            draft=draft,
            request=request,
            # The current Observation is already the mandatory first evidence
            # item above.  A counterpart-history claim may cite it to mean
            # "the counterpart just reported X", but it must not be looked up
            # again as a Context-slice ledger binding.
            non_ledger_source_refs=stable_identity_source_refs
            | frozenset((trigger.observation_ref,)),
        ),
    )
    if draft.timing_choice == "silent":
        return DecisionProposal(
            proposal_id=proposal_id,
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=evidence,
            confidence=draft.confidence,
            brief_rationale=draft.brief_rationale,
            private_turn_state=draft.private_turn_state,
            impulse_summary=draft.impulse_summary,
            response_expectation_assessment=draft.response_expectation_assessment,
            behavior_tendency="remain_silent",
            variation_profile=draft.variation_profile,
            stance=draft.stance,
            display_strategy="withhold_for_now",
            timing_choice="silent",
            turn_posture=draft.turn_posture,
            episode_disposition=(
                "supersede_pending"
                if draft.turn_posture == "supersede"
                else None
            ),
        )

    origin: datetime | None = None
    due_window: tuple[datetime, datetime] | None = None
    if draft.timing_choice == "later":
        try:
            origin = datetime.fromisoformat(
                str(json.loads(request.model_content_json)["logical_time"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("later expression requires pinned logical_time") from exc
        if origin.tzinfo is None or origin.utcoffset() is None:
            raise ValueError("later expression requires timezone-aware pinned logical_time")
        assert draft.delay_seconds is not None and draft.expires_after_seconds is not None
        due_window = (
            origin + timedelta(seconds=draft.delay_seconds),
            origin + timedelta(seconds=draft.expires_after_seconds),
        )
    elif len(draft.beats) > 1 and capabilities.recorded_cadence_mode != "off":
        try:
            origin = datetime.fromisoformat(
                str(json.loads(request.model_content_json)["logical_time"])
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("recorded cadence requires pinned logical_time") from exc
        if origin.tzinfo is None or origin.utcoffset() is None:
            raise ValueError("recorded cadence requires timezone-aware pinned logical_time")

    expanded_cadence = (
        cadence_windows(
            origin=origin,
            profile=draft.cadence,
            beat_count=len(draft.beats),
            draws=cadence_draws,
        )
        if origin is not None
        and draft.timing_choice == "now"
        and len(draft.beats) > 1
        and cadence_draws
        else tuple(None for _ in draft.beats)
    )

    change_id = f"change:expression:{identity}"
    plan_id = f"plan:expression:{identity}"
    beat_material = materialize_expression_plan_beats(
        draft=draft,
        identity=identity,
        namespace="expression",
        change_id=change_id,
        target=trigger.reply_target,
        provider_message_id=trigger.platform_message_id,
        effective_windows=tuple(
            expanded_cadence[position]
            if (draft.timing_choice == "now" and capabilities.recorded_cadence_mode == "on")
            else due_window
            for position in range(len(draft.beats))
        ),
        suggested_windows=(
            tuple(expanded_cadence) if capabilities.recorded_cadence_mode == "shadow" else None
        ),
        include_shadow_delay_window=capabilities.recorded_cadence_mode == "shadow",
    )
    beat_values, intents = beat_material
    change = TypedChange(
        change_id=change_id,
        kind="expression_plan_transition",
        target_id=plan_id,
        transition="accept",
        payload=CanonicalTypedPayload.from_value(
            payload_schema="expression_plan_transition.v1",
            value={
                "plan_id": plan_id,
                "overall_intent": f"expression:{draft.timing_choice}",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle",
                **(
                    {
                        "cadence_profile": draft.cadence,
                        "cadence_policy_version": capabilities.cadence_policy_version,
                        "recorded_cadence_mode": capabilities.recorded_cadence_mode,
                        "recorded_draw_refs": cadence_draw_refs,
                    }
                    if capabilities.recorded_cadence_mode != "off"
                    else {}
                ),
                "beat_drafts": beat_values,
                "response_expectation": (
                    draft.response_expectation.model_dump(mode="json")
                    if draft.response_expectation is not None
                    else None
                ),
                "world_claims": [item.model_dump(mode="json") for item in draft.world_claims],
            },
        ),
    )
    return DecisionProposal(
        proposal_id=proposal_id,
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=evidence,
        proposed_changes=(change,),
        action_intents=tuple(intents),
        confidence=draft.confidence,
        brief_rationale=draft.brief_rationale,
        private_turn_state=draft.private_turn_state,
        impulse_summary=draft.impulse_summary,
        response_expectation_assessment=draft.response_expectation_assessment,
        behavior_tendency="respond" if draft.timing_choice == "now" else "defer",
        variation_profile=draft.variation_profile,
        stance=draft.stance,
        display_strategy="model_selected_expression",
        timing_choice=draft.timing_choice,
        turn_posture=draft.turn_posture,
        # Translate only the model's explicit conversational posture into the
        # existing episode lifecycle seam.  This is protocol plumbing, not a
        # host-selected social rule: an explicit episode_disposition supplied
        # by the adapter is applied afterwards and remains authoritative.
        episode_disposition=(
            "append"
            if draft.turn_posture == "continue"
            else "supersede_pending"
            if draft.turn_posture == "supersede"
            else None
        ),
    )


def _payload_for(
    *,
    choice: ExpressionBeatDraftChoice,
    timing_choice: TimingChoice,
    provider_message_id: str | None,
) -> tuple[str, str, str]:
    if choice.modality == "text":
        assert choice.text is not None
        return choice.text, "text/plain", "followup" if timing_choice == "later" else "reply"
    if choice.modality == "reaction":
        assert choice.reaction_id is not None and provider_message_id is not None
        return (
            _canonical_json(
                {
                    "provider_message_id": provider_message_id,
                    "reaction_id": choice.reaction_id,
                    "version": "expression-reaction.1",
                }
            ),
            "application/vnd.world-v2.reaction+json",
            "reaction",
        )
    if choice.modality == "sticker":
        assert choice.sticker_id is not None
        return (
            _canonical_json({"sticker_id": choice.sticker_id, "version": "expression-sticker.1"}),
            "application/vnd.world-v2.sticker+json",
            "sticker",
        )
    return (
        _canonical_json({"state": "composing", "version": "expression-typing.1"}),
        "application/vnd.world-v2.typing+json",
        "typing",
    )


__all__ = [
    "ExpressionDraft",
    "ExpressionDraftCapabilities",
    "ExpressionOption",
    "PrivateTurnStateValidationError",
    "QQ_NAPCAT_EXPRESSION_CAPABILITIES",
    "SourceRefAliasTable",
    "TEXT_ONLY_EXPRESSION_CAPABILITIES",
    "build_source_ref_alias_table",
    "current_counterpart_report_source_refs",
    "expand_expression_source_ref_aliases",
    "expression_hard_boundary_manifest",
    "invalid_world_claim_source_indexes",
    "materialize_expression_draft",
    "normalize_expression_draft_wire",
    "qq_expression_capabilities",
    "request_requires_response_expectation_assessment",
    "single_report_epistemic_scope_boundary",
    "world_source_scope_boundary",
    "world_claim_source_refs_by_scope",
    "world_claim_source_ref_aliases_by_scope",
]
