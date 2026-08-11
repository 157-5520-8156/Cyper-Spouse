"""Compile one audited role-authored interaction act into a typed candidate.

The compiler does not classify dialogue or choose an act kind.  It verifies
the exact generic proposal, source bytes, participant authority and current
entity revision, then records an inert ``InteractionActProposalRecorded``.
Acceptance and projection mutation remain separate authorities.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .interaction_act_identity import (
    interaction_act_conversation_ref,
    interaction_act_overlapping_occurrence_count,
    normalize_interaction_act_source_text,
)
from .interaction_act_events import (
    InteractionActProposalRecordedPayload,
    canonical_interaction_act_change_hash,
    canonical_interaction_act_mutation_hash,
    interaction_act_typed_proposal_id,
)
from .interaction_act_runtime import (
    DeliveredExpressionInteractionActSource,
    InteractionActRoleOutput,
    ObservedInteractionActSource,
    materialize_interaction_act_mutation,
)
from .interaction_act_schemas import InteractionActProjection
from .ledger import LedgerPort
from .minimal_reply_events import (
    ExpressionBeatAuthorizedPayload,
    ExpressionPlanAcceptedPayload,
    MessagePayloadStoredPayload,
)
from .schema_core import FrozenModel, canonicalize_json_value
from .schemas import (
    Action,
    CommitResult,
    CommittedWorldEventRef,
    ExecutionReceipt,
    Observation,
    ProjectionCursor,
    WorldEvent,
)


_CONTRACT = "interaction-act-proposal-compiler.1"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InteractionActProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"interaction_act_proposal_compiler.{code}"
        super().__init__(self.code)


class InteractionActProposalCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str | None = None
    typed_proposal_event_ref: str | None = None
    commit: CommitResult | None = None
    acceptance_cursor: ProjectionCursor | None = None


class InteractionActProposalCompiler:
    """Source-close the generic ``interaction_act`` change at one cursor."""

    __slots__ = ("_ledger", "_reader")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def record_rebased(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> InteractionActProposalCompilation:
        if (
            current_cursor.world_revision < audit_cursor.world_revision
            or current_cursor.deliberation_revision < audit_cursor.deliberation_revision
            or current_cursor.ledger_sequence < audit_cursor.ledger_sequence
        ):
            raise InteractionActProposalCompilerError("rebase_cursor_precedes_audit")
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            change
            for change in authority.proposal.proposed_changes
            if change.kind == "interaction_act"
        )
        if not changes:
            return InteractionActProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(changes) != 1:
            raise InteractionActProposalCompilerError("change_count_invalid")
        change = changes[0]
        projection = self._ledger.project_at(current_cursor)
        current = self._current_acts(projection)
        raw = change.payload.value()
        try:
            authored = InteractionActRoleOutput(
                contract="interaction-act-role-output.2",
                source_text_span=raw["source_text_span"],
                operation=change.transition,
                status_code=raw["status_code"],
                interaction_act_ref=raw["interaction_act_ref"],
                act_kind=raw["act_kind"],
                subject_ref=raw["subject_ref"],
                counterparty_refs=tuple(raw["counterparty_refs"]),
                object_ref=raw["object_ref"],
                object_label=raw["object_label"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InteractionActProposalCompilerError("change_payload_invalid") from exc
        self._verify_change_coordinates(
            change=change,
            authored=authored,
            current=current,
        )
        observed_event, source_commit, observation = self._observed_source(
            authority=authority,
            change=change,
            cursor=current_cursor,
        )
        conversation_ref = interaction_act_conversation_ref(
            world_id=world_id,
            channel=observation.channel,
            participant_refs=(
                authored.subject_ref,
                *authored.counterparty_refs,
            ),
        )
        observed_source_text: str | None = None
        if raw.get("source_scope") == "current_message":
            observed_source_text = normalize_interaction_act_source_text(observation.text)
            source = ObservedInteractionActSource(
                world_id=world_id,
                conversation_ref=conversation_ref,
                source_event_ref=observed_event.event_id,
                source_world_revision=source_commit.world_revision,
                source_payload_hash=observed_event.payload_hash,
                source_actor_ref=observation.actor,
                source_text=observed_source_text,
            )
            materialization_event = observed_event
        elif raw.get("source_scope") == "delivered_expression":
            action_target_ref = observation.actor
            if observation.reply_context is not None:
                candidate_target = observation.reply_context.get("target")
                if not isinstance(candidate_target, str) or not candidate_target:
                    raise InteractionActProposalCompilerError("delivered_action_target_invalid")
                action_target_ref = candidate_target
            source, materialization_event = self._delivered_source(
                authority=authority,
                authored=authored,
                projection=projection,
                cursor=current_cursor,
                conversation_ref=conversation_ref,
                action_target_ref=action_target_ref,
            )
        else:
            raise InteractionActProposalCompilerError("source_scope_invalid")
        logical_time = projection.logical_time or materialization_event.logical_time
        try:
            mutation = materialize_interaction_act_mutation(
                authored=authored,
                source=source,
                current=current,
                logical_time=logical_time,
            )
        except ValueError as exc:
            message = str(exc)
            if "source text span" in message:
                code = "source_text_span_invalid"
            elif "object label" in message:
                code = "object_label_invalid"
            else:
                code = "materialization_invalid"
            raise InteractionActProposalCompilerError(code) from exc
        mutation_hash = canonical_interaction_act_mutation_hash(mutation)
        typed_proposal_id = interaction_act_typed_proposal_id(
            source_audit_event_ref=authority.audit.event_ref,
            source_audit_event_payload_hash=authority.audit.event_payload_hash,
            source_change_id=change.change_id,
            evaluated_world_revision=current_cursor.world_revision,
            mutation_payload_hash=mutation_hash,
        )
        typed = InteractionActProposalRecordedPayload(
            contract="interaction-act-proposal.1",
            proposal_id=typed_proposal_id,
            proposal_hash=authority.proposal.proposal_hash,
            change_id=change.change_id,
            accepted_change_hash=canonical_interaction_act_change_hash(change),
            evaluated_world_revision=current_cursor.world_revision,
            mutation_payload_hash=mutation_hash,
            mutation=mutation,
            observed_source_text=observed_source_text,
        )
        event = self._proposal_event(
            world_id=world_id,
            authority=authority,
            typed=typed,
            source_event=materialization_event,
            logical_time=logical_time,
        )
        existing = self._ledger.lookup_event_commit(event.event_id)
        if existing is not None:
            if existing[0] != event:
                raise InteractionActProposalCompilerError("candidate_identity_conflict")
            pending = tuple(
                item
                for item in getattr(projection, "interaction_act_proposals", ())
                if item.proposal_id == typed.proposal_id
                and item.proposal_hash == typed.proposal_hash
                and item.change_id == typed.change_id
                and item.accepted_change_hash == typed.accepted_change_hash
                and item.evaluated_world_revision == typed.evaluated_world_revision
                and item.mutation_payload_hash == typed.mutation_payload_hash
                and item.mutation == typed.mutation
                and item.recorded_event_ref == event.event_id
                and item.recorded_event_payload_hash == event.payload_hash
            )
            if len(pending) != 1:
                raise InteractionActProposalCompilerError("pending_candidate_not_exact")
            commit = existing[1]
            acceptance_cursor = current_cursor
        else:
            commit = self._ledger.commit_at_cursor(
                (event,),
                expected_cursor=current_cursor,
                commit_id="commit:interaction-act-proposal-compiler:"
                + _canonical_hash(
                    {
                        "world_id": world_id,
                        "source_proposal_event_ref": authority.audit.event_ref,
                        "event_id": event.event_id,
                    }
                ),
            )
            acceptance_cursor = ProjectionCursor(
                world_revision=commit.world_revision,
                deliberation_revision=commit.deliberation_revision,
                ledger_sequence=commit.ledger_sequence,
            )
        return InteractionActProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            typed_proposal_id=typed.proposal_id,
            typed_proposal_event_ref=event.event_id,
            commit=commit,
            acceptance_cursor=acceptance_cursor,
        )

    @staticmethod
    def _current_acts(projection: object) -> tuple[InteractionActProjection, ...]:
        values = getattr(projection, "interaction_acts", ())
        try:
            return tuple(
                item
                if type(item) is InteractionActProjection
                else InteractionActProjection.model_validate(item, strict=True)
                for item in values
            )
        except (TypeError, ValueError) as exc:
            raise InteractionActProposalCompilerError("current_projection_invalid") from exc

    @staticmethod
    def _verify_change_coordinates(*, change, authored, current) -> None:
        if authored.operation == "declare":
            if change.expected_entity_revision != 0:
                raise InteractionActProposalCompilerError("entity_revision_invalid")
            return
        if authored.interaction_act_ref is None or change.target_id != authored.interaction_act_ref:
            raise InteractionActProposalCompilerError("target_binding_invalid")
        matches = tuple(
            item for item in current if item.interaction_act_id == authored.interaction_act_ref
        )
        if len(matches) != 1:
            raise InteractionActProposalCompilerError("current_act_not_exact")
        current_act = matches[0]
        if (
            change.expected_entity_revision is not None
            and current_act.entity_revision != change.expected_entity_revision
        ):
            raise InteractionActProposalCompilerError("entity_revision_stale")
        current_object_ref = (
            current_act.object_descriptor.object_ref
            if current_act.object_descriptor is not None
            else None
        )
        if (
            authored.subject_ref != current_act.subject_ref
            or authored.counterparty_refs != current_act.counterparty_refs
            or authored.act_kind != current_act.act_kind
            or authored.object_ref != current_object_ref
        ):
            raise InteractionActProposalCompilerError("immutable_coordinates_changed")

    def _observed_source(self, *, authority, change, cursor):
        located = self._ledger.lookup_event_commit(authority.proposal.trigger_ref)
        if located is None:
            raise InteractionActProposalCompilerError("source_event_missing")
        event, commit = located
        if (
            event.world_id != self._ledger.world_id
            or event.event_type != "ObservationRecorded"
            or commit.world_revision > cursor.world_revision
            or commit.deliberation_revision > cursor.deliberation_revision
            or commit.ledger_sequence > cursor.ledger_sequence
        ):
            raise InteractionActProposalCompilerError("observed_source_invalid")
        try:
            observation = Observation.model_validate_json(event.payload_json, strict=True)
        except ValueError as exc:
            raise InteractionActProposalCompilerError("observed_source_invalid") from exc
        if (
            observation.world_id != event.world_id
            or observation.actor != event.actor
            or observation.text is None
            or tuple(change.evidence_refs) != (event.event_id,)
        ):
            raise InteractionActProposalCompilerError("observed_source_invalid")
        evidence = tuple(
            item for item in authority.proposal.evidence_refs if item.ref_id == event.event_id
        )
        if len(evidence) != 1 or (
            evidence[0].evidence_kind != "committed_world_event"
            or evidence[0].source_world_revision != commit.world_revision
            or evidence[0].immutable_hash != "sha256:" + event.payload_hash
        ):
            raise InteractionActProposalCompilerError("source_evidence_invalid")
        return event, commit, observation

    def _delivered_source(
        self,
        *,
        authority,
        authored: InteractionActRoleOutput,
        projection,
        cursor: ProjectionCursor,
        conversation_ref: str,
        action_target_ref: str,
    ) -> tuple[DeliveredExpressionInteractionActSource, WorldEvent]:
        candidates: list[tuple[object, object, object]] = []
        selected_span = authored.source_text_span
        for plan in projection.expression_plans:
            if plan.proposal_id != authority.proposal.proposal_id or plan.state != "completed":
                continue
            for beat in projection.expression_beats:
                if (
                    beat.proposal_id != authority.proposal.proposal_id
                    or beat.plan_id != plan.plan_id
                    or beat.acceptance_id != plan.acceptance_id
                    or beat.state != "settled"
                ):
                    continue
                stored_matches = tuple(
                    item
                    for item in projection.stored_message_payloads
                    if item.proposal_id == authority.proposal.proposal_id
                    and item.acceptance_id == plan.acceptance_id
                    and item.payload_ref == beat.payload_ref
                    and item.payload_hash == beat.payload_hash
                )
                for stored in stored_matches:
                    normalized_text = normalize_interaction_act_source_text(stored.text)
                    if (
                        stored.payload_hash
                        != "sha256:" + hashlib.sha256(stored.text.encode("utf-8")).hexdigest()
                        or interaction_act_overlapping_occurrence_count(
                            source_text=normalized_text,
                            selected_text=selected_span,
                        )
                        != 1
                    ):
                        continue
                    candidates.append((plan, beat, stored))
        if len(candidates) != 1:
            raise InteractionActProposalCompilerError("delivered_expression_source_not_exact")
        plan, beat, stored = candidates[0]
        if beat.action_id is None:
            raise InteractionActProposalCompilerError("delivered_action_missing")
        permitted_source_actors = (
            authored.subject_ref,
            *authored.counterparty_refs,
        )
        actions = tuple(
            item
            for item in projection.actions
            if item.action_id == beat.action_id
            and item.expression_plan_id == plan.plan_id
            and item.expression_beat_id == beat.beat_id
            and item.payload_ref == stored.payload_ref
            and item.payload_hash == stored.payload_hash
            and item.actor in permitted_source_actors
            and item.target == action_target_ref
            and item.state == "delivered"
        )
        if len(actions) != 1:
            raise InteractionActProposalCompilerError("delivered_action_not_exact")
        action = actions[0]
        receipts = tuple(
            item
            for item in projection.execution_receipts
            if item.action_id == action.action_id
            and item.receipt_kind == "terminal"
            and item.observed_state == "delivered"
            and item.is_terminal
        )
        if len(receipts) != 1:
            raise InteractionActProposalCompilerError("delivered_receipt_not_exact")
        receipt = receipts[0]
        plan_terminal = tuple(
            item
            for item in plan.history
            if item.state == "completed"
            and item.receipt_id == receipt.receipt_id
            and item.terminal_action_state == "delivered"
        )
        beat_terminal = tuple(
            item
            for item in beat.history
            if item.state == "settled"
            and item.receipt_id == receipt.receipt_id
            and item.terminal_action_state == "delivered"
        )
        if len(plan_terminal) != 1 or len(beat_terminal) != 1:
            raise InteractionActProposalCompilerError("delivered_terminal_not_exact")

        plan_event, _ = self._pinned_event(
            projection=projection,
            cursor=cursor,
            event_ref=plan.event_ref,
            payload_hash=plan.event_payload_hash,
            event_type="ExpressionPlanAccepted",
        )
        beat_event, _ = self._pinned_event(
            projection=projection,
            cursor=cursor,
            event_ref=beat.event_ref,
            payload_hash=beat.event_payload_hash,
            event_type="ExpressionBeatAuthorized",
        )
        stored_event, stored_commit = self._pinned_event(
            projection=projection,
            cursor=cursor,
            event_ref=stored.event_ref,
            payload_hash=stored.event_payload_hash,
            event_type="MessagePayloadStored",
        )
        try:
            plan_payload = ExpressionPlanAcceptedPayload.model_validate_json(
                plan_event.payload_json,
                strict=True,
            )
            beat_payload = ExpressionBeatAuthorizedPayload.model_validate_json(
                beat_event.payload_json,
                strict=True,
            )
            stored_payload = MessagePayloadStoredPayload.model_validate_json(
                stored_event.payload_json,
                strict=True,
            )
        except ValueError as exc:
            raise InteractionActProposalCompilerError("delivered_expression_event_invalid") from exc
        if (
            plan_payload.acceptance_id != plan.acceptance_id
            or plan_payload.proposal_id != plan.proposal_id
            or plan_payload.expression_change_id != plan.expression_change_id
            or plan_payload.plan_id != plan.plan_id
            or beat_payload.acceptance_id != beat.acceptance_id
            or beat_payload.proposal_id != beat.proposal_id
            or beat_payload.expression_change_id != beat.expression_change_id
            or beat_payload.beat.plan_id != beat.plan_id
            or beat_payload.beat.beat_id != beat.beat_id
            or beat_payload.beat.payload.payload_ref != beat.payload_ref
            or beat_payload.beat.payload.payload_hash != beat.payload_hash
            or stored_payload.acceptance_id != stored.acceptance_id
            or stored_payload.proposal_id != stored.proposal_id
            or stored_payload.message.payload_ref != stored.payload_ref
            or stored_payload.message.payload_hash != stored.payload_hash
            or stored_payload.message.text != stored.text
            or stored_payload.message.content_type != stored.content_type
        ):
            raise InteractionActProposalCompilerError("delivered_expression_event_mismatch")

        action_events = self._committed_payload_events(
            projection=projection,
            cursor=cursor,
            event_type="ActionAuthorized",
            nested_key="action",
            identity_key="action_id",
            identity_value=action.action_id,
            payload_model=Action,
        )
        receipt_events = self._committed_payload_events(
            projection=projection,
            cursor=cursor,
            event_type="ExecutionReceiptRecorded",
            nested_key="receipt",
            identity_key="receipt_id",
            identity_value=receipt.receipt_id,
            payload_model=ExecutionReceipt,
        )
        if len(action_events) != 1 or len(receipt_events) != 1:
            raise InteractionActProposalCompilerError("delivered_authority_event_not_exact")
        action_event, _, authorized = action_events[0]
        receipt_event, receipt_commit, recorded_receipt = receipt_events[0]
        if (
            authorized.action_id != action.action_id
            or authorized.world_id != action.world_id
            or authorized.actor != action.actor
            or authorized.target != action.target
            or authorized.payload_ref != action.payload_ref
            or authorized.payload_hash != action.payload_hash
            or authorized.expression_plan_id != action.expression_plan_id
            or authorized.expression_beat_id != action.expression_beat_id
            or authorized.state != "authorized"
            or recorded_receipt != receipt
            or stored_event.world_id != action.world_id
            or action_event.world_id != action.world_id
            or receipt_event.world_id != action.world_id
        ):
            raise InteractionActProposalCompilerError("delivered_authority_event_mismatch")
        return (
            DeliveredExpressionInteractionActSource(
                world_id=self._ledger.world_id,
                conversation_ref=conversation_ref,
                source_event_ref=stored_event.event_id,
                source_world_revision=stored_commit.world_revision,
                source_payload_hash=stored_event.payload_hash,
                source_actor_ref=action.actor,
                source_text=stored.text,
                expression_plan_id=plan.plan_id,
                expression_plan_event_ref=plan_event.event_id,
                expression_plan_event_payload_hash=plan_event.payload_hash,
                expression_beat_id=beat.beat_id,
                expression_beat_event_ref=beat_event.event_id,
                expression_beat_event_payload_hash=beat_event.payload_hash,
                stored_payload_event_ref=stored_event.event_id,
                stored_payload_event_payload_hash=stored_event.payload_hash,
                action_id=action.action_id,
                action_payload_hash=action.payload_hash,
                action_target_ref=action_target_ref,
                action_event_ref=action_event.event_id,
                action_event_payload_hash=action_event.payload_hash,
                receipt_id=receipt.receipt_id,
                receipt_event_ref=receipt_event.event_id,
                receipt_world_revision=receipt_commit.world_revision,
                receipt_payload_hash=receipt_event.payload_hash,
                receipt_status="delivered",
            ),
            stored_event,
        )

    def _pinned_event(
        self,
        *,
        projection,
        cursor: ProjectionCursor,
        event_ref: str,
        payload_hash: str,
        event_type: str,
    ) -> tuple[WorldEvent, CommittedWorldEventRef]:
        committed = tuple(
            item for item in projection.committed_world_event_refs if item.event_id == event_ref
        )
        located = self._ledger.lookup_event_commit(event_ref)
        if len(committed) != 1 or located is None:
            raise InteractionActProposalCompilerError("source_event_not_exact")
        event, commit = located
        authority = committed[0]
        if (
            event.world_id != self._ledger.world_id
            or event.event_type != event_type
            or authority.event_type != event_type
            or event.payload_hash != payload_hash
            or authority.payload_hash != payload_hash
            or event.event_id not in commit.event_ids
            or authority.world_revision > commit.world_revision
            or authority.logical_time != event.logical_time
            or commit.world_revision > cursor.world_revision
            or commit.deliberation_revision > cursor.deliberation_revision
            or commit.ledger_sequence > cursor.ledger_sequence
        ):
            raise InteractionActProposalCompilerError("source_event_mismatch")
        return event, authority

    def _committed_payload_events(
        self,
        *,
        projection,
        cursor: ProjectionCursor,
        event_type: str,
        nested_key: str,
        identity_key: str,
        identity_value: str,
        payload_model,
    ) -> tuple[tuple[WorldEvent, CommittedWorldEventRef, object], ...]:
        matches: list[tuple[WorldEvent, CommittedWorldEventRef, object]] = []
        for committed in projection.committed_world_event_refs:
            if committed.event_type != event_type:
                continue
            event, commit = self._pinned_event(
                projection=projection,
                cursor=cursor,
                event_ref=committed.event_id,
                payload_hash=committed.payload_hash,
                event_type=event_type,
            )
            raw = event.payload().get(nested_key)
            if not isinstance(raw, dict) or raw.get(identity_key) != identity_value:
                continue
            try:
                parsed = payload_model.model_validate_json(
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    strict=True,
                )
            except ValueError as exc:
                raise InteractionActProposalCompilerError("source_event_payload_invalid") from exc
            matches.append((event, commit, parsed))
        return tuple(matches)

    @staticmethod
    def _proposal_event(
        *,
        world_id: str,
        authority,
        typed: InteractionActProposalRecordedPayload,
        source_event: WorldEvent,
        logical_time: datetime,
    ) -> WorldEvent:
        payload = typed.model_dump(mode="json")
        identity = _canonical_hash(
            {
                "contract": _CONTRACT,
                "world_id": world_id,
                "source_proposal_event_ref": authority.audit.event_ref,
                "proposal_id": typed.proposal_id,
                "change_id": typed.change_id,
                "mutation_payload_hash": typed.mutation_payload_hash,
            }
        )
        event_id = f"event:interaction-act-proposal:{identity}"
        idempotency_key = domain_idempotency_key(
            event_type="InteractionActProposalRecorded",
            world_id=world_id,
            payload=payload,
        )
        if idempotency_key is None:
            raise InteractionActProposalCompilerError("candidate_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=world_id,
            event_type="InteractionActProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="worker:interaction-act-proposal-compiler",
            source=_CONTRACT,
            trace_id=source_event.trace_id,
            causation_id=authority.audit.event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=idempotency_key,
            payload=payload,
        )


__all__ = [
    "InteractionActProposalCompilation",
    "InteractionActProposalCompiler",
    "InteractionActProposalCompilerError",
]
