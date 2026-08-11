from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import (
    AcceptedLedgerBatchHandle,
    AcceptedLedgerBatchIssuer,
)
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.interaction_act_events import (
    InteractionActProposalRecordedPayload,
    build_interaction_act_accepted_payload,
    canonical_interaction_act_mutation_hash,
)
from companion_daemon.world_v2.interaction_act_acceptance_manifest import (
    INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION,
    InteractionActAcceptanceManifest,
    build_interaction_act_acceptance_manifest,
    canonical_interaction_act_acceptance_manifest_hash,
)
from companion_daemon.world_v2.interaction_act_acceptance_runtime import (
    INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
    InteractionActAcceptanceError,
    InteractionActAtomicRecorder,
    InteractionActProposalAuthorityReader,
)
from companion_daemon.world_v2.interaction_act_events import InteractionActAcceptedPayload
from companion_daemon.world_v2.interaction_act_runtime import (
    DeliveredExpressionInteractionActSource,
    InteractionActRoleOutput,
    ObservedInteractionActSource,
    materialize_interaction_act_mutation,
)
from companion_daemon.world_v2.schemas import (
    CommitResult,
    InteractionActProposalProjection,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)
WORLD = "world:interaction-act-acceptance"
CONVERSATION = "conversation:qq:geoff"
COUNTERPART = "user:geoff"
COMPANION = "actor:companion"


def _observed_mutation():
    return materialize_interaction_act_mutation(
        authored=InteractionActRoleOutput(
            contract="interaction-act-role-output.2",
            source_text_span="回头寄给你怎么样",
            operation="declare",
            status_code="等待对方回应",
            interaction_act_ref=None,
            act_kind="offer_to_transfer_possession",
            subject_ref=COUNTERPART,
            counterparty_refs=(COMPANION,),
            object_ref=None,
            object_label="一本稀有古董莎士比亚",
        ),
        source=ObservedInteractionActSource(
            world_id=WORLD,
            conversation_ref=CONVERSATION,
            source_event_ref="event:observation:offer-book",
            source_world_revision=7,
            source_payload_hash="a" * 64,
            source_actor_ref=COUNTERPART,
            source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
        ),
        current=(),
        logical_time=NOW,
    )


def _proposal_event(payload: InteractionActProposalRecordedPayload) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-act-proposal:book",
        world_id=WORLD,
        event_type="InteractionActProposalRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:character-interior",
        source="test",
        trace_id="trace:interaction-act",
        causation_id="event:observation:offer-book",
        correlation_id="correlation:interaction-act",
        idempotency_key="idempotency:interaction-act-proposal:book",
        payload=payload.model_dump(mode="json"),
    )


class _ProposalAuthorityLedger:
    def __init__(
        self,
        *,
        event: WorldEvent,
        cursor: ProjectionCursor,
        persisted: bool = True,
    ) -> None:
        self.world_id = event.world_id
        self._event = event
        self._cursor = cursor
        payload = InteractionActProposalRecordedPayload.model_validate_json(
            event.payload_json,
            strict=True,
        )
        self._projection = InteractionActProposalProjection(
            proposal_id=payload.proposal_id,
            proposal_hash=payload.proposal_hash,
            change_id=payload.change_id,
            accepted_change_hash=payload.accepted_change_hash,
            evaluated_world_revision=payload.evaluated_world_revision,
            mutation_payload_hash=payload.mutation_payload_hash,
            mutation=payload.mutation,
            recorded_event_ref=event.event_id,
            recorded_event_payload_hash=event.payload_hash,
        )
        self._persisted = persisted

    def project_at(self, cursor: ProjectionCursor):
        if cursor != self._cursor:
            raise AssertionError("proposal authority read escaped its cursor")
        return SimpleNamespace(interaction_act_proposals=(self._projection,))

    def lookup_event_commit(self, event_id: str):
        if not self._persisted or event_id != self._event.event_id:
            return None
        return self._event, CommitResult(
            world_revision=self._cursor.world_revision,
            deliberation_revision=self._cursor.deliberation_revision,
            ledger_sequence=self._cursor.ledger_sequence,
            event_ids=(self._event.event_id,),
        )


def _pinned_recorder(
    *,
    proposal_event: WorldEvent,
    cursor: ProjectionCursor,
    issuer: AcceptedLedgerBatchIssuer | None = None,
):
    reader = InteractionActProposalAuthorityReader(
        ledger=_ProposalAuthorityLedger(event=proposal_event, cursor=cursor)  # type: ignore[arg-type]
    )
    handle = reader.pin(
        world_id=proposal_event.world_id,
        cursor=cursor,
        proposal_event_ref=proposal_event.event_id,
    )
    recorder = InteractionActAtomicRecorder(
        proposal_reader=reader,
        batch_issuer=issuer or AcceptedLedgerBatchIssuer(),
    )
    return recorder, handle


def test_accepted_payload_binds_proposal_audit_change_and_observed_mutation_hashes() -> None:
    mutation = _observed_mutation()
    mutation_hash = canonical_interaction_act_mutation_hash(mutation)
    forged_span = "一本稀有古董莎士比亚"
    forged_status = mutation.act_after.participant_statuses[0].model_copy(
        update={"source_text_span": forged_span}
    )
    forged_after = mutation.act_after.model_copy(
        update={"participant_statuses": (forged_status,)}
    )
    forged_span_mutation = mutation.model_copy(
        update={"source_text_span": forged_span, "act_after": forged_after}
    )
    with pytest.raises(ValueError, match="proposal mutation hash is invalid"):
        InteractionActProposalRecordedPayload(
            contract="interaction-act-proposal.1",
            proposal_id="proposal:interaction-act:forged-span",
            proposal_hash="sha256:" + "1" * 64,
            change_id="change:interaction-act:forged-span",
            accepted_change_hash="2" * 64,
            evaluated_world_revision=7,
            mutation_payload_hash=mutation_hash,
            mutation=forged_span_mutation,
            observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
        )
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=mutation_hash,
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    proposal_event = _proposal_event(proposal)

    accepted = build_interaction_act_accepted_payload(
        acceptance_id="acceptance:interaction-act:book",
        source_proposal_event=proposal_event,
    )

    assert accepted.source_proposal_event_ref == proposal_event.event_id
    assert accepted.source_proposal_event_payload_hash == proposal_event.payload_hash
    assert accepted.proposal_id == proposal.proposal_id
    assert accepted.proposal_hash == proposal.proposal_hash
    assert accepted.change_id == proposal.change_id
    assert accepted.accepted_change_hash == proposal.accepted_change_hash
    assert accepted.evaluated_world_revision == 7
    assert accepted.mutation_payload_hash == mutation_hash
    assert accepted.mutation == mutation
    assert accepted.mutation.source_text_span == "回头寄给你怎么样"
    assert accepted.mutation.act_after.external_outcome == "not_established"
    assert tuple(
        (item.actor_ref, item.status_code)
        for item in accepted.mutation.act_after.participant_statuses
    ) == ((COUNTERPART, "等待对方回应"),)


def test_acceptance_manifest_self_hashes_exact_effect_and_authority() -> None:
    mutation = _observed_mutation()
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    accepted = build_interaction_act_accepted_payload(
        acceptance_id="acceptance:interaction-act:book",
        source_proposal_event=_proposal_event(proposal),
    )

    manifest = build_interaction_act_acceptance_manifest(
        accepted_payload=accepted,
        policy_digest=INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST,
    )

    assert INTERACTION_ACT_ACCEPTANCE_POLICY_VERSION == (
        "interaction-act-acceptance-policy.2"
    )
    assert manifest.manifest_version == "interaction-act-acceptance.1"
    assert manifest.status == "accepted"
    assert manifest.acceptance_id == accepted.acceptance_id
    assert manifest.source_proposal_event_ref == accepted.source_proposal_event_ref
    assert manifest.source_proposal_event_payload_hash == (
        accepted.source_proposal_event_payload_hash
    )
    assert manifest.proposal_hash == accepted.proposal_hash
    assert manifest.accepted_change_hash == accepted.accepted_change_hash
    assert manifest.mutation_payload_hash == accepted.mutation_payload_hash
    assert manifest.effect_event_id == accepted.accepted_event_ref
    assert manifest.effect_event_type == "InteractionActTransitionAccepted"
    assert manifest.manifest_hash == canonical_interaction_act_acceptance_manifest_hash(
        manifest.model_dump(mode="json")
    )
    with pytest.raises(ValueError, match="policy digest is not installed"):
        build_interaction_act_acceptance_manifest(
            accepted_payload=accepted,
            policy_digest="3" * 64,
        )


def test_atomic_recorder_prepares_exact_cursor_ordered_effect_once_events() -> None:
    mutation = _observed_mutation()
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    proposal_event = _proposal_event(proposal)
    cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=12,
    )
    recorder, handle = _pinned_recorder(
        proposal_event=proposal_event,
        cursor=cursor,
    )

    prepared = recorder.prepare_events(
        handle=handle,
        actor="system:interaction-act-acceptance",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act",
        correlation_id="correlation:interaction-act",
    )
    replayed = recorder.prepare_events(
        handle=handle,
        actor="system:interaction-act-acceptance",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act",
        correlation_id="correlation:interaction-act",
    )

    assert prepared == replayed
    assert prepared.expected_cursor == cursor
    assert prepared.policy_digest == INTERACTION_ACT_ACCEPTANCE_POLICY_DIGEST
    assert len(prepared.events) == 2
    acceptance_event, effect_event = prepared.events
    assert acceptance_event.event_type == "AcceptanceRecorded"
    assert effect_event.event_type == "InteractionActTransitionAccepted"
    assert acceptance_event.causation_id == proposal_event.event_id
    assert effect_event.causation_id == acceptance_event.event_id
    manifest = InteractionActAcceptanceManifest.model_validate_json(
        acceptance_event.payload_json,
        strict=True,
    )
    accepted = InteractionActAcceptedPayload.model_validate_json(
        effect_event.payload_json,
        strict=True,
    )
    assert manifest.manifest_hash == prepared.manifest_hash
    assert manifest.effect_event_id == effect_event.event_id
    assert manifest.effect_payload_hash == effect_event.payload_hash
    assert accepted.accepted_event_ref == effect_event.event_id
    assert accepted.source_proposal_event_ref == proposal_event.event_id
    assert accepted.source_proposal_event_payload_hash == proposal_event.payload_hash
    assert prepared.commit_id.startswith("commit:interaction-act-acceptance:")


def test_proposal_authority_reader_rejects_uncommitted_and_cross_reader_handles() -> None:
    mutation = _observed_mutation()
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    event = _proposal_event(proposal)
    cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=12,
    )
    uncommitted = InteractionActProposalAuthorityReader(
        ledger=_ProposalAuthorityLedger(  # type: ignore[arg-type]
            event=event,
            cursor=cursor,
            persisted=False,
        )
    )
    with pytest.raises(InteractionActAcceptanceError, match="proposal_event_missing"):
        uncommitted.pin(
            world_id=WORLD,
            cursor=cursor,
            proposal_event_ref=event.event_id,
        )

    trusted_reader = InteractionActProposalAuthorityReader(
        ledger=_ProposalAuthorityLedger(event=event, cursor=cursor)  # type: ignore[arg-type]
    )
    handle = trusted_reader.pin(
        world_id=WORLD,
        cursor=cursor,
        proposal_event_ref=event.event_id,
    )
    other_reader = InteractionActProposalAuthorityReader(
        ledger=_ProposalAuthorityLedger(event=event, cursor=cursor)  # type: ignore[arg-type]
    )
    recorder = InteractionActAtomicRecorder(
        proposal_reader=other_reader,
        batch_issuer=AcceptedLedgerBatchIssuer(),
    )
    with pytest.raises(InteractionActAcceptanceError, match="proposal_handle_untrusted"):
        recorder.prepare_events(
            handle=handle,
            actor="system:interaction-act-acceptance",
            source="test",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:interaction-act",
            correlation_id="correlation:interaction-act",
        )


def test_atomic_recorder_prepares_public_accepted_batch_capability() -> None:
    mutation = _observed_mutation()
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=12,
    )
    issuer = AcceptedLedgerBatchIssuer()
    proposal_event = _proposal_event(proposal)
    recorder, handle = _pinned_recorder(
        proposal_event=proposal_event,
        cursor=cursor,
        issuer=issuer,
    )

    batch = recorder.prepare_batch(
        handle=handle,
        actor="system:interaction-act-acceptance",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act",
        correlation_id="correlation:interaction-act",
    )

    assert type(batch) is AcceptedLedgerBatchHandle


def test_interaction_act_manifest_requires_recorder_and_exact_two_event_batch() -> None:
    mutation = _observed_mutation()
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:book",
        proposal_hash="sha256:" + "1" * 64,
        change_id="change:interaction-act:book",
        accepted_change_hash="2" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我从英国带回来一本稀有古董莎士比亚，回头寄给你怎么样",
    )
    cursor = ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=12,
    )
    recorder, handle = _pinned_recorder(
        proposal_event=_proposal_event(proposal),
        cursor=cursor,
    )
    prepared = recorder.prepare_events(
        handle=handle,
        actor="system:interaction-act-acceptance",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act",
        correlation_id="correlation:interaction-act",
    )

    validate_commit_batch(
        prepared.events,
        expected_world_revision=7,
        accepted_manifest_v3_authorized=True,
    )
    with pytest.raises(ValueError, match="recorder_capability_required"):
        validate_commit_batch(
            prepared.events,
            expected_world_revision=7,
            accepted_manifest_v3_authorized=False,
        )

    third = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-act:forged-third",
        world_id=WORLD,
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="system:interaction-act-acceptance",
        source="test",
        trace_id="trace:interaction-act",
        causation_id=prepared.events[-1].event_id,
        correlation_id="correlation:interaction-act",
        idempotency_key="forged:interaction-act:third",
        payload={"observation_id": "observation:interaction-act:forged-third"},
    )
    with pytest.raises(ValueError, match="accepted_batch_must_be_exact"):
        validate_commit_batch(
            (*prepared.events, third),
            expected_world_revision=7,
            accepted_manifest_v3_authorized=True,
        )


def test_atomic_recorder_requires_delivered_expression_proof_inside_exact_cursor() -> None:
    mutation = materialize_interaction_act_mutation(
        authored=InteractionActRoleOutput(
            contract="interaction-act-role-output.2",
            source_text_span="我们下次继续聊这个吧。",
            operation="declare",
            status_code="等待下次延续",
            interaction_act_ref=None,
            act_kind="提议下次继续讨论",
            subject_ref=COMPANION,
            counterparty_refs=(COUNTERPART,),
            object_ref=None,
            object_label=None,
        ),
        source=DeliveredExpressionInteractionActSource(
            world_id=WORLD,
            conversation_ref=CONVERSATION,
            source_event_ref="event:expression:continue-next-time",
            source_world_revision=8,
            source_payload_hash="4" * 64,
            source_actor_ref=COMPANION,
            source_text="我们下次继续聊这个吧。",
            expression_plan_id="expression-plan:continue-next-time",
            expression_plan_event_ref="event:plan:continue-next-time",
            expression_plan_event_payload_hash="9" * 64,
            expression_beat_id="expression-beat:continue-next-time",
            expression_beat_event_ref="event:beat:continue-next-time",
            expression_beat_event_payload_hash="a" * 64,
            stored_payload_event_ref="event:expression:continue-next-time",
            stored_payload_event_payload_hash="4" * 64,
            action_id="action:continue-next-time",
            action_payload_hash="sha256:" + "5" * 64,
            action_target_ref=COUNTERPART,
            action_event_ref="event:action:continue-next-time",
            action_event_payload_hash="b" * 64,
            receipt_id="receipt:continue-next-time",
            receipt_event_ref="event:receipt:continue-next-time",
            receipt_world_revision=10,
            receipt_payload_hash="6" * 64,
            receipt_status="delivered",
        ),
        current=(),
        logical_time=NOW,
    )
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:continue-next-time",
        proposal_hash="sha256:" + "7" * 64,
        change_id="change:interaction-act:continue-next-time",
        accepted_change_hash="8" * 64,
        evaluated_world_revision=10,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
    )
    proposal_event = _proposal_event(proposal)
    premature_proposal_event = _proposal_event(
        proposal.model_copy(update={"evaluated_world_revision": 9})
    )
    premature_cursor = ProjectionCursor(
        world_revision=9,
        deliberation_revision=3,
        ledger_sequence=14,
    )
    premature_recorder, premature_handle = _pinned_recorder(
        proposal_event=premature_proposal_event,
        cursor=premature_cursor,
    )

    with pytest.raises(InteractionActAcceptanceError, match="receipt_after_cursor"):
        premature_recorder.prepare_events(
            handle=premature_handle,
            actor="system:interaction-act-acceptance",
            source="test",
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:interaction-act",
            correlation_id="correlation:interaction-act",
        )

    cursor = ProjectionCursor(
        world_revision=10,
        deliberation_revision=3,
        ledger_sequence=15,
    )
    recorder, handle = _pinned_recorder(
        proposal_event=proposal_event,
        cursor=cursor,
    )
    prepared = recorder.prepare_events(
        handle=handle,
        actor="system:interaction-act-acceptance",
        source="test",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:interaction-act",
        correlation_id="correlation:interaction-act",
    )
    accepted = InteractionActAcceptedPayload.model_validate_json(
        prepared.events[1].payload_json,
        strict=True,
    )
    proof = accepted.mutation.act_after.participant_statuses[0].source_ref.delivery_proof
    assert proof is not None
    assert proof.receipt_event_ref == "event:receipt:continue-next-time"
    assert proof.receipt_status == "delivered"
    assert accepted.mutation.act_after.external_outcome == "not_established"
