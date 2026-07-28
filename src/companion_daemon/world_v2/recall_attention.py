"""Bounded present-attention packets for automatic associative recall.

The packet separates exact inbound wording from a small semantic description
of the companion's already-accepted current state.  It is accessibility input,
not a motive, mood verdict, or response instruction.  Opaque source identities
stay in structured link selectors instead of consuming embedding text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from .recall_audit import CharacterRecallRequest
from .recall_index import MAX_RECALL_QUERY_CHARACTERS


_MAX_LEXICAL_CHARACTERS = 768
_MAX_DENSE_OBSERVATION_CHARACTERS = 384
_MAX_AFFECT_COMPONENTS = 6
_MAX_APPRAISALS = 3
_MAX_RELATIONSHIPS = 2
_MAX_ACTIVITIES = 3
_MAX_THREADS = 3


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _append_bounded(parts: list[str], material: str) -> None:
    material = material.strip()
    if not material:
        return
    # Joining a new part also adds one separator for every existing part.
    used = sum(len(item) for item in parts) + len(parts)
    remaining = MAX_RECALL_QUERY_CHARACTERS - used
    if remaining <= 0:
        return
    if len(material) <= remaining:
        parts.append(material)
        return
    if remaining >= 8:
        parts.append(material[: remaining - 1].rstrip() + "…")


def _affect_summary(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for episode in values:
        for raw_component in _sequence(episode.get("components")):
            component = _mapping(raw_component)
            dimension = component.get("dimension")
            intensity = component.get("intensity_bp")
            if not isinstance(dimension, str):
                continue
            item: dict[str, object] = {"dimension": dimension}
            if isinstance(intensity, int):
                item["intensity_bp"] = intensity
            residue = component.get("residue_bp")
            if isinstance(residue, int):
                item["residue_bp"] = residue
            summaries.append(item)
            if len(summaries) >= _MAX_AFFECT_COMPONENTS:
                return summaries
    return summaries


def _appraisal_summary(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for appraisal in values[:_MAX_APPRAISALS]:
        hypotheses: list[dict[str, object]] = []
        for raw_hypothesis in _sequence(appraisal.get("hypotheses"))[:3]:
            hypothesis = _mapping(raw_hypothesis)
            item = {
                key: hypothesis[key]
                for key in ("meaning", "attribution", "severity", "weight_bp")
                if key in hypothesis
            }
            if item:
                hypotheses.append(item)
        summary = {
            key: appraisal[key]
            for key in ("confidence_bp", "expires_at")
            if key in appraisal
        }
        if hypotheses:
            summary["hypotheses"] = hypotheses
        if summary:
            summaries.append(summary)
    return summaries


def _relationship_summary(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: value[key]
            for key in ("stage", "temperature", "variables", "last_adjusted_at")
            if key in value
        }
        for value in values[:_MAX_RELATIONSHIPS]
    ]


def _situation_summary(value: Mapping[str, object]) -> dict[str, object]:
    summary = {
        key: value[key]
        for key in ("time_segment", "attention_slice", "social_environment", "plan_relation")
        if key in value
    }
    activities = [
        _mapping(item)
        for item in _sequence(value.get("activity_slices"))[:_MAX_ACTIVITIES]
    ]
    if activities:
        summary["activity_slices"] = activities
    return summary


def _thread_summary(values: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    return [
        {
            key: value[key]
            for key in ("kind", "importance_bp", "due_window", "status")
            if key in value
        }
        for value in values[:_MAX_THREADS]
    ]


def build_automatic_recall_request(
    *,
    observation_text: str,
    affect_values: Sequence[Mapping[str, object]] = (),
    appraisal_values: Sequence[Mapping[str, object]] = (),
    relationship_values: Sequence[Mapping[str, object]] = (),
    situation_value: Mapping[str, object] | None = None,
    open_thread_values: Sequence[Mapping[str, object]] = (),
    link_refs: Sequence[str] = (),
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    memory_kinds: Sequence[Literal["episodic", "semantic", "reflective"]] = (),
    limit: int = 4,
) -> CharacterRecallRequest:
    """Build the canonical bounded request used by automatic prefetch.

    Values must already come from the pinned projection.  This function only
    selects human-readable semantic fields and enforces the downstream schema
    budget before Pydantic construction.
    """

    lexical = observation_text.strip()
    if not lexical:
        raise ValueError("automatic recall requires inbound observation text")
    lexical = lexical[:_MAX_LEXICAL_CHARACTERS]
    parts: list[str] = []
    _append_bounded(parts, f"用户刚说：{lexical[:_MAX_DENSE_OBSERVATION_CHARACTERS]}")
    affect = _affect_summary(affect_values)
    if affect:
        _append_bounded(parts, "当前感受：" + _compact_json(affect))
    appraisals = _appraisal_summary(appraisal_values)
    if appraisals:
        _append_bounded(parts, "当前解读：" + _compact_json(appraisals))
    relationships = _relationship_summary(relationship_values)
    if relationships:
        _append_bounded(parts, "当前关系：" + _compact_json(relationships))
    situation = _situation_summary(situation_value or {})
    if situation:
        _append_bounded(parts, "当前处境：" + _compact_json(situation))
    threads = _thread_summary(open_thread_values)
    if threads:
        _append_bounded(parts, "未完话题：" + _compact_json(threads))
    query_text = "\n".join(parts)
    canonical_links = tuple(sorted({item for item in link_refs if item}))[:16]
    canonical_kinds = tuple(sorted(set(memory_kinds)))
    return CharacterRecallRequest(
        query_text=query_text,
        lexical_text=lexical,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        link_refs=canonical_links,
        memory_kinds=canonical_kinds,
        limit=min(max(limit, 1), 6),
    )


__all__ = [
    "MAX_RECALL_QUERY_CHARACTERS",
    "build_automatic_recall_request",
]
