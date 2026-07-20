"""Per-NPC relationship reading: a pure projection over lived shared history.

``relationship_states`` describes the slow variables of the user
relationship; NPCs had nothing — the NPC-initiative weight policy noted
"phase one has no per-NPC relationship state yet" and used accepted Affect as
a stand-in.  This module closes that gap as a *derived Projection* in the
CONTEXT.md sense: a deterministic, rebuildable view over already-committed
World Events, never a second write authority.

Per registered NPC it reads:

* ``familiarity_bp`` — how much settled shared history exists at all;
* ``closeness_bp``   — a resting 3_000bp baseline warmed by recent settled
  shared occurrences and cooled by unresolved friction;
* ``friction_bp``    — active, unexpired ``npc_conflict`` appraisal weight.

All arithmetic is integer and every input is committed authority (settled
occurrence events, accepted appraisals), so any weight policy consuming the
reading stays exactly replayable through its recorded draw, and the advisory
stays ledger-backed.
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
_FRICTION_COOLING_NUMERATOR = 4_000  # closeness -= friction * this // 10_000


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class NpcRelationshipReading(FrozenModel):
    """One NPC's derived relationship variables, ledger-backed via sources."""

    npc_ref: str
    closeness_bp: int
    familiarity_bp: int
    friction_bp: int
    settled_shared_count: int
    last_shared_at: datetime | None = None
    source_event_refs: tuple[str, ...] = ()


def npc_relationship_readings(projection) -> tuple[NpcRelationshipReading, ...]:
    """Derive every registered NPC's reading from committed shared history."""

    logical_time = getattr(projection, "logical_time", None)
    npcs = tuple(getattr(projection, "npcs", ()))
    if logical_time is None or not npcs:
        return ()
    occurrences = tuple(getattr(projection, "world_occurrences", ()))
    appraisals = tuple(getattr(projection, "appraisals", ()))
    readings: list[NpcRelationshipReading] = []
    for npc in npcs:
        npc_ref = f"npc:{npc.npc_id}"
        recent = older = 0
        last_shared: datetime | None = None
        sources: list[str] = []
        for occurrence in occurrences:
            if (
                occurrence.status != "settled"
                or occurrence.settled_at is None
                or occurrence.settlement_event_ref is None
                or npc_ref not in occurrence.participant_refs
            ):
                continue
            if logical_time - occurrence.settled_at <= _RECENT_WINDOW:
                recent += 1
            else:
                older += 1
            sources.append(occurrence.settlement_event_ref)
            if last_shared is None or occurrence.settled_at > last_shared:
                last_shared = occurrence.settled_at
        friction = 0
        for appraisal in appraisals:
            if appraisal.status != "active" or appraisal.expires_at <= logical_time:
                continue
            conflict_weight = sum(
                item.weight_bp
                for item in appraisal.hypotheses
                if item.meaning == "npc_conflict" and item.attribution == "npc"
            )
            if not conflict_weight:
                continue
            friction = max(
                friction,
                conflict_weight * appraisal.confidence_bp // 10_000,
            )
            sources.append(appraisal.origin.accepted_event_ref)
        shared_count = recent + older
        closeness = (
            RESTING_CLOSENESS_BP
            + recent * _RECENT_SHARED_BP
            + older * _OLDER_SHARED_BP
            - friction * _FRICTION_COOLING_NUMERATOR // 10_000
        )
        readings.append(NpcRelationshipReading(
            npc_ref=npc_ref,
            closeness_bp=max(0, min(10_000, closeness)),
            familiarity_bp=max(0, min(10_000, shared_count * _FAMILIARITY_PER_SHARED_BP)),
            friction_bp=max(0, min(10_000, friction)),
            settled_shared_count=shared_count,
            last_shared_at=last_shared,
            source_event_refs=tuple(dict.fromkeys(sources)),
        ))
    readings.sort(key=lambda item: item.npc_ref)
    return tuple(readings)


def npc_relationship_by_ref(
    readings: tuple[NpcRelationshipReading, ...],
) -> dict[str, NpcRelationshipReading]:
    return {item.npc_ref: item for item in readings}


def _closeness_prose(reading: NpcRelationshipReading) -> str:
    if reading.settled_shared_count == 0:
        base = "认识，但还没真正一起做过什么"
    elif reading.closeness_bp >= 5_000:
        base = f"最近走得挺近（一起经历过 {reading.settled_shared_count} 件小事）"
    elif reading.closeness_bp >= 3_500:
        base = f"关系在慢慢熟起来（一起经历过 {reading.settled_shared_count} 件小事）"
    else:
        base = f"有点疏远（虽然一起经历过 {reading.settled_shared_count} 件小事）"
    if reading.friction_bp >= 2_000:
        base += "，之间还搁着一点没说开的别扭"
    return base


def npc_relationship_advisories(projection) -> tuple:
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
        for item in npc_relationship_readings(projection)
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
                + _closeness_prose(reading)
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
