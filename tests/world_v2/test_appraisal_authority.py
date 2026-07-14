from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.batch_invariants import (
    interaction_appraisal_trigger_identity,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import (
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    ClaimLease,
    EvidenceRef,
    TriggerProcess,
    WorldEvent,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world-v2-appraisal-authority"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type, world_id=WORLD_ID, payload=payload
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor=str(payload.get("actor", "system:test")),
        source=str(payload.get("source", "test")),
        trace_id=str(payload.get("trace_id", "trace:appraisal-authority")),
        causation_id=str(payload.get("causation_id", f"cause:{event_id}")),
        correlation_id=str(
            payload.get("correlation_id", "correlation:appraisal-authority")
        ),
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


Ledger = WorldLedger | SQLiteWorldLedger


def commit(ledger: Ledger, events: list[WorldEvent]) -> None:
    head = ledger.project()
    ledger.commit(
        events,
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )


def message_payload(observation_id: str) -> dict[str, object]:
    return {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD_ID,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:message",
        "causation_id": f"cause:{observation_id}",
        "correlation_id": "correlation:message",
        "source": "test-platform",
        "source_event_id": f"source:{observation_id}",
        "actor": "user:test",
        "channel": "direct_message",
        "payload_ref": f"payload:{observation_id}",
        "payload_hash": "a" * 64,
        "received_at": NOW.isoformat(),
    }


def prepare_claimed_interaction(
    ledger: Ledger | None = None,
) -> tuple[Ledger, TriggerProcess, EvidenceRef]:
    ledger = ledger or WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [event("message-event:1", "ObservationRecorded", message_payload("message:1"))],
    )
    opened = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "message:1"),
        trigger_ref="interaction:message:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:1",
        state="open",
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-opened",
                "TriggerProcessOpened",
                {"process": opened.model_dump(mode="json")},
            )
        ],
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:interaction-appraisal",
                attempt_id="attempt:interaction:1",
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:interaction:1",),
        }
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-claimed",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    message = ledger.project().message_observations[0]
    evidence = EvidenceRef(
        ref_id="message:1",
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        source_world_revision=message.world_revision,
        immutable_hash=message.event_payload_hash,
    )
    return ledger, claimed, evidence


def accepted_payload(
    ledger: Ledger, trigger: TriggerProcess, evidence: EvidenceRef
) -> dict[str, object]:
    appraisal = AppraisalProjection(
        appraisal_id="appraisal:interaction:1",
        entity_revision=1,
        subject_ref="interaction:user:1",
        source_cluster_ref="conversation:1",
        origin=AppraisalOrigin(
            change_id="change:interaction-appraisal:1",
            transition_id="transition:interaction-appraisal:1",
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref="interaction-appraisal-accepted",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="meaning:disappointment",
                meaning="disappointment",
                attribution="user",
                controllability="partly_controllable",
                severity="moderate",
                weight_bp=6_500,
            ),
            AppraisalHypothesis(
                hypothesis_id="meaning:misunderstanding",
                meaning="misunderstanding",
                attribution="unknown",
                controllability="controllable",
                severity="low",
                weight_bp=3_500,
            ),
        ),
        evidence_refs=(evidence,),
        confidence_bp=7_200,
        accepted_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    payload: dict[str, object] = {
        "change_id": "change:interaction-appraisal:1",
        "transition_id": "transition:interaction-appraisal:1",
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "policy_refs": ["policy:appraisal-v1"],
        "acceptance_id": "acceptance:interaction-appraisal:1",
        "proposal_id": "proposal:interaction-appraisal:1",
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "trigger_id": trigger.trigger_id,
        "appraisal": appraisal.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    return payload


def record_proposal(
    ledger: Ledger,
    trigger: TriggerProcess,
    evidence: EvidenceRef,
    payload: dict[str, object],
    additional_evidence: tuple[EvidenceRef, ...] = (),
) -> None:
    commit(ledger, [proposal_event(trigger, evidence, payload, additional_evidence)])


def proposal_event(
    trigger: TriggerProcess,
    evidence: EvidenceRef,
    payload: dict[str, object],
    additional_evidence: tuple[EvidenceRef, ...] = (),
) -> WorldEvent:
    return event(
        "interaction-appraisal-proposed",
        "ProposalRecorded",
        {
            "proposal_id": payload["proposal_id"],
            "proposal_kind": "appraisal_transition",
            "transition_kind": "accept",
            "change_id": payload["change_id"],
            "trigger_id": trigger.trigger_id,
            "trigger_ref": trigger.trigger_ref,
            "source_evidence_ref": trigger.source_evidence_ref,
            "evaluated_world_revision": payload["evaluated_world_revision"],
            "expected_entity_revision": payload["expected_entity_revision"],
            "proposed_change_hash": payload["accepted_change_hash"],
            "evidence_refs": [
                evidence.model_dump(mode="json"),
                *(item.model_dump(mode="json") for item in additional_evidence),
            ],
            "policy_refs": payload["policy_refs"],
            "proposed_mutation": {
                "event_type": "AppraisalAccepted",
                "payload_json": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        },
    )


def authorized_batch(
    trigger: TriggerProcess, payload: dict[str, object]
) -> list[WorldEvent]:
    return [
        event(
            "interaction-appraisal-acceptance",
            "AcceptanceRecorded",
            {
                "status": "accepted",
                "acceptance_id": payload["acceptance_id"],
                "proposal_id": payload["proposal_id"],
                "evaluated_world_revision": payload["evaluated_world_revision"],
                "accepted_change_id": payload["change_id"],
                "accepted_change_hash": payload["accepted_change_hash"],
            },
        ),
        event("interaction-appraisal-accepted", "AppraisalAccepted", payload),
        event(
            "interaction-appraisal-completed",
            "TriggerProcessCompleted",
            {
                "trigger_id": trigger.trigger_id,
                "owner_id": "worker:interaction-appraisal",
                "attempt_id": "attempt:interaction:1",
                "completed_at": NOW.isoformat(),
                "runtime_outcome_ref": "appraisal:appraisal:interaction:1",
            },
        ),
    ]


def test_observed_message_can_follow_the_shared_appraisal_authority_path() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)

    commit(ledger, authorized_batch(trigger, payload))

    assert ledger.project().appraisals[0].hypotheses[0].meaning == "disappointment"
    assert ledger.project().trigger_processes[0].state == "terminal"
    assert ledger.project().appraisal_proposals == ()
    assert ledger.project().appraisal_proposal_ids == (
        "proposal:interaction-appraisal:1",
    )


def test_sqlite_replays_the_complete_interaction_appraisal_authority_path(
    tmp_path,
) -> None:
    path = tmp_path / "appraisal-authority.sqlite3"
    ledger, trigger, evidence = prepare_claimed_interaction(
        SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    )
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))
    expected = ledger.project()
    assert ledger.rebuild() == expected
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_appraisal_acceptance_cannot_be_synthesized_without_persisted_proposal() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)

    with pytest.raises(ValueError, match="unknown proposal"):
        commit(ledger, authorized_batch(trigger, payload))


def test_typed_accepted_decision_cannot_commit_without_its_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)

    with pytest.raises(ValueError, match="domain mutation immediately"):
        commit(ledger, [authorized_batch(trigger, payload)[0]])


def test_world_change_cannot_intervene_between_acceptance_and_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    batch = authorized_batch(trigger, payload)
    batch.insert(
        1,
        event(
            "intervening-observation",
            "ObservationRecorded",
            {"observation_id": "observation:intervening"},
        ),
    )

    with pytest.raises(ValueError, match="domain mutation immediately"):
        commit(ledger, batch)


def test_rejected_decision_matches_and_terminalizes_its_proposal() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    rejection = event(
        "interaction-appraisal-rejected",
        "AcceptanceRecorded",
        {
            "status": "rejected",
            "acceptance_id": "acceptance:interaction-appraisal:rejected",
            "proposal_id": payload["proposal_id"],
            "evaluated_world_revision": payload["evaluated_world_revision"],
        },
    )
    commit(ledger, [rejection])

    assert ledger.project().appraisal_proposals == ()
    assert ledger.project().acceptance_decisions[0].status == "rejected"


def test_appraisal_proposal_rejects_nonexistent_evidence() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    missing = evidence.model_copy(update={"ref_id": "message:missing"})
    missing_dump = missing.model_dump(mode="json")
    payload["evidence_refs"] = [*payload["evidence_refs"], missing_dump]
    payload["appraisal"]["evidence_refs"] = [
        *payload["appraisal"]["evidence_refs"],
        missing_dump,
    ]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="observed-message evidence"):
        record_proposal(
            ledger,
            trigger,
            evidence,
            payload,
            additional_evidence=(missing,),
        )


def test_interaction_trigger_cannot_open_for_an_unobserved_message() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    invalid = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "message:missing"),
        trigger_ref="interaction:message:missing",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:missing",
        state="open",
    )

    with pytest.raises(ValueError, match="observed message"):
        commit(
            ledger,
            [
                event(
                    "invalid-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": invalid.model_dump(mode="json")},
                )
            ],
        )


def test_proposal_and_acceptance_require_separate_atomic_commits() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)

    with pytest.raises(ValueError, match="separate deliberation commit"):
        commit(
            ledger,
            [proposal_event(trigger, evidence, payload), *authorized_batch(trigger, payload)],
        )


def test_proposal_reducer_rejects_a_forged_stale_caller_revision() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    payload["evaluated_world_revision"] = 0
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    proposed = proposal_event(trigger, evidence, payload)
    head = ledger.project()

    with pytest.raises(ValueError, match="current world revision"):
        ledger.commit(
            [proposed],
            expected_world_revision=0,
            expected_deliberation_revision=head.deliberation_revision,
        )


def test_acceptance_must_precede_the_authorized_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    batch = authorized_batch(trigger, payload)

    with pytest.raises(ValueError, match="AcceptanceRecorded"):
        commit(ledger, [batch[1], batch[0], batch[2]])


def test_interaction_trigger_identity_is_deterministic() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [event("message-event:1", "ObservationRecorded", message_payload("message:1"))],
    )
    invalid = TriggerProcess(
        trigger_id="appraisal:interaction:arbitrary",
        trigger_ref="interaction:message:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:1",
        state="open",
    )

    with pytest.raises(ValueError, match="identity is not deterministic"):
        commit(
            ledger,
            [
                event(
                    "nondeterministic-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": invalid.model_dump(mode="json")},
                )
            ],
        )


def test_observed_message_rejects_forged_provenance_metadata() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    forged = evidence.model_copy(update={"immutable_hash": "a" * 64})
    forged_dump = forged.model_dump(mode="json")
    payload["evidence_refs"] = [forged_dump]
    payload["appraisal"]["evidence_refs"] = [forged_dump]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="provenance"):
        record_proposal(ledger, trigger, forged, payload)


def test_acceptance_identity_rejects_a_conflicting_second_decision() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))

    with pytest.raises(IdempotencyConflict):
        commit(
            ledger,
            [
                event(
                    "conflicting-interaction-appraisal-acceptance",
                    "AcceptanceRecorded",
                    {
                        "status": "stale",
                        "acceptance_id": payload["acceptance_id"],
                        "proposal_id": payload["proposal_id"],
                        "evaluated_world_revision": payload["evaluated_world_revision"],
                    },
                )
            ],
        )


def test_uninstalled_appraisal_policy_cannot_authorize_a_transition() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    payload["policy_refs"] = ["policy:does-not-exist"]
    payload["appraisal"]["origin"]["policy_refs"] = ["policy:does-not-exist"]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="uninstalled policy"):
        record_proposal(ledger, trigger, evidence, payload)


def test_consumed_proposal_identity_cannot_be_reused() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))
    reused = proposal_event(trigger, evidence, payload).payload()
    reused["change_id"] = "change:reused-proposal-id"
    reused["evaluated_world_revision"] = ledger.project().world_revision

    with pytest.raises(ValueError, match="identity is already registered"):
        commit(
            ledger,
            [event("reused-appraisal-proposal", "ProposalRecorded", reused)],
        )


def test_generic_observation_cannot_masquerade_as_a_message() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [
            event(
                "tool-shaped-observation",
                "ObservationRecorded",
                {"observation_id": "tool-result:1"},
            )
        ],
    )
    trigger = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "tool-result:1"),
        trigger_ref="interaction:tool-result:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="tool-result:1",
        state="open",
    )

    with pytest.raises(ValueError, match="observed message"):
        commit(
            ledger,
            [
                event(
                    "tool-result-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": trigger.model_dump(mode="json")},
                )
            ],
        )


def test_partial_observation_shape_cannot_become_message_authority() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    partial = message_payload("partial:1")
    for field in (
        "schema_version",
        "logical_time",
        "created_at",
        "trace_id",
        "causation_id",
        "correlation_id",
        "received_at",
    ):
        partial.pop(field)
    with pytest.raises(ValueError, match="Field required"):
        commit(
            ledger,
            [
                event(
                    "partial-observation",
                    "ObservationRecorded",
                    partial,
                )
            ],
        )
    commit(
        ledger,
        [
            event(
                "corrected-partial-observation",
                "ObservationRecorded",
                message_payload("partial:1"),
            )
        ],
    )
    assert ledger.project().message_observations[0].observation_id == "partial:1"


def test_observation_id_cannot_alias_different_message_bytes() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    first = message_payload("message:alias")
    second = {
        **first,
        "source_event_id": "source:message:alias:second",
        "payload_hash": "d" * 64,
    }
    commit(ledger, [event("message-alias-first", "ObservationRecorded", first)])

    with pytest.raises(ValueError, match="observation identity"):
        commit(
            ledger,
            [event("message-alias-second", "ObservationRecorded", second)],
        )


def test_acceptance_without_a_proposal_is_rejected_at_ingress() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)

    with pytest.raises(ValueError, match="proposal_id"):
        commit(
            ledger,
            [
                event(
                    "orphan-acceptance",
                    "AcceptanceRecorded",
                    {"status": "accepted"},
                )
            ],
        )


def test_acceptance_identity_is_globally_unique_across_proposals() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [
            event(
                "generic-proposal:1",
                "ProposalRecorded",
                {"proposal_id": "generic:1", "evaluated_world_revision": 0},
            ),
            event(
                "generic-proposal:2",
                "ProposalRecorded",
                {"proposal_id": "generic:2", "evaluated_world_revision": 0},
            ),
        ],
    )
    commit(
        ledger,
        [
            event(
                "generic-rejection:1",
                "AcceptanceRecorded",
                {
                    "status": "rejected",
                    "acceptance_id": "acceptance:global",
                    "proposal_id": "generic:1",
                    "evaluated_world_revision": 0,
                },
            )
        ],
    )

    with pytest.raises(ValueError, match="acceptance identity"):
        commit(
            ledger,
            [
                event(
                    "generic-rejection:2",
                    "AcceptanceRecorded",
                    {
                        "status": "rejected",
                        "acceptance_id": "acceptance:global",
                        "proposal_id": "generic:2",
                        "evaluated_world_revision": 0,
                    },
                )
            ],
        )
