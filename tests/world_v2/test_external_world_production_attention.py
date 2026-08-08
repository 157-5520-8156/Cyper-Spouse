from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.external_world_perception.contracts import (
    CharacterAttentionContext,
    LiveCharacterAttentionContext,
    PerceptionChannelProof,
)
from companion_daemon.world_v2.external_world_perception.production_attention import (
    CapsuleBackedLiveAttentionContextPort,
    CapsuleBackedShadowAttentionContextPort,
    LedgerPublicInformationChannelPort,
    StaticLiveAttentionChannelPort,
    public_information_capability_id,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
REGISTRY_HASH = "sha256:" + "a" * 64


def _world() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id="world:attention-production")
    ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:world-started:attention-production",
                world_id=ledger.world_id,
                event_type="WorldStarted",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:attention-production",
                causation_id="cause:attention-production",
                correlation_id="correlation:attention-production",
                idempotency_key="identity:attention-production",
                payload={},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


class _ForbiddenUserSlicesCapsule:
    capsule_id = "c" * 64

    def __init__(self) -> None:
        source = SimpleNamespace(ref="event:self:1")
        value = {"values": {"slow_evolving": {"activity": "在窗边整理东西"}}}
        item = SimpleNamespace(
            item_ref="self:1",
            payload_json=json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            source_bindings=(source,),
        )
        self.character_core = SimpleNamespace(items=(item,))
        self.affect_episodes = SimpleNamespace(items=())
        self.current_situation = SimpleNamespace(items=())
        self.world_life = SimpleNamespace(items=())
        self.relationship_slice = SimpleNamespace(items=())
        self.appraisals = SimpleNamespace(items=())
        self.open_threads = SimpleNamespace(items=())
        self.recent_experiences = SimpleNamespace(items=())
        slices = {
            name: {"availability": "unavailable"}
            for name in (
                "current_situation",
                "relationship_slice",
                "appraisals",
                "affect_episodes",
                "open_threads",
                "recent_experiences",
                "world_life",
            )
        }
        slices["character_core"] = {
            "availability": "available",
            "items": [{"item_ref": "self:1", "value": value}],
        }
        self.model_content_json = json.dumps(
            {
                "world_id": "world:attention-production",
                "actor_ref": "character:zhizhi",
                "trigger_ref": "external-attention-context:test",
                "world_revision": 1,
                "deliberation_revision": 0,
                "ledger_sequence": 1,
                "logical_time": NOW.isoformat(),
                "consumer_scope": "deliberation_internal",
                "slices": slices,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def recent_dialogue(self) -> object:
        raise AssertionError("external attention must not read user dialogue")

    @property
    def relevant_facts(self) -> object:
        raise AssertionError("external attention must not read user location facts")

    @property
    def private_impressions(self) -> object:
        raise AssertionError("external attention must not read private user impressions")


class _Compiler:
    def __init__(self) -> None:
        self.queries = []

    def compile(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return _ForbiddenUserSlicesCapsule()


@pytest.mark.asyncio
async def test_context_port_freezes_complete_cursor_and_only_role_safe_capsule_slices() -> None:
    ledger = _world()
    compiler = _Compiler()
    channel = PerceptionChannelProof(
        channel_ref="channel:public-feed",
        channel_kind="public_online_feed",
        evidence_refs=("event:world-started:attention-production",),
        accessible_source_ids=("source:public-feed",),
        valid_until=NOW + timedelta(hours=1),
    )
    port = CapsuleBackedLiveAttentionContextPort(
        ledger=ledger,
        capsule_compiler=compiler,
        channel_port=StaticLiveAttentionChannelPort((channel,)),
    )

    context = await port.freeze_attention_context(
        world_id=ledger.world_id,
        actor_ref="character:zhizhi",
        observed_at=NOW,
    )

    assert isinstance(context, LiveCharacterAttentionContext)
    projected = ledger.project()
    assert context.pinned_world_cursor == ProjectionCursor(
        world_revision=projected.world_revision,
        deliberation_revision=projected.deliberation_revision,
        ledger_sequence=projected.ledger_sequence,
    )
    assert context.world_logical_time == NOW
    assert not hasattr(context, "inner_life_snapshot")
    assert context.available_channels == (channel,)
    assert compiler.queries[0].cursor == context.pinned_world_cursor


@pytest.mark.asyncio
async def test_context_port_rejects_channel_evidence_missing_from_pinned_world() -> None:
    ledger = _world()
    port = CapsuleBackedLiveAttentionContextPort(
        ledger=ledger,
        capsule_compiler=_Compiler(),
        channel_port=StaticLiveAttentionChannelPort(
            (
                PerceptionChannelProof(
                    channel_ref="channel:invented",
                    channel_kind="public_online_feed",
                    evidence_refs=("event:not-committed",),
                    accessible_source_ids=("source:public-feed",),
                    valid_until=NOW + timedelta(hours=1),
                ),
            )
        ),
    )

    with pytest.raises(ValueError, match="absent from the pinned World cursor"):
        await port.freeze_attention_context(
            world_id=ledger.world_id,
            actor_ref="character:zhizhi",
            observed_at=NOW,
        )


@pytest.mark.asyncio
async def test_public_information_channel_is_derived_from_exact_active_ledger_capability() -> None:
    capability = SimpleNamespace(
        grant_id=public_information_capability_id(REGISTRY_HASH),
        entity_revision=1,
        values=SimpleNamespace(
            capability_kind="public_information_read",
            actor_ref="character:zhizhi",
            target_scope_refs=("channel:public_information",),
            constraint_refs=("constraint:read-only",),
            valid_from=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(hours=1),
            state="active",
        ),
        origin=SimpleNamespace(
            event_ref="event:public-information-capability",
            enforcement_eligible=True,
        ),
    )
    projection = SimpleNamespace(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=9,
        logical_time=NOW,
        capability_grants=(capability,),
    )
    ledger = SimpleNamespace(world_id="world:attention-production", project=lambda: projection)
    port = LedgerPublicInformationChannelPort(
        ledger=ledger,
        accessible_source_ids=("cn.weibo.search.hot.v1", "cn.cctv.xwlb.v1"),
        registry_content_hash=REGISTRY_HASH,
    )

    channels = await port.available_channels(
        world_id=ledger.world_id,
        actor_ref="character:zhizhi",
        cursor=ProjectionCursor(
            world_revision=3,
            deliberation_revision=2,
            ledger_sequence=9,
        ),
        capsule=SimpleNamespace(),
        observed_at=NOW + timedelta(days=1),
    )

    assert len(channels) == 1
    assert channels[0].evidence_refs == ("event:public-information-capability",)
    assert channels[0].accessible_source_ids == (
        "cn.cctv.xwlb.v1",
        "cn.weibo.search.hot.v1",
    )
    changed_registry = LedgerPublicInformationChannelPort(
        ledger=ledger,
        accessible_source_ids=("cn.weibo.search.hot.v1",),
        registry_content_hash="sha256:" + "c" * 64,
    )
    assert changed_registry.authority_is_available(actor_ref="character:zhizhi") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["revoked", "expired"])
@pytest.mark.asyncio
async def test_public_information_channel_fails_closed_without_active_authority(
    state: str,
) -> None:
    capability = SimpleNamespace(
        grant_id=public_information_capability_id(REGISTRY_HASH),
        entity_revision=1,
        values=SimpleNamespace(
            capability_kind="public_information_read",
            actor_ref="character:zhizhi",
            target_scope_refs=("channel:public_information",),
            constraint_refs=("constraint:read-only",),
            valid_from=NOW - timedelta(minutes=1),
            expires_at=NOW - timedelta(seconds=1) if state == "expired" else None,
            state="active" if state == "expired" else "revoked",
        ),
        origin=SimpleNamespace(
            event_ref="event:public-information-capability",
            enforcement_eligible=True,
        ),
    )
    projection = SimpleNamespace(
        world_revision=3,
        deliberation_revision=2,
        ledger_sequence=9,
        logical_time=NOW,
        capability_grants=(capability,),
    )
    port = LedgerPublicInformationChannelPort(
        ledger=SimpleNamespace(world_id="world:attention-production", project=lambda: projection),
        accessible_source_ids=("cn.weibo.search.hot.v1",),
        registry_content_hash=REGISTRY_HASH,
    )

    channels = await port.available_channels(
        world_id="world:attention-production",
        actor_ref="character:zhizhi",
        cursor=ProjectionCursor(
            world_revision=3,
            deliberation_revision=2,
            ledger_sequence=9,
        ),
        capsule=SimpleNamespace(),
        observed_at=NOW,
    )

    assert channels == ()


@pytest.mark.asyncio
async def test_shadow_context_uses_same_capsule_but_only_an_opaque_read_cursor() -> None:
    ledger = _world()
    compiler = _Compiler()
    channel = PerceptionChannelProof(
        channel_ref="channel:public-feed",
        channel_kind="public_online_feed",
        evidence_refs=("event:world-started:attention-production",),
        accessible_source_ids=("source:public-feed",),
        valid_until=NOW + timedelta(hours=1),
    )
    port = CapsuleBackedShadowAttentionContextPort(
        ledger=ledger,
        capsule_compiler=compiler,
        channel_port=StaticLiveAttentionChannelPort((channel,)),
    )

    context = await port.freeze_attention_context(
        world_id=ledger.world_id,
        actor_ref="character:zhizhi",
        observed_at=NOW,
    )

    assert isinstance(context, CharacterAttentionContext)
    cursor = json.loads(context.pinned_world_cursor.removeprefix("projection-cursor:"))
    assert cursor == {
        "deliberation_revision": 0,
        "ledger_sequence": 1,
        "world_revision": 1,
    }
    assert context.world_logical_time == NOW
