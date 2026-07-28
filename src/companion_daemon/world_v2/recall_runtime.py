"""Character-owned, read-only recall over a cursor-pinned disposable index."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Literal

from pydantic import Field

from .recall_corpus import RecallCorpusCompiler, RecallCorpusSources
from .recall_audit import (
    CharacterRecallRequest,
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
_MAX_PINNED_RECALL_CONTEXTS = 16
# The first cognition call may wait this long for a local prefetch that is
# still searching.  A lexical search over the in-process index finishes in a
# few milliseconds; a semantic search additionally spends one query-embedding
# provider call (cached documents embed once).  The bound exists so a
# pathological search can never hold the visible reply hostage — a prefetch
# that misses the join stays available for the character-chosen pull.
_PREFETCH_FIRST_PASS_JOIN_SECONDS = 0.3
_RecallContextKey = tuple[RecallCursor, str | None]
_RecallPrefetchKey = tuple[RecallCursor, str]


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


@dataclass(frozen=True, slots=True)
class _PinnedRecallContext:
    snapshot: RecallIndexSnapshot
    actor_ref: str
    subject_refs: tuple[str, ...]
    logical_time: datetime
    trigger_ref: str | None
    paired_predecessor_cursor: RecallCursor | None


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
) -> tuple[TrustedRecallTrace | None, TrustedRecallTrace]:
    """Join preparatory local attention and the character's chosen pull."""

    prefetch_task = asyncio.create_task(
        coordinator.consume_scheduled_prefetch(
            expected_cursor=expected_cursor,
            trigger_ref=trigger_ref,
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
        self._context_key: _RecallContextKey | None = None
        self._contexts: OrderedDict[_RecallContextKey, _PinnedRecallContext] = OrderedDict()
        self._prefetch_slots = threading.BoundedSemaphore(value=4)
        self._prefetch_futures: OrderedDict[_RecallPrefetchKey, _PrefetchJob] = OrderedDict()
        self._prefetch_join_attempted: set[_RecallPrefetchKey] = set()
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
        return self._context_key[0] if self._context_key is not None else None

    @property
    def is_closed(self) -> bool:
        return self._closed

    def semantic_health(self) -> dict[str, object]:
        with self._prefetch_health_lock:
            health = self._prefetch_health
        prefetch = {
            "last_prefetch_status": health.status,
            "last_prefetch_failure_code": health.failure_code,
            "last_prefetch_hit_count": health.hit_count,
            "last_prefetch_match_channels": list(health.match_channels),
            "last_prefetch_embedding_status": health.embedding_status,
            "turn_summary": {
                "hot_context": "ready" if self._context_key is not None else "unavailable",
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
        keys = tuple(
            key
            for key in self._contexts
            if key[0] == cursor and (trigger_ref is None or key[1] == trigger_ref)
        )
        for key in keys:
            self._contexts.pop(key, None)
        self.discard_scheduled_prefetch(cursor, trigger_ref=trigger_ref)
        if self._context_key in keys:
            self._context_key = next(reversed(self._contexts), None)

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
        context_key = self._context_key
        if context_key is None:
            raise ValueError("recall corpus has not been refreshed")
        cursor = context_key[0]
        context = self._context_for(cursor, trigger_ref=trigger_ref)
        if context is None:
            raise ValueError("prefetch trigger does not match the pinned Context")
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
            evaluated_cursor=cursor,
        )
        return trace

    def schedule_prefetch(
        self,
        *,
        query_text: str,
        lexical_text: str | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        link_refs: tuple[str, ...] = (),
        memory_kinds: tuple[Literal["episodic", "semantic", "reflective"], ...] = (),
        accessibility_seed: str,
        trigger_ref: str,
        limit: int = 4,
    ) -> None:
        """Start bounded local attention search without delaying the first model call."""

        with self._active_recall_guard:
            if self._closed:
                raise RuntimeError("recall coordinator is closed")
        context_key = self._context_key
        if context_key is None:
            raise ValueError("recall corpus has not been refreshed")
        cursor = context_key[0]
        context = self._context_for(cursor, trigger_ref=trigger_ref)
        if context is None:
            raise ValueError("prefetch trigger does not match the pinned Context")
        prefetch_key = (cursor, trigger_ref)
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
            evaluated_cursor=cursor,
        )
        previous = self._prefetch_futures.pop(prefetch_key, None)
        self._prefetch_join_attempted.discard(prefetch_key)
        if previous is not None:
            previous.cancel()
        if not self._prefetch_slots.acquire(blocking=False):
            # Provider saturation must not darken the automatic memory lane:
            # the source-bound local result is already available, so publish
            # it as a completed job for this exact Context identity.
            self._prefetch_epoch += 1
            future: Future[TrustedRecallTrace | None] = Future()
            future.set_result(local_fallback)
            self._prefetch_futures[prefetch_key] = _PrefetchJob(
                future=future,
                cancelled=threading.Event(),
                thread=None,
                epoch=self._prefetch_epoch,
                local_fallback=local_fallback,
            )
            self._set_prefetch_health(
                epoch=self._prefetch_epoch,
                status="degraded",
                failure_code="prefetch_capacity",
                trace=local_fallback,
            )
            while len(self._prefetch_futures) > _MAX_PINNED_RECALL_CONTEXTS:
                evicted_key, evicted = self._prefetch_futures.popitem(last=False)
                self._prefetch_join_attempted.discard(evicted_key)
                evicted.cancel()
            return
        self._prefetch_epoch += 1
        epoch = self._prefetch_epoch
        future: Future[TrustedRecallTrace | None] = Future()
        cancelled = threading.Event()
        thread = threading.Thread(
            target=self._run_prefetch_job,
            kwargs={
                "future": future,
                "cancelled": cancelled,
                "context": context,
                "request": request,
                "accessibility_seed": accessibility_seed,
                "trigger_ref": trigger_ref,
                "evaluated_cursor": cursor,
                "epoch": epoch,
                "local_fallback": local_fallback,
            },
            name="world-v2-recall-prefetch",
            daemon=True,
        )
        job = _PrefetchJob(
            future=future,
            cancelled=cancelled,
            thread=thread,
            epoch=epoch,
            local_fallback=local_fallback,
        )
        self._prefetch_futures[prefetch_key] = job
        # Closing the embedding and publishing a new provider worker share the
        # same lifecycle latch.  If close won the race, unwind the unstarted
        # job; if schedule won, close will see the worker registry before it
        # can close the shared provider.
        with self._active_recall_guard:
            if self._closed:
                if self._prefetch_futures.get(prefetch_key) is job:
                    self._prefetch_futures.pop(prefetch_key, None)
                self._prefetch_join_attempted.discard(prefetch_key)
                job.cancel()
                self._prefetch_slots.release()
                return
            with self._prefetch_worker_guard:
                self._prefetch_worker_threads.add(thread)
                thread.start()
        while len(self._prefetch_futures) > _MAX_PINNED_RECALL_CONTEXTS:
            evicted_key, evicted = self._prefetch_futures.popitem(last=False)
            self._prefetch_join_attempted.discard(evicted_key)
            evicted.cancel()

    async def consume_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        timeout_seconds: float = 0.05,
    ) -> TrustedRecallTrace | None:
        """Return candidates only when the character chose a second recall pass.

        A pending prefetch is allowed a tiny bounded join.  Timeout or search
        failure is fail-empty because this sidecar is not World authority.
        """

        job = self._prefetch_futures.pop(
            (expected_cursor, trigger_ref),
            None,
        )
        self._prefetch_join_attempted.discard((expected_cursor, trigger_ref))
        if job is None:
            return None
        try:
            async with asyncio.timeout(timeout_seconds):
                trace = await asyncio.wrap_future(job.future)
        except Exception:
            job.cancel()
            return None
        if trace is None:
            return None
        audit = verify_trusted_recall_trace(trace)
        if audit.trigger_ref != trigger_ref:
            return None
        return trace

    def take_ready_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
    ) -> TrustedRecallTrace | None:
        """Take an already-finished candidate set without waiting at all.

        This is the automatic half of dual-channel recall.  A candidate set
        that lost the race with the first model call remains available for the
        optional character pull, while a completed set can be placed in the
        first call and audited as material the character actually saw.
        """

        key = (expected_cursor, trigger_ref)
        job = self._prefetch_futures.get(key)
        if job is None or not job.future.done():
            return None
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
        return trace

    async def await_scheduled_prefetch(
        self,
        *,
        expected_cursor: RecallCursor,
        trigger_ref: str,
        timeout_seconds: float = _PREFETCH_FIRST_PASS_JOIN_SECONDS,
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

        key = (expected_cursor, trigger_ref)
        job = self._prefetch_futures.get(key)
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
            if key in self._prefetch_join_attempted:
                return job.local_fallback
            self._prefetch_join_attempted.add(key)
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
                return job.local_fallback
            except Exception:
                self._prefetch_futures.pop(key, None)
                self._prefetch_join_attempted.discard(key)
                job.cancel()
                logger.warning(
                    "recall prefetch search failed: trigger=%s", trigger_ref, exc_info=True
                )
                return job.local_fallback
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
        return trace

    def discard_scheduled_prefetch(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None = None,
    ) -> None:
        keys = tuple(
            key
            for key in self._prefetch_futures
            if key[0] == cursor and (trigger_ref is None or key[1] == trigger_ref)
        )
        for key in keys:
            self._prefetch_futures.pop(key).cancel()
            self._prefetch_join_attempted.discard(key)

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
            if not future.done() and not cancelled.is_set():
                future.set_result(trace)
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
                            {
                                channel
                                for hit in trace.audit.hits
                                for channel in hit.match_channels
                            }
                        )
                    )
                    if trace is not None
                    else ()
                ),
                embedding_status=(
                    trace.audit.embedding_status if trace is not None else "unknown"
                ),
                fallback_channels=(
                    tuple(
                        (
                            "lexical",
                            *(
                                ("structured",)
                                if trace.audit.request.link_refs
                                else ()
                            ),
                            *(
                                ("temporal",)
                                if trace.audit.request.occurred_from is not None
                                or trace.audit.request.occurred_to is not None
                                else ()
                            ),
                        )
                    )
                    if trace is not None
                    and status in {"degraded", "technical_failure"}
                    else ()
                ),
            )

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
            semantic_index = InMemoryRecallIndex(embedding=self._semantic_embedding)
            semantic_index.rebuild(
                cursor=context.snapshot.cursor,
                documents=context.snapshot.documents,
            )
            return semantic_index.search(query)
        except (RecallEmbeddingUnavailable, ValueError) as exc:
            # Embedding is an accessibility channel, not factual authority and
            # not a prerequisite for speaking. A provider/contract failure
            # therefore falls back to the already-pinned local index while the
            # trace and health surface retain the exact degraded reason.
            logger.warning("semantic recall degraded to local index", exc_info=True)
            local = context.snapshot.search(query)
            return local.model_copy(
                update={
                    "embedding_status": "degraded",
                    "embedding_failure_code": (f"{type(exc).__name__}:{str(exc)}"[:128]),
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
            self.discard_scheduled_prefetch(
                evicted_key[0],
                trigger_ref=evicted_key[1],
            )

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
        jobs = tuple(self._prefetch_futures.values())
        for job in jobs:
            job.cancel()
        self._prefetch_futures.clear()
        self._prefetch_join_attempted.clear()
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
    "augment_model_content_with_recall",
    "CharacterRecallRequest",
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
