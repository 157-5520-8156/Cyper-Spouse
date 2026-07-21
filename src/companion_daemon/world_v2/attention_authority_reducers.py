"""Pure `.16.0` AttentionAuthority reducer and expiry-due validator.

DORMANT — no producer: no production ledger holds a committed
``V2Attention*`` event and no runtime constructs these payloads (the tests
guard replay semantics only).  The live phone-attention need is served by the
``attention_view`` advisory (a pure projection, never an event writer).
Before wiring a producer, read the Producer-First Authority rule in
CONTEXT.md and record the activation verdict in
``configs/mechanism_closure.yaml`` (``v16-situation-constituents``).
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json

from .actor_authority_reducers import ACTOR_AUTHORITY_V2_POLICY_DIGEST
from .attention_authority_events import (
    V2AttentionChangedPayload,
    v2_attention_evidence_refs,
    v2_attention_mutation_hash,
)
from .attention_authority_contract import require_attention_event_operation
from .attention_authority_schemas import (
    AttentionCompensationCauseAuthority,
    AttentionOperatorCorrectionBasis,
    AttentionReappraisalCorrectionBasis,
    OccurrenceAttentionFocusBinding,
    PlanAttentionFocusBinding,
    TriggerAttentionFocusBinding,
    V2AttentionExpiryDuePayload,
    V2AttentionProjection,
    V2AttentionTransitionProjection,
    canonical_projection_hash,
    v2_attention_semantic_fingerprint,
)
from .clock_authority import resolve_latest_clock
from .goal_situation_schemas import (
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
)
from .schemas import (
    ActorAuthorityProjection,
    ClockTransitionProjection,
    CommittedWorldEventRef,
    PlanStateProjection,
    TriggerProcess,
    WorldOccurrenceProjection,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
)


V2_ATTENTION_POLICY_REFS = ("policy:v2-attention-authority.1",)
V2_ATTENTION_POLICY_VERSION = "v2-attention-authority-policy.1"
V2_ATTENTION_OPERATOR_OPERATION = "v2_attention_governance"
V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION = "v2-attention-internal-intention.1"
_INTERNAL_POLICY = {
    "version": V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION,
    "installed_operation": "change",
    "intention_kind": "attention_choice",
    "selection_modes": ["direct"],
}
V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_INTERNAL_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
_POLICY_ARTIFACT = {
    "version": V2_ATTENTION_POLICY_VERSION,
    "single_head": "actor",
    "installed_lanes": ["operator", "deliberative", "compensation"],
    "settlement_adapter": None,
    "selection_modes": ["direct"],
    "mode_focus_invariants": True,
    "allocation_mapping": None,
    "interruptibility_mapping": None,
    "privacy": "lifetime-max",
    "zero_cascade": True,
}
V2_ATTENTION_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

ATTENTION_EXPIRY_POLICY_VERSION = "attention-expiry-policy.1"
_EXPIRY_POLICY = {
    "version": ATTENTION_EXPIRY_POLICY_VERSION,
    "due_when": "clock_to_gte_expires_at",
    "mechanical_effect": "open_trigger_only",
    "one_trigger_per_attention_revision": True,
}
ATTENTION_EXPIRY_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_EXPIRY_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()

_PRIVACY_RANK = {
    "public": 0,
    "shareable": 1,
    "personal": 2,
    "private": 3,
    "withhold": 4,
}


def reduce_v2_attention(
    attentions: tuple[V2AttentionProjection, ...],
    history: tuple[V2AttentionTransitionProjection, ...],
    payload: V2AttentionChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    plans: tuple[PlanStateProjection, ...] = (),
    world_occurrences: tuple[WorldOccurrenceProjection, ...] = (),
    triggers: tuple[TriggerProcess, ...] = (),
) -> tuple[tuple[V2AttentionProjection, ...], tuple[V2AttentionTransitionProjection, ...]]:
    """Apply one exact accepted mutation without changing any other domain."""

    # Reducers are an authority boundary.  Do not trust callers to have reached
    # us through Pydantic construction: model_copy and replay adapters can hand
    # us an instance whose nested validators never ran.
    payload = V2AttentionChangedPayload.model_validate(
        payload.model_dump(mode="python")
    )

    require_attention_event_operation(event_type=event_type, operation=payload.operation)
    if payload.accepted_change_hash != v2_attention_mutation_hash(payload) or (
        payload.evidence_refs != v2_attention_evidence_refs(payload)
    ):
        raise ValueError("Attention accepted hash or evidence binding is invalid")
    if payload.policy_refs != V2_ATTENTION_POLICY_REFS or (
        payload.policy_version != V2_ATTENTION_POLICY_VERSION
        or payload.policy_digest != V2_ATTENTION_POLICY_DIGEST
    ):
        raise ValueError("Attention mutation references an uninstalled policy")
    if payload.selection_mode == "random_draw":
        raise ValueError("random_authority_not_installed")
    if payload.random_draw_binding is not None:
        raise ValueError("random_authority_not_installed")
    if payload.authority_lane == "settlement":
        raise ValueError("attention_settlement_authority_not_installed")
    lane_is_exact = (
        payload.operation == "establish"
        and payload.authority_lane == "operator"
        and isinstance(payload.cause_authority, DomainOperatorAuthorityBinding)
    ) or (
        payload.operation == "change"
        and (
            (
                payload.authority_lane == "operator"
                and isinstance(payload.cause_authority, DomainOperatorAuthorityBinding)
            )
            or (
                payload.authority_lane == "deliberative"
                and isinstance(payload.cause_authority, DeliberativeCauseAuthority)
            )
        )
    ) or (
        payload.operation == "compensate"
        and payload.authority_lane == "compensation"
        and isinstance(payload.cause_authority, AttentionCompensationCauseAuthority)
    )
    if not lane_is_exact:
        raise ValueError("Attention operation, authority lane, and cause are not exact")

    after = payload.attention_after
    if (
        after.origin.accepted_event_ref != event_id
        or after.origin.change_id != payload.change_id
        or after.origin.transition_id != payload.transition_id
        or after.origin.policy_refs != V2_ATTENTION_POLICY_REFS
        or after.updated_at != logical_time
        or after.semantic_fingerprint
        != v2_attention_semantic_fingerprint(
            actor_ref=after.actor_ref,
            values=after.values,
            policy_refs=after.origin.policy_refs,
        )
    ):
        raise ValueError("Attention after image is not exact or event-pinned")
    if any(
        item.transition_id == payload.transition_id
        or item.change_id == payload.change_id
        or item.accepted_event_ref == event_id
        for item in history
    ):
        raise ValueError("Attention transition identity already exists")
    matches = [item for item in attentions if item.actor_ref == after.actor_ref]
    if len(matches) > 1:
        raise ValueError("actor has duplicate Attention heads")
    current = matches[0] if matches else None

    if payload.operation == "establish":
        _resolve_operator(
            payload.cause_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
        if (
            current is not None
            or payload.attention_before is not None
            or payload.expected_entity_revision != 0
            or after.entity_revision != 1
            or after.values.since != logical_time
        ):
            raise ValueError("Attention establish must create one event-pinned revision")
        _validate_normal_expiry(after, logical_time)
    elif payload.operation == "change":
        _validate_current(current, payload)
        assert current is not None
        if payload.authority_lane == "operator":
            _resolve_operator(
                payload.cause_authority,
                actor_authorities=actor_authorities,
                committed_events=committed_events,
                logical_time=logical_time,
                evaluated_world_revision=payload.evaluated_world_revision,
            )
            cause_privacy = "public"
        else:
            cause_privacy = _resolve_attention_intention(
                payload.cause_authority,
                actor_ref=after.actor_ref,
                evaluated_world_revision=payload.evaluated_world_revision,
                logical_time=logical_time,
            )
        if after.values == current.values:
            raise ValueError("Attention change is an exact semantic no-op")
        _validate_change_chronology(current, after, logical_time)
        _require_privacy(after.values.privacy_class, current.values.privacy_class, cause_privacy)
        _validate_normal_expiry(after, logical_time)
    else:
        _validate_current(current, payload)
        assert current is not None
        cause = payload.cause_authority
        if not isinstance(cause, AttentionCompensationCauseAuthority):
            raise ValueError("Attention compensation lacks typed target authority")
        _validate_compensation(
            current,
            after,
            payload,
            cause,
            history=history,
            committed_events=committed_events,
            actor_authorities=actor_authorities,
            logical_time=logical_time,
        )

    focus_privacy = _resolve_focus(
        after,
        pinned_world_revision=payload.evaluated_world_revision,
        plans=plans,
        committed_events=committed_events,
        world_occurrences=world_occurrences,
        triggers=triggers,
    )
    prior_privacy = current.values.privacy_class if current is not None else "public"
    _require_privacy(after.values.privacy_class, prior_privacy, focus_privacy)

    updated = tuple(
        sorted(
            (*(item for item in attentions if item.actor_ref != after.actor_ref), after),
            key=lambda item: item.actor_ref,
        )
    )
    transition = V2AttentionTransitionProjection(
        transition_id=payload.transition_id,
        actor_ref=after.actor_ref,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        authority_lane=payload.authority_lane,
        values_before=payload.attention_before.values if payload.attention_before else None,
        values_after=after.values,
        semantic_fingerprint_after=after.semantic_fingerprint,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        cause_authority=payload.cause_authority,
        compensates_transition_id=(
            cause.target_transition_id
            if isinstance((cause := payload.cause_authority), AttentionCompensationCauseAuthority)
            else None
        ),
    )
    return updated, (*history, transition)


def _validate_current(
    current: V2AttentionProjection | None, payload: V2AttentionChangedPayload
) -> None:
    after = payload.attention_after
    if (
        current is None
        or payload.attention_before != current
        or payload.expected_entity_revision != current.entity_revision
        or after.entity_revision != current.entity_revision + 1
        or after.actor_ref != current.actor_ref
        or after.updated_at < current.updated_at
    ):
        raise ValueError("Attention before image or entity revision is stale")


def _focus_identity(projection: V2AttentionProjection) -> tuple[str | None, str | None]:
    binding = projection.values.focus_binding
    return (binding.kind if binding is not None else None, projection.values.focus_ref)


def _validate_change_chronology(
    current: V2AttentionProjection, after: V2AttentionProjection, logical_time: datetime
) -> None:
    identity_changed = (
        current.values.mode != after.values.mode
        or _focus_identity(current) != _focus_identity(after)
    )
    expected_since = logical_time if identity_changed else current.values.since
    if after.values.since != expected_since or after.values.since > logical_time:
        raise ValueError("Attention since does not follow mode/focus identity")


def _validate_normal_expiry(after: V2AttentionProjection, logical_time: datetime) -> None:
    if after.values.expires_at is not None and after.values.expires_at <= logical_time:
        raise ValueError("ordinary Attention expiry must be after updated_at")


def _resolve_focus(
    after: V2AttentionProjection,
    *,
    pinned_world_revision: int,
    plans: tuple[PlanStateProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    triggers: tuple[TriggerProcess, ...],
) -> str:
    binding = after.values.focus_binding
    if binding is None:
        return "public"
    if binding.actor_ref != after.actor_ref or binding.pinned_world_revision != pinned_world_revision:
        raise ValueError("Attention focus actor or pinned revision is not exact")
    if isinstance(binding, PlanAttentionFocusBinding):
        source = next((item for item in plans if item.plan_id == binding.plan_id), None)
        origin = source.authority_origin if source is not None else None
        owner = source.owner_actor_ref if source is not None else None
        event = (
            next(
                (
                    item
                    for item in committed_events
                    if origin is not None and item.event_id == origin.accepted_event_ref
                ),
                None,
            )
            if origin is not None
            else None
        )
        if (
            source is None
            or source.entity_revision != binding.entity_revision
            or source.status != "active"
            or owner != after.actor_ref
            or owner == "legacy:unknown-owner"
            or origin is None
            or event is None
            or event.event_type != origin.accepted_event_type
            or event.event_type not in {"ActivityStarted", "ActivityResumed"}
            or event.world_revision != origin.accepted_world_revision
            or event.payload_hash != origin.accepted_payload_hash
            or event.logical_time != origin.accepted_at
            or origin.authority_projection_hash != plan_authority_projection_hash(source)
            or origin.binding_hash
            != plan_authority_binding_hash(
                plan_id=source.plan_id,
                owner_actor_ref=owner,
                entity_revision=source.entity_revision,
                transition_id=origin.transition_id,
                event_type=event.event_type,
                accepted_event_ref=event.event_id,
                accepted_world_revision=event.world_revision,
                accepted_payload_hash=event.payload_hash,
                accepted_at=event.logical_time,
                projection_hash=origin.authority_projection_hash,
            )
            or canonical_projection_hash(source) != binding.projection_hash
        ):
            raise ValueError("Attention Plan focus is not exact current active authority")
        return source.privacy_class
    if isinstance(binding, OccurrenceAttentionFocusBinding):
        source = next(
            (item for item in world_occurrences if item.occurrence_id == binding.occurrence_id),
            None,
        )
        if (
            source is None
            or source.entity_revision != binding.entity_revision
            or source.status != "active"
            or after.actor_ref not in source.participant_refs
            or canonical_projection_hash(source) != binding.projection_hash
        ):
            raise ValueError("Attention occurrence focus is not exact current active authority")
        return source.visibility
    assert isinstance(binding, TriggerAttentionFocusBinding)
    # TriggerProcess v2.1 has no actor authority field.  Exact ref/hash/state
    # therefore cannot prove that a trigger belongs to this Attention actor.
    # Keep the wire member for a future schema bundle, but fail closed until a
    # typed actor binding is installed in the shared TriggerProcess contract.
    raise ValueError("attention_trigger_focus_authority_not_installed")


def _resolve_attention_intention(
    cause: object,
    *,
    actor_ref: str,
    evaluated_world_revision: int,
    logical_time: datetime,
) -> str:
    cause = DeliberativeCauseAuthority.model_validate(
        cause.model_dump(mode="python") if hasattr(cause, "model_dump") else cause
    )
    if not isinstance(cause, DeliberativeCauseAuthority) or not isinstance(
        cause.basis, InternalIntentionBasis
    ):
        raise ValueError("Attention deliberative change requires exact internal intention")
    basis = cause.basis
    if (
        basis.actor_ref != actor_ref
        or basis.intention_kind != "attention_choice"
        or basis.evaluated_world_revision != evaluated_world_revision
        or basis.logical_time != logical_time
        or basis.policy_version != V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION
        or basis.policy_digest != V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST
    ):
        raise ValueError("internal intention cannot authorize Attention change")
    return max((basis.privacy_class, basis.rationale.privacy_class), key=_PRIVACY_RANK.__getitem__)


def _resolve_operator(
    cause: object,
    *,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
    evaluated_world_revision: int,
) -> None:
    if not isinstance(cause, DomainOperatorAuthorityBinding):
        raise ValueError("Attention mutation lacks operator authority")
    authority = next(
        (
            item
            for item in actor_authorities
            if item.authority_id == cause.authority_id
            and item.entity_revision == cause.authority_revision
        ),
        None,
    )
    event = next(
        (item for item in committed_events if item.event_id == cause.authority_event_ref),
        None,
    )
    if (
        authority is None
        or event is None
        or event.event_type
        not in {"ActorAuthorityBootstrapped", "ActorAuthorityRotated", "ActorAuthorityCompensated"}
        or event.world_revision != cause.authority_world_revision
        or event.payload_hash != cause.authority_payload_hash
        or event.world_revision > evaluated_world_revision
        or authority.origin.event_ref != event.event_id
        or authority.values.principal_ref != cause.principal_ref
        or authority.values.principal_kind != "deployment_operator"
        or authority.values.status != "active"
        or authority.values.valid_from > logical_time
        or (authority.values.expires_at is not None and authority.values.expires_at <= logical_time)
        or cause.required_operation != V2_ATTENTION_OPERATOR_OPERATION
        or cause.required_operation not in authority.values.allowed_operations
        or cause.authority_values_hash != canonical_projection_hash(authority.values)
        or authority.policy_version != "actor-authority-policy.2"
        or authority.policy_digest != ACTOR_AUTHORITY_V2_POLICY_DIGEST
        or cause.authority_policy_digest != authority.policy_digest
    ):
        raise ValueError("Attention operator cause lacks active exact ActorAuthority")


def _validate_compensation(
    current: V2AttentionProjection,
    after: V2AttentionProjection,
    payload: V2AttentionChangedPayload,
    cause: AttentionCompensationCauseAuthority,
    *,
    history: tuple[V2AttentionTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    logical_time: datetime,
) -> None:
    lineage = tuple(item for item in history if item.actor_ref == current.actor_ref)
    target = next(
        (item for item in lineage if item.transition_id == cause.target_transition_id), None
    )
    target_event = next(
        (item for item in committed_events if item.event_id == cause.target_accepted_event_ref),
        None,
    )
    expected_event_type = (
        "V2AttentionTransitionCompensated"
        if target is not None and target.operation == "compensate"
        else "V2AttentionChanged"
    )
    if (
        target is None
        or not lineage
        or target != lineage[-1]
        or target.values_before is None
        or target.entity_revision != cause.target_entity_revision
        or target.entity_revision != current.entity_revision
        or target.accepted_event_ref != cause.target_accepted_event_ref
        or target.values_after != current.values
        or target.semantic_fingerprint_after != current.semantic_fingerprint
        or target_event is None
        or target_event.event_type != expected_event_type
        or target_event.world_revision != cause.target_accepted_world_revision
        or target_event.payload_hash != cause.target_accepted_payload_hash
        or target_event.logical_time != target.accepted_at
        or target_event.world_revision > payload.evaluated_world_revision
    ):
        raise ValueError("Attention compensation target is not exact latest transition")
    lane = _effective_lane(target, lineage)
    if cause.expected_target_lane is not None and cause.expected_target_lane != lane:
        raise ValueError("Attention compensation expected lane is not authoritative")
    privacy_sources = [
        current.values.privacy_class,
        target.values_before.privacy_class,
        cause.correction_basis.privacy_class,
        cause.correction_rationale.privacy_class,
    ]
    if lane == "operator":
        if not isinstance(cause.correction_basis, AttentionOperatorCorrectionBasis):
            raise ValueError("operator Attention lineage requires operator correction basis")
        _resolve_operator(
            cause.operator_authority,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
            evaluated_world_revision=payload.evaluated_world_revision,
        )
    else:
        if (
            not isinstance(cause.correction_basis, AttentionReappraisalCorrectionBasis)
            or cause.operator_authority is not None
        ):
            raise ValueError("deliberative Attention lineage requires reappraisal correction")
        intention = cause.correction_basis.new_intention
        intention_privacy = _resolve_attention_intention(
            DeliberativeCauseAuthority(basis=intention),
            actor_ref=current.actor_ref,
            evaluated_world_revision=payload.evaluated_world_revision,
            logical_time=logical_time,
        )
        privacy_sources.append(intention_privacy)
    required_privacy = max(privacy_sources, key=_PRIVACY_RANK.__getitem__)
    expected_values = target.values_before.model_copy(update={"privacy_class": required_privacy})
    if after.values == current.values or after.values != expected_values:
        raise ValueError("Attention compensation must restore prior values with privacy max")


def _effective_lane(
    target: V2AttentionTransitionProjection,
    lineage: tuple[V2AttentionTransitionProjection, ...],
) -> str:
    current = target
    seen: set[str] = set()
    while current.operation == "compensate":
        if current.transition_id in seen or current.compensates_transition_id is None:
            raise ValueError("Attention compensation lineage is invalid or cyclic")
        seen.add(current.transition_id)
        current = next(
            (item for item in lineage if item.transition_id == current.compensates_transition_id),
            None,
        )  # type: ignore[assignment]
        if current is None:
            raise ValueError("Attention compensation lineage is incomplete")
    if current.authority_lane not in {"operator", "deliberative"}:
        raise ValueError("Attention compensation lineage has unsupported authority")
    return current.authority_lane


def _require_privacy(actual: str, *sources: str) -> None:
    required = max(sources, key=_PRIVACY_RANK.__getitem__)
    if _PRIVACY_RANK[actual] < _PRIVACY_RANK[required]:
        raise ValueError("Attention privacy cannot weaken below its authority floor")


def _digest(material: object) -> str:
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def attention_expiry_target_identity(
    *, world_id: str, actor_ref: str, attention_entity_revision: int
) -> str:
    return _digest(
        {
            "world_id": world_id,
            "process_kind": "v2_attention_expiry_due",
            "actor_ref": actor_ref,
            "attention_entity_revision": attention_entity_revision,
            "expiry_policy_digest": ATTENTION_EXPIRY_POLICY_DIGEST,
        }
    )


def validate_attention_expiry_due(
    payload: V2AttentionExpiryDuePayload,
    *,
    current: V2AttentionProjection,
    clock_transition_history: tuple[ClockTransitionProjection, ...],
    current_logical_time: datetime,
    occupied_target_identities: tuple[str, ...] = (),
) -> str:
    """Validate opening one due trigger; this never mutates the Attention head."""

    latest = resolve_latest_clock(
        clock_transition_history, current_logical_time=current_logical_time
    )
    binding = payload.binding
    target = attention_expiry_target_identity(
        world_id=payload.world_id,
        actor_ref=current.actor_ref,
        attention_entity_revision=current.entity_revision,
    )
    idempotency = _digest(
        {
            "world_id": payload.world_id,
            "event_type": "TriggerProcessOpened",
            "operation": "open_attention_expiry_due",
            "target_identity": target,
            "before_revision": current.entity_revision,
            "clock_event_ref": latest.clock_event_ref,
            "policy_digest": ATTENTION_EXPIRY_POLICY_DIGEST,
        }
    )
    if (
        current.values.expires_at is None
        or current.values.expires_at > latest.logical_time_to
        or binding.actor_ref != current.actor_ref
        or binding.attention_entity_revision != current.entity_revision
        or binding.attention_semantic_fingerprint != current.semantic_fingerprint
        or binding.expires_at != current.values.expires_at
        or binding.clock_event_ref != latest.clock_event_ref
        or binding.clock_world_revision != latest.computed_world_revision
        or binding.clock_payload_hash != latest.payload_hash
        or binding.logical_time_from != latest.logical_time_from
        or binding.logical_time_to != latest.logical_time_to
        or binding.clock_policy_version != latest.installed_policy_version
        or binding.clock_policy_digest != latest.installed_policy_digest
        or binding.expiry_policy_version != ATTENTION_EXPIRY_POLICY_VERSION
        or binding.expiry_policy_digest != ATTENTION_EXPIRY_POLICY_DIGEST
        or binding.idempotency_key != idempotency
        or binding.target_identity != target
        or payload.trigger_id != target
    ):
        raise ValueError("Attention expiry due binding is not exact")
    if target in occupied_target_identities:
        raise ValueError("Attention revision already owns an expiry due trigger")
    return target
