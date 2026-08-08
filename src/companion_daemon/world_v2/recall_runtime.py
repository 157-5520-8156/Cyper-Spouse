"""Character-owned, read-only recall over a cursor-pinned disposable index."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import logging
import math
import secrets
import threading
import time
from typing import Literal

from pydantic import Field

from .recall_corpus import RecallCorpusCompiler, RecallCorpusSources
from .recall_audit import (
    CharacterRecallRequest,
    PrefetchPresentationAudit,
    RecallAuditHit,
    RecallAuditTrace,
    paired_recall_transition_hash,
)
from .recall_index import (
    InMemoryRecallIndex,
    RecallCursor,
    RecallEmbedding,
    RecallEmbeddingUnavailable,
    RecallIndex,
    RecallIndexSnapshot,
    RecallQuery,
    RecallResult,
)
from .schema_core import FrozenModel


logger = logging.getLogger(__name__)

_TRACE_AUTHORITY_KEY = secrets.token_bytes(32)


def install_trace_authority_key(key: bytes) -> None:
    """Pin the recall-trace HMAC authority key.

    Production keeps a fresh process-unique key (``secrets.token_bytes``),
    which is intentional: a trace seal must not be forgeable across daemon
    restarts.  Offline fixtures and the frozen scenario suite, however, must
    be byte-deterministic across separate processes, so they install one
    fixed key before composing the application.
    """

    global _TRACE_AUTHORITY_KEY
    if len(key) != 32:
        raise ValueError("recall trace authority key must be 32 bytes")
    _TRACE_AUTHORITY_KEY = key


_MAX_PINNED_RECALL_CONTEXTS = 16
# The first cognition call may wait this long for a local prefetch that is
# still searching.  A lexical search over the in-process index finishes in a
# few milliseconds; a semantic search additionally spends one query-embedding
# provider call (cached documents embed once). Production samples through the
# configured proxy are typically 310–420 ms warm, so a 300 ms bound made the
# semantic lane structurally dark despite healthy results. The bound remains
# below half a second so a pathological search cannot hold the visible reply
# hostage — a prefetch that misses the join stays available for the
# character-chosen pull.
PREFETCH_FIRST_PASS_JOIN_SECONDS = 0.45
_RecallContextKey = tuple[RecallCursor, str | None]
_RecallPrefetchKey = tuple[RecallCursor, str]


@dataclass(frozen=True, slots=True)
class PrefetchJobToken:
    """Generation identity for compare-and-remove cleanup."""

    expected_cursor: RecallCursor
    trigger_ref: str
    epoch: int


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class TrustedRecallTrace(FrozenModel):
    """Live-only seal proving a trace came from the installed recall module."""

    audit: RecallAuditTrace
    authority_seal: str = Field(pattern=r"^[0-9a-f]{64}$")


class PresentedPrefetchTrace(FrozenModel):
    """Live proof of one prefetch actually exposed to a role-model call."""

    phase: Literal[
        "initial",
        "recall_followup",
        "recovery_initial",
        "delegated_initial",
    ]
    model_call_id: str = Field(min_length=1, max_length=256)
    trace: TrustedRecallTrace

    def recorded(self) -> PrefetchPresentationAudit:
        return PrefetchPresentationAudit(
            phase=self.phase,
            model_call_id=self.model_call_id,
            trace=verify_trusted_recall_trace(self.trace),
        )


def append_presented_prefetch(
    presentations: tuple[PresentedPrefetchTrace, ...],
    *,
    phase: Literal[
        "initial",
        "recall_followup",
        "recovery_initial",
        "delegated_initial",
    ],
    model_call_id: str,
    trace: TrustedRecallTrace | None,
) -> tuple[PresentedPrefetchTrace, ...]:
    """Record each provider-visible presentation in model-call order."""

    if trace is None:
        return presentations
    audit = verify_trusted_recall_trace(trace)
    if audit.mode != "prefetch":
        raise ValueError("only a prefetch trace can be presented as automatic recall")
    if any(
        item.phase == phase
        and item.model_call_id == model_call_id
        and item.trace.audit.result_hash == audit.result_hash
        for item in presentations
    ):
        return presentations
    if len(presentations) >= 4:
        raise ValueError("prefetch presentation sequence exceeds its bounded budget")
    return (
        *presentations,
        PresentedPrefetchTrace(
            phase=phase,
            model_call_id=model_call_id,
            trace=trace,
        ),
    )


@dataclass(frozen=True, slots=True)
class _PinnedRecallContext:
    snapshot: RecallIndexSnapshot
    actor_ref: str
    subject_refs: tuple[str, ...]
    logical_time: datetime
    trigger_ref: str | None
    paired_predecessor_cursor: RecallCursor | None


class _PinnedBatchRecallEmbedding:
    """Replay one provider batch through the ordinary index/search contract."""

    def __init__(
        self,
        *,
        source: RecallEmbedding,
        texts: tuple[str, ...],
        vectors: tuple[tuple[float, ...], ...],
    ) -> None:
        if len(texts) != len(vectors):
            raise ValueError("semantic recall batch count is invalid")
        self.version = source.version
        self.dimensions = source.dimensions
        self.dense_match_threshold_bp = int(
            getattr(source, "dense_match_threshold_bp", 5_500)
        )
        self._vectors = dict(zip(texts, vectors, strict=True))

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        try:
            return tuple(self._vectors[text] for text in texts)
        except KeyError as exc:
            raise ValueError(
                "semantic recall batch was reused for unpinned text"
            ) from exc


@dataclass(frozen=True, slots=True)
class _PrefetchJob:
    future: Future[TrustedRecallTrace | None]
    cancelled: threading.Event
    thread: threading.Thread | None
    epoch: int
    local_fallback: TrustedRecallTrace

    def cancel(self) -> None:
        self.cancelled.set()
        self.future.cancel()


@dataclass(frozen=True, slots=True)
class _PrefetchHealth:
    epoch: int
    status: str
    failure_code: str | None
    hit_count: int = 0
    match_channels: tuple[str, ...] = ()
    fallback_channels: tuple[str, ...] = ()
    embedding_status: str = "unknown"


@dataclass(slots=True)
class _PrefetchDeliveryHealth:
    epoch: int = 0
    last_status: str = "unknown"
    first_pass_local_count: int = 0
    first_pass_semantic_count: int = 0
    late_semantic_ready_count: int = 0
    late_semantic_consumed_count: int = 0
    late_semantic_unpresented_count: int = 0


def _trace_seal(audit: RecallAuditTrace) -> str:
    return hmac.new(
        _TRACE_AUTHORITY_KEY,
        _canonical_json(audit.model_dump(mode="json")).encode(),
        hashlib.sha256,
    ).hexdigest()


def verify_trusted_recall_trace(trace: TrustedRecallTrace) -> RecallAuditTrace:
    if not hmac.compare_digest(trace.authority_seal, _trace_seal(trace.audit)):
        raise ValueError("recall trace was not issued by the trusted recall runtime")
    return trace.audit


async def perform_character_recall(
    coordinator: "RecallCoordinator",
    *,
    request: CharacterRecallRequest,
    accessibility_seed: str,
    expected_cursor: RecallCursor,
    trigger_ref: str,
    timeout_seconds: float,
) -> TrustedRecallTrace:
    """Run the shared read-only pull off the event loop under one deadline."""

    async with asyncio.timeout(timeout_seconds):
        return await asyncio.to_thread(
            coordinator.recall,
            request=request,
            accessibility_seed=accessibility_seed,
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
        )


async def perform_character_recall_with_prefetch(
    coordinator: "RecallCoordinator",
    *,
    request: CharacterRecallRequest,
    accessibility_seed: str,
    expected_cursor: RecallCursor,
    trigger_ref: str,
    timeout_seconds: float,
    prefetch_job_token: PrefetchJobToken | None = None,
) -> tuple[TrustedRecallTrace | None, TrustedRecallTrace]:
    """Join preparatory local attention and the character's chosen pull."""

    if prefetch_job_token is None:
        return (
            None,
            await perform_character_recall(
                coordinator,
                request=request,
                accessibility_seed=accessibility_seed,
                expected_cursor=expected_cursor,
                trigger_ref=trigger_ref,
                timeout_seconds=timeout_seconds,
            ),
        )
    prefetch_task = asyncio.create_task(
        coordinator.consume_scheduled_prefetch(
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
            job_token=prefetch_job_token,
        )
    )
    try:
        recall = await perform_character_recall(
            coordinator,
            request=request,
            accessibility_seed=accessibility_seed,
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
            timeout_seconds=timeout_seconds,
        )
        prefetch = await prefetch_task
    except BaseException:
        prefetch_task.cancel()
        try:
            await prefetch_task
        except BaseException:
            pass
        raise
    return prefetch, recall


class RecallCoordinator:
    """Share one deep recall module between Context prefetch and model pull."""

    def __init__(
        self,
        *,
        index: RecallIndex,
        corpus: RecallCorpusCompiler | None = None,
        semantic_embedding: RecallEmbedding | None = None,
    ) -> None:
        self._index = index
        self._corpus = corpus or RecallCorpusCompiler()
        self._semantic_embedding = semantic_embedding
        self._closed = False
        self._active_recall_guard = threading.Lock()
        self._active_recalls = 0
        self._active_recalls_drained = threading.Event()
        self._active_recalls_drained.set()
        # Context compilation can run concurrently for foreground ingress and
        # background cognition.  The disposable SQLite index has one mutable
        # head, while every turn must retain the immutable snapshot at its
        # exact ledger cursor.  Guard rebuild+snapshot+publication as one
        # operation and never make a later lookup depend on the global latest
        # cursor.
        self._context_guard = threading.RLock()
        self._context_key: _RecallContextKey | None = None
        self._contexts: OrderedDict[_RecallContextKey, _PinnedRecallContext] = OrderedDict()
        self._prefetch_slots = threading.BoundedSemaphore(value=4)
        self._prefetch_state_guard = threading.RLock()
        self._prefetch_futures: OrderedDict[_RecallPrefetchKey, _PrefetchJob] = OrderedDict()
        self._prefetch_join_attempted: set[_RecallPrefetchKey] = set()
        self._active_prefetch_consumers: set[
            tuple[_RecallPrefetchKey, int]
        ] = set()
        self._prefetch_trace_epochs: OrderedDict[tuple[str, str], int] = (
            OrderedDict()
        )
        self._presented_prefetch_results: OrderedDict[
            tuple[str, str],
            None,
        ] = OrderedDict()
        self._presented_local_prefetch_keys: OrderedDict[
            tuple[_RecallPrefetchKey, int],
            None,
        ] = OrderedDict()
        self._prefetch_late_ready_recorded: OrderedDict[
            tuple[_RecallPrefetchKey, int],
            None,
        ] = OrderedDict()
        # One provider attempt may time out after already consuming the
        # cursor-pinned automatic recall.  Keep that read-only trace available
        # to the technical recovery model at the same exact turn identity;
        # otherwise fallback changes not only provider but remembered
        # context.  Entries are bounded and evicted with their pinned Context.
        self._prefetch_replays: OrderedDict[
            _RecallPrefetchKey,
            TrustedRecallTrace,
        ] = OrderedDict()
        # Futures leave the keyed map when a caller consumes or abandons
        # them, but their non-cancellable provider threads may still be
        # running. Keep an independent shutdown registry until thread exit.
        self._prefetch_worker_guard = threading.Lock()
        self._prefetch_worker_threads: set[threading.Thread] = set()
        self._prefetch_epoch = 0
        self._prefetch_health_lock = threading.Lock()
        self._prefetch_health = _PrefetchHealth(
            epoch=0,
            status="unknown",
            failure_code=None,
        )
        self._prefetch_delivery_lock = threading.Lock()
        self._prefetch_delivery_health = _PrefetchDeliveryHealth()

    @classmethod
    def from_built_index(
        cls,
        *,
        index: RecallIndex,
        cursor: RecallCursor,
        actor_ref: str,
        subject_refs: tuple[str, ...],
        logical_time: datetime,
        semantic_embedding: RecallEmbedding | None = None,
        trigger_ref: str | None = None,
    ) -> "RecallCoordinator":
        coordinator = cls(
            index=index,
            semantic_embedding=semantic_embedding,
        )
        snapshot = index.snapshot()
        if snapshot.cursor != cursor:
            raise ValueError("built recall index cursor does not match its declared Context")
        coordinator._remember(
            key=(cursor, trigger_ref),
            context=_PinnedRecallContext(
                snapshot=snapshot,
                actor_ref=actor_ref,
                subject_refs=subject_refs,
                logical_time=logical_time,
                trigger_ref=trigger_ref,
                paired_predecessor_cursor=None,
            ),
        )
        return coordinator

    @property
    def cursor(self) -> RecallCursor | None:
        with self._context_guard:
            return self._context_key[0] if self._context_key is not None else None

    @property
    def is_closed(self) -> bool:
        with self._active_recall_guard:
            return self._closed

    def semantic_health(self) -> dict[str, object]:
        with self._prefetch_health_lock:
            health = self._prefetch_health
        with self._prefetch_delivery_lock:
            delivery = _PrefetchDeliveryHealth(
                epoch=self._prefetch_delivery_health.epoch,
                last_status=self._prefetch_delivery_health.last_status,
                first_pass_local_count=(self._prefetch_delivery_health.first_pass_local_count),
                first_pass_semantic_count=(
                    self._prefetch_delivery_health.first_pass_semantic_count
                ),
                late_semantic_ready_count=(
                    self._prefetch_delivery_health.late_semantic_ready_count
                ),
                late_semantic_consumed_count=(
                    self._prefetch_delivery_health.late_semantic_consumed_count
                ),
                late_semantic_unpresented_count=(
                    self._prefetch_delivery_health.late_semantic_unpresented_count
                ),
            )
        with self._context_guard:
            hot_context_ready = self._context_key is not None
        prefetch = {
            "last_prefetch_status": health.status,
            "last_prefetch_failure_code": health.failure_code,
            "last_prefetch_hit_count": health.hit_count,
            "last_prefetch_match_channels": list(health.match_channels),
            "last_prefetch_embedding_status": health.embedding_status,
            "last_prefetch_delivery_status": delivery.last_status,
            "prefetch_first_pass_local_count": delivery.first_pass_local_count,
            "prefetch_first_pass_semantic_count": delivery.first_pass_semantic_count,
            "prefetch_late_semantic_ready_count": delivery.late_semantic_ready_count,
            "prefetch_late_semantic_consumed_count": (delivery.late_semantic_consumed_count),
            "prefetch_late_semantic_unpresented_count": (delivery.late_semantic_unpresented_count),
            "turn_summary": {
                "hot_context": "ready" if hot_context_ready else "unavailable",
                "recall": health.status,
                "fallback_channels": list(health.fallback_channels),
                "hits": health.hit_count,
                # The recall module has no authority over the later character
                # deliberation or Action commit. Keep that outcome explicitly
                # outside this sub-projection instead of guessing from recall.
                "character_outcome": "reported_by_turn_application",
            },
        }
        if self._semantic_embedding is None:
            return {"enabled": False, **prefetch}
        snapshot = getattr(self._semantic_embedding, "health_snapshot", None)
        if callable(snapshot):
            return {**dict(snapshot()), **prefetch}
        return {
            "enabled": True,
            "embedding_version": self._semantic_embedding.version,
            **prefetch,
        }

    def is_available(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None = None,
    ) -> bool:
        if self._context_for(cursor, trigger_ref=trigger_ref) is not None:
            return True
        # A silent False here turns the whole recall channel dark for the
        # turn, so the mismatch must be observable rather than inferred.
        logger.warning(
            "recall unavailable for this turn: requested cursor=%s trigger=%s "
            "pinned_context_key=%s",
            cursor,
            trigger_ref,
            self._context_key,
        )
        return False

    def discard(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None = None,
    ) -> None:
        with self._context_guard:
            keys = tuple(
                key
                for key in self._contexts
                if key[0] == cursor and (trigger_ref is None or key[1] == trigger_ref)
            )
            for key in keys:
                self._contexts.pop(key, None)
            if self._context_key in keys:
                self._context_key = next(reversed(self._contexts), None)
            replay_keys = tuple(
                key
                for key in self._prefetch_replays
                if key[0] == cursor and (trigger_ref is None or key[1] == trigger_ref)
            )
            for key in replay_keys:
                self._prefetch_replays.pop(key, None)
        self.discard_scheduled_prefetch(cursor, trigger_ref=trigger_ref)

    def refresh(
        self,
        *,
        cursor: RecallCursor,
        actor_ref: str,
        subject_refs: tuple[str, ...],
        logical_time: datetime,
        sources: RecallCorpusSources,
        trigger_ref: str | None = None,
    ) -> None:
        documents = self._corpus.compile(
            cursor=cursor,
            actor_ref=actor_ref,
            subject_refs=subject_refs,
            sources=sources,
        )
        with self._context_guard:
            self._index.rebuild(cursor=cursor, documents=documents)
            snapshot = self._index.snapshot()
            if snapshot.cursor != cursor:
                raise ValueError("recall sidecar changed while its Context was being pinned")
            key = (cursor, trigger_ref)
            existing = self._contexts.get(key)
            predecessor = (
                existing.paired_predecessor_cursor
                if existing is not None and existing.trigger_ref == trigger_ref
                else self._latest_context_cursor(excluding=cursor)
            )
            self._remember(
                key=key,
                context=_PinnedRecallContext(
                    snapshot=snapshot,
                    actor_ref=actor_ref,
                    subject_refs=subject_refs,
                    logical_time=logical_time,
                    trigger_ref=trigger_ref,
                    paired_predecessor_cursor=predecessor,
                ),
            )

    def prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        query_text: str,
        lexical_text: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        link_refs: tuple[str, ...] = (),
        memory_kinds: tuple[Literal["episodic", "semantic", "reflective"], ...] = (),
        accessibility_seed: str,
        trigger_ref: str,
        limit: int = 4,
    ) -> TrustedRecallTrace:
        context = self._context_for(expected_cursor, trigger_ref=trigger_ref)
        if context is None:
            raise ValueError("prefetch request does not match the pinned Context")
        request = CharacterRecallRequest(
            query_text=query_text,
            lexical_text=lexical_text,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            link_refs=link_refs,
            memory_kinds=memory_kinds,
            limit=min(limit, 6),
        )
        result = self._search(
            context=context,
            request=request,
            accessibility_seed=accessibility_seed,
            semantic=self._semantic_embedding is not None,
        )
        trace = self._issue_trace(
            mode="prefetch",
            trigger_ref=trigger_ref,
            request=request,
            result=result,
            evaluated_cursor=expected_cursor,
        )
        return trace

    def schedule_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        query_text: str,
        lexical_text: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        link_refs: tuple[str, ...] = (),
        memory_kinds: tuple[Literal["episodic", "semantic", "reflective"], ...] = (),
        accessibility_seed: str,
        trigger_ref: str,
        limit: int = 4,
    ) -> PrefetchJobToken:
        """Start bounded local attention search without delaying the first model call."""

        with self._active_recall_guard:
            if self._closed:
                raise RuntimeError("recall coordinator is closed")
        context = self._context_for(expected_cursor, trigger_ref=trigger_ref)
        if context is None:
            raise ValueError("prefetch request does not match the pinned Context")
        prefetch_key = (expected_cursor, trigger_ref)
        request = CharacterRecallRequest(
            query_text=query_text,
            lexical_text=lexical_text,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            link_refs=link_refs,
            memory_kinds=memory_kinds,
            limit=min(limit, 6),
        )
        local_fallback = self._issue_trace(
            mode="prefetch",
            trigger_ref=trigger_ref,
            request=request,
            result=self._search(
                context=context,
                request=request,
                accessibility_seed=accessibility_seed,
                semantic=False,
            ),
            evaluated_cursor=expected_cursor,
        )
        with self._context_guard:
            self._prefetch_replays.pop(prefetch_key, None)
        has_worker_slot = self._prefetch_slots.acquire(blocking=False)
        previous: _PrefetchJob | None = None
        evicted_jobs: list[_PrefetchJob] = []
        capacity_fallback = False
        with self._active_recall_guard:
            if self._closed:
                if has_worker_slot:
                    self._prefetch_slots.release()
                raise RuntimeError("recall coordinator is closed")
            with self._prefetch_state_guard:
                previous = self._prefetch_futures.pop(prefetch_key, None)
                self._prefetch_join_attempted.discard(prefetch_key)
                self._prefetch_epoch += 1
                epoch = self._prefetch_epoch
                future: Future[TrustedRecallTrace | None] = Future()
                cancelled = threading.Event()
                thread: threading.Thread | None = None
                if has_worker_slot:
                    inherited_context = copy_context()
                    thread = threading.Thread(
                        target=inherited_context.run,
                        args=(self._run_prefetch_job,),
                        kwargs={
                            "future": future,
                            "cancelled": cancelled,
                            "context": context,
                            "request": request,
                            "accessibility_seed": accessibility_seed,
                            "trigger_ref": trigger_ref,
                            "evaluated_cursor": expected_cursor,
                            "epoch": epoch,
                            "local_fallback": local_fallback,
                        },
                        name="world-v2-recall-prefetch",
                        daemon=True,
                    )
                else:
                    # Provider saturation must not darken automatic memory:
                    # publish the already source-bound local search as a
                    # completed generation for this exact Context.
                    future.set_result(local_fallback)
                    capacity_fallback = True
                job = _PrefetchJob(
                    future=future,
                    cancelled=cancelled,
                    thread=thread,
                    epoch=epoch,
                    local_fallback=local_fallback,
                )
                self._prefetch_futures[prefetch_key] = job
                self._remember_prefetch_trace_epoch_locked(
                    (
                        local_fallback.audit.result_hash,
                        local_fallback.audit.embedding_version,
                    ),
                    epoch,
                )
                while len(self._prefetch_futures) > _MAX_PINNED_RECALL_CONTEXTS:
                    evicted_key, evicted = self._prefetch_futures.popitem(last=False)
                    self._prefetch_join_attempted.discard(evicted_key)
                    evicted_jobs.append(evicted)
            if thread is not None:
                with self._prefetch_worker_guard:
                    self._prefetch_worker_threads.add(thread)
                    thread.start()
        if previous is not None:
            previous.cancel()
        for evicted in evicted_jobs:
            evicted.cancel()
        if capacity_fallback:
            self._set_prefetch_health(
                epoch=epoch,
                status="degraded",
                failure_code="prefetch_capacity",
                trace=local_fallback,
            )
        return PrefetchJobToken(
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
            epoch=epoch,
        )

    def scheduled_prefetch_token(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
    ) -> PrefetchJobToken | None:
        """Snapshot the current generation for one cleanup/consume owner."""

        key = (expected_cursor, trigger_ref)
        with self._prefetch_state_guard:
            job = self._prefetch_futures.get(key)
            if job is None:
                return None
            return PrefetchJobToken(
                expected_cursor=expected_cursor,
                trigger_ref=trigger_ref,
                epoch=job.epoch,
            )

    @staticmethod
    def _job_matches_token(
        key: _RecallPrefetchKey,
        job: _PrefetchJob,
        token: PrefetchJobToken | None,
    ) -> bool:
        return token is None or (
            key == (token.expected_cursor, token.trigger_ref)
            and job.epoch == token.epoch
        )

    async def consume_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        timeout_seconds: float = 0.05,
        job_token: PrefetchJobToken | None = None,
    ) -> TrustedRecallTrace | None:
        """Return candidates only when the character chose a second recall pass.

        A pending prefetch is allowed a tiny bounded join.  Timeout or search
        failure is fail-empty because this sidecar is not World authority.
        """

        key = (expected_cursor, trigger_ref)
        with self._prefetch_state_guard:
            job = self._prefetch_futures.get(key)
            if job is not None and not self._job_matches_token(
                key,
                job,
                job_token,
            ):
                job = None
            if job is not None:
                self._active_prefetch_consumers.add((key, job.epoch))
                self._prefetch_join_attempted.discard(key)
        if job is None:
            return None
        try:
            async with asyncio.timeout(timeout_seconds):
                while not job.future.done():
                    if job.cancelled.is_set():
                        return None
                    await asyncio.sleep(0.005)
                trace = job.future.result()
        except asyncio.CancelledError:
            with self._active_recall_guard:
                if self._closed:
                    return None
            raise
        except Exception:
            job.cancel()
            return None
        finally:
            with self._prefetch_state_guard:
                self._active_prefetch_consumers.discard((key, job.epoch))
                if self._prefetch_futures.get(key) is job:
                    self._prefetch_futures.pop(key, None)
                self._prefetch_join_attempted.discard(key)
        if trace is None:
            return None
        audit = verify_trusted_recall_trace(trace)
        if (
            audit.trigger_ref != trigger_ref
            or audit.evaluated_cursor != expected_cursor
        ):
            return None
        if not self._remember_prefetch_replay(
            key=key,
            trace=trace,
            expected_epoch=job.epoch,
        ):
            return None
        return trace

    def take_ready_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        job_token: PrefetchJobToken | None = None,
    ) -> TrustedRecallTrace | None:
        """Take an already-finished candidate set without waiting at all.

        This is the automatic half of dual-channel recall.  A candidate set
        that lost the race with the first model call remains available for the
        optional character pull, while a completed set can be placed in the
        first call and audited as material the character actually saw.
        """

        key = (expected_cursor, trigger_ref)
        with self._prefetch_state_guard:
            job = self._prefetch_futures.get(key)
            if (
                job is None
                or not self._job_matches_token(key, job, job_token)
                or not job.future.done()
            ):
                return None
            if self._prefetch_futures.get(key) is job:
                self._prefetch_futures.pop(key, None)
            self._prefetch_join_attempted.discard(key)
        try:
            trace = job.future.result()
            if trace is None:
                return None
            audit = verify_trusted_recall_trace(trace)
        except Exception:
            job.cancel()
            return None
        if audit.trigger_ref != trigger_ref or audit.evaluated_cursor != expected_cursor:
            return None
        if not self._remember_prefetch_replay(
            key=key,
            trace=trace,
            expected_epoch=job.epoch,
        ):
            return None
        return trace

    async def await_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        timeout_seconds: float = PREFETCH_FIRST_PASS_JOIN_SECONDS,
        job_token: PrefetchJobToken | None = None,
    ) -> TrustedRecallTrace | None:
        """Join a pending prefetch within a small bound before the first call.

        The zero-wait ``take_ready_scheduled_prefetch`` lost the race against
        the first model call on effectively every real turn, which silently
        reduced automatic recall to the ~2 facts already inside the capsule
        slice.  A bounded join trades at most ``timeout_seconds`` of latency —
        noise next to a multi-second provider call — for the character
        actually seeing what she remembers.  On timeout the job is left in
        place for the optional character-chosen pull, and every drop is
        logged so a dark recall channel is observable instead of silent.
        """

        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds < 0
        ):
            raise ValueError("recall prefetch join timeout must be finite and non-negative")
        key = (expected_cursor, trigger_ref)
        with self._active_recall_guard:
            if self._closed:
                return None
        # A bounded first-pass miss stores the local trace for immediate model
        # continuity but deliberately leaves the semantic worker alive.  Once
        # that worker finishes, its source-bound result is the better attention
        # candidate for every later phase of this exact pinned turn.  Check the
        # future before the replay; otherwise the local trace permanently
        # shadows a completed semantic result.
        ready = self.take_ready_scheduled_prefetch(
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
            job_token=job_token,
        )
        if ready is not None:
            return ready
        with self._context_guard:
            replay = self._prefetch_replays.get(key)
        if replay is not None and job_token is not None:
            with self._prefetch_state_guard:
                replay_epoch = self._prefetch_trace_epochs.get(
                    (
                        replay.audit.result_hash,
                        replay.audit.embedding_version,
                    )
                )
            if replay_epoch != job_token.epoch:
                replay = None
        if replay is not None:
            with self._context_guard:
                if self._prefetch_replays.get(key) is replay:
                    self._prefetch_replays.move_to_end(key)
            with self._active_recall_guard:
                if self._closed:
                    return None
            return replay
        with self._prefetch_state_guard:
            job = self._prefetch_futures.get(key)
            if job is not None and not self._job_matches_token(
                key,
                job,
                job_token,
            ):
                job = None
        if job is None:
            logger.debug(
                "recall prefetch unavailable at first pass: trigger=%s cursor=%s",
                trigger_ref,
                expected_cursor,
            )
            return None
        if not job.future.done():
            # Automatic prefetch is deliberately local-only.  Give that local
            # search one small bounded join even when no semantic provider is
            # configured; otherwise the first call races past useful lexical
            # memories on nearly every cold thread.
            reuse_local_fallback = False
            with self._prefetch_state_guard:
                if self._prefetch_futures.get(key) is not job:
                    return None
                if key in self._prefetch_join_attempted:
                    reuse_local_fallback = True
                else:
                    self._prefetch_join_attempted.add(key)
            if reuse_local_fallback:
                with self._active_recall_guard:
                    if self._closed:
                        return None
                return job.local_fallback
            if timeout_seconds == 0:
                logger.debug(
                    "recall prefetch used immediate local fallback because the "
                    "ingress-to-provider budget was exhausted: trigger=%s",
                    trigger_ref,
                )
                if not self._remember_prefetch_replay(
                    key=key,
                    trace=job.local_fallback,
                    expected_epoch=job.epoch,
                ):
                    return None
                return job.local_fallback
            try:
                async with asyncio.timeout(timeout_seconds):
                    await asyncio.shield(asyncio.wrap_future(job.future))
            except TimeoutError:
                logger.warning(
                    "recall prefetch missed the bounded first-pass join "
                    "(%.0f ms); using local fallback and leaving semantic "
                    "work for the character pull: trigger=%s",
                    timeout_seconds * 1000,
                    trigger_ref,
                )
                if not self._remember_prefetch_replay(
                    key=key,
                    trace=job.local_fallback,
                    expected_epoch=job.epoch,
                ):
                    return None
                return job.local_fallback
            except Exception:
                with self._prefetch_state_guard:
                    if self._prefetch_futures.get(key) is job:
                        self._prefetch_futures.pop(key, None)
                    self._prefetch_join_attempted.discard(key)
                job.cancel()
                logger.warning(
                    "recall prefetch search failed: trigger=%s", trigger_ref, exc_info=True
                )
                if not self._remember_prefetch_replay(
                    key=key,
                    trace=job.local_fallback,
                    expected_epoch=job.epoch,
                ):
                    return None
                return job.local_fallback
        with self._prefetch_state_guard:
            if self._prefetch_futures.get(key) is job:
                self._prefetch_futures.pop(key, None)
            self._prefetch_join_attempted.discard(key)
        try:
            trace = job.future.result()
            if trace is None:
                return None
            audit = verify_trusted_recall_trace(trace)
        except Exception:
            job.cancel()
            logger.warning(
                "recall prefetch result rejected: trigger=%s", trigger_ref, exc_info=True
            )
            return None
        if audit.trigger_ref != trigger_ref or audit.evaluated_cursor != expected_cursor:
            logger.warning(
                "recall prefetch dropped on identity mismatch: trigger=%s "
                "expected_cursor=%s trace_cursor=%s",
                trigger_ref,
                expected_cursor,
                audit.evaluated_cursor,
            )
            return None
        if not self._remember_prefetch_replay(
            key=key,
            trace=trace,
            expected_epoch=job.epoch,
        ):
            return None
        return trace

    def discard_scheduled_prefetch(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None = None,
        job_token: PrefetchJobToken | None = None,
    ) -> None:
        if job_token is not None and (
            job_token.expected_cursor != cursor
            or (
                trigger_ref is not None
                and job_token.trigger_ref != trigger_ref
            )
        ):
            raise ValueError("prefetch cleanup token does not match its target")
        removed: list[tuple[_RecallPrefetchKey, _PrefetchJob, bool]] = []
        with self._prefetch_state_guard:
            keys = tuple(
                key
                for key in self._prefetch_futures
                if key[0] == cursor and (trigger_ref is None or key[1] == trigger_ref)
            )
            for key in keys:
                job = self._prefetch_futures.get(key)
                if job is None:
                    continue
                if job_token is not None and (
                    key != (
                        job_token.expected_cursor,
                        job_token.trigger_ref,
                    )
                    or job.epoch != job_token.epoch
                ):
                    continue
                if self._prefetch_futures.get(key) is job:
                    self._prefetch_futures.pop(key, None)
                    self._prefetch_join_attempted.discard(key)
                    removed.append(
                        (
                            key,
                            job,
                            (key, job.epoch)
                            in self._presented_local_prefetch_keys,
                        )
                    )
        for key, job, local_replay_was_presented in removed:
            if local_replay_was_presented and self._semantic_embedding is not None:
                status = "semantic_pending_unpresented"
                if job.future.done():
                    try:
                        completed = job.future.result()
                    except Exception:
                        completed = None
                    if completed is not None and self._is_semantic_trace(completed):
                        status = "semantic_late_unpresented"
                self._record_prefetch_delivery(epoch=job.epoch, status=status)
            job.cancel()

    def recall(
        self,
        *,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        expected_cursor: RecallCursor,
        trigger_ref: str,
    ) -> TrustedRecallTrace:
        self._begin_recall()
        try:
            context = self._context_for(expected_cursor, trigger_ref=trigger_ref)
            if context is None:
                raise ValueError(
                    "recall request does not match the pinned Context cursor: "
                    f"available={tuple(self._contexts)!r} expected="
                    f"{(expected_cursor, trigger_ref)!r}"
                )
            result = self._search(
                context=context,
                request=request,
                accessibility_seed=accessibility_seed,
                semantic=True,
            )
            return self._issue_trace(
                mode="character_pull",
                trigger_ref=trigger_ref,
                request=request,
                result=result,
                evaluated_cursor=expected_cursor,
            )
        finally:
            self._end_recall()

    def _begin_recall(self) -> None:
        with self._active_recall_guard:
            if self._closed:
                raise RuntimeError("recall coordinator is closed")
            self._active_recalls += 1
            self._active_recalls_drained.clear()

    def _end_recall(self) -> None:
        with self._active_recall_guard:
            self._active_recalls -= 1
            if self._active_recalls == 0:
                self._active_recalls_drained.set()

    def _issue_trace(
        self,
        *,
        mode: str,
        trigger_ref: str,
        request: CharacterRecallRequest,
        result: RecallResult,
        evaluated_cursor: RecallCursor,
    ) -> TrustedRecallTrace:
        audit = RecallAuditTrace(
            mode=mode,
            trigger_ref=trigger_ref,
            request=request,
            query=result.query,
            query_hash=result.query_hash,
            result_hash=result.result_hash,
            index_version=result.index_version,
            embedding_version=result.embedding_version,
            embedding_status=result.embedding_status,
            embedding_failure_code=result.embedding_failure_code,
            index_cursor=result.index_cursor,
            evaluated_cursor=evaluated_cursor,
            hits=tuple(
                RecallAuditHit(
                    document=hit.document,
                    match_channels=hit.match_channels,
                    score_bp=hit.score_bp,
                    lexical_score_bp=hit.lexical_score_bp,
                    dense_score_bp=hit.dense_score_bp,
                    temporal_score_bp=hit.temporal_score_bp,
                    structured_score_bp=hit.structured_score_bp,
                    accessibility_offset_bp=hit.accessibility_offset_bp,
                )
                for hit in result.hits
            ),
        )
        return TrustedRecallTrace(audit=audit, authority_seal=_trace_seal(audit))

    def carry_forward(
        self,
        trace: TrustedRecallTrace,
        *,
        evaluated_cursor: RecallCursor,
        trigger_ref: str,
    ) -> TrustedRecallTrace:
        audit = verify_trusted_recall_trace(trace)
        if audit.trigger_ref != trigger_ref:
            raise ValueError("paired recall carry belongs to another trigger")
        if audit.reuse_contract != "same_context":
            raise ValueError("paired recall can be carried exactly once")
        target = self._context_for(evaluated_cursor, trigger_ref=trigger_ref)
        if target is None:
            raise ValueError("paired recall carry has no pinned target Context")
        if target.trigger_ref != trigger_ref:
            raise ValueError("paired recall target belongs to another trigger")
        source_cursor = audit.index_cursor
        if target.paired_predecessor_cursor != source_cursor:
            raise ValueError("paired recall source is not the target Context's exact predecessor")
        carried = audit.model_copy(
            update={
                "evaluated_cursor": evaluated_cursor,
                "reuse_contract": "paired_cognition_carry",
                "paired_transition_hash": paired_recall_transition_hash(
                    trigger_ref=trigger_ref,
                    source_cursor=audit.index_cursor,
                    target_cursor=evaluated_cursor,
                ),
            }
        )
        carried = RecallAuditTrace.model_validate(
            carried.model_dump(mode="python", warnings="error")
        )
        return TrustedRecallTrace(
            audit=carried,
            authority_seal=_trace_seal(carried),
        )

    def _prefetch_context(
        self,
        *,
        context: _PinnedRecallContext,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        trigger_ref: str,
        evaluated_cursor: RecallCursor,
    ) -> TrustedRecallTrace:
        # Automatic attention may use the configured semantic lane. It runs in
        # its own daemon thread and the first author call joins it only through
        # the bounded deadline above, so an embedding outage cannot hold the
        # visible reply hostage. The result is still only source-bound
        # attention material; it does not choose a response or behavior.
        result = self._search(
            context=context,
            request=request,
            accessibility_seed=accessibility_seed,
            semantic=self._semantic_embedding is not None,
        )
        return self._issue_trace(
            mode="prefetch",
            trigger_ref=trigger_ref,
            request=request,
            result=result,
            evaluated_cursor=evaluated_cursor,
        )

    def _run_prefetch_job(
        self,
        *,
        future: Future[TrustedRecallTrace | None],
        cancelled: threading.Event,
        context: _PinnedRecallContext,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        trigger_ref: str,
        evaluated_cursor: RecallCursor,
        epoch: int,
        local_fallback: TrustedRecallTrace,
    ) -> None:
        try:
            if not future.set_running_or_notify_cancel():
                return
            if cancelled.is_set():
                return
            trace = self._prefetch_context(
                context=context,
                request=request,
                accessibility_seed=accessibility_seed,
                trigger_ref=trigger_ref,
                evaluated_cursor=evaluated_cursor,
            )
            if cancelled.is_set():
                return
            self._set_prefetch_health(
                epoch=epoch,
                status=("degraded" if trace.audit.embedding_status == "degraded" else "ready"),
                failure_code=trace.audit.embedding_failure_code,
                trace=trace,
            )
            with self._prefetch_state_guard:
                self._remember_prefetch_trace_epoch_locked(
                    (
                        trace.audit.result_hash,
                        trace.audit.embedding_version,
                    ),
                    epoch,
                )
            if not future.done() and not cancelled.is_set():
                future.set_result(trace)
            key = (evaluated_cursor, trigger_ref)
            record_late_ready = False
            with self._prefetch_state_guard:
                late_ready_key = (key, epoch)
                if (
                    (key, epoch) in self._presented_local_prefetch_keys
                    and self._is_semantic_trace(trace)
                    and late_ready_key not in self._prefetch_late_ready_recorded
                ):
                    self._remember_prefetch_marker_locked(
                        self._prefetch_late_ready_recorded,
                        late_ready_key,
                    )
                    record_late_ready = True
            if record_late_ready:
                self._record_prefetch_delivery(
                    epoch=epoch,
                    status="semantic_late_ready",
                )
        except BaseException as exc:
            if not cancelled.is_set():
                self._set_prefetch_health(
                    epoch=epoch,
                    status="technical_failure",
                    failure_code=type(exc).__name__[:128],
                    trace=local_fallback,
                )
            if not future.done() and not cancelled.is_set():
                future.set_exception(exc)
        finally:
            if not future.done():
                # A running concurrent Future cannot be cancelled. Complete it
                # with a benign empty result so an abandoned asyncio wrapper
                # does not emit "Future exception was never retrieved" after
                # the bounded first-pass join times out or shutdown begins.
                future.set_result(None)
            cancelled.set()
            self._prefetch_slots.release()
            with self._prefetch_worker_guard:
                self._prefetch_worker_threads.discard(threading.current_thread())

    def _set_prefetch_health(
        self,
        *,
        epoch: int,
        status: str,
        failure_code: str | None,
        trace: TrustedRecallTrace | None = None,
    ) -> None:
        with self._prefetch_health_lock:
            if epoch < self._prefetch_health.epoch:
                return
            self._prefetch_health = _PrefetchHealth(
                epoch=epoch,
                status=status,
                failure_code=failure_code,
                hit_count=(len(trace.audit.hits) if trace is not None else 0),
                match_channels=(
                    tuple(
                        sorted(
                            {channel for hit in trace.audit.hits for channel in hit.match_channels}
                        )
                    )
                    if trace is not None
                    else ()
                ),
                embedding_status=(trace.audit.embedding_status if trace is not None else "unknown"),
                fallback_channels=(
                    tuple(
                        (
                            "lexical",
                            *(("structured",) if trace.audit.request.link_refs else ()),
                            *(
                                ("temporal",)
                                if trace.audit.request.occurred_from is not None
                                or trace.audit.request.occurred_to is not None
                                else ()
                            ),
                        )
                    )
                    if trace is not None and status in {"degraded", "technical_failure"}
                    else ()
                ),
            )

    def _is_semantic_trace(self, trace: TrustedRecallTrace) -> bool:
        semantic = self._semantic_embedding
        return (
            semantic is not None
            and trace.audit.embedding_version == semantic.version
            and trace.audit.embedding_status == "used"
        )

    def record_prefetch_presentation(
        self,
        presentation: PresentedPrefetchTrace,
    ) -> None:
        """Count only source material actually shown to a successful role call."""

        audit = verify_trusted_recall_trace(presentation.trace)
        if audit.mode != "prefetch":
            raise ValueError("prefetch presentation requires a prefetch trace")
        key = (audit.evaluated_cursor or audit.index_cursor, audit.trigger_ref)
        record_status: str | None = None
        record_late_ready = False
        with self._prefetch_state_guard:
            trace_identity = (audit.result_hash, audit.embedding_version)
            if trace_identity in self._presented_prefetch_results:
                self._presented_prefetch_results.move_to_end(trace_identity)
                return
            self._presented_prefetch_results[trace_identity] = None
            while len(self._presented_prefetch_results) > 64:
                self._presented_prefetch_results.popitem(last=False)
            epoch = self._prefetch_trace_epochs.get(
                trace_identity,
                self._prefetch_epoch,
            )
            local_presentation_key = (key, epoch)
            if self._is_semantic_trace(presentation.trace):
                record_status = (
                    "semantic_late_consumed"
                    if local_presentation_key
                    in self._presented_local_prefetch_keys
                    else "semantic_first_pass"
                )
            else:
                self._remember_prefetch_marker_locked(
                    self._presented_local_prefetch_keys,
                    local_presentation_key,
                )
                record_status = "local_first_pass"
                job = self._prefetch_futures.get(key)
                late_ready_key = (key, epoch)
                if (
                    job is not None
                    and job.future.done()
                    and late_ready_key not in self._prefetch_late_ready_recorded
                ):
                    try:
                        completed = job.future.result()
                    except Exception:
                        completed = None
                    if completed is not None and self._is_semantic_trace(completed):
                        self._remember_prefetch_marker_locked(
                            self._prefetch_late_ready_recorded,
                            late_ready_key,
                        )
                        record_late_ready = True
        if record_status is not None:
            self._record_prefetch_delivery(epoch=epoch, status=record_status)
        if record_late_ready:
            self._record_prefetch_delivery(
                epoch=epoch,
                status="semantic_late_ready",
            )

    def _remember_prefetch_trace_epoch_locked(
        self,
        trace_identity: tuple[str, str],
        epoch: int,
    ) -> None:
        existing = self._prefetch_trace_epochs.get(trace_identity)
        if existing is not None and existing > epoch:
            return
        self._prefetch_trace_epochs.pop(trace_identity, None)
        self._prefetch_trace_epochs[trace_identity] = epoch
        while len(self._prefetch_trace_epochs) > 64:
            self._prefetch_trace_epochs.popitem(last=False)

    @staticmethod
    def _remember_prefetch_marker_locked(
        values: OrderedDict[tuple[_RecallPrefetchKey, int], None],
        key: tuple[_RecallPrefetchKey, int],
    ) -> None:
        values.pop(key, None)
        values[key] = None
        while len(values) > 64:
            values.popitem(last=False)

    def _record_prefetch_delivery(self, *, epoch: int, status: str) -> None:
        with self._prefetch_delivery_lock:
            health = self._prefetch_delivery_health
            if status in {"local_first_pass", "local_fallback_first_pass"}:
                health.first_pass_local_count += 1
            elif status == "semantic_first_pass":
                health.first_pass_semantic_count += 1
            elif status == "semantic_late_ready":
                health.late_semantic_ready_count += 1
            elif status == "semantic_late_consumed":
                health.late_semantic_consumed_count += 1
            elif status in {
                "semantic_late_unpresented",
                "semantic_pending_unpresented",
            }:
                health.late_semantic_unpresented_count += 1
            if epoch >= health.epoch:
                health.epoch = epoch
                health.last_status = status

    def _search(
        self,
        *,
        context: _PinnedRecallContext,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        semantic: bool,
    ) -> RecallResult:
        query = RecallQuery(
            query_text=request.query_text,
            lexical_text=request.lexical_text,
            cursor=context.snapshot.cursor,
            actor_ref=context.actor_ref,
            subject_refs=context.subject_refs,
            viewer_privacy_ceiling="withhold",
            at=context.logical_time,
            occurred_from=request.occurred_from,
            occurred_to=request.occurred_to,
            link_refs=request.link_refs,
            memory_kinds=request.memory_kinds,
            include_historical=request.include_historical,
            limit=request.limit,
            accessibility_seed=accessibility_seed,
        )
        if not semantic or self._semantic_embedding is None:
            return context.snapshot.search(query)
        try:
            # A cold corpus used to spend one provider round-trip embedding
            # documents during rebuild and a second embedding the query during
            # search. The bounded first-pass join would therefore publish the
            # lexical fallback even when the completed semantic result had a
            # valid hit. Freeze documents and query in one provider batch, then
            # replay those exact vectors through the unchanged eligibility,
            # ranking, source-binding, and audit path.
            documents = context.snapshot.documents
            document_texts = tuple(
                document.retrieval_text or document.text
                for document in documents
            )
            batch_texts = (*document_texts, query.query_text)
            batch_vectors = self._semantic_embedding.embed(batch_texts)
            semantic_index = InMemoryRecallIndex(
                embedding=_PinnedBatchRecallEmbedding(
                    source=self._semantic_embedding,
                    texts=batch_texts,
                    vectors=batch_vectors,
                )
            )
            semantic_index.rebuild(
                cursor=context.snapshot.cursor,
                documents=documents,
            )
            return semantic_index.search(query)
        except (RecallEmbeddingUnavailable, ValueError) as exc:
            # Embedding is an accessibility channel, not factual authority and
            # not a prerequisite for speaking. A provider/contract failure
            # therefore falls back to the already-pinned local index while the
            # trace and health surface retain the exact degraded reason.
            logger.warning("semantic recall degraded to local index", exc_info=True)
            local = context.snapshot.search(query)
            failure_code = (
                str(exc)[:128]
                if isinstance(exc, RecallEmbeddingUnavailable)
                else f"{type(exc).__name__}:{str(exc)}"[:128]
            )
            return local.model_copy(
                update={
                    "embedding_status": "degraded",
                    "embedding_failure_code": failure_code,
                }
            )

    def _remember(
        self,
        *,
        key: _RecallContextKey,
        context: _PinnedRecallContext,
    ) -> None:
        self._contexts.pop(key, None)
        self._contexts[key] = context
        self._context_key = key
        while len(self._contexts) > _MAX_PINNED_RECALL_CONTEXTS:
            evicted_key, _ = self._contexts.popitem(last=False)
            self._prefetch_replays.pop(
                (evicted_key[0], evicted_key[1]),
                None,
            )
            self.discard_scheduled_prefetch(
                evicted_key[0],
                trigger_ref=evicted_key[1],
            )

    def _remember_prefetch_replay(
        self,
        *,
        key: _RecallPrefetchKey,
        trace: TrustedRecallTrace,
        expected_epoch: int,
    ) -> bool:
        audit = verify_trusted_recall_trace(trace)
        if audit.evaluated_cursor != key[0] or audit.trigger_ref != key[1]:
            raise ValueError("prefetch replay identity does not match its pinned Context")
        with self._active_recall_guard:
            if self._closed:
                return False
            with self._context_guard:
                with self._prefetch_state_guard:
                    current = self._prefetch_futures.get(key)
                    trace_epoch = self._prefetch_trace_epochs.get(
                        (
                            audit.result_hash,
                            audit.embedding_version,
                        )
                    )
                    if trace_epoch != expected_epoch or (
                        current is not None
                        and current.epoch != expected_epoch
                    ):
                        return False
                self._prefetch_replays.pop(key, None)
                self._prefetch_replays[key] = trace
                while len(self._prefetch_replays) > _MAX_PINNED_RECALL_CONTEXTS:
                    self._prefetch_replays.popitem(last=False)
        return True

    def _latest_context_cursor(
        self,
        *,
        excluding: RecallCursor,
    ) -> RecallCursor | None:
        for (cursor, _trigger_ref), _context in reversed(self._contexts.items()):
            if cursor != excluding:
                return cursor
        return None

    def _context_for(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None,
    ) -> _PinnedRecallContext | None:
        with self._context_guard:
            if trigger_ref is not None:
                exact = self._contexts.get((cursor, trigger_ref))
                if exact is not None:
                    return exact
            return self._contexts.get((cursor, None))

    def close(self) -> None:
        with self._active_recall_guard:
            if self._closed:
                return
            self._closed = True
            with self._prefetch_state_guard:
                jobs = tuple(self._prefetch_futures.values())
                self._prefetch_futures.clear()
                self._prefetch_join_attempted.clear()
        for job in jobs:
            job.cancel()
        with self._context_guard:
            self._prefetch_replays.clear()
        with self._prefetch_worker_guard:
            worker_threads = tuple(self._prefetch_worker_threads)
        close_embedding = getattr(self._semantic_embedding, "close", None)
        if not callable(close_embedding):
            return
        deadline = time.monotonic() + 0.05
        for thread in worker_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        alive = tuple(thread for thread in worker_threads if thread.is_alive())
        if not alive and self._active_recalls_drained.is_set():
            close_embedding()
            return

        # A provider call already in flight cannot be force-cancelled safely.
        # Keep shutdown bounded, but defer closing its shared embedding/cache
        # until every cancelled prefetch and character pull has left the
        # critical section.
        def close_after_workers() -> None:
            for thread in alive:
                thread.join()
            self._active_recalls_drained.wait()
            close_embedding()

        threading.Thread(
            target=close_after_workers,
            name="world-v2-recall-prefetch-close",
            daemon=True,
        ).start()


def recall_evidence_json(trace: RecallAuditTrace) -> str:
    """Bounded model-visible candidates with authority labels, not instructions."""

    return _canonical_json(
        {
            "recall_query": trace.request.model_dump(mode="json"),
            "executed_query": trace.query.model_dump(mode="json"),
            "query_hash": trace.query_hash,
            "result_hash": trace.result_hash,
            "index_version": trace.index_version,
            "embedding_version": trace.embedding_version,
            "candidates": tuple(
                {
                    "memory_kind": hit.document.memory_kind,
                    "authority": hit.document.authority,
                    "epistemic_scope": hit.document.effective_epistemic_scope,
                    "actor_ref": hit.document.actor_ref,
                    "speaker_ref": hit.document.speaker_ref,
                    "subject_refs": hit.document.subject_refs,
                    "text": hit.document.text,
                    "source_refs": hit.document.source_refs,
                    "source_slice": hit.document.source_slice,
                    "occurred_from": hit.document.occurred_from.isoformat(),
                    "occurred_to": (
                        hit.document.occurred_to.isoformat()
                        if hit.document.occurred_to is not None
                        else None
                    ),
                    "valid_from": (
                        hit.document.valid_from.isoformat()
                        if hit.document.valid_from is not None
                        else None
                    ),
                    "valid_to": (
                        hit.document.valid_to.isoformat()
                        if hit.document.valid_to is not None
                        else None
                    ),
                    "status": hit.document.status,
                    "privacy_class": hit.document.privacy_class,
                    "match_channels": hit.match_channels,
                    "score_bp": hit.score_bp,
                    "lexical_score_bp": hit.lexical_score_bp,
                    "dense_score_bp": hit.dense_score_bp,
                    "temporal_score_bp": hit.temporal_score_bp,
                    "structured_score_bp": hit.structured_score_bp,
                    "accessibility_offset_bp": hit.accessibility_offset_bp,
                }
                for hit in trace.hits
            ),
        }
    )


def recall_followup_evidence_json(
    *,
    prefetch: RecallAuditTrace | None,
    character_pull: RecallAuditTrace,
) -> str:
    """Encode exactly the recall candidates shown in the optional second pass."""

    return _canonical_json(
        {
            "parallel_attention_prefetch": (
                json.loads(recall_evidence_json(prefetch)) if prefetch is not None else None
            ),
            "character_chosen_recall": json.loads(recall_evidence_json(character_pull)),
        }
    )


def augment_model_content_with_recall(
    model_content_json: str,
    trace: RecallAuditTrace,
) -> str:
    """Add verified pull results to their semantic lanes for claim validation."""

    value = json.loads(model_content_json)
    if not isinstance(value, dict) or not isinstance(value.get("slices"), dict):
        raise ValueError("recall augmentation requires a model-facing Context object")
    slices = value["slices"]
    for hit in trace.hits:
        document = hit.document
        lane = slices.get(document.source_slice)
        if not isinstance(lane, dict):
            lane = {"availability": "available", "source_refs": [], "items": []}
            slices[document.source_slice] = lane
        lane["availability"] = "available"
        refs = lane.get("source_refs")
        if not isinstance(refs, list):
            refs = []
        lane["source_refs"] = sorted({*refs, *document.source_refs})
        items = lane.get("items")
        if not isinstance(items, list):
            items = []
        items = [
            item
            for item in items
            if not isinstance(item, dict) or item.get("item_ref") != document.source_item_ref
        ]
        items.append(
            {
                "item_ref": document.source_item_ref,
                "privacy_class": document.privacy_class,
                # This is a provider-view selection marker, not World
                # authority.  compact_chat_model_facing_context uses it to
                # keep an audited ready-prefetch item when the source slice
                # was already at its ordinary item limit, then strips it from
                # the final semantic item.
                "recall_injected": True,
                "value": {
                    "memory_kind": document.memory_kind,
                    "authority": document.authority,
                    "epistemic_scope": document.effective_epistemic_scope,
                    "actor_ref": document.actor_ref,
                    "speaker_ref": document.speaker_ref,
                    "subject_refs": document.subject_refs,
                    "text": document.text,
                    "source_refs": document.source_refs,
                    "occurred_from": document.occurred_from.isoformat(),
                    "occurred_to": (
                        document.occurred_to.isoformat()
                        if document.occurred_to is not None
                        else None
                    ),
                    "valid_from": (
                        document.valid_from.isoformat() if document.valid_from is not None else None
                    ),
                    "valid_to": (
                        document.valid_to.isoformat() if document.valid_to is not None else None
                    ),
                    "status": document.status,
                },
                # Full model_content_json remains the acceptance authority.
                # These bindings are removed only from the compact provider
                # view, never from source-closure validation.
                "source_bindings": tuple(
                    binding.model_dump(mode="json") for binding in document.source_bindings
                ),
            }
        )
        lane["items"] = items
    return _canonical_json(value)


def mark_recall_budget_consumed(model_content_json: str) -> str:
    value = json.loads(model_content_json)
    if not isinstance(value, dict):
        raise ValueError("recall budget marker requires a model-facing Context object")
    value["recall_control"] = {"remaining_character_pulls": 0}
    return _canonical_json(value)


def model_content_allows_recall(model_content_json: str) -> bool:
    try:
        value = json.loads(model_content_json)
    except (TypeError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    control = value.get("recall_control")
    return not (isinstance(control, dict) and control.get("remaining_character_pulls") == 0)


__all__ = [
    "append_presented_prefetch",
    "augment_model_content_with_recall",
    "CharacterRecallRequest",
    "PREFETCH_FIRST_PASS_JOIN_SECONDS",
    "PrefetchJobToken",
    "PresentedPrefetchTrace",
    "RecallAuditTrace",
    "RecallCoordinator",
    "TrustedRecallTrace",
    "recall_evidence_json",
    "recall_followup_evidence_json",
    "perform_character_recall",
    "perform_character_recall_with_prefetch",
    "mark_recall_budget_consumed",
    "model_content_allows_recall",
    "verify_trusted_recall_trace",
]
