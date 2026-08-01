"""Run the targeted private-self expression audit through an isolated real host.

This is a manual, descriptive audit.  It never uses the production database,
never sends to QQ, and never turns question counts or any other surface
statistic into an acceptance rule.  Version 2 keeps every Observation-bound
model attempt for diagnosis while assigning a content-free attempt lane, so a
background appraisal can neither hide nor manufacture a foreground expression
failure. Historical or future attempt identities that cannot be proved from
the current expression lifecycle remain visibly ``unknown``; the audit never
guesses that they are background.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from typing import Any

from companion_daemon.config import Settings
from companion_daemon.world_v2.interactive_turn_budget import InteractiveTurnBudgetPolicy
from companion_daemon.world_v2.isolated_source_closure_trace import (
    BoundedSourceClosureTraceCollector,
    SourceClosureTraceRecord,
    capture_isolated_source_closure_trace,
)
from companion_daemon.world_v2.private_self_expression_audit import (
    PreconversationLifeEcologyAudit,
    PrivateSelfExpressionAuditEvaluator,
    PrivateSelfExpressionScenario,
    PrivateSelfExpressionScenarioTurn,
    assess_naturalness_readiness,
    load_private_self_expression_scenario,
)
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host
from companion_daemon.world_v2.qq_ingress_policy import QQIngressFragment


_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_FIXTURE = _ROOT / "tests/world_v2/fixtures/private_self_expression_targeted.json"
_PRECONVERSATION_LIFE_ECOLOGY_UNIT = timedelta(minutes=10)
_FINAL_RECEIPT_GRACE = timedelta(seconds=121)
_LIFE_ECOLOGY_OUTCOME_PREFIX = "life-ecology:"
_LIFE_ECOLOGY_STATUS_KEYS = (
    "accepted",
    "cooldown",
    "no_op",
    "not_observed",
    "technical_failure",
    "unknown",
)
_LIFE_ECOLOGY_NO_OP_OUTCOMES = frozenset(
    {
        "idle",
        "author_idle",
        "author_no_opening",
        "life_development_no_op",
    }
)
_LIFE_ECOLOGY_ACCEPTED_OUTCOMES = frozenset(
    {
        "activity_transitioned",
        "aftermath_occurrence_opened",
        "aftermath_recovered_experience",
        "aftermath_recovered_memory",
        "aftermath_settled",
        "author_planned",
        "biographical_transitioned",
        "future_author_planned",
        "life_development_occurrence_committed",
        "life_development_plan_committed",
        "life_development_recovered",
        "npc_initiative_committed",
        "open_world_committed",
    }
)


class IsolatedAuditDelivery:
    """Capture and positively verify sends without touching a QQ provider."""

    def __init__(self, *, run_namespace: str | None = None) -> None:
        self.sent: list[dict[str, str]] = []
        self._messages: dict[str, str] = {}
        self._observed_ns: list[int] = []
        # Provider references are immutable ledger identities. A retained or
        # cloned audit database can outlive this in-memory adapter, so an
        # ordinal alone would let a later audit reuse an old provider ref with
        # different content. Keep ordinals readable while namespacing every
        # isolated provider lifetime.
        self._run_namespace = run_namespace or secrets.token_hex(12)

    def _record(self, recipient_id: str, modality: str, content: str) -> dict[str, object]:
        message_id = f"private-self-real-audit-{self._run_namespace}-{len(self.sent) + 1}"
        self._observed_ns.append(time.perf_counter_ns())
        self.sent.append(
            {
                "recipient_id": recipient_id,
                "message_id": message_id,
                "modality": modality,
                "content": content,
            }
        )
        self._messages[message_id] = content
        return {"status": "ok", "data": {"message_id": message_id}}

    def first_visible_observed_ns(self, *, start_index: int) -> int | None:
        """Return the first captured content receipt on the process clock.

        Typing is visible UI state but not a reply.  The audit reports it in
        ``deliveries`` while reserving first-reply latency for text/reaction/
        sticker content accepted by the isolated provider.
        """

        for index in range(max(0, start_index), len(self.sent)):
            if self.sent[index]["modality"] in {"text", "reaction", "sticker"}:
                return self._observed_ns[index]
        return None

    def observed_ns_for_message(self, message_id: str) -> int | None:
        """Return the capture instant for one immutable provider message id."""

        for index, sent in enumerate(self.sent):
            if sent["message_id"] == message_id:
                return self._observed_ns[index]
        return None

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        return self._record(recipient_id, "text", text)

    async def send_reaction(
        self,
        recipient_id: str,
        *,
        message_id: str,
        reaction_id: str,
    ) -> dict[str, object]:
        return self._record(recipient_id, "reaction", f"{message_id}:{reaction_id}")

    async def send_sticker(
        self,
        recipient_id: str,
        *,
        sticker_id: str,
    ) -> dict[str, object]:
        return self._record(recipient_id, "sticker", sticker_id)

    async def send_typing(
        self,
        recipient_id: str,
        *,
        state: str,
    ) -> dict[str, object]:
        return self._record(recipient_id, "typing", state)

    async def get_message(
        self,
        recipient_id: str,
        *,
        message_id: str,
    ) -> dict[str, object]:
        del recipient_id
        content = self._messages.get(message_id)
        if content is None:
            return {"status": "failed", "retcode": 1404, "message": "not found"}
        return {
            "status": "ok",
            "retcode": 0,
            "data": {"message_id": message_id, "message": content},
        }


class _VirtualPacingClock:
    """Skip humanized wall waits while preserving their ordering semantics."""

    def __init__(self, current: datetime) -> None:
        self.current = current

    def now(self) -> datetime:
        return self.current

    def advance_to(self, instant: datetime) -> None:
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("virtual pacing instant must be timezone-aware")
        normalized = instant.astimezone(UTC)
        if normalized > self.current:
            self.current = normalized

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=max(float(seconds), 0.0))
        await asyncio.sleep(0)


def _aware_utc_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("start-at must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("start-at must include a timezone offset")
    return parsed.astimezone(UTC)


def _latency_samples(host: object) -> tuple[object, ...]:
    read = getattr(host, "latency_samples", None)
    if not callable(read):
        return ()
    return tuple(read())


def _latency_sample_identity(sample: object) -> tuple[str, str]:
    return (str(getattr(sample, "trace_id")), str(getattr(sample, "segment")))


def _serialize_latency_sample(
    sample: object,
    *,
    fast_pacing: bool,
) -> dict[str, object]:
    segment = str(getattr(sample, "segment"))
    if fast_pacing and segment in {"coalescing", "queue", "ingress_to_visible"}:
        clock_semantics = (
            "monotonic_plus_persisted_virtual_ingress_duration"
            if segment == "ingress_to_visible"
            else "persisted_virtual_ingress_duration"
        )
    elif segment in {"coalescing", "queue"}:
        clock_semantics = "persisted_wall_ingress_duration"
    elif segment == "ingress_to_visible":
        clock_semantics = "monotonic_plus_persisted_wall_ingress_duration"
    else:
        clock_semantics = "process_monotonic_observed_span"
    return {
        "trace_id": str(getattr(sample, "trace_id")),
        "startup": str(getattr(sample, "startup")),
        "environment": str(getattr(sample, "environment")),
        "segment": segment,
        "duration_ms": round(float(getattr(sample, "duration_ms")), 3),
        "clock_semantics": clock_semantics,
    }


def _attribute_runtime_turn_evidence(
    *,
    runtime_turns: list[dict[str, Any]],
    immutable_replay_audit: dict[str, Any],
    delivery: IsolatedAuditDelivery,
    latency_samples: tuple[object, ...],
    started_ns_by_turn: dict[str, int],
    fast_pacing: bool,
) -> None:
    """Join runtime observations by durable source, Action, receipt, and batch.

    Overlapped inbound tasks can complete in either order.  Global delivery
    offsets and before/after latency snapshots therefore cannot identify the
    turn that caused an effect.  Cold replay already proves the chain from one
    source Observation through its authorized Actions to provider receipts;
    the QQ trace carries the same coalesced batch identity as that Observation.
    """

    raw_audit_turns = immutable_replay_audit.get("turns")
    if not isinstance(raw_audit_turns, list):
        return
    audit_by_source = {
        str(item["source_event_id"]): item
        for item in raw_audit_turns
        if isinstance(item, dict) and isinstance(item.get("source_event_id"), str)
    }
    captured_by_id = {
        str(item["message_id"]): item
        for item in delivery.sent
        if isinstance(item.get("message_id"), str)
    }
    provider_prefix = "platform:message_id:"
    observation_batch_marker = ":qq-coalesced:"
    trace_batch_marker = ":qq-ingress-batch:"

    for row in runtime_turns:
        audit_turn = audit_by_source.get(str(row.get("source_event_id")))
        if audit_turn is None:
            # Lightweight runner fixtures may intentionally omit replay
            # identities.  Production audit reports always contain them.
            continue
        observation_id = audit_turn.get("observation_id")
        if not isinstance(observation_id, str) or observation_batch_marker not in observation_id:
            continue
        batch_identity = observation_id.split(observation_batch_marker, 1)[1]

        action_ids = {
            str(item["action_id"])
            for item in audit_turn.get("actions", ())
            if isinstance(item, dict) and isinstance(item.get("action_id"), str)
        }
        attributed_message_ids: set[str] = set()
        for receipt in audit_turn.get("receipts", ()):
            if (
                not isinstance(receipt, dict)
                or receipt.get("action_id") not in action_ids
                or receipt.get("observed_state")
                not in {"provider_accepted", "delivered"}
            ):
                continue
            provider_ref = receipt.get("provider_ref")
            if not isinstance(provider_ref, str):
                continue
            for message_id in captured_by_id:
                if provider_ref == provider_prefix + message_id:
                    attributed_message_ids.add(message_id)

        attributed_deliveries = [
            item
            for item in delivery.sent
            if item.get("message_id") in attributed_message_ids
        ]
        row["deliveries"] = attributed_deliveries
        visible_instants = tuple(
            observed_ns
            for item in attributed_deliveries
            if item.get("modality") in {"text", "reaction", "sticker"}
            and (
                observed_ns := delivery.observed_ns_for_message(
                    str(item["message_id"])
                )
            )
            is not None
        )
        started_ns = started_ns_by_turn.get(str(row.get("turn_id")))
        if visible_instants and started_ns is not None:
            row["first_visible_reply_wall_ms"] = round(
                max(0, min(visible_instants) - started_ns) / 1_000_000,
                1,
            )
            row["first_visible_reply_measurement"] = (
                "process_monotonic_to_isolated_provider_acceptance"
            )
        else:
            row["first_visible_reply_wall_ms"] = None
            row["first_visible_reply_measurement"] = "not_observed"

        attributed_samples = tuple(
            sample
            for sample in latency_samples
            if (
                trace_id := str(getattr(sample, "trace_id"))
            ).split(trace_batch_marker, 1)[-1]
            == batch_identity
            and trace_batch_marker in trace_id
        )
        row["latency_segments"] = [
            _serialize_latency_sample(sample, fast_pacing=fast_pacing)
            for sample in sorted(
                attributed_samples,
                key=_latency_sample_identity,
            )
        ]


def _safe_explicit_database(path: Path, *, production_database: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == production_database.expanduser().resolve():
        raise ValueError("the audit database must not be the production database")
    if resolved.exists():
        raise ValueError("the explicit audit database must not already exist")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _reserve_private_database(path: Path) -> Path:
    """Reserve one isolated SQLite target as 0600 without an overwrite race."""

    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        resolved,
        os.O_RDWR | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return resolved


def _safe_output(
    path: Path,
    *,
    production_database: Path,
    audit_database: Path | None,
) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == production_database.expanduser().resolve():
        raise ValueError("the audit output must not overwrite the production database")
    if audit_database is not None and resolved == audit_database:
        raise ValueError("the audit output and retained audit database must be different files")
    if resolved.exists():
        raise ValueError("the audit output must not already exist")
    return resolved


def _safe_rejection_trace_output(
    path: Path,
    *,
    production_database: Path,
    audit_database: Path | None,
    report_output: Path,
) -> Path:
    """Validate the separately protected, explicitly enabled trace target."""

    resolved = path.expanduser().resolve()
    forbidden = {
        production_database.expanduser().resolve(),
        report_output.expanduser().resolve(),
    }
    if audit_database is not None:
        forbidden.add(audit_database.expanduser().resolve())
    if resolved in forbidden:
        raise ValueError(
            "the rejection trace, production database, audit database, and report "
            "must be different files"
        )
    if resolved.exists():
        raise ValueError("the rejection trace output must not already exist")
    return resolved


def _write_private_json_exclusive(path: Path, document: dict[str, Any]) -> str:
    """Create one 0600 diagnostic artifact without an overwrite race."""

    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    return sha256(content).hexdigest()


def _trace_document(
    *,
    events: tuple[SourceClosureTraceRecord, ...],
    turn_ranges: tuple[tuple[str, int, int], ...],
    dropped_count: int,
) -> dict[str, Any]:
    covered: set[int] = set()
    turns: list[dict[str, Any]] = []
    for turn_id, start, end in turn_ranges:
        indexes = range(max(start, 0), min(end, len(events)))
        covered.update(indexes)
        turns.append(
            {
                "turn_id": turn_id,
                "events": [events[index].as_dict() for index in indexes],
            }
        )
    return {
        "contract": "isolated-source-closure-trace.3",
        "authority": "process_local_non_authoritative",
        "turns": turns,
        "unattributed_events": [
            event.as_dict() for index, event in enumerate(events) if index not in covered
        ],
        "dropped_count": dropped_count,
    }


def _audit_event_payload(item: Any) -> dict[str, Any]:
    """Decode one replay-evidence event without trusting its payload shape."""

    raw = getattr(getattr(item, "event", None), "payload_json", None)
    if not isinstance(raw, str):
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _life_model_role(attempt_id: object) -> str | None:
    """Read only the stable Life attempt identity; never infer a role from prose."""

    prefix = "attempt:life-development:"
    if not isinstance(attempt_id, str) or not attempt_id.startswith(prefix):
        return None
    role, separator, _ = attempt_id.removeprefix(prefix).partition(":")
    return role if separator and role else None


def _unit_life_model_observation(
    *,
    evidence_after: Any,
    prior_sequence: int,
    after_sequence: int,
) -> dict[str, Any]:
    """Describe which Life model authorities actually ran in this clock unit."""

    attempt_counts: Counter[str] = Counter()
    world_author_decisions: list[tuple[int, str]] = []
    for item in tuple(getattr(evidence_after, "events", ())):
        sequence = int(getattr(getattr(item, "cursor", None), "ledger_sequence", -1))
        if not prior_sequence < sequence <= after_sequence:
            continue
        event_type = getattr(getattr(item, "event", None), "event_type", None)
        payload = _audit_event_payload(item)
        if event_type == "ModelResultRecorded":
            role = _life_model_role(payload.get("attempt_id"))
            if role is not None:
                attempt_counts[role] += 1
        elif (
            event_type == "ProposalRecorded"
            and payload.get("proposal_kind") == "life_development"
            and payload.get("model_role") == "world_author"
            and payload.get("world_author_decision") in {"no_op", "propose"}
        ):
            world_author_decisions.append((sequence, str(payload["world_author_decision"])))
    return {
        "life_model_attempt_counts_by_role": dict(sorted(attempt_counts.items())),
        "world_author_decision": (
            max(world_author_decisions, key=lambda candidate: candidate[0])[1]
            if world_author_decisions
            else None
        ),
        "cadence_draw_event_ref": None,
        "cadence_delay_seconds": None,
    }


def _classify_life_ecology_outcome(
    outcome_ref: str | None,
) -> tuple[str, str]:
    """Collapse a durable runtime outcome into a descriptive audit category."""

    if not isinstance(outcome_ref, str) or not outcome_ref:
        return ("not_observed", "life_ecology_completion_not_observed")
    if not outcome_ref.startswith(_LIFE_ECOLOGY_OUTCOME_PREFIX):
        return ("unknown", outcome_ref)

    outcome = outcome_ref.removeprefix(_LIFE_ECOLOGY_OUTCOME_PREFIX)
    technical_prefix = "technical_failure."
    if outcome.startswith(technical_prefix):
        reason_code = outcome.removeprefix(technical_prefix)
        return ("technical_failure", reason_code or "unknown")
    if outcome == "failed_safe":
        return ("technical_failure", "failed_safe")
    if outcome == "cooldown":
        return ("cooldown", outcome)
    if outcome in _LIFE_ECOLOGY_NO_OP_OUTCOMES:
        return ("no_op", outcome)
    if outcome in _LIFE_ECOLOGY_ACCEPTED_OUTCOMES:
        return ("accepted", outcome)
    return ("unknown", outcome or "empty_outcome")


def _unit_life_ecology_observation(
    *,
    evidence_before: Any,
    evidence_after: Any,
) -> dict[str, str | None]:
    """Read the Life terminal written by this unit, without doing more work."""

    prior_sequence = int(evidence_before.cursor.ledger_sequence)
    after_sequence = int(evidence_after.cursor.ledger_sequence)
    all_events = tuple(getattr(evidence_after, "events", ()))
    model_observation = _unit_life_model_observation(
        evidence_after=evidence_after,
        prior_sequence=prior_sequence,
        after_sequence=after_sequence,
    )
    life_trigger_ids: set[str] = set()
    for item in all_events:
        if getattr(getattr(item, "event", None), "event_type", None) != ("TriggerProcessOpened"):
            continue
        process = _audit_event_payload(item).get("process")
        if not isinstance(process, dict) or process.get("process_kind") != "life_ecology":
            continue
        trigger_id = process.get("trigger_id")
        if isinstance(trigger_id, str) and trigger_id:
            life_trigger_ids.add(trigger_id)

    completions: list[tuple[int, Any, dict[str, Any]]] = []
    for item in all_events:
        sequence = int(getattr(getattr(item, "cursor", None), "ledger_sequence", -1))
        if not prior_sequence < sequence <= after_sequence:
            continue
        if getattr(getattr(item, "event", None), "event_type", None) != ("TriggerProcessCompleted"):
            continue
        payload = _audit_event_payload(item)
        trigger_id = payload.get("trigger_id")
        if isinstance(trigger_id, str) and trigger_id in life_trigger_ids:
            completions.append((sequence, item, payload))

    if completions:
        _, item, payload = max(completions, key=lambda candidate: candidate[0])
        outcome_ref = payload.get("runtime_outcome_ref")
        normalized_outcome_ref = outcome_ref if isinstance(outcome_ref, str) else None
        status, reason_code = _classify_life_ecology_outcome(normalized_outcome_ref)
        event_id = getattr(getattr(item, "event", None), "event_id", None)
        trigger_id = payload.get("trigger_id")
        cadence_draw_event_ref = payload.get("cadence_draw_event_ref")
        cadence_delay_seconds = payload.get("cadence_delay_seconds")
        if not (
            isinstance(cadence_draw_event_ref, str)
            and cadence_draw_event_ref
            and isinstance(cadence_delay_seconds, int)
            and not isinstance(cadence_delay_seconds, bool)
            and cadence_delay_seconds >= 0
        ):
            cadence_draw_event_ref = None
            cadence_delay_seconds = None
        return {
            "ecology_status": status,
            "ecology_reason_code": reason_code,
            "ecology_runtime_outcome_ref": normalized_outcome_ref,
            "ecology_trigger_id": trigger_id if isinstance(trigger_id, str) else None,
            "ecology_completion_event_ref": (event_id if isinstance(event_id, str) else None),
            **model_observation,
            "cadence_draw_event_ref": cadence_draw_event_ref,
            "cadence_delay_seconds": cadence_delay_seconds,
        }

    before_schedule = getattr(evidence_before.projection, "life_ecology_schedule", None)
    after_schedule = getattr(evidence_after.projection, "life_ecology_schedule", None)
    if after_schedule is not None and after_schedule != before_schedule:
        trigger_id = getattr(after_schedule, "last_trigger_id", None)
        outcome_ref = getattr(after_schedule, "last_outcome_ref", None)
        normalized_outcome_ref = outcome_ref if isinstance(outcome_ref, str) else None
        status, reason_code = _classify_life_ecology_outcome(normalized_outcome_ref)
        return {
            "ecology_status": status,
            "ecology_reason_code": reason_code,
            "ecology_runtime_outcome_ref": normalized_outcome_ref,
            "ecology_trigger_id": trigger_id if isinstance(trigger_id, str) else None,
            "ecology_completion_event_ref": None,
            **model_observation,
        }

    status, reason_code = _classify_life_ecology_outcome(None)
    return {
        "ecology_status": status,
        "ecology_reason_code": reason_code,
        "ecology_runtime_outcome_ref": None,
        "ecology_trigger_id": None,
        "ecology_completion_event_ref": None,
        **model_observation,
    }


async def _run_preconversation_life_ecology(
    *,
    host: Any,
    conversation_started_at: datetime,
    units: int,
) -> dict[str, Any]:
    """Give a fresh isolated World real, model-owned life opportunities.

    Each unit is one production-shaped ten-minute Clock wake with Life
    Ecology enabled.  The runner supplies no activity, motive, dialogue, or
    expected outcome.  It deliberately does not drain Actions or generic
    social cognition: this preparation can create only the same world-life
    opportunities that a production heartbeat would create.
    """

    if conversation_started_at.tzinfo is None or conversation_started_at.utcoffset() is None:
        raise ValueError("preconversation audit time must be timezone-aware")
    if units < 0:
        raise ValueError("preconversation Life Ecology units must be non-negative")

    duration = _PRECONVERSATION_LIFE_ECOLOGY_UNIT * units
    world_started_at = conversation_started_at - duration
    evidence_before = host.export_replay_evidence()
    evidence_after = evidence_before
    initial_sequence = int(evidence_before.cursor.ledger_sequence)
    tick_statuses: list[str] = []
    unit_observations: list[dict[str, Any]] = []
    logical_from = world_started_at
    for ordinal in range(1, units + 1):
        logical_to = world_started_at + (_PRECONVERSATION_LIFE_ECOLOGY_UNIT * ordinal)
        unit_evidence_before = evidence_after
        status = await host.tick(
            tick_id=(
                f"tick:private-self-expression-audit-prelife:{ordinal}:{logical_to.isoformat()}"
            ),
            logical_time_from=logical_from,
            logical_time_to=logical_to,
            observed_at=logical_to,
            reason="private_self_expression_audit_preconversation_life_ecology",
            run_life_ecology=True,
        )
        clock_status = str(status)
        tick_statuses.append(clock_status)
        evidence_after = host.export_replay_evidence()
        ecology = _unit_life_ecology_observation(
            evidence_before=unit_evidence_before,
            evidence_after=evidence_after,
        )
        unit_observations.append(
            {
                "ordinal": ordinal,
                "logical_time_from": logical_from,
                "logical_time_to": logical_to,
                "clock_status": clock_status,
                **ecology,
                "ledger_sequence_before": int(unit_evidence_before.cursor.ledger_sequence),
                "ledger_sequence_after": int(evidence_after.cursor.ledger_sequence),
            }
        )
        logical_from = logical_to

    event_counts = Counter(
        item.event.event_type
        for item in evidence_after.events
        if item.cursor.ledger_sequence > initial_sequence
    )
    ecology_status_counts = {key: 0 for key in _LIFE_ECOLOGY_STATUS_KEYS}
    ecology_reason_code_counts: Counter[str] = Counter()
    life_model_attempt_counts_by_role: Counter[str] = Counter()
    world_author_decision_counts: Counter[str] = Counter()
    for item in unit_observations:
        ecology_status_counts[str(item["ecology_status"])] += 1
        ecology_reason_code_counts[str(item["ecology_reason_code"])] += 1
        for role, count in dict(item["life_model_attempt_counts_by_role"]).items():
            life_model_attempt_counts_by_role[str(role)] += int(count)
        decision = item["world_author_decision"]
        if isinstance(decision, str):
            world_author_decision_counts[decision] += 1
    before_projection = evidence_before.projection
    after_projection = evidence_after.projection
    final_schedule = getattr(after_projection, "life_ecology_schedule", None)
    next_consideration_at = getattr(final_schedule, "next_consideration_at", None)
    if not isinstance(next_consideration_at, datetime):
        next_consideration_at = None
    report = PreconversationLifeEcologyAudit.model_validate(
        {
            "contract": "private-self-expression-preconversation-life-ecology.2",
            "requested_units": units,
            "unit_seconds": int(_PRECONVERSATION_LIFE_ECOLOGY_UNIT.total_seconds()),
            "world_started_at": world_started_at,
            "conversation_started_at": conversation_started_at,
            "tick_statuses": tuple(tick_statuses),
            "tick_statuses_deprecated": True,
            "tick_statuses_semantics": "legacy_clock_status_only",
            "units": tuple(unit_observations),
            "ecology_status_counts": ecology_status_counts,
            "ecology_reason_code_counts": dict(sorted(ecology_reason_code_counts.items())),
            "recorded_cadence_cooldown_ordinals": tuple(
                int(item["ordinal"])
                for item in unit_observations
                if item["ecology_status"] == "cooldown"
            ),
            "next_recorded_consideration_at": next_consideration_at,
            "life_model_attempt_counts_by_role": dict(
                sorted(life_model_attempt_counts_by_role.items())
            ),
            "world_author_consideration_ordinals": tuple(
                int(item["ordinal"])
                for item in unit_observations
                if dict(item["life_model_attempt_counts_by_role"]).get("world_author", 0) > 0
            ),
            "world_author_decision_counts": dict(sorted(world_author_decision_counts.items())),
            "ledger_sequence_before": initial_sequence,
            "ledger_sequence_after": evidence_after.cursor.ledger_sequence,
            "new_event_type_counts": dict(sorted(event_counts.items())),
            "experience_count_before": len(before_projection.experiences),
            "experience_count_after": len(after_projection.experiences),
            "plan_count_before": len(before_projection.plans),
            "plan_count_after": len(after_projection.plans),
            "memory_candidate_count_before": len(before_projection.memory_candidates),
            "memory_candidate_count_after": len(after_projection.memory_candidates),
        }
    )
    document = report.model_dump(mode="json")
    # Preserve the runner's established ISO representation while the schema
    # itself retains timezone-aware datetime validation.
    document["world_started_at"] = report.world_started_at.isoformat()
    document["conversation_started_at"] = report.conversation_started_at.isoformat()
    if report.next_recorded_consideration_at is not None:
        document["next_recorded_consideration_at"] = (
            report.next_recorded_consideration_at.isoformat()
        )
    for unit_document, unit in zip(document["units"], report.units, strict=True):
        unit_document["logical_time_from"] = unit.logical_time_from.isoformat()
        unit_document["logical_time_to"] = unit.logical_time_to.isoformat()
    return document


async def _drain_terminal_receipts(
    *,
    host: Any,
    fast_clock: _VirtualPacingClock | None = None,
) -> None:
    """Let the final turn complete the same Action/receipt chain as earlier turns.

    A multi-beat expression can consume more than the ordinary bounded
    per-turn audit drain because every beat has authorization, dispatch,
    provider-accepted settlement, and positive lookup work. Earlier turns get
    another scheduler pass when the next message arrives; the last one does
    not, so the retained audit gives that final pass the host's full bounded
    Action budget before exporting replay evidence.
    """

    if fast_clock is None:
        await host.drain(max_action_units=64, max_background_units=0)
        return

    # The Action pump deliberately waits out its 120-second dispatch lease
    # before converting a provider acknowledgement into terminal positive
    # lookup evidence. Its authority is the persisted World logical time, not
    # this process-local pacing clock. Run the same scheduler seam production
    # uses so the isolated audit records ClockAdvanced, crosses exact Action
    # boundaries, and performs the positive lookup without rewriting history.
    final_scheduler_at = fast_clock.now() + _FINAL_RECEIPT_GRACE
    fast_clock.advance_to(final_scheduler_at)
    await host.scheduler_once(
        observed_at=final_scheduler_at,
        max_action_units=64,
        max_background_units=0,
    )


async def _submit_scenario_turn(
    *,
    host: Any,
    scenario: PrivateSelfExpressionScenario,
    turn: PrivateSelfExpressionScenarioTurn,
    recipient_id: str,
    conversation_started_at: datetime,
    clock: _VirtualPacingClock,
    fast_pacing: bool,
) -> dict[str, Any]:
    """Submit one fixture turn through the same ingress API as QQ.

    A turn with ``fragments`` represents one user volley.  Every bubble is
    submitted concurrently with its own durable source id, leaving the real
    QQ ingress store—not the audit harness—to decide the resulting batch.
    """

    turn_started_at = (
        conversation_started_at
        + timedelta(minutes=turn.at_minutes)
        + timedelta(milliseconds=getattr(turn, "launch_offset_ms", 0))
    )
    if fast_pacing:
        clock.advance_to(turn_started_at)
    fragments = tuple(getattr(turn, "fragments", ()))
    if not fragments:
        outcome = await host.inbound_text(
            message_id=scenario.source_event_id(turn),
            recipient_id=recipient_id,
            text=turn.text,
            observed_at=turn_started_at,
        )
        return {
            "status": outcome.status,
            "statuses": [outcome.status],
            "source_event_ids": [scenario.source_event_id(turn)],
            "user_messages": [turn.text],
        }

    async def submit_fragment(index: int) -> Any:
        fragment = fragments[index]
        if fragment.offset_ms:
            if fast_pacing:
                # Let the zero-offset caller enter the production coalescing
                # wait before advancing the shared audit wall clock.
                await asyncio.sleep(0)
                clock.advance_to(
                    turn_started_at + timedelta(milliseconds=fragment.offset_ms)
                )
            else:
                await asyncio.sleep(fragment.offset_ms / 1000)
        return await host.inbound_fragment(
            QQIngressFragment(
                source_event_id=scenario.source_event_id_for_fragment(turn, index),
                recipient_id=recipient_id,
                observed_at=(
                    turn_started_at + timedelta(milliseconds=fragment.offset_ms)
                ),
                content_shape="text",
                text=fragment.text,
            )
        )

    outcomes = await asyncio.gather(
        *(
            asyncio.create_task(submit_fragment(index))
            for index in range(len(fragments))
        )
    )
    statuses = [outcome.status for outcome in outcomes]
    return {
        "status": statuses[0] if len(set(statuses)) == 1 else "mixed",
        "statuses": statuses,
        "source_event_ids": [
            scenario.source_event_id_for_fragment(turn, index)
            for index in range(len(fragments))
        ],
        "user_messages": [fragment.text for fragment in fragments],
    }


async def _submit_scenario_overlap_group(
    *,
    host: Any,
    scenario: PrivateSelfExpressionScenario,
    turns: tuple[PrivateSelfExpressionScenarioTurn, ...],
    recipient_id: str,
    conversation_started_at: datetime,
    clock: _VirtualPacingClock,
    fast_pacing: bool,
) -> tuple[dict[str, Any], ...]:
    """Launch consecutive fixture turns while an earlier turn may still run."""

    if len(turns) < 2:
        raise ValueError("an overlap group requires at least two turns")
    group = turns[0].overlap_group
    if group is None or any(turn.overlap_group != group for turn in turns):
        raise ValueError("overlapped turns must share one explicit group")
    offsets = tuple(turn.launch_offset_ms for turn in turns)
    if offsets != tuple(sorted(offsets)) or len(offsets) != len(set(offsets)):
        raise ValueError("overlap launch offsets must be ordered and unique")

    async def launch(turn: PrivateSelfExpressionScenarioTurn) -> dict[str, Any]:
        if turn.launch_offset_ms:
            # This is deliberately a process-local launch offset, not logical
            # authority. It lets a real-provider audit place a second inbound
            # inside an earlier provider call without changing either
            # Observation timestamp after submission.
            await asyncio.sleep(turn.launch_offset_ms / 1000)
        started_ns = time.perf_counter_ns()
        execution = await _submit_scenario_turn(
            host=host,
            scenario=scenario,
            turn=turn,
            recipient_id=recipient_id,
            conversation_started_at=conversation_started_at,
            clock=clock,
            fast_pacing=fast_pacing,
        )
        execution["_audit_started_ns"] = started_ns
        execution["_audit_ended_ns"] = time.perf_counter_ns()
        return execution

    return tuple(
        await asyncio.gather(
            *(asyncio.create_task(launch(turn)) for turn in turns)
        )
    )


async def run(
    *,
    fixture: Path,
    output: Path,
    database: Path,
    start_at: datetime | None = None,
    fast_pacing: bool,
    preconversation_life_ecology_units: int,
    first_turn_background_units: int,
    background_units: int,
    max_turns: int | None,
    rejection_trace_output: Path | None = None,
) -> dict[str, Any]:
    """Run the public QQ host and derive the report only from its cold replay."""

    scenario = load_private_self_expression_scenario(fixture)
    if max_turns is not None:
        scenario = scenario.model_copy(update={"turns": scenario.turns[:max_turns]})
    database = _reserve_private_database(database)
    ambient = Settings()
    if not ambient.deepseek_api_key:
        raise RuntimeError(
            "DEEPSEEK_API_KEY is required: this runner intentionally has no fake-model mode"
        )
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID="private-self-expression-audit-user",
    )
    delivery = IsolatedAuditDelivery()
    trace_path: Path | None = None
    if rejection_trace_output is not None:
        configured_database = Path(ambient.database_path).expanduser()
        production_database = (
            configured_database
            if configured_database.is_absolute()
            else _ROOT / configured_database
        ).resolve()
        if database.expanduser().resolve() == production_database:
            raise ValueError(
                "the rejection trace requires an audit database distinct from production"
            )
        if not isinstance(delivery, IsolatedAuditDelivery):
            raise ValueError("the rejection trace requires isolated in-process delivery")
        trace_path = _safe_rejection_trace_output(
            rejection_trace_output,
            production_database=production_database,
            audit_database=database,
            report_output=output,
        )
    if start_at is not None and (start_at.tzinfo is None or start_at.utcoffset() is None):
        raise ValueError("audit conversation start must be timezone-aware")
    conversation_started_at = (
        start_at.astimezone(UTC)
        if start_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    world_started_at = conversation_started_at - (
        _PRECONVERSATION_LIFE_ECOLOGY_UNIT * preconversation_life_ecology_units
    )
    clock = _VirtualPacingClock(conversation_started_at)
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="private-self-expression-audit-recipient",
        bootstrap_at=world_started_at,
        delivery=delivery,
        ingress_now=clock.now if fast_pacing else None,
        ingress_sleep=clock.sleep if fast_pacing else None,
        action_due_now=clock.now if fast_pacing else None,
        interactive_turn_budget_policy=(
            InteractiveTurnBudgetPolicy(wall_clock=clock.now) if fast_pacing else None
        ),
    )
    runtime_turns: list[dict[str, Any]] = []
    started_ns_by_turn: dict[str, int] = {}
    source_review_authority_health: dict[str, object] = {}
    trace_collector = BoundedSourceClosureTraceCollector() if trace_path is not None else None
    turn_trace_ranges: list[tuple[str, int, int]] = []
    trace_scope = (
        capture_isolated_source_closure_trace(trace_collector)
        if trace_collector is not None
        else nullcontext()
    )
    with trace_scope:
        try:
            preconversation_life_ecology = await _run_preconversation_life_ecology(
                host=host,
                conversation_started_at=conversation_started_at,
                units=preconversation_life_ecology_units,
            )
            overlap_executions: dict[str, dict[str, Any]] = {}
            overlap_failures: dict[str, str] = {}
            for index, turn in enumerate(scenario.turns):
                trace_start = len(trace_collector.snapshot()) if trace_collector is not None else 0
                observed_at = (
                    conversation_started_at
                    + timedelta(minutes=turn.at_minutes)
                    + timedelta(
                        milliseconds=getattr(turn, "launch_offset_ms", 0)
                    )
                )
                if fast_pacing:
                    clock.advance_to(observed_at)
                sent_before = len(delivery.sent)
                foreground_started_ns = time.perf_counter_ns()
                latency_before = {
                    _latency_sample_identity(sample): float(
                        getattr(sample, "duration_ms")
                    )
                    for sample in _latency_samples(host)
                }
                error: str | None = None
                post_inbound_drain_error: str | None = None
                status = "error"
                inbound_execution: dict[str, Any] = {
                    "statuses": [],
                    "source_event_ids": [scenario.source_event_id(turn)],
                    "user_messages": [turn.text],
                }
                try:
                    overlap_group = getattr(turn, "overlap_group", None)
                    if overlap_group is None:
                        inbound_execution = await _submit_scenario_turn(
                            host=host,
                            scenario=scenario,
                            turn=turn,
                            recipient_id="private-self-expression-audit-recipient",
                            conversation_started_at=conversation_started_at,
                            clock=clock,
                            fast_pacing=fast_pacing,
                        )
                    elif turn.turn_id in overlap_executions:
                        inbound_execution = overlap_executions[turn.turn_id]
                    elif overlap_group in overlap_failures:
                        raise RuntimeError(overlap_failures[overlap_group])
                    else:
                        grouped_turns = tuple(
                            candidate
                            for candidate in scenario.turns
                            if getattr(candidate, "overlap_group", None)
                            == overlap_group
                        )
                        try:
                            grouped_executions = (
                                await _submit_scenario_overlap_group(
                                    host=host,
                                    scenario=scenario,
                                    turns=grouped_turns,
                                    recipient_id=(
                                        "private-self-expression-audit-recipient"
                                    ),
                                    conversation_started_at=(
                                        conversation_started_at
                                    ),
                                    clock=clock,
                                    fast_pacing=fast_pacing,
                                )
                            )
                        except Exception as group_exc:
                            overlap_failures[overlap_group] = (
                                f"{type(group_exc).__name__}: {group_exc}"
                            )
                            raise
                        overlap_executions.update(
                            {
                                candidate.turn_id: execution
                                for candidate, execution in zip(
                                    grouped_turns,
                                    grouped_executions,
                                    strict=True,
                                )
                            }
                        )
                        inbound_execution = overlap_executions[turn.turn_id]
                    status = str(inbound_execution["status"])
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    try:
                        await host.drain(max_action_units=16, max_background_units=0)
                    except Exception as exc:
                        post_inbound_drain_error = f"{type(exc).__name__}: {exc}"
                foreground_ended_ns = time.perf_counter_ns()
                attributed_started_ns = int(
                    inbound_execution.get("_audit_started_ns", foreground_started_ns)
                )
                attributed_ended_ns = int(
                    inbound_execution.get("_audit_ended_ns", foreground_ended_ns)
                )
                started_ns_by_turn[str(turn.turn_id)] = attributed_started_ns
                foreground_ms = round(
                    (attributed_ended_ns - attributed_started_ns) / 1_000_000,
                    1,
                )
                first_visible_observed_ns = delivery.first_visible_observed_ns(
                    start_index=sent_before
                )
                first_visible_reply_wall_ms = (
                    round(
                        (
                            first_visible_observed_ns
                            - foreground_started_ns
                        )
                        / 1_000_000,
                        1,
                    )
                    if first_visible_observed_ns is not None
                    else None
                )
                latency_after = _latency_samples(host)
                turn_latency_samples = tuple(
                    sample
                    for sample in latency_after
                    if latency_before.get(_latency_sample_identity(sample))
                    != float(getattr(sample, "duration_ms"))
                )

                background_started_ns = time.perf_counter_ns()
                background_error: str | None = None
                try:
                    units = first_turn_background_units if index == 0 else background_units
                    if units:
                        await host.drain(max_action_units=0, max_background_units=units)
                except Exception as exc:
                    background_error = f"{type(exc).__name__}: {exc}"
                background_ms = round(
                    (time.perf_counter_ns() - background_started_ns) / 1_000_000,
                    1,
                )
                trace_end = len(trace_collector.snapshot()) if trace_collector is not None else 0
                turn_trace_ranges.append((str(turn.turn_id), trace_start, trace_end))
                row = {
                    "turn_id": turn.turn_id,
                    "source_event_id": scenario.source_event_id(turn),
                    "source_event_ids": inbound_execution["source_event_ids"],
                    "observed_at": observed_at.isoformat(),
                    "user_text": turn.text,
                    "user_messages": inbound_execution["user_messages"],
                    "ingress_mode": (
                        "burst"
                        if len(inbound_execution["source_event_ids"]) > 1
                        else "single"
                    ),
                    "overlap_group": getattr(turn, "overlap_group", None),
                    "status": status,
                    "fragment_statuses": inbound_execution["statuses"],
                    "foreground_wall_ms": foreground_ms,
                    "first_visible_reply_wall_ms": first_visible_reply_wall_ms,
                    "first_visible_reply_measurement": (
                        "process_monotonic_to_isolated_provider_acceptance"
                        if first_visible_reply_wall_ms is not None
                        else "not_observed"
                    ),
                    "latency_segments": [
                        _serialize_latency_sample(
                            sample,
                            fast_pacing=fast_pacing,
                        )
                        for sample in sorted(
                            turn_latency_samples,
                            key=_latency_sample_identity,
                        )
                    ],
                    "background_wall_ms": background_ms,
                    "deliveries": delivery.sent[sent_before:],
                    "error": error,
                    "post_inbound_drain_error": post_inbound_drain_error,
                    "background_error": background_error,
                }
                runtime_turns.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

            await _drain_terminal_receipts(
                host=host,
                fast_clock=clock if fast_pacing else None,
            )
            evidence = host.export_replay_evidence()
            audit = PrivateSelfExpressionAuditEvaluator().evaluate(
                evidence=evidence,
                scenario=scenario,
            )
            source_review_authority_health = host.proactive_source_authority_health()
        finally:
            await host.aclose()

    trace_summary: dict[str, object] = {
        "enabled": False,
        "event_count": 0,
        "trace_sha256": None,
    }
    if trace_path is not None and trace_collector is not None:
        events = trace_collector.snapshot()
        trace_hash = _write_private_json_exclusive(
            trace_path,
            _trace_document(
                events=events,
                turn_ranges=tuple(turn_trace_ranges),
                dropped_count=trace_collector.dropped_count,
            ),
        )
        trace_summary = {
            "enabled": True,
            "event_count": len(events),
            "trace_sha256": trace_hash,
        }

    immutable_replay_audit = audit.model_dump(mode="json")
    final_latency_samples = _latency_samples(host)
    _attribute_runtime_turn_evidence(
        runtime_turns=runtime_turns,
        immutable_replay_audit=immutable_replay_audit,
        delivery=delivery,
        latency_samples=final_latency_samples,
        started_ns_by_turn=started_ns_by_turn,
        fast_pacing=fast_pacing,
    )
    naturalness_readiness = assess_naturalness_readiness(
        projection=getattr(evidence, "replay", evidence.projection),
        immutable_replay_audit=immutable_replay_audit,
        requested_preconversation_units=preconversation_life_ecology_units,
    )
    document: dict[str, Any] = {
        "contract": "private-self-expression-real-audit-run.2",
        "reporting_policy": "descriptive_only_not_an_acceptance_rule",
        "latency_evidence": {
            "first_visible_clock": "process_monotonic",
            "pacing_clock": "virtual" if fast_pacing else "wall",
            "scheduler_clock": "virtual" if fast_pacing else "wall",
            "segment_semantics": (
                "observed spans are accumulated by label and may overlap; "
                "they are not an additive phase partition"
            ),
            "role_provider_timing": {
                "entry_segment": "ingress_to_first_role_provider",
                "ttft_segment": "model_ttft",
                "ttft_status": "unavailable",
                "ttft_reason": "non_streaming_completion_api",
                "completion_segment": "model_completion",
            },
            "fast_pacing_ingress_semantics": (
                "coalescing, queue, and ingress_to_visible samples may include "
                "persisted virtual durations; first_visible_reply_wall_ms does not"
                if fast_pacing
                else "not_applicable"
            ),
        },
        "isolation": {
            "database": str(database),
            "qq_delivery": "in_process_capture_with_positive_lookup",
            "production_database_used": False,
            "fast_pacing": fast_pacing,
            "conversation_started_at": conversation_started_at.isoformat(),
        },
        "source_closure_rejection_trace": trace_summary,
        "source_review_authority_health": source_review_authority_health,
        "preconversation_life_ecology": preconversation_life_ecology,
        "naturalness_readiness": naturalness_readiness.model_dump(mode="json"),
        "runtime_turns": runtime_turns,
        "final_latency_segments": [
            _serialize_latency_sample(sample, fast_pacing=fast_pacing)
            for sample in sorted(
                final_latency_samples,
                key=_latency_sample_identity,
            )
        ],
        "immutable_replay_audit": immutable_replay_audit,
    }
    _write_private_json_exclusive(output, document)
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the private-self → character recall → expression audit with the "
            "configured real model in an isolated World-v2 database."
        )
    )
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional nonexistent JSON output path; defaults to a timestamped output file.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="Optional nonexistent isolated database to retain after the run.",
    )
    parser.add_argument(
        "--rejection-trace-output",
        type=Path,
        help=(
            "Optional nonexistent 0600 JSON path for process-local source-closure "
            "rejection diagnostics. This is separate from the ordinary audit report."
        ),
    )
    parser.add_argument(
        "--fast-pacing",
        action="store_true",
        help="Skip sender/cadence sleeps; provider and application work remain real.",
    )
    parser.add_argument(
        "--start-at",
        type=_aware_utc_instant,
        help=(
            "Optional timezone-aware ISO conversation start instant. "
            "Defaults to the current UTC instant."
        ),
    )
    parser.add_argument(
        "--preconversation-life-ecology-units",
        type=int,
        default=0,
        help=(
            "Before the first user turn, advance this many isolated ten-minute "
            "Clock wakes with real Life Ecology enabled. The model remains free "
            "to create or decline life events; no Actions are drained."
        ),
    )
    parser.add_argument("--first-turn-background-units", type=int, default=24)
    parser.add_argument("--background-units", type=int, default=4)
    parser.add_argument(
        "--max-turns",
        type=int,
        help="Run only the first N fixture turns for a bounded retained diagnostic.",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if (
        args.preconversation_life_ecology_units < 0
        or args.first_turn_background_units < 0
        or args.background_units < 0
    ):
        raise SystemExit("background unit counts must be non-negative")
    if args.max_turns is not None and args.max_turns < 1:
        raise SystemExit("max turns must be positive")
    ambient = Settings()
    configured_production_database = Path(ambient.database_path).expanduser()
    production_database = (
        configured_production_database
        if configured_production_database.is_absolute()
        else _ROOT / configured_production_database
    ).resolve()
    explicit_database = (
        _safe_explicit_database(
            args.database,
            production_database=production_database,
        )
        if args.database is not None
        else None
    )
    default_output = _ROOT / (
        "output/private-self-expression-real-audit-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    output = _safe_output(
        args.output or default_output,
        production_database=production_database,
        audit_database=explicit_database,
    )
    rejection_trace_output = (
        _safe_rejection_trace_output(
            args.rejection_trace_output,
            production_database=production_database,
            audit_database=explicit_database,
            report_output=output,
        )
        if args.rejection_trace_output is not None
        else None
    )
    if args.database is not None:
        assert explicit_database is not None
        asyncio.run(
            run(
                fixture=args.fixture,
                output=output,
                database=explicit_database,
                start_at=args.start_at,
                fast_pacing=args.fast_pacing,
                preconversation_life_ecology_units=(args.preconversation_life_ecology_units),
                first_turn_background_units=args.first_turn_background_units,
                background_units=args.background_units,
                max_turns=args.max_turns,
                rejection_trace_output=rejection_trace_output,
            )
        )
    else:
        with tempfile.TemporaryDirectory(prefix="private-self-expression-audit-") as directory:
            asyncio.run(
                run(
                    fixture=args.fixture,
                    output=output,
                    database=Path(directory) / "world-v2.sqlite",
                    start_at=args.start_at,
                    fast_pacing=args.fast_pacing,
                    preconversation_life_ecology_units=(args.preconversation_life_ecology_units),
                    first_turn_background_units=args.first_turn_background_units,
                    background_units=args.background_units,
                    max_turns=args.max_turns,
                    rejection_trace_output=rejection_trace_output,
                )
            )
    print(
        json.dumps(
            {
                "output": str(output),
                "reporting_policy": "descriptive_only_not_an_acceptance_rule",
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
