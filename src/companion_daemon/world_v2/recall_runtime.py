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
import secrets
import threading

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
    RecallIndex,
    RecallIndexSnapshot,
    RecallQuery,
    RecallResult,
)
from .schema_core import FrozenModel


_TRACE_AUTHORITY_KEY = secrets.token_bytes(32)
_MAX_PINNED_RECALL_CONTEXTS = 16
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
    future: Future[TrustedRecallTrace]
    cancelled: threading.Event
    thread: threading.Thread

    def cancel(self) -> None:
        self.cancelled.set()
        self.future.cancel()


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
        self._context_key: _RecallContextKey | None = None
        self._contexts: OrderedDict[_RecallContextKey, _PinnedRecallContext] = OrderedDict()
        self._prefetch_slots = threading.BoundedSemaphore(value=4)
        self._prefetch_futures: OrderedDict[_RecallPrefetchKey, _PrefetchJob] = OrderedDict()

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
        if self._semantic_embedding is None:
            return {"enabled": False}
        snapshot = getattr(self._semantic_embedding, "health_snapshot", None)
        if callable(snapshot):
            return dict(snapshot())
        return {
            "enabled": True,
            "embedding_version": self._semantic_embedding.version,
        }

    def is_available(
        self,
        cursor: RecallCursor,
        *,
        trigger_ref: str | None = None,
    ) -> bool:
        return self._context_for(cursor, trigger_ref=trigger_ref) is not None

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
        request = CharacterRecallRequest(query_text=query_text, limit=min(limit, 6))
        result = self._search(
            context=context,
            request=request,
            accessibility_seed=accessibility_seed,
            semantic=False,
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
        accessibility_seed: str,
        trigger_ref: str,
        limit: int = 4,
    ) -> None:
        """Start bounded local attention search without delaying the first model call."""

        context_key = self._context_key
        if context_key is None:
            raise ValueError("recall corpus has not been refreshed")
        cursor = context_key[0]
        context = self._context_for(cursor, trigger_ref=trigger_ref)
        if context is None:
            raise ValueError("prefetch trigger does not match the pinned Context")
        prefetch_key = (cursor, trigger_ref)
        request = CharacterRecallRequest(query_text=query_text, limit=min(limit, 6))
        previous = self._prefetch_futures.pop(prefetch_key, None)
        if previous is not None:
            previous.cancel()
        if not self._prefetch_slots.acquire(blocking=False):
            return
        future: Future[TrustedRecallTrace] = Future()
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
            },
            name="world-v2-recall-prefetch",
            daemon=True,
        )
        job = _PrefetchJob(
            future=future,
            cancelled=cancelled,
            thread=thread,
        )
        self._prefetch_futures[prefetch_key] = job
        thread.start()
        while len(self._prefetch_futures) > _MAX_PINNED_RECALL_CONTEXTS:
            _, evicted = self._prefetch_futures.popitem(last=False)
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
        if job is None:
            return None
        try:
            async with asyncio.timeout(timeout_seconds):
                trace = await asyncio.wrap_future(job.future)
        except Exception:
            job.cancel()
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
        try:
            trace = job.future.result()
            audit = verify_trusted_recall_trace(trace)
        except Exception:
            job.cancel()
            return None
        if audit.trigger_ref != trigger_ref or audit.evaluated_cursor != expected_cursor:
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

    def recall(
        self,
        *,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        expected_cursor: RecallCursor,
        trigger_ref: str,
    ) -> TrustedRecallTrace:
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
        result = self._search(
            context=context,
            request=request,
            accessibility_seed=accessibility_seed,
            semantic=False,
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
        future: Future[TrustedRecallTrace],
        cancelled: threading.Event,
        context: _PinnedRecallContext,
        request: CharacterRecallRequest,
        accessibility_seed: str,
        trigger_ref: str,
        evaluated_cursor: RecallCursor,
    ) -> None:
        try:
            if not future.set_running_or_notify_cancel():
                return
            trace = self._prefetch_context(
                context=context,
                request=request,
                accessibility_seed=accessibility_seed,
                trigger_ref=trigger_ref,
                evaluated_cursor=evaluated_cursor,
            )
            if not future.done():
                future.set_result(trace)
        except BaseException as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            cancelled.set()
            self._prefetch_slots.release()

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
        semantic = InMemoryRecallIndex(embedding=self._semantic_embedding)
        semantic.rebuild(
            cursor=context.snapshot.cursor,
            documents=context.snapshot.documents,
        )
        return semantic.search(query)

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
        if self._closed:
            return
        self._closed = True
        for job in self._prefetch_futures.values():
            job.cancel()
        self._prefetch_futures.clear()
        close_embedding = getattr(self._semantic_embedding, "close", None)
        if callable(close_embedding):
            close_embedding()


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
