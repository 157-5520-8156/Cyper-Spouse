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

from pydantic import Field

from .context_capsule import FactRecallItem
from .memory_retrieval import MemoryRetrievalItem
from .recall_index import (
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from .recent_dialogue import RecentDialogueItem
from .schema_core import FrozenModel
from .schemas import (
    AppraisalHypothesis,
    AppraisalProjection,
    ExperienceProjection,
    PrivateImpressionProjection,
    ThreadProjection,
)
from .world_life_context import WorldLifeContextItem


MAX_RECALL_CORPUS_DOCUMENTS = 256


class RecallCorpusSources(FrozenModel):
    """The bounded, typed World views from which a recall sidecar is rebuilt."""

    recent_dialogue: tuple[RecentDialogueItem, ...] = ()
    relevant_facts: tuple[FactRecallItem, ...] = ()
    open_threads: tuple[ThreadProjection, ...] = ()
    recent_experiences: tuple[ExperienceProjection, ...] = ()
    world_life: tuple[WorldLifeContextItem, ...] = ()
    active_memory_candidates: tuple[MemoryRetrievalItem, ...] = ()
    appraisals: tuple[AppraisalProjection, ...] = ()
    private_impressions: tuple[PrivateImpressionProjection, ...] = ()
    authority_bindings: tuple[RecallSourceBinding, ...] = Field(
        default=(), max_length=4_096
    )


def _document_id(*parts: str) -> str:
    encoded = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
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

    VERSION = "world-v2-recall-corpus.1"

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
        authority = {
            item.ref: item for item in _canonical_bindings(sources.authority_bindings)
        }
        documents: list[RecallDocument] = []

        for item in sources.recent_dialogue:
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
                    subject_refs=subject_refs,
                    link_refs=(
                        *item.acknowledges_observation_event_refs,
                        *(claim.authority_event_ref for claim in item.source_claims),
                    ),
                    occurred_from=item.occurred_at,
                    privacy_class=item.privacy_class,
                )
            )

        facts_by_id = {item.fact_id: item for item in sources.relevant_facts}
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
                    actor_ref=actor_ref,
                    subject_refs=_subjects(item.subject_ref),
                    link_refs=(item.predicate_code, item.source_observation_id),
                    occurred_from=item.committed_at,
                    valid_from=item.committed_at,
                    privacy_class=item.privacy_class,
                )
            )

        experiences_by_id = {
            item.experience_id: item for item in sources.recent_experiences
        }
        for candidate in sources.active_memory_candidates:
            for excerpt in candidate.source_excerpts:
                fact = facts_by_id.get(excerpt.source_id)
                experience = experiences_by_id.get(excerpt.source_id)
                if excerpt.source_kind == "fact" and fact is not None:
                    occurred_from = fact.committed_at
                    occurred_to = None
                    item_subjects = _subjects(fact.subject_ref)
                elif excerpt.source_kind == "experience" and experience is not None:
                    occurred_from = experience.values.occurred_from
                    occurred_to = experience.values.occurred_to
                    item_subjects = _subjects(*experience.values.participant_refs)
                else:
                    # Exact prose without an exact temporal/source projection
                    # is not promoted into the searchable corpus.
                    continue
                binding = self._prefer_binding(
                    authority=authority,
                    fallback=RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type=(
                            "FactCommitted"
                            if excerpt.source_kind == "fact"
                            else "ExperienceCommitted"
                        ),
                        ref=excerpt.authority_event_ref,
                        source_world_revision=excerpt.authority_world_revision,
                        immutable_hash=excerpt.authority_payload_hash,
                    ),
                )
                documents.append(
                    self._document(
                        memory_kind=(
                            "semantic" if excerpt.source_kind == "fact" else "episodic"
                        ),
                        source_item_ref=excerpt.source_id,
                        source_slice=(
                            "relevant_facts"
                            if excerpt.source_kind == "fact"
                            else "recent_experiences"
                        ),
                        bindings=(binding,),
                        text=excerpt.text,
                        actor_ref=actor_ref,
                        subject_refs=item_subjects,
                        link_refs=(
                            candidate.candidate_id,
                            candidate.cue_kind,
                            *candidate.retention_rationales,
                        ),
                        occurred_from=occurred_from,
                        occurred_to=occurred_to,
                        privacy_class=candidate.privacy_ceiling,
                    )
                )

        for item in sources.world_life:
            if item.content is None:
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

        meanings: dict[
            str, tuple[AppraisalProjection, AppraisalHypothesis]
        ] = {}
        for appraisal in sources.appraisals:
            for hypothesis in appraisal.hypotheses:
                meanings[
                    f"appraisal:{appraisal.appraisal_id}:{hypothesis.hypothesis_id}"
                ] = (appraisal, hypothesis)
        for item in sources.private_impressions:
            resolved = tuple(
                meanings[ref] for ref in item.interpretation_refs if ref in meanings
            )
            if len(resolved) != len(item.interpretation_refs):
                continue
            refs = {
                *item.source_refs,
                *(appraisal.origin.accepted_event_ref for appraisal, _ in resolved),
                *(
                    (item.origin.accepted_event_ref,)
                    if item.origin is not None
                    else ()
                ),
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

    @staticmethod
    def _document(
        *,
        memory_kind: str,
        source_item_ref: str,
        source_slice: str,
        bindings: tuple[RecallSourceBinding, ...],
        text: str,
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
            source_world_revision=max(
                item.source_world_revision for item in canonical
            ),
            # Recall exposes a source-bound excerpt, not an unbounded payload
            # archive.  The immutable binding still points to the complete
            # authority while this prefix keeps the replay audit compact.
            text=text[:1_024],
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
        )


__all__ = [
    "RecallCorpusCompiler",
    "RecallCorpusSources",
    "MAX_RECALL_CORPUS_DOCUMENTS",
]
