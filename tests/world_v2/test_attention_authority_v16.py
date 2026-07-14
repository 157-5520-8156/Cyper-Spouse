from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.attention_authority_events import (
    V2_ATTENTION_CODEC,
    V2AttentionChangedPayload,
    v2_attention_evidence_refs,
    v2_attention_mutation_hash,
)
from companion_daemon.world_v2.attention_authority_reducers import (
    ATTENTION_EXPIRY_POLICY_DIGEST,
    ATTENTION_EXPIRY_POLICY_VERSION,
    V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST,
    V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION,
    V2_ATTENTION_POLICY_DIGEST,
    V2_ATTENTION_POLICY_REFS,
    V2_ATTENTION_POLICY_VERSION,
    attention_expiry_target_identity,
    reduce_v2_attention,
    validate_attention_expiry_due,
)
from companion_daemon.world_v2.attention_authority_schemas import (
    AttentionExpiryDueBinding,
    AttentionOperatorCorrectionBasis,
    AttentionCompensationCauseAuthority,
    AttentionCorrectionRationale,
    PlanAttentionFocusBinding,
    TriggerAttentionFocusBinding,
    V2AttentionExpiryDuePayload,
    V2AttentionOrigin,
    V2AttentionProjection,
    V2AttentionProposalProjection,
    V2AttentionProposedMutation,
    V2AttentionValues,
    canonical_projection_hash,
    validate_v2_attention_authority_state,
    v2_attention_semantic_fingerprint,
)
from companion_daemon.world_v2.clock_authority import (
    CLOCK_AUTHORITY_POLICY_DIGEST,
    CLOCK_AUTHORITY_POLICY_VERSION,
)
from companion_daemon.world_v2.goal_situation_schemas import (
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
    RandomDrawBinding,
    V2GoalRationale,
)
from companion_daemon.world_v2.schemas import (
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityTransitionProjection,
    ActorAuthorityValues,
    ClockTransitionProjection,
    CommittedWorldEventRef,
    PlanAuthorityOrigin,
    PlanStateProjection,
    TriggerProcess,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
)
from companion_daemon.world_v2.schema_core import EvidenceRef


NOW = datetime(2026, 7, 15, 12, tzinfo=UTC)
ACTOR = "actor:companion"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def operator_authority() -> tuple[
    ActorAuthorityProjection, CommittedWorldEventRef, DomainOperatorAuthorityBinding
]:
    values = ActorAuthorityValues(
        principal_ref="operator:deployment",
        principal_kind="deployment_operator",
        credential_ref="credential:attention",
        allowed_operations=("v2_attention_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        status="active",
    )
    event = CommittedWorldEventRef(
        event_id="event:actor-authority:attention",
        event_type="ActorAuthorityBootstrapped",
        world_revision=3,
        payload_hash="a" * 64,
        logical_time=NOW - timedelta(hours=1),
    )
    authority = ActorAuthorityProjection(
        authority_id="authority:attention",
        entity_revision=1,
        values=values,
        policy_version="actor-authority-policy.2",
        policy_digest=ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        origin=ActorAuthorityOrigin(
            transition_id="transition:actor-authority:attention",
            event_ref=event.event_id,
            root_key_id="deployment-root:1",
            root_keyset_version="deployment-root-keyset.1",
            root_keyset_digest="b" * 64,
            root_nonce_hash="c" * 64,
            root_proof_hash="d" * 64,
        ),
        updated_at=event.logical_time,
    )
    binding = DomainOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=1,
        principal_ref=values.principal_ref,
        authority_event_ref=event.event_id,
        authority_world_revision=event.world_revision,
        authority_payload_hash=event.payload_hash,
        authority_values_hash=canonical_projection_hash(values),
        authority_policy_digest=authority.policy_digest,
        authorization_contract="deployment-actor-authority:v16-domain.1",
        required_operation="v2_attention_governance",
    )
    return authority, event, binding


def authority_transition(
    authority: ActorAuthorityProjection,
    event: CommittedWorldEventRef,
) -> ActorAuthorityTransitionProjection:
    return ActorAuthorityTransitionProjection(
        transition_id=authority.origin.transition_id,
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        operation="bootstrap",
        values_after=authority.values,
        policy_version=authority.policy_version,
        policy_digest=authority.policy_digest,
        root_key_id=authority.origin.root_key_id,
        root_keyset_version=authority.origin.root_keyset_version,
        root_keyset_digest=authority.origin.root_keyset_digest,
        root_nonce_hash=authority.origin.root_nonce_hash,
        root_proof_hash=authority.origin.root_proof_hash,
        accepted_event_ref=event.event_id,
        accepted_world_revision=event.world_revision,
        accepted_payload_hash=event.payload_hash,
        changed_at=event.logical_time,
    )


def intention(
    at: datetime, revision: int = 7, *, rationale_privacy: str = "private"
) -> DeliberativeCauseAuthority:
    raw = {
        "basis_kind": "internal_intention",
        "actor_ref": ACTOR,
        "trigger_ref": "trigger:attention-choice",
        "decision_slot": "attention:primary",
        "evaluated_world_revision": revision,
        "logical_time": at,
        "intention_kind": "attention_choice",
        "intention_class": "priority_reassessment",
        "rationale": V2GoalRationale(
            text="I want to refocus.", privacy_class=rationale_privacy
        ),
        "intention_material_hash": "0" * 64,
        "policy_version": V2_ATTENTION_INTERNAL_BASIS_POLICY_VERSION,
        "policy_digest": V2_ATTENTION_INTERNAL_BASIS_POLICY_DIGEST,
        "privacy_class": "private",
    }
    material = dict(raw)
    material.pop("intention_material_hash")
    material["logical_time"] = at.isoformat().replace("+00:00", "Z")
    material["rationale"] = raw["rationale"].model_dump(mode="json")
    raw["intention_material_hash"] = digest(material)
    return DeliberativeCauseAuthority(basis=InternalIntentionBasis.model_validate(raw))


def projection(
    revision: int,
    values: V2AttentionValues,
    *,
    event_ref: str,
    updated_at: datetime,
) -> V2AttentionProjection:
    origin = V2AttentionOrigin(
        change_id=f"change:attention:{revision}",
        transition_id=f"transition:attention:{revision}",
        policy_refs=V2_ATTENTION_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return V2AttentionProjection(
        actor_ref=ACTOR,
        entity_revision=revision,
        semantic_fingerprint=v2_attention_semantic_fingerprint(
            actor_ref=ACTOR, values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
        updated_at=updated_at,
    )


def payload(
    after: V2AttentionProjection,
    *,
    cause: object,
    before: V2AttentionProjection | None = None,
    operation: str = "establish",
    lane: str = "operator",
    evaluated_world_revision: int = 7,
) -> V2AttentionChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": (),
        "policy_refs": V2_ATTENTION_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.entity_revision}",
        "proposal_id": f"proposal:{after.entity_revision}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane,
        "selection_mode": "direct",
        "random_draw_binding": None,
        "attention_before": before,
        "attention_after": after,
        "cause_authority": cause,
        "policy_version": V2_ATTENTION_POLICY_VERSION,
        "policy_digest": V2_ATTENTION_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_attention_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_attention_mutation_hash(raw)
    return V2AttentionChangedPayload.model_validate(raw)


def available(at: datetime, *, privacy: str = "private") -> V2AttentionValues:
    return V2AttentionValues(
        mode="available",
        allocation_bp=3000,
        interruptibility_bp=8000,
        since=at,
        privacy_class=privacy,
    )


def test_operator_establishes_single_head_without_mapping_numeric_fields() -> None:
    authority, authority_event, cause = operator_authority()
    after = projection(1, available(NOW), event_ref="event:attention:1", updated_at=NOW)
    heads, history = reduce_v2_attention(
        (),
        (),
        payload(after, cause=cause),
        event_type="V2AttentionChanged",
        event_id="event:attention:1",
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    assert heads == (after,)
    assert history[0].values_after.allocation_bp == 3000
    assert history[0].values_after.interruptibility_bp == 8000


def test_deliberative_change_preserves_since_when_mode_and_focus_are_unchanged() -> None:
    before = projection(1, available(NOW), event_ref="event:attention:1", updated_at=NOW)
    changed_at = NOW + timedelta(minutes=2)
    after = projection(
        2,
        before.values.model_copy(update={"allocation_bp": 8700}),
        event_ref="event:attention:2",
        updated_at=changed_at,
    )
    heads, _ = reduce_v2_attention(
        (before,),
        (),
        payload(
            after,
            before=before,
            cause=intention(changed_at),
            operation="change",
            lane="deliberative",
        ),
        event_type="V2AttentionChanged",
        event_id="event:attention:2",
        logical_time=changed_at,
        actor_authorities=(),
        committed_events=(),
    )
    assert heads[0].values.since == NOW


def test_mode_change_resets_since_and_allows_same_tick() -> None:
    authority, authority_event, cause = operator_authority()
    before = projection(1, available(NOW), event_ref="event:attention:1", updated_at=NOW)
    after = projection(
        2,
        before.values.model_copy(
            update={"mode": "do_not_disturb", "allocation_bp": 0, "since": NOW}
        ),
        event_ref="event:attention:2",
        updated_at=NOW,
    )
    heads, _ = reduce_v2_attention(
        (before,),
        (),
        payload(after, before=before, cause=cause, operation="change"),
        event_type="V2AttentionChanged",
        event_id="event:attention:2",
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    assert heads[0].values.mode == "do_not_disturb"
    assert heads[0].values.interruptibility_bp == 8000


def test_typed_plan_focus_must_resolve_exact_current_active_projection() -> None:
    authority, authority_event, cause = operator_authority()
    plan_event = CommittedWorldEventRef(
        event_id="event:plan:started",
        event_type="ActivityStarted",
        world_revision=6,
        payload_hash="e" * 64,
        logical_time=NOW,
    )
    plan = PlanStateProjection(
        plan_id="plan:work",
        activity_id="activity:work",
        entity_revision=2,
        activity_kind="work",
        evidence_refs=(
            EvidenceRef(
                ref_id="message:plan",
                evidence_type="observed_message",
                claim_purpose="future_plan",
            ),
        ),
        status="active",
        importance_bp=7000,
        participant_refs=(ACTOR,),
        privacy_class="private",
        owner_actor_ref=ACTOR,
        last_transitioned_at=NOW,
    )
    projection_hash = plan_authority_projection_hash(plan)
    plan_origin = PlanAuthorityOrigin(
        transition_id="transition:plan:started",
        accepted_event_type="ActivityStarted",
        accepted_event_ref=plan_event.event_id,
        accepted_world_revision=plan_event.world_revision,
        accepted_payload_hash=plan_event.payload_hash,
        accepted_at=NOW,
        authority_projection_hash=projection_hash,
        binding_hash=plan_authority_binding_hash(
            plan_id="plan:work",
            owner_actor_ref=ACTOR,
            entity_revision=2,
            transition_id="transition:plan:started",
            event_type=plan_event.event_type,
            accepted_event_ref=plan_event.event_id,
            accepted_world_revision=plan_event.world_revision,
            accepted_payload_hash=plan_event.payload_hash,
            accepted_at=NOW,
            projection_hash=projection_hash,
        ),
    )
    plan = plan.model_copy(update={"authority_origin": plan_origin})
    binding = PlanAttentionFocusBinding(
        actor_ref=ACTOR,
        focus_ref=plan.plan_id,
        plan_id=plan.plan_id,
        entity_revision=plan.entity_revision,
        projection_hash=canonical_projection_hash(plan),
        pinned_world_revision=7,
    )
    values = V2AttentionValues(
        mode="deep_focus",
        focus_ref=plan.plan_id,
        focus_binding=binding,
        allocation_bp=9500,
        interruptibility_bp=2500,
        since=NOW,
        privacy_class="private",
    )
    after = projection(1, values, event_ref="event:attention:focus", updated_at=NOW)
    heads, _ = reduce_v2_attention(
        (), (), payload(after, cause=cause),
        event_type="V2AttentionChanged", event_id="event:attention:focus", logical_time=NOW,
        actor_authorities=(authority,), committed_events=(authority_event, plan_event), plans=(plan,),
    )
    assert heads[0].values.focus_binding == binding

    forged_origin = plan_origin.model_copy(update={"accepted_event_type": "ActivityPaused"})
    forged_plan = plan.model_copy(update={"authority_origin": forged_origin})
    forged_binding = binding.model_copy(
        update={"projection_hash": canonical_projection_hash(forged_plan)}
    )
    forged_values = values.model_copy(update={"focus_binding": forged_binding})
    forged_after = projection(
        1, forged_values, event_ref="event:attention:focus", updated_at=NOW
    )
    with pytest.raises(ValueError, match="Plan focus"):
        reduce_v2_attention(
            (), (), payload(forged_after, cause=cause),
            event_type="V2AttentionChanged",
            event_id="event:attention:focus",
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event, plan_event),
            plans=(forged_plan,),
        )

    stale = plan.model_copy(update={"status": "completed"})
    with pytest.raises(ValueError, match="Plan focus"):
        reduce_v2_attention(
            (), (), payload(after, cause=cause),
            event_type="V2AttentionChanged", event_id="event:attention:focus", logical_time=NOW,
            actor_authorities=(authority,), committed_events=(authority_event,), plans=(stale,),
        )


def test_trigger_focus_fails_closed_until_trigger_actor_authority_is_installed() -> None:
    authority, authority_event, cause = operator_authority()
    trigger = TriggerProcess(
        trigger_id="trigger:foreign",
        trigger_ref="trigger-ref:foreign",
        process_kind="observation",
        state="open",
    )
    binding = TriggerAttentionFocusBinding(
        actor_ref=ACTOR,
        focus_ref=trigger.trigger_id,
        trigger_id=trigger.trigger_id,
        trigger_ref=trigger.trigger_ref,
        projection_hash=canonical_projection_hash(trigger),
        pinned_world_revision=7,
    )
    values = V2AttentionValues(
        mode="occupied",
        focus_ref=trigger.trigger_id,
        focus_binding=binding,
        allocation_bp=7000,
        interruptibility_bp=4000,
        since=NOW,
        privacy_class="private",
    )
    after = projection(1, values, event_ref="event:attention:trigger", updated_at=NOW)
    with pytest.raises(
        ValueError, match="attention_trigger_focus_authority_not_installed"
    ):
        reduce_v2_attention(
            (),
            (),
            payload(after, cause=cause),
            event_type="V2AttentionChanged",
            event_id="event:attention:trigger",
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
            triggers=(trigger,),
        )


def test_focus_mode_invariants_and_normal_expiry_fail_closed() -> None:
    with pytest.raises(ValueError, match="requires a typed focus"):
        V2AttentionValues(
            mode="occupied", allocation_bp=5000, interruptibility_bp=5000,
            since=NOW, privacy_class="private",
        )
    authority, authority_event, cause = operator_authority()
    after = projection(
        1,
        available(NOW).model_copy(update={"expires_at": NOW}),
        event_ref="event:attention:expired",
        updated_at=NOW,
    )
    with pytest.raises(ValueError, match="expiry must be after"):
        reduce_v2_attention(
            (), (), payload(after, cause=cause),
            event_type="V2AttentionChanged", event_id="event:attention:expired", logical_time=NOW,
            actor_authorities=(authority,), committed_events=(authority_event,),
        )


def test_random_draw_is_wire_visible_but_payload_and_codec_reject_it() -> None:
    _, _, cause = operator_authority()
    after = projection(1, available(NOW), event_ref="event:attention:random", updated_at=NOW)
    raw = payload(after, cause=cause).model_dump(mode="python")
    raw["selection_mode"] = "random_draw"
    raw["random_draw_binding"] = RandomDrawBinding(
        draw_event_ref="event:draw", draw_world_revision=1, draw_payload_hash="e" * 64,
        attempt_id="attempt:draw", candidate_set_hash="f" * 64,
        selected_candidate_ref="available", catalog_version="catalog.1", sampler_version="sampler.1",
    )
    raw["accepted_change_hash"] = v2_attention_mutation_hash(raw)
    with pytest.raises(ValueError, match="random_authority_not_installed"):
        V2AttentionChangedPayload.model_validate(raw)
    encoded = json.dumps(raw, default=str, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError):
        V2_ATTENTION_CODEC.decode_payload("V2AttentionChanged", encoded)


def test_reducer_revalidates_model_copy_lane_random_and_focus_attacks() -> None:
    authority, authority_event, cause = operator_authority()
    after = projection(1, available(NOW), event_ref="event:attention:copy", updated_at=NOW)
    valid = payload(after, cause=cause)
    draw = RandomDrawBinding(
        draw_event_ref="event:draw",
        draw_world_revision=1,
        draw_payload_hash="e" * 64,
        attempt_id="attempt:draw",
        candidate_set_hash="f" * 64,
        selected_candidate_ref="available",
        catalog_version="catalog.1",
        sampler_version="sampler.1",
    )
    bad_values = after.values.model_copy(update={"focus_ref": "plan:ghost"})
    bad_after = after.model_copy(
        update={
            "values": bad_values,
            "semantic_fingerprint": v2_attention_semantic_fingerprint(
                actor_ref=ACTOR,
                values=bad_values,
                policy_refs=after.origin.policy_refs,
            ),
        }
    )
    attacks = (
        valid.model_copy(update={"authority_lane": "deliberative"}),
        valid.model_copy(update={"random_draw_binding": draw}),
        valid.model_copy(update={"attention_after": bad_after}),
    )
    for attack in attacks:
        attack = attack.model_copy(
            update={"accepted_change_hash": v2_attention_mutation_hash(attack)}
        )
        with pytest.raises(ValueError):
            reduce_v2_attention(
                (),
                (),
                attack,
                event_type="V2AttentionChanged",
                event_id="event:attention:copy",
                logical_time=NOW,
                actor_authorities=(authority,),
                committed_events=(authority_event,),
            )

    changed_at = NOW + timedelta(minutes=1)
    changed = projection(
        2,
        after.values.model_copy(update={"mode": "do_not_disturb", "since": changed_at}),
        event_ref="event:attention:copy:2",
        updated_at=changed_at,
    )
    deliberative = intention(changed_at)
    assert isinstance(deliberative.basis, InternalIntentionBasis)
    bad_basis = deliberative.basis.model_copy(
        update={"intention_material_hash": "0" * 64}
    )
    bad_cause = deliberative.model_copy(update={"basis": bad_basis})
    bad_intention = payload(
        changed,
        cause=deliberative,
        before=after,
        operation="change",
        lane="deliberative",
    ).model_copy(update={"cause_authority": bad_cause})
    bad_intention = bad_intention.model_copy(
        update={"accepted_change_hash": v2_attention_mutation_hash(bad_intention)}
    )
    with pytest.raises(ValueError, match="intention material hash"):
        reduce_v2_attention(
            (after,),
            (),
            bad_intention,
            event_type="V2AttentionChanged",
            event_id="event:attention:copy:2",
            logical_time=changed_at,
            actor_authorities=(),
            committed_events=(),
        )


def test_codec_roundtrip_and_proposal_mapping_are_closed() -> None:
    authority, authority_event, cause = operator_authority()
    after = projection(1, available(NOW), event_ref="event:attention:codec", updated_at=NOW)
    mutation = payload(after, cause=cause)
    encoded = V2_ATTENTION_CODEC.encode_payload(mutation)
    assert V2_ATTENTION_CODEC.decode_payload("V2AttentionChanged", encoded) == mutation
    proposed = V2AttentionProposedMutation(event_type="V2AttentionChanged", payload_json=encoded)
    proposal = V2AttentionProposalProjection(
        proposal_id=mutation.proposal_id,
        transition_kind="establish",
        change_id=mutation.change_id,
        transition_id=mutation.transition_id,
        actor_ref=ACTOR,
        evaluated_world_revision=mutation.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=mutation.accepted_change_hash,
        evidence_refs=mutation.evidence_refs,
        policy_refs=mutation.policy_refs,
        proposed_mutation=proposed,
    )
    assert V2_ATTENTION_CODEC.bind(proposal).proposal_kind == "v2_attention_transition"

    cutoff = CommittedWorldEventRef(
        event_id="event:cutoff:7",
        event_type="ClockAdvanced",
        world_revision=7,
        payload_hash="9" * 64,
        logical_time=NOW,
    )
    authority_history = authority_transition(authority, authority_event)
    validate_v2_attention_authority_state(
        (),
        (),
        (proposal,),
        (proposal.proposal_id,),
        global_proposal_ids=(proposal.proposal_id,),
        actor_authority_transitions=(authority_history,),
        committed_events=(authority_event, cutoff),
    )
    ghost = proposal.model_copy(update={"actor_ref": "actor:ghost"})
    with pytest.raises(ValueError):
        validate_v2_attention_authority_state(
            (),
            (),
            (ghost,),
            (ghost.proposal_id,),
            global_proposal_ids=(ghost.proposal_id,),
            actor_authority_transitions=(authority_history,),
            committed_events=(authority_event, cutoff),
        )


def test_replay_state_validator_requires_exact_committed_event_and_head() -> None:
    authority, authority_event, cause = operator_authority()
    after = projection(1, available(NOW), event_ref="event:attention:state", updated_at=NOW)
    heads, history = reduce_v2_attention(
        (), (), payload(after, cause=cause),
        event_type="V2AttentionChanged", event_id="event:attention:state", logical_time=NOW,
        actor_authorities=(authority,), committed_events=(authority_event,),
    )
    mutation_event = CommittedWorldEventRef(
        event_id="event:attention:state",
        event_type="V2AttentionChanged",
        world_revision=8,
        payload_hash="7" * 64,
        logical_time=NOW,
    )
    validate_v2_attention_authority_state(
        heads,
        history,
        (),
        (),
        global_proposal_ids=(),
        actor_authority_transitions=(authority_transition(authority, authority_event),),
        committed_events=(authority_event, mutation_event),
    )
    with pytest.raises(ValueError, match="head is not latest"):
        validate_v2_attention_authority_state(
            (after.model_copy(update={"updated_at": NOW + timedelta(seconds=1)}),),
            history,
            (),
            (),
            global_proposal_ids=(),
            actor_authority_transitions=(authority_transition(authority, authority_event),),
            committed_events=(authority_event, mutation_event),
        )


def test_replay_state_validator_rejects_deliberative_privacy_ghost() -> None:
    authority, authority_event, operator = operator_authority()
    first = projection(
        1, available(NOW), event_ref="event:attention:privacy:1", updated_at=NOW
    )
    heads, history = reduce_v2_attention(
        (),
        (),
        payload(first, cause=operator),
        event_type="V2AttentionChanged",
        event_id="event:attention:privacy:1",
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    changed_at = NOW + timedelta(minutes=1)
    second = projection(
        2,
        first.values.model_copy(
            update={
                "mode": "do_not_disturb",
                "since": changed_at,
                "privacy_class": "withhold",
            }
        ),
        event_ref="event:attention:privacy:2",
        updated_at=changed_at,
    )
    heads, history = reduce_v2_attention(
        heads,
        history,
        payload(
            second,
            before=first,
            cause=intention(changed_at, rationale_privacy="withhold"),
            operation="change",
            lane="deliberative",
        ),
        event_type="V2AttentionChanged",
        event_id="event:attention:privacy:2",
        logical_time=changed_at,
        actor_authorities=(),
        committed_events=(),
    )
    weakened_values = second.values.model_copy(update={"privacy_class": "private"})
    weakened_fingerprint = v2_attention_semantic_fingerprint(
        actor_ref=ACTOR,
        values=weakened_values,
        policy_refs=V2_ATTENTION_POLICY_REFS,
    )
    weakened_transition = history[-1].model_copy(
        update={
            "values_after": weakened_values,
            "semantic_fingerprint_after": weakened_fingerprint,
        }
    )
    weakened_head = heads[-1].model_copy(
        update={
            "values": weakened_values,
            "semantic_fingerprint": weakened_fingerprint,
        }
    )
    events = (
        CommittedWorldEventRef(
            event_id="event:attention:privacy:1",
            event_type="V2AttentionChanged",
            world_revision=8,
            payload_hash="1" * 64,
            logical_time=NOW,
        ),
        CommittedWorldEventRef(
            event_id="event:attention:privacy:2",
            event_type="V2AttentionChanged",
            world_revision=9,
            payload_hash="2" * 64,
            logical_time=changed_at,
        ),
    )
    with pytest.raises(ValueError, match="deliberative history weakens privacy"):
        validate_v2_attention_authority_state(
            (weakened_head,),
            (*history[:-1], weakened_transition),
            (),
            (),
            global_proposal_ids=(),
            actor_authority_transitions=(authority_transition(authority, authority_event),),
            committed_events=(authority_event, *events),
        )


def test_operator_compensation_targets_exact_latest_and_restores_privacy_max() -> None:
    authority, authority_event, operator = operator_authority()
    before = projection(1, available(NOW), event_ref="event:attention:1", updated_at=NOW)
    changed_at = NOW + timedelta(minutes=1)
    changed = projection(
        2,
        before.values.model_copy(update={"mode": "do_not_disturb", "since": changed_at}),
        event_ref="event:attention:2",
        updated_at=changed_at,
    )
    _, history = reduce_v2_attention(
        (before,), (), payload(changed, before=before, cause=operator, operation="change"),
        event_type="V2AttentionChanged", event_id="event:attention:2", logical_time=changed_at,
        actor_authorities=(authority,), committed_events=(authority_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id="event:attention:2", event_type="V2AttentionChanged", world_revision=8,
        payload_hash="8" * 64, logical_time=changed_at,
    )
    compensated_at = changed_at
    restored_values = before.values.model_copy(update={"privacy_class": "withhold"})
    restored = projection(
        3, restored_values, event_ref="event:attention:3", updated_at=compensated_at
    )
    correction = AttentionCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        expected_target_lane="operator",
        correction_basis=AttentionOperatorCorrectionBasis(
            correction_class="attention_assignment_error", privacy_class="withhold"
        ),
        correction_rationale=AttentionCorrectionRationale(
            text="The assigned mode was wrong.", privacy_class="private"
        ),
        operator_authority=operator,
    )
    heads, new_history = reduce_v2_attention(
        (changed,), history,
        payload(restored, before=changed, cause=correction, operation="compensate", lane="compensation", evaluated_world_revision=9),
        event_type="V2AttentionTransitionCompensated", event_id="event:attention:3",
        logical_time=compensated_at, actor_authorities=(authority,),
        committed_events=(authority_event, target_event),
    )
    assert heads[0].values == restored_values
    assert new_history[-1].compensates_transition_id == history[-1].transition_id


def test_expiry_due_is_read_only_exact_and_one_per_attention_revision() -> None:
    expires = NOW + timedelta(minutes=5)
    current = projection(
        4,
        available(NOW).model_copy(update={"expires_at": expires}),
        event_ref="event:attention:4",
        updated_at=NOW,
    )
    clock = ClockTransitionProjection(
        clock_event_ref="event:clock:due",
        computed_world_revision=12,
        payload_hash="1" * 64,
        logical_time_from=NOW,
        logical_time_to=expires,
        installed_policy_version=CLOCK_AUTHORITY_POLICY_VERSION,
        installed_policy_digest=CLOCK_AUTHORITY_POLICY_DIGEST,
    )
    target = attention_expiry_target_identity(
        world_id="world:test", actor_ref=ACTOR, attention_entity_revision=4
    )
    idempotency = digest(
        {
            "world_id": "world:test",
            "event_type": "TriggerProcessOpened",
            "operation": "open_attention_expiry_due",
            "target_identity": target,
            "before_revision": 4,
            "clock_event_ref": clock.clock_event_ref,
            "policy_digest": ATTENTION_EXPIRY_POLICY_DIGEST,
        }
    )
    due = V2AttentionExpiryDuePayload(
        world_id="world:test",
        trigger_id=target,
        binding=AttentionExpiryDueBinding(
            actor_ref=ACTOR,
            attention_entity_revision=4,
            attention_semantic_fingerprint=current.semantic_fingerprint,
            expires_at=expires,
            clock_event_ref=clock.clock_event_ref,
            clock_world_revision=clock.computed_world_revision,
            clock_payload_hash=clock.payload_hash,
            logical_time_from=clock.logical_time_from,
            logical_time_to=clock.logical_time_to,
            clock_policy_version=clock.installed_policy_version,
            clock_policy_digest=clock.installed_policy_digest,
            expiry_policy_version=ATTENTION_EXPIRY_POLICY_VERSION,
            expiry_policy_digest=ATTENTION_EXPIRY_POLICY_DIGEST,
            idempotency_key=idempotency,
            target_identity=target,
        ),
    )
    snapshot = current.model_dump(mode="json")
    assert validate_attention_expiry_due(
        due, current=current, clock_transition_history=(clock,), current_logical_time=expires
    ) == target
    assert current.model_dump(mode="json") == snapshot
    with pytest.raises(ValueError, match="already owns"):
        validate_attention_expiry_due(
            due, current=current, clock_transition_history=(clock,), current_logical_time=expires,
            occupied_target_identities=(target,),
        )


def test_queries_do_not_implicitly_expire_attention() -> None:
    past = NOW - timedelta(minutes=1)
    committed = projection(
        1,
        available(NOW - timedelta(hours=1)).model_copy(update={"expires_at": past}),
        event_ref="event:attention:old",
        updated_at=NOW - timedelta(hours=1),
    )
    assert committed.values.mode == "available"
    assert committed.values.expires_at == past
