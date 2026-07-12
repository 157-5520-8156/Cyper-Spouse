from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_cost_ledger import (
    ALL_COST_CATEGORIES,
    CostPolicy,
    CostRequest,
    SocialTransgressionPolicy,
    SocialTransgressionRecord,
    WorldCostLedger,
    evaluate_social_transgression,
)


def _policy() -> CostPolicy:
    return CostPolicy(
        daily_budget_units=100,
        automatic_daily_budget_units=60,
        category_daily_budget_units={"chat": 70, "image": 50},
        category_automatic_daily_budget_units={"chat": 50, "image": 30},
    )


def test_reservations_share_one_logical_day_budget_and_manual_can_cross_soft_limit() -> None:
    ledger = WorldCostLedger(_policy())

    first = ledger.reserve(
        CostRequest("chat:auto:1", "chat", "2032-04-03", 45, automatic=True)
    )
    blocked = ledger.reserve(
        CostRequest("chat:auto:2", "chat", "2032-04-03", 10, automatic=True)
    )
    manual = ledger.reserve(
        CostRequest("chat:manual:1", "chat", "2032-04-03", 20, automatic=False)
    )

    assert first.allowed is True
    assert blocked.allowed is False
    assert blocked.reason == "category_automatic_daily_budget_exceeded"
    assert manual.allowed is True
    assert ledger.usage("2032-04-03", "chat").reserved_units == 65


def test_reservation_retry_is_idempotent_and_payload_conflicts_are_rejected() -> None:
    ledger = WorldCostLedger(_policy())
    request = CostRequest("vision:message-7", "vision", "2032-04-03", 18, automatic=True)

    original = ledger.reserve(request)
    retry = ledger.reserve(request)

    assert retry == original
    assert ledger.usage("2032-04-03", "vision").reserved_units == 18
    with pytest.raises(ValueError, match="idempotency key reused with different request"):
        ledger.reserve(
            CostRequest("vision:message-7", "vision", "2032-04-03", 19, automatic=True)
        )


def test_settlement_releases_unused_reservation_and_is_idempotent() -> None:
    ledger = WorldCostLedger(_policy())
    reserved = ledger.reserve(
        CostRequest("chat:turn-1", "chat", "2032-04-03", 40, automatic=False)
    )

    settled = ledger.settle(
        reserved.reservation_id,
        actual_units=25,
        idempotency_key="settle:chat:turn-1",
    )
    retry = ledger.settle(
        reserved.reservation_id,
        actual_units=25,
        idempotency_key="settle:chat:turn-1",
    )

    assert settled == retry
    assert settled.charged_units == 25
    assert ledger.usage("2032-04-03", "chat").reserved_units == 0
    assert ledger.usage("2032-04-03", "chat").settled_units == 25
    assert ledger.reserve(
        CostRequest("chat:turn-2", "chat", "2032-04-03", 45, automatic=False)
    ).allowed


def test_settlement_cannot_charge_more_than_reserved() -> None:
    ledger = WorldCostLedger(_policy())
    reservation = ledger.reserve(
        CostRequest("tool:proposal-1", "tool", "2032-04-03", 12, automatic=False)
    )

    with pytest.raises(ValueError, match="actual units exceed reservation"):
        ledger.settle(
            reservation.reservation_id,
            actual_units=13,
            idempotency_key="settle:tool:proposal-1",
        )


def test_cache_hit_reuses_settled_result_without_a_second_charge() -> None:
    ledger = WorldCostLedger(_policy())
    reservation = ledger.reserve(
        CostRequest(
            "vision:message-8",
            "vision",
            "2032-04-03",
            18,
            automatic=True,
            cache_key="user-1:sha256:abc",
        )
    )
    ledger.settle(
        reservation.reservation_id,
        actual_units=16,
        idempotency_key="settle:vision:message-8",
        result_ref="attachment-insight:abc",
    )

    reused = ledger.reserve(
        CostRequest(
            "vision:message-9",
            "vision",
            "2032-04-04",
            18,
            automatic=True,
            cache_key="user-1:sha256:abc",
        )
    )

    assert reused.allowed is True
    assert reused.reason == "cache_reused"
    assert reused.reused is True
    assert reused.reused_result_ref == "attachment-insight:abc"
    assert reused.charge_units == 0
    assert reused.reservation_id is None
    assert ledger.usage("2032-04-04", "vision").total_units == 0


def test_cache_keys_are_namespaced_by_cost_category() -> None:
    ledger = WorldCostLedger(_policy())
    vision = ledger.reserve(
        CostRequest("vision:1", "vision", "2032-04-03", 10, False, "content:abc")
    )
    ledger.settle(
        vision.reservation_id,
        actual_units=10,
        idempotency_key="settle:vision:1",
        result_ref="vision-result:1",
    )

    audio = ledger.reserve(
        CostRequest("audio:1", "audio", "2032-04-03", 10, False, "content:abc")
    )

    assert audio.reason == "reserved"
    assert audio.charge_units == 10


def test_all_required_external_work_categories_use_the_same_ledger() -> None:
    ledger = WorldCostLedger(
        CostPolicy(daily_budget_units=100, automatic_daily_budget_units=100)
    )

    for category in ALL_COST_CATEGORIES:
        decision = ledger.reserve(
            CostRequest(f"{category}:1", category, f"2032-04-{len(category):02d}", 1, True)
        )
        assert decision.allowed, category

    assert ALL_COST_CATEGORIES == (
        "chat",
        "repair",
        "audit",
        "proactive",
        "vision",
        "audio",
        "image",
        "tool",
    )


def test_releasing_unstarted_work_returns_reserved_capacity() -> None:
    ledger = WorldCostLedger(_policy())
    reservation = ledger.reserve(
        CostRequest("image:cancelled", "image", "2032-04-03", 30, True)
    )

    released = ledger.release(
        reservation.reservation_id,
        idempotency_key="release:image:cancelled",
        reason="action_cancelled_before_dispatch",
    )

    assert released.reason == "action_cancelled_before_dispatch"
    assert released.charged_units == 0
    assert ledger.usage("2032-04-03", "image").total_units == 0


def test_exported_events_replay_budget_cache_and_command_receipts_deterministically() -> None:
    ledger = WorldCostLedger(_policy())
    cached = ledger.reserve(
        CostRequest("vision:seed", "vision", "2032-04-03", 20, True, "user-1:file")
    )
    ledger.settle(
        cached.reservation_id,
        actual_units=17,
        idempotency_key="settle:vision:seed",
        result_ref="insight:1",
    )
    rejected_request = CostRequest("image:over", "image", "2032-04-03", 31, True)
    assert ledger.reserve(rejected_request).allowed is False
    events = ledger.export_events()

    replayed = WorldCostLedger.from_events(_policy(), events)

    assert replayed.export_events() == events
    assert replayed.usage("2032-04-03", "vision").settled_units == 17
    assert replayed.reserve(rejected_request).allowed is False
    assert len(replayed.export_events()) == len(events)
    reused = replayed.reserve(
        CostRequest("vision:reuse", "vision", "2032-04-04", 20, True, "user-1:file")
    )
    assert reused.reused_result_ref == "insight:1"


def test_social_transgression_budget_enforces_cooldown_and_logical_day_strikes() -> None:
    policy = SocialTransgressionPolicy(daily_strike_budget=3, cooldown=timedelta(hours=6))
    morning = datetime(2032, 4, 3, 9, tzinfo=UTC)

    first = evaluate_social_transgression(policy, (), logical_at=morning, requested_strikes=2)
    history = (SocialTransgressionRecord("override:1", morning, first.strikes_charged),)
    too_soon = evaluate_social_transgression(
        policy,
        history,
        logical_at=morning + timedelta(hours=2),
        requested_strikes=1,
    )
    over_day_budget = evaluate_social_transgression(
        policy,
        history,
        logical_at=morning + timedelta(hours=7),
        requested_strikes=2,
    )
    next_day = evaluate_social_transgression(
        policy,
        history,
        logical_at=morning + timedelta(hours=25),
        requested_strikes=2,
    )

    assert first.allowed is True
    assert first.reason == "social_risk_budget_available"
    assert too_soon.allowed is False
    assert too_soon.reason == "transgression_cooldown"
    assert too_soon.cooldown_remaining_seconds == 4 * 3600
    assert over_day_budget.allowed is False
    assert over_day_budget.reason == "daily_transgression_strike_budget_exceeded"
    assert next_day.allowed is True


def test_invalid_cost_request_is_rejected_before_it_can_change_capacity() -> None:
    ledger = WorldCostLedger(_policy())

    with pytest.raises(ValueError, match="unknown cost category"):
        ledger.reserve(CostRequest("bad:1", "other", "2032-04-03", 10, True))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="units must be positive"):
        ledger.reserve(CostRequest("bad:2", "chat", "2032-04-03", -10, True))

    assert ledger.usage("2032-04-03").total_units == 0
    assert ledger.export_events() == ()


def test_hard_budget_reason_takes_precedence_when_soft_and_hard_are_both_exceeded() -> None:
    ledger = WorldCostLedger(_policy())

    decision = ledger.reserve(
        CostRequest("image:far-over", "image", "2032-04-03", 51, automatic=True)
    )

    assert decision.allowed is False
    assert decision.reason == "category_daily_budget_exceeded"


def test_duplicate_transgression_records_are_idempotent_but_conflicts_fail() -> None:
    policy = SocialTransgressionPolicy(daily_strike_budget=3, cooldown=timedelta(0))
    morning = datetime(2032, 4, 3, 9, tzinfo=UTC)
    record = SocialTransgressionRecord("override:1", morning, 1)

    decision = evaluate_social_transgression(
        policy,
        (record, record),
        logical_at=morning + timedelta(hours=1),
        requested_strikes=2,
    )

    assert decision.allowed is True
    with pytest.raises(ValueError, match="idempotency key reused with different transgression"):
        evaluate_social_transgression(
            policy,
            (record, SocialTransgressionRecord("override:1", morning, 2)),
            logical_at=morning + timedelta(hours=1),
            requested_strikes=1,
        )
