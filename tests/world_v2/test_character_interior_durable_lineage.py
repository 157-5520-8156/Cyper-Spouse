from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.proposal_audit_schemas import (
    ModelResultRecordedPayload,
    RecordedCharacterInteriorTurnLineage,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from companion_daemon.world_v2.character_interior.audit import (
    recorded_character_interior_model_result,
)
from companion_daemon.world_v2.character_interior.run_result import CausalOpportunityIdentity
from companion_daemon.world_v2.character_interior.contracts import (
    InnerDecision,
    _InstantPrivateSelf,
    _InteriorAuthorLineage,
    _PrivateSelfLineage,
)
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.experience_memory_decision import (
    ExperienceMemoryDecisionRecordedPayload,
    canonical_experience_memory_decision_json,
    experience_memory_decision_hash,
    experience_memory_decision_identity,
)


def _lineage() -> RecordedCharacterInteriorTurnLineage:
    snapshot_hash = "a" * 64
    return RecordedCharacterInteriorTurnLineage(
        inner_turn_id="character-inner-turn:sha256:" + "b" * 64,
        purpose="inbound_turn",
        opportunity_ref="inbound-opportunity:sha256:" + "c" * 64,
        snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        snapshot_hash=snapshot_hash,
        capability_ref="inbound-turn-capability:sha256:" + "d" * 64,
        author_model_id="deepseek-chat",
        author_model_version="deepseek-v4-flash",
        author_model_call_id="model-call:inner:1",
        author_request_hash="sha256:" + "e" * 64,
        author_response_hash="sha256:" + "f" * 64,
        author_attempt_ordinal=0,
        private_self_lineage_hash="sha256:" + "1" * 64,
        decision_hash="sha256:" + "2" * 64,
    )


def _audit() -> RecordedModelResultAudit:
    response_hash = "3" * 64
    model_call_id = "model-call:inner:1"
    result_ref = "model-result:" + sha256(
        canonical_json(
            {
                "model_call_id": model_call_id,
                "response_hash": response_hash,
            }
        )
    )
    return RecordedModelResultAudit(
        model_call_id=model_call_id,
        model_result_ref=result_ref,
        attempt_id="attempt:inbound:1",
        route=RecordedModelRoute(
            tier="flash",
            reason_code="ordinary_chat",
            router_version="router:test.1",
        ),
        model_id="deepseek-chat",
        model_version="deepseek-v4-flash",
        request_hash="e" * 64,
        response_hash=response_hash,
        character_interior_lineage=_lineage(),
        status="proposal_validated",
    )


def test_character_interior_lineage_is_durable_in_audit_7_and_round_trips() -> None:
    audit = _audit()
    audit_json = model_audit_json(audit)
    payload = ModelResultRecordedPayload(
        audit_contract="model-result-audit.7",
        model_result_ref=audit.model_result_ref,
        deliberation_result_id="deliberation:inner:1",
        proposal_hash="sha256:" + "4" * 64,
        model_call_id=audit.model_call_id,
        attempt_id=audit.attempt_id,
        capsule_id="5" * 64,
        trigger_ref="observation:inbound:1",
        evaluated_world_revision=7,
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )

    replayed = RecordedModelResultAudit.model_validate_json(payload.audit_json)

    assert payload.audit_contract == "model-result-audit.7"
    assert replayed.character_interior_lineage == _lineage()
    assert (
        json.loads(payload.audit_json)["character_interior_lineage"]["decision_hash"]
        == "sha256:" + "2" * 64
    )


def _causal_lineage(identity: CausalOpportunityIdentity) -> RecordedCharacterInteriorTurnLineage:
    payload = _lineage().model_dump(mode="python")
    payload.update(
        {
            "purpose": identity.purpose,
            "opportunity_ref": identity.opportunity_ref,
            "causal_world_id": identity.world_id,
            "causal_source_refs": identity.source_refs,
            "causal_epoch": identity.epoch,
            "causal_actor_ref": identity.actor_ref,
            "causal_contract_version": identity.contract_version,
        }
    )
    return RecordedCharacterInteriorTurnLineage.model_validate(payload)


def test_causal_lineage_reconstructs_and_binds_the_authoritative_opportunity_ref() -> None:
    identity = CausalOpportunityIdentity.from_source_refs(
        world_id="world:test",
        actor_ref="actor:companion",
        purpose="world_stimulus_appraisal",
        source_refs=("source:a", "source:b"),
        epoch="epoch:1",
    )

    lineage = _causal_lineage(identity)

    assert lineage.opportunity_ref == identity.opportunity_ref
    assert RecordedCharacterInteriorTurnLineage.model_validate(
        lineage.model_dump(mode="python")
    ) == lineage


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("opportunity_ref", "opportunity:causal:forged"),
        ("causal_source_refs", ("source:a", "source:c")),
        ("causal_source_refs", ("source:b", "source:a")),
        ("causal_actor_ref", "actor:other"),
        ("purpose", "other-purpose"),
        ("causal_epoch", "epoch:2"),
        ("causal_contract_version", "causal-opportunity.2"),
    ),
)
def test_causal_lineage_rejects_any_forged_identity_coordinate(field: str, value: object) -> None:
    identity = CausalOpportunityIdentity.from_source_refs(
        world_id="world:test",
        actor_ref="actor:companion",
        purpose="world_stimulus_appraisal",
        source_refs=("source:a", "source:b"),
        epoch="epoch:1",
    )
    payload = _lineage().model_dump(mode="python")
    payload.update(
        {
            "purpose": identity.purpose,
            "opportunity_ref": identity.opportunity_ref,
            "causal_world_id": identity.world_id,
            "causal_source_refs": identity.source_refs,
            "causal_epoch": identity.epoch,
            "causal_actor_ref": identity.actor_ref,
            "causal_contract_version": identity.contract_version,
        }
    )
    payload[field] = value

    with pytest.raises(ValueError, match="causal opportunity"):
        RecordedCharacterInteriorTurnLineage.model_validate(payload)


def test_audit_7_cannot_claim_missing_character_interior_lineage() -> None:
    audit = _audit().model_copy(update={"character_interior_lineage": None})
    audit_json = model_audit_json(audit)

    with pytest.raises(ValueError, match="CharacterInterior lineage"):
        ModelResultRecordedPayload(
            audit_contract="model-result-audit.7",
            model_result_ref=audit.model_result_ref,
            deliberation_result_id="deliberation:inner:missing",
            proposal_hash="sha256:" + "4" * 64,
            model_call_id=audit.model_call_id,
            attempt_id=audit.attempt_id,
            capsule_id="5" * 64,
            trigger_ref="observation:inbound:missing",
            evaluated_world_revision=7,
            attempt_index=0,
            attempt_count=1,
            audit_json=audit_json,
            audit_hash=sha256(audit_json),
        )


def test_canonical_character_interior_model_result_closes_full_lineage() -> None:
    cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=11,
        ledger_sequence=19,
    )
    snapshot_hash = "a" * 64
    author = _InteriorAuthorLineage(
        model_id="deepseek-chat",
        model_version="deepseek-v4-flash",
        model_call_id="model-call:inner:canonical",
        request_hash="sha256:" + "b" * 64,
        response_hash="sha256:" + "c" * 64,
        attempt_ordinal=0,
    )
    private_self = _InstantPrivateSelf(
        summary="她认真权衡了这次机会。",
        attended_source_refs=("event:source:1",),
    )
    private_lineage = _PrivateSelfLineage(
        relation="single_pass",
        initial_private_self=private_self,
        initial_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        initial_snapshot_hash=snapshot_hash,
        initial_author_lineage=author,
        final_private_self=private_self,
        final_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        final_snapshot_hash=snapshot_hash,
        final_author_lineage=author,
    )
    result = InnerDecision(
        inner_turn_id="character-inner-turn:sha256:" + "d" * 64,
        opportunity_ref="opportunity:test:canonical",
        actor_ref="character:zhizhi",
        cursor=cursor,
        snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        snapshot_hash=snapshot_hash,
        status="decided",
        summary=private_self.summary,
        attended_source_refs=private_self.attended_source_refs,
        instant_private_self=private_self,
        private_self_lineage=private_lineage,
        decision={"decision": "select"},
        author_lineage=author,
    )

    payload = recorded_character_interior_model_result(
        result,
        purpose="media_selection",
        subject_ref=result.opportunity_ref,
        trigger_ref="event:source:1",
        capability_ref="capability:media:1",
        route_tier="flash",
        route_reason_code="media_selection.character_choice",
        router_version="character-interior-media-selection.1",
    )
    audit = RecordedModelResultAudit.model_validate_json(payload.audit_json)
    lineage = audit.character_interior_lineage

    assert payload.audit_contract == "model-result-audit.7"
    assert lineage is not None
    assert lineage.inner_turn_id == result.inner_turn_id
    assert lineage.snapshot_id == result.snapshot_id
    assert lineage.snapshot_hash == result.snapshot_hash
    assert lineage.private_self_lineage_hash.startswith("sha256:")
    assert lineage.author_request_hash == author.request_hash
    assert lineage.author_response_hash == author.response_hash


def test_experience_memory_codec_preserves_new_lineage_and_reads_history_without_it() -> None:
    audit = _audit()
    audit_json = model_audit_json(audit)
    model_result = ModelResultRecordedPayload(
        audit_contract="model-result-audit.7",
        model_result_ref=audit.model_result_ref,
        deliberation_result_id="deliberation:experience-memory:1",
        proposal_hash="sha256:" + "4" * 64,
        model_call_id=audit.model_call_id,
        attempt_id=audit.attempt_id,
        capsule_id="5" * 64,
        trigger_ref="event:experience:accepted:1",
        evaluated_world_revision=7,
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )
    authority_ref = "event:experience:accepted:1"
    decision_json = canonical_experience_memory_decision_json(
        {"decision": "no_change"}
    )
    payload = ExperienceMemoryDecisionRecordedPayload(
        decision_id=experience_memory_decision_identity(
            experience_authority_event_ref=authority_ref
        ),
        experience_id="experience:1",
        experience_entity_revision=1,
        experience_authority_event_ref=authority_ref,
        experience_authority_world_revision=6,
        experience_authority_payload_hash="6" * 64,
        evaluated_world_revision=7,
        adapter_version="character-interior-experience-memory-retention.1",
        model_id="deepseek-chat",
        request_hash="7" * 64,
        decision_kind="no_change",
        decision_json=decision_json,
        decision_hash=experience_memory_decision_hash(decision_json),
        recorded_at=datetime(2026, 8, 4, tzinfo=UTC),
        character_interior_model_result=model_result,
    )

    assert payload.character_interior_model_result == model_result
    historical = payload.model_dump(
        mode="json", exclude={"character_interior_model_result"}
    )
    assert (
        ExperienceMemoryDecisionRecordedPayload.model_validate_json(
            json.dumps(historical)
        )
        .character_interior_model_result
        is None
    )
