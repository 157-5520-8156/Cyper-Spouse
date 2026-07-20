"""Change Phase: a pure, model-safe reading of how she moves around baseline.

CONTEXT.md defines a Change Phase as "a sourced, time-bound stage describing
how the companion is departing from or returning toward baseline".  This
module keeps the concept exactly where the glossary places it: a *projection
level* derivation over already-accepted Affect episodes, never a new event
pipeline.  Nothing here writes World truth; the reading enters deliberation
only through the ordinary Inner-Advisory envelope (like ``mood_view`` and
``aspiration_view``) and through explicitly versioned weight policies.

The phase vocabulary is deliberately small and derived from committed decay
mechanics rather than model prose:

* ``departing``   — a feeling was stimulated recently and still carries most
  of its anchor intensity ("刚陷入 / 正在升起");
* ``holding``     — the feeling has settled in: no fresh stimulus, but decay
  has not yet meaningfully eroded it;
* ``returning``   — decay is visibly underway: current intensity has fallen a
  meaningful fraction below its anchor while staying noticeable ("正在走出");
* ``recovering``  — the feeling has faded below the noticeable floor (or the
  episode resolved recently) but its trace is still fresh ("刚回到平静").

A dimension with no active accepted material is simply at baseline and is
omitted, mirroring ``mood_summary_prose``'s refusal to assert calmness.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from typing import Literal

from .mood_view import MOOD_LABELS
from .schema_core import FrozenModel


CHANGE_PHASE_VIEW_VERSION = "change-phase-view.1"

ChangePhase = Literal["departing", "holding", "returning", "recovering"]

# Same noticeable floor as mood_view: below this an accepted feeling is
# background noise, not a stage of departure or return.
_NOTICEABLE_BP = 2_000

# A stimulus within this window keeps the phase "departing": the feeling is
# still being actively fed rather than merely persisting.
_FRESH_STIMULUS = timedelta(hours=3)

# Decay progress below this share of the anchor marks visible return.
_RETURNING_SHARE_BP = 8_000

# A resolved episode stays visible as "recovering" for this long.
_RECOVERY_WINDOW = timedelta(hours=12)

_HEAVY_DIMENSIONS = frozenset(
    {"hurt", "anger", "sadness", "loneliness", "anxiety", "resentment"}
)

_PHASE_PROSE = {
    ("departing", True): "刚陷入{label}，情绪还在升起",
    ("holding", True): "{label}已经持续了一阵，还没有松动",
    ("returning", True): "正在慢慢走出{label}",
    ("recovering", True): "{label}刚刚平复下来",
    ("departing", False): "{label}正在升起",
    ("holding", False): "{label}还稳稳地在",
    ("returning", False): "{label}在慢慢回落",
    ("recovering", False): "{label}刚刚淡下去",
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ChangePhaseReading(FrozenModel):
    """One dimension's sourced stage relative to baseline.

    ``source_event_refs`` name the accepted episode events this reading is
    derived from, so an advisory built on it stays ledger-backed.
    """

    dimension: str
    phase: ChangePhase
    intensity_bp: int
    anchor_intensity_bp: int
    source_event_refs: tuple[str, ...]


def _component_phase(
    component, *, logical_time: datetime
) -> tuple[ChangePhase, int, int] | None:
    intensity = int(getattr(component, "intensity_bp", 0))
    anchor = int(getattr(component, "decay_anchor_intensity_bp", 0))
    last_stimulus = getattr(component, "last_stimulus_at", None)
    if not isinstance(last_stimulus, datetime) or anchor <= 0:
        return None
    if intensity < _NOTICEABLE_BP:
        # Faded but still-active material: only a recently noticeable anchor
        # counts as "just recovered" rather than long-quiet residue.
        if anchor >= _NOTICEABLE_BP and logical_time - last_stimulus <= _RECOVERY_WINDOW * 2:
            return ("recovering", intensity, anchor)
        return None
    if logical_time - last_stimulus <= _FRESH_STIMULUS:
        return ("departing", intensity, anchor)
    if intensity * 10_000 < anchor * _RETURNING_SHARE_BP:
        return ("returning", intensity, anchor)
    return ("holding", intensity, anchor)


_PHASE_PRIORITY = {"departing": 0, "returning": 1, "holding": 2, "recovering": 3}


def change_phase_readings(
    affect_episodes: tuple[object, ...], *, logical_time: datetime
) -> tuple[ChangePhaseReading, ...]:
    """Derive per-dimension phases from accepted, decay-versioned episodes.

    Pure over the pinned projection: replaying the same episodes at the same
    Logical Time always yields the same readings.
    """

    if logical_time.tzinfo is None or logical_time.utcoffset() is None:
        raise ValueError("change phase derivation requires timezone-aware logical time")
    per_dimension: dict[str, tuple[ChangePhase, int, int, list[str]]] = {}
    for episode in affect_episodes:
        status = getattr(episode, "status", None)
        origin = getattr(episode, "origin", None)
        source_ref = str(getattr(origin, "accepted_event_ref", "") or "")
        if not source_ref:
            continue
        if status == "resolved":
            closed_at = getattr(episode, "closed_at", None)
            if (
                isinstance(closed_at, datetime)
                and logical_time - closed_at <= _RECOVERY_WINDOW
            ):
                for component in getattr(episode, "components", ()):
                    dimension = str(getattr(component, "dimension", ""))
                    anchor = int(getattr(component, "decay_anchor_intensity_bp", 0))
                    if dimension not in MOOD_LABELS or anchor < _NOTICEABLE_BP:
                        continue
                    _merge(per_dimension, dimension, "recovering", 0, anchor, source_ref)
            continue
        if status != "active":
            continue
        for component in getattr(episode, "components", ()):
            dimension = str(getattr(component, "dimension", ""))
            if dimension not in MOOD_LABELS:
                continue
            derived = _component_phase(component, logical_time=logical_time)
            if derived is None:
                continue
            phase, intensity, anchor = derived
            _merge(per_dimension, dimension, phase, intensity, anchor, source_ref)
    readings = tuple(
        ChangePhaseReading(
            dimension=dimension,
            phase=phase,
            intensity_bp=intensity,
            anchor_intensity_bp=anchor,
            source_event_refs=tuple(dict.fromkeys(refs)),
        )
        for dimension, (phase, intensity, anchor, refs) in per_dimension.items()
    )
    return tuple(
        sorted(
            readings,
            key=lambda item: (_PHASE_PRIORITY[item.phase], -item.intensity_bp, item.dimension),
        )
    )


def _merge(
    per_dimension: dict[str, tuple[ChangePhase, int, int, list[str]]],
    dimension: str,
    phase: ChangePhase,
    intensity: int,
    anchor: int,
    source_ref: str,
) -> None:
    existing = per_dimension.get(dimension)
    if existing is None:
        per_dimension[dimension] = (phase, intensity, anchor, [source_ref])
        return
    current_phase, current_intensity, current_anchor, refs = existing
    if source_ref not in refs:
        refs.append(source_ref)
    # The livelier stage wins (departing > returning > holding > recovering),
    # then the stronger intensity: "刚陷入" must not be masked by an older,
    # already-fading component of the same feeling.
    if (_PHASE_PRIORITY[phase], -intensity) < (
        _PHASE_PRIORITY[current_phase], -current_intensity
    ):
        per_dimension[dimension] = (phase, intensity, anchor, refs)
    else:
        per_dimension[dimension] = (
            current_phase, current_intensity, current_anchor, refs,
        )


def change_phase_reading_prose(reading: ChangePhaseReading) -> str:
    """Render one dimension's phase as the same short Chinese fragment the
    advisory envelope uses, so viewer surfaces never invent new mood prose."""

    label = MOOD_LABELS.get(reading.dimension, reading.dimension)
    template = _PHASE_PROSE[(reading.phase, reading.dimension in _HEAVY_DIMENSIONS)]
    return template.format(label=label)


def change_phase_summary_prose(readings: tuple[ChangePhaseReading, ...]) -> str:
    """Render the most salient phases as one short advisory sentence.

    Empty when nothing is moving around baseline, so callers omit the line
    instead of asserting "she is fine".
    """

    if not readings:
        return ""
    fragments = []
    for reading in readings[:3]:
        label = MOOD_LABELS.get(reading.dimension, reading.dimension)
        template = _PHASE_PROSE[(reading.phase, reading.dimension in _HEAVY_DIMENSIONS)]
        fragments.append(template.format(label=label))
    return "她此刻的状态变化：" + "；".join(fragments) + "。"


def change_phase_advisories(projection) -> tuple:
    """Wrap the phase reading in the ordinary non-authoritative advisory envelope.

    The capsule import stays local: this module is also consumed by weight
    policies inside the reducer import graph, and ``context_capsule`` sits on
    the other side of the ledger module boundary.
    """

    from .context_capsule import InnerAdvisoryCandidate, InnerAdvisoryProjection

    logical_time = getattr(projection, "logical_time", None)
    if not isinstance(logical_time, datetime):
        return ()
    readings = change_phase_readings(
        tuple(getattr(projection, "affect_episodes", ())), logical_time=logical_time
    )
    if not readings:
        return ()
    visible = readings[:3]
    source_refs = tuple(
        dict.fromkeys(ref for reading in visible for ref in reading.source_event_refs)
    )
    if not source_refs:
        return ()
    candidates = tuple(
        InnerAdvisoryCandidate(
            candidate_ref="change-phase:" + _digest(
                {"dimension": reading.dimension, "phase": reading.phase}
            ),
            value=(
                _PHASE_PROSE[(reading.phase, reading.dimension in _HEAVY_DIMENSIONS)]
                .format(label=MOOD_LABELS.get(reading.dimension, reading.dimension))
            )[:256],
            weight_bp=10_000,
            confidence_bp=10_000,
        )
        for reading in visible
    )
    return (
        InnerAdvisoryProjection(
            advisory_id="advisory:change-phase:" + _digest(
                tuple((item.dimension, item.phase) for item in visible)
            ),
            kind="change_phase",
            source_refs=source_refs,
            candidate_refs=tuple(item.candidate_ref for item in candidates),
            candidates=candidates,
            # Deliberately below the continuity floors' rank: under extreme
            # capsule budget pressure this texture must be evicted before the
            # sole relationship / appraisal / affect head it derives from.
            confidence_bp=6_000,
            # A phase is a short-lived deliberation aid anchored to the pinned
            # durable head, not wall-clock process time.
            expiry=logical_time + timedelta(hours=6),
            producer_version=CHANGE_PHASE_VIEW_VERSION,
        ),
    )


def change_phase_by_dimension(
    readings: tuple[ChangePhaseReading, ...],
) -> dict[str, ChangePhase]:
    """Small lookup for weight policies: dimension -> current phase."""

    return {reading.dimension: reading.phase for reading in readings}


__all__ = [
    "CHANGE_PHASE_VIEW_VERSION",
    "ChangePhase",
    "ChangePhaseReading",
    "change_phase_advisories",
    "change_phase_by_dimension",
    "change_phase_reading_prose",
    "change_phase_readings",
    "change_phase_summary_prose",
]
