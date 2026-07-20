from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
)
from companion_daemon.world_v2.context_resolver import ContextCompileQuery
from companion_daemon.world_v2.deliberation import RouteRequest
from companion_daemon.world_v2.route_hints import RouteHints, derive_route_hints
from companion_daemon.world_v2.semantic_compute_router import SemanticComputeRouter
from test_context_capsule import NOW, _bound, _request
from test_deliberation import _Resolver


def _capsule_with_advisories(
    advisories: tuple[InnerAdvisoryProjection, ...],
):
    request = _request(
        advisories=_bound(advisories, source_ref="event:source:1", slice_name="advisories")
    )
    query = ContextCompileQuery(
        world_id=request.world_id,
        snapshot_id=request.snapshot_id,
        snapshot_hash=request.snapshot_hash,
        actor_ref=request.actor_ref,
        consumer_scope=request.consumer_scope,
        trigger_ref=request.trigger_ref,
        world_revision=request.world_revision,
        deliberation_revision=request.deliberation_revision,
        ledger_sequence=request.ledger_sequence,
        logical_time=request.logical_time,
    )
    return ContextCapsuleCompiler(resolver=_Resolver(request)).compile_for_deliberation(query)


def _advisory(kind: str, *values: str) -> InnerAdvisoryProjection:
    advisory_id = f"advisory:{kind}"
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref=f"{advisory_id}:candidate:{index}",
            value=value,
            weight_bp=6_000,
            confidence_bp=7_000,
        )
        for index, value in enumerate(values, start=1)
    )
    return InnerAdvisoryProjection(
        advisory_id=advisory_id,
        kind=kind,
        source_refs=("event:source:1",),
        candidate_refs=tuple(item.candidate_ref for item in candidates),
        candidates=candidates,
        confidence_bp=7_000,
        expiry=NOW + timedelta(minutes=5),
        producer_version="semantic@test:world-v2-matrix-2",
    )


def _request_for(hints: RouteHints) -> RouteRequest:
    capsule_id = hints.source_capsule_id or "a" * 64
    return RouteRequest(
        capsule_id=capsule_id,
        trigger_ref="event:message:1",
        model_content_hash="b" * 64,
        route_hints=hints,
    )


@pytest.mark.asyncio
async def test_ordinary_hints_take_flash_without_reply_or_content_material() -> None:
    request = _request_for(RouteHints())

    route = await SemanticComputeRouter(thinking_available=True).route(request)

    assert route.tier == "flash"
    assert route.reason_code == "ordinary_compute"
    assert set(request.route_hints.model_dump()) == {
        "source",
        "source_capsule_id",
        "ambiguity",
        "severity",
        "conflict_complexity",
        "continuity",
        "derivation_version",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        ({"severity": "high"}, "high_severity"),
        (
            {"conflict_complexity": "complex", "continuity": "persistent"},
            "persistent_complex_conflict",
        ),
    ],
)
async def test_semantically_expensive_categories_can_take_thinking(
    updates: dict[str, str], reason: str
) -> None:
    hints = RouteHints(**updates)

    route = await SemanticComputeRouter(thinking_available=True).route(_request_for(hints))

    assert route.tier == "thinking"
    assert route.reason_code == reason


@pytest.mark.asyncio
async def test_thinking_unavailable_degrades_to_flash_compute_only() -> None:
    hints = RouteHints(severity="high")

    route = await SemanticComputeRouter(thinking_available=False).route(_request_for(hints))

    assert route.tier == "flash"
    assert route.reason_code == "thinking_unavailable"


@pytest.mark.asyncio
async def test_transient_ambiguity_stays_on_flash_for_first_response_latency() -> None:
    hints = RouteHints(ambiguity="significant", continuity="transient")

    route = await SemanticComputeRouter(thinking_available=True).route(_request_for(hints))

    assert route.tier == "flash"
    assert route.reason_code == "ordinary_compute"


def test_route_request_keeps_legacy_construction_and_rejects_foreign_hint_binding() -> None:
    legacy = RouteRequest(
        capsule_id="a" * 64,
        trigger_ref="event:message:1",
        model_content_hash="b" * 64,
    )
    assert legacy.route_hints == RouteHints()

    with pytest.raises(ValidationError, match="do not belong"):
        RouteRequest(
            capsule_id="a" * 64,
            trigger_ref="event:message:1",
            model_content_hash="b" * 64,
            route_hints=RouteHints(source="trusted_capsule", source_capsule_id="c" * 64),
        )


def test_route_hints_are_derived_from_trusted_categorical_advisories_only() -> None:
    capsule = _capsule_with_advisories(
        (
            _advisory("appraisal.base", "uncertainty"),
            _advisory("appraisal.negative", "dismissal", "boundary_violation"),
        )
    )
    severe_capsule = _capsule_with_advisories(
        (_advisory("appraisal.severity", "acute"),)
    )

    hints = derive_route_hints(capsule)

    assert hints.source_capsule_id == capsule.capsule.capsule_id
    assert hints.ambiguity == "significant"
    assert hints.conflict_complexity == "complex"
    assert derive_route_hints(severe_capsule).severity == "acute"
    assert hints.model_dump_json().find("uncertainty") == -1
    assert hints.model_dump_json().find("dismissal") == -1

    with pytest.raises(ValueError, match="compiler-issued.*handle"):
        derive_route_hints(capsule.capsule)  # type: ignore[arg-type]
