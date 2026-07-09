import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from companion_daemon.models import IncomingMessage, MoodState, Platform
from companion_daemon.time import utc_now


class CompanionStore:
    def __init__(self, path: Path, *, primary_user_id: str | None = "geoff"):
        self.path = path
        self.primary_user_id = primary_user_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists users (
                  id text primary key,
                  display_name text not null,
                  created_at text not null
                );

                create table if not exists platform_accounts (
                  platform text not null,
                  platform_user_id text not null,
                  canonical_user_id text not null references users(id),
                  created_at text not null,
                  primary key (platform, platform_user_id)
                );

                create table if not exists messages (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  platform text not null,
                  platform_user_id text not null,
                  channel_id text,
                  message_id text,
                  direction text not null,
                  text text not null,
                  attachments_json text not null default '[]',
                  sent_at text not null
                );

                create table if not exists mood_state (
                  canonical_user_id text primary key references users(id),
                  mood text not null,
                  intimacy integer not null,
                  trust integer not null,
                  attachment integer not null,
                  patience integer not null default 70,
                  security integer not null default 45,
                  curiosity integer not null default 40,
                  initiative integer not null default 20,
                  emotional_charge integer not null default 0,
                  boundary_level integer not null default 0,
                  relationship_stage text not null default 'stranger',
                  unresolved_emotion text,
                  last_user_intent text,
                  last_interaction_event text,
                  reply_style_hint text,
                  emotion_vector_json text not null default '{}',
                  emotion_baseline_json text not null default '{}',
                  emotion_affinity_json text not null default '{}',
                  last_emotion_impact_json text not null default '{}',
                  last_emotion_source text,
                  last_platform text,
                  updated_at text not null
                );

                create table if not exists memories (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  kind text not null,
                  content text not null,
                  source text not null,
                  confidence real not null,
                  created_at text not null,
                  updated_at text not null,
                  unique (canonical_user_id, kind, content)
                );

                create table if not exists proactive_events (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  private_thought text not null,
                  should_send integer not null,
                  platform text,
                  message_type text not null,
                  message text,
                  sticker_category text,
                  trigger_type text,
                  cooldown_minutes integer not null,
                  created_at text not null
                );

                create table if not exists proactive_delivery (
                  canonical_user_id text not null,
                  platform text not null,
                  sent_at text not null,
                  primary key (canonical_user_id, platform, sent_at)
                );

                create table if not exists usage_events (
                  id integer primary key autoincrement,
                  kind text not null,
                  estimated_cny real not null,
                  note text not null,
                  created_at text not null
                );

                create table if not exists interaction_events (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  event_kind text not null,
                  user_intent text not null,
                  intensity integer not null,
                  private_note text not null,
                  platform text not null,
                  message_id text,
                  created_at text not null
                );
                """
            )
            self._ensure_column(conn, "messages", "attachments_json", "text not null default '[]'")
            self._ensure_column(
                conn,
                "mood_state",
                "relationship_stage",
                "text not null default 'stranger'",
            )
            self._ensure_column(conn, "mood_state", "patience", "integer not null default 70")
            self._ensure_column(conn, "mood_state", "security", "integer not null default 45")
            self._ensure_column(conn, "mood_state", "curiosity", "integer not null default 40")
            self._ensure_column(conn, "mood_state", "initiative", "integer not null default 20")
            self._ensure_column(conn, "mood_state", "emotional_charge", "integer not null default 0")
            self._ensure_column(conn, "mood_state", "boundary_level", "integer not null default 0")
            self._ensure_column(conn, "mood_state", "last_user_intent", "text")
            self._ensure_column(conn, "mood_state", "last_interaction_event", "text")
            self._ensure_column(conn, "mood_state", "reply_style_hint", "text")
            self._ensure_column(conn, "mood_state", "emotion_vector_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "emotion_baseline_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "emotion_affinity_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "last_emotion_impact_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "last_emotion_source", "text")
            self._ensure_column(conn, "proactive_events", "trigger_type", "text")

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"pragma table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"alter table {table} add column {column} {definition}")

    def resolve_user(self, platform: Platform, platform_user_id: str) -> str:
        with self.connect() as conn:
            existing = conn.execute(
                """
                select canonical_user_id
                from platform_accounts
                where platform = ? and platform_user_id = ?
                """,
                (platform, platform_user_id),
            ).fetchone()
            if existing:
                return str(existing["canonical_user_id"])

            canonical_user_id = self.primary_user_id or (
                "geoff" if platform_user_id in {"geoff", "self", "me"} else platform_user_id
            )
            now = utc_now().isoformat()
            conn.execute(
                "insert or ignore into users (id, display_name, created_at) values (?, ?, ?)",
                (canonical_user_id, canonical_user_id, now),
            )
            conn.execute(
                """
                insert into platform_accounts
                  (platform, platform_user_id, canonical_user_id, created_at)
                values (?, ?, ?, ?)
                """,
                (platform, platform_user_id, canonical_user_id, now),
            )
            return canonical_user_id

    def map_account(self, platform: Platform, platform_user_id: str, canonical_user_id: str) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                "insert or ignore into users (id, display_name, created_at) values (?, ?, ?)",
                (canonical_user_id, canonical_user_id, now),
            )
            conn.execute(
                """
                insert or replace into platform_accounts
                  (platform, platform_user_id, canonical_user_id, created_at)
                values (?, ?, ?, ?)
                """,
                (platform, platform_user_id, canonical_user_id, now),
            )

    def save_incoming(self, canonical_user_id: str, message: IncomingMessage) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into messages (
                  canonical_user_id, platform, platform_user_id, channel_id, message_id,
                  direction, text, attachments_json, sent_at
                ) values (?, ?, ?, ?, ?, 'in', ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    message.platform,
                    message.platform_user_id,
                    message.channel_id,
                    message.message_id,
                    message.text,
                    message.model_dump_json(include={"attachments"}),
                    message.sent_at.isoformat(),
                ),
            )

    def save_outgoing(self, canonical_user_id: str, platform: Platform, text: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into messages (
                  canonical_user_id, platform, platform_user_id, direction, text, sent_at
                ) values (?, ?, '', 'out', ?, ?)
                """,
                (canonical_user_id, platform, text, utc_now().isoformat()),
            )

    def get_mood_state(self, canonical_user_id: str) -> MoodState:
        with self.connect() as conn:
            row = conn.execute(
                """
                select
                  mood, intimacy, trust, attachment, patience, security, curiosity,
                  initiative, emotional_charge, boundary_level, relationship_stage,
                  unresolved_emotion, last_user_intent, last_interaction_event,
                  reply_style_hint, emotion_vector_json, emotion_baseline_json,
                  emotion_affinity_json, last_emotion_impact_json, last_emotion_source,
                  last_platform, updated_at
                from mood_state
                where canonical_user_id = ?
                """,
                (canonical_user_id,),
            ).fetchone()
        if not row:
            return MoodState()
        return MoodState(
            mood=row["mood"],
            intimacy=row["intimacy"],
            trust=row["trust"],
            attachment=row["attachment"],
            patience=row["patience"],
            security=row["security"],
            curiosity=row["curiosity"],
            initiative=row["initiative"],
            emotional_charge=row["emotional_charge"],
            boundary_level=row["boundary_level"],
            relationship_stage=row["relationship_stage"],
            unresolved_emotion=row["unresolved_emotion"],
            last_user_intent=row["last_user_intent"],
            last_interaction_event=row["last_interaction_event"],
            reply_style_hint=row["reply_style_hint"],
            emotion_vector=_json_map(row["emotion_vector_json"]),
            emotion_baseline=_json_map(row["emotion_baseline_json"]),
            emotion_affinity=_json_map(row["emotion_affinity_json"]),
            last_emotion_impact=_json_map(row["last_emotion_impact_json"]),
            last_emotion_source=row["last_emotion_source"],
            last_platform=row["last_platform"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def has_mood_state(self, canonical_user_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "select 1 from mood_state where canonical_user_id = ?",
                (canonical_user_id,),
            ).fetchone()
        return row is not None

    def save_mood_state(self, canonical_user_id: str, state: MoodState) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into mood_state (
                  canonical_user_id, mood, intimacy, trust, attachment,
                  patience, security, curiosity, initiative, emotional_charge,
                  boundary_level, relationship_stage, unresolved_emotion,
                  last_user_intent, last_interaction_event, reply_style_hint,
                  emotion_vector_json, emotion_baseline_json, emotion_affinity_json,
                  last_emotion_impact_json, last_emotion_source, last_platform, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    state.mood,
                    state.intimacy,
                    state.trust,
                    state.attachment,
                    state.patience,
                    state.security,
                    state.curiosity,
                    state.initiative,
                    state.emotional_charge,
                    state.boundary_level,
                    state.relationship_stage,
                    state.unresolved_emotion,
                    state.last_user_intent,
                    state.last_interaction_event,
                    state.reply_style_hint,
                    json.dumps(state.emotion_vector, ensure_ascii=False),
                    json.dumps(state.emotion_baseline, ensure_ascii=False),
                    json.dumps(state.emotion_affinity, ensure_ascii=False),
                    json.dumps(state.last_emotion_impact, ensure_ascii=False),
                    state.last_emotion_source,
                    state.last_platform,
                    state.updated_at.isoformat(),
                ),
            )

    def record_interaction_event(
        self,
        canonical_user_id: str,
        *,
        event_kind: str,
        user_intent: str,
        intensity: int,
        private_note: str,
        platform: Platform,
        message_id: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into interaction_events (
                  canonical_user_id, event_kind, user_intent, intensity,
                  private_note, platform, message_id, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    event_kind,
                    user_intent,
                    intensity,
                    private_note,
                    platform,
                    message_id,
                    utc_now().isoformat(),
                ),
            )

    def recent_interaction_events(
        self,
        canonical_user_id: str,
        limit: int = 6,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select event_kind, user_intent, intensity, private_note, platform, created_at
                from interaction_events
                where canonical_user_id = ?
                order by id desc
                limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def recent_messages(self, canonical_user_id: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select direction, platform, text, sent_at
                from messages
                where canonical_user_id = ?
                order by id desc
                limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def recent_proactive_trigger_history(
        self,
        canonical_user_id: str,
        limit: int = 80,
    ) -> dict[str, datetime]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select trigger_type, created_at
                from proactive_events
                where canonical_user_id = ? and trigger_type is not null
                order by id desc
                limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        history: dict[str, datetime] = {}
        for row in rows:
            trigger_type = row["trigger_type"]
            if not trigger_type or trigger_type in history:
                continue
            history[str(trigger_type)] = datetime.fromisoformat(row["created_at"])
        return history

    def incoming_message_count(self, canonical_user_id: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as count
                from messages
                where canonical_user_id = ? and direction = 'in'
                """,
                (canonical_user_id,),
            ).fetchone()
        return int(row["count"])

    def platform_user_id(self, canonical_user_id: str, platform: Platform) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select platform_user_id
                from platform_accounts
                where canonical_user_id = ? and platform = ?
                order by created_at desc
                limit 1
                """,
                (canonical_user_id, platform),
            ).fetchone()
        return str(row["platform_user_id"]) if row else None

    def upsert_memory(
        self,
        canonical_user_id: str,
        *,
        kind: str,
        content: str,
        source: str,
        confidence: float = 0.7,
    ) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                insert into memories (
                  canonical_user_id, kind, content, source, confidence, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(canonical_user_id, kind, content)
                do update set
                  source = excluded.source,
                  confidence = max(memories.confidence, excluded.confidence),
                  updated_at = excluded.updated_at
                """,
                (canonical_user_id, kind, content, source, confidence, now, now),
            )

    def memories(self, canonical_user_id: str, limit: int = 12) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select kind, content, confidence, updated_at
                from memories
                where canonical_user_id = ?
                order by updated_at desc
                limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(rows)

    def memory_by_source(
        self,
        canonical_user_id: str,
        *,
        kind: str,
        source: str,
    ) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                select kind, content, confidence, updated_at
                from memories
                where canonical_user_id = ? and kind = ? and source = ?
                order by updated_at desc
                limit 1
                """,
                (canonical_user_id, kind, source),
            ).fetchone()

    def canonical_users(self) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("select id from users order by id").fetchall()
        return [str(row["id"]) for row in rows]

    def last_proactive_delivery(self, canonical_user_id: str, platform: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select sent_at
                from proactive_delivery
                where canonical_user_id = ? and platform = ?
                order by sent_at desc
                limit 1
                """,
                (canonical_user_id, platform),
            ).fetchone()
        return str(row["sent_at"]) if row else None

    def record_proactive_delivery(self, canonical_user_id: str, platform: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into proactive_delivery (canonical_user_id, platform, sent_at)
                values (?, ?, ?)
                """,
                (canonical_user_id, platform, utc_now().isoformat()),
            )

    def record_usage(self, kind: str, estimated_cny: float, *, note: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into usage_events (kind, estimated_cny, note, created_at)
                values (?, ?, ?, ?)
                """,
                (kind, estimated_cny, note, utc_now().isoformat()),
            )

    def usage_total(self, window: str, now: datetime) -> float:
        if window == "day":
            prefix = now.date().isoformat()
            where = "substr(created_at, 1, 10) = ?"
            args = (prefix,)
        elif window == "month":
            prefix = now.strftime("%Y-%m")
            where = "substr(created_at, 1, 7) = ?"
            args = (prefix,)
        else:
            raise ValueError(f"Unsupported usage window: {window}")
        with self.connect() as conn:
            row = conn.execute(
                f"select coalesce(sum(estimated_cny), 0) as total from usage_events where {where}",
                args,
            ).fetchone()
        return float(row["total"])

    def usage_count(self, kind: str, window: str, now: datetime) -> int:
        if window == "day":
            prefix = now.date().isoformat()
            where = "kind = ? and substr(created_at, 1, 10) = ?"
            args = (kind, prefix)
        elif window == "month":
            prefix = now.strftime("%Y-%m")
            where = "kind = ? and substr(created_at, 1, 7) = ?"
            args = (kind, prefix)
        else:
            raise ValueError(f"Unsupported usage window: {window}")
        with self.connect() as conn:
            row = conn.execute(
                f"select count(*) as count from usage_events where {where}",
                args,
            ).fetchone()
        return int(row["count"])

    def save_proactive_event(
        self,
        canonical_user_id: str,
        private_thought: str,
        should_send: bool,
        platform: str | None,
        message_type: str,
        message: str | None,
        sticker_category: str | None,
        trigger_type: str | None,
        cooldown_minutes: int,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into proactive_events (
                  canonical_user_id, private_thought, should_send, platform,
                  message_type, message, sticker_category, trigger_type, cooldown_minutes, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    private_thought,
                    int(should_send),
                    platform,
                    message_type,
                    message,
                    sticker_category,
                    trigger_type,
                    cooldown_minutes,
                    utc_now().isoformat(),
                ),
            )


def _json_map(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    result = {}
    for key, value in data.items():
        try:
            result[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return result
