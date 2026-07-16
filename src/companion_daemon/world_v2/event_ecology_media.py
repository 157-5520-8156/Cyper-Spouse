"""Derive picture opportunities from *already committed* lived-world evidence.

This is deliberately an ecology, not a second world author.  It cannot create
an activity, a place, a meal, an NPC interaction, or a mood.  Its one public
operation examines a pinned ledger projection and freezes a bounded number of
source-bound ``PhotoCandidate`` / ``MediaOpportunity`` pairs.  The image
machine subsequently owns how (or whether) that evidence becomes a picture.

The taxonomy is a selection vocabulary, rather than a social-behaviour rule:
it tells the selector which authority shapes are visually meaningful.  No
taxonomy item supplies a prompt, a caption, a posture, or missing event data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .image_evidence_contract import ImageEvidenceDeclaredPayload
from .media_v2 import (
    ImmutableMediaPayloadStore,
    MediaOpportunity,
    MediaOpportunityFrozenPayload,
    MediaEvidenceSource,
    PhotoCandidate,
    PhotoCandidateOpenedPayload,
    StoredMediaPayload,
    media_digest,
)
from .media_evidence_snapshot import (
    CompiledMediaEvidence,
    MediaEvidenceCompileRequest,
    MediaEvidenceNotRenderable,
    MediaEvidenceSnapshotCompiler,
)
from .schemas import ProjectionCursor, WorldEvent


EcologyCategory = Literal[
    "activity_process",
    "activity_result",
    "settled_outcome",
    "npc_shared_outcome",
    "shared_experience",
    "place_environment",
    "object_or_food",
]


# This is intentionally a small, inspectable *evidence* matrix.  A fact is
# eligible only when its predicate already says that a visible thing exists;
# arbitrary user facts (preferences, diagnoses, relationship interpretations,
# etc.) can never become a photo candidate merely by being committed.
_VISUAL_FACT_CATEGORY: dict[str, EcologyCategory] = {
    "environment.weather": "place_environment",
    "environment.light": "place_environment",
    "environment.location_grounding": "place_environment",
    "activity.visible_object": "object_or_food",
    "meal.visible_food": "object_or_food",
    "meal.visible_drink": "object_or_food",
}
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
_ECOLOGY_WAKE_EVENT_TYPES = frozenset({
    "ClockAdvanced",
    "ActivityStarted", "ActivityResumed", "ActivityCompleted", "ActivityAbandoned",
    "WorldOccurrenceSettled", "ExperienceCommitted", "FactCommitted", "FactCorrected",
    "NpcRegistered",
})
_ECOLOGY_CATEGORY_SET = frozenset({
    "activity_process", "activity_result", "settled_outcome", "npc_shared_outcome",
    "shared_experience", "place_environment", "object_or_food",
})


@dataclass(frozen=True, slots=True)
class EcologyPolicy:
    """Frequency policy for source-derived opportunities.

    The policy only suppresses repetitions.  It never invents an alternative
    event when one is suppressed.  Ordering is deterministic, so this first
    vertical uses no random draw (and therefore has no unrecorded randomness).
    """

    catalog_version: str = "event-ecology-media-candidate.1"
    max_candidates_per_drain: int = 2
    max_opportunities_per_day: int = 2
    category_cooldown: timedelta = timedelta(hours=6)
    default_expiry: timedelta = timedelta(hours=48)
    fleeting_expiry: timedelta = timedelta(hours=12)
    # The former one-step candidate → preview path is migration-only.  P1
    # replaces it with selection and authorization in Deliberation.
    direct_preview_compatibility: bool = False


@dataclass(frozen=True, slots=True)
class EcologyCandidate:
    """Internal selected evidence; no field is an assertion of a new event."""

    category: EcologyCategory
    source_event_refs: tuple[str, ...]
    source_payload_hashes: tuple[str, ...]
    privacy_ceiling: Literal["public", "shareable"]
    observed_at: datetime
    context: dict[str, object]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class EcologyDrainResult:
    status: Literal["created", "idle", "not_renderable"]
    candidate_ids: tuple[str, ...] = ()
    opportunity_ids: tuple[str, ...] = ()
    reason_code: str | None = None


class _MediaEvidenceCompiler(Protocol):
    def compile(self, request: MediaEvidenceCompileRequest) -> CompiledMediaEvidence: ...


class _ProjectionLike(Protocol):
    world_revision: int
    deliberation_revision: int
    ledger_sequence: int
    logical_time: datetime | None
    committed_world_event_refs: tuple[object, ...]
    plans: tuple[object, ...]
    world_occurrences: tuple[object, ...]
    experiences: tuple[object, ...]
    facts: tuple[object, ...]
    npcs: tuple[object, ...]
    photo_candidates: tuple[PhotoCandidate, ...]
    media_opportunities: tuple[MediaOpportunity, ...]


def _cursor(projection: _ProjectionLike) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _event_id(*, role: str, stable: str) -> str:
    return "event:event-ecology:" + role + ":" + media_digest({"role": role, "stable": stable})


def _identity(*, event_type: str, world_id: str, payload: dict[str, object]) -> str:
    identity = domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
    if identity is None:  # Both ecology records have installed domain identities.
        raise ValueError(f"event ecology has no installed identity for {event_type}")
    return identity


def _bounded_privacy(value: object) -> Literal["public", "shareable"] | None:
    if value not in _PRIVACY_RANK or _PRIVACY_RANK[value] > _PRIVACY_RANK["shareable"]:
        return None
    return value  # type: ignore[return-value]


def _canonical_sources(values: list[tuple[str, str]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ordered = tuple(sorted(set(values)))
    return tuple(item[0] for item in ordered), tuple(item[1] for item in ordered)


class EventEcologyMediaCandidateRuntime:
    """One deep seam: derive and freeze bounded media opportunities once.

    ``drain_once`` is safe after a clock tick, accepted life mutation, or
    background worker wake-up.  It is also safe to replay: candidate and
    opportunity IDs derive solely from frozen source identities and existing
    candidates/opportunities are joined rather than recreated.
    """

    def __init__(
        self, *, ledger: LedgerPort, sidecar: ImmutableMediaPayloadStore,
        policy: EcologyPolicy = EcologyPolicy(),
        compiler: _MediaEvidenceCompiler | None = None,
    ) -> None:
        self._ledger, self._sidecar, self._policy = ledger, sidecar, policy
        self._compiler = compiler or MediaEvidenceSnapshotCompiler(ledger=ledger)

    def drain_once(
        self, *, wake_event_ref: str, logical_time: datetime, actor: str, trace_id: str, correlation_id: str,
    ) -> EcologyDrainResult:
        projection = self._ledger.project()
        wake = next(
            (item for item in projection.committed_world_event_refs if item.event_id == wake_event_ref),
            None,
        )
        if wake is None or wake.event_type not in _ECOLOGY_WAKE_EVENT_TYPES:
            raise ValueError("event ecology requires a committed life/clock/worker wake event")
        if projection.logical_time is None or logical_time != projection.logical_time:
            raise ValueError("event ecology must run at the current authoritative logical time")
        if wake.logical_time > logical_time:
            raise ValueError("event ecology wake cannot be later than the authoritative logical time")
        candidates = self._discover(projection=projection, logical_time=logical_time)
        if not candidates:
            return EcologyDrainResult(status="idle")
        events: list[WorldEvent] = []
        candidate_ids: list[str] = []
        opportunity_ids: list[str] = []
        for selected in candidates:
            candidate_id = "photo-candidate:ecology:" + media_digest({
                "contract": self._policy.catalog_version,
                "world_id": self._ledger.world_id,
                "category": selected.category,
                "sources": selected.source_event_refs,
            })
            opportunity_id = "media-opportunity:ecology:" + media_digest({
                "candidate_id": candidate_id,
                "compiler_contract": "world-image-event-snapshot-v1",
                "snapshot": selected.source_payload_hashes,
            })
            candidate = PhotoCandidate(
                candidate_id=candidate_id, source_event_refs=selected.source_event_refs,
                family="life_share", privacy_ceiling=selected.privacy_ceiling,
                opened_at=logical_time,
                expires_at=selected.expires_at,
                ecology_category=selected.category,
                ecology_observed_at=selected.observed_at,
                source_events=tuple(
                    MediaEvidenceSource(event_ref=event_ref, payload_hash=payload_hash)
                    for event_ref, payload_hash in zip(
                        selected.source_event_refs, selected.source_payload_hashes, strict=True
                    )
                ),
            )
            candidate_payload = PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json")
            previous = events[-1].event_id if events else f"event:ecology-wake:{projection.ledger_sequence}"
            candidate_event = WorldEvent.from_payload(
                schema_version="world-v2.1", event_id=_event_id(role="candidate", stable=candidate_id),
                event_type="PhotoCandidateOpened", world_id=self._ledger.world_id,
                logical_time=logical_time, created_at=logical_time, actor=actor,
                source="world-v2:event-ecology", trace_id=trace_id, causation_id=previous,
                correlation_id=correlation_id,
                idempotency_key=_identity(event_type="PhotoCandidateOpened", world_id=self._ledger.world_id, payload=candidate_payload),
                payload=candidate_payload,
            )
            events.append(candidate_event)
            candidate_ids.append(candidate_id)
            if not self._policy.direct_preview_compatibility:
                # P1's normal path exposes source-bound candidates only.  A
                # separate selector/authorizer decides whether any becomes a
                # frozen opportunity; ecology itself never chooses a photo.
                continue
            try:
                compiled = self._compiler.compile(MediaEvidenceCompileRequest(
                    candidate=candidate, category=selected.category, cursor=_cursor(projection),
                ))
            except MediaEvidenceNotRenderable as exc:
                # Do not substitute a generic image when a source cannot be
                # rendered.  No partial candidate/opportunity batch is
                # committed before the future suppression state machine exists.
                return EcologyDrainResult(status="not_renderable", reason_code=exc.reason_code)
            snapshot_body = compiled.snapshot_body
            snapshot_ref = compiled.snapshot_ref
            snapshot_hash = compiled.snapshot_hash
            self._sidecar.put_if_absent(StoredMediaPayload(
                payload_ref=snapshot_ref, payload_hash=snapshot_hash,
                content_type="application/vnd.world-v2.media-opportunity+json", body=snapshot_body,
            ))
            opportunity = MediaOpportunity(
                opportunity_id=opportunity_id, candidate_id=candidate_id, family="life_share",
                delivery_mode="preview", privacy_ceiling=selected.privacy_ceiling,
                media_privacy_ceiling="ordinary",
                event_snapshot_ref=snapshot_ref, event_snapshot_hash=snapshot_hash,
                source_event_refs=selected.source_event_refs, catalog_version=self._policy.catalog_version,
                ecology_category=selected.category, ecology_observed_at=selected.observed_at,
                expires_at=selected.expires_at,
            )
            opportunity_payload = MediaOpportunityFrozenPayload(opportunity=opportunity).model_dump(mode="json")
            events.append(
                WorldEvent.from_payload(
                    schema_version="world-v2.1", event_id=_event_id(role="opportunity", stable=opportunity_id),
                    event_type="MediaOpportunityFrozen", world_id=self._ledger.world_id,
                    logical_time=logical_time, created_at=logical_time, actor=actor,
                    source="world-v2:event-ecology", trace_id=trace_id,
                    causation_id=candidate_event.event_id, correlation_id=correlation_id,
                    idempotency_key=_identity(event_type="MediaOpportunityFrozen", world_id=self._ledger.world_id, payload=opportunity_payload),
                    payload=opportunity_payload,
                )
            )
            opportunity_ids.append(opportunity_id)
        self._ledger.commit_at_cursor(
            tuple(events), expected_cursor=_cursor(projection),
            commit_id="commit:event-ecology:" + media_digest([event.event_id for event in events]),
        )
        return EcologyDrainResult(status="created", candidate_ids=tuple(candidate_ids), opportunity_ids=tuple(opportunity_ids))

    def _discover(self, *, projection: _ProjectionLike, logical_time: datetime) -> tuple[EcologyCandidate, ...]:
        refs = {item.event_id: item for item in projection.committed_world_event_refs}
        declarations = self._declared_visual_sources(projection=projection, refs=refs)
        existing_sources = {item.source_event_refs for item in projection.photo_candidates}
        historical = self._historical_categories(projection=projection)
        daily = sum(
            1 for category, at in historical
            if at >= logical_time - timedelta(days=1) and at <= logical_time
        )
        if daily >= self._policy.max_opportunities_per_day:
            return ()
        discovered: list[EcologyCandidate] = []

        def add(
            *, category: EcologyCategory, source_refs: tuple[str, ...], privacy: object,
            observed_at: datetime, context: dict[str, object], expiry: timedelta,
        ) -> None:
            nonlocal daily
            allowed = _bounded_privacy(privacy)
            sources = tuple(sorted(set(source_refs)))
            if allowed is None or not sources or sources in existing_sources or daily >= self._policy.max_opportunities_per_day:
                return
            selected_refs: list[tuple[str, str]] = []
            for source_ref in sources:
                committed = refs.get(source_ref)
                if committed is None:
                    return
                selected_refs.append((committed.event_id, committed.payload_hash))
            if declarations is not None:
                declaration_visibilities: list[Literal["public", "shareable"]] = []
                for source_ref in sources:
                    declaration = declarations.get(source_ref)
                    if declaration is None:
                        # A production ledger can discover a candidate only
                        # after a separate accepted visual declaration.  This
                        # prevents bare activity/fact envelopes from turning
                        # into generic lifestyle pictures later.
                        return
                    declaration_ref, declaration_hash, declaration_visibility = declaration
                    selected_refs.append((declaration_ref, declaration_hash))
                    declaration_visibilities.append(declaration_visibility)
                if allowed == "shareable" and "public" in declaration_visibilities:
                    allowed = "public"
            # A category cooldown is recorded in the ordinary media projection;
            # it survives replay/restart without a shadow mutable history.
            if any(
                category == prior_category
                and logical_time >= prior_at
                and logical_time - prior_at < self._policy.category_cooldown
                for prior_category, prior_at in historical
            ) or any(item.category == category for item in discovered):
                return
            source_ids, source_hashes = _canonical_sources(selected_refs)
            discovered.append(EcologyCandidate(
                category=category, source_event_refs=source_ids, source_payload_hashes=source_hashes,
                privacy_ceiling=allowed, observed_at=observed_at, context=context,
                expires_at=logical_time + expiry,
            ))
            daily += 1

        for plan in projection.plans:
            origin = getattr(plan, "authority_origin", None)
            if origin is None or getattr(plan, "status", None) not in {"active", "completed"}:
                continue
            add(
                category="activity_process" if plan.status == "active" else "activity_result",
                source_refs=(origin.accepted_event_ref,), privacy=getattr(plan, "privacy_class", None),
                observed_at=getattr(plan, "last_transitioned_at", None) or getattr(origin, "accepted_at"),
                context=self._activity_context(plan),
                expiry=self._policy.fleeting_expiry if plan.status == "active" else self._policy.default_expiry,
            )
        npc_by_id = {item.npc_id: item for item in projection.npcs}
        for occurrence in projection.world_occurrences:
            if getattr(occurrence, "status", None) != "settled" or getattr(occurrence, "settlement_event_ref", None) is None:
                continue
            participant_npcs = tuple(
                item for item in getattr(occurrence, "participant_refs", ())
                if item.startswith("npc:") and _bounded_privacy(getattr(npc_by_id.get(item), "privacy_class", None)) is not None
            )
            add(
                category="npc_shared_outcome" if participant_npcs else "settled_outcome",
                source_refs=(occurrence.settlement_event_ref,), privacy=getattr(occurrence, "visibility", None),
                observed_at=getattr(occurrence, "settled_at", None),
                context={
                    "location_ref": occurrence.location_ref,
                    "participant_refs": tuple(occurrence.participant_refs),
                    "settled_outcome_ref": occurrence.settled_outcome_ref,
                    "npc_participant_refs": participant_npcs,
                }, expiry=self._policy.default_expiry,
            )
        for experience in projection.experiences:
            origin = getattr(experience, "origin", None)
            values = getattr(experience, "values", None)
            if origin is None or values is None:
                continue
            add(
                category="shared_experience", source_refs=(origin.accepted_event_ref,),
                privacy=getattr(values, "privacy_class", None), observed_at=values.occurred_to,
                context={
                    "summary_ref": values.summary_ref,
                    "summary_payload_hash": values.summary_payload_hash,
                    "participant_refs": tuple(values.participant_refs),
                }, expiry=self._policy.default_expiry,
            )
        for fact in projection.facts:
            values = getattr(fact, "values", None)
            origin = getattr(fact, "origin", None)
            category = _VISUAL_FACT_CATEGORY.get(getattr(values, "predicate_code", ""))
            if category is None or origin is None or values is None or getattr(values, "status", None) != "active":
                continue
            add(
                category=category, source_refs=(origin.accepted_event_ref,),
                privacy=getattr(values, "privacy_class", None), observed_at=getattr(fact, "updated_at", None),
                context={
                    "subject_ref": values.subject_ref,
                    "predicate_code": values.predicate_code,
                    "value_ref": values.value_ref,
                    "value_hash": values.value_hash,
                }, expiry=self._policy.fleeting_expiry if category == "place_environment" else self._policy.default_expiry,
            )
        # Newest evidence wins while deterministic IDs make retries/replay
        # independent of Python iteration order.
        ordered = sorted(discovered, key=lambda item: (-item.observed_at.timestamp(), item.category, item.source_event_refs))
        return tuple(ordered[: self._policy.max_candidates_per_drain])

    def _declared_visual_sources(
        self, *, projection: _ProjectionLike, refs: dict[str, object],
    ) -> dict[str, tuple[str, str, Literal["public", "shareable"]]] | None:
        """Return declarations by their exact source, or legacy ``None``.

        Real ledgers provide immutable event lookup; embedded historical test
        adapters that cannot inspect declaration bytes retain the former
        candidate-discovery behavior only as a compatibility surface.  The
        production SQLite ledger always takes the declaration-required branch.
        """

        lookup = getattr(self._ledger, "lookup_event_commit", None)
        if not callable(lookup):
            return None
        declared: dict[str, tuple[str, str, Literal["public", "shareable"]]] = {}
        ambiguous: set[str] = set()
        for ref in projection.committed_world_event_refs:
            if getattr(ref, "event_type", None) != "ImageEvidenceDeclared":
                continue
            located = lookup(ref.event_id)
            if located is None:
                continue
            event, _commit = located
            if event.payload_hash != getattr(ref, "payload_hash", None):
                continue
            try:
                payload = ImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
            except ValueError:
                continue
            source = refs.get(payload.source_event_ref)
            if (
                source is None
                or getattr(source, "event_type", None) != payload.source_event_type
                or getattr(source, "payload_hash", None) != payload.source_event_payload_hash
            ):
                continue
            if payload.source_event_ref in declared:
                ambiguous.add(payload.source_event_ref)
                continue
            declared[payload.source_event_ref] = (
                event.event_id,
                event.payload_hash,
                payload.image_evidence.visibility,
            )
        for source_ref in ambiguous:
            declared.pop(source_ref, None)
        return declared

    def _historical_categories(self, *, projection: _ProjectionLike) -> tuple[tuple[EcologyCategory, datetime], ...]:
        values: list[tuple[EcologyCategory, datetime]] = []
        for opportunity in projection.media_opportunities:
            # Only ecology-created opportunities participate.  Existing manual
            # media has no trustworthy category coordinate and must not be
            # guessed from prose or image output.
            if opportunity.catalog_version != self._policy.catalog_version:
                continue
            category = opportunity.ecology_category
            at = opportunity.ecology_observed_at
            if category in _ECOLOGY_CATEGORY_SET and at is not None and at.tzinfo is not None:
                values.append((category, at))
        return tuple(values)

    @staticmethod
    def _activity_context(plan: object) -> dict[str, object]:
        return {
            "activity_kind": plan.activity_kind,
            "location_ref": plan.location_ref,
            "participant_refs": tuple(plan.participant_refs),
            "status": plan.status,
        }


__all__ = [
    "EcologyCandidate", "EcologyCategory", "EcologyDrainResult", "EcologyPolicy",
    "EventEcologyMediaCandidateRuntime",
]
