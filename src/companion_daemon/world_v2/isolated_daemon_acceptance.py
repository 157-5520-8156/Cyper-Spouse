"""Deterministic verdicts for the isolated daemon acceptance report.

The role model remains free to speak, defer, or stay silent.  This module
judges only infrastructure properties whose truth cannot depend on that
choice.
"""

from __future__ import annotations

import math
from typing import Literal, Mapping
from urllib.parse import urlsplit


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
_VISIBLE_SOURCE_GUARD_CONTRACT = "visible-beat-source-verdict.1"
_VISIBLE_SOURCE_GUARD_SCHEMA_DIGEST = (
    "347069477180408b262fd4ac7da64341c"
    "07b8456d18ad6bd6ab7dee7f9ea78e7"
)
_VISIBLE_SOURCE_GUARD_EVIDENCE_REVISION = (
    "visible-beat-verdict-deepseek-v4-flash-20260810.3"
)
_VISIBLE_SOURCE_GUARD_MODEL = "deepseek-v4-flash"


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


def _exact_loopback_capture_route(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


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
    if runtime_status not in {"qualified_unprobed", "runtime_succeeded"}:
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
    return tuple(models)


def _visible_source_guard_ready(value: object) -> bool:
    """Recognize only the retained correlated Flash contract evidence."""

    health = _mapping(value)
    selective = _mapping(health.get("selective_source_review"))
    runtime = _mapping(selective.get("runtime"))
    strict_output = _mapping(runtime.get("strict_output"))
    contracts = _string_sequence(strict_output.get("contracts"))
    digests = _mapping(strict_output.get("contract_schema_digests"))
    return (
        health.get("status") == "correlated_guard"
        and health.get("visible_review_strategy") == "visible_beat_verdict"
        and health.get("active_source_review_protocol")
        == "visible_beat_source_verdict.1"
        and health.get("source_guard_relation") == "correlated_same_checkpoint"
        and health.get("independent_reviewer") is False
        and health.get("reviewer_model") == _VISIBLE_SOURCE_GUARD_MODEL
        and health.get("redundancy_state") == "single_active_correlated_lane"
        and health.get("source_review_authority") is None
        and selective.get("enabled") is True
        and runtime.get("contract") == "visible-source-review-model.1"
        and runtime.get("model") == _VISIBLE_SOURCE_GUARD_MODEL
        and _exact_loopback_capture_route(runtime.get("route"))
        and runtime.get("semantic_authority_relation")
        == "correlated_same_checkpoint"
        and runtime.get("independent_semantic_authority") is False
        and strict_output.get("status") == "verified"
        and strict_output.get("evidence_source")
        == "isolated_correlated_checkpoint_contract_audit"
        and strict_output.get("reason_code")
        == "strict_output.endpoint_capability_verified"
        and strict_output.get("provider") == "deepseek"
        and strict_output.get("model") == _VISIBLE_SOURCE_GUARD_MODEL
        and contracts == (_VISIBLE_SOURCE_GUARD_CONTRACT,)
        and strict_output.get("observed_at") == "2026-08-10"
        and strict_output.get("qualified_at") == "2026-08-10"
        and strict_output.get("evidence_revision")
        == _VISIBLE_SOURCE_GUARD_EVIDENCE_REVISION
        and _integer(strict_output.get("audit_sample_count")) == 100
        and _integer(strict_output.get("audit_success_count")) == 100
        and digests.get(_VISIBLE_SOURCE_GUARD_CONTRACT)
        == _VISIBLE_SOURCE_GUARD_SCHEMA_DIGEST
    )


def qualified_visible_source_guard_models(value: object) -> tuple[str, ...]:
    """Return the correlated model only for the exact retained guard evidence."""

    return (_VISIBLE_SOURCE_GUARD_MODEL,) if _visible_source_guard_ready(value) else ()


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
            _strings(
                causal.get("inner_life_snapshot_correlated_character_choice_request_hashes")
            )
        )
        if (
            accepted_choice_count is None
            or len(accepted_request_hashes) != accepted_choice_count
            or not set(accepted_request_hashes).issubset(correlated_request_hashes)
        ):
            failures.append("causal.inner_life_snapshot_not_correlated")

    source_authority = _mapping(report.get("source_authority_acceptance"))
    if source_authority.get("requested") is True:
        if model_mode != "real-provider":
            failures.append("source_authority.invalid_model_mode")
        first_health = source_authority.get("first_start_health")
        restart_health = source_authority.get("after_restart_health")
        compact_acceptance_contract = (
            source_authority.get("contract")
            == "isolated-source-authority-acceptance.3"
            and source_authority.get("qualification_scope")
            == "isolated_test_only_correlated_capture"
            and source_authority.get("production_qualification_claimed") is False
        )
        if not (
            compact_acceptance_contract
            and _visible_source_guard_ready(first_health)
        ):
            failures.append("source_authority.first_start_not_qualified")
        if not (
            compact_acceptance_contract
            and _visible_source_guard_ready(restart_health)
        ):
            failures.append("source_authority.restart_not_qualified")
        terminal_source = _mapping(
            source_authority.get("terminal_candidate_source_authority")
        )
        compact_route = (
            compact_acceptance_contract
            and _visible_source_guard_ready(first_health)
            and _visible_source_guard_ready(restart_health)
        )
        if compact_route and terminal_source.get("proof_contract") != (
            _VISIBLE_SOURCE_GUARD_CONTRACT
        ):
            failures.append("source_authority.terminal_source_review_not_proven")
        eligible_count = _integer(
            terminal_source.get(
                "source_review_eligible_terminal_candidate_count"
            )
        )
        proven_count = _integer(
            terminal_source.get(
                "source_authority_proven_terminal_candidate_count"
            )
        )
        compact_terminal_proven = (
            eligible_count is not None
            and eligible_count > 0
            and proven_count == eligible_count
            and terminal_source.get(
                "all_source_review_eligible_terminal_candidates_proven"
            )
            is True
        )
        terminal_not_proven = (
            not compact_terminal_proven
            if compact_route
            else terminal_source.get(
                "all_source_review_eligible_terminal_candidates_proven"
            )
            is not True
        )
        if terminal_not_proven:
            if "source_authority.terminal_source_review_not_proven" not in failures:
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
    "qualified_inventory_route_models",
    "qualified_visible_source_guard_models",
]
