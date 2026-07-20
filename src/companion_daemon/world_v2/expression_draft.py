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
from typing import Literal

from pydantic import Field, model_validator

from .deliberation import ModelInput
from .expression_payload_contract import QQ_REACTION_OPTIONS, QQ_STICKER_OPTIONS
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
    VariationProfile,
)
from .schema_core import FrozenModel


ExpressionModality = Literal["text", "reaction", "sticker", "typing"]
TimingChoice = Literal["now", "later", "silent"]


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
    # Deferred commitment settlement currently owns one future effect.  This
    # is an installed execution limit, not a judgement about when to defer.
    max_later_beats: int = Field(default=1, ge=1, le=16)

    @model_validator(mode="after")
    def option_sets_match_modalities(self) -> "ExpressionDraftCapabilities":
        if self.modalities != tuple(dict.fromkeys(self.modalities)) or "text" not in self.modalities:
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
        return {
            "profile_id": self.profile_id,
            "modalities": self.modalities,
            "reaction_options": tuple(item.model_dump(mode="json") for item in self.reaction_options),
            "sticker_options": tuple(item.model_dump(mode="json") for item in self.sticker_options),
            "max_beats": self.max_beats,
            "max_later_beats": self.max_later_beats,
        }


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


def qq_expression_capabilities(adapter: str) -> ExpressionDraftCapabilities:
    """Return only modalities proven by the configured QQ transport dialect."""

    return (
        QQ_NAPCAT_EXPRESSION_CAPABILITIES
        if adapter.strip().lower() == "napcat"
        else TEXT_ONLY_EXPRESSION_CAPABILITIES
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
    wait_seconds: int = Field(ge=30, le=86_400)
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
        "shared_history",
        "stable_identity",
        "subjective_or_hypothetical",
    ]
    source_refs: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def source_shape_matches_scope(self) -> "WorldClaimDraft":
        grounded = self.scope in {"current_world", "past_world", "shared_history"}
        if grounded and not self.source_refs:
            raise ValueError("world claim scope requires matching source refs")
        if self.scope == "subjective_or_hypothetical" and self.source_refs:
            raise ValueError("subjective world claim cannot cite source refs")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("world claim source refs must be unique")
        return self


class ExpressionDraft(FrozenModel):
    """Small model draft; no IDs, targets, provider parameters or budgets."""

    timing_choice: TimingChoice = "now"
    beats: tuple[ExpressionBeatDraftChoice, ...] = Field(default=(), max_length=16)
    delay_seconds: int | None = Field(default=None, ge=1, le=86_400)
    expires_after_seconds: int | None = Field(default=None, ge=2, le=172_800)
    stance: str = Field(min_length=1, max_length=128)
    brief_rationale: str = Field(min_length=1, max_length=240)
    confidence: int = Field(default=5_000, ge=0, le=10_000)
    variation_profile: VariationProfile | None = None
    response_expectation: ResponseExpectationDraft | None = None
    world_claims: tuple[WorldClaimDraft, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def timing_and_visible_expression_are_orthogonal_but_complete(self) -> "ExpressionDraft":
        visible_content_seen = False
        for beat in self.beats:
            if beat.modality == "typing":
                if visible_content_seen:
                    raise ValueError("typing beats must precede visible content")
            else:
                visible_content_seen = True
        if self.timing_choice == "silent":
            if (
                self.beats
                or self.delay_seconds is not None
                or self.expires_after_seconds is not None
                or self.response_expectation is not None
            ):
                raise ValueError("silent expression cannot smuggle visible beats or a due window")
            return self
        if not self.beats:
            raise ValueError("visible expression requires at least one beat")
        if self.timing_choice == "now":
            if self.delay_seconds is not None or self.expires_after_seconds is not None:
                raise ValueError("immediate expression cannot select a due window")
            return self
        if self.delay_seconds is None or self.expires_after_seconds is None:
            raise ValueError("later expression requires a relative due window")
        if self.expires_after_seconds <= self.delay_seconds:
            raise ValueError("later expression expiry must follow its opening delay")
        return self


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
            for field in ("item_ref", "source_hash", "value_hash"):
                token = item.get(field)
                if isinstance(token, str):
                    tokens.add(token)
            bindings = item.get("source_bindings", ())
            if isinstance(bindings, list):
                tokens.update(
                    binding["ref"]
                    for binding in bindings
                    if isinstance(binding, dict) and isinstance(binding.get("ref"), str)
                )
    return tokens


def _validate_world_claims(*, draft: ExpressionDraft, request: ModelInput) -> None:
    try:
        context = json.loads(request.model_content_json)
    except (TypeError, json.JSONDecodeError) as exc:  # already validated upstream
        raise ValueError("world claim validation requires Context JSON") from exc
    if not isinstance(context, dict):
        raise ValueError("world claim validation requires a Context object")
    allowed = {
        "current_world": _slice_source_tokens(
            context, "current_situation", "world_life"
        ),
        "past_world": _slice_source_tokens(
            context, "world_life", "recent_experiences"
        ),
        "shared_history": _slice_source_tokens(
            context, "recent_dialogue", "recent_experiences"
        ),
        "stable_identity": _slice_source_tokens(context, "character_core"),
    }
    for claim in draft.world_claims:
        permitted = allowed.get(claim.scope)
        if permitted is not None and not set(claim.source_refs).issubset(permitted):
            outside = sorted(set(claim.source_refs) - permitted)
            raise ValueError(
                "world claim cites authority outside its semantic source lane: "
                f"scope={claim.scope} refs={outside[:4]} claim={claim.claim_text[:80]!r}"
            )


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
        if (
            isinstance(claim, dict)
            and "exact_source_refs" in claim
            and "source_refs" not in claim
        ):
            claim = {
                ("source_refs" if key == "exact_source_refs" else key): item
                for key, item in claim.items()
            }
            changed = True
        repaired.append(claim)
    if not changed:
        return value
    return {**value, "world_claims": repaired}


def materialize_expression_draft(
    *, value: dict[str, object], request: ModelInput, capabilities: ExpressionDraftCapabilities
) -> DecisionProposal:
    """Bind one model choice to the verified trigger and immutable effects."""

    trigger = request.trigger_message
    if trigger is None:
        raise ValueError("ExpressionDraft requires a verified current message")
    value = _normalize_world_claim_aliases(value)
    # JSON arrays are the natural wire representation of immutable tuples.
    # Field validators remain strict about every scalar and cross-field rule.
    draft = ExpressionDraft.model_validate_json(_canonical_json(value), strict=True)
    _validate_world_claims(draft=draft, request=request)
    if len(draft.beats) > capabilities.max_beats:
        raise ValueError("expression draft exceeds the deployment beat limit")
    if draft.timing_choice == "later" and len(draft.beats) > capabilities.max_later_beats:
        raise ValueError("later expression exceeds the installed deferred-effect limit")
    # The installed deferred-social contract currently materializes one
    # follow-up text Action.  Other modalities require their own source/target
    # and settlement contract at the future due time, so fail closed instead
    # of coercing the model's choice into text or an immediate effect.
    if draft.timing_choice == "later" and any(
        item.modality != "text" for item in draft.beats
    ):
        raise ValueError("later expression supports only the installed text modality")
    available = set(capabilities.modalities)
    if any(item.modality not in available for item in draft.beats):
        raise ValueError("expression modality is not available in this deployment")
    reaction_ids = {item.option_id for item in capabilities.reaction_options}
    sticker_ids = {item.option_id for item in capabilities.sticker_options}
    if any(item.reaction_id not in reaction_ids for item in draft.beats if item.modality == "reaction"):
        raise ValueError("reaction option is not available in this deployment")
    if any(item.sticker_id not in sticker_ids for item in draft.beats if item.modality == "sticker"):
        raise ValueError("sticker option is not available in this deployment")
    if any(item.modality == "reaction" for item in draft.beats) and not trigger.platform_message_id:
        raise ValueError("reaction requires a provider message binding")

    identity = _digest(
        {
            "contract": "expression-draft-materialization.1",
            "capability_profile": capabilities.profile_id,
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "reply_target": trigger.reply_target,
            "draft": draft.model_dump(mode="json"),
            # When upstream RandomAuthority supplies a draw, its immutable ref
            # participates in proposal identity and remains in ModelInput audit.
            "recorded_draw_refs": request.recorded_draw_refs,
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
    )
    if draft.timing_choice == "silent":
        return DecisionProposal(
            proposal_id=proposal_id,
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=evidence,
            confidence=draft.confidence,
            brief_rationale=draft.brief_rationale,
            behavior_tendency="remain_silent",
            variation_profile=draft.variation_profile,
            stance=draft.stance,
            display_strategy="withhold_for_now",
            timing_choice="silent",
        )

    origin: datetime | None = None
    due_window: tuple[datetime, datetime] | None = None
    if draft.timing_choice == "later":
        try:
            origin = datetime.fromisoformat(str(json.loads(request.model_content_json)["logical_time"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("later expression requires pinned logical_time") from exc
        if origin.tzinfo is None or origin.utcoffset() is None:
            raise ValueError("later expression requires timezone-aware pinned logical_time")
        assert draft.delay_seconds is not None and draft.expires_after_seconds is not None
        due_window = (
            origin + timedelta(seconds=draft.delay_seconds),
            origin + timedelta(seconds=draft.expires_after_seconds),
        )

    change_id = f"change:expression:{identity}"
    plan_id = f"plan:expression:{identity}"
    beat_values: list[dict[str, object]] = []
    intents: list[ProposalActionIntent] = []
    previous_beat_id: str | None = None
    previous_intent_id: str | None = None
    for position, choice in enumerate(draft.beats, start=1):
        body, content_type, action_kind = _payload_for(
            choice=choice,
            timing_choice=draft.timing_choice,
            provider_message_id=trigger.platform_message_id,
        )
        beat_id = f"beat:expression:{identity}:{position}"
        intent_id = f"intent:expression:{identity}:{position}"
        payload_ref = f"payload:expression:{identity}:{position}"
        payload_hash = "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()
        delay_value = (
            {
                "not_before": due_window[0].isoformat(),
                "expires_at": due_window[1].isoformat(),
            }
            if due_window is not None
            else None
        )
        beat_values.append(
            {
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
        )
        intents.append(
            ProposalActionIntent(
                intent_id=intent_id,
                kind=action_kind,
                layer="external_action",
                target=trigger.reply_target,
                payload_ref=payload_ref,
                payload_hash=payload_hash,
                causal_change_id=change_id,
                beat_ref=beat_id,
                dependencies=(previous_intent_id,) if previous_intent_id else (),
                due_window=due_window,
            )
        )
        previous_beat_id, previous_intent_id = beat_id, intent_id
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
                "beat_drafts": beat_values,
                "response_expectation": (
                    draft.response_expectation.model_dump(mode="json")
                    if draft.response_expectation is not None
                    else None
                ),
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
        behavior_tendency="respond" if draft.timing_choice == "now" else "defer",
        variation_profile=draft.variation_profile,
        stance=draft.stance,
        display_strategy="model_selected_expression",
        timing_choice=draft.timing_choice,
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
            _canonical_json(
                {"sticker_id": choice.sticker_id, "version": "expression-sticker.1"}
            ),
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
    "QQ_NAPCAT_EXPRESSION_CAPABILITIES",
    "TEXT_ONLY_EXPRESSION_CAPABILITIES",
    "materialize_expression_draft",
    "qq_expression_capabilities",
]
