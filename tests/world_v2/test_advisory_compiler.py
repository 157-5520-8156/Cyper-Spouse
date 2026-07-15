from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import time
from typing import Any

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.advisory_compiler import (
    AdvisoryAdapterInput,
    AdvisoryCompileRequest,
    AdvisoryCompiler as _AdvisoryCompiler,
    AdvisoryCompilerLimits,
    ResolverProof,
    SnapshotMaterial,
    SourceAuthorityBinding,
    authenticate_advisory_request,
    canonical_snapshot_hash,
    canonical_trigger_hash,
    source_authority_bindings_hash,
)
from companion_daemon.world_v2.matrix_catalog import (
    CandidateDistribution,
    ClassificationCandidate,
    FrequencyBudget,
    default_matrix_catalog,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
AUTHORITY_KEY = b"world-v2-advisory-test-authority-key"
TRIGGER = {"kind": "user_message", "text": "算了，当我没说"}


def AdvisoryCompiler(**kwargs: Any) -> _AdvisoryCompiler:
    kwargs.setdefault("authority_key", AUTHORITY_KEY)
    return _AdvisoryCompiler(**kwargs)


def _distribution(
    *,
    field_id: str = "appraisal.negative",
    value: str = "disappointment",
    producer: str = "emotion@classifier-1",
    source_refs: tuple[str, ...] = ("event:message:1",),
    expires_at: datetime | None = None,
    frequency_budget: FrequencyBudget | None = None,
) -> CandidateDistribution:
    return CandidateDistribution(
        catalog_version="world-v2-matrix-1",
        field_id=field_id,
        candidates=(
            ClassificationCandidate(
                value=value,
                weight=6200,
                confidence=7100,
                producer=producer,
                source_refs=source_refs,
                expires_at=expires_at or NOW + timedelta(seconds=30),
            ),
        ),
        produced_at=NOW,
        frequency_budget=frequency_budget,
    )


def _snapshot(values: dict[str, Any] | None = None, *, revision: int = 7) -> SnapshotMaterial:
    material = values if values is not None else {"relationship": {"stage": "friend"}}
    return SnapshotMaterial(
        world_revision=revision,
        values=material,
        canonical_hash=canonical_snapshot_hash(material),
    )


def _source_authorities(count: int = 2) -> tuple[SourceAuthorityBinding, ...]:
    refs = (
        ("event:message:1", "thread:life-share")
        if count == 2
        else tuple(f"event:{index:03d}" for index in range(count))
    )
    return tuple(
        SourceAuthorityBinding(
            ref=ref,
            world_revision=7,
            hash_kind="semantic",
            authority_hash=f"{index:064x}",
            content_hash=canonical_trigger_hash(TRIGGER) if ref == "event:message:1" else None,
        )
        for index, ref in enumerate(refs, start=1)
    )


def _request(**updates: Any) -> AdvisoryCompileRequest:
    snapshot = updates.pop("snapshot", _snapshot())
    source_authorities = updates.pop("source_authorities", _source_authorities())
    values: dict[str, Any] = {
        "world_id": "world:1",
        "snapshot_id": "snapshot:7",
        "snapshot_hash": snapshot.canonical_hash,
        "world_revision": 7,
        "logical_time": NOW,
        "trigger_ref": "event:message:1",
        "expires_at": NOW + timedelta(seconds=45),
        "source_authorities": source_authorities,
        "trigger": TRIGGER,
        "recent_context": ({"role": "companion", "text": "然后呢？"},),
        "snapshot": snapshot,
        "resolver_proof": ResolverProof(
            snapshot_id="snapshot:7",
            snapshot_hash=snapshot.canonical_hash,
            world_revision=7,
            completeness="full",
            policy_version="resolver-policy.1",
            source_bindings_hash=source_authority_bindings_hash(source_authorities),
            authentication_tag="0" * 64,
        ),
    }
    values.update(updates)
    request = AdvisoryCompileRequest(**values)
    return authenticate_advisory_request(request, authority_key=AUTHORITY_KEY)


class StubAdapter:
    def __init__(
        self,
        adapter_id: str,
        outputs: tuple[CandidateDistribution, ...] = (),
        *,
        delay: float = 0,
        error: Exception | None = None,
    ) -> None:
        self.adapter_id = adapter_id
        self.version = "classifier-1"
        self.outputs = outputs
        self.delay = delay
        self.error = error
        self.received: AdvisoryAdapterInput | None = None

    async def classify(self, request: AdvisoryAdapterInput) -> tuple[CandidateDistribution, ...]:
        self.received = request
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.outputs


@pytest.mark.asyncio
async def test_compiles_source_bound_rejectable_advisory_without_mutation_ports() -> None:
    adapter = StubAdapter("emotion", (_distribution(),))

    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(), adapters=(adapter,), timeout_seconds=0.1
    ).compile(_request())

    assert result.world_revision == 7
    assert result.catalog_version == "world-v2-matrix-1"
    assert len(result.advisories) == 1
    assert result.advisories[0].field_id == "appraisal.negative"
    assert result.advisories[0].candidates[0].value == "disappointment"
    assert result.advisories[0].authoritative is False
    assert result.trace[0].status == "success"
    assert adapter.received is not None
    assert adapter.received.snapshot.world_revision == 7
    assert not hasattr(adapter.received, "ledger")


@pytest.mark.asyncio
async def test_timeout_exception_and_invalid_output_fail_open_with_bounded_trace() -> None:
    valid = StubAdapter("a-valid", (_distribution(producer="a-valid@classifier-1"),))
    timeout = StubAdapter("b-timeout", delay=0.04)
    broken = StubAdapter("c-error", error=RuntimeError("secret provider message"))
    invalid = StubAdapter(
        "d-invalid",
        (_distribution(producer="someone-else@classifier-1"),),
    )

    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(timeout, invalid, valid, broken),
        timeout_seconds=0.01,
    ).compile(_request())

    assert [item.adapter_id for item in result.trace] == [
        "a-valid",
        "b-timeout",
        "c-error",
        "d-invalid",
    ]
    assert [item.status for item in result.trace] == [
        "success",
        "timeout",
        "exception",
        "invalid_output",
    ]
    assert "secret" not in result.model_dump_json()
    assert [item.producer for item in result.advisories] == ["a-valid@classifier-1"]


@pytest.mark.asyncio
async def test_timeout_does_not_wait_for_adapter_that_suppresses_cancellation() -> None:
    class CancellationSuppressingAdapter(StubAdapter):
        async def classify(
            self, request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
            return ()

    compiler = AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(CancellationSuppressingAdapter("stubborn"),),
        timeout_seconds=0.01,
    )
    started = time.perf_counter()
    result = await compiler.compile(_request())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.08
    assert result.trace[0].status == "timeout"
    # Let the tracked task settle so the test itself leaves no pending loop work.
    await asyncio.sleep(0.22)


@pytest.mark.asyncio
async def test_timed_out_adapter_has_one_outstanding_slot_and_is_permanently_fused() -> None:
    class PersistentAdapter(StubAdapter):
        calls = 0

        async def classify(
            self, request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            self.calls += 1
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
            return ()

    adapter = PersistentAdapter("persistent")
    compiler = AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(adapter,),
        timeout_seconds=0.01,
    )

    first = await compiler.compile(_request())
    second = await compiler.compile(_request())

    assert first.trace[0].status == "timeout"
    assert second.trace[0].status == "unavailable"
    assert second.trace[0].error_code == "adapter_fused"
    assert adapter.calls == 1
    assert compiler.outstanding_task_count == 1
    assert compiler.fused_adapter_ids == ("persistent",)
    await compiler.aclose(timeout_seconds=0)
    assert compiler.outstanding_task_count == 1
    await asyncio.sleep(0.22)


@pytest.mark.asyncio
async def test_adapter_self_cancellation_is_one_fail_open_trace_not_a_cancelled_turn() -> None:
    class SelfCancellingAdapter(StubAdapter):
        async def classify(
            self, _request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            raise asyncio.CancelledError

    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(), adapters=(SelfCancellingAdapter("cancelled"),)
    ).compile(_request())

    assert result.advisories == ()
    assert result.trace[0].status == "unavailable"
    assert result.trace[0].error_code == "adapter_cancelled"


@pytest.mark.asyncio
async def test_timeout_fuse_recovers_after_bounded_cooldown_once_task_has_stopped() -> None:
    class SlowThenValidAdapter(StubAdapter):
        calls = 0

        async def classify(
            self, request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            self.calls += 1
            if self.calls == 1:
                await asyncio.sleep(1)
            return (_distribution(producer="recovering@classifier-1"),)

    adapter = SlowThenValidAdapter("recovering")
    compiler = AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(adapter,),
        timeout_seconds=0.01,
        timeout_cooldown_seconds=0.01,
    )

    first = await compiler.compile(_request())
    await asyncio.sleep(0.02)
    second = await compiler.compile(_request())

    assert first.trace[0].status == "timeout"
    assert second.trace[0].status == "success"
    assert adapter.calls == 2
    assert compiler.outstanding_task_count == 0


@pytest.mark.asyncio
async def test_forged_candidate_is_revalidated_and_fails_open() -> None:
    forged_candidate = ClassificationCandidate.model_construct(
        value="disappointment",
        weight="oops",
        confidence=7100,
        producer="forged@classifier-1",
        source_refs=("event:message:1",),
        expires_at=NOW + timedelta(seconds=30),
    )
    forged_distribution = CandidateDistribution.model_construct(
        catalog_version="world-v2-matrix-1",
        field_id="appraisal.negative",
        candidates=(forged_candidate,),
        frequency_budget=None,
        produced_at=NOW,
    )

    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("forged", (forged_distribution,)),),
    ).compile(_request())

    assert result.advisories == ()
    assert result.trace[0].status == "invalid_output"
    assert result.trace[0].error_code == "invalid_structure"


@pytest.mark.asyncio
async def test_catalog_expiry_and_source_injection_are_invalid_per_adapter() -> None:
    expired = StubAdapter(
        "expired",
        (_distribution(producer="expired@classifier-1", expires_at=NOW),),
    )
    beyond_turn = StubAdapter(
        "late",
        (_distribution(producer="late@classifier-1", expires_at=NOW + timedelta(minutes=2)),),
    )
    injected = StubAdapter(
        "inject",
        (_distribution(producer="inject@classifier-1", source_refs=("event:not-in-input",)),),
    )

    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(), adapters=(expired, beyond_turn, injected)
    ).compile(_request())

    assert result.advisories == ()
    assert all(trace.status == "invalid_output" for trace in result.trace)
    assert {trace.error_code for trace in result.trace} == {
        "expired_candidate",
        "expiry_out_of_bounds",
        "source_ref_not_in_input",
    }


@pytest.mark.asyncio
async def test_frequency_budget_is_source_checked_preserved_and_identity_bound() -> None:
    valid_budget = FrequencyBudget(
        state="recently_varied",
        window="last-10-turns",
        used=2,
        limit=4,
        source_refs=("thread:life-share",),
    )
    injected_budget = valid_budget.model_copy(update={"source_refs": ("event:not-in-input",)})
    valid = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(
            StubAdapter(
                "emotion",
                (_distribution(frequency_budget=valid_budget),),
            ),
        ),
    ).compile(_request())
    without_budget = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("emotion", (_distribution(),)),),
    ).compile(_request())
    injected = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(
            StubAdapter(
                "emotion",
                (_distribution(frequency_budget=injected_budget),),
            ),
        ),
    ).compile(_request())

    assert valid.advisories[0].frequency_budget == valid_budget
    assert valid.advisories[0].source_refs == (
        "event:message:1",
        "thread:life-share",
    )
    assert valid.advisory_set_id != without_budget.advisory_set_id
    assert injected.advisories == ()
    assert injected.trace[0].error_code == "frequency_budget_source_not_in_input"


@pytest.mark.asyncio
async def test_unknown_non_advisory_and_unknown_value_fields_are_omitted() -> None:
    adapters = (
        StubAdapter(
            "unknown-field",
            (_distribution(field_id="made.up", producer="unknown-field@classifier-1"),),
        ),
        StubAdapter(
            "wrong-owner",
            (
                _distribution(
                    field_id="expression.stance",
                    value="defer",
                    producer="wrong-owner@classifier-1",
                ),
            ),
        ),
        StubAdapter(
            "unknown-value",
            (_distribution(value="must_comfort", producer="unknown-value@classifier-1"),),
        ),
    )

    result = await AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=adapters).compile(
        _request()
    )

    assert result.advisories == ()
    assert [trace.error_code for trace in result.trace] == [
        "unknown_field",
        "catalog_schema_invalid",
        "field_not_advisory_owned",
    ]


def test_duplicate_adapter_is_rejected_and_duplicate_field_from_one_adapter_fails_open() -> None:
    with pytest.raises(ValueError, match="adapter IDs must be unique"):
        AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=(StubAdapter("same"), StubAdapter("same")),
        )


@pytest.mark.asyncio
async def test_duplicate_field_from_one_adapter_fails_open() -> None:
    adapter = StubAdapter(
        "duplicate",
        (
            _distribution(producer="duplicate@classifier-1"),
            _distribution(producer="duplicate@classifier-1"),
        ),
    )
    result = await AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=(adapter,)).compile(
        _request()
    )
    assert result.advisories == ()
    assert result.trace[0].error_code == "duplicate_field"


@pytest.mark.asyncio
async def test_completion_order_cannot_change_output_order_or_identity() -> None:
    slow = StubAdapter(
        "z-slow",
        (
            _distribution(
                field_id="interruption.motive",
                value="care_impulse",
                producer="z-slow@classifier-1",
            ),
        ),
        delay=0.02,
    )
    fast = StubAdapter("a-fast", (_distribution(producer="a-fast@classifier-1"),))
    compiler = AdvisoryCompiler(
        catalog=default_matrix_catalog(), adapters=(slow, fast), timeout_seconds=0.1
    )

    result = await compiler.compile(_request())

    assert [item.producer for item in result.advisories] == [
        "a-fast@classifier-1",
        "z-slow@classifier-1",
    ]
    assert result.advisory_set_id.startswith("advisory-set:")


@pytest.mark.asyncio
async def test_candidate_input_order_cannot_change_canonical_advisory() -> None:
    low = ClassificationCandidate(
        value="dismissal",
        weight=3000,
        confidence=5000,
        producer="emotion@classifier-1",
        source_refs=("event:message:1",),
        expires_at=NOW + timedelta(seconds=30),
    )
    high = ClassificationCandidate(
        value="disappointment",
        weight=7000,
        confidence=8000,
        producer="emotion@classifier-1",
        source_refs=("event:message:1",),
        expires_at=NOW + timedelta(seconds=30),
    )

    def distribution(candidates: tuple[ClassificationCandidate, ...]) -> CandidateDistribution:
        return CandidateDistribution(
            catalog_version="world-v2-matrix-1",
            field_id="appraisal.negative",
            candidates=candidates,
            produced_at=NOW,
        )

    forward = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("emotion", (distribution((low, high)),)),),
    ).compile(_request())
    reverse = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("emotion", (distribution((high, low)),)),),
    ).compile(_request())

    assert forward.advisory_set_id == reverse.advisory_set_id
    assert forward.advisories == reverse.advisories
    assert [item.value for item in forward.advisories[0].candidates] == [
        "disappointment",
        "dismissal",
    ]


@pytest.mark.asyncio
async def test_each_adapter_receives_an_isolated_input_copy() -> None:
    class MutatingAdapter(StubAdapter):
        async def classify(
            self, request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            request.trigger["text"] = "injected"
            request.snapshot.values["relationship"] = {"stage": "enemy"}
            return ()

    class ObservingAdapter(StubAdapter):
        async def classify(
            self, request: AdvisoryAdapterInput
        ) -> tuple[CandidateDistribution, ...]:
            await asyncio.sleep(0.01)
            assert request.trigger["text"] == "算了，当我没说"
            assert request.snapshot.values["relationship"] == {"stage": "friend"}
            return ()

    request = _request()
    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(MutatingAdapter("mutator"), ObservingAdapter("observer")),
    ).compile(request)

    assert all(item.status == "success" for item in result.trace)
    assert request.trigger["text"] == "算了，当我没说"
    assert request.snapshot.values["relationship"] == {"stage": "friend"}


@pytest.mark.asyncio
async def test_interrupt_is_only_a_semantic_candidate_not_a_behavior_verdict() -> None:
    adapter = StubAdapter(
        "interrupt",
        (
            _distribution(
                field_id="interruption.motive",
                value="strong_disagreement",
                producer="interrupt@classifier-1",
            ),
        ),
    )

    result = await AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=(adapter,)).compile(
        _request()
    )

    dumped = result.model_dump()
    assert result.advisories[0].field_id == "interruption.motive"
    assert "action" not in dumped
    assert "must_interrupt" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_no_advisors_returns_immediately_with_empty_candidates() -> None:
    result = await AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=()).compile(
        _request()
    )
    assert result.advisories == ()
    assert result.trace == ()


def test_input_limits_reject_dos_before_calling_adapters() -> None:
    with pytest.raises(ValueError, match="source authorities"):
        _request(source_authorities=_source_authorities(65))
    with pytest.raises(ValidationError, match="snapshot material exceeds"):
        _request(
            snapshot=SnapshotMaterial(
                world_revision=7,
                values={"text": "x" * 70_000},
                canonical_hash="a" * 64,
            )
        )
    with pytest.raises(ValidationError, match="node limit"):
        _request(
            snapshot=SnapshotMaterial(
                world_revision=7,
                values={str(index): index for index in range(4_097)},
                canonical_hash="a" * 64,
            )
        )
    with pytest.raises(ValueError, match="at most 8 advisory adapters"):
        AdvisoryCompiler(
            catalog=default_matrix_catalog(),
            adapters=tuple(StubAdapter(str(index)) for index in range(9)),
        )


@pytest.mark.asyncio
async def test_candidate_and_distribution_limits_fail_open() -> None:
    oversized = tuple(
        _distribution(
            field_id="appraisal.negative" if index % 2 == 0 else "appraisal.base",
            producer="oversized@classifier-1",
        )
        for index in range(17)
    )
    result = await AdvisoryCompiler(
        catalog=default_matrix_catalog(), adapters=(StubAdapter("oversized", oversized),)
    ).compile(_request())
    assert result.advisories == ()
    assert result.trace[0].error_code == "too_many_distributions"


@pytest.mark.asyncio
async def test_million_item_raw_containers_are_rejected_by_constant_time_preflight() -> None:
    candidate = _distribution().candidates[0]
    distribution = _distribution()
    million_distributions = (distribution,) * 1_000_000
    forged_many_candidates = CandidateDistribution.model_construct(
        catalog_version="world-v2-matrix-1",
        field_id="appraisal.negative",
        candidates=(candidate,) * 1_000_000,
        frequency_budget=None,
        produced_at=NOW,
    )

    too_many_distributions = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("many-distributions", million_distributions),),
    ).compile(_request())
    too_many_candidates = await AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(StubAdapter("many-candidates", (forged_many_candidates,)),),
    ).compile(_request())

    assert too_many_distributions.trace[0].error_code == "too_many_distributions"
    assert too_many_candidates.trace[0].error_code == "too_many_candidates"


def test_request_requires_same_revision_aware_time_and_canonical_sources() -> None:
    with pytest.raises(ValidationError, match="same world revision"):
        _request(snapshot=_snapshot({}, revision=6))
    with pytest.raises(ValidationError, match="timezone-aware"):
        _request(logical_time=datetime(2026, 7, 15, 8, 0))
    duplicate = _source_authorities()
    with pytest.raises(ValidationError, match="sorted and unique"):
        _request(source_authorities=(duplicate[1], duplicate[0], duplicate[0]))
    with pytest.raises(ValidationError, match="trigger_ref must be an allowed source"):
        _request(source_authorities=(_source_authorities()[1],))


@pytest.mark.asyncio
async def test_compile_revalidates_forged_request_and_complete_resolver_proof() -> None:
    request = _request()
    forged = request.model_copy(update={"world_revision": 99})
    compiler = AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=())

    with pytest.raises(ValidationError, match="same world revision"):
        await compiler.compile(forged)

    bad_proof = request.resolver_proof.model_copy(update={"source_bindings_hash": "f" * 64})
    with pytest.raises(ValidationError, match="source binding hash mismatch"):
        _request(resolver_proof=bad_proof)
    with pytest.raises(ValidationError, match="snapshot hash does not match"):
        _request(snapshot_hash="e" * 64)
    with pytest.raises(ValidationError, match="canonical hash mismatch"):
        SnapshotMaterial(
            world_revision=7,
            values={"relationship": {"stage": "friend"}},
            canonical_hash="d" * 64,
        )


@pytest.mark.asyncio
async def test_compiler_rejects_self_reported_proof_and_trigger_content_substitution() -> None:
    request = _request()
    unauthenticated = request.model_copy(
        update={
            "resolver_proof": request.resolver_proof.model_copy(
                update={"authentication_tag": "0" * 64}
            )
        }
    )
    substituted_trigger = request.model_copy(
        update={"trigger": {"kind": "user_message", "text": "different content"}}
    )
    substituted_context = request.model_copy(
        update={"recent_context": ({"role": "user", "text": "replacement"},)}
    )
    compiler = AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=())

    with pytest.raises(ValueError, match="authentication failed"):
        await compiler.compile(unauthenticated)
    with pytest.raises(ValidationError, match="trigger authority content hash mismatch"):
        await compiler.compile(substituted_trigger)
    with pytest.raises(ValueError, match="authentication failed"):
        await compiler.compile(substituted_context)


@pytest.mark.asyncio
async def test_nested_million_item_forgery_is_bounded_before_model_dump() -> None:
    request = _request()
    compiler = AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=())
    oversized_sources = request.model_copy(
        update={"source_authorities": (request.source_authorities[0],) * 1_000_000}
    )
    oversized_string = request.model_copy(update={"trigger": {"text": "x" * 1_000_000}})

    with pytest.raises(ValueError, match="source authorities exceed"):
        await compiler.compile(oversized_sources)
    with pytest.raises(ValueError, match="source authorities exceed"):
        authenticate_advisory_request(oversized_sources, authority_key=AUTHORITY_KEY)
    with pytest.raises(ValueError, match="source authorities exceed"):
        source_authority_bindings_hash(oversized_sources.source_authorities)
    with pytest.raises(ValueError, match="character limit"):
        await compiler.compile(oversized_string)

    base = _distribution().candidates[0]
    forged_candidate = ClassificationCandidate.model_construct(
        value=base.value,
        weight=base.weight,
        confidence=base.confidence,
        producer="nested@classifier-1",
        source_refs=("event:message:1",) * 1_000_000,
        expires_at=base.expires_at,
    )
    forged_budget = FrequencyBudget.model_construct(
        state="normal",
        window="turn",
        used=0,
        limit=1,
        source_refs=("event:message:1",) * 1_000_000,
    )
    candidate_distribution = CandidateDistribution.model_construct(
        catalog_version="world-v2-matrix-1",
        field_id="appraisal.negative",
        candidates=(forged_candidate,),
        frequency_budget=None,
        produced_at=NOW,
    )
    budget_distribution = CandidateDistribution.model_construct(
        catalog_version="world-v2-matrix-1",
        field_id="appraisal.negative",
        candidates=(base.model_copy(update={"producer": "nested@classifier-1"}),),
        frequency_budget=forged_budget,
        produced_at=NOW,
    )
    nested_compiler = AdvisoryCompiler(
        catalog=default_matrix_catalog(),
        adapters=(
            StubAdapter("nested", (candidate_distribution,)),
            StubAdapter("nested-budget", (budget_distribution,)),
        ),
    )
    result = await nested_compiler.compile(_request())
    assert result.advisories == ()
    assert all(item.error_code == "invalid_structure" for item in result.trace)


def test_producer_identity_has_one_consistent_256_character_boundary() -> None:
    accepted = StubAdapter("a" * 128)
    accepted.version = "v" * 127
    AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=(accepted,))

    rejected = StubAdapter("a" * 128)
    rejected.version = "v" * 128
    with pytest.raises(ValueError, match="producer identity is oversized"):
        AdvisoryCompiler(catalog=default_matrix_catalog(), adapters=(rejected,))


def test_limits_are_strict_and_prevent_unbounded_configuration() -> None:
    with pytest.raises(ValidationError):
        AdvisoryCompilerLimits(max_adapters=100)
    with pytest.raises(ValidationError):
        AdvisoryCompilerLimits(max_candidates_per_distribution=100)
