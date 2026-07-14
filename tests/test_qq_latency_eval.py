import asyncio
import json

import pytest

from companion_daemon.models import CompanionReply, IncomingMessage
from companion_daemon.conversation_cadence import ConversationCadence
from companion_daemon.qq_latency_eval import (
    QQLatencySummary,
    _main,
    assess_live_qq_observation_evidence,
    qq_latency_observation_jsonl_report,
    qq_latency_report,
    run_synthetic_qq_latency_smoke,
    summarize_qq_latency,
    summarize_qq_latency_observation_rows,
)
from companion_daemon.qq_websocket import QQMessageCoalescer, TurnRuntimeObservation
from companion_daemon.turn_taking import TurnTakingPolicy


@pytest.mark.asyncio
async def test_qq_latency_smoke_records_coalescing_and_visible_receipt_by_cadence() -> None:
    observations = await run_synthetic_qq_latency_smoke()

    assert {item.cadence for item in observations} == {"cold", "warm", "hot"}
    assert all(item.input_count == 1 for item in observations)
    assert all(item.coalescing_wait_seconds is not None for item in observations)
    assert all(item.first_visible_elapsed_seconds is not None for item in observations)
    assert all(
        item.first_visible_elapsed_seconds >= item.coalescing_wait_seconds
        for item in observations
        if item.first_visible_elapsed_seconds is not None
        and item.coalescing_wait_seconds is not None
    )

    summary = {item.cadence: item for item in summarize_qq_latency(observations)}
    assert summary["all"].sample_count == 3
    for cadence in ("cold", "warm", "hot"):
        assert summary[cadence].sample_count == 1
        assert summary[cadence].visible_count == 1
        assert summary[cadence].p50_first_visible_ms is not None

    report = qq_latency_report(observations)
    assert json.loads(json.dumps(report))["observations"][0]["observed_at"]


@pytest.mark.asyncio
async def test_qq_latency_starts_at_first_input_when_a_new_message_resets_debounce() -> None:
    class Clock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

    class Engine:
        def conversation_cadence(self, _incoming: IncomingMessage) -> ConversationCadence:
            return ConversationCadence("hot", 10.0, 3, "active_back_and_forth")

        async def handle_message(self, _incoming: IncomingMessage) -> CompanionReply:
            return CompanionReply(canonical_user_id="eval", mood="calm", text="收到。")

    class Target:
        async def reply(self, **_kwargs: object) -> dict[str, str]:
            return {"id": "qq-receipt"}

    clock = Clock()
    sleeping = asyncio.Event()
    release = asyncio.Event()

    async def controllable_sleep(seconds: float) -> None:
        sleeping.set()
        await release.wait()
        clock.now += seconds

    observations: list[TurnRuntimeObservation] = []
    coalescer = QQMessageCoalescer(
        Engine(),  # type: ignore[arg-type]
        delay_seconds=0.1,
        turn_policy=TurnTakingPolicy(short_wait_seconds=0.1, long_wait_seconds=0.1),
        sleep=controllable_sleep,
        monotonic=clock.monotonic,
        on_turn_observation=observations.append,
    )
    target = Target()
    await coalescer.add(
        "c2c:eval",
        IncomingMessage(platform="qq", platform_user_id="eval", text="第一句说完了。"),
        target,
    )
    await sleeping.wait()
    clock.now = 0.4
    await coalescer.add(
        "c2c:eval",
        IncomingMessage(platform="qq", platform_user_id="eval", text="第二句也说完了。"),
        target,
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.sleep(0)

    assert len(observations) == 1
    observation = observations[0]
    assert observation.cadence == "hot"
    assert observation.input_count == 2
    assert observation.coalescing_wait_seconds == pytest.approx(0.5)
    assert observation.first_visible_elapsed_seconds == pytest.approx(0.5)


def test_live_observation_jsonl_latency_report_reuses_redacted_rows(tmp_path) -> None:
    report_path = tmp_path / "qq-turns.jsonl"
    report_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": 2,
                        "outcome": "reply_delivered",
                        "cadence": "hot",
                        "elapsed_ms": 900,
                        "first_visible_elapsed_ms": 500,
                        "message_kinds": ["reply"],
                        "segment_count": 1,
                        "selected_affordance_kind": "soft_repair",
                        "user_affect_kinds": ["disappointment"],
                        "user_affect_recorded": True,
                        "private_impression_recorded": True,
                    }
                ),
                json.dumps(
                    {
                        "schema_version": 2,
                        "outcome": "reply_delivered",
                        "cadence": "hot",
                        "elapsed_ms": 1200,
                        "first_visible_elapsed_ms": 800,
                        "message_kinds": ["reply", "afterthought"],
                        "segment_count": 2,
                        "multi_segment": True,
                        "selected_affordance_kind": "delayed_afterthought",
                        "user_affect_kinds": [],
                    }
                ),
                json.dumps(
                    {
                        "schema_version": 2,
                        "outcome": "reply_delivered",
                        "cadence": "cold",
                        "elapsed_ms": 3000,
                        "first_visible_elapsed_ms": 2400,
                        "message_kinds": ["reply"],
                        "segment_count": 1,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    row_summary = {
        item.cadence: item
        for item in summarize_qq_latency_observation_rows(
            [
                {"cadence": "hot", "elapsed_ms": 900, "first_visible_elapsed_ms": 500},
                {"cadence": "hot", "elapsed_ms": 1200, "first_visible_elapsed_ms": 800},
                {"cadence": "cold", "elapsed_ms": 3000, "first_visible_elapsed_ms": 2400},
            ]
        )
    }
    assert row_summary["all"].sample_count == 3
    assert row_summary["hot"].p50_first_visible_ms == 500
    assert row_summary["hot"].p95_complete_ms == 1200
    assert row_summary["cold"].p50_complete_ms == 3000

    report = qq_latency_observation_jsonl_report(report_path)

    assert report["live"] is True
    assert report["source"] == "redacted_qq_turn_observation_jsonl"
    assert report["evidence_status"] == "insufficient_evidence"
    assert report["evidence_reasons"]
    summaries = {item["cadence"]: item for item in report["summaries"]}  # type: ignore[index]
    assert summaries["hot"]["sample_count"] == 2
    assert summaries["hot"]["p95_first_visible_ms"] == 800
    experience = report["experience_summary"]  # type: ignore[assignment]
    assert experience["sample_count"] == 3  # type: ignore[index]
    assert experience["multi_segment_count"] == 1  # type: ignore[index]
    assert experience["afterthought_count"] == 1  # type: ignore[index]
    assert experience["user_affect_recorded_count"] == 1  # type: ignore[index]
    assert report["privacy"] == {
        "contains_message_text": False,
        "contains_user_or_platform_identifier": False,
        "contains_external_receipts": False,
        "contains_free_form_failure_reason": False,
    }


def test_live_qq_observation_evidence_status_requires_real_hot_sample_size() -> None:
    insufficient = assess_live_qq_observation_evidence(
        (
            QQLatencySummary("all", 3, 3, 500, 800, 900, 1200),
            QQLatencySummary("hot", 3, 3, 500, 800, 900, 1200),
        )
    )

    assert insufficient["status"] == "insufficient_evidence"
    assert "Need at least" in str(insufficient["reasons"])

    passed = assess_live_qq_observation_evidence(
        (
            QQLatencySummary("all", 8, 8, 900, 1600, 1200, 2200),
            QQLatencySummary("hot", 8, 8, 900, 1600, 1200, 2200),
        )
    )

    assert passed == {"status": "pass", "reasons": []}

    latency_watch = assess_live_qq_observation_evidence(
        (
            QQLatencySummary("all", 8, 8, 2000, 7600, 2600, 9000),
            QQLatencySummary("hot", 8, 8, 2000, 7600, 2600, 9000),
        )
    )

    assert latency_watch["status"] == "latency_watch"
    assert "exceeds" in str(latency_watch["reasons"])


def test_live_qq_observation_cli_can_assert_evidence_status(tmp_path, capsys) -> None:
    insufficient_path = tmp_path / "insufficient.jsonl"
    insufficient_path.write_text("", encoding="utf-8")

    assert _main(("--observation-jsonl", str(insufficient_path), "--assert-live-evidence")) == 1
    insufficient_output = json.loads(capsys.readouterr().out)
    assert insufficient_output["evidence_status"] == "insufficient_evidence"

    passing_path = tmp_path / "passing.jsonl"
    passing_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": 2,
                    "outcome": "reply_delivered",
                    "cadence": "hot",
                    "elapsed_ms": 1100 + index,
                    "first_visible_elapsed_ms": 900 + index,
                    "message_kinds": ["reply"],
                    "segment_count": 1,
                }
            )
            for index in range(8)
        )
        + "\n",
        encoding="utf-8",
    )

    assert _main(("--observation-jsonl", str(passing_path), "--assert-live-evidence")) == 0
    passing_output = json.loads(capsys.readouterr().out)
    assert passing_output["evidence_status"] == "pass"
