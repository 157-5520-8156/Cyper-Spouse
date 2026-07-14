from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.goal_situation_schemas import (
    ClockCauseAuthority,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
    RandomDrawBinding,
    SettledEventCauseAuthority,
    V2GoalRationale,
)
from companion_daemon.world_v2.resource_authority_contract import (
    V2_RESOURCE_MECHANICAL_EVENT_BY_OPERATION,
)
from companion_daemon.world_v2.resource_authority_events import (
    V2_RESOURCE_CODEC,
    V2ResourceChangedPayload,
    V2ResourceClockAdjustedPayload,
    V2SettledRecoveryInterval,
    reduce_v2_resource_clock_adjustment,
    v2_resource_evidence_refs,
    v2_resource_mutation_hash,
)
from companion_daemon.world_v2.resource_authority_reducers import (
    RESOURCE_BAND_POLICY_DIGEST,
    RESOURCE_BAND_POLICY_VERSION,
    V2_RESOURCE_INTERNAL_BASIS_POLICY_DIGEST,
    V2_RESOURCE_INTERNAL_BASIS_POLICY_VERSION,
    V2_RESOURCE_POLICY_DIGEST,
    V2_RESOURCE_POLICY_REFS,
    V2_RESOURCE_POLICY_VERSION,
    reduce_v2_resource,
)
from companion_daemon.world_v2.resource_authority_schemas import (
    ResourceCompensationCauseAuthority,
    ResourceCorrectionRationale,
    ResourceOperatorCorrectionBasis,
    ResourceSelfAssessmentCorrectionBasis,
    V2ResourceOrigin,
    V2ResourceProposalProjection,
    V2ResourceProjection,
    V2ResourceProposedMutation,
    V2ResourceTransitionProjection,
    V2ResourceValues,
    validate_v2_resource_authority_state,
    v2_resource_semantic_fingerprint,
)
from companion_daemon.world_v2.schema_core import PrivacyClass
from companion_daemon.world_v2.schemas import (
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityValues,
    CommittedWorldEventRef,
)


NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def operator_authority() -> tuple[
    ActorAuthorityProjection,
    CommittedWorldEventRef,
    DomainOperatorAuthorityBinding,
]:
    values = ActorAuthorityValues(
        principal_ref="operator:deployment",
        principal_kind="deployment_operator",
        credential_ref="credential:resource-governance",
        allowed_operations=("v2_resource_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        status="active",
    )
    event = CommittedWorldEventRef(
        event_id="event:actor-authority:resource",
        event_type="ActorAuthorityBootstrapped",
        world_revision=4,
        payload_hash="9" * 64,
        logical_time=NOW - timedelta(hours=1),
    )
    authority = ActorAuthorityProjection(
        authority_id="actor-authority:resource",
        entity_revision=1,
        values=values,
        policy_version="actor-authority-policy.2",
        policy_digest=ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        origin=ActorAuthorityOrigin(
            transition_id="transition:actor-authority:resource",
            event_ref=event.event_id,
            root_key_id="deployment-root:production-1",
            root_keyset_version="deployment-root-keyset.1",
            root_keyset_digest="a" * 64,
            root_nonce_hash="b" * 64,
            root_proof_hash="c" * 64,
        ),
        updated_at=event.logical_time,
    )
    binding = DomainOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        principal_ref=values.principal_ref,
        authority_event_ref=event.event_id,
        authority_world_revision=event.world_revision,
        authority_payload_hash=event.payload_hash,
        authority_values_hash=canonical_hash(values.model_dump(mode="json")),
        authority_policy_digest=authority.policy_digest,
        authorization_contract="deployment-actor-authority:v16-domain.1",
        required_operation="v2_resource_governance",
    )
    return authority, event, binding


def internal_cause(
    *,
    privacy: PrivacyClass = "private",
    logical_time: datetime = NOW,
    evaluated_world_revision: int = 7,
) -> DeliberativeCauseAuthority:
    rationale = V2GoalRationale(
        text="I reassessed how much capacity I actually have right now.",
        privacy_class=privacy,
    )
    material = {
        "basis_kind": "internal_intention",
        "actor_ref": "actor:companion",
        "trigger_ref": "trigger:resource-reflection:1",
        "decision_slot": "resource-self-regulation:1",
        "evaluated_world_revision": evaluated_world_revision,
        "logical_time": logical_time,
        "intention_kind": "resource_self_regulation",
        "intention_class": "constraint_response",
        "rationale": rationale,
        "policy_version": V2_RESOURCE_INTERNAL_BASIS_POLICY_VERSION,
        "policy_digest": V2_RESOURCE_INTERNAL_BASIS_POLICY_DIGEST,
        "privacy_class": "private",
    }
    basis = InternalIntentionBasis.model_validate(
        {
            **material,
            "intention_material_hash": canonical_hash(
                InternalIntentionBasis.model_construct(**material).model_dump(mode="json")
            ),
        }
    )
    return DeliberativeCauseAuthority(basis=basis)


def resource_projection(
    *,
    revision: int,
    value_bp: int,
    event_ref: str,
    privacy: PrivacyClass = "personal",
    kind: str = "physical_energy",
    updated_at: datetime = NOW,
) -> V2ResourceProjection:
    band = (
        "depleted"
        if value_bp < 1000
        else "low"
        if value_bp < 3500
        else "moderate"
        if value_bp < 6500
        else "high"
        if value_bp < 9000
        else "full"
    )
    values = V2ResourceValues(
        value_bp=value_bp,
        derived_band=band,
        band_policy_version=RESOURCE_BAND_POLICY_VERSION,
        band_policy_digest=RESOURCE_BAND_POLICY_DIGEST,
        privacy_class=privacy,
    )
    origin = V2ResourceOrigin(
        change_id=f"change:resource:{kind}:{revision}",
        transition_id=f"transition:resource:{kind}:{revision}",
        policy_refs=V2_RESOURCE_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return V2ResourceProjection(
        actor_ref="actor:companion",
        resource_kind=kind,
        entity_revision=revision,
        semantic_fingerprint=v2_resource_semantic_fingerprint(
            actor_ref="actor:companion",
            resource_kind=kind,
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        updated_at=updated_at,
    )


def initialized_history(head: V2ResourceProjection) -> tuple[V2ResourceTransitionProjection, ...]:
    """Independent committed revision-one fixture for pure reducer entry tests."""

    _, _, operator = operator_authority()
    return (
        V2ResourceTransitionProjection(
            transition_id=head.origin.transition_id,
            actor_ref=head.actor_ref,
            resource_kind=head.resource_kind,
            entity_revision=1,
            operation="initialize",
            authority_lane="operator",
            value_after=head.values.value_bp,
            band_after=head.values.derived_band,
            values_after=head.values,
            semantic_fingerprint_after=head.semantic_fingerprint,
            change_id=head.origin.change_id,
            policy_refs=head.origin.policy_refs,
            policy_version=V2_RESOURCE_POLICY_VERSION,
            policy_digest=V2_RESOURCE_POLICY_DIGEST,
            accepted_event_ref=head.origin.accepted_event_ref,
            accepted_at=head.updated_at,
            cause_authority=operator,
        ),
    )


def payload(
    after: V2ResourceProjection,
    *,
    cause: object,
    before: V2ResourceProjection | None = None,
    operation: str = "initialize",
    lane: str | None = None,
    adjust_kind: str | None = None,
    delta_bp: int | None = None,
    selection_mode: str = "direct",
    random_draw_binding: RandomDrawBinding | None = None,
    evaluated_world_revision: int = 7,
) -> V2ResourceChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": (),
        "policy_refs": V2_RESOURCE_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane
        or (
            "compensation"
            if operation == "compensate"
            else "operator"
            if operation == "initialize"
            else "deliberative"
        ),
        "selection_mode": selection_mode,
        "random_draw_binding": random_draw_binding,
        "resource_before": before,
        "resource_after": after,
        "adjust_kind": adjust_kind,
        "delta_bp": delta_bp,
        "cause_authority": cause,
        "policy_version": V2_RESOURCE_POLICY_VERSION,
        "policy_digest": V2_RESOURCE_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_resource_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_resource_mutation_hash(raw)
    return V2ResourceChangedPayload.model_validate(raw)


def apply(
    heads: tuple[V2ResourceProjection, ...],
    history: tuple,
    mutation: V2ResourceChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    authorities: tuple[ActorAuthorityProjection, ...] = (),
    committed: tuple[CommittedWorldEventRef, ...] = (),
):
    known = {item.event_id for item in committed}
    synthetic = tuple(
        CommittedWorldEventRef(
            event_id=item.accepted_event_ref,
            event_type={
                "initialize": "V2ResourceStateInitialized",
                "adjust": "V2ResourceStateAdjusted",
                "compensate": "V2ResourceTransitionCompensated",
            }[item.operation],
            world_revision=index,
            payload_hash="1" * 64,
            logical_time=item.accepted_at,
        )
        for index, item in enumerate(history, start=1)
        if item.accepted_event_ref not in known
    )
    return reduce_v2_resource(
        heads,
        history,
        mutation,
        event_type=event_type,
        event_id=event_id,
        logical_time=logical_time,
        actor_authorities=authorities,
        committed_events=(*synthetic, *committed),
    )


@pytest.mark.parametrize(
    ("kind", "value", "band"),
    (
        ("physical_energy", 0, "depleted"),
        ("physical_energy", 999, "depleted"),
        ("cognitive_capacity", 1000, "low"),
        ("cognitive_capacity", 3499, "low"),
        ("social_capacity", 3500, "moderate"),
        ("social_capacity", 6499, "moderate"),
        ("social_capacity", 6500, "high"),
        ("physical_energy", 8999, "high"),
        ("cognitive_capacity", 9000, "full"),
        ("physical_energy", 10000, "full"),
    ),
)
def test_operator_initializes_each_closed_resource_with_frozen_band(
    kind: str, value: int, band: str
) -> None:
    authority, authority_event, operator = operator_authority()
    after = resource_projection(
        revision=1, value_bp=value, kind=kind, event_ref=f"event:resource:{kind}:1"
    )
    heads, history = apply(
        (),
        (),
        payload(after, cause=operator),
        event_type="V2ResourceStateInitialized",
        event_id=after.origin.accepted_event_ref,
        logical_time=NOW,
        authorities=(authority,),
        committed=(authority_event,),
    )
    assert heads == (after,)
    assert history[0].band_after == band
    assert history[0].delta_bp is None


def test_band_policy_digest_is_a_fixed_cross_replay_contract() -> None:
    assert RESOURCE_BAND_POLICY_DIGEST == (
        "fca79bf8359b73c5e52cfd1c0c0d429511a39ce40210b121f52b9a531a87f06d"
    )


def test_deliberative_self_regulation_adjusts_by_exact_nonzero_integer_delta() -> None:
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    after = resource_projection(
        revision=2,
        value_bp=6200,
        privacy="private",
        event_ref="event:resource:2",
        updated_at=NOW,
    )
    mutation = payload(
        after,
        before=before,
        cause=internal_cause(),
        operation="adjust",
        adjust_kind="state_change",
        delta_bp=-800,
    )
    heads, history = apply(
        (before,),
        initialized_history(before),
        mutation,
        event_type="V2ResourceStateAdjusted",
        event_id="event:resource:2",
        logical_time=NOW,
    )
    assert heads == (after,)
    assert history[-1].value_before == 7000
    assert history[-1].delta_bp == -800
    assert history[-1].value_after == 6200
    assert history[-1].band_after == "moderate"


@pytest.mark.parametrize(
    ("delta", "after_value"), ((0, 7000), (-800, 6300), (4000, 11000))
)
def test_adjustment_rejects_zero_nonconserving_and_out_of_range_values(
    delta: int, after_value: int
) -> None:
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    if after_value > 10000:
        with pytest.raises(ValidationError):
            resource_projection(
                revision=2, value_bp=after_value, event_ref="event:resource:2"
            )
        return
    after = resource_projection(revision=2, value_bp=after_value, event_ref="event:resource:2")
    with pytest.raises(ValueError):
        apply(
            (before,),
            initialized_history(before),
            payload(
                after,
                before=before,
                cause=internal_cause(),
                operation="adjust",
                adjust_kind="state_change",
                delta_bp=delta,
            ),
            event_type="V2ResourceStateAdjusted",
            event_id="event:resource:2",
            logical_time=NOW,
        )


def test_derived_band_is_recomputed_and_reclassify_has_no_installed_new_policy() -> None:
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    forged_values = before.values.model_copy(update={"value_bp": 3000})
    forged = resource_projection(revision=2, value_bp=3000, event_ref="event:resource:2").model_copy(
        update={"values": forged_values}
    )
    with pytest.raises(ValueError, match="band"):
        apply(
            (before,),
            initialized_history(before),
            payload(
                forged,
                before=before,
                cause=internal_cause(),
                operation="adjust",
                adjust_kind="state_change",
                delta_bp=-4000,
            ),
            event_type="V2ResourceStateAdjusted",
            event_id="event:resource:2",
            logical_time=NOW,
        )

    reclassified = resource_projection(revision=2, value_bp=7000, event_ref="event:resource:2")
    with pytest.raises(ValueError, match="reclassification_policy_not_installed"):
        apply(
            (before,),
            initialized_history(before),
            payload(
                reclassified,
                before=before,
                cause=internal_cause(),
                operation="adjust",
                adjust_kind="reclassify",
                delta_bp=0,
            ),
            event_type="V2ResourceStateAdjusted",
            event_id="event:resource:2",
            logical_time=NOW,
        )


def test_settlement_random_and_clock_recovery_are_all_fail_closed_without_change() -> None:
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    after = resource_projection(revision=2, value_bp=7100, event_ref="event:resource:2")
    settlement = SettledEventCauseAuthority(
        event_ref="event:activity:1",
        event_type="ActivityCompleted",
        world_revision=6,
        payload_hash="6" * 64,
    )
    settlement_payload = payload(
        after,
        before=before,
        cause=settlement,
        operation="adjust",
        lane="settlement",
        adjust_kind="state_change",
        delta_bp=100,
    )
    with pytest.raises(ValueError, match="resource_settlement_authority_not_installed"):
        apply(
            (before,), initialized_history(before), settlement_payload,
            event_type="V2ResourceStateAdjusted", event_id="event:resource:2", logical_time=NOW,
        )

    draw = RandomDrawBinding(
        draw_event_ref="event:draw:1",
        draw_world_revision=6,
        draw_payload_hash="d" * 64,
        attempt_id="attempt:draw:1",
        candidate_set_hash="c" * 64,
        selected_candidate_ref="resource-delta:+100",
        catalog_version="resource-deltas.1",
        sampler_version="sampler.1",
    )
    random_payload = payload(
        after,
        before=before,
        cause=internal_cause(),
        operation="adjust",
        adjust_kind="state_change",
        delta_bp=100,
        selection_mode="random_draw",
        random_draw_binding=draw,
    )
    random_json = json.dumps(
        random_payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with pytest.raises(ValueError, match="random_authority_not_installed"):
        V2_RESOURCE_CODEC.encode_payload(random_payload)
    with pytest.raises(ValueError, match="random_authority_not_installed"):
        V2_RESOURCE_CODEC.decode_payload("V2ResourceStateAdjusted", random_json)
    with pytest.raises(ValueError, match="random_authority_not_installed"):
        apply(
            (before,), initialized_history(before), random_payload,
            event_type="V2ResourceStateAdjusted", event_id="event:resource:2", logical_time=NOW,
        )
    with pytest.raises(ValidationError, match="random_authority_not_installed"):
        V2ResourceProposalProjection(
            proposal_id=random_payload.proposal_id,
            transition_kind="adjust",
            change_id=random_payload.change_id,
            transition_id=random_payload.transition_id,
            actor_ref=after.actor_ref,
            resource_kind=after.resource_kind,
            evaluated_world_revision=random_payload.evaluated_world_revision,
            expected_entity_revision=random_payload.expected_entity_revision,
            proposed_change_hash=random_payload.accepted_change_hash,
            evidence_refs=random_payload.evidence_refs,
            policy_refs=random_payload.policy_refs,
            proposed_mutation=V2ResourceProposedMutation(
                event_type="V2ResourceStateAdjusted", payload_json=random_json
            ),
        )

    direct_payload = payload(
        after,
        before=before,
        cause=internal_cause(),
        operation="adjust",
        adjust_kind="state_change",
        delta_bp=100,
    )
    clock_raw = direct_payload.model_dump(mode="python")
    clock_raw.update(
        operation="clock_adjust",
        authority_lane="clock_runtime",
    )
    with pytest.raises(ValidationError):
        V2ResourceChangedPayload.model_validate(clock_raw)
    with pytest.raises(ValueError, match="not owned"):
        V2_RESOURCE_CODEC.decode_payload(
            "V2ResourceClockAdjusted",
            json.dumps(clock_raw, default=str, sort_keys=True, separators=(",", ":")),
        )
    assert V2_RESOURCE_MECHANICAL_EVENT_BY_OPERATION == {
        "clock_adjust": "V2ResourceClockAdjusted"
    }


def test_mechanical_clock_wire_validates_exact_inputs_but_registry_is_empty() -> None:
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    after = resource_projection(revision=2, value_bp=7100, event_ref="event:resource:clock")
    clock = ClockCauseAuthority(
        clock_event_ref="event:clock:1",
        clock_world_revision=8,
        clock_payload_hash="a" * 64,
        logical_time_from=NOW - timedelta(hours=1),
        logical_time_to=NOW,
        policy_version="world-clock-policy.1",
        policy_digest="b" * 64,
    )
    interval = V2SettledRecoveryInterval(
        recovery_id="recovery:1",
        actor_ref=before.actor_ref,
        resource_kind=before.resource_kind,
        rest_class="sleep",
        interval_start=NOW - timedelta(minutes=30),
        interval_end=NOW,
        source_event_ref="event:rest:1",
        source_world_revision=7,
        source_payload_hash="c" * 64,
        source_entity_ref="activity:rest:1",
        source_entity_revision=1,
        privacy_class="private",
    )
    raw = {
        "world_id": "world:test",
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision,
        "resource_before": before,
        "resource_after": after,
        "clock_authority": clock,
        "recovery_intervals": (interval,),
        "raw_delta_bp": 100,
        "applied_delta_bp": 100,
        "input_digest": "0" * 64,
        "recovery_policy_version": "resource-recovery-policy.1",
        "recovery_policy_digest": "d" * 64,
    }
    material = V2ResourceClockAdjustedPayload.model_construct(**raw).model_dump(
        mode="json", exclude={"input_digest"}
    )
    raw["input_digest"] = canonical_hash(material)
    wire = V2ResourceClockAdjustedPayload.model_validate(raw)
    with pytest.raises(ValueError, match="resource_recovery_authority_not_installed"):
        reduce_v2_resource_clock_adjustment((before,), wire)
    with pytest.raises(ValidationError, match="input digest"):
        V2ResourceClockAdjustedPayload.model_validate(
            {**raw, "input_digest": "e" * 64}
        )
    malformed_interval = interval.model_copy(update={"source_world_revision": 8})
    malformed_raw = {**raw, "recovery_intervals": (malformed_interval,)}
    malformed_material = V2ResourceClockAdjustedPayload.model_construct(
        **malformed_raw
    ).model_dump(mode="json", exclude={"input_digest"})
    malformed_raw["input_digest"] = canonical_hash(malformed_material)
    with pytest.raises(ValidationError, match="exact Clock interval"):
        V2ResourceClockAdjustedPayload.model_validate(malformed_raw)
    nonconserving = {**raw, "applied_delta_bp": 99}
    nonconserving_material = V2ResourceClockAdjustedPayload.model_construct(
        **nonconserving
    ).model_dump(mode="json", exclude={"input_digest"})
    nonconserving["input_digest"] = canonical_hash(nonconserving_material)
    with pytest.raises(ValidationError, match="mechanical recovery envelope"):
        V2ResourceClockAdjustedPayload.model_validate(nonconserving)


def test_compensation_uses_exact_latest_recursive_lane_and_lifetime_privacy() -> None:
    before = resource_projection(
        revision=1, value_bp=7000, privacy="personal", event_ref="event:resource:1"
    )
    current = resource_projection(
        revision=2, value_bp=6200, privacy="private", event_ref="event:resource:2"
    )
    _, history = apply(
        (before,),
        initialized_history(before),
        payload(
            current,
            before=before,
            cause=internal_cause(),
            operation="adjust",
            adjust_kind="state_change",
            delta_bp=-800,
        ),
        event_type="V2ResourceStateAdjusted",
        event_id="event:resource:2",
        logical_time=NOW,
    )
    target_event = CommittedWorldEventRef(
        event_id="event:resource:2",
        event_type="V2ResourceStateAdjusted",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=NOW,
    )
    correction_intention = internal_cause(
        privacy="withhold", evaluated_world_revision=9
    ).basis
    assert isinstance(correction_intention, InternalIntentionBasis)
    correction = ResourceCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        expected_target_lane="deliberative",
        correction_basis=ResourceSelfAssessmentCorrectionBasis(
            correction_class="self_assessment_revised",
            new_intention=correction_intention,
            privacy_class="withhold",
        ),
        correction_rationale=ResourceCorrectionRationale(
            text="My earlier capacity estimate overstated the effect.",
            privacy_class="private",
        ),
    )
    after = resource_projection(
        revision=3,
        value_bp=7000,
        privacy="withhold",
        event_ref="event:resource:3",
    )
    heads, compensated = apply(
        (current,),
        history,
        payload(
            after,
            before=current,
            cause=correction,
            operation="compensate",
            lane="compensation",
            evaluated_world_revision=9,
        ),
        event_type="V2ResourceTransitionCompensated",
        event_id="event:resource:3",
        logical_time=NOW,
        committed=(target_event,),
    )
    assert heads[0].values.value_bp == 7000
    assert heads[0].values.privacy_class == "withhold"
    assert compensated[-1].compensates_transition_id == history[-1].transition_id

    compensation_event = CommittedWorldEventRef(
        event_id="event:resource:3",
        event_type="V2ResourceTransitionCompensated",
        world_revision=9,
        payload_hash="7" * 64,
        logical_time=NOW,
    )
    recursive_intention = internal_cause(
        privacy="withhold", evaluated_world_revision=10
    ).basis
    assert isinstance(recursive_intention, InternalIntentionBasis)
    recursive_cause = ResourceCompensationCauseAuthority(
        target_transition_id=compensated[-1].transition_id,
        target_entity_revision=compensated[-1].entity_revision,
        target_accepted_event_ref=compensation_event.event_id,
        target_accepted_world_revision=compensation_event.world_revision,
        target_accepted_payload_hash=compensation_event.payload_hash,
        expected_target_lane="deliberative",
        correction_basis=ResourceSelfAssessmentCorrectionBasis(
            correction_class="source_interpretation_revised",
            new_intention=recursive_intention,
            privacy_class="withhold",
        ),
        correction_rationale=ResourceCorrectionRationale(
            text="The correction itself used the wrong interpretation.",
            privacy_class="withhold",
        ),
    )
    recursive_after = resource_projection(
        revision=4,
        value_bp=6200,
        privacy="withhold",
        event_ref="event:resource:4",
    )
    recursive_heads, recursive_history = apply(
        heads,
        compensated,
        payload(
            recursive_after,
            before=heads[0],
            cause=recursive_cause,
            operation="compensate",
            lane="compensation",
            evaluated_world_revision=10,
        ),
        event_type="V2ResourceTransitionCompensated",
        event_id="event:resource:4",
        logical_time=NOW,
        committed=(target_event, compensation_event),
    )
    assert recursive_heads == (recursive_after,)
    assert recursive_history[-1].compensates_transition_id == compensated[-1].transition_id

    forged_lane = recursive_cause.model_copy(update={"expected_target_lane": "operator"})
    with pytest.raises(ValueError, match="expected lane"):
        apply(
            heads,
            compensated,
            payload(
                recursive_after,
                before=heads[0],
                cause=forged_lane,
                operation="compensate",
                lane="compensation",
                evaluated_world_revision=10,
            ),
            event_type="V2ResourceTransitionCompensated",
            event_id="event:resource:4",
            logical_time=NOW,
            committed=(target_event, compensation_event),
        )

    with pytest.raises(ValueError, match="exact latest|canonical committed"):
        apply(
            (heads[0],),
            compensated,
            payload(
                resource_projection(
                    revision=4, value_bp=6200, privacy="withhold", event_ref="event:resource:4"
                ),
                before=heads[0],
                cause=correction,
                operation="compensate",
                lane="compensation",
                evaluated_world_revision=10,
            ),
            event_type="V2ResourceTransitionCompensated",
            event_id="event:resource:4",
            logical_time=NOW,
            committed=(target_event,),
        )


def test_operator_origin_compensation_requires_typed_operator_reauthorization() -> None:
    authority, authority_event, operator = operator_authority()
    before = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    current = resource_projection(revision=2, value_bp=6500, event_ref="event:resource:2")
    _, history = apply(
        (before,), initialized_history(before),
        payload(
            current, before=before, cause=operator, operation="adjust", lane="operator",
            adjust_kind="state_change", delta_bp=-500,
        ),
        event_type="V2ResourceStateAdjusted", event_id="event:resource:2", logical_time=NOW,
        authorities=(authority,), committed=(authority_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id="event:resource:2", event_type="V2ResourceStateAdjusted",
        world_revision=8, payload_hash="8" * 64, logical_time=NOW,
    )
    cause = ResourceCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        expected_target_lane="operator",
        correction_basis=ResourceOperatorCorrectionBasis(
            correction_class="resource_assignment_error", privacy_class="private"
        ),
        correction_rationale=ResourceCorrectionRationale(
            text="The operator entered the wrong capacity value.", privacy_class="private"
        ),
        operator_authority=operator,
    )
    after = resource_projection(
        revision=3, value_bp=7000, privacy="private", event_ref="event:resource:3"
    )
    heads, _ = apply(
        (current,), history,
        payload(
            after, before=current, cause=cause, operation="compensate", lane="compensation",
            evaluated_world_revision=9,
        ),
        event_type="V2ResourceTransitionCompensated", event_id="event:resource:3",
        logical_time=NOW, authorities=(authority,), committed=(authority_event, target_event),
    )
    assert heads == (after,)

    without_reauth = cause.model_copy(update={"operator_authority": None})
    with pytest.raises(ValueError, match="operator"):
        apply(
            (current,), history,
            payload(
                after, before=current, cause=without_reauth, operation="compensate",
                lane="compensation", evaluated_world_revision=9,
            ),
            event_type="V2ResourceTransitionCompensated", event_id="event:resource:3",
            logical_time=NOW, committed=(target_event,),
        )


def test_same_tick_is_allowed_but_stale_before_and_privacy_weakening_are_rejected() -> None:
    before = resource_projection(
        revision=1, value_bp=7000, privacy="private", event_ref="event:resource:1"
    )
    after = resource_projection(
        revision=2, value_bp=6900, privacy="private", event_ref="event:resource:2"
    )
    mutation = payload(
        after, before=before, cause=internal_cause(), operation="adjust",
        adjust_kind="state_change", delta_bp=-100,
    )
    heads, _ = apply(
        (before,), initialized_history(before), mutation, event_type="V2ResourceStateAdjusted",
        event_id="event:resource:2", logical_time=NOW,
    )
    assert heads == (after,)

    attacked = before.model_copy(update={"entity_revision": 2})
    with pytest.raises(ValueError, match="latest canonical|stale"):
        apply(
            (attacked,), initialized_history(before), mutation, event_type="V2ResourceStateAdjusted",
            event_id="event:resource:2", logical_time=NOW,
        )
    weakened = resource_projection(
        revision=2, value_bp=6900, privacy="public", event_ref="event:resource:2"
    )
    with pytest.raises(ValueError, match="privacy"):
        apply(
            (before,), initialized_history(before),
            payload(
                weakened, before=before, cause=internal_cause(), operation="adjust",
                adjust_kind="state_change", delta_bp=-100,
            ),
            event_type="V2ResourceStateAdjusted", event_id="event:resource:2", logical_time=NOW,
        )


def test_codec_enforces_event_routing_and_canonical_json() -> None:
    _, _, operator = operator_authority()
    after = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    mutation = payload(after, cause=operator)
    encoded = V2_RESOURCE_CODEC.encode_payload(mutation)
    assert V2_RESOURCE_CODEC.decode_payload("V2ResourceStateInitialized", encoded) == mutation
    proposal = V2ResourceProposalProjection(
        proposal_id=mutation.proposal_id,
        transition_kind="initialize",
        change_id=mutation.change_id,
        transition_id=mutation.transition_id,
        actor_ref=after.actor_ref,
        resource_kind=after.resource_kind,
        evaluated_world_revision=mutation.evaluated_world_revision,
        expected_entity_revision=mutation.expected_entity_revision,
        proposed_change_hash=mutation.accepted_change_hash,
        evidence_refs=mutation.evidence_refs,
        policy_refs=mutation.policy_refs,
        proposed_mutation=V2ResourceProposedMutation(
            event_type="V2ResourceStateInitialized", payload_json=encoded
        ),
    )
    decoded_proposal = V2_RESOURCE_CODEC.decode_record(
        event_type="ProposalRecorded", payload=proposal.model_dump(mode="json")
    )
    assert V2_RESOURCE_CODEC.bind(decoded_proposal).mutation_event_type == (
        "V2ResourceStateInitialized"
    )
    assert V2_RESOURCE_CODEC.bind_mutation(mutation).change_id == mutation.change_id
    assert V2_RESOURCE_CODEC.mutation_identity(
        world_id="world:test",
        event_type="V2ResourceStateInitialized",
        payload=mutation.model_dump(mode="json"),
    ) == (
        "world:test",
        "actor:companion",
        "physical_energy",
        0,
        "transition:resource:physical_energy:1",
    )
    with pytest.raises(ValueError, match="event type"):
        V2_RESOURCE_CODEC.decode_payload("V2ResourceStateAdjusted", encoded)
    with pytest.raises(ValueError, match="canonical"):
        V2_RESOURCE_CODEC.decode_payload(
            "V2ResourceStateInitialized", json.dumps(mutation.model_dump(mode="json"), indent=2)
        )


def test_reducer_is_zero_cascade_and_rejects_duplicate_transition_identity() -> None:
    authority, authority_event, operator = operator_authority()
    after = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    heads, history = apply(
        (), (), payload(after, cause=operator), event_type="V2ResourceStateInitialized",
        event_id="event:resource:1", logical_time=NOW,
        authorities=(authority,), committed=(authority_event,),
    )
    assert isinstance(heads, tuple) and isinstance(history, tuple)
    with pytest.raises(ValueError, match="lineage|identity"):
        apply(
            (), history, payload(after, cause=operator),
            event_type="V2ResourceStateInitialized", event_id="event:resource:1",
            logical_time=NOW, authorities=(authority,), committed=(authority_event,),
        )


def test_resource_state_validator_rejects_ghost_heads_history_and_missing_commit() -> None:
    head = resource_projection(revision=1, value_bp=7000, event_ref="event:resource:1")
    history = initialized_history(head)
    committed = (
        CommittedWorldEventRef(
            event_id="event:resource:1",
            event_type="V2ResourceStateInitialized",
            world_revision=8,
            payload_hash="8" * 64,
            logical_time=NOW,
        ),
    )
    validate_v2_resource_authority_state(
        (head,), history, (), (), global_proposal_ids=(),
        committed_events=committed, logical_time=NOW,
    )
    with pytest.raises(ValueError, match="unique canonical"):
        validate_v2_resource_authority_state(
            (head, head), history, (), (), global_proposal_ids=(),
            committed_events=committed, logical_time=NOW,
        )
    forged_transition = history[0].model_copy(
        update={"semantic_fingerprint_after": "f" * 64}
    )
    forged_head = head.model_copy(update={"semantic_fingerprint": "f" * 64})
    with pytest.raises(ValueError, match="redundant values|fingerprint"):
        validate_v2_resource_authority_state(
            (forged_head,), (forged_transition,), (), (), global_proposal_ids=(),
            committed_events=committed, logical_time=NOW,
        )
    with pytest.raises(ValueError, match="requires committed events"):
        validate_v2_resource_authority_state(
            (head,), history, (), (), global_proposal_ids=(), logical_time=NOW,
        )


def test_unknown_resource_kind_and_non_integer_value_are_schema_errors() -> None:
    with pytest.raises(ValidationError):
        resource_projection(
            revision=1, value_bp=7000, kind="financial_budget", event_ref="event:resource:1"
        )
    with pytest.raises(ValidationError):
        V2ResourceValues(
            value_bp=7000.5,
            derived_band="high",
            band_policy_version=RESOURCE_BAND_POLICY_VERSION,
            band_policy_digest=RESOURCE_BAND_POLICY_DIGEST,
            privacy_class="private",
        )
