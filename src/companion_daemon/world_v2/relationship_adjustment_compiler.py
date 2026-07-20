"""Compile an accepted relationship signal into one bounded slow adjustment.

This second stage has no model call.  It preserves the model's already audited
six-axis suggestion on ``RelationshipSignalProjection`` and deterministically
clips it against the installed policy before proposing an explicit mutation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .relationship_events import RelationshipSlowVariableAdjustedPayload, relationship_mutation_hash
from .relationship_reducers import preview_relationship_slow_variable_adjustment
from .relationship_adjustment_trigger import relationship_adjustment_trigger_id
from .schema_core import FrozenModel
from .schemas import (
    CommitResult,
    ProjectionCursor,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipVariableDeltas,
    WorldEvent,
)


_POLICY_REFS = ("policy:relationship-v1",)
_DELTA_CAP_BP = 500
_CONTRACT = "relationship-adjustment-compiler.1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def relationship_adjustment_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str
) -> str:
    return "event:relationship-adjustment-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": "RelationshipSlowVariableAdjusted",
        }
    )


class RelationshipAdjustmentCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"relationship_adjustment_compiler.{code}"
        super().__init__(self.code)


class RelationshipAdjustmentCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    signal_id: str
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None


class RelationshipAdjustmentCompiler:
    """Deep compiler for exactly one, previously accepted relationship signal."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, signal_id: str
    ) -> RelationshipAdjustmentCompilation:
        if world_id != self._ledger.world_id:
            raise RelationshipAdjustmentCompilerError("world_mismatch")
        projection = self._ledger.project_at(cursor)
        signal = next((item for item in projection.relationship_signals if item.signal_id == signal_id), None)
        if signal is None:
            raise RelationshipAdjustmentCompilerError("signal_not_accepted")
        source_event = self._source_signal_event(signal=signal, projection=projection)
        self._require_claimed_trigger(
            projection=projection, signal_event=source_event, world_id=world_id
        )
        if self._is_consumed(signal_id=signal.signal_id, projection=projection):
            return RelationshipAdjustmentCompilation(status="no_change", signal_id=signal.signal_id)
        accepted = self._clip(signal.suggested_deltas)
        if not any(accepted.model_dump().values()):
            return RelationshipAdjustmentCompilation(status="no_change", signal_id=signal.signal_id)
        if projection.logical_time is None:
            raise RelationshipAdjustmentCompilerError("logical_time_missing")
        typed = self._compile(signal=signal, accepted_deltas=accepted, projection=projection)
        event = self._proposal_event(typed=typed, source_event=source_event, logical_time=projection.logical_time)
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:relationship-adjustment-compiler:"
            + _digest({"cursor": cursor.model_dump(mode="json"), "signal": signal.signal_id}),
        )
        return RelationshipAdjustmentCompilation(
            status="candidate_recorded", signal_id=signal.signal_id, typed_proposal_id=typed.proposal_id, commit=commit
        )

    @staticmethod
    def _clip(deltas: RelationshipVariableDeltas) -> RelationshipVariableDeltas:
        return RelationshipVariableDeltas(
            **{
                name: min(_DELTA_CAP_BP, max(-_DELTA_CAP_BP, getattr(deltas, name)))
                for name in type(deltas).model_fields
            }
        )

    def _compile(self, *, signal, accepted_deltas, projection) -> RelationshipProposalProjection:
        identity = _digest(
            {
                "contract": _CONTRACT,
                "signal_id": signal.signal_id,
                "signal_event": signal.origin.accepted_event_ref,
            }
        )
        proposal_id = f"proposal:relationship-adjustment:{identity}"
        change_id = f"change:relationship-adjustment:{identity}"
        transition_id = f"transition:relationship-adjustment:{identity}"
        preview = preview_relationship_slow_variable_adjustment(
            states=projection.relationship_states,
            history=projection.relationship_adjustments,
            signals=projection.relationship_signals,
            subject_ref=signal.subject_ref,
            signal_refs=(signal.signal_id,),
            proposed_deltas=signal.suggested_deltas,
            accepted_deltas=accepted_deltas,
            logical_time=projection.logical_time,
        )
        mutation: dict[str, object] = {
            "change_id": change_id,
            "transition_id": transition_id,
            "expected_entity_revision": preview.expected_entity_revision,
            "evidence_refs": signal.evidence_refs,
            "policy_refs": _POLICY_REFS,
            "acceptance_id": f"acceptance:relationship-adjustment:{identity}",
            "proposal_id": proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "relationship_id": preview.relationship_id,
            "subject_ref": signal.subject_ref,
            "adjustment_id": f"adjustment:relationship:{identity}",
            "operation": "adjust",
            "signal_refs": (signal.signal_id,),
            "proposed_deltas": signal.suggested_deltas.model_dump(mode="json"),
            "accepted_deltas": accepted_deltas.model_dump(mode="json"),
            "variables_before": preview.variables_before.model_dump(mode="json"),
            "variables_after": preview.variables_after.model_dump(mode="json"),
            "stage_before": preview.stage_before,
            "stage_after": preview.stage_after,
            # This payload is validated as Python data before its canonical
            # JSON transport is derived below; serializing datetimes here
            # would make candidate_since fail Pydantic's datetime contract.
            "hysteresis_before": preview.hysteresis_before.model_dump(mode="python"),
            "hysteresis_after": preview.hysteresis_after.model_dump(mode="python"),
            "commitment_refs": preview.commitment_refs,
            "confidence_bp": signal.confidence_bp,
            "persistence": signal.persistence,
            "contradiction_group_ref": signal.contradiction_group_ref,
            "rationale_code": signal.rationale_code,
            "policy_version": preview.policy_version,
            "policy_digest": preview.policy_digest,
            "adjusted_at": projection.logical_time,
            "compensates_adjustment_id": None,
        }
        mutation["accepted_change_hash"] = relationship_mutation_hash(mutation)
        # Hashing accepts the raw mapping and normalizes it through the same
        # JSON transport used by the authority contract. Validate only after
        # the hash is populated, because the payload validator correctly
        # refuses a placeholder hash.
        mutation = RelationshipSlowVariableAdjustedPayload.model_validate(
            mutation
        ).model_dump(mode="json")
        return RelationshipProposalProjection(
            proposal_id=proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:relationship.1",
            transition_kind="adjust",
            change_id=change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=preview.expected_entity_revision,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=signal.evidence_refs,
            policy_refs=_POLICY_REFS,
            proposed_mutation=RelationshipProposedMutation(
                event_type="RelationshipSlowVariableAdjusted", payload_json=_canonical(mutation)
            ),
        )

    def _source_signal_event(self, *, signal, projection) -> WorldEvent:
        located = self._ledger.lookup_event_commit(signal.origin.accepted_event_ref)
        if located is None or located[0].event_type != "RelationshipSignalAccepted":
            raise RelationshipAdjustmentCompilerError("signal_event_unavailable")
        event, commit = located
        if commit.world_revision > projection.world_revision:
            raise RelationshipAdjustmentCompilerError("signal_event_outside_cursor")
        return event

    @staticmethod
    def _require_claimed_trigger(*, projection, signal_event: WorldEvent, world_id: str) -> None:
        trigger_id = relationship_adjustment_trigger_id(
            world_id=world_id, signal_event_id=signal_event.event_id
        )
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id),
            None,
        )
        if (
            process is None
            or process.process_kind != "relationship_adjustment"
            or process.state != "claimed"
            or process.source_evidence_ref != signal_event.event_id
        ):
            raise RelationshipAdjustmentCompilerError("adjustment_trigger_not_claimed")

    @staticmethod
    def _is_consumed(*, signal_id: str, projection) -> bool:
        return any(
            item.operation == "adjust" and signal_id in item.signal_refs
            for item in projection.relationship_adjustments
        )

    def _proposal_event(self, *, typed: RelationshipProposalProjection, source_event: WorldEvent, logical_time) -> WorldEvent:
        # Keep the same complete projection image as the relationship signal
        # compiler; the shared authority reader verifies this exact payload.
        payload = typed.model_dump(mode="json")
        identity = domain_idempotency_key(event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise RelationshipAdjustmentCompilerError("event_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:relationship-adjustment-proposal:" + _digest({"world": self._ledger.world_id, "proposal": typed.proposal_id}),
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="world-v2:relationship-adjustment-compiler",
            source="world-v2:relationship-adjustment-compiler",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )


__all__ = [
    "RelationshipAdjustmentCompilation",
    "RelationshipAdjustmentCompiler",
    "RelationshipAdjustmentCompilerError",
    "relationship_adjustment_mutation_event_id",
]
