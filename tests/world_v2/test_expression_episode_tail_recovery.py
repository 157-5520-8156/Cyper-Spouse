from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


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


class _DurableDispositionModel:
    """Let the full proposal win so its disposition is durable before the crash."""

    model = "fixture:durable-expression-disposition"

    def __init__(self, disposition: str) -> None:
        self.disposition = disposition
        self.expression_calls = 0
        self._fallback = FakeCompanionModel()

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        prompt = "\n".join(message["content"] for message in messages)
        if not _is_expression_prompt(prompt):
            return await self._fallback.complete(messages, temperature=temperature)
        self.expression_calls += 1
        provisional = "provisional first beat" in prompt
        if provisional:
            # Expression Episode `on` races the first beat with the full
            # proposal.  This fixture intentionally makes the full result the
            # winner so episode_disposition lives in its immutable audit.
            await asyncio.sleep(0.2)
        draft: dict[str, object] = {
            "private_turn_state": {
                "inner_state_summary": "I want to answer once, then stop this episode.",
                "attended_source_refs": [],
            },
            "timing_choice": "now",
            "cadence": "conversational",
            "beats": [{"modality": "text", "text": "这条待发送内容只生成一次。"}],
            "stance": "answer_directly",
            "brief_rationale": "Answer the current observation without inventing facts.",
            "confidence": 7600,
            "world_claims": [],
        }
        if not provisional:
            draft["episode_disposition"] = self.disposition
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

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-{len(self.sent)}"}}

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


def _runtime(host):  # type: ignore[no-untyped-def]
    return host._host._application._turns._runtime  # noqa: SLF001


async def _recover_expression(host, *, observation_id: str):  # type: ignore[no-untyped-def]
    for _ in range(32):
        outcome = await host._host.drain_background_once()  # noqa: SLF001
        if getattr(outcome, "observation_ref", None) == observation_id:
            return outcome
    raise AssertionError("durable Expression Episode was not recovered")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "disposition",
    ("cancel_pending", "supersede_pending"),
)
async def test_restart_recovers_durable_tail_cancellation_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
) -> None:
    database = tmp_path / f"episode-tail-recovery-{disposition}.sqlite"
    model = _DurableDispositionModel(disposition)
    first = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
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
    runtime = _runtime(first)

    async def crash_before_acceptance(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection between Proposal and Acceptance")

    monkeypatch.setattr(runtime, "_commit_visible_acceptance", crash_before_acceptance)
    try:
        with pytest.raises(RuntimeError, match="between Proposal and Acceptance"):
            await first._host._application.inbound(  # noqa: SLF001
                platform="qq",
                platform_user_id="10001",
                platform_message_id=f"tail-recovery-{disposition}",
                text="先把这个决定记下来，再测试崩溃恢复。",
                observed_at=NOW,
                trace_id=f"trace:tail-recovery-{disposition}",
            )
        before = await first._host.action_due_projection()  # noqa: SLF001
    finally:
        await first.aclose()

    observation_id = before.message_observations[-1].observation_id
    assert any(
        disposition in audit.proposal_json for audit in before.proposal_audits
    ), [audit.proposal_json for audit in before.proposal_audits]
    assert before.actions == ()
    calls_before_restart = model.expression_calls

    delivery = _Delivery()
    restarted = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
        ),
        _test_only_expression_episode_mode="on",
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=delivery,
        use_configured_recall_embedding=False,
    )
    try:
        recovered = await _recover_expression(
            restarted,
            observation_id=observation_id,
        )
        projection = await restarted._host.action_due_projection()  # noqa: SLF001
    finally:
        await restarted.aclose()

    assert recovered.status == "observed_only"
    assert recovered.authorized_action_ids == ()
    assert model.expression_calls == calls_before_restart
    assert delivery.sent == []
    assert projection.actions
    assert {action.state for action in projection.actions} == {"cancelled"}
    assert {
        reservation.state
        for reservation in projection.budget_reservations
        if reservation.action_id in {action.action_id for action in projection.actions}
    } == {"released"}
    assert {plan.state for plan in projection.expression_plans} == {"terminated"}
    episode = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"
    assert disposition in (episode.runtime_outcome_ref or "")


@pytest.mark.asyncio
async def test_restart_recovers_complete_without_more_and_terminates_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "episode-tail-recovery-complete.sqlite"
    model = _DurableDispositionModel("complete_without_more")
    first = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
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
    runtime = _runtime(first)

    async def crash_before_acceptance(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("fault injection between Proposal and Acceptance")

    monkeypatch.setattr(runtime, "_commit_visible_acceptance", crash_before_acceptance)
    try:
        with pytest.raises(RuntimeError, match="between Proposal and Acceptance"):
            await first._host._application.inbound(  # noqa: SLF001
                platform="qq",
                platform_user_id="10001",
                platform_message_id="tail-recovery-complete",
                text="这轮只需要当前这一条。",
                observed_at=NOW,
                trace_id="trace:tail-recovery-complete",
            )
        before = await first._host.action_due_projection()  # noqa: SLF001
    finally:
        await first.aclose()

    observation_id = before.message_observations[-1].observation_id
    calls_before_restart = model.expression_calls
    restarted = build_qq_c2c_host(
        settings=Settings(
            database_path=database,
            PRIMARY_USER_ID="geoff",
        ),
        _test_only_expression_episode_mode="on",
        recipient_id="10001",
        bootstrap_at=NOW + timedelta(seconds=1),
        model=model,
        advisory_model=FakeCompanionModel(),
        delivery=_Delivery(),
        use_configured_recall_embedding=False,
    )
    try:
        recovered = await _recover_expression(
            restarted,
            observation_id=observation_id,
        )
        projection = await restarted._host.action_due_projection()  # noqa: SLF001
    finally:
        await restarted.aclose()

    assert recovered.status == "action_authorized"
    assert recovered.authorized_action_ids
    assert model.expression_calls == calls_before_restart
    assert {action.state for action in projection.actions} == {"authorized"}
    episode = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "expression_episode"
    )
    assert episode.state == "terminal"
    assert episode.runtime_outcome_ref == "expression-episode:complete_without_more"
