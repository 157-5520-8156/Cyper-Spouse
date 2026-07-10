import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from companion_daemon.models import IncomingMessage, LifeRuntimeState, MoodState, Platform
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

                create table if not exists outbox_messages (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  platform text not null,
                  text text not null,
                  kind text not null,
                  status text not null,
                  created_at text not null,
                  delivered_at text,
                  failed_at text,
                  failure_reason text
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
                  perceived_respect integer not null default 50,
                  perceived_reliability integer not null default 50,
                  perceived_responsiveness integer not null default 50,
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
                  has_unread integer not null default 0,
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

                create table if not exists tool_proposals (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  kind text not null,
                  risk text not null,
                  summary text not null,
                  status text not null,
                  created_at text not null
                );

                create table if not exists life_runtime (
                  canonical_user_id text primary key references users(id),
                  activity text not null,
                  activity_kind text not null,
                  base_attention_demand integer not null,
                  attention_demand integer not null,
                  interruptible integer not null,
                  started_at text not null,
                  ends_at text not null,
                  phone_attention text not null,
                  notification_count integer not null default 0,
                  last_notification_at text,
                  last_read_at text,
                  user_event_effect text,
                  user_event_effect_until text,
                  user_event_attention_delta integer not null default 0,
                  state_effect text,
                  updated_at text not null
                );

                create table if not exists life_runtime_events (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  kind text not null,
                  content text not null,
                  started_at text not null,
                  ends_at text not null,
                  status text not null,
                  source text not null,
                  shared_at text,
                  created_at text not null
                );

                create table if not exists life_day_plans (
                  canonical_user_id text not null references users(id),
                  local_date text not null,
                  generated_at text not null,
                  primary key (canonical_user_id, local_date)
                );

                create table if not exists life_day_plan_items (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  local_date text not null,
                  slot text not null,
                  kind text not null,
                  activity text not null,
                  attention_demand integer not null,
                  interruptible integer not null,
                  starts_at text not null,
                  ends_at text not null,
                  status text not null default 'planned',
                  adjustment_note text,
                  created_at text not null,
                  unique (canonical_user_id, local_date, slot)
                );

                create table if not exists social_tasks (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  kind text not null,
                  status text not null,
                  platform text not null,
                  platform_user_id text not null,
                  payload_json text not null,
                  reason text not null,
                  due_at text not null,
                  expires_at text not null,
                  claimed_at text,
                  resolved_at text,
                  created_at text not null
                );
                create index if not exists idx_social_tasks_due
                  on social_tasks (kind, status, due_at);
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
            self._ensure_column(conn, "mood_state", "perceived_respect", "integer not null default 50")
            self._ensure_column(conn, "mood_state", "perceived_reliability", "integer not null default 50")
            self._ensure_column(conn, "mood_state", "perceived_responsiveness", "integer not null default 50")
            self._ensure_column(conn, "mood_state", "last_user_intent", "text")
            self._ensure_column(conn, "mood_state", "last_interaction_event", "text")
            self._ensure_column(conn, "mood_state", "reply_style_hint", "text")
            self._ensure_column(conn, "mood_state", "emotion_vector_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "emotion_baseline_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "emotion_affinity_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "last_emotion_impact_json", "text not null default '{}'")
            self._ensure_column(conn, "mood_state", "last_emotion_source", "text")
            self._ensure_column(conn, "mood_state", "has_unread", "integer not null default 0")
            self._ensure_column(conn, "proactive_events", "trigger_type", "text")
            self._ensure_column(conn, "life_runtime", "user_event_effect", "text")
            self._ensure_column(conn, "life_runtime", "user_event_effect_until", "text")
            self._ensure_column(conn, "life_runtime", "base_attention_demand", "integer")
            self._ensure_column(conn, "life_runtime", "user_event_attention_delta", "integer not null default 0")
            self._ensure_column(conn, "life_runtime", "state_effect", "text")
            self._ensure_column(conn, "life_runtime_events", "shared_at", "text")
            conn.execute(
                "update life_runtime set base_attention_demand = attention_demand where base_attention_demand is null"
            )

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
                insert or ignore into platform_accounts
                  (platform, platform_user_id, canonical_user_id, created_at)
                values (?, ?, ?, ?)
                """,
                (platform, platform_user_id, canonical_user_id, now),
            )
            row = conn.execute(
                """
                select canonical_user_id
                from platform_accounts
                where platform = ? and platform_user_id = ?
                """,
                (platform, platform_user_id),
            ).fetchone()
            return str(row["canonical_user_id"]) if row else canonical_user_id

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

    def primary_platform_user_id(self, canonical_user_id: str, *, platform: Platform = "qq") -> str:
        with self.connect() as conn:
            row = conn.execute(
                """
                select platform_user_id from platform_accounts
                where canonical_user_id = ? and platform = ?
                order by created_at asc limit 1
                """,
                (canonical_user_id, platform),
            ).fetchone()
        return str(row["platform_user_id"]) if row else canonical_user_id

    def has_active_social_task(self, canonical_user_id: str, *, kind: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from social_tasks
                where canonical_user_id = ? and kind = ? and status in ('pending', 'claimed')
                limit 1
                """,
                (canonical_user_id, kind),
            ).fetchone()
        return row is not None

    def social_task_payload(self, task_id: int) -> dict[str, object]:
        with self.connect() as conn:
            row = conn.execute(
                "select payload_json from social_tasks where id = ?",
                (task_id,),
            ).fetchone()
        if not row or not row["payload_json"]:
            return {}
        return json.loads(str(row["payload_json"]))

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

    def queue_outgoing(
        self,
        canonical_user_id: str,
        platform: Platform,
        text: str,
        *,
        kind: str,
    ) -> int:
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into outbox_messages (
                  canonical_user_id, platform, text, kind, status, created_at
                ) values (?, ?, ?, ?, 'planned', ?)
                """,
                (canonical_user_id, platform, text, kind, now),
            )
            return int(cursor.lastrowid)

    def mark_outgoing_delivered(self, delivery_id: int) -> sqlite3.Row | None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, platform, text, kind, status from outbox_messages where id = ?",
                (delivery_id,),
            ).fetchone()
            if not row or row["status"] != "planned":
                return row
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
            return row

    def mark_outgoing_failed(self, delivery_id: int, reason: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update outbox_messages
                set status = 'failed', failed_at = ?, failure_reason = ?
                where id = ? and status = 'planned'
                """,
                (utc_now().isoformat(), reason[:500], delivery_id),
            )

    def outbox_message(self, delivery_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                select canonical_user_id, platform, text, kind, status, delivered_at, failed_at, failure_reason
                from outbox_messages where id = ?
                """,
                (delivery_id,),
            ).fetchone()

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
                  initiative, emotional_charge, boundary_level,
                  perceived_respect, perceived_reliability, perceived_responsiveness,
                  relationship_stage,
                  unresolved_emotion, last_user_intent, last_interaction_event,
                  reply_style_hint, emotion_vector_json, emotion_baseline_json,
                  emotion_affinity_json, last_emotion_impact_json, last_emotion_source,
                  last_platform, has_unread, updated_at
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
            perceived_respect=row["perceived_respect"],
            perceived_reliability=row["perceived_reliability"],
            perceived_responsiveness=row["perceived_responsiveness"],
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
            has_unread=bool(row["has_unread"]),
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
                  boundary_level, perceived_respect, perceived_reliability, perceived_responsiveness,
                  relationship_stage, unresolved_emotion,
                  last_user_intent, last_interaction_event, reply_style_hint,
                  emotion_vector_json, emotion_baseline_json, emotion_affinity_json,
                  last_emotion_impact_json, last_emotion_source, last_platform, has_unread, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    state.perceived_respect,
                    state.perceived_reliability,
                    state.perceived_responsiveness,
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
                    state.has_unread,
                    state.updated_at.isoformat(),
                ),
            )

    def get_life_runtime(self, canonical_user_id: str) -> LifeRuntimeState | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select activity, activity_kind, base_attention_demand, attention_demand, interruptible, started_at, ends_at,
                       phone_attention, notification_count, last_notification_at, last_read_at,
                       user_event_effect, user_event_effect_until, user_event_attention_delta, state_effect, updated_at
                from life_runtime where canonical_user_id = ?
                """,
                (canonical_user_id,),
            ).fetchone()
        if not row:
            return None
        return LifeRuntimeState(
            activity=row["activity"],
            activity_kind=row["activity_kind"],
            base_attention_demand=row["base_attention_demand"],
            attention_demand=row["attention_demand"],
            interruptible=bool(row["interruptible"]),
            started_at=datetime.fromisoformat(row["started_at"]),
            ends_at=datetime.fromisoformat(row["ends_at"]),
            phone_attention=row["phone_attention"],
            notification_count=row["notification_count"],
            last_notification_at=(datetime.fromisoformat(row["last_notification_at"]) if row["last_notification_at"] else None),
            last_read_at=datetime.fromisoformat(row["last_read_at"]) if row["last_read_at"] else None,
            user_event_effect=row["user_event_effect"],
            user_event_effect_until=(datetime.fromisoformat(row["user_event_effect_until"]) if row["user_event_effect_until"] else None),
            user_event_attention_delta=row["user_event_attention_delta"],
            state_effect=row["state_effect"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_life_runtime(self, canonical_user_id: str, runtime: LifeRuntimeState) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert or replace into life_runtime (
                  canonical_user_id, activity, activity_kind, base_attention_demand, attention_demand, interruptible,
                  started_at, ends_at, phone_attention, notification_count, last_notification_at,
                  last_read_at, user_event_effect, user_event_effect_until, user_event_attention_delta, state_effect, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id, runtime.activity, runtime.activity_kind, runtime.base_attention_demand, runtime.attention_demand,
                    runtime.interruptible, runtime.started_at.isoformat(), runtime.ends_at.isoformat(),
                    runtime.phone_attention, runtime.notification_count,
                    runtime.last_notification_at.isoformat() if runtime.last_notification_at else None,
                    runtime.last_read_at.isoformat() if runtime.last_read_at else None,
                    runtime.user_event_effect,
                    runtime.user_event_effect_until.isoformat() if runtime.user_event_effect_until else None,
                    runtime.user_event_attention_delta,
                    runtime.state_effect,
                    runtime.updated_at.isoformat(),
                ),
            )

    def record_life_event(
        self,
        canonical_user_id: str,
        *,
        kind: str,
        content: str,
        started_at: datetime,
        ends_at: datetime,
        status: str,
        source: str,
        shared_at: datetime | None = None,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into life_runtime_events (
                  canonical_user_id, kind, content, started_at, ends_at, status, source, shared_at, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id, kind, content, started_at.isoformat(), ends_at.isoformat(), status,
                    source, shared_at.isoformat() if shared_at else None, utc_now().isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def complete_active_life_events(self, canonical_user_id: str, *, completed_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update life_runtime_events
                set status = 'completed', ends_at = ?
                where canonical_user_id = ? and status = 'active'
                """,
                (completed_at.isoformat(), canonical_user_id),
            )

    def recent_life_events(self, canonical_user_id: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source, shared_at
                from life_runtime_events where canonical_user_id = ?
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def upcoming_life_plan_items(self, canonical_user_id: str, *, now: datetime, limit: int = 5) -> list[sqlite3.Row]:
        now = now.astimezone(UTC)
        with self.connect() as conn:
            rows = conn.execute(
                """
                select slot, kind, activity, attention_demand, interruptible, starts_at, ends_at, status, adjustment_note
                from life_day_plan_items
                where canonical_user_id = ? and ends_at > ?
                order by starts_at asc limit ?
                """,
                (canonical_user_id, now.isoformat(), limit),
            ).fetchall()
        return list(rows)

    def unshared_private_life_events(self, canonical_user_id: str, limit: int = 4) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source
                from life_runtime_events
                where canonical_user_id = ? and kind = 'private_life_event'
                  and status = 'completed' and shared_at is null
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(rows)

    def mark_life_event_shared(self, event_id: int, *, shared_at: datetime | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "update life_runtime_events set shared_at = ? where id = ? and shared_at is null",
                ((shared_at or utc_now()).isoformat(), event_id),
            )

    def life_event_by_source(self, canonical_user_id: str, source: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source, shared_at
                from life_runtime_events where canonical_user_id = ? and source = ?
                order by id desc limit 1
                """,
                (canonical_user_id, source),
            ).fetchone()

    def has_life_day_plan(self, canonical_user_id: str, local_date: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "select 1 from life_day_plans where canonical_user_id = ? and local_date = ?",
                (canonical_user_id, local_date),
            ).fetchone()
        return row is not None

    def save_life_day_plan(self, canonical_user_id: str, local_date: str, items: list[dict[str, object]]) -> None:
        """Persist a private schedule. Planned entries are not lived facts."""
        with self.connect() as conn:
            conn.execute(
                "insert or ignore into life_day_plans (canonical_user_id, local_date, generated_at) values (?, ?, ?)",
                (canonical_user_id, local_date, utc_now().isoformat()),
            )
            conn.executemany(
                """
                insert or ignore into life_day_plan_items (
                  canonical_user_id, local_date, slot, kind, activity, attention_demand,
                  interruptible, starts_at, ends_at, status, adjustment_note, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', null, ?)
                """,
                [
                    (
                        canonical_user_id, local_date, item["slot"], item["kind"], item["activity"],
                        item["attention_demand"], int(bool(item["interruptible"])), item["starts_at"],
                        item["ends_at"], utc_now().isoformat(),
                    )
                    for item in items
                ],
            )

    def life_day_plan_item_at(self, canonical_user_id: str, now: datetime) -> sqlite3.Row | None:
        now = now.astimezone(UTC)
        with self.connect() as conn:
            row = conn.execute(
                """
                select id, slot, kind, activity, attention_demand, interruptible, starts_at, ends_at, status, adjustment_note
                from life_day_plan_items
                where canonical_user_id = ? and starts_at <= ? and ends_at > ?
                order by starts_at desc limit 1
                """,
                (canonical_user_id, now.isoformat(), now.isoformat()),
            ).fetchone()
        return row

    def update_life_day_plan_status(self, canonical_user_id: str, *, before: datetime, status: str) -> None:
        before = before.astimezone(UTC)
        with self.connect() as conn:
            conn.execute(
                """
                update life_day_plan_items set status = ?
                where canonical_user_id = ? and ends_at <= ? and status in ('planned', 'active')
                """,
                (status, canonical_user_id, before.isoformat()),
            )

    def activate_life_day_plan_item(self, item_id: int) -> None:
        with self.connect() as conn:
            conn.execute("update life_day_plan_items set status = 'active' where id = ?", (item_id,))

    def adjust_next_life_day_plan_item(
        self,
        canonical_user_id: str,
        *,
        now: datetime,
        activity: str,
        note: str,
        attention_delta: int = 0,
    ) -> None:
        """A salient interaction may nudge one future activity, never rewrite history."""
        with self.connect() as conn:
            row = conn.execute(
                """
                select id, attention_demand from life_day_plan_items
                where canonical_user_id = ? and starts_at > ? and status = 'planned'
                order by starts_at asc limit 1
                """,
                (canonical_user_id, now.isoformat()),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    update life_day_plan_items
                    set activity = ?, adjustment_note = ?, attention_demand = ?
                    where id = ?
                    """,
                    (activity, note, max(0, min(100, row["attention_demand"] + attention_delta)), row["id"]),
                )

    def create_social_task(
        self,
        canonical_user_id: str,
        *,
        kind: str,
        platform: Platform,
        platform_user_id: str,
        payload: dict[str, object],
        reason: str,
        due_at: datetime,
        expires_at: datetime,
    ) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into social_tasks (
                  canonical_user_id, kind, status, platform, platform_user_id, payload_json,
                  reason, due_at, expires_at, claimed_at, resolved_at, created_at
                ) values (?, ?, 'pending', ?, ?, ?, ?, ?, ?, null, null, ?)
                """,
                (
                    canonical_user_id, kind, platform, platform_user_id,
                    json.dumps(payload, ensure_ascii=False), reason, due_at.astimezone(UTC).isoformat(),
                    expires_at.astimezone(UTC).isoformat(), utc_now().isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def cancel_social_task(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'cancelled', resolved_at = ?
                where id = ? and status in ('pending', 'claimed')
                """,
                (utc_now().isoformat(), task_id),
            )

    def resolve_social_task(self, task_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'resolved', resolved_at = ?
                where id = ? and status in ('pending', 'claimed')
                """,
                (utc_now().isoformat(), task_id),
            )

    def cancel_active_social_tasks(self, canonical_user_id: str, *, kind: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'cancelled', resolved_at = ?
                where canonical_user_id = ? and kind = ? and status in ('pending', 'claimed')
                """,
                (utc_now().isoformat(), canonical_user_id, kind),
            )

    def claim_due_social_tasks(self, *, kind: str, now: datetime, limit: int = 8) -> list[sqlite3.Row]:
        """Claim due work; stale claims are retried after a daemon crash."""
        now = now.astimezone(UTC)
        stale_before = now - timedelta(minutes=10)
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'expired', resolved_at = ?
                where kind = ? and status in ('pending', 'claimed') and expires_at <= ?
                """,
                (now.isoformat(), kind, now.isoformat()),
            )
            ids = [
                row["id"]
                for row in conn.execute(
                    """
                    select id from social_tasks
                    where kind = ? and due_at <= ? and expires_at > ?
                      and (status = 'pending' or (status = 'claimed' and claimed_at <= ?))
                    order by due_at asc limit ?
                    """,
                    (kind, now.isoformat(), now.isoformat(), stale_before.isoformat(), limit),
                ).fetchall()
            ]
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"update social_tasks set status = 'claimed', claimed_at = ? where id in ({placeholders})",
                (now.isoformat(), *ids),
            )
            rows = conn.execute(
                f"""
                select id, canonical_user_id, kind, platform, platform_user_id, payload_json, reason,
                       due_at, expires_at, status, claimed_at, created_at
                from social_tasks where id in ({placeholders}) order by due_at asc
                """,
                ids,
            ).fetchall()
        return list(rows)

    def recent_social_tasks(self, canonical_user_id: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, kind, status, reason, due_at, expires_at, claimed_at, resolved_at, created_at
                from social_tasks where canonical_user_id = ?
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def next_due_social_task(
        self,
        canonical_user_id: str,
        *,
        kinds: tuple[str, ...],
        now: datetime,
    ) -> sqlite3.Row | None:
        if not kinds:
            return None
        now = now.astimezone(UTC)
        placeholders = ",".join("?" for _ in kinds)
        with self.connect() as conn:
            conn.execute(
                f"""
                update social_tasks set status = 'expired', resolved_at = ?
                where canonical_user_id = ? and kind in ({placeholders})
                  and status = 'pending' and expires_at <= ?
                """,
                (now.isoformat(), canonical_user_id, *kinds, now.isoformat()),
            )
            return conn.execute(
                f"""
                select id, canonical_user_id, kind, status, reason, due_at, expires_at, created_at, payload_json
                from social_tasks where canonical_user_id = ? and kind in ({placeholders})
                  and status = 'pending' and due_at <= ? and expires_at > ?
                order by due_at asc limit 1
                """,
                (canonical_user_id, *kinds, now.isoformat(), now.isoformat()),
            ).fetchone()

    def defer_social_task(self, task_id: int, *, due_at: datetime) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set due_at = ?
                where id = ? and status = 'pending'
                """,
                (due_at.astimezone(UTC).isoformat(), task_id),
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

    def record_tool_proposal(
        self,
        canonical_user_id: str,
        *,
        kind: str,
        risk: str,
        summary: str,
        status: str = "proposed",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                insert into tool_proposals (
                  canonical_user_id, kind, risk, summary, status, created_at
                ) values (?, ?, ?, ?, ?, ?)
                """,
                (canonical_user_id, kind, risk, summary, status, utc_now().isoformat()),
            )

    def recent_tool_proposals(self, canonical_user_id: str, limit: int = 8) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select kind, risk, summary, status, created_at
                from tool_proposals
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

    def message_count_since(
        self,
        canonical_user_id: str,
        *,
        direction: str,
        since_iso: str,
    ) -> int:
        with self.connect() as conn:
            row = conn.execute(
                """
                select count(*) as count
                from messages
                where canonical_user_id = ?
                  and direction = ?
                  and sent_at > ?
                """,
                (canonical_user_id, direction, since_iso),
            ).fetchone()
        return int(row["count"])

    def last_proactive_event(self, canonical_user_id: str) -> sqlite3.Row | None:
        """Return the newest proactive decision, sent or withheld."""
        with self.connect() as conn:
            row = conn.execute(
                """
                select should_send, cooldown_minutes, created_at
                from proactive_events
                where canonical_user_id = ?
                order by id desc
                limit 1
                """,
                (canonical_user_id,),
            ).fetchone()
        return row

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
            duplicates = conn.execute(
                """
                select content
                from memories
                where canonical_user_id = ? and kind = ?
                order by updated_at desc
                limit 30
                """,
                (canonical_user_id, kind),
            ).fetchall()
            for row in duplicates:
                existing = str(row["content"])
                if _text_similarity(existing, content) > 0.55:
                    conn.execute(
                        """
                        update memories
                        set source = ?, confidence = max(confidence, ?), updated_at = ?
                        where canonical_user_id = ? and kind = ? and content = ?
                        """,
                        (source, confidence, now, canonical_user_id, kind, existing),
                    )
                    return
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

    def latest_memory(self, canonical_user_id: str, *, kind: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                select kind, content, confidence, updated_at
                from memories
                where canonical_user_id = ? and kind = ?
                order by updated_at desc
                limit 1
                """,
                (canonical_user_id, kind),
            ).fetchone()

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

    def delete_memory(self, canonical_user_id: str, *, kind: str, content: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                delete from memories
                where canonical_user_id = ? and kind = ? and content = ?
                """,
                (canonical_user_id, kind, content),
            )
            return cursor.rowcount

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

    def last_initiated_delivery(self, canonical_user_id: str, platform: str) -> str | None:
        """Return the newest direct proactive or life-share delivery for a platform."""
        channels = (platform, f"{platform}:life_event")
        with self.connect() as conn:
            row = conn.execute(
                """
                select sent_at from proactive_delivery
                where canonical_user_id = ? and platform in (?, ?)
                order by sent_at desc limit 1
                """,
                (canonical_user_id, *channels),
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


def _text_similarity(left: str, right: str) -> float:
    left = left.lower().strip()[:80]
    right = right.lower().strip()[:80]
    if not left or not right:
        return 0.0
    return _longest_common_substring(left, right) / max(len(left), len(right))


def _longest_common_substring(left: str, right: str) -> int:
    previous = [0] * (len(right) + 1)
    best = 0
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, start=1):
            value = previous[index - 1] + 1 if left_char == right_char else 0
            current.append(value)
            best = max(best, value)
        previous = current
    return best
