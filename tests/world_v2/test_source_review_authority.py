from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from companion_daemon.world_v2.source_review_authority import (
    AuditedSourceReviewText,
    SourceReviewAttemptsExhausted,
    SourceReviewAuthority,
)
from companion_daemon.world_v2.deliberation import (
    ValidationTechnicalFailure,
    run_validation_review,
)


class _ImmediateLane:
    def __init__(self, raw: str, usage: object) -> None:
        self.raw = raw
        self.usage = usage
        self.calls: list[str] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        self.calls.append("complete")
        return self.raw

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        self.calls.append("complete_json")
        return self.raw

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls.append("complete_with_usage")
        return self.raw, self.usage

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls.append("complete_json_with_usage")
        return self.raw, self.usage


class _BlockingLane:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_reasons: list[str] = []

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as exc:
            self.cancel_reasons.append(str(exc.args[0]) if exc.args else "")
            raise


class _BlockingThenImmediateLane(_BlockingLane):
    def __init__(self, raw: str, usage: object) -> None:
        super().__init__()
        self.raw = raw
        self.usage = usage
        self.calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls += 1
        if self.calls > 1:
            return self.raw, self.usage
        return await super().complete_json_with_usage([], temperature=0.0)


class _MonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class ModelCircuitOpenError(RuntimeError):
    pass


class _CircuitThenImmediateLane:
    def __init__(self, raw: str, usage: object) -> None:
        self.raw = raw
        self.usage = usage
        self.calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls += 1
        if self.calls == 1:
            raise ModelCircuitOpenError("provider circuit is open")
        return self.raw, self.usage


class _CancellationIgnoringLane:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.ignored_cancellation = asyncio.Event()
        self.release = asyncio.Event()

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.ignored_cancellation.set()
            await self.release.wait()
            return "late-primary", {"lane": "primary"}


class _RaisingLane:
    def __init__(self, detail: str) -> None:
        self.detail = detail
        self.calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls += 1
        raise ConnectionError(self.detail)


class _RaisingSpecificLane:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls += 1
        raise self.error


class _RoutedLane:
    def __init__(self, lane_name: str) -> None:
        self.lane_name = lane_name

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del temperature
        marker = messages[-1]["content"]
        requested_lane, call_id = marker.split(":", maxsplit=1)
        if requested_lane != self.lane_name:
            raise ConnectionError(f"{self.lane_name} unavailable for {call_id}")
        return f"{self.lane_name}:{call_id}", {
            "lane": self.lane_name,
            "call_id": call_id,
        }


class _ContractCapableLane(_ImmediateLane):
    def __init__(
        self,
        raw: str,
        usage: object,
        *,
        supported_contracts: frozenset[str],
    ) -> None:
        super().__init__(raw, usage)
        self.supported_contracts = supported_contracts

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in self.supported_contracts


def test_authority_advertises_only_contracts_supported_by_every_winning_lane() -> None:
    contract = "candidate-external-proposition-inventory.3"
    capable = _ContractCapableLane(
        "{}",
        {"lane": "primary"},
        supported_contracts=frozenset({contract}),
    )
    also_capable = _ContractCapableLane(
        "{}",
        {"lane": "secondary"},
        supported_contracts=frozenset({contract}),
    )
    plain = _ImmediateLane("{}", {"lane": "plain"})

    fully_capable = SourceReviewAuthority(
        primary=capable,
        secondary=also_capable,
        hedge_after_seconds=0.01,
        deadline_seconds=0.2,
    )
    mixed = SourceReviewAuthority(
        primary=capable,
        secondary=plain,
        hedge_after_seconds=0.01,
        deadline_seconds=0.2,
    )

    assert fully_capable.supports_strict_output_contract(contract) is True
    assert (
        fully_capable.wire_reselection_route().supports_strict_output_contract(
            contract
        )
        is True
    )
    assert mixed.supports_strict_output_contract(contract) is False
    assert fully_capable.supports_strict_output_contract("unknown-contract.1") is False


@pytest.mark.asyncio
async def test_primary_result_before_hedge_wins_without_starting_secondary() -> None:
    primary_usage: dict[str, Any] = {"lane": "primary"}
    primary = _ImmediateLane('{"supported":true}', primary_usage)
    secondary = _ImmediateLane('{"supported":false}', {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
    )

    result = await authority.complete_json_with_usage(
        [{"role": "user", "content": "review"}],
        temperature=0.0,
    )

    assert result == ('{"supported":true}', primary_usage)
    assert primary.calls == ["complete_json_with_usage"]
    assert secondary.calls == []
    assert authority.health_snapshot()["last_winner_lane"] == "primary"


@pytest.mark.asyncio
async def test_timed_out_primary_is_terminated_before_serial_secondary_starts() -> None:
    primary = _BlockingLane()
    secondary_usage = {"lane": "secondary"}
    secondary = _ImmediateLane('{"supported":true}', secondary_usage)
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.2,
    )

    result = await authority.complete_json_with_usage(
        [{"role": "user", "content": "review"}],
        temperature=0.0,
    )
    await asyncio.sleep(0)

    assert result == ('{"supported":true}', secondary_usage)
    assert isinstance(result[0], AuditedSourceReviewText)
    assert [
        (attempt.lane, attempt.outcome, attempt.model_id)
        for attempt in result[0].source_review_attempts
    ] == [
        ("primary", "timeout", "_BlockingLane"),
        ("secondary", "winner", "_ImmediateLane"),
    ]
    assert all(
        len(attempt.request_hash) == 64
        and attempt.model_call_id.startswith("model-call:source-review:")
        for attempt in result[0].source_review_attempts
    )
    assert result[0].source_review_attempts[0].response_hash is None
    assert (
        result[0].source_review_attempts[1].response_hash
        == "578b9d38ecc7e8d0e6a0fe4f7f72f8e98d6acf3efd465d0a499a0f9774d1581d"
    )
    assert primary.cancel_reasons == ["provider_timeout"]
    health = authority.health_snapshot()
    assert health["last_winner_lane"] == "secondary"
    assert health["review_strategy"] == "serial_failover"
    assert health["hedges_started"] == 1
    assert health["hedges_won"] == 1
    assert health["lane_failures"]["primary"] == 1
    assert health["billing_unknown"] == 1


@pytest.mark.asyncio
async def test_primary_timeout_is_suppressed_for_later_reviews_without_fabricated_attempts() -> None:
    clock = _MonotonicClock()
    primary = _BlockingLane()
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.2,
        monotonic_clock=clock,
    )

    first = await authority.complete_json_with_usage(
        [{"role": "user", "content": "first"}],
        temperature=0.0,
    )
    second = await authority.complete_json_with_usage(
        [{"role": "user", "content": "second"}],
        temperature=0.0,
    )

    assert [
        (attempt.lane, attempt.outcome)
        for attempt in first[0].source_review_attempts
    ] == [("primary", "timeout"), ("secondary", "winner")]
    assert [
        (attempt.lane, attempt.outcome)
        for attempt in second[0].source_review_attempts
    ] == [("secondary", "winner")]
    assert primary.cancel_reasons == ["provider_timeout"]
    assert secondary.calls == [
        "complete_json_with_usage",
        "complete_json_with_usage",
    ]
    health = authority.health_snapshot()
    assert health["route_suppression"] == {
        "primary": {
            "active": True,
            "reason": "provider_timeout",
            "retry_after_seconds": 600.0,
            "skipped_calls": 1,
        },
        "secondary": {
            "active": False,
            "reason": None,
            "retry_after_seconds": 0.0,
            "skipped_calls": 0,
        },
    }
    assert health["technical_failure_cooldown_seconds"] == 600.0


@pytest.mark.asyncio
async def test_suppressed_primary_is_reprobed_after_cooldown_and_success_clears_health() -> None:
    clock = _MonotonicClock()
    primary = _BlockingThenImmediateLane("primary-recovered", {"lane": "primary"})
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.2,
        monotonic_clock=clock,
    )

    await authority.complete_json_with_usage(
        [{"role": "user", "content": "failure"}],
        temperature=0.0,
    )
    clock.advance(599.0)
    await authority.complete_json_with_usage(
        [{"role": "user", "content": "still cooling"}],
        temperature=0.0,
    )
    clock.advance(1.0)
    recovered = await authority.complete_json_with_usage(
        [{"role": "user", "content": "reprobe"}],
        temperature=0.0,
    )

    assert recovered == ("primary-recovered", {"lane": "primary"})
    assert primary.calls == 2
    assert secondary.calls == [
        "complete_json_with_usage",
        "complete_json_with_usage",
    ]
    assert authority.health_snapshot()["route_suppression"]["primary"] == {
        "active": False,
        "reason": None,
        "retry_after_seconds": 0.0,
        "skipped_calls": 1,
    }


@pytest.mark.asyncio
async def test_primary_circuit_open_is_suppressed_and_concurrent_reviews_keep_backup_audit() -> None:
    clock = _MonotonicClock()
    primary = _CircuitThenImmediateLane("primary-recovered", {"lane": "primary"})
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
        monotonic_clock=clock,
    )

    initial = await authority.complete_json_with_usage(
        [{"role": "user", "content": "opens circuit"}],
        temperature=0.0,
    )
    concurrent = await asyncio.gather(
        *(
            authority.complete_json_with_usage(
                [{"role": "user", "content": f"review-{ordinal}"}],
                temperature=0.0,
            )
            for ordinal in range(8)
        )
    )

    assert [
        (attempt.lane, attempt.outcome, attempt.failure_code)
        for attempt in initial[0].source_review_attempts
    ] == [
        ("primary", "exception", "ModelCircuitOpenError"),
        ("secondary", "winner", None),
    ]
    assert all(
        [(attempt.lane, attempt.outcome) for attempt in result[0].source_review_attempts]
        == [("secondary", "winner")]
        for result in concurrent
    )
    assert primary.calls == 1
    health = authority.health_snapshot()
    assert health["route_suppression"]["primary"] == {
        "active": True,
        "reason": "ModelCircuitOpenError",
        "retry_after_seconds": 600.0,
        "skipped_calls": 8,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (httpx.ReadTimeout("read timed out"), "ReadTimeout"),
        (httpx.ConnectTimeout("connect timed out"), "ConnectTimeout"),
        (TimeoutError("async transport timed out"), "TimeoutError"),
    ),
)
async def test_direct_transport_timeout_categories_enter_route_cooldown(
    error: BaseException,
    reason: str,
) -> None:
    clock = _MonotonicClock()
    primary = _RaisingSpecificLane(error)
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
        monotonic_clock=clock,
    )

    initial = await authority.complete_json_with_usage(
        [{"role": "user", "content": "transport timeout"}],
        temperature=0.0,
    )
    repeated = await authority.complete_json_with_usage(
        [{"role": "user", "content": "same cooling route"}],
        temperature=0.0,
    )

    assert initial[0].source_review_attempts[0].failure_code == reason
    assert [
        (attempt.lane, attempt.outcome)
        for attempt in repeated[0].source_review_attempts
    ] == [("secondary", "winner")]
    assert primary.calls == 1
    assert authority.health_snapshot()["route_suppression"]["primary"] == {
        "active": True,
        "reason": reason,
        "retry_after_seconds": 600.0,
        "skipped_calls": 1,
    }


@pytest.mark.asyncio
async def test_http_403_route_rejection_uses_inventory_compatible_cooldown() -> None:
    clock = _MonotonicClock()
    request = httpx.Request("POST", "https://reviewer.invalid/v1/chat/completions")
    error = httpx.HTTPStatusError(
        "forbidden",
        request=request,
        response=httpx.Response(403, request=request),
    )
    primary = _RaisingSpecificLane(error)
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
        monotonic_clock=clock,
    )

    await authority.complete_json_with_usage(
        [{"role": "user", "content": "route rejected"}],
        temperature=0.0,
    )
    repeated = await authority.complete_json_with_usage(
        [{"role": "user", "content": "route still cooling"}],
        temperature=0.0,
    )

    assert [
        (attempt.lane, attempt.outcome)
        for attempt in repeated[0].source_review_attempts
    ] == [("secondary", "winner")]
    assert primary.calls == 1
    assert authority.health_snapshot()["route_suppression"]["primary"] == {
        "active": True,
        "reason": "http_403",
        "retry_after_seconds": 600.0,
        "skipped_calls": 1,
    }


@pytest.mark.asyncio
async def test_cancellation_ignoring_primary_never_overlaps_the_reserve() -> None:
    primary = _CancellationIgnoringLane()
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.3,
    )

    try:
        with pytest.raises(SourceReviewAttemptsExhausted) as caught:
            await authority.complete_json_with_usage(
                [{"role": "user", "content": "review"}],
                temperature=0.0,
            )
        assert caught.value.lane_failures == {"primary": "provider_timeout"}
        assert primary.ignored_cancellation.is_set()
        assert secondary.calls == []
    finally:
        primary.release.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_close_is_bounded_while_timed_out_reviewer_retains_shutdown_lease() -> None:
    primary = _CancellationIgnoringLane()
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=_ImmediateLane("secondary", {"lane": "secondary"}),
        hedge_after_seconds=0.001,
        deadline_seconds=0.3,
    )

    try:
        with pytest.raises(SourceReviewAttemptsExhausted):
            await authority.complete_json_with_usage(
                [{"role": "user", "content": "review"}],
                temperature=0.0,
            )
        await asyncio.wait_for(primary.ignored_cancellation.wait(), timeout=1)

        await asyncio.wait_for(authority.aclose(), timeout=0.2)

        assert authority.shutdown_pending_task_count == 1
        quiescence = asyncio.create_task(authority.wait_for_shutdown_quiescence())
        await asyncio.sleep(0)
        assert quiescence.done() is False
    finally:
        primary.release.set()

    await asyncio.wait_for(quiescence, timeout=1)
    assert authority.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_caller_cancelled_reviewer_retains_shutdown_lease_until_it_exits() -> None:
    primary = _CancellationIgnoringLane()
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=_ImmediateLane("secondary", {"lane": "secondary"}),
        hedge_after_seconds=0.2,
        deadline_seconds=0.3,
    )
    running = asyncio.create_task(
        authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        )
    )
    await asyncio.wait_for(primary.started.wait(), timeout=1)

    try:
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        await asyncio.wait_for(primary.ignored_cancellation.wait(), timeout=1)

        await asyncio.wait_for(authority.aclose(), timeout=0.2)

        assert authority.shutdown_pending_task_count == 1
    finally:
        primary.release.set()

    await asyncio.wait_for(authority.wait_for_shutdown_quiescence(), timeout=1)
    assert authority.shutdown_pending_task_count == 0


@pytest.mark.asyncio
async def test_all_explicit_lane_failures_raise_validation_exhaustion() -> None:
    authority = SourceReviewAuthority(
        primary=_RaisingLane("primary unavailable"),
        secondary=_RaisingLane("secondary unavailable"),
        hedge_after_seconds=10.0,
        deadline_seconds=10.2,
    )

    with pytest.raises(SourceReviewAttemptsExhausted) as failure:
        await asyncio.wait_for(
            authority.complete_json_with_usage(
                [{"role": "user", "content": "review"}],
                temperature=0.0,
            ),
            timeout=0.2,
        )

    assert failure.value.validation_attempts_exhausted is True
    assert failure.value.lane_failures == {
        "primary": "ConnectionError",
        "secondary": "ConnectionError",
    }
    assert failure.value.model_call_id.startswith("model-call:source-review:")
    assert len(failure.value.request_hash) == 64
    assert failure.value.attempted_model_id == "_RaisingLane"
    assert failure.value.attempted_model_version == "_RaisingLane"
    assert [
        (attempt.lane, attempt.outcome)
        for attempt in failure.value.source_review_attempts
    ] == [
        ("primary", "exception"),
        ("secondary", "exception"),
    ]
    health = authority.health_snapshot()
    assert health["all_lanes_failed"] == 1
    assert health["billing_unknown"] == 2
    assert health["lane_failures"] == {"primary": 1, "secondary": 1}
    assert health["last_lane_failure_reasons"] == {
        "primary": "ConnectionError",
        "secondary": "ConnectionError",
    }


@pytest.mark.asyncio
async def test_caller_cancellation_retains_ambiguous_provider_attempt_without_leaking_text() -> None:
    primary = _BlockingLane()
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=4.0,
        deadline_seconds=20.0,
    )
    running = asyncio.create_task(
        authority.complete_json_with_usage(
            [{"role": "user", "content": "private review payload"}],
            temperature=0.0,
        )
    )
    await primary.started.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await running
    await asyncio.sleep(0)

    attempts = caught.value.source_review_attempts
    assert [
        (attempt.lane, attempt.outcome, attempt.model_id)
        for attempt in attempts
    ] == [("primary", "timeout", "_BlockingLane")]
    assert primary.cancel_reasons == ["caller_cancelled"]
    assert secondary.calls == []
    health = authority.health_snapshot()
    assert health["billing_unknown"] == 1
    assert health["lane_failures"] == {"primary": 1, "secondary": 0}
    assert health["last_lane_failure_reasons"]["primary"] == "caller_cancelled"


@pytest.mark.asyncio
async def test_health_failure_category_never_contains_provider_exception_text() -> None:
    secret = "Bearer sk-test private-payload"
    authority = SourceReviewAuthority(
        primary=_RaisingLane(secret),
        secondary=_ImmediateLane("secondary", {"lane": "secondary"}),
        hedge_after_seconds=1.0,
        deadline_seconds=2.0,
    )

    result = await authority.complete_json_with_usage(
        [{"role": "user", "content": "review"}],
        temperature=0.0,
    )

    assert result[0] == "secondary"
    health_json = str(authority.health_snapshot())
    assert secret not in health_json
    assert "sk-test" not in health_json
    assert authority.health_snapshot()["last_lane_failure_reasons"]["primary"] == (
        "ConnectionError"
    )


@pytest.mark.asyncio
async def test_exhausted_authority_is_not_retried_as_another_full_lane_race() -> None:
    primary = _RaisingLane("primary unavailable")
    secondary = _RaisingLane("secondary unavailable")
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=10.0,
        deadline_seconds=10.2,
    )

    async def review() -> tuple[str, object]:
        return await authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        )

    with pytest.raises(ValidationTechnicalFailure) as failure:
        await run_validation_review(review, timeout_seconds=22.0)

    assert failure.value.failure_code == "source_review_exception"
    assert failure.value.model_call_id is not None
    assert failure.value.attempted_model_id == "_RaisingLane"
    assert primary.calls == 1
    assert secondary.calls == 1


@pytest.mark.asyncio
async def test_authority_deadline_reports_timeout_before_outer_validation_retry() -> None:
    primary = _BlockingLane()
    secondary = _BlockingLane()
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.02,
    )

    async def review() -> tuple[str, object]:
        return await authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        )

    with pytest.raises(ValidationTechnicalFailure) as failure:
        await run_validation_review(review, timeout_seconds=0.2)
    await asyncio.sleep(0)

    assert failure.value.failure_code == "source_review_timeout"
    assert primary.cancel_reasons == ["provider_timeout"]
    assert secondary.cancel_reasons == ["provider_timeout"]
    assert authority.health_snapshot()["all_lanes_failed"] == 1


@pytest.mark.asyncio
async def test_caller_bound_authority_exhausts_before_validation_wrapper_timeout() -> None:
    primary = _BlockingLane()
    secondary = _BlockingLane()
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.005,
        deadline_seconds=0.2,
        caller_timeout_seconds=0.04,
    )

    async def review() -> tuple[str, object]:
        return await authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        )

    with pytest.raises(ValidationTechnicalFailure) as failure:
        await run_validation_review(review, timeout_seconds=0.04)
    await asyncio.sleep(0)

    assert failure.value.failure_code == "source_review_timeout"
    assert bool(
        getattr(
            failure.value.__cause__,
            "validation_attempts_exhausted",
            False,
        )
    )
    assert primary.cancel_reasons == ["provider_timeout"]
    assert secondary.cancel_reasons == ["provider_timeout"]
    assert authority.health_snapshot()["all_lanes_failed"] == 1
    assert authority.deadline_seconds < 0.04


@pytest.mark.asyncio
async def test_shared_deadline_cancels_both_lanes_as_provider_timeouts() -> None:
    primary = _BlockingLane()
    secondary = _BlockingLane()
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.001,
        deadline_seconds=0.02,
    )

    with pytest.raises(SourceReviewAttemptsExhausted) as failure:
        await authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        )
    await asyncio.sleep(0)

    assert failure.value.lane_failures == {
        "primary": "provider_timeout",
        "secondary": "provider_timeout",
    }
    assert primary.cancel_reasons == ["provider_timeout"]
    assert secondary.cancel_reasons == ["provider_timeout"]
    health = authority.health_snapshot()
    assert health["all_lanes_failed"] == 1
    assert health["billing_unknown"] == 2
    assert health["lane_failures"] == {"primary": 1, "secondary": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("complete", "primary"),
        ("complete_json", "primary"),
        ("complete_with_usage", ("primary", {"tokens": 7})),
        ("complete_json_with_usage", ("primary", {"tokens": 7})),
    ],
)
async def test_all_chat_model_interfaces_preserve_the_winner_result(
    method_name: str,
    expected: object,
) -> None:
    primary = _ImmediateLane("primary", {"tokens": 7})
    secondary = _ImmediateLane("secondary", {"tokens": 9})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
    )

    method = getattr(authority, method_name)
    result = await method(
        [{"role": "user", "content": "review"}],
        temperature=0.3,
    )

    assert result == expected
    assert primary.calls == [method_name]
    assert secondary.calls == []


@pytest.mark.asyncio
async def test_primary_exception_starts_secondary_without_waiting_for_hedge() -> None:
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=_RaisingLane("primary unavailable"),
        secondary=secondary,
        hedge_after_seconds=10.0,
        deadline_seconds=10.2,
    )

    result = await asyncio.wait_for(
        authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        ),
        timeout=0.2,
    )

    assert result == ("secondary", {"lane": "secondary"})
    assert secondary.calls == ["complete_json_with_usage"]
    assert authority.health_snapshot()["lane_failures"]["primary"] == 1


@pytest.mark.asyncio
async def test_missing_primary_usage_is_a_lane_failure_not_a_winner() -> None:
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=_ImmediateLane("primary", None),
        secondary=secondary,
        hedge_after_seconds=10.0,
        deadline_seconds=10.2,
    )

    result = await asyncio.wait_for(
        authority.complete_json_with_usage(
            [{"role": "user", "content": "review"}],
            temperature=0.0,
        ),
        timeout=0.2,
    )

    assert result == ("secondary", {"lane": "secondary"})
    assert authority.health_snapshot()["lane_failures"]["primary"] == 1


@pytest.mark.asyncio
async def test_healthy_primary_never_starts_the_secondary_reviewer() -> None:
    primary = _ImmediateLane("primary", {"lane": "primary"})
    secondary = _ImmediateLane("secondary", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
    )

    result = await authority.complete_json_with_usage(
        [{"role": "user", "content": "review"}],
        temperature=0.0,
    )

    assert result == ("primary", {"lane": "primary"})
    assert primary.calls == ["complete_json_with_usage"]
    assert secondary.calls == []
    assert authority.health_snapshot()["last_winner_lane"] == "primary"


@pytest.mark.asyncio
async def test_wire_reselection_tries_the_other_independent_lane_first() -> None:
    primary = _ImmediateLane("primary-invalid", {"lane": "primary"})
    secondary = _ImmediateLane("secondary-valid", {"lane": "secondary"})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.05,
        deadline_seconds=0.2,
    )

    result = await authority.wire_reselection_route().complete_json_with_usage(
        [{"role": "user", "content": "same-review-contract"}],
        temperature=0.0,
    )

    assert result == ("secondary-valid", {"lane": "secondary"})
    assert primary.calls == []
    assert secondary.calls == ["complete_json_with_usage"]
    assert authority.health_snapshot()["last_winner_lane"] == "secondary"


def test_calls_from_multiple_threads_keep_attempt_state_isolated() -> None:
    authority = SourceReviewAuthority(
        primary=_RoutedLane("primary"),
        secondary=_RoutedLane("secondary"),
        hedge_after_seconds=0.01,
        deadline_seconds=0.5,
    )
    markers = [
        f"{lane}:{index}"
        for index in range(24)
        for lane in ("primary", "secondary")
    ]

    def invoke(marker: str) -> tuple[str, object]:
        return asyncio.run(
            authority.complete_json_with_usage(
                [{"role": "user", "content": marker}],
                temperature=0.0,
            )
        )

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(invoke, markers))

    assert results == [
        (
            marker,
            {
                "lane": marker.split(":", maxsplit=1)[0],
                "call_id": marker.split(":", maxsplit=1)[1],
            },
        )
        for marker in markers
    ]
    health = authority.health_snapshot()
    assert health["hedges_started"] == 24
    assert health["hedges_won"] == 24
    assert health["lane_failures"] == {"primary": 24, "secondary": 0}


def test_health_snapshot_is_read_only_and_does_not_call_reviewers() -> None:
    primary = _ImmediateLane("primary", {"tokens": 1})
    secondary = _ImmediateLane("secondary", {"tokens": 1})
    authority = SourceReviewAuthority(
        primary=primary,
        secondary=secondary,
        hedge_after_seconds=0.01,
        deadline_seconds=0.2,
    )

    snapshot = authority.health_snapshot()
    snapshot["lane_failures"]["primary"] = 999

    assert authority.health_snapshot() == {
        "configured_lanes": ("primary", "secondary"),
        "lane_models": {
            "primary": "_ImmediateLane",
            "secondary": "_ImmediateLane",
        },
        "lane_providers": {
            "primary": "unknown",
            "secondary": "unknown",
        },
        "lane_capability_evidence": {
            "primary": None,
            "secondary": None,
        },
        "hedge_after_seconds": 0.01,
        "review_strategy": "serial_failover",
        "primary_attempt_timeout_seconds": 0.01,
        "configured_absolute_timeout_seconds": 0.2,
        "absolute_timeout_seconds": 0.2,
        "caller_timeout_seconds": None,
        "terminal_completion_reserve_seconds": 0.0,
        "last_winner_lane": None,
        "hedges_started": 0,
        "hedges_won": 0,
        "all_lanes_failed": 0,
        "billing_unknown": 0,
        "lane_failures": {"primary": 0, "secondary": 0},
        "last_lane_failure_reasons": {"primary": None, "secondary": None},
        "technical_failure_cooldown_seconds": 600.0,
        "route_suppression": {
            "primary": {
                "active": False,
                "reason": None,
                "retry_after_seconds": 0.0,
                "skipped_calls": 0,
            },
            "secondary": {
                "active": False,
                "reason": None,
                "retry_after_seconds": 0.0,
                "skipped_calls": 0,
            },
        },
    }
    assert primary.calls == []
    assert secondary.calls == []
