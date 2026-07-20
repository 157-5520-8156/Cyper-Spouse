"""Author source-bound visual declarations from settled reviewed life.

This is the missing supply seam between the life ecology and the image
machine: committed occurrences alone are envelopes, and the production media
ecology deliberately refuses to photograph an envelope without a separate
accepted visual declaration.  The author closes that gap without becoming a
second world writer:

- it only reads occurrences that already settled through the aftermath lane;
- every visible fact it declares is copied verbatim from the reviewed
  ``visual_evidence`` annex of the opening that produced the occurrence, plus
  the settled outcome text that is already immutable sidecar content;
- whether she "bothers to keep this moment photographable" is one recorded
  uniform draw per occurrence (stable across retries), compared against a
  deterministic threshold modulated by the reviewed visual class, accepted
  Affect and the day's declaration rhythm;
- the write itself goes through the existing declaration runtimes, so source
  privacy, hash binding and idempotent identity stay owned by those seams.

The author never opens an opportunity, never renders, and never sends.  A
declared occurrence still has to survive candidate discovery, bounded
selection, Acceptance, planning and inspection downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import logging
from types import MappingProxyType
from typing import Literal, Mapping, Protocol

from .character_media_fact_binder import CharacterMediaCandidateRuntime
from .image_evidence_contract import CharacterMediaEvidenceV1, ImageEvidenceV1
from .image_evidence_runtime import (
    ImageEvidenceDeclarationCommand,
    ImageEvidenceDeclarationRuntime,
)
from .life_author_seed import (
    ReviewedLifeSeedCatalog,
    ReviewedLifeSeedOpening,
    ReviewedOpeningVisualEvidence,
)
from .mood_view import active_mood_intensities
from .private_image_evidence_contract import RecipientScopedImageEvidenceV1
from .private_image_evidence_runtime import (
    RecipientScopedImageEvidenceDeclarationCommand,
    RecipientScopedImageEvidenceDeclarationRuntime,
)
from .random_authority import RandomAuthority
from .schema_core import FrozenModel


_LOG = logging.getLogger(__name__)

_DECLARATION_EVENT_TYPES = frozenset({
    "ImageEvidenceDeclared", "RecipientScopedImageEvidenceDeclared",
})
_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})
_POSITIVE_MOODS = ("warmth", "joy")
_NEGATIVE_MOODS = ("hurt", "anger", "sadness", "loneliness", "anxiety", "resentment")
# 40 recorded buckets of 250bp keep the whole chance draw inside one stable
# RandomDrawRecorded event while still allowing mood to move the threshold.
_BUCKET_COUNT = 40
_BUCKET_WIDTH_BP = 10_000 // _BUCKET_COUNT
_PRIVATE_ELIGIBLE_STAGES = frozenset({"close_friend", "ambiguous", "lover"})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class VisualEvidenceAuthorPolicy:
    """Rhythm and chance policy, versioned independently of the seed catalog.

    The policy suppresses and weights; it never invents an occurrence or a
    visible fact.  ``base_share_chance_bp`` is the per-visual-class mass an
    eligible settled occurrence has of being kept photographable at a neutral
    mood; the reviewed annex may override it per opening.
    """

    catalog_version: str = "life-visual-evidence.1"
    lookback: timedelta = timedelta(hours=12)
    min_gap: timedelta = timedelta(hours=2)
    max_declarations_per_day: int = 3
    max_private_declarations_per_day: int = 1
    base_share_chance_bp: Mapping[str, int] = field(
        default_factory=lambda: MappingProxyType({
            "place": 4_500,
            "food": 4_000,
            "object": 3_800,
            "character": 3_500,
            "activity": 3_200,
            "social": 3_000,
            "private_transition": 2_800,
            "ambient": 2_200,
        })
    )
    # Accepted Affect modulates the threshold, not the recorded draw: a warm
    # day makes the same ticket cross the line, a heavy day holds it back,
    # and an undeclared moment may still be picked up by a later, brighter
    # wake while its lookback lasts.
    mood_positive_gain_bp: int = 4_000
    mood_negative_drop_bp: int = 5_000
    multiplier_floor_bp: int = 5_000
    multiplier_cap_bp: int = 14_000
    threshold_cap_bp: int = 9_500

    def __post_init__(self) -> None:
        if self.max_declarations_per_day < 1 or self.max_private_declarations_per_day < 0:
            raise ValueError("visual evidence policy caps are invalid")
        if self.lookback <= timedelta(0) or self.min_gap < timedelta(0):
            raise ValueError("visual evidence policy windows are invalid")


class VisualEvidenceAuthorResult(FrozenModel):
    status: Literal["declared", "idle", "unavailable"]
    reason_code: str
    declared_event_ref: str | None = None
    declared_source_ref: str | None = None
    lane: Literal["public", "private"] | None = None
    opened_candidate_ids: tuple[str, ...] = ()


class _ProjectionLike(Protocol):
    logical_time: datetime | None
    committed_world_event_refs: tuple[object, ...]
    plans: tuple[object, ...]
    world_occurrences: tuple[object, ...]
    affect_episodes: tuple[object, ...]


class LifeVisualEvidenceAuthor:
    """Turn one settled, annex-backed occurrence into one visual declaration."""

    def __init__(
        self,
        *,
        ledger,  # type: ignore[no-untyped-def]
        catalog: ReviewedLifeSeedCatalog,
        content_store,  # type: ignore[no-untyped-def]
        character_ref: str,
        recipient_ref: str | None = None,
        policy: VisualEvidenceAuthorPolicy = VisualEvidenceAuthorPolicy(),
        image_evidence: ImageEvidenceDeclarationRuntime | None = None,
        recipient_scoped: RecipientScopedImageEvidenceDeclarationRuntime | None = None,
        character_candidates: CharacterMediaCandidateRuntime | None = None,
        actor: str = "worker:world-v2:life-visual-evidence",
    ) -> None:
        if not character_ref or not actor:
            raise ValueError("life visual evidence author requires character and actor refs")
        self._ledger = ledger
        self._catalog = catalog
        self._content_store = content_store
        self._character_ref = character_ref
        self._recipient_ref = recipient_ref
        self._policy = policy
        self._image_evidence = image_evidence or ImageEvidenceDeclarationRuntime(
            ledger=ledger, source="world-v2:life-visual-evidence"
        )
        self._recipient_scoped = recipient_scoped or RecipientScopedImageEvidenceDeclarationRuntime(
            ledger=ledger, source="world-v2:life-visual-evidence"
        )
        self._character_candidates = character_candidates or CharacterMediaCandidateRuntime(
            ledger=ledger
        )
        self._random = RandomAuthority(ledger=ledger, source="world-v2:life-visual-evidence-random")
        self._actor = actor

    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> VisualEvidenceAuthorResult:
        projection: _ProjectionLike = self._ledger.project()
        logical_time = getattr(projection, "logical_time", None)
        if not isinstance(logical_time, datetime):
            return VisualEvidenceAuthorResult(
                status="unavailable", reason_code="visual_evidence.logical_time_unavailable"
            )
        declared_sources, recent = self._declaration_ledger_view(
            projection=projection, logical_time=logical_time
        )
        daily = sum(1 for _lane, at in recent if logical_time - at <= timedelta(days=1))
        if daily >= self._policy.max_declarations_per_day:
            return VisualEvidenceAuthorResult(
                status="idle", reason_code="visual_evidence.daily_budget_exhausted"
            )
        if recent and logical_time - max(at for _lane, at in recent) < self._policy.min_gap:
            return VisualEvidenceAuthorResult(
                status="idle", reason_code="visual_evidence.min_gap_not_elapsed"
            )
        private_today = sum(
            1 for lane, at in recent
            if lane == "private" and logical_time - at <= timedelta(days=1)
        )
        mood_multiplier = self._mood_multiplier_bp(projection)
        eligible = self._eligible_occurrences(
            projection=projection, logical_time=logical_time, declared_sources=declared_sources,
        )
        if not eligible:
            return VisualEvidenceAuthorResult(
                status="idle", reason_code="visual_evidence.no_eligible_settled_occurrence"
            )
        for occurrence, opening, annex, lane in eligible:
            if lane == "private":
                if (
                    private_today >= self._policy.max_private_declarations_per_day
                    or not self._recipient_relationship_ready(projection)
                ):
                    continue
            threshold = self._threshold_bp(
                opening=opening, annex=annex, mood_multiplier_bp=mood_multiplier
            )
            bucket = self._chance_bucket(occurrence=occurrence, logical_time=logical_time,
                                         trace_id=trace_id, correlation_id=correlation_id)
            if bucket * _BUCKET_WIDTH_BP + _BUCKET_WIDTH_BP // 2 >= threshold:
                continue
            return self._declare(
                occurrence=occurrence, opening=opening, annex=annex, lane=lane,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        return VisualEvidenceAuthorResult(
            status="idle", reason_code="visual_evidence.nothing_selected"
        )

    # -- discovery -------------------------------------------------------

    def _eligible_occurrences(
        self, *, projection: _ProjectionLike, logical_time: datetime,
        declared_sources: frozenset[str],
    ) -> tuple[tuple[object, ReviewedLifeSeedOpening, ReviewedOpeningVisualEvidence, str], ...]:
        plans = {
            plan_id: item
            for item in getattr(projection, "plans", ())
            if (plan_id := getattr(item, "plan_id", None))
        }
        rows: list[tuple[object, ReviewedLifeSeedOpening, ReviewedOpeningVisualEvidence, str]] = []
        for occurrence in getattr(projection, "world_occurrences", ()):
            settled_at = getattr(occurrence, "settled_at", None)
            settlement_ref = getattr(occurrence, "settlement_event_ref", None)
            if (
                getattr(occurrence, "status", None) != "settled"
                or not isinstance(settled_at, datetime)
                or settlement_ref is None
                or settlement_ref in declared_sources
                or settled_at > logical_time
                or logical_time - settled_at > self._policy.lookback
            ):
                continue
            plan = plans.get(getattr(occurrence, "trigger_ref", None))
            activity_kind = getattr(plan, "activity_kind", None)
            if not isinstance(activity_kind, str):
                continue
            opening = self._catalog.opening_for_activity(activity_kind)
            if opening is None or opening.visual_evidence is None:
                continue
            annex = opening.visual_evidence
            visibility = getattr(occurrence, "visibility", None)
            if (
                visibility in _PUBLIC_VISIBILITIES
                and opening.visual_potential not in {"none", "private_transition"}
            ):
                rows.append((occurrence, opening, annex, "public"))
            elif (
                visibility == "private"
                and opening.visual_potential == "private_transition"
                and self._recipient_ref is not None
                and annex.self_capture
            ):
                rows.append((occurrence, opening, annex, "private"))
        rows.sort(key=lambda row: (getattr(row[0], "settled_at"), getattr(row[0], "occurrence_id", "")), reverse=True)
        return tuple(rows)

    def _declaration_ledger_view(
        self, *, projection: _ProjectionLike, logical_time: datetime,
    ) -> tuple[frozenset[str], tuple[tuple[str, datetime], ...]]:
        """Collect already-declared source refs and recent declaration beats."""

        lookup = getattr(self._ledger, "lookup_event_commit", None)
        declared: set[str] = set()
        recent: list[tuple[str, datetime]] = []
        for ref in getattr(projection, "committed_world_event_refs", ()):
            if getattr(ref, "event_type", None) not in _DECLARATION_EVENT_TYPES:
                continue
            at = getattr(ref, "logical_time", None)
            if isinstance(at, datetime) and at <= logical_time:
                lane = (
                    "private"
                    if ref.event_type == "RecipientScopedImageEvidenceDeclared"
                    else "public"
                )
                recent.append((lane, at))
            if not callable(lookup):
                continue
            located = lookup(ref.event_id)
            if located is None:
                continue
            event, _commit = located
            try:
                payload = event.payload()
            except (AttributeError, ValueError):
                continue
            source_ref = payload.get("source_event_ref")
            if isinstance(source_ref, str):
                declared.add(source_ref)
        return frozenset(declared), tuple(recent)

    # -- controlled chance -------------------------------------------------

    def _mood_multiplier_bp(self, projection: _ProjectionLike) -> int:
        intensities = active_mood_intensities(getattr(projection, "affect_episodes", ()))
        positive = max((intensities.get(name, 0) for name in _POSITIVE_MOODS), default=0)
        negative = max((intensities.get(name, 0) for name in _NEGATIVE_MOODS), default=0)
        multiplier = (
            10_000
            + positive * self._policy.mood_positive_gain_bp // 10_000
            - negative * self._policy.mood_negative_drop_bp // 10_000
        )
        return max(self._policy.multiplier_floor_bp, min(self._policy.multiplier_cap_bp, multiplier))

    def _threshold_bp(
        self, *, opening: ReviewedLifeSeedOpening,
        annex: ReviewedOpeningVisualEvidence, mood_multiplier_bp: int,
    ) -> int:
        base = (
            annex.share_chance_bp
            if annex.share_chance_bp is not None
            else self._policy.base_share_chance_bp.get(opening.visual_potential, 2_500)
        )
        return min(self._policy.threshold_cap_bp, base * mood_multiplier_bp // 10_000)

    def _chance_bucket(
        self, *, occurrence, logical_time: datetime, trace_id: str, correlation_id: str,
    ) -> int:
        """One stable recorded uniform ticket per settled occurrence."""

        settlement_ref = getattr(occurrence, "settlement_event_ref")
        buckets = tuple(f"chance-bucket:{index:02d}" for index in range(_BUCKET_COUNT))
        draw = self._random.draw(
            attempt_id="attempt:visual-evidence:" + _digest([self._ledger.world_id, settlement_ref]),
            candidate_refs=buckets,
            catalog_version=self._policy.catalog_version,
            logical_time=logical_time,
            seed_instant=getattr(occurrence, "settled_at"),
            actor=self._actor,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return int(draw.selected_candidate_ref.rsplit(":", 1)[1])

    # -- declaration -------------------------------------------------------

    def _declare(
        self, *, occurrence, opening: ReviewedLifeSeedOpening,
        annex: ReviewedOpeningVisualEvidence, lane: str, trace_id: str, correlation_id: str,
    ) -> VisualEvidenceAuthorResult:
        settlement_ref = getattr(occurrence, "settlement_event_ref")
        summary = self._settled_summary(occurrence)
        location = self._location_facts(annex)
        environment = self._environment_facts(annex)
        objects = tuple(
            {
                key: value
                for key, value in {
                    "id": item.id, "kind": item.kind, "description": item.description,
                }.items()
                if value is not None
            }
            for item in annex.objects
        )
        activity: dict[str, object] = {
            "id": getattr(occurrence, "trigger_ref", None) or opening.activity_kind,
            "kind": opening.activity_kind,
            "description": annex.activity_description,
        }
        character_media = (
            CharacterMediaEvidenceV1(
                character_ref=self._character_ref,
                present=True,
                capture_capabilities=annex.self_capture,
            )
            if annex.self_capture
            else None
        )
        projection = self._ledger.project()
        logical_time = projection.logical_time
        command_id = "visual-evidence:" + _digest([self._ledger.world_id, settlement_ref, lane])
        if lane == "private":
            activity["private_transition"] = True
            evidence = RecipientScopedImageEvidenceV1(
                visibility="private",
                summary=summary,
                activity=activity,
                location=location,
                environment=environment,
                character_media=character_media,
            )
            assert self._recipient_ref is not None
            commit = self._recipient_scoped.declare(
                RecipientScopedImageEvidenceDeclarationCommand(
                    command_id=command_id,
                    source_event_ref=settlement_ref,
                    recipient_ref=self._recipient_ref,
                    image_evidence=evidence,
                ),
                logical_time=logical_time, created_at=logical_time, actor=self._actor,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        else:
            evidence = ImageEvidenceV1(
                visibility=getattr(occurrence, "visibility"),
                summary=summary,
                activity=activity,
                location=location,
                environment=environment,
                objects=objects,
                character_media=character_media,
            )
            commit = self._image_evidence.declare(
                ImageEvidenceDeclarationCommand(
                    command_id=command_id,
                    source_event_ref=settlement_ref,
                    image_evidence=evidence,
                ),
                logical_time=logical_time, created_at=logical_time, actor=self._actor,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        declared_ref = next(iter(getattr(commit, "event_ids", ())), None)
        opened: tuple[str, ...] = ()
        if declared_ref is not None and character_media is not None:
            try:
                opened = self._character_candidates.open_once(
                    wake_event_ref=declared_ref,
                    logical_time=logical_time,
                    actor=self._actor,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ValueError:
                # The declaration itself is durable; candidate opening can be
                # retried by any later declaration-aware pass without losing
                # or duplicating the declared evidence.
                _LOG.warning("character media candidates could not open for %s", declared_ref)
        return VisualEvidenceAuthorResult(
            status="declared",
            reason_code="visual_evidence.declared",
            declared_event_ref=declared_ref,
            declared_source_ref=settlement_ref,
            lane=lane,  # type: ignore[arg-type]
            opened_candidate_ids=opened,
        )

    def _settled_summary(self, occurrence) -> str | None:  # type: ignore[no-untyped-def]
        content_ref = getattr(occurrence, "result_payload_ref", None)
        content_hash = getattr(occurrence, "result_payload_hash", None)
        if content_ref is None or content_hash is None or self._content_store is None:
            return None
        record = self._content_store.read_exact(content_ref=content_ref)
        if record is None or record.content_payload_hash != content_hash:
            return None
        text = record.text.strip()
        return text[:480] if text else None

    @staticmethod
    def _location_facts(annex: ReviewedOpeningVisualEvidence) -> dict[str, object] | None:
        if annex.location is None:
            return None
        return {
            key: value
            for key, value in {
                "id": annex.location.id,
                "kind": annex.location.kind,
                "city": annex.location.city,
                "publicness": annex.location.publicness,
                "mirror_available": annex.location.mirror_available,
            }.items()
            if value is not None
        }

    @staticmethod
    def _environment_facts(annex: ReviewedOpeningVisualEvidence) -> dict[str, object] | None:
        if annex.environment is None:
            return None
        facts = {
            key: value
            for key, value in {
                "light": annex.environment.light,
                "structure": annex.environment.structure,
            }.items()
            if value is not None
        }
        return facts or None

    def _recipient_relationship_ready(self, projection: _ProjectionLike) -> bool:
        """P3 evidence only makes sense once the relationship carries it.

        The stage floor mirrors the media authorizer's own eligibility (any
        stage below ``close_friend`` raises there); declaring earlier would
        only accumulate dead recipient-scoped candidates.
        """

        if self._recipient_ref is None:
            return False
        states = tuple(
            state
            for state in getattr(projection, "relationship_states", ())
            if getattr(state, "subject_ref", None) == self._recipient_ref
        )
        if len(states) != 1:
            return False
        return getattr(states[0], "stage", None) in _PRIVATE_ELIGIBLE_STAGES


__all__ = [
    "LifeVisualEvidenceAuthor",
    "VisualEvidenceAuthorPolicy",
    "VisualEvidenceAuthorResult",
]
