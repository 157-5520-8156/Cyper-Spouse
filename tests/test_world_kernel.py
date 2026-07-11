from datetime import UTC, datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.world import ConcurrencyConflict, WorldError, WorldKernel, parse_reply_candidate


NOW = datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def world_seed() -> dict[str, object]:
    return {
        "world_id": "zhizhi-v1",
        "logical_at": NOW.isoformat(),
        "protagonist": {"id": "zhizhi", "name": "沈知栀", "kind": "companion", "templates": ["dorm_chat", "course_notes"]},
        "life_outcome_templates": {
            "dorm_chat": {"location": "华师大宿舍", "npc_id": "roommate-lin", "energy_cost": 4, "content": "晚饭后在宿舍聊了几句新书。"},
            "course_notes": {"location": "华东师范大学", "goal_id": "course-notes", "energy_cost": 7, "content": "整理完了今天的课程笔记。"},
        },
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
            {"slot": "morning", "title": "图书馆看书", "starts_hour": 9, "ends_hour": 10}
        ]
    }
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    advanced = kernel.advance("zhizhi-v1", NOW + timedelta(hours=2), expected_revision=started.revision)

    assert [event.event_type for event in advanced.events] == [
        "ClockAdvanced", "ActivityPlanned", "ActivityStarted", "ActivityCompleted"
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
    committed = kernel.submit(
        {
            "type": "commit_experience",
            "world_id": "zhizhi-v1",
            "experience_id": "shared-1",
            "action_id": "outgoing-1",
            "content": "她把一句话成功发给了用户。",
        },
        expected_revision=duplicate.revision,
    )

    assert duplicate.revision == settled.revision
    assert committed.events[-1].event_type == "ExperienceCommitted"
    assert kernel.snapshot("zhizhi-v1")["experiences"]["shared-1"]["action_id"] == "outgoing-1"


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
    assert kernel.revision("zhizhi-v1") == started.revision + 1


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

    assert {event.event_type for event in accepted.events} >= {"ActivityCompleted", "LifeOutcomeCommitted", "ExperienceCommitted"}
    assert kernel.snapshot("zhizhi-v1")["experiences"]["proposal-1"]["content"] == "晚饭后在宿舍聊了几句新书。"


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
    kernel.submit(
        {
            "type": "commit_experience",
            "world_id": "zhizhi-v1",
            "experience_id": "shared-2",
            "action_id": "outgoing-2",
            "content": "她已经发出一句问候。",
        },
        expected_revision=settled.revision,
    )

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
    committed = kernel.submit(
        {
            "type": "commit_experience", "world_id": "zhizhi-v1", "experience_id": "life-1",
            "action_id": "share-1", "content": "傍晚散步时拍到一片梧桐叶。",
        }, expected_revision=settled.revision,
    )

    with pytest.raises(WorldError, match="scheduled delivery"):
        kernel.submit(
            {"type": "share_experience", "world_id": "zhizhi-v1", "experience_id": "life-1", "action_id": "share-1"},
            expected_revision=committed.revision,
        )
    assert kernel.snapshot("zhizhi-v1")["experiences"]["life-1"].get("shared") is not True


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


def test_life_share_scheduling_is_idempotent_until_delivery(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    proposed = kernel.submit({"type": "record_model_proposal", "world_id": started.world_id, "proposal_id": "shareable", "entity_id": "zhizhi", "template_id": "dorm_chat", "content": "晚饭后在宿舍聊了几句新书。", "activity_id": "shareable-activity", "location": "华师大宿舍", "starts_at": (NOW - timedelta(hours=1)).isoformat(), "ends_at": NOW.isoformat(), "npc_id": "roommate-lin"}, expected_revision=started.revision)
    kernel.submit({"type": "accept_model_proposal", "world_id": started.world_id, "proposal_id": "shareable"}, expected_revision=proposed.revision)
    selected = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1))
    again = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1))
    assert selected is not None and again is not None
    assert selected.delivery_id == again.delivery_id


def test_delivered_life_share_consumes_daily_limit(tmp_path: Path) -> None:
    store = CompanionStore(tmp_path / "world.sqlite")
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    proposed = kernel.submit({"type": "record_model_proposal", "world_id": started.world_id, "proposal_id": "once", "entity_id": "zhizhi", "template_id": "dorm_chat", "content": "晚饭后在宿舍聊了几句新书。", "activity_id": "once-activity", "location": "华师大宿舍", "starts_at": (NOW - timedelta(hours=1)).isoformat(), "ends_at": NOW.isoformat(), "npc_id": "roommate-lin"}, expected_revision=started.revision)
    kernel.submit({"type": "accept_model_proposal", "world_id": started.world_id, "proposal_id": "once"}, expected_revision=proposed.revision)
    delivery_id, _, _ = kernel.queue_outgoing_action(canonical_user_id="geoff", platform="qq", text="分享。", kind="life_event", expires_at=NOW + timedelta(hours=1), trace={"world_id": started.world_id, "appraisal": "life_event_share", "expression_policy": "只分享已提交经历。", "allowed_facts": [], "experience_id": "once", "short_lived_constraint": None, "observable_reason": "测试。"})
    kernel.settle_outgoing_action(delivery_id, delivered=True)
    assert kernel.snapshot(started.world_id)["experiences"]["once"]["shared"] is True
    assert kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=2)) is None


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
    proposed = kernel.submit({"type": "record_model_proposal", "world_id": started.world_id, "proposal_id": "atomic-share", "entity_id": "zhizhi", "template_id": "dorm_chat", "content": "晚饭后在宿舍聊了几句新书。", "activity_id": "atomic-activity", "location": "华师大宿舍", "starts_at": (NOW - timedelta(hours=1)).isoformat(), "ends_at": NOW.isoformat(), "npc_id": "roommate-lin"}, expected_revision=started.revision)
    kernel.submit({"type": "accept_model_proposal", "world_id": started.world_id, "proposal_id": "atomic-share"}, expected_revision=proposed.revision)

    delivery = kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=1))
    assert delivery is not None
    assert kernel.snapshot(started.world_id)["actions"][delivery.action_id]["status"] == "scheduled"
    assert kernel.begin_outgoing_action(delivery.delivery_id) is True
    assert kernel.recover_interrupted_life_share_deliveries(started.world_id) == 1
    assert store.outbox_message(delivery.delivery_id)["status"] == "unknown"
    assert kernel.snapshot(started.world_id)["actions"][delivery.action_id]["status"] == "unknown"
    assert kernel.begin_outgoing_action(delivery.delivery_id) is False
    assert kernel.snapshot(started.world_id)["experiences"][delivery.experience_id].get("shared") is not True
    assert kernel.schedule_life_share_delivery(world_id=started.world_id, canonical_user_id="geoff", platform="qq", expires_at=NOW + timedelta(hours=2)) is None


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
