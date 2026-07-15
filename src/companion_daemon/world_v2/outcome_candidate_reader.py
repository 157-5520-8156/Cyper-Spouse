"""Read frozen, sidecar-backed outcome candidates without granting authority."""

from __future__ import annotations

from pydantic import Field

from .life_content_store import ImmutableLifeContentStore
from .schema_core import FrozenModel, PrivacyClass
from .schemas import OutcomeCandidateDescriptor, WorldOccurrenceProjection


_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


class OutcomeCandidateExcerpt(FrozenModel):
    candidate_result_ref: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_payload_ref: str = Field(min_length=1)
    result_payload_hash: str = Field(min_length=1)
    content_ref: str = Field(min_length=1)
    content_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    text: str = Field(min_length=1)
    privacy_class: PrivacyClass


class OutcomeCandidateSuppression(FrozenModel):
    candidate_result_ref: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class OutcomeCandidateReadResult(FrozenModel):
    candidates: tuple[OutcomeCandidateExcerpt, ...]
    suppressions: tuple[OutcomeCandidateSuppression, ...]


class OutcomeCandidateReader:
    """Deep read module for the model-visible candidate matrix.

    A missing content binding is not a fallback to the ref name: it is an
    explicit unavailable candidate.  Selection authority remains with the
    later Outcome compiler/acceptance lane.
    """

    def __init__(self, *, store: ImmutableLifeContentStore | None, max_characters: int = 480) -> None:
        if max_characters <= 0:
            raise ValueError("outcome candidate text budget must be positive")
        self._store = store
        self._max_characters = max_characters

    def read(
        self, *, occurrence: WorldOccurrenceProjection, viewer_privacy_ceiling: PrivacyClass
    ) -> OutcomeCandidateReadResult:
        if occurrence.status != "active":
            raise ValueError("outcome candidates require an active occurrence")
        items: list[OutcomeCandidateExcerpt] = []
        suppressions: list[OutcomeCandidateSuppression] = []
        for candidate in occurrence.candidate_outcomes:
            excerpt, suppression = self._one(
                candidate=candidate, viewer_privacy_ceiling=viewer_privacy_ceiling
            )
            if excerpt is not None:
                items.append(excerpt)
            elif suppression is not None:
                suppressions.append(suppression)
        if not occurrence.candidate_outcomes:
            suppressions.extend(
                OutcomeCandidateSuppression(candidate_result_ref=ref, reason="descriptor_missing")
                for ref in occurrence.candidate_outcome_refs
            )
        return OutcomeCandidateReadResult(candidates=tuple(items), suppressions=tuple(suppressions))

    def _one(
        self, *, candidate: OutcomeCandidateDescriptor, viewer_privacy_ceiling: PrivacyClass
    ) -> tuple[OutcomeCandidateExcerpt | None, OutcomeCandidateSuppression | None]:
        if candidate.privacy_class == "withhold" or (
            _PRIVACY_RANK[candidate.privacy_class] > _PRIVACY_RANK[viewer_privacy_ceiling]
        ):
            return None, OutcomeCandidateSuppression(
                candidate_result_ref=candidate.candidate_result_ref, reason="privacy_ceiling"
            )
        if candidate.content_ref is None or candidate.content_payload_hash is None:
            return None, OutcomeCandidateSuppression(
                candidate_result_ref=candidate.candidate_result_ref, reason="content_missing"
            )
        stored = self._store.read_exact(content_ref=candidate.content_ref) if self._store else None
        if stored is None:
            return None, OutcomeCandidateSuppression(
                candidate_result_ref=candidate.candidate_result_ref, reason="content_missing"
            )
        if stored.content_kind != "outcome_candidate" or stored.content_payload_hash != candidate.content_payload_hash:
            return None, OutcomeCandidateSuppression(
                candidate_result_ref=candidate.candidate_result_ref, reason="hash_mismatch"
            )
        return OutcomeCandidateExcerpt(
            candidate_result_ref=candidate.candidate_result_ref,
            result_id=candidate.result_id,
            result_payload_ref=candidate.result_payload_ref,
            result_payload_hash=candidate.result_payload_hash,
            content_ref=candidate.content_ref,
            content_payload_hash=candidate.content_payload_hash,
            text=stored.text[: self._max_characters],
            privacy_class=candidate.privacy_class,
        ), None


__all__ = [
    "OutcomeCandidateExcerpt",
    "OutcomeCandidateReadResult",
    "OutcomeCandidateReader",
    "OutcomeCandidateSuppression",
]
