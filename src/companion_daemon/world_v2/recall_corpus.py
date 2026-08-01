"""Compile ledger-derived Context domains into disposable recall documents.

This module performs no summarization and assigns no behavioural meaning.  It
only restores exact source text already exposed by trusted World read models,
attaches the immutable event closure, and classifies storage form as episodic,
semantic, or reflective.  Reflective material remains a defeasible private
interpretation and can never become factual authority through retrieval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from pydantic import Field

from .context_capsule import FactRecallItem, HistoricalFactRecallItem
from .fact_reducers import INSTALLED_FACT_PREDICATE_GUIDE
from .life_content import RecentExperienceContextItem
from .memory_retrieval import MemoryRetrievalItem
from .recall_index import (
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from .recent_dialogue import RecentDialogueItem
from .schema_core import FrozenModel
from .schemas import (
    AffectEpisodeProjection,
    AppraisalHypothesis,
    AppraisalProjection,
    PrivateImpressionProjection,
    ThreadProjection,
)
from .world_life_context import WorldLifeContextItem


MAX_RECALL_CORPUS_DOCUMENTS = 256


class AffectOpeningRecallItem(FrozenModel):
    """One opening Affect image plus exact authority for its subject scope."""

    episode: AffectEpisodeProjection
    subject_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    subject_authority_refs: tuple[str, ...] = Field(min_length=1, max_length=8)


class RecallCorpusSources(FrozenModel):
    """The bounded, typed World views from which a recall sidecar is rebuilt."""

    recent_dialogue: tuple[RecentDialogueItem, ...] = ()
    relevant_facts: tuple[FactRecallItem, ...] = ()
    historical_facts: tuple[HistoricalFactRecallItem, ...] = ()
    open_threads: tuple[ThreadProjection, ...] = ()
    recent_experiences: tuple[RecentExperienceContextItem, ...] = ()
    world_life: tuple[WorldLifeContextItem, ...] = ()
    active_memory_candidates: tuple[MemoryRetrievalItem, ...] = ()
    affect_openings: tuple[AffectOpeningRecallItem, ...] = ()
    appraisals: tuple[AppraisalProjection, ...] = ()
    private_impressions: tuple[PrivateImpressionProjection, ...] = ()
    authority_bindings: tuple[RecallSourceBinding, ...] = Field(default=(), max_length=4_096)


def required_recall_authority_refs(sources: RecallCorpusSources) -> frozenset[str]:
    """Return only immutable event refs reachable from the bounded corpus."""

    refs: set[str] = set()
    for item in sources.recent_dialogue:
        refs.update(claim.authority_event_ref for claim in item.source_claims)
    for item in sources.relevant_facts:
        refs.update((item.accepted_fact_event_ref, item.observation_event_ref))
    for item in sources.historical_facts:
        refs.update((item.accepted_fact_event_ref, item.observation_event_ref))
    for item in sources.open_threads:
        refs.add(item.origin.accepted_event_ref)
        refs.update(ref.ref_id for ref in item.values.source_evidence_refs)
        refs.update(ref.ref_id for ref in item.values.anchor_evidence_refs)
    for item in sources.recent_experiences:
        refs.add(item.content.authority_event_ref)
        refs.add(item.content.descriptor_event_ref)
    for item in sources.world_life:
        refs.add(item.source.authority_event_ref)
        if item.content is not None:
            refs.add(item.content.descriptor_event_ref)
    for item in sources.active_memory_candidates:
        refs.update(excerpt.authority_event_ref for excerpt in item.source_excerpts)
    for item in sources.affect_openings:
        refs.add(item.episode.origin.accepted_event_ref)
        refs.update(ref.ref_id for ref in item.episode.evidence_refs)
        refs.update(item.subject_authority_refs)
    for item in sources.appraisals:
        refs.add(item.origin.accepted_event_ref)
        refs.update(ref.ref_id for ref in item.evidence_refs)
    for item in sources.private_impressions:
        # A superseded private impression remains immutable World history, but
        # it is no longer part of the character's current private
        # understanding.  Historical recall is allowed to recover superseded
        # facts; it must not resurrect a hypothesis the character explicitly
        # replaced.
        if item.status != "active":
            continue
        refs.update(item.source_refs)
        if item.origin is not None:
            refs.add(item.origin.accepted_event_ref)
    return frozenset(ref for ref in refs if ref)


def select_recall_authority_bindings(
    *,
    sources: RecallCorpusSources,
    candidates: Iterable[RecallSourceBinding],
) -> tuple[RecallSourceBinding, ...]:
    """Select the source closure without scaling with total ledger history."""

    required = required_recall_authority_refs(sources)
    return _canonical_bindings(tuple(binding for binding in candidates if binding.ref in required))


def _document_id(*parts: str) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"recall:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _canonical_bindings(
    values: tuple[RecallSourceBinding, ...],
) -> tuple[RecallSourceBinding, ...]:
    by_identity: dict[tuple[str, str], RecallSourceBinding] = {}
    for value in values:
        key = (value.source_kind, value.ref)
        previous = by_identity.get(key)
        if previous is not None and previous != value:
            raise ValueError("recall authority has conflicting immutable bindings")
        by_identity[key] = value
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                item.source_kind,
                item.authority_type,
                item.ref,
                item.source_world_revision,
                item.immutable_hash,
            ),
        )
    )


def _subjects(*values: str) -> tuple[str, ...]:
    return tuple(sorted(set(value for value in values if value)))


class RecallCorpusCompiler:
    """Deep compiler for a source-closed hybrid recall corpus."""

    VERSION = "world-v2-recall-corpus.4"

    def compile(
        self,
        *,
        cursor: RecallCursor,
        actor_ref: str,
        subject_refs: tuple[str, ...],
        sources: RecallCorpusSources,
    ) -> tuple[RecallDocument, ...]:
        if subject_refs != tuple(sorted(set(subject_refs))) or actor_ref not in subject_refs:
            raise ValueError("recall corpus subjects must be canonical and include its actor")
        authority = {item.ref: item for item in _canonical_bindings(sources.authority_bindings)}
        documents: list[RecallDocument] = []

        for item in sources.recent_dialogue:
            if item.speaker == "companion":
                speaker_ref = item.speaker_ref or actor_ref
                dialogue_subject_refs = _subjects(speaker_ref)
                epistemic_scope = "companion_expression_record"
            else:
                counterpart_refs = tuple(
                    ref for ref in subject_refs if ref != actor_ref
                )
                speaker_ref = (
                    item.speaker_ref
                    or (counterpart_refs[0] if len(counterpart_refs) == 1 else None)
                )
                dialogue_subject_refs = _subjects(
                    *((speaker_ref,) if speaker_ref is not None else counterpart_refs)
                )
                epistemic_scope = "counterpart_report_only"
            if (
                speaker_ref is None
                or not dialogue_subject_refs
                or speaker_ref not in subject_refs
            ):
                continue
            bindings = _canonical_bindings(
                tuple(
                    self._prefer_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type=(
                                "ObservationRecorded"
                                if item.speaker == "counterpart"
                                else "ExpressionAccepted"
                            ),
                            ref=claim.authority_event_ref,
                            source_world_revision=claim.authority_world_revision,
                            immutable_hash=claim.authority_payload_hash,
                        ),
                    )
                    for claim in item.source_claims
                )
            )
            documents.append(
                self._document(
                    memory_kind="episodic",
                    source_item_ref=item.dialogue_id,
                    source_slice="recent_dialogue",
                    bindings=bindings,
                    text=item.text,
                    actor_ref=actor_ref,
                    subject_refs=dialogue_subject_refs,
                    link_refs=(
                        *item.acknowledges_observation_event_refs,
                        *(claim.authority_event_ref for claim in item.source_claims),
                    ),
                    occurred_from=item.occurred_at,
                    privacy_class=item.privacy_class,
                    authority="dialogue_record",
                    epistemic_scope=epistemic_scope,
                    speaker_ref=speaker_ref,
                )
            )

        facts_by_id = {item.fact_id: item for item in sources.relevant_facts}
        fact_memory_links: dict[str, set[str]] = {}
        for candidate in sources.active_memory_candidates:
            for excerpt in candidate.source_excerpts:
                if excerpt.source_kind != "fact" or excerpt.source_id not in facts_by_id:
                    continue
                fact_memory_links.setdefault(excerpt.source_id, set()).update(
                    {
                        candidate.candidate_id,
                        candidate.cue_kind,
                        f"retrieval_strength_bp:{candidate.retrieval_strength_bp}",
                        *candidate.retention_rationales,
                    }
                )
        for item in sources.relevant_facts:
            bindings = _canonical_bindings(
                (
                    self._prefer_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="FactCommitted",
                            ref=item.accepted_fact_event_ref,
                            source_world_revision=item.accepted_fact_world_revision,
                            immutable_hash=item.accepted_fact_payload_hash,
                        ),
                    ),
                    self._prefer_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="ObservationRecorded",
                            ref=item.observation_event_ref,
                            source_world_revision=item.observation_world_revision,
                            immutable_hash=item.observation_event_payload_hash,
                        ),
                    ),
                )
            )
            documents.append(
                self._document(
                    memory_kind="semantic",
                    source_item_ref=item.fact_id,
                    source_slice="relevant_facts",
                    bindings=bindings,
                    text=item.source_excerpt,
                    retrieval_text=(
                        "Exact source statement: "
                        f"{item.source_excerpt}\n"
                        "Semantic fact slot: "
                        f"{INSTALLED_FACT_PREDICATE_GUIDE[item.predicate_code]}"
                        if item.predicate_code in INSTALLED_FACT_PREDICATE_GUIDE
                        else None
                    ),
                    actor_ref=actor_ref,
                    subject_refs=_subjects(item.subject_ref),
                    link_refs=(
                        item.predicate_code,
                        item.source_observation_id,
                        *fact_memory_links.get(item.fact_id, ()),
                    ),
                    occurred_from=item.occurred_at,
                    valid_from=item.updated_at,
                    privacy_class=item.privacy_class,
                )
            )

        for item in sources.historical_facts:
            bindings = _canonical_bindings(
                (
                    self._prefer_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="FactAuthorityEvent",
                            ref=item.accepted_fact_event_ref,
                            source_world_revision=item.accepted_fact_world_revision,
                            immutable_hash=item.accepted_fact_payload_hash,
                        ),
                    ),
                    self._prefer_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="ObservationRecorded",
                            ref=item.observation_event_ref,
                            source_world_revision=item.observation_world_revision,
                            immutable_hash=item.observation_event_payload_hash,
                        ),
                    ),
                )
            )
            documents.append(
                self._document(
                    memory_kind="semantic",
                    source_item_ref=(
                        f"{item.fact_id}:historical:{item.accepted_fact_world_revision}"
                    ),
                    source_slice="relevant_facts",
                    bindings=bindings,
                    text=item.source_excerpt,
                    retrieval_text=(
                        "Exact historical source statement: "
                        f"{item.source_excerpt}\n"
                        "Semantic fact slot: "
                        f"{INSTALLED_FACT_PREDICATE_GUIDE[item.predicate_code]}"
                        if item.predicate_code in INSTALLED_FACT_PREDICATE_GUIDE
                        else None
                    ),
                    actor_ref=actor_ref,
                    subject_refs=_subjects(item.subject_ref),
                    link_refs=(item.fact_id, item.predicate_code, item.source_observation_id),
                    occurred_from=item.occurred_at,
                    valid_from=item.valid_from,
                    valid_to=item.valid_to,
                    status="superseded",
                    privacy_class=item.privacy_class,
                )
            )

        experiences_by_id = {item.experience_id: item for item in sources.recent_experiences}
        experience_memory_links: dict[str, set[str]] = {}
        for candidate in sources.active_memory_candidates:
            for excerpt in candidate.source_excerpts:
                experience = experiences_by_id.get(excerpt.source_id)
                if (
                    excerpt.source_kind != "experience"
                    or experience is None
                    or excerpt.source_entity_revision != experience.entity_revision
                    or excerpt.authority_event_ref
                    != experience.content.authority_event_ref
                    or excerpt.authority_world_revision
                    != experience.content.authority_world_revision
                    or excerpt.authority_payload_hash
                    != experience.content.authority_payload_hash
                    or excerpt.excerpt_ref != experience.content.content_ref
                    or excerpt.excerpt_payload_hash
                    != experience.content.content_payload_hash
                ):
                    continue
                experience_memory_links.setdefault(excerpt.source_id, set()).update(
                    {
                        candidate.candidate_id,
                        candidate.cue_kind,
                        f"retrieval_strength_bp:{candidate.retrieval_strength_bp}",
                        *candidate.retention_rationales,
                    }
                )
        for item in sources.recent_experiences:
            if actor_ref not in item.values.participant_refs:
                continue
            bindings = _canonical_bindings(
                (
                    self._prefer_exact_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="ExperienceCommitted",
                            ref=item.content.authority_event_ref,
                            source_world_revision=item.content.authority_world_revision,
                            immutable_hash=item.content.authority_payload_hash,
                        ),
                    ),
                    self._prefer_exact_binding(
                        authority=authority,
                        fallback=RecallSourceBinding(
                            source_kind="committed_event",
                            authority_type="LifeContentRecorded",
                            ref=item.content.descriptor_event_ref,
                            source_world_revision=item.content.descriptor_world_revision,
                            immutable_hash=item.content.descriptor_payload_hash,
                        ),
                    ),
                )
            )
            documents.append(
                self._document(
                    memory_kind="episodic",
                    source_item_ref=item.experience_id,
                    source_slice="recent_experiences",
                    bindings=bindings,
                    text=item.content.text,
                    actor_ref=actor_ref,
                    subject_refs=_subjects(*item.values.participant_refs),
                    link_refs=tuple(
                        sorted(experience_memory_links.get(item.experience_id, ()))
                    ),
                    occurred_from=item.values.occurred_from,
                    occurred_to=item.values.occurred_to,
                    privacy_class=item.content.privacy_class,
                )
            )
        for item in sources.world_life:
            if actor_ref not in item.participant_refs or item.content is None:
                continue
            bindings = _canonical_bindings(
                (
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="WorldOccurrenceSettled",
                        ref=item.source.authority_event_ref,
                        source_world_revision=item.source.authority_world_revision,
                        immutable_hash=item.source.authority_payload_hash,
                    ),
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="LifeContentDescriptorAccepted",
                        ref=item.content.descriptor_event_ref,
                        source_world_revision=item.content.descriptor_world_revision,
                        immutable_hash=item.content.descriptor_payload_hash,
                    ),
                )
            )
            documents.append(
                self._document(
                    memory_kind="episodic",
                    source_item_ref=item.occurrence_id,
                    source_slice="world_life",
                    bindings=bindings,
                    text=item.content.text,
                    actor_ref=actor_ref,
                    subject_refs=_subjects(*item.participant_refs),
                    link_refs=(item.location_ref, item.result_id),
                    occurred_from=item.settled_at,
                    privacy_class=item.privacy_class,
                )
            )

        dialogue_by_ref: dict[str, RecentDialogueItem] = {}
        for item in sources.recent_dialogue:
            dialogue_by_ref[item.dialogue_id] = item
            for claim in item.source_claims:
                dialogue_by_ref[claim.authority_event_ref] = item
        for item in sources.open_threads:
            anchors = tuple(
                dialogue_by_ref[ref.ref_id]
                for ref in item.values.anchor_evidence_refs
                if ref.ref_id in dialogue_by_ref
            )
            if not anchors:
                continue
            refs = {
                item.origin.accepted_event_ref,
                *(ref.ref_id for ref in item.values.source_evidence_refs),
                *(
                    claim.authority_event_ref
                    for anchor in anchors
                    for claim in anchor.source_claims
                ),
            }
            bindings = self._bindings_for_refs(refs, authority)
            if bindings is None:
                continue
            documents.append(
                self._document(
                    memory_kind="episodic",
                    source_item_ref=item.thread_id,
                    source_slice="open_threads",
                    bindings=bindings,
                    text="\n".join(anchor.text for anchor in anchors),
                    actor_ref=actor_ref,
                    subject_refs=_subjects(item.values.subject_ref),
                    link_refs=(
                        item.thread_id,
                        item.values.conversation_ref,
                        item.values.resolution_contract_ref,
                    ),
                    occurred_from=item.opened_at,
                    occurred_to=item.updated_at,
                    valid_from=item.opened_at,
                    valid_to=item.values.expires_at,
                    status=(
                        "active"
                        if item.values.status == "open"
                        else "expired"
                        if item.values.status == "expired"
                        else "superseded"
                    ),
                    privacy_class=item.values.privacy_class,
                )
            )

        meanings: dict[str, tuple[AppraisalProjection, AppraisalHypothesis]] = {}
        for appraisal in sources.appraisals:
            for hypothesis in appraisal.hypotheses:
                meanings[f"appraisal:{appraisal.appraisal_id}:{hypothesis.hypothesis_id}"] = (
                    appraisal,
                    hypothesis,
                )

        for item in sources.affect_openings:
            episode = item.episode
            if not set(item.subject_refs).issubset(subject_refs):
                continue
            refs = {
                episode.origin.accepted_event_ref,
                *(ref.ref_id for ref in episode.evidence_refs),
                *item.subject_authority_refs,
            }
            if len(refs) > 16:
                # A partial evidence set would make the emotional association
                # look better grounded than it is. Oversized episodes simply
                # remain available in the live Affect projection.
                continue
            bindings = self._bindings_for_refs(refs, authority)
            if bindings is None:
                continue
            appraisal_links = tuple(
                sorted(
                    {
                        f"appraisal:{meaning.appraisal_id}:{meaning.hypothesis_id}"
                        for component in episode.components
                        for meaning in component.appraisal_refs
                    }
                )
            )[:31]
            dimensions = " | ".join(
                f"{component.dimension}={component.intensity_bp}bp"
                for component in sorted(
                    episode.components,
                    key=lambda component: (
                        component.dimension,
                        component.component_id,
                    ),
                )
            )
            documents.append(
                self._document(
                    memory_kind="reflective",
                    source_item_ref=f"affect-opening:{episode.episode_id}",
                    source_slice="recalled_emotional_associations",
                    bindings=bindings,
                    text="Emotional episode at opening — " + dimensions,
                    actor_ref=actor_ref,
                    subject_refs=_subjects(actor_ref, *item.subject_refs),
                    link_refs=(
                        episode.episode_id,
                        *appraisal_links,
                    ),
                    occurred_from=episode.opened_at,
                    occurred_to=episode.updated_at,
                    status="historical",
                    privacy_class="withhold",
                    authority="defeasible_interpretation",
                )
            )

        for item in sources.appraisals:
            refs = {
                item.origin.accepted_event_ref,
                *(ref.ref_id for ref in item.evidence_refs),
            }
            if len(refs) > 16:
                # RecallDocument intentionally has a small complete-source
                # envelope. Never truncate a legal appraisal's evidence and
                # accidentally upgrade a partial interpretation.
                continue
            bindings = self._bindings_for_refs(refs, authority)
            if bindings is None:
                continue
            text = "Remembered private interpretation — " + " | ".join(
                " / ".join(
                    (
                        hypothesis.meaning,
                        hypothesis.attribution,
                        hypothesis.controllability,
                        hypothesis.severity,
                    )
                )
                for hypothesis in item.hypotheses
            )
            documents.append(
                self._document(
                    memory_kind="reflective",
                    source_item_ref=item.appraisal_id,
                    source_slice="recalled_emotional_associations",
                    bindings=bindings,
                    text=text,
                    actor_ref=actor_ref,
                    subject_refs=_subjects(actor_ref, item.subject_ref),
                    link_refs=tuple(
                        (
                            item.source_cluster_ref,
                            *(
                                f"appraisal:{item.appraisal_id}:{hypothesis.hypothesis_id}"
                                for hypothesis in item.hypotheses
                            ),
                        )
                    ),
                    occurred_from=item.accepted_at,
                    privacy_class="withhold",
                    authority="defeasible_interpretation",
                )
            )

        for item in sources.private_impressions:
            if item.status != "active":
                continue
            resolved = tuple(meanings[ref] for ref in item.interpretation_refs if ref in meanings)
            if len(resolved) != len(item.interpretation_refs):
                continue
            refs = {
                *item.source_refs,
                *(appraisal.origin.accepted_event_ref for appraisal, _ in resolved),
                *((item.origin.accepted_event_ref,) if item.origin is not None else ()),
            }
            bindings = self._bindings_for_refs(refs, authority)
            if bindings is None:
                continue
            text = item.reflection_summary or "\n".join(
                " ".join(
                    (
                        hypothesis.meaning,
                        hypothesis.attribution,
                        hypothesis.controllability,
                        hypothesis.severity,
                        f"weight_bp={hypothesis.weight_bp}",
                    )
                )
                for _, hypothesis in resolved
            )
            documents.append(
                self._document(
                    memory_kind="reflective",
                    source_item_ref=item.impression_id,
                    source_slice="private_impressions",
                    bindings=bindings,
                    text=text,
                    actor_ref=actor_ref,
                    subject_refs=_subjects(item.subject_ref),
                    link_refs=(*item.interpretation_refs, item.expiry_condition),
                    occurred_from=item.first_seen,
                    occurred_to=item.last_supported,
                    valid_from=item.first_seen,
                    status=item.status,
                    privacy_class="withhold",
                    authority="defeasible_interpretation",
                )
            )

        if len(documents) > MAX_RECALL_CORPUS_DOCUMENTS:
            documents = sorted(
                documents,
                key=lambda item: (
                    item.status == "active",
                    item.occurred_to or item.occurred_from,
                    item.document_id,
                ),
                reverse=True,
            )[:MAX_RECALL_CORPUS_DOCUMENTS]
        ordered = tuple(sorted(documents, key=lambda item: item.document_id))
        if len({item.document_id for item in ordered}) != len(ordered):
            raise ValueError("recall corpus contains duplicate document identities")
        if any(item.source_world_revision > cursor.world_revision for item in ordered):
            raise ValueError("recall corpus contains future source authority")
        return ordered

    @staticmethod
    def _bindings_for_refs(
        refs: set[str],
        authority: dict[str, RecallSourceBinding],
    ) -> tuple[RecallSourceBinding, ...] | None:
        if not refs or not refs.issubset(authority):
            return None
        return _canonical_bindings(tuple(authority[ref] for ref in refs))

    @staticmethod
    def _prefer_binding(
        *,
        authority: dict[str, RecallSourceBinding],
        fallback: RecallSourceBinding,
    ) -> RecallSourceBinding:
        exact = authority.get(fallback.ref)
        if exact is None:
            return fallback
        if (
            exact.source_world_revision != fallback.source_world_revision
            or exact.immutable_hash != fallback.immutable_hash
        ):
            raise ValueError("recall source disagrees with committed event authority")
        return exact

    @classmethod
    def _prefer_exact_binding(
        cls,
        *,
        authority: dict[str, RecallSourceBinding],
        fallback: RecallSourceBinding,
    ) -> RecallSourceBinding:
        binding = cls._prefer_binding(authority=authority, fallback=fallback)
        if (
            binding.source_kind != fallback.source_kind
            or binding.authority_type != fallback.authority_type
        ):
            raise ValueError("recall source has the wrong authority type")
        return binding

    @staticmethod
    def _document(
        *,
        memory_kind: str,
        source_item_ref: str,
        source_slice: str,
        bindings: tuple[RecallSourceBinding, ...],
        text: str,
        retrieval_text: str | None = None,
        actor_ref: str,
        subject_refs: tuple[str, ...],
        link_refs: tuple[str, ...] = (),
        occurred_from,
        occurred_to=None,
        valid_from=None,
        valid_to=None,
        status: str = "active",
        privacy_class: str,
        authority: str = "world_fact",
        epistemic_scope: str | None = None,
        speaker_ref: str | None = None,
    ) -> RecallDocument:
        canonical = _canonical_bindings(bindings)
        return RecallDocument(
            document_id=_document_id(
                source_slice,
                source_item_ref,
                *(item.ref for item in canonical),
            ),
            memory_kind=memory_kind,
            source_item_ref=source_item_ref,
            source_slice=source_slice,
            source_refs=tuple(sorted({item.ref for item in canonical})),
            source_bindings=canonical,
            source_world_revision=max(item.source_world_revision for item in canonical),
            # Recall exposes a source-bound excerpt, not an unbounded payload
            # archive.  The immutable binding still points to the complete
            # authority while this prefix keeps the replay audit compact.
            text=text[:1_024],
            retrieval_text=(retrieval_text[:4_096] if retrieval_text is not None else None),
            actor_ref=actor_ref,
            subject_refs=_subjects(*subject_refs),
            link_refs=_subjects(*link_refs),
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
            privacy_class=privacy_class,
            authority=authority,
            epistemic_scope=(
                epistemic_scope
                or (
                    "private_interpretation"
                    if authority == "defeasible_interpretation"
                    else "world_fact"
                )
            ),
            speaker_ref=speaker_ref,
        )


__all__ = [
    "AffectOpeningRecallItem",
    "RecallCorpusCompiler",
    "RecallCorpusSources",
    "MAX_RECALL_CORPUS_DOCUMENTS",
    "required_recall_authority_refs",
    "select_recall_authority_bindings",
]
