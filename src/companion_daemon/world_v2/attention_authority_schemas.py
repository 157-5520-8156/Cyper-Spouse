"""Immutable contracts for the pure `.16.0` AttentionAuthority seam."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .attention_authority_contract import V2AttentionEventType, V2AttentionOperation
from .goal_situation_schemas import (
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
    SettledEventCauseAuthority,
)
from .schema_core import EvidenceRef, FrozenModel, PrivacyClass, canonicalize_json_value


AttentionMode = Literal[
    "available",
    "glancing",
    "occupied",
    "deep_focus",
    "do_not_disturb",
    "recovering_attention",
]
AttentionAuthorityLane = Literal["operator", "deliberative", "settlement", "compensation"]


class PlanAttentionFocusBinding(FrozenModel):
    kind: Literal["plan"] = "plan"
    actor_ref: str = Field(min_length=1)
    focus_ref: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    pinned_world_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def ref_is_derived(self) -> PlanAttentionFocusBinding:
        if self.focus_ref != self.plan_id:
            raise ValueError("Plan focus_ref must equal plan_id")
        return self


class OccurrenceAttentionFocusBinding(FrozenModel):
    kind: Literal["world_occurrence"] = "world_occurrence"
    actor_ref: str = Field(min_length=1)
    focus_ref: str = Field(min_length=1)
    occurrence_id: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    pinned_world_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def ref_is_derived(self) -> OccurrenceAttentionFocusBinding:
        if self.focus_ref != self.occurrence_id:
            raise ValueError("Occurrence focus_ref must equal occurrence_id")
        return self


class TriggerAttentionFocusBinding(FrozenModel):
    kind: Literal["trigger"] = "trigger"
    actor_ref: str = Field(min_length=1)
    focus_ref: str = Field(min_length=1)
    trigger_id: str = Field(min_length=1)
    trigger_ref: str = Field(min_length=1)
    projection_hash: str = Field(min_length=64, max_length=64)
    pinned_world_revision: int = Field(ge=0)

    @model_validator(mode="after")
    def ref_is_derived(self) -> TriggerAttentionFocusBinding:
        if self.focus_ref != self.trigger_id:
            raise ValueError("Trigger focus_ref must equal trigger_id")
        return self


AttentionFocusBinding = Annotated[
    PlanAttentionFocusBinding | OccurrenceAttentionFocusBinding | TriggerAttentionFocusBinding,
    Field(discriminator="kind"),
]


class V2AttentionValues(FrozenModel):
    mode: AttentionMode
    focus_ref: str | None = Field(default=None, min_length=1)
    focus_binding: AttentionFocusBinding | None = None
    allocation_bp: int = Field(ge=0, le=10_000, strict=True)
    interruptibility_bp: int = Field(ge=0, le=10_000, strict=True)
    since: datetime
    expires_at: datetime | None = None
    privacy_class: PrivacyClass

    @model_validator(mode="after")
    def focus_matches_mode(self) -> V2AttentionValues:
        required = self.mode in {"glancing", "occupied", "deep_focus"}
        forbidden = self.mode in {"available", "recovering_attention"}
        if required and self.focus_binding is None:
            raise ValueError("Attention mode requires a typed focus binding")
        if forbidden and (self.focus_binding is not None or self.focus_ref is not None):
            raise ValueError("Attention mode forbids focus")
        if (self.focus_binding is None) != (self.focus_ref is None):
            raise ValueError("focus_ref and focus_binding must be supplied together")
        if self.focus_binding is not None and self.focus_ref != self.focus_binding.focus_ref:
            raise ValueError("focus_ref must be derived from the typed focus binding")
        return self


class V2AttentionOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def canonical_projection_hash(value: object) -> str:
    material = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(material), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def v2_attention_semantic_fingerprint(
    *, actor_ref: str, values: V2AttentionValues, policy_refs: tuple[str, ...]
) -> str:
    return canonical_projection_hash(
        {
            "actor_ref": actor_ref,
            "values": values.model_dump(mode="json"),
            "policy_refs": sorted(policy_refs),
        }
    )


class V2AttentionProjection(FrozenModel):
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: V2AttentionValues
    origin: V2AttentionOrigin
    updated_at: datetime


class AttentionCorrectionRationale(FrozenModel):
    text: str = Field(min_length=1, max_length=512)
    privacy_class: PrivacyClass

    @field_validator("text")
    @classmethod
    def text_is_canonical(cls, value: str) -> str:
        if (
            value != value.strip()
            or value != unicodedata.normalize("NFC", value)
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise ValueError("Attention correction rationale must be trimmed NFC text")
        return value


class AttentionOperatorCorrectionBasis(FrozenModel):
    basis_kind: Literal["attention_operator_correction"] = "attention_operator_correction"
    correction_class: Literal[
        "initialization_error",
        "attention_assignment_error",
        "focus_assignment_error",
        "expiry_assignment_error",
        "privacy_classification_error",
    ]
    privacy_class: PrivacyClass


class AttentionReappraisalCorrectionBasis(FrozenModel):
    basis_kind: Literal["attention_reappraisal_correction"] = (
        "attention_reappraisal_correction"
    )
    correction_class: Literal[
        "attention_reassessed", "focus_reassessed", "expiry_reassessed"
    ]
    new_intention: InternalIntentionBasis
    privacy_class: PrivacyClass


AttentionCorrectionBasis = Annotated[
    AttentionOperatorCorrectionBasis | AttentionReappraisalCorrectionBasis,
    Field(discriminator="basis_kind"),
]


class AttentionCompensationCauseAuthority(FrozenModel):
    kind: Literal["attention_compensation"] = "attention_compensation"
    target_transition_id: str = Field(min_length=1)
    target_entity_revision: int = Field(ge=1)
    target_accepted_event_ref: str = Field(min_length=1)
    target_accepted_world_revision: int = Field(ge=1)
    target_accepted_payload_hash: str = Field(min_length=64, max_length=64)
    expected_target_lane: Literal["operator", "deliberative"] | None = None
    correction_basis: AttentionCorrectionBasis
    correction_rationale: AttentionCorrectionRationale
    operator_authority: DomainOperatorAuthorityBinding | None = None


AttentionCauseAuthority = Annotated[
    DomainOperatorAuthorityBinding
    | DeliberativeCauseAuthority
    | SettledEventCauseAuthority
    | AttentionCompensationCauseAuthority,
    Field(discriminator="kind"),
]


class V2AttentionTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: V2AttentionOperation
    authority_lane: AttentionAuthorityLane
    values_before: V2AttentionValues | None = None
    values_after: V2AttentionValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    cause_authority: AttentionCauseAuthority
    compensates_transition_id: str | None = None


class V2AttentionProposedMutation(FrozenModel):
    event_type: V2AttentionEventType
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical_object(self) -> V2AttentionProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("Attention proposed mutation must be a JSON object")
        if self.payload_json != json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ):
            raise ValueError("Attention proposed mutation JSON must be canonical")
        return self


class V2AttentionProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["v2_attention_transition"] = "v2_attention_transition"
    proposal_encoding: Literal["typed-authority-v1"] = "typed-authority-v1"
    authority_contract_ref: Literal["proposal-contract:v2-attention.1"] = (
        "proposal-contract:v2-attention.1"
    )
    transition_kind: V2AttentionOperation
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: V2AttentionProposedMutation

    @model_validator(mode="after")
    def transition_matches_payload(self) -> V2AttentionProposalProjection:
        from .attention_authority_contract import attention_event_for_operation
        from .attention_authority_events import V2_ATTENTION_CODEC

        if self.proposed_mutation.event_type != attention_event_for_operation(
            self.transition_kind
        ):
            raise ValueError("Attention proposal transition does not match event")
        mutation = V2_ATTENTION_CODEC.decode_payload(
            self.proposed_mutation.event_type, self.proposed_mutation.payload_json
        )
        expected = {
            "proposal_id": self.proposal_id,
            "change_id": self.change_id,
            "transition_id": self.transition_id,
            "evaluated_world_revision": self.evaluated_world_revision,
            "expected_entity_revision": self.expected_entity_revision,
            "accepted_change_hash": self.proposed_change_hash,
        }
        decoded = mutation.model_dump(mode="json")
        if any(decoded.get(key) != value for key, value in expected.items()):
            raise ValueError("Attention proposal envelope does not match mutation")
        if mutation.attention_after.actor_ref != self.actor_ref:
            raise ValueError("Attention proposal actor does not match mutation")
        if mutation.evidence_refs != self.evidence_refs or mutation.policy_refs != self.policy_refs:
            raise ValueError("Attention proposal authority material does not match mutation")
        return self


class AttentionExpiryDueBinding(FrozenModel):
    actor_ref: str = Field(min_length=1)
    attention_entity_revision: int = Field(ge=1)
    attention_semantic_fingerprint: str = Field(min_length=64, max_length=64)
    expires_at: datetime
    clock_event_ref: str = Field(min_length=1)
    clock_world_revision: int = Field(ge=1)
    clock_payload_hash: str = Field(min_length=64, max_length=64)
    logical_time_from: datetime
    logical_time_to: datetime
    clock_policy_version: str = Field(min_length=1)
    clock_policy_digest: str = Field(min_length=64, max_length=64)
    expiry_policy_version: Literal["attention-expiry-policy.1"]
    expiry_policy_digest: str = Field(min_length=64, max_length=64)
    idempotency_key: str = Field(min_length=64, max_length=64)
    target_identity: str = Field(min_length=64, max_length=64)


class V2AttentionExpiryDuePayload(FrozenModel):
    world_id: str = Field(min_length=1)
    process_kind: Literal["v2_attention_expiry_due"] = "v2_attention_expiry_due"
    trigger_id: str = Field(min_length=64, max_length=64)
    binding: AttentionExpiryDueBinding


def validate_v2_attention_authority_state(
    attentions: tuple[V2AttentionProjection, ...],
    transitions: tuple[V2AttentionTransitionProjection, ...],
    proposals: tuple[V2AttentionProposalProjection, ...],
    proposal_ids: tuple[str, ...],
    *,
    global_proposal_ids: tuple[str, ...],
    actor_authority_transitions: tuple[object, ...] = (),
    committed_events: tuple[object, ...] = (),
) -> None:
    """Validate canonical replay shape without trusting persisted head indexes."""

    # Replay objects may have been produced with model_copy, which skips nested
    # validators.  Rebuild every authority object before trusting its fields.
    attentions = tuple(
        V2AttentionProjection.model_validate(item.model_dump(mode="python"))
        for item in attentions
    )
    transitions = tuple(
        V2AttentionTransitionProjection.model_validate(item.model_dump(mode="python"))
        for item in transitions
    )
    proposals = tuple(
        V2AttentionProposalProjection.model_validate(item.model_dump(mode="python"))
        for item in proposals
    )

    actor_refs = tuple(item.actor_ref for item in attentions)
    if len(actor_refs) != len(set(actor_refs)) or actor_refs != tuple(sorted(actor_refs)):
        raise ValueError("Attention heads must contain canonical unique actors")
    for values, label in (
        (tuple(item.transition_id for item in transitions), "transition ids"),
        (tuple(item.change_id for item in transitions), "change ids"),
        (tuple(item.accepted_event_ref for item in transitions), "event refs"),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Attention {label} must be unique")
    by_actor: dict[str, list[V2AttentionTransitionProjection]] = {}
    for transition in transitions:
        by_actor.setdefault(transition.actor_ref, []).append(transition)
    heads = {item.actor_ref: item for item in attentions}
    if set(by_actor) != set(heads):
        raise ValueError("each Attention lineage must have one current head")
    if transitions:
        committed_revisions = tuple(
            int(getattr(event, "world_revision", 0))
            for transition in transitions
            for event in committed_events
            if getattr(event, "event_id", None) == transition.accepted_event_ref
        )
        if (
            len(committed_revisions) != len(transitions)
            or committed_revisions != tuple(sorted(committed_revisions))
            or len(set(committed_revisions)) != len(committed_revisions)
        ):
            raise ValueError("Attention history must follow committed revision order")
    privacy_rank = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
    from .attention_authority_contract import attention_event_for_operation
    from .attention_authority_reducers import (
        V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST,
        V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION,
        V2_ATTENTION_POLICY_DIGEST,
        V2_ATTENTION_POLICY_REFS,
        V2_ATTENTION_POLICY_VERSION,
    )

    def require_operator(
        operator: DomainOperatorAuthorityBinding,
        *,
        at: datetime,
        cutoff_world_revision: int,
    ) -> None:
        authority = next(
            (
                item for item in actor_authority_transitions
                if getattr(item, "authority_id", None) == operator.authority_id
                and getattr(item, "authority_revision", None)
                == operator.authority_revision
            ),
            None,
        )
        event = next(
            (
                item for item in committed_events
                if getattr(item, "event_id", None) == operator.authority_event_ref
            ),
            None,
        )
        values = getattr(authority, "values_after", None)
        values_hash = (
            hashlib.sha256(
                json.dumps(
                    values.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            if values is not None
            else None
        )
        if (
            authority is None
            or event is None
            or getattr(authority, "accepted_event_ref", None)
            != operator.authority_event_ref
            or getattr(authority, "accepted_world_revision", None)
            != operator.authority_world_revision
            or getattr(authority, "accepted_payload_hash", None)
            != operator.authority_payload_hash
            or getattr(authority, "policy_version", None) != "actor-authority-policy.2"
            or getattr(authority, "policy_digest", None)
            != "b6ef98db3e5313349fad22179af3a0a079a126b9aafb374f9c16fe3783b2a4ce"
            or getattr(authority, "policy_digest", None)
            != operator.authority_policy_digest
            or values_hash != operator.authority_values_hash
            or values.principal_ref != operator.principal_ref
            or values.principal_kind != "deployment_operator"
            or "v2_attention_governance" not in values.allowed_operations
            or values.status != "active"
            or values.valid_from > at
            or (values.expires_at is not None and values.expires_at <= at)
            or operator.required_operation != "v2_attention_governance"
            or getattr(event, "world_revision", None) != operator.authority_world_revision
            or getattr(event, "payload_hash", None) != operator.authority_payload_hash
            or getattr(event, "event_type", None)
            != {
                "bootstrap": "ActorAuthorityBootstrapped",
                "rotate": "ActorAuthorityRotated",
                "revoke": "ActorAuthorityRevoked",
                "compensate": "ActorAuthorityCompensated",
            }.get(getattr(authority, "operation", None))
            or getattr(event, "logical_time", None) != getattr(authority, "changed_at", None)
            or getattr(event, "world_revision", 0) >= cutoff_world_revision
        ):
            raise ValueError("Attention lacks exact operator authority")

    for actor_ref, lineage in by_actor.items():
        previous: V2AttentionTransitionProjection | None = None
        previous_world_revision = 0
        for expected_revision, transition in enumerate(lineage, start=1):
            if transition.entity_revision != expected_revision:
                raise ValueError("Attention entity revisions must be contiguous")
            event = next(
                (
                    item
                    for item in committed_events
                    if getattr(item, "event_id", None) == transition.accepted_event_ref
                ),
                None,
            )
            if (
                event is None
                or getattr(event, "event_type", None)
                != attention_event_for_operation(transition.operation)
                or getattr(event, "logical_time", None) != transition.accepted_at
            ):
                raise ValueError("Attention transition lacks exact committed event")
            world_revision = int(getattr(event, "world_revision", 0))
            if world_revision <= previous_world_revision:
                raise ValueError("Attention committed revisions must increase")
            previous_world_revision = world_revision
            if transition.policy_refs != ("policy:v2-attention-authority.1",):
                raise ValueError("Attention transition references uninstalled policy")
            if transition.semantic_fingerprint_after != v2_attention_semantic_fingerprint(
                actor_ref=actor_ref,
                values=transition.values_after,
                policy_refs=transition.policy_refs,
            ):
                raise ValueError("Attention transition fingerprint is invalid")
            if previous is None:
                if (
                    transition.operation != "establish"
                    or transition.values_before is not None
                    or transition.authority_lane != "operator"
                    or not isinstance(
                        transition.cause_authority, DomainOperatorAuthorityBinding
                    )
                    or transition.values_after.since != transition.accepted_at
                ):
                    raise ValueError("Attention lineage must begin with operator establish")
            else:
                if transition.values_before != previous.values_after:
                    raise ValueError("Attention before image does not match prior transition")
                if transition.accepted_at < previous.accepted_at:
                    raise ValueError("Attention logical time moves backward")
                if privacy_rank[transition.values_after.privacy_class] < privacy_rank[
                    previous.values_after.privacy_class
                ]:
                    raise ValueError("Attention lineage privacy weakens")
                identity_changed = (
                    transition.values_after.mode != previous.values_after.mode
                    or transition.values_after.focus_ref != previous.values_after.focus_ref
                    or (
                        transition.values_after.focus_binding.kind
                        if transition.values_after.focus_binding is not None
                        else None
                    )
                    != (
                        previous.values_after.focus_binding.kind
                        if previous.values_after.focus_binding is not None
                        else None
                    )
                )
                if transition.operation == "change" and transition.values_after.since != (
                    transition.accepted_at if identity_changed else previous.values_after.since
                ):
                    raise ValueError("Attention change has invalid since chronology")
                if transition.operation == "change" and (
                    transition.values_after == previous.values_after
                    or transition.authority_lane not in {"operator", "deliberative"}
                    or (
                        transition.authority_lane == "operator"
                        and not isinstance(
                            transition.cause_authority, DomainOperatorAuthorityBinding
                        )
                    )
                    or (
                        transition.authority_lane == "deliberative"
                        and not isinstance(
                            transition.cause_authority, DeliberativeCauseAuthority
                        )
                    )
                ):
                    raise ValueError("Attention change authority or no-op invariant is invalid")
                if transition.operation == "change" and transition.authority_lane == "deliberative":
                    cause = transition.cause_authority
                    assert isinstance(cause, DeliberativeCauseAuthority)
                    basis = cause.basis
                    if (
                        not isinstance(basis, InternalIntentionBasis)
                        or basis.actor_ref != actor_ref
                        or basis.logical_time != transition.accepted_at
                        or basis.intention_kind != "attention_choice"
                        or basis.policy_version
                        != V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION
                        or basis.policy_digest
                        != V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST
                    ):
                        raise ValueError("Attention deliberative history basis is not exact")
                    required_privacy = max(
                        (
                            previous.values_after.privacy_class,
                            basis.privacy_class,
                            basis.rationale.privacy_class,
                        ),
                        key=privacy_rank.__getitem__,
                    )
                    if privacy_rank[transition.values_after.privacy_class] < privacy_rank[
                        required_privacy
                    ]:
                        raise ValueError("Attention deliberative history weakens privacy")
                if transition.operation == "compensate" and (
                    transition.compensates_transition_id != previous.transition_id
                    or transition.authority_lane != "compensation"
                    or not isinstance(
                        transition.cause_authority, AttentionCompensationCauseAuthority
                    )
                ):
                    raise ValueError("Attention compensation must target then-latest transition")
                if transition.operation == "compensate":
                    cause = transition.cause_authority
                    assert isinstance(cause, AttentionCompensationCauseAuthority)
                    target_event = next(
                        (
                            item
                            for item in committed_events
                            if getattr(item, "event_id", None)
                            == previous.accepted_event_ref
                        ),
                        None,
                    )
                    if (
                        cause.target_transition_id != previous.transition_id
                        or cause.target_entity_revision != previous.entity_revision
                        or cause.target_accepted_event_ref != previous.accepted_event_ref
                        or target_event is None
                        or getattr(target_event, "world_revision", None)
                        != cause.target_accepted_world_revision
                        or getattr(target_event, "payload_hash", None)
                        != cause.target_accepted_payload_hash
                    ):
                        raise ValueError("Attention compensation target binding is not exact")
                    base = previous
                    visited: set[str] = set()
                    while base.operation == "compensate":
                        if (
                            base.transition_id in visited
                            or base.compensates_transition_id is None
                        ):
                            raise ValueError("Attention compensation lineage is cyclic")
                        visited.add(base.transition_id)
                        resolved = next(
                            (
                                item
                                for item in lineage
                                if item.transition_id == base.compensates_transition_id
                            ),
                            None,
                        )
                        if resolved is None:
                            raise ValueError("Attention compensation lineage is incomplete")
                        base = resolved
                    if (
                        base.authority_lane == "operator"
                        and (
                            not isinstance(
                                cause.correction_basis,
                                AttentionOperatorCorrectionBasis,
                            )
                            or cause.operator_authority is None
                        )
                    ) or (
                        base.authority_lane == "deliberative"
                        and (
                            not isinstance(
                                cause.correction_basis,
                                AttentionReappraisalCorrectionBasis,
                            )
                            or cause.operator_authority is not None
                        )
                    ):
                        raise ValueError("Attention compensation authority crosses lineage")
                    intention_privacy: tuple[str, ...] = ()
                    if isinstance(
                        cause.correction_basis, AttentionReappraisalCorrectionBasis
                    ):
                        intention = cause.correction_basis.new_intention
                        if (
                            intention.actor_ref != actor_ref
                            or intention.logical_time != transition.accepted_at
                            or intention.intention_kind != "attention_choice"
                            or intention.policy_version
                            != V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION
                            or intention.policy_digest
                            != V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST
                        ):
                            raise ValueError(
                                "Attention compensation intention is not exact"
                            )
                        intention_privacy = (
                            intention.privacy_class,
                            intention.rationale.privacy_class,
                        )
                    required_privacy = max(
                        (
                            previous.values_after.privacy_class,
                            previous.values_before.privacy_class,
                            cause.correction_basis.privacy_class,
                            cause.correction_rationale.privacy_class,
                            *intention_privacy,
                        ),
                        key=privacy_rank.__getitem__,
                    )
                    if transition.values_after != previous.values_before.model_copy(
                        update={"privacy_class": required_privacy}
                    ):
                        raise ValueError("Attention compensation restore image is invalid")
            if transition.operation != "compensate" and transition.compensates_transition_id is not None:
                raise ValueError("ordinary Attention transition cannot claim compensation target")
            if transition.operation in {"establish", "change"} and (
                transition.values_after.expires_at is not None
                and transition.values_after.expires_at <= transition.accepted_at
            ):
                raise ValueError("ordinary Attention expiry must follow accepted time")
            operator = None
            if isinstance(transition.cause_authority, DomainOperatorAuthorityBinding):
                operator = transition.cause_authority
            elif isinstance(
                transition.cause_authority, AttentionCompensationCauseAuthority
            ):
                operator = transition.cause_authority.operator_authority
            if operator is not None:
                require_operator(
                    operator,
                    at=transition.accepted_at,
                    cutoff_world_revision=world_revision,
                )
            previous = transition
        latest = lineage[-1]
        head = heads[actor_ref]
        if (
            head.entity_revision != latest.entity_revision
            or head.values != latest.values_after
            or head.semantic_fingerprint != latest.semantic_fingerprint_after
            or head.origin.change_id != latest.change_id
            or head.origin.transition_id != latest.transition_id
            or head.origin.policy_refs != latest.policy_refs
            or head.origin.accepted_event_ref != latest.accepted_event_ref
            or head.updated_at != latest.accepted_at
        ):
            raise ValueError("Attention head is not latest canonical transition")
    actual_proposal_ids = tuple(item.proposal_id for item in proposals)
    if proposal_ids != actual_proposal_ids or len(actual_proposal_ids) != len(
        set(actual_proposal_ids)
    ):
        raise ValueError("Attention proposal index does not match proposals")
    if any(item not in global_proposal_ids for item in actual_proposal_ids):
        raise ValueError("Attention proposal id is absent from global index")

    from .attention_authority_events import V2_ATTENTION_CODEC

    current_world_revision = max(
        (int(getattr(item, "world_revision", 0)) for item in committed_events),
        default=0,
    )
    for proposal in proposals:
        payload = V2_ATTENTION_CODEC.decode_payload(
            proposal.proposed_mutation.event_type,
            proposal.proposed_mutation.payload_json,
        )
        if (
            payload.operation != proposal.transition_kind
            or payload.proposal_id != proposal.proposal_id
            or payload.change_id != proposal.change_id
            or payload.transition_id != proposal.transition_id
            or payload.attention_after.actor_ref != proposal.actor_ref
            or payload.evaluated_world_revision != proposal.evaluated_world_revision
            or payload.expected_entity_revision != proposal.expected_entity_revision
            or payload.accepted_change_hash != proposal.proposed_change_hash
            or payload.evidence_refs != proposal.evidence_refs
            or payload.policy_refs != proposal.policy_refs
            or payload.model_dump(mode="json")
            != json.loads(proposal.proposed_mutation.payload_json)
        ):
            raise ValueError("pending Attention proposal does not exactly bind its payload")
        if proposal.evaluated_world_revision > current_world_revision:
            raise ValueError("pending Attention proposal evaluates a future world revision")
        if any(
            evidence.source_world_revision is not None
            and evidence.source_world_revision > proposal.evaluated_world_revision
            for evidence in proposal.evidence_refs
        ):
            raise ValueError("pending Attention proposal uses evidence after its cutoff")
        if (
            payload.policy_refs != V2_ATTENTION_POLICY_REFS
            or payload.policy_version != V2_ATTENTION_POLICY_VERSION
            or payload.policy_digest != V2_ATTENTION_POLICY_DIGEST
            or payload.selection_mode != "direct"
            or payload.random_draw_binding is not None
        ):
            raise ValueError("pending Attention proposal uses uninstalled authority")

        eligible: list[V2AttentionTransitionProjection] = []
        for transition in transitions:
            if transition.actor_ref != proposal.actor_ref:
                continue
            event = next(
                (
                    item
                    for item in committed_events
                    if getattr(item, "event_id", None)
                    == transition.accepted_event_ref
                ),
                None,
            )
            if event is not None and int(getattr(event, "world_revision", 0)) <= (
                proposal.evaluated_world_revision
            ):
                eligible.append(transition)
        cutoff_transition = eligible[-1] if eligible else None
        cutoff_head = (
            V2AttentionProjection(
                actor_ref=cutoff_transition.actor_ref,
                entity_revision=cutoff_transition.entity_revision,
                semantic_fingerprint=cutoff_transition.semantic_fingerprint_after,
                values=cutoff_transition.values_after,
                origin=V2AttentionOrigin(
                    change_id=cutoff_transition.change_id,
                    transition_id=cutoff_transition.transition_id,
                    policy_refs=cutoff_transition.policy_refs,
                    accepted_event_ref=cutoff_transition.accepted_event_ref,
                ),
                updated_at=cutoff_transition.accepted_at,
            )
            if cutoff_transition is not None
            else None
        )
        before = payload.attention_before
        if payload.operation == "establish":
            cas_valid = (
                cutoff_head is None
                and before is None
                and payload.expected_entity_revision == 0
                and payload.attention_after.entity_revision == 1
                and payload.attention_after.values.since
                == payload.attention_after.updated_at
            )
        else:
            cas_valid = (
                cutoff_head is not None
                and before == cutoff_head
                and payload.expected_entity_revision == cutoff_head.entity_revision
                and payload.attention_after.entity_revision
                == cutoff_head.entity_revision + 1
                and payload.attention_after.actor_ref == cutoff_head.actor_ref
                and payload.attention_after.updated_at >= cutoff_head.updated_at
            )
        if not cas_valid:
            raise ValueError("pending Attention proposal has an invalid cutoff CAS")
        operator = None
        if isinstance(payload.cause_authority, DomainOperatorAuthorityBinding):
            operator = payload.cause_authority
        elif isinstance(payload.cause_authority, AttentionCompensationCauseAuthority):
            operator = payload.cause_authority.operator_authority
        if operator is not None:
            require_operator(
                operator,
                at=payload.attention_after.updated_at,
                cutoff_world_revision=proposal.evaluated_world_revision + 1,
            )
