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
        self.world_mode_enabled = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()
        self.world_mode_enabled = self._world_mode_is_persisted()

    def enable_world_mode(self) -> None:
        """Fail closed if a world-mode path tries to mutate a legacy write model."""
        self.world_mode_enabled = True
        with self.connect() as conn:
            conn.execute(
                "insert or replace into runtime_flags (key, value) values ('world_mode_enabled', '1')"
            )

    def _world_mode_is_persisted(self) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "select value from runtime_flags where key = 'world_mode_enabled'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    def _assert_legacy_behavior_write_allowed(self, operation: str) -> None:
        if self.world_mode_enabled or self._world_mode_is_persisted():
            raise RuntimeError(f"world mode forbids legacy behaviour write: {operation}")

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
                create table if not exists runtime_flags (
                  key text primary key,
                  value text not null
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

                create table if not exists fact_ledger (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  subject text not null,
                  predicate text not null,
                  fact_key text,
                  value text not null,
                  status text not null default 'active',
                  confidence real not null,
                  source text not null,
                  valid_from text not null,
                  valid_to text,
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_fact_ledger_active
                  on fact_ledger (canonical_user_id, subject, status, updated_at);
                create index if not exists idx_fact_ledger_key
                  on fact_ledger (canonical_user_id, subject, fact_key, status);

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

                create table if not exists turn_traces (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  direction text not null,
                  appraisal text not null,
                  expression_policy text not null,
                  allowed_facts_json text not null,
                  short_lived_constraint text,
                  observable_reason text not null,
                  output_text text,
                  delivery_id integer references outbox_messages(id),
                  status text not null,
                  failure_reason text,
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_turn_traces_user
                  on turn_traces (canonical_user_id, id desc);

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

                create table if not exists model_usage_events (
                  id integer primary key autoincrement,
                  purpose text not null,
                  model text not null,
                  status text not null,
                  latency_ms integer not null,
                  prompt_tokens integer not null,
                  completion_tokens integer not null,
                  reasoning_tokens integer not null,
                  cache_hit_tokens integer not null,
                  cache_miss_tokens integer not null,
                  total_tokens integer not null,
                  error text not null,
                  world_id text not null default '',
                  turn_id text not null default '',
                  action_id text not null default '',
                  cadence text not null default '',
                  attempt integer not null default 1,
                  pricing_version text not null default '',
                  estimated_cost_usd real not null default 0,
                  created_at text not null
                );
                create index if not exists idx_model_usage_created
                  on model_usage_events (created_at, purpose);

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

                create table if not exists calendar_events (
                  id integer primary key autoincrement,
                  canonical_user_id text not null references users(id),
                  title text not null,
                  event_type text not null,
                  starts_at text not null,
                  ends_at text not null,
                  status text not null default 'planned',
                  importance integer not null default 50,
                  source text not null,
                  details text,
                  memory_note text,
                  share_state text not null default 'private',
                  changed_reason text,
                  created_at text not null,
                  updated_at text not null
                );
                create index if not exists idx_calendar_events_window
                  on calendar_events (canonical_user_id, starts_at, ends_at, status);

                create table if not exists calendar_event_history (
                  id integer primary key autoincrement,
                  calendar_event_id integer not null references calendar_events(id),
                  from_status text,
                  to_status text not null,
                  reason text,
                  changed_at text not null
                );
                create index if not exists idx_calendar_event_history_event
                  on calendar_event_history (calendar_event_id, id);

                create table if not exists calendar_event_memories (
                  calendar_event_id integer primary key references calendar_events(id),
                  memory_id integer not null unique references memories(id),
                  linked_at text not null
                );

                create table if not exists calendar_weeks (
                  canonical_user_id text not null references users(id),
                  week_start text not null,
                  theme text not null,
                  summary text not null,
                  status text not null default 'active',
                  source text not null,
                  created_at text not null,
                  updated_at text not null,
                  primary key (canonical_user_id, week_start)
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
                  origin_turn_trace_id integer references turn_traces(id),
                  reason_code text,
                  resolution text,
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
            self._ensure_column(
                conn, "mood_state", "emotional_charge", "integer not null default 0"
            )
            self._ensure_column(conn, "mood_state", "boundary_level", "integer not null default 0")
            self._ensure_column(
                conn, "mood_state", "perceived_respect", "integer not null default 50"
            )
            self._ensure_column(
                conn, "mood_state", "perceived_reliability", "integer not null default 50"
            )
            self._ensure_column(
                conn, "mood_state", "perceived_responsiveness", "integer not null default 50"
            )
            self._ensure_column(conn, "mood_state", "last_user_intent", "text")
            self._ensure_column(conn, "mood_state", "last_interaction_event", "text")
            self._ensure_column(conn, "mood_state", "reply_style_hint", "text")
            self._ensure_column(
                conn, "mood_state", "emotion_vector_json", "text not null default '{}'"
            )
            self._ensure_column(
                conn, "mood_state", "emotion_baseline_json", "text not null default '{}'"
            )
            self._ensure_column(
                conn, "mood_state", "emotion_affinity_json", "text not null default '{}'"
            )
            self._ensure_column(
                conn, "mood_state", "last_emotion_impact_json", "text not null default '{}'"
            )
            self._ensure_column(conn, "mood_state", "last_emotion_source", "text")
            self._ensure_column(conn, "mood_state", "has_unread", "integer not null default 0")
            self._ensure_column(conn, "proactive_events", "trigger_type", "text")
            self._ensure_column(conn, "life_runtime", "user_event_effect", "text")
            self._ensure_column(conn, "life_runtime", "user_event_effect_until", "text")
            self._ensure_column(conn, "life_runtime", "base_attention_demand", "integer")
            self._ensure_column(
                conn, "life_runtime", "user_event_attention_delta", "integer not null default 0"
            )
            self._ensure_column(conn, "life_runtime", "state_effect", "text")
            self._ensure_column(conn, "life_runtime_events", "shared_at", "text")
            self._ensure_column(conn, "social_tasks", "origin_turn_trace_id", "integer")
            self._ensure_column(conn, "social_tasks", "reason_code", "text")
            self._ensure_column(conn, "social_tasks", "resolution", "text")
            self._ensure_column(conn, "model_usage_events", "world_id", "text not null default ''")
            self._ensure_column(conn, "model_usage_events", "turn_id", "text not null default ''")
            self._ensure_column(conn, "model_usage_events", "action_id", "text not null default ''")
            self._ensure_column(conn, "model_usage_events", "cadence", "text not null default ''")
            self._ensure_column(conn, "model_usage_events", "attempt", "integer not null default 1")
            self._ensure_column(
                conn, "model_usage_events", "pricing_version", "text not null default ''"
            )
            self._ensure_column(
                conn, "model_usage_events", "estimated_cost_usd", "real not null default 0"
            )
            self._init_world_schema(conn)
            conn.execute(
                "update life_runtime set base_attention_demand = attention_demand where base_attention_demand is null"
            )

    def _init_world_schema(self, conn: sqlite3.Connection) -> None:
        """Install the append-only world ledger and rebuildable read models."""
        conn.executescript(
            """
            create table if not exists worlds (
              world_id text primary key,
              revision integer not null default 0,
              logical_at text not null,
              seed_hash text not null,
              created_at text not null
            );
            create table if not exists world_events (
              event_id text primary key,
              world_id text not null references worlds(world_id),
              revision integer not null,
              event_type text not null,
              schema_version integer not null,
              logical_at text not null,
              observed_at text not null,
              actor_json text not null,
              source text not null,
              correlation_id text not null,
              causation_id text,
              idempotency_key text,
              payload_json text not null,
              payload_hash text not null,
              unique (world_id, revision),
              unique (world_id, idempotency_key)
            );
            create index if not exists idx_world_events_stream on world_events(world_id, revision);
            create table if not exists world_snapshots (
              world_id text not null references worlds(world_id),
              revision integer not null,
              state_json text not null,
              state_hash text not null,
              created_at text not null,
              primary key (world_id, revision)
            );
            create table if not exists world_projection_checkpoints (
              world_id text not null references worlds(world_id),
              projection_name text not null,
              applied_revision integer not null,
              state_hash text not null,
              updated_at text not null,
              primary key (world_id, projection_name)
            );
            create table if not exists world_current_state (
              world_id text primary key references worlds(world_id),
              applied_revision integer not null,
              state_json text not null,
              state_hash text not null,
              updated_at text not null
            );
            create table if not exists world_entities (
              world_id text not null references worlds(world_id),
              entity_id text not null,
              kind text not null,
              name text not null,
              state_json text not null,
              primary key (world_id, entity_id)
            );
            create table if not exists world_agenda (
              world_id text not null references worlds(world_id),
              activity_id text not null,
              entity_id text not null,
              starts_at text not null,
              ends_at text not null,
              status text not null,
              state_json text not null,
              primary key (world_id, activity_id)
            );
            create table if not exists world_actions (
              world_id text not null references worlds(world_id),
              action_id text not null,
              kind text not null,
              status text not null,
              expires_at text,
              state_json text not null,
              primary key (world_id, action_id)
            );
            create table if not exists world_experiences (
              world_id text not null references worlds(world_id),
              experience_id text not null,
              action_id text,
              content text not null,
              state_json text not null,
              primary key (world_id, experience_id)
            );
            create table if not exists world_fact_index (
              world_id text not null references worlds(world_id),
              fact_id text not null,
              state_json text not null,
              primary key (world_id, fact_id)
            );
            create table if not exists world_command_receipts (
              world_id text not null references worlds(world_id),
              idempotency_key text not null,
              revision integer not null,
              event_ids_json text not null,
              created_at text not null,
              primary key (world_id, idempotency_key)
            );
            create table if not exists world_projection_hashes (
              world_id text not null references worlds(world_id),
              projection_name text not null,
              applied_revision integer not null,
              state_hash text not null,
              checked_at text not null,
              primary key (world_id, projection_name, applied_revision)
            );
            """
        )

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
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

    def map_account(
        self, platform: Platform, platform_user_id: str, canonical_user_id: str
    ) -> None:
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
        self._assert_legacy_behavior_write_allowed("save_incoming")
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
        self._assert_legacy_behavior_write_allowed("queue_outgoing")
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

    def queue_outgoing_with_turn_trace(
        self,
        canonical_user_id: str,
        platform: Platform,
        text: str,
        *,
        kind: str,
        appraisal: str,
        expression_policy: str,
        allowed_facts: list[str],
        short_lived_constraint: str | None,
        observable_reason: str,
        direction: str = "incoming_reply",
    ) -> tuple[int, int]:
        self._assert_legacy_behavior_write_allowed("queue_outgoing_with_turn_trace")
        """Create a proposed delivery and its audit record in one transaction."""
        now = utc_now().isoformat()
        with self.connect() as conn:
            delivery = conn.execute(
                """
                insert into outbox_messages (
                  canonical_user_id, platform, text, kind, status, created_at
                ) values (?, ?, ?, ?, 'planned', ?)
                """,
                (canonical_user_id, platform, text, kind, now),
            )
            delivery_id = int(delivery.lastrowid)
            trace = conn.execute(
                """
                insert into turn_traces (
                  canonical_user_id, direction, appraisal, expression_policy,
                  allowed_facts_json, short_lived_constraint, observable_reason,
                  output_text, delivery_id, status, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)
                """,
                (
                    canonical_user_id,
                    direction,
                    appraisal,
                    expression_policy,
                    json.dumps(allowed_facts, ensure_ascii=False),
                    short_lived_constraint,
                    observable_reason,
                    text,
                    delivery_id,
                    now,
                    now,
                ),
            )
        return delivery_id, int(trace.lastrowid)

    def mark_outgoing_delivered(self, delivery_id: int) -> sqlite3.Row | None:
        self._assert_legacy_behavior_write_allowed("mark_outgoing_delivered")
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
        self._assert_legacy_behavior_write_allowed("mark_outgoing_failed")
        with self.connect() as conn:
            conn.execute(
                """
                update outbox_messages
                set status = 'failed', failed_at = ?, failure_reason = ?
                where id = ? and status = 'planned'
                """,
                (utc_now().isoformat(), reason[:500], delivery_id),
            )

    def resolve_outgoing_and_turn_trace(
        self,
        delivery_id: int,
        trace_id: int | None,
        *,
        delivered: bool,
        failure_reason: str | None = None,
    ) -> sqlite3.Row | None:
        self._assert_legacy_behavior_write_allowed("resolve_outgoing_and_turn_trace")
        """Set one outbox delivery and its audit trace in one transaction."""
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, platform, text, kind, status from outbox_messages where id = ?",
                (delivery_id,),
            ).fetchone()
            if not row or row["status"] != "planned":
                return row
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
                    """
                    update outbox_messages set status = 'failed', failed_at = ?, failure_reason = ?
                    where id = ?
                    """,
                    (now, (failure_reason or "delivery failed")[:500], delivery_id),
                )
            if trace_id is not None:
                conn.execute(
                    """
                    update turn_traces set status = ?, failure_reason = ?, updated_at = ?
                    where id = ? and delivery_id = ? and status = 'planned'
                    """,
                    (
                        "delivered" if delivered else "failed",
                        None if delivered else (failure_reason or "delivery failed")[:500],
                        now,
                        trace_id,
                        delivery_id,
                    ),
                )
            return row

    def mark_outgoing_and_turn_trace_unknown(
        self,
        delivery_id: int,
        trace_id: int | None,
        *,
        reason: str,
    ) -> sqlite3.Row | None:
        """Close a legacy outbound attempt whose platform result is unknowable.

        The pre-World outbox has no segment or external-observation ledger, but
        it can still distinguish a missing durable QQ receipt from a definite
        delivery failure.  Keeping that distinction prevents recovery code
        from either fabricating delivery or overwriting an ambiguous send as a
        failed one.
        """
        self._assert_legacy_behavior_write_allowed("mark_outgoing_and_turn_trace_unknown")
        now = utc_now().isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, platform, text, kind, status from outbox_messages where id = ?",
                (delivery_id,),
            ).fetchone()
            if not row or row["status"] != "planned":
                return row
            conn.execute(
                """
                update outbox_messages
                set status = 'unknown', failed_at = ?, failure_reason = ?
                where id = ? and status = 'planned'
                """,
                (now, reason[:500], delivery_id),
            )
            if trace_id is not None:
                conn.execute(
                    """
                    update turn_traces set status = 'unknown', failure_reason = ?, updated_at = ?
                    where id = ? and delivery_id = ? and status = 'planned'
                    """,
                    (reason[:500], now, trace_id, delivery_id),
                )
            return row

    def outbox_message(self, delivery_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                """
                select canonical_user_id, platform, text, kind, status, delivered_at, failed_at, failure_reason
                from outbox_messages where id = ?
                """,
                (delivery_id,),
            ).fetchone()

    def create_turn_trace(
        self,
        canonical_user_id: str,
        *,
        appraisal: str,
        expression_policy: str,
        allowed_facts: list[str],
        short_lived_constraint: str | None,
        observable_reason: str,
        output_text: str,
        delivery_id: int | None,
        direction: str = "incoming_reply",
        status: str = "planned",
    ) -> int:
        self._assert_legacy_behavior_write_allowed("create_turn_trace")
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into turn_traces (
                  canonical_user_id, direction, appraisal, expression_policy,
                  allowed_facts_json, short_lived_constraint, observable_reason,
                  output_text, delivery_id, status, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    direction,
                    appraisal,
                    expression_policy,
                    json.dumps(allowed_facts, ensure_ascii=False),
                    short_lived_constraint,
                    observable_reason,
                    output_text,
                    delivery_id,
                    status,
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def complete_turn_trace(
        self, trace_id: int, *, delivered: bool, failure_reason: str | None = None
    ) -> None:
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                update turn_traces
                set status = ?, failure_reason = ?, updated_at = ?
                where id = ? and status = 'planned'
                """,
                (
                    "delivered" if delivered else "failed",
                    failure_reason[:500] if failure_reason else None,
                    now,
                    trace_id,
                ),
            )

    def resolve_turn_trace(self, trace_id: int, *, status: str, reason: str | None = None) -> None:
        """Close a non-delivery trace such as a deferred attention decision."""
        with self.connect() as conn:
            conn.execute(
                """
                update turn_traces set status = ?, failure_reason = ?, updated_at = ?
                where id = ? and status in ('planned', 'deferred', 'observed')
                """,
                (status, reason[:500] if reason else None, utc_now().isoformat(), trace_id),
            )

    def recent_turn_traces(self, canonical_user_id: str, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, direction, appraisal, expression_policy, allowed_facts_json,
                       short_lived_constraint, observable_reason, output_text, delivery_id,
                       status, failure_reason, created_at, updated_at
                from turn_traces where canonical_user_id = ? order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def turn_trace_id_for_delivery(self, delivery_id: int) -> int | None:
        with self.connect() as conn:
            row = conn.execute(
                "select id from turn_traces where delivery_id = ? order by id desc limit 1",
                (delivery_id,),
            ).fetchone()
        return int(row["id"]) if row else None

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
        self._assert_legacy_behavior_write_allowed("save_mood_state")
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
            last_notification_at=(
                datetime.fromisoformat(row["last_notification_at"])
                if row["last_notification_at"]
                else None
            ),
            last_read_at=datetime.fromisoformat(row["last_read_at"])
            if row["last_read_at"]
            else None,
            user_event_effect=row["user_event_effect"],
            user_event_effect_until=(
                datetime.fromisoformat(row["user_event_effect_until"])
                if row["user_event_effect_until"]
                else None
            ),
            user_event_attention_delta=row["user_event_attention_delta"],
            state_effect=row["state_effect"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_life_runtime(self, canonical_user_id: str, runtime: LifeRuntimeState) -> None:
        self._assert_legacy_behavior_write_allowed("save_life_runtime")
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
                    canonical_user_id,
                    runtime.activity,
                    runtime.activity_kind,
                    runtime.base_attention_demand,
                    runtime.attention_demand,
                    runtime.interruptible,
                    runtime.started_at.isoformat(),
                    runtime.ends_at.isoformat(),
                    runtime.phone_attention,
                    runtime.notification_count,
                    runtime.last_notification_at.isoformat()
                    if runtime.last_notification_at
                    else None,
                    runtime.last_read_at.isoformat() if runtime.last_read_at else None,
                    runtime.user_event_effect,
                    runtime.user_event_effect_until.isoformat()
                    if runtime.user_event_effect_until
                    else None,
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
        self._assert_legacy_behavior_write_allowed("record_life_event")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into life_runtime_events (
                  canonical_user_id, kind, content, started_at, ends_at, status, source, shared_at, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_user_id,
                    kind,
                    content,
                    started_at.isoformat(),
                    ends_at.isoformat(),
                    status,
                    source,
                    shared_at.isoformat() if shared_at else None,
                    utc_now().isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def complete_active_life_events(
        self, canonical_user_id: str, *, completed_at: datetime
    ) -> None:
        self._assert_legacy_behavior_write_allowed("complete_active_life_events")
        with self.connect() as conn:
            conn.execute(
                """
                update life_runtime_events
                set status = 'completed', ends_at = ?
                where canonical_user_id = ? and status = 'active'
                """,
                (completed_at.isoformat(), canonical_user_id),
            )

    def correct_active_life_event(self, canonical_user_id: str, *, kind: str, content: str) -> None:
        """Correct an invalid generated runtime block before it becomes history."""
        self._assert_legacy_behavior_write_allowed("correct_active_life_event")
        with self.connect() as conn:
            conn.execute(
                """
                update life_runtime_events set kind = ?, content = ?
                where id = (
                  select id from life_runtime_events
                  where canonical_user_id = ? and status = 'active' and source = 'life_runtime'
                  order by id desc limit 1
                )
                """,
                (kind, content, canonical_user_id),
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

    def upcoming_life_plan_items(
        self, canonical_user_id: str, *, now: datetime, limit: int = 5
    ) -> list[sqlite3.Row]:
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

    def life_plan_items_between(
        self, canonical_user_id: str, *, starts_at: datetime, ends_at: datetime
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, local_date, slot, kind, activity, attention_demand, interruptible,
                       starts_at, ends_at, status, adjustment_note
                from life_day_plan_items
                where canonical_user_id = ? and starts_at < ? and ends_at > ?
                order by starts_at asc
                """,
                (
                    canonical_user_id,
                    ends_at.astimezone(UTC).isoformat(),
                    starts_at.astimezone(UTC).isoformat(),
                ),
            ).fetchall()
        return list(rows)

    def life_events_between(
        self, canonical_user_id: str, *, starts_at: datetime, ends_at: datetime
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source, shared_at
                from life_runtime_events
                where canonical_user_id = ? and started_at < ? and ends_at >= ?
                order by started_at asc, id asc
                """,
                (
                    canonical_user_id,
                    ends_at.astimezone(UTC).isoformat(),
                    starts_at.astimezone(UTC).isoformat(),
                ),
            ).fetchall()
        return list(rows)

    def create_calendar_event(
        self,
        canonical_user_id: str,
        *,
        title: str,
        event_type: str,
        starts_at: datetime,
        ends_at: datetime,
        importance: int = 50,
        source: str = "calendar",
        details: str | None = None,
        memory_note: str | None = None,
        status: str = "planned",
    ) -> int:
        self._assert_legacy_behavior_write_allowed("create_calendar_event")
        if status not in {"planned", "active", "completed", "cancelled", "postponed"}:
            raise ValueError(f"unsupported calendar status: {status}")
        if ends_at <= starts_at:
            raise ValueError("calendar event must end after it starts")
        now = utc_now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into calendar_events (
                  canonical_user_id,title,event_type,starts_at,ends_at,status,importance,source,
                  details,memory_note,share_state,changed_reason,created_at,updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'private', null, ?, ?)
                """,
                (
                    canonical_user_id,
                    title,
                    event_type,
                    starts_at.astimezone(UTC).isoformat(),
                    ends_at.astimezone(UTC).isoformat(),
                    status,
                    max(0, min(100, importance)),
                    source,
                    details,
                    memory_note,
                    now,
                    now,
                ),
            )
        event_id = int(cursor.lastrowid)
        self._record_calendar_transition(
            event_id, from_status=None, to_status=status, reason="创建事件"
        )
        self.sync_calendar_event_memory(canonical_user_id, event_id)
        return event_id

    def calendar_events_between(
        self, canonical_user_id: str, *, starts_at: datetime, ends_at: datetime
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select event.id,event.title,event.event_type,event.starts_at,event.ends_at,event.status,
                       event.importance,event.source,event.details,event.memory_note,event.share_state,
                       event.changed_reason, memory.id as memory_id, memory.kind as memory_kind,
                       memory.content as memory_content
                from calendar_events event
                left join calendar_event_memories link on link.calendar_event_id = event.id
                left join memories memory on memory.id = link.memory_id
                where event.canonical_user_id = ? and event.starts_at < ? and event.ends_at > ?
                order by event.starts_at asc, event.importance desc
                """,
                (
                    canonical_user_id,
                    ends_at.astimezone(UTC).isoformat(),
                    starts_at.astimezone(UTC).isoformat(),
                ),
            ).fetchall()
        return list(rows)

    def calendar_event_by_source(self, canonical_user_id: str, source: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "select * from calendar_events where canonical_user_id = ? and source = ? limit 1",
                (canonical_user_id, source),
            ).fetchone()

    def save_calendar_week(
        self, canonical_user_id: str, *, week_start: str, theme: str, summary: str, source: str
    ) -> None:
        self._assert_legacy_behavior_write_allowed("save_calendar_week")
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                insert or ignore into calendar_weeks (canonical_user_id,week_start,theme,summary,status,source,created_at,updated_at)
                values (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (canonical_user_id, week_start, theme, summary, source, now, now),
            )

    def calendar_week(self, canonical_user_id: str, week_start: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "select * from calendar_weeks where canonical_user_id = ? and week_start = ?",
                (canonical_user_id, week_start),
            ).fetchone()

    def update_calendar_event_status(
        self, event_id: int, *, status: str, changed_reason: str | None = None
    ) -> None:
        self._assert_legacy_behavior_write_allowed("update_calendar_event_status")
        allowed = {
            "planned": {"active", "completed", "cancelled", "postponed"},
            "active": {"completed", "cancelled", "postponed"},
            "postponed": {"active", "completed", "cancelled"},
            "completed": set(),
            "cancelled": set(),
        }
        with self.connect() as conn:
            row = conn.execute(
                "select canonical_user_id, status from calendar_events where id = ?", (event_id,)
            ).fetchone()
            if not row:
                raise ValueError(f"calendar event {event_id} does not exist")
            previous = str(row["status"])
            if status not in allowed.get(previous, set()):
                raise ValueError(f"invalid calendar transition: {previous} -> {status}")
            if status == "cancelled" and not (changed_reason or "").strip():
                raise ValueError("a cancelled calendar event requires a reason")
            conn.execute(
                "update calendar_events set status = ?, changed_reason = ?, updated_at = ? where id = ?",
                (status, changed_reason, utc_now().isoformat(), event_id),
            )
        self._record_calendar_transition(
            event_id, from_status=previous, to_status=status, reason=changed_reason
        )
        self.sync_calendar_event_memory(str(row["canonical_user_id"]), event_id)

    def _record_calendar_transition(
        self, event_id: int, *, from_status: str | None, to_status: str, reason: str | None
    ) -> None:
        self._assert_legacy_behavior_write_allowed("record_calendar_transition")
        with self.connect() as conn:
            conn.execute(
                "insert into calendar_event_history (calendar_event_id,from_status,to_status,reason,changed_at) values (?, ?, ?, ?, ?)",
                (event_id, from_status, to_status, reason, utc_now().isoformat()),
            )

    def calendar_event_history(self, event_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    "select from_status,to_status,reason,changed_at from calendar_event_history where calendar_event_id = ? order by id asc",
                    (event_id,),
                ).fetchall()
            )

    def postpone_next_calendar_event(
        self,
        canonical_user_id: str,
        *,
        now: datetime,
        event_types: tuple[str, ...],
        reason: str,
        delay: timedelta = timedelta(days=1),
    ) -> int | None:
        """Move one compatible unfinished named plan and preserve why it changed."""
        self._assert_legacy_behavior_write_allowed("postpone_next_calendar_event")
        placeholders = ", ".join("?" for _ in event_types)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select id, status, starts_at, ends_at from calendar_events
                where canonical_user_id = ? and status in ('planned', 'active')
                  and ends_at > ? and event_type in ({placeholders})
                order by starts_at asc limit 1
                """,
                (canonical_user_id, now.astimezone(UTC).isoformat(), *event_types),
            ).fetchone()
            if not row:
                return None
            event_id = int(row["id"])
            starts_at = datetime.fromisoformat(str(row["starts_at"]))
            ends_at = datetime.fromisoformat(str(row["ends_at"]))
            shift = delay if starts_at >= now else now - starts_at + delay
            conn.execute(
                "update calendar_events set starts_at = ?, ends_at = ?, status = 'postponed', changed_reason = ?, updated_at = ? where id = ?",
                (
                    (starts_at + shift).isoformat(),
                    (ends_at + shift).isoformat(),
                    reason,
                    utc_now().isoformat(),
                    event_id,
                ),
            )
        self._record_calendar_transition(
            event_id, from_status=str(row["status"]), to_status="postponed", reason=reason
        )
        self.sync_calendar_event_memory(canonical_user_id, event_id)
        return event_id

    def cancel_next_calendar_event(
        self,
        canonical_user_id: str,
        *,
        now: datetime,
        event_types: tuple[str, ...],
        reason: str,
    ) -> int | None:
        """Cancel one compatible unfinished named plan without erasing its record."""
        placeholders = ", ".join("?" for _ in event_types)
        with self.connect() as conn:
            row = conn.execute(
                f"""
                select id from calendar_events
                where canonical_user_id = ? and status in ('planned', 'active', 'postponed')
                  and ends_at > ? and event_type in ({placeholders})
                order by starts_at asc limit 1
                """,
                (canonical_user_id, now.astimezone(UTC).isoformat(), *event_types),
            ).fetchone()
        if not row:
            return None
        event_id = int(row["id"])
        self.update_calendar_event_status(event_id, status="cancelled", changed_reason=reason)
        return event_id

    def sync_calendar_event_memory(self, canonical_user_id: str, event_id: int) -> None:
        """Give every calendar event one stable, queryable memory record."""
        self._assert_legacy_behavior_write_allowed("sync_calendar_event_memory")
        with self.connect() as conn:
            event = conn.execute(
                "select * from calendar_events where id = ? and canonical_user_id = ?",
                (event_id, canonical_user_id),
            ).fetchone()
            if not event:
                return
            status_label = {
                "planned": "计划中",
                "active": "进行中",
                "completed": "已发生",
                "cancelled": "已取消",
                "postponed": "已推迟",
            }.get(str(event["status"]), str(event["status"]))
            content = f"{event['title']}（{status_label}）"
            detail = event["memory_note"] or event["details"]
            if detail:
                content += f"：{detail}"
            if event["changed_reason"]:
                content += f"；变更原因：{event['changed_reason']}"
            source = f"calendar:event:{event_id}"
            existing = conn.execute(
                "select id from memories where canonical_user_id = ? and kind = 'calendar_event' and source = ?",
                (canonical_user_id, source),
            ).fetchone()
            now = utc_now().isoformat()
            if existing:
                memory_id = int(existing["id"])
                conn.execute(
                    "update memories set content = ?, confidence = 1.0, updated_at = ? where id = ?",
                    (content, now, memory_id),
                )
            else:
                cursor = conn.execute(
                    "insert into memories (canonical_user_id,kind,content,source,confidence,created_at,updated_at) values (?, 'calendar_event', ?, ?, 1.0, ?, ?)",
                    (canonical_user_id, content, source, now, now),
                )
                memory_id = int(cursor.lastrowid)
            conn.execute(
                "insert into calendar_event_memories (calendar_event_id,memory_id,linked_at) values (?, ?, ?) on conflict(calendar_event_id) do update set memory_id=excluded.memory_id, linked_at=excluded.linked_at",
                (event_id, memory_id, now),
            )

    def delete_calendar_events_by_source_prefix(self, canonical_user_id: str, prefix: str) -> None:
        self._assert_legacy_behavior_write_allowed("delete_calendar_events_by_source_prefix")
        with self.connect() as conn:
            conn.execute(
                "delete from calendar_events where canonical_user_id = ? and source like ?",
                (canonical_user_id, f"{prefix}%"),
            )

    def cancel_elapsed_calendar_plans(self, canonical_user_id: str, *, now: datetime) -> None:
        self._assert_legacy_behavior_write_allowed("cancel_elapsed_calendar_plans")
        with self.connect() as conn:
            rows = conn.execute(
                "select id from calendar_events where canonical_user_id = ? and status in ('planned', 'active', 'postponed') and ends_at < ?",
                (canonical_user_id, now.astimezone(UTC).isoformat()),
            ).fetchall()
        reason = "该计划时段已过去，但没有完成凭据；不能把它伪装成已发生。"
        for row in rows:
            self.update_calendar_event_status(
                int(row["id"]), status="cancelled", changed_reason=reason
            )

    def normalize_single_day_weekly_plans(self, canonical_user_id: str) -> list[int]:
        """Repair prototype weekly plans that accidentally occupied the next day."""
        self._assert_legacy_behavior_write_allowed("normalize_single_day_weekly_plans")
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, starts_at, ends_at from calendar_events
                where canonical_user_id = ? and source like 'calendar:weekly:%'
                  and event_type <> 'trip' and status in ('planned', 'active')
                """,
                (canonical_user_id,),
            ).fetchall()
            for row in rows:
                starts_at = datetime.fromisoformat(str(row["starts_at"]))
                ends_at = datetime.fromisoformat(str(row["ends_at"]))
                if ends_at <= starts_at + timedelta(hours=6):
                    continue
                conn.execute(
                    "update calendar_events set ends_at = ?, updated_at = ? where id = ?",
                    (
                        (starts_at + timedelta(hours=2)).isoformat(),
                        utc_now().isoformat(),
                        int(row["id"]),
                    ),
                )
        return [int(row["id"]) for row in rows]

    def unshared_private_life_events(
        self, canonical_user_id: str, limit: int = 4
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source
                from life_runtime_events
                where canonical_user_id = ? and kind = 'private_life_event'
                  and status = 'completed' and shared_at is null
                  and source like 'life_runtime:%'
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(rows)

    def trusted_private_life_event(
        self, canonical_user_id: str, event_id: int
    ) -> sqlite3.Row | None:
        """Return one completed event that the deterministic world runtime owns."""
        with self.connect() as conn:
            return conn.execute(
                """
                select id, kind, content, started_at, ends_at, status, source, shared_at
                from life_runtime_events
                where id = ? and canonical_user_id = ?
                  and kind = 'private_life_event' and status = 'completed'
                  and source like 'life_runtime:%'
                """,
                (event_id, canonical_user_id),
            ).fetchone()

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

    def save_life_day_plan(
        self, canonical_user_id: str, local_date: str, items: list[dict[str, object]]
    ) -> None:
        """Persist a private schedule. Planned entries are not lived facts."""
        self._assert_legacy_behavior_write_allowed("save_life_day_plan")
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
                        canonical_user_id,
                        local_date,
                        item["slot"],
                        item["kind"],
                        item["activity"],
                        item["attention_demand"],
                        int(bool(item["interruptible"])),
                        item["starts_at"],
                        item["ends_at"],
                        utc_now().isoformat(),
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

    def update_life_day_plan_status(
        self, canonical_user_id: str, *, before: datetime, status: str
    ) -> None:
        self._assert_legacy_behavior_write_allowed("update_life_day_plan_status")
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
        self._assert_legacy_behavior_write_allowed("activate_life_day_plan_item")
        with self.connect() as conn:
            conn.execute(
                "update life_day_plan_items set status = 'active' where id = ?", (item_id,)
            )

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
        self._assert_legacy_behavior_write_allowed("adjust_next_life_day_plan_item")
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
                    (
                        activity,
                        note,
                        max(0, min(100, row["attention_demand"] + attention_delta)),
                        row["id"],
                    ),
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
        origin_turn_trace_id: int | None = None,
        reason_code: str | None = None,
    ) -> int:
        self._assert_legacy_behavior_write_allowed("create_social_task")
        with self.connect() as conn:
            cursor = conn.execute(
                """
                insert into social_tasks (
                  canonical_user_id, kind, status, platform, platform_user_id, payload_json,
                  reason, origin_turn_trace_id, reason_code, due_at, expires_at, claimed_at, resolved_at, created_at
                ) values (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, null, null, ?)
                """,
                (
                    canonical_user_id,
                    kind,
                    platform,
                    platform_user_id,
                    json.dumps(payload, ensure_ascii=False),
                    reason,
                    origin_turn_trace_id,
                    reason_code,
                    due_at.astimezone(UTC).isoformat(),
                    expires_at.astimezone(UTC).isoformat(),
                    utc_now().isoformat(),
                ),
            )
        return int(cursor.lastrowid)

    def cancel_social_task(self, task_id: int, *, resolution: str = "cancelled") -> None:
        self._assert_legacy_behavior_write_allowed("cancel_social_task")
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'cancelled', resolution = ?, resolved_at = ?
                where id = ? and status in ('pending', 'claimed')
                """,
                (resolution, utc_now().isoformat(), task_id),
            )

    def resolve_social_task(self, task_id: int, *, resolution: str = "completed") -> None:
        self._assert_legacy_behavior_write_allowed("resolve_social_task")
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'resolved', resolution = ?, resolved_at = ?
                where id = ? and status in ('pending', 'claimed')
                """,
                (resolution, utc_now().isoformat(), task_id),
            )

    def social_task_is_active(self, task_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "select 1 from social_tasks where id = ? and status in ('pending', 'claimed')",
                (task_id,),
            ).fetchone()
        return row is not None

    def cancel_active_social_tasks(self, canonical_user_id: str, *, kind: str) -> None:
        self._assert_legacy_behavior_write_allowed("cancel_active_social_tasks")
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'cancelled', resolution = 'cancelled_by_new_turn', resolved_at = ?
                where canonical_user_id = ? and kind = ? and status in ('pending', 'claimed')
                """,
                (utc_now().isoformat(), canonical_user_id, kind),
            )

    def claim_due_social_tasks(
        self, *, kind: str, now: datetime, limit: int = 8
    ) -> list[sqlite3.Row]:
        """Claim due work; stale claims are retried after a daemon crash."""
        self._assert_legacy_behavior_write_allowed("claim_due_social_tasks")
        now = now.astimezone(UTC)
        stale_before = now - timedelta(minutes=10)
        with self.connect() as conn:
            conn.execute(
                """
                update social_tasks set status = 'expired', resolution = 'expired', resolved_at = ?
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
                select id, kind, status, reason, origin_turn_trace_id, reason_code, resolution,
                       due_at, expires_at, claimed_at, resolved_at, created_at
                from social_tasks where canonical_user_id = ?
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def has_recent_unread_deferral(
        self,
        canonical_user_id: str,
        *,
        since: datetime,
    ) -> bool:
        """Return whether this conversation was deliberately left unread recently.

        Cancelled tasks count: a later message can merge into an existing task,
        but that should not repeatedly recreate the same missed-notification beat.
        """
        with self.connect() as conn:
            row = conn.execute(
                """
                select 1 from social_tasks
                where canonical_user_id = ?
                  and kind = 'reply_later'
                  and reason like 'unread_during_%'
                  and created_at >= ?
                limit 1
                """,
                (canonical_user_id, since.astimezone(UTC).isoformat()),
            ).fetchone()
        return row is not None

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
                update social_tasks set status = 'expired', resolution = 'expired', resolved_at = ?
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
        self._assert_legacy_behavior_write_allowed("defer_social_task")
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
        self._assert_legacy_behavior_write_allowed("record_interaction_event")
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
        self._assert_legacy_behavior_write_allowed("upsert_memory")
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

    def record_fact_observation(
        self,
        canonical_user_id: str,
        *,
        subject: str,
        predicate: str,
        value: str,
        source: str,
        confidence: float,
        fact_key: str | None = None,
    ) -> None:
        self._assert_legacy_behavior_write_allowed("record_fact_observation")
        """Append a sourced fact and supersede only an explicitly conflicting key.

        The normal memories table is a retrieval index. This ledger is the
        narrower authority for concrete assertions: it never accepts model
        flavor, and it preserves replaced values for point-in-time inspection.
        """
        now = utc_now().isoformat()
        with self.connect() as conn:
            if fact_key:
                conn.execute(
                    """
                    update fact_ledger
                    set status = 'superseded', valid_to = ?, updated_at = ?
                    where canonical_user_id = ? and subject = ? and fact_key = ?
                      and status = 'active' and value <> ?
                    """,
                    (now, now, canonical_user_id, subject, fact_key, value),
                )
            duplicate = conn.execute(
                """
                select id from fact_ledger
                where canonical_user_id = ? and subject = ? and predicate = ?
                  and value = ? and status = 'active'
                order by id desc limit 1
                """,
                (canonical_user_id, subject, predicate, value),
            ).fetchone()
            if duplicate:
                conn.execute(
                    """
                    update fact_ledger set confidence = max(confidence, ?), updated_at = ?
                    where id = ?
                    """,
                    (confidence, now, int(duplicate["id"])),
                )
                return
            conn.execute(
                """
                insert into fact_ledger (
                  canonical_user_id, subject, predicate, fact_key, value, status,
                  confidence, source, valid_from, valid_to, created_at, updated_at
                ) values (?, ?, ?, ?, ?, 'active', ?, ?, ?, null, ?, ?)
                """,
                (
                    canonical_user_id,
                    subject,
                    predicate,
                    fact_key,
                    value,
                    confidence,
                    source,
                    now,
                    now,
                    now,
                ),
            )

    def active_fact_lines(
        self, canonical_user_id: str, *, subject: str = "user", limit: int = 8
    ) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select predicate, value, source
                from fact_ledger
                where canonical_user_id = ? and subject = ? and status = 'active'
                order by updated_at desc, id desc limit ?
                """,
                (canonical_user_id, subject, limit),
            ).fetchall()
        return [f"- [{row['predicate']}; 来源=用户明确表达] {row['value']}" for row in rows]

    def fact_history(self, canonical_user_id: str, *, limit: int = 30) -> list[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                select subject, predicate, fact_key, value, status, confidence, source,
                       valid_from, valid_to, created_at, updated_at
                from fact_ledger where canonical_user_id = ?
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        return list(rows)

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
        self._assert_legacy_behavior_write_allowed("delete_memory")
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

    def unanswered_outgoing_streak(self, canonical_user_id: str, *, limit: int = 24) -> int:
        """Count delivered chat bubbles after the newest user turn.

        This is deliberately based on chat history rather than proactive event
        rows: life sharing, afterthoughts, and ordinary proactive messages must
        all consume the same "she has the floor" budget.
        """
        with self.connect() as conn:
            rows = conn.execute(
                """
                select direction from messages
                where canonical_user_id = ?
                order by id desc limit ?
                """,
                (canonical_user_id, limit),
            ).fetchall()
        streak = 0
        for row in rows:
            if row["direction"] == "in":
                break
            if row["direction"] == "out":
                streak += 1
        return streak

    def latest_outgoing_at(self, canonical_user_id: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                select sent_at from messages
                where canonical_user_id = ? and direction = 'out'
                order by id desc limit 1
                """,
                (canonical_user_id,),
            ).fetchone()
        return str(row["sent_at"]) if row else None

    def unanswered_outgoing_started_at(self, canonical_user_id: str) -> str | None:
        """Return when the current unanswered companion turn actually began."""
        with self.connect() as conn:
            row = conn.execute(
                """
                select sent_at from messages
                where canonical_user_id = ? and direction = 'out'
                  and id > coalesce((
                    select max(id) from messages
                    where canonical_user_id = ? and direction = 'in'
                  ), 0)
                order by id asc limit 1
                """,
                (canonical_user_id, canonical_user_id),
            ).fetchone()
        return str(row["sent_at"]) if row else None

    def record_proactive_delivery(self, canonical_user_id: str, platform: str) -> None:
        self._assert_legacy_behavior_write_allowed("record_proactive_delivery")
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

    def record_model_usage(
        self,
        *,
        purpose: str,
        model: str,
        status: str,
        latency_ms: int,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        reasoning_tokens: int = 0,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
        total_tokens: int = 0,
        error: str = "",
        world_id: str = "",
        turn_id: str = "",
        action_id: str = "",
        cadence: str = "",
        attempt: int = 1,
    ) -> None:
        from companion_daemon.usage_metrics import estimate_model_cost_usd

        estimated_cost_usd, pricing_version = estimate_model_cost_usd(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
        with self.connect() as conn:
            conn.execute(
                """
                insert into model_usage_events (
                  purpose, model, status, latency_ms, prompt_tokens,
                  completion_tokens, reasoning_tokens, cache_hit_tokens,
                  cache_miss_tokens, total_tokens, error, world_id, turn_id,
                  action_id, cadence, attempt, pricing_version,
                  estimated_cost_usd, created_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    purpose[:80],
                    model[:120],
                    status[:40],
                    max(0, int(latency_ms)),
                    max(0, int(prompt_tokens)),
                    max(0, int(completion_tokens)),
                    max(0, int(reasoning_tokens)),
                    max(0, int(cache_hit_tokens)),
                    max(0, int(cache_miss_tokens)),
                    max(0, int(total_tokens)),
                    error[:500],
                    world_id[:120],
                    turn_id[:120],
                    action_id[:120],
                    cadence[:20],
                    max(1, int(attempt)),
                    pricing_version[:80],
                    max(0.0, float(estimated_cost_usd)),
                    utc_now().isoformat(),
                ),
            )

    def model_usage_summary(self, window: str, now: datetime) -> dict[str, dict[str, int]]:
        if window == "day":
            prefix = now.date().isoformat()
        elif window == "month":
            prefix = now.strftime("%Y-%m")
        else:
            raise ValueError(f"Unsupported usage window: {window}")
        with self.connect() as conn:
            rows = conn.execute(
                """
                select purpose, count(*) as calls,
                       sum(prompt_tokens) as prompt_tokens,
                       sum(completion_tokens) as completion_tokens,
                       sum(reasoning_tokens) as reasoning_tokens,
                       sum(cache_hit_tokens) as cache_hit_tokens,
                       sum(cache_miss_tokens) as cache_miss_tokens,
                       sum(total_tokens) as total_tokens,
                       sum(latency_ms) as latency_ms
                from model_usage_events
                where substr(created_at, 1, ?) = ?
                group by purpose
                order by purpose
                """,
                (len(prefix), prefix),
            ).fetchall()
        keys = (
            "calls",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cache_hit_tokens",
            "cache_miss_tokens",
            "total_tokens",
            "latency_ms",
        )
        summary = {str(row["purpose"]): {key: int(row[key] or 0) for key in keys} for row in rows}
        summary["_total"] = {key: sum(item[key] for item in summary.values()) for key in keys}
        return summary

    def model_usage_report(
        self,
        window: str,
        now: datetime,
        *,
        cny_per_usd: float = 7.2,
    ) -> dict[str, object]:
        """Aggregate persisted provider usage by purpose/cadence/model and turn."""
        from companion_daemon.usage_metrics import aggregate_usage_rows

        if window == "day":
            prefix = now.date().isoformat()
        elif window == "month":
            prefix = now.strftime("%Y-%m")
        else:
            raise ValueError(f"Unsupported usage window: {window}")
        with self.connect() as conn:
            rows = conn.execute(
                """
                select * from model_usage_events
                where substr(created_at, 1, ?) = ?
                order by id
                """,
                (len(prefix), prefix),
            ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        turns: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            group_key = "|".join((str(row["purpose"]), str(row["cadence"]), str(row["model"])))
            groups.setdefault(group_key, []).append(row)
            turn_id = str(row["turn_id"] or "")
            if turn_id:
                turns.setdefault(turn_id, []).append(row)
        return {
            "window": window,
            "currency_rate": {"cny_per_usd": max(0.0, float(cny_per_usd))},
            "total": aggregate_usage_rows(rows, cny_per_usd=cny_per_usd),
            "groups": {
                key: aggregate_usage_rows(items, cny_per_usd=cny_per_usd)
                for key, items in groups.items()
            },
            "turns": {
                key: aggregate_usage_rows(items, cny_per_usd=cny_per_usd)
                for key, items in turns.items()
            },
        }

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
        self._assert_legacy_behavior_write_allowed("save_proactive_event")
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
