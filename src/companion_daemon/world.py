"""Append-only, deterministic world ledger for the companion's virtual life."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4

import yaml

from companion_daemon.db import CompanionStore
from companion_daemon.action_coordinator import (
    DeliveryStatus,
    OutgoingSegment,
    SegmentedActionCoordinator,
    UserInterjectionKind,
)
from companion_daemon.affect_display import plan_affect_display
from companion_daemon.expression_plan import ExpressionPlan, compile_expression_plan
from companion_daemon.life_simulation import LifeSimulation
from companion_daemon.life_appraisal import appraise_committed_life_outcome
from companion_daemon.life_evolution import LifeEvolution
from companion_daemon.time import utc_now
from companion_daemon.world_interaction_rules import (
    HARMFUL_INTERACTION_APPRAISALS,
    WorldInteractionRules,
)
from companion_daemon.world_relationship import (
    evaluate_relationship_stage,
    relationship_event_significance,
    relationship_slow_warmth,
    stage_event_payload,
)
from companion_daemon.world_affect import (
    apply_appraisal,
    decay_affect,
    initial_affect,
    outcome_payload as affect_outcome_payload,
)
from companion_daemon.world_affinity import settle_affinity_interaction
from companion_daemon.character_deliberation import CharacterDeliberation, UserRequest
from companion_daemon.character_core_evolution import (
    CharacterCoreEvolutionError,
    CoreChangeProposal,
    evaluate_core_change,
)
from companion_daemon.conversation_commitments import (
    ConversationCommitmentError,
    create_conversation_thread,
    evaluate_waiting_response,
)
from companion_daemon.tool_action import FakeToolAdapter, ToolExecutionRequest
from companion_daemon.world_behavior import WorldBehaviorPolicy
from companion_daemon.world_cost_ledger import (
    ALL_COST_CATEGORIES,
    CostCategory,
    CostLedgerEvent,
    CostPolicy,
    CostRequest,
    SocialTransgressionPolicy,
    SocialTransgressionRecord,
    WorldCostLedger,
    evaluate_social_transgression,
)


class WorldError(ValueError):
    """A world command violates a domain invariant."""


class ConcurrencyConflict(WorldError):
    """The caller planned from an obsolete world revision."""


@dataclass(frozen=True)
class WorldEvent:
    event_id: str
    world_id: str
    revision: int
    event_type: str
    schema_version: int
    logical_at: str
    observed_at: str
    actor: dict[str, object]
    source: str
    correlation_id: str
    causation_id: str | None
    idempotency_key: str | None
    payload: dict[str, object]
    payload_hash: str


@dataclass(frozen=True)
class WorldDecision:
    world_id: str
    revision: int
    events: tuple[WorldEvent, ...]
    state_hash: str


@dataclass(frozen=True)
class CommittedAppraisal:
    kind: str
    severity: int = 3
    target: str = "general"
    acts: tuple[str, ...] = ()
    evidence_spans: tuple[str, ...] = ()
    dimensions: dict[str, object] | None = None

    def interaction_payload(self) -> dict[str, object]:
        return {
            "severity": self.severity,
            "target": self.target,
            "acts": list(self.acts),
            "evidence_spans": list(self.evidence_spans),
            **dict(self.dimensions or {}),
        }


@dataclass(frozen=True)
class AcceptedTurn:
    world_id: str
    user_id: str
    message_id: str
    intent_id: str
    appraisal: CommittedAppraisal
    expected_revision: int
    causation_id: str | None = None
    private_impression: dict[str, object] | None = None


@dataclass(frozen=True)
class ProjectionReport:
    world_id: str
    projection_name: str
    applied_revision: int
    event_count: int
    state_hash: str
    matches_live: bool


@dataclass(frozen=True)
class WorldEnablementReport:
    """Evidence required before routing real chat traffic into a world epoch."""

    world_id: str
    ready: bool
    projection_reports: tuple[ProjectionReport, ...]
    open_action_ids: tuple[str, ...]
    unknown_action_ids: tuple[str, ...]
    delivery_receipts_supported: bool
    invariant_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifeShareDelivery:
    """A selected experience and its atomically-created external action."""

    experience_id: str
    delivery_id: int
    trace_id: int
    action_id: str
    text: str
    revision: int


@dataclass(frozen=True)
class WorldTurnProjection:
    """One coherent, bounded read model for a dialogue turn.

    The projection is deliberately read-only.  It lets reply generation use
    context, retrieval, and display strategy from exactly one ledger revision
    without promoting any of those derived views to separate world truth.
    """

    world_id: str
    revision: int
    state_hash: str
    state: dict[str, object]
    conversation_context: dict[str, object]
    retrieved_sources: tuple[dict[str, str], ...]
    expression_plan: ExpressionPlan


class WorldKernel:
    """The sole write seam for virtual-world facts, plans, and settled actions."""

    SNAPSHOT_INTERVAL = 25

    def accept_turn(self, turn: AcceptedTurn) -> WorldDecision:
        """Atomically commit one typed Appraisal without exposing command schema."""
        return self.submit(
            {
                "type": "appraise_turn",
                "world_id": turn.world_id,
                "appraisal": turn.appraisal.kind,
                "interaction": turn.appraisal.interaction_payload(),
                "intent_id": turn.intent_id,
                "message_id": turn.message_id,
                "user_id": turn.user_id,
                "actor": {"kind": "companion", "id": "zhizhi"},
                "causation_id": turn.causation_id or turn.message_id,
                "idempotency_key": f"appraise:{turn.intent_id}",
                **(
                    {"private_impression": dict(turn.private_impression)}
                    if turn.private_impression
                    else {}
                ),
            },
            expected_revision=turn.expected_revision,
        )
    COST_POLICY = CostPolicy(
        daily_budget_units=1000,
        automatic_daily_budget_units=800,
        category_daily_budget_units={"image": 120, "vision": 180, "audio": 180, "tool": 240},
        category_automatic_daily_budget_units={
            "image": 80, "vision": 140, "audio": 140, "tool": 0,
        },
    )
    TRANSGRESSION_POLICY = SocialTransgressionPolicy(
        daily_strike_budget=3, cooldown=timedelta(hours=6)
    )

    def __init__(self, store: CompanionStore):
        self.store = store
        self.life_simulation = LifeSimulation()
        self.life_evolution = LifeEvolution()
        self.interaction_rules = WorldInteractionRules()
        self.character_deliberation = CharacterDeliberation()
        self.behavior_policy = WorldBehaviorPolicy()
        self.action_coordinator = SegmentedActionCoordinator()

    def submit(self, command: dict[str, object], *, expected_revision: int) -> WorldDecision:
        command_type = str(command.get("type") or "")
        if command_type == "start_world":
            return self._start_world(command, expected_revision)
        world_id = self._command_world_id(command)
        idempotency_key = self._idempotency_key(command)
        with self.store.connect() as conn:
            existing = self._receipt(conn, world_id, idempotency_key)
            if existing:
                return self._decision_from_receipt(conn, world_id, existing)
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            events = self._events_for_command(command, state)
            events = self._guard_scheduled_outbound(command, state, events)
            if not events:
                return WorldDecision(world_id, revision, (), _state_hash(state))
            try:
                return self._append_and_project(
                    conn,
                    world_id,
                    revision,
                    state,
                    events,
                    idempotency_key=idempotency_key,
                    correlation_id=str(command.get("correlation_id") or uuid4()),
                    source=str(command.get("source") or "world_command"),
                    actor=_as_dict(command.get("actor", {"kind": "system"}), "actor"),
                    causation_id=(str(command["causation_id"]) if command.get("causation_id") else None),
                )
            except sqlite3.IntegrityError as exc:
                if "world_events.world_id, world_events.revision" in str(exc):
                    raise ConcurrencyConflict("world revision changed while command was being appended") from exc
                raise

    def _guard_scheduled_outbound(
        self,
        command: dict[str, object],
        state: dict[str, object],
        events: list[tuple[str, dict[str, object]]],
    ) -> list[tuple[str, dict[str, object]]]:
        """Attach the shared outbound audit before externally-visible Actions exist."""
        scheduled = next(
            (payload for event_type, payload in events if event_type == "ActionScheduled"),
            None,
        )
        if scheduled is None:
            return events
        # A user-confirmed tool operation is governed by the permission/risk
        # chain, not by proactive-chat cooldown and unanswered-message limits.
        if str(command.get("type") or "") == "authorize_tool_action":
            return events
        action_kind = str(scheduled.get("kind") or "")
        explicit_kind = str(command.get("outbound_kind") or "")
        message_kind = explicit_kind or {
            "conversation_pulse": "pulse",
            "reply_later": "reply",
            "media_delivery": "media",
            "sticker_delivery": "reaction",
            "reaction_delivery": "reaction",
        }.get(action_kind, "")
        if not message_kind and "followup" in action_kind:
            message_kind = "followup"
        if not message_kind and action_kind.startswith("tool_"):
            message_kind = "tool"
        if not message_kind:
            return events
        payload = _as_dict(scheduled.get("payload", {}), "scheduled outbound payload")
        request_id = str(scheduled.get("action_id") or command.get("idempotency_key") or uuid4())
        trigger = str(command.get("outbound_trigger") or action_kind or message_kind)
        text = str(command.get("text") or payload.get("text") or "") or None
        topic_key = str(command.get("topic_key") or payload.get("request_id") or "") or None
        request, projection, allowance = self.behavior_policy.outbound_allowance(
            state, request_id=request_id, message_kind=message_kind,
            trigger=trigger, text=text, topic_key=topic_key,
        )
        override = _outbound_soft_override(command.get("outbound_override"), allowance.reasons)
        audit = _outbound_policy_payload(
            request=request, projection=projection, allowance=allowance, override=override,
        )
        if not allowance.allowed and override is None:
            return [("OutboundActionRejected", audit)]
        prefix: list[tuple[str, dict[str, object]]] = []
        if override is not None:
            prefix.append(
                (
                    "ControlledTransgressionCommitted",
                    self._controlled_transgression_payload(
                        state,
                        request_id=request_id,
                        override=override,
                        user_id=str(command.get("user_id") or command.get("canonical_user_id") or ""),
                    ),
                )
            )
            prefix.append(("OutboundSoftGateOverridden", audit))
        prefix.append(("OutboundActionAllowed", audit))
        return [*prefix, *events]

    def _controlled_transgression_payload(
        self,
        state: dict[str, object],
        *,
        request_id: str,
        override: dict[str, object],
        user_id: str,
    ) -> dict[str, object]:
        logical_at = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
        history = tuple(
            SocialTransgressionRecord(
                idempotency_key=str(_as_dict(raw, "transgression")["request_id"]),
                logical_at=_parse_at(str(_as_dict(raw, "transgression")["logical_at"])),
                strikes=int(_as_dict(raw, "transgression")["strikes"]),
            )
            for raw in _as_list(state.get("controlled_transgressions", []), "transgressions")
        )
        strikes = int(override["strike"])
        decision = evaluate_social_transgression(
            self.TRANSGRESSION_POLICY,
            history,
            logical_at=logical_at,
            requested_strikes=strikes,
        )
        if not decision.allowed:
            raise WorldError(decision.reason)
        unresolved = bool(_as_dict(state.get("emotion_modulation", {}), "affect").get("unresolved"))
        return {
            "request_id": request_id,
            "user_id": user_id,
            "logical_at": logical_at.isoformat(),
            "reason": str(override["reason"]),
            "relationship_cost": int(override["cost"]),
            "affect_cost": 4 if unresolved else 1,
            "strikes": decision.strikes_charged,
            "overridden_gates": list(_as_list(override.get("overridden_gates", []), "gates")),
            "rule_version": "controlled-transgression-v1",
        }

    def claim_message_turn(self, world_id: str, message_id: str) -> bool:
        """Atomically elect one coordinator for an observed platform message."""
        key = f"turn-claim:{message_id}"
        with self.store.connect() as conn:
            if self._receipt(conn, world_id, key):
                return False
            revision, state = self._load_state(conn, world_id)
            try:
                self._append_and_project(
                    conn,
                    world_id,
                    revision,
                    state,
                    [("TurnProcessingClaimed", {"message_id": message_id})],
                    idempotency_key=key,
                    correlation_id=message_id,
                    source="turn_coordinator",
                    actor={"kind": "coordinator"},
                    causation_id=message_id,
                )
            except sqlite3.IntegrityError:
                if self._receipt(conn, world_id, key):
                    return False
                raise
            return True

    def settle_turn(
        self, world_id: str, message_id: str, *, status: str, reason: str,
        expected_revision: int,
    ) -> WorldDecision:
        return self.submit(
            {
                "type": "settle_turn", "world_id": world_id,
                "message_id": message_id, "status": status, "reason": reason,
                "idempotency_key": f"turn-settle:{message_id}:{status}:{reason}",
            },
            expected_revision=expected_revision,
        )

    def commit_private_impression(
        self,
        world_id: str,
        *,
        impression_id: str,
        user_id: str,
        kind: str,
        summary: str,
        confidence: float,
        source_event_ids: tuple[str, ...],
        expires_at: datetime,
        expected_revision: int,
    ) -> WorldDecision:
        """Persist a bounded, fallible interpretation without creating a fact.

        ``source_event_ids`` retains the architecture document's field name,
        but contains canonical projection source references (for example
        ``message:<id>``), never opaque ``WorldEvent.event_id`` UUIDs.
        """
        return self.submit(
            {
                "type": "commit_private_impression",
                "world_id": world_id,
                "impression_id": impression_id,
                "user_id": user_id,
                "kind": kind,
                "summary": summary,
                "confidence": confidence,
                "source_event_ids": list(source_event_ids),
                "expires_at": expires_at.isoformat(),
                "idempotency_key": f"private-impression:{impression_id}",
            },
            expected_revision=expected_revision,
        )

    def record_user_affect(
        self,
        world_id: str,
        *,
        message_id: str,
        user_id: str,
        affect: dict[str, object],
        expected_revision: int,
    ) -> WorldDecision:
        """Append a bounded, late-arriving reading of the user's affect.

        This is deliberately narrower than ``accept_turn``.  A semantic
        advisory can finish after a reply has been planned, but it must never
        retroactively change the turn's facts, action, companion affect, or
        relationship accounting.  It only contributes a sourced user-affect
        episode for future turns.
        """
        return self.submit(
            {
                "type": "record_user_affect",
                "world_id": world_id,
                "message_id": message_id,
                "user_id": user_id,
                "affect": dict(affect),
                "idempotency_key": f"user-affect:{message_id}",
            },
            expected_revision=expected_revision,
        )

    def record_advisory_companion_affect(
        self,
        world_id: str,
        *,
        message_id: str,
        user_id: str,
        appraisal: dict[str, object],
        expected_revision: int,
    ) -> WorldDecision:
        """Record a late, source-bound companion affect consequence only.

        It is intentionally unable to alter a settled turn's relationship,
        deliberation, delivery Action, or visible text.  The projection can
        nevertheless carry a verified negative residue into the next turn.
        """
        return self.submit(
            {
                "type": "record_advisory_companion_affect",
                "world_id": world_id,
                "message_id": message_id,
                "user_id": user_id,
                "appraisal": dict(appraisal),
                "idempotency_key": f"advisory-companion-affect:{message_id}",
            },
            expected_revision=expected_revision,
        )

    def contradict_private_impression(
        self,
        world_id: str,
        *,
        impression_id: str,
        source_event_ids: tuple[str, ...],
        reason: str,
        expected_revision: int,
    ) -> WorldDecision:
        return self.submit(
            {
                "type": "contradict_private_impression",
                "world_id": world_id,
                "impression_id": impression_id,
                "source_event_ids": list(source_event_ids),
                "reason": reason,
                "idempotency_key": f"private-impression-contradiction:{impression_id}:{_hash(reason)[:16]}",
            },
            expected_revision=expected_revision,
        )

    def commit_private_commitment(
        self,
        world_id: str,
        *,
        commitment_id: str,
        user_id: str,
        intention: str,
        source_event_ids: tuple[str, ...],
        expires_at: datetime,
        priority: int,
        related_thread_id: str = "",
        expected_revision: int,
    ) -> WorldDecision:
        """Persist an internal future concern; it is neither a plan nor an Action."""
        return self.submit(
            {
                "type": "commit_private_commitment",
                "world_id": world_id,
                "commitment_id": commitment_id,
                "user_id": user_id,
                "intention": intention,
                "source_event_ids": list(source_event_ids),
                "expires_at": expires_at.isoformat(),
                "priority": priority,
                "related_thread_id": related_thread_id,
                "idempotency_key": f"private-commitment:{commitment_id}",
            },
            expected_revision=expected_revision,
        )

    def resolve_private_commitment(
        self,
        world_id: str,
        *,
        commitment_id: str,
        outcome: str,
        reason: str,
        expected_revision: int,
    ) -> WorldDecision:
        return self.submit(
            {
                "type": "resolve_private_commitment",
                "world_id": world_id,
                "commitment_id": commitment_id,
                "outcome": outcome,
                "reason": reason,
                "idempotency_key": f"private-commitment-resolve:{commitment_id}:{outcome}",
            },
            expected_revision=expected_revision,
        )

    def start_from_seed_file(self, path: Path) -> WorldDecision:
        """Start one clean world epoch from a human-reviewed YAML seed."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        seed = _as_dict(raw, "world seed")
        return self.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    def ensure_seed_file(self, path: Path) -> WorldDecision:
        """Start the seed once; later process starts only load its revision."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        seed = _as_dict(raw, "world seed")
        world_id = str(seed.get("world_id") or "")
        with self.store.connect() as conn:
            row = conn.execute(
                "select revision from worlds where world_id = ?", (world_id,)
            ).fetchone()
            if row:
                state = json.loads(
                    conn.execute(
                        "select state_json from world_current_state where world_id = ?", (world_id,)
                    ).fetchone()["state_json"]
                )
                return WorldDecision(world_id, int(row["revision"]), (), _state_hash(state))
        return self.submit({"type": "start_world", "seed": seed}, expected_revision=0)

    def revision(self, world_id: str) -> int:
        with self.store.connect() as conn:
            row = conn.execute("select revision from worlds where world_id = ?", (world_id,)).fetchone()
        if not row:
            raise WorldError(f"unknown world: {world_id}")
        return int(row["revision"])

    def import_verified_facts(self, world_id: str, facts: list[str]) -> WorldDecision | None:
        """Carry explicit user facts into a fresh epoch without importing old narrative state."""
        latest: WorldDecision | None = None
        for value in facts:
            normalized = value.strip()
            if not normalized:
                continue
            fact_id = f"legacy-verified:{_hash(normalized)[:20]}"
            try:
                latest = self.submit(
                    {
                        "type": "confirm_fact",
                        "world_id": world_id,
                        "fact_id": fact_id,
                        "subject": "user",
                        "value": normalized,
                        "source": "verified_user_fact_import",
                        "idempotency_key": f"fact-import:{fact_id}",
                    },
                    expected_revision=self.revision(world_id),
                )
            except WorldError as exc:
                if "new id" not in str(exc):
                    raise
        return latest

    def queue_outgoing_action(
        self,
        *,
        canonical_user_id: str,
        platform: str,
        text: str,
        text_parts: list[str] | tuple[str, ...] | None = None,
        part_delays_ms: list[int] | tuple[int, ...] | None = None,
        kind: str,
        expires_at: datetime,
        trace: dict[str, object],
        complete_by_observed_at: datetime | None = None,
    ) -> tuple[int, int, str]:
        """Atomically create the outbox row, turn trace, and world action."""
        parts = tuple(text_parts or (text,))
        if "".join(parts) != text:
            raise WorldError("outgoing text_parts must concatenate to text")
        delays = tuple(part_delays_ms or (0,) * len(parts))
        if len(delays) != len(parts) or any(
            type(delay) is not int or not 0 <= delay <= 20_000
            for delay in delays
        ):
            raise WorldError("outgoing part delays must match bounded text parts")
        world_id = str(trace.get("world_id") or "")
        if not world_id:
            raise WorldError("world delivery trace requires world_id")
        if complete_by_observed_at is not None and complete_by_observed_at.tzinfo is None:
            raise WorldError("outgoing completion deadline must be timezone-aware")
        denied_reasons: tuple[str, ...] = ()
        with self.store.connect() as conn:
            revision, state = self._load_state(conn, world_id)
            settlement = trace.get("action_settlement")
            action_dependencies: tuple[str, ...] = ()
            if settlement is not None:
                settlement_view = _as_dict(settlement, "outgoing action settlement")
                if settlement_view.get("status") != "pending_guard_settlement":
                    raise WorldError("outgoing action settlement has unsupported status")
                action_dependencies = self._require_settleable_reply_actions_in_state(
                    state,
                    tuple(
                        str(item)
                        for item in _as_list(
                            settlement_view.get("action_ids", []),
                            "outgoing action settlement ids",
                        )
                    ),
                    user_id=str(trace.get("user_id") or f"user:{canonical_user_id}"),
                )
            logical_at = str(_as_dict(state["clock"], "clock")["logical_at"])
            request_id = str(trace.get("outbound_request_id") or _hash(
                f"{world_id}|{kind}|{text}|{trace.get('input_message_id') or ''}|{logical_at}"
            )[:24])
            trigger = str(trace.get("outbound_trigger") or trace.get("appraisal") or trace.get("direction") or kind)
            topic_key = str(trace.get("topic_key") or "") or None
            request, projection, allowance = self.behavior_policy.outbound_allowance(
                state, request_id=request_id, message_kind=kind, trigger=trigger,
                text=text, topic_key=topic_key,
            )
            override = _outbound_soft_override(trace.get("outbound_override"), allowance.reasons)
            allowed = allowance.allowed or override is not None
            policy_payload = _outbound_policy_payload(
                request=request, projection=projection, allowance=allowance, override=override,
            )
            if not allowed:
                self._append_and_project(
                    conn, world_id, revision, state,
                    [("OutboundActionRejected", policy_payload)],
                    idempotency_key=f"outbound-policy:rejected:{request_id}",
                    correlation_id=str(uuid4()), source="outbound_policy",
                    actor={"kind": "companion"}, causation_id=str(trace.get("input_message_id") or "") or None,
                )
                denied_reasons = allowance.reasons
            else:
                audit_events: list[tuple[str, dict[str, object]]] = []
                if override is not None:
                    audit_events.append(
                        (
                            "ControlledTransgressionCommitted",
                            self._controlled_transgression_payload(
                                state,
                                request_id=request_id,
                                override=override,
                                user_id=f"user:{canonical_user_id}",
                            ),
                        )
                    )
                    audit_events.append(("OutboundSoftGateOverridden", policy_payload))
                audit_events.append(("OutboundActionAllowed", policy_payload))
            now = utc_now().isoformat()
            if denied_reasons:
                delivery_id = trace_row_id = -1
            else:
                delivery = conn.execute(
                    """
                    insert into outbox_messages (canonical_user_id, platform, text, kind, status, created_at)
                    values (?, ?, ?, ?, 'planned', ?)
                    """,
                    (canonical_user_id, platform, text, kind, now),
                )
                delivery_id = int(delivery.lastrowid)
                trace_row = conn.execute(
                    """
                    insert into turn_traces (
                      canonical_user_id, direction, appraisal, expression_policy,
                      allowed_facts_json, short_lived_constraint, observable_reason,
                      output_text, delivery_id, status, created_at, updated_at
                    ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
                    """,
                    (
                        canonical_user_id,
                        str(trace.get("direction") or "incoming_reply"),
                        str(trace["appraisal"]),
                        str(trace["expression_policy"]),
                        _stable_json(trace.get("allowed_facts", [])),
                        trace.get("short_lived_constraint"),
                        str(trace["observable_reason"]),
                        text,
                        delivery_id,
                        now,
                        now,
                    ),
                )
                trace_row_id = int(trace_row.lastrowid)
                action_id = f"outgoing:{delivery_id}"
                private_inner_events: list[tuple[str, dict[str, object]]] = []
                expected_inner_user_id = f"user:{canonical_user_id}"
                raw_private_impression = trace.get("private_impression")
                if raw_private_impression is not None:
                    impression = self._private_impression_payload(
                        state,
                        _as_dict(
                            raw_private_impression,
                            "outgoing private impression",
                        ),
                    )
                    if impression["user_id"] != expected_inner_user_id:
                        raise WorldError("outgoing private impression belongs to another user")
                    private_inner_events.append(
                        (
                            "PrivateImpressionCommitted",
                            impression,
                        )
                    )
                raw_private_commitment = trace.get("private_commitment")
                if raw_private_commitment is not None:
                    commitment = self._private_commitment_payload(
                        state,
                        _as_dict(
                            raw_private_commitment,
                            "outgoing private commitment",
                        ),
                        pending_thread_id=str(
                            _as_dict(
                                trace.get("conversation_thread", {}),
                                "outgoing conversation thread",
                            ).get("thread_id")
                            or ""
                        ),
                    )
                    if commitment["user_id"] != expected_inner_user_id:
                        raise WorldError("outgoing private commitment belongs to another user")
                    private_inner_events.append(
                        (
                            "PrivateCommitmentCommitted",
                            commitment,
                        )
                    )
                segmented = self.action_coordinator.plan_action(
                    action_id=action_id,
                    texts=parts,
                    delays_before_ms=delays,
                )
                segment_projection = self.action_coordinator.to_projection(segmented)
                planned_event = self.action_coordinator.planned_world_event(segmented)
                self._append_and_project(
                    conn,
                    world_id,
                    revision,
                    state,
                    [
                        *audit_events,
                        (
                            "ActionScheduled",
                            {
                                "action_id": action_id,
                                "kind": "outgoing_message",
                                "message_kind": kind,
                                "outbound_trigger": trigger,
                                "topic_key": topic_key,
                                "expires_at": expires_at.isoformat(),
                                "complete_by_observed_at": (
                                    complete_by_observed_at.isoformat()
                                    if complete_by_observed_at is not None
                                    else None
                                ),
                                "canonical_user_id": canonical_user_id,
                                "platform": platform,
                                "text": text,
                                "segment_state": segment_projection,
                                "action_dependencies": {
                                    "referenced_action_ids": list(action_dependencies),
                                    "semantics": "pending_external_action_reference",
                                } if action_dependencies else None,
                                "trace": trace,
                                "delivery_id": delivery_id,
                                "trace_id": trace_row_id,
                            },
                        ),
                        planned_event,
                        *private_inner_events,
                    ],
                    idempotency_key=f"outgoing:{delivery_id}",
                    correlation_id=str(uuid4()),
                    source="outbox",
                    actor={"kind": "companion"},
                    causation_id=None,
                )
        if denied_reasons:
            raise WorldError(f"outbound policy rejected: {','.join(denied_reasons)}")
        return delivery_id, trace_row_id, action_id

    def claim_outgoing_segment(
        self,
        delivery_id: int,
        *,
        expected_revision: int,
        lease_seconds: int = 300,
    ) -> OutgoingSegment | None:
        """Claim exactly one planned segment without claiming later text."""
        with self.store.connect() as conn:
            row = conn.execute(
                "select status from outbox_messages where id = ?", (delivery_id,)
            ).fetchone()
            if not row or row["status"] not in {"planned", "sending"}:
                return None
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                raise WorldError(f"world action for delivery {delivery_id} is missing")
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            projection = _as_dict(action.get("segment_state", {}), "segment state")
            segmented = self.action_coordinator.from_projection(projection)
            try:
                updated, claimed = self.action_coordinator.claim_next(segmented)
            except ValueError as exc:
                raise WorldError(str(exc)) from exc
            if lease_seconds < 0:
                raise WorldError("delivery lease cannot be negative")
            now = utc_now().isoformat()
            claimed_event_type, claimed_payload = (
                self.action_coordinator.claimed_world_event(updated, claimed)
            )
            claimed_payload["lease_expires_observed_at"] = (
                utc_now() + timedelta(seconds=lease_seconds)
            ).isoformat()
            conn.execute(
                "update outbox_messages set status = 'sending' where id = ? and status in ('planned', 'sending')",
                (delivery_id,),
            )
            conn.execute(
                "update turn_traces set status = 'sending', updated_at = ? where delivery_id = ?",
                (now, delivery_id),
            )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    ("ActionAttempted", {"action_id": action_id}),
                    (claimed_event_type, claimed_payload),
                ],
                idempotency_key=f"segment-claim:{claimed.segment_id}",
                correlation_id=str(uuid4()),
                source="segment_delivery",
                actor={"kind": "transport"},
                causation_id=action_id,
            )
            return claimed

    def settle_outgoing_segment(
        self,
        delivery_id: int,
        segment_id: str,
        *,
        delivered: bool,
        expected_revision: int,
        reason: str | None = None,
        external_receipt: str | None = None,
        reconciliation_evidence: dict[str, object] | None = None,
        cancel_remaining: bool = False,
    ) -> dict[str, object] | None:
        """Settle one claimed segment; only delivered text enters history."""
        if not delivered:
            return self.settle_outgoing_action(
                delivery_id,
                delivered=False,
                reason=reason or "segment delivery failed",
                external_receipt=external_receipt,
            )
        with self.store.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, platform, status from outbox_messages where id = ?",
                (delivery_id,),
            ).fetchone()
            if not row:
                return None
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                raise WorldError(f"world action for delivery {delivery_id} is missing")
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            current_segments = _as_list(
                _as_dict(action.get("segment_state", {}), "segment state")["segments"],
                "segments",
            )
            current_segment = next(
                (
                    _as_dict(item, "segment")
                    for item in current_segments
                    if str(_as_dict(item, "segment").get("segment_id") or "")
                    == segment_id
                ),
                None,
            )
            if current_segment is None:
                raise WorldError("outgoing segment does not belong to delivery")
            if str(current_segment.get("status") or "") == "delivered":
                if str(current_segment.get("external_receipt") or "") != str(
                    external_receipt or ""
                ):
                    raise WorldError(
                        "delivered segment cannot be reconciled with another receipt"
                    )
                return action
            segmented = self.action_coordinator.from_projection(
                _as_dict(action.get("segment_state", {}), "segment state")
            )
            try:
                updated = self.action_coordinator.confirm_delivered(
                    segmented,
                    segment_id=segment_id,
                    external_receipt=external_receipt,
                )
            except ValueError as exc:
                raise WorldError(str(exc)) from exc
            segment = next(item for item in updated.segments if item.segment_id == segment_id)
            cancelled_ids: tuple[str, ...] = ()
            if cancel_remaining and updated.status is not DeliveryStatus.DELIVERED:
                updated, cancelled_ids = self.action_coordinator.observe_user_interjection(
                    updated,
                    kind=UserInterjectionKind.SUBSTANTIVE,
                    user_message_id="operator-reconciliation",
                )
            complete = updated.status is DeliveryStatus.DELIVERED
            now = utc_now().isoformat()
            conn.execute(
                """
                insert into messages (
                  canonical_user_id, platform, platform_user_id, channel_id, message_id,
                  direction, text, attachments_json, sent_at
                ) values (?, ?, '', null, null, 'out', ?, '[]', ?)
                """,
                (row["canonical_user_id"], row["platform"], segment.text, now),
            )
            conn.execute(
                "update outbox_messages set status = ?, delivered_at = ? where id = ?",
                (
                    "delivered" if complete else "cancelled" if cancelled_ids else "planned",
                    now if complete else None,
                    delivery_id,
                ),
            )
            conn.execute(
                "update turn_traces set status = ?, failure_reason = ?, updated_at = ? where delivery_id = ?",
                (
                    "delivered" if complete else "cancelled" if cancelled_ids else "planned",
                    "operator reconciliation cancelled unsent remainder" if cancelled_ids else None,
                    now,
                    delivery_id,
                ),
            )
            settled_event_type, settled_payload = self.action_coordinator.settled_world_event(
                updated, segment_id=segment_id
            )
            if reconciliation_evidence:
                _as_dict(settled_payload["result"], "segment result")[
                    "reconciliation_evidence"
                ] = reconciliation_evidence
            specifications: list[tuple[str, dict[str, object]]] = [
                (settled_event_type, settled_payload)
            ]
            if cancelled_ids:
                specifications.append(
                    self.action_coordinator.cancelled_world_event(
                        updated,
                        segment_ids=cancelled_ids,
                        user_message_id="operator-reconciliation",
                    )
                )
            if complete:
                specifications.append(
                    (
                        "ActionSettled",
                        {
                            "action_id": action_id,
                            "result": {
                                "kind": "delivery",
                                "status": "delivered",
                                "external_receipt": external_receipt,
                                "segmented": True,
                            },
                        },
                    )
                )
                specifications.extend(
                    self._delivered_turn_events(
                        state,
                        str(
                            _as_dict(action.get("trace", {}), "action trace").get(
                                "input_message_id"
                            )
                            or ""
                        ),
                    )
                )
                trace = _as_dict(action.get("trace", {}), "action trace")
                thread = trace.get("conversation_thread")
                if isinstance(thread, dict):
                    specifications.append(
                        (
                            "ConversationThreadOpened",
                            _conversation_thread_event_payload(
                                thread,
                                source_action_id=action_id,
                                logical_at=_parse_at(
                                    str(_as_dict(state["clock"], "clock")["logical_at"])
                                ),
                            ),
                        )
                    )
            elif cancelled_ids:
                specifications.extend(
                    self._release_trace_private_commitment(
                        action, reason="outgoing_remainder_cancelled"
                    )
                )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                specifications,
                idempotency_key=f"segment-settle:{segment_id}:delivered",
                correlation_id=str(uuid4()),
                source="delivery_reconciliation" if reconciliation_evidence else "segment_delivery",
                actor={
                    "kind": "operator",
                    "id": str(reconciliation_evidence.get("reviewer_id") or ""),
                }
                if reconciliation_evidence
                else {"kind": "transport"},
                causation_id=action_id,
            )
            return {
                "delivery_id": delivery_id,
                "segment_id": segment_id,
                "status": "delivered",
                "complete": complete,
                "cancelled_segment_ids": cancelled_ids,
            }

    def record_outgoing_segment_acceptance(
        self,
        delivery_id: int,
        segment_id: str,
        *,
        lookup_token: str,
        expected_revision: int,
    ) -> bool:
        """Persist a non-terminal platform acceptance for later reconciliation."""
        token = lookup_token.strip()
        if not token or len(token) > 500:
            raise WorldError("receipt lookup token must be present and bounded")
        with self.store.connect() as conn:
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                raise WorldError(f"world action for delivery {delivery_id} is missing")
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            segments = _as_list(
                _as_dict(action.get("segment_state", {}), "segment state")["segments"],
                "segments",
            )
            segment = next(
                (
                    _as_dict(item, "segment")
                    for item in segments
                    if str(_as_dict(item, "segment").get("segment_id") or "")
                    == segment_id
                ),
                None,
            )
            if segment is None or str(segment.get("status") or "") != "sending":
                raise WorldError("only a claimed outgoing segment can be accepted")
            existing = str(segment.get("receipt_lookup_token") or "")
            if existing:
                if existing != token:
                    raise WorldError("outgoing segment already has another lookup token")
                return False
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    (
                        "ActionSegmentDispatchAccepted",
                        {
                            "action_id": action_id,
                            "segment_id": segment_id,
                            "lookup_token": token,
                        },
                    )
                ],
                idempotency_key=f"segment-accepted:{segment_id}",
                correlation_id=str(uuid4()),
                source="segment_delivery",
                actor={"kind": "transport"},
                causation_id=action_id,
            )
            return True

    def mark_outgoing_segment_unknown(
        self,
        delivery_id: int,
        segment_id: str,
        *,
        reason: str,
        expected_revision: int,
    ) -> bool:
        """Record uncertainty for one segment and block later segments."""
        with self.store.connect() as conn:
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                return False
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                return False
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            segmented = self.action_coordinator.from_projection(
                _as_dict(action.get("segment_state", {}), "segment state")
            )
            try:
                updated = self.action_coordinator.mark_unknown(
                    segmented, segment_id=segment_id, reason=reason
                )
            except ValueError as exc:
                raise WorldError(str(exc)) from exc
            now = utc_now().isoformat()
            conn.execute(
                "update outbox_messages set status = 'unknown', failed_at = ?, failure_reason = ? where id = ?",
                (now, reason[:500], delivery_id),
            )
            conn.execute(
                "update turn_traces set status = 'unknown', failure_reason = ?, updated_at = ? where delivery_id = ?",
                (reason[:500], now, delivery_id),
            )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [self.action_coordinator.unknown_world_event(updated, segment_id=segment_id)],
                idempotency_key=f"segment-unknown:{segment_id}",
                correlation_id=str(uuid4()),
                source="segment_delivery",
                actor={"kind": "transport"},
                causation_id=action_id,
            )
            return True

    def observe_outgoing_interjection(
        self,
        delivery_id: int,
        *,
        kind: str,
        user_message_id: str,
        expected_revision: int,
    ) -> tuple[str, ...]:
        """Cancel only unsent segments after a substantive user takeover."""
        try:
            interjection_kind = UserInterjectionKind(kind)
        except ValueError as exc:
            raise WorldError(f"unsupported interjection kind: {kind}") from exc
        with self.store.connect() as conn:
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                return ()
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                return ()
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            segmented = self.action_coordinator.from_projection(
                _as_dict(action.get("segment_state", {}), "segment state")
            )
            updated, cancelled_ids = self.action_coordinator.observe_user_interjection(
                segmented,
                kind=interjection_kind,
                user_message_id=user_message_id,
            )
            if not cancelled_ids:
                return ()
            now = utc_now().isoformat()
            conn.execute(
                "update outbox_messages set status = 'cancelled', failed_at = ?, failure_reason = ? where id = ?",
                (now, "substantive user interjection", delivery_id),
            )
            conn.execute(
                "update turn_traces set status = 'cancelled', failure_reason = ?, updated_at = ? where delivery_id = ?",
                ("substantive user interjection", now, delivery_id),
            )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    self.action_coordinator.cancelled_world_event(
                        updated,
                        segment_ids=cancelled_ids,
                        user_message_id=user_message_id,
                    )
                    ,
                    *self._release_trace_private_commitment(
                        action, reason="substantive_user_interjection"
                    ),
                ],
                idempotency_key=f"segment-interjection:{action_id}:{user_message_id}",
                correlation_id=str(uuid4()),
                source="turn_coordinator",
                actor={"kind": "user"},
                causation_id=user_message_id,
            )
            return cancelled_ids

    def expire_outgoing_remainder(
        self,
        delivery_id: int,
        *,
        reason: str,
        expected_revision: int,
        terminal_reason: str = "complete_deadline_elapsed",
    ) -> tuple[str, ...]:
        """Cancel only never-dispatched beats after a terminal coordination event."""
        with self.store.connect() as conn:
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                return ()
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id:
                return ()
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            segments = _as_list(
                _as_dict(action.get("segment_state", {}), "segment state")["segments"],
                "segments",
            )
            cancelled_ids = tuple(
                str(_as_dict(item, "segment")["segment_id"])
                for item in segments
                if str(_as_dict(item, "segment").get("status") or "") == "planned"
            )
            if not cancelled_ids:
                return ()
            now = utc_now().isoformat()
            conn.execute(
                "update outbox_messages set status = 'cancelled', failed_at = ?, failure_reason = ? where id = ?",
                (now, reason[:500], delivery_id),
            )
            conn.execute(
                "update turn_traces set status = 'cancelled', failure_reason = ?, updated_at = ? where delivery_id = ?",
                (reason[:500], now, delivery_id),
            )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    (
                        "ActionSegmentsCancelled",
                        {
                            "action_id": action_id,
                            "segment_ids": list(cancelled_ids),
                            "user_message_id": "",
                            "reason": reason,
                            "terminal_reason": terminal_reason,
                        },
                    ),
                    *self._release_trace_private_commitment(
                        action, reason=terminal_reason
                    ),
                ],
                idempotency_key=f"segment-remainder-cancel:{action_id}:{terminal_reason}",
                correlation_id=str(uuid4()),
                source="turn_coordinator",
                actor={"kind": "system"},
                causation_id=action_id,
            )
            return cancelled_ids

    def _delivered_turn_events(
        self, state: dict[str, object], message_id: str
    ) -> list[tuple[str, dict[str, object]]]:
        """Settle the originating turn and affinity only after real delivery."""
        if not message_id:
            return []
        raw_turn = _as_dict(state.get("turns", {}), "turns").get(message_id)
        if not isinstance(raw_turn, dict) or raw_turn.get("status") == "delivered":
            return []
        events: list[tuple[str, dict[str, object]]] = [
            (
                "TurnProcessingSettled",
                {
                    "message_id": message_id,
                    "status": "delivered",
                    "reason": "outgoing_action_delivered",
                },
            )
        ]
        user_id = str(raw_turn.get("user_id") or "")
        appraisal = str(raw_turn.get("appraisal") or "")
        if not user_id or not appraisal:
            return events
        current_affinity = _as_dict(
            _as_dict(state.get("long_term_affinity", {}), "long-term affinity").get(
                user_id, {}
            ),
            "user affinity",
        )
        outcome = settle_affinity_interaction(
            current_affinity,
            user_id=user_id,
            appraisal=appraisal,
            settlement_id=f"turn:{message_id}",
            logical_at=str(_as_dict(state["clock"], "clock").get("logical_at") or ""),
        )
        events.append(
            (
                "AffinityInteractionSettled",
                {
                    "user_id": user_id,
                    "message_id": message_id,
                    "settlement_id": f"turn:{message_id}",
                    "appraisal": appraisal,
                    "state": outcome.state,
                    "delta": outcome.delta,
                    "rule_version": outcome.rule_version,
                },
            )
        )
        return events

    def schedule_life_share_delivery(
        self, *, world_id: str, canonical_user_id: str, platform: str, expires_at: datetime, expected_revision: int
    ) -> LifeShareDelivery | None:
        """Atomically select one experience and create its outbox/action trace.

        Selection is not a separate mutable decision.  A restart therefore sees
        either no selection or a concrete action that can be delivered, cancelled,
        failed, or marked uncertain.
        """
        with self.store.connect() as conn:
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            uncertain_experiences: set[str] = set()
            for action_id, action in _as_dict(state["actions"], "actions").items():
                item = _as_dict(action, "action")
                trace = _as_dict(item.get("trace", {}), "action trace")
                if item.get("kind") == "outgoing_message" and trace.get("life_share") and item.get("status") in {"scheduled", "sending"}:
                    return LifeShareDelivery(str(trace["experience_id"]), int(item["delivery_id"]), int(item["trace_id"]), action_id, str(item["text"]), revision)
                if trace.get("life_share") and item.get("status") == "unknown":
                    uncertain_experiences.add(str(trace.get("experience_id") or ""))
            needs = _as_dict(state["needs"], "needs")
            day = str(_as_dict(state["clock"], "clock")["logical_at"])[:10]
            chronic = _as_dict(
                _as_dict(state.get("life_evolution", {}), "life evolution").get(
                    "chronic", {}
                ),
                "chronic life pressure",
            )
            share_willingness = float(chronic.get("share_willingness", 1.0))
            if (
                needs["initiative"] < 20
                or needs["security"] < 45
                or share_willingness < 0.4
                or day in _as_dict(state.get("share_days", {}), "share days")
            ):
                return None
            candidate = self._select_shareable_experience(state)
            if not candidate:
                return None
            experience_id, experience, share_score = candidate
            share_score = round(share_score * share_willingness)
            if experience_id in uncertain_experiences:
                return None
            text = f"{str(experience['content']).rstrip('。！？!? ')}。刚想起这件小事，想跟你说一下。"
            request_id = f"life-share:{day}:{experience_id}"
            request, projection, allowance = self.behavior_policy.outbound_allowance(
                state, request_id=request_id, message_kind="life_share",
                trigger="life_share", text=text, topic_key=experience_id,
            )
            policy_payload = _outbound_policy_payload(
                request=request, projection=projection, allowance=allowance, override=None,
            )
            if not allowance.allowed:
                self._append_and_project(
                    conn, world_id, revision, state,
                    [("OutboundActionRejected", policy_payload)],
                    idempotency_key=f"outbound-policy:rejected:{request_id}:{revision}",
                    correlation_id=str(uuid4()), source="outbound_policy",
                    actor={"kind": "companion"}, causation_id=experience_id,
                )
                return None
            now = utc_now().isoformat()
            delivery = conn.execute("insert into outbox_messages (canonical_user_id, platform, text, kind, status, created_at) values (?, ?, ?, 'life_event', 'planned', ?)", (canonical_user_id, platform, text, now))
            delivery_id = int(delivery.lastrowid)
            trace = {
                "world_id": world_id, "direction": "life_event", "appraisal": "life_event_share",
                "expression_policy": "只分享已提交的世界经历，不补写新事实。", "allowed_facts": [str(experience["content"])],
                "experience_id": experience_id, "life_share": True, "selection_id": f"life-share:{day}:{experience_id}", "share_score": share_score,
                "short_lived_constraint": None, "observable_reason": "一个已发生但尚未分享的世界经历。",
            }
            trace_row = conn.execute("""insert into turn_traces (canonical_user_id, direction, appraisal, expression_policy, allowed_facts_json, short_lived_constraint, observable_reason, output_text, delivery_id, status, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)""", (canonical_user_id, trace["direction"], trace["appraisal"], trace["expression_policy"], _stable_json(trace["allowed_facts"]), None, trace["observable_reason"], text, delivery_id, now, now))
            action_id = f"outgoing:{delivery_id}"
            decision = self._append_and_project(conn, world_id, revision, state, [
                ("OutboundActionAllowed", policy_payload),
                ("LifeShareSelected", {"experience_id": experience_id, "selection_id": trace["selection_id"], "score": share_score, "reason": "freshness_and_initiative"}),
                ("ActionScheduled", {"action_id": action_id, "kind": "outgoing_message", "message_kind": "life_event", "outbound_trigger": "life_share", "topic_key": experience_id, "expires_at": expires_at.isoformat(), "canonical_user_id": canonical_user_id, "platform": platform, "text": text, "trace": trace, "delivery_id": delivery_id, "trace_id": int(trace_row.lastrowid)}),
            ], idempotency_key=f"life-share-delivery:{delivery_id}", correlation_id=str(uuid4()), source="life_share", actor={"kind": "companion"}, causation_id=None)
            return LifeShareDelivery(experience_id, delivery_id, int(trace_row.lastrowid), action_id, text, decision.revision)

    @staticmethod
    def _select_shareable_experience(state: dict[str, object]) -> tuple[str, dict[str, object], int] | None:
        logical_at = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
        outcomes = _as_dict(state.get("outcomes", {}), "outcomes")
        candidates: list[tuple[int, str, dict[str, object]]] = []
        for experience_id, raw in _as_dict(state["experiences"], "experiences").items():
            experience = _as_dict(raw, "experience")
            if experience.get("shared"):
                continue
            outcome = _as_dict(outcomes.get(str(experience.get("source_outcome_id") or ""), {}), "outcome")
            occurred_at = outcome.get("ends_at")
            if not occurred_at:
                continue
            age_hours = max(0, int((logical_at - _parse_at(str(occurred_at))).total_seconds() // 3600))
            freshness = max(0, 168 - age_hours)
            candidates.append((freshness, experience_id, experience))
        if not candidates:
            return None
        score, experience_id, experience = max(candidates, key=lambda item: (item[0], item[1]))
        return experience_id, experience, score

    def begin_outgoing_action(
        self,
        delivery_id: int,
        *,
        expected_revision: int,
        lease_seconds: int = 300,
    ) -> bool:
        """Durably claim an outbox delivery before calling an unreliable adapter."""
        with self.store.connect() as conn:
            row = conn.execute("select status from outbox_messages where id = ?", (delivery_id,)).fetchone()
            if not row or row["status"] != "planned":
                return False
            action_row = conn.execute("select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?", (delivery_id,)).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            if not action_id or _as_dict(state["actions"], "actions")[action_id]["status"] != "scheduled":
                return False
            now = utc_now().isoformat()
            conn.execute("update outbox_messages set status = 'sending' where id = ? and status = 'planned'", (delivery_id,))
            conn.execute("update turn_traces set status = 'sending', updated_at = ? where delivery_id = ? and status = 'planned'", (now, delivery_id))
            if lease_seconds < 0:
                raise WorldError("delivery lease cannot be negative")
            lease_expires = (utc_now() + timedelta(seconds=lease_seconds)).isoformat()
            self._append_and_project(conn, world_id, revision, state, [("ActionAttempted", {"action_id": action_id}), ("ActionDispatchClaimed", {"action_id": action_id, "lease_expires_observed_at": lease_expires})], idempotency_key=f"begin:{delivery_id}", correlation_id=str(uuid4()), source="delivery", actor={"kind": "transport"}, causation_id=None)
            return True

    def mark_outgoing_unknown(self, delivery_id: int, *, reason: str, expected_revision: int) -> bool:
        """Close an interrupted send without risking an unprovable duplicate retry."""
        with self.store.connect() as conn:
            row = conn.execute("select status from outbox_messages where id = ?", (delivery_id,)).fetchone()
            if not row or row["status"] != "sending":
                return False
            action_row = conn.execute("select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?", (delivery_id,)).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action_id = self.action_id_for_delivery(world_id, delivery_id)
            now = utc_now().isoformat()
            conn.execute("update outbox_messages set status = 'unknown', failed_at = ?, failure_reason = ? where id = ?", (now, reason[:500], delivery_id))
            conn.execute("update turn_traces set status = 'unknown', failure_reason = ?, updated_at = ? where delivery_id = ?", (reason[:500], now, delivery_id))
            self._append_and_project(conn, world_id, revision, state, [("ActionDeliveryUncertain", {"action_id": action_id, "reason": reason})], idempotency_key=f"unknown:{delivery_id}", correlation_id=str(uuid4()), source="delivery_recovery", actor={"kind": "system"}, causation_id=None)
            return True

    def recover_interrupted_life_share_deliveries(self, world_id: str) -> int:
        """Mark process-interrupted life shares uncertain; never blindly resend them."""
        snapshot = self.snapshot(world_id)
        delivery_ids = [
            int(action["delivery_id"])
            for action in _as_dict(snapshot["actions"], "actions").values()
            if _as_dict(action, "action").get("status") == "sending"
            and _as_dict(_as_dict(action, "action").get("trace", {}), "action trace").get("life_share")
        ]
        return sum(self.mark_outgoing_unknown(item, reason="process restarted during adapter delivery", expected_revision=self.revision(world_id)) for item in delivery_ids)

    def recover_interrupted_outgoing_deliveries(
        self, world_id: str, *, observed_now: datetime | None = None
    ) -> int:
        """Close expired unqueryable claims without racing durable receipt recovery.

        A ``sending`` segment with a persisted ``receipt_lookup_token`` is not
        an abandoned fire-and-forget send: the reconstructed CompanionTurn can
        still ask its platform whether the accepted request was delivered.
        Leave that segment intact until the transport has made that query; only
        sends without a durable recovery handle become ``unknown`` here.
        """
        snapshot = self.snapshot(world_id)
        observed_now = observed_now or utc_now()
        expired: list[tuple[int, str | None]] = []
        for raw_action in _as_dict(snapshot["actions"], "actions").values():
            action = _as_dict(raw_action, "action")
            lease = str(action.get("lease_expires_observed_at") or "")
            if (
                action.get("kind") != "outgoing_message"
                or action.get("status") != "sending"
                or action.get("delivery_id") is None
                or not lease
                or _parse_at(lease) > observed_now
            ):
                continue
            sending_segment = next(
                (
                    _as_dict(item, "segment")
                    for item in _as_list(
                        _as_dict(action.get("segment_state", {}), "segment state").get(
                            "segments", []
                        ),
                        "segments",
                    )
                    if _as_dict(item, "segment").get("status") == "sending"
                ),
                None,
            )
            if sending_segment and str(sending_segment.get("receipt_lookup_token") or ""):
                continue
            expired.append(
                (
                    int(action["delivery_id"]),
                    str(sending_segment.get("segment_id") or "") if sending_segment else None,
                )
            )
        recovered = 0
        for delivery_id, segment_id in expired:
            if segment_id:
                recovered += int(
                    self.mark_outgoing_segment_unknown(
                        delivery_id,
                        segment_id,
                        reason="delivery lease expired after process interruption",
                        expected_revision=self.revision(world_id),
                    )
                )
            else:
                recovered += int(
                    self.mark_outgoing_unknown(
                        delivery_id,
                        reason="delivery lease expired after process interruption",
                        expected_revision=self.revision(world_id),
                    )
                )
        return recovered

    def cancel_life_share_delivery(self, world_id: str, action_id: str, *, reason: str, expected_revision: int) -> bool:
        """Cancel a still-planned share and its outbox record in one transaction."""
        with self.store.connect() as conn:
            revision, state = self._load_state(conn, world_id)
            self._check_revision(revision, expected_revision)
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            trace = _as_dict(action.get("trace", {}), "action trace")
            if action.get("status") != "scheduled" or not trace.get("life_share"):
                return False
            delivery_id = int(action["delivery_id"])
            now = utc_now().isoformat()
            conn.execute("update outbox_messages set status = 'cancelled', failed_at = ?, failure_reason = ? where id = ? and status = 'planned'", (now, reason[:500], delivery_id))
            conn.execute("update turn_traces set status = 'cancelled', failure_reason = ?, updated_at = ? where delivery_id = ? and status = 'planned'", (reason[:500], now, delivery_id))
            self._append_and_project(conn, world_id, revision, state, [("ActionCancelled", {"action_id": action_id, "reason": reason})], idempotency_key=f"cancel-life-share:{action_id}", correlation_id=str(uuid4()), source="life_share", actor={"kind": "companion"}, causation_id=None)
            return True

    def settle_outgoing_action(
        self, delivery_id: int, *, delivered: bool, reason: str | None = None,
        external_receipt: str | None = None,
        expected_revision: int | None = None,
        reconciliation_evidence: dict[str, object] | None = None,
    ) -> dict[str, object] | None:
        """Atomically settle transport history, turn trace, and its world action."""
        with self.store.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, platform, text, kind, status from outbox_messages where id = ?",
                (delivery_id,),
            ).fetchone()
            if not row:
                return None
            result = dict(row)
            if row["status"] not in {"planned", "sending", "unknown"}:
                requested_status = "delivered" if delivered else "failed"
                if reconciliation_evidence and row["status"] != requested_status:
                    raise ConcurrencyConflict(
                        f"delivery {delivery_id} was already reconciled as {row['status']}"
                    )
                return result
            if row["status"] == "unknown" and not external_receipt:
                raise WorldError("unknown delivery needs an external receipt before reconciliation")
            action_row = conn.execute(
                "select world_id from world_actions where json_extract(state_json, '$.delivery_id') = ?",
                (delivery_id,),
            ).fetchone()
            if not action_row:
                raise WorldError(f"outbox delivery {delivery_id} has no world action")
            world_id = str(action_row["world_id"])
            revision, state = self._load_state(conn, world_id)
            if expected_revision is not None:
                self._check_revision(revision, expected_revision)
            action_id = next(
                (
                    candidate_id
                    for candidate_id, candidate in _as_dict(state["actions"], "actions").items()
                    if _as_dict(candidate, "action").get("delivery_id") == delivery_id
                ),
                None,
            )
            if not action_id:
                raise WorldError(f"world action for delivery {delivery_id} is missing")
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            segment_state = action.get("segment_state")
            segments = (
                _as_list(_as_dict(segment_state, "segment state").get("segments", []), "segments")
                if isinstance(segment_state, dict)
                else []
            )
            segments_to_deliver: list[dict[str, object]] = []
            if delivered and segments:
                statuses = {
                    str(_as_dict(item, "segment").get("status") or "")
                    for item in segments
                }
                if reconciliation_evidence and "planned" in statuses and statuses != {"planned"}:
                    raise WorldError(
                        "partially dispatched segmented delivery needs segment-level reconciliation"
                    )
                segments_to_deliver = [
                    _as_dict(item, "segment")
                    for item in segments
                    if str(_as_dict(item, "segment").get("status") or "")
                    != "delivered"
                ]
            now = utc_now().isoformat()
            if delivered:
                conn.execute(
                    "update outbox_messages set status = 'delivered', delivered_at = ? where id = ?",
                    (now, delivery_id),
                )
                delivered_texts = (
                    [str(item.get("text") or "") for item in segments_to_deliver]
                    if segments
                    else [str(row["text"])]
                )
                for delivered_text in delivered_texts:
                    conn.execute(
                        """
                        insert into messages (
                          canonical_user_id, platform, platform_user_id, channel_id, message_id,
                          direction, text, attachments_json, sent_at
                        ) values (?, ?, '', null, null, 'out', ?, '[]', ?)
                        """,
                        (row["canonical_user_id"], row["platform"], delivered_text, now),
                    )
            else:
                conn.execute(
                    "update outbox_messages set status = 'failed', failed_at = ?, failure_reason = ? where id = ?",
                    (now, (reason or "delivery failed")[:500], delivery_id),
                )
            conn.execute(
                """
                update turn_traces set status = ?, failure_reason = ?, updated_at = ?
                where delivery_id = ? and status in ('planned', 'sending', 'unknown')
                """,
                ("delivered" if delivered else "failed", None if delivered else (reason or "delivery failed")[:500], now, delivery_id),
            )
            trace = _as_dict(action.get("trace", {}), "action trace")
            specifications: list[tuple[str, dict[str, object]]] = []
            if delivered and segments:
                for segment in segments_to_deliver:
                    if segment.get("status") == "planned":
                        specifications.append(
                            (
                                "ActionSegmentDispatchClaimed",
                                {
                                    "action_id": action_id,
                                    "segment_id": str(segment["segment_id"]),
                                    "position": int(segment["position"]),
                                },
                            )
                        )
                    specifications.append(
                        (
                            "ActionSegmentSettled",
                            {
                                "action_id": action_id,
                                "segment_id": str(segment["segment_id"]),
                                "position": int(segment["position"]),
                                "result": {
                                    "kind": "delivery",
                                    "status": "delivered",
                                    "external_receipt": external_receipt,
                                },
                            },
                        )
                    )
            elif not delivered and segments:
                unresolved_segment_ids = [
                    str(_as_dict(item, "segment")["segment_id"])
                    for item in segments
                    if _as_dict(item, "segment").get("status") != "delivered"
                ]
                if unresolved_segment_ids:
                    specifications.append(
                        (
                            "ActionSegmentsCancelled",
                            {
                                "action_id": action_id,
                                "segment_ids": unresolved_segment_ids,
                                "user_message_id": "delivery-failed",
                                "reason": reason or "delivery failed",
                            },
                        )
                    )
            specifications.extend([
                ("ActionAttempted", {"action_id": action_id}),
                (
                    "ActionSettled",
                    {
                        "action_id": action_id,
                        "result": {
                            "kind": "delivery",
                            "status": "delivered" if delivered else "failed",
                            "reason": reason,
                            "external_receipt": external_receipt,
                            "segmented": bool(segments),
                            **(
                                {"reconciliation_evidence": reconciliation_evidence}
                                if reconciliation_evidence
                                else {}
                            ),
                        },
                    },
                ),
            ])
            if delivered:
                specifications.extend(
                    self._delivered_turn_events(
                        state, str(trace.get("input_message_id") or "")
                    )
                )
            if delivered and trace.get("life_share"):
                specifications.append(("ExperienceShared", {"experience_id": trace.get("experience_id"), "action_id": action_id}))
            thread = trace.get("conversation_thread")
            if delivered and isinstance(thread, dict):
                specifications.append((
                    "ConversationThreadOpened",
                    _conversation_thread_event_payload(
                        thread,
                        source_action_id=action_id,
                        logical_at=_parse_at(
                            str(_as_dict(state["clock"], "clock")["logical_at"])
                        ),
                    ),
                ))
            private_commitment = trace.get("private_commitment")
            if not delivered and isinstance(private_commitment, dict):
                commitment_id = str(private_commitment.get("commitment_id") or "")
                if commitment_id:
                    specifications.append(
                        (
                            "PrivateCommitmentResolved",
                            {
                                "commitment_id": commitment_id,
                                "outcome": "released",
                                "reason": "question_action_not_delivered",
                            },
                        )
                    )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                specifications,
                idempotency_key=f"settle:{delivery_id}:{'delivered' if delivered else 'failed'}",
                correlation_id=str(uuid4()),
                source="delivery_reconciliation" if reconciliation_evidence else "delivery",
                actor={
                    "kind": "operator",
                    "id": str(reconciliation_evidence.get("reviewer_id") or ""),
                }
                if reconciliation_evidence
                else {"kind": "transport"},
                causation_id=None,
            )
            return result

    def advance(
        self, world_id: str, target_logical_time: datetime, *, expected_revision: int
    ) -> WorldDecision:
        command = {
            "type": "advance_clock",
            "world_id": world_id,
            "target_logical_at": target_logical_time.isoformat(),
            "idempotency_key": f"clock:{world_id}:{target_logical_time.isoformat()}",
        }
        return self.submit(command, expected_revision=expected_revision)

    def record_external_result(
        self,
        action_id: str,
        result: dict[str, object],
        *,
        expected_revision: int,
        world_id: str | None = None,
    ) -> WorldDecision:
        if world_id is None:
            world_id = self._world_for_action(action_id)
        canonical = _stable_json(result)
        return self.submit(
            {
                "type": "record_external_result",
                "world_id": world_id,
                "action_id": action_id,
                "result": result,
                "idempotency_key": f"external:{action_id}:{_hash(canonical)}",
            },
            expected_revision=expected_revision,
        )

    def propose_tool_action(
        self,
        *,
        world_id: str,
        proposal_id: str,
        user_id: str,
        tool_name: str,
        arguments: dict[str, object],
        summary: str,
        risk: str,
        expected_revision: int,
    ) -> WorldDecision:
        """Record a reality-affecting proposal without creating an executable Action."""
        return self.submit(
            {
                "type": "propose_tool_action",
                "world_id": world_id,
                "proposal_id": proposal_id,
                "user_id": user_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "summary": summary,
                "risk": risk,
                "idempotency_key": f"tool-proposal:{proposal_id}",
            },
            expected_revision=expected_revision,
        )

    def authorize_tool_action(
        self,
        *,
        world_id: str,
        proposal_id: str,
        confirmation_message_id: str,
        expected_revision: int,
    ) -> WorldDecision:
        """Accept an explicit confirmation; policy-blocked proposals stay rejected."""
        return self.submit(
            {
                "type": "authorize_tool_action",
                "world_id": world_id,
                "proposal_id": proposal_id,
                "confirmation_message_id": confirmation_message_id,
                "idempotency_key": f"tool-authorization:{proposal_id}",
            },
            expected_revision=expected_revision,
        )

    def reject_tool_action(
        self,
        *,
        world_id: str,
        proposal_id: str,
        confirmation_message_id: str,
        reason: str,
        expected_revision: int,
    ) -> WorldDecision:
        return self.submit(
            {
                "type": "reject_tool_action",
                "world_id": world_id,
                "proposal_id": proposal_id,
                "confirmation_message_id": confirmation_message_id,
                "reason": reason,
                "idempotency_key": f"tool-rejection:{proposal_id}",
            },
            expected_revision=expected_revision,
        )

    def execute_fake_tool_action(
        self,
        *,
        world_id: str,
        proposal_id: str,
        adapter: FakeToolAdapter,
        expected_revision: int,
    ) -> WorldDecision:
        """Run an authorized proposal through the deliberately effect-free adapter."""
        tool = _as_dict(
            _as_dict(self.snapshot(world_id).get("tool_actions", {}), "tool actions").get(
                proposal_id
            ),
            "tool action",
        )
        action_id = str(tool.get("action_id") or "")
        if tool.get("status") != "authorized" or not action_id:
            raise WorldError("only an authorized tool proposal can execute")
        claimed = self.submit(
            {
                "type": "claim_external_action",
                "world_id": world_id,
                "action_id": action_id,
                "lease_expires_observed_at": (utc_now() + timedelta(minutes=2)).isoformat(),
                "idempotency_key": f"tool-claim:{proposal_id}",
            },
            expected_revision=expected_revision,
        )
        result = adapter.execute(
            ToolExecutionRequest(
                action_id=action_id,
                proposal_id=proposal_id,
                tool_name=str(tool["tool_name"]),
                arguments=_as_dict(tool.get("arguments", {}), "tool arguments"),
            )
        )
        return self.record_external_result(
            action_id,
            result.to_world_result(),
            world_id=world_id,
            expected_revision=claimed.revision,
        )

    def recover_expired_external_leases(
        self,
        world_id: str,
        *,
        observed_now: datetime,
        expected_revision: int,
    ) -> WorldDecision:
        """Settle external work abandoned by a crashed process.

        The deadline is observed wall time recorded in the ledger. Logical time
        deliberately has no authority over an in-flight external call.
        """
        return self.submit(
            {
                "type": "recover_expired_external_leases",
                "world_id": world_id,
                "observed_now": observed_now.isoformat(),
                "idempotency_key": f"external-lease-recovery:{world_id}:{observed_now.isoformat()}",
            },
            expected_revision=expected_revision,
        )

    def rebuild_projection(self, world_id: str, projection_name: str) -> ProjectionReport:
        projection_names = {
            "world_current_state", "world_entities", "world_agenda",
            "world_actions", "world_experiences", "world_fact_index",
        }
        if projection_name not in projection_names:
            raise WorldError(f"unsupported projection: {projection_name}")
        with self.store.connect() as conn:
            events = self._load_events(conn, world_id)
            state = reduce_events(events)
            revision = events[-1].revision if events else 0
            state_hash = _state_hash(state)
            if projection_name == "world_current_state":
                live = conn.execute(
                    "select state_hash from world_current_state where world_id = ?", (world_id,)
                ).fetchone()
            else:
                live = conn.execute(
                    "select state_hash from world_projection_checkpoints where world_id = ? and projection_name = ?",
                    (world_id, projection_name),
                ).fetchone()
            matches_live = bool(live and live["state_hash"] == state_hash)
            self._write_projection(conn, world_id, revision, state)
            now = utc_now().isoformat()
            conn.execute(
                """
                insert or replace into world_projection_hashes
                  (world_id, projection_name, applied_revision, state_hash, checked_at)
                values (?, ?, ?, ?, ?)
                """,
                (world_id, projection_name, revision, state_hash, now),
            )
        return ProjectionReport(world_id, projection_name, revision, len(events), state_hash, matches_live)

    def audit_enablement(self, world_id: str, *, delivery_receipts_supported: bool) -> WorldEnablementReport:
        """Rebuild every read model and state whether real chat may safely enable."""
        reports = tuple(
            self.rebuild_projection(world_id, projection)
            for projection in ("world_current_state", "world_entities", "world_agenda", "world_actions", "world_experiences", "world_fact_index")
        )
        actions = _as_dict(self.snapshot(world_id)["actions"], "actions")
        state = self.snapshot(world_id)
        open_actions = tuple(sorted(action_id for action_id, action in actions.items() if _as_dict(action, "action").get("status") in {"scheduled", "sending"}))
        unknown_actions = tuple(sorted(action_id for action_id, action in actions.items() if _as_dict(action, "action").get("status") == "unknown"))
        invariant_errors: list[str] = []
        outcomes = _as_dict(state.get("outcomes", {}), "outcomes")
        for activity_id, raw in _as_dict(state.get("agenda", {}), "agenda").items():
            activity = _as_dict(raw, "activity")
            if (
                activity.get("status") == "completed"
                and activity.get("template_id")
                and f"outcome:{activity_id}" not in outcomes
            ):
                invariant_errors.append(f"completed_activity_without_outcome:{activity_id}")
        delayed_by_message: dict[str, list[str]] = {}
        for action_id, raw in actions.items():
            action = _as_dict(raw, "action")
            if action.get("status") not in {"scheduled", "sending"} or action.get("kind") not in {"reply_later", "message_attention"}:
                continue
            payload = _as_dict(action.get("payload", {}), "action payload")
            message_id = str(payload.get("message_id") or _as_dict(payload.get("message", {}), "deferred message").get("message_id") or "")
            if message_id:
                delayed_by_message.setdefault(message_id, []).append(str(action_id))
        invariant_errors.extend(
            f"duplicate_deferred_actions:{message_id}:{','.join(sorted(action_ids))}"
            for message_id, action_ids in delayed_by_message.items()
            if len(action_ids) > 1
        )
        return WorldEnablementReport(
            world_id=world_id,
            ready=(
                all(report.matches_live for report in reports)
                and not open_actions
                and (not unknown_actions or delivery_receipts_supported)
                and not invariant_errors
            ),
            projection_reports=reports,
            open_action_ids=open_actions,
            unknown_action_ids=unknown_actions,
            delivery_receipts_supported=delivery_receipts_supported,
            invariant_errors=tuple(sorted(invariant_errors)),
        )

    def snapshot(self, world_id: str) -> dict[str, object]:
        with self.store.connect() as conn:
            _, state = self._load_state(conn, world_id)
        return state

    def expression_plan(
        self,
        world_id: str,
        *,
        user_id: str,
        purpose: str,
        intent_id: str = "",
        expected_revision: int | None = None,
    ) -> ExpressionPlan:
        """Compile a recipient- and revision-bound Display Strategy on demand."""
        with self.store.connect() as conn:
            revision, state = self._load_state(conn, world_id)
        if expected_revision is not None and revision != expected_revision:
            raise WorldError(
                f"expression plan revision mismatch: expected {expected_revision}, got {revision}"
            )
        return self._expression_plan_from_state(
            state,
            revision=revision,
            user_id=user_id,
            purpose=purpose,
            intent_id=intent_id,
        )

    @staticmethod
    def _expression_plan_from_state(
        state: dict[str, object],
        *,
        revision: int,
        user_id: str,
        purpose: str,
        intent_id: str,
    ) -> ExpressionPlan:
        relationships = _as_dict(state.get("relationships", {}), "relationships")
        relationship = _as_dict(
            relationships.get(user_id, {}), "expression relationship"
        )
        raw_appraisal = state.get("last_appraisal")
        appraisal = (
            _as_dict(raw_appraisal, "last appraisal")
            if isinstance(raw_appraisal, dict)
            else {}
        )
        return compile_expression_plan(
            _as_dict(state.get("emotion_modulation", {}), "emotion modulation"),
            relationship,
            _as_dict(state.get("needs", {}), "needs"),
            current_appraisal=str(appraisal.get("appraisal") or "ordinary_message"),
            revision=revision,
            user_id=user_id,
            intent_id=f"{purpose}:{intent_id}" if intent_id else purpose,
        )

    def turn_projection(
        self,
        world_id: str,
        *,
        user_id: str,
        text: str,
        current_message_id: str | None,
        purpose: str = "reply",
        intent_id: str = "",
    ) -> WorldTurnProjection:
        """Read every prompt-facing world view from one ledger revision."""
        with self.store.connect() as conn:
            conn.execute("begin")
            revision, state = self._load_state(conn, world_id)
        return WorldTurnProjection(
            world_id=world_id,
            revision=revision,
            state_hash=_state_hash(state),
            state=state,
            conversation_context=self._conversation_context_from_state(
                state, world_id=world_id, user_id=user_id
            ),
            retrieved_sources=tuple(
                self._conversation_sources_for_query_from_state(
                    state,
                    user_id=user_id,
                    text=text,
                    current_message_id=current_message_id,
                    limit=4,
                )
            ),
            expression_plan=self._expression_plan_from_state(
                state,
                revision=revision,
                user_id=user_id,
                purpose=purpose,
                intent_id=intent_id,
            ),
        )

    def dashboard_overview(self, world_id: str) -> dict[str, object]:
        """Return the bounded, read-only view required by the world console.

        This is deliberately a single read interface: browser code never needs
        to infer facts from event payloads, nor can it treat a visual preference
        as world state.  The full ledger remains available through the audit
        export endpoint when an operator needs forensic detail.
        """
        with self.store.connect() as conn:
            # A console command must be planned from one coherent ledger
            # revision.  Holding this read transaction also makes the returned
            # state hash meaningful to an operator inspecting a busy daemon.
            conn.execute("begin")
            revision, state = self._load_state(conn, world_id)
            events = self._load_events(conn, world_id)
        agenda = [_as_dict(item, "agenda item") for item in _as_dict(state["agenda"], "agenda").values()]
        unresolved = [item for item in agenda if str(item.get("status") or "") in {"active", "planned", "deferred"}]
        historical = [item for item in agenda if item not in unresolved]
        unresolved.sort(key=lambda item: (_activity_console_rank(str(item.get("status") or "")), str(item.get("starts_at") or ""), str(item.get("activity_id") or "")))
        historical.sort(key=lambda item: (str(item.get("ends_at") or item.get("starts_at") or ""), str(item.get("activity_id") or "")), reverse=True)
        actions = [_as_dict(item, "action") for item in _as_dict(state["actions"], "actions").values()]
        actions.sort(key=lambda item: (_action_console_rank(str(item.get("status") or "")), str(item.get("expires_at") or ""), str(item.get("action_id") or "")))
        goals = [_as_dict(item, "goal") for item in _as_dict(state.get("goals", {}), "goals").values()]
        goals.sort(key=lambda item: (str(item.get("status") or ""), str(item.get("deadline") or ""), str(item.get("id") or "")))
        outcomes = _as_dict(state.get("outcomes", {}), "outcomes")
        experiences: list[dict[str, object]] = []
        for experience_id, raw in _as_dict(state["experiences"], "experiences").items():
            experience = _as_dict(raw, "experience")
            outcome = _as_dict(outcomes.get(str(experience.get("source_outcome_id") or ""), {}), "outcome")
            experiences.append({
                "experience_id": experience_id,
                "content": str(experience.get("content") or ""),
                "occurred_at": str(outcome.get("ends_at") or ""),
                "shared": bool(experience.get("shared")),
            })
        experiences.sort(key=lambda item: (str(item["occurred_at"]), str(item["experience_id"])), reverse=True)
        return {
            "world_id": world_id,
            "revision": revision,
            "state_hash": _state_hash(state),
            "clock": dict(_as_dict(state["clock"], "clock")),
            "protagonist": dict(_as_dict(_as_dict(state["entities"], "entities").get("zhizhi", {}), "protagonist")),
            "needs": dict(_as_dict(state["needs"], "needs")),
            "goals": [_console_goal(item) for item in goals],
            # A bounded dashboard must retain what still constrains behavior;
            # completed history fills only the remaining slots.
            "agenda": [_console_activity(item) for item in (unresolved + historical)[:12]],
            "actions": [_console_action(item) for item in actions[:12]],
            "experiences": experiences[:10],
            "timeline": [_console_event(event) for event in events[-24:]][::-1],
        }

    def daemon_dashboard_projection(
        self, world_id: str, *, past_days: int = 15, future_days: int = 15
    ) -> dict[str, object]:
        """Project the world into the legacy dashboard's read contract.

        This is a compatibility projection, not a second state machine.  It
        lets the visual home retain its renderer while all displayed facts come
        from the same ledger as dialogue and the operator console.
        """
        overview = self.dashboard_overview(world_id)
        state = self.snapshot(world_id)
        clock = _as_dict(state["clock"], "clock")
        logical_at = _parse_at(str(clock["logical_at"]))
        agenda = [_as_dict(item, "agenda item") for item in _as_dict(state["agenda"], "agenda").values()]
        active = next((item for item in agenda if item.get("status") == "active"), None)
        current = active or next(
            (item for item in sorted(agenda, key=lambda value: str(value.get("starts_at") or "")) if item.get("status") in {"planned", "deferred"}),
            None,
        )
        scene = _world_scene_projection(state, current)
        communication = _as_dict(state["communication"], "communication")
        actions = [_as_dict(item, "action") for item in _as_dict(state["actions"], "actions").values()]
        open_actions = [item for item in actions if item.get("status") in {"scheduled", "sending", "unknown"}]
        user_relationship_stage = "stranger"
        entities = _as_dict(state["entities"], "entities")
        relationships = _as_dict(state["relationships"], "relationships")
        for entity_id, entity in entities.items():
            if _as_dict(entity, "entity").get("kind") == "user":
                user_relationship_stage = str(
                    _as_dict(relationships.get(entity_id, {}), "user relationship").get(
                        "stage", "stranger"
                    )
                )
                break
        days: list[dict[str, object]] = []
        # The calendar is a read projection over the complete committed
        # experience set, not an implicit "last event" cache.
        experiences = self._committed_experiences(state)
        for offset in range(-past_days, future_days + 1):
            day = (logical_at + timedelta(days=offset)).date().isoformat()
            day_agenda = [item for item in agenda if str(item.get("starts_at") or "")[:10] == day]
            day_experiences = [item for item in experiences if str(item.get("occurred_at") or "")[:10] == day]
            days.append({
                "date": day,
                "relative": "今天" if offset == 0 else ("昨天" if offset == -1 else ("明天" if offset == 1 else "")),
                "plans": [_dashboard_activity(item) for item in day_agenda],
                "events": [
                    {"starts_at": item["occurred_at"], "content": item["content"], "status": "completed"}
                    for item in day_experiences
                ],
                "special_events": [],
            })
        activity = str(current.get("title") if current else "空档")
        starts_at = str(current.get("starts_at") if current else logical_at.isoformat())
        ends_at = str(current.get("ends_at") if current else logical_at.isoformat())
        phone_label = _communication_phone_label(str(communication.get("attention") or "idle"), str(communication.get("typing") or "idle"))
        return {
            "state": {
                "world_id": world_id, "revision": overview["revision"], "state_hash": overview["state_hash"],
                "needs": overview["needs"], "communication": dict(communication),
                "emotion_modulation": dict(_as_dict(state["emotion_modulation"], "emotion modulation")),
                "relationship_stage": user_relationship_stage,
            },
            "life_runtime": {"activity": activity, "started_at": starts_at, "ends_at": ends_at, "phone_attention": communication.get("attention")},
            "calendar": {"days": days},
            "recent_social_tasks": [
                {"status": item["status"], "reason": item.get("reason") or item.get("kind"), "due_at": _as_dict(item.get("payload", {}), "action payload").get("due_at") or item.get("expires_at")}
                for item in open_actions
            ],
            "dashboard": {
                "mood_label": _world_mood_label(_as_dict(state["emotion_modulation"], "emotion modulation")),
                "phone_label": phone_label,
                "attention": int(_as_dict(state["needs"], "needs").get("attention", 0)),
                "activity": activity,
                "reasons": [str(scene["observable_reason"]), phone_label],
                "next_plan": [_dashboard_activity(item) for item in sorted(agenda, key=lambda value: str(value.get("starts_at") or "")) if item.get("status") in {"active", "planned", "deferred"}][:6],
                "active_task_count": len(open_actions),
                "relationship_stage": user_relationship_stage,
                "scene": scene,
            },
            "world_overview": overview,
        }

    def experiences_for_time_reference(self, world_id: str, reference: str) -> list[dict[str, object]]:
        """Return only committed experiences in a deterministic logical-time range."""
        state = self.snapshot(world_id)
        logical_at = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
        normalized = reference.strip().lower()
        if normalized in {"today", "今天"}:
            day = logical_at.date().isoformat()
        elif normalized in {"yesterday", "昨天"}:
            day = (logical_at - timedelta(days=1)).date().isoformat()
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            day = normalized
        elif normalized in {"last", "上次"}:
            day = ""
        else:
            raise WorldError("time reference must be today, yesterday, last, or YYYY-MM-DD")
        records = self._committed_experiences(state)
        if day:
            records = [item for item in records if str(item["occurred_at"])[:10] == day]
        return records[-1:] if normalized in {"last", "上次"} else records

    @staticmethod
    def _committed_experiences(state: dict[str, object]) -> list[dict[str, object]]:
        """Return every referencable experience in logical-time order."""
        outcomes = _as_dict(state.get("outcomes", {}), "outcomes")
        records: list[dict[str, object]] = []
        for experience_id, experience in _as_dict(state["experiences"], "experiences").items():
            item = _as_dict(experience, "experience")
            outcome = _as_dict(outcomes.get(str(item.get("source_outcome_id") or ""), {}), "outcome")
            occurred_at = str(outcome.get("ends_at") or "")
            if occurred_at:
                records.append({
                    "experience_id": experience_id,
                    "content": item["content"],
                    "occurred_at": occurred_at,
                    "shared": bool(item.get("shared")),
                    "source": f"world_event:ExperienceCommitted:{experience_id}",
                    "subject": str(item.get("entity_id") or "zhizhi"),
                    "purpose": "continuity",
                    "pinned": bool(item.get("pinned", False)),
                    "importance": int(item.get("importance") or 50),
                })
        records.sort(key=lambda item: str(item["occurred_at"]))
        return records

    def conversation_policy(self, world_id: str) -> dict[str, object]:
        """Expose behavior-only world state; never fabricate a conversational fact."""
        state = self.snapshot(world_id)
        return self._conversation_policy_from_state(state)

    @staticmethod
    def _conversation_policy_from_state(state: dict[str, object]) -> dict[str, object]:
        active = [item for item in _as_dict(state["agenda"], "agenda").values() if _as_dict(item, "activity").get("status") == "active"]
        logical_at = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
        urgent = [goal_id for goal_id, goal in _as_dict(state.get("goals", {}), "goals").items() if goal.get("status") == "active" and goal.get("deadline") and _parse_at(str(goal["deadline"])) - logical_at <= timedelta(hours=48)]
        if active:
            return {"mode": "busy", "reply_length": "brief", "initiative": "hold", "reason": "active_world_activity"}
        if urgent:
            return {"mode": "goal_urgent", "reply_length": "normal", "initiative": "low", "reason": "goal_deadline_near", "goal_ids": sorted(urgent)}
        return {"mode": "available", "reply_length": "normal", "initiative": "normal", "reason": "no_active_world_constraint"}

    def conversation_context(self, world_id: str, *, user_id: str) -> dict[str, object]:
        """Build the sole bounded read model used to authorize a world turn.

        This replaces the old concatenation of mood rows, self-core memories,
        calendar rows, and life-runtime prose.  It deliberately distinguishes
        referencable facts from private behaviour constraints so callers cannot
        accidentally turn a current plan into a claimed experience.
        """
        return self._conversation_context_from_state(
            self.snapshot(world_id), world_id=world_id, user_id=user_id
        )

    def _conversation_context_from_state(
        self, state: dict[str, object], *, world_id: str, user_id: str
    ) -> dict[str, object]:
        entities = _as_dict(state["entities"], "entities")
        protagonist = _as_dict(entities.get("zhizhi"), "protagonist")
        agenda = _as_dict(state["agenda"], "agenda")
        active = next(
            (
                _as_dict(item, "activity")
                for item in agenda.values()
                if _as_dict(item, "activity").get("status") == "active"
            ),
            None,
        )
        relationship = dict(_as_dict(_as_dict(state["relationships"], "relationships").get(user_id, {}), "user relationship"))
        user_facts = [
            item
            for item in _as_dict(state["facts"], "facts").values()
            if str(_as_dict(item, "fact").get("subject") or "") == user_id
            and _fact_is_current(item)
        ]
        goals = _as_dict(state.get("goals", {}), "goals")
        recent_conversation: list[dict[str, str]] = []
        referencable_conversation: list[dict[str, str]] = []
        for raw in _as_list(state.get("recent_messages", []), "recent messages"):
            item = _as_dict(raw, "recent message")
            direction = str(item.get("direction") or "")
            item_user_id = str(item.get("user_id") or "")
            if item_user_id and item_user_id != user_id:
                continue
            message_id = str(item.get("message_id") or "")
            text = str(item.get("text") or "").strip()
            if direction not in {"in", "out"} or not message_id or not text:
                continue
            transcript_item = {
                "source_id": f"message:{message_id}",
                "source": (
                    f"world_event:UserMessageObserved:{message_id}"
                    if direction == "in"
                    else f"action:{str(item.get('source_action_id') or message_id)}"
                ),
                "source_type": (
                    "user_message" if direction == "in" else "delivered_companion_message"
                ),
                "subject": user_id if direction == "in" else "zhizhi",
                "speaker": "user" if direction == "in" else "companion",
                "content": text,
                "logical_at": str(item.get("logical_at") or item.get("sent_at") or ""),
                "purpose": "conversation_continuity",
            }
            recent_conversation.append(transcript_item)
            if direction == "in":
                referencable_conversation.append(
                    {
                        **transcript_item,
                        "sent_at": str(item.get("sent_at") or ""),
                        "reference_state": "observed",
                    }
                )
        current_scene, current_scene_source = self._current_scene_source(state)
        open_threads = [
            {
                "thread_id": str(thread_id), "question": str(item.get("question") or ""),
                "expires_at": str(item.get("expires_at") or ""),
            }
            for thread_id, raw in _as_dict(state.get("conversation_threads", {}), "conversation threads").items()
            if (item := _as_dict(raw, "conversation thread")).get("status") == "open" and item.get("user_id") == user_id
        ]
        attachment_insights: list[dict[str, object]] = []
        for action_id, raw_action in _as_dict(state["actions"], "actions").items():
            action = _as_dict(raw_action, "action")
            if action.get("kind") != "attachment_analysis" or action.get("status") != "delivered":
                continue
            action_payload = _as_dict(action.get("payload", {}), "attachment analysis payload")
            if str(action_payload.get("user_id") or "") != user_id:
                continue
            result = _as_dict(action.get("result", {}), "attachment analysis result")
            attachment_insights.append({
                "source_id": str(action_id),
                "source_type": "attachment_analysis",
                "reference_state": "delivered",
                "source_message_id": str(result.get("source_message_id") or ""),
                "attachment_index": int(result.get("attachment_index") or 0),
                "kind": str(result.get("kind") or "unknown"),
                "summary": str(result.get("summary") or ""),
                "confidence": float(result.get("confidence") or 0.0),
            })
        return {
            "referencable_facts": [
                {
                    "source_id": str(fact_id),
                    "source": str(_as_dict(item, "fact").get("source") or fact_id),
                    "source_type": "fact",
                    "fact_id": str(fact_id),
                    "subject": str(_as_dict(item, "fact").get("subject") or "world"),
                    "logical_at": str(_as_dict(item, "fact").get("valid_from") or ""),
                    "purpose": "grounding",
                    "value": str(_as_dict(item, "fact").get("value") or ""),
                    "reference_state": "confirmed",
                    "status": str(_as_dict(item, "fact").get("status") or "current"),
                    "conflict_key": str(_as_dict(item, "fact").get("conflict_key") or ""),
                    "pinned": bool(_as_dict(item, "fact").get("pinned", False)),
                    "importance": int(_as_dict(item, "fact").get("importance") or 50),
                }
                for fact_id, item in _as_dict(state["facts"], "facts").items()
                if str(_as_dict(item, "fact").get("subject") or "")
                in {user_id, "world", "zhizhi"}
                and _fact_is_current(item)
            ],
            "referencable_experiences": [
                {
                    "source_id": str(item["experience_id"]),
                    "source_type": "experience",
                    "reference_state": "committed",
                    **item,
                }
                for item in self._committed_experiences(state)
            ],
            "recent_conversation": recent_conversation[-12:],
            "referencable_conversation": referencable_conversation[-8:],
            "referencable_attachment_insights": attachment_insights[-8:],
            "user_profile": [
                {
                    "source_id": str(_as_dict(item, "fact").get("fact_id") or ""),
                    "source": str(_as_dict(item, "fact").get("source") or "world-ledger"),
                    "source_type": "fact",
                    "subject": str(_as_dict(item, "fact").get("subject") or user_id),
                    "logical_at": str(_as_dict(item, "fact").get("valid_from") or ""),
                    "purpose": "personalize",
                    "value": str(_as_dict(item, "fact").get("value") or ""),
                    "reference_state": "confirmed",
                    "status": str(_as_dict(item, "fact").get("status") or "current"),
                    "conflict_key": str(_as_dict(item, "fact").get("conflict_key") or ""),
                    "pinned": bool(_as_dict(item, "fact").get("pinned", False)),
                    "importance": int(_as_dict(item, "fact").get("importance") or 50),
                }
                for item in user_facts[-8:]
            ],
            "current_scene": current_scene,
            "current_scene_source": current_scene_source,
            "behavior": {
                "policy": self._conversation_policy_from_state(state),
                "needs": dict(_as_dict(state["needs"], "needs")),
                "relationship": relationship,
                "emotion_modulation": dict(_as_dict(state["emotion_modulation"], "emotion modulation")),
                "open_threads": open_threads,
            },
            # This is a deterministic SelfCoreProjection, not separately
            # stored memory.  Its current activity is behavioural context,
            # never a license to say that the activity has completed.
            "self_core": {
                "entity_id": str(protagonist.get("id") or "zhizhi"),
                "name": str(protagonist.get("name") or ""),
                "source_id": f"world-started:{world_id}:protagonist",
                "source": "world_event:WorldStarted",
                "subject": str(protagonist.get("id") or "zhizhi"),
                "logical_at": str(state.get("world_started_at") or ""),
                "stable_traits": [str(item) for item in _as_list(protagonist.get("stable_traits", []), "stable traits")][:6],
                "values": [str(item) for item in _as_list(protagonist.get("values", []), "values")][:6],
                "preferences": [str(item) for item in _as_list(protagonist.get("preferences", []), "preferences")][:8],
                "relationship_principles": [str(item) for item in _as_list(protagonist.get("relationship_principles", []), "relationship principles")][:6],
                "speech_anchors": [str(item) for item in _as_list(protagonist.get("speech_anchors", []), "speech anchors")][:4],
                "location": str((active or protagonist).get("location") or ""),
                "active_activity": str((active or {}).get("title") or ""),
                "boundaries": [str(item) for item in _as_list(protagonist.get("boundaries", []), "boundaries")],
                "continuity": {
                    "completed_goals": [str(goal.get("title") or goal_id) for goal_id, goal in goals.items() if goal.get("status") == "completed"][:5],
                    "active_goals": [str(goal.get("title") or goal_id) for goal_id, goal in goals.items() if goal.get("status") == "active"][:5],
                    "user_relationship": relationship,
                },
            },
        }

    def conversation_sources_for_query(
        self,
        world_id: str,
        *,
        user_id: str,
        text: str,
        current_message_id: str | None,
        limit: int = 4,
    ) -> list[dict[str, str]]:
        """Retrieve older inbound messages without promoting them to permanent facts."""
        return self._conversation_sources_for_query_from_state(
            self.snapshot(world_id),
            user_id=user_id,
            text=text,
            current_message_id=current_message_id,
            limit=limit,
        )

    @staticmethod
    def _conversation_sources_for_query_from_state(
        state: dict[str, object],
        *,
        user_id: str,
        text: str,
        current_message_id: str | None,
        limit: int = 4,
    ) -> list[dict[str, str]]:
        """Retrieve older inbound messages from an already coherent state."""
        candidates: list[tuple[int, int, dict[str, str]]] = []
        history = _as_list(state.get("recent_messages", []), "recent messages")
        for index, raw in enumerate(history):
            item = _as_dict(raw, "recent message")
            message_id = str(item.get("message_id") or "")
            item_user_id = str(item.get("user_id") or "")
            content = str(item.get("text") or "").strip()
            if (
                item.get("direction") != "in"
                or not message_id
                or message_id == current_message_id
                or (item_user_id and item_user_id != user_id)
                or not content
            ):
                continue
            score = _conversation_relevance(text, content)
            if score <= 0:
                continue
            candidates.append(
                (
                    score,
                    index,
                    {
                        "source_id": f"message:{message_id}",
                        "source_type": "user_message",
                        "speaker": "user",
                        "content": content,
                        "logical_at": str(item.get("logical_at") or item.get("sent_at") or ""),
                        "sent_at": str(item.get("sent_at") or ""),
                        "reference_state": "observed",
                    },
                )
            )
        candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        for fact_id, raw in _as_dict(state.get("facts", {}), "facts").items():
            fact = _as_dict(raw, "fact")
            content = str(fact.get("value") or "").strip()
            if (
                not _fact_is_current(fact)
                or fact.get("scope") != "conversation"
                or str(fact.get("subject") or "") != user_id
                or not content
            ):
                continue
            score = _conversation_relevance(text, content)
            if score > 0:
                candidates.append(
                    (
                        score + 1,
                        -1,
                        {
                            "source_id": str(fact_id),
                            "source_type": "fact",
                            "speaker": "user",
                            "content": content,
                            "logical_at": "",
                            "sent_at": "",
                            "reference_state": "confirmed",
                        },
                    )
                )
        candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        unique: list[dict[str, str]] = []
        seen_content: set[str] = set()
        for _, _, item in candidates:
            if item["content"] in seen_content:
                continue
            seen_content.add(item["content"])
            unique.append(item)
            if len(unique) >= max(1, min(limit, 8)):
                break
        return unique

    @staticmethod
    def _current_scene_source(
        state: dict[str, object]
    ) -> tuple[dict[str, str], dict[str, str]]:
        entities = _as_dict(state["entities"], "entities")
        protagonist = _as_dict(entities.get("zhizhi"), "protagonist")
        active = next(
            (
                _as_dict(item, "activity")
                for item in _as_dict(state["agenda"], "agenda").values()
                if _as_dict(item, "activity").get("status") == "active"
            ),
            None,
        )
        logical_at = str(_as_dict(state["clock"], "clock")["logical_at"])
        location = str((active or protagonist).get("location") or "")
        activity = str((active or {}).get("title") or "")
        content = (
            f"现在在{location}，正在{activity}。"
            if location and activity
            else f"现在在{location}。"
            if location
            else "现在没有可确认的地点记录。"
        )
        scene = {
            "logical_at": logical_at,
            "location": location,
            "activity_id": str((active or {}).get("activity_id") or ""),
            "activity": activity,
            "activity_status": str((active or {}).get("status") or "available"),
        }
        source = {
            "source_id": f"current-scene:{logical_at}",
            "source": "world_projection:current_scene",
            "source_type": "current_scene",
            "subject": str(protagonist.get("id") or "zhizhi"),
            "logical_at": logical_at,
            "purpose": "current_state",
            "reference_state": "current",
            "content": content,
        }
        return scene, source

    def events(self, world_id: str) -> list[WorldEvent]:
        with self.store.connect() as conn:
            return self._load_events(conn, world_id)

    def export_ledger(self, world_id: str) -> list[dict[str, object]]:
        """Portable read-only event export for archival and audit tools."""
        return [
            {
                "event_id": event.event_id, "world_id": event.world_id, "revision": event.revision,
                "event_type": event.event_type, "logical_at": event.logical_at, "observed_at": event.observed_at,
                "source": event.source, "correlation_id": event.correlation_id, "causation_id": event.causation_id,
                "payload": event.payload, "payload_hash": event.payload_hash,
            }
            for event in self.events(world_id)
        ]

    def verify_ledger(self, world_id: str) -> dict[str, object]:
        events = self.events(world_id)
        invalid = [event.event_id for event in events if event.payload_hash != _hash(_stable_json(event.payload))]
        revisions = [event.revision for event in events]
        contiguous = revisions == list(range(1, len(events) + 1))
        rebuilt = self.rebuild_projection(world_id, "world_current_state")
        return {"world_id": world_id, "valid": not invalid and contiguous and rebuilt.matches_live, "invalid_event_ids": invalid, "contiguous_revisions": contiguous, "state_hash": rebuilt.state_hash}

    def validate_reply_candidate(
        self,
        world_id: str,
        candidate: dict[str, object],
        *,
        user_id: str | None = None,
    ) -> dict[str, object]:
        """Reject model output that cites a planned, absent, or failed world fact."""
        reply_text = str(candidate.get("reply_text") or "").strip()
        if not reply_text:
            raise WorldError("reply candidate requires reply_text")
        if (
            _reply_claims_real_tool_completion(reply_text)
            and not ("模拟" in reply_text and "未执行真实操作" in reply_text)
        ):
            raise WorldError("reply claims an unsettled tool operation")
        state = self.snapshot(world_id)
        experiences = _as_dict(state["experiences"], "experiences")
        facts = _as_dict(state["facts"], "facts")
        visible_facts = {
            fact_id: raw
            for fact_id, raw in facts.items()
            if _fact_is_current(raw)
            and (
                user_id is None
                or str(_as_dict(raw, "fact").get("subject") or "")
                in {user_id, "world", "zhizhi"}
            )
        }
        conversation_sources = {
            f"message:{str(item.get('message_id') or '')}": str(item.get("text") or "")
            for raw in _as_list(state.get("recent_messages", []), "recent messages")
            if (item := _as_dict(raw, "recent message")).get("direction") == "in"
            and item.get("message_id")
            and (user_id is None or str(item.get("user_id") or "") == user_id)
            and str(item.get("text") or "").strip()
        }
        user_owned_sources = set(conversation_sources)
        if user_id is not None:
            user_owned_sources.update(
                str(fact_id)
                for fact_id, raw in facts.items()
                if str(_as_dict(raw, "fact").get("subject") or "") == user_id
                and _fact_is_current(raw)
            )
        _, current_scene_source = self._current_scene_source(state)
        known = set(experiences) | set(visible_facts) | set(conversation_sources) | {current_scene_source["source_id"]}
        sources = {
            **{record_id: str(_as_dict(item, "experience")["content"]) for record_id, item in experiences.items()},
            **{record_id: str(_as_dict(item, "fact")["value"]) for record_id, item in visible_facts.items()},
            **conversation_sources,
            current_scene_source["source_id"]: current_scene_source["content"],
        }
        mentioned = [
            str(item)
            for item in _as_list(candidate.get("mentioned_event_ids", []), "mentioned_event_ids")
        ]
        raw_claims = _as_list(candidate.get("claims", []), "claims")
        claims: list[dict[str, object]] = []
        source_rewrites: dict[str, str] = {}
        for raw_claim in raw_claims:
            claim = dict(_as_dict(raw_claim, "reply claim"))
            source_id = str(claim.get("source_id") or "")
            text = str(claim.get("text") or "").strip()
            if source_id.startswith("user-conversation:") and source_id not in known and text:
                exact_sources = [
                    known_id for known_id, source_text in sources.items()
                    if text in source_text
                ]
                if len(exact_sources) == 1:
                    source_rewrites[source_id] = exact_sources[0]
                    claim["source_id"] = exact_sources[0]
            claims.append(claim)
        mentioned = [source_rewrites.get(source_id, source_id) for source_id in mentioned]
        mentioned = list(
            dict.fromkeys(
                [
                    *mentioned,
                    *(
                        str(_as_dict(item, "reply claim").get("source_id") or "")
                        for item in claims
                    ),
                ]
            )
        )
        mentioned = [source_id for source_id in mentioned if source_id]
        proposed_actions = _as_list(candidate.get("proposed_action_ids", []), "proposed_action_ids")
        unknown = [source_id for source_id in mentioned if source_id not in known]
        if unknown:
            raise WorldError(f"reply cites uncommitted world records: {', '.join(unknown)}")
        normalized_claims: list[dict[str, str]] = []
        epistemic_reply = bool(
            re.search(r"(?:我猜|我觉得可能|可能|也许|或许|大概|说不准|未必)", reply_text)
        )
        for raw_claim in claims:
            claim = _as_dict(raw_claim, "reply claim")
            source_id = str(claim.get("source_id") or "")
            text = str(claim.get("text") or "").strip()
            assertion = str(claim.get("assertion") or "").strip()
            if source_id not in sources or source_id not in mentioned or not text:
                raise WorldError("each reply claim needs a mentioned committed source id and text")
            if text not in sources[source_id]:
                raise WorldError("reply claim text must be quoted from its committed source")
            if (
                source_id in user_owned_sources
                and text.startswith("我")
                and not assertion
                and text in reply_text
                and f"“{text}”" not in reply_text
                and f'"{text}"' not in reply_text
                and f"说：{text}" not in reply_text
            ):
                raise WorldError(
                    "first-person user evidence must be quoted or rewritten as an assertion"
                )
            if assertion:
                if assertion not in reply_text:
                    raise WorldError("reply claim assertion must appear in reply_text")
                if not _bounded_paraphrase(assertion, text):
                    raise WorldError("reply claim assertion is not supported by its evidence")
                normalized_claims.append(
                    {"source_id": source_id, "text": text, "assertion": assertion}
                )
                continue
            if text not in reply_text and epistemic_reply:
                # Models sometimes attach the context that informed a guess.
                # The guess is not a factual assertion, so provenance remains
                # in mentioned_event_ids but is not promoted to a reply claim.
                continue
            if text not in reply_text:
                raise WorldError("reply claim text must appear in the factual reply")
            normalized_claims.append({"source_id": source_id, "text": text})
        # A reply without claims may still converse, but it cannot state a
        # completed off-screen experience.  Claim text is intentionally quoted
        # from its source, making provenance deterministic rather than a model
        # assertion that merely names an arbitrary id.
        event_claim = re.search(
            r"(?:我|她)(?:(?:刚刚?|昨天|昨晚|今天|上午|下午|之前|已经).{0,8}|.{0,12})"
            r"(?:去了|吃了|见了|聊了|做了|完成了|回来|逛了|看了|参加了|上了)",
            reply_text,
        )
        if event_claim and not normalized_claims:
            raise WorldError("reply states an experience without a committed source id")
        unsupported_environment_claim = re.search(
            r"(?:图书馆|学校|宿舍|门口|楼下|附近)[^。！!?！？]{0,24}"
            r"(?:新开|开了|有家|发生|正在)",
            reply_text,
        )
        environment_has_direct_source = bool(
            unsupported_environment_claim
            and any(
                unsupported_environment_claim.group(0)
                in sources.get(str(claim.get("source_id") or ""), "")
                for claim in normalized_claims
            )
        )
        if unsupported_environment_claim and not environment_has_direct_source:
            raise WorldError("reply states a local world detail without a committed source id")
        entities = _as_dict(state["entities"], "entities")
        for entity in entities.values():
            npc = _as_dict(entity, "entity")
            name = str(npc.get("name") or "")
            if (
                npc.get("kind") not in {"companion", "user"}
                and name
                and re.search(
                    rf"{re.escape(name)}[^。！!?！？]{{0,18}}(?:是我|是个|很顺利|不顺利|说了|告诉我|喜欢|讨厌)",
                    reply_text,
                )
                and not normalized_claims
            ):
                raise WorldError("reply states an NPC detail without a committed source id")
        remainder = reply_text
        for claim in normalized_claims:
            remainder = remainder.replace(claim.get("assertion") or claim["text"], "")
        # A question mark only protects the actual question about the user; it
        # must not launder a preceding first-person world claim.  Keep this
        # deterministic and deliberately conservative: claims are either
        # quoted from a committed source above, or use an explicitly
        # first-person/implicit-current-world opening here and are rejected.
        unsupported_world_claim = any(
            re.search(pattern, remainder)
            for pattern in (
                r"(?:这会儿|此刻|刚刚?|现在|正在|还在)[^。！!?！？]{0,36}(?:醒|睡|赖|爬|去|上课|下课|吃|看书|散步|整理|忙|回来|在床|在宿舍|在图书馆|盘)",
                r"(?:昨天|昨晚|早上|上午|下午|今晚|今天|明天)[^。！!?！？]{0,36}(?:去了|做了|吃了|见了|聊了|看了|参加了|完成了|回来|上课|下课)",
                r"我(?:以前|曾经|也)[^。！!?！？]{0,24}(?:有过|经历过|做过|去过|见过|聊过)",
                r"(?:我这儿|我这里|这边|这里)[^。！!?！？]{0,36}(?:空调|天气|温度|有点凉|有点冷|有点热|很吵|很安静|下雨)",
                r"我(?:书包里|包里|手边|桌上|宿舍里)[^。！!?！？]{0,30}(?:常备|放着|带着|有茶|有咖啡)",
                r"(?:桌上|手边|旁边|包里)[^。！!?！？]{0,24}(?:正好)?(?:有|放着|摆着)[^。！!?！？]{0,16}(?:杯|茶|咖啡|饮料|书|东西)",
                r"(?:难怪)?你[^。！!?！？]{0,12}(?:一大早|这么早)[^。！!?！？]{0,12}(?:起来|醒|没睡)",
                r"(?:最近|现在)[^。！!?！？]{0,28}(?:很多人|挺多人|大家都|很流行|都在做)",
                r"我[^。！!?！？]{0,36}(?:换个位置|换位置|靠窗|拿出|走到|坐到)",
                r"(?:我)?在宿舍[^。！!?！？]{0,18}(?:歇着|休息|躺着|发呆)",
                r"我(?:现在|这会儿|此刻)?在[^。！!?！？]{1,18}(?:上|里|馆|校|室|店|家)(?:。|，|！|$)",
                r"我(?:也|正|就|还)?(?:现在|这会儿|此刻|刚才|今天|昨天|明天|已经)?"
                r"在(?!意|听|想|乎|这[儿里]|呢|呀)[^。！!?！？]{1,32}(?:。|，|！|？|$)",
                r"我(?:有(?:个|一位|几个)?|认识|跟|和)[^。！!?！？]{0,20}"
                r"(?:哥哥|姐姐|弟弟|妹妹|爸爸|妈妈|爷爷|奶奶|外公|外婆|叔叔|阿姨|"
                r"父母|家人|室友|同学|老师|邻居|前任)",
                r"我(?:觉得|想|认为|听说)?[^。！!?！？]{0,12}"
                r"(?:哥哥|姐姐|弟弟|妹妹|爸爸|妈妈|爷爷|奶奶|外公|外婆|叔叔|阿姨|"
                r"父母|家人|室友|同学|老师|邻居|前任)[^。！!?！？]{0,20}"
                r"(?:住在|来自|出生于|是(?:个|一位)|有)",
                r"我(?:来自|出生于|住在)[^。！!?！？]{1,32}(?:。|，|！|？|$)",
                r"(?:本地|云端|服务器|硬盘|数据库)[^。！!?！？]{0,12}(?:没了|丢了|坏了|删除了)",
                r"我[^。！!?！？]{0,18}(?:睡不着|失眠|难受)(?:的时候|时)[^。！!?！？]{0,18}(?:会|就)",
                r"(?:我跟着[^。！!?！？]{0,18}|松了一口气|确实在意了|我反而觉得[^。！!?！？]{0,12}踏实)",
            )
        )
        if reply_text != "我在。" and unsupported_world_claim:
            raise WorldError("reply contains world-time or experience text outside committed claims")
        if not normalized_claims and re.search(
            r"(?:这位|那个|他|她|它|朋友|同事|室友|同学|老师|邻居|前任)"
            r"[^。！!?！？]{0,28}(?:正在|住在|输液|住院|生病|怀孕|结冰|"
            r"很(?:帅|漂亮|忙|冷|热)|是(?:个|一位)|有(?:个|一位))",
            reply_text,
        ):
            raise WorldError("reply states a third-party fact without a committed source id")
        if not normalized_claims and re.search(
            r"(?:气温|天气|楼上|楼下|隔壁|路上|街上|湖边|海边|公园|附近)"
            r"[^。！!?！？]{0,28}(?:正在|已经|装修|结冰|下雨|下雪|很(?:冷|热)|"
            r"有(?:人|家|店))",
            reply_text,
        ):
            raise WorldError("reply states an environment fact without a committed source id")
        actions = _as_dict(state["actions"], "actions")
        invalid_actions = [str(item) for item in proposed_actions if str(item) not in actions]
        if invalid_actions:
            raise WorldError(f"reply proposes unknown actions: {', '.join(invalid_actions)}")
        return {
            "reply_text": reply_text,
            "mentioned_event_ids": [str(item) for item in mentioned],
            "proposed_action_ids": [str(item) for item in proposed_actions],
            "claims": normalized_claims,
        }

    def require_settleable_reply_actions(
        self,
        world_id: str,
        action_ids: list[str] | tuple[str, ...],
        *,
        user_id: str,
    ) -> tuple[str, ...]:
        """Authorize pending external Action references in one reply.

        A reply may say that an already-authorized operation is pending, but it
        cannot use an arbitrary, terminal, cross-user, or ordinary message
        Action as evidence of an external capability.  This check does not
        settle the Action: only its own adapter receipt may do that.
        """
        state = self.snapshot(world_id)
        return self._require_settleable_reply_actions_in_state(
            state, action_ids, user_id=user_id
        )

    @staticmethod
    def _require_settleable_reply_actions_in_state(
        state: dict[str, object],
        action_ids: list[str] | tuple[str, ...],
        *,
        user_id: str,
    ) -> tuple[str, ...]:
        action_ids = tuple(dict.fromkeys(str(item) for item in action_ids if str(item)))
        actions = _as_dict(state.get("actions", {}), "actions")
        media = _as_dict(state.get("media", {}), "media")
        tools = _as_dict(state.get("tool_actions", {}), "tool actions")
        for action_id in action_ids:
            action = _as_dict(actions.get(action_id), "referenced reply action")
            kind = str(action.get("kind") or "")
            if action.get("status") != "scheduled":
                raise WorldError("reply action reference must still be scheduled")
            payload = _as_dict(action.get("payload", {}), "referenced reply action payload")
            owner = ""
            if kind == "tool_execution":
                proposal = _as_dict(
                    tools.get(str(payload.get("proposal_id") or "")),
                    "referenced tool proposal",
                )
                owner = str(proposal.get("user_id") or "")
            elif kind in {"media_generation", "media_delivery"}:
                request = _as_dict(
                    media.get(str(payload.get("request_id") or "")),
                    "referenced media request",
                )
                owner = str(request.get("user_id") or "")
            else:
                raise WorldError("reply action reference is not an external user action")
            if owner != user_id:
                raise WorldError("reply action reference belongs to another user")
        return action_ids

    def grounded_reply_from_mentions(
        self,
        world_id: str,
        candidate: dict[str, object],
        *,
        user_id: str | None = None,
    ) -> dict[str, object] | None:
        """Build an exact-source fallback when a model cited but misquoted it."""
        state = self.snapshot(world_id)
        _, scene = self._current_scene_source(state)
        sources = {
            **{
                str(record_id): str(_as_dict(item, "experience")["content"])
                for record_id, item in _as_dict(state["experiences"], "experiences").items()
            },
            **{
                str(record_id): str(_as_dict(item, "fact")["value"])
                for record_id, item in _as_dict(state["facts"], "facts").items()
                if _fact_is_current(item)
                and (
                    user_id is None
                    or str(_as_dict(item, "fact").get("subject") or "")
                    in {user_id, "world", "zhizhi"}
                )
            },
            **{
                f"message:{str(item.get('message_id') or '')}": str(item.get("text") or "")
                for raw in _as_list(state.get("recent_messages", []), "recent messages")
                if (item := _as_dict(raw, "recent message")).get("direction") == "in"
                and item.get("message_id")
                and (user_id is None or str(item.get("user_id") or "") == user_id)
                and str(item.get("text") or "").strip()
            },
            scene["source_id"]: scene["content"],
        }
        requested_ids = [
            str(item)
            for item in _as_list(candidate.get("mentioned_event_ids", []), "mentioned_event_ids")
        ]
        requested_ids.extend(
            str(_as_dict(item, "reply claim").get("source_id") or "")
            for item in _as_list(candidate.get("claims", []), "claims")
        )
        requested = list(dict.fromkeys(source_id for source_id in requested_ids if source_id in sources))
        if not requested:
            return None
        unique_mentions: list[str] = []
        seen_texts: set[str] = set()
        for source_id in requested:
            if sources[source_id] in seen_texts:
                continue
            seen_texts.add(sources[source_id])
            unique_mentions.append(source_id)
            if len(unique_mentions) == 2:
                break
        mentioned = unique_mentions
        facts = _as_dict(state["facts"], "facts")
        entities = _as_dict(state["entities"], "entities")

        def source_is_user(source_id: str) -> bool:
            if source_id.startswith("message:"):
                return True
            raw_fact = facts.get(source_id)
            if not isinstance(raw_fact, dict):
                return False
            raw_entity = entities.get(str(raw_fact.get("subject") or ""))
            return isinstance(raw_entity, dict) and raw_entity.get("kind") == "user"

        user_sourced = all(source_is_user(source_id) for source_id in mentioned)
        texts = [sources[source_id] for source_id in mentioned]
        return {
            "reply_text": "".join(texts),
            "mentioned_event_ids": mentioned,
            "proposed_action_ids": [],
            "_user_sourced": user_sourced,
            "claims": [
                {"source_id": source_id, "text": sources[source_id]}
                for source_id in mentioned
            ],
        }

    def action_id_for_delivery(self, world_id: str, delivery_id: int) -> str | None:
        for action_id, action in _as_dict(self.snapshot(world_id)["actions"], "actions").items():
            if _as_dict(action, "action").get("delivery_id") == delivery_id:
                return action_id
        return None

    def due_actions(self, world_id: str, *, now: datetime) -> list[dict[str, object]]:
        """Return scheduled actions whose recorded due time has passed in logical time."""
        actions = _as_dict(self.snapshot(world_id)["actions"], "actions")
        due: list[dict[str, object]] = []
        for action_id, action in actions.items():
            item = _as_dict(action, "action")
            due_at = _as_dict(item.get("payload", {}), "action payload").get("due_at")
            if item["status"] == "scheduled" and due_at and _parse_at(str(due_at)) <= now:
                due.append({"action_id": action_id, **item})
        return due

    def _start_world(self, command: dict[str, object], expected_revision: int) -> WorldDecision:
        if expected_revision != 0:
            raise ConcurrencyConflict("a new world must start at revision 0")
        seed = _as_dict(command.get("seed"), "seed")
        world_id = str(seed.get("world_id") or "")
        logical_at = str(seed.get("logical_at") or "")
        protagonist = _as_dict(seed.get("protagonist"), "protagonist")
        if not world_id or not logical_at or not protagonist.get("id"):
            raise WorldError("world seed requires world_id, logical_at, and protagonist.id")
        with self.store.connect() as conn:
            existing = conn.execute("select revision from worlds where world_id = ?", (world_id,)).fetchone()
            if existing:
                raise WorldError(f"world already exists: {world_id}")
            now = utc_now().isoformat()
            conn.execute(
                "insert into worlds (world_id, revision, logical_at, seed_hash, created_at) values (?, 0, ?, ?, ?)",
                (world_id, logical_at, _hash(_stable_json(seed)), now),
            )
            events = [
                (
                    "WorldStarted",
                    {
                        "protagonist": protagonist,
                        "logical_at": logical_at,
                        "daily_schedule": _as_list(seed.get("daily_schedule", []), "daily_schedule"),
                        "weekly_themes": _as_list(seed.get("weekly_themes", []), "weekly themes"),
                        "long_term_goals": _as_list(seed.get("long_term_goals", []), "long-term goals"),
                        "life_outcome_templates": _as_dict(seed.get("life_outcome_templates", {}), "life outcome templates"),
                        "location_travel_minutes": _as_dict(seed.get("location_travel_minutes", {}), "location travel minutes"),
                        "affect_profile": _as_dict(
                            seed.get("affect_profile", {}), "affect profile"
                        ),
                    },
                )
            ]
            for npc in _as_list(seed.get("npcs", []), "npcs"):
                events.append(("NpcRegistered", _as_dict(npc, "npc")))
            if bool(seed.get("materialize_current_schedule")):
                logical_now = _parse_at(logical_at)
                active_seed_items: list[tuple[dict[str, object], datetime, datetime]] = []
                for raw_item in _as_list(seed.get("daily_schedule", []), "daily schedule"):
                    item = _as_dict(raw_item, "daily schedule item")
                    starts = logical_now.replace(
                        hour=int(item["starts_hour"]), minute=0, second=0, microsecond=0
                    )
                    ends = logical_now.replace(
                        hour=int(item["ends_hour"]), minute=0, second=0, microsecond=0
                    )
                    if starts <= logical_now < ends:
                        active_seed_items.append((item, starts, ends))
                if len(active_seed_items) > 1:
                    raise WorldError("world seed has overlapping activities at logical epoch")
                for item, starts, ends in active_seed_items:
                    activity_id = f"{logical_now.date().isoformat()}:{item['slot']}"
                    activity = {
                        "activity_id": activity_id,
                        "entity_id": "zhizhi",
                        "title": str(item["title"]),
                        "template_id": str(item.get("template_id") or ""),
                        "location": str(item.get("location") or ""),
                        "starts_at": starts.isoformat(),
                        "ends_at": ends.isoformat(),
                        "attention_demand": int(item.get("attention_demand", 35)),
                        "interruptible": bool(item.get("interruptible", True)),
                    }
                    if str(item.get("kind") or "") == "rest":
                        activity["activity_kind"] = "rest"
                        activity["rest_recovery"] = int(item.get("rest_recovery", 8))
                    events.append(("ActivityPlanned", activity))
                    if activity["template_id"]:
                        events.append(
                            (
                                "ActivitySelected",
                                {
                                    "activity_id": activity_id,
                                    "template_id": activity["template_id"],
                                    "reason": "seed_epoch_activity",
                                    "rule_version": self.life_simulation.RULE_VERSION,
                                },
                            )
                        )
                    events.append(("ActivityStarted", {"activity_id": activity_id}))
            state = _empty_state(world_id)
            return self._append_and_project(
                conn,
                world_id,
                0,
                state,
                events,
                idempotency_key=f"world-start:{world_id}",
                correlation_id=str(uuid4()),
                source="world_seed",
                actor={"kind": "seed"},
                causation_id=None,
            )

    def _events_for_command(
        self, command: dict[str, object], state: dict[str, object]
    ) -> list[tuple[str, dict[str, object]]]:
        command_type = str(command["type"])
        if command_type == "set_clock_mode":
            mode = str(command.get("mode") or "")
            rate = int(command.get("rate") or 0)
            valid_mode = (mode == "paused" and rate == 0) or (mode == "realtime" and rate == 1) or (
                mode == "accelerated" and rate in {1, 2, 4, 8}
            )
            if not valid_mode:
                raise WorldError("invalid clock mode or rate")
            return [("ClockModeChanged", {"mode": mode, "rate": rate})]
        if command_type == "observe_input_merge_candidate":
            merge_key = str(command.get("merge_key") or "").strip()
            message = _as_dict(command.get("message", {}), "merge message")
            message_id = str(message.get("message_id") or "").strip()
            pending_count = int(command.get("pending_count") or 0)
            wait_seconds = float(command.get("wait_seconds") or 0)
            reason = str(command.get("reason") or "").strip()
            if (
                not merge_key
                or not message_id
                or not reason
                or not 1 <= pending_count <= 6
                or not 0 <= wait_seconds <= 300
            ):
                raise WorldError("input merge candidate requires bounded replayable decision data")
            existing = _as_dict(state.get("input_merges", {}), "input merges").get(merge_key)
            if isinstance(existing, dict) and existing.get("status") == "pending":
                known = {
                    str(_as_dict(item, "merge message").get("message_id") or "")
                    for item in _as_list(existing.get("messages", []), "merge messages")
                }
                if message_id in known:
                    return []
            return [
                (
                    "InputMergeCandidateObserved",
                    {
                        "merge_key": merge_key,
                        "message": dict(message),
                        "pending_count": pending_count,
                        "wait_seconds": wait_seconds,
                        "reason": reason,
                        "max_batch": 6,
                        "rule_version": "input-merge-v1",
                    },
                )
            ]
        if command_type == "settle_input_merge":
            merge_key = str(command.get("merge_key") or "").strip()
            merge = _as_dict(
                _as_dict(state.get("input_merges", {}), "input merges").get(merge_key),
                "input merge",
            )
            message_ids = tuple(
                str(item) for item in _as_list(command.get("message_ids", []), "merge message ids")
            )
            known = tuple(
                str(_as_dict(item, "merge message").get("message_id") or "")
                for item in _as_list(merge.get("messages", []), "merge messages")
            )
            if merge.get("status") != "pending" or not message_ids or message_ids != known:
                raise WorldError("input merge settlement must consume the exact pending batch")
            return [
                (
                    "InputMergeSettled",
                    {
                        "merge_key": merge_key,
                        "message_ids": list(message_ids),
                        "merged_message_id": str(command.get("merged_message_id") or message_ids[-1]),
                        "terminal_state": "settled",
                        "rule_version": "input-merge-v1",
                    },
                )
            ]
        if command_type == "advance_clock":
            target = str(command.get("target_logical_at") or "")
            current = str(_as_dict(state["clock"], "clock")["logical_at"])
            if not target or _parse_at(target) < _parse_at(current):
                raise WorldError("logical time cannot move backwards")
            target_at = _parse_at(target)
            current_at = _parse_at(current)
            world_id = str(state["world_id"])
            events: list[tuple[str, dict[str, object]]] = []
            working = json.loads(_stable_json(state))

            def emit(event_type: str, payload: dict[str, object], *, logical_at: str | None = None) -> None:
                nonlocal working
                events.append((event_type, payload))
                working = reduce_event(
                    working,
                    WorldEvent(
                        event_id="simulation",
                        world_id=world_id,
                        revision=0,
                        event_type=event_type,
                        schema_version=1,
                        logical_at=logical_at or target,
                        observed_at=logical_at or target,
                        actor={"kind": "simulation"},
                        source="life_simulation",
                        correlation_id="simulation",
                        causation_id=None,
                        idempotency_key=None,
                        payload=payload,
                        payload_hash="",
                    ),
                )

            clock_payload: dict[str, object] = {"target_logical_at": target}
            if command.get("observed_at"):
                clock_payload["observed_at"] = str(command["observed_at"])
            emit("ClockAdvanced", clock_payload)
            # Inner records are temporal projections, not immortal memories.
            # Emit terminal expiry events deterministically so replay and an
            # incremental logical-clock advance converge on the same state.
            for impression_id, raw_impression in sorted(
                _as_dict(working.get("private_impressions", {}), "private impressions").items(),
                key=lambda item: str(_as_dict(item[1], "private impression").get("expires_at") or ""),
            ):
                impression = _as_dict(raw_impression, "private impression")
                expires_at = str(impression.get("expires_at") or "")
                if (
                    impression.get("status") == "active"
                    and expires_at
                    and _parse_at(expires_at) <= target_at
                ):
                    emit(
                        "PrivateImpressionExpired",
                        {"impression_id": impression_id, "reason": "logical_expiry"},
                        logical_at=expires_at,
                    )
            for commitment_id, raw_commitment in sorted(
                _as_dict(working.get("private_commitments", {}), "private commitments").items(),
                key=lambda item: str(_as_dict(item[1], "private commitment").get("expires_at") or ""),
            ):
                commitment = _as_dict(raw_commitment, "private commitment")
                expires_at = str(commitment.get("expires_at") or "")
                if (
                    commitment.get("status") == "active"
                    and expires_at
                    and _parse_at(expires_at) <= target_at
                ):
                    emit(
                        "PrivateCommitmentExpired",
                        {"commitment_id": commitment_id, "reason": "logical_expiry"},
                        logical_at=expires_at,
                    )
            # Weekly plans are intentions materialized from the seed.  Use the
            # pre-advance clock as the planning cutoff so a long replay jump
            # produces the same future plan that incremental ticks would have
            # produced; completion still happens only in the activity loop.
            planning_week = (current_at - timedelta(days=current_at.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            last_week = (target_at - timedelta(days=target_at.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            while planning_week <= last_week:
                planning_state = json.loads(_stable_json(working))
                _as_dict(planning_state["clock"], "planning clock")[
                    "logical_at"
                ] = current_at.isoformat()
                for plan_event_type, plan_payload in self.life_evolution.plan_week(
                    planning_state, week_start=planning_week
                ):
                    if plan_event_type != "ActivityPlanned":
                        emit(plan_event_type, plan_payload)
                planning_week += timedelta(days=7)
            materialized_activity_ids = set(
                _as_dict(working["agenda"], "agenda")
            )
            for raw_plan in _as_dict(
                working.get("weekly_plans", {}), "weekly plans"
            ).values():
                plan = _as_dict(raw_plan, "weekly plan")
                for raw_theme in _as_dict(
                    plan.get("themes", {}), "weekly plan themes"
                ).values():
                    theme = _as_dict(raw_theme, "weekly plan theme")
                    for raw_activity in _as_list(
                        theme.get("activities", []), "weekly theme activities"
                    ):
                        planned_activity = _as_dict(
                            raw_activity, "weekly planned activity"
                        )
                        activity_id = str(planned_activity["activity_id"])
                        if (
                            activity_id not in materialized_activity_ids
                            and _parse_at(str(planned_activity["starts_at"]))
                            <= target_at
                        ):
                            emit("ActivityPlanned", dict(planned_activity))
                            materialized_activity_ids.add(activity_id)
            evolution = _as_dict(
                working.get("life_evolution", {}), "life evolution"
            )
            for observation_id, raw_observation in sorted(
                _as_dict(
                    evolution.get("observations", {}),
                    "environment observations",
                ).items()
            ):
                observation = _as_dict(
                    raw_observation, "environment observation"
                )
                if (
                    observation.get("status") == "active"
                    and observation.get("expires_at")
                    and _parse_at(str(observation["expires_at"])) <= target_at
                ):
                    emit(
                        "EnvironmentObservationExpired",
                        {
                            "observation_id": observation_id,
                            "reason": "logical_expiry",
                        },
                        logical_at=str(observation["expires_at"]),
                    )
            for influence_id, raw_influence in sorted(
                _as_dict(evolution.get("influences", {}), "life influences").items()
            ):
                influence = _as_dict(raw_influence, "life influence")
                if (
                    influence.get("status") == "active"
                    and influence.get("expires_at")
                    and _parse_at(str(influence["expires_at"])) <= target_at
                ):
                    emit(
                        "LifeInfluenceExpired",
                        {"influence_id": influence_id, "reason": "logical_expiry"},
                        logical_at=str(influence["expires_at"]),
                    )
            affect_cursor = current_at

            def decay_affect_until(at: datetime) -> None:
                nonlocal affect_cursor
                previous_affect = _as_dict(
                    working["emotion_modulation"], "emotion modulation"
                )
                recorded_anchor = str(previous_affect.get("decay_anchor_at") or "")
                effective_cursor = affect_cursor
                if recorded_anchor:
                    effective_cursor = max(effective_cursor, _parse_at(recorded_anchor))
                if at <= effective_cursor:
                    return
                decayed_affect = decay_affect(
                    previous_affect,
                    int((at - effective_cursor).total_seconds()),
                    at.isoformat(),
                )
                emit(
                    "AffectDecayed",
                    affect_outcome_payload(
                        decayed_affect,
                        logical_at=at.isoformat(),
                        event_type="AffectDecayed",
                    ),
                    logical_at=at.isoformat(),
                )
                if bool(previous_affect.get("unresolved")) and not decayed_affect.unresolved:
                    emit(
                        "AffectResolved",
                        affect_outcome_payload(
                            decayed_affect,
                            logical_at=at.isoformat(),
                            event_type="AffectResolved",
                        ),
                        logical_at=at.isoformat(),
                    )
                affect_cursor = at

            def expire_threads_until(at: datetime) -> None:
                threads = sorted(
                    _as_dict(
                        working.get("conversation_threads", {}),
                        "conversation threads",
                    ).items(),
                    key=lambda item: str(
                        _as_dict(item[1], "conversation thread").get(
                            "expires_at"
                        )
                        or ""
                    ),
                )
                for thread_id, raw_thread in threads:
                    thread = _as_dict(raw_thread, "conversation thread")
                    if (
                        thread.get("status") != "open"
                        or not thread.get("expires_at")
                        or thread.get("rule_version")
                        != "conversation-commitments-v1"
                    ):
                        continue
                    expires_at = _parse_at(str(thread["expires_at"]))
                    if expires_at > at:
                        continue
                    user_id = str(thread.get("user_id") or "")
                    relationship = _as_dict(
                        _as_dict(
                            working.get("relationships", {}), "relationships"
                        ).get(user_id, {}),
                        "thread relationship",
                    )
                    waiting = evaluate_waiting_response(
                        thread,
                        relationship=relationship,
                        logical_at=expires_at,
                    )
                    if waiting.phase != str(
                        thread.get("waiting_phase") or "not_due"
                    ):
                        emit(
                            "ConversationThreadWaitingChanged",
                            {
                                "thread_id": thread_id,
                                "phase": waiting.phase,
                                "reason": waiting.reason,
                                "expression_policy": waiting.expression_policy,
                                "relationship_deltas": dict(
                                    waiting.relationship_deltas
                                ),
                                "next_review_at": (
                                    waiting.next_review_at.isoformat()
                                    if waiting.next_review_at
                                    else None
                                ),
                                "rule_version": waiting.rule_version,
                            },
                            logical_at=expires_at.isoformat(),
                        )
                        for dimension, delta in waiting.relationship_deltas.items():
                            emit(
                                "RelationshipChanged",
                                {
                                    "entity_id": user_id,
                                    "dimension": dimension,
                                    "delta": delta,
                                    "reason": waiting.reason,
                                    "thread_id": thread_id,
                                    "rule_version": waiting.rule_version,
                                },
                                logical_at=expires_at.isoformat(),
                            )
                    decay_affect_until(expires_at)
                    emit(
                        "ConversationThreadExpired",
                        {"thread_id": thread_id, "reason": "logical_timeout"},
                        logical_at=expires_at.isoformat(),
                    )
                    for event_type, event_payload in self._thread_commitment_resolution_events(
                        working,
                        thread_id=thread_id,
                        outcome="released",
                        reason="conversation_thread_expired",
                    ):
                        emit(event_type, event_payload, logical_at=expires_at.isoformat())
                    affect = apply_appraisal(
                        _as_dict(
                            working["emotion_modulation"], "emotion modulation"
                        ),
                        "conversation_thread_expired",
                        expires_at.isoformat(),
                        source_reference=f"conversation_thread:{thread_id}",
                        target=user_id or "user",
                    )
                    emit(
                        "AffectChanged",
                        affect_outcome_payload(
                            affect,
                            logical_at=expires_at.isoformat(),
                            event_type="AffectChanged",
                        ),
                        logical_at=expires_at.isoformat(),
                    )

            timeline: list[tuple[datetime, datetime, dict[str, object], bool]] = []
            for raw in _as_dict(working["agenda"], "agenda").values():
                activity = dict(_as_dict(raw, "activity"))
                if activity.get("status") in {"planned", "active"}:
                    timeline.append(
                        (
                            _parse_at(str(activity["starts_at"])),
                            _parse_at(str(activity["ends_at"])),
                            activity,
                            True,
                        )
                    )

            known_ids = set(_as_dict(working["agenda"], "agenda"))
            local_day = _parse_at(current).date()
            while local_day <= target_at.date():
                for template in _as_list(state.get("daily_schedule", []), "daily_schedule"):
                    item = _as_dict(template, "daily schedule item")
                    starts = datetime(
                        local_day.year, local_day.month, local_day.day, int(item["starts_hour"]), tzinfo=target_at.tzinfo
                    )
                    ends = datetime(
                        local_day.year, local_day.month, local_day.day, int(item["ends_hour"]), tzinfo=target_at.tzinfo
                    )
                    activity_id = f"{local_day.isoformat()}:{item['slot']}"
                    conflicts_with_planned_theme = any(
                        starts < existing_ends and existing_starts < ends
                        for existing_starts, existing_ends, existing_activity, _ in timeline
                        if str(existing_activity.get("plan_source") or "").startswith(
                            "weekly:"
                        )
                        and existing_activity.get("status")
                        not in {"cancelled", "deferred", "rested", "completed"}
                    )
                    if (
                        activity_id not in known_ids
                        and starts <= target_at
                        and current_at <= ends
                        and not conflicts_with_planned_theme
                    ):
                        timeline.append((starts, ends, {**item, "activity_id": activity_id}, False))
                local_day += timedelta(days=1)

            occupied = [
                dict(_as_dict(item, "activity"))
                for item in _as_dict(working["agenda"], "agenda").values()
                if item.get("status") in {"completed", "active"} and item.get("ends_at")
                and _parse_at(str(item["ends_at"])) <= current_at
            ]
            previous = max(occupied, key=lambda item: str(item["ends_at"]), default=None)

            for starts, ends, raw, existing in sorted(timeline, key=lambda entry: (entry[0], entry[1], str(entry[2].get("activity_id")))):
                activity_id = str(raw["activity_id"])
                if starts > target_at:
                    continue
                if existing:
                    activity = _as_dict(_as_dict(working["agenda"], "agenda")[activity_id], "activity")
                    if activity.get("activity_kind") == "rest":
                        if activity.get("status") == "planned":
                            emit("ActivityStarted", {"activity_id": activity_id})
                        if ends <= target_at:
                            emit(
                                "ActivityRested",
                                {
                                    "activity_id": activity_id,
                                    "reason": "scheduled_rest_completed",
                                    "energy_delta": int(activity.get("rest_recovery", 8)),
                                },
                            )
                            previous = dict(
                                _as_dict(
                                    _as_dict(working["agenda"], "agenda")[activity_id],
                                    "activity",
                                )
                            )
                        continue
                    if (
                        activity.get("status") == "planned"
                        and str(activity.get("plan_source") or "").startswith(
                            "weekly:"
                        )
                    ):
                        alternatives = [
                            str(value)
                            for value in _as_list(
                                activity.get("fallback_templates", []),
                                "weekly fallback templates",
                            )
                        ]
                        candidate_ids = list(
                            dict.fromkeys(
                                [
                                    str(activity.get("template_id") or ""),
                                    *alternatives,
                                ]
                            )
                        )
                        assessments = [
                            self.life_evolution.score_candidate(
                                working, activity, candidate_id
                            )
                            for candidate_id in candidate_ids
                        ]
                        selected_activity, selection_reason = (
                            self.life_simulation.choose_template(
                                working,
                                dict(activity),
                                alternatives,
                                priority_scores={
                                    assessment.template_id: assessment.score
                                    for assessment in assessments
                                },
                            )
                        )
                        for assessment in assessments:
                            emit(
                                "ActivityCandidateEvaluated",
                                {
                                    "activity_id": activity_id,
                                    "template_id": assessment.template_id,
                                    "eligible": assessment.eligible,
                                    "score": assessment.score,
                                    "reasons": list(assessment.reasons),
                                    "rejected_reasons": list(
                                        assessment.rejected_reasons
                                    ),
                                    "selected": selection_reason
                                    != "no_eligible_template"
                                    and assessment.template_id
                                    == selected_activity["template_id"],
                                    "rule_version": self.life_evolution.RULE_VERSION,
                                },
                            )
                        if selection_reason == "no_eligible_template":
                            emit(
                                "ActivityDeferred",
                                {
                                    "activity_id": activity_id,
                                    "reason": "no_eligible_seeded_activity",
                                    "next_review_at": (
                                        ends + timedelta(hours=4)
                                    ).isoformat(),
                                },
                            )
                            continue
                        emit(
                            "ActivityPlanSelected",
                            {
                                "activity_id": activity_id,
                                "template_id": selected_activity["template_id"],
                                "location": selected_activity["location"],
                                "substitution_reason": selection_reason,
                                "rule_version": self.life_evolution.RULE_VERSION,
                            },
                        )
                        emit(
                            "ActivitySelected",
                            {
                                "activity_id": activity_id,
                                "template_id": selected_activity["template_id"],
                                "reason": selection_reason or "primary_template",
                                "rule_version": self.life_simulation.RULE_VERSION,
                            },
                        )
                        activity = _as_dict(
                            _as_dict(working["agenda"], "agenda")[activity_id],
                            "selected weekly activity",
                        )
                else:
                    schedule_item = raw
                    activity = {
                        "activity_id": activity_id,
                        "entity_id": "zhizhi",
                        "title": str(schedule_item["title"]),
                        "template_id": str(schedule_item.get("template_id") or ""),
                        "location": str(schedule_item.get("location") or ""),
                        "starts_at": starts.isoformat(),
                        "ends_at": ends.isoformat(),
                        "attention_demand": int(schedule_item.get("attention_demand", 35)),
                        "interruptible": bool(schedule_item.get("interruptible", True)),
                    }
                    if str(schedule_item.get("kind") or "") == "rest":
                        activity["activity_kind"] = "rest"
                        activity["rest_recovery"] = int(schedule_item.get("rest_recovery", 8))
                        emit("ActivityPlanned", activity)
                        if ends <= target_at:
                            emit("ActivityStarted", {"activity_id": activity_id})
                            emit(
                                "ActivityRested",
                                {
                                    "activity_id": activity_id,
                                    "reason": "scheduled_rest_completed",
                                    "energy_delta": int(schedule_item.get("rest_recovery", 8)),
                                },
                            )
                            previous = dict(_as_dict(_as_dict(working["agenda"], "agenda")[activity_id], "activity"))
                        else:
                            emit("ActivityStarted", {"activity_id": activity_id})
                        continue

                    alternatives = [
                        str(value)
                        for value in _as_list(
                            schedule_item.get("fallback_templates", []),
                            "fallback templates",
                        )
                    ]
                    life_evolution = _as_dict(
                        working.get("life_evolution", {}), "life evolution"
                    )
                    chronic = _as_dict(
                        life_evolution.get("chronic", {}), "chronic life pressure"
                    )
                    active_influences = [
                        item
                        for item in _as_dict(
                            life_evolution.get("influences", {}), "life influences"
                        ).values()
                        if _as_dict(item, "life influence").get("status") == "active"
                    ]
                    primary_template_id = str(activity.get("template_id") or "")
                    use_evolution_scoring = (
                        bool(alternatives)
                        or primary_template_id
                        not in _as_dict(
                            working.get("life_outcome_templates", {}),
                            "life outcome templates",
                        )
                        or bool(active_influences)
                        or any(
                            int(chronic.get(key, 0)) > 0
                            for key in ("fatigue", "relationship_pressure")
                        )
                    )
                    assessments = []
                    priority_scores = None
                    if use_evolution_scoring:
                        candidate_ids = list(
                            dict.fromkeys(
                                [str(activity.get("template_id") or ""), *alternatives]
                            )
                        )
                        assessments = [
                            self.life_evolution.score_candidate(
                                working, activity, candidate_id
                            )
                            for candidate_id in candidate_ids
                        ]
                        priority_scores = {
                            assessment.template_id: assessment.score
                            for assessment in assessments
                        }
                    activity, substitution_reason = self.life_simulation.choose_template(
                        working,
                        activity,
                        alternatives,
                        priority_scores=priority_scores,
                    )
                    if substitution_reason:
                        activity["substitution_reason"] = substitution_reason
                    emit("ActivityPlanned", activity)
                    for assessment in assessments:
                        emit(
                            "ActivityCandidateEvaluated",
                            {
                                "activity_id": activity_id,
                                "template_id": assessment.template_id,
                                "eligible": assessment.eligible,
                                "score": assessment.score,
                                "reasons": list(assessment.reasons),
                                "rejected_reasons": list(
                                    assessment.rejected_reasons
                                ),
                                "selected": substitution_reason
                                != "no_eligible_template"
                                and assessment.template_id
                                == activity["template_id"],
                                "rule_version": self.life_evolution.RULE_VERSION,
                            },
                        )
                    if substitution_reason == "no_eligible_template":
                        if bool(schedule_item.get("rest_when_unavailable")):
                            emit(
                                "ActivityRested",
                                {
                                    "activity_id": activity_id,
                                    "reason": "no_eligible_seeded_activity",
                                    "energy_delta": int(schedule_item.get("rest_recovery", 8)),
                                },
                            )
                        else:
                            emit(
                                "ActivityDeferred",
                                {
                                    "activity_id": activity_id,
                                    "reason": "no_eligible_seeded_activity",
                                    "next_review_at": (ends + timedelta(hours=int(schedule_item.get("review_after_hours", 4)))).isoformat(),
                                },
                            )
                        continue
                    emit(
                        "ActivitySelected",
                        {
                            "activity_id": activity_id,
                            "template_id": activity["template_id"],
                            "reason": substitution_reason or "primary_template",
                            "rule_version": self.life_simulation.RULE_VERSION,
                        },
                    )

                if previous:
                    gap_minutes = int((starts - _parse_at(str(previous["ends_at"]))).total_seconds() // 60)
                    travel_minutes = self._travel_minutes(
                        working,
                        str(previous.get("location") or ""),
                        str(activity.get("location") or ""),
                    )
                    if travel_minutes > max(0, gap_minutes):
                        emit(
                            "ActivityDeferred",
                            {
                                "activity_id": activity_id,
                                "reason": "travel_time_conflict",
                                "next_review_at": (ends + timedelta(hours=2)).isoformat(),
                            },
                        )
                        continue

                if activity.get("status") == "planned" or not existing:
                    emit("ActivityStarted", {"activity_id": activity_id})
                if ends <= target_at:
                    emit("ActivityCompleted", {"activity_id": activity_id})
                    completed = dict(_as_dict(_as_dict(working["agenda"], "agenda")[activity_id], "activity"))
                    outcome_events = self.life_simulation.advance(working, [completed])
                    if any(
                        outcome_type == "ExperienceAppraised"
                        for outcome_type, _ in outcome_events
                    ):
                        expire_threads_until(ends)
                        decay_affect_until(ends)
                    for outcome_type, outcome_payload in outcome_events:
                        if outcome_type == "ExperienceAppraised":
                            npc_id = str(outcome_payload.get("npc_id") or "")
                            goal_id = str(outcome_payload.get("goal_id") or "")
                            npc_relationship = _as_dict(
                                _as_dict(
                                    working.get("relationships", {}), "relationships"
                                ).get(npc_id, {}),
                                "npc relationship",
                            ) if npc_id else {}
                            goal = _as_dict(
                                _as_dict(working.get("goals", {}), "goals").get(
                                    goal_id, {}
                                ),
                                "affect goal",
                            ) if goal_id else {}
                            cognitive_appraisal = appraise_committed_life_outcome(
                                outcome_payload,
                                needs=_as_dict(working.get("needs", {}), "needs"),
                                npc_relationship=npc_relationship,
                                goal_importance=int(
                                    goal.get("importance")
                                    or goal.get("priority")
                                    or 50 if goal_id else 0
                                ),
                            )
                            outcome_payload = {
                                **outcome_payload,
                                "dimensions": cognitive_appraisal.payload(),
                                "rule_version": cognitive_appraisal.rule_version,
                            }
                            emit(outcome_type, outcome_payload)
                            affect = apply_appraisal(
                                _as_dict(
                                    working["emotion_modulation"],
                                    "emotion modulation",
                                ),
                                str(outcome_payload["appraisal"]),
                                ends.isoformat(),
                                source_reference=str(
                                    outcome_payload["source_reference"]
                                ),
                                intensity=cognitive_appraisal.salience,
                                target=(
                                    f"npc:{outcome_payload['npc_id']}"
                                    if outcome_payload.get("npc_id")
                                    else f"goal:{outcome_payload['goal_id']}"
                                    if outcome_payload.get("goal_id")
                                    else "world"
                                ),
                                appraisal_dimensions={
                                    **cognitive_appraisal.payload(),
                                    "program_target": (
                                        "valued_relationship"
                                        if npc_id and cognitive_appraisal.relationship_value >= 60
                                        else "goal"
                                        if goal_id
                                        else "world"
                                    ),
                                },
                            )
                            emit(
                                "AffectChanged",
                                affect_outcome_payload(
                                    affect,
                                    logical_at=ends.isoformat(),
                                    event_type="AffectChanged",
                                ),
                                logical_at=ends.isoformat(),
                            )
                        else:
                            emit(outcome_type, outcome_payload)
                    previous = dict(_as_dict(_as_dict(working["agenda"], "agenda")[activity_id], "activity"))

            for action_id, action in _as_dict(working["actions"], "actions").items():
                item = _as_dict(action, "action")
                if (
                    item["status"] == "scheduled"
                    and item.get("expires_at")
                    and _parse_at(str(item["expires_at"])) <= target_at
                ):
                    emit("ActionExpired", {"action_id": action_id, "reason": "logical_timeout"})
                    for event_type, event_payload in self._release_trace_private_commitment(
                        item, reason="outgoing_action_expired"
                    ):
                        emit(event_type, event_payload)
            expire_threads_until(target_at)
            threads = sorted(
                _as_dict(working.get("conversation_threads", {}), "conversation threads").items(),
                key=lambda item: str(_as_dict(item[1], "conversation thread").get("expires_at") or ""),
            )
            for thread_id, thread in threads:
                item = _as_dict(thread, "conversation thread")
                if (
                    item.get("status") == "open"
                    and item.get("rule_version") == "conversation-commitments-v1"
                ):
                    expires_at = _parse_at(str(item["expires_at"]))
                    waiting_at = min(target_at, expires_at)
                    user_id = str(item.get("user_id") or "")
                    relationship = _as_dict(
                        _as_dict(
                            working.get("relationships", {}), "relationships"
                        ).get(user_id, {}),
                        "thread relationship",
                    )
                    waiting = evaluate_waiting_response(
                        item,
                        relationship=relationship,
                        logical_at=waiting_at,
                    )
                    if waiting.phase != str(item.get("waiting_phase") or "not_due"):
                        waiting_payload: dict[str, object] = {
                            "thread_id": thread_id,
                            "phase": waiting.phase,
                            "reason": waiting.reason,
                            "expression_policy": waiting.expression_policy,
                            "relationship_deltas": dict(waiting.relationship_deltas),
                            "next_review_at": (
                                waiting.next_review_at.isoformat()
                                if waiting.next_review_at
                                else None
                            ),
                            "rule_version": waiting.rule_version,
                        }
                        emit(
                            "ConversationThreadWaitingChanged",
                            waiting_payload,
                            logical_at=waiting_at.isoformat(),
                        )
                        for dimension, delta in waiting.relationship_deltas.items():
                            emit(
                                "RelationshipChanged",
                                {
                                    "entity_id": user_id,
                                    "dimension": dimension,
                                    "delta": delta,
                                    "reason": waiting.reason,
                                    "thread_id": thread_id,
                                    "rule_version": waiting.rule_version,
                                },
                                logical_at=waiting_at.isoformat(),
                            )
            for goal_id, goal in list(_as_dict(working.get("goals", {}), "goals").items()):
                if goal.get("status") == "active" and goal.get("deadline") and _parse_at(str(goal["deadline"])) <= target_at:
                    emit("GoalDeferred", {"goal_id": goal_id, "reason": "deadline_reached", "next_review_at": (target_at + timedelta(days=1)).isoformat()})
                elif goal.get("status") == "deferred" and goal.get("next_review_at") and _parse_at(str(goal["next_review_at"])) <= target_at:
                    emit("GoalReviewDue", {"goal_id": goal_id})
            decay_affect_until(target_at)
            return events
        if command_type == "materialize_weekly_plan":
            week_start = _parse_at(str(command.get("week_start") or ""))
            return self.life_evolution.plan_week(state, week_start=week_start)
        if command_type == "record_life_influence":
            influence_id = str(command.get("influence_id") or "")
            influences = _as_dict(
                _as_dict(state.get("life_evolution", {}), "life evolution").get(
                    "influences", {}
                ),
                "life influences",
            )
            if not influence_id or influence_id in influences:
                raise WorldError("life influence requires a new id")
            try:
                return self.life_evolution.events_for_user_influence(
                    state,
                    influence_id=influence_id,
                    kind=str(command.get("kind") or ""),
                    observed_at=_parse_at(str(command.get("observed_at") or "")),
                    expires_at=_parse_at(str(command.get("expires_at") or "")),
                    source_message_id=(
                        str(command["source_message_id"])
                        if command.get("source_message_id")
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WorldError(str(exc)) from exc
        if command_type == "record_environment_observation":
            observation_id = str(command.get("observation_id") or "")
            observations = _as_dict(
                _as_dict(state.get("life_evolution", {}), "life evolution").get(
                    "observations", {}
                ),
                "environment observations",
            )
            if not observation_id or observation_id in observations:
                raise WorldError("environment observation requires a new id")
            try:
                return self.life_evolution.environment_observation_events(
                    observation_id=observation_id,
                    category=str(command.get("category") or ""),
                    value=str(command.get("value") or ""),
                    source_id=str(command.get("source_id") or ""),
                    observed_at=_parse_at(str(command.get("observed_at") or "")),
                    expires_at=_parse_at(str(command.get("expires_at") or "")),
                    confidence=float(command.get("confidence") or 0.0),
                    confirmed_current=bool(command.get("confirmed_current")),
                )
            except (TypeError, ValueError) as exc:
                raise WorldError(str(exc)) from exc
        if command_type == "record_life_pressure":
            try:
                return self.life_evolution.pressure_events(
                    state,
                    sample_id=str(command.get("sample_id") or ""),
                    week_start=_parse_at(str(command.get("week_start") or "")),
                    fatigue=int(command.get("fatigue") or 0),
                    relationship_pressure=int(
                        command.get("relationship_pressure") or 0
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise WorldError(str(exc)) from exc
        if command_type == "register_npc":
            npc = _as_dict(command.get("npc"), "npc")
            if not npc.get("id") or not npc.get("name") or npc["id"] in _as_dict(state["entities"], "entities"):
                raise WorldError("NPC must have a new id and name")
            return [("NpcRegistered", npc)]
        if command_type == "register_user":
            user_id = str(command.get("user_id") or "")
            name = str(command.get("name") or "").strip()
            entities = _as_dict(state["entities"], "entities")
            if not user_id or not name or user_id in entities:
                raise WorldError("user must have a new id and name")
            protagonist = next(
                (
                    _as_dict(raw, "protagonist")
                    for raw in entities.values()
                    if _as_dict(raw, "entity").get("kind") == "companion"
                ),
                {},
            )
            return [
                ("UserRegistered", {"id": user_id, "name": name, "kind": "user"}),
                (
                    "RelationshipStageEvaluated",
                    stage_event_payload(
                        entity_id=user_id,
                        stage="stranger",
                        from_stage=None,
                        relationship={"interaction_count": 0},
                        boundary=0,
                        reason="relationship_initialized",
                        slow_warmth=relationship_slow_warmth(protagonist),
                    ),
                ),
            ]
        if command_type == "plan_activity":
            payload = {key: command[key] for key in ("activity_id", "entity_id", "title", "starts_at", "ends_at")}
            if any(not payload.get(key) for key in payload) or _parse_at(str(payload["ends_at"])) <= _parse_at(str(payload["starts_at"])):
                raise WorldError("activity needs id, entity, title, and increasing times")
            if payload["entity_id"] not in _as_dict(state["entities"], "entities"):
                raise WorldError("activity entity is not registered")
            if payload["activity_id"] in _as_dict(state["agenda"], "agenda"):
                raise WorldError("activity id already exists")
            for existing in _as_dict(state["agenda"], "agenda").values():
                if existing["entity_id"] == payload["entity_id"] and existing["status"] in {"planned", "active"}:
                    overlaps = _parse_at(str(payload["starts_at"])) < _parse_at(str(existing["ends_at"])) and _parse_at(str(existing["starts_at"])) < _parse_at(str(payload["ends_at"]))
                    if overlaps:
                        raise WorldError("activity conflicts with an existing world commitment")
            return [("ActivityPlanned", payload)]
        if command_type == "schedule_action":
            action_id = str(command.get("action_id") or "")
            expires_at = str(command.get("expires_at") or "")
            if not action_id or not expires_at or action_id in _as_dict(state["actions"], "actions"):
                raise WorldError("action requires a new id and expiry")
            return [
                (
                    "ActionScheduled",
                    {
                        "action_id": action_id,
                        "kind": str(command.get("kind") or "generic"),
                        "expires_at": expires_at,
                        "payload": _as_dict(command.get("payload", {}), "action payload"),
                    },
                )
            ]
        if command_type == "claim_external_action":
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            if action.get("status") != "scheduled":
                raise WorldError("only a scheduled external action can be claimed")
            lease_expires_observed_at = str(command.get("lease_expires_observed_at") or "")
            if not lease_expires_observed_at:
                raise WorldError("external action claim requires an observed-time lease")
            return [
                ("ActionAttempted", {"action_id": action_id}),
                (
                    "ActionDispatchClaimed",
                    {
                        "action_id": action_id,
                        "lease_expires_observed_at": lease_expires_observed_at,
                    },
                ),
            ]
        if command_type == "recover_expired_external_leases":
            observed_now = _parse_at(str(command.get("observed_now") or ""))
            events: list[tuple[str, dict[str, object]]] = []
            for action_id, raw in _as_dict(state["actions"], "actions").items():
                action = _as_dict(raw, "action")
                lease = str(action.get("lease_expires_observed_at") or "")
                if (
                    action.get("status") == "sending"
                    and lease
                    and _parse_at(lease) <= observed_now
                ):
                    events.append(
                        (
                            "ActionSettled",
                            {
                                "action_id": action_id,
                                "result": {
                                    "status": "failed",
                                    "reason": "external_lease_expired",
                                    "observed_at": observed_now.isoformat(),
                                },
                            },
                        )
                    )
                    causation = str(_as_dict(action.get("payload", {}), "action payload").get("causation") or "")
                    if causation and _as_dict(state.get("intents", {}), "intents").get(causation, {}).get("status") == "open":
                        intent = _as_dict(state["intents"], "intents")[causation]
                        events.append(
                            (
                                "IntentFailed",
                                {"intent_id": causation, "reason": "external_lease_expired"},
                            )
                        )
                        if intent.get("message_id"):
                            events.append(
                                (
                                    "TurnProcessingSettled",
                                    {
                                        "message_id": intent["message_id"],
                                        "status": "failed",
                                        "reason": "external_lease_expired",
                                    },
                                )
                            )
            return events
        if command_type == "defer_message_reply":
            message_id = str(command.get("message_id") or "")
            action_id = str(command.get("action_id") or f"reply_later:{message_id}")
            due_at = str(command.get("due_at") or "")
            expires_at = str(command.get("expires_at") or "")
            reason = str(command.get("reason") or "").strip()
            known_message_ids = {
                str(_as_dict(item, "recent message").get("message_id") or "")
                for item in _as_list(state.get("recent_messages", []), "recent messages")
            }
            now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            if (
                not message_id
                or message_id not in known_message_ids
                or not reason
                or not due_at
                or not expires_at
                or _parse_at(due_at) <= now
                or _parse_at(expires_at) <= _parse_at(due_at)
                or action_id in _as_dict(state["actions"], "actions")
            ):
                raise WorldError("deferred reply requires one observed message, a future due time, and a new action")
            return [
                (
                    "ActionScheduled",
                    {
                        "action_id": action_id,
                        "kind": "reply_later",
                        "expires_at": expires_at,
                        "payload": {
                            "due_at": due_at,
                            "message_id": message_id,
                            "message": _as_dict(command.get("message", {}), "deferred message"),
                            "reason": reason,
                        },
                    },
                ),
                (
                    "MessageAttentionDecided",
                    {
                        "message_id": message_id,
                        "attention": "deferred",
                        "reason": reason,
                        "due_at": due_at,
                        "deferred_action_id": action_id,
                        "rule_version": str(command.get("rule_version") or ""),
                        "candidates": list(_as_list(command.get("candidates", []), "attention candidates")),
                    },
                ),
            ]
        if command_type == "set_message_attention":
            message_id = str(command.get("message_id") or "")
            attention = str(command.get("attention") or "")
            reason = str(command.get("reason") or "").strip()
            known_message_ids = {
                str(_as_dict(item, "recent message").get("message_id") or "")
                for item in _as_list(state.get("recent_messages", []), "recent messages")
            }
            if not message_id or message_id not in known_message_ids:
                raise WorldError("message attention requires an observed message")
            if attention not in {"seen", "deferred", "do_not_disturb"} or not reason:
                raise WorldError("message attention requires a supported attention state and reason")
            communication = _as_dict(state["communication"], "communication")
            prior_action_id = str(communication.get("deferred_action_id") or "")
            events: list[tuple[str, dict[str, object]]] = []
            preserve_action_id = str(command.get("preserve_action_id") or "")
            if prior_action_id and prior_action_id != preserve_action_id:
                prior = _as_dict(_as_dict(state["actions"], "actions").get(prior_action_id), "deferred attention action")
                if prior.get("status") == "scheduled":
                    events.append(("ActionCancelled", {"action_id": prior_action_id, "reason": "attention_reconsidered"}))
            payload: dict[str, object] = {
                "message_id": message_id, "attention": attention, "reason": reason,
                "due_at": None, "deferred_action_id": None,
                "rule_version": str(command.get("rule_version") or ""),
                "candidates": list(_as_list(command.get("candidates", []), "attention candidates")),
            }
            if attention == "deferred":
                due_at = str(command.get("due_at") or "")
                now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
                if not due_at or _parse_at(due_at) <= now:
                    raise WorldError("deferred message attention requires a future logical due_at")
                action_id = f"attention:{message_id}"
                if action_id in _as_dict(state["actions"], "actions"):
                    raise WorldError("message attention was already deferred")
                payload["due_at"] = due_at
                payload["deferred_action_id"] = action_id
                events.append((
                    "ActionScheduled",
                    {
                        "action_id": action_id, "kind": "message_attention",
                        "expires_at": ( _parse_at(due_at) + timedelta(hours=12) ).isoformat(),
                        "payload": {"due_at": due_at, "message_id": message_id, "reason": reason},
                    },
                ))
            events.append(("MessageAttentionDecided", payload))
            return events
        if command_type == "set_typing_state":
            message_id = str(command.get("message_id") or "")
            typing = str(command.get("typing") or "")
            reason = str(command.get("reason") or "").strip()
            communication = _as_dict(state["communication"], "communication")
            if message_id != str(communication.get("message_id") or ""):
                raise WorldError("typing state requires the current observed message")
            if not reason or typing not in {"started", "stopped"}:
                raise WorldError("typing state requires started or stopped and a reason")
            if typing == "started" and communication.get("attention") != "seen":
                raise WorldError("typing can start only for a seen message")
            if typing == "started" and communication.get("typing") != "idle":
                raise WorldError("typing is already active")
            if typing == "stopped" and communication.get("typing") != "started":
                raise WorldError("typing can stop only after it started")
            return [("TypingStateChanged", {"message_id": message_id, "typing": typing, "reason": reason})]
        if command_type == "select_reaction":
            message_id = str(command.get("message_id") or "")
            reaction_id = str(command.get("reaction_id") or "").strip()
            platform = str(command.get("platform") or "").strip()
            known_message_ids = {
                str(_as_dict(item, "recent message").get("message_id") or "")
                for item in _as_list(state.get("recent_messages", []), "recent messages")
                if _as_dict(item, "recent message").get("direction") == "in"
            }
            action_id = f"reaction:{platform}:{message_id}:{reaction_id}"
            if (
                not message_id
                or message_id not in known_message_ids
                or not reaction_id
                or len(reaction_id) > 64
                or not platform
                or action_id in _as_dict(state["actions"], "actions")
            ):
                raise WorldError("reaction selection requires an observed message and new bounded reaction")
            return [
                (
                    "ReactionSelected",
                    {
                        "action_id": action_id,
                        "message_id": message_id,
                        "reaction_id": reaction_id,
                        "platform": platform,
                        "rule_version": "world-reaction-v1",
                    },
                ),
                (
                    "ActionScheduled",
                    {
                        "action_id": action_id,
                        "kind": "reaction_delivery",
                        "expires_at": (
                            _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
                            + timedelta(hours=2)
                        ).isoformat(),
                        "payload": {
                            "message_id": message_id,
                            "reaction_id": reaction_id,
                            "platform": platform,
                        },
                    },
                ),
            ]
        if command_type == "defer_decision":
            decision_id = str(command.get("decision_id") or "")
            kind = str(command.get("kind") or "")
            reason = str(command.get("reason") or "").strip()
            review_at = str(command.get("review_at") or "")
            decisions = _as_dict(state["decisions"], "decisions")
            now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            if not decision_id or decision_id in decisions or not kind or not reason or len(reason) > 160 or not review_at or _parse_at(review_at) <= now:
                raise WorldError("deferred decision requires a new id, bounded reason, and future review time")
            action_id = f"decision:{decision_id}"
            return [
                ("DecisionDeferred", {"decision_id": decision_id, "kind": kind, "reason": reason, "review_at": review_at, "action_id": action_id}),
                ("ActionScheduled", {"action_id": action_id, "kind": "decision_review", "expires_at": (_parse_at(review_at) + timedelta(hours=12)).isoformat(), "payload": {"due_at": review_at, "decision_id": decision_id}}),
            ]
        if command_type == "deliberate_proactive":
            impulse_id = str(command.get("impulse_id") or "").strip()
            user_id = str(command.get("user_id") or "").strip()
            generation_action_id = str(command.get("generation_action_id") or "").strip()
            user = _as_dict(
                _as_dict(state["entities"], "entities").get(user_id), "proactive user"
            )
            if not impulse_id or not generation_action_id or user.get("kind") != "user":
                raise WorldError("proactive deliberation requires an impulse and registered user")
            if any(
                _as_dict(raw, "action").get("kind") == "proactive_generation"
                and _as_dict(raw, "action").get("status") in {"scheduled", "sending"}
                for raw in _as_dict(state["actions"], "actions").values()
            ):
                raise WorldError("a proactive generation is already in progress")
            protagonist = next(
                (
                    _as_dict(raw, "protagonist")
                    for raw in _as_dict(state["entities"], "entities").values()
                    if _as_dict(raw, "entity").get("kind") == "companion"
                ),
                {},
            )
            affect = _as_dict(state["emotion_modulation"], "emotion modulation")
            vector = _as_dict(affect.get("vector", {}), "affect vector")
            deliberation = self.character_deliberation.decide(
                situation={
                    "kind": "proactive",
                    "text": "",
                    "risk": "low",
                    "causation_ids": (impulse_id,),
                },
                self_core=protagonist,
                relationship=_as_dict(
                    _as_dict(state["relationships"], "relationships").get(user_id, {}),
                    "relationship",
                ),
                affect={"irritation": int(vector.get("anger", 0)), **vector},
                needs=_as_dict(state["needs"], "needs"),
                user_request=UserRequest(kind="unspecified", strength="implicit"),
                open_commitments=tuple(
                    thread_id
                    for thread_id, raw in _as_dict(
                        state.get("conversation_threads", {}), "threads"
                    ).items()
                    if _as_dict(raw, "thread").get("status") == "open"
                    and _as_dict(raw, "thread").get("user_id") == user_id
                ),
                available_actions=("initiate", "defer", "remain_silent"),
            )
            events: list[tuple[str, dict[str, object]]] = [
                (
                    "MotiveConflictEvaluated",
                    {
                        "message_id": impulse_id,
                        "appraisal": deliberation.appraisal,
                        "drives": dict(deliberation.drives),
                        "conflicts": list(deliberation.conflicts),
                        "stances_considered": list(deliberation.stances_considered),
                        "rule_version": deliberation.rule_version,
                    },
                ),
                (
                    "StanceSelected",
                    {
                        "message_id": impulse_id,
                        "stance": deliberation.chosen_stance,
                        "display_strategy": deliberation.display_strategy,
                        "drives": dict(deliberation.drives),
                        "conflicts": list(deliberation.conflicts),
                        "action_candidates": list(deliberation.action_candidates),
                        "selection_mode": deliberation.selection.mode,
                        "rule_version": deliberation.rule_version,
                    },
                ),
            ]
            if deliberation.chosen_stance == "initiate":
                due_at = _parse_at(
                    str(_as_dict(state["clock"], "clock")["logical_at"])
                ) + timedelta(minutes=10)
                events.append(
                    (
                        "ActionScheduled",
                        {
                            "action_id": generation_action_id,
                            "kind": "proactive_generation",
                            "expires_at": (due_at + timedelta(minutes=5)).isoformat(),
                            "payload": {
                                "due_at": due_at.isoformat(),
                                "user_id": user_id,
                                "impulse_id": impulse_id,
                            },
                        },
                    )
                )
            return events
        if command_type == "resolve_deferred_decision":
            decision_id = str(command.get("decision_id") or "")
            outcome = str(command.get("outcome") or "")
            reason = str(command.get("reason") or "").strip()
            decision = _as_dict(_as_dict(state["decisions"], "decisions").get(decision_id), "deferred decision")
            if decision.get("status") != "deferred" or outcome not in {"abandoned", "resumed"} or not reason:
                raise WorldError("only a deferred decision can be resolved as abandoned or resumed")
            action_id = str(decision["action_id"])
            events: list[tuple[str, dict[str, object]]] = [("DecisionResolved", {"decision_id": decision_id, "outcome": outcome, "reason": reason})]
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "decision review action")
            if action.get("status") == "scheduled":
                events.append(("ActionCancelled", {"action_id": action_id, "reason": "decision_resolved"}))
            return events
        if command_type == "open_conversation_thread":
            raw_thread = _as_dict(command.get("thread"), "conversation thread")
            thread_id = str(raw_thread.get("thread_id") or "")
            user_id = str(raw_thread.get("user_id") or "")
            if thread_id in _as_dict(
                state.get("conversation_threads", {}), "conversation threads"
            ):
                raise WorldError("conversation thread id already exists")
            user = _as_dict(
                _as_dict(state.get("entities", {}), "entities").get(user_id),
                "conversation thread user",
            )
            if user.get("kind") != "user":
                raise WorldError("conversation thread user must be registered")
            return [
                (
                    "ConversationThreadOpened",
                    _conversation_thread_event_payload(
                        raw_thread,
                        source_action_id=None,
                        logical_at=_parse_at(
                            str(_as_dict(state["clock"], "clock")["logical_at"])
                        ),
                    ),
                )
            ]
        if command_type == "resolve_conversation_thread":
            thread_id = str(command.get("thread_id") or "")
            outcome = str(command.get("outcome") or "")
            reason = str(command.get("reason") or "").strip()
            thread = _as_dict(_as_dict(state.get("conversation_threads", {}), "conversation threads").get(thread_id), "conversation thread")
            if thread.get("status") != "open" or outcome not in {"answered", "skipped", "meta"} or not reason:
                raise WorldError("only an open conversation thread can be resolved with a classified user response")
            return [
                ("ConversationThreadResolved", {"thread_id": thread_id, "outcome": outcome, "reason": reason[:160]}),
                *self._thread_commitment_resolution_events(
                    state,
                    thread_id=thread_id,
                    outcome="fulfilled" if outcome == "answered" else "released",
                    reason=f"conversation_thread_{outcome}",
                ),
            ]
        if command_type == "cancel_conversation_thread":
            thread_id = str(command.get("thread_id") or "")
            condition = str(command.get("condition") or "")
            reason = str(command.get("reason") or "").strip()
            thread = _as_dict(
                _as_dict(
                    state.get("conversation_threads", {}), "conversation threads"
                ).get(thread_id),
                "conversation thread",
            )
            conditions = {
                str(item)
                for item in _as_list(
                    thread.get("cancel_conditions", []), "thread cancel conditions"
                )
            }
            if thread.get("status") != "open" or not reason:
                raise WorldError("only an open conversation thread can be cancelled")
            if condition not in conditions:
                raise WorldError("conversation thread cancel condition is not declared")
            return [
                (
                    "ConversationThreadCancelled",
                    {
                        "thread_id": thread_id,
                        "condition": condition,
                        "reason": reason[:160],
                    },
                ),
                *self._thread_commitment_resolution_events(
                    state,
                    thread_id=thread_id,
                    outcome="released",
                    reason="conversation_thread_cancelled",
                ),
            ]
        if command_type == "cancel_action":
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            if action["status"] != "scheduled":
                raise WorldError("only a scheduled action can be cancelled")
            return [("ActionCancelled", {"action_id": action_id, "reason": str(command.get("reason") or "cancelled")})]
        if command_type == "review_activity":
            activity_id = str(command.get("activity_id") or "")
            decision = str(command.get("decision") or "")
            activity = _as_dict(_as_dict(state["agenda"], "agenda").get(activity_id), "activity")
            if activity.get("status") != "deferred":
                raise WorldError("only a deferred activity can be reviewed")
            if decision == "resume":
                return [("ActivityResumed", {"activity_id": activity_id})]
            if decision == "cancel":
                return [("ActivityCancelled", {"activity_id": activity_id, "reason": str(command.get("reason") or "review_cancelled")})]
            if decision == "rest":
                return [("ActivityRested", {"activity_id": activity_id, "reason": "review_rest", "energy_delta": int(command.get("energy_delta") or 6)})]
            raise WorldError("activity review decision must be resume, cancel, or rest")
        if command_type == "change_relationship":
            entity_id = str(command.get("entity_id") or "")
            dimension = str(command.get("dimension") or "")
            entities = _as_dict(state["entities"], "entities")
            if entity_id not in entities or dimension not in {"trust", "closeness", "respect"}:
                raise WorldError("relationship change requires a registered entity and supported dimension")
            delta = int(command.get("delta") or 0)
            events: list[tuple[str, dict[str, object]]] = [
                (
                    "NpcRelationshipChanged",
                    {"entity_id": entity_id, "dimension": dimension, "delta": delta},
                )
            ]
            if _as_dict(entities[entity_id], "relationship entity").get("kind") == "user":
                protagonist = next(
                    (
                        _as_dict(raw, "protagonist")
                        for raw in entities.values()
                        if _as_dict(raw, "entity").get("kind") == "companion"
                    ),
                    {},
                )
                slow_warmth = relationship_slow_warmth(protagonist)
                relation = dict(
                    _as_dict(
                        _as_dict(state["relationships"], "relationships").get(entity_id, {}),
                        "user relationship",
                    )
                )
                relation[dimension] = max(
                    -100, min(100, int(relation.get(dimension) or 0) + delta)
                )
                boundary = int(_as_dict(state["needs"], "needs").get("boundary", 0))
                stage, reason = evaluate_relationship_stage(
                    relation,
                    boundary=boundary,
                    slow_warmth=slow_warmth,
                )
                events.append(
                    (
                        "RelationshipStageEvaluated",
                        stage_event_payload(
                            entity_id=entity_id,
                            stage=stage,
                            from_stage=str(relation.get("stage") or "stranger"),
                            relationship=relation,
                            boundary=boundary,
                            reason=reason,
                            slow_warmth=slow_warmth,
                        ),
                    )
                )
            return events
        if command_type == "change_need":
            need = str(command.get("need") or "")
            if need not in {"energy", "attention", "security", "initiative", "boundary"}:
                raise WorldError("unsupported world need")
            return [("NeedChanged", {"need": need, "delta": int(command.get("delta") or 0)})]
        if command_type == "propose_character_core_change":
            protagonist = next(
                (
                    _as_dict(raw, "protagonist")
                    for raw in _as_dict(state["entities"], "entities").values()
                    if _as_dict(raw, "entity").get("kind") == "companion"
                ),
                {},
            )
            proposal = CoreChangeProposal(
                proposal_id=str(command.get("proposal_id") or ""),
                operation=str(command.get("operation") or ""),  # type: ignore[arg-type]
                field=str(command.get("field") or ""),
                value=str(command.get("value") or ""),
                evidence_ids=tuple(
                    str(item) for item in _as_list(command.get("evidence_ids", []), "core evidence ids")
                ),
                reason=str(command.get("reason") or ""),
            )
            sources: dict[str, dict[str, object]] = {}
            experiences = _as_dict(state.get("experiences", {}), "experiences")
            outcomes = _as_dict(state.get("outcomes", {}), "outcomes")
            goals = _as_dict(state.get("goals", {}), "goals")
            for evidence_id in proposal.evidence_ids:
                if evidence_id in experiences:
                    experience = _as_dict(experiences[evidence_id], "experience")
                    outcome = _as_dict(
                        outcomes.get(str(experience.get("source_outcome_id") or ""), {}),
                        "experience outcome",
                    )
                    sources[evidence_id] = {
                        "source_id": evidence_id,
                        "source_type": "experience",
                        "status": "committed",
                        "core_signal": str(
                            experience.get("core_signal")
                            or experience.get("template_id")
                            or outcome.get("template_id")
                            or experience.get("source_outcome_id")
                            or "lived_experience"
                        ),
                        "significant": bool(experience.get("significant")),
                    }
                elif evidence_id in goals:
                    goal = _as_dict(goals[evidence_id], "goal")
                    sources[evidence_id] = {
                        "source_id": evidence_id,
                        "source_type": "goal_outcome",
                        "status": str(goal.get("status") or ""),
                        "core_signal": f"goal:{evidence_id}",
                        "significant": goal.get("status") == "completed",
                    }
            try:
                decision = evaluate_core_change(protagonist, proposal, sources)
            except CharacterCoreEvolutionError as exc:
                raise WorldError(str(exc)) from exc
            core_payload = {
                **decision.event_payload(),
                "entity_id": str(protagonist.get("id") or ""),
            }
            if decision.accepted:
                return [("CharacterCoreChanged", core_payload)]
            return [("CharacterCoreChangeRejected", core_payload)]
        if command_type == "propose_tool_action":
            proposal_id = str(command.get("proposal_id") or "")
            user_id = str(command.get("user_id") or "")
            tool_name = str(command.get("tool_name") or "").strip()
            arguments = _as_dict(command.get("arguments", {}), "tool arguments")
            summary = str(command.get("summary") or "").strip()
            risk = str(command.get("risk") or "")
            tools = _as_dict(state.get("tool_actions", {}), "tool actions")
            user = _as_dict(
                _as_dict(state["entities"], "entities").get(user_id), "tool user"
            )
            arguments_json = _stable_json(arguments)
            if (
                not proposal_id
                or proposal_id in tools
                or user.get("kind") != "user"
                or not tool_name
                or len(tool_name) > 120
                or not re.fullmatch(r"[a-zA-Z0-9_.:-]+", tool_name)
                or not summary
                or len(summary) > 240
                or len(arguments_json) > 4000
                or risk not in {"read_only", "confirmation_required", "blocked"}
            ):
                raise WorldError(
                    "tool proposal requires a new id, registered user, bounded tool, "
                    "arguments and summary, and supported risk"
                )
            can_authorize = risk != "blocked"
            return [
                (
                    "ToolProposed",
                    {
                        "proposal_id": proposal_id,
                        "user_id": user_id,
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "summary": summary,
                        "risk": risk,
                        "execution_mode": "fake",
                        "message_cursor": len(
                            _as_list(state.get("recent_messages", []), "recent messages")
                        ),
                    },
                ),
                (
                    "UserConfirmationRequired",
                    {
                        "proposal_id": proposal_id,
                        "reason": (
                            "tool_action_requires_explicit_confirmation"
                            if can_authorize
                            else "tool_risk_blocked"
                        ),
                        "can_authorize": can_authorize,
                    },
                ),
            ]
        if command_type in {"authorize_tool_action", "reject_tool_action"}:
            proposal_id = str(command.get("proposal_id") or "")
            confirmation_message_id = str(command.get("confirmation_message_id") or "")
            tools = _as_dict(state.get("tool_actions", {}), "tool actions")
            proposal = _as_dict(tools.get(proposal_id), "tool proposal")
            if (
                proposal.get("status") != "awaiting_confirmation"
                or not confirmation_message_id
            ):
                raise WorldError(
                    "tool authorization decision requires an awaiting proposal and confirmation message"
                )
            history = _as_list(state.get("recent_messages", []), "recent messages")
            cursor = int(proposal.get("message_cursor") or 0)
            confirmation = next(
                (
                    _as_dict(raw, "confirmation message")
                    for raw in history[cursor:]
                    if str(_as_dict(raw, "confirmation message").get("message_id") or "")
                    == confirmation_message_id
                    and str(_as_dict(raw, "confirmation message").get("user_id") or "")
                    == str(proposal["user_id"])
                ),
                None,
            )
            used_confirmation_ids = {
                str(decision.get("confirmation_message_id") or "")
                for raw in tools.values()
                for decision in (
                    _as_dict(raw, "tool action").get("authorization"),
                    _as_dict(raw, "tool action").get("rejection"),
                )
                if isinstance(decision, dict)
            }
            expected_confirmation = (
                "rejected" if command_type == "reject_tool_action" else "authorized"
            )
            if (
                confirmation is None
                or confirmation_message_id in used_confirmation_ids
                or _tool_confirmation_kind(str(confirmation.get("text") or ""))
                != expected_confirmation
            ):
                raise WorldError(
                    "tool decision requires a new observed explicit confirmation from the proposing user"
                )
            if command_type == "reject_tool_action" or proposal.get("risk") == "blocked":
                reason = str(command.get("reason") or "").strip()
                if proposal.get("risk") == "blocked":
                    reason = "tool risk is blocked by policy"
                if not reason or len(reason) > 240:
                    raise WorldError("tool rejection requires a bounded reason")
                return [
                    (
                        "ToolRejected",
                        {
                            "proposal_id": proposal_id,
                            "confirmation_message_id": confirmation_message_id,
                            "reason": reason,
                            "result_summary": f"未执行：{reason}",
                        },
                    )
                ]
            action_id = f"tool:{proposal_id}"
            if action_id in _as_dict(state["actions"], "actions"):
                raise WorldError("tool action is already scheduled")
            logical_now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            return [
                (
                    "ToolAuthorized",
                    {
                        "proposal_id": proposal_id,
                        "action_id": action_id,
                        "confirmation_message_id": confirmation_message_id,
                        "execution_mode": "fake",
                    },
                ),
                (
                    "ActionScheduled",
                    {
                        "action_id": action_id,
                        "kind": "tool_execution",
                        "expires_at": (logical_now + timedelta(minutes=15)).isoformat(),
                        "payload": {
                            "proposal_id": proposal_id,
                            "tool_name": proposal["tool_name"],
                            "arguments": proposal["arguments"],
                            "summary": proposal["summary"],
                            "execution_mode": "fake",
                        },
                    },
                ),
            ]
        if command_type == "request_media":
            request_id = str(command.get("request_id") or "")
            user_id = str(command.get("user_id") or "")
            media_kind = str(command.get("media_kind") or "")
            topic = str(command.get("topic") or "").strip()
            reason = str(command.get("reason") or "").strip()
            media = _as_dict(state.get("media", {}), "media")
            entities = _as_dict(state["entities"], "entities")
            if (
                not request_id or request_id in media or not topic or len(topic) > 120 or not reason or len(reason) > 160
                or media_kind not in {"creative_image", "selfie", "relationship_private"}
                or _as_dict(entities.get(user_id), "media user").get("kind") != "user"
            ):
                raise WorldError("media request requires a registered user, new id, supported kind, bounded topic and reason")
            action_id = f"media-generation:{request_id}"
            now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            return [
                ("MediaRequested", {"request_id": request_id, "user_id": user_id, "media_kind": media_kind, "topic": topic, "reason": reason, "rule_version": str(command.get("rule_version") or "")}),
                ("ActionScheduled", {"action_id": action_id, "kind": "media_generation", "expires_at": (now + timedelta(hours=2)).isoformat(), "payload": {"request_id": request_id, "media_kind": media_kind, "topic": topic}}),
            ]
        if command_type == "reject_media_request":
            request_id = str(command.get("request_id") or "")
            user_id = str(command.get("user_id") or "")
            reason = str(command.get("reason") or "").strip()
            if not request_id or not reason or _as_dict(_as_dict(state["entities"], "entities").get(user_id), "media user").get("kind") != "user":
                raise WorldError("media rejection requires request id, registered user, and reason")
            return [("MediaRequestRejected", {"request_id": request_id, "user_id": user_id, "reason": reason[:160], "rule_version": str(command.get("rule_version") or "")})]
        if command_type == "schedule_media_delivery":
            request_id = str(command.get("request_id") or "")
            media = _as_dict(state.get("media", {}), "media")
            item = _as_dict(media.get(request_id), "media request")
            if item.get("status") != "generated" or not item.get("artifact_path"):
                raise WorldError("only generated media can be scheduled for delivery")
            action_id = f"media-delivery:{request_id}"
            if action_id in _as_dict(state["actions"], "actions"):
                raise WorldError("media delivery is already scheduled")
            now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            return [("ActionScheduled", {"action_id": action_id, "kind": "media_delivery", "expires_at": (now + timedelta(hours=12)).isoformat(), "payload": {"request_id": request_id, "artifact_path": item["artifact_path"], "media_kind": item["media_kind"]}})]
        if command_type == "schedule_sticker_delivery":
            sticker_id = str(command.get("sticker_id") or "")
            sticker_path = str(command.get("sticker_path") or "")
            intent = str(command.get("intent") or "")
            causation = str(command.get("causation_id") or "")
            if not sticker_id or not sticker_path or len(sticker_path) > 500 or not intent or not causation:
                raise WorldError("sticker delivery requires id, bounded path, intent, and causation")
            action_id = f"sticker-delivery:{causation}"
            if action_id in _as_dict(state["actions"], "actions"):
                raise WorldError("sticker delivery is already scheduled")
            now = _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            return [
                ("StickerSelected", {"action_id": action_id, "sticker_id": sticker_id, "sticker_path": sticker_path, "intent": intent, "rule_version": str(command.get("rule_version") or "")}),
                ("ActionScheduled", {"action_id": action_id, "kind": "sticker_delivery", "expires_at": (now + timedelta(hours=12)).isoformat(), "payload": {"sticker_id": sticker_id, "sticker_path": sticker_path, "intent": intent}}),
            ]
        if command_type == "review_goal":
            goal_id = str(command.get("goal_id") or "")
            decision = str(command.get("decision") or "")
            goal = _as_dict(_as_dict(state.get("goals", {}), "goals").get(goal_id), "goal")
            if goal.get("status") not in {"deferred", "review_due"}:
                raise WorldError("only a deferred goal can be reviewed")
            if decision == "resume":
                deadline = str(command.get("deadline") or "")
                if not deadline or _parse_at(deadline) <= _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"])):
                    raise WorldError("resumed goal needs a future logical deadline")
                return [("GoalResumed", {"goal_id": goal_id, "deadline": deadline})]
            if decision == "abandon":
                return [("GoalAbandoned", {"goal_id": goal_id, "reason": str(command.get("reason") or "review_abandoned")}), ("GoalCompensated", {"goal_id": goal_id, "need": "security", "delta": int(command.get("security_delta") or 2)})]
            raise WorldError("goal review decision must be resume or abandon")
        if command_type == "record_external_result":
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            result = _as_dict(command.get("result"), "result")
            status = str(result.get("status") or "")
            if status not in {"delivered", "failed", "cancelled"}:
                raise WorldError("external result requires a terminal status")
            late_reaction_receipt = (
                action.get("status") == "unknown"
                and action.get("kind") == "reaction_delivery"
                and status in {"delivered", "failed"}
                and bool(str(result.get("external_receipt") or "").strip())
            )
            if action["status"] not in {"scheduled", "sending"} and not late_reaction_receipt:
                raise WorldError(
                    "only an unresolved action, or an unknown reaction with a receipt, can settle"
                )
            if action.get("kind") == "attachment_analysis" and status == "delivered":
                payload = _as_dict(action.get("payload", {}), "attachment analysis payload")
                summary = str(result.get("summary") or "")
                if (
                    not summary
                    or len(summary) > 2000
                    or str(result.get("source_message_id") or "") != str(payload.get("source_message_id") or "")
                    or int(result.get("attachment_index", -1)) != int(payload.get("attachment_index", -2))
                ):
                    raise WorldError("attachment analysis result requires bounded summary and matching source")
            if action.get("kind") == "tool_execution":
                detail = str(result.get("detail") or "").strip()
                if (
                    result.get("kind") != "tool_execution"
                    or result.get("execution_mode") != "fake"
                    or result.get("effect_scope") != "none"
                    or not detail
                    or len(detail) > 1000
                ):
                    raise WorldError(
                        "tool result must be a bounded, effect-free fake execution result"
                    )
                _as_dict(result.get("output", {}), "tool result output")
            events: list[tuple[str, dict[str, object]]] = []
            if action["status"] == "scheduled":
                events.append(("ActionAttempted", {"action_id": action_id}))
            if action.get("kind") == "tool_execution":
                events.append(
                    (
                        "ExternalResultRecorded",
                        {"action_id": action_id, "result": result},
                    )
                )
            events.append(("ActionSettled", {"action_id": action_id, "result": result}))
            payload = _as_dict(action.get("payload", {}), "action payload")
            if action.get("kind") == "tool_execution":
                detail = str(result["detail"])
                summary = (
                    f"模拟完成（未执行真实操作）：{detail}"
                    if status == "delivered"
                    else f"未完成：{detail}"
                )
                events.append(
                    (
                        "NecessaryResultSummarized",
                        {
                            "proposal_id": str(payload["proposal_id"]),
                            "action_id": action_id,
                            "action_status": status,
                            "completed_in_reality": False,
                            "summary": summary,
                        },
                    )
                )
            if action.get("kind") == "media_generation" and status == "delivered":
                artifact_path = str(result.get("artifact_path") or "")
                artifact_hash = str(result.get("artifact_hash") or "")
                if not artifact_path or len(artifact_path) > 500 or not artifact_hash or len(artifact_hash) > 128:
                    raise WorldError("generated media result requires bounded artifact path and hash")
                events.append(("MediaGenerated", {"request_id": str(payload["request_id"]), "artifact_path": artifact_path, "artifact_hash": artifact_hash, "action_id": action_id}))
            if action.get("kind") == "media_delivery" and status == "delivered":
                events.append(("MediaShared", {"request_id": str(payload["request_id"]), "action_id": action_id}))
            if action.get("kind") == "sticker_delivery" and status == "delivered":
                events.append(("StickerShared", {"action_id": action_id}))
            if action.get("kind") == "reaction_delivery" and status == "delivered":
                events.append(
                    (
                        "ReactionShared",
                        {
                            "action_id": action_id,
                            "message_id": str(payload["message_id"]),
                            "reaction_id": str(payload["reaction_id"]),
                            "external_receipt": result.get("external_receipt"),
                        },
                    )
                )
            return events
        if command_type == "mark_external_action_unknown":
            action_id = str(command.get("action_id") or "")
            reason = str(command.get("reason") or "").strip()
            action = _as_dict(
                _as_dict(state["actions"], "actions").get(action_id), "external action"
            )
            if action.get("status") not in {"scheduled", "sending"} or not reason:
                raise WorldError("only an unresolved external action can become unknown")
            return [
                (
                    "ActionDeliveryUncertain",
                    {"action_id": action_id, "reason": reason[:300]},
                )
            ]
        if command_type == "commit_experience":
            raise WorldError("experiences are committed only by validated life outcomes")
        if command_type == "record_model_proposal":
            proposal_id = str(command.get("proposal_id") or "")
            entity_id = str(command.get("entity_id") or "")
            template_id = str(command.get("template_id") or "")
            content = str(command.get("content") or "").strip()
            entities = _as_dict(state["entities"], "entities")
            proposals = _as_dict(state["proposals"], "proposals")
            entity = _as_dict(entities.get(entity_id), "proposal entity")
            templates = _as_list(entity.get("templates", []), "entity templates")
            if (
                not proposal_id
                or proposal_id in proposals
                or template_id not in templates
                or not content
                or len(content) > 160
                or not str(command.get("activity_id") or "")
                or not str(command.get("location") or "")
                or not str(command.get("starts_at") or "")
                or not str(command.get("ends_at") or "")
            ):
                raise WorldError("model proposal is outside the registered low-risk template set")
            return [
                (
                    "ModelProposalRecorded",
                    {
                        "proposal_id": proposal_id,
                        "entity_id": entity_id,
                        "template_id": template_id,
                        "content": content,
                        "activity_id": str(command["activity_id"]),
                        "location": str(command["location"]),
                        "starts_at": str(command["starts_at"]),
                        "ends_at": str(command["ends_at"]),
                        "npc_id": command.get("npc_id"),
                    },
                )
            ]
        if command_type == "record_model_output":
            # Model output is audit data, never a world fact by itself.  This
            # separate command deliberately does not use the low-risk life
            # event template whitelist: conversation JSON and decision JSON
            # are external results, not proposed experiences.
            proposal_id = str(command.get("proposal_id") or "")
            purpose = str(command.get("purpose") or "")
            content = str(command.get("content") or "")
            proposals = _as_dict(state["proposals"], "proposals")
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "model action")
            if not proposal_id or proposal_id in proposals or not purpose or not content or len(content) > 8192 or action.get("status") != "delivered":
                raise WorldError("model output requires a new bounded proposal id, purpose, and content")
            return [
                (
                    "ModelProposalRecorded",
                    {
                        "proposal_id": proposal_id,
                        "entity_id": "zhizhi",
                        "template_id": f"model_output:{purpose}",
                        "content": content,
                        "action_id": action_id,
                        "audit_only": True,
                    },
                )
            ]
        if command_type == "accept_model_proposal":
            proposal_id = str(command.get("proposal_id") or "")
            proposal = _as_dict(_as_dict(state["proposals"], "proposals").get(proposal_id), "proposal")
            if proposal["status"] != "recorded":
                raise WorldError("only a recorded proposal can be accepted")
            accepted, reason, specs = self.life_simulation.events_for_candidate(state, proposal)
            if not accepted:
                return [("LifeOutcomeRejected", {"outcome_id": proposal_id, "reason": reason, "rule_version": self.life_simulation.RULE_VERSION})]
            return specs
        if command_type == "confirm_fact":
            fact_id = str(command.get("fact_id") or "")
            value = str(command.get("value") or "").strip()
            facts = _as_dict(state["facts"], "facts")
            if not fact_id or not value or fact_id in facts:
                raise WorldError("fact confirmation requires a new id and non-empty value")
            return [
                (
                    "FactConfirmed",
                    {
                        "fact_id": fact_id,
                        "subject": str(command.get("subject") or "world"),
                        "value": value,
                        "source": str(command.get("source") or "verified"),
                        "scope": str(command.get("scope") or "durable"),
                        "source_message_id": str(command.get("source_message_id") or ""),
                        "conflict_key": str(command.get("conflict_key") or ""),
                        "pinned": bool(command.get("pinned", False)),
                        "importance": max(0, min(100, int(command.get("importance") or 50))),
                        "status": "current",
                    },
                )
            ]
        if command_type == "share_experience":
            raise WorldError("life sharing must settle through its scheduled delivery action")
        if command_type == "select_life_share":
            raise WorldError("life sharing must use schedule_life_share_delivery")
        if command_type == "observe_user_message":
            return [(
                "UserMessageObserved",
                {
                    "message_id": command.get("message_id"),
                    "user_id": command.get("user_id"),
                    "text": command.get("text", ""),
                    "sent_at": command.get("sent_at"),
                    "attachments": _as_list(command.get("attachments", []), "attachments"),
                    "emoji": _as_list(command.get("emoji", []), "emoji")[:16],
                    "sticker_kind": str(command.get("sticker_kind") or "")[:80],
                    "reply_target": str(command.get("reply_target") or "")[:160],
                    "source_message_ids": _as_list(
                        command.get("source_message_ids", []), "source message ids"
                    )[:20],
                },
            )]
        if command_type == "commit_private_impression":
            return [
                (
                    "PrivateImpressionCommitted",
                    self._private_impression_payload(state, command),
                )
            ]
        if command_type == "contradict_private_impression":
            impression_id = str(command.get("impression_id") or "")
            impressions = _as_dict(state.get("private_impressions", {}), "private impressions")
            impression = _as_dict(impressions.get(impression_id), "private impression")
            sources = self._validated_inner_life_sources(
                state,
                user_id=str(impression.get("user_id") or ""),
                source_event_ids=command.get("source_event_ids", []),
            )
            reason = str(command.get("reason") or "").strip()
            if (
                impression.get("status") != "active"
                or not sources
                or not set(sources).isdisjoint(
                    set(_as_list(impression.get("source_event_ids", []), "impression sources"))
                )
                or not reason
                or len(reason) > 240
            ):
                raise WorldError("private impression contradiction requires active impression and evidence")
            return [
                (
                    "PrivateImpressionContradicted",
                    {
                        "impression_id": impression_id,
                        "source_event_ids": sources,
                        "reason": reason,
                    },
                )
            ]
        if command_type == "commit_private_commitment":
            return [
                (
                    "PrivateCommitmentCommitted",
                    self._private_commitment_payload(state, command),
                )
            ]
        if command_type == "resolve_private_commitment":
            commitment_id = str(command.get("commitment_id") or "")
            commitment = _as_dict(
                _as_dict(state.get("private_commitments", {}), "private commitments").get(
                    commitment_id
                ),
                "private commitment",
            )
            outcome = str(command.get("outcome") or "")
            reason = str(command.get("reason") or "").strip()
            if commitment.get("status") != "active" or outcome not in {"fulfilled", "released"} or not reason or len(reason) > 240:
                raise WorldError("private commitment resolution requires an active bounded commitment")
            return [
                (
                    "PrivateCommitmentResolved",
                    {"commitment_id": commitment_id, "outcome": outcome, "reason": reason},
                )
            ]
        if command_type == "settle_turn":
            message_id = str(command.get("message_id") or "")
            status = str(command.get("status") or "")
            reason = str(command.get("reason") or "")
            if status not in {"delivered", "deferred", "failed"} or not message_id or not reason:
                raise WorldError("turn settlement requires message, terminal status, and reason")
            turn = _as_dict(_as_dict(state.get("turns", {}), "turns").get(message_id), "turn")
            if turn.get("status") in {"delivered", "deferred", "failed"}:
                return []
            if turn.get("status") not in {"claimed", "processing"}:
                raise WorldError("only a claimed turn can be settled")
            events: list[tuple[str, dict[str, object]]] = [
                ("TurnProcessingSettled", {"message_id": message_id, "status": status, "reason": reason})
            ]
            user_id = str(turn.get("user_id") or "")
            appraisal = str(turn.get("appraisal") or "")
            if status == "delivered" and user_id and appraisal:
                current_affinity = _as_dict(
                    _as_dict(state.get("long_term_affinity", {}), "long-term affinity").get(user_id, {}),
                    "user affinity",
                )
                outcome = settle_affinity_interaction(
                    current_affinity,
                    user_id=user_id,
                    appraisal=appraisal,
                    settlement_id=f"turn:{message_id}",
                    logical_at=str(_as_dict(state["clock"], "clock").get("logical_at") or ""),
                )
                if not outcome.duplicate:
                    events.append(
                        (
                            "AffinityInteractionSettled",
                            {
                                "user_id": user_id,
                                "message_id": message_id,
                                "settlement_id": f"turn:{message_id}",
                                "appraisal": appraisal,
                                "state": outcome.state,
                                "delta": outcome.delta,
                                "rule_version": outcome.rule_version,
                            },
                        )
                    )
            return events
        if command_type == "record_advisory_companion_affect":
            """Apply a delayed semantic boundary reading to affect only.

            This command deliberately has no ``TurnAppraised`` or relationship
            side effects.  It is the narrow bridge from a high-confidence
            advisory to future-facing companion affect after the current reply
            is already immutable.
            """
            message_id = str(command.get("message_id") or "")
            user_id = str(command.get("user_id") or "")
            appraisal = _as_dict(
                command.get("appraisal", {}), "advisory companion affect"
            )
            kind = str(appraisal.get("appraisal") or "")
            confidence = float(appraisal.get("confidence") or 0.0)
            severity = int(appraisal.get("severity") or 0)
            evidence_spans = [
                str(item)[:120]
                for item in _as_list(
                    appraisal.get("evidence_spans", []),
                    "advisory companion affect evidence",
                )[:6]
            ]
            if (
                not message_id
                or not user_id
                or kind != "boundary_violation"
                or str(appraisal.get("agency") or "") != "user"
                or str(appraisal.get("target") or "") != "companion"
                or not 0.75 <= confidence <= 1.0
                or not 1 <= severity <= 4
                or not evidence_spans
            ):
                raise WorldError("advisory companion affect is outside the bounded schema")
            observed = next(
                (
                    item
                    for item in _as_list(state.get("recent_messages", []), "recent messages")
                    if str(_as_dict(item, "recent message").get("message_id") or "")
                    == message_id
                    and str(_as_dict(item, "recent message").get("user_id") or "")
                    == user_id
                ),
                None,
            )
            if observed is None:
                raise WorldError("advisory companion affect requires its observed user message")
            text = str(_as_dict(observed, "observed user message").get("text") or "")
            if any(not span or span not in text for span in evidence_spans):
                raise WorldError("advisory companion affect evidence must quote the observed message")
            logical_at = str(_as_dict(state["clock"], "clock").get("logical_at") or "")
            affect = apply_appraisal(
                _as_dict(state["emotion_modulation"], "emotion modulation"),
                kind,
                logical_at,
                source_reference=f"message:{message_id}",
                intensity=severity,
                target="companion",
                appraisal_dimensions={
                    "certainty": int(appraisal.get("certainty") or 0),
                    "goal_congruence": int(appraisal.get("goal_congruence") or 0),
                    "controllability": int(appraisal.get("controllability") or 50),
                    "norm_compatibility": int(appraisal.get("norm_compatibility") or 0),
                    "power_delta": int(appraisal.get("power_delta") or 0),
                    "confidence": confidence,
                    "agency": "user",
                    "program_target": "companion",
                    "self_evaluation": "specific_action",
                    "social_exposure": 0,
                },
            )
            return [
                (
                    "AffectChanged",
                    affect_outcome_payload(
                        affect, logical_at=logical_at, event_type="AffectChanged"
                    ),
                )
            ]
        if command_type == "record_user_affect":
            """Persist only material affect inferred after the visible turn.

            Delayed semantic work is an Advisory: it cannot re-appraise a
            delivered user turn or mutate companion-facing state.  The event
            shape intentionally matches the affect portion of ``appraise_turn``
            so the projection has one durable ledger representation.
            """
            message_id = str(command.get("message_id") or "")
            user_id = str(command.get("user_id") or "")
            affect = _as_dict(command.get("affect", {}), "late user affect")
            kind = str(affect.get("kind") or "")
            intensity = int(affect.get("intensity") or 0)
            unresolved = bool(affect.get("unresolved"))
            confidence = float(affect.get("confidence") or 0.0)
            evidence_spans = [
                str(item)[:120]
                for item in _as_list(
                    affect.get("evidence_spans", []), "late user affect evidence"
                )[:6]
            ]
            if (
                not message_id
                or not user_id
                or kind not in {"disappointment", "confusion", "repaired"}
                or not 1 <= intensity <= 4
                or not 0.0 <= confidence <= 1.0
                or not evidence_spans
            ):
                raise WorldError("late user affect is outside the bounded schema")
            observed = next(
                (
                    item
                    for item in _as_list(state.get("recent_messages", []), "recent messages")
                    if str(_as_dict(item, "recent message").get("message_id") or "")
                    == message_id
                    and str(_as_dict(item, "recent message").get("user_id") or "")
                    == user_id
                ),
                None,
            )
            if observed is None:
                raise WorldError("late user affect requires its observed user message")
            text = str(_as_dict(observed, "observed user message").get("text") or "")
            if any(not span or span not in text for span in evidence_spans):
                raise WorldError("late user affect evidence must quote the observed message")
            prior = _as_dict(
                _as_dict(state.get("user_affect", {}), "user affect projection").get(
                    user_id, {}
                ),
                "active user affect",
            )
            prior_episodes = [
                _as_dict(item, "active user affect episode")
                for item in _as_list(prior.get("active_episodes", []), "active user affect episodes")
            ]
            if bool(prior.get("unresolved")) and str(prior.get("source_message_id") or "") and not prior_episodes:
                prior_episodes = [prior]
            should_persist = intensity >= 2 and unresolved
            closes_episode = kind == "repaired" and bool(prior_episodes)
            if not should_persist and not closes_episode:
                return []
            return [
                (
                    "UserAffectAppraised",
                    {
                        "user_id": user_id,
                        "source_message_id": message_id,
                        "kind": kind,
                        "intensity": intensity,
                        "unresolved": unresolved,
                        "confidence": confidence,
                        "cause": str(affect.get("cause") or "companion_response")[:80],
                        "evidence_spans": evidence_spans,
                        "settles_source_message_id": (
                            str(prior_episodes[-1].get("source_message_id") or "")
                            if closes_episode
                            else ""
                        ),
                        "settles_source_message_ids": (
                            [
                                str(item.get("source_message_id") or "")
                                for item in prior_episodes
                                if str(item.get("source_message_id") or "")
                            ]
                            if closes_episode
                            else []
                        ),
                        "rule_version": "user-affect-v1",
                    },
                )
            ]
        if command_type == "appraise_turn":
            appraisal = str(command.get("appraisal") or "ordinary_message")
            interaction = _as_dict(command.get("interaction", {}), "interaction appraisal")
            raw_user_affect = _as_dict(
                interaction.get("user_affect", {}), "user affect appraisal"
            )
            has_interaction_severity = "severity" in interaction
            severity = int(interaction.get("severity") or 3)
            target = str(interaction.get("target") or "general")
            acts = [
                str(item)
                for item in _as_list(interaction.get("acts", []), "interaction acts")
            ]
            evidence_spans = [
                str(item)[:80]
                for item in _as_list(
                    interaction.get("evidence_spans", []), "interaction evidence"
                )
            ]
            confidence = float(interaction.get("confidence", 1.0))
            certainty = int(interaction.get("certainty", 100))
            goal_congruence = int(interaction.get("goal_congruence", 0))
            controllability = int(interaction.get("controllability", 50))
            norm_compatibility = int(interaction.get("norm_compatibility", 0))
            power_delta = int(interaction.get("power_delta", 0))
            agency = str(interaction.get("agency") or "unknown")
            self_evaluation = str(
                interaction.get("self_evaluation") or "specific_action"
            )
            social_exposure = int(interaction.get("social_exposure", 0))
            repair_evidence = (
                _as_dict(interaction.get("repair_evidence", {}), "repair evidence")
                if appraisal == "boundary_respected"
                else {}
            )
            repair_evidence_reference = ""
            repair_followthrough_duplicate = False
            if appraisal == "boundary_respected":
                violation_id = str(repair_evidence.get("violation_id") or "")
                commitment_id = str(repair_evidence.get("commitment_id") or "")
                opportunity_id = str(repair_evidence.get("opportunity_id") or "")
                behavior_key = str(repair_evidence.get("behavior_key") or "")
                active_violation = str(
                    _as_dict(
                        state["emotion_modulation"], "emotion modulation"
                    ).get("repair_target_reference")
                    or ""
                )
                repair_cases = _as_dict(state.get("repair_cases", {}), "repair cases")
                repair_case = _as_dict(
                    repair_cases.get(violation_id, {}), "repair case"
                )
                commitments = _as_dict(
                    repair_case.get("commitments", {}), "repair commitments"
                )
                commitment = _as_dict(
                    commitments.get(commitment_id, {}), "repair commitment"
                )
                opportunities = _as_dict(
                    state.get("repair_opportunities", {}), "repair opportunities"
                )
                existing_opportunity = _as_dict(
                    opportunities.get(opportunity_id, {}), "repair opportunity"
                )
                if (
                    not violation_id
                    or violation_id != active_violation
                    or not commitment_id
                    or not opportunity_id
                    or not behavior_key
                    or any(
                        len(value) > 160
                        for value in (
                            violation_id,
                            commitment_id,
                            opportunity_id,
                            behavior_key,
                        )
                    )
                ):
                    raise WorldError(
                        "repair followthrough requires the active violation, commitment, opportunity, and behavior"
                    )
                if (
                    not commitment
                    or str(commitment.get("violation_id") or "") != violation_id
                    or int(commitment.get("committed_revision") or 0)
                    <= int(repair_case.get("violation_revision") or 0)
                ):
                    raise WorldError(
                        "repair followthrough requires a committed repair commitment bound to the violation"
                    )
                if existing_opportunity:
                    if (
                        str(existing_opportunity.get("violation_id") or "")
                        != violation_id
                        or str(existing_opportunity.get("commitment_id") or "")
                        != commitment_id
                        or str(existing_opportunity.get("behavior_key") or "")
                        != behavior_key
                    ):
                        raise WorldError(
                            "repair opportunity cannot be reused for a different behavior, commitment, or violation"
                        )
                    repair_followthrough_duplicate = True
                repair_evidence_reference = (
                    f"repair:{violation_id}:{opportunity_id}:{behavior_key}"
                )
            if not 1 <= severity <= 4 or target not in {
                "general",
                "companion",
                "self",
                "third_party",
            } or len(acts) > 6 or not 0.0 <= confidence <= 1.0:
                raise WorldError("interaction appraisal is outside the bounded schema")
            if agency not in {
                "user", "companion", "npc", "third_party", "situation", "unknown"
            } or self_evaluation not in {"specific_action", "global_negative"}:
                raise WorldError("interaction appraisal agency or self evaluation is invalid")
            if (
                not 0 <= certainty <= 100
                or not -100 <= goal_congruence <= 100
                or not 0 <= controllability <= 100
                or not -100 <= norm_compatibility <= 100
                or not -100 <= power_delta <= 100
                or not 0 <= social_exposure <= 100
            ):
                raise WorldError("interaction appraisal dimensions are outside bounds")
            if (
                appraisal in HARMFUL_INTERACTION_APPRAISALS
                and appraisal != "repeated_violation"
                and int(
                    _as_dict(state["emotion_modulation"], "emotion modulation").get(
                        "repair_observation_seconds", 0
                    )
                    or 0
                ) > 0
            ):
                appraisal = "repeated_violation"
            consequence_appraisal = (
                "ordinary_message" if repair_followthrough_duplicate else appraisal
            )
            consequence = self.interaction_rules.consequence(
                consequence_appraisal,
                severity=severity if has_interaction_severity else 3,
                confidence=confidence,
            )
            events: list[tuple[str, dict[str, object]]] = [
                (
                    "TurnAppraised",
                    {
                        "message_id": str(command.get("message_id") or ""),
                        "user_id": str(command.get("user_id") or ""),
                        "appraisal": appraisal,
                        "severity": severity,
                        "acts": acts,
                        "target": target,
                        "evidence_spans": evidence_spans,
                        "literal_act": str(interaction.get("literal_act") or "")[:160],
                        "implied_attitude": str(
                            interaction.get("implied_attitude") or ""
                        )[:160],
                        "agency": agency,
                        "self_evaluation": self_evaluation,
                        "social_exposure": social_exposure,
                        "certainty": certainty,
                        "goal_congruence": goal_congruence,
                        "controllability": controllability,
                        "norm_compatibility": norm_compatibility,
                        "power_delta": power_delta,
                        "confidence": confidence,
                        "alternative_appraisal": str(
                            interaction.get("alternative_appraisal") or ""
                        )[:240],
                        "repair_evidence": dict(repair_evidence),
                        "policy": consequence.policy,
                        "rule_version": self.interaction_rules.RULE_VERSION,
                    },
                ),
                (
                    "IntentCreated",
                    {
                        "intent_id": str(command["intent_id"]), "kind": "reply", "status": "open",
                        "message_id": str(command.get("message_id") or ""),
                    },
                ),
            ]
            if raw_user_affect:
                affect_kind = str(raw_user_affect.get("kind") or "")
                affect_intensity = int(raw_user_affect.get("intensity") or 0)
                affect_unresolved = bool(raw_user_affect.get("unresolved"))
                affect_confidence = float(raw_user_affect.get("confidence") or 0.0)
                affect_evidence = [
                    str(item)[:120]
                    for item in _as_list(
                        raw_user_affect.get("evidence_spans", []),
                        "user affect evidence",
                    )[:6]
                ]
                if (
                    affect_kind not in {"disappointment", "confusion", "repaired"}
                    or not 1 <= affect_intensity <= 4
                    or not 0.0 <= affect_confidence <= 1.0
                    or not affect_evidence
                ):
                    raise WorldError("user affect appraisal is outside the bounded schema")
                prior_user_affect = _as_dict(
                    _as_dict(state.get("user_affect", {}), "user affect projection").get(
                        str(command.get("user_id") or ""), {}
                    ),
                    "active user affect",
                )
                prior_active_episodes = [
                    _as_dict(item, "active user affect episode")
                    for item in _as_list(
                        prior_user_affect.get("active_episodes", []),
                        "active user affect episodes",
                    )
                ]
                if (
                    bool(prior_user_affect.get("unresolved"))
                    and str(prior_user_affect.get("source_message_id") or "")
                    and not prior_active_episodes
                ):
                    prior_active_episodes = [prior_user_affect]
                # Persistence is a World invariant, not a caller preference.
                # Commands provide bounded observations; the authoritative
                # ledger decides whether they are large and unresolved enough
                # to survive beyond this turn.
                should_persist = affect_intensity >= 2 and affect_unresolved
                # A repair settlement is ledger-worthy only when it closes an
                # already committed unresolved episode. Mild, immediately
                # resolved reactions remain turn-local.
                closes_committed_episode = (
                    affect_kind == "repaired"
                    and bool(prior_active_episodes)
                )
                if should_persist or closes_committed_episode:
                    events.append(
                        (
                            "UserAffectAppraised",
                            {
                                "user_id": str(command.get("user_id") or ""),
                                "source_message_id": str(command.get("message_id") or ""),
                                "kind": affect_kind,
                                "intensity": affect_intensity,
                                "unresolved": affect_unresolved,
                                "confidence": affect_confidence,
                                "cause": str(
                                    raw_user_affect.get("cause") or "companion_response"
                                )[:80],
                                "evidence_spans": affect_evidence,
                                "settles_source_message_id": (
                                    str(
                                        prior_active_episodes[-1].get("source_message_id")
                                        or ""
                                    )
                                    if closes_committed_episode
                                    else ""
                                ),
                                "settles_source_message_ids": (
                                    [
                                        str(item.get("source_message_id") or "")
                                        for item in prior_active_episodes
                                        if str(item.get("source_message_id") or "")
                                    ]
                                    if closes_committed_episode
                                    else []
                                ),
                                "rule_version": "user-affect-v1",
                            },
                        )
                    )
            raw_private_impression = command.get("private_impression")
            if raw_private_impression is not None:
                impression = self._private_impression_payload(
                    state,
                    _as_dict(raw_private_impression, "turn private impression"),
                )
                expected_kind = (
                    "possible_disappointment"
                    if str(raw_user_affect.get("kind") or "") == "disappointment"
                    else "possible_confusion"
                    if str(raw_user_affect.get("kind") or "") == "confusion"
                    else ""
                )
                if (
                    not bool(raw_user_affect.get("unresolved"))
                    or int(raw_user_affect.get("intensity") or 0) < 3
                    or impression["kind"] != expected_kind
                    or impression["user_id"] != str(command.get("user_id") or "")
                ):
                    raise WorldError("turn private impression fails the materiality policy")
                events.append(("PrivateImpressionCommitted", impression))
            message_reference = (
                f"message:{command.get('message_id') or command.get('intent_id') or 'unknown'}"
            )
            active_violation = str(
                _as_dict(state["emotion_modulation"], "emotion modulation").get(
                    "repair_target_reference"
                )
                or ""
            )
            if appraisal in HARMFUL_INTERACTION_APPRAISALS:
                events.append(
                    (
                        "RepairViolationCommitted",
                        {
                            "violation_id": message_reference,
                            "message_id": str(command.get("message_id") or ""),
                            "user_id": str(command.get("user_id") or ""),
                            "boundary_kind": appraisal,
                        },
                    )
                )
            elif appraisal in {"repair_specific", "repair_restitution"} and active_violation:
                repair_case = _as_dict(
                    _as_dict(state.get("repair_cases", {}), "repair cases").get(
                        active_violation, {}
                    ),
                    "repair case",
                )
                commitment_id = f"commitment:{active_violation}"
                existing_commitments = _as_dict(
                    repair_case.get("commitments", {}), "repair commitments"
                )
                if commitment_id not in existing_commitments:
                    events.append(
                        (
                            "RepairCommitmentCommitted",
                            {
                                "commitment_id": commitment_id,
                                "violation_id": active_violation,
                                "message_id": str(command.get("message_id") or ""),
                                "user_id": str(command.get("user_id") or ""),
                                "promised_boundary": "honor_boundary",
                            },
                        )
                    )
            elif appraisal == "boundary_respected" and not repair_followthrough_duplicate:
                events.extend(
                    (
                        (
                            "RepairOpportunityObserved",
                            {
                                "opportunity_id": opportunity_id,
                                "violation_id": violation_id,
                                "commitment_id": commitment_id,
                                "behavior_key": behavior_key,
                            },
                        ),
                        (
                            "RepairFollowthroughCommitted",
                            {
                                "opportunity_id": opportunity_id,
                                "violation_id": violation_id,
                                "commitment_id": commitment_id,
                                "behavior_key": behavior_key,
                                "evidence_reference": repair_evidence_reference,
                            },
                        ),
                    )
                )
            events.extend(
                ("NeedChanged", {"need": need, "delta": delta})
                for need, delta in consequence.need_deltas.items()
            )
            user_id = str(command.get("user_id") or "")
            affinity_for_affect: dict[str, object] = {}
            if user_id:
                entities = _as_dict(state["entities"], "entities")
                user = _as_dict(entities.get(user_id), "appraised user")
                if user.get("kind") != "user":
                    raise WorldError("turn appraisal user must be a registered user")
                protagonist = next(
                    (
                        _as_dict(raw, "protagonist")
                        for raw in entities.values()
                        if _as_dict(raw, "entity").get("kind") == "companion"
                    ),
                    {},
                )
                slow_warmth = relationship_slow_warmth(protagonist)
                event_significance = relationship_event_significance(
                    consequence_appraisal
                )
                events.append(("RelationshipAppraised", {"user_id": user_id, "appraisal": appraisal, "rule_version": self.interaction_rules.RULE_VERSION}))
                events.extend(
                    ("RelationshipChanged", {"entity_id": user_id, "dimension": dimension, "delta": delta})
                    for dimension, delta in consequence.relationship_deltas.items()
                )
                relation = dict(
                    _as_dict(
                        _as_dict(state["relationships"], "relationships").get(user_id, {}),
                        "user relationship",
                    )
                )
                relation["interaction_count"] = int(relation.get("interaction_count") or 0) + 1
                for dimension, delta in consequence.relationship_deltas.items():
                    relation[dimension] = max(
                        -100,
                        min(100, int(relation.get(dimension) or 0) + int(delta)),
                    )
                current_stage = str(relation.get("stage") or "stranger")
                boundary = int(_as_dict(state["needs"], "needs").get("boundary", 0))
                boundary += int(consequence.need_deltas.get("boundary", 0))
                stage, reason = evaluate_relationship_stage(
                    relation,
                    boundary=boundary,
                    slow_warmth=slow_warmth,
                    event_significance=event_significance,
                )
                events.append(
                    (
                        "RelationshipStageEvaluated",
                        stage_event_payload(
                            entity_id=user_id,
                            stage=stage,
                            from_stage=current_stage,
                            relationship=relation,
                            boundary=boundary,
                            reason=reason,
                            slow_warmth=slow_warmth,
                            event_significance=event_significance,
                        ),
                    )
                )
                if appraisal in HARMFUL_INTERACTION_APPRAISALS:
                    current_affinity = _as_dict(
                        _as_dict(
                            state.get("long_term_affinity", {}),
                            "long-term affinity",
                        ).get(user_id, {}),
                        "user affinity",
                    )
                    affinity = settle_affinity_interaction(
                        current_affinity,
                        user_id=user_id,
                        appraisal=appraisal,
                        settlement_id=f"turn:{command.get('message_id') or command['intent_id']}",
                        logical_at=str(
                            _as_dict(state["clock"], "clock").get("logical_at") or ""
                        ),
                    )
                    affinity_for_affect = affinity.state
                    if not affinity.duplicate:
                        events.append(
                            (
                                "AffinityInteractionSettled",
                                {
                                    "user_id": user_id,
                                    "message_id": str(command.get("message_id") or ""),
                                    "settlement_id": f"turn:{command.get('message_id') or command['intent_id']}",
                                    "appraisal": appraisal,
                                    "state": affinity.state,
                                    "delta": affinity.delta,
                                    "rule_version": affinity.rule_version,
                                },
                            )
                        )
            logical_at = str(_as_dict(state["clock"], "clock").get("logical_at") or "")
            affect = apply_appraisal(
                _as_dict(state["emotion_modulation"], "emotion modulation"),
                appraisal,
                logical_at,
                source_reference=(
                    repair_evidence_reference
                    or message_reference
                ),
                intensity=severity if has_interaction_severity else None,
                target=target,
                appraisal_dimensions={
                    "certainty": certainty,
                    "goal_congruence": goal_congruence,
                    "controllability": controllability,
                    "norm_compatibility": norm_compatibility,
                    "power_delta": power_delta,
                    "confidence": confidence,
                    "agency": agency,
                    "program_target": target,
                    "self_evaluation": self_evaluation,
                    "social_exposure": social_exposure,
                },
                relationship_residue=affinity_for_affect,
            )
            events.append(
                (
                    "AffectChanged",
                    affect_outcome_payload(affect, logical_at=logical_at, event_type="AffectChanged"),
                )
            )
            relation_for_decision = (
                dict(
                    _as_dict(
                        _as_dict(state["relationships"], "relationships").get(
                            user_id, {}
                        ),
                        "relationship",
                    )
                )
                if user_id
                else {}
            )
            for dimension, delta in consequence.relationship_deltas.items():
                relation_for_decision[dimension] = max(
                    -100,
                    min(
                        100,
                        int(relation_for_decision.get(dimension, 0)) + int(delta),
                    ),
                )
            needs_for_decision = dict(_as_dict(state["needs"], "needs"))
            for need, delta in consequence.need_deltas.items():
                needs_for_decision[need] = max(
                    0, min(100, int(needs_for_decision.get(need, 0)) + int(delta))
                )
            affect_projection = affect_outcome_payload(
                affect, logical_at=logical_at, event_type="AffectChanged"
            )
            display_plan = plan_affect_display(
                affect_projection,
                relation_for_decision,
                needs_for_decision,
                current_appraisal=appraisal,
            )
            events.append(
                (
                    "AffectDisplaySelected",
                    {
                        "message_id": str(command.get("message_id") or ""),
                        **display_plan.payload(),
                    },
                )
            )
            message_id = str(command.get("message_id") or "")
            observed_message = next(
                (
                    item for item in reversed(_as_list(state.get("recent_messages", []), "recent messages"))
                    if str(_as_dict(item, "recent message").get("message_id") or "") == message_id
                ),
                {},
            )
            user_text = str(_as_dict(observed_message, "observed message").get("text") or "")
            decision = self.character_deliberation.decide(
                situation={
                    "text": user_text,
                    "risk": str(command.get("risk") or "low"),
                    "appraisal": appraisal,
                    "severity": severity,
                    "acts": tuple(acts),
                    "causation_ids": (message_id,) if message_id else (),
                },
                self_core=next(
                    (
                        _as_dict(raw, "self core")
                        for raw in _as_dict(state.get("entities", {}), "entities").values()
                        if _as_dict(raw, "entity").get("kind") == "companion"
                    ),
                    {},
                ),
                relationship=relation_for_decision,
                affect={
                    "irritation": affect.vector.get("anger", 0),
                    **affect.vector,
                },
                needs=needs_for_decision,
                user_request=UserRequest.from_text(user_text),
                open_commitments=tuple(
                    key for key, item in _as_dict(state.get("conversation_threads", {}), "threads").items()
                    if _as_dict(item, "thread").get("status") == "open"
                ),
                available_actions=("reply_now", "defer_reply", "remain_silent"),
            )
            events.extend(
                [
                    (
                        "UserRequestAppraised",
                        {
                            "message_id": message_id,
                            "kind": decision.user_request.kind,
                            "scope": decision.user_request.scope,
                            "strength": decision.user_request.strength,
                            "subject": decision.user_request.subject,
                            "rule_version": decision.rule_version,
                        },
                    ),
                    (
                        "MotiveConflictEvaluated",
                        {
                            "message_id": message_id,
                            "appraisal": decision.appraisal,
                            "drives": dict(decision.drives),
                            "conflicts": list(decision.conflicts),
                            "stances_considered": list(decision.stances_considered),
                            "rule_version": decision.rule_version,
                        },
                    ),
                    (
                        "StanceSelected",
                        {
                            "message_id": message_id,
                            "stance": decision.chosen_stance,
                            "display_strategy": decision.display_strategy,
                            "drives": dict(decision.drives),
                            "conflicts": list(decision.conflicts),
                            "action_candidates": list(decision.action_candidates),
                            "selection_mode": decision.selection.mode,
                            "rule_version": decision.rule_version,
                        },
                    ),
                ]
            )
            if (
                bool(_as_dict(state["emotion_modulation"], "emotion modulation").get("unresolved"))
                and not affect.unresolved
            ):
                events.append(
                    (
                        "AffectResolved",
                        affect_outcome_payload(affect, logical_at=logical_at, event_type="AffectResolved"),
                    )
                )
            return events
        if command_type == "commit_reply_affect":
            message_id = str(command.get("message_id") or "")
            known_message_ids = {
                str(_as_dict(item, "recent message").get("message_id") or "")
                for item in _as_list(state.get("recent_messages", []), "recent messages")
            }
            if not message_id or message_id not in known_message_ids:
                raise WorldError("reply affect requires an observed source message")
            logical_at = str(_as_dict(state["clock"], "clock").get("logical_at") or "")
            affect = apply_appraisal(
                _as_dict(state["emotion_modulation"], "emotion modulation"),
                "reply_discomfort",
                logical_at,
                source_reference=f"message:{message_id}",
                target="companion",
            )
            return [(
                "AffectCommitted",
                affect_outcome_payload(affect, logical_at=logical_at, event_type="AffectCommitted"),
            )]
        raise WorldError(f"unsupported command: {command_type}")

    def _append_and_project(
        self,
        conn,
        world_id: str,
        revision: int,
        state: dict[str, object],
        specifications: list[tuple[str, dict[str, object]]],
        *,
        idempotency_key: str,
        correlation_id: str,
        source: str,
        actor: dict[str, object],
        causation_id: str | None,
    ) -> WorldDecision:
        specifications = self._cost_enriched_specifications(state, specifications)
        logical_at = str(_as_dict(state.get("clock", {}), "clock").get("logical_at") or "")
        if specifications and specifications[0][0] == "WorldStarted":
            logical_at = str(specifications[0][1]["logical_at"])
        observed_at = utc_now().isoformat()
        events: list[WorldEvent] = []
        for offset, (event_type, payload) in enumerate(specifications, start=1):
            if event_type == "ClockAdvanced":
                logical_at = str(payload["target_logical_at"])
            event = WorldEvent(
                event_id=str(uuid4()),
                world_id=world_id,
                revision=revision + offset,
                event_type=event_type,
                schema_version=1,
                logical_at=logical_at,
                observed_at=observed_at,
                actor=actor,
                source=source,
                correlation_id=correlation_id,
                causation_id=causation_id,
                idempotency_key=idempotency_key if offset == 1 else None,
                payload=payload,
                payload_hash=_hash(_stable_json(payload)),
            )
            events.append(event)
            state = reduce_event(state, event)
        new_revision = revision + len(events)
        for event in events:
            conn.execute(
                """
                insert into world_events (
                  event_id, world_id, revision, event_type, schema_version, logical_at, observed_at,
                  actor_json, source, correlation_id, causation_id, idempotency_key, payload_json, payload_hash
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id, event.world_id, event.revision, event.event_type, event.schema_version,
                    event.logical_at, event.observed_at, _stable_json(event.actor), event.source,
                    event.correlation_id, event.causation_id, event.idempotency_key, _stable_json(event.payload), event.payload_hash,
                ),
            )
        state_hash = _state_hash(state)
        conn.execute(
            "update worlds set revision = ?, logical_at = ? where world_id = ?",
            (new_revision, _as_dict(state["clock"], "clock")["logical_at"], world_id),
        )
        self._write_projection(conn, world_id, new_revision, state)
        conn.execute(
            "insert into world_command_receipts (world_id, idempotency_key, revision, event_ids_json, created_at) values (?, ?, ?, ?, ?)",
            (world_id, idempotency_key, new_revision, _stable_json([event.event_id for event in events]), observed_at),
        )
        if new_revision % self.SNAPSHOT_INTERVAL == 0 or events[0].event_type == "WorldStarted":
            conn.execute(
                "insert or replace into world_snapshots (world_id, revision, state_json, state_hash, created_at) values (?, ?, ?, ?, ?)",
                (world_id, new_revision, _stable_json(state), state_hash, observed_at),
            )
        return WorldDecision(world_id, new_revision, tuple(events), state_hash)

    def _cost_enriched_specifications(
        self,
        state: dict[str, object],
        specifications: list[tuple[str, dict[str, object]]],
    ) -> list[tuple[str, dict[str, object]]]:
        """Reserve and terminally account for every costly external Action."""
        if not specifications or specifications[0][0] == "WorldStarted":
            return specifications
        recorded = _as_list(
            _as_dict(state.get("cost_ledger", {}), "cost ledger").get("events", []),
            "cost ledger events",
        )
        ledger = WorldCostLedger.from_events(
            self.COST_POLICY,
            tuple(
                CostLedgerEvent(str(_as_dict(raw, "cost event")["event_type"]), _as_dict(raw, "cost event")["payload"])
                for raw in recorded
            ),
        )
        actions = {
            action_id: dict(_as_dict(raw, "action"))
            for action_id, raw in _as_dict(state.get("actions", {}), "actions").items()
        }
        enriched: list[tuple[str, dict[str, object]]] = []
        logical_at = str(_as_dict(state.get("clock", {}), "clock").get("logical_at") or "")
        logical_day = _parse_at(logical_at).date().isoformat() if logical_at else "1970-01-01"
        for event_type, original_payload in specifications:
            payload = dict(original_payload)
            if event_type == "ActionScheduled" and "cost" not in payload:
                quote = _action_cost_quote(payload)
                if quote is not None:
                    category, units, automatic, cache_key = quote
                    before = len(ledger.export_events())
                    decision = ledger.reserve(
                        CostRequest(
                            idempotency_key=f"action:{payload['action_id']}",
                            category=category,
                            logical_day=logical_day,
                            units=units,
                            automatic=automatic,
                            cache_key=cache_key,
                        )
                    )
                    cost_event = ledger.export_events()[before]
                    enriched.append(("CostReservationDecided", dict(cost_event.payload)))
                    if not decision.allowed:
                        raise WorldError(f"world cost budget rejected action: {decision.reason}")
                    payload["cost"] = {
                        "category": category,
                        "estimated_units": units,
                        "automatic": automatic,
                        "reservation_id": decision.reservation_id,
                        "cache_reused": decision.reused,
                        "reused_result_ref": decision.reused_result_ref,
                    }
                actions[str(payload["action_id"])] = payload
                enriched.append((event_type, payload))
                continue
            enriched.append((event_type, payload))
            if event_type not in {"ActionSettled", "ActionCancelled", "ActionExpired"}:
                continue
            action_id = str(payload.get("action_id") or "")
            action = actions.get(action_id, {})
            cost = _as_dict(action.get("cost", {}), "action cost")
            reservation_id = str(cost.get("reservation_id") or "") or None
            if reservation_id is None:
                continue
            before = len(ledger.export_events())
            if event_type == "ActionSettled":
                result = _as_dict(payload.get("result", {}), "action settlement")
                result_ref = str(
                    result.get("artifact_hash")
                    or result.get("output_hash")
                    or result.get("external_receipt")
                    or ""
                ) or None
                ledger.settle(
                    reservation_id,
                    actual_units=int(cost.get("estimated_units") or 0),
                    idempotency_key=f"cost-settle:{action_id}",
                    result_ref=result_ref,
                )
            else:
                ledger.release(
                    reservation_id,
                    idempotency_key=f"cost-release:{action_id}",
                    reason=event_type.casefold(),
                )
            cost_event = ledger.export_events()[before]
            if cost_event.event_type == "CostReservationSettled":
                enriched.append(("CostReservationSettled", dict(cost_event.payload)))
            else:
                enriched.append(("CostReservationReleased", dict(cost_event.payload)))
        return enriched

    def _write_projection(self, conn, world_id: str, revision: int, state: dict[str, object]) -> None:
        now = utc_now().isoformat()
        state_hash = _state_hash(state)
        conn.execute(
            "insert or replace into world_current_state (world_id, applied_revision, state_json, state_hash, updated_at) values (?, ?, ?, ?, ?)",
            (world_id, revision, _stable_json(state), state_hash, now),
        )
        for projection_name in (
            "world_current_state", "world_entities", "world_agenda",
            "world_actions", "world_experiences", "world_fact_index",
        ):
            conn.execute(
                "insert or replace into world_projection_checkpoints (world_id, projection_name, applied_revision, state_hash, updated_at) values (?, ?, ?, ?, ?)",
                (world_id, projection_name, revision, state_hash, now),
            )
        for table in ("world_entities", "world_agenda", "world_actions", "world_experiences", "world_fact_index"):
            conn.execute(f"delete from {table} where world_id = ?", (world_id,))
        for entity_id, entity in _as_dict(state["entities"], "entities").items():
            item = _as_dict(entity, "entity")
            conn.execute(
                "insert into world_entities (world_id, entity_id, kind, name, state_json) values (?, ?, ?, ?, ?)",
                (world_id, entity_id, item["kind"], item["name"], _stable_json(item)),
            )
        for activity_id, activity in _as_dict(state["agenda"], "agenda").items():
            item = _as_dict(activity, "activity")
            conn.execute(
                "insert into world_agenda (world_id, activity_id, entity_id, starts_at, ends_at, status, state_json) values (?, ?, ?, ?, ?, ?, ?)",
                (world_id, activity_id, item["entity_id"], item["starts_at"], item["ends_at"], item["status"], _stable_json(item)),
            )
        for action_id, action in _as_dict(state["actions"], "actions").items():
            item = _as_dict(action, "action")
            conn.execute(
                "insert into world_actions (world_id, action_id, kind, status, expires_at, state_json) values (?, ?, ?, ?, ?, ?)",
                (world_id, action_id, item["kind"], item["status"], item.get("expires_at"), _stable_json(item)),
            )
        for experience_id, experience in _as_dict(state["experiences"], "experiences").items():
            item = _as_dict(experience, "experience")
            conn.execute(
                "insert into world_experiences (world_id, experience_id, action_id, content, state_json) values (?, ?, ?, ?, ?)",
                (world_id, experience_id, item.get("action_id"), item["content"], _stable_json(item)),
            )
        for fact_id, fact in _as_dict(state["facts"], "facts").items():
            conn.execute(
                "insert into world_fact_index (world_id, fact_id, state_json) values (?, ?, ?)",
                (world_id, fact_id, _stable_json(_as_dict(fact, "fact"))),
            )

    def _load_state(self, conn, world_id: str) -> tuple[int, dict[str, object]]:
        world = conn.execute(
            "select revision from worlds where world_id = ?", (world_id,)
        ).fetchone()
        if not world:
            raise WorldError(f"unknown world: {world_id}")
        revision = int(world["revision"])
        projection = conn.execute(
            "select applied_revision, state_json, state_hash from world_current_state where world_id = ?",
            (world_id,),
        ).fetchone()
        if projection and int(projection["applied_revision"]) == revision:
            try:
                state = json.loads(str(projection["state_json"]))
                state_hash = _state_hash(state) if self._projection_state_is_usable(state) else ""
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                state = None
            if (
                isinstance(state, dict)
                and state_hash == str(projection["state_hash"])
            ):
                return revision, state
        # Projection absence/lag/corruption is recoverable: the append-only
        # event stream remains the authority and is replayed only as fallback.
        events = self._load_events(conn, world_id)
        if not events:
            raise WorldError(f"world has no event stream: {world_id}")
        return events[-1].revision, reduce_events(events)

    @staticmethod
    def _projection_state_is_usable(state: object) -> bool:
        """Reject shape-corrupted cache rows before trusting their checksum."""
        if not isinstance(state, dict):
            return False
        mappings = ("clock", "entities", "agenda", "actions", "facts", "experiences")
        clock = state.get("clock")
        return (
            all(isinstance(state.get(key), dict) for key in mappings)
            and isinstance(state.get("recent_messages"), list)
            and isinstance(clock, dict)
            and bool(str(clock.get("logical_at") or ""))
        )

    def _load_events(self, conn, world_id: str) -> list[WorldEvent]:
        rows = conn.execute("select * from world_events where world_id = ? order by revision", (world_id,)).fetchall()
        return [
            WorldEvent(
                event_id=row["event_id"], world_id=row["world_id"], revision=row["revision"],
                event_type=row["event_type"], schema_version=row["schema_version"], logical_at=row["logical_at"],
                observed_at=row["observed_at"], actor=json.loads(row["actor_json"]), source=row["source"],
                correlation_id=row["correlation_id"], causation_id=row["causation_id"], idempotency_key=row["idempotency_key"],
                payload=json.loads(row["payload_json"]), payload_hash=row["payload_hash"],
            )
            for row in rows
        ]

    @staticmethod
    def _validated_inner_life_sources(
        state: dict[str, object], *, user_id: str, source_event_ids: object
    ) -> list[str]:
        """Allow only committed records to justify a fallible inner proposal."""
        source_ids = [
            str(item)
            for item in _as_list(source_event_ids, "inner life source ids")
            if str(item)
        ]
        if not source_ids or len(source_ids) > 6 or len(set(source_ids)) != len(source_ids):
            raise WorldError("private inner life requires one to six unique committed sources")
        known = {
            f"message:{str(item.get('message_id') or '')}"
            for raw in _as_list(state.get("recent_messages", []), "recent messages")
            if (item := _as_dict(raw, "recent message")).get("message_id")
            and (not user_id or str(item.get("user_id") or "") in {"", user_id})
        }
        known.update(
            str(fact_id)
            for fact_id, raw in _as_dict(state.get("facts", {}), "facts").items()
            if _fact_is_current(raw)
            and str(_as_dict(raw, "fact").get("subject") or "")
            in {user_id, "zhizhi", "world"}
        )
        known.update(
            str(experience_id)
            for experience_id in _as_dict(state.get("experiences", {}), "experiences")
        )
        known.update(
            str(thread_id)
            for thread_id, raw in _as_dict(state.get("conversation_threads", {}), "threads").items()
            if (thread := _as_dict(raw, "thread")).get("status") == "open"
            and str(thread.get("user_id") or "") == user_id
        )
        known.update(
            str(action_id)
            for action_id, raw in _as_dict(state.get("actions", {}), "actions").items()
            if _as_dict(raw, "action").get("status") == "delivered"
        )
        if any(source_id not in known for source_id in source_ids):
            raise WorldError("private inner life requires committed source ids")
        return source_ids

    @classmethod
    def _private_commitment_payload(
        cls,
        state: dict[str, object],
        raw: dict[str, object],
        *,
        pending_thread_id: str = "",
    ) -> dict[str, object]:
        """Validate the one shared commitment shape used by turns and API calls."""
        commitment_id = str(raw.get("commitment_id") or "")
        user_id = str(raw.get("user_id") or "")
        intention = str(raw.get("intention") or "").strip()
        sources = cls._validated_inner_life_sources(
            state,
            user_id=user_id,
            source_event_ids=raw.get("source_event_ids", []),
        )
        expires_at = str(raw.get("expires_at") or "")
        priority = int(raw.get("priority") or 0)
        related_thread_id = str(raw.get("related_thread_id") or "")
        threads = _as_dict(state.get("conversation_threads", {}), "threads")
        related_thread = _as_dict(threads.get(related_thread_id, {}), "related thread")
        if (
            not commitment_id
            or commitment_id
            in _as_dict(state.get("private_commitments", {}), "private commitments")
            or not intention
            or len(intention) > 240
            or not 1 <= priority <= 100
            or not expires_at
            or _parse_at(expires_at)
            <= _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
            or len(related_thread_id) > 160
            or (
                related_thread_id
                and (
                    related_thread.get("status") != "open"
                    or str(related_thread.get("user_id") or "") != user_id
                )
                and related_thread_id != pending_thread_id
            )
        ):
            raise WorldError("private commitment is outside the bounded schema")
        return {
            "commitment_id": commitment_id,
            "user_id": user_id,
            "intention": intention,
            "source_event_ids": sources,
            "expires_at": expires_at,
            "priority": priority,
            "related_thread_id": related_thread_id,
            "rule_version": "private-inner-life-v1",
        }

    @classmethod
    def _private_impression_payload(
        cls, state: dict[str, object], raw: dict[str, object]
    ) -> dict[str, object]:
        impression_id = str(raw.get("impression_id") or "")
        user_id = str(raw.get("user_id") or "")
        kind = str(raw.get("kind") or "")
        summary = str(raw.get("summary") or "").strip()
        confidence = float(raw.get("confidence") or 0.0)
        sources = cls._validated_inner_life_sources(
            state,
            user_id=user_id,
            source_event_ids=raw.get("source_event_ids", []),
        )
        expires_at = str(raw.get("expires_at") or "")
        if (
            not impression_id
            or impression_id
            in _as_dict(state.get("private_impressions", {}), "private impressions")
            or kind
            not in {
                "possible_disappointment",
                "possible_confusion",
                "boundary_concern",
                "relational_hypothesis",
                "continuity_note",
            }
            or not summary
            or len(summary) > 240
            or "\n" in summary
            or not 0.0 < confidence <= 1.0
            or not expires_at
            or _parse_at(expires_at)
            <= _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"]))
        ):
            raise WorldError("private impression is outside the bounded schema")
        return {
            "impression_id": impression_id,
            "user_id": user_id,
            "kind": kind,
            "summary": summary,
            "confidence": confidence,
            "source_event_ids": sources,
            "expires_at": expires_at,
            "contradictory_evidence": [],
            "rule_version": "private-inner-life-v1",
        }

    @staticmethod
    def _thread_commitment_resolution_events(
        state: dict[str, object],
        *,
        thread_id: str,
        outcome: str,
        reason: str,
    ) -> list[tuple[str, dict[str, object]]]:
        return [
            (
                "PrivateCommitmentResolved",
                {
                    "commitment_id": commitment_id,
                    "outcome": outcome,
                    "reason": reason,
                },
            )
            for commitment_id, raw in _as_dict(
                state.get("private_commitments", {}), "private commitments"
            ).items()
            if (commitment := _as_dict(raw, "private commitment")).get("status")
            == "active"
            and str(commitment.get("related_thread_id") or "") == thread_id
        ]

    @staticmethod
    def _release_trace_private_commitment(
        action: dict[str, object], *, reason: str
    ) -> list[tuple[str, dict[str, object]]]:
        trace = _as_dict(action.get("trace", {}), "action trace")
        commitment = trace.get("private_commitment")
        commitment_id = (
            str(_as_dict(commitment, "trace private commitment").get("commitment_id") or "")
            if isinstance(commitment, dict)
            else ""
        )
        return (
            [
                (
                    "PrivateCommitmentResolved",
                    {
                        "commitment_id": commitment_id,
                        "outcome": "released",
                        "reason": reason[:240],
                    },
                )
            ]
            if commitment_id
            else []
        )

    def _receipt(self, conn, world_id: str, key: str):
        return conn.execute(
            "select revision, event_ids_json from world_command_receipts where world_id = ? and idempotency_key = ?", (world_id, key)
        ).fetchone()

    def _decision_from_receipt(self, conn, world_id: str, receipt) -> WorldDecision:
        event_ids = json.loads(receipt["event_ids_json"])
        events = [event for event in self._load_events(conn, world_id) if event.event_id in event_ids]
        state = reduce_events(
            [event for event in self._load_events(conn, world_id) if event.revision <= int(receipt["revision"])]
        )
        return WorldDecision(world_id, int(receipt["revision"]), tuple(events), _state_hash(state))

    def _world_for_action(self, action_id: str) -> str:
        with self.store.connect() as conn:
            row = conn.execute("select world_id from world_actions where action_id = ?", (action_id,)).fetchone()
        if not row:
            raise WorldError(f"unknown action: {action_id}")
        return str(row["world_id"])

    @staticmethod
    def _travel_minutes(state: dict[str, object], origin: str, destination: str) -> int:
        if not origin or not destination or origin == destination:
            return 0
        routes = _as_dict(state.get("location_travel_minutes", {}), "location travel minutes")
        direct = routes.get(f"{origin}->{destination}")
        reverse = routes.get(f"{destination}->{origin}")
        # Different locations without a seeded route are not adjacent.  A
        # missing route used to mean zero minutes and silently enabled
        # teleportation; treating it as unreachable makes the planner defer
        # until the world seed explicitly defines the transition.
        return int(direct if direct is not None else reverse if reverse is not None else 24 * 60)

    @staticmethod
    def _command_world_id(command: dict[str, object]) -> str:
        world_id = str(command.get("world_id") or "")
        if not world_id:
            raise WorldError("world command requires world_id")
        return world_id

    @staticmethod
    def _idempotency_key(command: dict[str, object]) -> str:
        return str(command.get("idempotency_key") or f"command:{uuid4()}")

    @staticmethod
    def _check_revision(actual: int, expected: int) -> None:
        if actual != expected:
            raise ConcurrencyConflict(f"expected revision {expected}, current revision is {actual}")


def reduce_events(events: list[WorldEvent]) -> dict[str, object]:
    state: dict[str, object] = _empty_state(events[0].world_id if events else "")
    for event in events:
        state = reduce_event(state, event)
    return state


def reduce_event(state: dict[str, object], event: WorldEvent) -> dict[str, object]:
    """Pure reducer: external I/O must be represented by a recorded event."""
    next_state = json.loads(_stable_json(state))
    payload = event.payload
    if event.event_type == "WorldStarted":
        protagonist = _as_dict(payload["protagonist"], "protagonist")
        next_state = _empty_state(event.world_id)
        next_state["clock"] = {"logical_at": payload["logical_at"], "mode": "paused", "rate": 0}
        next_state["world_started_at"] = payload["logical_at"]
        next_state["clock_observed_at"] = event.observed_at
        next_state["emotion_modulation"] = initial_affect(
            str(payload["logical_at"]),
            protagonist=protagonist,
            profile=payload.get("affect_profile", {}),
        )
        next_state["affect_profile"] = dict(
            _as_dict(payload.get("affect_profile", {}), "affect profile")
        )
        next_state["entities"] = {str(protagonist["id"]): {**protagonist, "status": "active"}}
        next_state["daily_schedule"] = payload.get("daily_schedule", [])
        next_state["weekly_themes"] = payload.get("weekly_themes", [])
        next_state["life_outcome_templates"] = payload.get("life_outcome_templates", {})
        next_state["location_travel_minutes"] = payload.get("location_travel_minutes", {})
        resources = _as_dict(protagonist.get("resources", {}), "protagonist resources")
        needs = _as_dict(next_state["needs"], "needs")
        for need in ("energy", "attention"):
            if need in resources:
                needs[need] = max(0, min(100, int(resources[need])))
        next_state["goals"] = {str(goal["id"]): {**goal, "progress": 0, "status": "active"} for goal in _as_list(payload.get("long_term_goals", []), "long-term goals")}
    elif event.event_type == "NpcRegistered":
        npc = dict(payload)
        npc["status"] = "active"
        _as_dict(next_state["entities"], "entities")[str(npc["id"])] = npc
    elif event.event_type == "UserRegistered":
        user = {**payload, "status": "active"}
        _as_dict(next_state["entities"], "entities")[str(user["id"])] = user
    elif event.event_type in {"CharacterCoreChanged", "CharacterCoreChangeRejected"}:
        changes = _as_list(next_state["character_core_changes"], "character core changes")
        changes.append({"event_type": event.event_type, "logical_at": event.logical_at, **payload})
        next_state["character_core_changes"] = changes[-32:]
        if event.event_type == "CharacterCoreChanged":
            entity = _as_dict(next_state["entities"], "entities")[str(payload["entity_id"])]
            field = str(payload["field"])
            values = [str(item) for item in _as_list(entity.get(field, []), "character core field")]
            value = str(payload["value"])
            if value not in values:
                values.append(value)
            entity[field] = values[-8:]
    elif event.event_type == "RelationshipStageEvaluated":
        entity_id = str(payload["entity_id"])
        relationships = _as_dict(next_state["relationships"], "relationships")
        relation = _as_dict(relationships.setdefault(entity_id, {}), "relationship")
        relation["stage"] = str(payload["stage"])
        relation["interaction_count"] = int(payload.get("interaction_count") or relation.get("interaction_count") or 0)
        relation["stage_reason"] = str(payload.get("reason") or "")
        relation["stage_rule_version"] = str(payload.get("rule_version") or "")
        if payload.get("from_stage") != payload.get("stage"):
            relation["stage_changed_at"] = event.logical_at
    elif event.event_type == "ClockModeChanged":
        next_state["clock"] = {**_as_dict(next_state["clock"], "clock"), **payload}
        next_state["clock_observed_at"] = event.observed_at
    elif event.event_type == "ClockAdvanced":
        _as_dict(next_state["clock"], "clock")["logical_at"] = payload["target_logical_at"]
        next_state["clock_observed_at"] = str(payload.get("observed_at") or event.observed_at)
    elif event.event_type in {"AffectChanged", "AffectCommitted", "AffectDecayed", "AffectResolved"}:
        affect_payload = dict(payload)
        # Segment duration is audit metadata on the event, not a mutable
        # world fact.  Keeping it out of the projection preserves identical
        # state hashes for long-jump and incremental replay.
        affect_payload.pop("elapsed_seconds", None)
        next_state["emotion_modulation"] = {
            **_as_dict(next_state["emotion_modulation"], "emotion modulation"),
            **affect_payload,
        }
    elif event.event_type == "RepairViolationCommitted":
        repair_cases = _as_dict(next_state["repair_cases"], "repair cases")
        for existing in repair_cases.values():
            existing_case = _as_dict(existing, "repair case")
            if existing_case.get("status") == "active":
                existing_case["status"] = "superseded_by_recurrence"
                existing_case["superseded_revision"] = event.revision
        violation_id = str(payload["violation_id"])
        repair_cases[violation_id] = {
            **payload,
            "status": "active",
            "violation_revision": event.revision,
            "commitments": {},
            "opportunities": {},
            "followthrough": {},
        }
    elif event.event_type == "RepairCommitmentCommitted":
        repair_case = _as_dict(
            _as_dict(next_state["repair_cases"], "repair cases")[
                str(payload["violation_id"])
            ],
            "repair case",
        )
        _as_dict(repair_case["commitments"], "repair commitments")[
            str(payload["commitment_id"])
        ] = {**payload, "committed_revision": event.revision}
    elif event.event_type == "RepairOpportunityObserved":
        opportunity = {**payload, "observed_revision": event.revision}
        _as_dict(next_state["repair_opportunities"], "repair opportunities")[
            str(payload["opportunity_id"])
        ] = opportunity
        repair_case = _as_dict(
            _as_dict(next_state["repair_cases"], "repair cases")[
                str(payload["violation_id"])
            ],
            "repair case",
        )
        _as_dict(repair_case["opportunities"], "repair opportunities")[
            str(payload["opportunity_id"])
        ] = opportunity
    elif event.event_type == "RepairFollowthroughCommitted":
        repair_case = _as_dict(
            _as_dict(next_state["repair_cases"], "repair cases")[
                str(payload["violation_id"])
            ],
            "repair case",
        )
        opportunity = _as_dict(
            _as_dict(repair_case["opportunities"], "repair opportunities")[
                str(payload["opportunity_id"])
            ],
            "repair opportunity",
        )
        if event.revision <= int(opportunity.get("observed_revision") or 0):
            raise WorldError("repair followthrough must occur after its opportunity")
        _as_dict(repair_case["followthrough"], "repair followthrough")[
            str(payload["opportunity_id"])
        ] = {**payload, "committed_revision": event.revision}
    elif event.event_type == "ActivityPlanned":
        item = {**payload, "status": "planned"}
        _as_dict(next_state["agenda"], "agenda")[str(item["activity_id"])] = item
    elif event.event_type == "ActivityPlanSelected":
        activity = _as_dict(next_state["agenda"], "agenda")[
            str(payload["activity_id"])
        ]
        activity["template_id"] = str(payload["template_id"])
        activity["location"] = str(payload["location"])
        if payload.get("substitution_reason"):
            activity["substitution_reason"] = str(payload["substitution_reason"])
    elif event.event_type == "WeeklyPlanCreated":
        _as_dict(next_state["weekly_plans"], "weekly plans")[
            str(payload["week_id"])
        ] = {**payload, "themes": {}}
    elif event.event_type == "WeeklyThemePlanned":
        plan = _as_dict(next_state["weekly_plans"], "weekly plans")[
            str(payload["week_id"])
        ]
        _as_dict(plan["themes"], "weekly plan themes")[
            str(payload["theme_id"])
        ] = dict(payload)
    elif event.event_type == "LifeInfluenceRecorded":
        evolution = _as_dict(next_state["life_evolution"], "life evolution")
        _as_dict(evolution["influences"], "life influences")[
            str(payload["influence_id"])
        ] = {**payload, "status": "active"}
    elif event.event_type == "LifeInfluenceExpired":
        influence = _as_dict(
            _as_dict(next_state["life_evolution"], "life evolution")[
                "influences"
            ],
            "life influences",
        )[str(payload["influence_id"])]
        influence["status"] = "expired"
        influence["terminal_reason"] = str(payload["reason"])
    elif event.event_type == "FutureActivityAdjusted":
        activity_id = str(payload["activity_id"])
        activity = _as_dict(next_state["agenda"], "agenda").get(activity_id)
        if activity is None:
            for raw_plan in _as_dict(
                next_state["weekly_plans"], "weekly plans"
            ).values():
                plan = _as_dict(raw_plan, "weekly plan")
                for raw_theme in _as_dict(
                    plan.get("themes", {}), "weekly plan themes"
                ).values():
                    theme = _as_dict(raw_theme, "weekly plan theme")
                    activity = next(
                        (
                            _as_dict(item, "weekly theme activity")
                            for item in _as_list(
                                theme.get("activities", []),
                                "weekly theme activities",
                            )
                            if str(
                                _as_dict(item, "weekly theme activity").get(
                                    "activity_id"
                                )
                                or ""
                            )
                            == activity_id
                        ),
                        None,
                    )
                    if activity is not None:
                        break
                if activity is not None:
                    break
        if activity is None:
            raise WorldError("future activity adjustment has no planned activity")
        activity["attention_demand"] = int(payload["attention_demand"])
        activity["preference_bias"] = str(payload["preference_bias"])
        activity["last_influence_id"] = str(payload["influence_id"])
    elif event.event_type == "EnvironmentObservationRecorded":
        evolution = _as_dict(next_state["life_evolution"], "life evolution")
        _as_dict(evolution["observations"], "environment observations")[
            str(payload["observation_id"])
        ] = {**payload, "status": "active"}
    elif event.event_type == "EnvironmentObservationExpired":
        observation = _as_dict(
            _as_dict(next_state["life_evolution"], "life evolution")[
                "observations"
            ],
            "environment observations",
        )[str(payload["observation_id"])]
        observation["status"] = "expired"
        observation["terminal_reason"] = str(payload["reason"])
    elif event.event_type == "LifePressureRecorded":
        evolution = _as_dict(next_state["life_evolution"], "life evolution")
        _as_list(evolution["pressure_samples"], "life pressure samples").append(
            dict(payload)
        )
        evolution["chronic"] = dict(
            _as_dict(payload["chronic"], "chronic life pressure")
        )
    elif event.event_type in {"ActivityStarted", "ActivityCompleted", "ActivityInterrupted", "ActivityCancelled", "ActivityRested", "ActivityDeferred", "ActivityResumed"}:
        activity = _as_dict(next_state["agenda"], "agenda")[str(payload["activity_id"])]
        activity["status"] = {
            "ActivityStarted": "active", "ActivityCompleted": "completed",
            "ActivityInterrupted": "interrupted", "ActivityCancelled": "cancelled",
            "ActivityRested": "rested",
            "ActivityDeferred": "deferred", "ActivityResumed": "planned",
        }[event.event_type]
        if event.event_type == "ActivityRested":
            activity["reason"] = payload["reason"]
            needs = _as_dict(next_state["needs"], "needs")
            needs["energy"] = max(0, min(100, int(needs.get("energy", 50)) + int(payload["energy_delta"])))
        if event.event_type == "ActivityDeferred":
            activity["reason"] = payload["reason"]
            activity["next_review_at"] = payload["next_review_at"]
    elif event.event_type == "ToolProposed":
        _as_dict(next_state["tool_actions"], "tool actions")[str(payload["proposal_id"])] = {
            **payload,
            "status": "proposed",
            "confirmation_required": True,
            "action_id": None,
            "completed_in_reality": False,
            "result_summary": None,
        }
    elif event.event_type == "UserConfirmationRequired":
        proposal = _as_dict(next_state["tool_actions"], "tool actions")[
            str(payload["proposal_id"])
        ]
        proposal["status"] = "awaiting_confirmation"
        proposal["confirmation"] = dict(payload)
    elif event.event_type == "ToolAuthorized":
        proposal = _as_dict(next_state["tool_actions"], "tool actions")[
            str(payload["proposal_id"])
        ]
        proposal["status"] = "authorized"
        proposal["action_id"] = str(payload["action_id"])
        proposal["authorization"] = dict(payload)
    elif event.event_type == "ToolRejected":
        proposal = _as_dict(next_state["tool_actions"], "tool actions")[
            str(payload["proposal_id"])
        ]
        proposal["status"] = "rejected"
        proposal["completed_in_reality"] = False
        proposal["result_summary"] = str(payload["result_summary"])
        proposal["rejection"] = dict(payload)
    elif event.event_type == "ActionScheduled":
        item = {**payload, "status": "scheduled"}
        _as_dict(next_state["actions"], "actions")[str(item["action_id"])] = item
    elif event.event_type == "ActionSegmentsPlanned":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["segment_state"] = {
            "action_id": str(payload["action_id"]),
            "status": "planned",
            "segments": list(_as_list(payload["segments"], "segments")),
        }
    elif event.event_type == "ActionSegmentDispatchClaimed":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        segment_state = _as_dict(action["segment_state"], "segment state")
        for raw_segment in _as_list(segment_state["segments"], "segments"):
            segment = _as_dict(raw_segment, "segment")
            if str(segment["segment_id"]) == str(payload["segment_id"]):
                segment["status"] = "sending"
        segment_state["status"] = "sending"
        action["status"] = "sending"
        action["lease_expires_observed_at"] = str(
            payload.get("lease_expires_observed_at") or ""
        )
    elif event.event_type == "ActionSegmentSettled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        segment_state = _as_dict(action["segment_state"], "segment state")
        delivered_segment: dict[str, object] | None = None
        for raw_segment in _as_list(segment_state["segments"], "segments"):
            segment = _as_dict(raw_segment, "segment")
            if str(segment["segment_id"]) == str(payload["segment_id"]):
                segment["status"] = "delivered"
                result = _as_dict(payload["result"], "segment result")
                segment["external_receipt"] = result.get("external_receipt")
                segment["terminal_reason"] = None
                delivered_segment = segment
        statuses = {
            str(_as_dict(item, "segment")["status"])
            for item in _as_list(segment_state["segments"], "segments")
        }
        aggregate_status = next(
            (
                status
                for status in ("unknown", "sending", "planned", "cancelled")
                if status in statuses
            ),
            "delivered",
        )
        segment_state["status"] = aggregate_status
        action["status"] = "scheduled" if aggregate_status == "planned" else aggregate_status
        if delivered_segment is not None:
            history = _as_list(next_state["recent_messages"], "recent messages")
            trace = _as_dict(action.get("trace", {}), "outgoing trace")
            history.append(
                {
                    "direction": "out",
                    "message_id": str(delivered_segment["segment_id"]),
                    "text": str(delivered_segment["text"]),
                    "sent_at": event.logical_at,
                    "logical_at": event.logical_at,
                    "observed_at": event.observed_at,
                    "user_id": str(trace.get("user_id") or ""),
                    "source_action_id": str(payload["action_id"]),
                    "outgoing_direction": str(trace.get("direction") or ""),
                    "segment_id": str(delivered_segment["segment_id"]),
                }
            )
            next_state["recent_messages"] = history[-64:]
    elif event.event_type == "ActionSegmentDispatchAccepted":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        segment_state = _as_dict(action["segment_state"], "segment state")
        for raw_segment in _as_list(segment_state["segments"], "segments"):
            segment = _as_dict(raw_segment, "segment")
            if str(segment["segment_id"]) == str(payload["segment_id"]):
                segment["receipt_lookup_token"] = str(payload["lookup_token"])
    elif event.event_type == "ActionSegmentDeliveryUncertain":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        segment_state = _as_dict(action["segment_state"], "segment state")
        for raw_segment in _as_list(segment_state["segments"], "segments"):
            segment = _as_dict(raw_segment, "segment")
            if str(segment["segment_id"]) == str(payload["segment_id"]):
                segment["status"] = "unknown"
                segment["terminal_reason"] = str(payload.get("reason") or "")
        segment_state["status"] = "unknown"
        action["status"] = "unknown"
    elif event.event_type == "ActionSegmentsCancelled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        segment_state = _as_dict(action["segment_state"], "segment state")
        cancelled_ids = {str(item) for item in _as_list(payload["segment_ids"], "segment ids")}
        for raw_segment in _as_list(segment_state["segments"], "segments"):
            segment = _as_dict(raw_segment, "segment")
            if str(segment["segment_id"]) in cancelled_ids:
                segment["status"] = "cancelled"
                segment["terminal_reason"] = str(
                    payload.get("terminal_reason")
                    or f"interrupted_by:{payload['user_message_id']}"
                )
        statuses = {
            str(_as_dict(item, "segment").get("status") or "")
            for item in _as_list(segment_state["segments"], "segments")
        }
        aggregate_status = next(
            (
                status
                for status in ("unknown", "sending", "planned", "cancelled")
                if status in statuses
            ),
            "delivered",
        )
        segment_state["status"] = aggregate_status
        action["status"] = aggregate_status
        action["reason"] = str(payload.get("reason") or "")
    elif event.event_type == "ActionAttempted":
        _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]["attempted"] = True
    elif event.event_type == "ActionDispatchClaimed":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "sending"
        action["lease_expires_observed_at"] = str(payload.get("lease_expires_observed_at") or "")
    elif event.event_type == "ActionSettled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = str(_as_dict(payload["result"], "result")["status"])
        action["result"] = payload["result"]
        _reduce_media_action_terminal(next_state, action, str(_as_dict(payload["result"], "result")["status"]))
        _reduce_reaction_action_terminal(
            next_state, action, str(_as_dict(payload["result"], "result")["status"])
        )
        result = _as_dict(payload["result"], "result")
        if (
            action.get("kind") == "outgoing_message"
            and action["status"] == "delivered"
            and not bool(result.get("segmented"))
        ):
            history = _as_list(next_state["recent_messages"], "recent_messages")
            trace = _as_dict(action.get("trace", {}), "outgoing trace")
            history.append({
                "direction": "out", "message_id": str(payload["action_id"]),
                "text": str(action.get("text") or ""), "sent_at": event.logical_at,
                "logical_at": event.logical_at,
                "observed_at": event.observed_at,
                "user_id": str(trace.get("user_id") or ""),
                "source_action_id": str(payload["action_id"]), "outgoing_direction": str(trace.get("direction") or ""),
            })
            next_state["recent_messages"] = history[-64:]
    elif event.event_type == "ActionExpired":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "expired"
        action["reason"] = payload.get("reason")
        _reduce_media_action_terminal(next_state, action, "expired")
        _reduce_reaction_action_terminal(next_state, action, "expired")
    elif event.event_type == "ActionCancelled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "cancelled"
        action["reason"] = payload.get("reason")
        _reduce_media_action_terminal(next_state, action, "cancelled")
        _reduce_reaction_action_terminal(next_state, action, "cancelled")
    elif event.event_type == "ActionDeliveryUncertain":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "unknown"
        action["reason"] = payload.get("reason")
        _reduce_reaction_action_terminal(next_state, action, "unknown")
    elif event.event_type == "ExternalResultRecorded":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        if action.get("kind") == "tool_execution":
            proposal_id = str(
                _as_dict(action.get("payload", {}), "tool action payload")["proposal_id"]
            )
            proposal = _as_dict(next_state["tool_actions"], "tool actions")[proposal_id]
            proposal["result"] = payload["result"]
            proposal["status"] = "result_recorded"
    elif event.event_type == "NecessaryResultSummarized":
        proposal = _as_dict(next_state["tool_actions"], "tool actions")[
            str(payload["proposal_id"])
        ]
        action_status = str(payload["action_status"])
        proposal["status"] = "simulated" if action_status == "delivered" else action_status
        proposal["completed_in_reality"] = bool(payload["completed_in_reality"])
        proposal["result_summary"] = str(payload["summary"])
    elif event.event_type in {"OutboundActionAllowed", "OutboundActionRejected", "OutboundSoftGateOverridden"}:
        audit = _as_list(next_state.setdefault("outbound_policy_audit", []), "outbound policy audit")
        audit.append({"event_type": event.event_type, "logical_at": event.logical_at, **payload})
        next_state["outbound_policy_audit"] = audit[-64:]
    elif event.event_type == "ControlledTransgressionCommitted":
        history = _as_list(next_state["controlled_transgressions"], "controlled transgressions")
        history.append(dict(payload))
        next_state["controlled_transgressions"] = history[-64:]
        needs = _as_dict(next_state["needs"], "needs")
        needs["initiative"] = max(
            0,
            int(needs.get("initiative", 0)) - max(1, int(payload["relationship_cost"]) // 4),
        )
        needs["security"] = max(
            0, int(needs.get("security", 0)) - int(payload["affect_cost"])
        )
    elif event.event_type in {
        "CostReservationDecided", "CostReservationSettled", "CostReservationReleased"
    }:
        cost_ledger = _as_dict(next_state["cost_ledger"], "cost ledger")
        recorded = _as_list(cost_ledger["events"], "cost events")
        recorded.append({"event_type": event.event_type, "payload": dict(payload)})
        ledger = WorldCostLedger.from_events(
            WorldKernel.COST_POLICY,
            tuple(
                CostLedgerEvent(
                    str(_as_dict(raw, "cost event")["event_type"]),
                    _as_dict(_as_dict(raw, "cost event")["payload"], "cost payload"),
                )
                for raw in recorded
            ),
        )
        days = sorted(
            {
                str(
                    _as_dict(
                        _as_dict(raw, "cost event").get("payload", {}), "cost payload"
                    ).get("request", {}).get("logical_day")
                )
                for raw in recorded
                if _as_dict(raw, "cost event").get("event_type") == "CostReservationDecided"
                and isinstance(_as_dict(raw, "cost event").get("payload", {}).get("request"), dict)
            }
        )
        usage: dict[str, object] = {}
        for day in (item for item in days if item and item != "None"):
            categories: dict[str, object] = {}
            for category in ALL_COST_CATEGORIES:
                item = ledger.usage(day, category)
                categories[category] = {
                    "reserved_units": item.reserved_units,
                    "settled_units": item.settled_units,
                    "total_units": item.total_units,
                }
            usage[day] = categories
        cost_ledger["events"] = recorded
        cost_ledger["usage"] = usage
    elif event.event_type in {"NpcRelationshipChanged", "RelationshipChanged"}:
        relationships = _as_dict(next_state["relationships"], "relationships")
        relation = _as_dict(relationships.setdefault(str(payload["entity_id"]), {}), "relationship")
        dimension = str(payload["dimension"])
        relation[dimension] = max(-100, min(100, int(relation.get(dimension, 0)) + int(payload["delta"])))
    elif event.event_type == "RelationshipAppraised":
        next_state["last_relationship_appraisal"] = dict(payload)
        user_id = str(payload.get("user_id") or "")
        if user_id:
            relationships = _as_dict(next_state["relationships"], "relationships")
            relation = _as_dict(relationships.setdefault(user_id, {}), "user relationship")
            relation.setdefault("stage", "stranger")
            relation["interaction_count"] = int(relation.get("interaction_count") or 0) + 1
    elif event.event_type == "EmotionModulated":
        current = _as_dict(next_state["emotion_modulation"], "emotion modulation")
        next_state["emotion_modulation"] = {
            **current,
            "mode": payload["mode"], "expression": payload["expression"], "reason": payload["reason"],
            "charge": max(0, min(100, int(current.get("charge", 0)) + int(payload["charge_delta"]))),
            "last_decay_at": event.logical_at,
        }
    elif event.event_type == "NeedChanged":
        needs = _as_dict(next_state["needs"], "needs")
        need = str(payload["need"])
        needs[need] = max(0, min(100, int(needs.get(need, 50)) + int(payload["delta"])))
    elif event.event_type == "MessageAttentionDecided":
        communication = {
            "message_id": payload["message_id"], "attention": payload["attention"], "typing": "idle",
            "reason": payload["reason"], "due_at": payload["due_at"],
            "deferred_action_id": payload["deferred_action_id"],
        }
        if payload.get("rule_version"):
            communication["rule_version"] = payload["rule_version"]
        if payload.get("candidates"):
            communication["candidates"] = list(
                _as_list(payload["candidates"], "attention candidates")
            )
        next_state["communication"] = communication
    elif event.event_type == "TypingStateChanged":
        communication = _as_dict(next_state["communication"], "communication")
        communication["typing"] = "started" if payload["typing"] == "started" else "idle"
        communication["reason"] = payload["reason"]
    elif event.event_type == "DecisionDeferred":
        _as_dict(next_state["decisions"], "decisions")[str(payload["decision_id"])] = {**payload, "status": "deferred"}
    elif event.event_type == "DecisionResolved":
        decision = _as_dict(_as_dict(next_state["decisions"], "decisions")[str(payload["decision_id"])], "decision")
        decision["status"] = payload["outcome"]
        decision["resolution_reason"] = payload["reason"]
    elif event.event_type == "ConversationThreadOpened":
        _as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])] = {
            **payload, "status": "open",
        }
    elif event.event_type == "ConversationThreadResolved":
        thread = _as_dict(_as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])], "conversation thread")
        thread["status"] = str(payload["outcome"])
        thread["resolution_reason"] = str(payload["reason"])
        thread["terminal_state"] = "resolved"
        thread["terminal_outcome"] = str(payload["outcome"])
    elif event.event_type == "ConversationThreadCancelled":
        thread = _as_dict(_as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])], "conversation thread")
        if thread.get("status") == "open":
            thread["status"] = "cancelled"
            thread["terminal_state"] = "cancelled"
            thread["terminal_reason"] = str(payload["reason"])
            thread["terminal_condition"] = str(payload["condition"])
    elif event.event_type == "ConversationThreadWaitingChanged":
        thread = _as_dict(_as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])], "conversation thread")
        if thread.get("status") == "open":
            thread["waiting_phase"] = str(payload["phase"])
            thread["waiting_changed_at"] = event.logical_at
            thread["waiting_reason"] = str(payload["reason"])
            thread["waiting_expression_policy"] = str(payload["expression_policy"])
            thread["waiting_next_review_at"] = payload.get("next_review_at")
    elif event.event_type == "ConversationThreadExpired":
        thread = _as_dict(_as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])], "conversation thread")
        if thread.get("status") == "open":
            thread["status"] = "expired"
            thread["resolution_reason"] = str(payload["reason"])
            thread["terminal_state"] = "expired"
            thread["terminal_reason"] = str(payload["reason"])
    elif event.event_type == "PrivateImpressionCommitted":
        _as_dict(next_state.setdefault("private_impressions", {}), "private impressions")[
            str(payload["impression_id"])
        ] = {
            **payload,
            "status": "active",
            "committed_at": event.logical_at,
            "committed_revision": event.revision,
        }
    elif event.event_type == "PrivateImpressionContradicted":
        impression = _as_dict(
            _as_dict(next_state.setdefault("private_impressions", {}), "private impressions").get(
                str(payload["impression_id"])
            ),
            "private impression",
        )
        if impression.get("status") == "active":
            impression["status"] = "contradicted"
            impression["contradicted_at"] = event.logical_at
            impression["contradiction_reason"] = str(payload["reason"])
            impression["contradictory_evidence"] = list(payload["source_event_ids"])
    elif event.event_type == "PrivateCommitmentCommitted":
        _as_dict(next_state.setdefault("private_commitments", {}), "private commitments")[
            str(payload["commitment_id"])
        ] = {
            **payload,
            "status": "active",
            "committed_at": event.logical_at,
            "committed_revision": event.revision,
        }
    elif event.event_type == "PrivateImpressionExpired":
        impression = _as_dict(
            _as_dict(next_state.setdefault("private_impressions", {}), "private impressions").get(
                str(payload["impression_id"])
            ),
            "private impression",
        )
        if impression.get("status") == "active":
            impression["status"] = "expired"
            impression["expired_at"] = event.logical_at
            impression["expiry_reason"] = str(payload["reason"])
    elif event.event_type == "PrivateCommitmentResolved":
        commitment = _as_dict(
            _as_dict(next_state.setdefault("private_commitments", {}), "private commitments").get(
                str(payload["commitment_id"])
            ),
            "private commitment",
        )
        if commitment.get("status") == "active":
            commitment["status"] = str(payload["outcome"])
            commitment["resolved_at"] = event.logical_at
            commitment["resolution_reason"] = str(payload["reason"])
    elif event.event_type == "PrivateCommitmentExpired":
        commitment = _as_dict(
            _as_dict(next_state.setdefault("private_commitments", {}), "private commitments").get(
                str(payload["commitment_id"])
            ),
            "private commitment",
        )
        if commitment.get("status") == "active":
            commitment["status"] = "expired"
            commitment["expired_at"] = event.logical_at
            commitment["expiry_reason"] = str(payload["reason"])
    elif event.event_type == "MediaRequested":
        _as_dict(next_state["media"], "media")[str(payload["request_id"])] = {**payload, "status": "requested"}
    elif event.event_type == "MediaRequestRejected":
        _as_dict(next_state["media"], "media")[str(payload["request_id"])] = {**payload, "status": "rejected"}
    elif event.event_type == "MediaGenerated":
        media = _as_dict(_as_dict(next_state["media"], "media")[str(payload["request_id"])], "media request")
        media.update({"status": "generated", "artifact_path": payload["artifact_path"], "artifact_hash": payload["artifact_hash"], "generation_action_id": payload["action_id"]})
    elif event.event_type == "MediaShared":
        media = _as_dict(_as_dict(next_state["media"], "media")[str(payload["request_id"])], "media request")
        media["status"] = "shared"
        media["delivery_action_id"] = payload["action_id"]
    elif event.event_type == "NpcInteractionCommitted":
        _as_dict(next_state["npc_interactions"], "npc interactions")[str(payload["interaction_id"])] = dict(payload)
    elif event.event_type == "StickerSelected":
        _as_dict(next_state["stickers"], "stickers")[str(payload["action_id"])] = {**payload, "status": "selected"}
    elif event.event_type == "StickerShared":
        sticker = _as_dict(_as_dict(next_state["stickers"], "stickers")[str(payload["action_id"])], "sticker")
        sticker["status"] = "shared"
    elif event.event_type == "ReactionSelected":
        _as_dict(next_state["reactions"], "reactions")[str(payload["action_id"])] = {
            **payload, "status": "selected"
        }
    elif event.event_type == "ReactionShared":
        reaction = _as_dict(
            _as_dict(next_state["reactions"], "reactions")[str(payload["action_id"])],
            "reaction",
        )
        reaction["status"] = "shared"
        reaction["external_receipt"] = payload.get("external_receipt")
    elif event.event_type == "ModelProposalRecorded":
        item = {**payload, "status": "recorded"}
        _as_dict(next_state["proposals"], "proposals")[str(item["proposal_id"])] = item
    elif event.event_type == "ModelProposalAccepted":
        _as_dict(next_state["proposals"], "proposals")[str(payload["proposal_id"])]["status"] = "accepted"
    elif event.event_type == "ExperienceCommitted":
        item = dict(payload)
        _as_dict(next_state["experiences"], "experiences")[str(item["experience_id"])] = item
    elif event.event_type == "ExperienceAppraised":
        experience = _as_dict(next_state["experiences"], "experiences").get(
            str(payload["outcome_id"])
        )
        if isinstance(experience, dict):
            experience["affect_appraisal"] = str(payload["appraisal"])
            experience["affect_intensity"] = int(payload["intensity"])
    elif event.event_type == "LifeOutcomeProposed":
        _as_dict(next_state["proposals"], "proposals")[str(payload["outcome_id"])] = {**payload, "status": "proposed"}
    elif event.event_type == "LifeOutcomeCommitted":
        _as_dict(next_state.setdefault("outcomes", {}), "outcomes")[str(payload["outcome_id"])] = {**payload, "status": "committed"}
        _as_dict(next_state["proposals"], "proposals")[str(payload["outcome_id"])] ["status"] = "committed"
    elif event.event_type == "LifeOutcomeValidated":
        _as_dict(next_state["proposals"], "proposals")[str(payload["outcome_id"])] ["validated"] = True
    elif event.event_type == "LifeOutcomeRejected":
        proposal = _as_dict(next_state["proposals"], "proposals").get(str(payload["outcome_id"]))
        if proposal is not None:
            proposal["status"] = "rejected"
            proposal["rejection_reason"] = payload["reason"]
    elif event.event_type == "GoalProgressed":
        goal = _as_dict(_as_dict(next_state["goals"], "goals").get(str(payload["goal_id"])), "goal")
        goal["progress"] = min(int(goal["target"]), int(goal["progress"]) + int(payload["delta"]))
        if goal["progress"] >= int(goal["target"]):
            goal["status"] = "completed"
    elif event.event_type == "GoalDeferred":
        goal = _as_dict(_as_dict(next_state["goals"], "goals").get(str(payload["goal_id"])), "goal")
        if goal["status"] == "active":
            goal["status"] = "deferred"
            goal["deferred_reason"] = payload["reason"]
            goal["next_review_at"] = payload["next_review_at"]
    elif event.event_type == "GoalReviewDue":
        goal = _as_dict(_as_dict(next_state["goals"], "goals").get(str(payload["goal_id"])), "goal")
        if goal["status"] == "deferred":
            goal["status"] = "review_due"
    elif event.event_type == "GoalResumed":
        goal = _as_dict(_as_dict(next_state["goals"], "goals").get(str(payload["goal_id"])), "goal")
        goal["status"] = "active"
        goal["deadline"] = payload["deadline"]
        goal.pop("next_review_at", None)
    elif event.event_type == "GoalAbandoned":
        goal = _as_dict(_as_dict(next_state["goals"], "goals").get(str(payload["goal_id"])), "goal")
        goal["status"] = "abandoned"
        goal["abandoned_reason"] = payload["reason"]
    elif event.event_type == "GoalCompensated":
        needs = _as_dict(next_state["needs"], "needs")
        need = str(payload["need"])
        needs[need] = max(0, min(100, int(needs.get(need, 50)) + int(payload["delta"])))
    elif event.event_type == "ExperienceShared":
        _as_dict(next_state["experiences"], "experiences")[str(payload["experience_id"])]["shared"] = True
        _as_dict(next_state["experiences"], "experiences")[str(payload["experience_id"])]["shared_action_id"] = payload["action_id"]
        day = str(_as_dict(next_state["clock"], "clock")["logical_at"])[:10]
        _as_dict(next_state.setdefault("share_days", {}), "share days")[day] = payload["experience_id"]
    elif event.event_type == "LifeShareSelected":
        day = str(_as_dict(next_state["clock"], "clock")["logical_at"])[:10]
        _as_dict(next_state.setdefault("share_decisions", {}), "share decisions")[day] = dict(payload)
    elif event.event_type == "FactConfirmed":
        item = dict(payload)
        item["valid_from"] = event.logical_at
        item["pinned"] = bool(item.get("pinned", False))
        item["importance"] = int(item.get("importance") or 50)
        facts = _as_dict(next_state["facts"], "facts")
        conflict_key = str(item.get("conflict_key") or "")
        if conflict_key:
            for previous in facts.values():
                prior = _as_dict(previous, "fact")
                if (
                    _fact_is_current(prior)
                    and str(prior.get("subject") or "") == str(item.get("subject") or "")
                    and str(prior.get("conflict_key") or "") == conflict_key
                ):
                    prior["status"] = "superseded"
                    prior["superseded_by"] = str(item["fact_id"])
                    prior["valid_to"] = event.logical_at
        facts[str(item["fact_id"])] = item
    elif event.event_type == "UserMessageObserved":
        history = _as_list(next_state["recent_messages"], "recent_messages")
        history.append(
            {
                "direction": "in",
                "logical_at": event.logical_at,
                "observed_at": event.observed_at,
                **payload,
            }
        )
        next_state["recent_messages"] = history[-64:]
        next_state["communication"] = {
            "message_id": payload.get("message_id"), "attention": "unread", "typing": "idle",
            "reason": "message_observed", "due_at": None, "deferred_action_id": None,
        }
    elif event.event_type == "InputMergeCandidateObserved":
        merges = _as_dict(next_state["input_merges"], "input merges")
        merge_key = str(payload["merge_key"])
        merge = _as_dict(
            merges.setdefault(
                merge_key,
                {
                    "merge_key": merge_key,
                    "status": "pending",
                    "messages": [],
                },
            ),
            "input merge",
        )
        if merge.get("status") != "pending":
            merge["messages"] = []
        messages = _as_list(merge["messages"], "merge messages")
        messages.append(dict(_as_dict(payload["message"], "merge message")))
        merge.update(
            {
                "status": "pending",
                "pending_count": int(payload["pending_count"]),
                "wait_seconds": float(payload["wait_seconds"]),
                "reason": str(payload["reason"]),
                "max_batch": int(payload["max_batch"]),
                "rule_version": str(payload["rule_version"]),
                "updated_at": event.observed_at,
            }
        )
    elif event.event_type == "InputMergeSettled":
        merge = _as_dict(next_state["input_merges"], "input merges")[str(payload["merge_key"])]
        merge["status"] = "settled"
        merge["terminal_state"] = str(payload["terminal_state"])
        merge["merged_message_id"] = str(payload["merged_message_id"])
        merge["settled_at"] = event.observed_at
    elif event.event_type == "TurnProcessingClaimed":
        _as_dict(next_state.setdefault("turns", {}), "turns")[str(payload["message_id"])] = {
            "message_id": str(payload["message_id"]), "status": "claimed",
        }
    elif event.event_type == "TurnProcessingSettled":
        turn = _as_dict(
            _as_dict(next_state.setdefault("turns", {}), "turns").get(str(payload["message_id"])),
            "turn",
        )
        turn["status"] = str(payload["status"])
        turn["reason"] = str(payload["reason"])
    elif event.event_type == "TurnAppraised":
        next_state["last_appraisal"] = dict(payload)
        message_id = str(payload.get("message_id") or "")
        raw_turn = _as_dict(next_state.setdefault("turns", {}), "turns").get(message_id)
        if isinstance(raw_turn, dict):
            turn = raw_turn
            turn["appraisal"] = str(payload.get("appraisal") or "")
            turn["user_id"] = str(payload.get("user_id") or "")
    elif event.event_type == "UserAffectAppraised":
        user_id = str(payload["user_id"])
        affect_by_user = _as_dict(
            next_state.setdefault("user_affect", {}), "user affect"
        )
        previous = affect_by_user.get(user_id, {})
        active_episodes = {
            str(item.get("source_message_id") or ""): dict(item)
            for item in (
                _as_list(previous.get("active_episodes", []), "active user affect episodes")
                if isinstance(previous, dict)
                else []
            )
            if isinstance(item, dict) and str(item.get("source_message_id") or "")
        }
        if (
            isinstance(previous, dict)
            and bool(previous.get("unresolved"))
            and str(previous.get("source_message_id") or "")
        ):
            active_episodes.setdefault(
                str(previous["source_message_id"]),
                {key: value for key, value in previous.items() if key != "active_episodes"},
            )
        source_message_id = str(payload.get("source_message_id") or "")
        settles_source = str(payload.get("settles_source_message_id") or "")
        if bool(payload.get("unresolved")) and source_message_id:
            # A terse continuation updates the same semantic episode instead
            # of leaking one active entry per message. Different kinds (for
            # example disappointment plus confusion) remain independently
            # visible until an explicit repair settles them.
            for episode_id, episode in list(active_episodes.items()):
                if (
                    str(episode.get("kind") or "") == str(payload.get("kind") or "")
                    and str(episode.get("cause") or "") == str(payload.get("cause") or "")
                ):
                    active_episodes.pop(episode_id, None)
            active_episodes[source_message_id] = dict(payload)
        settles_sources = {
            str(item)
            for item in _as_list(
                payload.get("settles_source_message_ids", []),
                "settled user affect sources",
            )
            if str(item)
        }
        if settles_source:
            settles_sources.add(settles_source)
        for settled_id in settles_sources:
            active_episodes.pop(settled_id, None)
        projected = dict(payload)
        if not bool(payload.get("unresolved")) and active_episodes:
            projected = dict(next(reversed(active_episodes.values())))
        affect_by_user[user_id] = {
            **projected,
            "active_episodes": list(active_episodes.values()),
            "appraised_at": event.observed_at,
        }
    elif event.event_type == "AffectDisplaySelected":
        next_state["last_affect_display"] = dict(payload)
    elif event.event_type == "AffinityInteractionSettled":
        user_id = str(payload["user_id"])
        _as_dict(next_state["long_term_affinity"], "long-term affinity")[user_id] = dict(
            _as_dict(payload["state"], "settled affinity state")
        )
    elif event.event_type == "UserRequestAppraised":
        next_state["last_user_request"] = dict(payload)
    elif event.event_type == "MotiveConflictEvaluated":
        next_state["last_motive_conflict"] = dict(payload)
    elif event.event_type == "StanceSelected":
        next_state["last_deliberation"] = dict(payload)
    elif event.event_type == "IntentCreated":
        _as_dict(next_state["intents"], "intents")[str(payload["intent_id"])] = dict(payload)
    elif event.event_type == "IntentFailed":
        intent = _as_dict(next_state["intents"], "intents")[str(payload["intent_id"])]
        intent["status"] = "failed"
        intent["reason"] = payload["reason"]
    return next_state


_OVERRIDABLE_OUTBOUND_GATES = frozenset(
    {"global_cooldown", "trigger_cooldown", "unanswered_budget", "topic_similarity"}
)


def _outbound_soft_override(
    raw: object, failed_reasons: tuple[str, ...]
) -> dict[str, object] | None:
    """Accept an explicit costly override only when every failed gate is soft."""
    override = _as_dict(raw, "outbound override") if raw is not None else {}
    if not override:
        return None
    reason = str(override.get("reason") or "").strip()
    cost = int(override.get("cost") or 0)
    strike = int(override.get("strike") or 0)
    if (
        not reason
        or len(reason) > 160
        or cost <= 0
        or strike <= 0
        or not set(failed_reasons).issubset(_OVERRIDABLE_OUTBOUND_GATES)
    ):
        return None
    declared_gates = [str(value) for value in _as_list(override.get("gates", []), "outbound override gates")]
    if not failed_reasons and not declared_gates:
        return None
    if any(not gate.startswith("outreach:") for gate in declared_gates):
        return None
    return {
        "reason": reason,
        "cost": cost,
        "strike": strike,
        "overridden_gates": list(failed_reasons) or declared_gates,
    }


def _outbound_policy_payload(*, request, projection, allowance, override) -> dict[str, object]:
    return {
        "request_id": request.request_id,
        "kind": request.kind.value,
        "trigger": request.trigger,
        "topic_key": request.topic_key,
        "allowed_by_policy": allowance.allowed,
        "allowed_after_override": allowance.allowed or override is not None,
        "reasons": list(allowance.reasons),
        "retry_at": allowance.retry_at.isoformat() if allowance.retry_at else None,
        "checks": {
            check.name: {
                "passed": check.passed,
                "detail": check.detail,
                "retry_at": check.retry_at.isoformat() if check.retry_at else None,
            }
            for check in allowance.checks
        },
        "projection": {
            "last_outbound_at": projection.last_outbound_at.isoformat()
            if projection.last_outbound_at else None,
            "unanswered_outbound_count": projection.unanswered_outbound_count,
            "generation_lock_owner": projection.generation_lock_owner,
            "recent_outbound_count": len(projection.recent_outbounds),
        },
        "override": override,
        "rule_version": "outbound-policy-v1",
    }


def _action_cost_quote(
    action: dict[str, object],
) -> tuple[CostCategory, int, bool, str | None] | None:
    """Map one external Action to the shared cost vocabulary."""
    kind = str(action.get("kind") or "")
    payload = _as_dict(action.get("payload", {}), "action payload")
    if kind == "model_call":
        purpose = str(payload.get("purpose") or "reply")
        if "repair" in purpose:
            return "repair", 3, True, None
        if "audit" in purpose or "grounding" in purpose:
            return "audit", 2, True, None
        if "proactive" in purpose or "afterthought" in purpose:
            return "proactive", 2, True, None
        return "chat", 3, False, None
    if kind == "attachment_analysis":
        attachment_kind = str(payload.get("kind") or "image")
        category: CostCategory = "audio" if attachment_kind in {"audio", "voice"} else "vision"
        cache_key = None
        if payload.get("source_fingerprint") and payload.get("user_id"):
            cache_key = f"{payload['user_id']}:{payload['source_fingerprint']}"
        return category, 5, False, cache_key
    if kind == "media_generation":
        return "image", 20, False, None
    if kind == "tool_execution":
        return "tool", 8, False, None
    if kind == "outgoing_message":
        message_kind = str(action.get("message_kind") or "reply")
        if message_kind == "proactive":
            return "proactive", 1, True, None
        return "chat", 1, False, None
    return None


def _empty_state(world_id: str) -> dict[str, object]:
    return {
        "world_id": world_id,
        "clock": {},
        "world_started_at": "",
        "clock_observed_at": None,
        "entities": {},
        "agenda": {},
        "actions": {},
        "experiences": {},
        "facts": {},
        "proposals": {},
        "intents": {},
        "turns": {},
        "recent_messages": [],
        "input_merges": {},
        "last_appraisal": None,
        "user_affect": {},
        "private_impressions": {},
        "private_commitments": {},
        "relationships": {},
        "long_term_affinity": {},
        "repair_cases": {},
        "repair_opportunities": {},
        "character_core_changes": [],
        "needs": {"energy": 70, "attention": 55, "security": 50, "initiative": 20, "boundary": 0},
        "daily_schedule": [],
        "weekly_themes": [],
        "weekly_plans": {},
        "life_outcome_templates": {},
        "location_travel_minutes": {},
        "share_decisions": {},
        "share_days": {},
        "goals": {},
        "outcomes": {},
        "communication": {
            "message_id": None, "attention": "idle", "typing": "idle", "reason": None,
            "due_at": None, "deferred_action_id": None,
        },
        "emotion_modulation": initial_affect(""),
        "last_relationship_appraisal": None,
        "decisions": {},
        "conversation_threads": {},
        "media": {},
        "npc_interactions": {},
        "stickers": {},
        "reactions": {},
        "tool_actions": {},
        "outbound_policy_audit": [],
        "controlled_transgressions": [],
        "cost_ledger": {"events": [], "usage": {}},
        "life_evolution": {
            "influences": {},
            "observations": {},
            "pressure_samples": [],
            "chronic": {"fatigue": 0, "relationship_pressure": 0},
        },
    }


def _reduce_media_action_terminal(state: dict[str, object], action: dict[str, object], status: str) -> None:
    """Reflect a failed/cancelled media Action without inventing a media result."""
    kind = str(action.get("kind") or "")
    if kind == "sticker_delivery" and status != "delivered":
        sticker = _as_dict(state.get("stickers", {}), "stickers").get(str(action.get("action_id") or ""))
        if isinstance(sticker, dict):
            sticker["status"] = "delivery_failed"
            sticker["failure_status"] = status
        return
    if kind not in {"media_generation", "media_delivery"} or status == "delivered":
        return
    request_id = str(_as_dict(action.get("payload", {}), "action payload").get("request_id") or "")
    if not request_id:
        return
    media = _as_dict(state.get("media", {}), "media").get(request_id)
    if not isinstance(media, dict):
        return
    media["status"] = "generation_failed" if kind == "media_generation" else "delivery_failed"
    media["failure_status"] = status


def _reduce_reaction_action_terminal(
    state: dict[str, object], action: dict[str, object], status: str
) -> None:
    if action.get("kind") != "reaction_delivery" or status == "delivered":
        return
    reaction = _as_dict(state.get("reactions", {}), "reactions").get(
        str(action.get("action_id") or "")
    )
    if isinstance(reaction, dict):
        reaction["status"] = "delivery_failed" if status == "failed" else status


def _action_console_rank(status: str) -> int:
    """Keep unresolved delivery work above historical terminal actions."""
    return {"unknown": 0, "sending": 1, "scheduled": 2}.get(status, 3)


def _activity_console_rank(status: str) -> int:
    return {"active": 0, "deferred": 1, "planned": 2}.get(status, 3)


def _console_goal(goal: dict[str, object]) -> dict[str, object]:
    return {
        "id": str(goal.get("id") or ""),
        "title": str(goal.get("title") or goal.get("id") or "未命名目标"),
        "status": str(goal.get("status") or "unknown"),
        "progress": int(goal.get("progress") or 0),
        "target": int(goal.get("target") or 0),
        "deadline": str(goal.get("deadline") or ""),
        "next_review_at": str(goal.get("next_review_at") or ""),
    }


def _console_activity(activity: dict[str, object]) -> dict[str, object]:
    return {
        "activity_id": str(activity.get("activity_id") or ""),
        "title": str(activity.get("title") or "未命名活动"),
        "status": str(activity.get("status") or "unknown"),
        "location": str(activity.get("location") or ""),
        "starts_at": str(activity.get("starts_at") or ""),
        "ends_at": str(activity.get("ends_at") or ""),
        "reason": str(activity.get("reason") or activity.get("substitution_reason") or ""),
        "next_review_at": str(activity.get("next_review_at") or ""),
    }


def _console_action(action: dict[str, object]) -> dict[str, object]:
    """Expose delivery state, never private outgoing text or trace prompts."""
    return {
        "action_id": str(action.get("action_id") or ""),
        "kind": str(action.get("kind") or ""),
        "message_kind": str(action.get("message_kind") or ""),
        "status": str(action.get("status") or "unknown"),
        "expires_at": str(action.get("expires_at") or ""),
        "delivery_id": action.get("delivery_id"),
        "reason": str(action.get("reason") or ""),
    }


def _console_event(event: WorldEvent) -> dict[str, object]:
    payload = event.payload
    subject = (
        payload.get("title")
        or payload.get("content")
        or payload.get("activity_id")
        or payload.get("goal_id")
        or payload.get("action_id")
        or payload.get("fact_id")
        or ""
    )
    return {
        "revision": event.revision,
        "event_type": event.event_type,
        "logical_at": event.logical_at,
        "subject": str(subject),
    }


def _dashboard_activity(activity: dict[str, object]) -> dict[str, object]:
    return {
        "activity": str(activity.get("title") or "未命名活动"),
        "starts_at": str(activity.get("starts_at") or ""),
        "ends_at": str(activity.get("ends_at") or ""),
        "status": str(activity.get("status") or "unknown"),
        "interruptible": str(activity.get("status") or "") != "active",
        "adjustment_note": str(activity.get("reason") or activity.get("substitution_reason") or ""),
    }


def _world_scene_projection(state: dict[str, object], activity: dict[str, object] | None) -> dict[str, object]:
    title = str(activity.get("title") if activity else "")
    location = str(activity.get("location") if activity else "")
    lowered = f"{title} {location}"
    if any(token in lowered for token in ("吃", "饭", "食堂", "饮料")):
        anchor, action = "kitchen", "eat"
    elif any(token in lowered for token in ("散步", "出门", "校园", "嘉兴", "上海")):
        anchor, action = "entry", "walk_out"
    elif any(token in lowered for token in ("摄影", "照片", "窗")):
        anchor, action = "window", "gaze"
    elif any(token in lowered for token in ("休息", "睡", "宿舍")):
        anchor, action = "bed", "sleep" if "睡" in lowered else "relax"
    elif title:
        anchor, action = "desk", "study"
    else:
        anchor, action = "rug", "idle"
    communication = _as_dict(state["communication"], "communication")
    attention = str(communication.get("attention") or "idle")
    typing = str(communication.get("typing") or "idle")
    if typing == "started":
        action = "type_phone"
    elif attention == "unread":
        action = "notice_phone"
    elif attention == "seen":
        action = "read_phone"
    elif attention in {"deferred", "do_not_disturb"}:
        action = "withdraw"
    modulation = _as_dict(state["emotion_modulation"], "emotion modulation")
    return {
        "location": anchor, "action": action,
        "expression": str(modulation.get("expression") or "neutral"),
        "time_of_day": "night" if _parse_at(str(_as_dict(state["clock"], "clock")["logical_at"])).hour < 6 else "day",
        "has_notification": attention == "unread", "has_open_task": bool(communication.get("deferred_action_id")),
        "activity_kind": str(activity.get("template_id") if activity else "idle"),
        "phone_attention": attention,
        "observable_reason": str(communication.get("reason") or activity.get("reason") if activity else "world_idle"),
    }


def _communication_phone_label(attention: str, typing: str) -> str:
    if typing == "started":
        return "正在组织回复"
    return {
        "unread": "收到了提醒", "seen": "正在看消息", "deferred": "稍后再看",
        "do_not_disturb": "先不看手机", "idle": "手机放在一边",
    }.get(attention, "手机状态未知")


def _world_mood_label(modulation: dict[str, object]) -> str:
    return {
        "guarded": "在收着", "softening": "慢慢缓和", "warm": "心情不错",
        "caring": "有点挂心", "patient": "在等一等", "open": "愿意接近", "calm": "平静",
    }.get(str(modulation.get("mode") or "calm"), "平静")


def _fact_is_current(value: object) -> bool:
    """Treat pre-status ledger facts as current while excluding compensated facts."""
    fact = _as_dict(value, "fact")
    return str(fact.get("status") or "current") in {"current", "confirmed"}


def _tool_confirmation_kind(text: str) -> str:
    """Conservative parser for the explicit user decision at the permission seam."""
    normalized = re.sub(r"\s+", "", text.strip().lower())
    if re.search(r"(?:取消|拒绝|不同意|不执行|别执行|不要执行|不授权)", normalized):
        return "rejected"
    if re.search(r"(?:确认|同意|授权|可以执行|执行吧|继续执行)", normalized):
        return "authorized"
    return "ambiguous"


def _reply_claims_real_tool_completion(text: str) -> bool:
    """Detect concrete external-operation claims that require settled reality evidence."""
    objects = r"(?:日程|文件|文档|消息|邮件|转账|订单|账号|设置|命令|付款)"
    operations = r"(?:创建|删除|清空|发送|转账|下单|保存|修改|执行|运行|登录|付款)"
    recipient_completion = (
        r"我(?:已经|已|刚刚?)?(?:替你|帮你|给你)[^。！？!?]{0,12}"
        r"(?:点(?:好|完)|下(?:好|完)(?:单)?|买(?:好|到)|订(?:好|到)|约(?:好|到)|"
        r"联系(?:好|到)|发(?:好|出)|支付(?:好|完)|处理(?:好|完))(?:了)?"
    )


    return bool(
        re.search(
            rf"{objects}[^\u3002！？!?]{{0,24}}(?:已经|已)[^\u3002！？!?]{{0,10}}{operations}",
            text,
        )
        or re.search(
            rf"(?:我)?(?:已经|已|刚刚?)?(?:帮你)?[^\u3002！？!?]{{0,12}}{operations}[^\u3002！？!?]{{0,16}}{objects}[^\u3002！？!?]{{0,6}}(?:好|完|成功|了)",
            text,
        )
        or re.search(recipient_completion, text)
    )


def _as_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorldError(f"{name} must be an object")
    return value


def _as_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise WorldError(f"{name} must be a list")
    return value


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _state_hash(state: dict[str, object]) -> str:
    semantic_state = json.loads(_stable_json(state))
    for raw_action in _as_dict(
        semantic_state.get("actions", {}), "actions"
    ).values():
        _as_dict(raw_action, "action").pop("lease_expires_observed_at", None)
    # Transport observation timestamps drive adapter cadence, but are not
    # virtual-world facts.  The attention decision they influence is recorded
    # as a world event; excluding raw wall time keeps semantic replay stable.
    for raw_message in _as_list(
        semantic_state.get("recent_messages", []), "recent messages"
    ):
        _as_dict(raw_message, "recent message").pop("observed_at", None)
    return _hash(_stable_json(semantic_state))


def _parse_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorldError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise WorldError(f"timestamp requires an explicit timezone: {value}")
    return parsed


def _conversation_thread_event_payload(
    raw: dict[str, object],
    *,
    source_action_id: str | None,
    logical_at: datetime,
) -> dict[str, object]:
    """Upgrade a legacy question trace to the unified commitment payload."""
    kind = str(raw.get("kind") or ("question" if raw.get("question") else ""))
    origin_raw = raw.get("origin")
    origin = (
        _as_dict(origin_raw, "conversation thread origin")
        if isinstance(origin_raw, dict)
        else {
            "kind": "action",
            "reference": source_action_id or "",
        }
    )
    default_cancel_conditions = {
        "question": ["user_answered", "user_declined", "topic_superseded"],
        "comfort": ["user_returned", "care_no_longer_relevant", "boundary_withdrawn"],
        "promise": ["promise_fulfilled", "promise_cancelled", "topic_superseded"],
        "contradiction": ["clarified", "user_returned", "topic_superseded"],
        "life_share": ["experience_shared", "experience_invalidated", "user_returned"],
        "reply_reconsider": ["delivery_reconciled", "newer_reply_delivered", "user_returned"],
        "pulse": ["user_returned", "newer_outbound", "boundary_withdrawn"],
    }
    cancel_conditions = raw.get("cancel_conditions")
    if cancel_conditions is None:
        cancel_conditions = default_cancel_conditions.get(kind, [])
    due_at = _parse_at(str(raw.get("due_at") or logical_at.isoformat()))
    expires_at = _parse_at(str(raw.get("expires_at") or ""))
    try:
        thread = create_conversation_thread(
            thread_id=str(raw.get("thread_id") or ""),
            kind=kind,
            user_id=str(raw.get("user_id") or ""),
            origin=origin,
            reason=str(
                raw.get("reason")
                or ("awaiting_user_response" if kind == "question" else "conversation_followup")
            ),
            due_at=due_at,
            expires_at=expires_at,
            cancel_conditions=tuple(
                str(item)
                for item in _as_list(cancel_conditions, "thread cancel conditions")
            ),
            owner=str(raw.get("owner") or "world:conversation"),
        )
    except ConversationCommitmentError as exc:
        raise WorldError(str(exc)) from exc
    payload = thread.as_payload()
    question = str(raw.get("question") or "").strip()
    if question:
        payload["question"] = question
    if source_action_id:
        payload["source_action_id"] = source_action_id
    return payload


def _bounded_paraphrase(assertion: str, evidence: str) -> bool:
    """Allow close speaker/tense rewrites while rejecting unrelated sourced claims."""
    cleanup = re.compile(r"[\s，。！？!?、；;：:\"'“”‘’（）()…]+")
    normalized_assertion = cleanup.sub("", assertion)
    normalized_evidence = cleanup.sub("", evidence)
    if not normalized_assertion or not normalized_evidence:
        return False
    if any(pronoun in normalized_assertion for pronoun in ("他", "她")) and not any(
        pronoun in normalized_evidence for pronoun in ("他", "她")
    ):
        return False
    negation = re.compile(r"(?:没有|没怎么|没|未曾|未|不曾|不是|不太|不)")
    polarity_concepts = (
        "赶", "睡", "去", "来", "做", "完成", "见", "聊", "看", "吃", "喝",
        "喜欢", "同意", "记得", "找到", "恢复", "发送", "收到", "参加",
    )
    for concept in polarity_concepts:
        if concept not in normalized_assertion or concept not in normalized_evidence:
            continue

        def is_negated(text: str) -> bool:
            index = text.find(concept)
            return bool(negation.search(text[max(0, index - 4):index]))

        if is_negated(normalized_assertion) != is_negated(normalized_evidence):
            return False
    contradictory_pairs = (
        ("很好", "不好"), ("顺利", "不顺利"), ("成功", "失败"),
        ("完成", "没完成"), ("记得", "忘了"), ("有", "没有"),
        ("去了", "没去"), ("喜欢", "讨厌"), ("同意", "拒绝"),
    )
    for positive, negative in contradictory_pairs:
        if (positive in normalized_assertion and negative in normalized_evidence) or (
            negative in normalized_assertion and positive in normalized_evidence
        ):
            return False
    assertion_numbers = set(re.findall(r"\d+(?:\.\d+)?", normalized_assertion))
    evidence_numbers = set(re.findall(r"\d+(?:\.\d+)?", normalized_evidence))
    if assertion_numbers - evidence_numbers:
        return False
    time_groups = (
        ("今天", "昨日", "昨天", "明天"),
        ("今晚", "昨晚", "明晚"),
        ("上午", "下午", "晚上", "夜里"),
    )
    for group in time_groups:
        assertion_times = {token for token in group if token in normalized_assertion}
        evidence_times = {token for token in group if token in normalized_evidence}
        if assertion_times and evidence_times and assertion_times.isdisjoint(evidence_times):
            return False
    degree_anchors = ("一夜", "整晚", "完全", "一点都", "特别", "非常")
    if any(
        anchor in normalized_assertion and anchor not in normalized_evidence
        for anchor in degree_anchors
    ):
        return False
    additive_anchors = (
        "宿舍", "图书馆", "教室", "书店", "床上", "家里", "窗边", "路上",
        "因为", "所以", "不然", "免得", "导致", "为了",
        "顺便", "然后", "同时", "接着", "还要",
        "心里", "脑子里", "觉得", "想着", "担心", "害怕", "高兴", "难过", "会忘",
        "出神", "最想记住", "安静选片", "感觉",
        "上课", "下课", "课上完", "回宿舍", "到宿舍", "出门", "回来",
    )
    if any(
        anchor in normalized_assertion and anchor not in normalized_evidence
        for anchor in additive_anchors
    ):
        return False
    return SequenceMatcher(
        None,
        normalized_assertion,
        normalized_evidence,
        autojunk=False,
    ).ratio() >= 0.35


def _conversation_relevance(query: str, content: str) -> int:
    cleanup = re.compile(r"[\s，。！？!?、；;：:\"'“”‘’（）()…\d]+")
    query_text = cleanup.sub("", query)
    content_text = cleanup.sub("", content)
    ignored = set("你我他她的是了在有还记得为什么吗呢啊这那条第")
    overlap = {
        char for char in set(query_text) & set(content_text)
        if char not in ignored
    }
    score = len(overlap)
    topics = (
        ("睡", "失眠", "熬夜", "没睡", "困"),
        ("项目", "工作", "赶工", "方案", "代码"),
        ("胃", "咖啡", "冰美式", "喝"),
        ("数据", "丢", "找回", "文件"),
        ("难过", "伤心", "焦虑", "害怕", "撑不住"),
    )
    for topic in topics:
        if any(marker in query_text for marker in topic) and any(
            marker in content_text for marker in topic
        ):
            score += 20
    return score


def parse_reply_candidate(raw: str) -> dict[str, object]:
    """Parse the only model output shape accepted by world-mode delivery."""
    try:
        candidate = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorldError("world reply must be JSON") from exc
    if not isinstance(candidate, dict):
        raise WorldError("world reply must be a JSON object")
    return {
        "reply_text": str(candidate.get("reply_text") or "").strip(),
        "mentioned_event_ids": candidate.get("mentioned_event_ids", []),
        "proposed_action_ids": candidate.get("proposed_action_ids", []),
        "claims": candidate.get("claims", []),
    }
