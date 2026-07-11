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

    def settle_outgoing_action(
        self, delivery_id: int, *, delivered: bool, reason: str | None = None
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
            if row["status"] != "planned":
                return result
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
                where delivery_id = ? and status = 'planned'
                """,
                ("delivered" if delivered else "failed", None if delivered else (reason or "delivery failed")[:500], now, delivery_id),
            )
            self._append_and_project(
                conn,
                world_id,
                revision,
                state,
                [
                    ("ActionAttempted", {"action_id": action_id}),
                    (
                        "ActionSettled",
                        {
                            "action_id": action_id,
                            "result": {
                                "kind": "delivery",
                                "status": "delivered" if delivered else "failed",
                                "reason": reason,
                            },
                        },
                    ),
                ],
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

    def snapshot(self, world_id: str) -> dict[str, object]:
        with self.store.connect() as conn:
            _, state = self._load_state(conn, world_id)
        return state

    def events(self, world_id: str) -> list[WorldEvent]:
        with self.store.connect() as conn:
            return self._load_events(conn, world_id)

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
        if reply_text != "我在。" and re.search(r"(?:了|过|刚|已经|昨天|昨晚|早上|上午|下午|今晚|今天|明天|收到|买|等|在|去|来)", remainder):
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
                            "starts_at": starts.isoformat(),
                            "ends_at": ends.isoformat(),
                        }
                        events.append(("ActivityPlanned", payload))
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
            for goal_id, goal in _as_dict(state.get("goals", {}), "goals").items():
                if goal.get("status") == "active" and goal.get("deadline") and _parse_at(str(goal["deadline"])) <= target_at:
                    events.append(("GoalDeferred", {"goal_id": goal_id, "reason": "deadline_reached", "next_review_at": (target_at + timedelta(days=1)).isoformat()}))
            completed_activities: list[dict[str, object]] = []
            for event_type, payload in list(events):
                if event_type == "ActivityCompleted":
                    activity_id = str(payload["activity_id"])
                    activity = _as_dict(state["agenda"], "agenda").get(activity_id)
                    if activity is None:
                        activity = next((item for kind, item in events if kind == "ActivityPlanned" and item["activity_id"] == activity_id), None)
                    if activity is not None:
                        completed_activities.append(_as_dict(activity, "activity"))
            events.extend(self.life_simulation.advance(state, completed_activities))
            return events
        if command_type == "register_npc":
            npc = _as_dict(command.get("npc"), "npc")
            if not npc.get("id") or not npc.get("name") or npc["id"] in _as_dict(state["entities"], "entities"):
                raise WorldError("NPC must have a new id and name")
            return [("NpcRegistered", npc)]
        if command_type == "plan_activity":
            payload = {key: command[key] for key in ("activity_id", "entity_id", "title", "starts_at", "ends_at")}
            if any(not payload.get(key) for key in payload) or _parse_at(str(payload["ends_at"])) <= _parse_at(str(payload["starts_at"])):
                raise WorldError("activity needs id, entity, title, and increasing times")
            if payload["entity_id"] not in _as_dict(state["entities"], "entities"):
                raise WorldError("activity entity is not registered")
            if payload["activity_id"] in _as_dict(state["agenda"], "agenda"):
                raise WorldError("activity id already exists")
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
        if command_type == "cancel_action":
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            if action["status"] != "scheduled":
                raise WorldError("only a scheduled action can be cancelled")
            return [("ActionCancelled", {"action_id": action_id, "reason": str(command.get("reason") or "cancelled")})]
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
            action_id = str(command.get("action_id") or "")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            if action["status"] != "delivered":
                raise WorldError("only a delivered action can create a shared experience")
            experience_id = str(command.get("experience_id") or "")
            if not experience_id or experience_id in _as_dict(state["experiences"], "experiences"):
                raise WorldError("experience needs a new id")
            return [
                (
                    "ExperienceCommitted",
                    {"experience_id": experience_id, "action_id": action_id, "content": str(command.get("content") or "")},
                )
            ]
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
            entity = _as_dict(_as_dict(state["entities"], "entities").get(str(proposal["entity_id"])), "proposal entity")
            if str(proposal["template_id"]) not in _as_list(entity.get("templates", []), "entity templates"):
                raise WorldError("proposal template is no longer permitted")
            return [
                ("LifeOutcomeValidated", {"outcome_id": proposal_id, "validation": "registered_entity/template/content_bound", "rule_version": "life-proposal-v1"}),
                ("ModelProposalAccepted", {"proposal_id": proposal_id}),
                (
                    "ExperienceCommitted",
                    {
                        "experience_id": proposal_id,
                        "action_id": None,
                        "content": proposal["content"],
                        "source_proposal_id": proposal_id,
                    },
                ),
            ]
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
            experience_id = str(command.get("experience_id") or "")
            action_id = str(command.get("action_id") or "")
            experience = _as_dict(_as_dict(state["experiences"], "experiences").get(experience_id), "experience")
            action = _as_dict(_as_dict(state["actions"], "actions").get(action_id), "action")
            if experience.get("shared") or action["status"] != "delivered":
                raise WorldError("experience sharing requires one delivered action and an unshared experience")
            return [("ExperienceShared", {"experience_id": experience_id, "action_id": action_id})]
        if command_type == "select_life_share":
            needs = _as_dict(state["needs"], "needs")
            if needs["initiative"] < 20 or needs["security"] < 45:
                return [("LifeShareDeferred", {"reason": "needs_prefer_private"})]
            day = str(_as_dict(state["clock"], "clock")["logical_at"])[:10]
            if day in _as_dict(state.get("share_decisions", {}), "share decisions"):
                return [("LifeShareDeferred", {"reason": "daily_limit"})]
            candidate = next((key for key, value in _as_dict(state["experiences"], "experiences").items() if not value.get("shared")), None)
            return [("LifeShareSelected", {"experience_id": candidate})] if candidate else [("LifeShareDeferred", {"reason": "no_unshared_experience"})]
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
            events: list[tuple[str, dict[str, object]]] = [
                ("TurnAppraised", {"appraisal": appraisal, "policy": policies.get(appraisal, "自然回应当前消息。")}),
                ("IntentCreated", {"intent_id": str(command["intent_id"]), "kind": "reply", "status": "open"}),
            ]
            events.extend(
                ("NeedChanged", {"need": need, "delta": delta})
                for need, delta in need_deltas.get(appraisal, {}).items()
            )
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
        next_state["goals"] = {str(goal["id"]): {**goal, "progress": 0, "status": "active"} for goal in _as_list(payload.get("long_term_goals", []), "long-term goals")}
    elif event.event_type == "NpcRegistered":
        npc = dict(payload)
        npc["status"] = "active"
        _as_dict(next_state["entities"], "entities")[str(npc["id"])] = npc
    elif event.event_type == "ClockModeChanged":
        next_state["clock"] = {**_as_dict(next_state["clock"], "clock"), **payload}
    elif event.event_type == "ClockAdvanced":
        _as_dict(next_state["clock"], "clock")["logical_at"] = payload["target_logical_at"]
    elif event.event_type == "ActivityPlanned":
        item = {**payload, "status": "planned"}
        _as_dict(next_state["agenda"], "agenda")[str(item["activity_id"])] = item
    elif event.event_type in {"ActivityStarted", "ActivityCompleted", "ActivityInterrupted", "ActivityCancelled"}:
        activity = _as_dict(next_state["agenda"], "agenda")[str(payload["activity_id"])]
        activity["status"] = {
            "ActivityStarted": "active", "ActivityCompleted": "completed",
            "ActivityInterrupted": "interrupted", "ActivityCancelled": "cancelled",
        }[event.event_type]
    elif event.event_type == "ActionScheduled":
        item = {**payload, "status": "scheduled"}
        _as_dict(next_state["actions"], "actions")[str(item["action_id"])] = item
    elif event.event_type == "ActionAttempted":
        _as_dict(next_state["actions"], "actions")[str(payload["action_id"])]["attempted"] = True
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
    elif event.event_type == "NpcRelationshipChanged":
        relationships = _as_dict(next_state["relationships"], "relationships")
        relation = _as_dict(relationships.setdefault(str(payload["entity_id"]), {}), "relationship")
        dimension = str(payload["dimension"])
        relation[dimension] = max(-100, min(100, int(relation.get(dimension, 0)) + int(payload["delta"])))
    elif event.event_type == "NeedChanged":
        needs = _as_dict(next_state["needs"], "needs")
        need = str(payload["need"])
        needs[need] = max(0, min(100, int(needs.get(need, 50)) + int(payload["delta"])))
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
    elif event.event_type == "ExperienceShared":
        _as_dict(next_state["experiences"], "experiences")[str(payload["experience_id"])]["shared"] = True
        _as_dict(next_state["experiences"], "experiences")[str(payload["experience_id"])]["shared_action_id"] = payload["action_id"]
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
        "share_decisions": {},
        "goals": {},
        "outcomes": {},
    }


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
