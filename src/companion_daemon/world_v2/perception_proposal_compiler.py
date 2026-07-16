"""Narrow audited-decision bridge for optional vision/transcription requests."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .perception import PerceptionAcceptanceRuntime, PerceptionProposal
from .schema_core import FrozenModel
from .schemas import Observation, PerceptionAuthorizationBinding, ProjectionCursor


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def perception_input_ref(*, proposal_id: str, change_id: str) -> str:
    return "proposal-audit-perception-input:" + _digest(
        {"proposal": proposal_id, "change": change_id}
    )


class PerceptionAuthorizationResolver(Protocol):
    def resolve(
        self,
        *,
        projection: object,
        actor_ref: str,
        subject_ref: str,
        target: str,
        logical_time: object,
    ) -> PerceptionAuthorizationBinding: ...


class PerceptionProposalCompilerError(ValueError):
    pass


class PerceptionProposalCompilation(FrozenModel):
    status: str
    source_proposal_id: str
    request_id: str | None = None
    action_id: str | None = None


class PerceptionProposalCompiler:
    def __init__(
        self,
        *,
        ledger,
        authorization_resolver: PerceptionAuthorizationResolver,
        actor_ref: str,
        budget_account_id: str,
        budget_limit: int,
    ) -> None:
        if not actor_ref or not budget_account_id or budget_limit <= 0:
            raise ValueError("perception compiler needs deployment policy")
        self._ledger, self._authority, self._authorization = (
            ledger,
            DecisionProposalAuthorityReader(ledger=ledger),
            authorization_resolver,
        )
        self._actor_ref, self._budget_account_id, self._budget_limit = (
            actor_ref,
            budget_account_id,
            budget_limit,
        )

    def accept(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str, actor: str, source: str
    ) -> PerceptionProposalCompilation:
        authority = self._authority.read(
            self._authority.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        if not authority.proposal.proposed_changes:
            return PerceptionProposalCompilation(status="no_change", source_proposal_id=proposal_id)
        proposal = authority.proposal
        if len(proposal.proposed_changes) != 1 or len(proposal.action_intents) != 1:
            raise PerceptionProposalCompilerError("perception proposal shape is invalid")
        change, intent = proposal.proposed_changes[0], proposal.action_intents[0]
        raw = change.payload.value()
        kind = raw.get("analysis_kind")
        if (
            change.kind != "perception_request"
            or change.transition != "request"
            or kind not in {"vision", "transcription"}
            or change.target_id != f"perception:{kind}"
            or intent.kind != kind
            or intent.layer != "perception_tool"
            or intent.target != change.target_id
            or intent.causal_change_id != change.change_id
            or raw.get("budget_account_id") != self._budget_account_id
            or raw.get("budget_limit") != self._budget_limit
        ):
            raise PerceptionProposalCompilerError("perception proposal shape is invalid")
        input_body = str(raw.get("input_body", ""))
        input_hash = "sha256:" + hashlib.sha256(input_body.encode()).hexdigest()
        expected_ref = perception_input_ref(
            proposal_id=proposal.proposal_id, change_id=change.change_id
        )
        if intent.payload_ref != expected_ref or intent.payload_hash != input_hash:
            raise PerceptionProposalCompilerError("perception input audit binding is invalid")
        source_pair = self._ledger.lookup_event_commit(authority.audit.trigger_ref)
        if source_pair is None or source_pair[0].event_type != "ObservationRecorded":
            raise PerceptionProposalCompilerError("perception source observation is missing")
        source_event, source_commit = source_pair
        observation = Observation.model_validate_json(source_event.payload_json)
        evidence = next(
            (item for item in proposal.evidence_refs if item.ref_id == observation.observation_id),
            None,
        )
        projection = self._ledger.project_at(cursor)
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "perception_deliberation"
                and item.state == "claimed"
                and item.source_evidence_ref == observation.observation_id
            ),
            None,
        )
        if (
            process is None
            or proposal.evaluated_world_revision != cursor.world_revision
            or source_commit.world_revision > cursor.world_revision
            or evidence is None
            or evidence.immutable_hash != "sha256:" + source_event.payload_hash
        ):
            raise PerceptionProposalCompilerError("perception source authority is invalid")
        authorization = self._authorization.resolve(
            projection=projection,
            actor_ref=self._actor_ref,
            subject_ref=observation.actor,
            target=change.target_id,
            logical_time=projection.logical_time or source_event.logical_time,
        )
        accepted = PerceptionProposal(
            proposal_id=proposal.proposal_id,
            source_event_ref=source_event.event_id,
            source_world_revision=source_commit.world_revision,
            source_payload_hash=source_event.payload_hash,
            analysis_kind=kind,
            input_ref=expected_ref,
            input_hash=input_hash,
            content_privacy_class=raw.get("content_privacy_class", "private"),
            budget_account_id=self._budget_account_id,
            budget_limit=self._budget_limit,
            authorization=authorization,
        )
        PerceptionAcceptanceRuntime(ledger=self._ledger).accept(
            proposal=accepted,
            actor=actor,
            source=source,
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            correlation_id=source_event.correlation_id,
        )
        return PerceptionProposalCompilation(
            status="accepted",
            source_proposal_id=proposal_id,
            request_id=accepted.request_id,
            action_id=accepted.action_id,
        )


__all__ = [
    "PerceptionAuthorizationResolver",
    "PerceptionProposalCompilation",
    "PerceptionProposalCompiler",
    "PerceptionProposalCompilerError",
    "perception_input_ref",
]
