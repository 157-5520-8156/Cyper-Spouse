from __future__ import annotations

from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.aspiration_events import (
    AspirationPlantedPayload,
    AspirationRevisedPayload,
)
from companion_daemon.world_v2.character_interior import InteriorOpportunity
from companion_daemon.world_v2.character_interior.production import (
    _LedgerCapsuleInteriorProjection,
)
from companion_daemon.world_v2.schemas import (
    AspirationProjection,
    CommitResult,
    CommittedWorldEventRef,
    EvidenceRef,
    LedgerProjection,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
WORLD_ID = "world:interior-aspiration"
ACTOR_REF = "character:zhizhi"
EVENT_REF = "event:aspiration:planted:interior"


def _event_and_projection() -> tuple[WorldEvent, LedgerProjection]:
    aspiration = AspirationProjection(
        aspiration_id="aspiration:learn-pottery",
        entity_revision=1,
        owner_actor_ref=ACTOR_REF,
        seed_id="seed:pottery",
        text="有空想认真学一次拉坯。",
        privacy_class="private",
        planted_at=NOW,
        planted_event_ref=EVENT_REF,
        source_event_ref="event:experience:pottery-shop",
    )
    payload = AspirationPlantedPayload(
        change_id="change:aspiration:pottery",
        transition_id="transition:aspiration:pottery",
        expected_entity_revision=0,
        evidence_refs=(
            EvidenceRef(
                ref_id=aspiration.source_event_ref,
                evidence_type="committed_world_event",
                claim_purpose="life_transition",
                source_world_revision=1,
                immutable_hash="b" * 64,
            ),
        ),
        policy_refs=("policy:aspiration.1",),
        aspiration=aspiration,
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=EVENT_REF,
        world_id=WORLD_ID,
        event_type="AspirationPlanted",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:test",
        source="test",
        trace_id="trace:aspiration:interior",
        causation_id=aspiration.source_event_ref,
        correlation_id="correlation:aspiration:interior",
        idempotency_key="idempotency:aspiration:interior",
        payload=payload.model_dump(mode="json"),
    )
    projection = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=2,
        deliberation_revision=0,
        ledger_sequence=1,
        logical_time=NOW,
        aspirations=(aspiration,),
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=EVENT_REF,
                event_type=event.event_type,
                world_revision=2,
                payload_hash=event.payload_hash,
                logical_time=NOW,
            ),
        ),
        semantic_hash="a" * 64,
    )
    return event, projection


class _Ledger:
    world_id = WORLD_ID
    blocks_event_loop = False

    def __init__(self, event: WorldEvent, projection: LedgerProjection) -> None:
        self._event = event
        self._projection = projection

    def project_at(self, _cursor: ProjectionCursor) -> LedgerProjection:
        return self._projection

    def lookup_event_commit(self, event_ref: str):  # type: ignore[no-untyped-def]
        if event_ref != EVENT_REF:
            return None
        return self._event, CommitResult(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
            event_ids=(EVENT_REF,),
        )


class _Capsules:
    def __init__(
        self,
        *,
        logical_time: datetime = NOW,
        world_revision: int = 2,
        ledger_sequence: int = 1,
    ) -> None:
        self.logical_time = logical_time
        self.world_revision = world_revision
        self.ledger_sequence = ledger_sequence

    def compile(self, _query):  # type: ignore[no-untyped-def]
        return SimpleNamespace(
            model_content_json=json.dumps(
                {
                    "world_id": WORLD_ID,
                    "actor_ref": ACTOR_REF,
                    "world_revision": self.world_revision,
                    "deliberation_revision": 0,
                    "ledger_sequence": self.ledger_sequence,
                    "logical_time": self.logical_time.isoformat(),
                    "consumer_scope": "deliberation_internal",
                    "viewer_privacy_ceiling": "private",
                    "context_compiler_version": "context-capsule-compiler:test",
                    "truncation": {},
                    "slices": {},
                },
                sort_keys=True,
            )
        )


@pytest.mark.asyncio
async def test_canonical_interior_snapshot_includes_source_closed_active_aspiration() -> None:
    event, projection = _event_and_projection()
    compiler = _LedgerCapsuleInteriorProjection(
        ledger=_Ledger(event, projection),  # type: ignore[arg-type]
        capsules=_Capsules(),  # type: ignore[arg-type]
        companion_actor_ref=ACTOR_REF,
    )
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:aspiration:interior",
        inner_turn_ref="inner-turn:aspiration:interior",
        world_id=WORLD_ID,
        actor_ref=ACTOR_REF,
        trigger_ref=EVENT_REF,
        cursor=ProjectionCursor(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
        ),
        logical_time=NOW,
        purpose="world_stimulus_appraisal",
        source_refs=(EVENT_REF,),
    )

    snapshot = await compiler.project(subject=opportunity)

    assert snapshot.materials["aspirations"] == [
        {
            "aspiration_id": "aspiration:learn-pottery",
            "entity_revision": 1,
            "origin_kind": "reviewed_seed",
            "planted_at": NOW.isoformat(),
            "planted_event_ref": EVENT_REF,
            "privacy_class": "private",
            "reinforcement_count": 0,
            "source_event_ref": "event:experience:pottery-shop",
            "source_ref": EVENT_REF,
            "status": "active",
            "text": "有空想认真学一次拉坯。",
        }
    ]
    assert EVENT_REF in snapshot.facets["aspirations_conflicts"].source_refs
    assert "aspirations" in snapshot.facets["aspirations_conflicts"].content[
        "material_keys"
    ]
    assert "aspirations" in snapshot.facets["autonomous_impulses"].content[
        "material_keys"
    ]
    assert any(
        item.scope == "aspirations" and item.source_ref == EVENT_REF
        for item in snapshot.source_inventory
    )


@pytest.mark.asyncio
async def test_canonical_snapshot_rejects_projection_meaning_not_proved_by_aspiration_event() -> None:
    event, projection = _event_and_projection()
    changed = projection.aspirations[0].model_copy(
        update={"privacy_class": "shareable"}
    )
    projection = projection.model_copy(update={"aspirations": (changed,)})
    compiler = _LedgerCapsuleInteriorProjection(
        ledger=_Ledger(event, projection),  # type: ignore[arg-type]
        capsules=_Capsules(),  # type: ignore[arg-type]
        companion_actor_ref=ACTOR_REF,
    )
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:aspiration:tampered",
        inner_turn_ref="inner-turn:aspiration:tampered",
        world_id=WORLD_ID,
        actor_ref=ACTOR_REF,
        trigger_ref=EVENT_REF,
        cursor=ProjectionCursor(
            world_revision=2,
            deliberation_revision=0,
            ledger_sequence=1,
        ),
        logical_time=NOW,
        purpose="world_stimulus_appraisal",
        source_refs=(EVENT_REF,),
    )

    with pytest.raises(ValueError, match="changed planted meaning"):
        await compiler.project(subject=opportunity)


@pytest.mark.asyncio
async def test_canonical_snapshot_uses_the_latest_source_closed_aspiration_revision() -> None:
    planted_event, planted_projection = _event_and_projection()
    before = planted_projection.aspirations[0]
    revised_at = NOW.replace(hour=16)
    revised_ref = "event:aspiration:revised:interior"
    after = before.model_copy(
        update={
            "entity_revision": 2,
            "text": "想学拉坯，但只先约一次体验，不把兴趣变成任务。",
            "tension_summary": "兴趣很真，精力有限也是真的。",
            "tension_source_refs": (EVENT_REF,),
            "last_revised_at": revised_at,
            "revision_event_ref": revised_ref,
        }
    )
    evidence = EvidenceRef(
        ref_id=EVENT_REF,
        evidence_type="committed_world_event",
        claim_purpose="private_hypothesis",
        source_world_revision=2,
        immutable_hash=planted_event.payload_hash,
    )
    payload = AspirationRevisedPayload(
        change_id="change:aspiration:revised:interior",
        transition_id="transition:aspiration:revised:interior",
        expected_entity_revision=1,
        evidence_refs=(evidence,),
        policy_refs=("policy:aspiration.1",),
        aspiration_before=before,
        aspiration_after=after,
    )
    revised_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=revised_ref,
        world_id=WORLD_ID,
        event_type="AspirationRevised",
        logical_time=revised_at,
        created_at=revised_at,
        actor="worker:test",
        source="test",
        trace_id="trace:aspiration:revised:interior",
        causation_id=EVENT_REF,
        correlation_id="correlation:aspiration:interior",
        idempotency_key="idempotency:aspiration:revised:interior",
        payload=payload.model_dump(mode="json"),
    )
    projection = planted_projection.model_copy(
        update={
            "world_revision": 3,
            "ledger_sequence": 2,
            "logical_time": revised_at,
            "aspirations": (after,),
            "committed_world_event_refs": (
                *planted_projection.committed_world_event_refs,
                CommittedWorldEventRef(
                    event_id=revised_ref,
                    event_type="AspirationRevised",
                    world_revision=3,
                    payload_hash=revised_event.payload_hash,
                    logical_time=revised_at,
                ),
            ),
        }
    )

    class _RevisionLedger(_Ledger):
        def lookup_event_commit(self, event_ref: str):  # type: ignore[no-untyped-def]
            if event_ref == EVENT_REF:
                return planted_event, CommitResult(
                    world_revision=2,
                    deliberation_revision=0,
                    ledger_sequence=1,
                    event_ids=(EVENT_REF,),
                )
            if event_ref == revised_ref:
                return revised_event, CommitResult(
                    world_revision=3,
                    deliberation_revision=0,
                    ledger_sequence=2,
                    event_ids=(revised_ref,),
                )
            return None

    compiler = _LedgerCapsuleInteriorProjection(
        ledger=_RevisionLedger(planted_event, projection),  # type: ignore[arg-type]
        capsules=_Capsules(  # type: ignore[arg-type]
            logical_time=revised_at,
            world_revision=3,
            ledger_sequence=2,
        ),
        companion_actor_ref=ACTOR_REF,
    )
    opportunity = InteriorOpportunity(
        opportunity_ref="opportunity:aspiration:revised:interior",
        inner_turn_ref="inner-turn:aspiration:revised:interior",
        world_id=WORLD_ID,
        actor_ref=ACTOR_REF,
        trigger_ref=revised_ref,
        cursor=ProjectionCursor(
            world_revision=3,
            deliberation_revision=0,
            ledger_sequence=2,
        ),
        logical_time=revised_at,
        purpose="world_stimulus_appraisal",
        source_refs=(revised_ref,),
    )

    snapshot = await compiler.project(subject=opportunity)

    item = snapshot.materials["aspirations"][0]
    assert item["source_ref"] == revised_ref
    assert item["planted_event_ref"] == EVENT_REF
    assert item["text"] == "想学拉坯，但只先约一次体验，不把兴趣变成任务。"
    assert item["tension_summary"] == "兴趣很真，精力有限也是真的。"
    assert revised_ref in snapshot.facets["aspirations_conflicts"].source_refs
