"""Compile non-authoritative selection material for media deliberation.

The sole public interface takes a pinned projection and candidates.  It hides
freshness arithmetic, historical repetition and image-budget inspection while
leaving the social decision entirely to the model and later Acceptance.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from .media_v2 import PhotoCandidate
from .mood_view import mood_summary_prose
from .schema_core import FrozenModel


# ``.2`` fills the emotional_meaning signal from accepted, active Affect so
# the share/keep decision has an inner-state cause.  The advisory remains
# strictly model-facing material with no reducer or acceptance consumer.
ADVISORY_VERSION = "media-candidate-advisory.2"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class MediaCandidateAdvisory(FrozenModel):
    """Safe, model-readable material; it grants no media authority."""

    advisory_version: Literal["media-candidate-advisory.2"] = ADVISORY_VERSION
    candidate_id: str = Field(min_length=1, max_length=256)
    ecology_category: str = Field(min_length=1, max_length=128)
    freshness_bp: int = Field(ge=0, le=10_000)
    novelty_bp: int = Field(ge=0, le=10_000)
    visual_evidence_bp: int = Field(ge=0, le=10_000)
    budget_state: Literal["available", "constrained", "unconfigured"]
    advisory_score_bp: int = Field(ge=0, le=10_000)
    emotional_meaning: str | None = Field(default=None, min_length=1, max_length=400)
    missing_signals: tuple[Literal["emotional_meaning", "existing_media", "user_preference"], ...] = ()
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    def model_material(self) -> dict[str, object]:
        """Omit authority-bearing IDs and reduce numbers to a bounded context."""

        material: dict[str, object] = {
            "category": self.ecology_category,
            "freshness_bp": self.freshness_bp,
            "novelty_bp": self.novelty_bp,
            "visual_evidence_bp": self.visual_evidence_bp,
            "budget_state": self.budget_state,
            "advisory_score_bp": self.advisory_score_bp,
            "missing_signals": self.missing_signals,
        }
        if self.emotional_meaning is not None:
            material["emotional_meaning"] = self.emotional_meaning
        return material


class MediaCandidateAdvisoryCompiler:
    """Deep module for deterministic advisory compilation at a projection."""

    def compile(self, *, projection, candidate: PhotoCandidate) -> MediaCandidateAdvisory:  # type: ignore[no-untyped-def]
        if (
            projection.logical_time is None
            or candidate.opened_at is None
            or candidate.expires_at is None
            or candidate.ecology_category is None
        ):
            raise ValueError("media candidate advisory requires a P1 candidate and logical time")
        freshness = self._freshness(candidate=candidate, now=projection.logical_time)
        novelty = self._novelty(projection=projection, candidate=candidate, now=projection.logical_time)
        visual_evidence = 10_000 if len(candidate.source_events) >= 2 else 0
        budget_state, budget_score = self._budget(projection=projection)
        # Sharing a photo is a feeling-coloured act: the same lakeside light
        # reads differently on a bright day and on a drained one.  The mood
        # line comes only from accepted, active Affect; when nothing rises
        # above the noticeable floor the signal honestly stays missing.
        emotional_meaning = mood_summary_prose(
            getattr(projection, "affect_episodes", ())
        ) or None
        missing = tuple(
            signal
            for signal in ("emotional_meaning", "existing_media", "user_preference")
            if signal != "emotional_meaning" or emotional_meaning is None
        )
        score = (freshness + novelty + visual_evidence + budget_score) // 4
        material = {
            "advisory_version": ADVISORY_VERSION,
            "candidate_id": candidate.candidate_id,
            "ecology_category": candidate.ecology_category,
            "freshness_bp": freshness,
            "novelty_bp": novelty,
            "visual_evidence_bp": visual_evidence,
            "budget_state": budget_state,
            "advisory_score_bp": score,
            "emotional_meaning": emotional_meaning,
            "missing_signals": missing,
        }
        return MediaCandidateAdvisory(**material, digest=_digest(material))

    @staticmethod
    def _freshness(*, candidate: PhotoCandidate, now: datetime) -> int:
        assert candidate.opened_at is not None and candidate.expires_at is not None
        total = max(1.0, (candidate.expires_at - candidate.opened_at).total_seconds())
        remaining = max(0.0, (candidate.expires_at - now).total_seconds())
        return int(min(10_000, remaining * 10_000 / total))

    @staticmethod
    def _novelty(*, projection, candidate: PhotoCandidate, now: datetime) -> int:  # type: ignore[no-untyped-def]
        repeats = sum(
            1
            for item in getattr(projection, "media_opportunities", ())
            if item.ecology_category == candidate.ecology_category
            and item.ecology_observed_at is not None
            and now - timedelta(days=7) <= item.ecology_observed_at <= now
        )
        return 10_000 // (1 + repeats)

    @staticmethod
    def _budget(*, projection) -> tuple[Literal["available", "constrained", "unconfigured"], int]:  # type: ignore[no-untyped-def]
        accounts = tuple(item for item in getattr(projection, "budget_accounts", ()) if item.category == "image")
        if not accounts:
            return "unconfigured", 5_000
        available = sum(max(0, item.limit - item.spent - item.reserved) for item in accounts)
        return ("available", 10_000) if available > 0 else ("constrained", 0)


__all__ = ["ADVISORY_VERSION", "MediaCandidateAdvisory", "MediaCandidateAdvisoryCompiler"]
