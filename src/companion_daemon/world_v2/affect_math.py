"""Deterministic fixed-point affect decay and bounded aggregation.

The decay calculation is deliberately anchored to the last affect-changing event.  Reading
the value never moves that anchor, so one direct materialization and many intermediate reads
produce the same result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Iterable

BP_SCALE = 10_000
Q48_ONE = 1 << 48
_FRACTION_BITS = 32
_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_DAY = 86_400 * _MICROSECONDS_PER_SECOND
ALGORITHM_VERSION = "affect-decay-exp2-q48-binary-rhe-v1"
FACTOR_TABLE_DIGEST = "6a3abe39937394f738dcd1563189086020127b7e1e7189868c84ae3f890ee49f"

# Q48, round-half-even encodings of 2**(-2**-n), n=1..32.  These constants make
# the runtime algorithm independent of platform floating-point implementations.
_EXP2_NEG_BINARY_FACTORS_Q48 = (
    199_032_864_766_430,
    236_691_298_899_613,
    258_113_691_704_612,
    269_541_361_132_679,
    275_443_548_385_834,
    278_442_931_975_303,
    279_954_849_561_488,
    280_713_884_160_287,
    281_094_172_843_150,
    281_284_510_335_224,
    281_379_727_407_067,
    281_427_348_029_212,
    281_451_161_362_436,
    281_463_068_784_661,
    281_469_022_684_686,
    281_471_999_681_928,
    281_473_488_192_356,
    281_474_232_450_522,
    281_474_604_580_343,
    281_474_790_645_438,
    281_474_883_678_032,
    281_474_930_194_340,
    281_474_953_452_497,
    281_474_965_081_576,
    281_474_970_896_116,
    281_474_973_803_386,
    281_474_975_257_021,
    281_474_975_983_839,
    281_474_976_347_247,
    281_474_976_528_952,
    281_474_976_619_804,
    281_474_976_665_230,
)

if (
    hashlib.sha256(
        json.dumps(_EXP2_NEG_BINARY_FACTORS_Q48, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    != FACTOR_TABLE_DIGEST
):
    raise RuntimeError("affect decay factor table digest mismatch")


def _require_plain_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    return value


def _require_bp(name: str, value: object) -> int:
    integer = _require_plain_int(name, value)
    if not 0 <= integer <= BP_SCALE:
        raise ValueError(f"{name} must be between 0 and {BP_SCALE}")
    return integer


def _require_aware(name: str, value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class DecayProfile:
    """Versioned parameters controlling exponential half-life decay."""

    half_life_seconds: int
    floor_bp: int = 0
    delay_seconds: int = 0
    config_version: str = ALGORITHM_VERSION
    kind: str = "exponential_half_life"

    def __post_init__(self) -> None:
        half_life = _require_plain_int("half_life_seconds", self.half_life_seconds)
        if half_life <= 0:
            raise ValueError("half_life_seconds must be positive")
        _require_bp("floor_bp", self.floor_bp)
        delay = _require_plain_int("delay_seconds", self.delay_seconds)
        if delay < 0:
            raise ValueError("delay_seconds must be non-negative")
        if not isinstance(self.config_version, str) or not self.config_version.strip():
            raise ValueError("config_version must be a non-empty string")
        if self.kind != "exponential_half_life":
            raise ValueError("unsupported decay kind")


@dataclass(frozen=True, slots=True)
class DecayAnchor:
    """Stable source state written only when an affect-changing event occurs."""

    intensity_bp: int
    anchored_at: datetime
    baseline_bp: int = 0
    residue_bp: int = 0
    decay_not_before: datetime | None = None

    def __post_init__(self) -> None:
        _require_bp("intensity_bp", self.intensity_bp)
        _require_bp("baseline_bp", self.baseline_bp)
        _require_bp("residue_bp", self.residue_bp)
        _require_aware("anchored_at", self.anchored_at)
        if self.decay_not_before is not None:
            _require_aware("decay_not_before", self.decay_not_before)


@dataclass(frozen=True, slots=True)
class DecayMaterialization:
    """A read-only view of an anchor at one logical time."""

    anchor: DecayAnchor
    profile: DecayProfile
    intensity_bp: int
    materialized_at: datetime


def _round_half_even(numerator: int, denominator: int) -> int:
    """Round a non-negative rational to the nearest integer, ties to even."""

    if numerator < 0 or denominator <= 0:
        raise ValueError("rounding operands must be non-negative with a positive denominator")
    quotient, remainder = divmod(numerator, denominator)
    doubled_remainder = remainder << 1
    if doubled_remainder > denominator or (doubled_remainder == denominator and quotient & 1):
        return quotient + 1
    return quotient


def _elapsed_microseconds(start: datetime, end: datetime) -> int:
    start = _require_aware("start", start)
    end = _require_aware("end", end)
    delta = end.astimezone(UTC) - start.astimezone(UTC)
    elapsed = (
        delta.days * _MICROSECONDS_PER_DAY
        + delta.seconds * _MICROSECONDS_PER_SECOND
        + delta.microseconds
    )
    if elapsed < 0:
        raise ValueError("materialization time cannot precede the anchor")
    return elapsed


def _fractional_half_life_factor_q48(fraction_q32: int) -> int:
    factor = Q48_ONE
    mask = 1 << (_FRACTION_BITS - 1)
    for binary_factor in _EXP2_NEG_BINARY_FACTORS_Q48:
        if fraction_q32 & mask:
            factor = _round_half_even(factor * binary_factor, Q48_ONE)
        mask >>= 1
    return factor


def decay_intensity_bp(anchor: DecayAnchor, profile: DecayProfile, at: datetime) -> int:
    """Materialize an affect intensity from its stable anchor using integer arithmetic."""

    _elapsed_microseconds(anchor.anchored_at, at)
    decay_start = anchor.decay_not_before or (
        anchor.anchored_at + timedelta(seconds=profile.delay_seconds)
    )
    decay_start = max(anchor.anchored_at, decay_start)
    elapsed_us = _elapsed_microseconds(decay_start, at) if at >= decay_start else 0
    target_bp = max(anchor.baseline_bp, anchor.residue_bp, profile.floor_bp)
    if anchor.intensity_bp < target_bp:
        raise ValueError("anchor intensity cannot be below its effective decay target")

    if elapsed_us <= 0 or anchor.intensity_bp == target_bp:
        return anchor.intensity_bp

    half_life_us = profile.half_life_seconds * _MICROSECONDS_PER_SECOND
    whole_half_lives, remainder_us = divmod(elapsed_us, half_life_us)
    gap_bp = anchor.intensity_bp - target_bp
    # Even the largest basis-point gap rounds to zero after fifteen half-lives.
    if whole_half_lives >= 15:
        return target_bp

    fraction_q32 = (remainder_us << _FRACTION_BITS) // half_life_us
    fractional_factor_q48 = _fractional_half_life_factor_q48(fraction_q32)
    denominator = Q48_ONE << whole_half_lives
    decayed_gap_bp = _round_half_even(gap_bp * fractional_factor_q48, denominator)
    return target_bp + decayed_gap_bp


def materialize_decay(
    anchor: DecayAnchor, profile: DecayProfile, at: datetime
) -> DecayMaterialization:
    """Create a view without mutating or replacing the stable anchor."""

    at = _require_aware("at", at)
    return DecayMaterialization(
        anchor=anchor,
        profile=profile,
        intensity_bp=decay_intensity_bp(anchor, profile, at),
        materialized_at=at,
    )


def advance_decay(view: DecayMaterialization, at: datetime) -> DecayMaterialization:
    """Read a later value while continuing to calculate from the original anchor."""

    at = _require_aware("at", at)
    if _elapsed_microseconds(view.materialized_at, at) < 0:
        raise ValueError("advanced materialization cannot move backwards")
    return materialize_decay(view.anchor, view.profile, at)


def bounded_saturation_bp(values: Iterable[int]) -> int:
    """Combine basis-point strengths with one order-independent final rounding.

    The exact unrounded operation is ``1 - product(1 - value)``.  Accumulating the
    complete integer product before rounding avoids the order dependence of pairwise
    saturation.
    """

    complement_product = 1
    count = 0
    for value in values:
        strength_bp = _require_bp("saturation value", value)
        complement_product *= BP_SCALE - strength_bp
        count += 1
    if count == 0:
        return 0
    complement_bp = _round_half_even(complement_product, BP_SCALE ** (count - 1))
    return BP_SCALE - complement_bp


def relative_baseline_saturation_bp(baseline_bp: int, component_values: Iterable[int]) -> int:
    """Aggregate absolute component values without counting their baseline twice."""

    baseline = _require_bp("baseline_bp", baseline_bp)
    values = tuple(_require_bp("component value", item) for item in component_values)
    if not values:
        return baseline
    if any(item < baseline for item in values):
        raise ValueError("component value cannot be below its baseline")
    if baseline == BP_SCALE:
        return BP_SCALE
    complement_product = 1
    for value in values:
        complement_product *= BP_SCALE - value
    complement = _round_half_even(
        complement_product,
        (BP_SCALE - baseline) ** (len(values) - 1),
    )
    return BP_SCALE - complement


__all__ = [
    "ALGORITHM_VERSION",
    "BP_SCALE",
    "Q48_ONE",
    "DecayAnchor",
    "DecayMaterialization",
    "DecayProfile",
    "FACTOR_TABLE_DIGEST",
    "advance_decay",
    "bounded_saturation_bp",
    "decay_intensity_bp",
    "materialize_decay",
    "relative_baseline_saturation_bp",
]
