from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.media_preview_conductor import MediaPreviewConductorResult
from companion_daemon.world_v2.media_request_runtime import (
    MediaRequestRuntime,
    media_request_trigger_id,
)
from companion_daemon.world_v2.minimal_reply_events import ExpressionPlanAcceptedPayload
from companion_daemon.world_v2.private_turn_state import PrivateTurnState
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.reducers import ReducerState, reduce_event
from companion_daemon.world_v2.schemas import (
    Action,
    ClaimLease,
    CommittedWorldEventRef,
    ExpressionPlanManifestRef,
    ExpressionPlanProjection,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
WORLD = "world:media-request-test"


def _event(
    event_type: str,
    payload: dict[str, object],
    *,
    event_id: str,
    causation_id: str = "event:source",
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        event_type=event_type,
        world_id=WORLD,
        logical_time=NOW,
        created_at=NOW,
        actor="agent:companion",
        source="test:media-request",
        trace_id="trace:media-request",
        causation_id=causation_id,
        correlation_id="correlation:reply",
        idempotency_key=event_id,
        payload=payload,
    )


def _accepted_plan_event() -> WorldEvent:
    payload = ExpressionPlanAcceptedPayload(
        acceptance_id="acceptance:reply",
        proposal_id="proposal:reply",
        expression_change_id="change:reply",
        plan_id="plan:reply",
        media_request="consider_available_candidate",
    )
    return _event(
        "ExpressionPlanAccepted",
        payload.model_dump(mode="json"),
        event_id="event:expression-plan:accepted",
    )


def _proposal_audit(
    *, attended_source_ref: str, authorize_media_source: bool = True
) -> SimpleNamespace:
    proposal = DecisionProposal(
        proposal_id="proposal:reply",
        trigger_ref="event:observation:reply",
        evaluated_world_revision=1,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id=attended_source_ref,
                evidence_kind="settled_world_event",
                source_world_revision=1,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        proposed_changes=(
            TypedChange(
                change_id="change:reply",
                kind="expression_plan_transition",
                target_id="plan:reply",
                transition="accept",
                evidence_refs=(attended_source_ref,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="expression_plan_transition.v1",
                    value={
                        "plan_id": "plan:reply",
                        "overall_intent": "expression:now",
                        "ordering_policy": "dependencies",
                        "terminal_policy": "settle",
                        "beat_drafts": [
                            {
                                "beat_id": "beat:reply",
                                "inline_text": "I may share this moment.",
                                "materialized_payload_ref": "payload:reply",
                                "payload_hash": "sha256:" + "c" * 64,
                                "content_type": "text/plain",
                                "dependency_beat_ids": [],
                                "delay_window": None,
                                "cancel_policy": "cancel-before-dispatch",
                                "reconsider_policy": "reconsider-on-new-observation",
                                "merge_policy": "model-reconsider",
                            }
                        ],
                        "media_request": "consider_available_candidate",
                        "media_source_refs": (
                            [attended_source_ref] if authorize_media_source else []
                        ),
                    },
                ),
            ),
        ),
        confidence=8_000,
        brief_rationale="the current lived moment is worth considering visually",
        behavior_tendency="share_if_a_real_candidate_is_available",
        stance="interested",
        display_strategy="ordinary_reply",
        private_turn_state=PrivateTurnState(
            inner_state_summary="I want to consider sharing this exact moment.",
            attended_source_refs=(attended_source_ref,),
        ),
    )
    return SimpleNamespace(
        proposal_id=proposal.proposal_id,
        proposal_json=json.dumps(proposal.model_dump(mode="json")),
    )


class _Conductor:
    def __init__(
        self,
        result: MediaPreviewConductorResult | None = None,
        *,
        ledger: _Ledger | None = None,
    ) -> None:
        self.calls = 0
        self._result = result or MediaPreviewConductorResult(status="planned")
        self._ledger = ledger

    async def advance_once(self, **kwargs: object) -> MediaPreviewConductorResult:
        self.calls += 1
        if self._result.status == "planned" and self._ledger is not None:
            self._ledger._events.append(  # noqa: SLF001 - event-producing conductor fixture
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=f"event:media-plan:recorded:{self.calls}",
                    event_type="MediaPlanRecorded",
                    world_id=WORLD,
                    logical_time=NOW,
                    created_at=NOW,
                    actor="agent:companion",
                    source="test:media-request",
                    trace_id=str(kwargs["trace_id"]),
                    causation_id="event:selection",
                    correlation_id=str(kwargs["correlation_id"]),
                    idempotency_key=f"event:media-plan:recorded:{self.calls}",
                    payload={"test_only": True},
                )
            )
        return self._result


class _CandidateSupplier:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str, str]] = []

    def request_once(
        self,
        *,
        source_refs: tuple[str, ...],
        trace_id: str,
        correlation_id: str,
    ) -> object:
        self.calls.append((source_refs, trace_id, correlation_id))
        return SimpleNamespace(status="declared")


class _Ledger:
    blocks_event_loop = False
    world_id = WORLD

    def __init__(
        self, source: WorldEvent, *, proposal_audits: tuple[SimpleNamespace, ...] = ()
    ) -> None:
        lease = ClaimLease(
            owner_id="worker:action",
            attempt_id="attempt:action",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )
        self._source = source
        self._projection = SimpleNamespace(
            world_revision=1,
            deliberation_revision=0,
            ledger_sequence=1,
            logical_time=NOW,
            expression_plans=(
                ExpressionPlanProjection(
                    acceptance_id="acceptance:reply",
                    proposal_id="proposal:reply",
                    expression_change_id="change:reply",
                    plan_id="plan:reply",
                    event_ref=source.event_id,
                    event_payload_hash=source.payload_hash,
                ),
            ),
            actions=(
                Action(
                    schema_version="world-v2.1",
                    action_id="action:reply",
                    world_id=WORLD,
                    logical_time=NOW,
                    created_at=NOW,
                    trace_id="trace:reply",
                    causation_id="event:expression-plan:accepted",
                    correlation_id="correlation:reply",
                    kind="reply",
                    layer="external_action",
                    intent_ref="intent:reply",
                    actor="agent:companion",
                    target="user:geoff",
                    payload_ref="payload:reply",
                    payload_hash="sha256:" + "a" * 64,
                    expression_plan_id="plan:reply",
                    expression_beat_id="beat:reply",
                    idempotency_key="action:reply",
                    budget_reservation_id="reservation:reply",
                    claim_lease=lease,
                    state="provider_accepted",
                    recovery_policy="result_lookup",
                ),
            ),
            trigger_processes=(),
            completed_trigger_ids=(),
            proposal_audits=proposal_audits,
        )
        self._events: list[WorldEvent] = [source]
        self.fail_completion_once = False

    def project(self):  # type: ignore[no-untyped-def]
        return self._projection

    def lookup_event_commit(self, event_id: str):  # type: ignore[no-untyped-def]
        event = next((item for item in self._events if item.event_id == event_id), None)
        return None if event is None else (event, SimpleNamespace())

    def recent_events_by_type(
        self, *, event_types: frozenset[str], since: datetime, limit: int
    ) -> tuple[WorldEvent, ...]:
        return tuple(
            item
            for item in self._events[-limit:]
            if item.event_type in event_types and item.logical_time >= since
        )

    def commit_at_cursor(
        self,
        events: tuple[WorldEvent, ...],
        *,
        expected_cursor: ProjectionCursor,
        commit_id: str,
    ) -> None:
        del commit_id
        assert expected_cursor == ProjectionCursor(
            world_revision=self._projection.world_revision,
            deliberation_revision=self._projection.deliberation_revision,
            ledger_sequence=self._projection.ledger_sequence,
        )
        processes = list(self._projection.trigger_processes)
        completed = list(self._projection.completed_trigger_ids)
        for event in events:
            if event.event_type == "TriggerProcessCompleted" and self.fail_completion_once:
                self.fail_completion_once = False
                raise ConcurrencyConflict("simulated crash before durable completion")
            if event.event_type in {
                "TriggerProcessOpened",
                "TriggerProcessClaimed",
                "TriggerProcessReclaimed",
            }:
                process = TriggerProcess.model_validate_json(
                    json.dumps(event.payload()["process"])
                )
                index = next(
                    (
                        index
                        for index, item in enumerate(processes)
                        if item.trigger_id == process.trigger_id
                    ),
                    None,
                )
                if index is None:
                    processes.append(process)
                else:
                    processes[index] = process
            elif event.event_type == "TriggerProcessCompleted":
                trigger_id = str(event.payload()["trigger_id"])
                index = next(
                    index
                    for index, item in enumerate(processes)
                    if item.trigger_id == trigger_id
                )
                processes[index] = processes[index].model_copy(
                    update={
                        "state": "terminal",
                        "runtime_outcome_ref": event.payload()["runtime_outcome_ref"],
                    }
                )
                completed.append(trigger_id)
            self._events.append(event)
            self._projection.deliberation_revision += 1
            self._projection.ledger_sequence += 1
        self._projection.trigger_processes = tuple(processes)
        self._projection.completed_trigger_ids = tuple(completed)


@pytest.mark.asyncio
async def test_role_owned_media_request_is_durable_and_effect_once_across_restart() -> None:
    source = _accepted_plan_event()
    ledger = _Ledger(source)
    conductor = _Conductor(ledger=ledger)
    runtime = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]

    assert await runtime.request_for_actions(("action:reply",)) is True
    result = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:media-request",
        correlation_id="correlation:reply",
    )

    assert result.status == "completed"
    assert result.preview is not None and result.preview.status == "planned"
    assert conductor.calls == 1
    terminal = ledger.project().trigger_processes[0]
    assert terminal.state == "terminal"
    assert terminal.runtime_outcome_ref == "media-request:planned"

    restarted = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]
    repeated = await restarted.advance_once(
        logical_time=NOW + timedelta(minutes=1),
        trace_id="trace:restart",
        correlation_id="correlation:restart",
    )
    assert repeated.handled is False
    assert repeated.status == "idle"
    assert conductor.calls == 1


@pytest.mark.asyncio
async def test_media_request_supplies_only_the_exact_role_attended_visual_source() -> None:
    source = _accepted_plan_event()
    attended = "event:life:settlement:attended"
    ledger = _Ledger(source, proposal_audits=(_proposal_audit(attended_source_ref=attended),))
    conductor = _Conductor(ledger=ledger)
    supplier = _CandidateSupplier()
    runtime = MediaRequestRuntime(
        ledger=ledger,
        conductor=conductor,  # type: ignore[arg-type]
        candidate_supplier=supplier,
    )

    result = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:media-request",
        correlation_id="correlation:reply",
    )

    assert result.status == "completed"
    assert len(supplier.calls) == 1
    supplied_refs, supplied_trace, supplied_correlation = supplier.calls[0]
    assert supplied_refs == (attended,)
    assert supplied_trace == "trace:media-request"
    assert supplied_correlation.startswith("correlation:media-request:")
    assert conductor.calls == 1

    restarted = MediaRequestRuntime(
        ledger=ledger,
        conductor=conductor,  # type: ignore[arg-type]
        candidate_supplier=supplier,
    )
    repeated = await restarted.advance_once(
        logical_time=NOW + timedelta(minutes=1),
        trace_id="trace:restart",
        correlation_id="correlation:restart",
    )
    assert repeated.handled is False
    assert len(supplier.calls) == 1
    assert conductor.calls == 1


@pytest.mark.asyncio
async def test_private_turn_attention_alone_cannot_authorize_candidate_compilation() -> None:
    source = _accepted_plan_event()
    attended = "event:life:settlement:audit-only"
    ledger = _Ledger(
        source,
        proposal_audits=(
            _proposal_audit(
                attended_source_ref=attended,
                authorize_media_source=False,
            ),
        ),
    )
    conductor = _Conductor(ledger=ledger)
    supplier = _CandidateSupplier()
    runtime = MediaRequestRuntime(
        ledger=ledger,
        conductor=conductor,  # type: ignore[arg-type]
        candidate_supplier=supplier,
    )

    result = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:audit-only",
        correlation_id="correlation:audit-only",
    )

    assert result.status == "completed"
    assert supplier.calls == []
    assert conductor.calls == 1

@pytest.mark.asyncio
async def test_restart_recovers_media_plan_without_a_second_selection() -> None:
    source = _accepted_plan_event()
    ledger = _Ledger(source)
    ledger.fail_completion_once = True
    conductor = _Conductor(ledger=ledger)
    runtime = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]

    interrupted = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:interrupted",
        correlation_id="correlation:reply",
    )
    assert interrupted.status == "blocked"
    assert interrupted.preview is not None and interrupted.preview.status == "planned"
    assert conductor.calls == 1
    process = ledger.project().trigger_processes[0]
    assert process.state == "claimed"

    restarted = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]
    recovered = await restarted.advance_once(
        logical_time=NOW + timedelta(seconds=31),
        trace_id="trace:restarted",
        correlation_id="correlation:restarted",
    )

    assert recovered.status == "completed"
    assert recovered.reason_code == "media-request:planned"
    assert conductor.calls == 1
    assert ledger.project().trigger_processes[0].runtime_outcome_ref == (
        "media-request:planned"
    )


@pytest.mark.asyncio
async def test_typing_is_not_visibility_and_no_candidate_closes_the_request() -> None:
    source = _accepted_plan_event()
    ledger = _Ledger(source)
    action = ledger.project().actions[0]
    ledger.project().actions = (action.model_copy(update={"kind": "typing"}),)
    conductor = _Conductor(
        MediaPreviewConductorResult(
            status="idle",
            reason_code="media_selection.no_open_candidate",
        )
    )
    runtime = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]

    assert await runtime.request_for_actions(("action:reply",)) is True
    assert (
        await runtime.advance_once(
            logical_time=NOW,
            trace_id="trace:typing-only",
            correlation_id="correlation:typing-only",
        )
    ).handled is False
    assert conductor.calls == 0

    ledger.project().actions = (action,)
    pending = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:no-candidate",
        correlation_id="correlation:no-candidate",
    )
    assert pending.status == "completed"
    assert pending.reason_code == "media-request:no_candidate"
    assert ledger.project().trigger_processes[0].state == "terminal"
    assert conductor.calls == 1


@pytest.mark.asyncio
async def test_unrelated_pending_media_plan_cannot_complete_the_request() -> None:
    source = _accepted_plan_event()
    ledger = _Ledger(source)
    # A pre-existing planning Action can make the shared conductor report
    # planned, but it carries another correlation and therefore cannot consume
    # this role-owned request.
    conductor = _Conductor()
    runtime = MediaRequestRuntime(ledger=ledger, conductor=conductor)  # type: ignore[arg-type]

    result = await runtime.advance_once(
        logical_time=NOW,
        trace_id="trace:unrelated-plan",
        correlation_id="correlation:unrelated-plan",
    )

    assert result.status == "blocked"
    assert result.reason_code == "media_request.terminal_not_bound_to_request"
    assert ledger.project().trigger_processes[0].state == "claimed"
    assert conductor.calls == 1


def test_media_request_trigger_binds_exact_accepted_plan_and_terminal_outcome() -> None:
    source = _accepted_plan_event()
    state = ReducerState(
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=source.event_id,
                event_type=source.event_type,
                world_revision=1,
                payload_hash=source.payload_hash,
                logical_time=NOW,
            ),
        ),
        expression_plans=(
            ExpressionPlanProjection(
                acceptance_id="acceptance:reply",
                proposal_id="proposal:reply",
                expression_change_id="change:reply",
                plan_id="plan:reply",
                event_ref=source.event_id,
                event_payload_hash=source.payload_hash,
            ),
        ),
        expression_plan_manifests=(
            ExpressionPlanManifestRef.model_construct(
                acceptance_id="acceptance:reply",
                proposal_id="proposal:reply",
                proposal_event_ref="event:proposal:reply",
                proposal_event_payload_hash="b" * 64,
                proposal_hash="sha256:" + "c" * 64,
                evaluated_world_revision=0,
                policy_digest="d" * 64,
                expression_change_id="change:reply",
                expression_change_hash="sha256:" + "e" * 64,
                plan_id="plan:reply",
                ordering_policy="ordered",
                terminal_policy="settle_all",
                media_request="consider_available_candidate",
                beats=(),
                manifest_hash="f" * 64,
                acceptance_event_ref="event:acceptance:reply",
                acceptance_event_payload_hash="1" * 64,
                recorded_at_world_revision=1,
            ),
        ),
    )
    trigger_id = media_request_trigger_id(
        world_id=WORLD,
        source_event_ref=source.event_id,
        source_event_payload_hash=source.payload_hash,
    )
    process = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref="media-request:" + source.payload_hash,
        process_kind="media_request",
        source_evidence_ref=source.event_id,
        state="open",
    )
    opened = _event(
        "TriggerProcessOpened",
        {"process": process.model_dump(mode="json")},
        event_id="event:media-request:open",
        causation_id=source.event_id,
    )
    reduced = reduce_event(state, opened)
    assert reduced.trigger_processes == (process,)

    forged = process.model_copy(update={"trigger_id": "trigger:forged"})
    with pytest.raises(ValueError, match="identity is not deterministic"):
        reduce_event(
            state,
            _event(
                "TriggerProcessOpened",
                {"process": forged.model_dump(mode="json")},
                event_id="event:media-request:forged",
                causation_id=source.event_id,
            ),
        )

    without_request = state.model_copy(
        update={
            "expression_plan_manifests": (
                state.expression_plan_manifests[0].model_copy(
                    update={"media_request": "none"}
                ),
            )
        }
    )
    with pytest.raises(ValueError, match="exact accepted expression plan"):
        reduce_event(without_request, opened)
