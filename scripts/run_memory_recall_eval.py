#!/usr/bin/env python3
"""Measure whether the companion actually remembers, through the public QQ seam.

This is an experience-side eval, not a mechanism test.  The suite already
proves that capsules compile, facts commit and ledgers replay.  None of that
answers the only question a user cares about: when she was told something a
week ago, does it come back?

The eval separates three failure modes that all look identical from the
outside:

  retrieval_miss        the fact never reached the model at all
  supplied_but_unused   the fact was in the model-facing context, model ignored it
  recalled              the fact reached the model and surfaced in the reply

It therefore captures two different context sizes on every turn:

  capsule-side    pre-compaction slice counts, read from the
                  ``pinned turn mechanism consumption`` log line
  model-facing    post-compaction context actually handed to the provider,
                  captured by wrapping the flash model

Those two numbers diverge, and the divergence is the point.  Eviction happens
against the capsule budget, so facts can be dropped *before* the compactor
ever runs.  Reporting only the model-facing count hides where the loss occurs.

Isolation: the eval requires a new database path under a system temporary
root. It never unlinks or opens the configured production ledger.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import statistics
import sys
import tempfile
import time

from companion_daemon.config import Settings
from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host

_HERE = Path(__file__).resolve().parent

_CONSUMPTION_RE = re.compile(
    r"pinned turn mechanism consumption trace=(?P<trace>\S+) summary=(?P<summary>\{.*\})"
)
# The delivery seam also carries typing/reaction/sticker control markers.
# Counting them as speech would make every turn look non-silent and would let a
# probe "pass" on a marker that contains no words at all.
_CONTROL_MARKER_RE = re.compile(r"^\[(typing|reaction|sticker):")
_SLICES_OF_INTEREST = (
    "relevant_facts",
    "recent_dialogue",
    "active_memory_candidates",
    "advisories",
    "private_impressions",
    "recent_experiences",
)


def assert_safe_eval_paths(
    *,
    database: Path,
    output: Path,
    production_database: Path,
) -> None:
    """Reject any scratch path that resolves onto durable production state."""

    requested_database = database.expanduser().resolve()
    configured_production = production_database.expanduser().resolve()
    requested_output = output.expanduser().resolve()
    protected = {
        configured_production,
        Path(str(configured_production) + "-wal"),
        Path(str(configured_production) + "-shm"),
    }
    if requested_database in protected or requested_output in protected:
        raise ValueError(
            "memory recall eval refuses to replace configured production "
            f"database: {configured_production}"
        )
    if requested_output == requested_database:
        raise ValueError("memory recall eval output must not overwrite its database")
    scratch_roots = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
    }
    output_root = (_HERE.parent / "output" / "memory-eval").resolve()
    if not any(requested_database.is_relative_to(root) for root in scratch_roots):
        raise ValueError(
            "memory recall eval database must be under a system temporary root"
        )
    if requested_database.exists():
        raise ValueError("memory recall eval database must be a new scratch path")
    if not (
        any(requested_output.is_relative_to(root) for root in scratch_roots)
        or requested_output.is_relative_to(output_root)
    ):
        raise ValueError(
            f"memory recall eval output must be under a system temporary root or {output_root}"
        )


class EvalDelivery:
    """Capture provider-visible text without touching QQ or production state."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"memory-eval-{len(self.sent)}"}}

    async def send_reaction(self, recipient_id: str, *, message_id: str, reaction_id: str):
        self.sent.append((recipient_id, f"[reaction:{message_id}:{reaction_id}]"))
        return {"status": "ok", "data": {"message_id": f"memory-eval-{len(self.sent)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str):
        self.sent.append((recipient_id, f"[sticker:{sticker_id}]"))
        return {"status": "ok", "data": {"message_id": f"memory-eval-{len(self.sent)}"}}

    async def send_typing(self, recipient_id: str, *, state: str):
        self.sent.append((recipient_id, f"[typing:{state}]"))
        return {"status": "ok", "data": {"message_id": f"memory-eval-{len(self.sent)}"}}


class ConsumptionCapture(logging.Handler):
    """Read capsule-side (pre-compaction) slice counts off the existing log line.

    ``pinned_turn`` already emits exactly the operator evidence this eval
    needs.  Reading it is preferable to reaching into private capsule state:
    it measures the same thing production measures, through the same seam.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.summaries: list[dict[str, object]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            return
        match = _CONSUMPTION_RE.search(message)
        if match is None:
            return
        try:
            summary = json.loads(match.group("summary"))
        except json.JSONDecodeError:
            return
        if isinstance(summary, dict):
            summary["trace_id"] = match.group("trace")
            self.summaries.append(summary)


class RecordingModel:
    """Wrap the flash model to capture the post-compaction model-facing context.

    The capsule's own ``model_content_json`` is logged pre-compaction.  What
    the provider actually receives is compacted further
    (``compact_chat_model_facing_context`` strips the authority envelope and
    applies per-slice item caps).  Only by reading the outbound request can we
    tell whether a specific remembered value survived all the way to the model.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.model = getattr(inner, "model", "recording")
        self.provider = getattr(inner, "provider", "recording")
        fallback = getattr(inner, "fallback", None)
        if fallback is not None:
            self.fallback = fallback
        self.captures: list[str] = []
        self.outputs: list[str] = []

    def _capture(self, messages) -> None:  # type: ignore[no-untyped-def]
        try:
            envelope = json.loads(messages[1]["content"])
            raw = str(envelope.get("request", {}).get("model_content_json", ""))
        except Exception:
            raw = ""
        if raw:
            self.captures.append(raw)

    async def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self._capture(messages)
        output = await self._inner.complete(messages, **kwargs)  # type: ignore[attr-defined]
        self.outputs.append(output)
        return output

    async def complete_json(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self._capture(messages)
        structured = getattr(self._inner, "complete_json", None)
        if callable(structured):
            output = await structured(messages, **kwargs)
        else:
            output = await self._inner.complete(messages, **kwargs)  # type: ignore[attr-defined]
        self.outputs.append(output)
        return output


def _slice_stats(raw: str) -> dict[str, object]:
    """Item count and character cost per slice of one model-facing context."""

    try:
        context = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"status": "invalid", "slices": {}, "total_characters": len(raw)}
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        return {"status": "missing_slices", "slices": {}, "total_characters": len(raw)}
    out: dict[str, object] = {}
    for name in _SLICES_OF_INTEREST:
        lane = slices.get(name)
        if not isinstance(lane, dict) or lane.get("availability") != "available":
            out[name] = {"items": 0, "characters": 0}
            continue
        items = lane.get("items")
        items = items if isinstance(items, list) else []
        out[name] = {
            "items": len(items),
            "characters": len(json.dumps(items, ensure_ascii=False)),
        }
    return {"status": "ok", "slices": out, "total_characters": len(raw)}


def _memory_slice_text(raw: str) -> str:
    """Serialize the authoritative retrieval-memory slices of a Context.

    Searching the whole context for a remembered term gives false positives:
    the user's own message is in ``recent_dialogue``, so probing about a
    coffee shop would "find" 咖啡 and score as a successful recall of a fact
    stored days earlier. Retrieval is credited only when the term reached
    ``relevant_facts`` or ``active_memory_candidates``.
    """

    try:
        context = json.loads(raw)
        slices = context["slices"]
    except (TypeError, KeyError, json.JSONDecodeError):
        return ""
    items: list[object] = []
    for name in ("relevant_facts", "active_memory_candidates"):
        lane = slices.get(name) if isinstance(slices, dict) else None
        lane_items = lane.get("items") if isinstance(lane, dict) else None
        if isinstance(lane_items, list):
            items.extend(lane_items)
    return json.dumps(items, ensure_ascii=False)


def _score_probe(
    probe: dict[str, object],
    reply_text: str,
    context_text: str,
    memory_text: str,
) -> dict[str, object]:
    """Classify one probe into the retrieval/expression quadrant."""

    expect_any = [str(value) for value in probe.get("expect_any", [])]
    groups = probe.get("expect_all_groups")
    if isinstance(groups, list) and groups:
        hit = all(
            any(str(option) in reply_text for option in group)
            for group in groups
            if isinstance(group, list)
        )
        matched = [
            str(option)
            for group in groups
            if isinstance(group, list)
            for option in group
            if str(option) in reply_text
        ]
    else:
        matched = [value for value in expect_any if value in reply_text]
        hit = bool(matched)
    context_any = [str(value) for value in probe.get("context_any", [])]
    memory_matched = [value for value in context_any if value in memory_text]
    if isinstance(groups, list) and groups:
        in_memory = all(
            any(str(option) in memory_text for option in group)
            for group in groups
            if isinstance(group, list)
        )
    else:
        in_memory = bool(memory_matched)
    elsewhere = [
        value for value in context_any if value not in memory_text and value in context_text
    ]
    if hit and in_memory:
        verdict = "recalled"
    elif hit:
        # The reply named the value without it being in the memory slice, so
        # it came from the still-visible dialogue tail rather than from memory.
        verdict = "recalled_from_dialogue_tail"
    elif in_memory:
        verdict = "supplied_but_unused"
    else:
        verdict = "retrieval_miss"
    return {
        "key": probe.get("key"),
        "style": probe.get("style"),
        "distance_days": probe.get("distance_days"),
        "hit": hit,
        "in_memory": in_memory,
        # Compatibility for older result consumers.
        "in_facts": in_memory,
        "verdict": verdict,
        "matched_in_reply": matched,
        "matched_in_memory": memory_matched,
        "matched_in_facts": memory_matched,
        "present_outside_facts": elsewhere,
    }


def _score_negative_probe(
    probe: dict[str, object],
    reply_text: str,
    memory_text: str,
) -> dict[str, object]:
    """Detect irrelevant old-memory injection and its visible expression."""

    forbidden = [str(value) for value in probe.get("forbid_any", []) if str(value)]
    injected = [value for value in forbidden if value in memory_text]
    surfaced = [value for value in forbidden if value in reply_text]
    return {
        "key": probe.get("key"),
        "wrong_memory_injected": bool(injected),
        "unsupported_reply": bool(surfaced),
        "injected_terms": injected,
        "surfaced_terms": surfaced,
        "pass": not injected and not surfaced,
    }


def _recall_trace_text(database: Path, *, query_text: str) -> tuple[str, dict[str, object] | None]:
    """Read the trusted prefetch actually attached to this turn's winner.

    The provider-facing Context capture predates the adapter's bounded
    prefetch augmentation, so inspecting only that capture misclassified real
    RAG hits as dialogue-tail guesses.  The immutable ModelResult audit is the
    authoritative record of what the winning model candidate actually saw.
    """

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_json FROM world_v2_events ORDER BY ledger_sequence DESC"
        ).fetchall()
    for (raw,) in rows:
        event = json.loads(raw)
        if event.get("event_type") != "ModelResultRecorded":
            continue
        payload_raw = event.get("payload_json")
        if not isinstance(payload_raw, str):
            continue
        payload = json.loads(payload_raw)
        audit_raw = payload.get("audit_json")
        if not isinstance(audit_raw, str):
            continue
        audit = json.loads(audit_raw)
        trace = audit.get("prefetch_trace")
        if not isinstance(trace, dict):
            continue
        query = trace.get("query")
        if not isinstance(query, dict) or query.get("query_text") != query_text:
            continue
        hits = trace.get("hits")
        texts: list[str] = []
        if isinstance(hits, list):
            for hit in hits:
                document = hit.get("document") if isinstance(hit, dict) else None
                text = document.get("text") if isinstance(document, dict) else None
                if isinstance(text, str):
                    texts.append(text)
        summary = {
            "embedding_status": trace.get("embedding_status"),
            "embedding_failure_code": trace.get("embedding_failure_code"),
            "hit_count": len(texts),
            "source_slices": [
                hit["document"].get("source_slice")
                for hit in hits
                if isinstance(hit, dict) and isinstance(hit.get("document"), dict)
            ]
            if isinstance(hits, list)
            else [],
        }
        return "\n".join(texts), summary
    return "", None


def _build_models(*, stub: bool, settings: Settings, fixture: dict[str, object]):
    """Return (flash_model, advisory_model, closer) for the requested mode.

    Stub mode is not a weaker version of the real run; it answers a different
    question. The stub reply model surfaces a planted value if and only if
    that value is present in the model-facing context. Semantic embedding is
    deliberately disabled, so this is the provider-free lexical/structured
    ceiling, not the complete hybrid-RAG ceiling. A real-model run additionally
    measures configured semantic retrieval and the role model's use of it.
    """

    if not stub:
        from companion_daemon.llm import (
            DeepSeekChatModel,
        )

        if not settings.deepseek_api_key:
            raise SystemExit(
                "real-model mode needs DEEPSEEK_API_KEY; pass --stub for the "
                "provider-free lexical/structured ceiling"
            )
        primary = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking_enabled=False,
        )
        inner: object = primary
        recorder = RecordingModel(inner)

        async def close() -> None:
            closer = getattr(inner, "aclose", None)
            if callable(closer):
                await closer()

        return recorder, None, close, recorder

    # Resolve the sibling stub module regardless of how this file was invoked
    # (directly, through runpy, or from another working directory).
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))
    from _memory_eval_stubs import StubBackgroundModel, StubReplyModel

    recorder = RecordingModel(StubReplyModel(fixture))

    async def close() -> None:
        return None

    return recorder, StubBackgroundModel(fixture), close, recorder


async def run(
    *,
    database: Path,
    fixture_path: Path,
    output: Path,
    stub: bool,
    fast: bool,
    gap_threshold_minutes: int,
    gap_ticks: int,
    background_units: int,
    turn_limit: int | None = None,
) -> list[dict[str, object]]:
    document = json.loads(fixture_path.read_text(encoding="utf-8"))
    turns = document["turns"]
    if turn_limit is not None:
        if turn_limit < 1:
            raise ValueError("turn_limit must be positive")
        turns = turns[:turn_limit]
    ambient_settings = Settings()
    production_database = Path(ambient_settings.database_path).expanduser().resolve()
    assert_safe_eval_paths(
        database=database,
        output=output,
        production_database=production_database,
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    parsed_start = datetime.fromisoformat(str(document["started_at_local"]))
    if parsed_start.tzinfo is None or parsed_start.utcoffset() is None:
        raise ValueError("fixture started_at_local must include an explicit UTC offset")
    started_at = parsed_start.astimezone(UTC).replace(microsecond=0)

    settings = Settings(
        database_path=database,
        # Keep the production composition path while isolating the synthetic
        # counterpart identity. Otherwise the production name from .env
        # competes with the fixture's planted identity and makes a truthful
        # reply look like a recall failure.
        PRIMARY_USER_ID="memory-eval-user",
        # An offline provider-free run supplies every model seam itself. A developer
        # machine's optional local endpoint must not leak into the result.
        LOCAL_APPRAISAL_ENABLED=False if stub else ambient_settings.local_appraisal_enabled,
    )
    flash_model, advisory_model, close_models, recorder = _build_models(
        stub=stub, settings=settings, fixture=document
    )

    consumption = ConsumptionCapture()
    world_logger = logging.getLogger("companion_daemon")
    world_logger.addHandler(consumption)
    previous_level = world_logger.level
    world_logger.setLevel(logging.INFO)

    # The sender-rhythm hold is a wall-clock pause that makes replies feel
    # human.  It dominates offline runtime (about six seconds a turn) and
    # changes no world decision, so an eval may skip it.
    #
    # Skipping it needs a clock, not just a no-op sleep: the hold loop compares
    # ``ingress_now()`` against the last message time and re-sleeps until the
    # gap is genuinely quiet, so a sleep that returns immediately becomes a
    # busy-wait that costs exactly as much wall time and burns a core doing it.
    # Advancing a virtual clock by the requested duration satisfies the same
    # condition instantly while preserving the hold's logic and ordering.
    #
    # Latency numbers from a --fast run therefore measure cognition only and
    # are not comparable to the conversation audit's end-to-end figures.
    clock_now = datetime.now(UTC)

    def _virtual_now() -> datetime:
        return clock_now

    async def _virtual_sleep(seconds: float) -> None:
        nonlocal clock_now
        clock_now += timedelta(seconds=max(float(seconds), 0.0))
        await asyncio.sleep(0)

    delivery = EvalDelivery()
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="memory-eval-user",
        bootstrap_at=started_at,
        delivery=delivery,
        model=flash_model,
        advisory_model=advisory_model,
        ingress_now=_virtual_now if fast else None,
        ingress_sleep=_virtual_sleep if fast else None,
        # A deterministic lexical/structured run must remain provider-free even when the
        # caller's shell contains production embedding credentials.
        use_configured_recall_embedding=not stub,
        interactive_turn_budget_policy=(
            InteractiveTurnBudgetPolicy(
                wall_clock=_virtual_now,
            )
            if fast
            else None
        ),
    )

    rows: list[dict[str, object]] = []
    conversation_interval = 5
    next_scheduler_minute = conversation_interval
    try:
        for index, turn in enumerate(turns, 1):
            turn_minute = int(turn["at_minutes"])
            between_turn_messages: list[str] = []
            scheduler_errors: list[str] = []
            scheduler_ticks = 0

            # Within a session, wake every simulated five minutes as the
            # conversation audit does.  Across a multi-day gap that would mean
            # thousands of wakes, so the gap is replayed as a bounded, evenly
            # spaced set of wakes instead.  This keeps logical time advancing
            # through the same public seam while staying runnable; it does
            # trade away some life-chain richness, which this eval does not
            # measure.
            gap = turn_minute - (next_scheduler_minute - conversation_interval)
            if fast:
                # Inbound itself advances durable logical time to observed_at.
                # Idle scheduler/ecology work is unrelated to retrieval and
                # can introduce provider lanes the ceiling deliberately omits.
                next_scheduler_minute = turn_minute + conversation_interval
            elif gap > gap_threshold_minutes and gap_ticks > 0:
                anchor = next_scheduler_minute - conversation_interval
                step = max(1, (turn_minute - anchor) // gap_ticks)
                tick_minute = anchor + step
                while tick_minute < turn_minute:
                    try:
                        await host.scheduler_once(
                            observed_at=started_at + timedelta(minutes=tick_minute),
                            max_action_units=4,
                            max_background_units=8,
                        )
                        scheduler_ticks += 1
                    except Exception as exc:
                        scheduler_errors.append(repr(exc))
                    tick_minute += step
                next_scheduler_minute = turn_minute + conversation_interval
            else:
                while next_scheduler_minute <= turn_minute:
                    scheduler_before = len(delivery.sent)
                    try:
                        await host.scheduler_once(
                            observed_at=started_at + timedelta(minutes=next_scheduler_minute),
                            max_action_units=4,
                            max_background_units=8,
                        )
                        scheduler_ticks += 1
                    except Exception as exc:
                        scheduler_errors.append(repr(exc))
                    between_turn_messages.extend(
                        text for _recipient, text in delivery.sent[scheduler_before:]
                    )
                    next_scheduler_minute += conversation_interval

            before = len(delivery.sent)
            captures_before = len(recorder.captures)
            outputs_before = len(recorder.outputs)
            summaries_before = len(consumption.summaries)
            existing_trace_ids = {sample.trace_id for sample in host.latency_samples()}
            wall_started = time.perf_counter()
            observed_at = started_at + timedelta(minutes=turn_minute)
            if fast:
                clock_now = observed_at
            error = None
            reply_latency_ms = None
            try:
                outcome = await host.inbound_text(
                    message_id=f"memory-eval-{turn['id']}",
                    recipient_id="memory-eval-user",
                    text=str(turn["text"]),
                    observed_at=observed_at,
                )
                reply_latency_ms = round((time.perf_counter() - wall_started) * 1000, 1)
                await host.drain(max_action_units=8, max_background_units=0)
                status = outcome.status
            except Exception as exc:
                error = repr(exc)
                status = "error"

            # Facts commit on the background lane.  A memory eval that skipped
            # this would measure a world where nothing is ever learned, so the
            # background lane is drained after every turn rather than only on
            # selected probe turns. One unit is the scientific default because
            # Fact acceptance is first in the durable semantic backlog and
            # later turns naturally continue the queue. Larger drains measure
            # unrelated appraisal/ecology throughput, not recall quality.
            background_started = time.perf_counter()
            background_statuses: list[str] = []
            try:
                drained = await host.drain(
                    max_action_units=0,
                    max_background_units=background_units,
                )
                background_statuses = list(drained.background_statuses)
            except Exception as exc:
                scheduler_errors.append(repr(exc))
            background_ms = round((time.perf_counter() - background_started) * 1000, 1)

            emitted = [text for _recipient, text in delivery.sent[before:]]
            replies = [text for text in emitted if not _CONTROL_MARKER_RE.match(text)]
            markers = [text for text in emitted if _CONTROL_MARKER_RE.match(text)]
            reply_text = "\n".join(replies)
            new_captures = recorder.captures[captures_before:]
            model_facing = new_captures[0] if new_captures else ""
            recall_text, recall_summary = _recall_trace_text(
                database,
                query_text=str(turn["text"]),
            )
            retrieval_text = "\n".join(
                value
                for value in (_memory_slice_text(model_facing), recall_text)
                if value
            )
            new_summaries = consumption.summaries[summaries_before:]
            capsule_side = new_summaries[0] if new_summaries else None
            new_latency_samples = tuple(
                sample
                for sample in host.latency_samples()
                if sample.trace_id not in existing_trace_ids
            )

            row: dict[str, object] = {
                "turn": index,
                "turn_id": turn["id"],
                "user": turn["text"],
                "tags": turn.get("tags", []),
                "replies": replies,
                "control_markers": markers,
                "status": status,
                "silent": not replies,
                "reply_latency_ms": reply_latency_ms,
                "background_ms": background_ms,
                "background_statuses": background_statuses,
                "scheduler_ticks": scheduler_ticks,
                "scheduler_errors": scheduler_errors,
                "between_turn_messages": between_turn_messages,
                "model_facing": _slice_stats(model_facing),
                "recall_prefetch": recall_summary,
                "capsule_side_slices": (
                    capsule_side.get("slices") if isinstance(capsule_side, dict) else None
                ),
                "latency_segments_ms": {
                    sample.segment: round(sample.duration_ms, 1)
                    for sample in new_latency_samples
                },
                "error": error,
            }
            if os.environ.get("MEMORY_EVAL_CAPTURE_RAW") == "1":
                # The fixture is synthetic. Raw output capture is deliberately
                # opt-in because ordinary production-shaped evaluations
                # should retain hashes/audits rather than model prose.
                row["model_raw_outputs"] = recorder.outputs[outputs_before:]
            if isinstance(turn.get("plant"), dict):
                row["plant"] = turn["plant"]
            if isinstance(turn.get("probe"), dict):
                row["probe_result"] = _score_probe(
                    turn["probe"], reply_text, model_facing, retrieval_text
                )
            if isinstance(turn.get("negative_probe"), dict):
                row["negative_probe_result"] = _score_negative_probe(
                    turn["negative_probe"],
                    reply_text,
                    retrieval_text,
                )
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        try:
            await host.aclose()
        finally:
            await close_models()
            world_logger.removeHandler(consumption)
            world_logger.setLevel(previous_level)

        with sqlite3.connect(database) as connection:
            event_rows = connection.execute(
                "SELECT event_json FROM world_v2_events ORDER BY ledger_sequence"
            ).fetchall()
        parsed_events = tuple(json.loads(raw) for (raw,) in event_rows)
        event_types = Counter(str(event["event_type"]) for event in parsed_events)
        fact_committed = event_types.get("FactCommittedV2", 0)
        memory_candidate_opened = event_types.get("MemoryCandidateOpened", 0)
        memory_candidate_accepted = event_types.get("MemoryCandidateAccepted", 0)
        planted_facts = sum(
            1
            for turn in turns
            if isinstance(turn, dict) and isinstance(turn.get("plant"), dict)
        )
        if stub and (
            fact_committed != planted_facts
            or memory_candidate_opened != fact_committed
            or memory_candidate_accepted != fact_committed
        ):
            raise AssertionError(
                "stub recall eval did not close every planted Fact through "
                "MemoryCandidate acceptance: "
                f"plants={planted_facts} facts={fact_committed} "
                f"opened={memory_candidate_opened} accepted={memory_candidate_accepted}"
            )
        rows.append(
            {
                "ledger_evidence": True,
                "event_count": sum(event_types.values()),
                "fact_committed": fact_committed,
                "memory_candidate_opened": memory_candidate_opened,
                "memory_candidate_accepted": memory_candidate_accepted,
                "event_type_counts": dict(sorted(event_types.items())),
            }
        )
        rows.append(summarize(rows, stub=stub, fast=fast))
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
    return rows


def summarize(rows: list[dict[str, object]], *, stub: bool, fast: bool) -> dict[str, object]:
    """Reduce per-turn evidence to the handful of numbers worth tracking."""

    turn_rows = [row for row in rows if isinstance(row.get("turn_id"), str)]
    probes = [row for row in turn_rows if isinstance(row.get("probe_result"), dict)]
    negative_probes = [
        row
        for row in turn_rows
        if isinstance(row.get("negative_probe_result"), dict)
    ]
    verdicts = Counter(str(row["probe_result"]["verdict"]) for row in probes)  # type: ignore[index]
    latencies = [
        float(row["reply_latency_ms"])
        for row in turn_rows
        if isinstance(row.get("reply_latency_ms"), (int, float))
    ]
    fact_items = [
        int(row["model_facing"]["slices"]["relevant_facts"]["items"])  # type: ignore[index]
        for row in turn_rows
        if isinstance(row.get("model_facing"), dict)
        and isinstance(row["model_facing"].get("slices"), dict)  # type: ignore[union-attr]
        and row["model_facing"]["slices"]  # type: ignore[index]
    ]
    capsule_fact_items = [
        int(row["capsule_side_slices"]["relevant_facts"]["item_count"])  # type: ignore[index]
        for row in turn_rows
        if isinstance(row.get("capsule_side_slices"), dict)
        and isinstance(row["capsule_side_slices"].get("relevant_facts"), dict)  # type: ignore[union-attr]
    ]
    by_style: dict[str, dict[str, int]] = {}
    for row in probes:
        result = row["probe_result"]  # type: ignore[index]
        style = str(result.get("style", "unknown"))  # type: ignore[union-attr]
        bucket = by_style.setdefault(style, {"probes": 0, "hits": 0, "in_facts": 0})
        bucket["probes"] += 1
        bucket["hits"] += int(result.get("verdict") == "recalled")  # type: ignore[union-attr]
        bucket["in_facts"] += int(bool(result.get("in_facts")))  # type: ignore[union-attr]

    def _rate(numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator, 3) if denominator else None

    hits = sum(
        1
        for row in probes
        if row["probe_result"]["verdict"] == "recalled"  # type: ignore[index]
    )
    reply_hits = sum(1 for row in probes if row["probe_result"]["hit"])  # type: ignore[index]
    supplied = sum(1 for row in probes if row["probe_result"]["in_facts"])  # type: ignore[index]
    return {
        "summary": True,
        "mode": "stub_lexical_structured_ceiling" if stub else "real_model",
        "cadence_hold_skipped": fast,
        "turns": len(turn_rows),
        "probes": len(probes),
        "recall_rate": _rate(hits, len(probes)),
        "reply_hit_rate": _rate(reply_hits, len(probes)),
        "retrieval_rate": _rate(supplied, len(probes)),
        "negative_controls": len(negative_probes),
        "wrong_memory_injection_rate": _rate(
            sum(
                1
                for row in negative_probes
                if row["negative_probe_result"]["wrong_memory_injected"]  # type: ignore[index]
            ),
            len(negative_probes),
        ),
        "unsupported_reply_rate": _rate(
            sum(
                1
                for row in negative_probes
                if row["negative_probe_result"]["unsupported_reply"]  # type: ignore[index]
            ),
            len(negative_probes),
        ),
        "verdicts": dict(verdicts),
        "by_style": by_style,
        "silence_rate": _rate(sum(1 for row in turn_rows if row.get("silent")), len(turn_rows)),
        "errors": sum(1 for row in turn_rows if row.get("error")),
        "relevant_facts_model_facing_mean": (
            round(statistics.mean(fact_items), 2) if fact_items else None
        ),
        "relevant_facts_capsule_side_mean": (
            round(statistics.mean(capsule_fact_items), 2) if capsule_fact_items else None
        ),
        "reply_latency_ms": {
            "p50": round(statistics.median(latencies), 1) if latencies else None,
            "p95": (
                round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1)
                if latencies
                else None
            ),
            "max": round(max(latencies), 1) if latencies else None,
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/world_v2/fixtures/memory_recall_30_turns.json"),
    )
    parser.add_argument(
        "--stub",
        action="store_true",
        help=(
            "Run deterministic models to measure the provider-free "
            "lexical/structured ceiling."
        ),
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Skip the wall-clock sender-rhythm hold. Logical time and all world "
            "decisions are unchanged, but reply latency then measures cognition only."
        ),
    )
    parser.add_argument("--gap-threshold-minutes", type=int, default=120)
    parser.add_argument(
        "--gap-ticks",
        type=int,
        default=8,
        help="Scheduler wakes used to replay a multi-day gap. 0 jumps the clock directly.",
    )
    parser.add_argument(
        "--background-units",
        type=int,
        default=1,
        help="Maximum durable background work units drained after each turn.",
    )
    parser.add_argument(
        "--turn-limit",
        type=int,
        help="Run only the first N fixture turns for bounded diagnostics.",
    )
    args = parser.parse_args()
    result_rows = asyncio.run(
        run(
            database=args.database,
            fixture_path=args.fixture,
            output=args.output,
            stub=args.stub,
            fast=args.fast,
            gap_threshold_minutes=args.gap_threshold_minutes,
            gap_ticks=args.gap_ticks,
            background_units=args.background_units,
            turn_limit=args.turn_limit,
        )
    )
    print(
        json.dumps(
            next(row for row in reversed(result_rows) if row.get("summary")),
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
