"""Compile one audited generic affect choice into a typed Affect candidate.

This module sits strictly before :mod:`affect_acceptance_runtime`: it may
record a deliberation-only candidate, but never an accepted Affect mutation.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from typing import Literal

from pydantic_core import to_jsonable_python

from .affect_acceptance_runtime import affect_mutation_event_id
from .affect_events import AffectComponentUpdate, affect_mutation_hash
from .affect_math import DecayAnchor, DecayProfile, decay_intensity_bp
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
_MERGE_WINDOW_SECONDS = 900
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
    skip_reason: str | None = None
    typed_proposal_id: str | None = None
    commit: CommitResult | None = None


class AffectProposalCompiler:
    """Deep compiler for immediate open-or-merge Affect candidates.

    ``record`` has one small interface: it resolves the generic proposal from
    the ledger itself, validates all authority at the exact cursor, and either
    records one typed candidate or returns its durable no-change audit.  It
    deterministically translates a merge-eligible ``open`` hint into an
    ``update`` of the exact active source-cluster/dimension episode. Explicit
    model-authored update/resolve/supersede transitions remain unsupported.
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
        typed = self._compile_transition(authority=authority, change=change, projection=projection)
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

    def record_rebased(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
    ) -> AffectProposalCompilation:
        """Compile Affect after this same audited proposal's Appraisal was accepted.

        The generic DecisionProposal is authenticated only at its immutable
        audit cursor.  The current cursor supplies the newly accepted
        Appraisal and the revision against which the typed Affect candidate is
        recorded.  This is deliberately narrower than making the generic
        authority reader accept stale proposals at arbitrary cursors.
        """

        if (
            current_cursor.ledger_sequence < audit_cursor.ledger_sequence
            or current_cursor.world_revision < audit_cursor.world_revision
            or current_cursor.deliberation_revision < audit_cursor.deliberation_revision
        ):
            raise AffectProposalCompilerError("rebase_cursor_precedes_audit")
        authority = self._reader.read(
            self._reader.pin(
                world_id=world_id,
                cursor=audit_cursor,
                proposal_id=proposal_id,
            )
        )
        proposal = authority.proposal
        if proposal.affect_decision == "no_change":
            return AffectProposalCompilation(
                status="no_change",
                source_proposal_id=proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
            )
        changes = tuple(
            item for item in proposal.proposed_changes if item.kind == "affect_transition"
        )
        if len(changes) != 1:
            raise AffectProposalCompilerError("affect_change_count_invalid")
        change = changes[0]
        if change.transition != "open":
            raise AffectProposalCompilerError("transition_not_implemented")
        raw = change.payload.value()
        appraisal_refs = raw.get("appraisal_change_refs")
        appraisal_change_ids = {
            item.change_id
            for item in proposal.proposed_changes
            if item.kind == "appraisal_transition"
        }
        if (
            not isinstance(appraisal_refs, list)
            or not appraisal_refs
            or any(ref not in appraisal_change_ids for ref in appraisal_refs)
        ):
            raise AffectProposalCompilerError("appraisal_refs_not_from_source_proposal")
        typed_identity = _digest(
            {
                "source_proposal_event": authority.audit.event_ref,
                "source_change": change.change_id,
                "typed_contract": _CONTRACT,
            }
        )
        expected_typed_id = f"proposal:affect-compiled:{typed_identity}"
        expected_event_id = "event:affect-proposal-compiled:" + _digest(
            {"world": self._ledger.world_id, "proposal": expected_typed_id}
        )
        located_existing = self._ledger.lookup_event_commit(expected_event_id)
        if located_existing is not None:
            try:
                persisted = AffectProposalProjection.model_validate_json(
                    located_existing[0].payload_json
                )
            except ValueError as exc:
                raise AffectProposalCompilerError("rebased_candidate_event_invalid") from exc
            binding = persisted.source_audit
            if (
                persisted.proposal_id != expected_typed_id
                or binding is None
                or binding.proposal_event_ref != authority.audit.event_ref
                or binding.proposal_event_payload_hash != authority.audit.event_payload_hash
                or binding.model_result_ref != authority.audit.model_result_ref
                or binding.capsule_id != authority.audit.capsule_id
                or binding.change_id != change.change_id
                or binding.change_payload_hash != change.payload.payload_hash
            ):
                raise AffectProposalCompilerError("rebased_candidate_event_mismatch")
            return AffectProposalCompilation(
                status="candidate_recorded",
                source_proposal_id=proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
                typed_proposal_id=persisted.proposal_id,
                commit=located_existing[1],
            )
        projection = self._ledger.project_at(current_cursor)
        existing = tuple(
            item
            for item in projection.affect_proposals
            if item.source_audit is not None
            and item.source_audit.proposal_event_ref == authority.audit.event_ref
            and item.source_audit.proposal_event_payload_hash
            == authority.audit.event_payload_hash
            and item.source_audit.model_result_ref == authority.audit.model_result_ref
            and item.source_audit.capsule_id == authority.audit.capsule_id
            and item.source_audit.change_id == change.change_id
        )
        if len(existing) > 1:
            raise AffectProposalCompilerError("rebased_candidate_ambiguous")
        if existing:
            located = self._ledger.lookup_event_commit(existing[0].recorded_event_ref)
            if located is None or located[0].payload_hash != existing[0].recorded_event_payload_hash:
                raise AffectProposalCompilerError("rebased_candidate_event_missing")
            return AffectProposalCompilation(
                status="candidate_recorded",
                source_proposal_id=proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
                typed_proposal_id=existing[0].proposal_id,
                commit=located[1],
            )
        try:
            typed = self._compile_transition(
                authority=authority,
                change=change,
                projection=projection,
            )
        except AffectProposalCompilerError as exc:
            if exc.code != "affect_proposal_compiler.merge_target_ambiguous":
                raise
            return AffectProposalCompilation(
                status="no_change",
                source_proposal_id=proposal.proposal_id,
                source_proposal_event_ref=authority.audit.event_ref,
                skip_reason=exc.code,
            )
        source_event = self._event(authority.audit.event_ref)
        event = self._proposal_event(
            typed=typed,
            source_event=source_event,
            logical_time=projection.logical_time,
        )
        commit = self._ledger.commit(
            [event],
            expected_world_revision=current_cursor.world_revision,
            expected_deliberation_revision=current_cursor.deliberation_revision,
            commit_id="commit:affect-proposal-compiler:rebased:"
            + _digest(
                {
                    "audit_cursor": audit_cursor.model_dump(mode="json"),
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

    def _compile_transition(self, *, authority, change, projection) -> AffectProposalProjection:
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
        merge_target = self._merge_target(projection=projection, components=components)
        if merge_target is not None:
            return self._compile_update(
                authority=authority,
                change=change,
                projection=projection,
                episode=merge_target,
                evidence=evidence,
                meanings=meanings,
                proposed_components=components,
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

    def _merge_target(self, *, projection, components):
        if projection.logical_time is None:
            raise AffectProposalCompilerError("logical_time_missing")
        requested = {
            (component.dimension, component.source_cluster_ref) for component in components
        }
        candidates = tuple(
            (
                episode,
                sum(
                    (component.dimension, component.source_cluster_ref) in requested
                    and projection.logical_time - component.last_stimulus_at
                    <= timedelta(seconds=_MERGE_WINDOW_SECONDS)
                    for component in episode.components
                ),
                max(
                    component.last_stimulus_at
                    for component in episode.components
                    if (component.dimension, component.source_cluster_ref) in requested
                    and projection.logical_time - component.last_stimulus_at
                    <= timedelta(seconds=_MERGE_WINDOW_SECONDS)
                ),
            )
            for episode in projection.affect_episodes
            if episode.status == "active"
            and any(
                (component.dimension, component.source_cluster_ref) in requested
                and projection.logical_time - component.last_stimulus_at
                <= timedelta(seconds=_MERGE_WINDOW_SECONDS)
                for component in episode.components
            )
        )
        if not candidates:
            return None
        best_score = max((coverage, latest) for _episode, coverage, latest in candidates)
        matches = tuple(
            episode
            for episode, coverage, latest in candidates
            if (coverage, latest) == best_score
        )
        if len(matches) != 1:
            raise AffectProposalCompilerError("merge_target_ambiguous")
        target = matches[0]
        target_keys = {
            (component.dimension, component.source_cluster_ref)
            for component in target.components
        }
        new_keys = requested - target_keys
        if any(
            (component.dimension, component.source_cluster_ref) in new_keys
            for episode, _coverage, _latest in candidates
            if episode.episode_id != target.episode_id
            for component in episode.components
        ):
            # Adding that component to the selected episode would duplicate an
            # already active causal dimension in another episode.  Resolving
            # or superseding two episodes is outside this immediate lane.
            raise AffectProposalCompilerError("merge_target_ambiguous")
        return target

    def _compile_update(
        self,
        *,
        authority,
        change,
        projection,
        episode,
        evidence,
        meanings,
        proposed_components,
    ) -> AffectProposalProjection:
        """Translate a merge-eligible audited open hint into one typed update.

        The model still chooses the emotional dimensions and deltas.  This
        compiler only applies the installed episode merge invariant and
        materializes untouched sibling components, so Acceptance receives a
        complete deterministic update rather than an invalid second open.
        """

        at = projection.logical_time
        if at is None:
            raise AffectProposalCompilerError("logical_time_missing")
        source = authority.audit
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
        requested = {
            (component.dimension, component.source_cluster_ref): component
            for component in proposed_components
        }
        updates: list[AffectComponentUpdate] = []
        for current in episode.components:
            before = self._materialized_intensity(
                component=current,
                at=at,
                baselines=projection.affect_baselines,
            )
            proposed = requested.pop((current.dimension, current.source_cluster_ref), None)
            if proposed is None:
                updated = current.model_copy(
                    update={"intensity_bp": before, "last_updated_at": at}
                )
                updates.append(
                    AffectComponentUpdate(
                        component_id=current.component_id,
                        operation="materialize",
                        before_intensity_bp=before,
                        proposed_delta_bp=0,
                        accepted_delta_bp=0,
                        after_intensity_bp=before,
                        appraisal_refs=(),
                        updated_component=updated,
                    )
                )
                continue
            proposed_delta = proposed.intensity_bp
            accepted_delta = min(proposed_delta, 10_000 - before)
            after = before + accepted_delta
            updated = current.model_copy(
                update={
                    "appraisal_refs": (*current.appraisal_refs, *meanings),
                    "intensity_bp": after,
                    "decay_anchor_intensity_bp": after,
                    "decay_anchor_at": at,
                    "last_stimulus_at": at,
                    "last_updated_at": at,
                }
            )
            updates.append(
                AffectComponentUpdate(
                    component_id=current.component_id,
                    operation="stimulus",
                    before_intensity_bp=before,
                    proposed_delta_bp=proposed_delta,
                    accepted_delta_bp=accepted_delta,
                    after_intensity_bp=after,
                    appraisal_refs=meanings,
                    updated_component=updated,
                )
            )
        for proposed in requested.values():
            updates.append(
                AffectComponentUpdate(
                    component_id=proposed.component_id,
                    operation="open_component",
                    before_intensity_bp=0,
                    proposed_delta_bp=proposed.intensity_bp,
                    accepted_delta_bp=proposed.intensity_bp,
                    after_intensity_bp=proposed.intensity_bp,
                    appraisal_refs=meanings,
                    updated_component=proposed,
                )
            )
        mutation: dict[str, object] = {
            "change_id": typed_change_id,
            "transition_id": transition_id,
            "expected_entity_revision": episode.entity_revision,
            "evidence_refs": [item.model_dump(mode="json") for item in evidence],
            "appraisal_refs": [item.model_dump(mode="json") for item in meanings],
            "policy_refs": list(_POLICY_REFS),
            "acceptance_id": f"acceptance:affect-compiled:{identity}",
            "proposal_id": typed_proposal_id,
            "evaluated_world_revision": projection.world_revision,
            "accepted_change_hash": "0" * 64,
            "episode_id": episode.episode_id,
            "updated_at": to_jsonable_python(at),
            "component_updates": [item.model_dump(mode="json") for item in updates],
        }
        mutation["accepted_change_hash"] = affect_mutation_hash(mutation)
        return AffectProposalProjection(
            proposal_id=typed_proposal_id,
            transition_kind="update",
            change_id=typed_change_id,
            transition_id=transition_id,
            evaluated_world_revision=projection.world_revision,
            expected_entity_revision=episode.entity_revision,
            proposed_change_hash=str(mutation["accepted_change_hash"]),
            evidence_refs=evidence,
            appraisal_refs=meanings,
            policy_refs=_POLICY_REFS,
            proposed_mutation=AffectProposedMutation(
                event_type="AffectEpisodeUpdated",
                payload_json=_canonical(mutation),
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

    @staticmethod
    def _materialized_intensity(*, component, at, baselines) -> int:
        baseline = next(
            (item.baseline_bp for item in baselines if item.dimension == component.dimension),
            0,
        )
        profile = component.decay_profile
        return decay_intensity_bp(
            DecayAnchor(
                intensity_bp=component.decay_anchor_intensity_bp,
                anchored_at=component.decay_anchor_at,
                baseline_bp=baseline,
                residue_bp=component.residue_bp,
                decay_not_before=component.decay_not_before,
            ),
            DecayProfile(
                half_life_seconds=profile.half_life_seconds,
                floor_bp=profile.floor_bp,
                delay_seconds=profile.delay_seconds,
                config_version=profile.config_version,
                kind=profile.kind,
            ),
            at,
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
