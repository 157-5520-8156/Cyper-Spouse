"""Compile one audited tool-request decision into the existing acceptance lane.

The generic model envelope is intentionally not an authority to call a tool.
This compiler is the narrow bridge: it verifies the claimed Observation and
trigger, freezes query bytes from the immutable audit, resolves an exact
enforcement authorization binding, then delegates the atomic request/budget/
Action commit to :mod:`read_only_tool`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .ledger import LedgerPort
from .read_only_tool import ReadOnlyToolAcceptanceRuntime, ReadOnlyToolProposal
from .schema_core import FrozenModel
from .schemas import Observation, ProjectionCursor, ReadOnlyToolAuthorizationBinding


_CONTRACT = "read-only-tool-proposal-compiler.1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def tool_query_ref(*, proposal_id: str, change_id: str) -> str:
    """Opaque immutable query reference derived from a persisted audit event."""

    return "proposal-audit-tool-query:" + _digest(
        {"contract": _CONTRACT, "proposal_id": proposal_id, "change_id": change_id}
    )


class ReadOnlyToolAuthorizationResolver(Protocol):
    """Resolve only an exact enforcement triple; never a shadow decision."""

    def resolve(
        self,
        *,
        projection: object,
        actor_ref: str,
        subject_ref: str,
        target: str,
        logical_time: object,
    ) -> ReadOnlyToolAuthorizationBinding: ...


class ReadOnlyToolProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"read_only_tool_proposal_compiler.{code}"
        super().__init__(self.code)


class ReadOnlyToolProposalCompilation(FrozenModel):
    status: str
    source_proposal_id: str
    request_id: str | None = None
    action_id: str | None = None
    commit_world_revision: int | None = None


class ReadOnlyToolProposalCompiler:
    """One deep, source-bound proposal-to-acceptance module."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        authorization_resolver: ReadOnlyToolAuthorizationResolver,
        actor_ref: str,
    ) -> None:
        if not actor_ref:
            raise ValueError("read-only tool compiler actor is required")
        self._ledger = ledger
        self._authority = DecisionProposalAuthorityReader(ledger=ledger)
        self._authorization = authorization_resolver
        self._actor_ref = actor_ref

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def accept(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        proposal_id: str,
        actor: str,
        source: str,
    ) -> ReadOnlyToolProposalCompilation:
        authority = self._authority.read(
            self._authority.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        if not authority.proposal.proposed_changes:
            return ReadOnlyToolProposalCompilation(status="no_change", source_proposal_id=proposal_id)
        change, intent, source_event, source_commit, subject_ref = self._verify(
            authority=authority, cursor=cursor
        )
        raw = change.payload.value()
        query = str(raw["query"])
        query_hash = "sha256:" + hashlib.sha256(query.encode()).hexdigest()
        expected_ref = tool_query_ref(
            proposal_id=authority.proposal.proposal_id, change_id=change.change_id
        )
        if intent.payload_ref != expected_ref or intent.payload_hash != query_hash:
            raise ReadOnlyToolProposalCompilerError("query_intent_binding_invalid")
        projection = self._ledger.project_at(cursor)
        authorization = self._authorization.resolve(
            projection=projection,
            actor_ref=self._actor_ref,
            subject_ref=subject_ref,
            target=str(raw["target"]),
            logical_time=projection.logical_time or source_event.logical_time,
        )
        proposal = ReadOnlyToolProposal(
            proposal_id=authority.proposal.proposal_id,
            source_event_ref=source_event.event_id,
            source_world_revision=source_commit.world_revision,
            source_payload_hash=source_event.payload_hash,
            tool_name=str(raw["tool_name"]),
            target=str(raw["target"]),
            query_ref=expected_ref,
            query_hash=query_hash,
            budget_account_id=str(raw["budget_account_id"]),
            budget_limit=int(raw["budget_limit"]),
            authorization=authorization,
        )
        commit = ReadOnlyToolAcceptanceRuntime(ledger=self._ledger).accept(
            proposal=proposal,
            actor=actor,
            source=source,
            logical_time=projection.logical_time or source_event.logical_time,
            created_at=source_event.created_at,
            trace_id=source_event.trace_id,
            correlation_id=source_event.correlation_id,
        )
        return ReadOnlyToolProposalCompilation(
            status="accepted",
            source_proposal_id=proposal_id,
            request_id=proposal.request_id,
            action_id=proposal.action_id,
            commit_world_revision=commit.world_revision,
        )

    def _verify(self, *, authority, cursor: ProjectionCursor):
        proposal = authority.proposal
        if len(proposal.proposed_changes) != 1 or len(proposal.action_intents) != 1:
            raise ReadOnlyToolProposalCompilerError("proposal_shape_invalid")
        change = proposal.proposed_changes[0]
        intent = proposal.action_intents[0]
        if (
            change.kind != "read_only_tool_request"
            or change.transition != "request"
            or change.expected_entity_revision is not None
            or intent.kind != "read_only_tool"
            or intent.layer != "read_only_tool"
            or intent.causal_change_id != change.change_id
            or intent.beat_ref is not None
            or intent.dependencies
            or intent.due_window is not None
        ):
            raise ReadOnlyToolProposalCompilerError("proposal_shape_invalid")
        raw = change.payload.value()
        if change.target_id != raw["target"] or intent.target != raw["target"]:
            raise ReadOnlyToolProposalCompilerError("target_binding_invalid")
        source = self._ledger.lookup_event_commit(authority.audit.trigger_ref)
        if source is None or source[0].event_type != "ObservationRecorded":
            raise ReadOnlyToolProposalCompilerError("source_observation_missing")
        source_event, source_commit = source
        observation = Observation.model_validate_json(source_event.payload_json)
        projection = self._ledger.project_at(cursor)
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "read_only_tool_deliberation"
                and item.state == "claimed"
                and item.source_evidence_ref == observation.observation_id
                and item.trigger_ref == f"read-only-tool:{observation.observation_id}"
            ),
            None,
        )
        evidence = next((item for item in proposal.evidence_refs if item.ref_id == observation.observation_id), None)
        if (
            process is None
            or source_commit.world_revision > cursor.world_revision
            or proposal.evaluated_world_revision != cursor.world_revision
            or tuple(change.evidence_refs) != (observation.observation_id,)
            or evidence is None
            or evidence.evidence_kind != "observed_message"
            or evidence.source_world_revision != source_commit.world_revision
            or evidence.immutable_hash != "sha256:" + source_event.payload_hash
        ):
            raise ReadOnlyToolProposalCompilerError("source_authority_invalid")
        return change, intent, source_event, source_commit, observation.actor


__all__ = [
    "ReadOnlyToolAuthorizationResolver",
    "ReadOnlyToolProposalCompilation",
    "ReadOnlyToolProposalCompiler",
    "ReadOnlyToolProposalCompilerError",
    "tool_query_ref",
]
