"""Durable, provider-local QQ ingress normalization and coalescing.

The module is deliberately upstream of :class:`WorldV2PlatformHost`.  It owns
no World fact and makes no conversational decision: a versioned matrix turns
provider envelope categories into a bounded merge window, and the resulting
batch is submitted as one source-bound ``Observation``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Literal, Mapping, Protocol, Sequence


ContentShape = Literal["text", "attachment", "mixed", "reaction", "sticker", "control"]
ContinuitySignal = Literal[
    "unknown", "complete_thought", "possible_continuation", "long_narration", "new_interjection"
]
ControlKind = Literal["typing_started", "typing_stopped"]


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def onebot_attachment_ref(segment_type: str, data: Mapping[str, object]) -> str:
    """The one opaque, URL-free identity of an inbound OneBot attachment.

    Both ingress normalization and the deployment-local attachment archive
    must derive byte-identical refs from the same provider segment, so this
    helper is the single authority for that derivation.  The digest covers
    the provider's segment payload (including its transient URL) but the ref
    itself never exposes it.
    """

    return (
        f"qq-attachment:{segment_type}:sha256:"
        f"{_digest({'type': segment_type, 'data': dict(data)})}"
    )


@dataclass(frozen=True, slots=True)
class QQIngressPolicyRow:
    content_shape: ContentShape
    continuity_signal: ContinuitySignal
    window_ms: int
    batch_mode: Literal["ordered_multimodal", "metadata_only"]
    max_fragments: int = 8


class QQIngressPolicyCatalog:
    """Machine-readable categories; rows guide batching, never reply content."""

    version = "world-v2-qq-ingress-matrix.1"

    def __init__(self, rows: Sequence[QQIngressPolicyRow] | None = None) -> None:
        if rows is None:
            windows = {
                "unknown": 600,
                "complete_thought": 450,
                "possible_continuation": 750,
                "long_narration": 800,
                "new_interjection": 400,
            }
            rows = tuple(
                QQIngressPolicyRow(shape, signal, window, "ordered_multimodal")
                for shape in ("text", "attachment", "mixed", "reaction", "sticker")
                for signal, window in windows.items()
            ) + tuple(
                QQIngressPolicyRow("control", signal, window, "metadata_only")
                for signal, window in windows.items()
            )
        self._rows = {(row.content_shape, row.continuity_signal): row for row in rows}
        if len(self._rows) != 30:
            raise ValueError("QQ ingress catalog must cover every shape/signal coordinate")
        for row in self._rows.values():
            if not 400 <= row.window_ms <= 800 or not 1 <= row.max_fragments <= 16:
                raise ValueError("QQ ingress row exceeds the frozen latency/batch bounds")
        self.digest = _digest(self.manifest())

    def lookup(
        self, *, content_shape: ContentShape, continuity_signal: ContinuitySignal
    ) -> QQIngressPolicyRow:
        try:
            return self._rows[(content_shape, continuity_signal)]
        except KeyError as exc:
            raise ValueError("unknown QQ ingress matrix coordinate") from exc

    def manifest(self) -> dict[str, object]:
        return {
            "version": self.version,
            "orphan_control_expiry_ms": 30_000,
            "axes": {
                "content_shape": [
                    "text", "attachment", "mixed", "reaction", "sticker", "control"
                ],
                "continuity_signal": [
                    "unknown", "complete_thought", "possible_continuation",
                    "long_narration", "new_interjection",
                ],
            },
            "rows": [
                {
                    "content_shape": row.content_shape,
                    "continuity_signal": row.continuity_signal,
                    "window_ms": row.window_ms,
                    "batch_mode": row.batch_mode,
                    "max_fragments": row.max_fragments,
                }
                for row in sorted(
                    self._rows.values(), key=lambda item: (item.content_shape, item.continuity_signal)
                )
            ],
        }


@dataclass(frozen=True, slots=True)
class QQIngressFragment:
    source_event_id: str
    recipient_id: str
    observed_at: datetime
    content_shape: ContentShape
    continuity_signal: ContinuitySignal = "unknown"
    text: str | None = None
    attachment_refs: tuple[str, ...] = ()
    reply_ref: str | None = None
    reaction_refs: tuple[str, ...] = ()
    sticker_ref: str | None = None
    control_kind: ControlKind | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.source_event_id) <= 256 or not 1 <= len(self.recipient_id) <= 128:
            raise ValueError("QQ ingress fragment requires source event and recipient ids")
        _aware("observed_at", self.observed_at)
        if self.text is not None and len(self.text) > 12_000:
            raise ValueError("QQ ingress text exceeds the retained Observation bound")
        if len(self.attachment_refs) > 16 or len(self.reaction_refs) > 16:
            raise ValueError("QQ ingress fragment contains too many provider refs")
        if self.text == "" or any(not item for item in (*self.attachment_refs, *self.reaction_refs)):
            raise ValueError("QQ ingress content refs must not be empty")
        if any(len(item) > 512 for item in (*self.attachment_refs, *self.reaction_refs)):
            raise ValueError("QQ ingress provider ref exceeds the bounded envelope")
        if self.reply_ref is not None and len(self.reply_ref) > 512:
            raise ValueError("QQ ingress reply ref exceeds the bounded envelope")
        if self.content_shape == "text" and (self.text is None or self.attachment_refs):
            raise ValueError("text shape requires only text content")
        if self.content_shape == "attachment" and (not self.attachment_refs or self.text):
            raise ValueError("attachment shape requires only attachment refs")
        if self.content_shape == "mixed" and (not self.text or not self.attachment_refs):
            raise ValueError("mixed shape requires text and attachments")
        if self.content_shape == "reaction" and not self.reaction_refs:
            raise ValueError("reaction shape requires reaction refs")
        if self.content_shape == "sticker" and not self.sticker_ref:
            raise ValueError("sticker shape requires a sticker ref")
        if self.content_shape == "control" and self.control_kind is None:
            raise ValueError("control shape requires a control kind")
        if self.content_shape != "control" and self.control_kind is not None:
            raise ValueError("control kind is only valid for control fragments")
        object.__setattr__(self, "attachment_refs", tuple(self.attachment_refs))
        object.__setattr__(self, "reaction_refs", tuple(self.reaction_refs))

    @property
    def payload_hash(self) -> str:
        # Provider retries may omit an event timestamp; arrival/observation time
        # is therefore retained as evidence but is not part of source-content
        # identity. The first accepted envelope remains the durable timestamp.
        payload = self.canonical_payload()
        payload.pop("observed_at")
        return _digest(payload)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "source_event_id": self.source_event_id,
            "recipient_id": self.recipient_id,
            "observed_at": self.observed_at.isoformat(),
            "content_shape": self.content_shape,
            "continuity_signal": self.continuity_signal,
            "text": self.text,
            "attachment_refs": list(self.attachment_refs),
            "reply_ref": self.reply_ref,
            "reaction_refs": list(self.reaction_refs),
            "sticker_ref": self.sticker_ref,
            "control_kind": self.control_kind,
        }


@dataclass(frozen=True, slots=True)
class QQIngressSubmission:
    source_event_id: str
    due_at: datetime
    state: Literal["pending", "claimed", "committed"]
    batch_id: str | None = None
    outcome_status: str | None = None
    action_id: str | None = None


@dataclass(frozen=True, slots=True)
class QQIngressBatch:
    batch_id: str
    recipient_id: str
    source_event_ids: tuple[str, ...]
    platform_message_id: str
    observed_at: datetime
    text: str | None
    attachment_refs: tuple[str, ...]
    metadata: Mapping[str, object]


class QQIngressStore(Protocol):
    def submit(self, fragment: QQIngressFragment, *, received_at: datetime) -> QQIngressSubmission: ...
    def claim_due(self, *, now: datetime) -> QQIngressBatch | None: ...
    def complete(self, *, batch_id: str, outcome_status: str, action_id: str | None) -> None: ...
    def submission(self, source_event_id: str) -> QQIngressSubmission | None: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class _StoredFragment:
    fragment: QQIngressFragment
    received_at: datetime
    due_at: datetime
    state: Literal["pending", "claimed", "committed"] = "pending"
    batch_id: str | None = None


@dataclass(slots=True)
class _StoredBatch:
    batch: QQIngressBatch
    outcome_status: str | None = None
    action_id: str | None = None


def _build_batch(
    records: Sequence[_StoredFragment], *, catalog: QQIngressPolicyCatalog
) -> QQIngressBatch:
    ordered = sorted(records, key=lambda item: (item.fragment.observed_at, item.fragment.source_event_id))
    source_ids = tuple(item.fragment.source_event_id for item in ordered)
    recipient_id = ordered[0].fragment.recipient_id
    identity = _digest({"recipient_id": recipient_id, "sources": source_ids, "policy": catalog.digest})
    batch_id = f"qq-ingress-batch:{identity}"
    texts = tuple(item.fragment.text for item in ordered if item.fragment.text)
    attachment_refs = tuple(
        ref for item in ordered for ref in item.fragment.attachment_refs
    )
    metadata = {
        "schema_version": "world-v2-qq-coalescing.2",
        "policy_version": catalog.version,
        "policy_digest": catalog.digest,
        "batch_id": batch_id,
        "source_event_ids": list(source_ids),
        # A reaction applies to one concrete provider message, not the
        # synthetic coalesced batch identity.  The most recent fragment is the
        # natural visible anchor; this is an ingress fact, never a model ID.
        "reaction_target_message_id": source_ids[-1],
        "source_payload_hashes": [item.fragment.payload_hash for item in ordered],
        "content_shapes": [item.fragment.content_shape for item in ordered],
        "continuity_signals": [item.fragment.continuity_signal for item in ordered],
        "reply_refs": [item.fragment.reply_ref for item in ordered if item.fragment.reply_ref],
        "reaction_refs": [ref for item in ordered for ref in item.fragment.reaction_refs],
        "sticker_refs": [
            item.fragment.sticker_ref for item in ordered if item.fragment.sticker_ref
        ],
        "control_events": [
            {
                "kind": item.fragment.control_kind,
                "source_event_id": item.fragment.source_event_id,
            }
            for item in ordered
            if item.fragment.control_kind is not None
        ],
        "ordered_fragment_count": len(ordered),
        "window_opened_at": min(item.received_at for item in ordered).isoformat(),
        "window_closed_at": min(item.due_at for item in ordered).isoformat(),
    }
    return QQIngressBatch(
        batch_id=batch_id,
        recipient_id=recipient_id,
        source_event_ids=source_ids,
        platform_message_id=f"qq-coalesced:{identity}",
        observed_at=max(item.fragment.observed_at for item in ordered),
        text="\n".join(texts) if texts else None,
        attachment_refs=attachment_refs,
        metadata=metadata,
    )


def _freeze_processing_started_at(batch: QQIngressBatch, *, now: datetime) -> QQIngressBatch:
    """Persist the first claim instant so retrying one batch is byte-stable."""

    metadata = dict(batch.metadata)
    metadata.setdefault("processing_started_at", now.isoformat())
    return replace(batch, metadata=metadata)


def _select_batch_records(
    candidates: Sequence[_StoredFragment],
    *,
    anchor_source_event_id: str,
    limit: int,
) -> list[_StoredFragment]:
    """Select a bounded batch while guaranteeing its non-control anchor."""

    ordered = sorted(candidates, key=lambda item: (item.received_at, item.fragment.source_event_id))
    anchor = next(
        item for item in ordered if item.fragment.source_event_id == anchor_source_event_id
    )
    before = [item for item in ordered if item is not anchor and item.received_at <= anchor.received_at]
    after = [item for item in ordered if item is not anchor and item.received_at > anchor.received_at]
    priority = [anchor, *reversed(before), *after]
    selected: list[_StoredFragment] = []
    text_characters = 0
    attachment_count = 0
    for item in priority:
        text_size = len(item.fragment.text or "") + (1 if text_characters else 0)
        next_attachments = attachment_count + len(item.fragment.attachment_refs)
        if text_characters + text_size > 12_000 or next_attachments > 16:
            continue
        selected.append(item)
        text_characters += text_size
        attachment_count = next_attachments
        if len(selected) == limit:
            break
    selected.sort(key=lambda item: (item.fragment.observed_at, item.fragment.source_event_id))
    return selected


class MemoryQQIngressStore:
    def __init__(self, *, catalog: QQIngressPolicyCatalog | None = None) -> None:
        self.catalog = catalog or QQIngressPolicyCatalog()
        self._fragments: dict[str, _StoredFragment] = {}
        self._batches: dict[str, _StoredBatch] = {}

    def submit(self, fragment: QQIngressFragment, *, received_at: datetime) -> QQIngressSubmission:
        _aware("received_at", received_at)
        existing = self._fragments.get(fragment.source_event_id)
        if existing is not None:
            if existing.fragment.payload_hash != fragment.payload_hash:
                raise ValueError("QQ source event id conflicts with its immutable payload")
            return self._submission(existing)
        row = self.catalog.lookup(
            content_shape=fragment.content_shape, continuity_signal=fragment.continuity_signal
        )
        record = _StoredFragment(
            fragment=fragment,
            received_at=received_at,
            due_at=received_at + timedelta(milliseconds=row.window_ms),
        )
        self._fragments[fragment.source_event_id] = record
        return self._submission(record)

    def claim_due(self, *, now: datetime) -> QQIngressBatch | None:
        _aware("now", now)
        self._expire_orphan_controls(now)
        claimed = sorted(
            (
                item for item in self._batches.values()
                if item.outcome_status is None
            ),
            key=lambda item: item.batch.batch_id,
        )
        if claimed:
            return claimed[0].batch
        pending = sorted(
            (
                item
                for item in self._fragments.values()
                if item.state == "pending" and item.fragment.content_shape != "control"
            ),
            key=lambda item: (item.received_at, item.fragment.source_event_id),
        )
        if not pending or pending[0].due_at > now:
            return None
        anchor = pending[0]
        limit = self.catalog.lookup(
            content_shape=anchor.fragment.content_shape,
            continuity_signal=anchor.fragment.continuity_signal,
        ).max_fragments
        # The anchor's matrix window only decides when the batch becomes due.
        # Membership extends to the claim instant: while an earlier turn is
        # still occupying the world, an ongoing exchange keeps piling up here,
        # and one continuous conversation must join one turn instead of
        # queueing one full turn per message.
        selected = [
            item for item in self._fragments.values()
            if item.state == "pending"
            if item.fragment.recipient_id == anchor.fragment.recipient_id
            and item.received_at >= anchor.received_at - timedelta(milliseconds=800)
            and item.received_at <= now
        ]
        selected = _select_batch_records(
            selected,
            anchor_source_event_id=anchor.fragment.source_event_id,
            limit=limit,
        )
        batch = _freeze_processing_started_at(
            _build_batch(selected, catalog=self.catalog), now=now
        )
        self._batches[batch.batch_id] = _StoredBatch(batch=batch)
        for item in selected:
            item.state = "claimed"
            item.batch_id = batch.batch_id
        return batch

    def _expire_orphan_controls(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=30)
        expired = sorted(
            (
                item
                for item in self._fragments.values()
                if item.state == "pending"
                and item.fragment.content_shape == "control"
                and item.received_at <= cutoff
            ),
            key=lambda item: (item.received_at, item.fragment.source_event_id),
        )
        while expired:
            anchor = expired[0]
            selected = [
                item
                for item in expired
                if item.fragment.recipient_id == anchor.fragment.recipient_id
            ][:8]
            batch = _build_batch(selected, catalog=self.catalog)
            self._batches[batch.batch_id] = _StoredBatch(
                batch=batch, outcome_status="observed_only"
            )
            for item in selected:
                item.state = "committed"
                item.batch_id = batch.batch_id
            expired = [item for item in expired if item not in selected]

    def complete(self, *, batch_id: str, outcome_status: str, action_id: str | None) -> None:
        stored = self._batches.get(batch_id)
        if stored is None:
            raise ValueError("unknown QQ ingress batch")
        if stored.outcome_status is not None:
            if (stored.outcome_status, stored.action_id) != (outcome_status, action_id):
                raise ValueError("QQ ingress batch outcome is immutable")
            return
        stored.outcome_status = outcome_status
        stored.action_id = action_id
        for source_id in stored.batch.source_event_ids:
            self._fragments[source_id].state = "committed"

    def submission(self, source_event_id: str) -> QQIngressSubmission | None:
        record = self._fragments.get(source_event_id)
        return self._submission(record) if record is not None else None

    def _submission(self, record: _StoredFragment) -> QQIngressSubmission:
        batch = self._batches.get(record.batch_id or "")
        return QQIngressSubmission(
            source_event_id=record.fragment.source_event_id,
            due_at=record.due_at,
            state=record.state,
            batch_id=record.batch_id,
            outcome_status=batch.outcome_status if batch else None,
            action_id=batch.action_id if batch else None,
        )

    def close(self) -> None:
        return None


class SQLiteQQIngressStore:
    """SQLite mirror of the memory store; claimed batches survive restarts."""

    def __init__(self, path: Path, *, catalog: QQIngressPolicyCatalog | None = None) -> None:
        self.catalog = catalog or QQIngressPolicyCatalog()
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS world_v2_qq_ingress_fragments (
              source_event_id TEXT PRIMARY KEY, recipient_id TEXT NOT NULL,
              payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
              received_at TEXT NOT NULL, due_at TEXT NOT NULL,
              state TEXT NOT NULL, batch_id TEXT
            );
            CREATE TABLE IF NOT EXISTS world_v2_qq_ingress_batches (
              batch_id TEXT PRIMARY KEY, batch_json TEXT NOT NULL,
              outcome_status TEXT, action_id TEXT
            );
            CREATE INDEX IF NOT EXISTS world_v2_qq_ingress_pending
              ON world_v2_qq_ingress_fragments(state, received_at, source_event_id);
            """
        )

    def submit(self, fragment: QQIngressFragment, *, received_at: datetime) -> QQIngressSubmission:
        _aware("received_at", received_at)
        row_policy = self.catalog.lookup(
            content_shape=fragment.content_shape, continuity_signal=fragment.continuity_signal
        )
        due_at = received_at + timedelta(milliseconds=row_policy.window_ms)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                existing = self._connection.execute(
                    "SELECT payload_hash FROM world_v2_qq_ingress_fragments WHERE source_event_id=?",
                    (fragment.source_event_id,),
                ).fetchone()
                if existing is not None and existing["payload_hash"] != fragment.payload_hash:
                    raise ValueError("QQ source event id conflicts with its immutable payload")
                if existing is None:
                    self._connection.execute(
                        "INSERT INTO world_v2_qq_ingress_fragments "
                        "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL)",
                        (
                            fragment.source_event_id, fragment.recipient_id,
                            fragment.payload_hash, _canonical(fragment.canonical_payload()),
                            received_at.isoformat(), due_at.isoformat(),
                        ),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            result = self.submission(fragment.source_event_id)
            assert result is not None
            return result

    def claim_due(self, *, now: datetime) -> QQIngressBatch | None:
        _aware("now", now)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._expire_orphan_controls_locked(now)
                claimed = self._connection.execute(
                    "SELECT batch_json FROM world_v2_qq_ingress_batches "
                    "WHERE outcome_status IS NULL ORDER BY batch_id LIMIT 1"
                ).fetchone()
                if claimed is not None:
                    self._connection.execute("COMMIT")
                    return self._decode_batch(json.loads(claimed["batch_json"]))
                anchor = self._connection.execute(
                    "SELECT * FROM world_v2_qq_ingress_fragments WHERE state='pending' "
                    "AND json_extract(payload_json, '$.content_shape')!='control' "
                    "ORDER BY received_at, source_event_id LIMIT 1"
                ).fetchone()
                if anchor is None or datetime.fromisoformat(anchor["due_at"]) > now:
                    self._connection.execute("COMMIT")
                    return None
                anchor_fragment = self._decode_fragment(json.loads(anchor["payload_json"]))
                limit = self.catalog.lookup(
                    content_shape=anchor_fragment.content_shape,
                    continuity_signal=anchor_fragment.continuity_signal,
                ).max_fragments
                # Mirror the memory store: membership extends to the claim
                # instant so a continuous exchange that accumulated behind a
                # slow earlier turn joins one batch, not one turn per message.
                rows = self._connection.execute(
                    "SELECT * FROM world_v2_qq_ingress_fragments "
                    "WHERE state='pending' AND recipient_id=? AND received_at>=? AND received_at<=? "
                    "ORDER BY received_at, source_event_id LIMIT ?",
                    (
                        anchor["recipient_id"],
                        (
                            datetime.fromisoformat(anchor["received_at"])
                            - timedelta(milliseconds=800)
                        ).isoformat(),
                        now.isoformat(),
                        limit,
                    ),
                ).fetchall()
                if all(row["source_event_id"] != anchor["source_event_id"] for row in rows):
                    rows = [*rows, anchor]
                records = [
                    _StoredFragment(
                        fragment=self._decode_fragment(json.loads(row["payload_json"])),
                        received_at=datetime.fromisoformat(row["received_at"]),
                        due_at=datetime.fromisoformat(row["due_at"]),
                    )
                    for row in rows
                ]
                records = _select_batch_records(
                    records,
                    anchor_source_event_id=anchor_fragment.source_event_id,
                    limit=limit,
                )
                batch = _freeze_processing_started_at(
                    _build_batch(records, catalog=self.catalog), now=now
                )
                self._connection.execute(
                    "INSERT INTO world_v2_qq_ingress_batches VALUES (?, ?, NULL, NULL)",
                    (batch.batch_id, _canonical(self._encode_batch(batch))),
                )
                self._connection.executemany(
                    "UPDATE world_v2_qq_ingress_fragments SET state='claimed', batch_id=? "
                    "WHERE source_event_id=?",
                    ((batch.batch_id, source_id) for source_id in batch.source_event_ids),
                )
                self._connection.execute("COMMIT")
                return batch
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def _expire_orphan_controls_locked(self, now: datetime) -> None:
        cutoff = (now - timedelta(seconds=30)).isoformat()
        while True:
            anchor = self._connection.execute(
                "SELECT * FROM world_v2_qq_ingress_fragments WHERE state='pending' "
                "AND json_extract(payload_json, '$.content_shape')='control' "
                "AND received_at<=? ORDER BY received_at, source_event_id LIMIT 1",
                (cutoff,),
            ).fetchone()
            if anchor is None:
                return
            rows = self._connection.execute(
                "SELECT * FROM world_v2_qq_ingress_fragments WHERE state='pending' "
                "AND recipient_id=? "
                "AND json_extract(payload_json, '$.content_shape')='control' "
                "AND received_at<=? ORDER BY received_at, source_event_id LIMIT 8",
                (anchor["recipient_id"], cutoff),
            ).fetchall()
            records = [
                _StoredFragment(
                    fragment=self._decode_fragment(json.loads(row["payload_json"])),
                    received_at=datetime.fromisoformat(row["received_at"]),
                    due_at=datetime.fromisoformat(row["due_at"]),
                )
                for row in rows
            ]
            batch = _build_batch(records, catalog=self.catalog)
            self._connection.execute(
                "INSERT INTO world_v2_qq_ingress_batches VALUES (?, ?, 'observed_only', NULL)",
                (batch.batch_id, _canonical(self._encode_batch(batch))),
            )
            self._connection.executemany(
                "UPDATE world_v2_qq_ingress_fragments SET state='committed', batch_id=? "
                "WHERE source_event_id=?",
                ((batch.batch_id, source_id) for source_id in batch.source_event_ids),
            )

    def complete(self, *, batch_id: str, outcome_status: str, action_id: str | None) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT outcome_status, action_id FROM world_v2_qq_ingress_batches "
                    "WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
                if row is None:
                    raise ValueError("unknown QQ ingress batch")
                if row["outcome_status"] is not None:
                    if (row["outcome_status"], row["action_id"]) != (
                        outcome_status, action_id
                    ):
                        raise ValueError("QQ ingress batch outcome is immutable")
                    self._connection.execute("COMMIT")
                    return
                self._connection.execute(
                    "UPDATE world_v2_qq_ingress_batches SET outcome_status=?, action_id=? WHERE batch_id=?",
                    (outcome_status, action_id, batch_id),
                )
                self._connection.execute(
                    "UPDATE world_v2_qq_ingress_fragments SET state='committed' WHERE batch_id=?",
                    (batch_id,),
                )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def submission(self, source_event_id: str) -> QQIngressSubmission | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT f.*, b.outcome_status, b.action_id FROM world_v2_qq_ingress_fragments f "
                "LEFT JOIN world_v2_qq_ingress_batches b ON b.batch_id=f.batch_id "
                "WHERE f.source_event_id=?",
                (source_event_id,),
            ).fetchone()
        if row is None:
            return None
        return QQIngressSubmission(
            source_event_id=source_event_id,
            due_at=datetime.fromisoformat(row["due_at"]),
            state=row["state"], batch_id=row["batch_id"],
            outcome_status=row["outcome_status"], action_id=row["action_id"],
        )

    @staticmethod
    def _decode_fragment(payload: Mapping[str, object]) -> QQIngressFragment:
        return QQIngressFragment(
            source_event_id=str(payload["source_event_id"]), recipient_id=str(payload["recipient_id"]),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            content_shape=str(payload["content_shape"]),  # type: ignore[arg-type]
            continuity_signal=str(payload["continuity_signal"]),  # type: ignore[arg-type]
            text=payload.get("text") if isinstance(payload.get("text"), str) else None,
            attachment_refs=tuple(str(item) for item in payload.get("attachment_refs", [])),
            reply_ref=payload.get("reply_ref") if isinstance(payload.get("reply_ref"), str) else None,
            reaction_refs=tuple(str(item) for item in payload.get("reaction_refs", [])),
            sticker_ref=payload.get("sticker_ref") if isinstance(payload.get("sticker_ref"), str) else None,
            control_kind=payload.get("control_kind") if isinstance(payload.get("control_kind"), str) else None,  # type: ignore[arg-type]
        )

    @staticmethod
    def _encode_batch(batch: QQIngressBatch) -> dict[str, object]:
        return {
            "batch_id": batch.batch_id, "recipient_id": batch.recipient_id,
            "source_event_ids": list(batch.source_event_ids),
            "platform_message_id": batch.platform_message_id,
            "observed_at": batch.observed_at.isoformat(), "text": batch.text,
            "attachment_refs": list(batch.attachment_refs), "metadata": dict(batch.metadata),
        }

    @staticmethod
    def _decode_batch(payload: Mapping[str, object]) -> QQIngressBatch:
        return QQIngressBatch(
            batch_id=str(payload["batch_id"]), recipient_id=str(payload["recipient_id"]),
            source_event_ids=tuple(str(item) for item in payload["source_event_ids"]),  # type: ignore[union-attr]
            platform_message_id=str(payload["platform_message_id"]),
            observed_at=datetime.fromisoformat(str(payload["observed_at"])),
            text=payload.get("text") if isinstance(payload.get("text"), str) else None,
            attachment_refs=tuple(str(item) for item in payload["attachment_refs"]),  # type: ignore[union-attr]
            metadata=dict(payload["metadata"]),  # type: ignore[arg-type]
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def normalize_onebot_qq_ingress(
    event: Mapping[str, object], *, continuity_signal: ContinuitySignal = "unknown"
) -> QQIngressFragment | None:
    """Normalize supported private OneBot message/control shapes without prose rules."""

    post_type = event.get("post_type")
    user_id = str(event.get("user_id") or "")
    source_id = str(event.get("message_id") or event.get("event_id") or "")
    timestamp = event.get("time")
    observed_at = (
        datetime.fromtimestamp(float(timestamp), tz=UTC)
        if isinstance(timestamp, (int, float))
        else datetime.now(UTC)
    )
    notice_type = str(event.get("notice_type") or "")
    sub_type = str(event.get("sub_type") or "")
    is_input_status = post_type in {"notice", "meta_event"} and (
        notice_type in {"typing", "input_status"}
        # NapCat reports the peer's private-chat input state as
        # ``notice.notify.input_status`` with status_text/event_type fields
        # and no message id.
        or (notice_type == "notify" and sub_type == "input_status")
    )
    if is_input_status:
        if not user_id or event.get("group_id"):
            return None
        raw_status = str(
            event.get("status") or event.get("status_text") or sub_type or ""
        )
        stopped = raw_status in {"stop", "stopped", "0"} or str(
            event.get("event_type") or ""
        ) in {"0", "2"}
        control_kind: ControlKind = "typing_stopped" if stopped else "typing_started"
        if not source_id:
            # Input-status notices carry no message id; synthesize a stable
            # provider-local identity so retries of the same pulse dedup.
            source_id = "qq-input-status:" + _digest(
                {
                    "user_id": user_id,
                    "time": event.get("time"),
                    "event_type": event.get("event_type"),
                    "status_text": event.get("status_text"),
                }
            )
        return QQIngressFragment(
            source_event_id=source_id, recipient_id=user_id, observed_at=observed_at,
            content_shape="control", continuity_signal=continuity_signal,
            control_kind=control_kind,
        )
    if post_type != "message" or event.get("message_type") == "group" or not user_id or not source_id:
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        segments = []
    texts: list[str] = []
    attachments: list[str] = []
    reactions: list[str] = []
    sticker_ref: str | None = None
    reply_ref: str | None = None
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        segment_type = str(segment.get("type") or "")
        data = segment.get("data")
        data = data if isinstance(data, Mapping) else {}
        if segment_type == "text" and str(data.get("text") or "").strip():
            texts.append(str(data["text"]).strip())
        elif segment_type in {"image", "record", "video", "file"}:
            attachments.append(onebot_attachment_ref(segment_type, data))
        elif segment_type == "face" and data.get("id") is not None:
            reactions.append(f"qq-face:{str(data['id'])[:80]}")
        elif segment_type in {"mface", "market_face"}:
            sticker_ref = f"qq-sticker:sha256:{_digest({'type': segment_type, 'data': data})}"
        elif segment_type == "reply" and data.get("id") is not None:
            reply_ref = f"qq-message:{str(data['id'])[:160]}"
    if not texts and not segments and str(event.get("raw_message") or "").strip():
        texts.append(str(event["raw_message"]).strip())
    text = "".join(texts) or None
    if text and attachments:
        shape: ContentShape = "mixed"
    elif text:
        shape = "text"
    elif attachments:
        shape = "attachment"
    elif sticker_ref:
        shape = "sticker"
    elif reactions:
        shape = "reaction"
    else:
        return None
    return QQIngressFragment(
        source_event_id=source_id, recipient_id=user_id, observed_at=observed_at,
        content_shape=shape, continuity_signal=continuity_signal, text=text,
        attachment_refs=tuple(attachments), reply_ref=reply_ref,
        reaction_refs=tuple(reactions), sticker_ref=sticker_ref,
    )


__all__ = [
    "MemoryQQIngressStore", "QQIngressBatch", "QQIngressFragment", "QQIngressPolicyCatalog",
    "QQIngressPolicyRow", "QQIngressStore", "QQIngressSubmission", "SQLiteQQIngressStore",
    "normalize_onebot_qq_ingress", "onebot_attachment_ref",
]
