from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from companion_daemon.world_v2.isolated_daemon_acceptance import (
    deterministic_acceptance_exit_code,
    evaluate_deterministic_invariants,
    qualified_inventory_route_models,
)
from companion_daemon.world_v2.expression_draft import qq_expression_capabilities
from companion_daemon.world_v2.character_interior.inbound_tool_contract import (
    InboundToolContracts,
)
from companion_daemon.world_v2.structured_expression_reselection_model import (
    expression_reselection_output_contract,
    expression_reselection_tool_contract,
)


_RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_isolated_daemon_acceptance.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "_girl_agent_isolated_daemon_acceptance_runner",
    _RUNNER_PATH,
)
assert _RUNNER_SPEC is not None and _RUNNER_SPEC.loader is not None
_RUNNER_MODULE = importlib.util.module_from_spec(_RUNNER_SPEC)
sys.modules[_RUNNER_SPEC.name] = _RUNNER_MODULE
_RUNNER_SPEC.loader.exec_module(_RUNNER_MODULE)
_ProviderCaptureState = _RUNNER_MODULE._ProviderCaptureState
_forced_tool_request_hashes = _RUNNER_MODULE._forced_tool_request_hashes
_canonical_hash = _RUNNER_MODULE._canonical_hash
_ci_environment_detected = _RUNNER_MODULE._ci_environment_detected
_daemon_environment = _RUNNER_MODULE._daemon_environment
_network_topology = _RUNNER_MODULE._network_topology
_provider_request_evidence = _RUNNER_MODULE._provider_request_evidence
_validated_provider_settings = _RUNNER_MODULE._validated_provider_settings


def _passing_real_provider_report() -> dict[str, object]:
    """One silent-but-valid run: behavior is free, infrastructure is not."""

    return {
        "continuity": {
            "duplicate_after_restart_visible_effect_count": 0,
            "duplicate_after_restart_model_request_count": 0,
            "duplicate_source_persisted_once": True,
            "cold_replay_matches_live_head": True,
        },
        "interaction_stress": {
            "burst": {
                "source_event_ids": ["burst:1", "burst:2", "burst:3"],
                "coalesced_observation_source_event_ids": [
                    "burst:1",
                    "burst:2",
                    "burst:3",
                ],
                "all_sources_retained": True,
                "distinct_world_action_ids": [],
            },
            "interruption": {
                "overlap_observed": True,
                "second_ingress_reached_provider": True,
                "first_provider_in_flight_when_second_reached_provider": True,
                "latest_source_retained": True,
            },
        },
        "causal_audit": {
            "accepted_character_choice_count": 2,
            "accepted_private_turn_state_count": 2,
            "accepted_character_choice_request_hashes": ["request:1", "request:2"],
            "inner_life_snapshot_correlated_character_choice_request_hashes": [
                "request:1",
                "request:2",
            ],
        },
    }


def test_real_provider_deterministic_acceptance_rejects_duplicate_model_call() -> None:
    report = _passing_real_provider_report()
    continuity = report["continuity"]
    assert isinstance(continuity, dict)
    continuity["duplicate_after_restart_model_request_count"] = 1

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is False
    assert assessment["failure_codes"] == ["duplicate.model_request_replayed"]
    assert (
        deterministic_acceptance_exit_code(
            report=report,
            model_mode="real-provider",
        )
        != 0
    )


def test_real_provider_deterministic_acceptance_allows_model_silence() -> None:
    report = _passing_real_provider_report()

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is True
    assert assessment["failure_codes"] == []
    assert assessment["character_behavior_evaluated"] is False
    assert (
        deterministic_acceptance_exit_code(
            report=report,
            model_mode="real-provider",
        )
        == 0
    )


def test_real_provider_deterministic_acceptance_rejects_duplicate_visible_effect() -> None:
    report = _passing_real_provider_report()
    continuity = report["continuity"]
    assert isinstance(continuity, dict)
    continuity["duplicate_after_restart_visible_effect_count"] = 1

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == ["duplicate.visible_effect_replayed"]


def test_real_provider_deterministic_acceptance_requires_effect_once_replay() -> None:
    report = _passing_real_provider_report()
    continuity = report["continuity"]
    assert isinstance(continuity, dict)
    continuity["duplicate_source_persisted_once"] = False
    continuity["cold_replay_matches_live_head"] = False

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == [
        "source_identity.not_effect_once",
        "cold_replay.mismatch",
    ]


def test_real_provider_deterministic_acceptance_requires_actual_interruption_overlap() -> None:
    report = _passing_real_provider_report()
    stress = report["interaction_stress"]
    assert isinstance(stress, dict)
    interruption = stress["interruption"]
    assert isinstance(interruption, dict)
    interruption["first_provider_in_flight_when_second_reached_provider"] = False

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == ["interruption.no_actual_provider_overlap"]


def test_real_provider_deterministic_acceptance_requires_burst_coalescing_not_speech() -> None:
    report = _passing_real_provider_report()
    stress = report["interaction_stress"]
    assert isinstance(stress, dict)
    burst = stress["burst"]
    assert isinstance(burst, dict)
    burst["coalesced_observation_source_event_ids"] = ["burst:1", "burst:2"]
    # Multiple action identities would mean the one coalesced turn was applied
    # more than once; zero remains valid because silence is the role's choice.
    burst["distinct_world_action_ids"] = ["action:1", "action:2"]

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == [
        "burst.not_coalesced_once",
        "burst.action_identity_not_effect_once",
    ]


def test_real_provider_deterministic_acceptance_allows_burst_to_join_open_batch() -> None:
    report = _passing_real_provider_report()
    stress = report["interaction_stress"]
    assert isinstance(stress, dict)
    burst = stress["burst"]
    assert isinstance(burst, dict)
    burst["coalesced_observation_source_event_ids"] = [
        "message:already-in-open-window",
        "burst:1",
        "burst:2",
        "burst:3",
    ]

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is True
    assert assessment["failure_codes"] == []


def test_real_provider_deterministic_acceptance_requires_correlated_choice_chain() -> None:
    report = _passing_real_provider_report()
    causal = report["causal_audit"]
    assert isinstance(causal, dict)
    causal["accepted_private_turn_state_count"] = 1
    causal["inner_life_snapshot_correlated_character_choice_request_hashes"] = ["request:1"]

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == [
        "causal.private_turn_state_missing",
        "causal.inner_life_snapshot_not_correlated",
    ]


def _source_authority_health_with_unverified_inventory() -> dict[str, object]:
    inventory_contract = "candidate-external-proposition-inventory.5"
    inventory_schema_digest = (
        "cd55ce09687b5b4e68b1a6805244f76e"
        "9c43d4e286b3bee5bb183715a38519fb"
    )
    inventory_models = (
        "openai/gpt-5.4-nano",
        "gpt-5.4-mini",
    )

    def inventory_evidence(
        model: str,
        *,
        sample_count: int,
        success_count: int,
    ) -> dict[str, object]:
        provider = "openrouter" if model.startswith("openai/") else "openai"
        direct = provider == "openai"
        if direct:
            return {
                "status": "unverified",
                "evidence_source": "isolated_contract_diagnostic",
                "reason_code": "source_inventory.contract_response_unverified",
                "provider": provider,
                "model": model,
                "contracts": [],
                "observed_at": "2026-08-01",
                "qualified_at": None,
                "evidence_revision": None,
                "audit_sample_count": None,
                "audit_success_count": None,
                "contract_schema_digests": {},
            }
        return {
            "status": "verified",
            "evidence_source": "production_contract_audit",
            "reason_code": "strict_output.endpoint_capability_verified",
            "provider": provider,
            "model": model,
            "contracts": [inventory_contract],
            "observed_at": "2026-08-01",
            "qualified_at": "2026-08-01",
            "evidence_revision": "inventory-v5-openrouter-gpt54nano-20260801.1",
            "audit_sample_count": sample_count,
            "audit_success_count": success_count,
            "contract_schema_digests": {
                inventory_contract: inventory_schema_digest,
            },
        }

    route_evidence = [
        inventory_evidence(inventory_models[0], sample_count=14, success_count=13),
        inventory_evidence(inventory_models[1], sample_count=9, success_count=9),
    ]
    capability = {
        "inventory_v5": False,
        "coverage_v5": True,
        "roles_independent": False,
    }

    def full_review_evidence(*, provider: str, model: str) -> dict[str, object]:
        is_openrouter = provider == "openrouter"
        return {
            "status": "verified",
            "evidence_source": "production_contract_audit",
            "reason_code": "strict_output.endpoint_capability_verified",
            "provider": provider,
            "model": model,
            "contracts": ["source-closure-review.7"],
            "observed_at": "2026-08-01",
            "qualified_at": "2026-08-01",
            "evidence_revision": (
                "source-review-openrouter-qwen-qwen-plus-20260801.active-v7-rra3.2"
                if is_openrouter
                else "source-review-openai-gpt-4.1-mini-20260801.active-v7-rra3.1"
            ),
            "audit_sample_count": 13 if is_openrouter else 16,
            "audit_success_count": 13,
            "contract_schema_digests": {
                "source-closure-review.7": (
                    "99e95d9e68eb7648f8aa282d675ce0fbbf293078f1d6640031d693d23ee48beb"
                )
            },
        }
    return {
        "status": "ready",
        "visible_review_strategy": "full_source_review",
        "candidate_inventory_model": None,
        "requested_candidate_inventory_model": inventory_models[0],
        "inventory_capability_evidence": dict(route_evidence[0]),
        "inventory_runtime": {
            "status": "unavailable",
            "successful_calls": 0,
            "failed_calls": 0,
            "last_checked_at": None,
            "last_failure_code": None,
            "last_winner_lane": None,
            "lane_models": {
                "primary": inventory_models[0],
                "secondary": inventory_models[1],
            },
            "lane_providers": {
                "primary": "openrouter",
                "secondary": "openai",
            },
        },
        "inventory_transport": {
            "route_count": 0,
            "routes": [],
            "single_transport": False,
            "provider_count": 2,
            "single_provider": False,
            "capability_evidence": route_evidence,
            "attempt_timeout_seconds": 3.0,
            "secondary_reserved_seconds": 8.0,
        },
        "candidate_review_capabilities": {
            "ordinary": dict(capability),
            "recovery": dict(capability),
            "reselection": dict(capability),
        },
        "redundancy_state": "redundant",
        "source_review_authority": {
            "lane_models": {
                "primary": "qwen/qwen-plus",
                "secondary": "gpt-4.1-mini",
            },
            "lane_providers": {
                "primary": "openrouter",
                "secondary": "openai",
            },
            "lane_capability_evidence": {
                "primary": full_review_evidence(
                    provider="openrouter",
                    model="qwen/qwen-plus",
                ),
                "secondary": full_review_evidence(
                    provider="openai",
                    model="gpt-4.1-mini",
                ),
            },
        },
    }


def _source_authority_health_with_qualified_inventory_guard() -> dict[str, object]:
    health = _source_authority_health_with_unverified_inventory()
    health["visible_review_strategy"] = (
        "inventory_v5_guard_then_full_source_review"
    )
    health["active_source_review_protocol"] = (
        "inventory_v5_guard_then_full_source_review.7"
    )
    health["candidate_inventory_model"] = (
        "inventory-availability-authority:"
        "openai/gpt-5.4-nano|gpt-5.4-mini"
    )
    inventory_runtime = health["inventory_runtime"]
    assert isinstance(inventory_runtime, dict)
    inventory_runtime["status"] = "qualified_unprobed"
    transport = health["inventory_transport"]
    assert isinstance(transport, dict)
    route_evidence = transport["capability_evidence"]
    assert isinstance(route_evidence, list)
    route_evidence[1] = {
        "status": "verified",
        "evidence_source": "production_contract_audit",
        "reason_code": "strict_output.endpoint_capability_verified",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "contracts": ["candidate-external-proposition-inventory.5"],
        "observed_at": "2026-08-01",
        "qualified_at": "2026-08-01",
        "evidence_revision": "inventory-v5-openai-gpt54mini-20260801.2",
        "audit_sample_count": 12,
        "audit_success_count": 11,
        "contract_schema_digests": {
            "candidate-external-proposition-inventory.5": (
                "cd55ce09687b5b4e68b1a6805244f76e"
                "9c43d4e286b3bee5bb183715a38519fb"
            ),
        },
    }
    transport.update(
        {
            "route_count": 2,
            "routes": [
                "openrouter:openrouter.ai:openai/gpt-5.4-nano",
                "openai:api.openai.com:gpt-5.4-mini",
            ],
            "single_transport": False,
            "provider_count": 2,
            "single_provider": False,
        }
    )
    capabilities = health["candidate_review_capabilities"]
    assert isinstance(capabilities, dict)
    for lane in ("ordinary", "recovery", "reselection"):
        capabilities[lane] = {
            "inventory_v5": True,
            "coverage_v5": False,
            "roles_independent": True,
        }
    return health


def test_source_authority_report_rejects_unqualified_inventory_lineage() -> None:
    source_health = _source_authority_health_with_unverified_inventory()
    report = _RUNNER_MODULE._source_authority_acceptance_report(
        requested=True,
        first_health={
            "scheduler": {"proactive_source_authority": source_health},
        },
        restart_health={
            "scheduler": {"proactive_source_authority": source_health},
        },
        final_replay={
            "accepted_character_choices": [
                {
                    "proposal_id": "proposal:visible",
                    "disposition": "effect_accepted",
                    "trigger_ref": "event:observation:1",
                    "attempt_id": "attempt:1",
                    "model_call_id": "model-call:author:1",
                    "related_author_model_call_ids": ["model-call:author:1"],
                    "proposal_event_sequence": 10,
                    "source_review_eligible": True,
                },
                {
                    "proposal_id": "proposal:silent",
                    "disposition": "model_silent",
                    "trigger_ref": "event:observation:2",
                    "attempt_id": "attempt:2",
                    "model_call_id": "model-call:author:2",
                    "related_author_model_call_ids": ["model-call:author:2"],
                    "proposal_event_sequence": 20,
                    "source_review_eligible": False,
                },
            ],
            "model_result_records": [
                {
                    "model_call_id": "model-call:inventory:1",
                    "parent_model_call_id": "model-call:author:1",
                    "trigger_ref": "event:observation:1",
                    "attempt_id": "attempt:1",
                    # The durable audit records the one winning leaf, not the
                    # non-voting Nano -> Mini availability wrapper.  Exercise
                    # the fallback winner here so acceptance cannot silently
                    # regress to comparing the wrapper's synthetic model id.
                    "model_id": "gpt-5.4-mini",
                    "router_version": "provider-subcall-audit.1",
                    "route_reason_code": "validation.source_inventory_v5",
                    "status": "proposal_validated",
                    "outcome": "winner",
                    "event_ref": "event:model-result:inventory:1",
                    "event_sequence": 8,
                },
                {
                    "model_call_id": "model-call:coverage:1",
                    "parent_model_call_id": "model-call:author:1",
                    "trigger_ref": "event:observation:1",
                    "attempt_id": "attempt:1",
                    "model_id": "qwen/qwen-plus",
                    "router_version": "provider-subcall-audit.1",
                    "status": "proposal_validated",
                    "outcome": "winner",
                    "event_ref": "event:model-result:coverage:1",
                    "event_sequence": 9,
                },
            ],
        },
    )

    assert report["contract"] == "isolated-source-authority-acceptance.2"
    assert report["first_start_health"] == source_health
    assert report["after_restart_health"] == source_health
    assert report["terminal_candidate_inventory"] == {
        "scope": "terminal_source_review_eligible_character_choices",
        "terminal_character_choice_count": 2,
        "model_silent_terminal_count": 1,
        "non_silent_source_review_ineligible_terminal_count": 0,
        "inventory_eligible_terminal_candidate_count": 1,
        "inventory_proven_terminal_candidate_count": 0,
        "all_inventory_eligible_terminal_candidates_proven": False,
        "qualified_inventory_models": [],
        "evidence": [],
    }
    assert report["terminal_candidate_source_authority"] == {
        "scope": "terminal_source_review_eligible_character_choices",
        "source_review_eligible_terminal_candidate_count": 1,
        "source_authority_proven_terminal_candidate_count": 0,
        "all_source_review_eligible_terminal_candidates_proven": False,
        "qualified_inventory_models": [],
        "qualified_full_review_models": [
            "qwen/qwen-plus",
            "gpt-4.1-mini",
        ],
        "evidence": [],
    }
    assert report["coverage_assurance"] == {
        "proof_source": "private_self_expression_audit",
        "evaluated_by_this_process": False,
        "character_wording_forced": False,
    }


def test_source_authority_report_accepts_strict_full_review_v7_lineage() -> None:
    source_health = _source_authority_health_with_unverified_inventory()
    report = _RUNNER_MODULE._source_authority_acceptance_report(
        requested=True,
        first_health={"scheduler": {"proactive_source_authority": source_health}},
        restart_health={"scheduler": {"proactive_source_authority": source_health}},
        final_replay={
            "accepted_character_choices": [
                {
                    "proposal_id": "proposal:full-review",
                    "disposition": "effect_accepted",
                    "trigger_ref": "event:observation:full",
                    "attempt_id": "attempt:full",
                    "model_call_id": "model-call:author:full",
                    "related_author_model_call_ids": ["model-call:author:full"],
                    "proposal_event_sequence": 12,
                    "source_review_eligible": True,
                }
            ],
            "model_result_records": [
                {
                    "model_call_id": "model-call:full-review:1",
                    "parent_model_call_id": "model-call:author:full",
                    "trigger_ref": "event:observation:full",
                    "attempt_id": "attempt:full",
                    "model_id": "gpt-4.1-mini",
                    "router_version": "provider-subcall-audit.1",
                    "route_reason_code": "validation.source_closure_review_v7",
                    "status": "proposal_validated",
                    "outcome": "winner",
                    "event_ref": "event:model-result:full-review:1",
                    "event_sequence": 10,
                }
            ],
        },
    )

    terminal = report["terminal_candidate_source_authority"]
    assert terminal["all_source_review_eligible_terminal_candidates_proven"] is True
    assert terminal["evidence"] == [
        {
            "proposal_id": "proposal:full-review",
            "winning_protocol": "full_source_closure_review.7",
            "model_call_ids": ["model-call:full-review:1"],
            "model_result_event_refs": ["event:model-result:full-review:1"],
            "models": ["gpt-4.1-mini"],
        }
    ]
    assert report["terminal_candidate_inventory"][
        "all_inventory_eligible_terminal_candidates_proven"
    ] is False


def test_source_authority_requires_qualified_restart_and_terminal_lineage() -> None:
    report = _passing_real_provider_report()
    source_health = _source_authority_health_with_unverified_inventory()
    restart_health = json.loads(json.dumps(source_health))
    restart_authority = restart_health["source_review_authority"]
    restart_authority["lane_capability_evidence"]["secondary"]["status"] = (
        "unverified"
    )
    report["source_authority_acceptance"] = {
        "requested": True,
        "first_start_health": source_health,
        "after_restart_health": restart_health,
        "terminal_candidate_inventory": {
            "all_inventory_eligible_terminal_candidates_proven": False,
        },
        "terminal_candidate_source_authority": {
            "all_source_review_eligible_terminal_candidates_proven": False,
        },
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == [
        "source_authority.restart_not_qualified",
        "source_authority.terminal_source_review_not_proven",
    ]


def test_source_authority_deterministic_invariants_accept_full_review_proof() -> None:
    report = _passing_real_provider_report()
    source_health = _source_authority_health_with_unverified_inventory()
    report["source_authority_acceptance"] = {
        "requested": True,
        "first_start_health": source_health,
        "after_restart_health": source_health,
        "terminal_candidate_inventory": {
            "all_inventory_eligible_terminal_candidates_proven": False,
        },
        "terminal_candidate_source_authority": {
            "all_source_review_eligible_terminal_candidates_proven": True,
        },
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is True
    assert assessment["failure_codes"] == []


def test_source_authority_accepts_qualified_inventory_guard_then_full_v7() -> None:
    report = _passing_real_provider_report()
    source_health = _source_authority_health_with_qualified_inventory_guard()
    assert qualified_inventory_route_models(source_health) == (
        "openai/gpt-5.4-nano",
        "gpt-5.4-mini",
    )
    report["source_authority_acceptance"] = {
        "requested": True,
        "first_start_health": source_health,
        "after_restart_health": source_health,
        "terminal_candidate_inventory": {
            "all_inventory_eligible_terminal_candidates_proven": True,
        },
        "terminal_candidate_source_authority": {
            "all_source_review_eligible_terminal_candidates_proven": True,
        },
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is True
    assert assessment["failure_codes"] == []


def test_source_authority_rejects_full_review_without_exact_route_evidence() -> None:
    report = _passing_real_provider_report()
    source_health = _source_authority_health_with_unverified_inventory()
    review_authority = source_health["source_review_authority"]
    assert isinstance(review_authority, dict)
    review_authority.pop("lane_capability_evidence")
    report["source_authority_acceptance"] = {
        "requested": True,
        "first_start_health": source_health,
        "after_restart_health": source_health,
        "terminal_candidate_inventory": {
            "all_inventory_eligible_terminal_candidates_proven": True,
        },
        "terminal_candidate_source_authority": {
            "all_source_review_eligible_terminal_candidates_proven": True,
        },
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["failure_codes"] == [
        "source_authority.first_start_not_qualified",
        "source_authority.restart_not_qualified",
    ]


def test_unqualified_inventory_does_not_disqualify_strict_full_review() -> None:
    report = _passing_real_provider_report()
    source_health = _source_authority_health_with_unverified_inventory()
    inventory_transport = source_health["inventory_transport"]
    assert isinstance(inventory_transport, dict)
    route_evidence = inventory_transport["capability_evidence"]
    assert isinstance(route_evidence, list)
    fallback_evidence = route_evidence[1]
    assert isinstance(fallback_evidence, dict)
    assert fallback_evidence["status"] == "unverified"
    report["source_authority_acceptance"] = {
        "requested": True,
        "first_start_health": source_health,
        "after_restart_health": source_health,
        "terminal_candidate_inventory": {
            "all_inventory_eligible_terminal_candidates_proven": True,
        },
        "terminal_candidate_source_authority": {
            "all_source_review_eligible_terminal_candidates_proven": True,
        },
        "coverage_assurance": {
            "proof_source": "private_self_expression_audit",
            "evaluated_by_this_process": False,
            "character_wording_forced": False,
        },
    }

    assessment = evaluate_deterministic_invariants(
        report=report,
        model_mode="real-provider",
    )

    assert assessment["passed"] is True
    assert assessment["failure_codes"] == []


def _sanitized_provider_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ARK_API_KEY",
        "CIVITAI_API_KEY",
    ):
        environment[name] = ""
    for name in (
        "CI",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "BUILDKITE",
        "TF_BUILD",
        "JENKINS_URL",
        "CIRCLECI",
        "TRAVIS",
        "APPVEYOR",
        "BITBUCKET_BUILD_NUMBER",
        "TEAMCITY_VERSION",
        "DRONE",
        "CODEBUILD_BUILD_ID",
    ):
        environment[name] = "false"
    return environment


def _poisoned_proxy_environment() -> dict[str, str]:
    environment = _sanitized_provider_environment()
    environment.update(
        {
            "HTTP_PROXY": "http://127.0.0.1:1",
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
            "http_proxy": "http://127.0.0.1:1",
            "https_proxy": "http://127.0.0.1:1",
            "all_proxy": "http://127.0.0.1:1",
            "no_proxy": "",
        }
    )
    return environment


def _capture_provider_presentation(material: dict[str, object]) -> dict[str, object]:
    # The production wire carries this exact contract/authority pair.  Keep
    # the small unit-test helper ergonomic while still exercising the strict
    # acceptance boundary rather than the old recursive marker scan.
    material = dict(material)
    snapshot = material.get("inner_life_snapshot")
    if isinstance(snapshot, dict):
        snapshot = dict(snapshot)
        snapshot.setdefault("contract", "inner-life-snapshot.1")
        snapshot.setdefault("authority", "derived_from_verified_context")
        snapshot.setdefault("availability", "available")
        material["inner_life_snapshot"] = snapshot
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, _response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(material, ensure_ascii=False),
                }
            ],
            "temperature": 0.7,
        },
        authorization="Bearer isolated-test",
    )
    assert status == 200
    return state.report()


def test_loopback_stub_returns_required_tool_arguments_for_tool_request() -> None:
    """The isolated provider boundary must exercise the current forced-tool wire."""

    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    tool_name = "character_inbound_initial_v1"
    status, response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
                {"role": "user", "content": "请按当前角色状态回应。"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    choices = response["choices"]
    assert isinstance(choices, list) and choices
    message = choices[0]["message"]
    assert isinstance(message, dict)
    calls = message.get("tool_calls")
    assert isinstance(calls, list) and len(calls) == 1
    function = calls[0]["function"]
    assert function["name"] == tool_name
    arguments = function["arguments"]
    assert isinstance(arguments, str)
    decoded = json.loads(arguments)
    assert decoded["result_kind"] == "decision"
    assert decoded["appraisal_draft"]["affect"] == "open"
    assert decoded["expression_draft"]["timing_choice"] == "now"


def test_loopback_stub_accepts_deepseek_beta_tool_endpoint() -> None:
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, response = state.handle(
        path="/beta/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
                {"role": "user", "content": "请按当前角色状态回应。"},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "character_inbound_initial_v1",
                        "strict": True,
                        "parameters": {"type": "object"},
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": "character_inbound_initial_v1"},
            },
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    assert response["choices"][0]["message"]["tool_calls"]


def test_provider_capture_reconstructs_deepseek_strict_tool_identity() -> None:
    capabilities = qq_expression_capabilities(
        "napcat",
        recorded_cadence_mode="shadow",
    )
    contract = InboundToolContracts().contract_for(
        phase="initial",
        transport="atomic",
        capabilities=capabilities,
        recall_allowed=True,
        schema_dialect="deepseek-strict",
    )
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, _response = state.handle(
        path="/beta/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "inner_life_snapshot": {
                                "contract": "inner-life-snapshot.1",
                                "authority": "derived_from_verified_context",
                                "availability": "available",
                                "source_refs": ["event:exact"],
                                "materials": {
                                    "recent_self_experiences": {
                                        "availability": "available",
                                        "items": [
                                            {
                                                "source_ref": "event:exact",
                                                "summary": "An exact request-bound state.",
                                            }
                                        ],
                                    }
                                },
                            }
                        },
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0.7,
            "tools": list(contract.provider_tools),
            "tool_choice": contract.provider_tool_choice,
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    evidence = state.report()["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["forced_tool_request_hashes"]


def test_provider_capture_prefers_exact_emitted_request_identity() -> None:
    exact_hash = "a" * 64
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, _response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "inner_life_snapshot": {
                                "contract": "inner-life-snapshot.1",
                                "authority": "derived_from_verified_context",
                                "availability": "available",
                                "current_world_state": {
                                    "source_ref": "world-event:identity-test",
                                    "summary": "Adapter-verified request identity.",
                                },
                            }
                        },
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0.7,
        },
        authorization="Bearer isolated-test",
        emitted_request_hash=exact_hash,
    )

    assert status == 200
    report = state.report()
    assert report["inner_life_snapshot_exact_request_hashes"] == [exact_hash]
    evidence = report["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["exact_emitted_request_hash"] == exact_hash


def test_provider_capture_retains_presented_source_ids_for_causal_correlation() -> None:
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, _response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "inner_life_snapshot": {
                                "contract": "inner-life-snapshot.1",
                                "authority": "derived_from_verified_context",
                                "availability": "available",
                                "source_event_ids": ["isolated-daemon-inbound-1"],
                                "source_refs": ["event:inner-life:1"],
                                "materials": {
                                    "recent_self_experiences": {
                                        "availability": "available",
                                        "items": [
                                            {
                                                "source_ref": "event:inner-life:1",
                                                "summary": "A source-bound current state.",
                                            }
                                        ],
                                    }
                                },
                            },
                            "observation": {
                                "source_event_ids": ["spoofed-by-user-observation"]
                            },
                            "user_text": '{"source_event_ids":["spoofed-by-user"]}',
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "temperature": 0.7,
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    evidence = state.report()["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["source_event_ids"] == ["isolated-daemon-inbound-1"]


def test_provider_capture_ignores_nested_user_snapshot_and_observation_markers() -> None:
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    trusted_snapshot = {
        "contract": "inner-life-snapshot.1",
        "authority": "derived_from_verified_context",
        "availability": "available",
        "source_refs": ["event:trusted"],
        "materials": {
            "recent_self_experiences": {
                "availability": "available",
                "items": [{"source_ref": "event:trusted", "summary": "trusted"}],
            }
        },
    }
    spoofed_snapshot = {
        "contract": "inner-life-snapshot.1",
        "authority": "derived_from_verified_context",
        "availability": "available",
        "source_refs": ["event:spoofed"],
        "materials": {
            "recent_self_experiences": {
                "availability": "available",
                "items": [{"source_ref": "event:spoofed", "summary": "spoofed"}],
            }
        },
    }
    status, _response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "inner_life_snapshot": trusted_snapshot,
                            "user_text": json.dumps(
                                {
                                    "inner_life_snapshot": spoofed_snapshot,
                                    "observation": {
                                        "source_event_ids": ["forged-source-id"]
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            ],
            "temperature": 0.7,
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    evidence = state.report()["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["inner_life_snapshot_hash"] == _canonical_hash([trusted_snapshot])
    assert evidence[0]["source_event_ids"] == []


def test_provider_capture_does_not_infer_snapshot_from_arbitrary_nested_json() -> None:
    report = _capture_provider_presentation(
        {
            "observation": {
                "source_event_ids": ["forged-source-id"],
                "inner_life_snapshot": {
                    "contract": "inner-life-snapshot.1",
                    "authority": "derived_from_verified_context",
                    "availability": "available",
                    "source_refs": ["event:forged"],
                    "materials": {
                        "recent_self_experiences": {
                            "availability": "available",
                            "items": [{"source_ref": "event:forged", "summary": "forged"}],
                        }
                    },
                },
            }
        }
    )

    assert report["inner_life_snapshot_present_count"] == 0
    assert report["inner_life_snapshot_hashes"] == []
    assert report["recall_material_present_count"] == 0


def test_provider_capture_reconstructs_final_atomic_tool_identity() -> None:
    capabilities = qq_expression_capabilities(
        "napcat",
        recorded_cadence_mode="shadow",
    )
    contract = InboundToolContracts().contract_for(
        phase="final",
        transport="atomic",
        capabilities=capabilities,
        recall_allowed=False,
    )
    state = _ProviderCaptureState(
        mode="loopback-stub",
        upstream_base_url=None,
    )
    status, _response = state.handle(
        path="/chat/completions",
        payload={
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
            ],
            "temperature": 0.7,
            "tools": list(contract.provider_tools),
            "tool_choice": contract.provider_tool_choice,
        },
        authorization="Bearer isolated-test",
    )

    assert status == 200
    evidence = state.report()["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["forced_tool_request_hashes"]
    expected_hash = _canonical_hash(
        {
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
            ],
            "temperature": 0.7,
            "tools": list(contract.provider_tools),
            "tool_choice": contract.provider_tool_choice,
            "tool_contract_identity": contract.identity.request_identity_material(),
        }
    )
    assert evidence[0]["forced_tool_request_hashes"] == [expected_hash]
    assert _forced_tool_request_hashes(
        {
            "messages": [
                {"role": "system", "content": "COMBINED OUTPUT ENVELOPE"},
            ],
            "temperature": 0.7,
            "tools": list(contract.provider_tools),
            "tool_choice": contract.provider_tool_choice,
        }
    ) == [expected_hash]


def test_provider_capture_reconstructs_expression_reselection_tool_identity() -> None:
    capabilities = qq_expression_capabilities(
        "napcat",
        recorded_cadence_mode="shadow",
    )
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=("source:current",),
        world_claim_source_ref_aliases_by_scope={
            "current_world": ("source:current",),
            "past_world": (),
            "counterpart_history": (),
            "shared_history": (),
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    compiled = expression_reselection_tool_contract(output_contract)
    messages = [
        {"role": "system", "content": "Return the pinned expression correction."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "contract": "source-closure-reselection.2",
                    "authority": "categorical_failure_only_not_context_or_evidence",
                    "rejected_candidate_sha256": "a" * 64,
                    "rejected_categories": {"ci": [0], "v": [], "p": []},
                    "task": "Choose one complete replacement expression.",
                    "character_reselection_affordance": {
                        "answer_required": False,
                        "satisfy_request_required": False,
                        "valid_timing_choices": ["now", "later", "silent"],
                        "behavior_advice": False,
                    },
                    "final_source_self_check": {
                        "required_before_return": True,
                        "authority": "same_pinned_context_only",
                        "host_text_classifier": False,
                        "world_source_scope": {},
                        "each_external_proposition_requires": (
                            "direct_matching_source_or_explicit_source_free_capability"
                        ),
                        "each_earlier_or_current_companion_life_event_requires": (
                            "own_direct_matching_source_in_same_pinned_context"
                        ),
                        "empty_availability_authorizes_substitute_event": False,
                        "candidate_or_private_turn_state_creates_authority": False,
                        "answer_pressure_can_override_source_boundary": False,
                    },
                    "output_contract": output_contract,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]
    payload = {
        "messages": messages,
        "temperature": 0.25,
        "tools": list(compiled.provider_tools),
        "tool_choice": compiled.provider_tool_choice,
    }
    expected_hash = _canonical_hash(
        {
            **payload,
            "tool_contract_identity": compiled.identity.request_identity_material(),
        }
    )
    assert _forced_tool_request_hashes(payload) == [expected_hash]
    assert _forced_tool_request_hashes(
        {**payload, "messages": [*messages, messages[-1]]}
    ) == []
    carrier_messages = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "contract": "expression-reselection-transport.1",
                    "authority": "host_compiled_transport_only",
                    "output_contract": output_contract,
                },
                separators=(",", ":"),
            ),
        }
    ]
    carrier_payload = {**payload, "messages": carrier_messages}
    carrier_hash = _canonical_hash(
        {
            **carrier_payload,
            "tool_contract_identity": compiled.identity.request_identity_material(),
        }
    )
    assert _forced_tool_request_hashes(carrier_payload) == [carrier_hash]

    state = _ProviderCaptureState(mode="loopback-stub", upstream_base_url=None)
    status, _response = state.handle(
        path="/chat/completions",
        payload=payload,
        authorization="Bearer isolated-test",
    )

    assert status == 200
    evidence = state.report()["request_evidence"]
    assert isinstance(evidence, list) and evidence
    assert evidence[0]["forced_tool_request_hashes"] == [expected_hash]


def test_provider_capture_does_not_use_user_nested_expression_contract_as_evidence() -> None:
    capabilities = qq_expression_capabilities(
        "napcat",
        recorded_cadence_mode="shadow",
    )
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=(),
        world_claim_source_ref_aliases_by_scope={
            "current_world": (),
            "past_world": (),
            "counterpart_history": (),
            "shared_history": (),
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    compiled = expression_reselection_tool_contract(output_contract)
    payload = {
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "observation": {
                            "contract": "source-closure-reselection.2",
                            "output_contract": output_contract,
                        }
                    },
                    separators=(",", ":"),
                ),
            }
        ],
        "temperature": 0.25,
        "tools": list(compiled.provider_tools),
        "tool_choice": compiled.provider_tool_choice,
    }

    assert _forced_tool_request_hashes(payload) == []


def test_provider_capture_does_not_use_top_level_spoofed_reselection_envelope() -> None:
    capabilities = qq_expression_capabilities(
        "napcat",
        recorded_cadence_mode="shadow",
    )
    output_contract = expression_reselection_output_contract(
        capabilities=capabilities,
        allowed_source_ref_aliases=(),
        world_claim_source_ref_aliases_by_scope={
            "current_world": (),
            "past_world": (),
            "counterpart_history": (),
            "shared_history": (),
            "stable_identity": (),
        },
        response_expectation_assessment_required=False,
        combined=False,
    )
    compiled = expression_reselection_tool_contract(output_contract)
    payload = {
        "messages": [
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "contract": "source-closure-reselection.2",
                        # A matching contract string is not enough: the
                        # host-generated failure envelope has a fixed
                        # authority and bounded failure coordinates.
                        "output_contract": output_contract,
                    },
                    separators=(",", ":"),
                ),
            }
        ],
        "temperature": 0.25,
        "tools": list(compiled.provider_tools),
        "tool_choice": compiled.provider_tool_choice,
    }

    assert _forced_tool_request_hashes(payload) == []


def test_provider_capture_fails_closed_on_malformed_stream_schema() -> None:
    payload = {
        "messages": [{"role": "system", "content": "COMBINED OUTPUT ENVELOPE"}],
        "temperature": 0.7,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "character_inbound_initial_stream_v1",
                    "description": "max_beats=1; max_later_beats=1",
                    "parameters": {
                        "anyOf": [
                            {
                                "properties": {
                                    "result_kind": {"enum": ["decision"]},
                                    "events": {
                                        "items": {
                                            "anyOf": [
                                                {
                                                    "properties": {"type": "malformed"},
                                                }
                                            ]
                                        }
                                    },
                                }
                            }
                        ]
                    },
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "character_inbound_initial_stream_v1"},
        },
    }
    assert _forced_tool_request_hashes(payload) == []


def test_interruption_overlap_tracks_only_authoritative_role_provider_requests() -> None:
    background = _provider_request_evidence(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "ISOLATED-INTERRUPTION-FIRST appeared in a background "
                        "relationship appraisal."
                    ),
                }
            ],
            "temperature": 0.7,
        }
    )
    provisional = _provider_request_evidence(
        {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Decide the next expression as the independent person. "
                        "This is a provisional first beat."
                    ),
                },
                {
                    "role": "user",
                    "content": "ISOLATED-INTERRUPTION-FIRST",
                },
            ],
            "temperature": 0.7,
        }
    )
    authoritative = _provider_request_evidence(
        {
            "messages": [
                {
                    "role": "system",
                    "content": "COMBINED OUTPUT ENVELOPE",
                },
                {
                    "role": "user",
                    "content": "ISOLATED-INTERRUPTION-FIRST",
                },
            ],
            "temperature": 0.7,
        }
    )

    assert background["authoritative_role_request"] is False
    assert provisional["authoritative_role_request"] is False
    assert authoritative["authoritative_role_request"] is True


def test_provider_capture_does_not_count_unavailable_or_empty_recall() -> None:
    report = _capture_provider_presentation(
        {
            "inner_life_snapshot": {
                "remembered_material": [],
                "recalled_emotional_associations": {
                    "availability": "unavailable",
                    "items": [
                        {
                            "item_ref": "memory:must-stay-unavailable",
                            "recall_injected": True,
                            "value": {
                                "source_refs": ["event:unavailable"],
                                "text": "This payload is non-empty but unavailable.",
                            },
                        }
                    ],
                },
                "recent_self_experiences": {
                    "availability": "available",
                    "items": [
                        {"recall_injected": True},
                        {
                            "item_ref": "memory:metadata-only",
                            "recall_injected": True,
                            "value": {"memory_kind": "episodic"},
                        },
                    ],
                },
            }
        }
    )

    assert report["recall_material_present_count"] == 0
    assert report["recall_material_hashes"] == []


@pytest.mark.parametrize(
    "inner_life_snapshot",
    [
        {},
        {
            "contract": "current-self-state.1",
            "authority": "derived_from_verified_context",
            "availability": "unavailable",
            "source_refs": [],
            "recent_self_experiences": {"availability": "unavailable"},
            "affect": [],
            "mood": {},
        },
        {
            "availability": "available",
            "source_refs": ["event:placeholder-only"],
            "affect": [{}],
            "mood": [],
        },
    ],
)
def test_provider_capture_does_not_count_empty_inner_life_snapshot_placeholders(
    inner_life_snapshot: dict[str, object],
) -> None:
    report = _capture_provider_presentation({"inner_life_snapshot": inner_life_snapshot})

    assert report["inner_life_snapshot_present_count"] == 0
    assert report["inner_life_snapshot_hashes"] == []
    assert report["emotion_context_present_count"] == 0
    assert report["emotion_context_hashes"] == []


def test_provider_capture_counts_only_inspectable_recall_material() -> None:
    report = _capture_provider_presentation(
        {
            "inner_life_snapshot": {
                "remembered_material": [
                    {
                        "source_ref": "memory:accepted:one",
                        "summary": "A source-bound memory the model can inspect.",
                    }
                ]
            }
        }
    )

    assert report["recall_material_present_count"] == 1
    assert len(report["recall_material_hashes"]) == 1


def test_causal_report_does_not_join_unrelated_run_wide_coverage() -> None:
    accepted_request_hash = "a" * 64
    unrelated_recall_request_hash = "b" * 64
    unrelated_source_review_hash = "c" * 64
    accepted_chain = {
        "source_event_ids": ["source:accepted"],
        "observation_id": "observation:accepted",
        "observation_event_ref": "event:observation:accepted",
        "trigger_ref": "event:observation:accepted",
        "attempt_id": "attempt:accepted",
        "request_hash": accepted_request_hash,
        "model_call_id": "model-call:accepted",
        "parent_model_call_id": None,
        "related_author_model_call_ids": ["model-call:accepted"],
        "character_recall_selected": False,
        "character_recall_trace_result_hash": None,
        "model_result_ref": "model-result:accepted",
        "model_result_event_ref": "event:model-result:accepted",
        "proposal_id": "proposal:accepted",
        "proposal_event_ref": "event:proposal:accepted",
        "acceptance_id": "acceptance:accepted",
        "acceptance_event_ref": "event:acceptance:accepted",
        "expression_plan_id": "plan:accepted",
        "action_id": "action:accepted",
        "action_event_ref": "event:action:accepted",
        "receipt_id": "receipt:accepted",
        "receipt_event_ref": "event:receipt:accepted",
        "receipt_state": "provider_accepted",
        "settlement_event_ref": "event:settlement:accepted",
        "event_sequences": [1, 2, 3, 4, 5, 6, 7],
    }
    final_replay = {
        "event_type_counts": {
            "AppraisalAccepted": 1,
            "AffectEpisodeOpened": 1,
        },
        "model_result_request_hashes": [
            accepted_request_hash,
            unrelated_source_review_hash,
        ],
        "model_result_records": [
            {
                "model_call_id": "model-call:unrelated-review",
                "parent_model_call_id": "model-call:unrelated-author",
                "request_hash": unrelated_source_review_hash,
                "trigger_ref": "event:observation:unrelated",
                "attempt_id": "attempt:unrelated",
                "event_ref": "event:model-result:unrelated-review",
                "event_sequence": 20,
                "character_recall_selected": False,
                "model_id": None,
                "attempted_model_id": "openai/gpt-5.4-nano",
                "router_version": "provider-subcall-audit.1",
                "slot": "primary",
                "status": "main_exception",
                "outcome": "exception",
                "failure_code": "HTTPStatusError:http_403",
            }
        ],
        "recall_trace_count": 0,
        "presented_prefetch_count": 1,
        "private_turn_state_proposal_count": 1,
        "private_turn_state_hashes": ["d" * 64],
        "accepted_private_turn_state_hashes": ["d" * 64],
        "accepted_expression_candidate_count": 1,
        "accepted_expression_chains": [accepted_chain],
        "provider_effected_expression_proposal_count": 1,
        "accepted_character_choice_count": 1,
        "accepted_character_choices": [
            {
                "proposal_id": "proposal:accepted",
                "request_hash": accepted_request_hash,
                "disposition": "effect_accepted",
            }
        ],
        "accepted_private_turn_state_count": 1,
        "accepted_character_choice_request_hashes": [accepted_request_hash],
    }
    provider_audit = {
        "inner_life_snapshot_present_count": 1,
        "recall_material_present_count": 1,
        "source_closure_request_count": 1,
        # The raw provider-presentation hash intentionally differs from the
        # durable author request identity.  Only the guarded adapter handoff
        # supplies the exact hash that can close the causal chain.
        "inner_life_snapshot_model_request_hashes": ["raw-provider-presentation"],
        "inner_life_snapshot_exact_request_hashes": [accepted_request_hash],
        # A mixed run may contain both a forced-tool and a plain role request;
        # retaining both lanes is required for causal correlation.
        "inner_life_snapshot_forced_tool_request_hashes": [
            "forced-only-inner-life-request"
        ],
        "recall_material_model_request_hashes": [unrelated_recall_request_hash],
        "source_closure_model_request_hashes": [unrelated_source_review_hash],
        "request_evidence": [
            {
                "model_invocation_request_hash": accepted_request_hash,
                "inner_life_snapshot_hash": "e" * 64,
                "recall_context_hash": None,
                "emotion_context_hash": "f" * 64,
                "source_closure_request": False,
            },
            {
                "model_invocation_request_hash": unrelated_recall_request_hash,
                "inner_life_snapshot_hash": "1" * 64,
                "recall_context_hash": "2" * 64,
                "emotion_context_hash": "3" * 64,
                "source_closure_request": False,
            },
            {
                "model_invocation_request_hash": unrelated_source_review_hash,
                "inner_life_snapshot_hash": None,
                "recall_context_hash": None,
                "emotion_context_hash": None,
                "source_closure_request": True,
            },
        ],
    }

    causal = _RUNNER_MODULE.build_causal_audit(
        final_replay=final_replay,
        provider_audit=provider_audit,
    )

    assert causal["global_coverage"] == {
        "scope": "run_wide_not_causal",
        "inner_life_snapshot_provider_request_count": 1,
        "recall_material_provider_request_count": 1,
        "source_closure_provider_request_count": 1,
        "character_selected_recall_model_result_count": 0,
        "presented_prefetch_count": 1,
        "appraisal_event_count": 1,
        "affect_event_count": 1,
    }
    assert "inner_life_snapshot_to_recall_to_expression_supported" not in causal
    assert causal["recall_selected_accepted_expression_chains"] == []
    assert len(causal["accepted_expression_causal_chains"]) == 1
    correlated = causal["accepted_expression_causal_chains"][0]
    assert correlated["inner_life_snapshot_presented"] is True
    assert correlated["inner_life_snapshot_hash"] == "e" * 64
    assert correlated["recall_material_presented"] is False
    assert correlated["source_closure_model_calls"] == []
    assert causal["sanitized_model_result_diagnostics"] == [
        {
            "model_call_id": "model-call:unrelated-review",
            "parent_model_call_id": "model-call:unrelated-author",
            "model": "openai/gpt-5.4-nano",
            "router_version": "provider-subcall-audit.1",
            "route_reason_code": None,
            "slot": "primary",
            "status": "main_exception",
            "outcome": "exception",
            "failure_code": "HTTPStatusError:http_403",
        }
    ]


def test_real_provider_topology_distinguishes_daemon_and_external_networks() -> None:
    topology = _network_topology(
        model_mode="real-provider",
        upstream_base_url="https://api.deepseek.com/v1",
    )

    assert topology == {
        "daemon_http_scope": "loopback",
        "onebot_provider_scope": "loopback_capture",
        "model_gateway_scope": "loopback_hash_proxy",
        "model_upstream_scope": "external_https",
        "external_model_network": True,
        "aggregate_loopback_only": False,
    }
    local_topology = _network_topology(
        model_mode="real-provider",
        upstream_base_url="http://127.0.0.1:11434/v1",
    )
    assert local_topology["model_upstream_scope"] == "loopback_configured_provider"
    assert local_topology["external_model_network"] is False
    assert local_topology["aggregate_loopback_only"] is True


def test_production_source_authority_topology_reports_partial_hash_capture() -> None:
    topology = _network_topology(
        model_mode="real-provider",
        upstream_base_url="https://api.deepseek.com/v1",
        production_source_authority=True,
        openai_base_url="https://api.openai.com/v1",
        openrouter_base_url="https://openrouter.ai/api/v1",
    )

    assert topology["model_gateway_scope"] == "loopback_hash_proxy"
    assert topology["model_hash_capture_coverage"] == "partial_deepseek_only"
    assert topology["source_authority_network"] == {
        "enabled": True,
        "reviewer_transport_scope": "direct_external_https",
        "captured_by_deepseek_hash_proxy": False,
        "openai_endpoint_scope": "external_https",
        "openrouter_endpoint_scope": "external_https",
    }
    assert topology["aggregate_loopback_only"] is False


def test_production_source_authority_preserves_only_its_external_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-test-key")
    monkeypatch.setenv("ARK_API_KEY", "must-be-cleared")
    monkeypatch.setenv("CIVITAI_API_KEY", "must-be-cleared")
    monkeypatch.setenv("OPENAI_PROXY_URL", "http://127.0.0.1:9999")

    environment = _daemon_environment(
        database=tmp_path / "isolated.sqlite",
        capture_url="http://127.0.0.1:32123",
        attachment_cache=tmp_path / "attachments",
        model_mode="real-provider",
        provider_capture_url="http://127.0.0.1:32124",
        production_source_authority=True,
    )

    assert environment["NAPCAT_API_URL"] == "http://127.0.0.1:32123"
    assert environment["DEEPSEEK_BASE_URL"] == "http://127.0.0.1:32124"
    assert environment["OPENAI_API_KEY"] == "openai-test-key"
    assert environment["OPENROUTER_API_KEY"] == "openrouter-test-key"
    assert environment["WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED"] == "true"
    assert environment["OPENAI_PROXY_URL"] == ""
    assert environment["ARK_API_KEY"] == ""
    assert environment["CIVITAI_API_KEY"] == ""
    assert environment["WORLD_V2_RECORDED_CADENCE_MODE"] == "shadow"
    assert environment["WORLD_V2_TEST_ONLY_PROVIDER_CAPTURE_AUTHORITY_ID"] == (
        "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
    )


def test_provider_capture_environment_uses_validated_settings_over_ambient_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://wrong.example/v1")
    monkeypatch.setenv("DEEPSEEK_MODEL", "wrong-checkpoint")

    environment = _daemon_environment(
        database=tmp_path / "isolated.sqlite",
        capture_url="http://127.0.0.1:32123",
        attachment_cache=tmp_path / "attachments",
        model_mode="real-provider",
        provider_capture_url="http://127.0.0.1:32124",
        production_source_authority=True,
        deepseek_base_url="https://api.deepseek.com",
        deepseek_model="deepseek-v4-flash",
    )

    assert environment["DEEPSEEK_MODEL"] == "deepseek-v4-flash"
    assert environment["WORLD_V2_TEST_ONLY_PROVIDER_CAPTURE_AUTHORITY_ID"] == (
        "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
    )


def test_provider_acceptance_clears_source_authority_without_separate_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-cleared")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-be-cleared")

    environment = _daemon_environment(
        database=tmp_path / "isolated.sqlite",
        capture_url="http://127.0.0.1:32123",
        attachment_cache=tmp_path / "attachments",
        model_mode="real-provider",
        provider_capture_url="http://127.0.0.1:32124",
        production_source_authority=False,
    )

    assert environment["OPENAI_API_KEY"] == ""
    assert environment["OPENROUTER_API_KEY"] == ""
    assert environment["WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED"] == "false"
    assert environment["WORLD_V2_TEST_ONLY_PROVIDER_CAPTURE_AUTHORITY_ID"] == ""
    assert environment["WORLD_V2_RECORDED_CADENCE_MODE"] == "shadow"


def test_fake_acceptance_also_clears_ambient_source_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "must-be-cleared")
    monkeypatch.setenv("OPENROUTER_API_KEY", "must-be-cleared")
    monkeypatch.setenv("WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED", "true")

    environment = _daemon_environment(
        database=tmp_path / "isolated.sqlite",
        capture_url="http://127.0.0.1:32123",
        attachment_cache=tmp_path / "attachments",
        model_mode="fake",
        provider_capture_url=None,
        production_source_authority=False,
    )

    assert environment["OPENAI_API_KEY"] == ""
    assert environment["OPENROUTER_API_KEY"] == ""
    assert environment["WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED"] == "false"


def test_daemon_environment_rejects_non_ipv4_loopback_onebot_capture(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="OneBot capture must bind exact IPv4 loopback"):
        _daemon_environment(
            database=tmp_path / "isolated.sqlite",
            capture_url="http://localhost:32123",
            attachment_cache=tmp_path / "attachments",
            model_mode="fake",
            provider_capture_url=None,
            production_source_authority=False,
        )


def test_manual_provider_guard_recognizes_additional_ci_runtimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CIRCLECI", "true")

    assert _ci_environment_detected() is True


@pytest.mark.parametrize(
    ("model_mode", "allow_real_provider"),
    [
        ("fake", False),
        ("loopback-stub", False),
        ("real-provider", False),
    ],
)
def test_production_source_authority_requires_both_real_provider_opt_ins(
    model_mode: str,
    allow_real_provider: bool,
) -> None:
    with pytest.raises(
        ValueError,
        match=(
            "--production-source-authority is valid only with "
            "--model-mode real-provider and --allow-real-provider"
        ),
    ):
        _validated_provider_settings(
            model_mode=model_mode,
            allow_real_provider=allow_real_provider,
            production_source_authority=True,
        )


def test_production_source_authority_requires_both_reviewer_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _sanitized_provider_environment()
    environment.update(
        {
            "DEEPSEEK_API_KEY": "deepseek-test-key",
            "OPENAI_API_KEY": "openai-test-key",
        }
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        _validated_provider_settings(
            model_mode="real-provider",
            allow_real_provider=True,
            production_source_authority=True,
        )


def test_cli_rejects_source_authority_without_real_provider_opt_in(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "must-not-exist.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--production-source-authority",
        ],
        cwd=root,
        env=_sanitized_provider_environment(),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "--production-source-authority is valid only" in completed.stderr
    assert not output.exists()


def test_real_provider_mode_requires_explicit_manual_opt_in(tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "must-not-exist.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--model-mode",
            "real-provider",
        ],
        cwd=root,
        env=_sanitized_provider_environment(),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "--allow-real-provider" in completed.stderr
    assert not output.exists()


def test_real_provider_mode_requires_configured_provider_after_opt_in(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "must-not-exist.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--model-mode",
            "real-provider",
            "--allow-real-provider",
        ],
        cwd=root,
        env=_sanitized_provider_environment(),
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "DEEPSEEK_API_KEY" in completed.stderr
    assert not output.exists()


def test_real_provider_mode_refuses_ci_even_with_explicit_opt_in(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "must-not-exist.json"
    environment = _sanitized_provider_environment()
    environment.update({"CI": "true", "DEEPSEEK_API_KEY": "must-not-be-used"})
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--model-mode",
            "real-provider",
            "--allow-real-provider",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "refuses CI environments" in completed.stderr
    assert not output.exists()


def test_real_provider_non_loopback_upstream_requires_https(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[2]
    output = tmp_path / "must-not-exist.json"
    environment = _sanitized_provider_environment()
    environment.update(
        {
            "CI": "false",
            "DEEPSEEK_API_KEY": "must-not-be-used",
            "DEEPSEEK_BASE_URL": "http://provider.invalid",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--model-mode",
            "real-provider",
            "--allow-real-provider",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert completed.returncode != 0
    assert "must use HTTPS" in completed.stderr
    assert not output.exists()


def test_real_daemon_process_recovers_conversation_without_real_qq(
    tmp_path: Path,
) -> None:
    """Exercise the installed QQ daemon entry through HTTP and a cold restart."""

    root = Path(__file__).parents[2]
    output = tmp_path / "isolated-daemon-acceptance.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/run_isolated_daemon_acceptance.py"),
            "--output",
            str(output),
            "--startup-timeout-seconds",
            "45",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["contract"] == "isolated-daemon-process-acceptance.3"
    provenance = report["provenance"]
    assert isinstance(provenance, dict)
    assert isinstance(provenance["git_revision"], str)
    assert len(provenance["git_revision"]) == 40
    assert isinstance(provenance["source_file_sha256"], dict)
    assert set(provenance["source_file_sha256"]) == {
        "acceptance_script",
        "inbound_tool_contract",
        "structured_role_tool_contract",
        "delayed_trigger_catalog",
    }
    assert report["safety"]["capture_transport_only"] is True
    assert report["safety"]["loopback_only"] is True
    assert report["safety"]["onebot_loopback_only"] is True
    assert report["safety"]["production_database_touched"] is False
    assert report["safety"]["real_qq_send_possible"] is False
    assert report["safety"]["daemon_proxy_bypass_enforced"] is True
    assert report["safety"]["real_provider_https_guard_enforced"] is True
    assert report["safety"]["model_provider_network"] == "in_process_fake"
    assert report["daemon"]["entrypoint"] == "companion_daemon.napcat_cli"
    assert report["daemon"]["process_start_count"] == 2
    assert report["liveness"]["first_start"]["status"] == "running"
    assert report["liveness"]["after_restart"]["status"] == "running"
    assert report["liveness"]["first_start"]["scheduler"]["passes_completed"] >= 1
    assert report["liveness"]["after_restart"]["scheduler"]["passes_completed"] >= 1
    assert report["liveness"]["first_start"]["scheduler"]["failures"] == 0
    assert report["liveness"]["after_restart"]["scheduler"]["failures"] == 0
    assert report["continuity"]["submitted_source_event_ids"] == [
        "isolated-daemon-inbound-1",
        "isolated-daemon-inbound-2",
        "isolated-daemon-inbound-3",
    ]
    assert report["continuity"]["cold_replay_source_event_ids"] == [
        "isolated-daemon-inbound-1",
        "isolated-daemon-inbound-2",
        "isolated-daemon-inbound-3",
    ]
    assert report["continuity"]["first_shutdown_replay_source_event_ids"] == [
        "isolated-daemon-inbound-1",
        "isolated-daemon-inbound-2",
    ]
    assert report["continuity"]["duplicate_after_restart_visible_effect_count"] == 0
    assert report["continuity"]["new_turn_after_restart_visible_effect_count"] >= 1
    assert report["continuity"]["cold_replay_matches_live_head"] is True
    assert report["continuity"]["provider_accepted_action_count"] == 3
    assert all(
        turn["http_status"] == 200 and turn["roundtrip_ms"] >= 0
        for turn in report["latency"]["turns"]
    )
    assert report["latency"]["measurement"] == (
        "loopback_http_request_to_daemon_response_including_captured_provider_acceptance"
    )
