"""Regression tests for the 2026-07-20 fallback-rate reduction work.

Covers four seams measured in production as the dominant failsafe causes:
attempt-deadline awareness for corrective retries, corrective coverage for
non-claim structural rejects, the pre-failsafe violation-quoting retry, and
the timestamped failover-usage check that stops cross-lane flag contamination
from skipping a legitimate backup attempt.
"""

from __future__ import annotations

import json
import time
from time import monotonic

import httpx
import pytest

from companion_daemon.llm import (
    DeepSeekChatModel,
    FailoverChatModel,
    OpenAICompatibleChatModel,
)
from companion_daemon.world_v2 import production_reliability_metrics as metrics
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import (
    Deliberation,
    ModelInput,
    ModelOutput,
    fit_secondary_call_timeout,
    remaining_attempt_seconds,
)
from companion_daemon.world_v2.single_call_inbound_cognition import (
    SingleCallInboundCognition,
    _provider_already_used_fallback,
)
from test_deliberation import _Quick, _Router, _capsule, _decision_raw
from test_single_call_inbound_cognition import _request


@pytest.fixture(autouse=True)
def _reset_reliability_counters():
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


# --- attempt-deadline plumbing -------------------------------------------------


def test_fit_secondary_call_timeout_returns_default_outside_an_attempt() -> None:
    assert remaining_attempt_seconds() is None
    assert fit_secondary_call_timeout(8.0) == 8.0


class _DeadlineProbeMain:
    def __init__(self) -> None:
        self.remaining: list[float | None] = []
        self.fitted: list[float | None] = []

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.remaining.append(remaining_attempt_seconds())
        self.fitted.append(fit_secondary_call_timeout(8.0))
        return ModelOutput(model_id="main", model_version="v1", raw_proposal=_decision_raw())


class _DeadlineProbeQuick:
    def __init__(self) -> None:
        self.remaining: list[float | None] = []

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        self.remaining.append(remaining_attempt_seconds())
        raise RuntimeError("probe only")


@pytest.mark.asyncio
async def test_deliberation_installs_the_main_attempt_deadline_for_adapters() -> None:
    main = _DeadlineProbeMain()
    await Deliberation(
        router=_Router(),
        main_model=main,
        quick_recovery=_Quick(),
        main_timeout_seconds=5.0,
    ).deliberate(_capsule(), attempt_id="attempt:deadline-probe")

    assert len(main.remaining) == 1
    assert main.remaining[0] is not None and 0.0 < main.remaining[0] <= 5.0
    # A corrective retry must be capped below the remaining attempt budget.
    assert main.fitted[0] is not None and main.fitted[0] < 5.0
    # Outside the attempt the contextvar is cleaned up again.
    assert remaining_attempt_seconds() is None


@pytest.mark.asyncio
async def test_deliberation_installs_the_quick_attempt_deadline_for_recovery() -> None:
    class _FailingMain:
        async def propose(self, request: ModelInput) -> ModelOutput:
            raise RuntimeError("main down")

    quick = _DeadlineProbeQuick()
    result = await Deliberation(
        router=_Router(),
        main_model=_FailingMain(),
        quick_recovery=quick,
        quick_timeout_seconds=3.0,
    ).deliberate(_capsule(), attempt_id="attempt:quick-deadline-probe")

    assert result.audit.status == "recovery_failed"
    assert len(quick.remaining) == 1
    assert quick.remaining[0] is not None and 0.0 < quick.remaining[0] <= 3.0


def test_fit_secondary_call_timeout_skips_when_no_useful_budget_remains() -> None:
    from companion_daemon.world_v2 import deliberation as deliberation_module

    token = deliberation_module._ATTEMPT_DEADLINE.set(time.monotonic() + 1.0)
    try:
        assert fit_secondary_call_timeout(8.0) is None
        assert fit_secondary_call_timeout(8.0, minimum_seconds=0.1) is not None
    finally:
        deliberation_module._ATTEMPT_DEADLINE.reset(token)


# --- reliability counters ------------------------------------------------------


def test_reliability_snapshot_counts_and_rate() -> None:
    metrics.record_visible_reply()
    metrics.record_visible_reply()
    metrics.record_visible_reply()
    metrics.record_failsafe()
    metrics.record_claim_repair()
    metrics.record_shape_repair()
    metrics.record_claim_free_reply()
    metrics.record_backup_recovery()

    snapshot = metrics.reliability_snapshot()

    assert snapshot["window_hours"] == 24
    assert snapshot["visible_replies_24h"] == 3
    assert snapshot["failsafe_24h"] == 1
    assert snapshot["failsafe_rate_24h"] == round(1 / 3, 4)
    assert snapshot["claim_repair_24h"] == 1
    assert snapshot["shape_repair_24h"] == 1
    assert snapshot["claim_free_24h"] == 1
    assert snapshot["backup_recovery_24h"] == 1
    assert isinstance(snapshot["since"], str)


def test_reliability_snapshot_prunes_entries_older_than_the_window() -> None:
    metrics.record_failsafe()
    metrics._events["failsafe"].appendleft(time.time() - 25 * 3600)

    snapshot = metrics.reliability_snapshot()

    assert snapshot["failsafe_24h"] == 1
    assert snapshot["failsafe_rate_24h"] is None  # no visible replies recorded


# --- corrective coverage for non-claim structural rejects ----------------------


_VALID_APPRAISAL = {
    "appraise": False,
    "brief_rationale": "No material emotional shift.",
    "behavior_tendency": "observe",
    "stance": "wait",
    "display_strategy": "withhold",
    "confidence": 3000,
}

# A beat with an undeclared extra key defeats both the strict materializer and
# the bounded structural normalizer, producing a non-claim shape violation.
_BROKEN_SHAPE_EXPRESSION = {
    "timing_choice": "now",
    "beats": [{"modality": "text", "text": "我在的。", "note": "extra"}],
    "stance": "attentive",
    "brief_rationale": "Stay with the current conversation.",
    "confidence": 7200,
    "world_claims": [],
}

_CORRECTED_EXPRESSION = {
    "timing_choice": "now",
    "beats": [{"modality": "text", "text": "我在的，这句我接住了。"}],
    "stance": "attentive",
    "brief_rationale": "Corrected the beat shape only.",
    "confidence": 7200,
    "world_claims": [],
}


class _ShapeRepairedCombinedProvider:
    """First combined pass returns a broken beat shape; the corrective fixes it."""

    model = "combined-flash"

    def __init__(self, *, corrected_on_call: int = 2) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self._corrected_on_call = corrected_on_call

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        del temperature
        self.calls.append(messages)
        expression = (
            _CORRECTED_EXPRESSION
            if len(self.calls) >= self._corrected_on_call
            else _BROKEN_SHAPE_EXPRESSION
        )
        return json.dumps(
            {"appraisal_draft": _VALID_APPRAISAL, "expression_draft": expression},
            ensure_ascii=False,
        )


@pytest.mark.asyncio
async def test_paired_shape_reject_is_repaired_with_violation_feedback() -> None:
    provider = _ShapeRepairedCombinedProvider(corrected_on_call=2)
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:paired-shape-repair")

    await cognition.appraisal.propose(request)
    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:paired-shape-repair-expression"})
    )

    # One combined pass plus exactly one corrective retry; the cached
    # expression is then materialized without another provider call.
    assert len(provider.calls) == 2
    corrective = provider.calls[1][-1]["content"]
    assert "structural validation" in corrective
    assert "note" in corrective  # quotes the concrete violation
    assert "接住" in json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert expression.model_id == "combined-flash"
    assert metrics.reliability_snapshot()["shape_repair_24h"] == 1


@pytest.mark.asyncio
async def test_deadline_deferred_repair_is_spent_before_the_failsafe() -> None:
    # The paired attempt has nearly no budget left, so its in-attempt repair
    # is deferred (never started).  The post-acceptance expression pass then
    # spends the one violation-quoting corrective retry instead of landing on
    # a canned line.
    from companion_daemon.world_v2 import deliberation as deliberation_module

    provider = _ShapeRepairedCombinedProvider(corrected_on_call=2)
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:pre-failsafe-retry")

    token = deliberation_module._ATTEMPT_DEADLINE.set(time.monotonic() + 1.0)
    try:
        await cognition.appraisal.propose(request)
    finally:
        deliberation_module._ATTEMPT_DEADLINE.reset(token)
    assert len(provider.calls) == 1  # repair deferred, not spent

    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:pre-failsafe-retry-expression"})
    )

    assert len(provider.calls) == 2
    assert "structural validation" in provider.calls[1][-1]["content"]
    assert expression.model_id == "combined-flash"
    assert expression.model_version == SingleCallInboundCognition.VERSION
    assert "接住" in json.dumps(expression.raw_proposal, ensure_ascii=False)
    assert metrics.reliability_snapshot()["failsafe_24h"] == 0


@pytest.mark.asyncio
async def test_spent_corrective_is_not_repeated_before_the_failsafe() -> None:
    # The in-attempt corrective already ran once and failed; the expression
    # pass must not repeat the identical repair before its bounded failsafe.
    provider = _ShapeRepairedCombinedProvider(corrected_on_call=99)
    cognition = SingleCallInboundCognition(flash_model=provider)
    request = _request(revision=3, call="call:pre-failsafe-exhausted")

    await cognition.appraisal.propose(request)
    assert len(provider.calls) == 2  # paired pass plus one failed corrective

    expression = await cognition.expression.propose(
        request.model_copy(update={"call_id": "call:pre-failsafe-exhausted-expression"})
    )

    assert len(provider.calls) == 2  # no third identical repair
    assert expression.model_id == "local-expression-failsafe"
    assert metrics.reliability_snapshot()["failsafe_24h"] == 1


@pytest.mark.asyncio
async def test_direct_adapter_repairs_non_claim_shape_rejects_too() -> None:
    provider = _ShapeRepairedCombinedProvider(corrected_on_call=2)

    class _DirectShapeProvider:
        model = "direct-flash"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(
            self, messages: list[dict[str, str]], *, temperature: float = 0.8
        ) -> str:
            del temperature
            self.calls.append(messages)
            expression = (
                _CORRECTED_EXPRESSION if len(self.calls) >= 2 else _BROKEN_SHAPE_EXPRESSION
            )
            return json.dumps(expression, ensure_ascii=False)

    direct = _DirectShapeProvider()
    adapter = ChatModelDeliberationAdapter(model=direct)
    output = await adapter.propose(_request(revision=3, call="call:direct-shape-repair"))

    assert len(direct.calls) == 2
    assert "structural validation" in direct.calls[1][-1]["content"]
    assert "接住" in json.dumps(output.raw_proposal, ensure_ascii=False)
    del provider


# --- timestamped failover-usage check ------------------------------------------


class _TimestampedProvider:
    def __init__(self, used_at: float | None) -> None:
        self.last_fallback_used_at = used_at
        self.last_attempt_used_fallback = True  # stale boolean must not win


def test_recent_fallback_use_skips_and_stale_use_does_not() -> None:
    assert _provider_already_used_fallback(_TimestampedProvider(monotonic() - 5.0))
    assert not _provider_already_used_fallback(_TimestampedProvider(monotonic() - 300.0))
    assert not _provider_already_used_fallback(_TimestampedProvider(None))


def test_providers_without_the_timestamp_keep_boolean_semantics() -> None:
    class _LegacyProvider:
        last_attempt_used_fallback = True

    class _CleanProvider:
        pass

    assert _provider_already_used_fallback(_LegacyProvider())
    assert not _provider_already_used_fallback(_CleanProvider())


@pytest.mark.asyncio
async def test_failover_chat_model_records_the_fallback_use_timestamp() -> None:
    primary = DeepSeekChatModel(
        "deepseek-key",
        "https://api.deepseek.com",
        "deepseek-v4-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(402, json={"error": "insufficient balance"})
        ),
    )
    fallback = OpenAICompatibleChatModel(
        "openai-key",
        "https://api.openai.com/v1",
        "gpt-5.6-luna",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"choices": [{"message": {"content": "备用回复"}}]},
            )
        ),
    )
    model = FailoverChatModel(primary=primary, fallback=fallback)
    assert model.last_fallback_used_at is None

    before = monotonic()
    assert await model.complete([{"role": "user", "content": "你好"}]) == "备用回复"

    assert model.last_attempt_used_fallback is True
    assert model.last_fallback_used_at is not None
    assert model.last_fallback_used_at >= before
    assert _provider_already_used_fallback(model)
    await model.aclose()
