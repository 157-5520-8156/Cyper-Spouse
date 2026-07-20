from datetime import UTC, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import (
    ConcurrencyConflict,
    WorldError,
    WorldKernel,
    _state_hash,
    parse_reply_candidate,
)
from companion_daemon.media_shot import MediaShotPlanner
from companion_daemon.world_media import WorldMediaDecision


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def media_shot_payload(
    snapshot: dict[str, object], *, request_id: str, media_kind: str = "selfie"
) -> dict[str, object]:
    return MediaShotPlanner().plan(
        snapshot,
        WorldMediaDecision(True, media_kind, "test", capture_mode="handheld_selfie"),
        request_id,
    ).to_payload()


def world_seed() -> dict[str, object]:
    return {
        "world_id": "zhizhi-v1",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion", "templates": ["dorm_chat", "course_notes"]},
        "life_outcome_templates": {
            "dorm_chat": {"location": "华师大宿舍", "npc_id": "roommate-lin", "energy_cost": 4, "content": "晚饭后在宿舍聊了几句新书。"},
            "course_notes": {"location": "华东师范大学", "goal_id": "course-notes", "energy_cost": 7, "content": "整理完了今天的课程笔记。"},
        },
        "daily_schedule": [{"slot": "dorm", "title": "宿舍闲聊", "template_id": "dorm_chat", "location": "华师大宿舍", "starts_hour": 8, "ends_hour": 9}],
        "npcs": [
            {
                "id": "roommate-lin",
                "name": "林晚",
                "kind": "roommate",
                "location": "华师大宿舍",
                "availability": ["00:00-23:00"],
                "templates": ["dorm_chat"],
            }
        ],
    }


def test_started_world_is_append_only_and_builds_read_projection(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))

    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    assert started.world_id == "zhizhi-v1"
    assert started.revision == 2
    assert [event.event_type for event in started.events] == ["WorldStarted", "NpcRegistered"]
    snapshot = kernel.snapshot("zhizhi-v1")
    assert snapshot["clock"]["logical_at"] == NOW.isoformat()
    assert snapshot["entities"]["roommate-lin"]["name"] == "林晚"
    assert kernel.events("zhizhi-v1")[0].payload["protagonist"]["name"] == "沈知栀"


def test_world_communication_attention_and_typing_are_event_sourced(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    observed = kernel.submit(
        {
            "type": "observe_user_message", "world_id": started.world_id,
            "message_id": "m-1", "text": "你忙吗", "sent_at": NOW.isoformat(),
        },
        expected_revision=started.revision,
    )
    assert kernel.snapshot(started.world_id)["communication"]["attention"] == "unread"
    assert kernel.snapshot(started.world_id)["recent_messages"][-1]["attachments"] == []

    deferred = kernel.submit(
        {
            "type": "set_message_attention", "world_id": started.world_id,
            "message_id": "m-1", "attention": "deferred", "reason": "active_world_activity",
            "due_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
        expected_revision=observed.revision,
    )
    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["communication"] == {
        "message_id": "m-1", "attention": "deferred", "typing": "idle",
        "reason": "active_world_activity", "due_at": (NOW + timedelta(minutes=20)).isoformat(),
        "deferred_action_id": "attention:m-1",
    }
    assert snapshot["actions"]["attention:m-1"]["status"] == "scheduled"

    typing = kernel.submit(
        {"type": "set_message_attention", "world_id": started.world_id, "message_id": "m-1", "attention": "seen", "reason": "ready_to_reply"},
        expected_revision=deferred.revision,
    )
    typed = kernel.submit(
        {"type": "set_typing_state", "world_id": started.world_id, "message_id": "m-1", "typing": "started", "reason": "composing_reply"},
        expected_revision=typing.revision,
    )
    assert kernel.snapshot(started.world_id)["communication"]["typing"] == "started"
    stopped = kernel.submit(
        {"type": "set_typing_state", "world_id": started.world_id, "message_id": "m-1", "typing": "stopped", "reason": "reply_queued"},
        expected_revision=typed.revision,
    )
    assert [event.event_type for event in stopped.events] == ["TypingStateChanged"]


def test_world_communication_rejects_typing_for_unseen_or_unknown_message(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    with pytest.raises(WorldError, match="observed message"):
        kernel.submit(
            {"type": "set_message_attention", "world_id": started.world_id, "message_id": "missing", "attention": "seen", "reason": "test"},
            expected_revision=started.revision,
        )
    observed = kernel.submit(
        {"type": "observe_user_message", "world_id": started.world_id, "message_id": "m-1", "text": "hi", "sent_at": NOW.isoformat()},
        expected_revision=started.revision,
    )
    with pytest.raises(WorldError, match="seen message"):
        kernel.submit(
            {"type": "set_typing_state", "world_id": started.world_id, "message_id": "m-1", "typing": "started", "reason": "test"},
            expected_revision=observed.revision,
        )


def test_world_user_relationship_and_emotion_are_reduced_from_turn_appraisal(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    appraised = kernel.submit(
        {
            "type": "appraise_turn", "world_id": started.world_id, "intent_id": "turn:1",
            "appraisal": "boundary_violation", "user_id": "user:geoff",
        },
        expected_revision=user.revision,
    )

    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["entities"]["user:geoff"]["kind"] == "user"
    assert snapshot["relationships"]["user:geoff"] == {
        "respect": -12,
        "reliability": -4,
        "trust": -8,
        "stage": "stranger",
        "interaction_count": 1,
        "stage_reason": "relationship_steady",
        "stage_rule_version": "world-relationship-v2",
        "stage_changed_at": NOW.isoformat(),
    }
    affect = snapshot["emotion_modulation"]
    assert affect["mode"] == "guarded"
    assert affect["expression"] == "guarded"
    assert affect["charge"] == 16
    assert affect["vector"]["hurt"] == 18
    assert affect["vector"]["anger"] == 12
    assert affect["behavior_tendency"] == "guarded"
    assert {event.event_type for event in appraised.events} >= {
        "RelationshipAppraised", "RelationshipChanged", "AffectChanged",
    }


def test_world_turn_records_deliberation_instead_of_treating_user_request_as_command(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    observed = kernel.submit(
        {
            "type": "observe_user_message", "world_id": started.world_id,
            "message_id": "m-no-advice", "user_id": "user:geoff",
            "text": "别劝我，我就准备继续熬。", "sent_at": NOW.isoformat(),
        },
        expected_revision=user.revision,
    )

    decision = kernel.submit(
        {
            "type": "appraise_turn", "world_id": started.world_id,
            "intent_id": "turn:no-advice", "message_id": "m-no-advice",
            "appraisal": "ordinary_message", "user_id": "user:geoff",
        },
        expected_revision=observed.revision,
    )

    event_types = [event.event_type for event in decision.events]
    assert event_types[-3:] == [
        "UserRequestAppraised", "MotiveConflictEvaluated", "StanceSelected"
    ]
    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["last_user_request"]["kind"] == "no_advice"
    assert snapshot["last_deliberation"]["stance"] in {
        "comply", "comply_then_revisit", "disagree_gently", "refuse_to_affirm", "defer"
    }


def test_world_dashboard_projection_is_self_contained_and_never_needs_legacy_runtime(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.submit(
        {"type": "plan_activity", "world_id": started.world_id, "activity_id": "study", "entity_id": "zhizhi", "title": "图书馆看书", "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(hours=1)).isoformat()},
        expected_revision=started.revision,
    )
    projection = kernel.daemon_dashboard_projection(started.world_id, past_days=0, future_days=0)

    assert projection["dashboard"]["scene"]["location"] == "desk"
    assert projection["dashboard"]["scene"]["action"] == "study"
    assert projection["life_runtime"]["activity"] == "图书馆看书"
    assert projection["calendar"]["days"][0]["plans"][0]["activity"] == "图书馆看书"
    assert projection["state"]["world_id"] == started.world_id


def test_world_conversation_context_is_rebuildable_and_keeps_plans_nonreferencable(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    kernel.submit(
        {"type": "plan_activity", "world_id": started.world_id, "activity_id": "study", "entity_id": "zhizhi", "title": "图书馆看书", "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(hours=1)).isoformat()},
        expected_revision=user.revision,
    )
    context = kernel.conversation_context(started.world_id, user_id="user:geoff")

    assert context["self_core"]["name"] == "沈知栀"
    assert context["referencable_experiences"] == []
    assert context["behavior"]["policy"]["mode"] == "available"


def test_seeded_character_core_and_confirmed_user_profile_are_separate_projections(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    kernel.submit(
        {
            "type": "confirm_fact", "world_id": started.world_id,
            "fact_id": "user:tea", "subject": "user:geoff",
            "value": "用户喜欢桂花乌龙。", "source": "user_message:test",
        },
        expected_revision=user.revision,
    )

    context = kernel.conversation_context(started.world_id, user_id="user:geoff")

    assert "真诚比漂亮话重要" in context["self_core"]["values"]
    assert "中文短句，像 QQ 或微信私聊" in context["self_core"]["speech_anchors"]
    assert context["user_profile"] == [
        {
            "source_id": "user:tea",
            "source": "user_message:test",
            "source_type": "fact",
            "subject": "user:geoff",
            "logical_at": "2026-07-11T09:00:00+08:00",
            "purpose": "personalize",
            "value": "用户喜欢桂花乌龙。",
            "reference_state": "confirmed",
            "status": "current",
            "conflict_key": "",
            "pinned": False,
            "importance": 50,
        }
    ]
    assert context["self_core"]["source"] == "world_event:WorldStarted"
    assert context["self_core"]["subject"] == "zhizhi"
    assert context["self_core"]["logical_at"] == "2026-07-11T09:00:00+08:00"
    assert context["current_scene_source"]["subject"] == "zhizhi"
    assert context["current_scene_source"]["logical_at"] == "2026-07-11T09:00:00+08:00"


def test_world_deferred_decision_has_review_action_and_terminal_resolution(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    deferred = kernel.submit(
        {"type": "defer_decision", "world_id": started.world_id, "decision_id": "impulse:1", "kind": "withheld_impulse", "reason": "用户正在忙", "review_at": (NOW + timedelta(minutes=30)).isoformat()},
        expected_revision=started.revision,
    )
    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["decisions"]["impulse:1"]["status"] == "deferred"
    assert snapshot["actions"]["decision:impulse:1"]["status"] == "scheduled"
    resolved = kernel.submit(
        {"type": "resolve_deferred_decision", "world_id": started.world_id, "decision_id": "impulse:1", "outcome": "abandoned", "reason": "复核后仍不适合"},
        expected_revision=deferred.revision,
    )
    assert kernel.snapshot(started.world_id)["decisions"]["impulse:1"]["status"] == "abandoned"
    assert {event.event_type for event in resolved.events} == {"DecisionResolved", "ActionCancelled"}


def test_delivered_question_opens_a_world_thread_then_resolves_or_expires(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="你今天还好吗？",
        kind="reply",
        expires_at=NOW + timedelta(hours=12),
        trace={
            "world_id": started.world_id, "appraisal": "ordinary_message", "expression_policy": "test",
            "allowed_facts": [], "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "thread:one", "user_id": "user:geoff", "question": "你今天还好吗？",
                "expires_at": (NOW + timedelta(hours=24)).isoformat(),
            },
        },
    )
    kernel.settle_outgoing_action(delivery_id, delivered=True)
    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["conversation_threads"]["thread:one"]["status"] == "open"
    assert snapshot["conversation_threads"]["thread:one"]["source_action_id"] == action_id

    answered = kernel.submit(
        {"type": "resolve_conversation_thread", "world_id": started.world_id, "thread_id": "thread:one", "outcome": "answered", "reason": "用户给出了明确回答"},
        expected_revision=kernel.revision(started.world_id),
    )
    assert [event.event_type for event in answered.events] == ["ConversationThreadResolved"]
    assert kernel.snapshot(started.world_id)["conversation_threads"]["thread:one"]["status"] == "answered"

    second_delivery, _, _ = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="要不要明天再聊？",
        kind="reply",
        expires_at=NOW + timedelta(hours=12),
        trace={
            "world_id": started.world_id, "appraisal": "ordinary_message", "expression_policy": "test",
            "allowed_facts": [], "observable_reason": "test",
            "conversation_thread": {
                "thread_id": "thread:two", "user_id": "user:geoff", "question": "要不要明天再聊？",
                "expires_at": (NOW + timedelta(hours=2)).isoformat(),
            },
        },
    )
    kernel.settle_outgoing_action(second_delivery, delivered=True)
    kernel.advance(started.world_id, NOW + timedelta(hours=3), expected_revision=kernel.revision(started.world_id))
    assert kernel.snapshot(started.world_id)["conversation_threads"]["thread:two"]["status"] == "expired"


def test_media_generation_and_delivery_are_separate_world_actions(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    requested = kernel.submit(
        {
            "type": "request_media", "world_id": started.world_id, "request_id": "image:one",
            "user_id": "user:geoff", "media_kind": "selfie", "topic": "宿舍里的随手拍",
            "reason": "world_relationship_allows_selfie", "rule_version": "world-media-v1",
            "shot_plan": media_shot_payload(kernel.snapshot(started.world_id), request_id="image:one"),
        },
        expected_revision=user.revision,
    )
    generated = kernel.record_external_result(
        "media-generation:image:one",
        {"kind": "media_generation", "status": "delivered", "artifact_path": "assets/life/one.png", "artifact_hash": "abc123"},
        expected_revision=requested.revision,
        world_id=started.world_id,
    )
    assert kernel.snapshot(started.world_id)["media"]["image:one"]["status"] == "generated"
    delivery = kernel.submit(
        {"type": "schedule_media_delivery", "world_id": started.world_id, "request_id": "image:one"},
        expected_revision=generated.revision,
    )
    kernel.record_external_result(
        "media-delivery:image:one", {"kind": "media_delivery", "status": "delivered"},
        expected_revision=delivery.revision, world_id=started.world_id,
    )
    media = kernel.snapshot(started.world_id)["media"]["image:one"]
    assert media["status"] == "shared"
    assert media["delivery_action_id"] == "media-delivery:image:one"


def test_media_delivery_context_is_preserved_in_the_world_outbox(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    context = {"platform": "qq", "platform_user_id": "geoff", "text": "能发一张自拍吗", "message_id": "media-context"}
    requested = kernel.submit(
        {
            "type": "request_media", "world_id": started.world_id, "request_id": "image:context",
            "user_id": "user:geoff", "media_kind": "selfie", "topic": "窗边自拍",
            "reason": "world_relationship_allows_selfie", "delivery_context": context,
            "shot_plan": media_shot_payload(kernel.snapshot(started.world_id), request_id="image:context"),
        },
        expected_revision=user.revision,
    )

    generation = kernel.snapshot(started.world_id)["actions"]["media-generation:image:context"]
    assert generation["payload"]["delivery_context"] == context
    assert generation["payload"]["capture_mode"] == "handheld_selfie"
    assert generation["payload"]["shot_plan"] == kernel.snapshot(started.world_id)["media"]["image:context"]["shot_plan"]

    generated = kernel.record_external_result(
        "media-generation:image:context",
        {"kind": "media_generation", "status": "delivered", "artifact_path": "assets/life/one.png", "artifact_hash": "abc123"},
        expected_revision=requested.revision,
        world_id=started.world_id,
    )
    kernel.submit(
        {"type": "schedule_media_delivery", "world_id": started.world_id, "request_id": "image:context"},
        expected_revision=generated.revision,
    )
    delivery = kernel.snapshot(started.world_id)["actions"]["media-delivery:image:context"]
    assert delivery["payload"]["delivery_context"] == context


def test_personal_media_requires_a_frozen_shot_plan(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )

    with pytest.raises(WorldError, match="media request requires"):
        kernel.submit(
            {
                "type": "request_media", "world_id": started.world_id, "request_id": "image:no-plan",
                "user_id": "user:geoff", "media_kind": "character_media", "topic": "窗边自拍",
                "reason": "world_relationship_allows_personal_media",
            },
            expected_revision=user.revision,
        )


def test_personal_media_rejects_a_plan_not_grounded_in_current_activity(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    plan = media_shot_payload(kernel.snapshot(started.world_id), request_id="image:forged")
    plan["location"] = "未来旅行地"

    with pytest.raises(WorldError, match="media request requires"):
        kernel.submit(
            {
                "type": "request_media", "world_id": started.world_id, "request_id": "image:forged",
                "user_id": "user:geoff", "media_kind": "selfie", "topic": "旅行打卡",
                "reason": "world_relationship_allows_personal_media", "shot_plan": plan,
            },
            expected_revision=user.revision,
        )


def test_failed_media_generation_cannot_be_shared(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user = kernel.submit(
        {"type": "register_user", "world_id": started.world_id, "user_id": "user:geoff", "name": "geoff"},
        expected_revision=started.revision,
    )
    requested = kernel.submit(
        {"type": "request_media", "world_id": started.world_id, "request_id": "image:failed", "user_id": "user:geoff", "media_kind": "creative_image", "topic": "一张插画", "reason": "user_requested_creative_image"},
        expected_revision=user.revision,
    )
    failed = kernel.record_external_result(
        "media-generation:image:failed", {"kind": "media_generation", "status": "failed", "reason": "provider_down"},
        expected_revision=requested.revision, world_id=started.world_id,
    )

    assert kernel.snapshot(started.world_id)["media"]["image:failed"]["status"] == "generation_failed"
    with pytest.raises(WorldError, match="generated media"):
        kernel.submit(
            {"type": "schedule_media_delivery", "world_id": started.world_id, "request_id": "image:failed"},
            expected_revision=failed.revision,
        )

def test_world_can_start_from_the_reviewable_seed_file(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))

    decision = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))

    snapshot = kernel.snapshot(decision.world_id)
    assert snapshot["entities"]["zhizhi"]["location"] == "华东师范大学宿舍"
    assert {"mother-shen", "roommate-lin", "literature-fan", "photography-zhou"} <= set(
        snapshot["entities"]
    )

    resumed = kernel.ensure_seed_file(Path("configs/world_seed.yaml"))
    assert resumed.revision == decision.revision


def test_stale_revision_cannot_silently_overwrite_world(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    kernel.submit(
        {"type": "set_clock_mode", "world_id": "zhizhi-v1", "mode": "accelerated", "rate": 4},
        expected_revision=started.revision,
    )

    with pytest.raises(ConcurrencyConflict):
        kernel.submit(
            {"type": "set_clock_mode", "world_id": "zhizhi-v1", "mode": "paused", "rate": 0},
            expected_revision=started.revision,
        )


def test_clock_advance_completes_due_activity_and_expires_open_action(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    planned = kernel.submit(
        {
                "type": "plan_activity",
                "world_id": "zhizhi-v1",
            "activity_id": "library-1",
            "entity_id": "zhizhi",
            "title": "图书馆看书",
            "starts_at": NOW.isoformat(),
            "ends_at": (NOW + timedelta(minutes=30)).isoformat(),
        },
        expected_revision=started.revision,
    )
    scheduled = kernel.submit(
        {
                "type": "schedule_action",
                "world_id": "zhizhi-v1",
            "action_id": "reply-later-1",
            "kind": "reply_later",
            "expires_at": (NOW + timedelta(minutes=20)).isoformat(),
        },
        expected_revision=planned.revision,
    )

    advanced = kernel.advance(
        "zhizhi-v1", NOW + timedelta(minutes=40), expected_revision=scheduled.revision
    )

    assert {event.event_type for event in advanced.events} >= {
        "ClockAdvanced",
        "ActivityStarted",
        "ActivityCompleted",
        "ActionExpired",
    }
    snapshot = kernel.snapshot("zhizhi-v1")
    assert snapshot["agenda"]["library-1"]["status"] == "completed"
    assert snapshot["actions"]["reply-later-1"]["status"] == "expired"


def test_overlapping_activity_is_rejected(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    first = kernel.submit({"type": "plan_activity", "world_id": "zhizhi-v1", "activity_id": "one", "entity_id": "zhizhi", "title": "读书", "starts_at": NOW.isoformat(), "ends_at": (NOW + timedelta(hours=2)).isoformat()}, expected_revision=started.revision)
    with pytest.raises(WorldError, match="conflicts"):
        kernel.submit({"type": "plan_activity", "world_id": "zhizhi-v1", "activity_id": "two", "entity_id": "zhizhi", "title": "散步", "starts_at": (NOW + timedelta(hours=1)).isoformat(), "ends_at": (NOW + timedelta(hours=3)).isoformat()}, expected_revision=first.revision)


def test_clock_advance_materializes_seeded_daily_life_into_events(tmp_path: Path) -> None:
    seed = world_seed() | {
        "daily_schedule": [
            {
                "slot": "morning",
                "title": "宿舍闲聊",
                "template_id": "dorm_chat",
                "location": "华师大宿舍",
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ]
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    advanced = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)

    assert [event.event_type for event in advanced.events][:5] == [
        "ClockAdvanced", "ActivityPlanned", "ActivitySelected", "ActivityStarted", "ActivityCompleted"
    ]
    assert kernel.snapshot("zhizhi-v1")["agenda"]["2026-07-11:morning"]["status"] == "completed"


def test_completed_activity_creates_deterministic_outcome_goal_and_experience(tmp_path: Path) -> None:
    seed = world_seed() | {
        "daily_schedule": [{"slot": "notes", "title": "整理课程笔记", "template_id": "course_notes", "location": "华东师范大学", "starts_hour": 9, "ends_hour": 10}],
        "long_term_goals": [{"id": "course-notes", "title": "课程笔记", "target": 2}],
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    advanced = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)

    assert {event.event_type for event in advanced.events} >= {"LifeOutcomeProposed", "LifeOutcomeCommitted", "GoalProgressed", "ExperienceCommitted"}
    snapshot = kernel.snapshot("zhizhi-v1")
    assert snapshot["goals"]["course-notes"]["progress"] == 1
    assert snapshot["experiences"]["outcome:2026-07-11:notes"]["source_outcome_id"] == "outcome:2026-07-11:notes"
    assert kernel.experiences_for_time_reference("zhizhi-v1", "today")[0]["experience_id"] == "outcome:2026-07-11:notes"


def test_seeded_npc_activity_commits_a_constrained_npc_interaction(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    advanced = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)

    assert "NpcInteractionCommitted" in [event.event_type for event in advanced.events]
    interaction = next(iter(kernel.snapshot("zhizhi-v1")["npc_interactions"].values()))
    assert interaction["npc_id"] == "roommate-lin"
    assert interaction["rule_version"] == "life-sim-v3"


def test_seeded_fallback_template_replaces_an_unavailable_activity(tmp_path: Path) -> None:
    seed = world_seed() | {"daily_schedule": [{"slot": "photo", "title": "摄影整理", "template_id": "missing_template", "location": "上海", "fallback_templates": ["course_notes"], "starts_hour": 9, "ends_hour": 10}], "long_term_goals": [{"id": "course-notes", "title": "课程", "target": 2, "deadline": (NOW + timedelta(days=1)).isoformat()}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)
    activity = kernel.snapshot("zhizhi-v1")["agenda"]["2026-07-11:photo"]
    assert activity["template_id"] == "course_notes"
    assert activity["substitution_reason"] == "goal_priority"


def test_no_eligible_seeded_activity_becomes_rest_instead_of_fake_completion(tmp_path: Path) -> None:
    seed = world_seed() | {"daily_schedule": [{"slot": "rest", "title": "安排休息", "template_id": "missing_template", "location": "宿舍", "starts_hour": 9, "ends_hour": 10, "rest_when_unavailable": True, "rest_recovery": 9}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)
    snapshot = kernel.snapshot("zhizhi-v1")
    assert snapshot["agenda"]["2026-07-11:rest"]["status"] == "rested"
    assert snapshot["needs"]["energy"] == 79


def test_no_eligible_seeded_activity_is_deferred_by_default_and_never_started(tmp_path: Path) -> None:
    seed = world_seed() | {
        "daily_schedule": [
            {
                "slot": "impossible",
                "title": "没有可执行模板的安排",
                "template_id": "missing_template",
                "location": "宿舍",
                "starts_hour": 9,
                "ends_hour": 10,
            }
        ]
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    decision = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)

    assert kernel.snapshot("zhizhi-v1")["agenda"]["2026-07-11:impossible"]["status"] == "deferred"
    assert not any(
        event.event_type in {"ActivityStarted", "ActivityCompleted"}
        and event.payload["activity_id"] == "2026-07-11:impossible"
        for event in decision.events
    )


def test_crossing_a_completed_sleep_window_restores_energy(tmp_path: Path) -> None:
    seed = world_seed() | {
        "daily_schedule": [
            {
                "slot": "night_sleep",
                "title": "夜间睡眠",
                "kind": "rest",
                "location": "华师大宿舍",
                "starts_hour": 0,
                "ends_hour": 8,
                "rest_recovery": 18,
            }
        ]
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    drained = kernel.submit(
        {"type": "change_need", "world_id": started.world_id, "need": "energy", "delta": -30},
        expected_revision=started.revision,
    )

    kernel.advance(started.world_id, NOW + timedelta(days=1), expected_revision=drained.revision)

    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["needs"]["energy"] == 58
    assert snapshot["agenda"]["2026-07-12:night_sleep"]["status"] == "rested"


def test_sleep_started_and_completed_by_separate_ticks_still_restores_energy(tmp_path: Path) -> None:
    seed = world_seed() | {
        "daily_schedule": [
            {
                "slot": "night_sleep",
                "title": "夜间睡眠",
                "kind": "rest",
                "location": "华师大宿舍",
                "starts_hour": 0,
                "ends_hour": 8,
                "rest_recovery": 18,
            }
        ]
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    drained = kernel.submit(
        {"type": "change_need", "world_id": started.world_id, "need": "energy", "delta": -30},
        expected_revision=started.revision,
    )
    sleeping = kernel.advance(
        started.world_id, NOW + timedelta(hours=16), expected_revision=drained.revision
    )
    assert kernel.snapshot(started.world_id)["agenda"]["2026-07-12:night_sleep"]["status"] == "active"

    kernel.advance(
        started.world_id, NOW + timedelta(hours=23), expected_revision=sleeping.revision
    )

    snapshot = kernel.snapshot(started.world_id)
    assert snapshot["agenda"]["2026-07-12:night_sleep"]["status"] == "rested"
    assert snapshot["needs"]["energy"] == 58


def test_unavailable_activity_can_be_deferred_then_explicitly_reviewed(tmp_path: Path) -> None:
    seed = world_seed() | {"daily_schedule": [{"slot": "defer", "title": "等待安排", "template_id": "missing_template", "location": "宿舍", "starts_hour": 9, "ends_hour": 10, "defer_when_unavailable": True, "review_after_hours": 3}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    deferred = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)
    snapshot = kernel.snapshot("zhizhi-v1")
    assert snapshot["agenda"]["2026-07-11:defer"]["status"] == "deferred"
    rested = kernel.submit({"type": "review_activity", "world_id": started.world_id, "activity_id": "2026-07-11:defer", "decision": "rest", "energy_delta": 5}, expected_revision=deferred.revision)
    assert rested.events[-1].event_type == "ActivityRested"
    assert kernel.snapshot(started.world_id)["agenda"]["2026-07-11:defer"]["status"] == "rested"


def test_seeded_location_transition_defers_impossible_second_activity(tmp_path: Path) -> None:
    seed = world_seed() | {"location_travel_minutes": {"华师大宿舍->华东师范大学": 45}, "daily_schedule": [{"slot": "first", "title": "宿舍闲聊", "template_id": "dorm_chat", "location": "华师大宿舍", "starts_hour": 9, "ends_hour": 10}, {"slot": "second", "title": "课程笔记", "template_id": "course_notes", "location": "华东师范大学", "starts_hour": 10, "ends_hour": 11}], "long_term_goals": [{"id": "course-notes", "title": "课程", "target": 2, "deadline": (NOW + timedelta(days=1)).isoformat()}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    kernel.advance(started.world_id, NOW + timedelta(hours=3), expected_revision=started.revision)
    assert kernel.snapshot(started.world_id)["agenda"]["2026-07-11:second"]["status"] == "deferred"


def test_long_jump_and_incremental_advances_produce_the_same_life_state(tmp_path: Path) -> None:
    target = NOW + timedelta(days=3, hours=12)
    long_kernel = WorldKernel(CompanionStore(tmp_path / "long.sqlite"))
    long_started = long_kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    long_kernel.advance(long_started.world_id, target, expected_revision=long_started.revision)

    step_kernel = WorldKernel(CompanionStore(tmp_path / "step.sqlite"))
    step_started = step_kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    revision = step_started.revision
    for offset in (1, 2, 3):
        decision = step_kernel.advance(
            step_started.world_id,
            NOW + timedelta(days=offset),
            expected_revision=revision,
        )
        revision = decision.revision
    step_kernel.advance(step_started.world_id, target, expected_revision=revision)

    long_state = long_kernel.snapshot(long_started.world_id)
    step_state = step_kernel.snapshot(step_started.world_id)
    for field in ("agenda", "experiences", "outcomes", "goals", "needs", "npc_interactions"):
        assert long_state[field] == step_state[field]


def test_external_delivery_result_is_idempotent_and_only_settled_action_can_create_experience(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    scheduled = kernel.submit(
        {
                "type": "schedule_action",
                "world_id": "zhizhi-v1",
            "action_id": "outgoing-1",
            "kind": "outgoing_message",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
        expected_revision=started.revision,
    )

    settled = kernel.record_external_result(
        "outgoing-1",
        {"kind": "delivery", "status": "delivered", "external_id": "qq-42"},
        expected_revision=scheduled.revision,
    )
    duplicate = kernel.record_external_result(
        "outgoing-1",
        {"kind": "delivery", "status": "delivered", "external_id": "qq-42"},
        expected_revision=settled.revision,
    )
    assert duplicate.revision == settled.revision
    with pytest.raises(WorldError, match="validated life outcomes"):
        kernel.submit({"type": "commit_experience", "world_id": "zhizhi-v1", "experience_id": "shared-1", "action_id": "outgoing-1", "content": "她把一句话成功发给了用户。"}, expected_revision=duplicate.revision)


def test_outbox_trace_and_world_action_are_created_in_one_world_transaction(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    delivery_id, trace_id, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="我刚看到。",
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": "zhizhi-v1",
            "appraisal": "ordinary_message",
            "expression_policy": "自然接话。",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "用户发来一条普通消息。",
        },
    )

    assert store.outbox_message(delivery_id)["status"] == "planned"
    assert store.recent_turn_traces("geoff")[-1]["id"] == trace_id
    assert kernel.snapshot("zhizhi-v1")["actions"][action_id]["delivery_id"] == delivery_id
    assert kernel.revision("zhizhi-v1") == started.revision + 4
    assert [event.event_type for event in kernel.events("zhizhi-v1")[-4:]] == [
        "OutboundActionAllowed",
        "CostReservationDecided",
        "ActionScheduled",
        "ActionSegmentsPlanned",
    ]


def test_segmented_outgoing_action_commits_only_delivered_segments_to_history(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="第一句。第二句。",
        text_parts=["第一句。", "第二句。"],
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "自然接话。",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "用户发来一条普通消息。",
        },
    )

    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert [item["status"] for item in action["segment_state"]["segments"]] == [
        "planned",
        "planned",
    ]

    claimed = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert claimed is not None
    assert claimed.position == 0
    kernel.settle_outgoing_segment(
        delivery_id,
        claimed.segment_id,
        delivered=True,
        expected_revision=kernel.revision(started.world_id),
    )

    snapshot = kernel.snapshot(started.world_id)
    assert [item["text"] for item in snapshot["recent_messages"] if item["direction"] == "out"] == [
        "第一句。"
    ]
    assert [item["status"] for item in snapshot["actions"][action_id]["segment_state"]["segments"]] == [
        "delivered",
        "planned",
    ]
    assert [row["text"] for row in store.recent_messages("geoff") if row["direction"] == "out"] == [
        "第一句。"
    ]


def test_substantive_interjection_cancels_only_remaining_planned_segments(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="一。二。三。",
        text_parts=["一。", "二。", "三。"],
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    first = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert first is not None
    kernel.settle_outgoing_segment(
        delivery_id,
        first.segment_id,
        delivered=True,
        expected_revision=kernel.revision(started.world_id),
    )

    assert kernel.observe_outgoing_interjection(
        delivery_id,
        kind="backchannel",
        user_message_id="m-backchannel",
        expected_revision=kernel.revision(started.world_id),
    ) == ()
    cancelled = kernel.observe_outgoing_interjection(
        delivery_id,
        kind="substantive",
        user_message_id="m-takeover",
        expected_revision=kernel.revision(started.world_id),
    )

    assert cancelled == (
        f"{action_id}:segment:1",
        f"{action_id}:segment:2",
    )
    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert [item["status"] for item in action["segment_state"]["segments"]] == [
        "delivered",
        "cancelled",
        "cancelled",
    ]
    assert action["status"] == "cancelled"
    assert store.outbox_message(delivery_id)["status"] == "cancelled"


def test_snapshot_uses_current_projection_without_replaying_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "projection-cache.sqlite")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    def replay_must_not_run(*_args, **_kwargs):
        raise AssertionError("fresh world_current_state must avoid event replay")

    monkeypatch.setattr(kernel, "_load_events", replay_must_not_run)

    snapshot = kernel.snapshot(started.world_id)

    assert snapshot["clock"]["logical_at"]


def test_turn_projection_binds_context_retrieval_and_expression_to_one_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "turn-projection.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )
    observed = kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": started.world_id,
            "message_id": "m:history",
            "user_id": "user:geoff",
            "text": "我上周去看了展。",
            "sent_at": NOW.isoformat(),
        },
        expected_revision=registered.revision,
    )
    kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": started.world_id,
            "message_id": "m:current",
            "user_id": "user:geoff",
            "text": "那个展览你还记得吗？",
            "sent_at": NOW.isoformat(),
        },
        expected_revision=observed.revision,
    )

    def wrapper_must_not_run(*_args, **_kwargs):
        raise AssertionError("turn projection must not compose mixed public reads")

    monkeypatch.setattr(kernel, "conversation_context", wrapper_must_not_run)
    monkeypatch.setattr(kernel, "conversation_sources_for_query", wrapper_must_not_run)
    monkeypatch.setattr(kernel, "expression_plan", wrapper_must_not_run)

    projection = kernel.turn_projection(
        started.world_id,
        user_id="user:geoff",
        text="那个展览你还记得吗？",
        current_message_id="m:current",
        intent_id="turn:current",
    )

    assert projection.revision == kernel.revision(started.world_id)
    assert projection.state_hash
    assert projection.expression_plan.revision == projection.revision
    assert projection.conversation_context["current_scene"]["logical_at"] == NOW.isoformat()
    assert [item["source_id"] for item in projection.retrieved_sources] == ["message:m:history"]


def test_claimed_outgoing_segment_can_be_marked_unknown_without_false_history(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="也许发出去了。下一句。",
        text_parts=["也许发出去了。", "下一句。"],
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "ordinary_message",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    claimed = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert claimed is not None

    assert kernel.mark_outgoing_segment_unknown(
        delivery_id,
        claimed.segment_id,
        reason="adapter connection dropped after send",
        expected_revision=kernel.revision(started.world_id),
    ) is True

    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert [item["status"] for item in action["segment_state"]["segments"]] == [
        "unknown",
        "planned",
    ]
    assert action["status"] == "unknown"
    assert not [item for item in kernel.snapshot(started.world_id)["recent_messages"] if item["direction"] == "out"]
    assert not [row for row in store.recent_messages("geoff") if row["direction"] == "out"]


def test_rebuilding_projection_from_the_ledger_matches_live_projection(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.submit(
            {"type": "set_clock_mode", "world_id": "zhizhi-v1", "mode": "realtime", "rate": 1},
        expected_revision=started.revision,
    )

    report = kernel.rebuild_projection("zhizhi-v1", "world_current_state")

    assert report.applied_revision == 3
    assert report.event_count == 3
    assert report.matches_live is True


def test_enablement_audit_requires_clean_projection_and_no_unreconciled_delivery(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    report = kernel.audit_enablement(started.world_id, delivery_receipts_supported=False)
    assert report.ready is True
    kernel.submit({"type": "schedule_action", "world_id": started.world_id, "action_id": "open", "kind": "test", "expires_at": (NOW + timedelta(hours=1)).isoformat()}, expected_revision=started.revision)
    blocked = kernel.audit_enablement(started.world_id, delivery_receipts_supported=False)
    assert blocked.ready is False
    assert blocked.open_action_ids == ("open",)


def test_ledger_export_and_integrity_verification_are_read_only(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    exported = kernel.export_ledger(started.world_id)
    integrity = kernel.verify_ledger(started.world_id)
    assert exported[0]["event_type"] == "WorldStarted"
    assert integrity["valid"] is True


def test_model_proposal_is_not_a_fact_until_rules_accept_it(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    proposed = kernel.submit(
        {
            "type": "record_model_proposal",
            "world_id": "zhizhi-v1",
            "proposal_id": "proposal-1",
            "entity_id": "zhizhi",
                "template_id": "dorm_chat",
                "content": "晚饭后在宿舍聊了几句新书。",
                "activity_id": "proposal-activity", "location": "华师大宿舍",
                "starts_at": (NOW - timedelta(hours=1)).isoformat(), "ends_at": NOW.isoformat(), "npc_id": "roommate-lin",
        },
        expected_revision=started.revision,
    )

    assert kernel.snapshot("zhizhi-v1")["experiences"] == {}
    accepted = kernel.submit(
        {
            "type": "accept_model_proposal",
            "world_id": "zhizhi-v1",
            "proposal_id": "proposal-1",
        },
        expected_revision=proposed.revision,
    )

    assert accepted.events[-1].event_type == "LifeOutcomeRejected"
    assert kernel.snapshot("zhizhi-v1")["experiences"] == {}


def test_model_cannot_accept_an_event_outside_the_registered_entity_templates(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    with pytest.raises(WorldError):
        kernel.submit(
            {
                "type": "record_model_proposal",
                "world_id": "zhizhi-v1",
                "proposal_id": "proposal-1",
                "entity_id": "roommate-lin",
                "template_id": "unregistered_adventure",
                "content": "去了不存在的地方。",
            },
            expected_revision=started.revision,
        )


def test_reply_candidate_can_only_reference_committed_world_records(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    with pytest.raises(WorldError):
        kernel.validate_reply_candidate(
            "zhizhi-v1",
            {"reply_text": "我刚和室友去看展了。", "mentioned_event_ids": ["made-up"]},
        )

    scheduled = kernel.submit(
        {
            "type": "schedule_action",
            "world_id": "zhizhi-v1",
            "action_id": "outgoing-2",
            "kind": "outgoing_message",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
        expected_revision=started.revision,
    )
    settled = kernel.record_external_result(
        "outgoing-2", {"kind": "delivery", "status": "delivered"}, expected_revision=scheduled.revision
    )
    kernel.submit({"type": "confirm_fact", "world_id": "zhizhi-v1", "fact_id": "shared-2", "value": "她已经发出一句问候。"}, expected_revision=settled.revision)

    assert kernel.validate_reply_candidate(
        "zhizhi-v1",
        {"reply_text": "嗯。", "mentioned_event_ids": ["shared-2"], "proposed_action_ids": []},
    )["reply_text"] == "嗯。"


def test_only_explicit_fact_confirmation_enters_the_world_fact_index(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    confirmed = kernel.submit(
        {
            "type": "confirm_fact",
            "world_id": "zhizhi-v1",
            "fact_id": "user-city",
            "subject": "user",
            "value": "用户明确说自己在成都。",
            "source": "verified_user_fact",
        },
        expected_revision=started.revision,
    )

    assert confirmed.events[-1].event_type == "FactConfirmed"
    assert kernel.snapshot("zhizhi-v1")["facts"]["user-city"]["value"] == "用户明确说自己在成都。"


def test_new_conflicting_user_fact_supersedes_old_grounding_without_deleting_history(
    tmp_path: Path,
) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    first = kernel.submit(
        {
            "type": "confirm_fact",
            "world_id": started.world_id,
            "fact_id": "user-city-chengdu",
            "subject": "geoff",
            "conflict_key": "location:current",
            "value": "用户明确说自己现在住在成都。",
            "source": "qq:one",
        },
        expected_revision=started.revision,
    )
    kernel.submit(
        {
            "type": "confirm_fact",
            "world_id": started.world_id,
            "fact_id": "user-city-shanghai",
            "subject": "geoff",
            "conflict_key": "location:current",
            "value": "用户明确说自己现在住在上海。",
            "source": "qq:two",
        },
        expected_revision=first.revision,
    )

    facts = kernel.snapshot(started.world_id)["facts"]
    assert facts["user-city-chengdu"]["status"] == "superseded"
    assert facts["user-city-chengdu"]["superseded_by"] == "user-city-shanghai"
    assert facts["user-city-shanghai"]["status"] == "current"
    assert facts["user-city-shanghai"]["conflict_key"] == "location:current"

    context = kernel.conversation_context(started.world_id, user_id="geoff")
    assert [item["source_id"] for item in context["user_profile"]] == ["user-city-shanghai"]
    assert "user-city-chengdu" not in {
        item["source_id"] for item in context["referencable_facts"]
    }
    with pytest.raises(WorldError, match="uncommitted world records"):
        kernel.validate_reply_candidate(
            started.world_id,
            {
                "reply_text": "你现在住在成都。",
                "mentioned_event_ids": ["user-city-chengdu"],
                "proposed_action_ids": [],
            },
            user_id="geoff",
        )
    assert kernel.verify_ledger(started.world_id)["valid"] is True


def test_only_verified_facts_are_carried_into_a_fresh_world_epoch(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    kernel.import_verified_facts(started.world_id, ["用户明确说自己在成都。"])

    assert list(kernel.snapshot(started.world_id)["facts"].values())[0]["source"] == "verified_user_fact_import"


def test_world_reply_parser_refuses_free_text() -> None:
    with pytest.raises(WorldError):
        parse_reply_candidate("我刚和室友吃完饭。")

    assert parse_reply_candidate(
        '{"reply_text":"嗯。","mentioned_event_ids":[],"proposed_action_ids":[]}'
    )["reply_text"] == "嗯。"


def test_relationship_needs_and_cancelled_commitment_are_world_events(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    changed = kernel.submit(
        {
            "type": "change_relationship",
            "world_id": "zhizhi-v1",
            "entity_id": "roommate-lin",
            "dimension": "trust",
            "delta": 3,
        },
        expected_revision=started.revision,
    )
    needed = kernel.submit(
        {
            "type": "change_need",
            "world_id": "zhizhi-v1",
            "need": "energy",
            "delta": -8,
        },
        expected_revision=changed.revision,
    )
    planned = kernel.submit(
        {
            "type": "schedule_action",
            "world_id": "zhizhi-v1",
            "action_id": "comfort-1",
            "kind": "comfort_followup",
            "expires_at": (NOW + timedelta(hours=2)).isoformat(),
        },
        expected_revision=needed.revision,
    )
    cancelled = kernel.submit(
        {
            "type": "cancel_action",
            "world_id": "zhizhi-v1",
            "action_id": "comfort-1",
            "reason": "user_returned",
        },
        expected_revision=planned.revision,
    )

    state = kernel.snapshot("zhizhi-v1")
    assert state["relationships"]["roommate-lin"]["trust"] == 3
    assert state["needs"]["energy"] == 62
    assert state["actions"]["comfort-1"]["status"] == "cancelled"
    assert cancelled.events[-1].event_type == "ActionCancelled"


def test_experience_can_only_be_marked_shared_after_its_delivery_action(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    delivered = kernel.submit(
        {
            "type": "schedule_action", "world_id": "zhizhi-v1", "action_id": "share-1",
            "kind": "life_event", "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        }, expected_revision=started.revision,
    )
    settled = kernel.record_external_result("share-1", {"kind": "delivery", "status": "delivered"}, expected_revision=delivered.revision)
    with pytest.raises(WorldError, match="validated life outcomes"):
        kernel.submit(
            {"type": "commit_experience", "world_id": "zhizhi-v1", "experience_id": "life-1", "action_id": "share-1", "content": "傍晚散步时拍到一片梧桐叶。"},
            expected_revision=settled.revision,
        )


def test_command_hydrates_from_events_when_read_projection_is_corrupted(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    with store.connect() as conn:
        conn.execute(
            "update world_current_state set state_json = '{}' where world_id = ?", (started.world_id,)
        )

    decision = kernel.submit(
        {"type": "change_need", "world_id": started.world_id, "need": "energy", "delta": -5},
        expected_revision=started.revision,
    )

    assert decision.revision == started.revision + 1
    assert kernel.snapshot(started.world_id)["needs"]["energy"] == 65


def test_snapshot_recovers_when_projection_has_valid_json_but_invalid_shape(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    malformed = {
        "clock": {},
        "entities": {},
        "agenda": {},
        "actions": {},
        "facts": {},
        "experiences": {},
        "recent_messages": [],
    }
    with store.connect() as conn:
        conn.execute(
            "update world_current_state set state_json = ?, state_hash = ? where world_id = ?",
            (json.dumps(malformed), _state_hash(malformed), started.world_id),
        )

    snapshot = kernel.snapshot(started.world_id)

    assert snapshot["clock"]["logical_at"] == NOW.isoformat()


def test_projection_rebuild_reports_mismatch_before_repair(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    with store.connect() as conn:
        conn.execute(
            "update world_current_state set state_hash = 'corrupted' where world_id = ?", (started.world_id,)
        )

    report = kernel.rebuild_projection(started.world_id, "world_current_state")

    assert report.matches_live is False
    assert kernel.rebuild_projection(started.world_id, "world_current_state").matches_live is True


def test_outbox_action_transaction_rolls_back_when_projection_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    def broken_projection(*args, **kwargs):
        raise RuntimeError("injected projection failure")

    monkeypatch.setattr(kernel, "_write_projection", broken_projection)
    with pytest.raises(RuntimeError, match="injected projection failure"):
        kernel.queue_outgoing_action(
            canonical_user_id="geoff",
            platform="qq",
            text="这条不该留下。",
            kind="reply",
            expires_at=NOW + timedelta(hours=1),
            trace={
                "world_id": "zhizhi-v1",
                "appraisal": "ordinary_message",
                "expression_policy": "自然回应。",
                "allowed_facts": [],
                "short_lived_constraint": None,
                "observable_reason": "故障注入。",
            },
        )

    assert store.outbox_message(1) is None
    assert store.recent_turn_traces("geoff") == []
    assert [event.event_type for event in kernel.events("zhizhi-v1")] == ["WorldStarted", "NpcRegistered"]


def test_concurrent_same_revision_allows_exactly_one_world_write(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    def submit(index: int) -> str:
        try:
            kernel.submit(
                {
                    "type": "change_need", "world_id": "zhizhi-v1", "need": "energy", "delta": -1,
                    "idempotency_key": f"concurrent-{index}",
                },
                expected_revision=started.revision,
            )
            return "accepted"
        except ConcurrencyConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))

    assert sorted(outcomes) == ["accepted", "conflict"]
    assert kernel.snapshot("zhizhi-v1")["needs"]["energy"] == 69


def test_delivery_settlement_rolls_back_outbox_history_and_trace_on_world_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="这条先计划。",
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": "zhizhi-v1", "appraisal": "ordinary_message", "expression_policy": "自然回应。",
            "allowed_facts": [], "short_lived_constraint": None, "observable_reason": "故障注入。",
        },
    )

    def broken_projection(*args, **kwargs):
        raise RuntimeError("injected settlement failure")

    monkeypatch.setattr(kernel, "_write_projection", broken_projection)
    with pytest.raises(RuntimeError, match="injected settlement failure"):
        kernel.settle_outgoing_action(delivery_id, delivered=True)

    assert store.outbox_message(delivery_id)["status"] == "planned"
    assert store.recent_messages("geoff", limit=4) == []
    assert store.recent_turn_traces("geoff")[-1]["status"] == "planned"
    assert kernel.snapshot("zhizhi-v1")["actions"][action_id]["status"] == "scheduled"


def test_outgoing_transport_intent_and_input_time_are_replayable(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "user")
    kernel = WorldKernel(store)
    kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="我在。", kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={"world_id": "zhizhi-v1", "appraisal": "ordinary_message", "expression_policy": "自然回应。", "allowed_facts": [], "short_lived_constraint": None, "observable_reason": "测试。"},
    )
    kernel.submit(
        {"type": "observe_user_message", "world_id": "zhizhi-v1", "message_id": "in-1", "text": "回来啦", "sent_at": NOW.isoformat()},
        expected_revision=kernel.revision("zhizhi-v1"),
    )

    with store.connect() as conn:
        conn.execute("delete from outbox_messages where id = ?", (delivery_id,))
    state = kernel.snapshot("zhizhi-v1")
    action = state["actions"][action_id]
    assert action["canonical_user_id"] == "geoff"
    assert action["platform"] == "qq"
    assert action["text"] == "我在。"
    assert state["recent_messages"][-1]["sent_at"] == NOW.isoformat()


def test_reply_rejects_unreferenced_completed_experience_claim(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    with pytest.raises(WorldError, match="without a committed source"):
        kernel.validate_reply_candidate(
            "zhizhi-v1",
            {"reply_text": "我刚刚和林晚吃了饭。", "mentioned_event_ids": [], "proposed_action_ids": []},
        )


def test_reply_rejects_unsupported_experience_even_when_message_ends_with_question(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)

    with pytest.raises(WorldError, match="world-time or experience text"):
        kernel.validate_reply_candidate(
            "zhizhi-v1",
            {
                "reply_text": "刚爬起来，正对着课表发呆，在想要不要去图书馆。你呢？",
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
        )


def test_reply_claim_must_quote_the_specific_committed_source(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.submit(
        {"type": "confirm_fact", "world_id": started.world_id, "fact_id": "fact-1", "value": "下午在图书馆看书", "idempotency_key": "fact-1"},
        expected_revision=kernel.revision(started.world_id),
    )
    candidate = kernel.validate_reply_candidate(
        started.world_id,
        {"reply_text": "我下午在图书馆看书。", "mentioned_event_ids": ["fact-1"], "proposed_action_ids": [], "claims": [{"source_id": "fact-1", "text": "下午在图书馆看书"}]},
    )
    assert candidate["claims"] == [{"source_id": "fact-1", "text": "下午在图书馆看书"}]
    with pytest.raises(WorldError, match="quoted from"):
        kernel.validate_reply_candidate(
            started.world_id,
            {"reply_text": "我刚和林晚吃了饭。", "mentioned_event_ids": ["fact-1"], "proposed_action_ids": [], "claims": [{"source_id": "fact-1", "text": "刚和林晚吃了饭"}]},
        )


def test_reply_can_reference_only_the_current_scene_revision(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    context = kernel.conversation_context(started.world_id, user_id="user:geoff")
    source = context["current_scene_source"]

    candidate = kernel.validate_reply_candidate(
        started.world_id,
        {
            "reply_text": source["content"],
            "mentioned_event_ids": [source["source_id"]],
            "proposed_action_ids": [],
            "claims": [{"source_id": source["source_id"], "text": source["content"]}],
        },
    )
    assert candidate["reply_text"] == source["content"]

    logical_at = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
    kernel.advance(started.world_id, logical_at + timedelta(minutes=1), expected_revision=kernel.revision(started.world_id))
    with pytest.raises(WorldError, match="uncommitted world records"):
        kernel.validate_reply_candidate(
            started.world_id,
            {
                "reply_text": source["content"],
                "mentioned_event_ids": [source["source_id"]],
                "proposed_action_ids": [],
                "claims": [{"source_id": source["source_id"], "text": source["content"]}],
            },
        )


def test_life_share_scheduling_is_idempotent_until_delivery(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.advance(started.world_id, NOW, expected_revision=started.revision)
    selected = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1), expected_revision=kernel.revision(started.world_id))
    again = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1), expected_revision=selected.revision)
    assert selected is not None and again is not None
    assert selected.delivery_id == again.delivery_id


def test_world_outbound_policy_records_allowed_reply_and_ignores_unanswered_budget(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    user_id = "user:geoff"
    kernel.submit(
        {
            "type": "register_user", "world_id": started.world_id,
            "user_id": user_id, "name": "Geoff",
        },
        expected_revision=started.revision,
    )
    for index in range(2):
        delivery_id, _, _ = kernel.queue_outgoing_action(
            canonical_user_id="geoff", platform="qq", text=f"先前主动消息 {index}", kind="proactive",
            expires_at=NOW + timedelta(hours=1),
            trace={"world_id": started.world_id, "direction": "proactive", "appraisal": "pulse", "expression_policy": "test", "allowed_facts": [], "observable_reason": "test"},
        )
        kernel.settle_outgoing_action(delivery_id, delivered=True)
        logical_at = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
        kernel.advance(started.world_id, logical_at + timedelta(minutes=6), expected_revision=kernel.revision(started.world_id))

    kernel.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="这是对用户当前消息的回复", kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={"world_id": started.world_id, "user_id": user_id, "input_message_id": "input-1", "direction": "incoming_reply", "appraisal": "reply", "expression_policy": "test", "allowed_facts": [], "observable_reason": "test"},
    )

    policy_events = [event for event in kernel.events(started.world_id) if event.event_type == "OutboundActionAllowed"]
    assert policy_events[-1].payload["kind"] == "reply"
    assert policy_events[-1].payload["checks"]["unanswered_budget"]["passed"] is True
    assert policy_events[-1].payload["projection"]["unanswered_outbound_count"] == 2


def test_world_outbound_policy_rejects_third_unanswered_pulse_without_scheduling_action(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    for index in range(2):
        delivery_id, _, _ = kernel.queue_outgoing_action(
            canonical_user_id="geoff", platform="qq", text=f"主动消息 {index}", kind="proactive",
            expires_at=NOW + timedelta(hours=1),
            trace={"world_id": started.world_id, "direction": "proactive", "appraisal": f"pulse-{index}", "expression_policy": "test", "allowed_facts": [], "observable_reason": "test"},
        )
        kernel.settle_outgoing_action(delivery_id, delivered=True)
        logical_at = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
        kernel.advance(started.world_id, logical_at + timedelta(minutes=6), expected_revision=kernel.revision(started.world_id))

    action_count = len(kernel.snapshot(started.world_id)["actions"])
    with pytest.raises(WorldError, match="unanswered_budget"):
        kernel.queue_outgoing_action(
            canonical_user_id="geoff", platform="qq", text="第三次主动消息", kind="proactive",
            expires_at=NOW + timedelta(hours=1),
            trace={"world_id": started.world_id, "direction": "proactive", "appraisal": "pulse-3", "expression_policy": "test", "allowed_facts": [], "observable_reason": "test"},
        )

    assert len(kernel.snapshot(started.world_id)["actions"]) == action_count
    rejected = [event for event in kernel.events(started.world_id) if event.event_type == "OutboundActionRejected"][-1]
    assert rejected.payload["reasons"] == ["unanswered_budget"]


def test_world_outbound_policy_allows_costly_audited_override_of_soft_budget(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    for index in range(2):
        delivery_id, _, _ = kernel.queue_outgoing_action(
            canonical_user_id="geoff", platform="qq", text=f"主动消息 {index}", kind="proactive",
            expires_at=NOW + timedelta(hours=1),
            trace={"world_id": started.world_id, "direction": "proactive", "appraisal": f"pulse-{index}", "expression_policy": "test", "allowed_facts": [], "observable_reason": "test"},
        )
        kernel.settle_outgoing_action(delivery_id, delivered=True)
        logical_at = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
        kernel.advance(started.world_id, logical_at + timedelta(minutes=6), expected_revision=kernel.revision(started.world_id))

    _, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff", platform="qq", text="这次我还是决定主动说一句", kind="proactive",
        expires_at=NOW + timedelta(hours=2),
        trace={
            "world_id": started.world_id, "direction": "proactive", "appraisal": "repair_impulse",
            "expression_policy": "test", "allowed_facts": [], "observable_reason": "test",
            "outbound_override": {"reason": "角色选择承担打扰风险来修复关系", "cost": 25, "strike": 1},
        },
    )

    assert kernel.snapshot(started.world_id)["actions"][action_id]["status"] == "scheduled"
    overridden = [event for event in kernel.events(started.world_id) if event.event_type == "OutboundSoftGateOverridden"][-1]
    assert overridden.payload["override"] == {
        "reason": "角色选择承担打扰风险来修复关系", "cost": 25, "strike": 1,
        "overridden_gates": ["unanswered_budget"],
    }


def test_delivered_life_share_consumes_daily_limit(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.advance(started.world_id, NOW, expected_revision=started.revision)
    delivery = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1), expected_revision=kernel.revision(started.world_id))
    assert delivery is not None
    kernel.settle_outgoing_action(delivery.delivery_id, delivered=True)
    assert any(item.get("shared") for item in kernel.snapshot(started.world_id)["experiences"].values())
    assert kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=2), expected_revision=kernel.revision(started.world_id)) is None


def test_goal_deadline_is_deferred_by_logical_time_advance(tmp_path: Path) -> None:
    seed = world_seed() | {"long_term_goals": [{"id": "course-notes", "title": "课程", "target": 2, "deadline": (NOW + timedelta(days=1)).isoformat()}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    advanced = kernel.advance("zhizhi-v1", NOW + timedelta(days=2), expected_revision=started.revision)
    assert any(event.event_type == "GoalDeferred" for event in advanced.events)
    assert kernel.snapshot("zhizhi-v1")["goals"]["course-notes"]["status"] == "deferred"


def test_goal_review_can_resume_or_abandon_with_compensation(tmp_path: Path) -> None:
    seed = world_seed() | {"long_term_goals": [{"id": "course-notes", "title": "课程", "target": 2, "deadline": (NOW + timedelta(hours=1)).isoformat()}]}
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)
    deferred = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)
    reviewed = kernel.submit({"type": "review_goal", "world_id": "zhizhi-v1", "goal_id": "course-notes", "decision": "resume", "deadline": (NOW + timedelta(days=2)).isoformat()}, expected_revision=deferred.revision)
    assert reviewed.events[-1].event_type == "GoalResumed"
    deferred_again = kernel.advance("zhizhi-v1", NOW + timedelta(days=3), expected_revision=reviewed.revision)
    abandoned = kernel.submit({"type": "review_goal", "world_id": "zhizhi-v1", "goal_id": "course-notes", "decision": "abandon"}, expected_revision=deferred_again.revision)
    assert [event.event_type for event in abandoned.events] == ["GoalAbandoned", "GoalCompensated"]
    assert kernel.snapshot("zhizhi-v1")["goals"]["course-notes"]["status"] == "abandoned"


def test_life_share_delivery_is_atomic_and_uncertain_sends_are_not_retried(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    kernel.advance(started.world_id, NOW, expected_revision=started.revision)

    delivery = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1), expected_revision=kernel.revision(started.world_id))
    assert delivery is not None
    assert kernel.snapshot(started.world_id)["actions"][delivery.action_id]["status"] == "scheduled"
    assert kernel.begin_outgoing_action(delivery.delivery_id, expected_revision=delivery.revision) is True
    assert kernel.recover_interrupted_life_share_deliveries(started.world_id) == 1
    assert store.outbox_message(delivery.delivery_id)["status"] == "unknown"
    assert kernel.snapshot(started.world_id)["actions"][delivery.action_id]["status"] == "unknown"
    assert kernel.begin_outgoing_action(delivery.delivery_id, expected_revision=kernel.revision(started.world_id)) is False
    assert kernel.snapshot(started.world_id)["experiences"][delivery.experience_id].get("shared") is not True
    with pytest.raises(WorldError, match="external receipt"):
        kernel.settle_outgoing_action(delivery.delivery_id, delivered=True)
    kernel.settle_outgoing_action(delivery.delivery_id, delivered=True, external_receipt="qq:message-42")
    assert kernel.snapshot(started.world_id)["experiences"][delivery.experience_id]["shared"] is True
    assert kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=2), expected_revision=kernel.revision(started.world_id)) is None


def test_restart_marks_every_interrupted_outgoing_delivery_unknown(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit(
        {"type": "start_world", "seed": world_seed()}, expected_revision=0
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="普通回复也可能在发送时崩溃。",
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    assert kernel.begin_outgoing_action(
        delivery_id,
        expected_revision=kernel.revision(started.world_id),
        lease_seconds=0,
    )

    assert kernel.recover_interrupted_outgoing_deliveries(started.world_id) == 1
    assert store.outbox_message(delivery_id)["status"] == "unknown"
    assert kernel.snapshot(started.world_id)["actions"][action_id]["status"] == "unknown"
    assert (
        kernel.recover_interrupted_outgoing_deliveries(started.world_id) == 0
    )


def test_restart_expires_only_the_claimed_segment_and_preserves_unsent_remainder(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "segmented-recovery.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit(
        {"type": "start_world", "seed": world_seed()}, expected_revision=0
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="可能发出。还没发。",
        text_parts=["可能发出。", "还没发。"],
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    kernel.claim_outgoing_segment(
        delivery_id,
        expected_revision=kernel.revision(started.world_id),
    )

    assert kernel.recover_interrupted_outgoing_deliveries(started.world_id) == 0
    assert (
        kernel.recover_interrupted_outgoing_deliveries(
            started.world_id,
            observed_now=datetime.now(UTC) + timedelta(minutes=6),
        )
        == 1
    )
    segments = kernel.snapshot(started.world_id)["actions"][action_id]["segment_state"][
        "segments"
    ]
    assert [item["status"] for item in segments] == ["unknown", "planned"]


def test_whole_delivery_reconciliation_refuses_to_invent_a_planned_segment(
    tmp_path: Path,
) -> None:
    store = CompanionStore(tmp_path / "partial-reconciliation.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit(
        {"type": "start_world", "seed": world_seed()}, expected_revision=0
    )
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="第一。第二。第三。",
        text_parts=["第一。", "第二。", "第三。"],
        kind="reply",
        expires_at=NOW + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "observable_reason": "test",
        },
    )
    first = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert first
    kernel.settle_outgoing_segment(
        delivery_id,
        first.segment_id,
        delivered=True,
        expected_revision=kernel.revision(started.world_id),
    )
    second = kernel.claim_outgoing_segment(
        delivery_id, expected_revision=kernel.revision(started.world_id)
    )
    assert second
    kernel.mark_outgoing_segment_unknown(
        delivery_id,
        second.segment_id,
        reason="crash after send",
        expected_revision=kernel.revision(started.world_id),
    )

    with pytest.raises(WorldError, match="segment-level reconciliation"):
        kernel.settle_outgoing_action(
            delivery_id,
            delivered=True,
            external_receipt="whole-receipt",
            expected_revision=kernel.revision(started.world_id),
            reconciliation_evidence={"reviewer_id": "ops"},
        )

    action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert [item["status"] for item in action["segment_state"]["segments"]] == [
        "delivered",
        "unknown",
        "planned",
    ]
    assert [
        row["text"] for row in store.recent_messages("geoff") if row["direction"] == "out"
    ] == ["第一。"]

    kernel.settle_outgoing_action(
        delivery_id,
        delivered=False,
        reason="platform confirmed the uncertain segment failed",
        external_receipt="segment-failure-receipt",
        expected_revision=kernel.revision(started.world_id),
        reconciliation_evidence={"reviewer_id": "ops"},
    )
    failed_action = kernel.snapshot(started.world_id)["actions"][action_id]
    assert failed_action["status"] == "failed"
    assert [
        item["status"] for item in failed_action["segment_state"]["segments"]
    ] == ["delivered", "cancelled", "cancelled"]


def test_week_long_life_simulation_rebuilds_deterministically(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    advanced = kernel.advance(started.world_id, datetime(2026, 7, 20, 22, tzinfo=NOW.tzinfo), expected_revision=started.revision)
    snapshot = kernel.snapshot(started.world_id)
    report = kernel.rebuild_projection(started.world_id, "world_current_state")
    assert advanced.state_hash == report.state_hash
    assert snapshot["goals"]["literature-reading"]["status"] in {"completed", "deferred"}
    outcomes = [item for item in snapshot["outcomes"].values() if item.get("npc_id")]
    assert len({(item["activity_id"][:10], item["npc_id"]) for item in outcomes}) == len(outcomes)


def test_two_week_world_replay_has_no_duplicate_experiences_or_open_actions(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    advanced = kernel.advance(started.world_id, datetime(2026, 7, 25, 22, tzinfo=NOW.tzinfo), expected_revision=started.revision)
    snapshot = kernel.snapshot(started.world_id)
    assert advanced.state_hash == kernel.rebuild_projection(started.world_id, "world_current_state").state_hash
    assert len(snapshot["experiences"]) == len(set(snapshot["experiences"]))
    assert all(item["status"] not in {"planned", "active"} for item in snapshot["agenda"].values())
    assert all(item["status"] not in {"scheduled", "sending"} for item in snapshot["actions"].values())
