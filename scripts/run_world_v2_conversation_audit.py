"""Run an isolated real-model newcomer journey through the public World-v2 QQ seam."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import time

from companion_daemon.config import Settings
from companion_daemon.world_v2.conversation_audit_acceptance import (
    evaluate_conversation_acceptance,
)
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


class AuditDelivery:
    """Capture provider-visible text without touching QQ or production state."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {
            "status": "ok",
            "data": {"message_id": f"world-v2-audit-{len(self.sent)}"},
        }

    async def send_reaction(self, recipient_id: str, *, message_id: str, reaction_id: str):
        self.sent.append((recipient_id, f"[reaction:{message_id}:{reaction_id}]"))
        return {"status": "ok", "data": {"message_id": f"world-v2-audit-{len(self.sent)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str):
        self.sent.append((recipient_id, f"[sticker:{sticker_id}]"))
        return {"status": "ok", "data": {"message_id": f"world-v2-audit-{len(self.sent)}"}}

    async def send_typing(self, recipient_id: str, *, state: str):
        self.sent.append((recipient_id, f"[typing:{state}]"))
        return {"status": "ok", "data": {"message_id": f"world-v2-audit-{len(self.sent)}"}}


async def run(*, database: Path, fixture: Path, output: Path) -> list[dict[str, object]]:
    document = json.loads(fixture.read_text(encoding="utf-8"))
    turns = document["turns"]
    database.unlink(missing_ok=True)
    database.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    delivery = AuditDelivery()
    fixture_start = document.get("started_at_local")
    if fixture_start is not None:
        if not isinstance(fixture_start, str):
            raise ValueError("fixture started_at_local must be an ISO-8601 string")
        parsed_start = datetime.fromisoformat(fixture_start)
        if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
            raise ValueError("fixture started_at_local must include an explicit UTC offset")
        started_at = parsed_start.astimezone(UTC).replace(microsecond=0)
    else:
        started_at = datetime.now(UTC).replace(microsecond=0)
    settings = Settings(database_path=database, PRIMARY_USER_ID="geoff")
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="world-v2-audit-user",
        bootstrap_at=started_at,
        delivery=delivery,
    )
    rows: list[dict[str, object]] = []
    # Production wakes every few seconds.  Five simulated minutes is coarse
    # enough to keep this real-model audit bounded while still allowing a
    # multi-stage life chain to progress during the hour-long conversation.
    conversation_scheduler_interval_minutes = 5
    next_scheduler_minute = conversation_scheduler_interval_minutes
    try:
        for index, turn in enumerate(turns, 1):
            turn_minute = int(turn["at_minutes"])
            between_turn_messages: list[str] = []
            between_turn_scheduler_errors: list[str] = []
            while next_scheduler_minute <= turn_minute:
                scheduler_before = len(delivery.sent)
                try:
                    await host.scheduler_once(
                        observed_at=started_at + timedelta(minutes=next_scheduler_minute),
                        max_action_units=4,
                        max_background_units=8,
                    )
                except Exception as exc:
                    between_turn_scheduler_errors.append(repr(exc))
                between_turn_messages.extend(
                    text for _recipient, text in delivery.sent[scheduler_before:]
                )
                next_scheduler_minute += conversation_scheduler_interval_minutes
            before = len(delivery.sent)
            existing_trace_ids = {sample.trace_id for sample in host.latency_samples()}
            wall_started = time.perf_counter()
            observed_at = started_at + timedelta(minutes=turn_minute)
            error = None
            reply_latency_ms = None
            background_ms = 0.0
            try:
                outcome = await host.inbound_text(
                    message_id=f"real-audit-{turn['id']}",
                    recipient_id="world-v2-audit-user",
                    text=str(turn["text"]),
                    observed_at=observed_at,
                )
                reply_latency_ms = round((time.perf_counter() - wall_started) * 1000, 1)
                await host.drain(max_action_units=8, max_background_units=0)
                # Settle a bounded amount of source-owned cognition around
                # identity/fact, offense/repair, and life-grounding probes.
                if turn["id"] in {"T04", "T09", "T14", "T21", "T28"}:
                    background_started = time.perf_counter()
                    await host.drain(max_action_units=0, max_background_units=8)
                    background_ms = round(
                        (time.perf_counter() - background_started) * 1000, 1
                    )
                status = outcome.status
            except Exception as exc:  # retain failure evidence and continue the journey
                error = repr(exc)
                status = "error"
            replies = [text for _recipient, text in delivery.sent[before:]]
            new_latency_samples = tuple(
                sample
                for sample in host.latency_samples()
                if sample.trace_id not in existing_trace_ids
            )
            row = {
                "turn": index,
                "turn_id": turn["id"],
                "user": turn["text"],
                "between_turn_messages": between_turn_messages,
                "between_turn_scheduler_errors": between_turn_scheduler_errors,
                "replies": replies,
                "status": status,
                "reply_latency_ms": reply_latency_ms,
                "background_ms": background_ms,
                "latency_ms": round((time.perf_counter() - wall_started) * 1000, 1),
                "latency_segments_ms": {
                    sample.segment: round(sample.duration_ms, 1)
                    for sample in new_latency_samples
                },
                "startup": new_latency_samples[0].startup if new_latency_samples else None,
                "error": error,
            }
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)

        silence = document["after_silence"]
        before = len(delivery.sent)
        scheduler_error = None
        scheduler_ticks = 0
        try:
            # Production advances on recurring scheduler wakes.  Jumping the
            # clock once by several hours can only open one LifeAuthor family,
            # so it cannot exercise plan -> activity -> occurrence ->
            # experience and gives a false "empty world" result.  Replay the
            # silence as bounded 15-minute wakes, matching the public
            # scheduler seam without manufacturing any life event directly.
            anchor_minutes = int(turns[-1]["at_minutes"])
            advance_minutes = int(silence["advance_minutes"])
            silence_target_minute = anchor_minutes + advance_minutes
            # After the dense conversation window, use a production-like
            # bounded 15-minute audit cadence rather than carrying a partial
            # five-minute boundary into the silence window.
            next_scheduler_minute = anchor_minutes + 15
            while next_scheduler_minute <= silence_target_minute:
                await host.scheduler_once(
                    observed_at=started_at + timedelta(minutes=next_scheduler_minute),
                    max_action_units=4,
                    max_background_units=8,
                )
                scheduler_ticks += 1
                next_scheduler_minute += 15
            if next_scheduler_minute - 15 < silence_target_minute:
                await host.scheduler_once(
                    observed_at=started_at + timedelta(minutes=silence_target_minute),
                    max_action_units=4,
                    max_background_units=8,
                )
                scheduler_ticks += 1
        except Exception as exc:
            scheduler_error = repr(exc)
        rows.append(
            {
                "after_silence": True,
                "replies": [text for _recipient, text in delivery.sent[before:]],
                "scheduler_ticks": scheduler_ticks,
                "error": scheduler_error,
            }
        )
    finally:
        await host.aclose()
        with sqlite3.connect(database) as connection:
            event_rows = connection.execute(
                "SELECT event_json FROM world_v2_events ORDER BY ledger_sequence"
            ).fetchall()
        parsed_events = tuple(json.loads(raw_event_json) for (raw_event_json,) in event_rows)
        event_types = Counter(str(event["event_type"]) for event in parsed_events)
        proactive_opened = 0
        proactive_completed = 0
        proactive_considered = 0
        proactive_considered_silent = 0
        proactive_local_failsafe = 0
        proactive_silent = 0
        proactive_failed_safe = 0
        for event in parsed_events:
            try:
                payload = json.loads(str(event["payload_json"]))
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if event.get("event_type") == "TriggerProcessOpened":
                process = payload.get("process")
                if isinstance(process, dict) and process.get("process_kind") == (
                    "proactive_action_deliberation"
                ):
                    proactive_opened += 1
            if event.get("event_type") == "ProposalRecorded":
                proposal_json = payload.get("proposal_json")
                try:
                    proposal = json.loads(proposal_json) if isinstance(proposal_json, str) else None
                except json.JSONDecodeError:
                    proposal = None
                basis = (
                    proposal.get("proactive_opportunity_decision")
                    if isinstance(proposal, dict)
                    else None
                )
                if isinstance(basis, dict):
                    is_model = basis.get("decision_origin") == "model"
                    proactive_considered += int(is_model)
                    proactive_considered_silent += int(
                        is_model
                        and basis.get("disposition") == "silent_after_consideration"
                        and proposal.get("timing_choice") == "silent"
                    )
                    proactive_local_failsafe += int(
                        basis.get("decision_origin") == "local_failsafe"
                    )
            if event.get("event_type") != "TriggerProcessCompleted":
                continue
            outcome_ref = payload.get("runtime_outcome_ref")
            if isinstance(outcome_ref, str) and outcome_ref.startswith("proactive:"):
                proactive_completed += 1
                proactive_silent += int(outcome_ref == "proactive:silent")
                proactive_failed_safe += int(
                    outcome_ref.startswith("proactive:deliberation-failed:")
                )
        rows.append(
            {
                "ledger_evidence": True,
                "event_count": sum(event_types.values()),
                "event_type_counts": dict(sorted(event_types.items())),
                "proactive_evidence": {
                    "opened": proactive_opened,
                    "completed": proactive_completed,
                    "considered": proactive_considered,
                    "considered_silent": proactive_considered_silent,
                    "local_failsafe": proactive_local_failsafe,
                    "silent": proactive_silent,
                    "failed_safe": proactive_failed_safe,
                },
            }
        )
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/world_v2/fixtures/new_acquaintance_32_turns.json"),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when the real-journey hard acceptance smoke gate fails.",
    )
    args = parser.parse_args()
    result_rows = asyncio.run(run(database=args.database, fixture=args.fixture, output=args.output))
    turn_count = sum(isinstance(row.get("turn_id"), str) for row in result_rows)
    acceptance = (
        evaluate_conversation_acceptance(result_rows)
        if turn_count >= 30
        else {"applicable": False, "reason": "strict acceptance requires a 30+ turn journey"}
    )
    print(json.dumps({"acceptance": acceptance}, ensure_ascii=False), flush=True)
    if args.strict and acceptance.get("passed") is not True:
        raise SystemExit(2)
