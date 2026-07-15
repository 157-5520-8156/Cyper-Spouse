from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import sqlite3

import pytest

from legacy_migration_support import strip_v16_state_fields

from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.experience_events import (
    ExperienceCommittedPayload,
    experience_mutation_hash,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger import canonical_event_json
from companion_daemon.world_v2.ledger import commit_request_hash
from companion_daemon.world_v2.life_reducers import commit_experience
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import (
    Action,
    BudgetAccount,
    BudgetReservation,
    DueWindow,
    EvidenceRef,
    ExecutionReceipt,
    ExperienceExecutionReceiptBinding,
    ExperienceOccurrenceSettlementBinding,
    ExperienceOrigin,
    ExperienceProjection,
    ExperienceProposalProjection,
    ExperienceProposedMutation,
    ExperienceValues,
    FactAssertionBinding,
    FactOrigin,
    FactProjection,
    FactValues,
    LegacyExperienceProjection,
    PlanStateProjection,
    CommittedWorldEventRef,
    CommitResult,
    WorldEvent,
    WorldOccurrenceProjection,
    experience_semantic_fingerprint,
    fact_conflict_key,
    fact_semantic_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 15, 14, 0, tzinfo=UTC)
WORLD = "world-experience-authority"
POLICY = ("policy:experience-v1",)


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:experience",
        causation_id=f"cause:{event_id}",
        correlation_id="correlation:experience",
        idempotency_key=domain_idempotency_key(
            event_type=event_type, world_id=WORLD, payload=payload
        ) or f"identity:{event_id}",
        payload=payload,
    )


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def receipt() -> ExecutionReceipt:
    return ExecutionReceipt(
        receipt_id="receipt:experience",
        result_id="result:experience",
        action_id="action:experience",
        provider="test-provider",
        provider_ref="provider-ref:experience",
        source_event_id="source-event:experience",
        receipt_kind="terminal",
        observed_state="cancelled",
        is_terminal=True,
        cost_actual=0,
        received_at=NOW - timedelta(minutes=1),
        raw_payload_hash="a" * 64,
    )


def action() -> Action:
    return Action(
        schema_version="world-v2.1",
        action_id="action:experience",
        world_id=WORLD,
        logical_time=NOW - timedelta(minutes=2),
        created_at=NOW - timedelta(minutes=2),
        trace_id="trace:experience-action",
        causation_id="cause:experience-action",
        correlation_id="correlation:experience",
        kind="tool_result",
        layer="external_action",
        intent_ref="intent:experience",
        actor="actor:companion",
        target="target:test",
        payload_ref="payload:action",
        payload_hash="c" * 64,
        idempotency_key="action:experience:idempotency",
        budget_reservation_id="reservation:experience",
        state="authorized",
        recovery_policy="manual",
    )


def binding(
    *,
    receipt_id: str = "receipt:experience",
    authority: ExecutionReceipt | None = None,
    action_authority: Action | None = None,
) -> ExperienceExecutionReceiptBinding:
    authority = authority or receipt()
    action_authority = action_authority or action()
    return ExperienceExecutionReceiptBinding(
        source_kind="execution_receipt",
        receipt_id=receipt_id,
        receipt_hash=canonical_hash(authority),
        action_id=authority.action_id,
        action_payload_hash=action_authority.payload_hash,
        result_id=authority.result_id,
        observed_state=authority.observed_state,
        raw_payload_hash=authority.raw_payload_hash,
    )


def evidence(source: ExperienceExecutionReceiptBinding) -> EvidenceRef:
    return EvidenceRef(
        ref_id=source.receipt_id,
        evidence_type="settled_external_result",
        claim_purpose="past_experience",
        immutable_hash=source.receipt_hash,
    )


def experience(
    *,
    source_bindings: tuple[ExperienceExecutionReceiptBinding, ...] | None = None,
    summary_payload_hash: str = "b" * 64,
    experience_id: str = "experience:receipt",
    transition_id: str = "transition:experience",
    accepted_event_ref: str = "event:experience:commit",
    privacy_class: str = "private",
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
) -> ExperienceProjection:
    sources = source_bindings or (binding(),)
    values = ExperienceValues(
        summary_ref="summary:experience",
        summary_payload_hash=summary_payload_hash,
        occurred_from=occurred_from or NOW - timedelta(minutes=5),
        occurred_to=occurred_to or NOW - timedelta(minutes=1),
        participant_refs=("actor:companion",),
        source_bindings=sources,
        privacy_class=privacy_class,
    )
    origin = ExperienceOrigin(
        change_id="change:experience",
        transition_id=transition_id,
        policy_refs=POLICY,
        accepted_event_ref=accepted_event_ref,
    )
    return ExperienceProjection(
        experience_id=experience_id,
        entity_revision=1,
        authority_contract_version="experience.1",
        semantic_fingerprint=experience_semantic_fingerprint(
            values=values, policy_refs=origin.policy_refs
        ),
        values=values,
        origin=origin,
        status="committed",
    )


def mutation(
    value: ExperienceProjection,
    *,
    proposal_id: str,
    evaluated_world_revision: int,
    evidence_refs: tuple[EvidenceRef, ...] | None = None,
) -> ExperienceCommittedPayload:
    raw = {
        "change_id": value.origin.change_id,
        "transition_id": value.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": evidence_refs or (evidence(binding()),),
        "policy_refs": POLICY,
        "acceptance_id": f"acceptance:{proposal_id}",
        "proposal_id": proposal_id,
        "evaluated_world_revision": evaluated_world_revision,
        "accepted_change_hash": "0" * 64,
        "experience": value,
    }
    raw["accepted_change_hash"] = experience_mutation_hash(raw)
    return ExperienceCommittedPayload.model_validate(raw)


def proposal(value: ExperienceCommittedPayload) -> ExperienceProposalProjection:
    return ExperienceProposalProjection(
        proposal_id=value.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:experience.1",
        transition_kind="commit",
        change_id=value.change_id,
        transition_id=value.transition_id,
        evaluated_world_revision=value.evaluated_world_revision,
        expected_entity_revision=0,
        proposed_change_hash=value.accepted_change_hash,
        evidence_refs=value.evidence_refs,
        policy_refs=value.policy_refs,
        proposed_mutation=ExperienceProposedMutation(
            event_type="ExperienceCommitted",
            payload_json=json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )


def initialized(kind=WorldLedger.in_memory):
    ledger = kind(world_id=WORLD)
    ledger.commit(
        [event("world:start", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("clock:start", "ClockAdvanced", {
            "logical_time_from": (NOW - timedelta(minutes=10)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        })],
        expected_world_revision=1,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("budget:account", "BudgetAccountConfigured", {
            "account": BudgetAccount(
                account_id="account:experience",
                category="tool",
                window_id="window:experience",
                limit=10,
            ).model_dump(mode="json")
        })],
        expected_world_revision=2,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("budget:reservation", "BudgetReserved", {
            "reservation": BudgetReservation(
                reservation_id="reservation:experience",
                account_id="account:experience",
                action_id="action:experience",
                category="tool",
                amount_limit=1,
            ).model_dump(mode="json")
        })],
        expected_world_revision=3,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("action:authorized", "ActionAuthorized", {
            "action": action().model_dump(mode="json")
        })],
        expected_world_revision=4,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("action:cancelled", "ActionCancelled", {
            "action_id": "action:experience"
        })],
        expected_world_revision=5,
        expected_deliberation_revision=0,
    )
    ledger.commit(
        [event("receipt:recorded", "ExecutionReceiptRecorded", {
            "receipt": receipt().model_dump(mode="json")
        })],
        expected_world_revision=6,
        expected_deliberation_revision=0,
    )
    return ledger


def seed_second_receipt_authority(
    ledger: WorldLedger,
) -> tuple[Action, ExecutionReceipt]:
    second_action = action().model_copy(update={
        "action_id": "action:second",
        "payload_hash": "d" * 64,
        "idempotency_key": "action:second:idempotency",
        "budget_reservation_id": "reservation:second",
    })
    second_receipt = receipt().model_copy(update={
        "receipt_id": "receipt:second",
        "result_id": "result:second",
        "action_id": second_action.action_id,
        "provider_ref": "provider-ref:second",
        "source_event_id": "source-event:second",
        "raw_payload_hash": "e" * 64,
    })
    projected = ledger.project()
    ledger.commit(
        [event("budget:reservation:second", "BudgetReserved", {
            "reservation": BudgetReservation(
                reservation_id="reservation:second",
                account_id="account:experience",
                action_id=second_action.action_id,
                category="tool",
                amount_limit=1,
            ).model_dump(mode="json")
        })],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    for event_id, event_type, payload in (
        ("action:authorized:second", "ActionAuthorized", {
            "action": second_action.model_dump(mode="json")
        }),
        ("action:cancelled:second", "ActionCancelled", {
            "action_id": second_action.action_id
        }),
        ("receipt:recorded:second", "ExecutionReceiptRecorded", {
            "receipt": second_receipt.model_dump(mode="json")
        }),
    ):
        projected = ledger.project()
        ledger.commit(
            [event(event_id, event_type, payload)],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )
    return second_action, second_receipt


def record_accept_mutate(ledger: WorldLedger, value: ExperienceCommittedPayload) -> None:
    projected = ledger.project()
    ledger.commit(
        [event(f"event:{value.proposal_id}", "ProposalRecorded", proposal(value).model_dump(mode="json"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    ledger.commit(
        [
            event(f"event:{value.acceptance_id}", "AcceptanceRecorded", {
                "acceptance_id": value.acceptance_id,
                "status": "accepted",
                "proposal_id": value.proposal_id,
                "evaluated_world_revision": value.evaluated_world_revision,
                "accepted_change_id": value.change_id,
                "accepted_change_hash": value.accepted_change_hash,
            }),
            event(
                value.experience.origin.accepted_event_ref,
                "ExperienceCommitted",
                value.model_dump(mode="json"),
            ),
        ],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )


def test_experience_commit_is_typed_exact_immutable_and_zero_cascade() -> None:
    ledger = initialized()
    value = mutation(
        experience(),
        proposal_id="proposal:experience",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_accept_mutate(ledger, value)
    projected = ledger.project()
    assert projected.experiences == (value.experience,)
    assert projected.facts == projected.threads == projected.commitments == ()
    assert projected.actions == (action().model_copy(update={"state": "cancelled"}),)
    assert projected.affect_episodes == ()


def test_experience_rejects_receipt_alias_and_duplicate_source_identity() -> None:
    ledger = initialized()
    aliased = mutation(
        experience(source_bindings=(binding(receipt_id="result:experience"),)),
        proposal_id="proposal:receipt-alias",
        evaluated_world_revision=ledger.project().world_revision,
    )
    projected = ledger.project()
    ledger.commit(
        [event("event:proposal:receipt-alias", "ProposalRecorded", proposal(aliased).model_dump(mode="json"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="receipt authority"):
        ledger.commit(
            [
                event("event:acceptance:receipt-alias", "AcceptanceRecorded", {
                    "acceptance_id": aliased.acceptance_id,
                    "status": "accepted",
                    "proposal_id": aliased.proposal_id,
                    "evaluated_world_revision": aliased.evaluated_world_revision,
                    "accepted_change_id": aliased.change_id,
                    "accepted_change_hash": aliased.accepted_change_hash,
                }),
                event(
                    aliased.experience.origin.accepted_event_ref,
                    "ExperienceCommitted",
                    aliased.model_dump(mode="json"),
                ),
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )
    with pytest.raises(ValueError, match="at most 1 item"):
        experience(source_bindings=(binding(), binding()))


def test_experience_rejects_duplicate_transition_identity_before_live_append() -> None:
    ledger = initialized()
    first = mutation(
        experience(),
        proposal_id="proposal:first-transition",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_accept_mutate(ledger, first)

    second_action, second_receipt = seed_second_receipt_authority(ledger)

    duplicate = mutation(
        experience(
            source_bindings=(binding(
                authority=second_receipt,
                action_authority=second_action,
            ),),
            experience_id="experience:second",
            transition_id=first.transition_id,
            accepted_event_ref="event:experience:second",
            summary_payload_hash="f" * 64,
        ),
        proposal_id="proposal:duplicate-transition",
        evaluated_world_revision=ledger.project().world_revision,
    )
    projected = ledger.project()
    ledger.commit(
        [event(
            "event:proposal:duplicate-transition",
            "ProposalRecorded",
            proposal(duplicate).model_dump(mode="json"),
        )],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="transition identity"):
        ledger.commit(
            [
                event("event:acceptance:duplicate-transition", "AcceptanceRecorded", {
                    "acceptance_id": duplicate.acceptance_id,
                    "status": "accepted",
                    "proposal_id": duplicate.proposal_id,
                    "evaluated_world_revision": duplicate.evaluated_world_revision,
                    "accepted_change_id": duplicate.change_id,
                    "accepted_change_hash": duplicate.accepted_change_hash,
                }),
                event(
                    duplicate.experience.origin.accepted_event_ref,
                    "ExperienceCommitted",
                    duplicate.model_dump(mode="json"),
                ),
            ],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_experience_rejects_cross_confused_receipt_hash() -> None:
    ledger = initialized()
    _, second_receipt = seed_second_receipt_authority(ledger)
    confused = binding().model_copy(
        update={"receipt_hash": canonical_hash(second_receipt)}
    )
    value = mutation(
        experience(source_bindings=(confused,)),
        proposal_id="proposal:cross-confused-receipt",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(evidence(binding()),),
    )
    with pytest.raises(ValueError, match="exact receipt authority"):
        record_accept_mutate(ledger, value)


def test_experience_rejects_cross_confused_occurrence_settlement() -> None:
    occurrence_a = WorldOccurrenceProjection(
        occurrence_id="occurrence:a",
        entity_revision=4,
        trigger_ref="trigger:a",
        participant_refs=("actor:companion",),
        location_ref="location:a",
        time_window=DueWindow(
            opens_at=NOW - timedelta(minutes=5),
            closes_at=NOW + timedelta(minutes=1),
        ),
        candidate_outcome_refs=("outcome:a",),
        settled_outcome_ref="outcome:a",
        visibility="private",
        status="settled",
        activated_at=NOW - timedelta(minutes=4),
        result_id="result:a",
        result_payload_ref="payload:a",
        result_payload_hash="a" * 64,
        settled_at=NOW - timedelta(minutes=2),
        settlement_event_ref="event:settlement:a",
        settlement_world_revision=1,
        settlement_payload_hash="b" * 64,
    )
    occurrence_b = occurrence_a.model_copy(update={
        "occurrence_id": "occurrence:b",
        "trigger_ref": "trigger:b",
        "location_ref": "location:b",
        "candidate_outcome_refs": ("outcome:b",),
        "result_id": "result:b",
        "result_payload_ref": "payload:b",
        "result_payload_hash": "c" * 64,
        "settlement_event_ref": "event:settlement:b",
        "settlement_world_revision": 2,
        "settlement_payload_hash": "d" * 64,
    })
    mixed = ExperienceOccurrenceSettlementBinding(
        authority_event_ref=occurrence_a.settlement_event_ref,
        authority_world_revision=occurrence_a.settlement_world_revision,
        authority_payload_hash=occurrence_a.settlement_payload_hash,
        occurrence_id=occurrence_b.occurrence_id,
        occurrence_entity_revision=occurrence_b.entity_revision,
        result_id=occurrence_b.result_id,
        result_payload_ref=occurrence_b.result_payload_ref,
        result_payload_hash=occurrence_b.result_payload_hash,
    )
    authority_a = CommittedWorldEventRef(
        event_id="event:settlement:a",
        event_type="WorldOccurrenceSettled",
        world_revision=1,
        payload_hash="b" * 64,
        logical_time=NOW - timedelta(minutes=2),
    )
    authority_b = CommittedWorldEventRef(
        event_id="event:settlement:b",
        event_type="WorldOccurrenceSettled",
        world_revision=2,
        payload_hash="d" * 64,
        logical_time=NOW - timedelta(minutes=2),
    )
    source = EvidenceRef(
        ref_id=authority_a.event_id,
        evidence_type="settled_world_event",
        claim_purpose="past_experience",
        source_world_revision=authority_a.world_revision,
        immutable_hash=authority_a.payload_hash,
    )
    candidate = mutation(
        experience(source_bindings=(mixed,)),
        proposal_id="proposal:cross-confused-occurrence",
        evaluated_world_revision=2,
        evidence_refs=(source,),
    )
    with pytest.raises(ValueError, match="exact settlement authority"):
        commit_experience(
            (),
            (occurrence_a, occurrence_b),
            (),
            (authority_a, authority_b),
            (),
            (),
            (),
            candidate,
            logical_time=NOW,
        )


@pytest.mark.parametrize("mode", ["missing", "rejected", "non-adjacent"])
def test_experience_requires_accepted_adjacent_family_authority(mode: str) -> None:
    ledger = initialized()
    value = mutation(
        experience(),
        proposal_id=f"proposal:acceptance:{mode}",
        evaluated_world_revision=ledger.project().world_revision,
    )
    projected = ledger.project()
    ledger.commit(
        [event(
            f"event:proposal:acceptance:{mode}",
            "ProposalRecorded",
            proposal(value).model_dump(mode="json"),
        )],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )
    mutation_event = event(
        value.experience.origin.accepted_event_ref,
        "ExperienceCommitted",
        value.model_dump(mode="json"),
    )
    if mode == "missing":
        batch = [mutation_event]
    elif mode == "rejected":
        batch = [
            event(f"event:acceptance:{mode}", "AcceptanceRecorded", {
                "acceptance_id": value.acceptance_id,
                "status": "rejected",
                "proposal_id": value.proposal_id,
                "evaluated_world_revision": value.evaluated_world_revision,
            }),
            mutation_event,
        ]
    else:
        batch = [
            event(f"event:acceptance:{mode}", "AcceptanceRecorded", {
                "acceptance_id": value.acceptance_id,
                "status": "accepted",
                "proposal_id": value.proposal_id,
                "evaluated_world_revision": value.evaluated_world_revision,
                "accepted_change_id": value.change_id,
                "accepted_change_hash": value.accepted_change_hash,
            }),
            event("event:acceptance:intervening", "OperatorObservationRecorded", {
                "observation_id": "operator:intervening",
                "observation_hash": "9" * 64,
            }),
            mutation_event,
        ]
    projected = ledger.project()
    with pytest.raises(ValueError):
        ledger.commit(
            batch,
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_experience_rejects_receipt_authority_with_reversed_chronology() -> None:
    ledger = initialized()
    stored = ledger.project().execution_receipts[0]
    reversed_receipt = stored.model_copy(
        update={"received_at": action().logical_time - timedelta(seconds=1)}
    )
    # The reducer state is deliberately forged here: the Experience reducer
    # must defend itself even if an imported authority snapshot contains a
    # chronologically invalid but otherwise exact receipt.
    ledger._state = ledger._state.model_copy(  # noqa: SLF001
        update={"execution_receipts": (reversed_receipt,)}
    )
    candidate = experience(
        source_bindings=(binding(authority=reversed_receipt),),
        occurred_from=action().logical_time - timedelta(minutes=1),
        occurred_to=NOW - timedelta(minutes=1),
    )
    value = mutation(
        candidate,
        proposal_id="proposal:reversed-receipt",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(evidence(binding(authority=reversed_receipt)),),
    )
    with pytest.raises(ValueError, match="chronology is reversed"):
        record_accept_mutate(ledger, value)


def test_legacy_experience_shape_is_rejected_on_live_append() -> None:
    ledger = initialized()
    legacy = {
        "change_id": "change:legacy",
        "transition_id": "transition:legacy",
        "expected_entity_revision": 0,
        "evidence_refs": [evidence(binding()).model_dump(mode="json")],
        "policy_refs": [],
        "experience": {
            "experience_id": "experience:legacy",
            "entity_revision": 1,
            "summary_ref": "summary:legacy",
            "evidence_refs": [evidence(binding()).model_dump(mode="json")],
            "occurred_from": (NOW - timedelta(minutes=2)).isoformat(),
            "occurred_to": (NOW - timedelta(minutes=1)).isoformat(),
            "participant_refs": ["actor:companion"],
            "result_refs": ["result:experience"],
            "privacy_class": "private",
            "status": "committed",
        },
    }
    projected = ledger.project()
    with pytest.raises(ValueError):
        ledger.commit(
            [event("event:legacy-live", "ExperienceCommitted", legacy)],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


@pytest.mark.parametrize(
    ("candidate", "message"),
    [
        (experience(privacy_class="personal"), "remain private"),
        (
            experience(
                occurred_from=NOW - timedelta(seconds=30),
                occurred_to=NOW,
            ),
            "time window",
        ),
    ],
)
def test_experience_rejects_privacy_or_time_that_does_not_cover_authority(
    candidate: ExperienceProjection, message: str
) -> None:
    ledger = initialized()
    value = mutation(
        candidate,
        proposal_id=f"proposal:invalid:{message.replace(' ', '-')}",
        evaluated_world_revision=ledger.project().world_revision,
    )
    with pytest.raises(ValueError, match=message):
        record_accept_mutate(ledger, value)


def test_experience_source_authority_cannot_be_reused_by_another_experience() -> None:
    ledger = initialized()
    first = mutation(
        experience(),
        proposal_id="proposal:first-source",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_accept_mutate(ledger, first)
    second = mutation(
        experience(
            experience_id="experience:duplicate-source",
            transition_id="transition:duplicate-source",
            accepted_event_ref="event:experience:duplicate-source",
            summary_payload_hash="e" * 64,
        ),
        proposal_id="proposal:duplicate-source",
        evaluated_world_revision=ledger.project().world_revision,
    )
    with pytest.raises(ValueError, match="already committed elsewhere"):
        record_accept_mutate(ledger, second)


def test_committed_experience_evidence_resolves_exact_transition_event() -> None:
    ledger = initialized()
    first = mutation(
        experience(),
        proposal_id="proposal:resolver-base",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_accept_mutate(ledger, first)
    projected = ledger.project()
    transition = projected.experience_transitions[0]
    authority = next(
        item
        for item in projected.committed_world_event_refs
        if item.event_id == transition.accepted_event_ref
    )
    exact = EvidenceRef(
        ref_id=authority.event_id,
        evidence_type="committed_experience",
        claim_purpose="past_experience",
        source_world_revision=authority.world_revision,
        immutable_hash=canonical_hash(transition.values_after),
    )
    followup = mutation(
        experience(
            experience_id="experience:resolver-followup",
            transition_id="transition:resolver-followup",
            accepted_event_ref="event:experience:resolver-followup",
        ),
        proposal_id="proposal:resolver-followup",
        evaluated_world_revision=projected.world_revision,
        evidence_refs=(exact,),
    )
    ledger.commit(
        [event("event:proposal:resolver-followup", "ProposalRecorded", proposal(followup).model_dump(mode="json"))],
        expected_world_revision=projected.world_revision,
        expected_deliberation_revision=projected.deliberation_revision,
    )

    wrong = exact.model_copy(update={"source_world_revision": authority.world_revision - 1})
    rejected = mutation(
        experience(
            experience_id="experience:resolver-rejected",
            transition_id="transition:resolver-rejected",
            accepted_event_ref="event:experience:resolver-rejected",
        ),
        proposal_id="proposal:resolver-rejected",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(wrong,),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="experience evidence hash"):
        ledger.commit(
            [event("event:proposal:resolver-rejected", "ProposalRecorded", proposal(rejected).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_experience_cannot_weaken_committed_experience_actual_privacy() -> None:
    ledger = initialized()
    first = mutation(
        experience(privacy_class="withhold"),
        proposal_id="proposal:withheld-source",
        evaluated_world_revision=ledger.project().world_revision,
    )
    record_accept_mutate(ledger, first)
    projected = ledger.project()
    transition = projected.experience_transitions[0]
    authority = next(
        item
        for item in projected.committed_world_event_refs
        if item.event_id == transition.accepted_event_ref
    )
    source = EvidenceRef(
        ref_id=authority.event_id,
        evidence_type="committed_experience",
        claim_purpose="current_fact",
        source_world_revision=authority.world_revision,
        immutable_hash=canonical_hash(transition.values_after),
    )
    weakened = mutation(
        experience(
            experience_id="experience:weakened",
            transition_id="transition:weakened",
            accepted_event_ref="event:experience:weakened",
            privacy_class="private",
        ),
        proposal_id="proposal:weakened",
        evaluated_world_revision=projected.world_revision,
        evidence_refs=(source,),
    )
    with pytest.raises(ValueError, match="evidence/privacy matrix"):
        record_accept_mutate(ledger, weakened)


def test_experience_cannot_weaken_committed_fact_actual_privacy() -> None:
    source = EvidenceRef(
        ref_id="operator:withheld-fact",
        evidence_type="operator_observation",
        claim_purpose="current_fact",
        immutable_hash="1" * 64,
    )
    assertion = FactAssertionBinding(
        source_kind="operator_observation",
        source_ref=source.ref_id,
        asserted_subject_ref="subject:user",
        content_payload_hash="1" * 64,
    )
    values = FactValues(
        subject_ref="subject:user",
        predicate_code="preference.private",
        cardinality="single",
        conflict_key=fact_conflict_key(
            subject_ref="subject:user", predicate_code="preference.private"
        ),
        value_ref="value:private",
        value_hash="2" * 64,
        assertion_binding=assertion,
        anchor_evidence_refs=(source,),
        source_evidence_refs=(source,),
        confidence_bp=9000,
        privacy_class="withhold",
        status="active",
    )
    origin = FactOrigin(
        change_id="change:withheld-fact",
        transition_id="transition:withheld-fact",
        policy_refs=("policy:fact-v1",),
        accepted_event_ref="event:withheld-fact",
    )
    fact = FactProjection(
        fact_id="fact:withheld",
        entity_revision=1,
        semantic_fingerprint=fact_semantic_fingerprint(
            subject_ref=values.subject_ref,
            predicate_code=values.predicate_code,
            cardinality=values.cardinality,
            conflict_key=values.conflict_key,
            value_hash=values.value_hash,
            assertion_binding=values.assertion_binding,
            anchor_evidence_refs=values.anchor_evidence_refs,
            policy_refs=origin.policy_refs,
        ),
        values=values,
        origin=origin,
        committed_at=NOW - timedelta(minutes=3),
        updated_at=NOW - timedelta(minutes=3),
    )
    fact_evidence = EvidenceRef(
        ref_id=origin.accepted_event_ref,
        evidence_type="committed_fact",
        claim_purpose="current_fact",
        source_world_revision=1,
        immutable_hash="3" * 64,
    )
    candidate = mutation(
        experience(privacy_class="private"),
        proposal_id="proposal:weaken-fact",
        evaluated_world_revision=1,
        evidence_refs=(fact_evidence,),
    )
    with pytest.raises(ValueError, match="evidence/privacy matrix"):
        commit_experience(
            (),
            (),
            (),
            (),
            (receipt(),),
            (action().model_copy(update={"state": "cancelled"}),),
            (fact,),
            candidate,
            logical_time=NOW,
        )


@pytest.mark.parametrize("source_kind", ["active_plan", "settled_world_event"])
def test_experience_cannot_weaken_plan_or_occurrence_actual_privacy(
    source_kind: str,
) -> None:
    authority_ref = f"authority:{source_kind}"
    source = EvidenceRef(
        ref_id=authority_ref,
        evidence_type=source_kind,
        claim_purpose="current_fact",
        source_world_revision=1 if source_kind == "settled_world_event" else None,
        immutable_hash="4" * 64,
    )
    plan = PlanStateProjection(
        plan_id=authority_ref,
        activity_id="activity:withheld",
        entity_revision=1,
        activity_kind="private_activity",
        evidence_refs=(EvidenceRef(
            ref_id="operator:plan",
            evidence_type="operator_observation",
            claim_purpose="future_plan",
            immutable_hash="5" * 64,
        ),),
        status="active",
        importance_bp=5000,
        privacy_class="withhold",
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id="occurrence:withheld",
        entity_revision=3,
        trigger_ref="trigger:withheld",
        participant_refs=("actor:companion",),
        location_ref="location:private",
        time_window=DueWindow(
            opens_at=NOW - timedelta(minutes=5),
            closes_at=NOW + timedelta(minutes=5),
        ),
        candidate_outcome_refs=("outcome:withheld",),
        settled_outcome_ref="outcome:withheld",
        visibility="withhold",
        status="settled",
        activated_at=NOW - timedelta(minutes=3),
        result_id="result:withheld",
        result_payload_ref="payload:withheld",
        result_payload_hash="6" * 64,
        settled_at=NOW - timedelta(minutes=1),
        settlement_event_ref=authority_ref,
        settlement_world_revision=1,
        settlement_payload_hash="4" * 64,
    )
    candidate = mutation(
        experience(privacy_class="private"),
        proposal_id=f"proposal:weaken:{source_kind}",
        evaluated_world_revision=1,
        evidence_refs=(source,),
    )
    with pytest.raises(ValueError, match="evidence/privacy matrix"):
        commit_experience(
            (),
            (occurrence,) if source_kind == "settled_world_event" else (),
            (plan,) if source_kind == "active_plan" else (),
            (),
            (receipt(),),
            (action().model_copy(update={"state": "cancelled"}),),
            (),
            candidate,
            logical_time=NOW,
        )


def test_experience_proposal_rejects_future_settlement_as_current_evidence() -> None:
    ledger = initialized()
    future = EvidenceRef(
        ref_id="event:future-settlement",
        evidence_type="settled_world_event",
        claim_purpose="past_experience",
        source_world_revision=ledger.project().world_revision + 2,
        immutable_hash="f" * 64,
    )
    value = mutation(
        experience(),
        proposal_id="proposal:future-evidence",
        evaluated_world_revision=ledger.project().world_revision,
        evidence_refs=(future,),
    )
    projected = ledger.project()
    with pytest.raises(ValueError, match="world-event evidence"):
        ledger.commit(
            [event("event:proposal:future-evidence", "ProposalRecorded", proposal(value).model_dump(mode="json"))],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )


def test_sqlite_migrates_nonempty_v12_legacy_experience_without_fabricated_lineage(
    tmp_path,
) -> None:
    path = tmp_path / "experience-v12.sqlite3"
    ledger = initialized(
        lambda *, world_id: SQLiteWorldLedger(path=path, world_id=world_id)
    )
    ledger.close()

    unpinned = {
        "ref_id": "event:older-experience",
        "evidence_type": "committed_experience",
        "claim_purpose": "past_experience",
        "source_world_revision": None,
        "immutable_hash": None,
    }
    legacy_experience = LegacyExperienceProjection(
        experience_id="experience:legacy-v12",
        entity_revision=1,
        summary_ref="summary:legacy-v12",
        evidence_refs=(unpinned,),
        occurred_from=NOW - timedelta(minutes=4),
        occurred_to=NOW - timedelta(minutes=1),
        participant_refs=("actor:companion",),
        result_refs=("result:experience",),
        privacy_class="private",
    )
    old_experience = legacy_experience.model_dump(mode="json")
    old_experience.pop("authority_contract_version")
    old_experience["status"] = "committed"
    old_payload = {
        "change_id": "change:legacy-v12",
        "transition_id": "transition:legacy-v12",
        "expected_entity_revision": 0,
        "evidence_refs": [unpinned],
        "policy_refs": ["policy:experience-v0"],
        "experience": old_experience,
    }
    old_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:legacy-experience-v12",
        world_id=WORLD,
        event_type="ExperienceCommitted",
        logical_time=NOW,
        created_at=NOW,
        actor="system:legacy",
        source="legacy-test",
        trace_id="trace:legacy-experience",
        causation_id="cause:legacy-experience",
        correlation_id="correlation:legacy-experience",
        idempotency_key="legacy-experience-v12:idempotency",
        payload=old_payload,
    )

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT world_revision, deliberation_revision, ledger_sequence, state_json "
            "FROM world_v2_heads WHERE world_id = ?",
            (WORLD,),
        ).fetchone()
        assert row is not None
        world_revision, deliberation_revision, ledger_sequence, state_json = row
        current = ReducerState.model_validate_json(state_json)
        next_world_revision = int(world_revision) + 1
        next_sequence = int(ledger_sequence) + 1
        old_ref = CommittedWorldEventRef(
            event_id=old_event.event_id,
            event_type=old_event.event_type,
            world_revision=next_world_revision,
            payload_hash=old_event.payload_hash,
            logical_time=old_event.logical_time,
        )
        legacy_state = current.model_copy(update={
            "experiences": (*current.experiences, legacy_experience),
            "committed_world_event_refs": (
                *current.committed_world_event_refs,
                old_ref,
            ),
        })
        legacy_semantic = legacy_state.semantic_payload(
            world_id=WORLD,
            world_revision=next_world_revision,
            reducer_bundle_version="world-v2-reducers.12",
        )
        legacy_digest = hashlib.sha256(json.dumps(
            legacy_semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()).hexdigest()
        raw_state = legacy_state.model_dump(mode="json")
        strip_v16_state_fields(raw_state)
        raw_state["experiences"][0].pop("authority_contract_version")
        raw_state["experiences"][0]["status"] = "committed"
        raw_state.pop("experience_transitions")
        raw_state.pop("experience_proposals")
        raw_state.pop("experience_proposal_ids")

        commit_id = "commit:legacy-experience-v12"
        result = CommitResult(
            world_revision=next_world_revision,
            deliberation_revision=int(deliberation_revision),
            ledger_sequence=next_sequence,
            event_ids=(old_event.event_id,),
        )
        event_json = canonical_event_json(old_event)
        connection.execute(
            "INSERT INTO world_v2_commits "
            "(world_id, commit_id, request_hash, result_json) VALUES (?, ?, ?, ?)",
            (
                WORLD,
                commit_id,
                commit_request_hash((old_event,)),
                result.model_dump_json(),
            ),
        )
        connection.execute(
            "INSERT INTO world_v2_events "
            "(world_id, ledger_sequence, world_revision, deliberation_revision, "
            "commit_id, event_id, idempotency_key, event_json, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                WORLD,
                next_sequence,
                next_world_revision,
                int(deliberation_revision),
                commit_id,
                old_event.event_id,
                old_event.idempotency_key,
                event_json,
                hashlib.sha256(event_json.encode()).hexdigest(),
            ),
        )
        connection.execute(
            "UPDATE world_v2_heads SET world_revision = ?, ledger_sequence = ?, "
            "state_json = ?, semantic_hash = ?, reducer_bundle_version = ?, "
            "state_hash = '' WHERE world_id = ?",
            (
                next_world_revision,
                next_sequence,
                json.dumps(raw_state, ensure_ascii=False, separators=(",", ":")),
                legacy_digest,
                "world-v2-reducers.12",
                WORLD,
            ),
        )

    migrated = SQLiteWorldLedger(path=path, world_id=WORLD)
    projected = migrated.project()
    assert projected.reducer_bundle_version == "world-v2-reducers.21"
    assert len(projected.experiences) == 1
    assert isinstance(projected.experiences[0], LegacyExperienceProjection)
    assert projected.experiences[0].authority_contract_version == "legacy-unverified"
    assert projected.experiences[0].status == "legacy-unverified"
    assert projected.experience_transitions == ()
    assert migrated.rebuild() == projected

    legacy_authority = next(
        item
        for item in projected.committed_world_event_refs
        if item.event_id == old_event.event_id
    )
    rejected_evidence = EvidenceRef(
        ref_id=legacy_authority.event_id,
        evidence_type="committed_experience",
        claim_purpose="past_experience",
        source_world_revision=legacy_authority.world_revision,
        immutable_hash=legacy_authority.payload_hash,
    )
    rejected = mutation(
        experience(
            experience_id="experience:from-legacy",
            transition_id="transition:from-legacy",
            accepted_event_ref="event:experience:from-legacy",
        ),
        proposal_id="proposal:from-legacy",
        evaluated_world_revision=projected.world_revision,
        evidence_refs=(rejected_evidence,),
    )
    with pytest.raises(ValueError, match="experience evidence"):
        migrated.commit(
            [event(
                "event:proposal:from-legacy",
                "ProposalRecorded",
                proposal(rejected).model_dump(mode="json"),
            )],
            expected_world_revision=projected.world_revision,
            expected_deliberation_revision=projected.deliberation_revision,
        )
    migrated.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    assert reopened.rebuild() == reopened.project()
    assert reopened.project().experience_transitions == ()
    reopened.close()
