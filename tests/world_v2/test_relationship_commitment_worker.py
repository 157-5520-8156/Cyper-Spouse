from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.audited_change_terminal import (
    AUDITED_CHANGE_TERMINAL_ADVISORY_KIND,
    RELATIONSHIP_COMMITMENT_TERMINAL_REASON,
    audited_change_authority_fingerprint,
    audited_change_terminal_event_id,
    audited_change_terminal_proposal_id,
)
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundCharacterAuthor,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_commitment_worker import (
    RelationshipCommitmentWorker,
    RelationshipCommitmentWorkResult,
)
from companion_daemon.world_v2.relationship_proposal_compiler import (
    RelationshipProposalCompiler,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import CommitResult, WorldEvent
from companion_daemon.world_v2.world_turn_runtime import InboundTurn

from test_appraisal_authority import WORLD_ID
from test_interaction_act_proposal_compiler import (
    WORLD as INTERACTION_ACT_WORLD,
    _record_decision as _record_interaction_act_decision,
)
from test_relationship_commitment_compiler import _compiler_fixture
from test_production_turn_application import (
    NOW as PRODUCTION_NOW,
    _build_application,
    _config,
    _ConversationTargetIdentities,
    _DeliveredTransport,
    _fixture_observation_ref,
    _Router,
)


class _Acceptance:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.pinned: tuple[object, str] | None = None
        self.accepted = 0

    def pin_proposal(self, *, cursor, proposal_id: str):
        self.pinned = (cursor, proposal_id)
        return SimpleNamespace(cursor=cursor, proposal_id=proposal_id)

    def accept_runtime_owned(self, *, handle, actor: str, source: str) -> CommitResult:
        del actor, source
        self.accepted += 1
        return CommitResult(
            world_revision=handle.cursor.world_revision + 1,
            deliberation_revision=handle.cursor.deliberation_revision,
            ledger_sequence=handle.cursor.ledger_sequence + 2,
            event_ids=(
                "event:relationship-commitment-acceptance",
                "event:relationship-commitment-mutation",
            ),
        )


def _crafted_terminal_event(*, ledger, audit, change, reason_code: str) -> WorldEvent:
    payload = {
        "proposal_id": audited_change_terminal_proposal_id(
            audit=audit,
            change=change,
        ),
        "source_event_ref": audit.event_ref,
        "advisory_kind": AUDITED_CHANGE_TERMINAL_ADVISORY_KIND,
        "stage": "rejected",
        "reason_code": reason_code,
        "failure_fingerprint": audited_change_authority_fingerprint(
            audit=audit,
            change=change,
        ),
    }
    identity = domain_idempotency_key(
        event_type="AdvisoryAcceptanceRejected",
        world_id=ledger.world_id,
        payload=payload,
    )
    assert identity is not None
    source_event = ledger.lookup_event_commit(audit.event_ref)
    assert source_event is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=audited_change_terminal_event_id(audit=audit, change=change),
        world_id=ledger.world_id,
        event_type="AdvisoryAcceptanceRejected",
        logical_time=source_event[0].logical_time,
        created_at=source_event[0].created_at,
        actor="worker:relationship-commitment",
        source="test:crafted-terminal",
        trace_id=source_event[0].trace_id,
        causation_id=audit.event_ref,
        correlation_id=source_event[0].correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


def test_public_reducer_rejects_arbitrary_typed_change_terminal_reason() -> None:
    wrapped, proposal, audit_cursor, _current_cursor = _compiler_fixture(
        target_stage="close_friend"
    )
    ledger = wrapped._delegate
    audit = next(
        item
        for item in ledger.project_at(audit_cursor).proposal_audits
        if item.proposal_id == proposal.proposal_id
    )
    change = next(
        item
        for item in proposal.proposed_changes
        if item.kind == "relationship_commitment"
    )
    event = _crafted_terminal_event(
        ledger=ledger,
        audit=audit,
        change=change,
        reason_code="crafted_arbitrary_reason",
    )

    with pytest.raises(ValueError, match="reason"):
        ledger.commit_at_cursor((event,), expected_cursor=audit_cursor)


def test_public_reducer_rejects_terminal_for_installed_relationship_transition() -> None:
    wrapped, proposal, audit_cursor, _current_cursor = _compiler_fixture(
        target_stage="friend"
    )
    ledger = wrapped._delegate
    audit = next(
        item
        for item in ledger.project_at(audit_cursor).proposal_audits
        if item.proposal_id == proposal.proposal_id
    )
    change = next(
        item
        for item in proposal.proposed_changes
        if item.kind == "relationship_commitment"
    )
    event = _crafted_terminal_event(
        ledger=ledger,
        audit=audit,
        change=change,
        reason_code=RELATIONSHIP_COMMITMENT_TERMINAL_REASON,
    )

    with pytest.raises(ValueError, match="transition is installed"):
        ledger.commit_at_cursor((event,), expected_cursor=audit_cursor)


def test_terminal_identity_rejects_non_relationship_typed_change() -> None:
    ledger = WorldLedger.in_memory(world_id=INTERACTION_ACT_WORLD)
    proposal, audit_cursor, _source_event = _record_interaction_act_decision(ledger)
    audit = next(
        item
        for item in ledger.project_at(audit_cursor).proposal_audits
        if item.proposal_id == proposal.proposal_id
    )
    change = next(
        item for item in proposal.proposed_changes if item.kind == "interaction_act"
    )

    with pytest.raises(ValueError, match="relationship commitment"):
        audited_change_authority_fingerprint(audit=audit, change=change)


@pytest.mark.asyncio
async def test_background_commitment_waits_for_delivered_receipt_then_accepts_typed_output() -> None:
    ledger, proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    acceptance = _Acceptance(ledger)
    worker = RelationshipCommitmentWorker(
        ledger=ledger,
        compiler=RelationshipProposalCompiler(ledger=ledger),
        acceptance=acceptance,
        actor="worker:relationship-commitment",
    )

    ledger.without_delivered_action()
    assert await worker.drain_one() is None
    assert ledger.recorded == ()
    assert acceptance.accepted == 0

    ready, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    ready_acceptance = _Acceptance(ready)
    ready_worker = RelationshipCommitmentWorker(
        ledger=ready,
        compiler=RelationshipProposalCompiler(ledger=ready),
        acceptance=ready_acceptance,
        actor="worker:relationship-commitment",
    )
    result = await ready_worker.drain_one()

    assert result is not None
    assert result.status == "accepted"
    assert result.source_proposal_id == proposal.proposal_id
    assert len(ready.recorded) == 1
    assert ready_acceptance.accepted == 1


class _RuntimeCommitmentWorker:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.calls = 0

    async def drain_one(self) -> RelationshipCommitmentWorkResult:
        self.calls += 1
        return RelationshipCommitmentWorkResult(
            status="accepted",
            source_proposal_id="proposal:relationship-commitment",
            typed_proposal_id="proposal:relationship-commitment:compiled",
            compile_commit=CommitResult(
                world_revision=1,
                deliberation_revision=1,
                ledger_sequence=2,
                event_ids=("event:proposal:relationship-commitment",),
            ),
            acceptance_commit=CommitResult(
                world_revision=2,
                deliberation_revision=1,
                ledger_sequence=4,
                event_ids=(
                    "event:relationship-commitment-acceptance",
                    "event:relationship-commitment-mutation",
                ),
            ),
        )


class _RuntimeInteractionActWorker:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.calls = 0
        self.result = SimpleNamespace(status="accepted", lane="interaction_act")

    async def drain_one(self):
        self.calls += 1
        return self.result


class _RuntimeCharacterInterior:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.result = SimpleNamespace(status="authorized", lane="proactive")

    def _is_bound_to(self, ledger) -> bool:
        return ledger is self.ledger

    async def _drain_reconsideration_once(self):
        return None

    async def _drain_proactive_once(self):
        return self.result


class _FriendThenUnifiedSemanticModel:
    """First establish friendship, then author two independent typed lanes."""

    model = "test-friend-then-unified-semantic"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        observation_ref = _fixture_observation_ref(messages)
        repeated_friendship = self.calls > 1
        friendship_span = "你还是我朋友" if repeated_friendship else "你是我朋友了"
        text = (
            "好呀，你还是我朋友。下次见面我把这本书给你。"
            if repeated_friendship
            else "好呀，那说好了，你是我朋友了。"
        )
        appraisal: dict[str, object] = {
            "appraise": False,
            "affect": "no_change",
            "relationship_commitment": {
                "target_stage": "friend",
                "commitment_code": "mutual_friendship",
                "persistence": "durable",
                "visible_text_span": friendship_span,
            },
            "behavior_tendency": "明确回应",
            "stance": "接纳",
            "display_strategy": "直接表达",
            "brief_rationale": "她选择把自己的关系理解和后续安排明确说出来。",
            "confidence": 8200,
        }
        if repeated_friendship:
            appraisal["interaction_act"] = {
                "operation": "declare",
                "status_code": "等待后续交接",
                "source_scope": "delivered_expression",
                "source_text_span": "下次见面我把这本书给你",
                "interaction_act_ref": None,
                "act_kind": "约定后续交接",
                "subject_role": "self",
                "counterparty_roles": ["current_counterpart"],
                "object_ref": None,
                "object_label": "这本书",
            }
        return json.dumps(
            {
                "appraisal_draft": appraisal,
                "expression_draft": {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": "我愿意明确关系，也愿意留下后续交接安排。",
                        "attended_source_refs": [observation_ref],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": text}],
                    "stance": "认真",
                    "brief_rationale": "她选择明确说出自己的关系理解和后续安排。",
                    "confidence": 8200,
                    "world_claims": [],
                },
            },
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_terminal_relationship_change_does_not_close_unified_interaction_act(
    tmp_path: Path,
) -> None:
    path = tmp_path / "audited-change-terminal-granularity.sqlite"
    model = _FriendThenUnifiedSemanticModel()
    config = replace(
        _config(),
        reply_target="conversation:test:c2c:user.1",
        counterpart_actor_ref="user:user.1",
    )
    app = _build_application(
        path=path,
        config=config,
        identities=_ConversationTargetIdentities(),
        router=_Router(),
        inbound_author=_InboundCharacterAuthor(flash_model=model),
        transport=_DeliveredTransport(received_at=PRODUCTION_NOW),
        now=PRODUCTION_NOW,
    )
    try:
        for message_id, text in (
            ("friendship:opening", "我们可以成为好朋友吗？"),
            ("friendship:repeat-with-act", "那下次见面把书带给我吧。"),
        ):
            response = await app.respond(
                InboundTurn(
                    platform="test",
                    platform_user_id="user.1",
                    platform_message_id=message_id,
                    text=text,
                    observed_at=PRODUCTION_NOW,
                    trace_id=f"trace:{message_id}",
                )
            )
            assert response.status == "action_authorized"
            delivery = await app.drain_actions_once()
            assert delivery is not None and delivery.status == "settled"

        first_commitment = await app.drain_background_once()
        assert first_commitment is not None and first_commitment.status == "accepted"
        settlement = await app.drain_background_once()
        assert settlement is not None

        assert settlement.status == "stale"
        unified_source_proposal_id = settlement.source_proposal_id
        projection = app._ledger.project()  # noqa: SLF001 - public worker integration seam
        assert len(projection.relationship_commitments) == 1
        assert all(
            decision.proposal_id != unified_source_proposal_id
            for decision in projection.acceptance_decisions
        )
        assert len(settlement.acceptance_commit.event_ids) == 1
        terminal_event_id = settlement.acceptance_commit.event_ids[0]
        terminal = app._ledger.lookup_event_commit(terminal_event_id)  # noqa: SLF001
        assert terminal is not None
        terminal_payload = terminal[0].payload()
        assert terminal_payload["advisory_kind"] == "typed_change_terminal"
        assert terminal_payload["proposal_id"] != unified_source_proposal_id
    finally:
        app.close()

    reopened = _build_application(
        path=path,
        config=config,
        identities=_ConversationTargetIdentities(),
        router=_Router(),
        inbound_author=_InboundCharacterAuthor(
            flash_model=_FriendThenUnifiedSemanticModel()
        ),
        transport=_DeliveredTransport(received_at=PRODUCTION_NOW),
        now=PRODUCTION_NOW,
    )
    try:
        interaction = await reopened.drain_background_once()

        assert interaction is not None and interaction.status == "accepted"
        assert interaction.source_proposal_id == unified_source_proposal_id
        settled = reopened._ledger.project()  # noqa: SLF001 - cold replay evidence
        assert len(settled.relationship_commitments) == 1
        assert len(settled.interaction_acts) == 1
        replayed_terminal = reopened._ledger.lookup_event_commit(terminal_event_id)  # noqa: SLF001
        assert replayed_terminal == terminal
        assert reopened._ledger.rebuild() == settled  # noqa: SLF001
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_world_runtime_schedules_relationship_commitment_only_in_background() -> None:
    ledger, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    worker = _RuntimeCommitmentWorker(ledger)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        relationship_commitment_worker=worker,
    )

    result = await runtime.drain_background_once()

    assert result is not None
    assert result.status == "accepted"
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_world_runtime_prioritizes_eligible_proactive_over_pending_social_workers() -> None:
    ledger, _proposal, _audit_cursor, _current_cursor = _compiler_fixture()
    commitment_worker = _RuntimeCommitmentWorker(ledger)
    interaction_act_worker = _RuntimeInteractionActWorker(ledger)
    interior = _RuntimeCharacterInterior(ledger)
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        relationship_commitment_worker=commitment_worker,
        interaction_act_worker=interaction_act_worker,  # type: ignore[arg-type]
        character_interior=interior,  # type: ignore[arg-type]
    )

    first = await runtime.drain_background_once()

    assert first is interior.result
    assert commitment_worker.calls == 0
    assert interaction_act_worker.calls == 0
