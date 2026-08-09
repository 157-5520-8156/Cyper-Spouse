"""Per-NPC shared-history evidence and legacy relationship compatibility.

``relationship_states`` describes the slow variables of the user
relationship.  This module provides the narrower, observable side of an NPC
relationship as a *derived Projection* in the CONTEXT.md sense: a
deterministic, rebuildable view over already-settled shared World Events,
never a second write authority and never a reader of the protagonist's
private Appraisal/Affect.

The primary seam is :class:`SharedHistoryEvidence`: settled shared-event
count, last occurrence time, and exact settlement sources.  It deliberately
does not assign a relationship meaning.  ``NpcRelationshipReading`` remains
as a compatibility projection for retired/dashboard consumers; new actor
capsules must use the neutral evidence instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .schema_core import FrozenModel


NPC_RELATIONSHIP_VIEW_VERSION = "npc-relationship-view.1"

# A shared moment inside this window still feels current.
_RECENT_WINDOW = timedelta(days=7)

# The indifferent starting point for someone she merely knows exists.
RESTING_CLOSENESS_BP = 3_000

_RECENT_SHARED_BP = 450
_OLDER_SHARED_BP = 150
_FAMILIARITY_PER_SHARED_BP = 900


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class NpcRelationshipReading(FrozenModel):
    """One NPC's derived relationship variables, ledger-backed via sources."""

    npc_ref: str
    closeness_bp: int
    familiarity_bp: int
    settled_shared_count: int
    last_shared_at: datetime | None = None
    source_event_refs: tuple[str, ...] = ()


class SharedHistoryEvidence(FrozenModel):
    """Neutral, actor-scoped evidence of settled shared occurrences."""

    npc_ref: str
    settled_shared_count: int
    last_shared_at: datetime | None = None
    source_event_refs: tuple[str, ...] = ()


def npc_shared_history_evidence(
    projection,
    *,
    protagonist_actor_ref: str,
    npc_refs: tuple[str, ...] | None = None,
) -> tuple[SharedHistoryEvidence, ...]:
    """Project only settled events both the protagonist and NPC participated in."""

    if not protagonist_actor_ref:
        raise ValueError("NPC shared-history evidence requires the protagonist actor ref")
    requested_refs = None if npc_refs is None else frozenset(npc_refs)
    npcs = tuple(
        item
        for item in getattr(projection, "npcs", ())
        if requested_refs is None or f"npc:{item.npc_id}" in requested_refs
    )
    occurrences = tuple(getattr(projection, "world_occurrences", ()))
    evidence: list[SharedHistoryEvidence] = []
    for npc in npcs:
        npc_ref = f"npc:{npc.npc_id}"
        participant_refs = {npc_ref}
        edge = getattr(npc, "promotion_edge", None)
        if edge is not None:
            participant_refs.add(edge.provisional_entity_ref)
        source_refs: list[str] = []
        last_shared_at: datetime | None = None
        for occurrence in occurrences:
            if (
                occurrence.status != "settled"
                or occurrence.settled_at is None
                or occurrence.settlement_event_ref is None
                or not participant_refs.intersection(occurrence.participant_refs)
                or protagonist_actor_ref not in occurrence.participant_refs
            ):
                continue
            source_refs.append(occurrence.settlement_event_ref)
            if last_shared_at is None or occurrence.settled_at > last_shared_at:
                last_shared_at = occurrence.settled_at
        evidence.append(
            SharedHistoryEvidence(
                npc_ref=npc_ref,
                settled_shared_count=len(source_refs),
                last_shared_at=last_shared_at,
                source_event_refs=tuple(dict.fromkeys(source_refs)),
            )
        )
    evidence.sort(key=lambda item: item.npc_ref)
    return tuple(evidence)


def npc_relationship_readings(
    projection,
    *,
    protagonist_actor_ref: str,
    npc_refs: tuple[str, ...] | None = None,
) -> tuple[NpcRelationshipReading, ...]:
    """Derive each NPC reading from events both actors actually participated in.

    An NPC-only occurrence is valid NPC history, but it is not shared history
    with the protagonist and therefore cannot warm familiarity or closeness.
    The protagonist identity is explicit so deployments cannot silently rely
    on one conventional actor ref.
    """

    if not protagonist_actor_ref:
        raise ValueError("NPC relationship view requires the protagonist actor ref")
    logical_time = getattr(projection, "logical_time", None)
    requested_refs = None if npc_refs is None else frozenset(npc_refs)
    npcs = tuple(
        item
        for item in getattr(projection, "npcs", ())
        if requested_refs is None or f"npc:{item.npc_id}" in requested_refs
    )
    if logical_time is None or not npcs:
        return ()
    occurrences = tuple(getattr(projection, "world_occurrences", ()))
    shared_history = npc_shared_history_evidence(
        projection,
        protagonist_actor_ref=protagonist_actor_ref,
        npc_refs=npc_refs,
    )
    shared_by_ref = {item.npc_ref: item for item in shared_history}
    settled_at_by_ref = {
        occurrence.settlement_event_ref: occurrence.settled_at
        for occurrence in occurrences
        if occurrence.settlement_event_ref is not None and occurrence.settled_at is not None
    }
    readings: list[NpcRelationshipReading] = []
    for npc in npcs:
        npc_ref = f"npc:{npc.npc_id}"
        evidence = shared_by_ref[npc_ref]
        recent = sum(
            logical_time - settled_at_by_ref[ref] <= _RECENT_WINDOW
            for ref in evidence.source_event_refs
            if ref in settled_at_by_ref
        )
        shared_count = evidence.settled_shared_count
        older = max(shared_count - recent, 0)
        closeness = (
            RESTING_CLOSENESS_BP
            + recent * _RECENT_SHARED_BP
            + older * _OLDER_SHARED_BP
        )
        readings.append(NpcRelationshipReading(
            npc_ref=npc_ref,
            closeness_bp=max(0, min(10_000, closeness)),
            familiarity_bp=max(0, min(10_000, shared_count * _FAMILIARITY_PER_SHARED_BP)),
            settled_shared_count=shared_count,
            last_shared_at=evidence.last_shared_at,
            source_event_refs=evidence.source_event_refs,
        ))
    readings.sort(key=lambda item: item.npc_ref)
    return tuple(readings)


def npc_relationship_by_ref(
    readings: tuple[NpcRelationshipReading, ...],
) -> dict[str, NpcRelationshipReading]:
    return {item.npc_ref: item for item in readings}


def _shared_history_fact(reading: NpcRelationshipReading) -> str:
    """Describe only settled evidence; the actor owns its meaning."""

    if reading.last_shared_at is None:
        return f"已结算共同经历 {reading.settled_shared_count} 次"
    return (
        f"已结算共同经历 {reading.settled_shared_count} 次；"
        f"最近一次发生于 {reading.last_shared_at.isoformat()}"
    )


def npc_relationship_advisories(
    projection,
    *,
    protagonist_actor_ref: str,
) -> tuple:
    """Expose the reading through the ordinary non-authoritative advisory envelope.

    Only NPCs with any committed shared history (or live friction) appear:
    asserting "关系一般" about someone she never interacted with would be
    manufactured texture, not a sourced reading.
    """

    from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return ()
    readings = tuple(
        item
        for item in npc_relationship_readings(
            projection,
            protagonist_actor_ref=protagonist_actor_ref,
        )
        if item.source_event_refs
    )[:3]
    if not readings:
        return ()
    npc_names = {
        f"npc:{npc.npc_id}": (npc.stable_identity_ref or npc.npc_id)
        for npc in getattr(projection, "npcs", ())
    }
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref="npc-relationship:" + _digest(reading.npc_ref),
            value=(
                f"和{npc_names.get(reading.npc_ref, reading.npc_ref)}："
                + _shared_history_fact(reading)
            )[:256],
            weight_bp=10_000,
            confidence_bp=10_000,
        )
        for reading in readings
    )
    source_refs = tuple(
        dict.fromkeys(ref for reading in readings for ref in reading.source_event_refs)
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:npc-relationships:" + _digest(source_refs),
            kind="npc_relationships",
            source_refs=source_refs,
            candidate_refs=tuple(item.candidate_ref for item in candidates),
            candidates=candidates,
            # Below the continuity floors' rank: under extreme budget
            # pressure this texture yields before the sole relationship /
            # appraisal / affect head.
            confidence_bp=6_000,
            expiry=logical_time + timedelta(days=1),
            producer_version=NPC_RELATIONSHIP_VIEW_VERSION,
        ),
    )


__all__ = [
    "NPC_RELATIONSHIP_VIEW_VERSION",
    "RESTING_CLOSENESS_BP",
    "NpcRelationshipReading",
    "npc_relationship_advisories",
    "npc_relationship_by_ref",
    "npc_relationship_readings",
]
