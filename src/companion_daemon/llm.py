import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
from threading import Lock
from time import monotonic, time
from typing import Protocol, TypeVar

import httpx

from companion_daemon.model_call_policy import ProviderCircuitState


_T = TypeVar("_T")
logger = logging.getLogger(__name__)


_MODEL_CALL_PURPOSE: ContextVar[str] = ContextVar("model_call_purpose", default="unclassified")
_MODEL_CALL_META: ContextVar[dict[str, object]] = ContextVar("model_call_meta", default={})
_MODEL_CALL_STATE: ContextVar["ModelCallScopeState | None"] = ContextVar(
    "model_call_state", default=None
)
_MODEL_REQUEST_EMISSION_STATE: ContextVar[
    "ModelRequestEmissionScopeState | None"
] = ContextVar("model_request_emission_state", default=None)


@contextmanager
def model_turn_scope(*, world_id: str = "", turn_id: str = "", cadence: str = "") -> Iterator[None]:
    token = _MODEL_CALL_META.set(
        {
            **_MODEL_CALL_META.get(),
            "world_id": world_id,
            "turn_id": turn_id,
            "cadence": cadence,
        }
    )
    try:
        yield
    finally:
        _MODEL_CALL_META.reset(token)


@contextmanager
def model_call_scope(
    purpose: str,
    *,
    action_id: str = "",
    attempt: int = 1,
    budget_reservation_id: str = "",
) -> Iterator["ModelCallScopeState"]:
    # Background helpers may add a more specific purpose scope around an
    # already-reserved provider boundary.  Preserve its evidence instead of
    # creating an inner state that would be discarded before the reservation
    # is finalized.
    inherited_state = _MODEL_CALL_STATE.get()
    state = inherited_state or ModelCallScopeState()
    token = _MODEL_CALL_PURPOSE.set(purpose)
    state_token = None if inherited_state is not None else _MODEL_CALL_STATE.set(state)
    meta_token = _MODEL_CALL_META.set(
        {
            **_MODEL_CALL_META.get(),
            "action_id": action_id,
            "attempt": max(1, int(attempt)),
            "budget_reservation_id": budget_reservation_id,
        }
    )
    try:
        yield state
    finally:
        _MODEL_CALL_META.reset(meta_token)
        if state_token is not None:
            _MODEL_CALL_STATE.reset(state_token)
        _MODEL_CALL_PURPOSE.reset(token)


@dataclass(frozen=True)
class ModelCallUsage:
    purpose: str
    model: str
    status: str
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    total_tokens: int = 0
    provider: str = ""
    error: str = ""
    world_id: str = ""
    turn_id: str = ""
    action_id: str = ""
    cadence: str = ""
    attempt: int = 1
    budget_reservation_id: str = ""
    # The effective provider request mode, not an inferred token heuristic.
    # Flash can legitimately run with or without thinking, so model name alone
    # cannot support a truthful latency/cost baseline.
    thinking_enabled: bool = False
    reasoning_effort: str = ""
    # ``unknown`` means the provider may have accepted or charged the call.
    # Only a concrete local/provider rejection may use ``not_billed``.
    billing_state: str = "unknown"


@dataclass
class ModelCallScopeState:
    """Provider-boundary facts retained while the call scope remains active."""

    request_emitted: bool = False
    usage_persisted: bool | None = None


@dataclass
class ModelRequestEmissionScopeState:
    """Exact transport spans for every provider request in one role attempt."""

    provider_call_id: str
    entry_marker: Callable[[str], None] | None
    completion_marker: Callable[[str], None] | None
    emitted: bool = False
    _next_ordinal: int = 0
    _active_call_ids: set[str] = field(default_factory=set)
    _completed_call_ids: set[str] = field(default_factory=set)
    _lock: Lock = field(default_factory=Lock)

    def begin(self) -> "ModelRequestSpanToken":
        with self._lock:
            self._next_ordinal += 1
            ordinal = self._next_ordinal
            call_id = (
                self.provider_call_id
                if ordinal == 1
                else f"{self.provider_call_id}:provider-attempt:{ordinal}"
            )
            self._active_call_ids.add(call_id)
            self.emitted = True
        if self.entry_marker is not None:
            try:
                self.entry_marker(call_id)
            except Exception:
                logger.warning("model request emission marker failed", exc_info=True)
        return ModelRequestSpanToken(scope=self, provider_call_id=call_id)

    def complete(self, token: "ModelRequestSpanToken") -> None:
        with self._lock:
            call_id = token.provider_call_id
            if call_id not in self._active_call_ids:
                return
            self._active_call_ids.remove(call_id)
            if call_id in self._completed_call_ids:
                return
            self._completed_call_ids.add(call_id)
        if self.completion_marker is not None:
            try:
                self.completion_marker(call_id)
            except Exception:
                logger.warning("model request completion marker failed", exc_info=True)


@dataclass(frozen=True)
class ModelRequestSpanToken:
    """Identity needed to close the exact provider request that was opened."""

    scope: ModelRequestEmissionScopeState
    provider_call_id: str


@contextmanager
def model_request_emission_scope(
    *,
    provider_call_id: str,
    entry_marker: Callable[[str], None] | None,
    completion_marker: Callable[[str], None] | None,
) -> Iterator[ModelRequestEmissionScopeState]:
    """Observe every request when a provider reaches and leaves ``client.post``.

    The caller supplies the stable semantic call identity.  A failover or
    hedged provider request receives a deterministic ordinal suffix, so an
    exit can never be attributed to a different request.  Nested adapters
    reuse the deliberation-owned scope, allowing validation and source-review
    calls to remain in the same visible-turn timeline.

    Capacity admission, circuit checks and payload construction deliberately
    happen before this marker and therefore remain API-external latency.
    """

    inherited = _MODEL_REQUEST_EMISSION_STATE.get()
    if inherited is not None:
        yield inherited
        return
    if not provider_call_id:
        raise ValueError("model request emission scope requires a provider call id")
    if entry_marker is not None and not callable(entry_marker):
        raise TypeError("model request emission entry marker must be callable")
    if completion_marker is not None and not callable(completion_marker):
        raise TypeError("model request emission completion marker must be callable")
    state = ModelRequestEmissionScopeState(
        provider_call_id=provider_call_id,
        entry_marker=entry_marker,
        completion_marker=completion_marker,
    )
    token = _MODEL_REQUEST_EMISSION_STATE.set(state)
    try:
        yield state
    finally:
        _MODEL_REQUEST_EMISSION_STATE.reset(token)


def mark_model_request_emitted() -> ModelRequestSpanToken | None:
    """Record that the current provider request reached its transport seam."""

    state = _MODEL_CALL_STATE.get()
    if state is not None:
        state.request_emitted = True
    emission = _MODEL_REQUEST_EMISSION_STATE.get()
    if emission is None:
        return None
    return emission.begin()


def mark_model_request_completed(token: ModelRequestSpanToken | None) -> None:
    """Close the exact provider request opened at the transport seam."""

    if token is None:
        return
    token.scope.complete(token)


class ModelCircuitOpenError(ConnectionError):
    """Raised immediately while a model provider circuit is open."""


class ModelCapacityBusyError(ModelCircuitOpenError):
    """Raised before transport when a single-worker provider cannot admit work."""


@dataclass(frozen=True, slots=True)
class ProviderCapacityState:
    """Read-only admission evidence for health and incident diagnosis."""

    status: str
    admitted_calls: int
    rejected_active_calls: int
    rejected_cooldown_calls: int
    rejected_external_calls: int
    ambiguous_cancellations: int
    cooldown_remaining_seconds: float
    marker_path: str
    last_rejection_reason: str


class ProviderCapacityGate:
    """Non-queueing admission control for one serial inference worker.

    Client cancellation does not prove that an OpenAI-compatible server
    stopped generation.  The gate therefore keeps a conservative cooldown
    lease after any cancelled request.  An optional atomic marker directory
    extends the same active/busy signal to a watchdog process.  The marker is
    a bounded lease rather than a permanent lock, so a daemon crash cannot
    disable the provider forever.
    """

    def __init__(
        self,
        *,
        cooldown_seconds: float = 120.0,
        active_lease_seconds: float = 300.0,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
        marker_path: Path | None = None,
    ) -> None:
        if cooldown_seconds < 0 or cooldown_seconds > 3_600:
            raise ValueError("capacity cooldown must be in [0, 3600] seconds")
        if active_lease_seconds <= 0 or active_lease_seconds > 3_600:
            raise ValueError("capacity active lease must be in (0, 3600] seconds")
        if active_lease_seconds < cooldown_seconds:
            raise ValueError("capacity active lease must cover the cooldown")
        self.cooldown_seconds = float(cooldown_seconds)
        self.active_lease_seconds = float(active_lease_seconds)
        self.clock = clock
        self.wall_clock = wall_clock
        self.marker_path = marker_path
        self._lock = Lock()
        self._active_token: str | None = None
        self._cooldown_until: float | None = None
        self._ordinal = 0
        self._admitted_calls = 0
        self._rejected_active_calls = 0
        self._rejected_cooldown_calls = 0
        self._rejected_external_calls = 0
        self._ambiguous_cancellations = 0
        self._last_rejection_reason = ""

    def acquire(self) -> str:
        """Acquire the sole inference slot or reject without awaiting a queue."""

        with self._lock:
            now = self.clock()
            if self._active_token is not None:
                self._rejected_active_calls += 1
                self._last_rejection_reason = "in_flight"
                self._log_rejection("in_flight")
                raise ModelCapacityBusyError("model provider capacity has an in flight request")
            if self._cooldown_until is not None:
                if self._cooldown_until > now:
                    self._rejected_cooldown_calls += 1
                    self._last_rejection_reason = "cooldown"
                    self._log_rejection("cooldown")
                    raise ModelCapacityBusyError(
                        "model provider capacity is in post-cancellation cooldown"
                    )
                self._cooldown_until = None

            self._ordinal += 1
            token = f"daemon:{os.getpid()}:{self._ordinal}:{int(self.wall_clock() * 1000)}"
            marker_status = self._claim_marker(token)
            if marker_status is not None:
                self._rejected_external_calls += 1
                self._last_rejection_reason = marker_status
                self._log_rejection(marker_status)
                raise ModelCapacityBusyError(
                    f"model provider capacity is externally busy ({marker_status})"
                )
            self._active_token = token
            self._admitted_calls += 1
            return token

    def release(self, token: str) -> None:
        """Release a request that is known to have reached a terminal response."""

        with self._lock:
            if token != self._active_token:
                return
            self._active_token = None
            self._cooldown_until = None
            self._clear_marker(expected_token=token)

    def abandon(self, token: str, *, reason: str) -> None:
        """Retain a bounded busy lease when server-side completion is unknown."""

        with self._lock:
            if token != self._active_token:
                return
            self._active_token = None
            self._cooldown_until = self.clock() + self.cooldown_seconds
            self._ambiguous_cancellations += 1
            self._write_marker(
                token=token,
                status="cooldown",
                deadline=self.wall_clock() + self.cooldown_seconds,
            )
            logger.warning(
                "local_provider_capacity_cooldown reason=%s cooldown_seconds=%g "
                "ambiguous_cancellations=%d",
                reason,
                self.cooldown_seconds,
                self._ambiguous_cancellations,
            )

    def snapshot(self) -> ProviderCapacityState:
        with self._lock:
            now = self.clock()
            remaining = 0.0
            marker_problem: str | None = None
            if self._active_token is not None:
                status = "active"
            elif self._cooldown_until is not None and self._cooldown_until > now:
                status = "cooldown"
                remaining = self._cooldown_until - now
            else:
                marker, marker_problem = self._read_marker()
                if marker_problem is not None:
                    status = "degraded"
                elif marker is not None and marker[0] > self.wall_clock():
                    status = "external_busy"
                    remaining = marker[0] - self.wall_clock()
                else:
                    status = "idle"
            return ProviderCapacityState(
                status=status,
                admitted_calls=self._admitted_calls,
                rejected_active_calls=self._rejected_active_calls,
                rejected_cooldown_calls=self._rejected_cooldown_calls,
                rejected_external_calls=self._rejected_external_calls,
                ambiguous_cancellations=self._ambiguous_cancellations,
                cooldown_remaining_seconds=max(0.0, remaining),
                marker_path=str(self.marker_path or ""),
                last_rejection_reason=marker_problem or self._last_rejection_reason,
            )

    def health_snapshot(self) -> dict[str, object]:
        state = self.snapshot()
        return {
            "status": state.status,
            "admitted_calls": state.admitted_calls,
            "rejected_active_calls": state.rejected_active_calls,
            "rejected_cooldown_calls": state.rejected_cooldown_calls,
            "rejected_external_calls": state.rejected_external_calls,
            "ambiguous_cancellations": state.ambiguous_cancellations,
            "cooldown_remaining_seconds": round(state.cooldown_remaining_seconds, 3),
            "marker_path": state.marker_path,
            "last_rejection_reason": state.last_rejection_reason or None,
        }

    def _claim_marker(self, token: str) -> str | None:
        if self.marker_path is None:
            return None
        try:
            with self._marker_transaction():
                for _attempt in range(2):
                    try:
                        self.marker_path.mkdir(mode=0o700, parents=False, exist_ok=False)
                    except FileExistsError:
                        marker = self._read_marker_unlocked()
                        if marker is None:
                            stale = self._unreadable_marker_is_stale_unlocked()
                            if stale is None:
                                return "marker_unavailable"
                            if not stale:
                                # Another owner may have created the directory
                                # and not yet published its atomic state file.
                                return "marker_unreadable"
                            if not self._clear_marker_unlocked(expected_token=None):
                                return "marker_unavailable"
                            continue
                        deadline, owner, status = marker
                        if deadline > self.wall_clock():
                            return status or "external_busy"
                        # The stable sibling flock serializes the complete
                        # read/compare/delete/create transaction across daemon
                        # processes and the watchdog. No stale reader can
                        # unlink a newly installed owner between these steps.
                        self._clear_marker_unlocked(expected_token=owner)
                        continue
                    except OSError as exc:
                        logger.warning(
                            "local_provider_capacity_marker_unavailable error=%s",
                            type(exc).__name__,
                        )
                        return "marker_unavailable"
                    if self._write_marker_unlocked(
                        token=token,
                        status="active",
                        deadline=self.wall_clock() + self.active_lease_seconds,
                    ):
                        return None
                    # This process created the directory while holding the
                    # sibling flock, but a failed first state write leaves no
                    # token that owner-checked cleanup could match.
                    self._clear_marker_unlocked(expected_token=None)
                    return "marker_unavailable"
                return "external_busy"
        except OSError as exc:
            logger.warning(
                "local_provider_capacity_lock_unavailable error=%s",
                type(exc).__name__,
            )
            return "marker_unavailable"

    def _read_marker(
        self,
    ) -> tuple[tuple[float, str, str] | None, str | None]:
        if self.marker_path is None:
            return None, None
        try:
            with self._marker_transaction():
                try:
                    self.marker_path.stat()
                except FileNotFoundError:
                    return None, None
                except OSError:
                    return None, "marker_unavailable"
                marker = self._read_marker_unlocked()
                if marker is None:
                    return None, "marker_unreadable"
                return marker, None
        except OSError:
            return None, "marker_unavailable"

    def _read_marker_unlocked(self) -> tuple[float, str, str] | None:
        if self.marker_path is None or not self.marker_path.is_dir():
            return None
        try:
            lines = (self.marker_path / "state").read_text(encoding="utf-8").splitlines()
            if len(lines) < 3:
                return None
            deadline = float(lines[0])
            token = lines[1].strip()
            status = lines[2].strip()
            if not token or not status:
                return None
            return deadline, token, status
        except (OSError, ValueError):
            return None

    def _unreadable_marker_is_stale_unlocked(self) -> bool | None:
        """Bound recovery of a crash-partial marker by its active lease."""

        assert self.marker_path is not None
        try:
            modified_at = self.marker_path.stat().st_mtime
        except OSError:
            return None
        return self.wall_clock() - modified_at > self.active_lease_seconds

    def _write_marker(self, *, token: str, status: str, deadline: float) -> bool:
        if self.marker_path is None:
            return True
        try:
            with self._marker_transaction():
                return self._write_marker_unlocked(
                    token=token,
                    status=status,
                    deadline=deadline,
                )
        except OSError as exc:
            logger.warning(
                "local_provider_capacity_marker_write_failed error=%s",
                type(exc).__name__,
            )
            return False

    def _write_marker_unlocked(self, *, token: str, status: str, deadline: float) -> bool:
        assert self.marker_path is not None
        try:
            state = self.marker_path / "state"
            temporary = self.marker_path / f".state.{os.getpid()}.{self._ordinal}"
            temporary.write_text(
                f"{deadline:.6f}\n{token}\n{status}\n",
                encoding="utf-8",
            )
            temporary.replace(state)
            return True
        except OSError as exc:
            logger.warning(
                "local_provider_capacity_marker_write_failed error=%s",
                type(exc).__name__,
            )
            return False

    def _clear_marker(self, *, expected_token: str | None) -> None:
        if self.marker_path is None:
            return
        try:
            with self._marker_transaction():
                self._clear_marker_unlocked(expected_token=expected_token)
        except OSError:
            # A bounded lease is safer than deleting an unfamiliar marker.
            return

    def _clear_marker_unlocked(self, *, expected_token: str | None) -> bool:
        assert self.marker_path is not None
        marker = self._read_marker_unlocked()
        if expected_token is not None and (marker is None or marker[1] != expected_token):
            return False
        try:
            (self.marker_path / "state").unlink(missing_ok=True)
            for temporary in self.marker_path.glob(".state.*"):
                temporary.unlink(missing_ok=True)
            self.marker_path.rmdir()
        except OSError:
            # A bounded lease is safer than deleting an unfamiliar marker.
            return False
        return True

    @contextmanager
    def _marker_transaction(self) -> Iterator[None]:
        """Serialize marker ownership changes across independent processes."""

        if self.marker_path is None:
            yield
            return
        lock_path = self.marker_path.with_name(f"{self.marker_path.name}.lock")
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _log_rejection(self, reason: str) -> None:
        logger.info(
            "local_provider_capacity_rejected reason=%s active_rejections=%d "
            "cooldown_rejections=%d external_rejections=%d",
            reason,
            self._rejected_active_calls,
            self._rejected_cooldown_calls,
            self._rejected_external_calls,
        )


def local_provider_capacity_marker_path() -> Path:
    """Return the marker location shared with the launchd watchdog."""

    return Path(os.environ.get("TMPDIR") or "/tmp") / ("girl-agent-local-appraisal.capacity")


def _is_provider_outage(exc: Exception) -> bool:
    if isinstance(exc, ModelCircuitOpenError):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 408 or status == 429 or status >= 500
    return isinstance(exc, (ConnectionError, httpx.TransportError))


def _is_failover_eligible_provider_failure(exc: Exception) -> bool:
    """Classify availability failures without treating bad content as an outage."""
    if isinstance(exc, ModelCircuitOpenError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in {402, 408, 429} or status >= 500
    return isinstance(exc, (ConnectionError, TimeoutError, httpx.TransportError))


def _provider_completion_may_still_be_running(exc: Exception) -> bool:
    """Whether a failed client call cannot prove server-side termination."""

    return isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ReadError,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ),
    )


class ProviderCircuitBreaker:
    """Bound repeated provider stalls while allowing a timed recovery probe."""

    def __init__(
        self,
        *,
        failure_threshold: int = 2,
        cooldown_seconds: float = 30.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    def before_call(self) -> None:
        if self._opened_at is None:
            return
        if self.clock() - self._opened_at < self.cooldown_seconds:
            raise ModelCircuitOpenError("model provider circuit is open")
        if self._probe_in_flight:
            raise ModelCircuitOpenError("model provider recovery probe is in flight")
        self._probe_in_flight = True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._failures += 1
        self._probe_in_flight = False
        if self._failures >= self.failure_threshold:
            self._opened_at = self.clock()

    def release_probe(self) -> None:
        """Release a half-open lease without treating caller cancellation as failure."""
        self._probe_in_flight = False

    def snapshot(self) -> ProviderCircuitState:
        if self._opened_at is None:
            return ProviderCircuitState.closed()
        if self.clock() - self._opened_at < self.cooldown_seconds:
            return ProviderCircuitState.open()
        return ProviderCircuitState.half_open()


async def complete_with_timeout(
    awaitable: Awaitable[_T],
    *,
    timeout_seconds: float,
    cancellation_grace_seconds: float = 0.1,
) -> _T:
    """Bound one model operation while preserving why its task was cancelled."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait((task,), timeout=max(0.0, float(timeout_seconds)))
    except asyncio.CancelledError:
        await _cancel_with_grace(
            task,
            reason="caller_cancelled",
            grace_seconds=cancellation_grace_seconds,
        )
        raise
    if task in done:
        return task.result()
    await _cancel_with_grace(
        task,
        reason="provider_timeout",
        grace_seconds=cancellation_grace_seconds,
    )
    raise TimeoutError(f"model call exceeded {timeout_seconds:g}s")


async def _cancel_with_grace(
    task: asyncio.Future[object], *, reason: str, grace_seconds: float
) -> None:
    task.cancel(reason)
    done, _pending = await asyncio.wait((task,), timeout=max(0.0, float(grace_seconds)))
    if task in done:
        _consume_task_result(task)
    else:
        task.add_done_callback(_consume_task_result)


def _consume_task_result(task: asyncio.Future[object]) -> None:
    try:
        task.result()
    except BaseException:
        # A detached, already-cancelled child must not produce an unhandled-task
        # warning or replace the caller's cancellation/timeout outcome.
        pass


class ChatModel(Protocol):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        """Return assistant text for chat messages."""


class DeepSeekChatModel:
    provider = "deepseek"
    reports_exact_request_emission = True

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        thinking_enabled: bool = True,
        reasoning_effort: str = "high",
        transport: httpx.AsyncBaseTransport | None = None,
        usage_observer: Callable[[ModelCallUsage], None] | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        capacity_gate: ProviderCapacityGate | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
        self.transport = transport
        self.usage_observer = usage_observer
        self.circuit_breaker = circuit_breaker
        self.capacity_gate = capacity_gate
        self.client = client or httpx.AsyncClient(
            timeout=45,
            trust_env=False,
            transport=transport,
        )

    def request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
        }
        if self.thinking_enabled:
            # DeepSeek V4 ignores temperature in thinking mode. Leaving it out
            # makes the mode choice explicit and avoids false tuning knobs.
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = self.reasoning_effort
        else:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = temperature
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        result = await self._complete(
            messages,
            temperature=temperature,
            json_object=False,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered completion returned usage")
        return result

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        """Request one JSON object without changing the generic ChatModel API."""
        result = await self._complete(
            messages,
            temperature=temperature,
            json_object=True,
        )
        if not isinstance(result, str):
            raise AssertionError("unmetered JSON completion returned usage")
        return result

    async def complete_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, dict[str, object]]:
        """Return response bytes and provider-bound usage from the same call."""

        result = await self._complete(
            messages,
            temperature=temperature,
            json_object=False,
            include_usage=True,
        )
        if not isinstance(result, tuple):
            raise AssertionError("metered completion did not return usage")
        return result

    async def complete_json_with_usage(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> tuple[str, dict[str, object]]:
        """Return one JSON response and usage bound to that provider call."""

        result = await self._complete(
            messages,
            temperature=temperature,
            json_object=True,
            include_usage=True,
        )
        if not isinstance(result, tuple):
            raise AssertionError("metered JSON completion did not return usage")
        return result

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool,
        include_usage: bool = False,
    ) -> str | tuple[str, dict[str, object]]:
        started = monotonic()
        purpose = _MODEL_CALL_PURPOSE.get()
        call_meta = _MODEL_CALL_META.get()
        capacity_token: str | None = None
        try:
            if self.capacity_gate is not None:
                capacity_token = self.capacity_gate.acquire()
            if self.circuit_breaker is not None:
                self.circuit_breaker.before_call()
            request_payload = self.request_payload(
                messages,
                temperature=temperature,
                json_object=json_object,
            )
            request_span = mark_model_request_emitted()
            try:
                response = await self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
            finally:
                mark_model_request_completed(request_span)
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if not isinstance(choices, list) or not choices:
                raise ValueError("model response choices must be a non-empty list")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ValueError("model response content must be a non-empty string")
        except asyncio.CancelledError as exc:
            cancellation_kind = str(exc.args[0]) if exc.args else "caller_cancelled"
            provider_timeout = cancellation_kind == "provider_timeout"
            if self.capacity_gate is not None and capacity_token is not None:
                self.capacity_gate.abandon(
                    capacity_token,
                    reason=(
                        "provider_timeout" if provider_timeout else "cancelled_may_still_be_running"
                    ),
                )
            if self.circuit_breaker is not None:
                if provider_timeout:
                    self.circuit_breaker.record_failure()
                else:
                    self.circuit_breaker.release_probe()
            self._report_usage(
                ModelCallUsage(
                    purpose=purpose,
                    model=self.model,
                    status="failed",
                    provider=self.provider,
                    latency_ms=max(0, int((monotonic() - started) * 1000)),
                    error="provider_timeout" if provider_timeout else "caller_cancelled",
                    world_id=str(call_meta.get("world_id") or ""),
                    turn_id=str(call_meta.get("turn_id") or ""),
                    action_id=str(call_meta.get("action_id") or ""),
                    cadence=str(call_meta.get("cadence") or ""),
                    attempt=max(1, int(call_meta.get("attempt") or 1)),
                    budget_reservation_id=str(call_meta.get("budget_reservation_id") or ""),
                    thinking_enabled=self.thinking_enabled,
                    reasoning_effort=(self.reasoning_effort if self.thinking_enabled else ""),
                    billing_state="unknown",
                )
            )
            raise
        except Exception as exc:
            provider_outage = _is_provider_outage(exc)
            if self.capacity_gate is not None and capacity_token is not None:
                if _provider_completion_may_still_be_running(exc):
                    self.capacity_gate.abandon(
                        capacity_token,
                        reason=f"transport_ambiguous:{type(exc).__name__}",
                    )
                else:
                    self.capacity_gate.release(capacity_token)
            if self.circuit_breaker is not None and provider_outage:
                self.circuit_breaker.record_failure()
            if isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)):
                error = f"schema_error:{exc}"
            elif isinstance(exc, ModelCapacityBusyError):
                error = f"capacity_busy:{exc}"
            elif provider_outage:
                error = f"provider_error:{exc}"
            elif isinstance(exc, httpx.HTTPStatusError):
                error = f"provider_rejection:{exc}"
            else:
                error = f"unexpected_error:{exc}"
            billing_state = (
                "not_billed"
                if isinstance(exc, ModelCircuitOpenError)
                or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code not in {408, 429}
                    and exc.response.status_code < 500
                )
                else "unknown"
            )
            self._report_usage(
                ModelCallUsage(
                    purpose=purpose,
                    model=self.model,
                    status="failed",
                    provider=self.provider,
                    latency_ms=max(0, int((monotonic() - started) * 1000)),
                    error=error[:500],
                    world_id=str(call_meta.get("world_id") or ""),
                    turn_id=str(call_meta.get("turn_id") or ""),
                    action_id=str(call_meta.get("action_id") or ""),
                    cadence=str(call_meta.get("cadence") or ""),
                    attempt=max(1, int(call_meta.get("attempt") or 1)),
                    budget_reservation_id=str(call_meta.get("budget_reservation_id") or ""),
                    thinking_enabled=self.thinking_enabled,
                    reasoning_effort=(self.reasoning_effort if self.thinking_enabled else ""),
                    billing_state=billing_state,
                )
            )
            raise
        if self.capacity_gate is not None and capacity_token is not None:
            self.capacity_gate.release(capacity_token)
        if self.circuit_breaker is not None:
            self.circuit_breaker.record_success()
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("completion_tokens_details")
        details = details if isinstance(details, dict) else {}
        self._report_usage(
            ModelCallUsage(
                purpose=purpose,
                model=self.model,
                status="succeeded",
                provider=self.provider,
                latency_ms=max(0, int((monotonic() - started) * 1000)),
                prompt_tokens=_usage_int(usage, "prompt_tokens"),
                completion_tokens=_usage_int(usage, "completion_tokens"),
                reasoning_tokens=_usage_int(details, "reasoning_tokens"),
                cache_hit_tokens=_usage_int(usage, "prompt_cache_hit_tokens"),
                cache_miss_tokens=_usage_int(usage, "prompt_cache_miss_tokens"),
                total_tokens=_usage_int(usage, "total_tokens"),
                world_id=str(call_meta.get("world_id") or ""),
                turn_id=str(call_meta.get("turn_id") or ""),
                action_id=str(call_meta.get("action_id") or ""),
                cadence=str(call_meta.get("cadence") or ""),
                attempt=max(1, int(call_meta.get("attempt") or 1)),
                budget_reservation_id=str(call_meta.get("budget_reservation_id") or ""),
                thinking_enabled=self.thinking_enabled,
                reasoning_effort=(self.reasoning_effort if self.thinking_enabled else ""),
                billing_state="known",
            )
        )
        if include_usage:
            return content, self._provider_usage_provenance(
                content=content,
                request_payload=request_payload,
                response_payload=payload,
                usage=usage,
                details=details,
            )
        return content

    def _provider_usage_provenance(
        self,
        *,
        content: str,
        request_payload: dict[str, object],
        response_payload: dict[str, object],
        usage: dict[str, object],
        details: dict[str, object],
    ) -> dict[str, object]:
        input_tokens = _provider_usage_int(usage, "prompt_tokens")
        output_tokens = _provider_usage_int(usage, "completion_tokens")
        thinking_tokens = _optional_provider_usage_int(details, "reasoning_tokens")
        provider_response_id = response_payload.get("id")
        identity_material = {
            "provider": self.provider,
            "model": self.model,
            "provider_response_id": (
                provider_response_id if isinstance(provider_response_id, str) else None
            ),
            "request_hash": _canonical_digest(request_payload),
            "response_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
        }
        material: dict[str, object] = {
            "usage_contract": "model-usage.1",
            "route_class": ("deep_deliberation" if self.thinking_enabled else "chat"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "token_provenance": "provider_reported",
            "transport": "provider_api",
            "provider": self.provider,
            "provider_usage_ref": (
                f"provider-usage:{self.provider}:{_canonical_digest(identity_material)}"
            ),
        }
        material["provider_usage_hash"] = _canonical_digest(material)
        return material

    async def aclose(self) -> None:
        await self.client.aclose()

    def _report_usage(self, usage: ModelCallUsage) -> None:
        state = _MODEL_CALL_STATE.get()
        if self.usage_observer is None:
            if state is not None:
                state.usage_persisted = False
            return
        try:
            self.usage_observer(usage)
            if state is not None:
                state.usage_persisted = True
        except Exception:
            # Observability must never turn a successful model response into a
            # failed companion turn.
            if state is not None:
                state.usage_persisted = False
            return


class OpenAICompatibleChatModel(DeepSeekChatModel):
    """Chat Completions client for OpenAI-compatible fallback providers."""

    provider = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        reasoning_effort: str = "none",
        max_completion_tokens: int = 900,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_observer: Callable[[ModelCallUsage], None] | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        capacity_gate: ProviderCapacityGate | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.proxy_url = proxy_url
        if not 1 <= max_completion_tokens <= 8_192:
            raise ValueError("max_completion_tokens is out of bounds")
        self.max_completion_tokens = max_completion_tokens
        resolved_client = client
        if resolved_client is None and proxy_url:
            resolved_client = httpx.AsyncClient(
                timeout=45,
                trust_env=False,
                proxy=proxy_url,
                transport=transport,
            )
        super().__init__(
            api_key,
            base_url,
            model,
            thinking_enabled=False,
            reasoning_effort=reasoning_effort,
            transport=transport,
            usage_observer=usage_observer,
            circuit_breaker=circuit_breaker,
            capacity_gate=capacity_gate,
            client=resolved_client,
        )

    def request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool = False,
    ) -> dict[str, object]:
        del temperature
        payload: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "reasoning_effort": self.reasoning_effort,
            # World-v2 expression drafts are deliberately compact JSON.  A
            # bounded completion prevents a fallback provider from spending
            # latency/tokens exploring prose that the materializer will reject.
            "max_completion_tokens": self.max_completion_tokens,
        }
        if json_object:
            payload["response_format"] = {"type": "json_object"}
        return payload


class FailoverChatModel:
    """One availability-only failover boundary for a primary chat provider."""

    provider = "deepseek+openai"

    @property
    def reports_exact_request_emission(self) -> bool:
        return all(
            bool(getattr(model, "reports_exact_request_emission", False))
            for model in (self.primary, self.fallback)
        )

    def __init__(
        self,
        *,
        primary: ChatModel,
        fallback: ChatModel,
        implicit_failover: bool = True,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.implicit_failover = implicit_failover
        self.model = f"{getattr(primary, 'model', type(primary).__name__)}->{getattr(fallback, 'model', type(fallback).__name__)}"
        self.last_provider = str(getattr(primary, "provider", "primary"))
        self.last_model = str(getattr(primary, "model", type(primary).__name__))
        # The cognition layer may own one structural-recovery attempt after
        # this availability boundary.  Expose whether this call already
        # consumed the installed fallback so it never calls that same provider
        # a second time for one turn.
        self.last_attempt_used_fallback = False
        # This instance is shared by many concurrent cognition lanes, so the
        # boolean above can be stale by the time another lane reads it.  The
        # monotonic timestamp lets readers restrict "already used" to fallback
        # activity recent enough to belong to their own turn.
        self.last_fallback_used_at: float | None = None

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        return await self._complete(messages, temperature=temperature, json_object=False)

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        return await self._complete(messages, temperature=temperature, json_object=True)

    @property
    def complete_with_usage(
        self,
    ) -> Callable[..., Awaitable[tuple[str, object]]]:
        if not all(
            callable(getattr(model, "complete_with_usage", None))
            for model in (self.primary, self.fallback)
        ):
            raise AttributeError("failover route does not have end-to-end metering")
        return self._complete_with_usage

    @property
    def complete_json_with_usage(
        self,
    ) -> Callable[..., Awaitable[tuple[str, object]]]:
        if not all(
            callable(getattr(model, "complete_json_with_usage", None))
            for model in (self.primary, self.fallback)
        ):
            raise AttributeError("failover JSON route does not have end-to-end metering")
        return self._complete_json_with_usage

    async def _complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        return await self._complete_metered(
            messages,
            temperature=temperature,
            json_object=False,
        )

    async def _complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        return await self._complete_metered(
            messages,
            temperature=temperature,
            json_object=True,
        )

    async def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool,
    ) -> str:
        self.last_attempt_used_fallback = False
        try:
            result = await self._call(
                self.primary,
                messages,
                temperature=temperature,
                json_object=json_object,
            )
        except Exception as exc:
            if not self.implicit_failover or not _is_failover_eligible_provider_failure(exc):
                raise
            self.last_attempt_used_fallback = True
            self.last_fallback_used_at = monotonic()
            self.last_provider = str(getattr(self.fallback, "provider", "fallback"))
            self.last_model = str(getattr(self.fallback, "model", type(self.fallback).__name__))
            result = await self._call(
                self.fallback,
                messages,
                temperature=temperature,
                json_object=json_object,
            )
            return result
        self.last_provider = str(getattr(self.primary, "provider", "primary"))
        self.last_model = str(getattr(self.primary, "model", type(self.primary).__name__))
        return result

    async def _complete_metered(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool,
    ) -> tuple[str, object]:
        self.last_attempt_used_fallback = False
        try:
            result = await self._call_metered(
                self.primary,
                messages,
                temperature=temperature,
                json_object=json_object,
            )
        except Exception as exc:
            if not self.implicit_failover or not _is_failover_eligible_provider_failure(exc):
                raise
            self.last_attempt_used_fallback = True
            self.last_fallback_used_at = monotonic()
            self.last_provider = str(getattr(self.fallback, "provider", "fallback"))
            self.last_model = str(getattr(self.fallback, "model", type(self.fallback).__name__))
            return await self._call_metered(
                self.fallback,
                messages,
                temperature=temperature,
                json_object=json_object,
            )
        self.last_provider = str(getattr(self.primary, "provider", "primary"))
        self.last_model = str(getattr(self.primary, "model", type(self.primary).__name__))
        return result

    @staticmethod
    async def _call(
        model: ChatModel,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool,
    ) -> str:
        complete_json = getattr(model, "complete_json", None)
        if json_object and callable(complete_json):
            return await complete_json(messages, temperature=temperature)
        return await model.complete(messages, temperature=temperature)

    @staticmethod
    async def _call_metered(
        model: ChatModel,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool,
    ) -> tuple[str, object]:
        method_name = "complete_json_with_usage" if json_object else "complete_with_usage"
        complete = getattr(model, method_name, None)
        if not callable(complete):
            raise TypeError(f"{method_name} is unavailable")
        result = await complete(messages, temperature=temperature)
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], str):
            raise ValueError("metered provider result must be (text, usage)")
        return result

    async def aclose(self) -> None:
        for model in (self.primary, self.fallback):
            close = getattr(model, "aclose", None)
            if callable(close):
                await close()


def _usage_int(source: dict[str, object], key: str) -> int:
    value = source.get(key, 0)
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


def _provider_usage_int(source: dict[str, object], key: str) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"provider usage {key} must be a non-negative integer")
    return value


def _optional_provider_usage_int(source: dict[str, object], key: str) -> int:
    if key not in source:
        return 0
    return _provider_usage_int(source, key)


def _canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class FakeCompanionModel:
    def __init__(self):
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append(messages)
        joined = "\n".join(message["content"] for message in messages)
        if "AffectDraft" in joined:
            return json.dumps(
                {
                    "affect": "no_change",
                    "brief_rationale": "Fake simulator does not persist a new affect episode for this ordinary turn.",
                    "behavior_tendency": "observe",
                    "stance": "wait",
                    "display_strategy": "withhold",
                    "confidence": 3000,
                },
                ensure_ascii=False,
            )
        if "AppraisalDraft" in joined:
            return json.dumps(
                {
                    "appraise": False,
                    "brief_rationale": "Fake simulator leaves ordinary interaction without a durable appraisal.",
                    "behavior_tendency": "observe",
                    "stance": "wait",
                    "display_strategy": "withhold",
                    "confidence": 3000,
                },
                ensure_ascii=False,
            )
        if "Choose exactly one offered opaque candidate_result_ref" in joined:
            try:
                candidates = json.loads(messages[-1]["content"])["candidates"]
                candidate_ref = candidates[0]["candidate_result_ref"]
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                return "{}"
            return json.dumps({"candidate_result_ref": candidate_ref}, ensure_ascii=False)
        if "Choose at most one offered opaque opening token" in joined:
            try:
                openings = json.loads(messages[-1]["content"])["openings"]
                opening_token = openings[0]["opening_token"]
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                return '{"decision":"no_op"}'
            return json.dumps(
                {"decision": "select", "opening_token": opening_token}, ensure_ascii=False
            )
        if "Audit only factual source closure" in joined:
            return json.dumps(
                {
                    "ci": [],
                    "v": [],
                    "p": [],
                    "r": "Fake simulator accepts its source-free fixture reply.",
                },
                ensure_ascii=False,
            )
        if "exactly two keys: appraisal_draft and expression_draft" in joined:
            return json.dumps(
                {
                    "appraisal_draft": {
                        "appraise": False,
                        "brief_rationale": "Fake simulator leaves this as an ordinary interaction.",
                        "behavior_tendency": "observe",
                        "stance": "open",
                        "display_strategy": "natural",
                        "confidence": 3000,
                    },
                    "expression_draft": {
                        "private_turn_state": {
                            "inner_state_summary": (
                                "The current message feels like an ordinary invitation "
                                "to stay present."
                            ),
                            "attended_source_refs": [],
                        },
                        "timing_choice": "now",
                        "beats": [{"modality": "text", "text": "我在，刚刚这句我有接到。"}],
                        "cadence": "conversational",
                        "stance": "open",
                        "brief_rationale": "Fake World v2 expression for an end-to-end turn.",
                        "confidence": 6000,
                        "world_claims": [],
                    },
                },
                ensure_ascii=False,
            )
        if (
            "Return a ReplyDraft" in joined
            or "Return an ExpressionDraft" in joined
            or "raw JSON ExpressionDraft" in joined
        ):
            return json.dumps(
                {
                    "private_turn_state": {
                        "inner_state_summary": (
                            "The current message feels like an ordinary invitation to stay present."
                        ),
                        "attended_source_refs": [],
                    },
                    "timing_choice": "now",
                    "beats": [{"modality": "text", "text": "我在，刚刚这句我有接到。"}],
                    "cadence": "conversational",
                    "stance": "open",
                    "brief_rationale": "Fake World v2 draft for an end-to-end simulator turn.",
                    "confidence": 6000,
                    "world_claims": [],
                },
                ensure_ascii=False,
            )
        if "严格的虚拟世界事实审计器" in joined:
            return json.dumps(
                {"supported": True, "unsupported_spans": [], "reason": "fake audit pass"},
                ensure_ascii=False,
            )
        if "聊天余波" in joined and "WorldReplyJSON" in joined:
            return json.dumps(
                {
                    "reply_text": "想再补一句。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                },
                ensure_ascii=False,
            )
        if "WorldReplyJSON" in joined:
            return json.dumps(
                {
                    "reply_text": "刚看到，我在。",
                    "mentioned_event_ids": [],
                    "proposed_action_ids": [],
                },
                ensure_ascii=False,
            )
        if "Return strict JSON" in joined:
            return json.dumps(
                {
                    "private_thought": "他刚刚隔了一会儿才回来，我有点想贴近一点，但不想显得太黏。",
                    "should_send": True,
                    "platform": "qq",
                    "message_type": "text",
                    "message": "你回来了呀。我刚刚有一点点在等你。",
                    "sticker_category": None,
                    "cooldown_minutes": 45,
                },
                ensure_ascii=False,
            )
        return "刚刚是不是忙完了？我在呢。"
