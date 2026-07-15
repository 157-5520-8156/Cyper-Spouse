from __future__ import annotations

import pytest

from companion_daemon.world_v2.schemas import AcceptanceDecisionRef, LedgerProjection


def test_ledger_projection_exposes_empty_v3_fact_acceptance_indexes() -> None:
    projection = LedgerProjection(
        world_id="world:v3-schema",
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
        semantic_hash="0" * 64,
    )

    assert projection.fact_commit_proposal_audits_v2 == ()
    assert projection.acceptance_manifests_v3 == ()


def test_acceptance_decision_ref_accepts_v3_manifest_audit() -> None:
    decision = AcceptanceDecisionRef(
        proposal_id="proposal:v3-schema",
        evaluated_world_revision=7,
        acceptance_id="acceptance:v3-schema",
        status="accepted",
        accepted_change_id="change:v3-schema",
        accepted_change_hash="a" * 64,
        manifest_version="acceptance-manifest.3",
        manifest_hash="b" * 64,
        acceptance_event_ref="event:acceptance:v3-schema",
        acceptance_event_payload_hash="c" * 64,
    )

    assert decision.manifest_version == "acceptance-manifest.3"

    with pytest.raises(ValueError, match="manifest-backed decision"):
        AcceptanceDecisionRef(
            proposal_id="proposal:v3-incomplete",
            evaluated_world_revision=7,
            acceptance_id="acceptance:v3-incomplete",
            status="accepted",
            accepted_change_id="change:v3-incomplete",
            accepted_change_hash="a" * 64,
            manifest_version="acceptance-manifest.3",
        )
