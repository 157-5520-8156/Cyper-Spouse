"""Compile one audited relationship-signal suggestion into typed authority.

The model never writes relationship state.  It suggests one bounded signal in
a generic, replayable decision audit; this compiler re-proves the accepted
appraisal and claimed relationship trigger, then derives every authority id
and the only possible ``RelationshipSignalAccepted`` candidate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from .decision_proposal_authority import DecisionProposalAuthorityReader
from .character_interior.relationship_context import (
    relationship_transition_subject_refs,
)
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .relationship_events import relationship_mutation_hash
from .relationship_trigger import (
    relationship_continuity_trigger_id,
    relationship_deliberation_trigger_id,
)
from .schema_core import EvidenceRef, FrozenModel
from .schemas import (
    CommitResult,
    Observation,
    ProjectionCursor,
    RelationshipProposalAuditBinding,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    RelationshipVariableDeltas,
    WorldEvent,
    relationship_signal_fingerprint,
)


_CONTRACT = "relationship-proposal-compiler.1"
_WORLD_STIMULUS_CONTRACT = "relationship-proposal-compiler.world-stimulus.1"
_POLICY_REFS = ("policy:relationship-signal-v1",)
_WORLD_STIMULUS_SOURCE_EVIDENCE = {
    "WorldOccurrenceSettled": "settled_world_event",
    "ExecutionReceiptRecorded": "committed_world_event",
    "ActivityAbandoned": "committed_world_event",
    "PerceptionResultAccepted": "committed_world_event",
    "AppraisalAccepted": "committed_world_event",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def relationship_mutation_event_id(
    *, world_id: str, proposal_id: str, transition_id: str, event_type: str
) -> str:
    return "event:relationship-mutation:" + _digest(
        {
            "world_id": world_id,
            "proposal_id": proposal_id,
            "transition_id": transition_id,
            "event_type": event_type,
        }
    )


class RelationshipProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"relationship_proposal_compiler.{code}"
        super().__init__(self.code)


class RelationshipProposalCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None
    acceptance_cursor: ProjectionCursor | None = None


class RelationshipProposalCompiler:
    """Narrow source-bound compiler for the signal-before-adjustment stage."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger
        self._reader = DecisionProposalAuthorityReader(ledger=ledger)

    @property
    def ledger(self) -> LedgerPort:
        return self._ledger

    def record(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> RelationshipProposalCompilation:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        return self._record_authority(
            authority=authority,
            commit_cursor=cursor,
            identity_world_revision=None,
        )

    def record_rebased(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> RelationshipProposalCompilation:
        """Compile one already-authored signal against the current World head.

        The Character decision is authenticated only at ``audit_cursor``.
        Trigger ownership and the typed signal candidate are checked at
        ``current_cursor`` so expression or same-turn inner-state acceptance
        may advance the World without causing a second character call.
        """

        if (
            current_cursor.ledger_sequence < audit_cursor.ledger_sequence
            or current_cursor.world_revision < audit_cursor.world_revision
            or current_cursor.deliberation_revision < audit_cursor.deliberation_revision
        ):
            raise RelationshipProposalCompilerError("rebase_cursor_precedes_audit")
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not changes:
            return RelationshipProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(changes) != 1 or changes[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        projection = self._ledger.project_at(current_cursor)
        existing = self._existing_rebased_candidate(
            projection=projection,
            authority=authority,
            change=changes[0],
            current_cursor=current_cursor,
        )
        if existing is not None:
            candidate, commit, acceptance_cursor = existing
            return RelationshipProposalCompilation(
                status="candidate_recorded",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
                typed_proposal_id=candidate.proposal_id,
                commit=commit,
                acceptance_cursor=acceptance_cursor,
            )
        return self._record_authority(
            authority=authority,
            commit_cursor=current_cursor,
            identity_world_revision=current_cursor.world_revision,
        )

    def accepted_descendant(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> str | None:
        """Return the accepted typed descendant of one exact audited choice."""

        if (
            current_cursor.ledger_sequence < audit_cursor.ledger_sequence
            or current_cursor.world_revision < audit_cursor.world_revision
            or current_cursor.deliberation_revision < audit_cursor.deliberation_revision
        ):
            raise RelationshipProposalCompilerError("rebase_cursor_precedes_audit")
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if len(changes) != 1 or changes[0].transition != "suggest":
            return None
        accepted = self._accepted_rebased_candidate(
            projection=self._ledger.project_at(current_cursor),
            authority=authority,
            change=changes[0],
        )
        return accepted[0].proposal_id if accepted is not None else None

    def record_world_stimulus_rebased(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event_id: str,
    ) -> RelationshipProposalCompilation:
        """Compile one same-audit world-stimulus signal without a second author.

        This deliberately separate entry point accepts only the three committed
        CharacterInterior world-stimulus event kinds.  It re-proves the exact
        capability subject set recorded by that purpose and never weakens the
        Observation/Appraisal trigger requirements of :meth:`record_rebased`.
        """

        self._require_rebase_order(
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
        )
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not changes:
            return RelationshipProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(changes) != 1 or changes[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        current_projection = self._ledger.project_at(current_cursor)
        source_event, subject, evidence = self._world_stimulus_binding(
            authority=authority,
            change=changes[0],
            audit_projection=self._ledger.project_at(audit_cursor),
            current_projection=current_projection,
            source_event_id=source_event_id,
        )
        existing = self._existing_rebased_candidate(
            projection=current_projection,
            authority=authority,
            change=changes[0],
            current_cursor=current_cursor,
        )
        if existing is not None:
            candidate, commit, acceptance_cursor = existing
            return RelationshipProposalCompilation(
                status="candidate_recorded",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
                typed_proposal_id=candidate.proposal_id,
                commit=commit,
                acceptance_cursor=acceptance_cursor,
            )
        return self._record_authority(
            authority=authority,
            commit_cursor=current_cursor,
            identity_world_revision=current_cursor.world_revision,
            source_binding=(source_event, subject, evidence),
            identity_contract=_WORLD_STIMULUS_CONTRACT,
        )

    def accepted_world_stimulus_descendant(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event_id: str,
    ) -> str | None:
        """Return an accepted descendant only after re-proving its purpose binding."""

        self._require_rebase_order(
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
        )
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not changes:
            return None
        if len(changes) != 1 or changes[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        current_projection = self._ledger.project_at(current_cursor)
        self._world_stimulus_binding(
            authority=authority,
            change=changes[0],
            audit_projection=self._ledger.project_at(audit_cursor),
            current_projection=current_projection,
            source_event_id=source_event_id,
        )
        accepted = self._accepted_rebased_candidate(
            projection=current_projection,
            authority=authority,
            change=changes[0],
        )
        return accepted[0].proposal_id if accepted is not None else None

    def world_stimulus_signal_present(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event_id: str,
    ) -> bool:
        """Read-only proof that this exact purpose audit authored one signal."""

        self._require_rebase_order(
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
        )
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        changes = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not changes:
            return False
        if len(changes) != 1 or changes[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        self._world_stimulus_binding(
            authority=authority,
            change=changes[0],
            audit_projection=self._ledger.project_at(audit_cursor),
            current_projection=self._ledger.project_at(current_cursor),
            source_event_id=source_event_id,
        )
        return True

    def _record_authority(
        self,
        *,
        authority,
        commit_cursor: ProjectionCursor,
        identity_world_revision: int | None,
        source_binding: tuple[WorldEvent, str, tuple[EvidenceRef, ...]] | None = None,
        identity_contract: str = _CONTRACT,
    ) -> RelationshipProposalCompilation:
        change = tuple(
            item
            for item in authority.proposal.proposed_changes
            if item.kind == "relationship_signal"
        )
        if not change:
            return RelationshipProposalCompilation(
                status="no_change",
                source_proposal_id=authority.proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        if len(change) != 1 or change[0].transition != "suggest":
            raise RelationshipProposalCompilerError("signal_change_invalid")
        projection = self._ledger.project_at(commit_cursor)
        typed = self._compile_signal(
            authority=authority,
            change=change[0],
            projection=projection,
            identity_world_revision=identity_world_revision,
            source_binding=source_binding,
            identity_contract=identity_contract,
        )
        source_event = self._event(authority.audit.event_ref)
        event = self._proposal_event(typed=typed, source_event=source_event, logical_time=projection.logical_time)
        commit = self._ledger.commit_at_cursor(
            [event],
            expected_cursor=commit_cursor,
            commit_id="commit:relationship-proposal-compiler:"
            + _digest(
                {
                    "cursor": commit_cursor.model_dump(mode="json"),
                    "source": authority.audit.event_ref,
                    "typed_proposal_id": typed.proposal_id,
                }
            ),
        )
        return RelationshipProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=authority.proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            typed_proposal_id=typed.proposal_id,
            commit=commit,
            acceptance_cursor=self._cursor_from_commit(commit),
        )

    def _existing_rebased_candidate(
        self,
        *,
        projection,
        authority,
        change,
        current_cursor: ProjectionCursor,
    ) -> tuple[RelationshipProposalProjection, CommitResult, ProjectionCursor] | None:
        accepted = self._accepted_rebased_candidate(
            projection=projection,
            authority=authority,
            change=change,
        )
        if accepted is not None:
            candidate, commit = accepted
            return candidate, commit, self._cursor_from_commit(commit)

        located_existing: list[
            tuple[RelationshipProposalProjection, CommitResult]
        ] = []
        for candidate in projection.relationship_proposals:
            binding = candidate.source_audit
            if (
                binding is None
                or binding.proposal_event_ref != authority.audit.event_ref
                or binding.proposal_event_payload_hash
                != authority.audit.event_payload_hash
                or binding.model_result_ref != authority.audit.model_result_ref
                or binding.capsule_id != authority.audit.capsule_id
                or binding.change_id != change.change_id
                or binding.change_payload_hash != change.payload.payload_hash
            ):
                continue
            located = (
                self._ledger.lookup_event_commit(candidate.recorded_event_ref)
                if candidate.recorded_event_ref is not None
                else None
            )
            if (
                located is None
                or located[0].event_type != "ProposalRecorded"
                or located[0].payload_hash != candidate.recorded_event_payload_hash
            ):
                raise RelationshipProposalCompilerError(
                    "rebased_candidate_event_missing"
                )
            located_existing.append((candidate, located[1]))
        current = tuple(
            item
            for item in located_existing
            if item[0].evaluated_world_revision == current_cursor.world_revision
        )
        if len(current) > 1:
            raise RelationshipProposalCompilerError("rebased_candidate_ambiguous")
        if current:
            candidate, commit = current[0]
            return candidate, commit, current_cursor
        if located_existing:
            # A crash may leave an unactioned typed candidate.  Reusing it at a
            # different World revision would violate its manifest; silently
            # creating another candidate would leave two live authorities for
            # one character choice.  Keep the trigger retryable and expose the
            # exact technical condition instead.
            raise RelationshipProposalCompilerError("rebased_candidate_stale")
        return None

    def _accepted_rebased_candidate(
        self,
        *,
        projection,
        authority,
        change,
    ) -> tuple[RelationshipProposalProjection, CommitResult] | None:
        matches: list[tuple[RelationshipProposalProjection, CommitResult]] = []
        for decision in projection.acceptance_decisions:
            if (
                decision.manifest_version != "relationship-acceptance.1"
                or decision.status != "accepted"
                or decision.acceptance_event_ref is None
            ):
                continue
            acceptance = self._ledger.lookup_event_commit(
                decision.acceptance_event_ref
            )
            if acceptance is None:
                continue
            proposal_event_ref = acceptance[0].payload().get("proposal_event_ref")
            located = (
                self._ledger.lookup_event_commit(proposal_event_ref)
                if isinstance(proposal_event_ref, str)
                else None
            )
            if located is None or located[0].event_type != "ProposalRecorded":
                continue
            try:
                candidate = RelationshipProposalProjection.model_validate_json(
                    located[0].payload_json
                )
            except ValueError:
                continue
            binding = candidate.source_audit
            if (
                binding is None
                or binding.proposal_event_ref != authority.audit.event_ref
                or binding.proposal_event_payload_hash
                != authority.audit.event_payload_hash
                or binding.model_result_ref != authority.audit.model_result_ref
                or binding.capsule_id != authority.audit.capsule_id
                or binding.change_id != change.change_id
                or binding.change_payload_hash != change.payload.payload_hash
            ):
                continue
            matches.append((candidate, located[1]))
        if len(matches) > 1:
            raise RelationshipProposalCompilerError(
                "rebased_candidate_accepted_ambiguous"
            )
        return matches[0] if matches else None

    @staticmethod
    def _cursor_from_commit(commit: CommitResult) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=commit.world_revision,
            deliberation_revision=commit.deliberation_revision,
            ledger_sequence=commit.ledger_sequence,
        )

    def _compile_signal(
        self,
        *,
        authority,
        change,
        projection,
        identity_world_revision: int | None,
        source_binding: tuple[WorldEvent, str, tuple[EvidenceRef, ...]] | None = None,
        identity_contract: str = _CONTRACT,
    ) -> RelationshipProposalProjection:
        if source_binding is None:
            source_event, subject = self._source_relationship_subject(
                trigger_ref=authority.proposal.trigger_ref, projection=projection
            )
            self._require_claimed_trigger(
                source_event=source_event,
                projection=projection,
            )
            evidence = self._evidence(
                authority.proposal,
                change.evidence_refs,
                source_event,
                projection,
            )
        else:
            source_event, subject, evidence = source_binding
        raw = change.payload.value()
        subject_ref = raw.get("subject_ref")
        if subject_ref != subject:
            raise RelationshipProposalCompilerError("subject_not_bound_to_source")
        signal_code = raw.get("signal_code")
        confidence_bp = raw.get("confidence_bp")
        persistence = raw.get("persistence")
        rationale_code = raw.get("rationale_code")
        deltas = raw.get("suggested_deltas")
        if (
            not isinstance(signal_code, str)
            or not isinstance(confidence_bp, int)
            or persistence not in {"session", "durable"}
            or not isinstance(rationale_code, str)
            or not isinstance(deltas, dict)
        ):
            raise RelationshipProposalCompilerError("signal_payload_invalid")
        # Revalidate all six values before we emit typed authority.  They are
        # retained as a non-operative hint for the later adjustment worker.
        RelationshipVariableDeltas.model_validate(deltas)
        if projection.logical_time is None:
            raise RelationshipProposalCompilerError("logical_time_missing")
        identity_material: dict[str, object] = {
            "source_proposal_event": authority.audit.event_ref,
            "source_change": change.change_id,
            "typed_contract": identity_contract,
        }
        if identity_world_revision is not None:
            identity_material["rebase_world_revision"] = identity_world_revision
        identity = _digest(identity_material)
        typed_proposal_id = f"proposal:relationship-compiled:{identity}"
        typed_change_id = f"change:relationship-compiled:{identity}"
        transition_id = f"transition:relationship-compiled:{identity}"
        mutation_event_id = relationship_mutation_event_id(
            world_id=self._ledger.world_id,
            proposal_id=typed_proposal_id,
            transition_id=transition_id,
            event_type="RelationshipSignalAccepted",
        )
        signal = RelationshipSignalProjection(
            signal_id=f"signal:relationship-compiled:{identity}",
            semantic_fingerprint=relationship_signal_fingerprint(
                subject_ref=subject_ref, signal_code=signal_code, evidence_refs=evidence, policy_refs=_POLICY_REFS
            ),
            entity_revision=1,
            subject_ref=subject_ref,
            signal_code=signal_code,
            confidence_bp=confidence_bp,
            persistence=persistence,
            rationale_code=rationale_code,
            suggested_deltas=RelationshipVariableDeltas.model_validate(deltas),
            evidence_refs=evidence,
            origin=RelationshipSignalOrigin(
                change_id=typed_change_id,
                transition_id=transition_id,
                policy_refs=_POLICY_REFS,
                accepted_event_ref=mutation_event_id,
            ),
            accepted_at=projection.logical_time,
        )
        mutation: dict[str, object] = {
            "change_id": typed_change_id,
            "transition_id": transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": [item.model_dump(mode="json") for item in evidence],
            "policy_refs": list(_POLICY_REFS),
            "acceptance_id": f"acceptance:relationship-compiled:{identity}",
            "proposal_id": typed_proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "signal": signal.model_dump(mode="json"),
        }
        mutation["accepted_change_hash"] = relationship_mutation_hash(mutation)
        return RelationshipProposalProjection(
            proposal_id=typed_proposal_id,
            proposal_encoding="typed-authority-v1",
            authority_contract_ref="proposal-contract:relationship.1",
            transition_kind="signal",
            change_id=typed_change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=evidence,
            policy_refs=_POLICY_REFS,
            proposed_mutation=RelationshipProposedMutation(
                event_type="RelationshipSignalAccepted", payload_json=_canonical(mutation)
            ),
            source_audit=RelationshipProposalAuditBinding(
                proposal_event_ref=authority.audit.event_ref,
                proposal_event_payload_hash=authority.audit.event_payload_hash,
                model_result_ref=authority.audit.model_result_ref,
                capsule_id=authority.audit.capsule_id,
                change_id=change.change_id,
                change_payload_hash=change.payload.payload_hash,
            ),
        )

    @staticmethod
    def _require_rebase_order(
        *,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
    ) -> None:
        if (
            current_cursor.ledger_sequence < audit_cursor.ledger_sequence
            or current_cursor.world_revision < audit_cursor.world_revision
            or current_cursor.deliberation_revision
            < audit_cursor.deliberation_revision
        ):
            raise RelationshipProposalCompilerError(
                "rebase_cursor_precedes_audit"
            )

    def _world_stimulus_binding(
        self,
        *,
        authority,
        change,
        audit_projection,
        current_projection,
        source_event_id: str,
    ) -> tuple[WorldEvent, str, tuple[EvidenceRef, ...]]:
        if (
            authority.proposal.trigger_ref != source_event_id
            or authority.audit.trigger_ref != source_event_id
            or not authority.proposal.proposal_id.startswith(
                "proposal:character-interior-world-stimulus:"
            )
        ):
            raise RelationshipProposalCompilerError(
                "world_stimulus_source_mismatch"
            )
        located = self._ledger.lookup_event_commit(source_event_id)
        if located is None:
            raise RelationshipProposalCompilerError(
                "world_stimulus_source_unavailable"
            )
        source_event, source_commit = located
        evidence_type = _WORLD_STIMULUS_SOURCE_EVIDENCE.get(
            source_event.event_type
        )
        if evidence_type is None:
            raise RelationshipProposalCompilerError(
                "world_stimulus_source_kind_unsupported"
            )
        if (
            source_commit.world_revision > authority.cursor.world_revision
            or source_commit.deliberation_revision
            > authority.cursor.deliberation_revision
            or source_commit.ledger_sequence > authority.cursor.ledger_sequence
        ):
            raise RelationshipProposalCompilerError(
                "world_stimulus_source_outside_audit"
            )
        audit_committed = next(
            (
                item
                for item in audit_projection.committed_world_event_refs
                if item.event_id == source_event_id
            ),
            None,
        )
        current_committed = next(
            (
                item
                for item in current_projection.committed_world_event_refs
                if item.event_id == source_event_id
            ),
            None,
        )
        if (
            audit_committed is None
            or current_committed is None
            or audit_committed != current_committed
            or audit_committed.event_type != source_event.event_type
            or audit_committed.payload_hash != source_event.payload_hash
        ):
            raise RelationshipProposalCompilerError(
                "world_stimulus_source_not_authoritative"
            )
        raw = change.payload.value()
        subject_ref = raw.get("subject_ref")
        audit_event = self._event(authority.audit.event_ref)
        if (
            audit_event.source
            != "world-v2:character-interior-world-stimulus-authority"
            or not audit_event.event_id.startswith(
                "event:character-interior-world-stimulus:proposal:"
            )
        ):
            # This exact writer validates the capability manifest against the
            # pinned relationship projection before it can persist the audit.
            raise RelationshipProposalCompilerError(
                "world_stimulus_subject_authority_invalid"
            )
        audited_subjects = relationship_transition_subject_refs(
            projection=audit_projection,
            source_event=source_event,
        )
        current_subjects = relationship_transition_subject_refs(
            projection=current_projection,
            source_event=source_event,
        )
        if (
            not isinstance(subject_ref, str)
            or subject_ref not in audited_subjects
            or subject_ref not in current_subjects
        ):
            raise RelationshipProposalCompilerError(
                "world_stimulus_subject_not_authorized"
            )
        if tuple(change.evidence_refs) != (source_event_id,):
            raise RelationshipProposalCompilerError(
                "signal_evidence_not_exact_trigger"
            )
        source = next(
            (
                item
                for item in authority.proposal.evidence_refs
                if item.ref_id == source_event_id
            ),
            None,
        )
        if (
            source is None
            or source.evidence_kind != evidence_type
            or source.source_world_revision != audit_committed.world_revision
            or source.immutable_hash != "sha256:" + source_event.payload_hash
        ):
            raise RelationshipProposalCompilerError(
                "signal_evidence_not_authoritative"
            )
        return (
            source_event,
            subject_ref,
            (
                EvidenceRef(
                    ref_id=source_event_id,
                    evidence_type=evidence_type,
                    claim_purpose="private_hypothesis",
                    source_world_revision=audit_committed.world_revision,
                    immutable_hash=source_event.payload_hash,
                ),
            ),
        )

    def _source_relationship_subject(
        self, *, trigger_ref: str, projection
    ) -> tuple[WorldEvent, str]:
        located = self._ledger.lookup_event_commit(trigger_ref)
        if located is None or located[0].event_type not in {
            "AppraisalAccepted",
            "ObservationRecorded",
        }:
            raise RelationshipProposalCompilerError("source_event_unavailable")
        event, commit = located
        if commit.world_revision > projection.world_revision:
            raise RelationshipProposalCompilerError("source_event_outside_cursor")
        if event.event_type == "AppraisalAccepted":
            try:
                appraisal = next(
                    item
                    for item in projection.appraisals
                    if item.status == "active"
                    and item.origin.accepted_event_ref == event.event_id
                )
            except StopIteration as exc:
                raise RelationshipProposalCompilerError(
                    "source_appraisal_not_active"
                ) from exc
            return event, appraisal.subject_ref
        observation = Observation.model_validate_json(event.payload_json)
        reference = next(
            (
                item
                for item in projection.message_observations
                if item.observation_id == observation.observation_id
                and item.source == observation.source
                and item.source_event_id == observation.source_event_id
            ),
            None,
        )
        if (
            reference is None
            or reference.event_payload_hash != event.payload_hash
            or reference.content_payload_hash != observation.payload_hash
            or reference.world_revision != commit.world_revision
            or reference.actor != observation.actor
        ):
            raise RelationshipProposalCompilerError(
                "source_observation_not_authoritative"
            )
        return event, observation.actor

    def _require_claimed_trigger(self, *, source_event: WorldEvent, projection) -> None:
        if source_event.event_type == "AppraisalAccepted":
            trigger_id = relationship_deliberation_trigger_id(
                world_id=self._ledger.world_id,
                appraisal_event_id=source_event.event_id,
            )
            trigger_ref = f"relationship:{source_event.event_id}"
        else:
            trigger_id = relationship_continuity_trigger_id(
                world_id=self._ledger.world_id,
                observation_event_id=source_event.event_id,
            )
            trigger_ref = f"relationship-continuity:{source_event.event_id}"
        process = next(
            (item for item in projection.trigger_processes if item.trigger_id == trigger_id), None
        )
        if (
            process is None
            or process.process_kind != "relationship_deliberation"
            or process.state != "claimed"
            or process.trigger_ref != trigger_ref
            or process.source_evidence_ref != source_event.event_id
        ):
            raise RelationshipProposalCompilerError("relationship_trigger_not_claimed")

    def _evidence(self, proposal, refs, appraisal_event: WorldEvent, projection) -> tuple[EvidenceRef, ...]:
        if tuple(refs) != (appraisal_event.event_id,):
            raise RelationshipProposalCompilerError("signal_evidence_not_exact_trigger")
        source = next(
            (item for item in proposal.evidence_refs if item.ref_id == appraisal_event.event_id), None
        )
        committed = next(
            (item for item in projection.committed_world_event_refs if item.event_id == appraisal_event.event_id),
            None,
        )
        if (
            source is None
            or source.evidence_kind != "committed_world_event"
            or committed is None
            or source.source_world_revision != committed.world_revision
            or source.immutable_hash != "sha256:" + appraisal_event.payload_hash
        ):
            raise RelationshipProposalCompilerError("signal_evidence_not_authoritative")
        return (
            EvidenceRef(
                ref_id=appraisal_event.event_id,
                evidence_type="committed_world_event",
                claim_purpose="private_hypothesis",
                source_world_revision=committed.world_revision,
                immutable_hash=appraisal_event.payload_hash,
            ),
        )

    def _proposal_event(self, *, typed: RelationshipProposalProjection, source_event: WorldEvent, logical_time) -> WorldEvent:
        if logical_time is None:
            raise RelationshipProposalCompilerError("logical_time_missing")
        # Persist the complete projection image. The authority reader compares
        # this recorded payload byte-for-byte against its later projection, so
        # omitting optional None fields would make a valid proposal unpinnable.
        payload = typed.model_dump(mode="json")
        identity = domain_idempotency_key(event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload)
        if identity is None:
            raise RelationshipProposalCompilerError("event_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:relationship-proposal-compiled:"
            + _digest({"world": self._ledger.world_id, "proposal": typed.proposal_id}),
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="world-v2:relationship-proposal-compiler",
            source="world-v2:relationship-proposal-compiler",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None or located[0].event_type != "ProposalRecorded":
            raise RelationshipProposalCompilerError("source_audit_event_missing")
        return located[0]


__all__ = [
    "RelationshipProposalCompilation",
    "RelationshipProposalCompiler",
    "RelationshipProposalCompilerError",
    "relationship_mutation_event_id",
]
