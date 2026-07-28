"""Reconcile settled long-lived consequences with current biography.

The runtime never infers a career, residence, or relationship chapter from
text.  It consumes only a reviewed outcome effect that the ordinary Life
Author and aftermath model already selected and settled.  Calendar expiry is
mechanical; starting an arc is therefore still downstream of a character
decision, while ending its declared time window is a hard temporal boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

from .biographical_lifecycle import LifeArcChangedPayload
from .event_identity import domain_idempotency_key
from .life_author_seed import ReviewedLifeSeedCatalog
from .life_events import (
    NpcRegisteredPayload,
    NpcStatusChangedPayload,
)
from .life_content_store import ImmutableLifeContentStore
from .schema_core import FrozenModel
from .schemas import (
    CommittedWorldEventRef,
    DynamicLifeArcContextDescriptor,
    EvidenceRef,
    FrozenLifeArcEffectDescriptor,
    LifeArcProjection,
    NpcProjection,
    PendingBiographicalSettlementProjection,
    ProjectionCursor,
    WorldEvent,
    open_life_arc_id,
    open_life_npc_id,
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class BiographicalLifecycleResult(FrozenModel):
    status: Literal["transitioned", "idle", "rejected"]
    reason_code: str
    life_arc_ids: tuple[str, ...] = ()
    npc_ids: tuple[str, ...] = ()


class BiographicalLifecycleRuntime:
    """Apply reviewed long-lived effects and synchronize contextual NPCs."""

    def __init__(
        self,
        *,
        ledger,
        catalog: ReviewedLifeSeedCatalog,
        owner_actor_ref: str,
        content_store: ImmutableLifeContentStore | None = None,
        actor: str = "worker:world-v2:biographical-lifecycle",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("biographical lifecycle requires owner and actor")
        self._ledger = ledger
        self._catalog = catalog
        self._owner_actor_ref = owner_actor_ref
        self._content_store = content_store
        self._actor = actor

    def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> BiographicalLifecycleResult:
        projection = self._ledger.project()
        located_wake = self._ledger.lookup_event_commit(wake_event_ref)
        wake_event = located_wake[0] if located_wake is not None else None
        wake_commit = located_wake[1] if located_wake is not None else None
        clock_history = getattr(projection, "clock_transition_history", ())
        exact_wake_revision = (
            clock_history[-1].computed_world_revision
            if wake_event is not None
            and wake_event.event_type == "ClockAdvanced"
            and clock_history
            and clock_history[-1].clock_event_ref == wake_event_ref
            else next(
                (
                    item.settlement_world_revision
                    for item in projection.world_occurrences
                    if wake_event is not None
                    and wake_event.event_type == "WorldOccurrenceSettled"
                    and item.settlement_event_ref == wake_event_ref
                    and item.settlement_world_revision is not None
                ),
                getattr(wake_commit, "world_revision", None),
            )
        )
        wake = (
            CommittedWorldEventRef(
                event_id=wake_event.event_id,
                event_type=wake_event.event_type,
                world_revision=exact_wake_revision,
                payload_hash=wake_event.payload_hash,
                logical_time=wake_event.logical_time,
            )
            if wake_event is not None
            and wake_commit is not None
            and isinstance(exact_wake_revision, int)
            and wake_event.event_type
            in {
                "ClockAdvanced",
                "WorldOccurrenceSettled",
                "ActivityCompleted",
                "ActivityAbandoned",
            }
            else None
        )
        if wake is None or projection.logical_time is None:
            return BiographicalLifecycleResult(
                status="rejected",
                reason_code="biographical_lifecycle.wake_not_committed",
            )
        logical_time = projection.logical_time

        due = tuple(
            sorted(
                (
                    item
                    for item in projection.life_arcs
                    if item.status == "active"
                    and item.ends_at is not None
                    and item.ends_at <= logical_time
                ),
                key=lambda item: (item.ends_at, item.arc_id),
            )
        )
        pending = self._pending_effect_settlements(projection)
        if pending and any(
            not self._pending_content_is_available(item)
            for _, item in pending
        ):
            return BiographicalLifecycleResult(
                status="rejected",
                reason_code="biographical_lifecycle.effect_content_unavailable",
            )
        if not due and not pending:
            npc_events, npc_ids = self._npc_sync_events(
                projection=projection,
                life_arcs=projection.life_arcs,
                source=wake,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            if not npc_events:
                return BiographicalLifecycleResult(
                    status="idle",
                    reason_code="biographical_lifecycle.no_transition",
                )
            self._commit(npc_events)
            return BiographicalLifecycleResult(
                status="transitioned",
                reason_code="biographical_lifecycle.contextual_npcs_synchronized",
                npc_ids=npc_ids,
            )

        events: list[WorldEvent] = []
        virtual_arcs = list(projection.life_arcs)
        transitioned_arc_ids: list[str] = []
        for arc in due:
            closed, close_event = self._closed_arc_and_event(
                arc=arc,
                wake=wake,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            events.append(close_event)
            virtual_arcs = [
                closed if item.arc_id == arc.arc_id else item for item in virtual_arcs
            ]
            transitioned_arc_ids.append(arc.arc_id)

        introduced_npc_ids: list[str] = []
        for settlement_ref, settlement in pending:
            introduced_events, introduced_ids = self._dynamic_npc_events(
                projection=projection,
                settlement_ref=settlement_ref,
                pending=settlement,
                logical_time=logical_time,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            events.extend(introduced_events)
            introduced_npc_ids.extend(introduced_ids)
            effect = (
                settlement.life_arc_effect
                or settlement.dynamic_life_arc_context
            )
            if effect is not None:
                arc = self._arc_from_effect(
                    settlement_ref=settlement_ref,
                    effect=effect,
                    effective_at=settlement.settled_at,
                )
                events.append(
                    self._start_arc_event(
                        arc=arc,
                        settlement_ref=settlement_ref,
                        logical_time=logical_time,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                )
                virtual_arcs.append(arc)
                transitioned_arc_ids.append(arc.arc_id)
                if arc.ends_at is not None and arc.ends_at <= logical_time:
                    closed, close_event = self._closed_arc_and_event(
                        arc=arc,
                        wake=wake,
                        logical_time=logical_time,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                    events.append(close_event)
                    virtual_arcs[-1] = closed

        npc_events, npc_ids = self._npc_sync_events(
            projection=projection,
            life_arcs=tuple(virtual_arcs),
            source=wake,
            logical_time=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        self._commit((*events, *npc_events))
        if pending and due:
            reason_code = "biographical_lifecycle.transitions_applied"
        elif pending:
            reason_code = "biographical_lifecycle.arc_started"
        else:
            reason_code = "biographical_lifecycle.arc_completed"
        return BiographicalLifecycleResult(
            status="transitioned",
            reason_code=reason_code,
            life_arc_ids=tuple(transitioned_arc_ids),
            npc_ids=tuple((*introduced_npc_ids, *npc_ids)),
        )

    def _pending_effect_settlements(
        self, projection
    ) -> tuple[
        tuple[
            CommittedWorldEventRef,
            PendingBiographicalSettlementProjection,
        ],
        ...,
    ]:
        pending = [
            (
                CommittedWorldEventRef(
                    event_id=item.settlement_event_ref,
                    event_type="WorldOccurrenceSettled",
                    world_revision=item.settlement_world_revision,
                    payload_hash=item.settlement_payload_hash,
                    logical_time=item.settled_at,
                ),
                item,
            )
            for item in projection.pending_biographical_settlements
        ]
        return tuple(
            sorted(
                pending,
                key=lambda item: (
                    item[1].settled_at,
                    item[0].world_revision,
                    item[0].event_id,
                ),
            )
        )

    def _arc_from_effect(
        self,
        *,
        settlement_ref,
        effect: FrozenLifeArcEffectDescriptor | DynamicLifeArcContextDescriptor,
        effective_at: datetime,
    ) -> LifeArcProjection:
        dynamic = isinstance(effect, DynamicLifeArcContextDescriptor)
        arc_id = (
            open_life_arc_id(
                world_id=self._ledger.world_id,
                settlement_event_ref=settlement_ref.event_id,
                descriptor_hash=effect.descriptor_hash,
            )
            if dynamic
            else "life-arc:"
            + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "settlement_event_ref": settlement_ref.event_id,
                    "context_pack_ref": effect.context_pack_ref,
                }
            )
        )
        return LifeArcProjection(
            arc_id=arc_id,
            entity_revision=1,
            owner_actor_ref=self._owner_actor_ref,
            arc_kind="dynamic" if dynamic else effect.arc_kind,
            context_pack_ref=(
                effect.summary_content_ref if dynamic else effect.context_pack_ref
            ),
            context_tags=(
                effect.narrative_tags if dynamic else effect.context_tags
            ),
            effect_descriptor_hash=effect.descriptor_hash,
            status="active",
            started_at=effective_at,
            ends_at=(
                effective_at + timedelta(days=effect.duration_days)
                if effect.duration_days is not None
                else None
            ),
            source_event_ref=settlement_ref.event_id,
            privacy_class=effect.privacy_class,
        )

    def _start_arc_event(
        self,
        *,
        arc: LifeArcProjection,
        settlement_ref,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        evidence = self._evidence(settlement_ref, evidence_type="settled_world_event")
        payload = LifeArcChangedPayload(
            change_id=f"change:{arc.arc_id}:start",
            transition_id=f"transition:{arc.arc_id}:1",
            expected_entity_revision=0,
            evidence_refs=(evidence,),
            policy_refs=("policy:biographical-lifecycle.1",),
            operation="start",
            arc_before=None,
            arc_after=arc,
        )
        return self._event(
            event_id=f"event:{arc.arc_id}:start",
            event_type="LifeArcChanged",
            payload=payload.model_dump(mode="json"),
            logical_time=logical_time,
            causation_id=settlement_ref.event_id,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    def _closed_arc_and_event(
        self,
        *,
        arc: LifeArcProjection,
        wake,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> tuple[LifeArcProjection, WorldEvent]:
        source = next(
            item
            for item in self._ledger.project().committed_world_event_refs
            if item.event_id == arc.source_event_ref
        )
        after = arc.model_copy(
            update={
                "entity_revision": arc.entity_revision + 1,
                "status": "completed",
                "closed_at": arc.ends_at or logical_time,
            }
        )
        payload = LifeArcChangedPayload(
            change_id=f"change:{arc.arc_id}:complete",
            transition_id=f"transition:{arc.arc_id}:{after.entity_revision}",
            expected_entity_revision=arc.entity_revision,
            evidence_refs=(
                self._evidence(
                    source,
                    evidence_type=(
                        "settled_world_event"
                        if source.event_type == "WorldOccurrenceSettled"
                        else "committed_world_event"
                    ),
                ),
                self._evidence(wake, evidence_type="committed_world_event"),
            ),
            policy_refs=("policy:biographical-lifecycle.1",),
            operation="complete",
            arc_before=arc,
            arc_after=after,
        )
        return (
            after,
            self._event(
                event_id=f"event:{arc.arc_id}:complete:{after.entity_revision}",
                event_type="LifeArcChanged",
                payload=payload.model_dump(mode="json"),
                logical_time=logical_time,
                causation_id=wake.event_id,
                trace_id=trace_id,
                correlation_id=correlation_id,
            ),
        )

    def _pending_content_is_available(
        self,
        pending: PendingBiographicalSettlementProjection,
    ) -> bool:
        descriptors = (
            *pending.provisional_npc_introductions,
            *(
                (pending.dynamic_life_arc_context,)
                if pending.dynamic_life_arc_context is not None
                else ()
            ),
        )
        if not descriptors:
            return True
        if self._content_store is None:
            return False
        for descriptor in descriptors:
            stored = self._content_store.read_exact(
                content_ref=descriptor.summary_content_ref
            )
            expected_kind = (
                "dynamic_life_arc_context"
                if isinstance(descriptor, DynamicLifeArcContextDescriptor)
                else "provisional_npc_introduction"
            )
            if (
                stored is None
                or stored.content_kind != expected_kind
                or stored.content_payload_hash != descriptor.summary_payload_hash
            ):
                return False
        return True

    def _dynamic_npc_events(
        self,
        *,
        projection,
        settlement_ref: CommittedWorldEventRef,
        pending: PendingBiographicalSettlementProjection,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> tuple[tuple[WorldEvent, ...], tuple[str, ...]]:
        existing = {item.npc_id for item in projection.npcs}
        evidence = self._evidence(
            settlement_ref,
            evidence_type="settled_world_event",
        )
        events: list[WorldEvent] = []
        npc_ids: list[str] = []
        for descriptor in pending.provisional_npc_introductions:
            npc_id = open_life_npc_id(
                world_id=self._ledger.world_id,
                settlement_event_ref=settlement_ref.event_id,
                provisional_entity_ref=descriptor.provisional_entity_ref,
                descriptor_hash=descriptor.descriptor_hash,
            )
            if npc_id in existing:
                continue
            event_id = f"event:{npc_id}:register"
            payload = NpcRegisteredPayload(
                change_id=f"change:{npc_id}:register",
                transition_id=f"transition:{npc_id}:1",
                expected_entity_revision=0,
                evidence_refs=(evidence,),
                policy_refs=("policy:biographical-lifecycle.1",),
                npc=NpcProjection(
                    npc_id=npc_id,
                    entity_revision=1,
                    stable_identity_ref=descriptor.summary_content_ref,
                    known_trait_refs=(),
                    privacy_class=descriptor.privacy_class,
                    current_location_ref=None,
                    status="active",
                    source_event_ref=settlement_ref.event_id,
                    effect_descriptor_hash=descriptor.descriptor_hash,
                    accepted_event_ref=event_id,
                ),
            )
            events.append(
                self._event(
                    event_id=event_id,
                    event_type="NpcRegistered",
                    payload=payload.model_dump(mode="json"),
                    logical_time=logical_time,
                    causation_id=settlement_ref.event_id,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            )
            npc_ids.append(npc_id)
        return tuple(events), tuple(npc_ids)

    def _npc_sync_events(
        self,
        *,
        projection,
        life_arcs: tuple[object, ...],
        source,
        logical_time: datetime,
        trace_id: str,
        correlation_id: str,
    ) -> tuple[tuple[WorldEvent, ...], tuple[str, ...]]:
        context = self._catalog.biographical_context_at(
            instant=logical_time,
            life_arcs=life_arcs,
        )
        desired = {
            item.npc_id: item for item in self._catalog.contextual_npcs(context)
        }
        contextual = {
            item.npc_id: item
            for item in self._catalog.reviewed_npcs
            if item.requires_all_context_tags
        }
        current = {item.npc_id: item for item in projection.npcs}
        locations = {item.id: item for item in self._catalog.reviewed_locations}
        evidence_type = (
            "settled_world_event"
            if source.event_type == "WorldOccurrenceSettled"
            else "committed_world_event"
        )
        evidence = self._evidence(source, evidence_type=evidence_type)
        events: list[WorldEvent] = []
        changed: list[str] = []
        for npc_id, reviewed in contextual.items():
            existing = current.get(npc_id)
            should_be_active = npc_id in desired
            if existing is None and should_be_active:
                location_ref = (
                    locations[reviewed.location_id].location_ref
                    if reviewed.location_id is not None
                    else None
                )
                payload = NpcRegisteredPayload(
                    change_id=f"change:contextual-npc:{npc_id}:register",
                    transition_id=f"transition:contextual-npc:{npc_id}:1",
                    expected_entity_revision=0,
                    evidence_refs=(evidence,),
                    policy_refs=("policy:biographical-lifecycle.1",),
                    npc=NpcProjection(
                        npc_id=npc_id,
                        entity_revision=1,
                        stable_identity_ref=reviewed.stable_identity_ref,
                        known_trait_refs=reviewed.known_trait_refs,
                        privacy_class=reviewed.privacy,
                        current_location_ref=location_ref,
                        status="active",
                    ),
                )
                events.append(
                    self._event(
                        event_id=f"event:contextual-npc:{npc_id}:register",
                        event_type="NpcRegistered",
                        payload=payload.model_dump(mode="json"),
                        logical_time=logical_time,
                        causation_id=source.event_id,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                )
                changed.append(npc_id)
            elif existing is not None and (
                (should_be_active and existing.status == "retired")
                or (not should_be_active and existing.status == "active")
            ):
                after = existing.model_copy(
                    update={
                        "entity_revision": existing.entity_revision + 1,
                        "status": "active" if should_be_active else "retired",
                    }
                )
                payload = NpcStatusChangedPayload(
                    change_id=f"change:contextual-npc:{npc_id}:{after.status}",
                    transition_id=(
                        f"transition:contextual-npc:{npc_id}:{after.entity_revision}"
                    ),
                    expected_entity_revision=existing.entity_revision,
                    evidence_refs=(evidence,),
                    policy_refs=("policy:biographical-lifecycle.1",),
                    npc_before=existing,
                    npc_after=after,
                )
                events.append(
                    self._event(
                        event_id=(
                            f"event:contextual-npc:{npc_id}:"
                            f"{after.status}:{after.entity_revision}"
                        ),
                        event_type="NpcStatusChanged",
                        payload=payload.model_dump(mode="json"),
                        logical_time=logical_time,
                        causation_id=source.event_id,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                )
                changed.append(npc_id)
        return tuple(events), tuple(changed)

    @staticmethod
    def _evidence(ref, *, evidence_type: str) -> EvidenceRef:
        return EvidenceRef(
            ref_id=ref.event_id,
            evidence_type=evidence_type,
            claim_purpose="life_transition",
            source_world_revision=ref.world_revision,
            immutable_hash=ref.payload_hash,
        )

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        payload: dict[str, object],
        logical_time: datetime,
        causation_id: str,
        trace_id: str,
        correlation_id: str,
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=logical_time,
            actor=self._actor,
            source="world-v2:biographical-lifecycle",
            trace_id=trace_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type=event_type,
                    world_id=self._ledger.world_id,
                    payload=payload,
                )
                or f"biographical-lifecycle:{_digest([event_type, event_id])}"
            ),
            payload=payload,
        )

    def _commit(self, events: tuple[WorldEvent, ...]) -> None:
        if not events:
            return
        projection = self._ledger.project()
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        self._ledger.commit_at_cursor(
            events,
            expected_cursor=cursor,
            commit_id="commit:biographical-lifecycle:" + _digest(
                [item.event_id for item in events]
            ),
        )


__all__ = [
    "BiographicalLifecycleResult",
    "BiographicalLifecycleRuntime",
]
