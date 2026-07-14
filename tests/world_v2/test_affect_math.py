from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from itertools import permutations

import pytest

from companion_daemon.world_v2 import affect_math
from companion_daemon.world_v2.affect_math import (
    DecayAnchor,
    DecayProfile,
    FACTOR_TABLE_DIGEST,
    advance_decay,
    bounded_saturation_bp,
    decay_intensity_bp,
    materialize_decay,
    relative_baseline_saturation_bp,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def test_half_life_golden_values() -> None:
    anchor = DecayAnchor(intensity_bp=10_000, anchored_at=NOW)
    profile = DecayProfile(half_life_seconds=60)

    assert decay_intensity_bp(anchor, profile, NOW) == 10_000
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=30)) == 7_071
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=60)) == 5_000
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=120)) == 2_500


def test_exact_half_life_uses_round_half_even() -> None:
    profile = DecayProfile(half_life_seconds=10)

    one = DecayAnchor(intensity_bp=1, anchored_at=NOW)
    three = DecayAnchor(intensity_bp=3, anchored_at=NOW)

    assert decay_intensity_bp(one, profile, NOW + timedelta(seconds=10)) == 0
    assert decay_intensity_bp(three, profile, NOW + timedelta(seconds=10)) == 2


def test_delay_prevents_decay_until_delay_has_elapsed() -> None:
    anchor = DecayAnchor(intensity_bp=8_000, anchored_at=NOW)
    profile = DecayProfile(half_life_seconds=60, delay_seconds=30)

    assert decay_intensity_bp(anchor, profile, NOW + timedelta(microseconds=29_999_999)) == 8_000
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=30)) == 8_000
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=90)) == 4_000


def test_effective_target_uses_highest_floor_baseline_or_residue() -> None:
    anchor = DecayAnchor(
        intensity_bp=9_700,
        anchored_at=NOW,
        baseline_bp=1_000,
        residue_bp=1_700,
    )
    profile = DecayProfile(half_life_seconds=60, floor_bp=1_200)

    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=60)) == 5_700
    assert decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=900)) == 1_700


def test_direct_and_incremental_materialization_are_identical() -> None:
    anchor = DecayAnchor(
        intensity_bp=9_321,
        anchored_at=NOW,
        baseline_bp=300,
        residue_bp=700,
    )
    profile = DecayProfile(half_life_seconds=83, floor_bp=500, delay_seconds=7)
    final_time = NOW + timedelta(seconds=527, microseconds=431_219)

    direct = materialize_decay(anchor, profile, final_time)
    incremental = materialize_decay(anchor, profile, NOW + timedelta(seconds=19))
    for seconds in (61, 137, 281):
        incremental = advance_decay(incremental, NOW + timedelta(seconds=seconds))
    incremental = advance_decay(incremental, final_time)

    assert incremental == direct
    assert incremental.anchor is anchor


def test_decay_is_monotonic_and_bounded_across_profiles() -> None:
    for half_life in (1, 7, 60, 3_600, 86_400):
        anchor = DecayAnchor(
            intensity_bp=9_999,
            anchored_at=NOW,
            baseline_bp=321,
            residue_bp=654,
        )
        profile = DecayProfile(half_life_seconds=half_life, floor_bp=432)
        readings = [
            decay_intensity_bp(anchor, profile, NOW + timedelta(seconds=offset))
            for offset in (0, half_life, half_life * 2, half_life * 5, half_life * 15)
        ]

        assert readings == sorted(readings, reverse=True)
        assert all(654 <= reading <= 9_999 for reading in readings)
        assert readings[-1] == 654


def test_timezone_equivalent_instants_produce_same_result() -> None:
    anchor = DecayAnchor(intensity_bp=10_000, anchored_at=NOW)
    profile = DecayProfile(half_life_seconds=60)
    china_time = (NOW + timedelta(seconds=60)).astimezone(timezone(timedelta(hours=8)))

    assert decay_intensity_bp(anchor, profile, china_time) == 5_000


def test_dst_transition_uses_elapsed_instant_time_not_wall_clock_time() -> None:
    new_york = ZoneInfo("America/New_York")
    start = datetime(2026, 3, 8, 1, 30, tzinfo=new_york)
    end = datetime(2026, 3, 8, 3, 30, tzinfo=new_york)
    anchor = DecayAnchor(intensity_bp=10_000, anchored_at=start)

    assert decay_intensity_bp(anchor, DecayProfile(half_life_seconds=3_600), end) == 5_000


def test_decay_rejects_invalid_parameters_and_time_travel() -> None:
    with pytest.raises(TypeError):
        DecayProfile(half_life_seconds=True)
    with pytest.raises(ValueError):
        DecayProfile(half_life_seconds=0)
    with pytest.raises(ValueError):
        DecayProfile(half_life_seconds=1, floor_bp=10_001)
    with pytest.raises(ValueError):
        DecayProfile(half_life_seconds=1, kind="linear")
    with pytest.raises(ValueError):
        DecayAnchor(intensity_bp=1, anchored_at=datetime(2026, 1, 1))

    anchor = DecayAnchor(intensity_bp=1_000, anchored_at=NOW, residue_bp=2_000)
    with pytest.raises(ValueError):
        decay_intensity_bp(anchor, DecayProfile(half_life_seconds=1), NOW)
    with pytest.raises(ValueError):
        decay_intensity_bp(
            DecayAnchor(intensity_bp=1_000, anchored_at=NOW),
            DecayProfile(half_life_seconds=1),
            NOW - timedelta(microseconds=1),
        )


def test_bounded_saturation_golden_and_edges() -> None:
    assert bounded_saturation_bp([]) == 0
    assert bounded_saturation_bp([2_345]) == 2_345
    assert bounded_saturation_bp([5_000, 5_000]) == 7_500
    assert bounded_saturation_bp([0, 0, 0]) == 0
    assert bounded_saturation_bp([1, 10_000, 500]) == 10_000


def test_bounded_saturation_is_order_independent() -> None:
    strengths = (1_000, 2_500, 7_000, 3_333)
    results = {bounded_saturation_bp(order) for order in permutations(strengths)}

    assert len(results) == 1


def test_relative_baseline_saturation_counts_baseline_once() -> None:
    assert relative_baseline_saturation_bp(1_000, []) == 1_000
    assert relative_baseline_saturation_bp(1_000, [5_000]) == 5_000
    assert relative_baseline_saturation_bp(1_000, [5_000, 5_000]) == 7_222
    assert relative_baseline_saturation_bp(1_000, [5_000, 5_000]) == (
        relative_baseline_saturation_bp(1_000, [5_000, 5_000][::-1])
    )


def test_bounded_saturation_rejects_non_basis_point_values() -> None:
    with pytest.raises(TypeError):
        bounded_saturation_bp([True])
    with pytest.raises(ValueError):
        bounded_saturation_bp([-1])
    with pytest.raises(ValueError):
        bounded_saturation_bp([10_001])


def test_runtime_implementation_contains_no_floating_point_path() -> None:
    source = inspect.getsource(affect_math)
    tree = ast.parse(source)

    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, float) for node in ast.walk(tree)
    )
    assert not any(isinstance(node, ast.Div) for node in ast.walk(tree))
    assert "total_seconds" not in source
    assert "Decimal" not in source


def test_frozen_factor_table_digest_is_versioned() -> None:
    assert FACTOR_TABLE_DIGEST == (
        "6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f"
    )
