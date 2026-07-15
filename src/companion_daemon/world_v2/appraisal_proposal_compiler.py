"""Compile an audited generic appraisal decision into a typed Appraisal candidate.

The compiler is deliberately the only bridge between a model's generic
``appraisal_transition`` and the source-bound Appraisal acceptance lane.  It
does not accept a proposal, interpret an uncommitted message, or call a model.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .appraisal_acceptance_runtime import appraisal_mutation_event_id
from .appraisal_events import appraisal_mutation_hash
from .batch_invariants import interaction_appraisal_trigger_identity
from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schema_core import EvidenceRef, FrozenModel
from .schemas import (
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    AppraisalProposalProjection,
    CommitResult,
    Observation,
    ProjectionCursor,
    WorldEvent,
)


_CONTRACT = "appraisal-proposal-compiler.1"
_POLICY_REFS = ("policy:appraisal-v1",)
_MATRIX_VERSION = "appraisal-matrix.1"
_CLUSTERING_POLICY_VERSION = "source-clustering.1"
_ALLOWED_MEANINGS = set(AppraisalHypothesis.model_fields["meaning"].annotation.__args__)
_ALLOWED_ATTRIBUTIONS = set(AppraisalHypothesis.model_fields["attribution"].annotation.__args__)
_EVIDENCE_TYPE_BY_KIND = {
    "committed_fact": "committed_fact",
    "committed_experience": "committed_experience",
    "committed_world_event": "committed_world_event",
    "settled_world_event": "settled_world_event",
    "settled_external_result": "settled_external_result",
    "observed_message": "observed_message",
    "active_plan": "active_plan",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class AppraisalProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"appraisal_proposal_compiler.{code}"
        super().__init__(self.code)


class AppraisalProposalCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None


class AppraisalProposalCompiler:
    """Deep compiler for the source-bound ``activate`` Appraisal lane.

    The public interface is intentionally one method.  It verifies generic
    proposal authority at an exact cursor, derives all IDs and source bindings
    internally, and records one deliberation-only typed candidate.  Acceptance
    remains exclusively owned by :class:`AppraisalAcceptanceRuntime`.
    """

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    @property
    def ledger(self) -> LedgerPort:
        """The immutable composition dependency shared with Acceptance."""

        return self._ledger

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> AppraisalProposalCompilation:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        changes = tuple(
            item for item in authority.proposal.proposed_changes if item.kind == "appraisal_transition"
        )
        if not changes:
            return AppraisalProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(changes) != 1:
            raise AppraisalProposalCompilerError("appraisal_change_count_invalid")
        change = changes[0]
        if change.transition != "activate":
            raise AppraisalProposalCompilerError("transition_not_implemented")
        projection = self._ledger.project_at(cursor)
        source_event = self._event(authority.audit.trigger_ref)
        observation = self._observation(source_event)
        typed = self._compile_activate(
            authority=authority,
            change=change,
            projection=projection,
            observation=observation,
        )
        event = self._proposal_event(
            typed=typed,
            source_event=source_event,
            source_proposal_event_ref=authority.audit.event_ref,
            logical_time=projection.logical_time,
        )
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:appraisal-proposal-compiler:"
            + _digest(
                {
                    "cursor": cursor.model_dump(mode="json"),
                    "source": authority.audit.event_ref,
                    "typed_proposal_id": typed.proposal_id,
                }
            ),
        )
        return AppraisalProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            typed_proposal_id=typed.proposal_id,
            commit=commit,
        )

    def _compile_activate(self, *, authority, change, projection, observation: Observation):
        trigger_id = interaction_appraisal_trigger_identity(self._ledger.world_id, observation.observation_id)
        trigger = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if (
            trigger is None
            or trigger.process_kind != "interaction_appraisal"
            or trigger.state != "claimed"
            or trigger.source_evidence_ref != observation.observation_id
        ):
            raise AppraisalProposalCompilerError("source_trigger_not_claimed")
        if change.expected_entity_revision != 0:
            raise AppraisalProposalCompilerError("activate_requires_new_entity")
        raw = change.payload.value()
        evidence = self._evidence(proposal=authority.proposal, refs=change.evidence_refs)
        source_evidence = next(
            (item for item in evidence if item.ref_id == observation.observation_id), None
        )
        if source_evidence is None:
            raise AppraisalProposalCompilerError("source_evidence_missing")
        if source_evidence.evidence_type != "observed_message":
            raise AppraisalProposalCompilerError("source_evidence_kind_invalid")
        identity = _digest(
            {
                "source_proposal_event": authority.audit.event_ref,
                "source_change": change.change_id,
                "contract": _CONTRACT,
            }
        )
        proposal_id = f"proposal:appraisal-compiled:{identity}"
        transition_id = f"transition:appraisal-compiled:{identity}"
        mutation_event_id = appraisal_mutation_event_id(
            world_id=self._ledger.world_id,
            proposal_id=proposal_id,
            transition_id=transition_id,
            event_type="AppraisalAccepted",
        )
        appraisal = AppraisalProjection(
            appraisal_id=f"appraisal:compiled:{identity}",
            entity_revision=1,
            subject_ref=observation.actor,
            source_cluster_ref=self._source_cluster(observation),
            origin=AppraisalOrigin(
                change_id=change.change_id,
                transition_id=transition_id,
                policy_refs=_POLICY_REFS,
                matrix_catalog_version=_MATRIX_VERSION,
                clustering_policy_version=_CLUSTERING_POLICY_VERSION,
                accepted_event_ref=mutation_event_id,
            ),
            hypotheses=self._hypotheses(raw=raw, identity=identity),
            evidence_refs=evidence,
            confidence_bp=int(raw["confidence"]),
            accepted_at=projection.logical_time,
            expires_at=self._expiry(raw=raw, at=projection.logical_time),
        )
        mutation: dict[str, object] = {
            "change_id": change.change_id,
            "transition_id": transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": [item.model_dump(mode="json") for item in evidence],
            "policy_refs": list(_POLICY_REFS),
            "acceptance_id": f"acceptance:appraisal-compiled:{identity}",
            "proposal_id": proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "trigger_id": trigger_id,
            "appraisal": appraisal.model_dump(mode="json"),
        }
        mutation["accepted_change_hash"] = appraisal_mutation_hash(mutation)
        return AppraisalProposalProjection(
            proposal_id=proposal_id,
            transition_kind="accept",
            change_id=change.change_id,
            trigger_id=trigger_id,
            trigger_ref=trigger.trigger_ref,
            source_evidence_ref=observation.observation_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=evidence,
            policy_refs=_POLICY_REFS,
            proposed_mutation={
                "event_type": "AppraisalAccepted",
                "payload_json": _canonical(mutation),
            },
        )

    @staticmethod
    def _source_cluster(observation: Observation) -> str:
        return "conversation:" + _digest(
            {"actor": observation.actor, "channel": observation.channel}
        )

    @staticmethod
    def _expiry(*, raw: dict[str, object], at):
        expiry = raw["expiry"]
        if expiry is None:
            return at + timedelta(hours=2)
        if expiry <= at:
            raise AppraisalProposalCompilerError("expiry_not_future")
        return expiry

    @staticmethod
    def _severity(value: int) -> str:
        if value <= 2_500:
            return "low"
        if value <= 6_000:
            return "moderate"
        if value <= 8_500:
            return "high"
        return "acute"

    def _hypotheses(self, *, raw: dict[str, object], identity: str):
        attribution = raw["attribution"]
        if attribution not in _ALLOWED_ATTRIBUTIONS:
            raise AppraisalProposalCompilerError("attribution_invalid")
        candidates = raw["meaning_candidates"]
        if not candidates:
            raise AppraisalProposalCompilerError("meanings_missing")
        if any(item["meaning"] not in _ALLOWED_MEANINGS for item in candidates):
            raise AppraisalProposalCompilerError("meaning_invalid")
        if len({item["meaning"] for item in candidates}) != len(candidates):
            raise AppraisalProposalCompilerError("meanings_duplicate")
        total = sum(int(item["confidence"]) for item in candidates)
        if total <= 0:
            raise AppraisalProposalCompilerError("meaning_weights_zero")
        weights = [int(item["confidence"]) * 10_000 // total for item in candidates]
        weights[0] += 10_000 - sum(weights)
        severity = self._severity(int(raw["severity"]))
        return tuple(
            AppraisalHypothesis(
                hypothesis_id=f"meaning:appraisal-compiled:{identity}:{index}",
                meaning=item["meaning"],
                attribution=attribution,
                controllability="partly_controllable",
                severity=severity,
                weight_bp=weights[index],
            )
            for index, item in enumerate(candidates)
        )

    def _evidence(self, *, proposal, refs: tuple[str, ...]) -> tuple[EvidenceRef, ...]:
        by_id = {item.ref_id: item for item in proposal.evidence_refs}
        if not refs or len(set(refs)) != len(refs) or any(ref not in by_id for ref in refs):
            raise AppraisalProposalCompilerError("evidence_not_authoritative")
        result: list[EvidenceRef] = []
        for ref in refs:
            source = by_id[ref]
            evidence_type = _EVIDENCE_TYPE_BY_KIND.get(source.evidence_kind)
            if evidence_type is None:
                raise AppraisalProposalCompilerError("evidence_kind_invalid")
            result.append(
                EvidenceRef(
                    ref_id=source.ref_id,
                    evidence_type=evidence_type,
                    claim_purpose="private_hypothesis",
                    source_world_revision=source.source_world_revision,
                    immutable_hash=source.immutable_hash.removeprefix("sha256:"),
                )
            )
        return tuple(result)

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None:
            raise AppraisalProposalCompilerError("source_event_missing")
        return located[0]

    @staticmethod
    def _observation(event: WorldEvent) -> Observation:
        if event.event_type != "ObservationRecorded":
            raise AppraisalProposalCompilerError("trigger_not_observation")
        try:
            return Observation.model_validate_json(event.payload_json)
        except ValueError as exc:
            raise AppraisalProposalCompilerError("trigger_observation_invalid") from exc

    def _proposal_event(
        self,
        *,
        typed: AppraisalProposalProjection,
        source_event: WorldEvent,
        source_proposal_event_ref: str,
        logical_time,
    ) -> WorldEvent:
        payload = typed.model_dump(mode="json")
        identity = domain_idempotency_key(
            event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise AppraisalProposalCompilerError("proposal_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:appraisal-proposal-compiled:"
            + _digest({"world_id": self._ledger.world_id, "proposal_id": typed.proposal_id}),
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="worker:appraisal-proposal-compiler",
            source=_CONTRACT,
            trace_id=source_event.trace_id,
            causation_id=source_proposal_event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )


__all__ = [
    "AppraisalProposalCompilation",
    "AppraisalProposalCompiler",
    "AppraisalProposalCompilerError",
]
