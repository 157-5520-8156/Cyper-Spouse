from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest

from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_V2_POLICY_DIGEST,
)
from companion_daemon.world_v2.goal_situation_schemas import (
    DomainOperatorAuthorityBinding,
    RandomDrawBinding,
)
from companion_daemon.world_v2.location_authority_events import (
    V2LocationChangedPayload,
    v2_location_evidence_refs,
    v2_location_mutation_hash,
)
from companion_daemon.world_v2.location_authority_reducers import (
    V2_LOCATION_POLICY_DIGEST,
    V2_LOCATION_POLICY_REFS,
    V2_LOCATION_POLICY_VERSION,
    reduce_v2_location,
)
from companion_daemon.world_v2.location_authority_schemas import (
    LocationCompensationCauseAuthority,
    LocationCorrectionRationale,
    LocationOperatorCorrectionBasis,
    V2LocationOrigin,
    V2LocationProposalProjection,
    V2LocationProjection,
    V2LocationProposedMutation,
    V2LocationValues,
    v2_location_semantic_fingerprint,
)
from companion_daemon.world_v2.schemas import (
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityValues,
    CommittedWorldEventRef,
)


NOW = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


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
        credential_ref="credential:location-governance",
        allowed_operations=("v2_location_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        status="active",
    )
    event = CommittedWorldEventRef(
        event_id="event:actor-authority:location",
        event_type="ActorAuthorityBootstrapped",
        world_revision=5,
        payload_hash="9" * 64,
        logical_time=NOW - timedelta(hours=2),
    )
    authority = ActorAuthorityProjection(
        authority_id="actor-authority:location",
        entity_revision=1,
        values=values,
        policy_version="actor-authority-policy.2",
        policy_digest=ACTOR_AUTHORITY_V2_POLICY_DIGEST,
        origin=ActorAuthorityOrigin(
            transition_id="transition:actor-authority:location",
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
        required_operation="v2_location_governance",
    )
    return authority, event, binding


def location_projection(
    *,
    revision: int,
    values: V2LocationValues,
    event_ref: str,
    updated_at: datetime = NOW,
) -> V2LocationProjection:
    origin = V2LocationOrigin(
        change_id=f"change:location:{revision}",
        transition_id=f"transition:location:{revision}",
        policy_refs=V2_LOCATION_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return V2LocationProjection(
        actor_ref="actor:companion",
        entity_revision=revision,
        semantic_fingerprint=v2_location_semantic_fingerprint(
            actor_ref="actor:companion", values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
        updated_at=updated_at,
    )


def location_payload(
    after: V2LocationProjection,
    *,
    cause: object,
    before: V2LocationProjection | None = None,
    operation: str = "establish",
    lane: str | None = None,
    evaluated_world_revision: int = 7,
) -> V2LocationChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": (),
        "policy_refs": V2_LOCATION_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane or ("compensation" if operation == "compensate" else "operator"),
        "selection_mode": "direct",
        "random_draw_binding": None,
        "location_before": before,
        "location_after": after,
        "cause_authority": cause,
        "policy_version": V2_LOCATION_POLICY_VERSION,
        "policy_digest": V2_LOCATION_POLICY_DIGEST,
    }
    raw["evidence_refs"] = v2_location_evidence_refs(raw)
    raw["accepted_change_hash"] = v2_location_mutation_hash(raw)
    return V2LocationChangedPayload.model_validate(raw)


def test_operator_can_establish_one_location_head() -> None:
    authority, authority_event, cause = operator_authority()
    after = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            zone_ref="zone:study",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    payload = location_payload(after, cause=cause)

    heads, history = reduce_v2_location(
        (),
        (),
        payload,
        event_type="V2LocationChanged",
        event_id="event:location:1",
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )

    assert heads == (after,)
    assert history[0].operation == "establish"
    assert history[0].values_before is None
    assert history[0].values_after == after.values


def test_operator_location_change_resets_since_only_for_location_identity() -> None:
    authority, authority_event, cause = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            zone_ref="zone:study",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    changed_at = NOW + timedelta(minutes=5)
    moved = location_projection(
        revision=2,
        values=V2LocationValues(
            location_ref="location:library",
            zone_ref="zone:reading-room",
            scene_visibility="public",
            privacy_class="private",
            since=changed_at,
        ),
        event_ref="event:location:2",
        updated_at=changed_at,
    )

    heads, history = reduce_v2_location(
        (before,),
        (),
        location_payload(moved, before=before, cause=cause, operation="change"),
        event_type="V2LocationChanged",
        event_id="event:location:2",
        logical_time=changed_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )

    assert heads == (moved,)
    assert history[0].values_before == before.values
    assert heads[0].values.scene_visibility == "public"
    assert heads[0].values.privacy_class == "private"


def test_same_tick_change_is_allowed_but_establish_cannot_be_compensated_away() -> None:
    authority, authority_event, operator = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:same-tick:1",
    )
    same_tick = location_projection(
        revision=2,
        values=before.values.model_copy(
            update={"location_ref": "location:hallway", "since": NOW}
        ),
        event_ref="event:location:same-tick:2",
    )
    heads, _ = reduce_v2_location(
        (before,),
        (),
        location_payload(same_tick, before=before, cause=operator, operation="change"),
        event_type="V2LocationChanged",
        event_id=same_tick.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    assert heads == (same_tick,)

    _, establish_history = reduce_v2_location(
        (),
        (),
        location_payload(before, cause=operator),
        event_type="V2LocationChanged",
        event_id=before.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    establish_event = CommittedWorldEventRef(
        event_id=before.origin.accepted_event_ref,
        event_type="V2LocationChanged",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=NOW,
    )
    correction = LocationCompensationCauseAuthority(
        target_transition_id=establish_history[-1].transition_id,
        target_entity_revision=establish_history[-1].entity_revision,
        target_accepted_event_ref=establish_event.event_id,
        target_accepted_world_revision=establish_event.world_revision,
        target_accepted_payload_hash=establish_event.payload_hash,
        correction_basis=LocationOperatorCorrectionBasis(
            correction_class="location_assignment_error", privacy_class="private"
        ),
        correction_rationale=LocationCorrectionRationale(
            text="The initial location was entered incorrectly.",
            privacy_class="private",
        ),
        operator_authority=operator,
    )
    with pytest.raises(ValueError, match="exact latest"):
        reduce_v2_location(
            (before,),
            establish_history,
            location_payload(
                same_tick,
                before=before,
                cause=correction,
                operation="compensate",
                evaluated_world_revision=9,
            ),
            event_type="V2LocationChangeCompensated",
            event_id=same_tick.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event, establish_event),
        )


def test_metadata_change_preserves_since_and_rejects_noop_or_privacy_weakening() -> None:
    authority, authority_event, cause = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            zone_ref="zone:study",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    changed_at = NOW + timedelta(minutes=5)
    metadata = location_projection(
        revision=2,
        values=before.values.model_copy(update={"scene_visibility": "shareable"}),
        event_ref="event:location:2",
        updated_at=changed_at,
    )
    heads, _ = reduce_v2_location(
        (before,),
        (),
        location_payload(metadata, before=before, cause=cause, operation="change"),
        event_type="V2LocationChanged",
        event_id="event:location:2",
        logical_time=changed_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    assert heads[0].values.since == NOW

    for values in (
        before.values,
        before.values.model_copy(update={"privacy_class": "public"}),
        before.values.model_copy(update={"since": changed_at}),
    ):
        attacked = location_projection(
            revision=2,
            values=values,
            event_ref="event:location:attack",
            updated_at=changed_at,
        )
        with pytest.raises(ValueError):
            reduce_v2_location(
                (before,),
                (),
                location_payload(attacked, before=before, cause=cause, operation="change"),
                event_type="V2LocationChanged",
                event_id="event:location:attack",
                logical_time=changed_at,
                actor_authorities=(authority,),
                committed_events=(authority_event,),
            )


def test_exact_operator_compensation_restores_prior_values_but_not_old_privacy() -> None:
    authority, authority_event, operator = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            zone_ref="zone:study",
            scene_visibility="private",
            privacy_class="personal",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    changed_at = NOW + timedelta(minutes=5)
    current = location_projection(
        revision=2,
        values=V2LocationValues(
            location_ref="location:library",
            zone_ref="zone:reading-room",
            scene_visibility="public",
            privacy_class="private",
            since=changed_at,
        ),
        event_ref="event:location:2",
        updated_at=changed_at,
    )
    _, history = reduce_v2_location(
        (before,),
        (),
        location_payload(current, before=before, cause=operator, operation="change"),
        event_type="V2LocationChanged",
        event_id="event:location:2",
        logical_time=changed_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id="event:location:2",
        event_type="V2LocationChanged",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=changed_at,
    )
    cause = LocationCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        expected_target_lane="operator",
        correction_basis=LocationOperatorCorrectionBasis(
            correction_class="location_assignment_error", privacy_class="private"
        ),
        correction_rationale=LocationCorrectionRationale(
            text="The operator assigned the wrong current location.",
            privacy_class="private",
        ),
        operator_authority=operator,
    )
    compensated_at = changed_at + timedelta(minutes=1)
    after = location_projection(
        revision=3,
        values=before.values.model_copy(update={"privacy_class": "private"}),
        event_ref="event:location:3",
        updated_at=compensated_at,
    )
    heads, compensated_history = reduce_v2_location(
        (current,),
        history,
        location_payload(
            after,
            before=current,
            cause=cause,
            operation="compensate",
            evaluated_world_revision=9,
        ),
        event_type="V2LocationChangeCompensated",
        event_id="event:location:3",
        logical_time=compensated_at,
        actor_authorities=(authority,),
        committed_events=(authority_event, target_event),
    )

    assert heads[0].values.location_ref == before.values.location_ref
    assert heads[0].values.privacy_class == "private"
    assert compensated_history[-1].compensates_transition_id == history[-1].transition_id

    compensation_event = CommittedWorldEventRef(
        event_id="event:location:3",
        event_type="V2LocationChangeCompensated",
        world_revision=9,
        payload_hash="7" * 64,
        logical_time=compensated_at,
    )
    lineage_cause = LocationCompensationCauseAuthority(
        target_transition_id=compensated_history[-1].transition_id,
        target_entity_revision=compensated_history[-1].entity_revision,
        target_accepted_event_ref=compensation_event.event_id,
        target_accepted_world_revision=compensation_event.world_revision,
        target_accepted_payload_hash=compensation_event.payload_hash,
        expected_target_lane="operator",
        correction_basis=LocationOperatorCorrectionBasis(
            correction_class="location_assignment_error", privacy_class="private"
        ),
        correction_rationale=LocationCorrectionRationale(
            text="The prior correction targeted the wrong transition.",
            privacy_class="private",
        ),
        operator_authority=operator,
    )
    lineage_at = compensated_at + timedelta(minutes=1)
    lineage_after = location_projection(
        revision=4,
        values=current.values,
        event_ref="event:location:4",
        updated_at=lineage_at,
    )
    lineage_heads, lineage_history = reduce_v2_location(
        heads,
        compensated_history,
        location_payload(
            lineage_after,
            before=heads[0],
            cause=lineage_cause,
            operation="compensate",
            evaluated_world_revision=10,
        ),
        event_type="V2LocationChangeCompensated",
        event_id="event:location:4",
        logical_time=lineage_at,
        actor_authorities=(authority,),
        committed_events=(authority_event, target_event, compensation_event),
    )
    assert lineage_heads == (lineage_after,)
    assert lineage_history[-1].compensates_transition_id == compensated_history[-1].transition_id

    with pytest.raises(ValueError, match="exact latest"):
        reduce_v2_location(
            (current,),
            history,
            location_payload(
                after,
                before=current,
                cause=cause,
                operation="compensate",
                evaluated_world_revision=9,
            ),
            event_type="V2LocationChangeCompensated",
            event_id="event:location:3",
            logical_time=compensated_at,
            actor_authorities=(authority,),
            committed_events=(
                authority_event,
                target_event.model_copy(update={"event_type": "FactCommitted"}),
            ),
        )

    with pytest.raises(ValueError, match="exact latest"):
        reduce_v2_location(
            (current,),
            history,
            location_payload(
                after,
                before=current,
                cause=cause,
                operation="compensate",
                evaluated_world_revision=9,
            ),
            event_type="V2LocationChangeCompensated",
            event_id="event:location:3",
            logical_time=compensated_at,
            actor_authorities=(authority,),
            committed_events=(
                authority_event,
                target_event.model_copy(
                    update={"logical_time": changed_at - timedelta(seconds=1)}
                ),
            ),
        )


def test_privacy_only_strengthening_cannot_be_compensated_into_a_noop() -> None:
    authority, authority_event, operator = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="personal",
            since=NOW,
        ),
        event_ref="event:location:privacy-before",
    )
    changed_at = NOW + timedelta(minutes=1)
    current = location_projection(
        revision=2,
        values=before.values.model_copy(update={"privacy_class": "private"}),
        event_ref="event:location:privacy-strengthened",
        updated_at=changed_at,
    )
    _, history = reduce_v2_location(
        (before,),
        (),
        location_payload(current, before=before, cause=operator, operation="change"),
        event_type="V2LocationChanged",
        event_id=current.origin.accepted_event_ref,
        logical_time=changed_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id=current.origin.accepted_event_ref,
        event_type="V2LocationChanged",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=changed_at,
    )
    correction = LocationCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        correction_basis=LocationOperatorCorrectionBasis(
            correction_class="privacy_classification_error",
            privacy_class="private",
        ),
        correction_rationale=LocationCorrectionRationale(
            text="The privacy classification was reviewed after the change.",
            privacy_class="private",
        ),
        operator_authority=operator,
    )
    compensated_at = changed_at + timedelta(minutes=1)
    attacked = location_projection(
        revision=3,
        values=current.values,
        event_ref="event:location:privacy-noop",
        updated_at=compensated_at,
    )
    with pytest.raises(ValueError, match="restore exact prior values"):
        reduce_v2_location(
            (current,),
            history,
            location_payload(
                attacked,
                before=current,
                cause=correction,
                operation="compensate",
                evaluated_world_revision=9,
            ),
            event_type="V2LocationChangeCompensated",
            event_id=attacked.origin.accepted_event_ref,
            logical_time=compensated_at,
            actor_authorities=(authority,),
            committed_events=(authority_event, target_event),
        )

@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("entity_revision", 99),
        ("semantic_fingerprint", "f" * 64),
        ("change_id", "change:location:forged"),
        ("transition_id", "transition:location:forged"),
        ("accepted_event_ref", "event:location:forged"),
        ("policy_refs", ("policy:v2-location-authority:forged",)),
    ),
)
def test_compensation_rejects_same_values_with_different_current_head_identity(
    field: str, value: object
) -> None:
    authority, authority_event, operator = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    changed_at = NOW + timedelta(minutes=5)
    current = location_projection(
        revision=2,
        values=before.values.model_copy(
            update={"location_ref": "location:library", "since": changed_at}
        ),
        event_ref="event:location:2",
        updated_at=changed_at,
    )
    _, history = reduce_v2_location(
        (before,),
        (),
        location_payload(current, before=before, cause=operator, operation="change"),
        event_type="V2LocationChanged",
        event_id="event:location:2",
        logical_time=changed_at,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id="event:location:2",
        event_type="V2LocationChanged",
        world_revision=8,
        payload_hash="8" * 64,
        logical_time=changed_at,
    )
    cause = LocationCompensationCauseAuthority(
        target_transition_id=history[-1].transition_id,
        target_entity_revision=history[-1].entity_revision,
        target_accepted_event_ref=target_event.event_id,
        target_accepted_world_revision=target_event.world_revision,
        target_accepted_payload_hash=target_event.payload_hash,
        expected_target_lane="operator",
        correction_basis=LocationOperatorCorrectionBasis(
            correction_class="location_assignment_error", privacy_class="private"
        ),
        correction_rationale=LocationCorrectionRationale(
            text="The current location came from the wrong operator assignment.",
            privacy_class="private",
        ),
        operator_authority=operator,
    )
    origin_fields = {"change_id", "transition_id", "accepted_event_ref", "policy_refs"}
    if field in origin_fields:
        attacked = current.model_copy(
            update={"origin": current.origin.model_copy(update={field: value})}
        )
    else:
        attacked = current.model_copy(update={field: value})
    compensated_at = changed_at + timedelta(minutes=1)
    after = location_projection(
        revision=attacked.entity_revision + 1,
        values=before.values,
        event_ref="event:location:3",
        updated_at=compensated_at,
    )

    with pytest.raises(ValueError, match="exact latest"):
        reduce_v2_location(
            (attacked,),
            history,
            location_payload(
                after,
                before=attacked,
                cause=cause,
                operation="compensate",
                evaluated_world_revision=9,
            ),
            event_type="V2LocationChangeCompensated",
            event_id="event:location:3",
            logical_time=compensated_at,
            actor_authorities=(authority,),
            committed_events=(authority_event, target_event),
        )


def test_reducer_rejects_tampered_acceptance_hash_even_for_valid_after_image() -> None:
    authority, authority_event, cause = operator_authority()
    after = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            zone_ref=None,
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    payload = location_payload(after, cause=cause).model_copy(
        update={"accepted_change_hash": "f" * 64}
    )

    with pytest.raises(ValueError, match="hash"):
        reduce_v2_location(
            (),
            (),
            payload,
            event_type="V2LocationChanged",
            event_id="event:location:1",
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
        )


def test_random_and_non_operator_movement_lanes_fail_closed() -> None:
    _, _, cause = operator_authority()
    after = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    direct = location_payload(after, cause=cause)
    draw = RandomDrawBinding(
        draw_event_ref="event:draw:1",
        draw_world_revision=6,
        draw_payload_hash="d" * 64,
        attempt_id="attempt:draw:1",
        candidate_set_hash="c" * 64,
        selected_candidate_ref="location:apartment",
        catalog_version="location-candidates.1",
        sampler_version="sampler.1",
    )
    random_raw = direct.model_dump(mode="python")
    random_raw.update(
        selection_mode="random_draw",
        random_draw_binding=draw,
        accepted_change_hash="0" * 64,
    )
    random_raw["accepted_change_hash"] = v2_location_mutation_hash(random_raw)
    random_payload = V2LocationChangedPayload.model_validate(random_raw)
    with pytest.raises(ValueError, match="random_authority_not_installed"):
        reduce_v2_location(
            (),
            (),
            random_payload,
            event_type="V2LocationChanged",
            event_id="event:location:1",
            logical_time=NOW,
            actor_authorities=(),
            committed_events=(),
        )

    for lane in ("deliberative", "settlement"):
        raw = direct.model_dump(mode="python")
        raw.update(authority_lane=lane, accepted_change_hash="0" * 64)
        raw["accepted_change_hash"] = v2_location_mutation_hash(raw)
        with pytest.raises(ValueError):
            V2LocationChangedPayload.model_validate(raw)


def test_actor_authority_capability_and_exact_before_image_are_required() -> None:
    authority, authority_event, cause = operator_authority()
    before = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    changed_at = NOW + timedelta(minutes=5)
    after = location_projection(
        revision=2,
        values=V2LocationValues(
            location_ref="location:library",
            scene_visibility="public",
            privacy_class="private",
            since=changed_at,
        ),
        event_ref="event:location:2",
        updated_at=changed_at,
    )
    payload = location_payload(after, before=before, cause=cause, operation="change")

    with pytest.raises(ValueError, match="ActorAuthority"):
        reduce_v2_location(
            (before,),
            (),
            payload,
            event_type="V2LocationChanged",
            event_id="event:location:2",
            logical_time=changed_at,
            actor_authorities=(),
            committed_events=(authority_event,),
        )

    wrong_operation_values = authority.values.model_copy(
        update={"allowed_operations": ("v2_goal_governance",)}
    )
    with pytest.raises(ValueError, match="ActorAuthority"):
        reduce_v2_location(
            (before,),
            (),
            payload,
            event_type="V2LocationChanged",
            event_id="event:location:2",
            logical_time=changed_at,
            actor_authorities=(authority.model_copy(update={"values": wrong_operation_values}),),
            committed_events=(authority_event,),
        )

    expired_at = authority.values.expires_at
    assert expired_at is not None
    expired_after = location_projection(
        revision=2,
        values=after.values.model_copy(update={"since": expired_at}),
        event_ref="event:location:expired",
        updated_at=expired_at,
    )
    with pytest.raises(ValueError, match="ActorAuthority"):
        reduce_v2_location(
            (before,),
            (),
            location_payload(expired_after, before=before, cause=cause, operation="change"),
            event_type="V2LocationChanged",
            event_id="event:location:expired",
            logical_time=expired_at,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
        )

    wrong_operation_cause = cause.model_copy(
        update={"required_operation": "v2_goal_governance"}
    )
    wrong_operation_payload = location_payload(
        after,
        before=before,
        cause=wrong_operation_cause,
        operation="change",
    )
    with pytest.raises(ValueError, match="ActorAuthority"):
        reduce_v2_location(
            (before,),
            (),
            wrong_operation_payload,
            event_type="V2LocationChanged",
            event_id="event:location:2",
            logical_time=changed_at,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
        )

    actual = before.model_copy(
        update={"values": before.values.model_copy(update={"zone_ref": "zone:bedroom"})}
    )
    with pytest.raises(ValueError, match="stale"):
        reduce_v2_location(
            (actual,),
            (),
            payload,
            event_type="V2LocationChanged",
            event_id="event:location:2",
            logical_time=changed_at,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
        )


@pytest.mark.parametrize("text", (" trailing ", "line\nbreak", "e\u0301"))
def test_location_correction_rationale_rejects_noncanonical_text(text: str) -> None:
    with pytest.raises(ValueError, match="trimmed NFC"):
        LocationCorrectionRationale(text=text, privacy_class="private")


def test_location_proposal_codec_requires_canonical_object_and_exact_event_mapping() -> None:
    _, _, cause = operator_authority()
    after = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    payload = location_payload(after, cause=cause)
    payload_json = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    proposal = V2LocationProposalProjection(
        proposal_id=payload.proposal_id,
        transition_kind="establish",
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=V2LocationProposedMutation(
            event_type="V2LocationChanged", payload_json=payload_json
        ),
    )
    assert proposal.authority_contract_ref == "proposal-contract:v2-location.1"

    with pytest.raises(ValueError):
        V2LocationProposedMutation(
            event_type="V2LocationChanged", payload_json=json.dumps({"z": 1, "a": 2})
        )
    with pytest.raises(ValueError):
        mismatched = proposal.model_copy(
            update={
                "proposed_mutation": V2LocationProposedMutation(
                    event_type="V2LocationChangeCompensated", payload_json=payload_json
                )
            }
        )
        V2LocationProposalProjection.model_validate(mismatched.model_dump(mode="python"))


def test_one_head_per_actor_and_unrelated_actor_state_are_preserved() -> None:
    authority, authority_event, cause = operator_authority()
    existing = location_projection(
        revision=1,
        values=V2LocationValues(
            location_ref="location:apartment",
            scene_visibility="private",
            privacy_class="private",
            since=NOW,
        ),
        event_ref="event:location:1",
    )
    with pytest.raises(ValueError, match="already established"):
        reduce_v2_location(
            (existing,),
            (),
            location_payload(existing, cause=cause),
            event_type="V2LocationChanged",
            event_id="event:location:1",
            logical_time=NOW,
            actor_authorities=(authority,),
            committed_events=(authority_event,),
        )

    other = existing.model_copy(
        update={
            "actor_ref": "actor:other",
            "semantic_fingerprint": v2_location_semantic_fingerprint(
                actor_ref="actor:other",
                values=existing.values,
                policy_refs=existing.origin.policy_refs,
            ),
        }
    )
    established = existing.model_copy(
        update={
            "origin": existing.origin.model_copy(
                update={
                    "change_id": "change:location:companion",
                    "transition_id": "transition:location:companion",
                    "accepted_event_ref": "event:location:companion",
                }
            )
        }
    )
    payload = location_payload(established, cause=cause)
    heads, _ = reduce_v2_location(
        (other,),
        (),
        payload,
        event_type="V2LocationChanged",
        event_id="event:location:companion",
        logical_time=NOW,
        actor_authorities=(authority,),
        committed_events=(authority_event,),
    )
    assert other in heads
    assert heads == tuple(sorted(heads, key=lambda item: item.actor_ref))
