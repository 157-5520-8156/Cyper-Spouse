from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2 import runtime as runtime_module
from companion_daemon.world_v2 import deliberation as deliberation_module
from companion_daemon.world_v2.appraisal_trigger import (
    interaction_appraisal_trigger_events as real_interaction_appraisal_trigger_events,
)
from companion_daemon.world_v2.expression_episode_lifecycle import (
    next_expression_retry_due,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
)
from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.perception_trigger import perception_trigger_event
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.schemas import Observation, WorldEvent


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class _MonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _world_only_clock_event(
    *, world_id: str, origin: datetime, ordinal: int
) -> WorldEvent:
    target = origin + timedelta(seconds=ordinal)
    payload = {
        "logical_time_from": origin.isoformat(),
        "logical_time_to": target.isoformat(),
    }
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=f"event:expression-repin:clock:{ordinal}",
        world_id=world_id,
        event_type="ClockAdvanced",
        logical_time=target,
        created_at=target,
        actor="system:test",
        source="test:expression-repin",
        trace_id=f"trace:expression-repin:clock:{ordinal}",
        causation_id=f"cause:expression-repin:clock:{ordinal}",
        correlation_id=f"correlation:expression-repin:clock:{ordinal}",
        idempotency_key=domain_idempotency_key(
            event_type="ClockAdvanced",
            world_id=world_id,
            payload=payload,
        )
        or f"clock:expression-repin:{ordinal}",
        payload=payload,
    )


def _is_expression_prompt(prompt: str) -> bool:
    return (
        "Return one raw JSON ExpressionDraft" in prompt
        or "raw JSON ExpressionDraft only" in prompt
        or "provisional first beat" in prompt
        or (
            "appraisal_draft and expression_draft" in prompt
            and "COMBINED OUTPUT ENVELOPE" in prompt
        )
    )


def _expression_response(
    prompt: str,
    *,
    text: str,
    episode_disposition: str | None = None,
) -> str:
    draft: dict[str, object] = {
        "private_turn_state": {
            "inner_state_summary": "I want to answer the current message directly.",
            "attended_source_refs": [],
        },
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": text}],
        "cadence": "conversational",
        "stance": "answer_directly",
        "brief_rationale": "Answer the current observation.",
        "confidence": 7600,
        "world_claims": [],
    }
    if episode_disposition is not None:
        draft["episode_disposition"] = episode_disposition
    if (
        "appraisal_draft and expression_draft" in prompt
        and "COMBINED OUTPUT ENVELOPE" in prompt
    ):
        return json.dumps(
            {
                "appraisal_draft": {
                    "appraise": False,
                    "brief_rationale": "No durable appraisal is needed.",
                    "behavior_tendency": "observe",
                    "stance": "open",
                    "display_strategy": "natural",
                    "confidence": 3000,
                },
                "expression_draft": draft,
            },
            ensure_ascii=False,
        )
    return json.dumps(draft, ensure_ascii=False)


class _Delivery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self._messages: dict[str, str] = {}

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        message_id = f"qq-{len(self.sent)}"
        self._messages[message_id] = text
        return {"status": "ok", "data": {"message_id": message_id}}

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"reaction:{message_id}:{reaction_id}"))
        return {"status": "ok", "data": {"message_id": f"reaction-{len(self.sent)}"}}

    async def send_sticker(
        self, recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"sticker:{sticker_id}"))
        return {"status": "ok", "data": {"message_id": f"sticker-{len(self.sent)}"}}

    async def send_typing(
        self, recipient_id: str, *, state: str
    ) -> dict[str, object]:
        self.sent.append((recipient_id, f"typing:{state}"))
        return {"status": "ok", "data": {"message_id": f"typing-{len(self.sent)}"}}

    async def get_message(
        self,
        recipient_id: str,
        *,
        message_id: str,
    ) -> dict[str, object]:
        del recipient_id
        text = self._messages.get(message_id)
        if text is None:
            return {"status": "failed", "retcode": 1404, "message": "not found"}
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message": text},
        }


class _BlockingExpressionModel:
    model = "fixture:blocking-expression"

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        if self.expression_calls == 1:
            self.entered.set()
            await self.release.wait()
        return _expression_response(prompt, text="这句只应该生成一次。")


class _LatestTurnCapacityModel:
    """Hold superseded turns so they would otherwise consume every provider slot."""

    model = "fixture:latest-turn-capacity"

    def __init__(self) -> None:
        self.expression_calls = 0
        self.entered = (asyncio.Event(), asyncio.Event())
        self.cancelled = (asyncio.Event(), asyncio.Event())
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        ordinal = self.expression_calls
        if ordinal <= 2:
            self.entered[ordinal - 1].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled[ordinal - 1].set()
                raise
        return _expression_response(
            prompt,
            text="我看到你连着发的这些了，这句接住了。",
        )


class _CountingFactModel:
    """Count semantic Fact calls while accepting both single and batch contracts."""

    model = "fixture:counting-fact"

    def __init__(self) -> None:
        self.fact_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        if "long-term user-fact memory" not in system:
            return await self._fallback.complete(messages, temperature=temperature)
        self.fact_calls += 1
        request = json.loads(messages[-1]["content"])
        observations = request.get("observations")
        if isinstance(observations, list):
            return json.dumps(
                {
                    "decisions": [
                        {
                            "observation_id": item["observation_id"],
                            "result": {"retain": False},
                        }
                        for item in observations
                    ]
                },
                ensure_ascii=False,
            )
        return '{"retain":false}'


class _BlockingExpressionAndAppraisalModel:
    """Expose both obsolete provider calls so newer inbound can cancel them."""

    model = "fixture:blocking-expression-and-appraisal"

    def __init__(self) -> None:
        self.expression_entered = asyncio.Event()
        self.appraisal_entered = asyncio.Event()
        self.expression_cancelled = asyncio.Event()
        self.appraisal_cancelled = asyncio.Event()
        self.expression_calls = 0
        self.appraisal_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if (
            "appraisal_draft and expression_draft" in prompt
            and "COMBINED OUTPUT ENVELOPE" in prompt
        ):
            self.appraisal_calls += 1
            if self.appraisal_calls == 1:
                self.appraisal_entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.appraisal_cancelled.set()
                    raise
            return await self._fallback.complete(messages, temperature=temperature)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        if self.expression_calls == 1:
            self.expression_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.expression_cancelled.set()
                raise
        return _expression_response(prompt, text="新的这句接住了。")


class _BatchUpdatingFactModel:
    model = "fixture:batch-updating-fact"

    def __init__(self) -> None:
        self.fact_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        system = str(messages[0]["content"])
        if "retrieval memory" in system:
            return '{"retain":false}'
        if "long-term user-fact memory" not in system:
            return await self._fallback.complete(messages, temperature=temperature)
        self.fact_calls += 1
        request = json.loads(messages[-1]["content"])
        observations = request.get("observations")
        if not isinstance(observations, list):
            return '{"retain":false}'
        return json.dumps(
            {
                "decisions": [
                    {
                        "observation_id": item["observation_id"],
                        "result": {
                            "retain": True,
                            "predicate_code": "location.home",
                            "value": "深圳" if "深圳" in item["text"] else "广州",
                            "privacy_class": "personal",
                            "confidence": 9000,
                            "rationale": "Explicit residence update.",
                        },
                    }
                    for item in observations
                ]
            },
            ensure_ascii=False,
        )


class _HeadAdvancingExpressionModel:
    """Advance an unrelated deliberation head before returning the first draft."""

    model = "fixture:head-advancing-expression"

    def __init__(self) -> None:
        self.expression_calls = 0
        self.advance_head: Callable[[], Awaitable[None]] | None = None
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        if self.expression_calls == 1:
            assert self.advance_head is not None
            await self.advance_head()
        return _expression_response(
            prompt,
            text=(
                "这句绑定的是旧游标，不能发送。"
                if self.expression_calls == 1
                else "我重新看过现在的情况，再认真接住这句。"
            ),
        )


class _AlwaysHeadAdvancingExpressionModel:
    """Make every reply draft stale so live and scheduler recovery share a cap."""

    model = "fixture:always-head-advancing-expression"

    def __init__(self) -> None:
        self.expression_calls = 0
        self.advance_head: Callable[[int], Awaitable[None]] | None = None
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        assert self.advance_head is not None
        await self.advance_head(self.expression_calls)
        return _expression_response(
            prompt,
            text=f"这份第 {self.expression_calls} 次草稿也绑定了旧游标。",
        )


class _SecondTurnBlockingExpressionModel:
    """Hold the second turn while the previous delivery lease is due."""

    model = "fixture:second-turn-blocking-expression"

    def __init__(self) -> None:
        self.second_entered = asyncio.Event()
        self.second_release = asyncio.Event()
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        if self.expression_calls == 2:
            self.second_entered.set()
            await self.second_release.wait()
        return _expression_response(
            prompt,
            text=(
                "第一句已经送到。"
                if self.expression_calls == 1
                else "旧回执不该让我把这一句想两遍。"
            ),
        )


class _TwoStageBlockingExpressionModel:
    """Block both the crashed call and its concurrent recovery call."""

    model = "fixture:two-stage-blocking-expression"

    def __init__(self) -> None:
        self.first_entered = asyncio.Event()
        self.first_release = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.second_release = asyncio.Event()
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        if self.expression_calls == 1:
            self.first_entered.set()
            await self.first_release.wait()
        elif self.expression_calls == 2:
            self.second_entered.set()
            await self.second_release.wait()
        return _expression_response(prompt, text="并发恢复也只能生成这一份。")


class _CountingExpressionModel:
    model = "fixture:counting-expression"

    def __init__(self) -> None:
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        return _expression_response(prompt, text="情绪处理完了，我现在接住这句。")


class _RevisionAwareExpressionModel:
    model = "fixture:revision-aware-expression"

    def __init__(self) -> None:
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        texts = (
            "我刚才想说的是，这句我接住了。",
            "我重新想了一下，这次认真回你。",
            "这回我换个说法接着聊。",
            "现在可以把这句好好发出去了。",
        )
        return _expression_response(
            prompt,
            text=texts[min(self.expression_calls - 1, len(texts) - 1)],
        )


class _AppendEpisodeModel:
    model = "fixture:append-expression-episode"

    def __init__(self) -> None:
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        provisional = "provisional first beat" in prompt
        return _expression_response(
            prompt,
            text=(
                "我先接住你这句话。"
                if provisional
                else "还有一点，我等这条送达后再补充。"
            ),
            episode_disposition=None if provisional else "append",
        )


def _runtime(host):  # type: ignore[no-untyped-def]
    return host._host._application._turns._runtime  # noqa: SLF001


def _application(host):  # type: ignore[no-untyped-def]
    return host._host._application  # noqa: SLF001


async def _direct_inbound(host, *, message_id: str, text: str):  # type: ignore[no-untyped-def]
    return await _application(host).inbound(
        platform="qq",
        platform_user_id="10001",
        platform_message_id=message_id,
        text=text,
        observed_at=NOW,
        trace_id=f"trace:{message_id}",
    )


@pytest.mark.asyncio
async def test_live_ingest_model_call_is_not_mistaken_for_crash_recovery(
    tmp_path: Path,
) -> None:
    """A claimed/no-audit process still has a live owner while ingest holds its lock."""

    model = _BlockingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "live-ingest-is-not-retry.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    inbound = asyncio.create_task(
        _direct_inbound(
            host,
            message_id="live-ingest-is-not-retry",
            text="模型还在想的时候，不要再叫第二遍。",
        )
    )
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=3)

        retry = await asyncio.wait_for(
            _runtime(host)._drain_expression_retry_once(),  # noqa: SLF001
            timeout=1,
        )

        assert retry is None
        assert model.expression_calls == 1
        model.release.set()
        outcome = await asyncio.wait_for(inbound, timeout=5)
        assert outcome.status == "action_authorized"
        assert model.expression_calls == 1
    finally:
        model.release.set()
        if not inbound.done():
            inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_newest_inbound_cancels_superseded_provider_work_before_authoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user volley must reserve real provider capacity for its newest turn."""

    monkeypatch.setattr(deliberation_module, "MAX_INFLIGHT_PROVIDER_TASKS", 2)
    monkeypatch.setattr(deliberation_module, "MAX_INFLIGHT_QUICK_TASKS", 0)
    model = _LatestTurnCapacityModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "newest-inbound-provider-capacity.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    first = asyncio.create_task(
        _direct_inbound(host, message_id="capacity-volley-1", text="先说第一句。")
    )
    second: asyncio.Task[object] | None = None
    third: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(model.entered[0].wait(), timeout=3)
        second = asyncio.create_task(
            _direct_inbound(host, message_id="capacity-volley-2", text="再补第二句。")
        )
        await asyncio.wait_for(model.entered[1].wait(), timeout=3)
        third = asyncio.create_task(
            _direct_inbound(host, message_id="capacity-volley-3", text="最后是这一句。")
        )

        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, third),
            timeout=5,
        )

        assert [item.status for item in outcomes] == [
            "observed_only",
            "observed_only",
            "action_authorized",
        ]
        assert model.expression_calls in {3, 4}
        assert all(event.is_set() for event in model.cancelled)
        projection = await host._host.action_due_projection()  # noqa: SLF001
        assert [item.text for item in projection.stored_message_payloads] == [
            "我看到你连着发的这些了，这句接住了。"
        ]
    finally:
        for task in (first, second, third):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second, third) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_superseded_volley_folds_appraisal_and_batches_fact_model_work(
    tmp_path: Path,
) -> None:
    """A burst keeps every source message without paying one model call per fragment."""

    model = _LatestTurnCapacityModel()
    fact_model = _CountingFactModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "volley-background-cognition.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=fact_model,
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    first = asyncio.create_task(
        _direct_inbound(host, message_id="cognition-volley-1", text="我下午")
    )
    second: asyncio.Task[object] | None = None
    third: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(model.entered[0].wait(), timeout=3)
        second = asyncio.create_task(
            _direct_inbound(host, message_id="cognition-volley-2", text="去了医院")
        )
        await asyncio.wait_for(model.entered[1].wait(), timeout=3)
        third = asyncio.create_task(
            _direct_inbound(host, message_id="cognition-volley-3", text="现在没事了")
        )
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, third),
            timeout=5,
        )
        assert [item.status for item in outcomes] == [
            "observed_only",
            "observed_only",
            "action_authorized",
        ]

        for ordinal in range(8):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=1, seconds=ordinal),
                max_action_units=8,
                max_background_units=16,
            )
            projection = await host._host.action_due_projection()  # noqa: SLF001
            if not any(
                process.process_kind in {"interaction_appraisal", "interaction_fact"}
                and process.state != "terminal"
                for process in projection.trigger_processes
            ):
                break

        projection = await host._host.action_due_projection()  # noqa: SLF001
        appraisals = tuple(
            process
            for process in projection.trigger_processes
            if process.process_kind == "interaction_appraisal"
        )
        facts = tuple(
            process
            for process in projection.trigger_processes
            if process.process_kind == "interaction_fact"
        )
        assert len(projection.message_observations) == 3
        assert len(appraisals) == len(facts) == 3
        assert all(process.state == "terminal" for process in (*appraisals, *facts))
        assert sum(
            process.runtime_outcome_ref
            == "interaction-appraisal:folded-into-newer-inbound"
            for process in appraisals
        ) == 2
        # Three visible attempts include the two promptly cancelled calls.
        # The two folded appraisal processes cannot have proposal audits, so
        # only the newest conversational moment receives background appraisal;
        # Fact keeps all three exact sources in one model batch.
        assert model.expression_calls in {3, 4}
        assert sum(
            audit.proposal_id.startswith("proposal:appraisal-draft:")
            for audit in projection.proposal_audits
        ) == 1
        assert fact_model.fact_calls == 1
    finally:
        for task in (first, second, third):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second, third) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_new_inbound_cancels_an_appraisal_provider_after_durable_fold(
    tmp_path: Path,
) -> None:
    model = _BlockingExpressionAndAppraisalModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "appraisal-provider-fold.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=_CountingFactModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    first = asyncio.create_task(
        _direct_inbound(host, message_id="appraisal-fold-1", text="我先说半句")
    )
    scheduler: asyncio.Task[object] | None = None
    second: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(model.expression_entered.wait(), timeout=3)
        scheduler = asyncio.create_task(
            host.scheduler_once(
                observed_at=NOW + timedelta(seconds=1),
                max_action_units=0,
                max_background_units=2,
            )
        )
        await asyncio.wait_for(model.appraisal_entered.wait(), timeout=3)
        second = asyncio.create_task(
            _direct_inbound(host, message_id="appraisal-fold-2", text="现在说完整了")
        )
        first_outcome, second_outcome, _scheduler_outcome = await asyncio.wait_for(
            asyncio.gather(first, second, scheduler),
            timeout=5,
        )

        assert first_outcome.status == "observed_only"
        assert second_outcome.status == "action_authorized"
        assert model.expression_cancelled.is_set()
        assert model.appraisal_cancelled.is_set()
        projection = await host._host.action_due_projection()  # noqa: SLF001
        old_observation = projection.message_observations[0]
        old_appraisal = next(
            process
            for process in projection.trigger_processes
            if process.process_kind == "interaction_appraisal"
            and process.source_evidence_ref == old_observation.observation_id
        )
        assert old_appraisal.state == "terminal"
        assert (
            old_appraisal.runtime_outcome_ref
            == "interaction-appraisal:folded-into-newer-inbound"
        )
    finally:
        for task in (first, second, scheduler):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second, scheduler) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_fact_batch_settles_ordered_slot_updates_without_a_second_model_call(
    tmp_path: Path,
) -> None:
    model = _BlockingExpressionModel()
    fact_model = _BatchUpdatingFactModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "fact-batch-slot-update.sqlite",
            PRIMARY_USER_ID="geoff",
            LOCAL_APPRAISAL_ENABLED=False,
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=fact_model,
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    first = asyncio.create_task(
        _direct_inbound(host, message_id="fact-batch-update-1", text="我住在深圳")
    )
    second: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=3)
        second = asyncio.create_task(
            _direct_inbound(host, message_id="fact-batch-update-2", text="我现在搬到广州了")
        )
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second),
            timeout=5,
        )
        assert [item.status for item in outcomes] == [
            "observed_only",
            "action_authorized",
        ]

        await host.scheduler_once(
            observed_at=NOW + timedelta(minutes=1),
            max_action_units=8,
            max_background_units=1,
        )
        midway = await host._host.action_due_projection()  # noqa: SLF001
        midway_facts = tuple(
            process
            for process in midway.trigger_processes
            if process.process_kind == "interaction_fact"
        )
        assert len(midway.interaction_fact_decisions) == 1
        assert sum(process.state == "terminal" for process in midway_facts) == 1
        assert fact_model.fact_calls == 1

        # Rebuild the runtime from the immutable ledger between members.  The
        # second slot update must recover the recorded batch decision rather
        # than ask the model again under the first member's new Fact Context.
        await host.aclose()
        host = build_qq_c2c_host(
            settings=Settings(
                database_path=tmp_path / "fact-batch-slot-update.sqlite",
                PRIMARY_USER_ID="geoff",
                LOCAL_APPRAISAL_ENABLED=False,
                WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            ),
            recipient_id="10001",
            bootstrap_at=NOW + timedelta(minutes=1),
            model=model,
            advisory_model=fact_model,
            delivery=_Delivery(),
            use_configured_recall_embedding=False,
        )
        replayed = await host._host.action_due_projection()  # noqa: SLF001
        assert len(replayed.interaction_fact_decisions) == 1
        for ordinal in range(10):
            await host.scheduler_once(
                observed_at=NOW + timedelta(minutes=2, seconds=ordinal),
                max_action_units=8,
                max_background_units=16,
            )
            projection = await host._host.action_due_projection()  # noqa: SLF001
            facts = tuple(
                process
                for process in projection.trigger_processes
                if process.process_kind == "interaction_fact"
            )
            if facts and all(process.state == "terminal" for process in facts):
                break

        projection = await host._host.action_due_projection()  # noqa: SLF001
        assert fact_model.fact_calls == 1
        assert all(
            process.state == "terminal"
            for process in projection.trigger_processes
            if process.process_kind == "interaction_fact"
        )
        assert projection.interaction_fact_decisions == ()
        active_home = next(
            fact
            for fact in projection.facts
            if fact.values.status == "active"
            and fact.values.predicate_code == "location.home"
        )
        assert active_home.values.value_hash == hashlib.sha256("广州".encode()).hexdigest()
    finally:
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_newer_inbound_cancels_scheduler_owned_retry_without_stopping_recovery(
    tmp_path: Path,
) -> None:
    """A stale retry cancellation is a completed race, not scheduler shutdown."""

    model = _LatestTurnCapacityModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "scheduler-owned-retry-cancellation.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    interrupted = asyncio.create_task(
        _direct_inbound(host, message_id="scheduler-owned-old", text="旧消息。")
    )
    retry: asyncio.Task[object] | None = None
    newest: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(model.entered[0].wait(), timeout=3)
        interrupted.cancel()
        interrupted_result = await asyncio.gather(
            interrupted,
            return_exceptions=True,
        )
        assert isinstance(interrupted_result[0], asyncio.CancelledError)

        retry = asyncio.create_task(runtime._drain_expression_retry_once())  # noqa: SLF001
        await asyncio.wait_for(model.entered[1].wait(), timeout=3)
        newest = asyncio.create_task(
            _direct_inbound(host, message_id="scheduler-owned-new", text="以这句为准。")
        )

        retry_outcome, newest_outcome = await asyncio.wait_for(
            asyncio.gather(retry, newest),
            timeout=5,
        )

        assert retry_outcome is not None
        assert retry_outcome.status == "observed_only"
        assert newest_outcome.status == "action_authorized"
        assert model.expression_calls == 3
        assert all(event.is_set() for event in model.cancelled)
    finally:
        for task in (interrupted, retry, newest):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (interrupted, retry, newest) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_unrelated_head_advance_repins_valid_expression_before_delivery(
    tmp_path: Path,
) -> None:
    """A stale valid draft is discarded and authored again, never rebased."""

    model = _HeadAdvancingExpressionModel()
    delivery = _Delivery()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "valid-expression-repins.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)

    async def advance_unrelated_head() -> None:
        projection = await host._host.action_due_projection()  # noqa: SLF001
        observation_ref = projection.message_observations[-1]
        authority = projection.committed_world_event_refs[
            observation_ref.world_revision - 1
        ]
        persisted = await asyncio.to_thread(
            runtime._ledger.lookup_event_commit,  # noqa: SLF001
            authority.event_id,
        )
        assert persisted is not None
        observation = Observation.model_validate_json(persisted[0].payload_json)
        event = perception_trigger_event(
            observation=observation,
            observation_event=persisted[0],
        )
        await runtime._commit(  # noqa: SLF001
            [event],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:test:valid-expression-unrelated-head",
        )

    model.advance_head = advance_unrelated_head
    try:
        outcome = await _direct_inbound(
            host,
            message_id="valid-expression-repins",
            text="回执推进账本时，也别把这一句弄丢。",
        )

        assert outcome.status == "action_authorized"
        assert model.expression_calls == 2
        projection = await host._host.action_due_projection()  # noqa: SLF001
        assert len(projection.proposal_audits) == 1
        assert [item.text for item in projection.stored_message_payloads] == [
            "我重新看过现在的情况，再认真接住这句。"
        ]
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_due_previous_delivery_reconciliation_waits_for_visible_reply(
    tmp_path: Path,
) -> None:
    """An old positive lookup must not make the current reply author twice."""

    model = _SecondTurnBlockingExpressionModel()
    delivery = _Delivery()
    scheduler_now = [NOW]
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "old-receipt-does-not-repin.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        action_due_now=lambda: scheduler_now[0],
        use_configured_recall_embedding=False,
    )
    second: asyncio.Task[object] | None = None
    due_wake: asyncio.Task[None] | None = None
    try:
        first = await host.inbound_text(
            message_id="old-receipt-first",
            recipient_id="10001",
            text="先发第一句。",
            observed_at=NOW,
        )
        assert first.status == "action_authorized"
        assert first.action_id is not None
        before = await host._host.action_due_projection()  # noqa: SLF001
        first_action = next(
            item for item in before.actions if item.action_id == first.action_id
        )
        assert first_action.state == "provider_accepted"
        assert first_action.claim_lease is not None

        second = asyncio.create_task(
            host.inbound_text(
                message_id="old-receipt-second",
                recipient_id="10001",
                text="两分钟后的这一句只想一次。",
                observed_at=NOW + timedelta(seconds=1),
            )
        )
        await asyncio.wait_for(model.second_entered.wait(), timeout=3)

        scheduler_now[0] = first_action.claim_lease.expires_at
        due_wake = asyncio.create_task(host._wake_due_actions())  # noqa: SLF001
        await asyncio.sleep(0.05)
        during = await host._host.action_due_projection()  # noqa: SLF001
        during_first = next(
            item for item in during.actions if item.action_id == first.action_id
        )

        assert during_first.state == "provider_accepted"
        assert not due_wake.done()
        assert model.expression_calls == 2

        model.second_release.set()
        second_result, _ = await asyncio.wait_for(
            asyncio.gather(second, due_wake),
            timeout=8,
        )
        assert getattr(second_result, "status", None) == "action_authorized"
        assert model.expression_calls == 2

        after = await host._host.action_due_projection()  # noqa: SLF001
        settled_first = next(
            item for item in after.actions if item.action_id == first.action_id
        )
        assert settled_first.state == "delivered"
        first_sends = [
            text for _recipient, text in delivery.sent if text == "第一句已经送到。"
        ]
        assert first_sends == ["第一句已经送到。"]
    finally:
        model.second_release.set()
        for task in (second, due_wake):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (second, due_wake) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_world_only_head_advance_repins_with_same_absolute_turn_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clock/Action competition invalidates prose without renewing its deadline."""

    clock = _MonotonicClock()
    policy = InteractiveTurnBudgetPolicy(
        total_seconds=10.0,
        hedge_after_seconds=1.0,
        acceptance_dispatch_reserve_seconds=0.5,
        technical_recovery_seconds=2.0,
        validation_recovery_seconds=3.0,
        validation_reselection_seconds=4.0,
        clock=clock,
        wall_clock=lambda: NOW,
    )
    model = _HeadAdvancingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "world-only-expression-repins.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        interactive_turn_budget_policy=policy,
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    seen_budgets: list[object] = []
    real_audit_once = runtime._audit_expression_attempt_once  # noqa: SLF001

    async def capture_budget(**kwargs):  # type: ignore[no-untyped-def]
        seen_budgets.append(kwargs["turn_budget"])
        return await real_audit_once(**kwargs)

    monkeypatch.setattr(runtime, "_audit_expression_attempt_once", capture_budget)

    async def advance_world_only_head() -> None:
        projection = await host._host.action_due_projection()  # noqa: SLF001
        origin = projection.logical_time or NOW
        await runtime._commit(  # noqa: SLF001
            [
                _world_only_clock_event(
                    world_id=runtime.world_id,
                    origin=origin,
                    ordinal=1,
                )
            ],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:test:world-only-expression-repin",
        )

    model.advance_head = advance_world_only_head
    try:
        outcome = await _direct_inbound(
            host,
            message_id="world-only-expression-repins",
            text="时钟刚好推进，也不能把旧游标写的回复发出来。",
        )

        assert outcome.status == "action_authorized"
        assert model.expression_calls == 2
        assert len(seen_budgets) == 2
        assert seen_budgets[0] is seen_budgets[1]
        projection = await host._host.action_due_projection()  # noqa: SLF001
        assert len(projection.proposal_audits) == 1
        assert [item.text for item in projection.stored_message_payloads] == [
            "我重新看过现在的情况，再认真接住这句。"
        ]
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_exhausted_repin_budget_records_durable_defer_without_reauthoring(
    tmp_path: Path,
) -> None:
    """Repin exhaustion is one audited failure, not a fresh provider allowance."""

    clock = _MonotonicClock()
    policy = InteractiveTurnBudgetPolicy(
        total_seconds=1.0,
        hedge_after_seconds=0.2,
        acceptance_dispatch_reserve_seconds=0.1,
        technical_recovery_seconds=0.5,
        validation_recovery_seconds=0.5,
        validation_reselection_seconds=0.5,
        clock=clock,
        wall_clock=lambda: NOW,
    )
    model = _HeadAdvancingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "exhausted-expression-repin.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        interactive_turn_budget_policy=policy,
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)

    async def advance_then_exhaust() -> None:
        projection = await host._host.action_due_projection()  # noqa: SLF001
        origin = projection.logical_time or NOW
        await runtime._commit(  # noqa: SLF001
            [
                _world_only_clock_event(
                    world_id=runtime.world_id,
                    origin=origin,
                    ordinal=2,
                )
            ],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:test:exhausted-expression-repin",
        )
        clock.value = 2.0

    model.advance_head = advance_then_exhaust
    try:
        outcome = await _direct_inbound(
            host,
            message_id="exhausted-expression-repin",
            text="如果这一轮时间用完了，就留到可恢复状态，别重新开预算。",
        )

        assert outcome.status == "deferred"
        assert outcome.deferred_refs == (
            "expression_episode.repin_budget_exhausted",
        )
        assert model.expression_calls == 1
        projection = await host._host.action_due_projection()  # noqa: SLF001
        assert projection.proposal_audits == ()
        assert len(projection.model_result_audits) == 1
        recorded = json.loads(projection.model_result_audits[0].audit_json)
        assert recorded["outcome"] == "budget_exhausted"
        assert recorded["failure_code"] == "primary_timeout"
        assert recorded["route"]["reason_code"] == "interactive_budget_exhausted"
        episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.claim_lease is not None
        assert next_expression_retry_due(projection) == (
            episode.claim_lease.acquired_at + timedelta(minutes=10)
        )
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_fresh_context_repin_cap_is_durable_across_scheduler_recovery(
    tmp_path: Path,
) -> None:
    """Two repins exhaust one durable attempt; recovery cannot make call four."""

    model = _AlwaysHeadAdvancingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "durable-expression-repin-cap.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)

    async def advance_world_head(call_ordinal: int) -> None:
        projection = await host._host.action_due_projection()  # noqa: SLF001
        origin = projection.logical_time or NOW
        await runtime._commit(  # noqa: SLF001
            [
                _world_only_clock_event(
                    world_id=runtime.world_id,
                    origin=origin,
                    ordinal=100 + call_ordinal,
                )
            ],
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id=f"commit:test:durable-expression-repin:{call_ordinal}",
        )

    model.advance_head = advance_world_head
    try:
        outcome = await _direct_inbound(
            host,
            message_id="durable-expression-repin-cap",
            text="账本一直有并发推进时，也别无限重新生成。",
        )

        assert outcome.status == "deferred"
        assert outcome.deferred_refs == (
            "expression_episode.repin_budget_exhausted",
        )
        assert model.expression_calls == 3

        projection = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert len(episode.expression_repin_reservation_ids) == 2
        assert len(projection.model_result_audits) == 1
        recorded = json.loads(projection.model_result_audits[0].audit_json)
        assert recorded["outcome"] == "budget_exhausted"
        assert recorded["failure_code"] == "primary_timeout"
        assert recorded["route"]["reason_code"] == (
            "expression_fresh_context_repin_exhausted"
        )
        assert episode.claim_lease is not None
        assert next_expression_retry_due(projection) == (
            episode.claim_lease.acquired_at + timedelta(minutes=10)
        )

        # The same-owner scheduler sees the durable terminal failure/backoff;
        # it neither leaks a cursor conflict nor invokes the provider again.
        await runtime.drain_background_once()
        assert model.expression_calls == 3
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_same_runtime_resumes_its_unrecorded_expression_claim_immediately(
    tmp_path: Path,
) -> None:
    """Local crash continuation is owner-aware and need not wait for the lease."""

    model = _BlockingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "same-runtime-expression-owner.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    inbound = asyncio.create_task(
        _direct_inbound(
            host,
            message_id="same-runtime-expression-owner",
            text="同一个运行实例可以接着自己没落账的工作。",
        )
    )
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=3)
        inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        projection = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.claim_lease is not None
        assert next_expression_retry_due(projection) == episode.claim_lease.expires_at

        recovered = await _runtime(host)._drain_expression_retry_once()  # noqa: SLF001

        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 2
        settled = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in settled.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 1
    finally:
        model.release.set()
        if not inbound.done():
            inbound.cancel()
            await asyncio.gather(inbound, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_scheduler_and_duplicate_ingress_join_one_recovery_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingress may take the world lock after retry polling has already begun."""

    model = _TwoStageBlockingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "expression-retry-ingress-interleaving.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    message_id = "expression-retry-ingress-interleaving"
    text = "调度器和重复入站撞在一起，也不能生成两份回复。"
    crashed = asyncio.create_task(
        _direct_inbound(host, message_id=message_id, text=text)
    )
    retry = None
    duplicate = None
    try:
        await asyncio.wait_for(model.first_entered.wait(), timeout=3)
        crashed.cancel()
        await asyncio.gather(crashed, return_exceptions=True)

        # Pause the scheduler after it has selected the due attempt.  The
        # duplicate ingress then acquires the world lock and enters provider
        # call two before the scheduler continues -- the exact TOCTOU window
        # that a bare ``Lock.locked()`` observation cannot close.
        scheduler_selected = asyncio.Event()
        release_scheduler = asyncio.Event()
        real_retry_source = runtime._expression_retry_source  # noqa: SLF001

        async def paused_retry_source(*, projection, process):  # type: ignore[no-untyped-def]
            scheduler_selected.set()
            await release_scheduler.wait()
            return await real_retry_source(
                projection=projection,
                process=process,
            )

        monkeypatch.setattr(
            runtime,
            "_expression_retry_source",
            paused_retry_source,
        )
        retry = asyncio.create_task(runtime._drain_expression_retry_once())  # noqa: SLF001
        await asyncio.wait_for(scheduler_selected.wait(), timeout=3)

        logical_callers = 0
        both_callers_joined = asyncio.Event()
        real_audit_once = runtime._audit_expression_attempt_once  # noqa: SLF001

        async def observed_audit_once(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal logical_callers
            logical_callers += 1
            if logical_callers == 2:
                both_callers_joined.set()
            return await real_audit_once(**kwargs)

        monkeypatch.setattr(
            runtime,
            "_audit_expression_attempt_once",
            observed_audit_once,
        )
        duplicate = asyncio.create_task(
            _direct_inbound(host, message_id=message_id, text=text)
        )
        await asyncio.wait_for(model.second_entered.wait(), timeout=3)
        release_scheduler.set()
        await asyncio.wait_for(both_callers_joined.wait(), timeout=3)

        assert logical_callers == 2
        assert model.expression_calls == 2

        model.second_release.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(retry, duplicate, return_exceptions=True),
            timeout=8,
        )
        assert not tuple(item for item in outcomes if isinstance(item, Exception))
        assert model.expression_calls == 2

        settled = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in settled.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 1
        reply_results = tuple(
            item
            for item in settled.model_result_audits
            if item.attempt_id == episode.attempt_ids[0]
        )
        assert len(reply_results) == 1
    finally:
        model.first_release.set()
        model.second_release.set()
        for task in (crashed, retry, duplicate):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (crashed, retry, duplicate) if task is not None),
            return_exceptions=True,
        )
        await host.aclose()


@pytest.mark.asyncio
async def test_foreign_runtime_waits_for_expression_claim_expiry_before_recovery(
    tmp_path: Path,
) -> None:
    """A second runtime cannot treat another live instance's claim as its crash."""

    model = _BlockingExpressionModel()
    database = tmp_path / "foreign-runtime-expression-owner.sqlite"
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )
    first = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    inbound = asyncio.create_task(
        _direct_inbound(
            first,
            message_id="foreign-runtime-expression-owner",
            text="另一个进程不能把这次正在生成当成崩溃恢复。",
        )
    )
    second = None
    try:
        await asyncio.wait_for(model.entered.wait(), timeout=3)
        second = build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=NOW,
            model=model,
            advisory_model=FakeCompanionModel(),
            delivery=_Delivery(),
            use_configured_recall_embedding=False,
        )
        first_runtime = _runtime(first)
        second_runtime = _runtime(second)
        assert (
            first_runtime._expression_episode_owner  # noqa: SLF001
            != second_runtime._expression_episode_owner  # noqa: SLF001
        )

        assert await second_runtime._drain_expression_retry_once() is None  # noqa: SLF001
        assert model.expression_calls == 1

        # Simulate the first process dying after its durable claim and before
        # ModelResultRecorded. The foreign worker still waits for the lease.
        inbound.cancel()
        await asyncio.gather(inbound, return_exceptions=True)
        projection = await second._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.claim_lease is not None
        assert episode.claim_lease.owner_id == first_runtime._expression_episode_owner  # noqa: SLF001
        assert next_expression_retry_due(projection) == episode.claim_lease.expires_at
        assert await second_runtime._drain_expression_retry_once() is None  # noqa: SLF001
        assert model.expression_calls == 1

        recover_at = episode.claim_lease.expires_at + timedelta(seconds=1)
        await _application(second).tick(
            tick_id="foreign-expression-owner-expired",
            logical_time_from=NOW,
            logical_time_to=recover_at,
            observed_at=recover_at,
            trace_id="trace:foreign-expression-owner-expired",
            causation_id="cause:foreign-expression-owner-expired",
            correlation_id="correlation:foreign-expression-owner-expired",
            reason="foreign_expression_owner_recovery",
            run_life_ecology=False,
        )
        recovered = await second_runtime._drain_expression_retry_once()  # noqa: SLF001

        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 2
        settled = await second._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in settled.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert episode.claim_lease is not None
        assert episode.claim_lease.owner_id == second_runtime._expression_episode_owner  # noqa: SLF001
        assert len(episode.attempt_ids) == 2
    finally:
        model.release.set()
        if not inbound.done():
            inbound.cancel()
            await asyncio.gather(inbound, return_exceptions=True)
        if second is not None:
            await second.aclose()
        await first.aclose()


@pytest.mark.asyncio
async def test_context_preparation_failure_is_a_durable_backed_off_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-provider Context fault is recorded once instead of busy-spinning."""

    model = _CountingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "context-preparation-expression-failure.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    capsules = runtime._pinned_turn._capsules  # noqa: SLF001
    real_prepare = capsules.prepare_for_deliberation

    def malformed_context(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("fault injection: persistent resolver failure")

    monkeypatch.setattr(capsules, "prepare_for_deliberation", malformed_context)
    try:
        outcome = await _direct_inbound(
            host,
            message_id="context-preparation-expression-failure",
            text="即使上下文坏了，也不能在同一个 claim 上不停重试。",
        )

        assert outcome.status == "deferred"
        assert outcome.deferred_refs == (
            "expression_episode.technical_retry_pending",
        )
        assert model.expression_calls == 0
        failed = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in failed.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.claim_lease is not None
        audits = tuple(
            item
            for item in failed.model_result_audits
            if item.trigger_ref == failed.committed_world_event_refs[
                failed.message_observations[-1].world_revision - 1
            ].event_id
        )
        assert len(audits) == 1
        assert audits[0].proposal_hash is None
        raw_audit = json.loads(audits[0].audit_json)
        assert raw_audit["status"] == "main_exception"
        assert raw_audit["failure_code"] == "main_exception"
        assert raw_audit["model_call_id"].startswith(
            "model-call:skipped-pre-provider:"
        )
        assert "persistent resolver failure" not in audits[0].audit_json
        assert next_expression_retry_due(failed) == NOW + timedelta(minutes=10)

        # Polling the scheduler cannot re-enter context preparation before the
        # exact durable deadline.
        assert await runtime._drain_expression_retry_once() is None  # noqa: SLF001
        after_poll = await host._host.action_due_projection()  # noqa: SLF001
        assert len(after_poll.model_result_audits) == len(failed.model_result_audits)
        assert model.expression_calls == 0

        monkeypatch.setattr(capsules, "prepare_for_deliberation", real_prepare)
        due_at = next_expression_retry_due(after_poll)
        assert due_at is not None
        await _application(host).tick(
            tick_id="context-preparation-recovered",
            logical_time_from=NOW,
            logical_time_to=due_at,
            observed_at=due_at,
            trace_id="trace:context-preparation-recovered",
            causation_id="cause:context-preparation-recovered",
            correlation_id="correlation:context-preparation-recovered",
            reason="context_preparation_recovered",
            run_life_ecology=False,
        )
        recovered = await runtime._drain_expression_retry_once()  # noqa: SLF001

        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 1
        settled = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in settled.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 2
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_snapshot_failure_is_a_durable_backed_off_pre_provider_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned snapshot fault is audited without leaking its exception text."""

    model = _CountingExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "snapshot-expression-failure.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    pinned_turn = runtime._pinned_turn  # noqa: SLF001
    real_project_at = pinned_turn._project_at  # noqa: SLF001
    secret = "fault injection: snapshot backend exposed private-token-123"

    async def broken_snapshot(_cursor):  # type: ignore[no-untyped-def]
        raise OSError(secret)

    monkeypatch.setattr(pinned_turn, "_project_at", broken_snapshot)
    try:
        outcome = await _direct_inbound(
            host,
            message_id="snapshot-expression-failure",
            text="快照读取失败也必须变成可恢复的技术失败。",
        )

        assert outcome.status == "deferred"
        assert outcome.deferred_refs == (
            "expression_episode.technical_retry_pending",
        )
        assert model.expression_calls == 0
        failed = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in failed.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.claim_lease is not None
        audits = tuple(
            item
            for item in failed.model_result_audits
            if item.attempt_id == episode.claim_lease.attempt_id
        )
        assert len(audits) == 1
        assert audits[0].proposal_hash is None
        assert json.loads(audits[0].audit_json)["model_call_id"].startswith(
            "model-call:skipped-pre-provider:"
        )
        assert secret not in audits[0].audit_json
        assert next_expression_retry_due(failed) == NOW + timedelta(minutes=10)

        assert await runtime._drain_expression_retry_once() is None  # noqa: SLF001
        after_poll = await host._host.action_due_projection()  # noqa: SLF001
        assert len(after_poll.model_result_audits) == len(failed.model_result_audits)
        assert model.expression_calls == 0

        monkeypatch.setattr(pinned_turn, "_project_at", real_project_at)
        due_at = next_expression_retry_due(after_poll)
        assert due_at is not None
        await _application(host).tick(
            tick_id="snapshot-expression-recovered",
            logical_time_from=NOW,
            logical_time_to=due_at,
            observed_at=due_at,
            trace_id="trace:snapshot-expression-recovered",
            causation_id="cause:snapshot-expression-recovered",
            correlation_id="correlation:snapshot-expression-recovered",
            reason="snapshot_expression_recovered",
            run_life_ecology=False,
        )
        recovered = await runtime._drain_expression_retry_once()  # noqa: SLF001

        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 1
        settled = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in settled.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 2
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_new_inbound_atomically_supersedes_old_retry_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A newer Observation closes old no-Action work without another attempt."""

    database = tmp_path / "new-inbound-supersedes-old-retry.sqlite"
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID="geoff",
        WORLD_V2_EXPRESSION_EPISODE_MODE="off",
    )
    first = build_qq_c2c_host(
        settings=settings,
        recipient_id="10001",
        bootstrap_at=NOW,
        model=_CountingExpressionModel(),
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    capsules = _runtime(first)._pinned_turn._capsules  # noqa: SLF001

    def broken_context(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ConnectionError("fault injection: old reply cannot compile")

    monkeypatch.setattr(capsules, "prepare_for_deliberation", broken_context)
    second = None
    newer_inbound = None
    blocking_model = _BlockingExpressionModel()
    try:
        failed_outcome = await _direct_inbound(
            first,
            message_id="old-technical-retry-before-restart",
            text="这一句先发生技术失败。",
        )
        assert failed_outcome.status == "deferred"
        assert failed_outcome.deferred_refs == (
            "expression_episode.technical_retry_pending",
        )
        failed = await first._host.action_due_projection()  # noqa: SLF001
        old_episode = next(
            item
            for item in failed.trigger_processes
            if item.process_kind == "expression_episode"
        )
        old_observation_id = old_episode.source_evidence_ref
        old_attempt_ids = old_episode.attempt_ids
        assert old_episode.state == "claimed"
        assert len(old_attempt_ids) == 1
        assert failed.actions == ()
        await first.aclose()

        second = build_qq_c2c_host(
            settings=settings,
            recipient_id="10001",
            bootstrap_at=NOW,
            model=blocking_model,
            advisory_model=FakeCompanionModel(),
            delivery=_Delivery(),
            use_configured_recall_embedding=False,
        )
        newer_inbound = asyncio.create_task(
            _direct_inbound(
                second,
                message_id="new-inbound-after-restart",
                text="这是新的会话时刻，旧失败回复不该再复活。",
            )
        )
        # The provider for the new turn is deliberately still blocked.  Seeing
        # it entered proves the ingress transaction has already committed, so
        # the old lifecycle must be terminal at this exact boundary.
        await asyncio.wait_for(blocking_model.entered.wait(), timeout=3)
        after_new_observation = await second._host.action_due_projection()  # noqa: SLF001
        assert len(after_new_observation.message_observations) == 2
        old_after_restart = next(
            item
            for item in after_new_observation.trigger_processes
            if item.process_kind == "expression_episode"
            and item.source_evidence_ref == old_observation_id
        )
        assert old_after_restart.state == "terminal"
        assert old_after_restart.runtime_outcome_ref == (
            "expression-episode:superseded-by-newer-inbound"
        )
        assert old_after_restart.attempt_ids == old_attempt_ids
        assert len(old_after_restart.attempt_ids) == 1
    finally:
        blocking_model.release.set()
        if newer_inbound is not None and not newer_inbound.done():
            newer_inbound.cancel()
        if newer_inbound is not None:
            await asyncio.gather(newer_inbound, return_exceptions=True)
        if second is not None:
            await second.aclose()
        await first.aclose()


@pytest.mark.asyncio
async def test_new_inbound_preserves_old_episode_with_authorized_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inbound supersession stops before the external Action boundary."""

    model = _TwoStageBlockingExpressionModel()
    model.first_release.set()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "new-inbound-preserves-action.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    real_complete = runtime._complete_expression_episode  # noqa: SLF001

    async def crash_window_after_action(**_kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(
        runtime,
        "_complete_expression_episode",
        crash_window_after_action,
    )
    newer_inbound = None
    try:
        first_outcome = await _direct_inbound(
            host,
            message_id="authorized-action-before-new-inbound",
            text="这条回复已经跨过 Action 授权边界。",
        )
        assert first_outcome.status == "action_authorized"
        before_new = await host._host.action_due_projection()  # noqa: SLF001
        old_episode = next(
            item
            for item in before_new.trigger_processes
            if item.process_kind == "expression_episode"
        )
        old_attempt_ids = old_episode.attempt_ids
        assert old_episode.state == "claimed"
        assert any(action.expression_plan_id is not None for action in before_new.actions)

        monkeypatch.setattr(
            runtime,
            "_complete_expression_episode",
            real_complete,
        )
        newer_inbound = asyncio.create_task(
            _direct_inbound(
                host,
                message_id="new-inbound-must-preserve-authorized-action",
                text="新消息不能撤销已经授权的旧 Action。",
            )
        )
        await asyncio.wait_for(model.second_entered.wait(), timeout=3)
        after_new = await host._host.action_due_projection()  # noqa: SLF001
        preserved = next(
            item
            for item in after_new.trigger_processes
            if item.trigger_id == old_episode.trigger_id
        )

        assert preserved.state == "claimed"
        assert preserved.attempt_ids == old_attempt_ids
        assert preserved.runtime_outcome_ref is None
    finally:
        model.second_release.set()
        if newer_inbound is not None and not newer_inbound.done():
            newer_inbound.cancel()
        if newer_inbound is not None:
            await asyncio.gather(newer_inbound, return_exceptions=True)
        await host.aclose()


@pytest.mark.asyncio
async def test_foreign_background_appraisal_claim_does_not_delay_the_visible_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable Affect follows in background and is never an Expression dependency."""

    model = _CountingExpressionModel()

    def foreign_appraisal_claim(**kwargs):  # type: ignore[no-untyped-def]
        # Commit the production open+claim pair, but bind the live lease to a
        # different durable owner. Expression must remain independent of it.
        return real_interaction_appraisal_trigger_events(
            observation=kwargs["observation"],
            observation_event=kwargs["observation_event"],
            owner_id="worker:foreign-appraisal",
            lease_seconds=120,
        )

    monkeypatch.setattr(
        runtime_module,
        "interaction_appraisal_trigger_events",
        foreign_appraisal_claim,
    )
    host = build_qq_c2c_host(
            settings=Settings(
                database_path=tmp_path / "owned-emotion-keeps-expression.sqlite",
                PRIMARY_USER_ID="geoff",
                LOCAL_APPRAISAL_ENABLED=False,
                WORLD_V2_EXPRESSION_EPISODE_MODE="off",
            ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        outcome = await _direct_inbound(
            host,
            message_id="owned-emotion-keeps-expression",
            text="这句话需要先处理当轮情绪。",
        )
        projection = await host._host.action_due_projection()  # noqa: SLF001
        appraisal = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "interaction_appraisal"
        )

        assert outcome.status == "action_authorized"
        assert model.expression_calls == 1
        assert appraisal.state == "claimed"
        assert appraisal.claim_lease is not None
        assert appraisal.claim_lease.owner_id == "worker:foreign-appraisal"
        assert await _runtime(host).drain_background_once() is not None
        assert model.expression_calls == 1
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_stale_crash_proposal_is_closed_then_redeliberated_at_current_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash-durable Proposal cannot be authorized against a later World head."""

    model = _RevisionAwareExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "stale-proposal-redeliberates.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    # These cases isolate reply Proposal recovery.  Appraisal has its own
    # durable claim regression above and must not become a prerequisite here.
    runtime._interaction_appraisal_owner = None  # noqa: SLF001
    real_commit_visible_acceptance = runtime._commit_visible_acceptance  # noqa: SLF001

    async def crash_after_proposal(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection after ProposalRecorded")

    monkeypatch.setattr(
        runtime,
        "_commit_visible_acceptance",
        crash_after_proposal,
    )
    try:
        with pytest.raises(RuntimeError, match="after ProposalRecorded"):
            await _direct_inbound(
                host,
                message_id="stale-proposal-redeliberates",
                text="先形成回复，但在授权前崩溃。",
            )
        crashed = await host._host.action_due_projection()  # noqa: SLF001
        old_audit = next(
            item
            for item in crashed.proposal_audits
            if item.proposal_id.startswith(
                ("proposal:expression:", "proposal:chat-reply:")
            )
        )
        assert model.expression_calls == 1
        assert crashed.actions == ()
        assert crashed.expression_plan_manifests == ()

        advanced_at = NOW + timedelta(seconds=1)
        await _application(host).tick(
            tick_id="stale-proposal-world-advanced",
            logical_time_from=NOW,
            logical_time_to=advanced_at,
            observed_at=advanced_at,
            trace_id="trace:stale-proposal-world-advanced",
            causation_id="cause:stale-proposal-world-advanced",
            correlation_id="correlation:stale-proposal-world-advanced",
            reason="stale_proposal_recovery_regression",
            run_life_ecology=False,
        )
        monkeypatch.setattr(
            runtime,
            "_commit_visible_acceptance",
            real_commit_visible_acceptance,
        )

        stale = await runtime.drain_background_once()
        assert stale is not None
        assert stale.status == "deferred"
        assert "expression_acceptance.stale" in stale.deferred_refs
        assert model.expression_calls == 1
        after_stale = await host._host.action_due_projection()  # noqa: SLF001
        old_decision = next(
            item
            for item in after_stale.acceptance_decisions
            if item.proposal_id == old_audit.proposal_id
        )
        assert old_decision.status == "stale"

        recovered = await runtime.drain_background_once()
        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 2

        final = await host._host.action_due_projection()  # noqa: SLF001
        reply_audits = [
            item
            for item in final.proposal_audits
            if item.proposal_id.startswith(
                ("proposal:expression:", "proposal:chat-reply:")
            )
        ]
        assert len(reply_audits) == 2
        assert reply_audits[-1].proposal_id != old_audit.proposal_id
        assert all(
            item.proposal_id != old_audit.proposal_id
            for item in final.expression_plan_manifests
        )
        assert final.expression_plan_manifests[-1].proposal_id == (
            reply_audits[-1].proposal_id
        )
        episode = next(
            item
            for item in final.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_budget_rejected_crash_proposal_uses_capped_retry_schedule_without_spin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance outages are durable technical failures, not immediate busy loops."""

    model = _RevisionAwareExpressionModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "budget-rejected-proposal-retries.sqlite",
            PRIMARY_USER_ID="geoff",
            WORLD_V2_EXPRESSION_EPISODE_MODE="off",
        ),
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    runtime = _runtime(host)
    runtime._interaction_appraisal_owner = None  # noqa: SLF001
    real_commit_visible_acceptance = runtime._commit_visible_acceptance  # noqa: SLF001
    real_derive_expression_plan_material = (
        runtime_module.derive_expression_plan_material
    )

    async def crash_after_proposal(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection before budget acceptance")

    def budget_unavailable(**_kwargs):  # type: ignore[no-untyped-def]
        raise ExpressionPlanAcceptanceError("budget_unavailable")

    monkeypatch.setattr(
        runtime,
        "_commit_visible_acceptance",
        crash_after_proposal,
    )
    try:
        with pytest.raises(RuntimeError, match="before budget acceptance"):
            await _direct_inbound(
                host,
                message_id="budget-rejected-proposal-retries",
                text="回复已经写好，但预算边界暂时不可用。",
            )
        monkeypatch.setattr(
            runtime,
            "_commit_visible_acceptance",
            real_commit_visible_acceptance,
        )
        monkeypatch.setattr(
            runtime_module,
            "derive_expression_plan_material",
            budget_unavailable,
        )

        first_rejection = await runtime._drain_expression_retry_once()  # noqa: SLF001
        assert first_rejection is not None
        assert first_rejection.status == "deferred"
        assert "expression_acceptance.rejected" in first_rejection.deferred_refs
        assert model.expression_calls == 1
        first_projection = await host._host.action_due_projection()  # noqa: SLF001
        first_episode = next(
            item
            for item in first_projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        first_due = next_expression_retry_due(first_projection)
        assert first_episode.claim_lease is not None
        assert first_due == NOW + timedelta(minutes=10)
        assert {
            item.status for item in first_projection.acceptance_decisions
        } == {"rejected"}

        # Repeated scheduler polls before the exact due time are read-only and
        # neither regenerate prose nor append another rejection.
        assert await runtime._drain_expression_retry_once() is None  # noqa: SLF001
        no_spin = await host._host.action_due_projection()  # noqa: SLF001
        assert model.expression_calls == 1
        assert len(no_spin.acceptance_decisions) == 1
        assert next_expression_retry_due(no_spin) == first_due

        previous_at = NOW
        expected_delays = (timedelta(minutes=30), timedelta(minutes=120))
        expected_calls = (2, 3)
        for ordinal, (expected_delay, expected_call_count) in enumerate(
            zip(expected_delays, expected_calls, strict=True),
            start=1,
        ):
            due_at = next_expression_retry_due(
                await host._host.action_due_projection()  # noqa: SLF001
            )
            assert due_at is not None
            await _application(host).tick(
                tick_id=f"budget-retry-clock-{ordinal}",
                logical_time_from=previous_at,
                logical_time_to=due_at,
                observed_at=due_at,
                trace_id=f"trace:budget-retry-clock-{ordinal}",
                causation_id=f"cause:budget-retry-clock-{ordinal}",
                correlation_id="correlation:budget-retry",
                reason="expression_budget_retry_regression",
                run_life_ecology=False,
            )
            rejected = await runtime._drain_expression_retry_once()  # noqa: SLF001
            assert rejected is not None
            assert rejected.status == "deferred"
            assert "expression_acceptance.rejected" in rejected.deferred_refs
            assert model.expression_calls == expected_call_count
            projection = await host._host.action_due_projection()  # noqa: SLF001
            episode = next(
                item
                for item in projection.trigger_processes
                if item.process_kind == "expression_episode"
            )
            assert episode.claim_lease is not None
            next_due = next_expression_retry_due(projection)
            assert next_due == due_at + expected_delay
            assert len(projection.acceptance_decisions) == ordinal + 1
            assert await runtime._drain_expression_retry_once() is None  # noqa: SLF001
            assert model.expression_calls == expected_call_count
            previous_at = due_at

        monkeypatch.setattr(
            runtime_module,
            "derive_expression_plan_material",
            real_derive_expression_plan_material,
        )
        final_due = next_expression_retry_due(
            await host._host.action_due_projection()  # noqa: SLF001
        )
        assert final_due is not None
        await _application(host).tick(
            tick_id="budget-retry-clock-recovered",
            logical_time_from=previous_at,
            logical_time_to=final_due,
            observed_at=final_due,
            trace_id="trace:budget-retry-clock-recovered",
            causation_id="cause:budget-retry-clock-recovered",
            correlation_id="correlation:budget-retry",
            reason="expression_budget_retry_recovered",
            run_life_ecology=False,
        )
        recovered = await runtime._drain_expression_retry_once()  # noqa: SLF001
        assert recovered is not None
        assert recovered.status == "action_authorized"
        assert model.expression_calls == 4
        final = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in final.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 4
        assert next_expression_retry_due(final) is None
    finally:
        await host.aclose()


@pytest.mark.asyncio
async def test_on_episode_can_complete_from_action_receipt_after_claim_lease(
    tmp_path: Path,
) -> None:
    """Provider latency must not make a model-authored append episode unfinishable."""

    model = _AppendEpisodeModel()
    host = build_qq_c2c_host(
        settings=Settings(
            database_path=tmp_path / "late-receipt-expression-episode.sqlite",
            PRIMARY_USER_ID="geoff",
        ),
        _test_only_expression_episode_mode="on",
        recipient_id="10001",
        bootstrap_at=NOW,
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        outcome = await _direct_inbound(
            host,
            message_id="late-receipt-expression-episode",
            text="这条回执可能过很久才回来。",
        )
        assert outcome.status == "action_authorized"
        assert outcome.authorized_action_ids

        before = await host._host.action_due_projection()  # noqa: SLF001
        episode_before = next(
            item
            for item in before.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode_before.state == "claimed"
        assert episode_before.claim_lease is not None
        action = next(
            item
            for item in before.actions
            if item.action_id == outcome.authorized_action_ids[0]
        )

        receipt_at = episode_before.claim_lease.expires_at + timedelta(seconds=1)
        await _application(host).tick(
            tick_id="late-expression-receipt-clock",
            logical_time_from=NOW,
            logical_time_to=receipt_at,
            observed_at=receipt_at,
            trace_id="trace:late-expression-receipt-clock",
            causation_id="cause:late-expression-receipt-clock",
            correlation_id="correlation:late-expression-receipt",
            reason="expression_episode_late_receipt_regression",
            run_life_ecology=False,
        )
        receipt = await _application(host).receipt(
            source="provider:test",
            source_event_id="late-expression-receipt",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            status="provider_accepted",
            provider_ref="provider-ref:late-expression-receipt",
            observed_at=receipt_at,
            trace_id="trace:late-expression-receipt",
            causation_id="cause:late-expression-receipt",
            correlation_id="correlation:late-expression-receipt",
            raw_payload_hash="raw:late-expression-receipt",
        )

        # Settlement may also open a durable external-result cognition trigger,
        # so its host-facing status is allowed to be deferred.  The reliability
        # assertion below is the expression episode's own terminal state.
        assert receipt.status in {"observed_only", "deferred"}
        projection = await host._host.action_due_projection()  # noqa: SLF001
        episode = next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "expression_episode"
        )
        assert episode.state == "terminal"
        assert len(episode.attempt_ids) == 2
        assert episode.runtime_outcome_ref is not None
        assert episode.runtime_outcome_ref.endswith(":provider_accepted")
    finally:
        await host.aclose()
