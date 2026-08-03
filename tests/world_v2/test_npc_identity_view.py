from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.life_events import NpcRegisteredPayload
from companion_daemon.world_v2.life_reducers import register_npc
from companion_daemon.world_v2.npc_identity_view import npc_identity_views
from companion_daemon.world_v2.schemas import EvidenceRef, NpcProjection, NpcPromotionEdge


NOW = datetime(2026, 8, 4, tzinfo=UTC)


class _Projection:
    npcs = (
        NpcProjection(
            npc_id="lin",
            entity_revision=1,
            stable_identity_ref="content:npc:lin",
            privacy_class="personal",
            source_event_ref="event:settlement:met-lin",
            effect_descriptor_hash="1" * 64,
            accepted_event_ref="event:npc:lin:register",
            promotion_edge=NpcPromotionEdge(
                provisional_entity_ref="provisional:npc:lin",
                stable_npc_ref="npc:lin",
                origin_settlement_event_ref="event:settlement:met-lin",
                descriptor_content_ref="content:npc:lin",
                descriptor_hash="1" * 64,
                registration_event_ref="event:npc:lin:register",
            ),
        ),
    )
    world_occurrences = (
        SimpleNamespace(
            status="settled",
            occurrence_id="occurrence:met-lin",
            settlement_event_ref="event:settlement:met-lin",
            result_id="result:met-lin",
            participant_refs=("actor:companion", "provisional:npc:lin"),
            candidate_outcomes=(
                SimpleNamespace(
                    result_id="result:met-lin",
                    provisional_npc_introductions=(
                        SimpleNamespace(
                            descriptor_hash="1" * 64,
                            provisional_entity_ref="provisional:npc:lin",
                            summary_content_ref="content:npc:lin",
                            summary_payload_hash=life_content_payload_hash(
                                "林嘉，角色在实习团队里刚认识的设计师。"
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )
    experiences = ()
    plans = ()


def test_identity_view_exposes_exact_descriptor_not_opaque_content_ref() -> None:
    store = InMemoryImmutableLifeContentStore()
    descriptor = "林嘉，角色在实习团队里刚认识的设计师。"
    store.put_if_absent(
        StoredLifeContent(
            content_ref="content:npc:lin",
            content_kind="provisional_npc_introduction",
            text=descriptor,
            content_payload_hash=life_content_payload_hash(descriptor),
        )
    )

    views = npc_identity_views(_Projection(), content_store=store)

    assert len(views) == 1
    assert views[0].descriptor == descriptor
    assert views[0].promotion_event_ref == "event:npc:lin:register"
    assert "content:npc:lin" not in views[0].descriptor


def test_identity_view_fails_closed_when_descriptor_bytes_are_missing() -> None:
    store = InMemoryImmutableLifeContentStore()
    assert npc_identity_views(_Projection(), content_store=store) == ()


def test_dynamic_promotion_projects_provisional_first_experience_to_stable_npc() -> None:
    store = InMemoryImmutableLifeContentStore()
    descriptor = "林嘉，角色在实习团队里刚认识的设计师。"
    experience = "第一次在项目复盘会上碰见林嘉，两个人后来一起改了作品集。"
    store.put_if_absent(
        StoredLifeContent(
            content_ref="content:npc:lin",
            content_kind="provisional_npc_introduction",
            text=descriptor,
            content_payload_hash=life_content_payload_hash(descriptor),
        )
    )
    store.put_if_absent(
        StoredLifeContent(
            content_ref="content:experience:met-lin",
            content_kind="experience_summary",
            text=experience,
            content_payload_hash=life_content_payload_hash(experience),
        )
    )
    projection = SimpleNamespace(
        npcs=_Projection.npcs,
        world_occurrences=_Projection.world_occurrences,
        experiences=(
                SimpleNamespace(
                    experience_id="experience:met-lin",
                    origin=SimpleNamespace(
                        accepted_event_ref="event:experience:met-lin"
                    ),
                    values=SimpleNamespace(
                    participant_refs=("actor:companion", "provisional:npc:lin"),
                    summary_ref="content:experience:met-lin",
                    summary_payload_hash=life_content_payload_hash(experience),
                ),
            ),
        ),
        plans=(),
    )

    view = npc_identity_views(projection, content_store=store)[0]

    assert view.provisional_entity_ref == "provisional:npc:lin"
    assert view.first_occurrence_ref == "occurrence:met-lin"
    assert view.shared_experience_refs == ("experience:met-lin",)
    assert view.shared_experience_summaries == (experience,)


def test_dynamic_identity_fails_closed_without_exact_promotion_edge() -> None:
    store = InMemoryImmutableLifeContentStore()
    descriptor = "林嘉，角色在实习团队里刚认识的设计师。"
    store.put_if_absent(
        StoredLifeContent(
            content_ref="content:npc:lin",
            content_kind="provisional_npc_introduction",
            text=descriptor,
            content_payload_hash=life_content_payload_hash(descriptor),
        )
    )
    projection = SimpleNamespace(
        npcs=(
            _Projection.npcs[0].model_copy(update={"promotion_edge": None}),
        ),
        world_occurrences=_Projection.world_occurrences,
        experiences=(),
        plans=(),
    )

    assert npc_identity_views(projection, content_store=store) == ()


def _dynamic_npc(
    *,
    npc_id: str,
    provisional_ref: str,
    settlement_ref: str,
    descriptor_ref: str,
    descriptor_hash: str,
) -> NpcProjection:
    registration_ref = f"event:npc:{npc_id}:register"
    return NpcProjection(
        npc_id=npc_id,
        entity_revision=1,
        stable_identity_ref=descriptor_ref,
        privacy_class="personal",
        source_event_ref=settlement_ref,
        effect_descriptor_hash=descriptor_hash,
        accepted_event_ref=registration_ref,
        promotion_edge=NpcPromotionEdge(
            provisional_entity_ref=provisional_ref,
            stable_npc_ref=f"npc:{npc_id}",
            origin_settlement_event_ref=settlement_ref,
            descriptor_content_ref=descriptor_ref,
            descriptor_hash=descriptor_hash,
            registration_event_ref=registration_ref,
        ),
    )


def _registration(npc: NpcProjection) -> NpcRegisteredPayload:
    return NpcRegisteredPayload(
        change_id=f"change:{npc.npc_id}",
        transition_id=f"transition:{npc.npc_id}",
        expected_entity_revision=0,
        evidence_refs=(
            EvidenceRef(
                ref_id=npc.source_event_ref or "event:bootstrap",
                evidence_type="settled_world_event",
                claim_purpose="current_fact",
                source_world_revision=1,
                immutable_hash="a" * 64,
            ),
        ),
        npc=npc,
    )


def test_registration_rejects_one_provisional_identity_promoted_twice() -> None:
    first = _dynamic_npc(
        npc_id="lin",
        provisional_ref="provisional:npc:lin",
        settlement_ref="event:settlement:lin",
        descriptor_ref="content:npc:lin",
        descriptor_hash="1" * 64,
    )
    duplicate = _dynamic_npc(
        npc_id="lin-copy",
        provisional_ref="provisional:npc:lin",
        settlement_ref="event:settlement:other",
        descriptor_ref="content:npc:lin-copy",
        descriptor_hash="2" * 64,
    )

    with pytest.raises(ValueError, match="already promoted"):
        register_npc((first,), _registration(duplicate))


def test_registration_rejects_one_origin_descriptor_merged_into_two_people() -> None:
    first = _dynamic_npc(
        npc_id="lin",
        provisional_ref="provisional:npc:lin",
        settlement_ref="event:settlement:lin",
        descriptor_ref="content:npc:lin",
        descriptor_hash="1" * 64,
    )
    wrong_merge = _dynamic_npc(
        npc_id="someone-else",
        provisional_ref="provisional:npc:someone-else",
        settlement_ref="event:settlement:lin",
        descriptor_ref="content:npc:someone-else",
        descriptor_hash="1" * 64,
    )

    with pytest.raises(ValueError, match="already promoted"):
        register_npc((first,), _registration(wrong_merge))


def test_promotion_edge_cannot_point_at_another_stable_npc() -> None:
    with pytest.raises(ValueError, match="promotion edge disagrees"):
        NpcProjection(
            npc_id="lin",
            entity_revision=1,
            stable_identity_ref="content:npc:lin",
            privacy_class="personal",
            source_event_ref="event:settlement:lin",
            effect_descriptor_hash="1" * 64,
            accepted_event_ref="event:npc:lin:register",
            promotion_edge=NpcPromotionEdge(
                provisional_entity_ref="provisional:npc:lin",
                stable_npc_ref="npc:someone-else",
                origin_settlement_event_ref="event:settlement:lin",
                descriptor_content_ref="content:npc:lin",
                descriptor_hash="1" * 64,
                registration_event_ref="event:npc:lin:register",
            ),
        )
