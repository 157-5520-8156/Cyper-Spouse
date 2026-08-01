"""Bounded, caller-owned serial failover for semantic source review."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import hashlib
import json
import math
from threading import Lock
import time
from typing import Any, Callable, Literal


_LaneName = Literal["primary", "secondary"]
# Public expression-adapter bound shared with the composition root. The
# authority receives the same bound and finishes slightly earlier so its own
# ``validation_attempts_exhausted`` terminal state cannot be replaced by an
# enclosing ``asyncio.wait_for`` cancellation.
SOURCE_REVIEW_CALL_TIMEOUT_SECONDS = 22.0
_CALLER_TERMINAL_RESERVE_FRACTION = 0.1
_CALLER_TERMINAL_RESERVE_MAX_SECONDS = 0.5
SOURCE_REVIEW_TECHNICAL_FAILURE_COOLDOWN_SECONDS = 600.0
_SOURCE_REVIEW_CLOSE_GRACE_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class SourceReviewAttemptTrace:
    """Exact provider invocation evidence for one authority lane.

    The trace is attached to the returned text (or the terminal exhaustion)
    so the caller can commit one immutable ``ModelResultRecorded`` per actual
    provider invocation.  It intentionally contains no prompt or response
    text.
    """

    lane: _LaneName
    model_call_id: str
    request_hash: str
    model_id: str
    model_version: str
    outcome: Literal["winner", "timeout", "exception"]
    response_hash: str | None = None
    usage: object | None = None
    # Stable, provider-text-free cause for failed leaf calls.  The outer
    # authority still exposes source_review_exception/timeout as its aggregate
    # retry category; this field preserves which transport actually failed.
    failure_code: str | None = None


class AuditedSourceReviewText(str):
    """String-compatible reviewer bytes carrying exact lane attempts."""

    source_review_attempts: tuple[SourceReviewAttemptTrace, ...]

    def __new__(
        cls,
        value: str,
        attempts: tuple[SourceReviewAttemptTrace, ...],
    ) -> "AuditedSourceReviewText":
        instance = super().__new__(cls, value)
        instance.source_review_attempts = attempts
        return instance


class SourceReviewAttemptsExhausted(RuntimeError):
    """Both configured source-review lanes ended without usable bytes."""

    validation_attempts_exhausted = True

    def __init__(
        self,
        lane_failures: dict[_LaneName, str],
        *,
        model_call_id: str | None = None,
        request_hash: str | None = None,
        attempted_model_id: str | None = None,
        attempted_model_version: str | None = None,
        source_review_attempts: tuple[SourceReviewAttemptTrace, ...] = (),
    ) -> None:
        self.lane_failures = dict(lane_failures)
        self.model_call_id = model_call_id
        self.request_hash = request_hash
        self.attempted_model_id = attempted_model_id
        self.attempted_model_version = attempted_model_version
        self.source_review_attempts = source_review_attempts
        self.usage = None
        self.failure_code: Literal[
            "source_review_timeout", "source_review_exception"
        ] = (
            "source_review_timeout"
            if "provider_timeout" in self.lane_failures.values()
            else "source_review_exception"
        )
        super().__init__(
            "source review validation attempts exhausted: "
            + ", ".join(
                f"{lane}={reason}" for lane, reason in self.lane_failures.items()
            )
        )


class InventoryAvailabilityExhausted(SourceReviewAttemptsExhausted):
    """Every qualified Inventory transport is unavailable for this attempt.

    This type carries no semantic verdict.  It lets the expression adapter
    distinguish availability loss in the optional Inventory optimization from
    a Coverage or full source-review failure and activate the established
    strict ``source-closure-review.7`` boundary without regenerating wording.
    """


class SourceReviewAuthority:
    """Run a primary reviewer, then a reserve only after terminal failure."""

    def __init__(
        self,
        *,
        primary: object,
        secondary: object,
        hedge_after_seconds: float,
        deadline_seconds: float,
        caller_timeout_seconds: float | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        technical_failure_cooldown_seconds: float = (
            SOURCE_REVIEW_TECHNICAL_FAILURE_COOLDOWN_SECONDS
        ),
    ) -> None:
        if not math.isfinite(hedge_after_seconds) or hedge_after_seconds < 0:
            raise ValueError("hedge_after_seconds must be non-negative")
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be finite and positive")
        if caller_timeout_seconds is not None and (
            not math.isfinite(caller_timeout_seconds)
            or caller_timeout_seconds <= 0
        ):
            raise ValueError("caller_timeout_seconds must be finite and positive")
        if (
            not math.isfinite(technical_failure_cooldown_seconds)
            or technical_failure_cooldown_seconds < 0
        ):
            raise ValueError(
                "technical_failure_cooldown_seconds must be finite and non-negative"
            )
        terminal_reserve_seconds = (
            min(
                _CALLER_TERMINAL_RESERVE_MAX_SECONDS,
                caller_timeout_seconds * _CALLER_TERMINAL_RESERVE_FRACTION,
            )
            if caller_timeout_seconds is not None
            else 0.0
        )
        effective_deadline_seconds = min(
            deadline_seconds,
            (
                caller_timeout_seconds - terminal_reserve_seconds
                if caller_timeout_seconds is not None
                else deadline_seconds
            ),
        )
        if effective_deadline_seconds <= hedge_after_seconds:
            raise ValueError("deadline_seconds must be greater than hedge_after_seconds")
        self.primary = primary
        self.secondary = secondary
        self.hedge_after_seconds = float(hedge_after_seconds)
        self.configured_deadline_seconds = float(deadline_seconds)
        self.caller_timeout_seconds = (
            float(caller_timeout_seconds)
            if caller_timeout_seconds is not None
            else None
        )
        self.terminal_reserve_seconds = terminal_reserve_seconds
        self.deadline_seconds = float(effective_deadline_seconds)
        primary_model = str(getattr(primary, "model", type(primary).__name__))
        secondary_model = str(getattr(secondary, "model", type(secondary).__name__))
        self.model = f"source-review-authority:{primary_model}|{secondary_model}"
        self.provider = "source-review-authority"
        self._lane_models = {
            "primary": primary_model,
            "secondary": secondary_model,
        }
        self._lane_providers = {
            "primary": str(getattr(primary, "provider", "unknown")),
            "secondary": str(getattr(secondary, "provider", "unknown")),
        }
        self._health_lock = Lock()
        self._last_winner_lane: str | None = None
        self._hedges_started = 0
        self._hedges_won = 0
        self._all_lanes_failed = 0
        self._billing_unknown = 0
        self._lane_failures: dict[_LaneName, int] = {
            "primary": 0,
            "secondary": 0,
        }
        self._last_lane_failure_reasons: dict[_LaneName, str | None] = {
            "primary": None,
            "secondary": None,
        }
        self._monotonic_clock = monotonic_clock
        self._technical_failure_cooldown_seconds = float(
            technical_failure_cooldown_seconds
        )
        self._route_suppressed_until: dict[_LaneName, float] = {
            "primary": 0.0,
            "secondary": 0.0,
        }
        self._route_suppression_reason: dict[_LaneName, str | None] = {
            "primary": None,
            "secondary": None,
        }
        self._route_skipped_calls: dict[_LaneName, int] = {
            "primary": 0,
            "secondary": 0,
        }
        # Provider transports occasionally suppress cancellation while they
        # finish their own socket cleanup.  Keep those calls owned until the
        # coroutine really exits so a composition cannot close the shared
        # reviewer client underneath late cleanup.
        self._task_lock = Lock()
        self._provider_tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    def supports_strict_output_contract(self, contract: str) -> bool:
        """Return true only when either possible winning lane is schema-safe."""

        return self._lane_supports_strict_output_contract(
            self.primary,
            contract,
        ) and self._lane_supports_strict_output_contract(
            self.secondary,
            contract,
        )

    @staticmethod
    def _lane_supports_strict_output_contract(
        lane: object,
        contract: str,
    ) -> bool:
        try:
            checker = getattr(lane, "supports_strict_output_contract", None)
            return callable(checker) and checker(contract) is True
        except Exception:
            # Capability discovery happens before any provider call. A broken
            # declaration cannot safely prove that this lane enforces schema.
            return False

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        result = await self._race(
            method_name="complete",
            messages=messages,
            temperature=temperature,
            metered=False,
            first_lane="primary",
            allow_hedge=True,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered authority race returned usage")
        return result

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        result = await self._race(
            method_name="complete_json",
            messages=messages,
            temperature=temperature,
            metered=False,
            first_lane="primary",
            allow_hedge=True,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered JSON authority race returned usage")
        return result

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        result = await self._race(
            method_name="complete_with_usage",
            messages=messages,
            temperature=temperature,
            metered=True,
            first_lane="primary",
            allow_hedge=True,
        )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
        ):
            raise AssertionError("metered authority race returned an invalid result")
        return result

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        result = await self._race(
            method_name="complete_json_with_usage",
            messages=messages,
            temperature=temperature,
            metered=True,
            first_lane="primary",
            allow_hedge=True,
        )
        if (
            not isinstance(result, tuple)
            or len(result) != 2
            or not isinstance(result[0], str)
        ):
            raise AssertionError("metered authority race returned an invalid result")
        return result

    async def _race(
        self,
        *,
        method_name: str,
        messages: list[dict[str, str]],
        temperature: float,
        metered: bool,
        first_lane: _LaneName,
        allow_hedge: bool,
    ) -> str | tuple[str, object]:
        with self._task_lock:
            if self._closed:
                raise RuntimeError("source review authority is closed")
        loop = asyncio.get_running_loop()
        deadline_at = loop.time() + self.deadline_seconds
        fallback_lane: _LaneName = (
            "secondary" if first_lane == "primary" else "primary"
        )
        failures: dict[_LaneName, str] = {}
        attempt_traces: list[SourceReviewAttemptTrace] = []
        active: asyncio.Task[str | tuple[str, object]] | None = None
        active_lane: _LaneName | None = None
        active_identity: tuple[str, str, str, str] | None = None
        lanes = (first_lane, fallback_lane) if allow_hedge else (first_lane,)
        last_attempt_identity: tuple[str, str, str, str] | None = None

        try:
            for ordinal, lane in enumerate(lanes):
                remaining = deadline_at - loop.time()
                if remaining <= 0:
                    break
                preflight_failure = self._lane_preflight_failure(lane)
                if preflight_failure is not None:
                    # A skipped route is not a provider invocation and must
                    # not create a fabricated sub-call audit.  Retain the
                    # reason only on the aggregate availability terminal.
                    failures[lane] = preflight_failure
                    continue
                # The old "hedge" setting is now the primary lane's explicit
                # attempt timeout. Crossing it terminates that call before a
                # fallback is created; a healthy primary therefore never
                # speculatively starts or bills a second reviewer.
                timeout = self._lane_attempt_timeout_seconds(
                    lane=lane,
                    ordinal=ordinal,
                    remaining_seconds=remaining,
                    allow_hedge=allow_hedge,
                )
                if timeout <= 0:
                    failures[lane] = "provider_timeout"
                    self._record_lane_failure(
                        lane,
                        reason="provider_timeout",
                        billing_unknown=False,
                    )
                    continue
                if ordinal == 1:
                    with self._health_lock:
                        # Retained as backward-compatible health field names;
                        # these now count serial fallbacks, never overlapping
                        # hedged calls.
                        self._hedges_started += 1
                model = self.primary if lane == "primary" else self.secondary
                last_attempt_identity = self._provider_attempt_identity(
                    lane=lane,
                    model=model,
                    method_name=method_name,
                    messages=messages,
                    temperature=temperature,
                )
                (
                    attempt_model_call_id,
                    attempt_request_hash,
                    attempt_model_id,
                    attempt_model_version,
                ) = last_attempt_identity
                active_lane = lane
                active_identity = last_attempt_identity
                active = self._create_provider_task(
                    self._invoke(
                        model,
                        method_name=method_name,
                        messages=messages,
                        temperature=temperature,
                        metered=metered,
                    )
                )
                done, _pending = await asyncio.wait((active,), timeout=timeout)
                if not done:
                    active.cancel("provider_timeout")
                    failures[lane] = "provider_timeout"
                    self._record_lane_failure(
                        lane,
                        reason="provider_timeout",
                        billing_unknown=True,
                    )
                    self._after_lane_failure(lane, "provider_timeout")
                    attempt_traces.append(
                        SourceReviewAttemptTrace(
                            lane=lane,
                            model_call_id=attempt_model_call_id,
                            request_hash=attempt_request_hash,
                            model_id=attempt_model_id,
                            model_version=attempt_model_version,
                            outcome="timeout",
                            failure_code="provider_timeout",
                        )
                    )
                    # A reserve call must never overlap a provider task that
                    # ignored cancellation. Give ordinary cancellation one
                    # small bounded drain; if the task remains alive, end this
                    # authority attempt without starting the second lane.
                    cancellation_done, _pending = await asyncio.wait(
                        (active,),
                        timeout=min(0.1, max(0.0, deadline_at - loop.time())),
                    )
                    if not cancellation_done:
                        active = None
                        active_lane = None
                        active_identity = None
                        break
                    self._consume_task_result(active)
                    active = None
                    active_lane = None
                    active_identity = None
                    continue
                try:
                    result = active.result()
                except BaseException as exc:
                    reason = self._failure_reason(exc)
                    failures[lane] = reason
                    self._record_lane_failure(
                        lane,
                        reason=reason,
                        billing_unknown=True,
                    )
                    self._after_lane_failure(lane, reason)
                    attempt_traces.append(
                        SourceReviewAttemptTrace(
                            lane=lane,
                            model_call_id=attempt_model_call_id,
                            request_hash=attempt_request_hash,
                            model_id=attempt_model_id,
                            model_version=attempt_model_version,
                            outcome="exception",
                            failure_code=reason,
                        )
                    )
                    active = None
                    active_lane = None
                    active_identity = None
                    continue
                active = None
                active_lane = None
                active_identity = None
                with self._health_lock:
                    self._last_winner_lane = lane
                    if ordinal == 1:
                        self._hedges_won += 1
                self._after_lane_success(lane)
                raw = result[0] if isinstance(result, tuple) else result
                usage = result[1] if isinstance(result, tuple) else None
                attempt_traces.append(
                    SourceReviewAttemptTrace(
                        lane=lane,
                        model_call_id=attempt_model_call_id,
                        request_hash=attempt_request_hash,
                        model_id=attempt_model_id,
                        model_version=attempt_model_version,
                        outcome="winner",
                        response_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        usage=usage,
                    )
                )
                audited_raw = AuditedSourceReviewText(raw, tuple(attempt_traces))
                return (
                    (audited_raw, result[1])
                    if isinstance(result, tuple)
                    else audited_raw
                )
            with self._health_lock:
                self._all_lanes_failed += 1
            raise self._exhausted_exception(
                failures,
                model_call_id=(
                    last_attempt_identity[0]
                    if last_attempt_identity is not None
                    else None
                ),
                request_hash=(
                    last_attempt_identity[1]
                    if last_attempt_identity is not None
                    else None
                ),
                attempted_model_id=(
                    last_attempt_identity[2]
                    if last_attempt_identity is not None
                    else None
                ),
                attempted_model_version=(
                    last_attempt_identity[3]
                    if last_attempt_identity is not None
                    else None
                ),
                source_review_attempts=tuple(attempt_traces),
            )
        except asyncio.CancelledError as exc:
            if (
                active is not None
                and active_lane is not None
                and active_identity is not None
                and all(
                    trace.model_call_id != active_identity[0]
                    for trace in attempt_traces
                )
            ):
                (
                    attempt_model_call_id,
                    attempt_request_hash,
                    attempt_model_id,
                    attempt_model_version,
                ) = active_identity
                self._record_lane_failure(
                    active_lane,
                    reason="caller_cancelled",
                    billing_unknown=True,
                )
                attempt_traces.append(
                    SourceReviewAttemptTrace(
                        lane=active_lane,
                        model_call_id=attempt_model_call_id,
                        request_hash=attempt_request_hash,
                        model_id=attempt_model_id,
                        model_version=attempt_model_version,
                        # The durable provider-subcall taxonomy uses timeout for
                        # a call cancelled by its enclosing attempt deadline.
                        outcome="timeout",
                        failure_code="caller_cancelled",
                    )
                )
            exc.source_review_attempts = tuple(attempt_traces)
            if active is not None and not active.done():
                active.cancel("caller_cancelled")
            raise

    def _create_provider_task(
        self,
        awaitable: Awaitable[str | tuple[str, object]],
    ) -> asyncio.Task[str | tuple[str, object]]:
        """Create one process-owned provider call behind the close gate."""

        task = asyncio.create_task(awaitable)
        close_raced = False
        with self._task_lock:
            if self._closed:
                close_raced = True
            else:
                self._provider_tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(self._finish_provider_task)
        if close_raced:
            task.cancel("authority_closed")
            raise RuntimeError("source review authority is closed")
        return task

    def _finish_provider_task(self, task: asyncio.Task[object]) -> None:
        """Observe one provider result and release its dependency lease."""

        with self._task_lock:
            self._provider_tasks.discard(task)
        self._consume_task_result(task)

    def _pending_provider_tasks(self) -> tuple[asyncio.Task[object], ...]:
        with self._task_lock:
            return tuple(task for task in self._provider_tasks if not task.done())

    async def aclose(self) -> None:
        """Bound shutdown while retaining late reviewer dependency leases."""

        with self._task_lock:
            self._closed = True
            tasks = tuple(task for task in self._provider_tasks if not task.done())
        if not tasks:
            return
        current_loop = asyncio.get_running_loop()
        local_tasks: list[asyncio.Task[object]] = []
        for task in tasks:
            if task.cancelling() == 0:
                if task.get_loop() is current_loop:
                    task.cancel("authority_close")
                else:
                    task.get_loop().call_soon_threadsafe(
                        task.cancel,
                        "authority_close",
                    )
            if task.get_loop() is current_loop:
                local_tasks.append(task)
        if local_tasks:
            await asyncio.wait(
                local_tasks,
                timeout=_SOURCE_REVIEW_CLOSE_GRACE_SECONDS,
            )
        # Foreign-loop calls are possible for offline threaded audits.  They
        # remain visible through the lease instead of making this loop wait
        # on an incompatible Future.

    @property
    def shutdown_pending_task_count(self) -> int:
        """Provider calls still retaining reviewer/client dependencies."""

        return len(self._pending_provider_tasks())

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait for every detached reviewer without owning its cancellation."""

        while self._pending_provider_tasks():
            # Polling also supports the documented offline multi-thread use;
            # shielding a Task owned by another event loop is invalid.
            await asyncio.sleep(0.01)

    def _lane_attempt_timeout_seconds(
        self,
        *,
        lane: _LaneName,
        ordinal: int,
        remaining_seconds: float,
        allow_hedge: bool,
    ) -> float:
        del lane
        return (
            min(self.hedge_after_seconds, remaining_seconds)
            if allow_hedge and ordinal == 0
            else remaining_seconds
        )

    def _lane_preflight_failure(self, lane: _LaneName) -> str | None:
        now = self._monotonic_clock()
        with self._health_lock:
            retry_at = self._route_suppressed_until[lane]
            reason = self._route_suppression_reason[lane]
            if retry_at <= now:
                return None
            self._route_skipped_calls[lane] += 1
        return f"route_suppressed:{reason or 'technical_failure'}"

    def _after_lane_failure(self, lane: _LaneName, reason: str) -> None:
        category = reason.split(":", 1)[0]
        direct_transport_timeout = (
            category == "TimeoutError" or category.endswith("Timeout")
        )
        route_rejected = reason.endswith(":http_403")
        if (
            reason != "provider_timeout"
            and not direct_transport_timeout
            and not category.endswith("CircuitOpenError")
            and not route_rejected
        ):
            return
        suppression_reason = "http_403" if route_rejected else reason
        with self._health_lock:
            self._route_suppression_reason[lane] = suppression_reason
            self._route_suppressed_until[lane] = (
                self._monotonic_clock()
                + self._technical_failure_cooldown_seconds
            )

    def _after_lane_success(self, lane: _LaneName) -> None:
        with self._health_lock:
            self._route_suppressed_until[lane] = 0.0
            self._route_suppression_reason[lane] = None

    @staticmethod
    def _exhausted_exception(
        lane_failures: dict[_LaneName, str],
        **kwargs: object,
    ) -> SourceReviewAttemptsExhausted:
        return SourceReviewAttemptsExhausted(lane_failures, **kwargs)

    def wire_reselection_route(self) -> "_SourceReviewWireReselectionRoute":
        """Return a view that tries the other independent lane first.

        A malformed reviewer wire is not a semantic verdict. The caller may
        ask once for the exact same review contract again; this view ensures
        that retry does not deterministically select the same faster lane.
        """

        return _SourceReviewWireReselectionRoute(self)

    @staticmethod
    async def _invoke(
        lane: object,
        *,
        method_name: str,
        messages: list[dict[str, str]],
        temperature: float,
        metered: bool,
    ) -> str | tuple[str, object]:
        complete = getattr(lane, method_name, None)
        if not callable(complete) and method_name == "complete_json":
            complete = getattr(lane, "complete", None)
        elif not callable(complete) and method_name == "complete_json_with_usage":
            complete = getattr(lane, "complete_with_usage", None)
        if not callable(complete):
            raise TypeError(f"reviewer does not expose {method_name}")
        lane_messages = [dict(message) for message in messages]
        result = await complete(lane_messages, temperature=temperature)
        if metered:
            if (
                not isinstance(result, tuple)
                or len(result) != 2
                or not isinstance(result[0], str)
                or result[1] is None
            ):
                raise ValueError("metered reviewer result must be (raw, usage)")
            return result
        if not isinstance(result, str):
            raise ValueError("reviewer result must be text")
        return result

    def _record_lane_failure(
        self,
        lane: _LaneName,
        *,
        reason: str,
        billing_unknown: bool,
    ) -> None:
        with self._health_lock:
            self._lane_failures[lane] += 1
            self._last_lane_failure_reasons[lane] = reason
            if billing_unknown:
                self._billing_unknown += 1

    @staticmethod
    def _provider_attempt_identity(
        *,
        lane: _LaneName,
        model: object,
        method_name: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> tuple[str, str, str, str]:
        request_hash = hashlib.sha256(
            json.dumps(
                {"messages": messages, "temperature": temperature},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        model_id = str(getattr(model, "model", "")).strip() or type(model).__name__
        model_version = (
            str(getattr(model, "VERSION", "")).strip()
            or type(model).__name__
        )
        call_hash = hashlib.sha256(
            json.dumps(
                {
                    "contract": "source-review-provider-attempt.1",
                    "lane": lane,
                    "method": method_name,
                    "model": model_id,
                    "request_hash": request_hash,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return (
            f"model-call:source-review:{call_hash}",
            request_hash,
            model_id,
            model_version,
        )

    @staticmethod
    def _failure_reason(exc: BaseException) -> str:
        """Return a stable public category without provider-controlled text."""

        error_type = type(exc).__name__
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        try:
            normalized_status = int(status_code)
        except (TypeError, ValueError):
            normalized_status = 0
        if 100 <= normalized_status <= 599:
            return f"{error_type}:http_{normalized_status}"
        return error_type

    @staticmethod
    def _consume_task_result(task: asyncio.Future[object]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    def health_snapshot(self) -> dict[str, Any]:
        lane_capability_evidence: dict[str, object | None] = {}
        for lane, model in (("primary", self.primary), ("secondary", self.secondary)):
            reader = getattr(model, "strict_output_capability_snapshot", None)
            try:
                lane_capability_evidence[lane] = reader() if callable(reader) else None
            except Exception:
                lane_capability_evidence[lane] = None
        now = self._monotonic_clock()
        with self._health_lock:
            return {
                "configured_lanes": ("primary", "secondary"),
                "lane_models": dict(self._lane_models),
                "lane_providers": dict(self._lane_providers),
                "lane_capability_evidence": lane_capability_evidence,
                "hedge_after_seconds": self.hedge_after_seconds,
                "review_strategy": "serial_failover",
                "primary_attempt_timeout_seconds": self.hedge_after_seconds,
                "configured_absolute_timeout_seconds": (
                    self.configured_deadline_seconds
                ),
                "absolute_timeout_seconds": self.deadline_seconds,
                "caller_timeout_seconds": self.caller_timeout_seconds,
                "terminal_completion_reserve_seconds": (
                    self.terminal_reserve_seconds
                ),
                "last_winner_lane": self._last_winner_lane,
                "hedges_started": self._hedges_started,
                "hedges_won": self._hedges_won,
                "all_lanes_failed": self._all_lanes_failed,
                "billing_unknown": self._billing_unknown,
                "lane_failures": dict(self._lane_failures),
                "last_lane_failure_reasons": dict(
                    self._last_lane_failure_reasons
                ),
                "technical_failure_cooldown_seconds": (
                    self._technical_failure_cooldown_seconds
                ),
                "route_suppression": {
                    lane: {
                        "active": self._route_suppressed_until[lane] > now,
                        "reason": self._route_suppression_reason[lane],
                        "retry_after_seconds": max(
                            0.0,
                            self._route_suppressed_until[lane] - now,
                        ),
                        "skipped_calls": self._route_skipped_calls[lane],
                    }
                    for lane in ("primary", "secondary")
                },
            }


class _SourceReviewWireReselectionRoute:
    """Secondary-first, caller-owned view over one authority instance."""

    def __init__(self, authority: SourceReviewAuthority) -> None:
        self._authority = authority
        self.primary = authority.secondary
        self.secondary = authority.primary
        self.model = f"{authority.model}:wire-reselection"
        self.provider = authority.provider

    def supports_strict_output_contract(self, contract: str) -> bool:
        return self._authority.supports_strict_output_contract(contract)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        result = await self._authority._race(
            method_name="complete",
            messages=messages,
            temperature=temperature,
            metered=False,
            first_lane="secondary",
            allow_hedge=False,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered authority reselection returned usage")
        return result

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        result = await self._authority._race(
            method_name="complete_json",
            messages=messages,
            temperature=temperature,
            metered=False,
            first_lane="secondary",
            allow_hedge=False,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered authority JSON reselection returned usage")
        return result

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        result = await self._authority._race(
            method_name="complete_with_usage",
            messages=messages,
            temperature=temperature,
            metered=True,
            first_lane="secondary",
            allow_hedge=False,
        )
        if not isinstance(result, tuple):
            raise AssertionError("metered authority reselection omitted usage")
        return result

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        result = await self._authority._race(
            method_name="complete_json_with_usage",
            messages=messages,
            temperature=temperature,
            metered=True,
            first_lane="secondary",
            allow_hedge=False,
        )
        if not isinstance(result, tuple):
            raise AssertionError("metered authority JSON reselection omitted usage")
        return result
