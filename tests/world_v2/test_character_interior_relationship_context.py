from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.character_interior import InteriorOpportunity
from companion_daemon.world_v2.character_interior.production import (
    _LedgerCapsuleInteriorProjection,
)
from companion_daemon.world_v2.character_interior.relationship_context import (
    build_relationship_context_join,
    relationship_transition_subject_refs,
)
from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    SNAPSHOT_COMPILER_VERSION,
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipCommitmentAcceptedPayload,
    RelationshipSlowVariableAdjustedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from companion_daemon.world_v2.relationship_adjustment_compiler import (
    RelationshipAdjustmentCompiler,
)
from companion_daemon.world_v2.relationship_adjustment_trigger_runtime import (
    RelationshipAdjustmentTriggerRuntime,
)
from companion_daemon.world_v2.relationship_adjustment_worker import (
    RelationshipAdjustmentWorker,
)
from companion_daemon.world_v2.relationship_reducers import relationship_primary_id
from companion_daemon.world_v2.schemas import (
    CommitResult,
    CommittedWorldEventRef,
    EvidenceRef,
    LedgerProjection,
    NpcProjection,
    PlanAuthorityOrigin,
    PlanStateProjection,
    ProjectionCursor,
    RelationshipHysteresisProjection,
    RelationshipCommitmentDeliveryProof,
    RelationshipCommitmentOrigin,
    RelationshipCommitmentProjection,
    RelationshipStateOrigin,
    RelationshipStateProjection,
    RelationshipVariableDeltas,
    RelationshipVariablesProjection,
    WorldEvent,
    plan_authority_binding_hash,
    plan_authority_projection_hash,
)
from test_life_projection import (
    WORLD_ID as LIFE_WORLD_ID,
    commit as commit_life,
    seed_through_proposal,
    settlement_batch,
)
from test_character_interior_world_stimulus import (
    _RoleModel as _WorldStimulusRoleModel,
    _runtime_for_ledger as _world_stimulus_runtime_for_ledger,
    _seed_relationship_state,
)


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
WORLD_ID = "world:relationship-context"
ACTOR_REF = "actor:companion"
RELATIONSHIP_EVENT_REF = "event:relationship:npc:lin"


def _context_with_directional_relationships() -> dict[str, object]:
    return {
        "world_id": "world:relationship-context",
        "actor_ref": "actor:companion",
        "trigger_ref": "event:stimulus",
        "world_revision": 12,
        "deliberation_revision": 4,
        "ledger_sequence": 19,
        "logical_time": "2026-08-04T18:00:00+00:00",
        "consumer_scope": "deliberation_internal",
        "viewer_privacy_ceiling": "private",
        "slices": {
            "protagonist_npc_relationships": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "interior:relationship:protagonist:npc:lin",
                        "privacy_class": "private",
                        "value": {
                            "relationship_id": "relationship:npc:lin",
                            "direction": "protagonist_to_npc",
                            "subject_ref": "npc:lin",
                            "stage": "friend",
                            "variables": {
                                "trust_bp": 7100,
                                "closeness_bp": 6400,
                                "respect_bp": 7600,
                                "reliability_bp": 5900,
                                "mutuality_bp": 6200,
                                "repair_confidence_bp": 3300,
                            },
                            "temperature": "warm",
                            "hysteresis": {},
                            "commitment_refs": ["commitment:coffee"],
                            "last_adjusted_at": "2026-08-04T17:00:00+00:00",
                        },
                    }
                ],
            },
            "npc_observable_attitudes": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "interior:relationship:npc:lin:plan:walk",
                        "privacy_class": "shareable",
                        "value": {
                            "direction": "npc_to_protagonist",
                            "npc_ref": "npc:lin",
                            "toward_actor_ref": "actor:companion",
                            "epistemic_scope": "observable_action_only",
                            "observable_act": {
                                "plan_ref": "plan:walk",
                                "activity_kind": "walk_together",
                                "status": "active",
                                "participant_refs": [
                                    "actor:companion",
                                    "npc:lin",
                                ],
                                "location_ref": "place:riverside",
                                "observed_at": "2026-08-04T17:30:00+00:00",
                            },
                            # These simulate data a careless join could leak.
                            "inner_state": "secret",
                            "affect": {"envy": 9000},
                            "private_goal": "never expose me",
                        },
                    }
                ],
            },
        },
    }


def test_canonical_snapshot_keeps_both_relationship_directions_without_npc_private_state() -> None:
    snapshot = compile_inner_life_snapshot(
        _context_with_directional_relationships()
    ).model_view()

    assert SNAPSHOT_COMPILER_VERSION == "inner-life-snapshot-compiler.7"
    assert snapshot["materials"]["protagonist_npc_relationships"][0] == {
        "relationship_id": "relationship:npc:lin",
        "direction": "protagonist_to_npc",
        "subject_ref": "npc:lin",
        "stage": "friend",
        "variables": {
            "trust_bp": 7100,
            "closeness_bp": 6400,
            "respect_bp": 7600,
            "reliability_bp": 5900,
            "mutuality_bp": 6200,
            "repair_confidence_bp": 3300,
        },
        "temperature": "warm",
        "hysteresis": {},
        "commitment_refs": ["commitment:coffee"],
        "last_adjusted_at": "2026-08-04T17:00:00+00:00",
        "source_ref": "interior:relationship:protagonist:npc:lin",
    }
    assert snapshot["materials"]["npc_observable_attitudes"][0] == {
        "direction": "npc_to_protagonist",
        "npc_ref": "npc:lin",
        "toward_actor_ref": "actor:companion",
        "epistemic_scope": "observable_action_only",
        "observable_act": {
            "plan_ref": "plan:walk",
            "activity_kind": "walk_together",
            "status": "active",
            "participant_refs": ["actor:companion", "npc:lin"],
            "location_ref": "place:riverside",
            "observed_at": "2026-08-04T17:30:00+00:00",
        },
        "source_ref": "interior:relationship:npc:lin:plan:walk",
    }
    assert snapshot["faculties"]["subjective_relationship"]["material_keys"] == [
        "protagonist_npc_relationships",
        "npc_observable_attitudes",
    ]
    serialized = json.dumps(snapshot["materials"], ensure_ascii=False)
    assert "inner_state" not in serialized
    assert "affect" not in serialized
    assert "private_goal" not in serialized


def _relationship_event_and_projection() -> tuple[WorldEvent, LedgerProjection]:
    before = RelationshipVariablesProjection()
    after = before.model_copy(update={"trust_bp": 120, "closeness_bp": 80})
    deltas = RelationshipVariableDeltas(trust_bp=120, closeness_bp=80)
    raw: dict[str, object] = {
        "change_id": "change:relationship:npc:lin",
        "transition_id": "transition:relationship:npc:lin",
        "expected_entity_revision": 0,
        "evidence_refs": (
            EvidenceRef(
                ref_id="event:occurrence:lin",
                evidence_type="committed_world_event",
                claim_purpose="private_hypothesis",
                source_world_revision=1,
                immutable_hash="b" * 64,
            ),
        ),
        "policy_refs": ("policy:relationship-v1",),
        "acceptance_id": "acceptance:relationship:npc:lin",
        "proposal_id": "proposal:relationship:npc:lin",
        "evaluated_world_revision": 1,
        "accepted_change_hash": "0" * 64,
        "relationship_id": "relationship:npc:lin",
        "subject_ref": "npc:lin",
        "adjustment_id": "adjustment:relationship:npc:lin",
        "operation": "adjust",
        "signal_refs": ("signal:relationship:npc:lin",),
        "proposed_deltas": deltas,
        "accepted_deltas": deltas,
        "variables_before": before,
        "variables_after": after,
        "stage_before": "stranger",
        "stage_after": "stranger",
        "hysteresis_before": RelationshipHysteresisProjection(),
        "hysteresis_after": RelationshipHysteresisProjection(),
        "commitment_refs": (),
        "confidence_bp": 7600,
        "persistence": "durable",
        "contradiction_group_ref": None,
        "rationale_code": "shared_experience",
        "policy_version": "relationship-policy.1",
        "policy_digest": "a" * 64,
        "adjusted_at": NOW,
        "compensates_adjustment_id": None,
    }
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    payload = RelationshipSlowVariableAdjustedPayload.model_validate(raw)
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=RELATIONSHIP_EVENT_REF,
        world_id=WORLD_ID,
        event_type="RelationshipSlowVariableAdjusted",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:relationship",
        source="test",
        trace_id="trace:relationship:npc:lin",
        causation_id="event:occurrence:lin",
        correlation_id="correlation:relationship:npc:lin",
        idempotency_key="idempotency:relationship:npc:lin",
        payload=payload.model_dump(mode="json"),
    )
    state = RelationshipStateProjection(
        relationship_id=payload.relationship_id,
        subject_ref=payload.subject_ref,
        entity_revision=1,
        stage=payload.stage_after,
        variables=payload.variables_after,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        hysteresis=payload.hysteresis_after,
        commitment_refs=payload.commitment_refs,
        last_adjusted_at=payload.adjusted_at,
        origin=RelationshipStateOrigin(
            change_id=payload.change_id,
            transition_id=payload.transition_id,
            policy_refs=payload.policy_refs,
            accepted_event_ref=RELATIONSHIP_EVENT_REF,
        ),
    )
    projection = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=1,
        logical_time=NOW,
        npcs=(
            NpcProjection(
                npc_id="lin",
                entity_revision=1,
                stable_identity_ref="identity:npc:lin",
                privacy_class="shareable",
            ),
        ),
        relationship_states=(state,),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=event.event_id,
                event_type=event.event_type,
                world_revision=2,
                payload_hash=event.payload_hash,
                logical_time=NOW,
            ),
        ),
        semantic_hash="c" * 64,
    )
    return event, projection


def _commitment_event_and_projection() -> tuple[WorldEvent, LedgerProjection]:
    subject_ref = "npc:lin"
    relationship_id = relationship_primary_id(subject_ref=subject_ref)
    event_ref = "event:relationship-commitment:npc:lin"
    evidence = (
        EvidenceRef(
            ref_id="event:observation:friendship",
            evidence_type="committed_world_event",
            claim_purpose="private_hypothesis",
            source_world_revision=1,
            immutable_hash="b" * 64,
        ),
    )
    policy_refs = ("policy:relationship-v1",)
    commitment = RelationshipCommitmentProjection(
        commitment_id="relationship-commitment:npc:lin:friend",
        relationship_id=relationship_id,
        subject_ref=subject_ref,
        stage_before="stranger",
        committed_stage="friend",
        commitment_code="mutual_friendship",
        visible_text_span="你是我朋友了",
        delivery_proof=RelationshipCommitmentDeliveryProof(
            expression_proposal_id="proposal:expression:friendship",
            expression_acceptance_id="acceptance:expression:friendship",
            expression_plan_id="plan:expression:friendship",
            plan_event_ref="event:expression-plan:friendship",
            plan_event_payload_hash="1" * 64,
            expression_beat_id="beat:expression:friendship",
            beat_event_ref="event:expression-beat:friendship",
            beat_event_payload_hash="2" * 64,
            message_payload_ref="payload:expression:friendship",
            message_payload_hash="sha256:" + "3" * 64,
            stored_payload_event_ref="event:message-payload:friendship",
            stored_payload_event_hash="4" * 64,
            action_id="action:expression:friendship",
            action_target_ref=subject_ref,
            action_event_ref="event:action:expression:friendship",
            action_event_payload_hash="5" * 64,
            receipt_id="receipt:expression:friendship",
            receipt_event_ref="event:receipt:expression:friendship",
            receipt_event_payload_hash="6" * 64,
            receipt_world_revision=1,
        ),
        evidence_refs=evidence,
        origin=RelationshipCommitmentOrigin(
            change_id="change:relationship-commitment:npc:lin",
            transition_id="transition:relationship-commitment:npc:lin",
            policy_refs=policy_refs,
            accepted_event_ref=event_ref,
        ),
        committed_at=NOW,
    )
    raw: dict[str, object] = {
        "change_id": commitment.origin.change_id,
        "transition_id": commitment.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": evidence,
        "policy_refs": policy_refs,
        "acceptance_id": "acceptance:relationship-commitment:npc:lin",
        "proposal_id": "proposal:relationship-commitment:npc:lin",
        "evaluated_world_revision": 1,
        "accepted_change_hash": "0" * 64,
        "relationship_id": relationship_id,
        "subject_ref": subject_ref,
        "stage_before": "stranger",
        "stage_after": "friend",
        "hysteresis_before": RelationshipHysteresisProjection(),
        "hysteresis_after": RelationshipHysteresisProjection(),
        "commitment_refs_before": (),
        "commitment": commitment,
        "policy_version": "relationship-policy.1",
        "policy_digest": "a" * 64,
    }
    raw["accepted_change_hash"] = relationship_mutation_hash(raw)
    payload = RelationshipCommitmentAcceptedPayload.model_validate(raw)
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_ref,
        world_id=WORLD_ID,
        event_type="RelationshipCommitmentAccepted",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:relationship-commitment",
        source="test",
        trace_id="trace:relationship-commitment:npc:lin",
        causation_id="event:relationship-commitment-acceptance:npc:lin",
        correlation_id="correlation:relationship:npc:lin",
        idempotency_key="idempotency:relationship-commitment:npc:lin",
        payload=payload.model_dump(mode="json"),
    )
    state = RelationshipStateProjection(
        relationship_id=relationship_id,
        subject_ref=subject_ref,
        entity_revision=1,
        stage="friend",
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        hysteresis=payload.hysteresis_after,
        commitment_refs=(commitment.commitment_id,),
        origin=RelationshipStateOrigin(
            change_id=payload.change_id,
            transition_id=payload.transition_id,
            policy_refs=payload.policy_refs,
            accepted_event_ref=event_ref,
        ),
    )
    projection = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=1,
        logical_time=NOW,
        npcs=(
            NpcProjection(
                npc_id="lin",
                entity_revision=1,
                stable_identity_ref="identity:npc:lin",
                privacy_class="shareable",
            ),
        ),
        relationship_commitments=(commitment,),
        relationship_states=(state,),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=event.event_id,
                event_type=event.event_type,
                world_revision=2,
                payload_hash=event.payload_hash,
                logical_time=NOW,
            ),
        ),
        semantic_hash="c" * 64,
    )
    return event, projection


@pytest.mark.asyncio
async def test_relationship_context_accepts_commitment_as_exact_head_source() -> None:
    event, projection = _commitment_event_and_projection()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )

    join = await build_relationship_context_join(
        ledger=_Ledger(event, projection),
        projection=projection,
        actor_ref=ACTOR_REF,
        cursor=cursor,
    )

    assert len(join.protagonist_npc_items) == 1
    value = join.protagonist_npc_items[0]["value"]
    assert value["stage"] == "friend"
    assert value["commitment_refs"] == [
        "relationship-commitment:npc:lin:friend"
    ]
    envelope = next(iter(join.source_envelopes.values()))
    assert envelope["source_bindings"][0]["authority_type"] == (
        "RelationshipCommitmentAccepted"
    )


class _Ledger:
    world_id = WORLD_ID
    blocks_event_loop = False

    def __init__(self, event: WorldEvent, projection: LedgerProjection) -> None:
        self.event = event
        self.projection = projection

    def project_at(self, _cursor: ProjectionCursor) -> LedgerProjection:
        return self.projection

    def lookup_event_commit(self, event_ref: str):  # type: ignore[no-untyped-def]
        if event_ref != self.event.event_id:
            return None
        return self.event, CommitResult(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
            event_ids=(self.event.event_id,),
        )


class _Capsules:
    def compile(self, _query):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            model_content_json=json.dumps(
                {
                    "world_id": WORLD_ID,
                    "actor_ref": ACTOR_REF,
                    "world_revision": 2,
                    "deliberation_revision": 0,
                    "ledger_sequence": 1,
                    "logical_time": NOW.isoformat(),
                    "consumer_scope": "deliberation_internal",
                    "viewer_privacy_ceiling": "private",
                    "context_compiler_version": "context-capsule-compiler:test",
                    "truncation": {},
                    "slices": {},
                },
                sort_keys=True,
            )
        )


@pytest.mark.asyncio
async def test_directional_relationship_join_is_source_closed_and_identical_for_all_purposes() -> None:
    event, projection = _relationship_event_and_projection()
    compiler = _LedgerCapsuleInteriorProjection(
        ledger=_Ledger(event, projection),  # type: ignore[arg-type]
        capsules=_Capsules(),  # type: ignore[arg-type]
        companion_actor_ref=ACTOR_REF,
    )
    snapshots = []
    for purpose in (
        "inbound_cognition",
        "world_stimulus_appraisal",
        "proactive_social",
    ):
        snapshots.append(
            await compiler.project(
                subject=InteriorOpportunity(
                    opportunity_ref=f"opportunity:{purpose}",
                    inner_turn_ref=f"inner-turn:{purpose}",
                    world_id=WORLD_ID,
                    actor_ref=ACTOR_REF,
                    trigger_ref=event.event_id,
                    cursor=ProjectionCursor(
                        world_revision=2,
                        deliberation_revision=0,
                        ledger_sequence=1,
                    ),
                    logical_time=NOW,
                    purpose=purpose,
                    source_refs=(event.event_id,),
                )
            )
        )

    assert len({item.snapshot_hash for item in snapshots}) == 1
    relationship = snapshots[0].materials["protagonist_npc_relationships"][0]
    assert relationship["direction"] == "protagonist_to_npc"
    assert relationship["subject_ref"] == "npc:lin"
    inventory = next(
        item
        for item in snapshots[0].source_inventory
        if item.scope == "protagonist_npc_relationships"
    )
    assert inventory.authority_bindings[0].ref == RELATIONSHIP_EVENT_REF
    assert inventory.authority_bindings[0].immutable_hash == event.payload_hash


@pytest.mark.asyncio
async def test_directional_relationship_join_rejects_a_tampered_projection_head() -> None:
    event, projection = _relationship_event_and_projection()
    forged = projection.relationship_states[0].model_copy(
        update={"variables": RelationshipVariablesProjection(trust_bp=9999)}
    )
    projection = projection.model_copy(update={"relationship_states": (forged,)})
    compiler = _LedgerCapsuleInteriorProjection(
        ledger=_Ledger(event, projection),  # type: ignore[arg-type]
        capsules=_Capsules(),  # type: ignore[arg-type]
        companion_actor_ref=ACTOR_REF,
    )
    subject = InteriorOpportunity(
        opportunity_ref="opportunity:tampered",
        inner_turn_ref="inner-turn:tampered",
        world_id=WORLD_ID,
        actor_ref=ACTOR_REF,
        trigger_ref=event.event_id,
        cursor=ProjectionCursor(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
        ),
        logical_time=NOW,
        purpose="inbound_cognition",
        source_refs=(event.event_id,),
    )

    with pytest.raises(ValueError, match="changed accepted meaning"):
        await compiler.project(subject=subject)


@pytest.mark.asyncio
async def test_reverse_relationship_view_uses_only_a_shareable_committed_npc_action() -> None:
    action_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:npc:lin:walk:started",
        world_id=WORLD_ID,
        event_type="ActivityStarted",
        logical_time=NOW,
        created_at=NOW,
        actor="npc:lin",
        source="test",
        trace_id="trace:npc:lin:walk",
        causation_id="event:npc:lin:choice",
        correlation_id="correlation:npc:lin:walk",
        idempotency_key="idempotency:npc:lin:walk",
        payload={"plan_id": "plan:npc:lin:walk"},
    )
    evidence = EvidenceRef(
        ref_id="event:npc:lin:choice",
        evidence_type="committed_world_event",
        claim_purpose="life_transition",
        source_world_revision=1,
        immutable_hash="d" * 64,
    )
    base = PlanStateProjection(
        plan_id="plan:npc:lin:walk",
        activity_id="activity:npc:lin:walk",
        entity_revision=2,
        activity_kind="walk_together",
        evidence_refs=(evidence,),
        status="active",
        importance_bp=8400,
        participant_refs=("npc:lin", ACTOR_REF),
        location_ref="place:riverside",
        last_transitioned_at=NOW,
        privacy_class="shareable",
        owner_actor_ref="npc:lin",
    )
    projection_hash = plan_authority_projection_hash(base)
    plan = base.model_copy(
        update={
            "authority_origin": PlanAuthorityOrigin(
                transition_id="transition:npc:lin:walk:started",
                accepted_event_type="ActivityStarted",
                accepted_event_ref=action_event.event_id,
                accepted_world_revision=2,
                accepted_payload_hash=action_event.payload_hash,
                accepted_at=NOW,
                authority_projection_hash=projection_hash,
                binding_hash=plan_authority_binding_hash(
                    plan_id=base.plan_id,
                    owner_actor_ref="npc:lin",
                    entity_revision=base.entity_revision,
                    transition_id="transition:npc:lin:walk:started",
                    event_type="ActivityStarted",
                    accepted_event_ref=action_event.event_id,
                    accepted_world_revision=2,
                    accepted_payload_hash=action_event.payload_hash,
                    accepted_at=NOW,
                    projection_hash=projection_hash,
                ),
            )
        }
    )
    projection = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=1,
        logical_time=NOW,
        npcs=(
            NpcProjection(
                npc_id="lin",
                entity_revision=1,
                stable_identity_ref="identity:npc:lin",
                privacy_class="shareable",
            ),
        ),
        plans=(plan,),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=action_event.event_id,
                event_type=action_event.event_type,
                world_revision=2,
                payload_hash=action_event.payload_hash,
                logical_time=NOW,
            ),
        ),
        semantic_hash="e" * 64,
    )
    join = await build_relationship_context_join(
        ledger=_Ledger(action_event, projection),
        projection=projection,
        actor_ref=ACTOR_REF,
        cursor=ProjectionCursor(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
        ),
    )

    assert join.npc_observable_items[0]["value"] == {
        "entity_revision": 2,
        "direction": "npc_to_protagonist",
        "npc_ref": "npc:lin",
        "toward_actor_ref": ACTOR_REF,
        "epistemic_scope": "observable_action_only",
        "observable_act": {
            "plan_ref": "plan:npc:lin:walk",
            "activity_kind": "walk_together",
            "status": "active",
            "participant_refs": ["npc:lin", ACTOR_REF],
            "location_ref": "place:riverside",
            "observed_at": NOW.isoformat(),
        },
        "accepted_event_ref": action_event.event_id,
    }
    serialized = json.dumps(join.npc_observable_items, ensure_ascii=False)
    assert "importance_bp" not in serialized
    assert "goal_ref" not in serialized
    assert "subjective_state" not in serialized

    # An NPC's accepted plan is not observable relationship material merely
    # because it exists. Both protagonist participation and a shareable
    # visibility grant are required at this seam.
    for hidden_plan in (
        plan.model_copy(update={"participant_refs": ("npc:lin",)}),
        plan.model_copy(update={"privacy_class": "personal"}),
    ):
        hidden_projection = projection.model_copy(update={"plans": (hidden_plan,)})
        hidden = await build_relationship_context_join(
            ledger=_Ledger(action_event, hidden_projection),
            projection=hidden_projection,
            actor_ref=ACTOR_REF,
            cursor=ProjectionCursor(
                world_revision=2,
                deliberation_revision=0,
                ledger_sequence=1,
            ),
        )
        assert hidden.npc_observable_items == ()


def test_typed_relationship_seam_offers_only_npc_bound_to_the_exact_settled_stimulus() -> None:
    ledger = WorldLedger.in_memory(world_id=LIFE_WORLD_ID)
    seed_through_proposal(ledger)
    commit_life(ledger, settlement_batch())
    located = ledger.lookup_event_commit("occurrence-settled")
    assert located is not None
    source_event, _commit = located

    assert relationship_transition_subject_refs(
        projection=ledger.project(),
        source_event=source_event,
    ) == ("npc:lin",)


@pytest.mark.asyncio
async def test_npc_typed_signal_creates_a_second_subject_head_beside_the_user_head() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=LIFE_WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit_life(ledger, settlement_batch())
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref="occurrence-settled",
        subject_ref="user:geoff",
    )
    runtime, _ledger, _projection = _world_stimulus_runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=_WorldStimulusRoleModel(
            decision="activate",
            relationship_subject_ref="npc:lin",
        ),
        source_ref="occurrence-settled",
        companion_actor_ref="actor:companion",
        settle_relationship=True,
    )

    authored = await runtime.drain_one()
    assert authored.work_status == "accepted"
    assert any(
        item.subject_ref == "npc:lin"
        and item.signal_code == "她觉得这件事改变了自己对这段关系的感受"
        for item in ledger.project().relationship_signals
    )

    adjusted = await RelationshipAdjustmentTriggerRuntime(
        ledger=ledger,
        worker=RelationshipAdjustmentWorker(
            ledger=ledger,
            compiler=RelationshipAdjustmentCompiler(ledger=ledger),
            acceptance=RelationshipAdjustmentAcceptanceRuntime(
                ledger=ledger,
                batch_issuer=issuer,
            ),
            actor="worker:relationship-adjustment",
        ),
        owner_id="worker:relationship-adjustment",
    ).drain_one()

    assert adjusted.work_status == "accepted"
    assert {item.subject_ref for item in ledger.project().relationship_states} == {
        "user:geoff",
        "npc:lin",
    }
    projection = ledger.project()
    join = await build_relationship_context_join(
        ledger=ledger,
        projection=projection,
        actor_ref="actor:companion",
        cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
    )
    assert [
        item["value"]["subject_ref"] for item in join.protagonist_npc_items
    ] == ["npc:lin"]
