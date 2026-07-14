"""Bounded, fail-open compilation of non-authoritative classifier advice.

This module is deliberately read-only.  It receives a revision-pinned, already-trimmed
snapshot and gives classifier adapters no ledger, clock, network, or mutation port.  Every
adapter result is treated as untrusted data and checked against ``MatrixCatalog`` before it
can become an advisory for deliberation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import hmac
import json
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    FrequencyBudget,
    MatrixCatalog,
    MatrixSchemaError,
)


MAX_SOURCE_REFS = 64
MAX_SOURCE_REF_CHARACTERS = 256
MAX_RECENT_CONTEXT_ITEMS = 32
MAX_INPUT_CHARACTERS = 64_000
MAX_JSON_NODES = 4_096
MAX_JSON_DEPTH = 24
MAX_ADAPTERS = 8
MAX_DISTRIBUTIONS_PER_ADAPTER = 16
MAX_CANDIDATES_PER_DISTRIBUTION = 8
MAX_OUTPUT_ADVISORIES = 64


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _bounded_json(value: object, *, label: str) -> str:
    """Validate depth/node bounds before serialization can become a DoS vector."""

    stack: list[tuple[object, int]] = [(value, 0)]
    seen = 0
    scalar_characters = 0
    while stack:
        item, depth = stack.pop()
        seen += 1
        if seen > MAX_JSON_NODES:
            raise ValueError(f"{label} exceeds its node limit")
        if depth > MAX_JSON_DEPTH:
            raise ValueError(f"{label} exceeds its depth limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError(f"{label} must use string object keys")
            scalar_characters += sum(len(key) for key in item)
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            scalar_characters += len(item)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError(f"{label} must be canonical JSON data")
        if scalar_characters > MAX_INPUT_CHARACTERS:
            raise ValueError(f"{label} exceeds its character limit")
    try:
        serialized = _canonical_json(value)
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError(f"{label} must be canonical JSON data") from error
    if len(serialized) > MAX_INPUT_CHARACTERS:
        raise ValueError(f"{label} exceeds its character limit")
    return serialized


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def canonical_snapshot_hash(values: dict[str, Any]) -> str:
    """Hash the complete canonical snapshot material supplied by its resolver."""

    _bounded_json(values, label="snapshot material")
    return _digest({"values": values})


def canonical_trigger_hash(trigger: dict[str, Any]) -> str:
    """Hash the exact bounded trigger content referenced by the trigger authority."""

    _bounded_json(trigger, label="trigger")
    return _digest({"trigger": trigger})


def canonical_recent_context_hash(recent_context: tuple[dict[str, Any], ...]) -> str:
    """Hash the complete bounded recent context supplied to every classifier."""

    if type(recent_context) is not tuple or len(recent_context) > MAX_RECENT_CONTEXT_ITEMS:
        raise ValueError("recent context exceeds its item limit")
    _bounded_json(recent_context, label="recent context")
    return _digest({"recent_context": recent_context})


def source_authority_bindings_hash(bindings: tuple[SourceAuthorityBinding, ...]) -> str:
    """Bind resolver completeness to the exact ordered authority evidence."""

    _preflight_source_authorities(bindings)
    return _digest(
        [
            {
                "ref": binding.ref,
                "world_revision": binding.world_revision,
                "hash_kind": binding.hash_kind,
                "authority_hash": binding.authority_hash,
                "content_hash": binding.content_hash,
            }
            for binding in bindings
        ]
    )


def _aware(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


class SourceAuthorityBinding(_FrozenModel):
    ref: str = Field(min_length=1, max_length=MAX_SOURCE_REF_CHARACTERS)
    world_revision: int = Field(ge=0)
    hash_kind: Literal["payload", "semantic"]
    authority_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    content_hash: str | None = Field(
        default=None, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    )


def _preflight_source_authorities(bindings: object) -> None:
    if type(bindings) is not tuple or len(bindings) > MAX_SOURCE_REFS:
        raise ValueError("advisory request source authorities exceed their limit")
    for binding in bindings:
        if type(binding) is not SourceAuthorityBinding:
            raise ValueError("advisory source authority has invalid structure")
        if (
            type(binding.ref) is not str
            or len(binding.ref) > MAX_SOURCE_REF_CHARACTERS
            or type(binding.authority_hash) is not str
            or len(binding.authority_hash) != 64
            or type(binding.hash_kind) is not str
            or len(binding.hash_kind) > 16
            or type(binding.world_revision) is not int
            or (
                binding.content_hash is not None
                and (type(binding.content_hash) is not str or len(binding.content_hash) != 64)
            )
        ):
            raise ValueError("advisory source authority exceeds its scalar limits")


class ResolverProof(_FrozenModel):
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    world_revision: int = Field(ge=0)
    completeness: Literal["full"] = "full"
    policy_version: str = Field(min_length=1, max_length=128)
    source_bindings_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    authentication_tag: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class SnapshotMaterial(_FrozenModel):
    """Already-resolved authority material; no retrieval capability is carried with it."""

    world_revision: int = Field(ge=0)
    values: dict[str, Any]
    canonical_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def material_is_bounded_json(self) -> Self:
        _bounded_json(self.values, label="snapshot material")
        if self.canonical_hash != canonical_snapshot_hash(self.values):
            raise ValueError("snapshot material canonical hash mismatch")
        return self


class AdvisoryCompileRequest(_FrozenModel):
    world_id: str = Field(min_length=1, max_length=256)
    snapshot_id: str = Field(min_length=1, max_length=256)
    snapshot_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    world_revision: int = Field(ge=0)
    logical_time: datetime
    trigger_ref: str = Field(min_length=1, max_length=MAX_SOURCE_REF_CHARACTERS)
    expires_at: datetime
    source_authorities: tuple[SourceAuthorityBinding, ...] = Field(
        min_length=1, max_length=MAX_SOURCE_REFS
    )
    resolver_proof: ResolverProof
    trigger: dict[str, Any]
    recent_context: tuple[dict[str, Any], ...] = Field(max_length=MAX_RECENT_CONTEXT_ITEMS)
    snapshot: SnapshotMaterial

    @field_validator("logical_time", "expires_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        return _aware(value, label="advisory compile time")

    @model_validator(mode="after")
    def input_is_source_bound_and_bounded(self) -> Self:
        if self.snapshot.world_revision != self.world_revision:
            raise ValueError("snapshot and request must use the same world revision")
        if self.expires_at <= self.logical_time:
            raise ValueError("advisory request expiry must be after logical time")
        if self.snapshot_hash != self.snapshot.canonical_hash:
            raise ValueError("request snapshot hash does not match snapshot material")
        refs = tuple(binding.ref for binding in self.source_authorities)
        if refs != tuple(sorted(set(refs))):
            raise ValueError("source authority refs must be sorted and unique")
        if any(
            binding.world_revision != self.world_revision for binding in self.source_authorities
        ):
            raise ValueError("source authority binding must use the pinned world revision")
        if self.trigger_ref not in refs:
            raise ValueError("trigger_ref must be an allowed source")
        trigger_binding = next(
            binding for binding in self.source_authorities if binding.ref == self.trigger_ref
        )
        if trigger_binding.content_hash != canonical_trigger_hash(self.trigger):
            raise ValueError("trigger authority content hash mismatch")
        proof = self.resolver_proof
        if (
            proof.snapshot_id != self.snapshot_id
            or proof.snapshot_hash != self.snapshot_hash
            or proof.world_revision != self.world_revision
        ):
            raise ValueError("resolver proof does not match the pinned snapshot")
        if proof.source_bindings_hash != source_authority_bindings_hash(self.source_authorities):
            raise ValueError("resolver proof source binding hash mismatch")
        _bounded_json(
            {"trigger": self.trigger, "recent_context": self.recent_context},
            label="trigger and recent context",
        )
        return self


def _preflight_request_structure(request: object) -> None:
    """Bound forged nested state before hashing or recursive Pydantic serialization."""

    if type(request) is not AdvisoryCompileRequest:
        raise ValueError("advisory request has invalid structure")
    direct_strings = (
        request.world_id,
        request.snapshot_id,
        request.snapshot_hash,
        request.trigger_ref,
    )
    if any(type(value) is not str or len(value) > 256 for value in direct_strings):
        raise ValueError("advisory request exceeds its scalar limits")
    if (
        type(request.world_revision) is not int
        or type(request.logical_time) is not datetime
        or type(request.expires_at) is not datetime
    ):
        raise ValueError("advisory request has invalid scalar structure")
    _preflight_source_authorities(request.source_authorities)
    if type(request.resolver_proof) is not ResolverProof:
        raise ValueError("advisory resolver proof has invalid structure")
    proof = request.resolver_proof
    for scalar in (
        proof.snapshot_id,
        proof.snapshot_hash,
        proof.policy_version,
        proof.source_bindings_hash,
        proof.authentication_tag,
    ):
        if type(scalar) is not str or len(scalar) > 256:
            raise ValueError("advisory resolver proof exceeds its scalar limits")
    if type(proof.world_revision) is not int or type(proof.completeness) is not str:
        raise ValueError("advisory resolver proof has invalid scalar structure")
    if type(request.snapshot) is not SnapshotMaterial or type(request.snapshot.values) is not dict:
        raise ValueError("advisory snapshot has invalid structure")
    if (
        type(request.snapshot.world_revision) is not int
        or type(request.snapshot.canonical_hash) is not str
        or len(request.snapshot.canonical_hash) != 64
    ):
        raise ValueError("advisory snapshot has invalid scalar structure")
    _bounded_json(request.snapshot.values, label="snapshot material")
    if type(request.trigger) is not dict:
        raise ValueError("advisory trigger has invalid structure")
    canonical_trigger_hash(request.trigger)
    canonical_recent_context_hash(request.recent_context)


def _request_authentication_tag(request: AdvisoryCompileRequest, *, key: bytes) -> str:
    payload = {
        "world_id": request.world_id,
        "snapshot_id": request.snapshot_id,
        "snapshot_hash": request.snapshot_hash,
        "world_revision": request.world_revision,
        "logical_time": request.logical_time.isoformat(),
        "trigger_ref": request.trigger_ref,
        "trigger_hash": canonical_trigger_hash(request.trigger),
        "recent_context_hash": canonical_recent_context_hash(request.recent_context),
        "expires_at": request.expires_at.isoformat(),
        "source_bindings_hash": source_authority_bindings_hash(request.source_authorities),
        "resolver_snapshot_id": request.resolver_proof.snapshot_id,
        "resolver_snapshot_hash": request.resolver_proof.snapshot_hash,
        "resolver_world_revision": request.resolver_proof.world_revision,
        "resolver_completeness": request.resolver_proof.completeness,
        "resolver_policy_version": request.resolver_proof.policy_version,
    }
    return hmac.new(key, _canonical_json(payload).encode(), hashlib.sha256).hexdigest()


def authenticate_advisory_request(
    request: AdvisoryCompileRequest, *, authority_key: bytes
) -> AdvisoryCompileRequest:
    """Trusted resolver factory seam: authenticate an already validated bounded request."""

    if type(authority_key) is not bytes or len(authority_key) < 32:
        raise ValueError("advisory authority key must contain at least 32 bytes")
    _preflight_request_structure(request)
    tag = _request_authentication_tag(request, key=authority_key)
    return request.model_copy(
        update={
            "resolver_proof": request.resolver_proof.model_copy(update={"authentication_tag": tag})
        }
    )


class AdvisoryAdapterInput(_FrozenModel):
    """The complete capability-free view supplied to one classifier."""

    world_id: str
    snapshot_id: str
    snapshot_hash: str
    world_revision: int
    logical_time: datetime
    trigger_ref: str
    expires_at: datetime
    source_authorities: tuple[SourceAuthorityBinding, ...]
    resolver_proof: ResolverProof
    trigger: dict[str, Any]
    recent_context: tuple[dict[str, Any], ...]
    snapshot: SnapshotMaterial


class AdvisoryClassifierAdapter(Protocol):
    adapter_id: str
    version: str

    async def classify(
        self, request: AdvisoryAdapterInput
    ) -> tuple[CandidateDistribution, ...]: ...


class AdvisoryCompilerLimits(_FrozenModel):
    max_adapters: int = Field(default=MAX_ADAPTERS, ge=0, le=MAX_ADAPTERS)
    max_distributions_per_adapter: int = Field(
        default=MAX_DISTRIBUTIONS_PER_ADAPTER,
        ge=1,
        le=MAX_DISTRIBUTIONS_PER_ADAPTER,
    )
    max_candidates_per_distribution: int = Field(
        default=MAX_CANDIDATES_PER_DISTRIBUTION,
        ge=1,
        le=MAX_CANDIDATES_PER_DISTRIBUTION,
    )
    max_output_advisories: int = Field(
        default=MAX_OUTPUT_ADVISORIES, ge=1, le=MAX_OUTPUT_ADVISORIES
    )


TraceStatus = Literal["success", "timeout", "exception", "invalid_output", "unavailable"]


class AdvisoryTraceEntry(_FrozenModel):
    adapter_id: str = Field(min_length=1, max_length=128)
    producer: str = Field(min_length=1, max_length=256)
    status: TraceStatus
    output_count: int = Field(ge=0, le=MAX_DISTRIBUTIONS_PER_ADAPTER)
    error_code: str | None = Field(default=None, max_length=64)


class CompiledAdvisory(_FrozenModel):
    """A rejectable distribution, explicitly carrying no behavioural authority."""

    advisory_id: str = Field(min_length=1)
    producer: str = Field(min_length=1)
    field_id: str = Field(min_length=1)
    candidates: tuple[ClassificationCandidate, ...] = Field(
        min_length=1, max_length=MAX_CANDIDATES_PER_DISTRIBUTION
    )
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=MAX_SOURCE_REFS)
    produced_at: datetime
    expires_at: datetime
    catalog_version: str = Field(min_length=1)
    frequency_budget: FrequencyBudget | None = None
    authoritative: Literal[False] = False


class AdvisoryCompilation(_FrozenModel):
    advisory_set_id: str = Field(min_length=1)
    world_id: str
    snapshot_id: str
    snapshot_hash: str
    world_revision: int
    trigger_ref: str
    logical_time: datetime
    catalog_version: str
    advisories: tuple[CompiledAdvisory, ...]
    trace: tuple[AdvisoryTraceEntry, ...]


class _InvalidAdapterOutput(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdvisoryCompiler:
    """Run independent classifiers concurrently and fail open at each adapter boundary."""

    def __init__(
        self,
        *,
        catalog: MatrixCatalog,
        adapters: tuple[AdvisoryClassifierAdapter, ...],
        timeout_seconds: float = 0.25,
        limits: AdvisoryCompilerLimits | None = None,
        authority_key: bytes,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 10:
            raise ValueError("advisory timeout must be in (0, 10] seconds")
        if type(authority_key) is not bytes or len(authority_key) < 32:
            raise ValueError("advisory authority key must contain at least 32 bytes")
        self._authority_key = authority_key
        self._catalog = catalog
        self._limits = limits or AdvisoryCompilerLimits()
        if len(adapters) > self._limits.max_adapters:
            raise ValueError(f"at most {self._limits.max_adapters} advisory adapters are allowed")
        adapter_ids = [adapter.adapter_id for adapter in adapters]
        if len(adapter_ids) != len(set(adapter_ids)):
            raise ValueError("advisory adapter IDs must be unique")
        if any(not adapter_id or len(adapter_id) > 128 for adapter_id in adapter_ids):
            raise ValueError("advisory adapter ID is empty or oversized")
        if any(not adapter.version or len(adapter.version) > 128 for adapter in adapters):
            raise ValueError("advisory adapter version is empty or oversized")
        if any(len(self._producer(adapter)) > 256 for adapter in adapters):
            raise ValueError("advisory producer identity is oversized")
        self._adapters = tuple(sorted(adapters, key=lambda adapter: adapter.adapter_id))
        self._timeout_seconds = timeout_seconds
        self._outstanding_tasks: dict[str, asyncio.Task[object]] = {}
        self._fused_adapters: set[str] = set()

    async def compile(self, request: AdvisoryCompileRequest) -> AdvisoryCompilation:
        # Frozen Pydantic objects can still be forged with ``model_construct``.  The public
        # seam never trusts its nominal type and validates a fresh complete representation.
        self._preflight_request(request)
        request = AdvisoryCompileRequest.model_validate(
            request.model_dump(mode="python", warnings=False), strict=True
        )
        expected_tag = _request_authentication_tag(request, key=self._authority_key)
        if not hmac.compare_digest(request.resolver_proof.authentication_tag, expected_tag):
            raise ValueError("resolver proof authentication failed")
        results = await asyncio.gather(
            *(
                self._call(adapter, AdvisoryAdapterInput(**request.model_dump()))
                for adapter in self._adapters
            )
        )

        advisories: list[CompiledAdvisory] = []
        trace: list[AdvisoryTraceEntry] = []
        for adapter, (distributions, status, error_code) in zip(
            self._adapters, results, strict=True
        ):
            producer = self._producer(adapter)
            if status == "success" and (
                len(advisories) + len(distributions) > self._limits.max_output_advisories
            ):
                distributions = ()
                status = "invalid_output"
                error_code = "global_output_limit"
            compiled = tuple(
                self._compile_distribution(
                    request=request, producer=producer, distribution=distribution
                )
                for distribution in distributions
            )
            advisories.extend(compiled)
            trace.append(
                AdvisoryTraceEntry(
                    adapter_id=adapter.adapter_id,
                    producer=producer,
                    status=status,
                    output_count=len(compiled),
                    error_code=error_code,
                )
            )

        ordered = tuple(
            sorted(advisories, key=lambda item: (item.producer, item.field_id, item.advisory_id))
        )
        trace_tuple = tuple(trace)
        identity = {
            "world_id": request.world_id,
            "snapshot_id": request.snapshot_id,
            "snapshot_hash": request.snapshot_hash,
            "world_revision": request.world_revision,
            "trigger_ref": request.trigger_ref,
            "logical_time": request.logical_time.isoformat(),
            "catalog_version": self._catalog.catalog_version,
            "advisories": [item.model_dump(mode="json") for item in ordered],
            "trace": [item.model_dump(mode="json") for item in trace_tuple],
        }
        return AdvisoryCompilation(
            advisory_set_id=f"advisory-set:{_digest(identity)}",
            world_id=request.world_id,
            snapshot_id=request.snapshot_id,
            snapshot_hash=request.snapshot_hash,
            world_revision=request.world_revision,
            trigger_ref=request.trigger_ref,
            logical_time=request.logical_time,
            catalog_version=self._catalog.catalog_version,
            advisories=ordered,
            trace=trace_tuple,
        )

    async def _call(
        self,
        adapter: AdvisoryClassifierAdapter,
        request: AdvisoryAdapterInput,
    ) -> tuple[tuple[CandidateDistribution, ...], TraceStatus, str | None]:
        if adapter.adapter_id in self._fused_adapters:
            return (), "unavailable", "adapter_fused"
        if adapter.adapter_id in self._outstanding_tasks:
            return (), "unavailable", "adapter_outstanding"
        try:
            task = asyncio.create_task(adapter.classify(request))
            self._outstanding_tasks[adapter.adapter_id] = task
            self._track_outstanding_task(adapter.adapter_id, task)
            done, _ = await asyncio.wait({task}, timeout=self._timeout_seconds)
            if not done:
                task.cancel()
                self._fused_adapters.add(adapter.adapter_id)
                return (), "timeout", "adapter_timeout"
            self._outstanding_tasks.pop(adapter.adapter_id, None)
            raw = task.result()
            distributions = self._revalidate_raw_output(raw)
            self._validate_distributions(adapter, request, distributions)
        except _InvalidAdapterOutput as error:
            return (), "invalid_output", error.code
        except (MatrixSchemaError, ValueError, TypeError):
            return (), "invalid_output", "invalid_structure"
        except Exception:
            return (), "exception", "adapter_exception"
        return distributions, "success", None

    def _track_outstanding_task(self, adapter_id: str, task: asyncio.Task[object]) -> None:
        """Consume a late result without awaiting a classifier that suppresses cancellation.

        This is an event-loop latency boundary, not a claim that Python can forcibly terminate
        arbitrary code. Adapters receive only a capability-free data copy; a task that ignores
        cancellation is retained until eventual completion and its exception is consumed.
        """

        def consume(completed: asyncio.Task[object]) -> None:
            if self._outstanding_tasks.get(adapter_id) is completed:
                self._outstanding_tasks.pop(adapter_id, None)
            if not completed.cancelled():
                try:
                    completed.exception()
                except Exception:
                    pass

        task.add_done_callback(consume)

    async def aclose(self, *, timeout_seconds: float = 0.05) -> None:
        """Request cancellation of bounded outstanding work without claiming forced kill."""

        if timeout_seconds < 0 or timeout_seconds > 1:
            raise ValueError("advisory close timeout must be in [0, 1] seconds")
        tasks = set(self._outstanding_tasks.values())
        self._fused_adapters.update(self._outstanding_tasks)
        for task in tasks:
            task.cancel()
        if tasks and timeout_seconds:
            await asyncio.wait(tasks, timeout=timeout_seconds)

    @property
    def outstanding_task_count(self) -> int:
        return len(self._outstanding_tasks)

    @property
    def fused_adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._fused_adapters))

    def _preflight_request(self, request: object) -> None:
        _preflight_request_structure(request)

    def _revalidate_raw_output(self, raw: object) -> tuple[CandidateDistribution, ...]:
        # These constant-time container checks happen before model dumping or recursive
        # validation, so forged million-item tuples cannot consume proportional CPU here.
        if type(raw) is not tuple:
            raise _InvalidAdapterOutput("invalid_structure")
        if len(raw) > self._limits.max_distributions_per_adapter:
            raise _InvalidAdapterOutput("too_many_distributions")
        validated: list[CandidateDistribution] = []
        for item in raw:
            if type(item) is not CandidateDistribution:
                raise _InvalidAdapterOutput("invalid_structure")
            if (
                type(item.catalog_version) is not str
                or len(item.catalog_version) > 256
                or type(item.field_id) is not str
                or len(item.field_id) > 256
                or type(item.produced_at) is not datetime
            ):
                raise _InvalidAdapterOutput("invalid_structure")
            candidates = item.candidates
            if type(candidates) is not tuple:
                raise _InvalidAdapterOutput("invalid_structure")
            if len(candidates) > self._limits.max_candidates_per_distribution:
                raise _InvalidAdapterOutput("too_many_candidates")
            for candidate in candidates:
                if type(candidate) is not ClassificationCandidate:
                    raise _InvalidAdapterOutput("invalid_structure")
                self._preflight_candidate(candidate)
                ClassificationCandidate.model_validate(
                    candidate.model_dump(mode="python", warnings=False), strict=True
                )
            if item.frequency_budget is not None:
                if type(item.frequency_budget) is not FrequencyBudget:
                    raise _InvalidAdapterOutput("invalid_structure")
                self._preflight_frequency_budget(item.frequency_budget)
                FrequencyBudget.model_validate(
                    item.frequency_budget.model_dump(mode="python", warnings=False), strict=True
                )
            validated.append(
                CandidateDistribution.model_validate(
                    item.model_dump(mode="python", warnings=False), strict=True
                )
            )
        return tuple(validated)

    @staticmethod
    def _preflight_candidate(candidate: ClassificationCandidate) -> None:
        if (
            type(candidate.value) is not str
            or len(candidate.value) > 256
            or type(candidate.producer) is not str
            or len(candidate.producer) > 256
            or type(candidate.weight) is not int
            or type(candidate.confidence) is not int
            or (candidate.expires_at is not None and type(candidate.expires_at) is not datetime)
            or type(candidate.source_refs) is not tuple
            or len(candidate.source_refs) > MAX_SOURCE_REFS
        ):
            raise _InvalidAdapterOutput("invalid_structure")
        if any(
            type(ref) is not str or len(ref) > MAX_SOURCE_REF_CHARACTERS
            for ref in candidate.source_refs
        ):
            raise _InvalidAdapterOutput("invalid_structure")

    @staticmethod
    def _preflight_frequency_budget(budget: FrequencyBudget) -> None:
        if (
            type(budget.state) is not str
            or len(budget.state) > 64
            or type(budget.window) is not str
            or len(budget.window) > 256
            or type(budget.used) is not int
            or type(budget.limit) is not int
            or type(budget.source_refs) is not tuple
            or len(budget.source_refs) > MAX_SOURCE_REFS
        ):
            raise _InvalidAdapterOutput("invalid_structure")
        if any(
            type(ref) is not str or len(ref) > MAX_SOURCE_REF_CHARACTERS
            for ref in budget.source_refs
        ):
            raise _InvalidAdapterOutput("invalid_structure")

    def _validate_distributions(
        self,
        adapter: AdvisoryClassifierAdapter,
        request: AdvisoryAdapterInput,
        distributions: tuple[CandidateDistribution, ...],
    ) -> None:
        if len(distributions) > self._limits.max_distributions_per_adapter:
            raise _InvalidAdapterOutput("too_many_distributions")
        fields = [distribution.field_id for distribution in distributions]
        if len(fields) != len(set(fields)):
            raise _InvalidAdapterOutput("duplicate_field")
        producer = self._producer(adapter)
        allowed_sources = {binding.ref for binding in request.source_authorities}
        for distribution in distributions:
            try:
                field = self._catalog.lookup(distribution.field_id)
            except MatrixSchemaError as error:
                raise _InvalidAdapterOutput("unknown_field") from error
            if field.owner != "advisory" or field.persistence != "candidate":
                raise _InvalidAdapterOutput("field_not_advisory_owned")
            if "classifier" not in field.candidate_producers:
                raise _InvalidAdapterOutput("producer_kind_not_allowed")
            if distribution.catalog_version != self._catalog.catalog_version:
                raise _InvalidAdapterOutput("catalog_version_mismatch")
            if distribution.produced_at != request.logical_time:
                raise _InvalidAdapterOutput("produced_at_mismatch")
            if len(distribution.candidates) > self._limits.max_candidates_per_distribution:
                raise _InvalidAdapterOutput("too_many_candidates")
            for candidate in distribution.candidates:
                if candidate.producer != producer:
                    raise _InvalidAdapterOutput("producer_mismatch")
                if candidate.source_refs != tuple(sorted(set(candidate.source_refs))):
                    raise _InvalidAdapterOutput("noncanonical_source_refs")
                if not set(candidate.source_refs).issubset(allowed_sources):
                    raise _InvalidAdapterOutput("source_ref_not_in_input")
                if candidate.expires_at is None:
                    raise _InvalidAdapterOutput("missing_expiry")
                if candidate.expires_at <= request.logical_time:
                    raise _InvalidAdapterOutput("expired_candidate")
                if candidate.expires_at > request.expires_at:
                    raise _InvalidAdapterOutput("expiry_out_of_bounds")
            if distribution.frequency_budget is not None:
                budget_refs = distribution.frequency_budget.source_refs
                if budget_refs != tuple(sorted(set(budget_refs))):
                    raise _InvalidAdapterOutput("noncanonical_frequency_budget_sources")
                if not set(budget_refs).issubset(allowed_sources):
                    raise _InvalidAdapterOutput("frequency_budget_source_not_in_input")
            try:
                self._catalog.validate_candidates(distribution, at=request.logical_time)
            except MatrixSchemaError as error:
                raise _InvalidAdapterOutput("catalog_schema_invalid") from error

    @staticmethod
    def _producer(adapter: AdvisoryClassifierAdapter) -> str:
        return f"{adapter.adapter_id}@{adapter.version}"

    @staticmethod
    def _compile_distribution(
        *,
        request: AdvisoryCompileRequest,
        producer: str,
        distribution: CandidateDistribution,
    ) -> CompiledAdvisory:
        candidates = tuple(
            sorted(
                distribution.candidates,
                key=lambda candidate: (
                    -candidate.weight,
                    -candidate.confidence,
                    candidate.value,
                    candidate.producer,
                    candidate.source_refs,
                    candidate.expires_at,
                ),
            )
        )
        source_refs = tuple(
            sorted(
                {ref for candidate in candidates for ref in candidate.source_refs}
                | (
                    set(distribution.frequency_budget.source_refs)
                    if distribution.frequency_budget is not None
                    else set()
                )
            )
        )
        expiry = min(
            candidate.expires_at for candidate in candidates if candidate.expires_at is not None
        )
        identity = {
            "snapshot_hash": request.snapshot_hash,
            "world_revision": request.world_revision,
            "trigger_ref": request.trigger_ref,
            "producer": producer,
            "field_id": distribution.field_id,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "produced_at": distribution.produced_at.isoformat(),
            "catalog_version": distribution.catalog_version,
            "frequency_budget": (
                distribution.frequency_budget.model_dump(mode="json")
                if distribution.frequency_budget is not None
                else None
            ),
        }
        return CompiledAdvisory(
            advisory_id=f"advisory:{_digest(identity)}",
            producer=producer,
            field_id=distribution.field_id,
            candidates=candidates,
            source_refs=source_refs,
            produced_at=distribution.produced_at,
            expires_at=expiry,
            catalog_version=distribution.catalog_version,
            frequency_budget=distribution.frequency_budget,
        )


__all__ = [
    "AdvisoryAdapterInput",
    "AdvisoryClassifierAdapter",
    "AdvisoryCompilation",
    "AdvisoryCompileRequest",
    "AdvisoryCompiler",
    "AdvisoryCompilerLimits",
    "AdvisoryTraceEntry",
    "CompiledAdvisory",
    "ResolverProof",
    "SnapshotMaterial",
    "SourceAuthorityBinding",
    "authenticate_advisory_request",
    "canonical_recent_context_hash",
    "canonical_snapshot_hash",
    "canonical_trigger_hash",
    "source_authority_bindings_hash",
]
