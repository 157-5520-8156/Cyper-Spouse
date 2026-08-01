from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.affect_target_bounds import (
    lower_bounds_from_projection,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute
from companion_daemon.world_v2.schemas import AffectBaselineProjection, LedgerProjection


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _projection() -> LedgerProjection:
    return LedgerProjection(
        world_id="world:affect-target-bounds",
        world_revision=9,
        deliberation_revision=4,
        ledger_sequence=12,
        logical_time=NOW,
        semantic_hash="a" * 64,
        affect_baselines=(
            AffectBaselineProjection(
                dimension="hurt",
                baseline_bp=4200,
                calibration_revision=3,
                policy_version="affect-baseline-calibration.1",
                last_calibrated_at=NOW,
                calibrated_through=NOW,
                last_calibration_basis_hash="d" * 64,
            ),
        ),
    )


def test_lower_bound_manifest_binds_projection_cursor_and_baseline_source() -> None:
    bounds = lower_bounds_from_projection(_projection())

    assert (
        bounds.source_world_revision,
        bounds.source_deliberation_revision,
        bounds.source_ledger_sequence,
    ) == (9, 4, 12)
    assert bounds.minimum_for("hurt") == 4200
    assert bounds.minimum_for("anger") == 500
    hurt = next(item for item in bounds.bounds if item.dimension == "hurt")
    assert hurt.baseline_calibration_revision == 3
    assert hurt.baseline_policy_version == "affect-baseline-calibration.1"
    assert hurt.baseline_basis_hash == "d" * 64


def test_model_input_rejects_a_lower_bound_manifest_from_another_cursor() -> None:
    bounds = lower_bounds_from_projection(_projection()).model_copy(
        update={"source_ledger_sequence": 11}
    )

    with pytest.raises(ValidationError, match="bind the ModelInput cursor"):
        ModelInput(
            call_id="call:affect-target-bounds",
            attempt_id="attempt:affect-target-bounds",
            route=ModelRoute(
                tier="flash",
                reason_code="background",
                router_version="test.1",
            ),
            capsule_id="a" * 64,
            trigger_ref="event:affect-target-bounds",
            evaluated_world_revision=9,
            evaluated_deliberation_revision=4,
            evaluated_ledger_sequence=12,
            model_content_json=json.dumps({"world_revision": 9}),
            affect_target_bounds=bounds,
        )
