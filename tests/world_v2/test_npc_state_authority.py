from __future__ import annotations

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_content_events import LifeContentRecordedPayload
from companion_daemon.world_v2.life_events import NpcStateChangedPayload
from companion_daemon.world_v2.schemas import NpcSocialVariables, NpcSubjectiveState
from companion_daemon.world_v2.schemas import ProjectionCursor
from test_life_projection import (
    WORLD_ID,
    commit,
    event,
    seed_through_proposal,
    settlement_batch,
    world_evidence,
)


def _content_event(*, change, kind: str, ref: str, content_hash: str, revision: int):
    state_change = NpcStateChangedPayload.model_validate_json(change.payload_json)
    return event(
        "event:descriptor:" + ref,
        "LifeContentRecorded",
        LifeContentRecordedPayload(
            content_id="descriptor:" + ref,
            content_kind=kind,
            content_ref=ref,
            content_payload_hash=content_hash,
            privacy_class=state_change.npc_after.privacy_class,
            source_kind="npc_state",
            source_event_ref=change.event_id,
            source_world_revision=revision,
            source_payload_hash=change.payload_hash,
            source_entity_id=state_change.npc_after.npc_id,
            source_entity_revision=state_change.npc_after.entity_revision,
        ).model_dump(mode="json"),
    )


def test_npc_owned_state_is_replayable_directional_and_source_bound() -> None:
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=AcceptedLedgerBatchIssuer(),
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    before = ledger.project().npcs[0]
    state = NpcSubjectiveState(
        subject_ref="actor:companion",
        inner_state_content_ref="content:npc-state:lin:1",
        inner_state_payload_hash="a" * 64,
        relationship_to_subject=NpcSocialVariables(
            trust_bp=5_600,
            closeness_bp=4_200,
            tension_bp=900,
        ),
        goal_content_refs=("content:npc-goal:lin:portfolio",),
        goal_content_hashes=("b" * 64,),
        organization_refs=("organization:internship-team",),
        life_arc_refs=("life-arc:npc:lin:internship",),
        source_event_refs=("occurrence-settled",),
        evolved_at=ledger.project().logical_time,
    )
    after = before.model_copy(
        update={"entity_revision": before.entity_revision + 1, "subjective_state": state}
    )
    payload = NpcStateChangedPayload(
        change_id="change:npc:lin:state:1",
        transition_id="transition:npc:lin:state:1",
        expected_entity_revision=before.entity_revision,
        evidence_refs=(
            world_evidence(ledger, "occurrence-settled", "private_hypothesis"),
        ),
        policy_refs=("policy:npc-ecology.1",),
        npc_before=before,
        npc_after=after,
    )
    change = event(
        "event:npc:lin:state:1",
        "NpcStateChanged",
        payload.model_dump(mode="json"),
    ).model_copy(update={"actor": "npc:lin"})

    commit(
        ledger,
        (
            change,
            _content_event(
                change=change,
                kind="npc_inner_state",
                ref=state.inner_state_content_ref,
                content_hash=state.inner_state_payload_hash,
                revision=ledger.project().world_revision + 1,
            ),
            _content_event(
                change=change,
                kind="npc_goal",
                ref=state.goal_content_refs[0],
                content_hash=state.goal_content_hashes[0],
                revision=ledger.project().world_revision + 1,
            ),
        ),
    )

    projected = ledger.project().npcs[0]
    assert projected.subjective_state == state
    assert projected.subjective_state.relationship_to_subject.closeness_bp == 4_200
    cursor = ProjectionCursor(
        world_revision=ledger.project().world_revision,
        deliberation_revision=ledger.project().deliberation_revision,
        ledger_sequence=ledger.project().ledger_sequence,
    )
    assert ledger.project_at(cursor).semantic_hash == ledger.project().semantic_hash


def test_npc_state_cannot_be_authored_by_the_host() -> None:
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=AcceptedLedgerBatchIssuer(),
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    before = ledger.project().npcs[0]
    after = before.model_copy(
        update={
            "entity_revision": 2,
            "subjective_state": NpcSubjectiveState(
                subject_ref="actor:companion",
                inner_state_content_ref="content:npc-state:lin:1",
                inner_state_payload_hash="a" * 64,
                source_event_refs=("occurrence-settled",),
                evolved_at=ledger.project().logical_time,
            ),
        }
    )
    payload = NpcStateChangedPayload(
        change_id="change:npc:lin:state:1",
        transition_id="transition:npc:lin:state:1",
        expected_entity_revision=1,
        evidence_refs=(
            world_evidence(ledger, "occurrence-settled", "private_hypothesis"),
        ),
        npc_before=before,
        npc_after=after,
    )
    change = event(
        "event:npc:lin:state:1", "NpcStateChanged", payload.model_dump(mode="json")
    )

    try:
        commit(
            ledger,
            (
                change,
                _content_event(
                    change=change,
                    kind="npc_inner_state",
                    ref=after.subjective_state.inner_state_content_ref,
                    content_hash=after.subjective_state.inner_state_payload_hash,
                    revision=ledger.project().world_revision + 1,
                ),
            ),
        )
    except ValueError as exc:
        assert "authored by that NPC actor" in str(exc)
    else:
        raise AssertionError("host-authored NPC inner state was accepted")
