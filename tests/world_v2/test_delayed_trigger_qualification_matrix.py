from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from companion_daemon.delayed_trigger_catalog import (
    DelayedTriggerCatalog,
    DelayedTriggerCatalogError,
    ModelContract,
    RetryPolicy,
    load_delayed_trigger_catalog,
    verify_delayed_trigger_catalog,
)
from companion_daemon.world_v2.vertical_registry import VERTICAL_REGISTRY
from companion_daemon.world_v2.delayed_trigger_owner_registry import (
    DELAYED_TRIGGER_OWNERS,
    DelayedTriggerOwner,
)


CATALOG = Path("configs/delayed_trigger_qualification.v1.yaml")
MECHANISMS = Path("configs/mechanism_closure.yaml")


def _mechanism_rows() -> tuple[dict[str, object], ...]:
    raw = yaml.safe_load(MECHANISMS.read_text(encoding="utf-8"))
    return tuple(raw["mechanisms"])


def test_catalog_is_a_complete_read_only_static_declaration_inventory() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)

    assert catalog.schema_version == 1
    assert catalog.qualification_layer == "declaration_only"
    assert {
        "proactive.event_driven",
        "proactive.ambient",
        "proactive.post_silent",
        "proactive.technical_retry",
        "life.ecology",
        "npc.ecology",
        "reflection.life",
        "memory.candidate_consolidation",
        "action.authorized_due",
        "expression.deferred_reply",
        "expression.multibeat",
        "expression.reconsideration",
        "conversation.commitment_due",
        "conversation.thread_expiry",
        "conversation.expectation_expiry",
        "life.activity_occurrence",
        "affect.decay",
        "relationship.silence_aftermath",
    } <= {row.mechanism_id for row in catalog.mechanisms}
    assert all(row.due_identity.merge_dedup_key for row in catalog.mechanisms)
    assert all(
        row.controlled_injection.public_seams
        for row in catalog.mechanisms
        if row.release_status != "dormant"
    )
    by_id = {row.mechanism_id: row for row in catalog.mechanisms}
    assert by_id["memory.candidate_consolidation"].release_status == "dormant"
    assert by_id["memory.candidate_consolidation"].model_contract is None
    assert by_id["memory.candidate_consolidation"].vertical_lanes == ()
    assert by_id["memory.candidate_consolidation"].controlled_injection.public_seams == ()
    assert by_id["memory.candidate_consolidation"].projection_due_fields == ()
    assert by_id["reflection.life"].release_status == "limited"
    assert by_id["reflection.life"].projection_due_fields == ()
    assert by_id["reflection.life"].model_contract is None
    assert by_id["npc.ecology"].release_status == "limited"
    assert by_id["npc.ecology"].projection_due_fields == ()
    assert by_id["perception.refresh_attention"].vertical_lanes == ()
    assert by_id["expression.reconsideration"].trigger_mode == "event_triggered"
    assert by_id["expression.reconsideration"].projection_due_fields == ()
    assert by_id["relationship.silence_aftermath"].trigger_mode == "derived_formula"
    assert by_id["relationship.silence_aftermath"].projection_due_fields == ()
    assert by_id["conversation.thread_expiry"].release_status == "dormant"
    assert by_id["conversation.thread_expiry"].controlled_injection.public_seams == ()
    assert by_id["conversation.thread_expiry"].projection_due_fields == ()
    assert by_id["conversation.expectation_expiry"].release_status == "limited"
    assert not any(
        name.startswith(("execute", "drain", "advance", "write", "inject"))
        for name in dir(catalog)
    )


def test_catalog_cannot_claim_host_qualification_at_root_or_row_level() -> None:
    raw = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    raw["qualification_layer"] = "host_qualified"
    with pytest.raises(ValueError, match="qualification_layer"):
        DelayedTriggerCatalog.model_validate(raw)

    raw["qualification_layer"] = "declaration_only"
    raw["mechanisms"][0]["host_qualified"] = True
    with pytest.raises(ValueError, match="host_qualified"):
        DelayedTriggerCatalog.model_validate(raw)


def test_host_scenario_evidence_is_static_cross_link_not_release_authority() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)

    evidence = catalog.host_scenario("qq-later-text-restart-effect-once.1")

    assert evidence.mechanism_ids == (
        "expression.deferred_reply",
        "conversation.commitment_due",
        "action.authorized_due",
    )
    assert evidence.test_nodeid == (
        "tests/world_v2/test_delayed_trigger_host_qualification.py::"
        "test_public_host_later_text_survives_restart_and_settles_effect_once"
    )
    assert evidence.qualification_scope == "qq_transport_terminal"
    assert "WorldV2PlatformHost.receipt" in evidence.excluded_scope
    assert catalog.qualification_layer == "declaration_only"
    assert not hasattr(evidence, "host_qualified")


def test_verifier_rejects_host_evidence_for_dormant_or_unknown_mechanisms() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    evidence = catalog.host_scenario("qq-later-text-restart-effect-once.1")

    dormant = evidence.model_copy(update={"mechanism_ids": ("conversation.thread_expiry",)})
    with pytest.raises(DelayedTriggerCatalogError, match="dormant"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"host_scenario_evidence": (dormant,)}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )

    missing = evidence.model_copy(update={"mechanism_ids": ("missing.host.evidence",)})
    with pytest.raises(DelayedTriggerCatalogError, match="missing.host.evidence"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"host_scenario_evidence": (missing,)}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_host_scenario_evidence_registers_only_the_committed_public_host_cases() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)

    assert {
        evidence.scenario_id: evidence.test_nodeid
        for evidence in catalog.host_scenario_evidence
    } == {
        "qq-later-text-restart-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_host_qualification.py::"
            "test_public_host_later_text_survives_restart_and_settles_effect_once"
        ),
        "proactive.event-driven-silent-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_proactive_host_qualification.py::"
            "test_public_host_event_driven_silence_is_effect_once"
        ),
        "proactive.ambient-restart-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_proactive_host_qualification.py::"
            "test_public_host_ambient_consideration_survives_restart_and_is_effect_once"
        ),
        "proactive.technical-retry-restart-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_proactive_host_qualification.py::"
            "test_public_host_technical_retry_survives_restart_and_is_effect_once"
        ),
        "proactive.technical-retry-superseded-by-inbound.1": (
            "tests/world_v2/test_delayed_trigger_proactive_host_qualification.py::"
            "test_public_host_new_inbound_supersedes_old_technical_retry"
        ),
        "expression.multibeat_due": (
            "tests/world_v2/test_delayed_trigger_expression_host_qualification.py::"
            "test_public_host_multibeat_due_survives_restart_and_settles_each_beat_once"
        ),
        "expression.interjection_reconsideration": (
            "tests/world_v2/test_delayed_trigger_expression_host_qualification.py::"
            "test_public_host_interjection_reconsideration_cancels_unsent_plan_effect_once"
        ),
        "platform-receipt-provider-accepted-terminal-restart.1": (
            "tests/world_v2/test_delayed_trigger_platform_receipt_host_qualification.py::"
            "test_public_host_receipt_settles_terminal_effect_once_and_cold_replays"
        ),
        "platform-receipt-unknown-late-conflict-restart.1": (
            "tests/world_v2/test_delayed_trigger_platform_receipt_host_qualification.py::"
            "test_public_host_receipt_preserves_unknown_and_records_late_terminal_reconciliation"
        ),
        "affect.decay-boundary-restart-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_affect_silence_host_qualification.py::"
            "test_public_host_affect_decay_obeys_boundary_restart_and_effect_once"
        ),
        "relationship.silence-aftermath-no_change-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_affect_silence_host_qualification.py::"
            "test_public_host_silence_aftermath_is_role_owned_and_effect_once[no_change]"
        ),
        "relationship.silence-aftermath-open_affect-effect-once.1": (
            "tests/world_v2/test_delayed_trigger_affect_silence_host_qualification.py::"
            "test_public_host_silence_aftermath_is_role_owned_and_effect_once[open_affect]"
        ),
        "relationship.silence-aftermath-technical-failure-lease-recovery.1": (
            "tests/world_v2/test_delayed_trigger_affect_silence_host_qualification.py::"
            "test_public_host_silence_technical_failure_is_not_role_no_change"
        ),
        "life.ecology-clock-wake-retry-isolation.1": (
            "tests/world_v2/test_delayed_trigger_life_ecology_host_qualification.py::"
            "test_public_host_life_ecology_wake_terminal_and_retry_does_not_block_inbound"
        ),
        "life.activity-lifecycle-public-host.1": (
            "tests/world_v2/test_delayed_trigger_life_activity_host_qualification.py::"
            "test_public_host_activity_lifecycle_is_role_owned_and_effect_once"
        ),
    }
    assert all(
        {"real_provider_author_transport", "production_stream_expression_episode", "character_autonomy",
         "onebot_provider_callback_normalization", "24_hour_soak"} <= set(evidence.excluded_scope)
        for evidence in catalog.host_scenario_evidence
    )


def test_host_evidence_nodeids_are_present_in_real_pytest_collection() -> None:
    """Keep the declaration linked to an actually collected test node.

    The catalog remains the only scenario inventory.  This check observes
    pytest's collection output rather than copying function names or scanning
    source text, so deleting or renaming a qualified scenario cannot leave the
    static matrix green.
    """

    catalog = load_delayed_trigger_catalog(CATALOG)
    paths = tuple(
        sorted(
            {
                evidence.test_nodeid.split("::", 1)[0]
                for evidence in catalog.host_scenario_evidence
            }
        )
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *paths],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    collected = {
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.lstrip().startswith("=")
    }
    missing = sorted(
        evidence.test_nodeid
        for evidence in catalog.host_scenario_evidence
        if evidence.test_nodeid not in collected
    )
    assert not missing, "host evidence points to uncollected pytest node(s): " + repr(missing)


def test_catalog_rejects_duplicate_host_evidence_scenario_ids_and_nodeids() -> None:
    raw = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    duplicate_scenario = dict(raw["host_scenario_evidence"][0])
    duplicate_scenario["test_nodeid"] += "_duplicate"
    raw["host_scenario_evidence"].append(duplicate_scenario)
    with pytest.raises(ValueError, match="scenario evidence ids"):
        DelayedTriggerCatalog.model_validate(raw)

    raw = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    duplicate_nodeid = dict(raw["host_scenario_evidence"][0])
    duplicate_nodeid["scenario_id"] += ".duplicate"
    raw["host_scenario_evidence"].append(duplicate_nodeid)
    with pytest.raises(ValueError, match="scenario evidence nodeids"):
        DelayedTriggerCatalog.model_validate(raw)


def test_verifier_requires_every_host_evidence_row_to_exclude_the_out_of_scope_claims() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    evidence = catalog.host_scenario("qq-later-text-restart-effect-once.1")
    incomplete = evidence.model_copy(update={"excluded_scope": ("character_autonomy",)})

    with pytest.raises(DelayedTriggerCatalogError, match="missing explicit excluded scope"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"host_scenario_evidence": (incomplete,)}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_life_model_followups_are_independently_and_honestly_declared() -> None:
    by_id = {
        row.mechanism_id: row for row in load_delayed_trigger_catalog(CATALOG).mechanisms
    }

    assert by_id["life.activity_lifecycle"].model_contract == ModelContract(
        purpose="activity_lifecycle_choice",
        contract_identity="character-interior-activity-lifecycle-choice.1",
    )
    assert by_id["life.aftermath_outcome"].model_contract == ModelContract(
        purpose="outcome_selection",
        contract_identity="character-interior-outcome-selection-decision.1",
    )
    assert by_id["life.aftermath_memory"].model_contract == ModelContract(
        purpose="experience_memory_retention",
        contract_identity="character-interior-experience-memory-retention.1",
    )
    assert by_id["life.development"].model_contract == ModelContract(
        purpose="life_development_choice",
        contract_identity="character-interior-life-development-choice.1",
    )
    assert by_id["life.development"].release_status == "limited"
    assert by_id["life.open_world_generation"].release_status == "limited"
    assert by_id["life.open_world_generation"].model_contract is None


def test_life_activity_occurrence_cannot_be_active_without_an_active_producer() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(
        row for row in catalog.mechanisms if row.mechanism_id == "life.activity_occurrence"
    )
    owner = next(
        row for row in DELAYED_TRIGGER_OWNERS if row.mechanism_id == target.mechanism_id
    )

    assert target.release_status == "limited"
    assert owner.producer_dependencies == (
        "life.development",
        "life.open_world_generation",
    )
    falsely_active = target.model_copy(update={"release_status": "active"})
    mechanisms = tuple(
        falsely_active if row.mechanism_id == target.mechanism_id else row
        for row in catalog.mechanisms
    )

    with pytest.raises(DelayedTriggerCatalogError, match="no active producer dependency"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"mechanisms": mechanisms}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_external_world_perception_has_its_own_closure() -> None:
    by_id = {
        row.mechanism_id: row for row in load_delayed_trigger_catalog(CATALOG).mechanisms
    }

    assert by_id["perception.refresh_attention"].closure_mechanisms == (
        "external-world-perception-attention",
    )


def test_media_responsibilities_have_exact_callable_action_owners() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    by_id = {row.mechanism_id: row for row in catalog.mechanisms}
    owners = {row.mechanism_id: row for row in DELAYED_TRIGGER_OWNERS}

    assert "media.pipeline" not in by_id
    assert by_id["media.planning"].action_kinds == ("media_planning",)
    assert set(by_id["media.execution"].action_kinds) == {
        "media_render",
        "media_inspection",
        "media_repair",
    }
    assert by_id["media.delivery"].action_kinds == ("media_delivery",)
    assert {
        kind: callable_owner.__qualname__
        for kind, callable_owner in owners["media.planning"].action_kind_owners
    } == {"media_planning": "MediaPlanningWorker.drain_once"}
    assert {
        kind: callable_owner.__qualname__
        for kind, callable_owner in owners["media.execution"].action_kind_owners
    } == {
        "media_render": "MediaExecutionWorker.drain_once",
        "media_inspection": "MediaExecutionWorker.drain_once",
        "media_repair": "MediaExecutionWorker.drain_once",
    }
    assert {
        kind: callable_owner.__qualname__
        for kind, callable_owner in owners["media.delivery"].action_kind_owners
    } == {"media_delivery": "MediaDeliveryRuntime.authorize_delivery"}
    assert {
        owner.__qualname__ for owner in owners["media.delivery"].runtime_owners
    } == {
        "MediaDeliveryRuntime.authorize_delivery",
        "MediaDeliveryReceiptLifecycle.events_for_terminal_receipt",
    }


def test_commitment_due_closes_advisory_expression_and_action_lifecycles() -> None:
    by_id = {
        row.mechanism_id: row for row in load_delayed_trigger_catalog(CATALOG).mechanisms
    }

    assert set(by_id["conversation.commitment_due"].closure_mechanisms) == {
        "situation-context-and-advisory",
        "expression-beats-deferred-replies",
        "action-budget-receipt-recovery",
    }


def test_catalog_matches_explicit_vertical_and_mechanism_metadata_both_ways() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    mechanism_rows = _mechanism_rows()

    verify_delayed_trigger_catalog(
        catalog,
        vertical_registry=VERTICAL_REGISTRY,
        mechanism_rows=mechanism_rows,
    )


def test_every_released_owner_points_to_a_real_runtime_callable() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    owners = {row.mechanism_id: row for row in DELAYED_TRIGGER_OWNERS}

    assert all(
        owners[row.mechanism_id].runtime_owners
        and all(callable(owner) for owner in owners[row.mechanism_id].runtime_owners)
        for row in catalog.mechanisms
        if row.release_status != "dormant"
    )


def test_verifier_rejects_a_matrix_row_not_claimed_by_the_vertical_registry() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(row for row in catalog.mechanisms if row.vertical_lanes)
    rows = tuple(
        replace(row, delayed_trigger_ids=())
        if target.vertical_lanes[0] == row.lane_id
        else row
        for row in VERTICAL_REGISTRY
    )

    with pytest.raises(DelayedTriggerCatalogError, match=target.mechanism_id):
        verify_delayed_trigger_catalog(
            catalog,
            vertical_registry=rows,
            mechanism_rows=_mechanism_rows(),
        )


def test_verifier_rejects_registry_metadata_absent_from_the_matrix() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    first = VERTICAL_REGISTRY[0]
    rows = (replace(first, delayed_trigger_ids=("missing.from.matrix",)), *VERTICAL_REGISTRY[1:])

    with pytest.raises(DelayedTriggerCatalogError, match="missing.from.matrix"):
        verify_delayed_trigger_catalog(
            catalog,
            vertical_registry=rows,
            mechanism_rows=_mechanism_rows(),
        )


def test_verifier_rejects_one_missing_vertical_pair_even_when_the_id_is_still_claimed() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(row for row in catalog.mechanisms if len(row.vertical_lanes) > 1)
    removed_lane = target.vertical_lanes[0]
    rows = tuple(
        replace(
            row,
            delayed_trigger_ids=tuple(
                item for item in row.delayed_trigger_ids if item != target.mechanism_id
            ),
        )
        if row.lane_id == removed_lane
        else row
        for row in VERTICAL_REGISTRY
    )

    with pytest.raises(DelayedTriggerCatalogError, match=removed_lane):
        verify_delayed_trigger_catalog(
            catalog, vertical_registry=rows, mechanism_rows=_mechanism_rows()
        )


def test_verifier_rejects_one_missing_closure_pair_when_another_closure_still_claims_it() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(row for row in catalog.mechanisms if len(row.closure_mechanisms) > 1)
    removed_closure = target.closure_mechanisms[0]
    rows = tuple(
        {
            **row,
            "delayed_trigger_ids": [
                item
                for item in row.get("delayed_trigger_ids", ())
                if item != target.mechanism_id
            ],
        }
        if row["id"] == removed_closure
        else row
        for row in _mechanism_rows()
    )

    with pytest.raises(DelayedTriggerCatalogError, match=removed_closure):
        verify_delayed_trigger_catalog(
            catalog, vertical_registry=VERTICAL_REGISTRY, mechanism_rows=rows
        )


def test_verifier_rejects_duplicate_owner_ids() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    duplicate = DelayedTriggerOwner(
        mechanism_id=DELAYED_TRIGGER_OWNERS[0].mechanism_id,
    )

    with pytest.raises(DelayedTriggerCatalogError, match="duplicate delayed-trigger owner"):
        verify_delayed_trigger_catalog(
            catalog,
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
            owner_registry=(*DELAYED_TRIGGER_OWNERS, duplicate),
        )


def test_active_rows_cannot_use_private_or_worker_injection_seams() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    row = next(item for item in catalog.mechanisms if item.release_status == "active")
    invalid = row.model_copy(
        update={
            "controlled_injection": row.controlled_injection.model_copy(
                update={"public_seams": ("SomeWorker.drain_one",)}
            )
        }
    )

    mechanisms = tuple(invalid if item.mechanism_id == row.mechanism_id else item for item in catalog.mechanisms)
    with pytest.raises(DelayedTriggerCatalogError, match="public production seam"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"mechanisms": mechanisms}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


@pytest.mark.parametrize(
    ("field", "replacement", "match"),
    (
        ("projection_due_fields", ("MadeUpProjection.never",), "MadeUpProjection.never"),
        ("action_kinds", ("made_up_action",), "made_up_action"),
        (
            "model_contract",
            ModelContract(purpose="fake_model", contract_identity="fake-contract.1"),
            "fake_model",
        ),
        (
            "retry_policy",
            RetryPolicy(policy_id="fake-retry.1", seconds=(1, 2, 3)),
            "fake-retry.1",
        ),
        ("release_status", "dormant", "dormant compatibility status"),
    ),
)
def test_verifier_rejects_runtime_contract_claims_without_an_explicit_owner(
    field: str, replacement: object, match: str
) -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(
        row
        for row in catalog.mechanisms
        if row.release_status == "active" and row.action_kinds and row.model_contract and row.retry_policy
    )
    invalid = target.model_copy(update={field: replacement})
    mechanisms = tuple(
        invalid if row.mechanism_id == target.mechanism_id else row
        for row in catalog.mechanisms
    )

    with pytest.raises(DelayedTriggerCatalogError, match=match):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"mechanisms": mechanisms}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_verifier_rejects_a_nonexistent_public_seam() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = catalog.mechanisms[0]
    invalid = target.model_copy(
        update={
            "controlled_injection": target.controlled_injection.model_copy(
                update={"public_seams": ("DoesNotExist.do_magic",)}
            )
        }
    )
    mechanisms = tuple(
        invalid if row.mechanism_id == target.mechanism_id else row
        for row in catalog.mechanisms
    )

    with pytest.raises(DelayedTriggerCatalogError, match="DoesNotExist.do_magic"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"mechanisms": mechanisms}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_verifier_rejects_an_existing_but_unowned_public_seam() -> None:
    catalog = load_delayed_trigger_catalog(CATALOG)
    target = next(
        row
        for row in catalog.mechanisms
        if row.mechanism_id == "memory.candidate_consolidation"
    )
    invalid = target.model_copy(
        update={
            "controlled_injection": target.controlled_injection.model_copy(
                update={"public_seams": ("WorldV2PlatformHost.receipt",)}
            )
        }
    )
    mechanisms = tuple(
        invalid if row.mechanism_id == target.mechanism_id else row
        for row in catalog.mechanisms
    )

    with pytest.raises(DelayedTriggerCatalogError, match="runtime owner"):
        verify_delayed_trigger_catalog(
            catalog.model_copy(update={"mechanisms": mechanisms}),
            vertical_registry=VERTICAL_REGISTRY,
            mechanism_rows=_mechanism_rows(),
        )


def test_repository_cli_verifies_static_declarations_without_claiming_host_qualification() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/verify_delayed_trigger_catalog.py"],
        cwd=Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "static declarations verified: 28 delayed trigger mechanisms"
