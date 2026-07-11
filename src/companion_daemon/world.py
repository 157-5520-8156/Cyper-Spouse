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
from companion_daemon.life_simulation import LifeSimulation
from companion_daemon.time import utc_now
from companion_daemon.world_interaction_rules import WorldInteractionRules
from companion_daemon.world_relationship import evaluate_relationship_stage, stage_event_payload


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


class WorldKernel:
    """The sole write seam for virtual-world facts, plans, and settled actions."""

    SNAPSHOT_INTERVAL = 25

    def __init__(self, store: CompanionStore):
        self.store = store
        self.life_simulation = LifeSimulation()
        self.interaction_rules = WorldInteractionRules()

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
        kind: str,
        expires_at: datetime,
        trace: dict[str, object],
    ) -> tuple[int, int, str]:
        """Atomically create the outbox row, turn trace, and world action."""
        world_id = str(trace.get("world_id") or "")
        if not world_id:
            raise WorldError("world delivery trace requires world_id")
        with self.store.connect() as conn:
            revision, state = self._load_state(conn, world_id)
            now = utc_now().isoformat()
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
            action_id = f"outgoing:{delivery_id}"
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    (
                        "ActionScheduled",
                        {
                            "action_id": action_id,
                            "kind": "outgoing_message",
                            "message_kind": kind,
                            "expires_at": expires_at.isoformat(),
                            "canonical_user_id": canonical_user_id,
                            "platform": platform,
                            "text": text,
                            "trace": trace,
                            "delivery_id": delivery_id,
                            "trace_id": int(trace_row.lastrowid),
                        },
                    )
                ],
                idempotency_key=f"outgoing:{delivery_id}",
                correlation_id=str(uuid4()),
                source="outbox",
                actor={"kind": "companion"},
                causation_id=None,
            )
        return delivery_id, int(trace_row.lastrowid), action_id

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
            if needs["initiative"] < 20 or needs["security"] < 45 or day in _as_dict(state.get("share_days", {}), "share days"):
                return None
            candidate = self._select_shareable_experience(state)
            if not candidate:
                return None
            experience_id, experience, share_score = candidate
            if experience_id in uncertain_experiences:
                return None
            text = f"{str(experience['content']).rstrip('。！？!? ')}。刚想起这件小事，想跟你说一下。"
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
                ("LifeShareSelected", {"experience_id": experience_id, "selection_id": trace["selection_id"], "score": share_score, "reason": "freshness_and_initiative"}),
                ("ActionScheduled", {"action_id": action_id, "kind": "outgoing_message", "message_kind": "life_event", "expires_at": expires_at.isoformat(), "canonical_user_id": canonical_user_id, "platform": platform, "text": text, "trace": trace, "delivery_id": delivery_id, "trace_id": int(trace_row.lastrowid)}),
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

    def begin_outgoing_action(self, delivery_id: int, *, expected_revision: int) -> bool:
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
            self._append_and_project(conn, world_id, revision, state, [("ActionAttempted", {"action_id": action_id}), ("ActionDispatchClaimed", {"action_id": action_id})], idempotency_key=f"begin:{delivery_id}", correlation_id=str(uuid4()), source="delivery", actor={"kind": "transport"}, causation_id=None)
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
            now = utc_now().isoformat()
            if delivered:
                conn.execute(
                    "update outbox_messages set status = 'delivered', delivered_at = ? where id = ?",
                    (now, delivery_id),
                )
                conn.execute(
                    """
                    insert into messages (
                      canonical_user_id, platform, platform_user_id, channel_id, message_id,
                      direction, text, attachments_json, sent_at
                    ) values (?, ?, '', null, null, 'out', ?, '[]', ?)
                    """,
                    (row["canonical_user_id"], row["platform"], row["text"], now),
                )
            else:
                conn.execute(
                    "update outbox_messages set status = 'failed', failed_at = ?, failure_reason = ? where id = ?",
                    (now, (reason or "delivery failed")[:500], delivery_id),
                )
            conn.execute(
                """
                update turn_traces set status = ?, failure_reason = ?, updated_at = ?
                where delivery_id = ? and status in ('planned', 'sending')
                """,
                ("delivered" if delivered else "failed", None if delivered else (reason or "delivery failed")[:500], now, delivery_id),
            )
            action = _as_dict(_as_dict(state["actions"], "actions")[action_id], "action")
            trace = _as_dict(action.get("trace", {}), "action trace")
            specifications: list[tuple[str, dict[str, object]]] = [
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
                        },
                    },
                ),
            ]
            if delivered and trace.get("life_share"):
                specifications.append(("ExperienceShared", {"experience_id": trace.get("experience_id"), "action_id": action_id}))
            thread = trace.get("conversation_thread")
            if delivered and isinstance(thread, dict):
                specifications.append((
                    "ConversationThreadOpened",
                    {
                        "thread_id": str(thread["thread_id"]),
                        "user_id": str(thread["user_id"]),
                        "question": str(thread["question"]),
                        "expires_at": str(thread["expires_at"]),
                        "source_action_id": action_id,
                    },
                ))
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                specifications,
                idempotency_key=f"settle:{delivery_id}:{'delivered' if delivered else 'failed'}",
                correlation_id=str(uuid4()),
                source="delivery",
                actor={"kind": "transport"},
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
                records.append({"experience_id": experience_id, "content": item["content"], "occurred_at": occurred_at, "shared": bool(item.get("shared"))})
        records.sort(key=lambda item: str(item["occurred_at"]))
        return records

    def conversation_policy(self, world_id: str) -> dict[str, object]:
        """Expose behavior-only world state; never fabricate a conversational fact."""
        state = self.snapshot(world_id)
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
        state = self.snapshot(world_id)
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
                "speaker": "user" if direction == "in" else "companion",
                "content": text,
                "logical_at": str(item.get("logical_at") or item.get("sent_at") or ""),
            }
            recent_conversation.append(transcript_item)
            if direction == "in":
                referencable_conversation.append(
                    {
                        **transcript_item,
                        "source_type": "user_message",
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
        return {
            "referencable_facts": [
                {
                    "source_id": str(fact_id),
                    "source_type": "fact",
                    "fact_id": str(fact_id),
                    "value": str(_as_dict(item, "fact").get("value") or ""),
                    "reference_state": "confirmed",
                }
                for fact_id, item in _as_dict(state["facts"], "facts").items()
                if str(_as_dict(item, "fact").get("subject") or "")
                in {user_id, "world", "zhizhi"}
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
            "user_profile": [
                {
                    "source_id": str(_as_dict(item, "fact").get("fact_id") or ""),
                    "value": str(_as_dict(item, "fact").get("value") or ""),
                    "reference_state": "confirmed",
                }
                for item in user_facts[-8:]
            ],
            "current_scene": current_scene,
            "current_scene_source": current_scene_source,
            "behavior": {
                "policy": self.conversation_policy(world_id),
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
        state = self.snapshot(world_id)
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
                fact.get("scope") != "conversation"
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
            "source_type": "current_scene",
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
        state = self.snapshot(world_id)
        experiences = _as_dict(state["experiences"], "experiences")
        facts = _as_dict(state["facts"], "facts")
        visible_facts = {
            fact_id: raw
            for fact_id, raw in facts.items()
            if user_id is None
            or str(_as_dict(raw, "fact").get("subject") or "")
            in {user_id, "world", "zhizhi"}
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
            )
        _, current_scene_source = self._current_scene_source(state)
        known = set(experiences) | set(visible_facts) | set(conversation_sources) | {current_scene_source["source_id"]}
        mentioned = [
            str(item)
            for item in _as_list(candidate.get("mentioned_event_ids", []), "mentioned_event_ids")
        ]
        claims = _as_list(candidate.get("claims", []), "claims")
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
        sources = {
            **{record_id: str(_as_dict(item, "experience")["content"]) for record_id, item in experiences.items()},
            **{record_id: str(_as_dict(item, "fact")["value"]) for record_id, item in visible_facts.items()},
            **conversation_sources,
            current_scene_source["source_id"]: current_scene_source["content"],
        }
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
                r"(?:本地|云端|服务器|硬盘|数据库)[^。！!?！？]{0,12}(?:没了|丢了|坏了|删除了)",
                r"我[^。！!?！？]{0,18}(?:睡不着|失眠|难受)(?:的时候|时)[^。！!?！？]{0,18}(?:会|就)",
                r"(?:我跟着[^。！!?！？]{0,18}|松了一口气|确实在意了|我反而觉得[^。！!?！？]{0,12}踏实)",
                r"有一点[^。！!?！？]{0,16}不舒服",
            )
        )
        if reply_text != "我在。" and unsupported_world_claim:
            raise WorldError("reply contains world-time or experience text outside committed claims")
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
                if user_id is None
                or str(_as_dict(item, "fact").get("subject") or "")
                in {user_id, "world", "zhizhi"}
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
                        "long_term_goals": _as_list(seed.get("long_term_goals", []), "long-term goals"),
                        "life_outcome_templates": _as_dict(seed.get("life_outcome_templates", {}), "life outcome templates"),
                        "location_travel_minutes": _as_dict(seed.get("location_travel_minutes", {}), "location travel minutes"),
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

            def emit(event_type: str, payload: dict[str, object]) -> None:
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
                        logical_at=target,
                        observed_at=target,
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

            timeline: list[tuple[datetime, datetime, dict[str, object], bool]] = []
            for raw in _as_dict(state["agenda"], "agenda").values():
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

            known_ids = set(_as_dict(state["agenda"], "agenda"))
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
                    if activity_id not in known_ids and starts <= target_at and current_at <= ends:
                        timeline.append((starts, ends, {**item, "activity_id": activity_id}, False))
                local_day += timedelta(days=1)

            occupied = [
                dict(_as_dict(item, "activity"))
                for item in _as_dict(state["agenda"], "agenda").values()
                if item.get("status") in {"completed", "active"} and item.get("ends_at")
                and _parse_at(str(item["ends_at"])) <= current_at
            ]
            previous = max(occupied, key=lambda item: str(item["ends_at"]), default=None)

            for starts, ends, raw, existing in sorted(timeline, key=lambda entry: (entry[0], entry[1], str(entry[2].get("activity_id")))):
                activity_id = str(raw["activity_id"])
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

                    activity, substitution_reason = self.life_simulation.choose_template(
                        working,
                        activity,
                        [str(value) for value in _as_list(schedule_item.get("fallback_templates", []), "fallback templates")],
                    )
                    if substitution_reason:
                        activity["substitution_reason"] = substitution_reason
                    emit("ActivityPlanned", activity)
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
                    for outcome_type, outcome_payload in self.life_simulation.advance(working, [completed]):
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
            for thread_id, thread in _as_dict(working.get("conversation_threads", {}), "conversation threads").items():
                item = _as_dict(thread, "conversation thread")
                if (
                    item.get("status") == "open"
                    and item.get("expires_at")
                    and _parse_at(str(item["expires_at"])) <= target_at
                ):
                    emit("ConversationThreadExpired", {"thread_id": thread_id, "reason": "logical_timeout"})
            for goal_id, goal in list(_as_dict(working.get("goals", {}), "goals").items()):
                if goal.get("status") == "active" and goal.get("deadline") and _parse_at(str(goal["deadline"])) <= target_at:
                    emit("GoalDeferred", {"goal_id": goal_id, "reason": "deadline_reached", "next_review_at": (target_at + timedelta(days=1)).isoformat()})
                elif goal.get("status") == "deferred" and goal.get("next_review_at") and _parse_at(str(goal["next_review_at"])) <= target_at:
                    emit("GoalReviewDue", {"goal_id": goal_id})
            return events
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
        if command_type == "resolve_conversation_thread":
            thread_id = str(command.get("thread_id") or "")
            outcome = str(command.get("outcome") or "")
            reason = str(command.get("reason") or "").strip()
            thread = _as_dict(_as_dict(state.get("conversation_threads", {}), "conversation threads").get(thread_id), "conversation thread")
            if thread.get("status") != "open" or outcome not in {"answered", "skipped", "meta"} or not reason:
                raise WorldError("only an open conversation thread can be resolved with a classified user response")
            return [("ConversationThreadResolved", {"thread_id": thread_id, "outcome": outcome, "reason": reason[:160]})]
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
                stage, reason = evaluate_relationship_stage(relation, boundary=boundary)
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
                        ),
                    )
                )
            return events
        if command_type == "change_need":
            need = str(command.get("need") or "")
            if need not in {"energy", "attention", "security", "initiative", "boundary"}:
                raise WorldError("unsupported world need")
            return [("NeedChanged", {"need": need, "delta": int(command.get("delta") or 0)})]
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
                or media_kind not in {"creative_image", "selfie"}
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
            if action["status"] not in {"scheduled", "sending"}:
                raise WorldError("only a scheduled or claimed external action can settle")
            result = _as_dict(command.get("result"), "result")
            status = str(result.get("status") or "")
            if status not in {"delivered", "failed", "cancelled"}:
                raise WorldError("external result requires a terminal status")
            events: list[tuple[str, dict[str, object]]] = []
            if action["status"] == "scheduled":
                events.append(("ActionAttempted", {"action_id": action_id}))
            events.append(("ActionSettled", {"action_id": action_id, "result": result}))
            payload = _as_dict(action.get("payload", {}), "action payload")
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
            return events
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
                },
            )]
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
            return [("TurnProcessingSettled", {"message_id": message_id, "status": status, "reason": reason})]
        if command_type == "appraise_turn":
            appraisal = str(command.get("appraisal") or "ordinary_message")
            consequence = self.interaction_rules.consequence(appraisal)
            events: list[tuple[str, dict[str, object]]] = [
                ("TurnAppraised", {"appraisal": appraisal, "policy": consequence.policy, "rule_version": self.interaction_rules.RULE_VERSION}),
                (
                    "IntentCreated",
                    {
                        "intent_id": str(command["intent_id"]), "kind": "reply", "status": "open",
                        "message_id": str(command.get("message_id") or ""),
                    },
                ),
            ]
            events.extend(
                ("NeedChanged", {"need": need, "delta": delta})
                for need, delta in consequence.need_deltas.items()
            )
            user_id = str(command.get("user_id") or "")
            if user_id:
                user = _as_dict(_as_dict(state["entities"], "entities").get(user_id), "appraised user")
                if user.get("kind") != "user":
                    raise WorldError("turn appraisal user must be a registered user")
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
                stage, reason = evaluate_relationship_stage(relation, boundary=boundary)
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
                        ),
                    )
                )
            events.append((
                "EmotionModulated",
                {"mode": consequence.emotion_mode, "expression": consequence.emotion_expression, "charge_delta": consequence.emotion_charge_delta, "reason": appraisal, "rule_version": self.interaction_rules.RULE_VERSION},
            ))
            return events
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
        exists = conn.execute("select 1 from worlds where world_id = ?", (world_id,)).fetchone()
        if not exists:
            raise WorldError(f"unknown world: {world_id}")
        events = self._load_events(conn, world_id)
        if not events:
            raise WorldError(f"world has no event stream: {world_id}")
        return events[-1].revision, reduce_events(events)

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
        next_state["clock_observed_at"] = event.observed_at
        next_state["entities"] = {str(protagonist["id"]): {**protagonist, "status": "active"}}
        next_state["daily_schedule"] = payload.get("daily_schedule", [])
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
        modulation = _as_dict(next_state["emotion_modulation"], "emotion modulation")
        decay_anchor = _parse_at(
            str(modulation.get("last_decay_at") or _as_dict(next_state["clock"], "clock")["logical_at"])
        )
        target_at = _parse_at(str(payload["target_logical_at"]))
        elapsed_hours = max(0, int((target_at - decay_anchor).total_seconds() // 3600))
        if elapsed_hours:
            modulation["charge"] = max(0, int(modulation.get("charge", 0)) - elapsed_hours * 2)
            modulation["last_decay_at"] = (decay_anchor + timedelta(hours=elapsed_hours)).isoformat()
            if modulation["charge"] == 0:
                modulation.update({"mode": "calm", "expression": "neutral", "reason": "logical_time_decay"})
        _as_dict(next_state["clock"], "clock")["logical_at"] = payload["target_logical_at"]
        next_state["clock_observed_at"] = str(payload.get("observed_at") or event.observed_at)
    elif event.event_type == "ActivityPlanned":
        item = {**payload, "status": "planned"}
        _as_dict(next_state["agenda"], "agenda")[str(item["activity_id"])] = item
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
    elif event.event_type == "ActionScheduled":
        item = {**payload, "status": "scheduled"}
        _as_dict(next_state["actions"], "actions")[str(item["action_id"])] = item
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
        if action.get("kind") == "outgoing_message" and action["status"] == "delivered":
            history = _as_list(next_state["recent_messages"], "recent_messages")
            trace = _as_dict(action.get("trace", {}), "outgoing trace")
            history.append({
                "direction": "out", "message_id": str(payload["action_id"]),
                "text": str(action.get("text") or ""), "sent_at": event.logical_at,
                "logical_at": event.logical_at,
                "user_id": str(trace.get("user_id") or ""),
                "source_action_id": str(payload["action_id"]), "outgoing_direction": str(trace.get("direction") or ""),
            })
            next_state["recent_messages"] = history[-64:]
    elif event.event_type == "ActionExpired":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "expired"
        action["reason"] = payload.get("reason")
        _reduce_media_action_terminal(next_state, action, "expired")
    elif event.event_type == "ActionCancelled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "cancelled"
        action["reason"] = payload.get("reason")
        _reduce_media_action_terminal(next_state, action, "cancelled")
    elif event.event_type == "ActionDeliveryUncertain":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "unknown"
        action["reason"] = payload.get("reason")
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
    elif event.event_type == "ConversationThreadExpired":
        thread = _as_dict(_as_dict(next_state["conversation_threads"], "conversation threads")[str(payload["thread_id"])], "conversation thread")
        if thread.get("status") == "open":
            thread["status"] = "expired"
            thread["resolution_reason"] = str(payload["reason"])
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
    elif event.event_type == "ModelProposalRecorded":
        item = {**payload, "status": "recorded"}
        _as_dict(next_state["proposals"], "proposals")[str(item["proposal_id"])] = item
    elif event.event_type == "ModelProposalAccepted":
        _as_dict(next_state["proposals"], "proposals")[str(payload["proposal_id"])]["status"] = "accepted"
    elif event.event_type == "ExperienceCommitted":
        item = dict(payload)
        _as_dict(next_state["experiences"], "experiences")[str(item["experience_id"])] = item
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
        _as_dict(next_state["facts"], "facts")[str(item["fact_id"])] = item
    elif event.event_type == "UserMessageObserved":
        history = _as_list(next_state["recent_messages"], "recent_messages")
        history.append({"direction": "in", "logical_at": event.logical_at, **payload})
        next_state["recent_messages"] = history[-64:]
        next_state["communication"] = {
            "message_id": payload.get("message_id"), "attention": "unread", "typing": "idle",
            "reason": "message_observed", "due_at": None, "deferred_action_id": None,
        }
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
    elif event.event_type == "IntentCreated":
        _as_dict(next_state["intents"], "intents")[str(payload["intent_id"])] = dict(payload)
    elif event.event_type == "IntentFailed":
        intent = _as_dict(next_state["intents"], "intents")[str(payload["intent_id"])]
        intent["status"] = "failed"
        intent["reason"] = payload["reason"]
    return next_state


def _empty_state(world_id: str) -> dict[str, object]:
    return {
        "world_id": world_id,
        "clock": {},
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
        "last_appraisal": None,
        "relationships": {},
        "needs": {"energy": 70, "attention": 55, "security": 50, "initiative": 20, "boundary": 0},
        "daily_schedule": [],
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
        "emotion_modulation": {"mode": "calm", "expression": "neutral", "reason": "world_started", "charge": 0, "last_decay_at": None},
        "last_relationship_appraisal": None,
        "decisions": {},
        "conversation_threads": {},
        "media": {},
        "npc_interactions": {},
        "stickers": {},
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
    return _hash(_stable_json(state))


def _parse_at(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise WorldError(f"invalid ISO timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise WorldError(f"timestamp requires an explicit timezone: {value}")
    return parsed


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
