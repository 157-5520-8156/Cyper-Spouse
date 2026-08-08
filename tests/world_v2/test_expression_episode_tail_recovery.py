"""Upgrade and replay compatibility for the retired two-author episode."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.deliberation import (
    DeliberationResult,
    ModelResultAudit,
    ModelRoute,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.appraisal_trigger import interaction_appraisal_trigger_events
from companion_daemon.world_v2.expression_episode_lifecycle import (
    expression_episode_claim_event,
    expression_episode_open_event,
)
from companion_daemon.world_v2.expression_plan_acceptance import (
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from companion_daemon.world_v2.expression_plan_atomic_recorder import (
    ExpressionPlanAtomicRecorder,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit import (
    ProposalAuditContext,
    ProposalAuditRecorder,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.reducers import (
    RETIRED_EXPRESSION_EPISODE_OUTCOME,
)
from companion_daemon.world_v2.schemas import BudgetAccount, Observation, ProjectionCursor, TriggerProcess, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
WORLD = "world:historical-expression-episode"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _raw_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=WORLD,
        payload=payload,
    )
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace:historical-expression-episode",
        causation_id="cause:historical-expression-episode",
        correlation_id="correlation:historical-expression-episode",
        idempotency_key=identity or f"idempotency:{event_id}",
        payload=payload,
    )


def _observation(*, suffix: str = "historical-expression-episode") -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id=f"observation:{suffix}",
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:historical-expression-episode",
        causation_id=f"qq:{suffix}",
        correlation_id="correlation:historical-expression-episode",
        source="platform:qq",
        source_event_id=f"qq:{suffix}",
        actor="user:historical",
        channel="qq",
        payload_ref="ingress:historical-expression-episode",
        payload_hash=_hash("historical message"),
        text="历史消息",
        received_at=NOW,
    )


def _observation_event(observation: Observation) -> WorldEvent:
    payload = observation.model_dump(mode="json")
    identity = domain_idempotency_key(
        event_type="ObservationRecorded", world_id=WORLD, payload=payload
    )
    assert identity is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:{observation.observation_id}",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor=observation.actor,
        source=observation.source,
        trace_id=observation.trace_id,
        causation_id=observation.causation_id,
        correlation_id=observation.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


def _seed_episode(*, claimed: bool, with_budget: bool = False):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD, accepted_batch_issuer=issuer)
    ledger.commit(
        (_event("event:world:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    if with_budget:
        account = BudgetAccount(
            account_id="account:historical-expression",
            category="chat",
            window_id="window:historical-expression",
            limit=1_000,
        )
        ledger.commit(
            (
                _event(
                    "event:budget:historical-expression",
                    "BudgetAccountConfigured",
                    {"account": account.model_dump(mode="json")},
                ),
            ),
            expected_world_revision=1,
            expected_deliberation_revision=0,
        )
    observation = _observation()
    observed = _observation_event(observation)
    opened_event = expression_episode_open_event(
        observation=observation,
        observation_event=observed,
    )
    opened = TriggerProcess.model_validate_json(
        json.dumps(opened_event.payload()["process"])
    )
    events = [observed, opened_event]
    active = opened
    if claimed:
        claimed_event, active = expression_episode_claim_event(
            world_id=WORLD,
            process=opened,
            owner_id="worker:historical-expression-episode",
            at=NOW,
            trace_id=observation.trace_id,
            correlation_id=observation.correlation_id,
        )
        events.append(claimed_event)
    before = ledger.project()
    ledger.commit(
        events,
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    return ledger, issuer, observation, observed, active


@pytest.mark.parametrize("claimed", (False, True))
def test_upgrade_terminalizes_preexisting_open_and_claimed_episode_once(claimed: bool) -> None:
    ledger, _issuer, _observation_value, _observed, active = _seed_episode(claimed=claimed)
    immutable_before = tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in ledger.export_replay_evidence().events
    )

    projection = ledger.project()
    rebuilt = ledger.rebuild()
    immutable_after = tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in ledger.export_replay_evidence().events
    )
    terminal = next(item for item in projection.trigger_processes if item.trigger_id == active.trigger_id)
    assert terminal.state == "terminal"
    assert terminal.runtime_outcome_ref == RETIRED_EXPRESSION_EPISODE_OUTCOME + ":no-disposition"
    assert rebuilt == projection
    assert immutable_after == immutable_before


def _historical_cancel_proposal(*, trigger_ref: str, evaluated_world_revision: int) -> DecisionProposal:
    text = "这条旧尾巴不应在升级后发送。"
    payload_hash = _hash(text)
    return DecisionProposal(
        proposal_id="proposal:historical-expression-tail:1",
        trigger_ref=trigger_ref,
        evaluated_world_revision=evaluated_world_revision,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=trigger_ref,
                evidence_kind="observed_message",
                source_world_revision=evaluated_world_revision,
                immutable_hash=_hash("historical source"),
            ),
        ),
        proposed_changes=(
            TypedChange(
                change_id="change:historical-expression-tail:1",
                kind="expression_plan_transition",
                target_id="plan:historical-expression-tail:1",
                transition="accept",
                evidence_refs=(trigger_ref,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="expression_plan_transition.v1",
                    value={
                        "plan_id": "plan:historical-expression-tail:1",
                        "overall_intent": "historical provisional/full tail",
                        "ordering_policy": "dependencies",
                        "terminal_policy": "settle_after_terminal_beats",
                        "beat_drafts": [
                            {
                                "beat_id": "beat:historical-expression-tail:1",
                                "inline_text": text,
                                "materialized_payload_ref": "payload:historical-expression-tail:1",
                                "payload_hash": payload_hash,
                                "content_type": "text/plain",
                                "dependency_beat_ids": [],
                                "delay_window": None,
                                "cancel_policy": "cancel-before-dispatch",
                                "reconsider_policy": "reconsider-on-new-observation",
                                "merge_policy": "never",
                            }
                        ],
                    },
                ),
            ),
        ),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:historical-expression-tail:1",
                kind="reply",
                layer="external_action",
                target="user:historical",
                payload_ref="payload:historical-expression-tail:1",
                payload_hash=payload_hash,
                causal_change_id="change:historical-expression-tail:1",
                beat_ref="beat:historical-expression-tail:1",
            ),
        ),
        confidence=8000,
        brief_rationale="Historical tail chose to cancel its pending visible effect.",
        behavior_tendency="engage",
        stance="warm",
        display_strategy="paced",
        episode_disposition="cancel_pending",
    )


def _record_historical_proposal(ledger: WorldLedger, proposal: DecisionProposal) -> None:
    model_call_id = "model-call:historical-expression-tail:1"
    response_hash = _raw_hash("historical response")
    audit = ModelResultAudit(
        model_call_id=model_call_id,
        model_result_ref="model-result:"
        + _digest({"model_call_id": model_call_id, "response_hash": response_hash}),
        attempt_id="attempt:historical-expression-tail:1",
        route=ModelRoute(tier="flash", reason_code="ordinary", router_version="router.1"),
        model_id="model:historical",
        model_version="1",
        request_hash=_raw_hash("historical request"),
        response_hash=response_hash,
        status="proposal_validated",
    )
    capsule_id = _raw_hash("historical capsule")
    result = DeliberationResult(
        result_id="deliberation:"
        + _digest(
            {
                "capsule_id": capsule_id,
                "proposal_hash": proposal.proposal_hash,
                "attempt_audits": [audit.model_dump(mode="json")],
            }
        ),
        capsule_id=capsule_id,
        proposal=proposal,
        audit=audit,
        attempt_audits=(audit,),
    )
    before = ledger.project()
    ProposalAuditRecorder(ledger=ledger).record(
        result,
        ProposalAuditContext(
            world_id=WORLD,
            trigger_ref=proposal.trigger_ref,
            logical_time=NOW,
            created_at=NOW,
            actor="character:historical",
            source="world-v2:historical-expression",
            trace_id="trace:historical-expression-episode",
            causation_id=proposal.trigger_ref,
            correlation_id="correlation:historical-expression-episode",
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_commit_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
            expected_ledger_sequence=before.ledger_sequence,
        ),
    )


def test_upgrade_honors_recorded_cancel_disposition_and_releases_action_budget() -> None:
    ledger, issuer, observation, observed, active = _seed_episode(
        claimed=True,
        with_budget=True,
    )
    proposal = _historical_cancel_proposal(
        trigger_ref=observed.event_id,
        evaluated_world_revision=ledger.project().world_revision,
    )
    _record_historical_proposal(ledger, proposal)
    audited = ledger.project()
    audit = next(item for item in audited.proposal_audits if item.proposal_id == proposal.proposal_id)
    policy = ExpressionPlanBudgetPolicy(
        account_id="account:historical-expression",
        amount_limit_per_action=100,
        actor="character:historical",
        allowed_targets=("user:historical",),
        recovery_policy="effect_once",
    )
    material = derive_expression_plan_material(
        audit=audit,
        cursor=ProjectionCursor(
            world_revision=audited.world_revision,
            deliberation_revision=audited.deliberation_revision,
            ledger_sequence=audited.ledger_sequence,
        ),
        world_id=WORLD,
        policy=policy,
        account=next(item for item in audited.budget_accounts if item.account_id == policy.account_id),
        logical_time=NOW,
        created_at=NOW,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
        source_observation=observation,
    )
    handle = ExpressionPlanAtomicRecorder(batch_issuer=issuer).prepare_batch(
        acceptance_id="acceptance:historical-expression-tail:1",
        material=material,
        actor="character:historical",
        source="world-v2:historical-expression",
    )
    ledger.commit_accepted(handle, expected_cursor=material.cursor)
    immutable_before = tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in ledger.export_replay_evidence().events
    )
    after = ledger.project()
    action = next(item for item in after.actions if item.expression_plan_id == material.plan_id)
    reservation = next(
        item for item in after.budget_reservations if item.reservation_id == action.budget_reservation_id
    )
    settled_action = next(item for item in after.actions if item.action_id == action.action_id)
    settled_reservation = next(
        item for item in after.budget_reservations if item.reservation_id == reservation.reservation_id
    )
    terminal = next(item for item in after.trigger_processes if item.trigger_id == active.trigger_id)
    assert settled_action.state == "cancelled"
    assert settled_reservation.state == "released"
    assert next(item for item in after.budget_accounts if item.account_id == policy.account_id).reserved == 0
    assert next(item for item in after.expression_plans if item.plan_id == material.plan_id).state == "terminated"
    assert next(item for item in after.expression_beats if item.plan_id == material.plan_id).state == "terminated"
    assert terminal.runtime_outcome_ref == (
        RETIRED_EXPRESSION_EPISODE_OUTCOME + ":recorded-disposition:cancel_pending"
    )
    evidence = ledger.export_replay_evidence()
    assert evidence.projection == evidence.replay
    assert tuple(
        (item.event.event_id, item.event_envelope_hash) for item in evidence.events
    ) == immutable_before


def test_character_interior_stream_episode_is_not_folded_as_legacy() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD)
    ledger.commit(
        (_event("event:world:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation(suffix="current-stream-after-upgrade")
    observed = _observation_event(observation)
    opened = expression_episode_open_event(
        observation=observation,
        observation_event=observed,
    )
    opened_process = TriggerProcess.model_validate_json(
        json.dumps(opened.payload()["process"])
    )
    claimed, active = expression_episode_claim_event(
        world_id=WORLD,
        process=opened_process,
        owner_id="worker:current-stream",
        at=NOW,
        trace_id=observation.trace_id,
        correlation_id=observation.correlation_id,
    )
    appraisal_events = interaction_appraisal_trigger_events(
        observation=observation,
        observation_event=observed,
        owner_id="worker:character-interior",
    )
    before = ledger.project()
    ledger.commit(
        (observed, opened, claimed, *appraisal_events),
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )

    stream_episode = next(
        item
        for item in ledger.project().trigger_processes
        if item.source_evidence_ref == observation.observation_id
        and item.process_kind == "expression_episode"
    )
    assert stream_episode == active
    assert stream_episode.state == "claimed"


def test_sqlite_reopen_rebuilds_same_fold_without_touching_event_history(tmp_path) -> None:
    path = tmp_path / "retired-expression-episode.sqlite"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    ledger.commit(
        (_event("event:world:start", "WorldStarted", {}),),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    observation = _observation(suffix="sqlite-upgrade-open")
    observed = _observation_event(observation)
    opened = expression_episode_open_event(
        observation=observation,
        observation_event=observed,
    )
    before = ledger.project()
    ledger.commit(
        (observed, opened),
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    immutable_before = tuple(
        (item.event.event_id, item.event_envelope_hash)
        for item in ledger.export_replay_evidence().events
    )
    expected = ledger.project()
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    try:
        projection = reopened.project()
        assert projection == expected
        assert reopened.rebuild() == projection
        assert tuple(
            (item.event.event_id, item.event_envelope_hash)
            for item in reopened.export_replay_evidence().events
        ) == immutable_before
    finally:
        reopened.close()
