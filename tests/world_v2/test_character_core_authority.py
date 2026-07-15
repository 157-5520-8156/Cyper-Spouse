from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import strip_v16_state_fields
from nacl.signing import SigningKey
from pydantic import ValidationError

from companion_daemon.world_v2.actor_authority_events import (
    ROOT_KEYSET_DIGEST,
    actor_authority_mutation_hash,
    root_envelope_signature_message,
)
from companion_daemon.world_v2.actor_authority_reducers import (
    ACTOR_AUTHORITY_POLICY_DIGEST,
)
from companion_daemon.world_v2.character_core_events import (
    CharacterCoreChangedPayload,
    CharacterCoreCompensationTarget,
    character_core_evidence_refs,
    character_core_mutation_hash,
)
from companion_daemon.world_v2.character_core_reducers import (
    CHARACTER_CORE_POLICY_DIGEST,
    CHARACTER_CORE_POLICY_REFS,
    CHARACTER_CORE_POLICY_VERSION,
    _reject_evidence_reuse,
    _validate_longitudinal_delta,
    reduce_character_core,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import LedgerIntegrityError
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    CHARACTER_CORE_COORDINATE_CATALOG_DIGEST,
    ActorAuthorityOrigin,
    ActorAuthorityProjection,
    ActorAuthorityValues,
    CharacterCoreAxis,
    CharacterCoreEvidenceBinding,
    CharacterCoreEvidenceWindow,
    CharacterCoreImmutableIdentity,
    CharacterCoreOperatorAuthorityBinding,
    CharacterCoreOperatorGoverned,
    CharacterCoreOrigin,
    CharacterCoreProjection,
    CharacterCoreProposalProjection,
    CharacterCoreProposedMutation,
    CharacterCoreSlowEvolving,
    CharacterCoreTransitionProjection,
    CharacterCoreValuePriority,
    CharacterCoreValues,
    CommittedWorldEventRef,
    DueWindow,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceTransitionProjection,
    ExperienceValues,
    WorldOccurrenceProjection,
    WorldEvent,
    character_core_semantic_fingerprint,
    experience_semantic_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
WORLD = "world-character-core"
ROOT_SIGNING_KEY = SigningKey(bytes.fromhex("11" * 32))


def canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def operator_authority(
    *, allowed: tuple[str, ...] = ("character_core_governance",)
) -> tuple[
    ActorAuthorityProjection,
    CharacterCoreOperatorAuthorityBinding,
    CommittedWorldEventRef,
]:
    values = ActorAuthorityValues(
        principal_ref="operator:deployment",
        principal_kind="deployment_operator",
        credential_ref="credential:operator",
        allowed_operations=allowed,
        valid_from=NOW - timedelta(days=30),
        status="active",
    )
    origin = ActorAuthorityOrigin(
        transition_id="transition:operator",
        event_ref="event:operator",
        root_key_id="deployment-root:production-1",
        root_keyset_version="deployment-root-keyset.1",
        root_keyset_digest="a" * 64,
        root_nonce_hash="b" * 64,
        root_proof_hash="c" * 64,
    )
    authority = ActorAuthorityProjection(
        authority_id="authority:operator",
        entity_revision=1,
        values=values,
        policy_version="actor-authority-policy.1",
        policy_digest=ACTOR_AUTHORITY_POLICY_DIGEST,
        origin=origin,
        updated_at=NOW - timedelta(days=1),
    )
    committed = CommittedWorldEventRef(
        event_id=origin.event_ref,
        event_type="ActorAuthorityBootstrapped",
        world_revision=1,
        payload_hash="d" * 64,
        logical_time=authority.updated_at,
    )
    binding = CharacterCoreOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        principal_ref=values.principal_ref,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        authority_values_hash=canonical_hash(values),
        authority_policy_digest=authority.policy_digest,
        authorization_contract="deployment-actor-authority:character-core.1",
    )
    return authority, binding, committed


def core_values(
    *, curiosity_bp: int = 5000, assertiveness_bp: int = 5000,
    privacy: str = "private",
) -> CharacterCoreValues:
    return CharacterCoreValues(
        immutable_identity=CharacterCoreImmutableIdentity(
            canonical_identity_refs=("identity:companion",),
            continuity_anchor_refs=("continuity:world",),
        ),
        operator_governed=CharacterCoreOperatorGoverned(
            role_refs=("role:virtual-companion",),
            non_negotiable_value_refs=("value:agency",),
            hard_boundary_refs=("boundary:no-coercion",),
        ),
        slow_evolving=CharacterCoreSlowEvolving(
            coordinate_catalog_version="character-core-coordinate-catalog.1",
            coordinate_catalog_digest=CHARACTER_CORE_COORDINATE_CATALOG_DIGEST,
            trait_axes=(
                CharacterCoreAxis(axis_code="assertiveness", value_bp=assertiveness_bp),
                CharacterCoreAxis(axis_code="curiosity", value_bp=curiosity_bp),
            ),
            value_priorities=(
                CharacterCoreValuePriority(value_ref="value:autonomy", priority_bp=7000),
            ),
            preference_refs=("preference:quiet_reflection",),
            autonomy_style="self_directed",
            attachment_tendency="balanced",
            conflict_style="deliberative",
            privacy_tendency="selective",
        ),
        privacy_class=privacy,
    )


def core(
    *, revision: int, values: CharacterCoreValues, event_ref: str, updated_at: datetime
) -> CharacterCoreProjection:
    origin = CharacterCoreOrigin(
        change_id=f"change:core:{revision}",
        transition_id=f"transition:core:{revision}",
        policy_refs=CHARACTER_CORE_POLICY_REFS,
        accepted_event_ref=event_ref,
    )
    return CharacterCoreProjection(
        core_id="core:companion",
        actor_ref="actor:companion",
        entity_revision=revision,
        semantic_fingerprint=character_core_semantic_fingerprint(
            core_id="core:companion",
            actor_ref="actor:companion",
            values=values,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        created_at=NOW if revision == 1 else NOW,
        updated_at=updated_at,
    )


def occurrence_experience(
    *, index: int, occurred_to: datetime, location_ref: str,
    participant_ref: str = "actor:companion",
) -> tuple[
    ExperienceProjection,
    ExperienceTransitionProjection,
    WorldOccurrenceProjection,
    CommittedWorldEventRef,
    CharacterCoreEvidenceBinding,
]:
    occurrence_id = f"occurrence:{index}"
    settlement_ref = f"event:settlement:{index}"
    digest = format(index % 16, "x") * 64
    settlement_digest = format((index + 1) % 16, "x") * 64
    summary_digest = format((index + 2) % 16, "x") * 64
    experience_event_digest = format((index + 3) % 16, "x") * 64
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=2,
        trigger_ref=f"trigger:{index}",
        participant_refs=(participant_ref,),
        location_ref=location_ref,
        time_window=DueWindow(
            opens_at=occurred_to - timedelta(hours=1), closes_at=occurred_to
        ),
        candidate_outcome_refs=(f"outcome:{index}",),
        settled_outcome_ref=f"outcome:{index}",
        visibility="private",
        status="settled",
        result_id=f"result:{index}",
        result_payload_ref=f"payload:result:{index}",
        result_payload_hash=digest,
        settled_at=occurred_to,
        settlement_event_ref=settlement_ref,
        settlement_world_revision=index + 1,
        settlement_payload_hash=settlement_digest,
    )
    values = ExperienceValues(
        summary_ref=f"summary:experience:{index}",
        summary_payload_hash=summary_digest,
        occurred_from=occurred_to - timedelta(hours=1),
        occurred_to=occurred_to,
        participant_refs=(participant_ref,),
        source_bindings=(
            ExperienceOccurrenceSettlementBinding(
                authority_event_ref=settlement_ref,
                authority_world_revision=index + 1,
                authority_payload_hash=settlement_digest,
                occurrence_id=occurrence_id,
                occurrence_entity_revision=2,
                result_id=f"result:{index}",
                result_payload_ref=f"payload:result:{index}",
                result_payload_hash=digest,
            ),
        ),
        privacy_class="private",
    )
    origin = ExperienceOrigin(
        change_id=f"change:experience:{index}",
        transition_id=f"transition:experience:{index}",
        policy_refs=("policy:experience-v1",),
        accepted_event_ref=f"event:experience:{index}",
    )
    experience = ExperienceProjection(
        experience_id=f"experience:{index}",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
    )
    transition = ExperienceTransitionProjection(
        transition_id=origin.transition_id,
        experience_id=experience.experience_id,
        values_after=values,
        semantic_fingerprint_after=experience.semantic_fingerprint,
        change_id=origin.change_id,
        policy_refs=origin.policy_refs,
        accepted_event_ref=origin.accepted_event_ref,
        accepted_at=occurred_to,
    )
    committed = CommittedWorldEventRef(
        event_id=origin.accepted_event_ref,
        event_type="ExperienceCommitted",
        world_revision=index + 10,
        payload_hash=experience_event_digest,
        logical_time=occurred_to,
    )
    source = CharacterCoreEvidenceBinding(
        source_kind="experience",
        source_id=experience.experience_id,
        source_entity_revision=1,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        source_values_hash=canonical_hash(values),
        polarity="supporting",
        scene_ref=(
            f"scene:occurrence:{location_ref}:"
            f"{occurrence.time_window.opens_at.date().isoformat()}"
        ),
        trigger_kind=f"occurrence:{occurrence.trigger_ref}",
        observed_from=values.occurred_from,
        observed_to=values.occurred_to,
    )
    return experience, transition, occurrence, committed, source


def evidence_window(*sources: CharacterCoreEvidenceBinding) -> CharacterCoreEvidenceWindow:
    material = {
        "policy_version": CHARACTER_CORE_POLICY_VERSION,
        "source_bindings": [item.model_dump(mode="json") for item in sources],
    }
    return CharacterCoreEvidenceWindow(
        opens_at=min(item.observed_from for item in sources),
        closes_at=max(item.observed_to for item in sources),
        source_bindings=tuple(sources),
        distinct_scene_refs=tuple(sorted({item.scene_ref for item in sources})),
        distinct_trigger_kinds=tuple(sorted({item.trigger_kind for item in sources})),
        supporting_count=sum(item.polarity == "supporting" for item in sources),
        contradicting_count=sum(item.polarity == "contradicting" for item in sources),
        privacy_floor="private",
        policy_version=CHARACTER_CORE_POLICY_VERSION,
        evidence_digest=hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def mutation(
    after: CharacterCoreProjection,
    *,
    operation: str,
    lane: str,
    changed: tuple[str, ...],
    before: CharacterCoreProjection | None = None,
    window: CharacterCoreEvidenceWindow | None = None,
    operator: CharacterCoreOperatorAuthorityBinding | None = None,
    compensation_target: CharacterCoreCompensationTarget | None = None,
    evaluated_world_revision: int = 1,
) -> CharacterCoreChangedPayload:
    raw = {
        "change_id": after.origin.change_id,
        "transition_id": after.origin.transition_id,
        "expected_entity_revision": before.entity_revision if before else 0,
        "evidence_refs": (),
        "policy_refs": CHARACTER_CORE_POLICY_REFS,
        "acceptance_id": f"acceptance:{after.origin.transition_id}",
        "proposal_id": f"proposal:{after.origin.transition_id}",
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": operation,
        "authority_lane": lane,
        "changed_field_classes": changed,
        "core_before": before,
        "core_after": after,
        "evidence_window": window,
        "operator_authority": operator,
        "compensation_target": compensation_target,
        "policy_version": CHARACTER_CORE_POLICY_VERSION,
        "policy_digest": CHARACTER_CORE_POLICY_DIGEST,
    }
    raw["evidence_refs"] = character_core_evidence_refs(raw)
    raw["accepted_change_hash"] = character_core_mutation_hash(raw)
    return CharacterCoreChangedPayload.model_validate(raw)


def ledger_event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    source: str = "test",
    actor: str = "system:test",
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor=actor,
        source=source,
        trace_id="trace:character-core",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:character-core",
        idempotency_key=(
            domain_idempotency_key(
                event_type=event_type,
                world_id=WORLD,
                payload=payload,
            )
            or f"identity:{event_id}"
        ),
        payload=payload,
    )


def bootstrap_character_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[WorldLedger, CharacterCoreOperatorAuthorityBinding]:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    values = ActorAuthorityValues(
        principal_ref="operator:character-core",
        principal_kind="deployment_operator",
        credential_ref="credential:character-core",
        allowed_operations=("character_core_governance",),
        valid_from=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=365),
        status="active",
    )
    raw: dict[str, object] = {
        "world_id": WORLD,
        "authority_id": "authority:character-core",
        "transition_id": "transition:authority:character-core",
        "operation": "bootstrap",
        "expected_entity_revision": 0,
        "values_before": None,
        "values_after": values.model_dump(mode="json"),
        "policy_version": "actor-authority-policy.1",
        "policy_digest": ACTOR_AUTHORITY_POLICY_DIGEST,
        "changed_at": NOW.isoformat(),
        "compensates_transition_id": None,
        "root_proof": {
            "keyset_version": "deployment-root-keyset.1",
            "keyset_digest": ROOT_KEYSET_DIGEST,
            "root_key_id": "test-only:development-root-1",
            "nonce": "nonce:character-core:bootstrap:1234567890",
            "signed_mutation_hash": "0" * 64,
            "signature_hex": "0" * 128,
        },
    }
    mutation_digest = actor_authority_mutation_hash(raw)
    raw["root_proof"]["signed_mutation_hash"] = mutation_digest  # type: ignore[index]
    identity = domain_idempotency_key(
        event_type="ActorAuthorityBootstrapped", world_id=WORLD, payload=raw
    )
    event_id = "event:authority:character-core"
    actor = "deployment:test-root"
    raw["root_proof"]["signature_hex"] = ROOT_SIGNING_KEY.sign(  # type: ignore[index]
        root_envelope_signature_message(
            schema_version="world-v2.1",
            world_id=WORLD,
            event_type="ActorAuthorityBootstrapped",
            event_id=event_id,
            actor=actor,
            source="deployment-root-ingress",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:character-core",
            causation_id=f"cause:{event_id}",
            correlation_id="correlation:character-core",
            idempotency_key=identity,
            mutation_hash=mutation_digest,
        )
    ).signature.hex()
    root_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type="ActorAuthorityBootstrapped",
        logical_time=NOW,
        created_at=NOW,
        actor=actor,
        source="deployment-root-ingress",
        trace_id="trace:character-core",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:character-core",
        idempotency_key=identity,
        payload=raw,
    )
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        [root_event],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            ledger_event(
                "event:clock:character-core",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
            )
        ],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    projection = ledger.project()
    authority = projection.actor_authorities[0]
    committed = projection.committed_world_event_refs[0]
    return ledger, CharacterCoreOperatorAuthorityBinding(
        authority_id=authority.authority_id,
        authority_revision=authority.entity_revision,
        principal_ref=authority.values.principal_ref,
        authority_event_ref=committed.event_id,
        authority_world_revision=committed.world_revision,
        authority_payload_hash=committed.payload_hash,
        authority_values_hash=canonical_hash(authority.values),
        authority_policy_digest=authority.policy_digest,
        authorization_contract="deployment-actor-authority:character-core.1",
    )


def core_proposal(
    payload: CharacterCoreChangedPayload, event_type: str
) -> CharacterCoreProposalProjection:
    return CharacterCoreProposalProjection(
        proposal_id=payload.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:character-core.1",
        transition_kind=payload.operation,
        change_id=payload.change_id,
        transition_id=payload.transition_id,
        evaluated_world_revision=payload.evaluated_world_revision,
        expected_entity_revision=payload.expected_entity_revision,
        proposed_change_hash=payload.accepted_change_hash,
        evidence_refs=payload.evidence_refs,
        policy_refs=payload.policy_refs,
        proposed_mutation=CharacterCoreProposedMutation(
            event_type=event_type,
            payload_json=json.dumps(
                payload.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def core_acceptance(payload: CharacterCoreChangedPayload) -> dict[str, object]:
    return {
        "acceptance_id": payload.acceptance_id,
        "status": "accepted",
        "proposal_id": payload.proposal_id,
        "evaluated_world_revision": payload.evaluated_world_revision,
        "accepted_change_id": payload.change_id,
        "accepted_change_hash": payload.accepted_change_hash,
    }


def record_core_proposal(
    ledger: WorldLedger,
    payload: CharacterCoreChangedPayload,
    event_type: str,
) -> None:
    projected = ledger.project()
    proposal = core_proposal(payload, event_type)
    ledger.commit(
        [
            ledger_event(
                f"event:{payload.proposal_id}",
                "ProposalRecorded",
                proposal.model_dump(mode="json"),
            )
        ],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def accept_and_record_core(
    ledger: WorldLedger,
    payload: CharacterCoreChangedPayload,
    event_type: str,
) -> None:
    record_core_proposal(ledger, payload, event_type)
    projected = ledger.project()
    ledger.commit(
        [
            ledger_event(
                f"event:{payload.acceptance_id}",
                "AcceptanceRecorded",
                core_acceptance(payload),
            ),
            ledger_event(
                payload.core_after.origin.accepted_event_ref,
                event_type,
                payload.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def initialized_character_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    WorldLedger,
    CharacterCoreOperatorAuthorityBinding,
    CharacterCoreProjection,
]:
    ledger, operator = bootstrap_character_operator(monkeypatch)
    initialized = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:ledger-initialize",
        updated_at=NOW,
    )
    payload = mutation(
        initialized,
        operation="initialize",
        lane="operator_initialize",
        changed=(
            "immutable_identity",
            "operator_governed",
            "privacy_class",
            "slow_evolving",
        ),
        operator=operator,
        evaluated_world_revision=ledger.project().world_revision,
    )
    accept_and_record_core(ledger, payload, "CharacterCoreInitialized")
    return ledger, operator, initialized


def operator_revision_payload(
    ledger: WorldLedger,
    operator: CharacterCoreOperatorAuthorityBinding,
    before: CharacterCoreProjection,
    *,
    curiosity_bp: int,
    event_ref: str,
) -> CharacterCoreChangedPayload:
    revised = core(
        revision=before.entity_revision + 1,
        values=core_values(
            curiosity_bp=curiosity_bp,
            privacy=before.values.privacy_class,
        ),
        event_ref=event_ref,
        updated_at=NOW,
    )
    if revised.origin.transition_id in {
        item.transition_id for item in ledger.project().character_core_transitions
    }:
        revised = revised.model_copy(
            update={
                "origin": revised.origin.model_copy(
                    update={
                        "change_id": f"change:{event_ref}",
                        "transition_id": f"transition:{event_ref}",
                    }
                )
            }
        )
    return mutation(
        revised,
        operation="revise",
        lane="operator_revision",
        changed=("slow_evolving",),
        before=before,
        operator=operator,
        evaluated_world_revision=ledger.project().world_revision,
    )


def test_operator_initialize_is_exact_and_short_term_fields_are_not_schema() -> None:
    authority, operator, committed = operator_authority()
    initialized = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:initialize",
        updated_at=NOW,
    )
    payload = mutation(
        initialized,
        operation="initialize",
        lane="operator_initialize",
        changed=("immutable_identity", "operator_governed", "privacy_class", "slow_evolving"),
        operator=operator,
    )
    head, history = reduce_character_core(
        None,
        (),
        payload,
        event_type="CharacterCoreInitialized",
        event_id=initialized.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        world_occurrences=(),
        committed_events=(committed,),
    )
    assert head == initialized
    assert history[0].authority_lane == "operator_initialize"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        CharacterCoreValues.model_validate(
            {**core_values().model_dump(mode="json"), "mood": "sad"}
        )


def test_ledger_character_core_initialize_is_typed_replayable_and_zero_cascade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, operator = bootstrap_character_operator(monkeypatch)
    baseline = ledger.project()
    initialized = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:ledger-initialize",
        updated_at=NOW,
    )
    payload = mutation(
        initialized,
        operation="initialize",
        lane="operator_initialize",
        changed=(
            "immutable_identity",
            "operator_governed",
            "privacy_class",
            "slow_evolving",
        ),
        operator=operator,
        evaluated_world_revision=baseline.world_revision,
    )

    accept_and_record_core(ledger, payload, "CharacterCoreInitialized")

    projected = ledger.project()
    assert projected.character_core == initialized
    assert projected.character_core_transitions[-1].operation == "initialize"
    assert projected.character_core_proposals == ()
    assert ledger.rebuild() == projected
    for field in (
        "facts",
        "fact_transitions",
        "experiences",
        "experience_transitions",
        "memory_candidates",
        "memory_candidate_transitions",
        "threads",
        "thread_transitions",
        "actions",
        "affect_episodes",
        "relationship_states",
        "commitments",
    ):
        assert getattr(projected, field) == getattr(baseline, field), field


def test_ledger_character_core_rejects_missing_rejected_and_nonadjacent_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, operator, initialized = initialized_character_ledger(monkeypatch)
    missing = operator_revision_payload(
        ledger,
        operator,
        initialized,
        curiosity_bp=5200,
        event_ref="event:core:missing-acceptance",
    )
    record_core_proposal(ledger, missing, "CharacterCoreRevised")
    projected = ledger.project()
    with pytest.raises(ValueError, match="adjacent|AcceptanceRecorded"):
        ledger.commit(
            [
                ledger_event(
                    missing.core_after.origin.accepted_event_ref,
                    "CharacterCoreRevised",
                    missing.model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    rejected_ledger, rejected_operator, rejected_initial = initialized_character_ledger(
        monkeypatch
    )
    rejected = operator_revision_payload(
        rejected_ledger,
        rejected_operator,
        rejected_initial,
        curiosity_bp=5300,
        event_ref="event:core:rejected-acceptance",
    )
    record_core_proposal(rejected_ledger, rejected, "CharacterCoreRevised")
    projected = rejected_ledger.project()
    rejection = {
        "acceptance_id": rejected.acceptance_id,
        "status": "rejected",
        "proposal_id": rejected.proposal_id,
        "evaluated_world_revision": rejected.evaluated_world_revision,
        "accepted_change_id": None,
        "accepted_change_hash": None,
    }
    with pytest.raises(ValueError, match="adjacent|accepted authority|accepted status"):
        rejected_ledger.commit(
            [
                ledger_event(
                    f"event:{rejected.acceptance_id}",
                    "AcceptanceRecorded",
                    rejection,
                ),
                ledger_event(
                    rejected.core_after.origin.accepted_event_ref,
                    "CharacterCoreRevised",
                    rejected.model_dump(mode="json"),
                ),
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    adjacent_ledger, adjacent_operator, adjacent_initial = initialized_character_ledger(
        monkeypatch
    )
    nonadjacent = operator_revision_payload(
        adjacent_ledger,
        adjacent_operator,
        adjacent_initial,
        curiosity_bp=5400,
        event_ref="event:core:nonadjacent-acceptance",
    )
    record_core_proposal(adjacent_ledger, nonadjacent, "CharacterCoreRevised")
    projected = adjacent_ledger.project()
    with pytest.raises(ValueError, match="immediately after|adjacent"):
        adjacent_ledger.commit(
            [
                ledger_event(
                    f"event:{nonadjacent.acceptance_id}",
                    "AcceptanceRecorded",
                    core_acceptance(nonadjacent),
                ),
                ledger_event(
                    "event:observation:between-acceptance-and-core",
                    "ObservationRecorded",
                    {"observation_id": "observation:between-acceptance-and-core"},
                ),
                ledger_event(
                    nonadjacent.core_after.origin.accepted_event_ref,
                    "CharacterCoreRevised",
                    nonadjacent.model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_ledger_character_core_rejects_proposal_hash_mismatch_and_stale_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, operator, initialized = initialized_character_ledger(monkeypatch)
    mismatch = operator_revision_payload(
        ledger,
        operator,
        initialized,
        curiosity_bp=5200,
        event_ref="event:core:hash-mismatch",
    )
    bad_proposal = core_proposal(mismatch, "CharacterCoreRevised").model_copy(
        update={"proposed_change_hash": "f" * 64}
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="proposal body does not match index"):
        ledger.commit(
            [
                ledger_event(
                    "event:proposal:core:hash-mismatch",
                    "ProposalRecorded",
                    bad_proposal.model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )

    revised = core(
        revision=2,
        values=core_values(curiosity_bp=5300, privacy="withhold"),
        event_ref="event:core:first-revision",
        updated_at=NOW,
    )
    valid = mutation(
        revised,
        operation="revise",
        lane="operator_revision",
        changed=("privacy_class", "slow_evolving"),
        before=initialized,
        operator=operator,
        evaluated_world_revision=ledger.project().world_revision,
    )
    pre_revision = ledger.project()
    accept_and_record_core(ledger, valid, "CharacterCoreRevised")
    target_event = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == valid.core_after.origin.accepted_event_ref
    )
    compensated = core(
        revision=3,
        values=core_values(privacy="withhold"),
        event_ref="event:core:ledger-compensated",
        updated_at=NOW,
    )
    compensation = mutation(
        compensated,
        operation="compensate",
        lane="compensation",
        changed=("slow_evolving",),
        before=valid.core_after,
        operator=operator,
        compensation_target=CharacterCoreCompensationTarget(
            transition_id=valid.transition_id,
            entity_revision=valid.core_after.entity_revision,
            accepted_event_ref=target_event.event_id,
            accepted_world_revision=target_event.world_revision,
            accepted_payload_hash=target_event.payload_hash,
        ),
        evaluated_world_revision=ledger.project().world_revision,
    )
    accept_and_record_core(
        ledger,
        compensation,
        "CharacterCoreRevisionCompensated",
    )
    after_compensation = ledger.project()
    assert after_compensation.character_core == compensated
    assert after_compensation.character_core.values.privacy_class == "withhold"
    assert tuple(
        item.operation for item in after_compensation.character_core_transitions
    ) == ("initialize", "revise", "compensate")
    assert after_compensation.character_core_proposals == ()
    assert ledger.rebuild() == after_compensation
    for field in (
        "facts",
        "fact_transitions",
        "experiences",
        "experience_transitions",
        "memory_candidates",
        "memory_candidate_transitions",
        "threads",
        "thread_transitions",
        "actions",
        "affect_episodes",
        "relationship_states",
        "commitments",
    ):
        assert getattr(after_compensation, field) == getattr(pre_revision, field), field
    stale = operator_revision_payload(
        ledger,
        operator,
        initialized,
        curiosity_bp=5400,
        event_ref="event:core:stale-revision",
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="before image does not match current head"):
        ledger.commit(
            [
                ledger_event(
                    "event:proposal:core:stale",
                    "ProposalRecorded",
                    core_proposal(stale, "CharacterCoreRevised").model_dump(mode="json"),
                )
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_operator_lane_rejects_actor_authority_without_character_scope() -> None:
    authority, operator, committed = operator_authority(allowed=("privacy_policy",))
    initialized = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:initialize",
        updated_at=NOW,
    )
    payload = mutation(
        initialized,
        operation="initialize",
        lane="operator_initialize",
        changed=("immutable_identity", "operator_governed", "privacy_class", "slow_evolving"),
        operator=operator,
    )
    with pytest.raises(ValueError, match="current root authority"):
        reduce_character_core(
            None, (), payload,
            event_type="CharacterCoreInitialized",
            event_id=initialized.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(authority,), facts=(), fact_history=(),
            experiences=(), experience_history=(), world_occurrences=(),
            committed_events=(committed,),
        )


def test_longitudinal_revision_requires_exact_cross_scene_cross_time_experiences() -> None:
    authority, operator, operator_event = operator_authority()
    current = core(
        revision=1, values=core_values(), event_ref="event:core:initialize", updated_at=NOW
    )
    init = mutation(
        current,
        operation="initialize",
        lane="operator_initialize",
        changed=("immutable_identity", "operator_governed", "privacy_class", "slow_evolving"),
        operator=operator,
    )
    _, history = reduce_character_core(
        None, (), init,
        event_type="CharacterCoreInitialized", event_id=current.origin.accepted_event_ref,
        logical_time=NOW, actor_authorities=(authority,), facts=(), fact_history=(),
        experiences=(), experience_history=(), world_occurrences=(),
        committed_events=(operator_event,),
    )
    first = occurrence_experience(
        index=1, occurred_to=NOW - timedelta(days=30), location_ref="location:park"
    )
    second = occurrence_experience(
        index=2, occurred_to=NOW - timedelta(days=1), location_ref="location:cafe"
    )
    window = evidence_window(first[-1], second[-1])
    revised = core(
        revision=2,
        values=core_values(curiosity_bp=5500),
        event_ref="event:core:evolve",
        updated_at=NOW,
    )
    payload = mutation(
        revised,
        operation="revise",
        lane="longitudinal_evolution",
        changed=("slow_evolving",),
        before=current,
        window=window,
    )
    head, evolved_history = reduce_character_core(
        current, history, payload,
        event_type="CharacterCoreRevised", event_id=revised.origin.accepted_event_ref,
        logical_time=NOW, actor_authorities=(authority,), facts=(), fact_history=(),
        experiences=(first[0], second[0]),
        experience_history=(first[1], second[1]),
        world_occurrences=(first[2], second[2]),
        committed_events=(operator_event, first[3], second[3]),
    )
    assert next(
        item.value_bp
        for item in head.values.slow_evolving.trait_axes
        if item.axis_code == "curiosity"
    ) == 5500

    reused = core(
        revision=3,
        values=core_values(curiosity_bp=5700),
        event_ref="event:core:reused-lineage",
        updated_at=NOW,
    )
    reused_payload = mutation(
        reused,
        operation="revise",
        lane="longitudinal_evolution",
        changed=("slow_evolving",),
        before=head,
        window=window,
    )
    with pytest.raises(ValueError, match="already consumed"):
        reduce_character_core(
            head,
            evolved_history,
            reused_payload,
            event_type="CharacterCoreRevised",
            event_id=reused.origin.accepted_event_ref,
            logical_time=NOW,
            actor_authorities=(authority,),
            facts=(),
            fact_history=(),
            experiences=(first[0], second[0]),
            experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(operator_event, first[3], second[3]),
        )

    revised_bindings = tuple(
        item.model_copy(
            update={
                "source_entity_revision": item.source_entity_revision + 1,
                "authority_event_ref": f"{item.authority_event_ref}:corrected",
                "authority_world_revision": item.authority_world_revision + 100,
                "authority_payload_hash": "f" * 64,
                "source_values_hash": "e" * 64,
            }
        )
        for item in window.source_bindings
    )
    stable_lineage_window = evidence_window(*revised_bindings)
    stable_lineage_payload = mutation(
        reused,
        operation="revise",
        lane="longitudinal_evolution",
        changed=("slow_evolving",),
        before=head,
        window=stable_lineage_window,
    )
    with pytest.raises(ValueError, match="stable source lineage is already consumed"):
        _reject_evidence_reuse(stable_lineage_payload, evolved_history)

    forged_source = second[-1].model_copy(update={"scene_ref": "scene:occurrence:fake:2099-01-01"})
    forged_window = evidence_window(first[-1], forged_source)
    forged_payload = mutation(
        revised, operation="revise", lane="longitudinal_evolution",
        changed=("slow_evolving",), before=current, window=forged_window,
    )
    with pytest.raises(ValueError, match="classification is not source-derived"):
        reduce_character_core(
            current, history, forged_payload,
            event_type="CharacterCoreRevised", event_id=revised.origin.accepted_event_ref,
            logical_time=NOW, actor_authorities=(authority,), facts=(), fact_history=(),
            experiences=(first[0], second[0]), experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(operator_event, first[3], second[3]),
        )


def test_long_duration_single_event_cannot_fake_longitudinal_separation() -> None:
    authority, _, operator_event = operator_authority()
    first = occurrence_experience(
        index=3, occurred_to=NOW - timedelta(days=1), location_ref="location:park"
    )
    second = occurrence_experience(
        index=4, occurred_to=NOW - timedelta(days=1), location_ref="location:cafe"
    )
    forged_first = first[-1].model_copy(
        update={"observed_from": NOW - timedelta(days=20)}
    )
    window = evidence_window(forged_first, second[-1])
    current = core(
        revision=1, values=core_values(), event_ref="event:core:initialize", updated_at=NOW
    )
    revised = core(
        revision=2, values=core_values(curiosity_bp=5200),
        event_ref="event:core:evolve", updated_at=NOW,
    )
    payload = mutation(
        revised, operation="revise", lane="longitudinal_evolution",
        changed=("slow_evolving",), before=current, window=window,
    )
    with pytest.raises(ValueError, match="classification is not source-derived|too short"):
        reduce_character_core(
            current, (), payload,
            event_type="CharacterCoreRevised", event_id=revised.origin.accepted_event_ref,
            logical_time=NOW, actor_authorities=(authority,), facts=(), fact_history=(),
            experiences=(first[0], second[0]), experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(operator_event, first[3], second[3]),
        )


def test_short_term_preference_injection_is_rejected_by_installed_catalog() -> None:
    raw = core_values().slow_evolving.model_dump(mode="json")
    raw["preference_refs"] = ["mood:sad"]
    with pytest.raises(ValidationError, match="preference_refs"):
        CharacterCoreSlowEvolving.model_validate(raw)


def test_legacy_raw_character_core_shape_is_quarantined_not_promoted() -> None:
    # Reducer bundles through .14 had no CharacterCore authority field.  This
    # old snapshot-only shape may become an operator-reviewed import candidate,
    # but it cannot enter the ledger except through CharacterCoreInitialized.
    legacy_raw = {
        "core_revision": 7,
        "identity_refs": ["identity:legacy"],
        "traits": {"mood": "sad", "curiosity": 0.9},
        "values": ["care"],
        "preferences": ["tea"],
        "boundaries": ["privacy"],
    }
    with pytest.raises(ValidationError, match="Field required|extra_forbidden"):
        CharacterCoreProjection.model_validate(legacy_raw)


def test_naive_character_evidence_time_is_rejected_locally() -> None:
    source = occurrence_experience(
        index=5, occurred_to=NOW - timedelta(days=20), location_ref="location:park"
    )[-1]
    raw = source.model_dump()
    raw["observed_to"] = datetime(2026, 6, 20, 12, 0)
    with pytest.raises(ValidationError, match="timezone-aware"):
        CharacterCoreEvidenceBinding.model_validate(raw)


def test_unrelated_npc_experiences_cannot_change_companion_core() -> None:
    first = occurrence_experience(
        index=6,
        occurred_to=NOW - timedelta(days=30),
        location_ref="location:park",
        participant_ref="npc:other",
    )
    second = occurrence_experience(
        index=7,
        occurred_to=NOW - timedelta(days=1),
        location_ref="location:cafe",
        participant_ref="npc:other",
    )
    current = core(
        revision=1, values=core_values(), event_ref="event:core:initialize", updated_at=NOW
    )
    revised = core(
        revision=2, values=core_values(curiosity_bp=5200),
        event_ref="event:core:evolve", updated_at=NOW,
    )
    payload = mutation(
        revised, operation="revise", lane="longitudinal_evolution",
        changed=("slow_evolving",), before=current,
        window=evidence_window(first[-1], second[-1]),
    )
    with pytest.raises(ValueError, match="does not involve target actor"):
        reduce_character_core(
            current, (), payload,
            event_type="CharacterCoreRevised", event_id=revised.origin.accepted_event_ref,
            logical_time=NOW, actor_authorities=(), facts=(), fact_history=(),
            experiences=(first[0], second[0]), experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(first[3], second[3]),
        )


def test_multi_axis_small_deltas_cannot_exceed_total_variation_budget() -> None:
    first = occurrence_experience(
        index=8, occurred_to=NOW - timedelta(days=30), location_ref="location:park"
    )
    second = occurrence_experience(
        index=9, occurred_to=NOW - timedelta(days=1), location_ref="location:cafe"
    )
    current = core(
        revision=1, values=core_values(), event_ref="event:core:initialize", updated_at=NOW
    )
    revised = core(
        revision=2,
        values=core_values(curiosity_bp=5700, assertiveness_bp=5700),
        event_ref="event:core:evolve",
        updated_at=NOW,
    )
    payload = mutation(
        revised, operation="revise", lane="longitudinal_evolution",
        changed=("slow_evolving",), before=current,
        window=evidence_window(first[-1], second[-1]),
    )
    with pytest.raises(ValueError, match="total variation"):
        reduce_character_core(
            current, (), payload,
            event_type="CharacterCoreRevised", event_id=revised.origin.accepted_event_ref,
            logical_time=NOW, actor_authorities=(), facts=(), fact_history=(),
            experiences=(first[0], second[0]), experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(first[3], second[3]),
        )


def test_longitudinal_evolution_enforces_ninety_day_rolling_axis_drift() -> None:
    current = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:rolling-base",
        updated_at=NOW,
    )
    history = ()

    for revision, curiosity_bp, first_index, logical_time in (
        (2, 5800, 20, NOW),
        (3, 6600, 22, NOW + timedelta(days=1)),
    ):
        first = occurrence_experience(
            index=first_index,
            occurred_to=NOW - timedelta(days=30),
            location_ref=f"location:rolling:{first_index}",
        )
        second = occurrence_experience(
            index=first_index + 1,
            occurred_to=NOW - timedelta(days=1),
            location_ref=f"location:rolling:{first_index + 1}",
        )
        after = core(
            revision=revision,
            values=core_values(curiosity_bp=curiosity_bp),
            event_ref=f"event:core:rolling:{revision}",
            updated_at=logical_time,
        )
        payload = mutation(
            after,
            operation="revise",
            lane="longitudinal_evolution",
            changed=("slow_evolving",),
            before=current,
            window=evidence_window(first[-1], second[-1]),
        )
        current, history = reduce_character_core(
            current,
            history,
            payload,
            event_type="CharacterCoreRevised",
            event_id=after.origin.accepted_event_ref,
            logical_time=logical_time,
            actor_authorities=(),
            facts=(),
            fact_history=(),
            experiences=(first[0], second[0]),
            experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(first[3], second[3]),
        )

    first = occurrence_experience(
        index=24,
        occurred_to=NOW - timedelta(days=30),
        location_ref="location:rolling:24",
    )
    second = occurrence_experience(
        index=25,
        occurred_to=NOW - timedelta(days=1),
        location_ref="location:rolling:25",
    )
    over_limit = core(
        revision=4,
        values=core_values(curiosity_bp=6700),
        event_ref="event:core:rolling:4",
        updated_at=NOW + timedelta(days=2),
    )
    payload = mutation(
        over_limit,
        operation="revise",
        lane="longitudinal_evolution",
        changed=("slow_evolving",),
        before=current,
        window=evidence_window(first[-1], second[-1]),
    )
    with pytest.raises(ValueError, match="rolling drift limit"):
        reduce_character_core(
            current,
            history,
            payload,
            event_type="CharacterCoreRevised",
            event_id=over_limit.origin.accepted_event_ref,
            logical_time=NOW + timedelta(days=2),
            actor_authorities=(),
            facts=(),
            fact_history=(),
            experiences=(first[0], second[0]),
            experience_history=(first[1], second[1]),
            world_occurrences=(first[2], second[2]),
            committed_events=(first[3], second[3]),
        )


def test_longitudinal_evolution_enforces_rolling_preference_and_style_churn() -> None:
    def values_with(
        base: CharacterCoreValues,
        *,
        preferences: tuple[str, ...] | None = None,
        autonomy_style: str | None = None,
        attachment_tendency: str | None = None,
        conflict_style: str | None = None,
    ) -> CharacterCoreValues:
        slow = base.slow_evolving.model_copy(
            update={
                **(
                    {"preference_refs": preferences}
                    if preferences is not None
                    else {}
                ),
                **(
                    {"autonomy_style": autonomy_style}
                    if autonomy_style is not None
                    else {}
                ),
                **(
                    {"attachment_tendency": attachment_tendency}
                    if attachment_tendency is not None
                    else {}
                ),
                **(
                    {"conflict_style": conflict_style}
                    if conflict_style is not None
                    else {}
                ),
            }
        )
        return base.model_copy(update={"slow_evolving": slow})

    def transition(
        index: int,
        before: CharacterCoreValues,
        after: CharacterCoreValues,
    ) -> CharacterCoreTransitionProjection:
        return CharacterCoreTransitionProjection(
            transition_id=f"transition:rolling-categorical:{index}",
            core_id="core:companion",
            entity_revision=index + 1,
            operation="revise",
            authority_lane="longitudinal_evolution",
            changed_field_classes=("slow_evolving",),
            values_before=before,
            values_after=after,
            evidence_window=None,
            operator_authority=None,
            change_id=f"change:rolling-categorical:{index}",
            policy_refs=CHARACTER_CORE_POLICY_REFS,
            policy_version=CHARACTER_CORE_POLICY_VERSION,
            policy_digest=CHARACTER_CORE_POLICY_DIGEST,
            accepted_event_ref=f"event:rolling-categorical:{index}",
            accepted_at=NOW - timedelta(days=3 - index),
            compensates_transition_id=None,
        )

    preference_0 = core_values()
    preference_1 = values_with(
        preference_0,
        preferences=("preference:direct_communication",),
    )
    preference_2 = values_with(
        preference_1,
        preferences=("preference:independent_time",),
    )
    preference_3 = values_with(
        preference_2,
        preferences=("preference:playful_banter",),
    )
    with pytest.raises(ValueError, match="rolling preference churn"):
        _validate_longitudinal_delta(
            preference_2,
            preference_3,
            (
                transition(1, preference_0, preference_1),
                transition(2, preference_1, preference_2),
            ),
            NOW,
        )

    style_0 = core_values()
    style_1 = values_with(style_0, autonomy_style="collaborative")
    style_2 = values_with(style_1, attachment_tendency="guarded")
    style_3 = values_with(style_2, conflict_style="direct")
    with pytest.raises(ValueError, match="rolling style drift"):
        _validate_longitudinal_delta(
            style_2,
            style_3,
            (
                transition(1, style_0, style_1),
                transition(2, style_1, style_2),
            ),
            NOW,
        )

def test_compensation_restores_semantics_but_never_loosens_privacy_floor() -> None:
    authority, operator, operator_event = operator_authority()
    initial = core(
        revision=1,
        values=core_values(),
        event_ref="event:core:initialize",
        updated_at=NOW,
    )
    init_payload = mutation(
        initial,
        operation="initialize",
        lane="operator_initialize",
        changed=(
            "immutable_identity",
            "operator_governed",
            "privacy_class",
            "slow_evolving",
        ),
        operator=operator,
    )
    _, history = reduce_character_core(
        None,
        (),
        init_payload,
        event_type="CharacterCoreInitialized",
        event_id=initial.origin.accepted_event_ref,
        logical_time=NOW,
        actor_authorities=(authority,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        world_occurrences=(),
        committed_events=(operator_event,),
    )
    revised_at = NOW + timedelta(hours=1)
    revised = core(
        revision=2,
        values=core_values(curiosity_bp=5600, privacy="withhold"),
        event_ref="event:core:operator-revision",
        updated_at=revised_at,
    )
    revision_payload = mutation(
        revised,
        operation="revise",
        lane="operator_revision",
        changed=("privacy_class", "slow_evolving"),
        before=initial,
        operator=operator,
    )
    revised, history = reduce_character_core(
        initial,
        history,
        revision_payload,
        event_type="CharacterCoreRevised",
        event_id=revised.origin.accepted_event_ref,
        logical_time=revised_at,
        actor_authorities=(authority,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        world_occurrences=(),
        committed_events=(operator_event,),
    )
    target_event = CommittedWorldEventRef(
        event_id=revised.origin.accepted_event_ref,
        event_type="CharacterCoreRevised",
        world_revision=2,
        payload_hash="e" * 64,
        logical_time=revised_at,
    )
    compensated_at = revised_at + timedelta(hours=1)
    compensated = core(
        revision=3,
        values=core_values(curiosity_bp=5000, privacy="withhold"),
        event_ref="event:core:compensated",
        updated_at=compensated_at,
    )
    compensation_payload = mutation(
        compensated,
        operation="compensate",
        lane="compensation",
        changed=("slow_evolving",),
        before=revised,
        operator=operator,
        compensation_target=CharacterCoreCompensationTarget(
            transition_id=revision_payload.transition_id,
            entity_revision=revised.entity_revision,
            accepted_event_ref=target_event.event_id,
            accepted_world_revision=target_event.world_revision,
            accepted_payload_hash=target_event.payload_hash,
        ),
    )
    head, history = reduce_character_core(
        revised,
        history,
        compensation_payload,
        event_type="CharacterCoreRevisionCompensated",
        event_id=compensated.origin.accepted_event_ref,
        logical_time=compensated_at,
        actor_authorities=(authority,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        world_occurrences=(),
        committed_events=(operator_event, target_event),
    )

    assert head.values.slow_evolving == initial.values.slow_evolving
    assert head.values.privacy_class == "withhold"
    assert history[-1].changed_field_classes == ("slow_evolving",)
    assert history[-1].compensates_transition_id == revision_payload.transition_id

    compensation_event = CommittedWorldEventRef(
        event_id=compensated.origin.accepted_event_ref,
        event_type="CharacterCoreRevisionCompensated",
        world_revision=3,
        payload_hash="f" * 64,
        logical_time=compensated_at,
    )
    redo_at = compensated_at + timedelta(hours=1)
    redone = core(
        revision=4,
        values=core_values(curiosity_bp=5600, privacy="withhold"),
        event_ref="event:core:redo",
        updated_at=redo_at,
    )
    redo_target = CharacterCoreCompensationTarget(
        transition_id=history[-1].transition_id,
        entity_revision=head.entity_revision,
        accepted_event_ref=compensation_event.event_id,
        accepted_world_revision=compensation_event.world_revision,
        accepted_payload_hash=compensation_event.payload_hash,
    )
    unauthorized_redo = mutation(
        redone,
        operation="compensate",
        lane="compensation",
        changed=("slow_evolving",),
        before=head,
        compensation_target=redo_target,
    )
    with pytest.raises(ValueError, match="lacks actor authority"):
        reduce_character_core(
            head,
            history,
            unauthorized_redo,
            event_type="CharacterCoreRevisionCompensated",
            event_id=redone.origin.accepted_event_ref,
            logical_time=redo_at,
            actor_authorities=(authority,),
            facts=(),
            fact_history=(),
            experiences=(),
            experience_history=(),
            world_occurrences=(),
            committed_events=(operator_event, target_event, compensation_event),
        )
    authorized_redo = mutation(
        redone,
        operation="compensate",
        lane="compensation",
        changed=("slow_evolving",),
        before=head,
        operator=operator,
        compensation_target=redo_target,
    )
    redone_head, redone_history = reduce_character_core(
        head,
        history,
        authorized_redo,
        event_type="CharacterCoreRevisionCompensated",
        event_id=redone.origin.accepted_event_ref,
        logical_time=redo_at,
        actor_authorities=(authority,),
        facts=(),
        fact_history=(),
        experiences=(),
        experience_history=(),
        world_occurrences=(),
        committed_events=(operator_event, target_event, compensation_event),
    )
    assert redone_head == redone
    assert redone_history[-1].compensates_transition_id == history[-1].transition_id


def test_sqlite_migrates_nonempty_v14_to_v15_replays_and_rejects_tamper(
    tmp_path,
) -> None:
    path = tmp_path / "character-core-v14.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        [ledger_event("event:world:start", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [
            ledger_event(
                "event:clock:start",
                "ClockAdvanced",
                {
                    "logical_time_from": (NOW - timedelta(minutes=1)).isoformat(),
                    "logical_time_to": NOW.isoformat(),
                },
            )
        ],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    ledger.close()

    def downgrade(*, tamper: bool) -> None:
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT world_revision, state_json FROM world_v2_heads WHERE world_id = ?",
                (WORLD,),
            ).fetchone()
            assert row is not None
            world_revision, state_json = row
            state = ReducerState.model_validate_json(state_json)
            legacy_semantic = state.semantic_payload(
                world_id=WORLD,
                world_revision=int(world_revision),
                reducer_bundle_version="world-v2-reducers.14",
            )
            legacy_hash = hashlib.sha256(
                json.dumps(
                    legacy_semantic,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            raw_state = state.model_dump(mode="json")
            strip_v16_state_fields(raw_state)
            for field in (
                "character_core",
                "character_core_transitions",
                "character_core_proposals",
                "character_core_proposal_ids",
            ):
                raw_state.pop(field)
            if tamper:
                raw_state["logical_time"] = (NOW + timedelta(days=1)).isoformat()
            connection.execute(
                "UPDATE world_v2_heads SET state_json = ?, semantic_hash = ?, "
                "reducer_bundle_version = ?, state_hash = '' WHERE world_id = ?",
                (
                    json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                    legacy_hash,
                    "world-v2-reducers.14",
                    WORLD,
                ),
            )

    downgrade(tamper=False)
    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projected = migrated.project()
    assert projected.reducer_bundle_version == "world-v2-reducers.24"
    assert projected.world_revision == 2
    assert projected.logical_time == NOW
    assert projected.character_core is None
    assert projected.character_core_transitions == ()
    assert migrated.rebuild() == projected
    migrated.close()

    downgrade(tamper=True)
    with pytest.raises(LedgerIntegrityError, match="legacy head semantic hash"):
        SQLiteWorldLedger(path=path, world_id=WORLD)
