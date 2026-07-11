"""Append-only, deterministic world ledger for the companion's virtual life."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
        open_actions = tuple(sorted(action_id for action_id, action in actions.items() if _as_dict(action, "action").get("status") in {"scheduled", "sending"}))
        unknown_actions = tuple(sorted(action_id for action_id, action in actions.items() if _as_dict(action, "action").get("status") == "unknown"))
        return WorldEnablementReport(
            world_id=world_id,
            ready=all(report.matches_live for report in reports) and not open_actions and (not unknown_actions or delivery_receipts_supported),
            projection_reports=reports,
            open_action_ids=open_actions,
            unknown_action_ids=unknown_actions,
            delivery_receipts_supported=delivery_receipts_supported,
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
                "relationship_stage": "world_projected",
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
                {"fact_id": str(fact_id), "value": str(_as_dict(item, "fact").get("value") or "")}
                for fact_id, item in _as_dict(state["facts"], "facts").items()
            ],
            "referencable_experiences": self._committed_experiences(state),
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
                "location": str((active or protagonist).get("location") or ""),
                "active_activity": str((active or {}).get("title") or ""),
                "boundaries": [str(item) for item in _as_list(protagonist.get("boundaries", []), "boundaries")],
            },
        }

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
        self, world_id: str, candidate: dict[str, object]
    ) -> dict[str, object]:
        """Reject model output that cites a planned, absent, or failed world fact."""
        reply_text = str(candidate.get("reply_text") or "").strip()
        if not reply_text:
            raise WorldError("reply candidate requires reply_text")
        state = self.snapshot(world_id)
        experiences = _as_dict(state["experiences"], "experiences")
        facts = _as_dict(state["facts"], "facts")
        known = set(experiences) | set(facts)
        mentioned = _as_list(candidate.get("mentioned_event_ids", []), "mentioned_event_ids")
        claims = _as_list(candidate.get("claims", []), "claims")
        proposed_actions = _as_list(candidate.get("proposed_action_ids", []), "proposed_action_ids")
        unknown = [str(item) for item in mentioned if str(item) not in known]
        if unknown:
            raise WorldError(f"reply cites uncommitted world records: {', '.join(unknown)}")
        sources = {
            **{record_id: str(_as_dict(item, "experience")["content"]) for record_id, item in experiences.items()},
            **{record_id: str(_as_dict(item, "fact")["value"]) for record_id, item in facts.items()},
        }
        normalized_claims: list[dict[str, str]] = []
        for raw_claim in claims:
            claim = _as_dict(raw_claim, "reply claim")
            source_id = str(claim.get("source_id") or "")
            text = str(claim.get("text") or "").strip()
            if source_id not in sources or source_id not in mentioned or not text:
                raise WorldError("each reply claim needs a mentioned committed source id and text")
            if text not in reply_text or text not in sources[source_id]:
                raise WorldError("reply claim text must be quoted from its committed source")
            normalized_claims.append({"source_id": source_id, "text": text})
        # A reply without claims may still converse, but it cannot state a
        # completed off-screen experience.  Claim text is intentionally quoted
        # from its source, making provenance deterministic rather than a model
        # assertion that merely names an arbitrary id.
        event_claim = re.search(
            r"(?:我|她).{0,24}(?:去了|吃了|见了|聊了|做了|完成了|回来|逛了|看了|参加了|上了)",
            reply_text,
        )
        if event_claim and not normalized_claims:
            raise WorldError("reply states an experience without a committed source id")
        remainder = reply_text
        for claim in normalized_claims:
            remainder = remainder.replace(claim["text"], "")
        # Questions about the user or a future choice are not claims that an
        # off-screen world event occurred.  Treating every “今天/明天/在” as a
        # historical assertion used to silently replace ordinary questions
        # with the fallback reply, which in turn hid the conversation state.
        is_question = "?" in reply_text or "？" in reply_text
        if reply_text != "我在。" and not is_question and re.search(r"(?:了|过|刚|已经|昨天|昨晚|早上|上午|下午|今晚|今天|明天|收到|买|等|在|去|来)", remainder):
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
            events: list[tuple[str, dict[str, object]]] = [("ClockAdvanced", {"target_logical_at": target})]
            agenda = _as_dict(state["agenda"], "agenda")
            target_at = _parse_at(target)
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
                    if activity_id not in agenda and starts <= target_at:
                        payload = {
                            "activity_id": activity_id,
                            "entity_id": "zhizhi",
                            "title": str(item["title"]),
                            "template_id": str(item.get("template_id") or ""),
                            "location": str(item.get("location") or ""),
                            "starts_at": starts.isoformat(),
                            "ends_at": ends.isoformat(),
                        }
                        payload, substitution_reason = self.life_simulation.choose_template(
                            state, payload, [str(value) for value in _as_list(item.get("fallback_templates", []), "fallback templates")]
                        )
                        if substitution_reason:
                            payload["substitution_reason"] = substitution_reason
                        events.append(("ActivityPlanned", payload))
                        events.append(("ActivitySelected", {"activity_id": activity_id, "template_id": payload["template_id"], "reason": substitution_reason or "primary_template", "rule_version": self.life_simulation.RULE_VERSION}))
                        previous = next((candidate for kind, candidate in reversed(events[:-2]) if kind == "ActivityPlanned" and str(candidate.get("ends_at", ""))[:10] == starts.date().isoformat()), None)
                        if previous and self._travel_minutes(state, str(previous.get("location") or ""), str(payload.get("location") or "")) > int((starts - _parse_at(str(previous["ends_at"]))).total_seconds() // 60):
                            events.append(("ActivityDeferred", {"activity_id": activity_id, "reason": "travel_time_conflict", "next_review_at": (target_at + timedelta(hours=int(item.get("review_after_hours", 2)))).isoformat()}))
                            continue
                        if substitution_reason == "no_eligible_template" and bool(item.get("rest_when_unavailable")):
                            events.append(("ActivityRested", {"activity_id": activity_id, "reason": "no_eligible_seeded_activity", "energy_delta": int(item.get("rest_recovery", 8))}))
                            continue
                        if substitution_reason == "no_eligible_template" and bool(item.get("defer_when_unavailable")):
                            events.append(("ActivityDeferred", {"activity_id": activity_id, "reason": "no_eligible_seeded_activity", "next_review_at": (target_at + timedelta(hours=int(item.get("review_after_hours", 4)))).isoformat()}))
                            continue
                        events.append(("ActivityStarted", {"activity_id": activity_id}))
                        if ends <= target_at:
                            events.append(("ActivityCompleted", {"activity_id": activity_id}))
                local_day += timedelta(days=1)
            for activity_id, activity in _as_dict(state["agenda"], "agenda").items():
                item = _as_dict(activity, "activity")
                if item["status"] == "planned" and _parse_at(str(item["starts_at"])) <= _parse_at(target):
                    events.append(("ActivityStarted", {"activity_id": activity_id}))
                if item["status"] in {"planned", "active"} and _parse_at(str(item["ends_at"])) <= _parse_at(target):
                    events.append(("ActivityCompleted", {"activity_id": activity_id}))
            for action_id, action in _as_dict(state["actions"], "actions").items():
                item = _as_dict(action, "action")
                if (
                    item["status"] == "scheduled"
                    and item.get("expires_at")
                    and _parse_at(str(item["expires_at"])) <= _parse_at(target)
                ):
                    events.append(("ActionExpired", {"action_id": action_id, "reason": "logical_timeout"}))
            for thread_id, thread in _as_dict(state.get("conversation_threads", {}), "conversation threads").items():
                item = _as_dict(thread, "conversation thread")
                if (
                    item.get("status") == "open"
                    and item.get("expires_at")
                    and _parse_at(str(item["expires_at"])) <= target_at
                ):
                    events.append(("ConversationThreadExpired", {"thread_id": thread_id, "reason": "logical_timeout"}))
            for goal_id, goal in _as_dict(state.get("goals", {}), "goals").items():
                if goal.get("status") == "active" and goal.get("deadline") and _parse_at(str(goal["deadline"])) <= target_at:
                    events.append(("GoalDeferred", {"goal_id": goal_id, "reason": "deadline_reached", "next_review_at": (target_at + timedelta(days=1)).isoformat()}))
                elif goal.get("status") == "deferred" and goal.get("next_review_at") and _parse_at(str(goal["next_review_at"])) <= target_at:
                    events.append(("GoalReviewDue", {"goal_id": goal_id}))
            completed_activities: list[dict[str, object]] = []
            for event_type, payload in list(events):
                if event_type == "ActivityCompleted":
                    activity_id = str(payload["activity_id"])
                    activity = _as_dict(state["agenda"], "agenda").get(activity_id)
                    if activity is None:
                        activity = next((item for kind, item in events if kind == "ActivityPlanned" and item["activity_id"] == activity_id), None)
                    if activity is not None:
                        completed_activities.append(_as_dict(activity, "activity"))
            state_for_outcomes = json.loads(_stable_json(state))
            _as_dict(state_for_outcomes["clock"], "clock")["logical_at"] = target
            events.extend(self.life_simulation.advance(state_for_outcomes, completed_activities))
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
            return [("UserRegistered", {"id": user_id, "name": name, "kind": "user"})]
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
            if prior_action_id:
                prior = _as_dict(_as_dict(state["actions"], "actions").get(prior_action_id), "deferred attention action")
                if prior.get("status") == "scheduled":
                    events.append(("ActionCancelled", {"action_id": prior_action_id, "reason": "attention_reconsidered"}))
            payload: dict[str, object] = {
                "message_id": message_id, "attention": attention, "reason": reason,
                "due_at": None, "deferred_action_id": None,
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
            if entity_id not in _as_dict(state["entities"], "entities") or dimension not in {"trust", "closeness", "respect"}:
                raise WorldError("relationship change requires a registered entity and supported dimension")
            return [
                (
                    "NpcRelationshipChanged",
                    {"entity_id": entity_id, "dimension": dimension, "delta": int(command.get("delta") or 0)},
                )
            ]
        if command_type == "change_need":
            need = str(command.get("need") or "")
            if need not in {"energy", "attention", "security", "initiative", "boundary"}:
                raise WorldError("unsupported world need")
            return [("NeedChanged", {"need": need, "delta": int(command.get("delta") or 0)})]
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
            if action["status"] != "scheduled":
                raise WorldError("only a scheduled action can settle")
            result = _as_dict(command.get("result"), "result")
            status = str(result.get("status") or "")
            if status not in {"delivered", "failed", "cancelled"}:
                raise WorldError("external result requires a terminal status")
            return [("ActionAttempted", {"action_id": action_id}), ("ActionSettled", {"action_id": action_id, "result": result})]
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
                {"message_id": command.get("message_id"), "text": command.get("text", ""), "sent_at": command.get("sent_at")},
            )]
        if command_type == "appraise_turn":
            appraisal = str(command.get("appraisal") or "ordinary_message")
            policies = {
                "user_vulnerable": "先接住情绪，不急着追问。",
                "boundary_violation": "短而清楚地守住边界。",
                "control_pressure": "不讨好，平静地说明边界。",
                "repair_attempt": "可以缓和，但不立刻翻篇。",
                "availability_drop": "收住主动性，不追发。",
                "return_after_gap": "自然接上，不抱怨。",
            }
            need_deltas = {
                "boundary_violation": {"security": -12, "boundary": 12, "initiative": -8},
                "control_pressure": {"security": -8, "boundary": 8, "initiative": -5},
                "repair_attempt": {"security": 5, "boundary": -3},
                "warmth_received": {"security": 4, "initiative": 3},
                "user_vulnerable": {"initiative": 5, "attention": -4},
                "availability_drop": {"initiative": -6},
                "return_after_gap": {"security": 2, "initiative": 2},
            }
            relationship_deltas = {
                "boundary_violation": {"respect": -12, "reliability": -4},
                "control_pressure": {"respect": -8},
                "repair_attempt": {"respect": 3, "reliability": 2},
                "warmth_received": {"closeness": 4, "reliability": 1},
                "user_vulnerable": {"closeness": 2},
                "availability_drop": {"reliability": -1},
                "return_after_gap": {"closeness": 1, "reliability": 1},
            }
            modulation = {
                "boundary_violation": ("guarded", "guarded", 16),
                "control_pressure": ("guarded", "guarded", 11),
                "repair_attempt": ("softening", "soft", -5),
                "warmth_received": ("warm", "smile", 5),
                "user_vulnerable": ("caring", "worry", 7),
                "availability_drop": ("patient", "neutral", -2),
                "return_after_gap": ("open", "soft", 3),
            }
            events: list[tuple[str, dict[str, object]]] = [
                ("TurnAppraised", {"appraisal": appraisal, "policy": policies.get(appraisal, "自然回应当前消息。")}),
                ("IntentCreated", {"intent_id": str(command["intent_id"]), "kind": "reply", "status": "open"}),
            ]
            events.extend(
                ("NeedChanged", {"need": need, "delta": delta})
                for need, delta in need_deltas.get(appraisal, {}).items()
            )
            user_id = str(command.get("user_id") or "")
            if user_id:
                user = _as_dict(_as_dict(state["entities"], "entities").get(user_id), "appraised user")
                if user.get("kind") != "user":
                    raise WorldError("turn appraisal user must be a registered user")
                events.append(("RelationshipAppraised", {"user_id": user_id, "appraisal": appraisal}))
                events.extend(
                    ("RelationshipChanged", {"entity_id": user_id, "dimension": dimension, "delta": delta})
                    for dimension, delta in relationship_deltas.get(appraisal, {}).items()
                )
            mode, expression, charge_delta = modulation.get(appraisal, ("calm", "neutral", -1))
            events.append((
                "EmotionModulated",
                {"mode": mode, "expression": expression, "charge_delta": charge_delta, "reason": appraisal},
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
        if not origin or origin == destination:
            return 0
        routes = _as_dict(state.get("location_travel_minutes", {}), "location travel minutes")
        return int(routes.get(f"{origin}->{destination}", routes.get(f"{destination}->{origin}", 0)))

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
        next_state["entities"] = {str(protagonist["id"]): {**protagonist, "status": "active"}}
        next_state["daily_schedule"] = payload.get("daily_schedule", [])
        next_state["life_outcome_templates"] = payload.get("life_outcome_templates", {})
        next_state["location_travel_minutes"] = payload.get("location_travel_minutes", {})
        next_state["goals"] = {str(goal["id"]): {**goal, "progress": 0, "status": "active"} for goal in _as_list(payload.get("long_term_goals", []), "long-term goals")}
    elif event.event_type == "NpcRegistered":
        npc = dict(payload)
        npc["status"] = "active"
        _as_dict(next_state["entities"], "entities")[str(npc["id"])] = npc
    elif event.event_type == "UserRegistered":
        user = {**payload, "status": "active"}
        _as_dict(next_state["entities"], "entities")[str(user["id"])] = user
    elif event.event_type == "ClockModeChanged":
        next_state["clock"] = {**_as_dict(next_state["clock"], "clock"), **payload}
    elif event.event_type == "ClockAdvanced":
        _as_dict(next_state["clock"], "clock")["logical_at"] = payload["target_logical_at"]
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
        _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]["status"] = "sending"
    elif event.event_type == "ActionSettled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = str(_as_dict(payload["result"], "result")["status"])
        action["result"] = payload["result"]
    elif event.event_type == "ActionExpired":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "expired"
        action["reason"] = payload.get("reason")
    elif event.event_type == "ActionCancelled":
        action = _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]
        action["status"] = "cancelled"
        action["reason"] = payload.get("reason")
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
    elif event.event_type == "EmotionModulated":
        current = _as_dict(next_state["emotion_modulation"], "emotion modulation")
        next_state["emotion_modulation"] = {
            "mode": payload["mode"], "expression": payload["expression"], "reason": payload["reason"],
            "charge": max(0, min(100, int(current.get("charge", 0)) + int(payload["charge_delta"]))),
        }
    elif event.event_type == "NeedChanged":
        needs = _as_dict(next_state["needs"], "needs")
        need = str(payload["need"])
        needs[need] = max(0, min(100, int(needs.get(need, 50)) + int(payload["delta"])))
    elif event.event_type == "MessageAttentionDecided":
        next_state["communication"] = {
            "message_id": payload["message_id"], "attention": payload["attention"], "typing": "idle",
            "reason": payload["reason"], "due_at": payload["due_at"],
            "deferred_action_id": payload["deferred_action_id"],
        }
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
        history.append({"direction": "in", **payload})
        next_state["recent_messages"] = history[-16:]
        next_state["communication"] = {
            "message_id": payload.get("message_id"), "attention": "unread", "typing": "idle",
            "reason": "message_observed", "due_at": None, "deferred_action_id": None,
        }
    elif event.event_type == "TurnAppraised":
        next_state["last_appraisal"] = dict(payload)
    elif event.event_type == "IntentCreated":
        _as_dict(next_state["intents"], "intents")[str(payload["intent_id"])] = dict(payload)
    return next_state


def _empty_state(world_id: str) -> dict[str, object]:
    return {
        "world_id": world_id,
        "clock": {},
        "entities": {},
        "agenda": {},
        "actions": {},
        "experiences": {},
        "facts": {},
        "proposals": {},
        "intents": {},
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
        "emotion_modulation": {"mode": "calm", "expression": "neutral", "reason": "world_started", "charge": 0},
        "last_relationship_appraisal": None,
        "decisions": {},
        "conversation_threads": {},
    }


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
