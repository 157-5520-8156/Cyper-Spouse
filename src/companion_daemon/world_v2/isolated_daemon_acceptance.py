"""Deterministic verdicts for the isolated daemon acceptance report.

The role model remains free to speak, defer, or stay silent.  This module
judges only infrastructure properties whose truth cannot depend on that
choice.
"""

from __future__ import annotations

import math
from typing import Literal, Mapping


ModelMode = Literal["fake", "loopback-stub", "real-provider"]
_INVENTORY_V5_CONTRACT = "candidate-external-proposition-inventory.5"
_INVENTORY_V5_SCHEMA_DIGEST = (
    "cd55ce09687b5b4e68b1a6805244f76e"
    "9c43d4e286b3bee5bb183715a38519fb"
)
_QUALIFIED_INVENTORY_MODELS = (
    "openai/gpt-5.4-nano",
    "gpt-5.4-mini",
)
_QUALIFIED_INVENTORY_RELEASE_EVIDENCE = {
    "openai/gpt-5.4-nano": (
        "openrouter",
        "production_contract_audit",
        "inventory-v5-openrouter-gpt54nano-20260801.1",
        14,
        13,
        "openrouter:openrouter.ai:openai/gpt-5.4-nano",
    ),
    "gpt-5.4-mini": (
        "openai",
        "production_contract_audit",
        "inventory-v5-openai-gpt54mini-20260801.2",
        12,
        11,
        "openai:api.openai.com:gpt-5.4-mini",
    ),
}
_FULL_SOURCE_REVIEW_CONTRACT = "source-closure-review.7"
_FULL_SOURCE_REVIEW_SCHEMA_DIGEST = (
    "99e95d9e68eb7648f8aa282d675ce0f"
    "bbf293078f1d6640031d693d23ee48beb"
)
_QUALIFIED_FULL_REVIEW_RELEASES = {
    ("openrouter", "qwen/qwen-plus"): (
        "source-review-openrouter-qwen-qwen-plus-20260801.active-v7-rra3.2",
        13,
        13,
    ),
    ("openai", "gpt-4.1-mini"): (
        "source-review-openai-gpt-4.1-mini-20260801.active-v7-rra3.1",
        16,
        13,
    ),
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)


def _string_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and bool(item) for item in value
    ):
        return ()
    return tuple(value)


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) and normalized > 0 else None


def qualified_inventory_route_models(value: object) -> tuple[str, ...]:
    """Return leaf models only when the complete failover lane is qualified.

    The wrapper model id is a scheduling identity, not provider capability
    evidence.  Acceptance therefore derives the eligible durable winner ids
    from every release-qualified route and fails closed if either failover
    lane lacks evidence for the exact Inventory V5 schema.
    """

    health = _mapping(value)
    transport = _mapping(health.get("inventory_transport"))
    raw_evidence = transport.get("capability_evidence")
    if not isinstance(raw_evidence, (list, tuple)) or len(raw_evidence) != 2:
        return ()
    route_count = _integer(transport.get("route_count"))
    routes = _string_sequence(transport.get("routes"))
    if route_count != len(raw_evidence) or len(routes) != route_count:
        return ()
    if len(set(routes)) != len(routes) or transport.get("single_transport") is not False:
        return ()

    models: list[str] = []
    providers: list[str] = []
    for raw_item in raw_evidence:
        evidence = _mapping(raw_item)
        provider = evidence.get("provider")
        model = evidence.get("model")
        contracts = _string_sequence(evidence.get("contracts"))
        digests = _mapping(evidence.get("contract_schema_digests"))
        sample_count = _integer(evidence.get("audit_sample_count"))
        success_count = _integer(evidence.get("audit_success_count"))
        expected_release_evidence = (
            _QUALIFIED_INVENTORY_RELEASE_EVIDENCE.get(model)
            if isinstance(model, str)
            else None
        )
        if expected_release_evidence is None:
            return ()
        (
            expected_provider,
            expected_evidence_source,
            expected_revision,
            expected_sample_count,
            expected_success_count,
            _expected_route,
        ) = expected_release_evidence
        audit_counts_match = (
            sample_count == expected_sample_count
            and success_count == expected_success_count
        )
        if not (
            evidence.get("status") == "verified"
            and evidence.get("evidence_source") == expected_evidence_source
            and evidence.get("reason_code")
            == "strict_output.endpoint_capability_verified"
            and provider == expected_provider
            and isinstance(model, str)
            and bool(model)
            and contracts == (_INVENTORY_V5_CONTRACT,)
            and digests.get(_INVENTORY_V5_CONTRACT) == _INVENTORY_V5_SCHEMA_DIGEST
            and isinstance(evidence.get("qualified_at"), str)
            and bool(evidence.get("qualified_at"))
            and isinstance(evidence.get("evidence_revision"), str)
            and bool(evidence.get("evidence_revision"))
            and evidence.get("evidence_revision") == expected_revision
            and audit_counts_match
            and (
                expected_sample_count is None
                or (
                    expected_sample_count > 0
                    and expected_success_count is not None
                    and 0 < expected_success_count <= expected_sample_count
                )
            )
        ):
            return ()
        models.append(model)
        providers.append(provider)

    if len(set(models)) != len(models):
        return ()
    if tuple(models) != _QUALIFIED_INVENTORY_MODELS:
        return ()
    expected_routes = tuple(
        _QUALIFIED_INVENTORY_RELEASE_EVIDENCE[model][5] for model in models
    )
    if routes != expected_routes:
        return ()
    provider_count = _integer(transport.get("provider_count"))
    distinct_provider_count = len({provider.casefold() for provider in providers})
    if provider_count != distinct_provider_count:
        return ()
    expected_single_provider = distinct_provider_count == 1
    if transport.get("single_provider") is not expected_single_provider:
        return ()
    if distinct_provider_count != 2 or expected_single_provider:
        return ()
    attempt_timeout = _positive_number(transport.get("attempt_timeout_seconds"))
    secondary_reserve = _positive_number(transport.get("secondary_reserved_seconds"))
    if (
        attempt_timeout is None
        or attempt_timeout > 10
        or secondary_reserve is None
        or secondary_reserve < attempt_timeout
    ):
        return ()

    expected_wrapper = "inventory-availability-authority:" + "|".join(models)
    if health.get("candidate_inventory_model") != expected_wrapper:
        return ()
    if health.get("requested_candidate_inventory_model") != models[0]:
        return ()
    primary_evidence = _mapping(health.get("inventory_capability_evidence"))
    if (
        primary_evidence != _mapping(raw_evidence[0])
        or primary_evidence.get("status") != "verified"
        or primary_evidence.get("provider") != providers[0]
        or primary_evidence.get("model") != models[0]
        or _mapping(primary_evidence.get("contract_schema_digests")).get(
            _INVENTORY_V5_CONTRACT
        )
        != _INVENTORY_V5_SCHEMA_DIGEST
    ):
        return ()

    runtime = _mapping(health.get("inventory_runtime"))
    runtime_status = runtime.get("status")
    if runtime_status not in {
        "qualified_unprobed",
        "runtime_succeeded",
        "degraded",
    }:
        return ()
    lane_models = _mapping(runtime.get("lane_models"))
    lane_providers = _mapping(runtime.get("lane_providers"))
    if (
        tuple(lane_models.get(lane) for lane in ("primary", "secondary"))
        != tuple(models)
        or tuple(lane_providers.get(lane) for lane in ("primary", "secondary"))
        != tuple(providers)
    ):
        return ()
    last_winner_lane = runtime.get("last_winner_lane")
    if runtime_status == "qualified_unprobed" and last_winner_lane is not None:
        return ()
    if runtime_status == "runtime_succeeded" and last_winner_lane not in {
        "primary",
        "secondary",
    }:
        return ()
    if (
        runtime_status == "degraded"
        and runtime.get("last_winner_protocol")
        != "full_source_closure_review.7"
    ):
        return ()
    return tuple(models)


def qualified_full_review_route_models(value: object) -> tuple[str, ...]:
    """Return exact reviewer leaves qualified for strict full review v7."""

    health = _mapping(value)
    authority = _mapping(health.get("source_review_authority"))
    lane_models = _mapping(authority.get("lane_models"))
    lane_providers = _mapping(authority.get("lane_providers"))
    lane_evidence = _mapping(authority.get("lane_capability_evidence"))
    models: list[str] = []
    for lane in ("primary", "secondary"):
        model = lane_models.get(lane)
        provider = lane_providers.get(lane)
        evidence = _mapping(lane_evidence.get(lane))
        if not isinstance(model, str) or not isinstance(provider, str):
            return ()
        expected_release = _QUALIFIED_FULL_REVIEW_RELEASES.get((provider, model))
        contracts = _string_sequence(evidence.get("contracts"))
        digests = _mapping(evidence.get("contract_schema_digests"))
        if not (
            expected_release is not None
            and evidence.get("status") == "verified"
            and evidence.get("evidence_source")
            == "production_contract_audit"
            and evidence.get("reason_code")
            == "strict_output.endpoint_capability_verified"
            and evidence.get("provider") == provider
            and evidence.get("model") == model
            and _FULL_SOURCE_REVIEW_CONTRACT in contracts
            and digests.get(_FULL_SOURCE_REVIEW_CONTRACT)
            == _FULL_SOURCE_REVIEW_SCHEMA_DIGEST
            and evidence.get("evidence_revision") == expected_release[0]
            and evidence.get("audit_sample_count") == expected_release[1]
            and evidence.get("audit_success_count") == expected_release[2]
            and isinstance(evidence.get("qualified_at"), str)
            and bool(evidence.get("qualified_at"))
        ):
            return ()
        models.append(model)
    if len(set(models)) != len(models):
        return ()
    return tuple(models)


def _v5_source_authority_ready(value: object) -> bool:
    health = _mapping(value)
    capabilities = _mapping(health.get("candidate_review_capabilities"))
    strategy = health.get("visible_review_strategy")
    lanes = tuple(
        _mapping(capabilities.get(lane))
        for lane in ("ordinary", "recovery", "reselection")
    )
    inventory_and_independence_ready = all(
        lane.get("inventory_v5") is True
        and lane.get("roles_independent") is True
        for lane in lanes
    )
    if not (
        health.get("status") == "ready"
        and len(qualified_inventory_route_models(health)) == 2
        and health.get("redundancy_state") == "redundant"
        and inventory_and_independence_ready
    ):
        return False
    if strategy == "inventory_v5_coverage_v5":
        return all(lane.get("coverage_v5") is True for lane in lanes)
    if strategy == "inventory_v5_guard_then_full_source_review":
        return (
            all(lane.get("coverage_v5") is False for lane in lanes)
            and len(qualified_full_review_route_models(health)) == 2
            and health.get("active_source_review_protocol")
            == "inventory_v5_guard_then_full_source_review.7"
        )
    return False


def _full_source_authority_ready(value: object) -> bool:
    health = _mapping(value)
    return (
        health.get("status") == "ready"
        and health.get("visible_review_strategy") == "full_source_review"
        and len(qualified_full_review_route_models(health)) == 2
        and health.get("redundancy_state") == "redundant"
    )


def evaluate_deterministic_invariants(
    *,
    report: Mapping[str, object],
    model_mode: ModelMode,
) -> dict[str, object]:
    """Return a machine-readable verdict without evaluating character behavior."""

    failures: list[str] = []
    continuity = _mapping(report.get("continuity"))
    duplicate_visible_effects = _integer(
        continuity.get("duplicate_after_restart_visible_effect_count")
    )
    if duplicate_visible_effects != 0:
        failures.append("duplicate.visible_effect_replayed")
    duplicate_model_requests = _integer(
        continuity.get("duplicate_after_restart_model_request_count")
    )
    if model_mode in {"loopback-stub", "real-provider"} and duplicate_model_requests != 0:
        failures.insert(0, "duplicate.model_request_replayed")
    if continuity.get("duplicate_source_persisted_once") is not True:
        failures.append("source_identity.not_effect_once")
    if continuity.get("cold_replay_matches_live_head") is not True:
        failures.append("cold_replay.mismatch")

    if model_mode in {"loopback-stub", "real-provider"}:
        stress = _mapping(report.get("interaction_stress"))
        interruption = _mapping(stress.get("interruption"))
        actual_overlap = all(
            interruption.get(name) is True
            for name in (
                "overlap_observed",
                "second_ingress_reached_provider",
                "first_provider_in_flight_when_second_reached_provider",
            )
        )
        if not actual_overlap:
            failures.append("interruption.no_actual_provider_overlap")
        if interruption.get("latest_source_retained") is not True:
            failures.append("interruption.latest_source_not_retained")

        burst = _mapping(stress.get("burst"))
        burst_sources = _strings(burst.get("source_event_ids"))
        coalesced_sources = _strings(burst.get("coalesced_observation_source_event_ids"))
        # A stress burst may arrive while the ingress window opened by the
        # immediately preceding message is still live.  That is still one
        # correct coalesced Observation: require every burst identity in the
        # reported group, rather than incorrectly requiring the group to
        # contain no earlier source identity.
        if not burst_sources or not set(burst_sources).issubset(coalesced_sources):
            failures.append("burst.not_coalesced_once")
        if burst.get("all_sources_retained") is not True:
            failures.append("burst.source_not_retained")
        action_ids = _strings(burst.get("distinct_world_action_ids"))
        if len(action_ids) > 1:
            failures.append("burst.action_identity_not_effect_once")

        causal = _mapping(report.get("causal_audit"))
        accepted_choice_count = _integer(causal.get("accepted_character_choice_count"))
        accepted_private_state_count = _integer(causal.get("accepted_private_turn_state_count"))
        if accepted_choice_count is None or accepted_choice_count < 1:
            failures.append("causal.no_terminal_character_choice")
        elif accepted_private_state_count != accepted_choice_count:
            failures.append("causal.private_turn_state_missing")
        accepted_request_hashes = _strings(causal.get("accepted_character_choice_request_hashes"))
        correlated_request_hashes = set(
            _strings(causal.get("current_self_correlated_character_choice_request_hashes"))
        )
        if (
            accepted_choice_count is None
            or len(accepted_request_hashes) != accepted_choice_count
            or not set(accepted_request_hashes).issubset(correlated_request_hashes)
        ):
            failures.append("causal.current_self_not_correlated")

    source_authority = _mapping(report.get("source_authority_acceptance"))
    if source_authority.get("requested") is True:
        if model_mode != "real-provider":
            failures.append("source_authority.invalid_model_mode")
        first_health = source_authority.get("first_start_health")
        restart_health = source_authority.get("after_restart_health")
        if not (
            _v5_source_authority_ready(first_health)
            or _full_source_authority_ready(first_health)
        ):
            failures.append("source_authority.first_start_not_qualified")
        if not (
            _v5_source_authority_ready(restart_health)
            or _full_source_authority_ready(restart_health)
        ):
            failures.append("source_authority.restart_not_qualified")
        terminal_source = _mapping(
            source_authority.get("terminal_candidate_source_authority")
        )
        if terminal_source.get("all_source_review_eligible_terminal_candidates_proven") is not True:
            failures.append("source_authority.terminal_source_review_not_proven")
        coverage = _mapping(source_authority.get("coverage_assurance"))
        if (
            coverage.get("proof_source") != "private_self_expression_audit"
            or coverage.get("evaluated_by_this_process") is not False
            or coverage.get("character_wording_forced") is not False
        ):
            failures.append("source_authority.coverage_assurance_invalid")
    return {
        "contract": "isolated-daemon-deterministic-invariants.1",
        "model_mode": model_mode,
        "passed": not failures,
        "failure_codes": failures,
        "character_behavior_evaluated": False,
    }


def deterministic_acceptance_exit_code(
    *,
    report: Mapping[str, object],
    model_mode: ModelMode,
) -> int:
    """Map the deterministic verdict to the CLI process contract."""

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode=model_mode,
    )
    return 0 if assessment["passed"] is True else 2


__all__ = [
    "ModelMode",
    "deterministic_acceptance_exit_code",
    "evaluate_deterministic_invariants",
    "qualified_full_review_route_models",
    "qualified_inventory_route_models",
]
