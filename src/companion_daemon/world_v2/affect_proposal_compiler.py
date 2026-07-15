"""Compile one audited generic affect choice into a typed Affect candidate.

This module sits strictly before :mod:`affect_acceptance_runtime`: it may
record a deliberation-only candidate, but never an accepted Affect mutation.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from .affect_acceptance_runtime import affect_mutation_event_id
from .affect_events import affect_mutation_hash
from .decision_proposal_authority import DecisionProposalAuthorityReader
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .schema_core import EvidenceRef, FrozenModel
from .schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AffectProposalAuditBinding,
    AffectProposalProjection,
    AffectProposedMutation,
    AppraisalMeaningRef,
    CommitResult,
    ProjectionCursor,
    WorldEvent,
    affect_decay_config_digest,
)


_CONTRACT = "affect-proposal-compiler.1"
_POLICY_REFS = ("policy:affect-v1",)
_MATRIX_VERSION = "affect-matrix.1"
_DECAY_SELECTORS = {
    ("policy:decay:standard", "affect-decay.1"): (3_600, 300, 120),
}
_RESIDUE_SELECTORS = {
    ("policy:residue:standard", "affect-residue.1"): 500,
}
_DIMENSIONS = {
    "hurt",
    "anger",
    "sadness",
    "loneliness",
    "anxiety",
    "resentment",
    "warmth",
    "joy",
}


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class AffectProposalCompilerError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = f"affect_proposal_compiler.{code}"
        super().__init__(self.code)


class AffectProposalCompilation(FrozenModel):
    status: Literal["no_change", "candidate_recorded"]
    source_proposal_id: str
    source_proposal_event_ref: str
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None


class AffectProposalCompiler:
    """Deep compiler for the open-episode Affect candidate lane.

    ``record`` has one small interface: it resolves the generic proposal from
    the ledger itself, validates all authority at the exact cursor, and either
    records one typed candidate or returns its durable no-change audit.  It
    deliberately rejects update/resolve/supersede until their source-cluster
    merge/reconciliation semantics have equally complete reverse verifiers.
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
    ) -> AffectProposalCompilation:
        authority = self._reader.read(
            self._reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        )
        proposal = authority.proposal
        if proposal.affect_decision == "no_change":
            return AffectProposalCompilation(
                status="no_change",
                source_proposal_id=proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        changes = tuple(item for item in proposal.proposed_changes if item.kind == "affect_transition")
        if len(changes) != 1:
            raise AffectProposalCompilerError("affect_change_count_invalid")
        change = changes[0]
        if change.transition != "open":
            raise AffectProposalCompilerError("transition_not_implemented")
        projection = self._ledger.project_at(cursor)
        typed = self._compile_open(authority=authority, change=change, projection=projection)
        source_event = self._event(authority.audit.event_ref)
        event = self._proposal_event(
            typed=typed, source_event=source_event, logical_time=projection.logical_time
        )
        commit = self._ledger.commit(
            [event],
            expected_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
            commit_id="commit:affect-proposal-compiler:"
            + _digest(
                {
                    "cursor": cursor.model_dump(mode="json"),
                    "source": authority.audit.event_ref,
                    "typed_proposal_id": typed.proposal_id,
                }
            ),
        )
        return AffectProposalCompilation(
            status="candidate_recorded",
            source_proposal_id=proposal.proposal_id,
            source_proposal_event_ref=authority.audit.event_ref,
            typed_proposal_id=typed.proposal_id,
            commit=commit,
        )

    def _compile_open(self, *, authority, change, projection) -> AffectProposalProjection:
        source = authority.audit
        raw = change.payload.value()
        appraisals = self._appraisals(
            projection=projection, change_refs=raw["appraisal_change_refs"]
        )
        cluster = appraisals[0].source_cluster_ref
        if any(item.source_cluster_ref != cluster for item in appraisals):
            raise AffectProposalCompilerError("appraisal_clusters_mixed")
        evidence = self._evidence(
            proposal=authority.proposal,
            refs=change.evidence_refs,
            projection=projection,
        )
        meanings = tuple(
            AppraisalMeaningRef(
                appraisal_id=appraisal.appraisal_id,
                hypothesis_id=hypothesis.hypothesis_id,
                source_cluster_ref=appraisal.source_cluster_ref,
                accepted_change_id=appraisal.origin.change_id,
                accepted_transition_id=appraisal.origin.transition_id,
            )
            for appraisal in appraisals
            for hypothesis in appraisal.hypotheses
        )
        components = self._components(
            raw=raw,
            cluster=cluster,
            meanings=meanings,
            at=projection.logical_time,
            proposal_id=source.proposal_id,
            change_id=change.change_id,
        )
        identity = _digest(
            {
                "source_proposal_event": source.event_ref,
                "source_change": change.change_id,
                "typed_contract": _CONTRACT,
            }
        )
        typed_proposal_id = f"proposal:affect-compiled:{identity}"
        typed_change_id = f"change:affect-compiled:{identity}"
        transition_id = f"transition:affect-compiled:{identity}"
        mutation_event_id = affect_mutation_event_id(
            world_id=self._ledger.world_id,
            proposal_id=typed_proposal_id,
            transition_id=transition_id,
            event_type="AffectEpisodeOpened",
        )
        episode = AffectEpisodeProjection(
            episode_id=f"affect:compiled:{identity}",
            entity_revision=1,
            origin=AffectOrigin(
                change_id=typed_change_id,
                transition_id=transition_id,
                policy_refs=_POLICY_REFS,
                matrix_catalog_version=_MATRIX_VERSION,
                accepted_event_ref=mutation_event_id,
            ),
            components=components,
            evidence_refs=evidence,
            opened_at=projection.logical_time,
            updated_at=projection.logical_time,
            status="active",
        )
        mutation: dict[str, object] = {
            "change_id": typed_change_id,
            "transition_id": transition_id,
            "expected_entity_revision": 0,
            "evidence_refs": [item.model_dump(mode="json") for item in evidence],
            "appraisal_refs": [item.model_dump(mode="json") for item in meanings],
            "policy_refs": list(_POLICY_REFS),
            "acceptance_id": f"acceptance:affect-compiled:{identity}",
            "proposal_id": typed_proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "episode": episode.model_dump(mode="json"),
        }
        mutation["accepted_change_hash"] = affect_mutation_hash(mutation)
        payload_json = _canonical(mutation)
        return AffectProposalProjection(
            proposal_id=typed_proposal_id,
            transition_kind="open",
            change_id=typed_change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=0,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=evidence,
            appraisal_refs=meanings,
            policy_refs=_POLICY_REFS,
            proposed_mutation=AffectProposedMutation(
                event_type="AffectEpisodeOpened", payload_json=payload_json
            ),
            authority_contract_ref=_CONTRACT,
            source_audit=AffectProposalAuditBinding(
                proposal_event_ref=source.event_ref,
                proposal_event_payload_hash=source.event_payload_hash,
                model_result_ref=source.model_result_ref,
                capsule_id=source.capsule_id,
                change_id=change.change_id,
                change_payload_hash=change.payload.payload_hash,
            ),
        )

    def _appraisals(self, *, projection, change_refs: object):
        if not isinstance(change_refs, list) or not change_refs:
            raise AffectProposalCompilerError("appraisal_refs_invalid")
        if len(set(change_refs)) != len(change_refs) or not all(isinstance(item, str) for item in change_refs):
            raise AffectProposalCompilerError("appraisal_refs_invalid")
        result = tuple(
            appraisal
            for ref in change_refs
            for appraisal in projection.appraisals
            if appraisal.status == "active" and appraisal.origin.change_id == ref
        )
        if len(result) != len(change_refs):
            raise AffectProposalCompilerError("appraisal_not_active")
        return result

    def _evidence(self, *, proposal, refs: tuple[str, ...], projection) -> tuple[EvidenceRef, ...]:
        if not refs:
            raise AffectProposalCompilerError("evidence_missing")
        by_id = {item.ref_id: item for item in proposal.evidence_refs}
        if len(set(refs)) != len(refs) or any(ref not in by_id for ref in refs):
            raise AffectProposalCompilerError("evidence_not_authoritative")
        result: list[EvidenceRef] = []
        for ref in refs:
            source = by_id[ref]
            ref_id = source.ref_id
            if source.evidence_kind == "observed_message":
                # Context exposes committed event ids so generic Deliberation
                # can prove provenance.  Affect reducers, however, index an
                # observed-message EvidenceRef by its observation id.  Reverse
                # only an exact revision/hash alias from this pinned projection.
                aliases = tuple(
                    item.observation_id
                    for item in projection.message_observations
                    if item.world_revision == source.source_world_revision
                    and item.event_payload_hash == source.immutable_hash.removeprefix("sha256:")
                )
                if ref_id not in {item.observation_id for item in projection.message_observations}:
                    if len(aliases) != 1:
                        raise AffectProposalCompilerError("observation_evidence_alias_invalid")
                    ref_id = aliases[0]
            result.append(
                EvidenceRef(
                    ref_id=ref_id,
                    evidence_type=source.evidence_kind,
                    claim_purpose="private_hypothesis",
                    source_world_revision=source.source_world_revision,
                    immutable_hash=source.immutable_hash.removeprefix("sha256:"),
                )
            )
        return tuple(result)

    def _components(self, *, raw, cluster, meanings, at, proposal_id, change_id):
        if at is None:
            raise AffectProposalCompilerError("logical_time_missing")
        decay = raw.get("decay_config")
        residue = raw.get("residue_config")
        if not isinstance(decay, dict) or not isinstance(residue, dict):
            raise AffectProposalCompilerError("selector_invalid")
        decay_values = _DECAY_SELECTORS.get((decay.get("object_ref"), decay.get("schema_version")))
        residue_bp = _RESIDUE_SELECTORS.get((residue.get("object_ref"), residue.get("schema_version")))
        if decay_values is None or residue_bp is None:
            raise AffectProposalCompilerError("selector_uninstalled")
        profile = AffectDecayProfileProjection(
            half_life_seconds=decay_values[0],
            floor_bp=decay_values[1],
            delay_seconds=decay_values[2],
            config_version=str(decay["schema_version"]),
            config_digest=affect_decay_config_digest(
                kind="exponential_half_life",
                half_life_seconds=decay_values[0],
                floor_bp=decay_values[1],
                delay_seconds=decay_values[2],
                config_version=str(decay["schema_version"]),
            ),
        )
        deltas = raw.get("component_deltas")
        if not isinstance(deltas, list) or not deltas:
            raise AffectProposalCompilerError("component_deltas_invalid")
        dimensions: set[str] = set()
        result: list[AffectComponentProjection] = []
        for item in deltas:
            if not isinstance(item, dict):
                raise AffectProposalCompilerError("component_deltas_invalid")
            dimension, intensity = item.get("name"), item.get("value")
            if dimension not in _DIMENSIONS or not isinstance(intensity, int) or not 1 <= intensity <= 10_000:
                raise AffectProposalCompilerError("component_delta_invalid")
            if dimension in dimensions or intensity < max(profile.floor_bp, residue_bp):
                raise AffectProposalCompilerError("component_delta_invalid")
            dimensions.add(dimension)
            result.append(
                AffectComponentProjection(
                    component_id=f"component:{dimension}:compiled:{_digest([proposal_id, change_id, dimension])}",
                    dimension=dimension,
                    source_cluster_ref=cluster,
                    appraisal_refs=meanings,
                    intensity_bp=intensity,
                    decay_anchor_intensity_bp=intensity,
                    opened_at=at,
                    decay_anchor_at=at,
                    decay_not_before=at + timedelta(seconds=profile.delay_seconds),
                    last_stimulus_at=at,
                    last_updated_at=at,
                    decay_profile=profile,
                    residue_bp=residue_bp,
                )
            )
        return tuple(result)

    def _proposal_event(
        self, *, typed: AffectProposalProjection, source_event: WorldEvent, logical_time
    ) -> WorldEvent:
        if logical_time is None:
            raise AffectProposalCompilerError("logical_time_missing")
        payload = typed.model_dump(mode="json", exclude_none=True)
        identity = domain_idempotency_key(
            event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise AffectProposalCompilerError("event_identity_missing")
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:affect-proposal-compiled:"
            + _digest({"world": self._ledger.world_id, "proposal": typed.proposal_id}),
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor="world-v2:affect-proposal-compiler",
            source="world-v2:affect-proposal-compiler",
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )

    def _event(self, event_id: str) -> WorldEvent:
        located = self._ledger.lookup_event_commit(event_id)
        if located is None:
            raise AffectProposalCompilerError("source_event_missing")
        return located[0]


__all__ = ["AffectProposalCompilation", "AffectProposalCompiler", "AffectProposalCompilerError"]
