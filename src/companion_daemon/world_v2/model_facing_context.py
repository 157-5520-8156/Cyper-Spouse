"""Compact, non-authoritative model view of a verified Context Capsule.

The complete Capsule remains the only acceptance/audit authority.  Provider
models need its typed values and copyable semantic source refs, but not the
cryptographic transport envelopes that the host has already verified.
"""

from __future__ import annotations

import json


_CHAT_OMITTED_SLICES = frozenset({"action_budget", "available_capabilities"})
_CHAT_ITEM_LIMITS = {
    "recent_dialogue": 6,
    "current_situation": 1,
    "relationship_slice": 2,
    "character_core": 2,
    "affect_episodes": 4,
    "appraisals": 3,
    "relevant_facts": 6,
    "world_life": 3,
    "recent_experiences": 3,
    "open_threads": 4,
    "private_impressions": 2,
    "active_memory_candidates": 2,
    # A same-turn semantic pass can legitimately return several orthogonal
    # coordinates (affect, thread, boundary, interruption).  Treating the
    # slice as one item here silently discarded all but the lexicographic tail
    # after the trusted Capsule had already preserved the full matrix.
    "advisories": 12,
}
_AUTHORITY_VALUE_KEYS = frozenset(
    {
        "origin",
        "source_bindings",
        "source_evidence_refs",
        "anchor_evidence_refs",
        "source_revisions",
        "policy_versions",
        "policy_refs",
        "resolver_proof",
        "accepted_event_ref",
        "entity_revision",
        "authority_contract_version",
        "semantic_fingerprint",
    }
)
_RECOVERY_SLICE_LIMITS = {
    "recent_dialogue": 8,
    "relevant_facts": 12,
    "active_memory_candidates": 8,
    "recent_experiences": 6,
    "current_situation": 1,
    "world_life": 6,
    "character_core": 2,
    "relationship_slice": 2,
    "affect_episodes": 4,
    "appraisals": 4,
    "open_threads": 4,
}


def _context_items_for_chat(name: str, items: list[object], limit: int) -> list[object]:
    """Keep the newest dialogue and highest-ranked items without guessing values."""

    if name != "recent_dialogue":
        return items[:limit]
    keyed: list[tuple[tuple[str, int, str], object]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return items[-limit:]
        value = item.get("value")
        if not isinstance(value, dict):
            return items[-limit:]
        occurred_at = value.get("occurred_at")
        sequence = value.get("sequence")
        if not isinstance(occurred_at, str) and not isinstance(sequence, int):
            # Small synthetic/legacy packets may not expose chronology; keep
            # their established tail behavior rather than inventing an order.
            return items[-limit:]
        keyed.append(
            (
                (
                    occurred_at if isinstance(occurred_at, str) else "",
                    sequence if isinstance(sequence, int) else -1,
                    str(item.get("item_ref") or index),
                ),
                item,
            )
        )
    return [item for _, item in sorted(keyed, key=lambda pair: pair[0])[-limit:]]


def compact_model_facing_context(raw: str) -> str:
    """Remove proof noise while preserving typed values and source tokens."""

    try:
        context = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return raw
    if not isinstance(context, dict) or not isinstance(context.get("slices"), dict):
        return raw
    compact: dict[str, object] = {
        key: context[key]
        for key in (
            "world_id",
            "actor_ref",
            "trigger_ref",
            "world_revision",
            "logical_time",
        )
        if key in context
    }
    compact_slices: dict[str, object] = {}
    for name, slice_value in context["slices"].items():
        if not isinstance(name, str) or not isinstance(slice_value, dict):
            continue
        if slice_value.get("availability") != "available":
            compact_slices[name] = {"availability": "unavailable"}
            continue
        compact_items: list[dict[str, object]] = []
        items = slice_value.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Production capsules use ``item_ref``.  A few trusted
                # compatibility packets already expose the normalized name
                # ``source_ref``; retain either spelling so a recovery model
                # does not lose the semantic source token while we compact
                # proof metadata.
                item_ref = item.get("item_ref")
                if not isinstance(item_ref, str):
                    item_ref = item.get("source_ref")
                material: dict[str, object] = {"value": item.get("value")}
                if isinstance(item_ref, str):
                    # item_ref is an accepted semantic source token in the
                    # complete Capsule's claim validator.
                    material["source_ref"] = item_ref
                privacy = item.get("privacy_class")
                if isinstance(privacy, str):
                    material["privacy_class"] = privacy
                compact_items.append(material)
        compact_slices[name] = {
            "availability": "available",
            "items": compact_items,
        }
    compact["slices"] = compact_slices
    relationship = context.get("relationship_evaluation")
    if isinstance(relationship, dict):
        compact["relationship_evaluation"] = {
            key: relationship[key]
            for key in (
                "subject_ref",
                "trigger_appraisal_id",
                "appraisal_summary_json",
                "relationship_summary_json",
            )
            if key in relationship
        }
    return json.dumps(
        compact,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _semantic_value(value: object) -> object:
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if (
            key in _AUTHORITY_VALUE_KEYS
            or key.endswith("_hash")
            or key.endswith("_digest")
            or key.endswith("_version")
        ):
            continue
        result[key] = _semantic_value(item)
    return result


def compact_chat_model_facing_context(raw: str) -> str:
    """Produce the bounded semantic view used only by interactive cognition.

    The complete Context Capsule remains unchanged and is still the acceptance
    authority.  This derivative removes nested proof/accounting material,
    unavailable placeholders and old dialogue beyond the conversational
    working set.  Every retained item keeps its accepted semantic ``source_ref``.
    """

    compacted = compact_model_facing_context(raw)
    try:
        context = json.loads(compacted)
    except (TypeError, json.JSONDecodeError):
        return compacted
    if not isinstance(context, dict) or not isinstance(context.get("slices"), dict):
        return compacted
    slices: dict[str, object] = {}
    for name, raw_slice in context["slices"].items():
        if (
            not isinstance(name, str)
            or name in _CHAT_OMITTED_SLICES
            or not isinstance(raw_slice, dict)
            or raw_slice.get("availability") != "available"
        ):
            continue
        items = raw_slice.get("items")
        if not isinstance(items, list) or not items:
            continue
        limit = _CHAT_ITEM_LIMITS.get(name, 8)
        # Capsule items are emitted in rank order (highest first). Keeping
        # the tail silently preferred stale dialogue/facts when the resolver
        # returned more than the chat budget.
        selected = _context_items_for_chat(name, items, limit)
        semantic_items: list[dict[str, object]] = []
        for item in selected:
            if not isinstance(item, dict):
                continue
            material: dict[str, object] = {"value": _semantic_value(item.get("value"))}
            for key in ("source_ref", "privacy_class"):
                if isinstance(item.get(key), str):
                    material[key] = item[key]
            semantic_items.append(material)
        if semantic_items:
            slices[name] = {"availability": "available", "items": semantic_items}
    context["slices"] = slices
    return json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compact_recovery_model_facing_context(raw: str) -> str:
    """Small claim-capable view for the latency-bounded recovery call.

    Recovery does not need capabilities, budgets, advisories, private media,
    or every social mechanism.  It does need the recent conversational thread
    and every bounded fact/memory source token that could authorize an answer.
    This is a provider view only; the full Context remains the acceptance
    authority when the returned claim refs are materialized.
    """

    compacted = compact_chat_model_facing_context(raw)
    try:
        context = json.loads(compacted)
    except (TypeError, json.JSONDecodeError):
        return compacted
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        return compacted
    retained: dict[str, object] = {}
    for name, limit in _RECOVERY_SLICE_LIMITS.items():
        lane = slices.get(name)
        if not isinstance(lane, dict) or lane.get("availability") != "available":
            continue
        items = lane.get("items")
        if isinstance(items, list) and items:
            retained[name] = {
                "availability": "available",
                "items": _context_items_for_chat(name, items, limit),
            }
    context["slices"] = retained
    return json.dumps(
        context,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def mechanism_consumption_summary(raw: str) -> dict[str, object]:
    """Summarize which verified context mechanisms reached a model turn.

    This is operator evidence, not a second authority.  It intentionally
    reports counts and bounded status labels rather than prose, memory text,
    or private source values.  Keeping this summary beside the provider view
    makes a missing mechanism distinguishable from a model choosing not to
    mention a mechanism that was actually supplied.
    """

    try:
        context = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"status": "invalid_context", "slices": {}}
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        return {"status": "missing_slices", "slices": {}}

    result: dict[str, object] = {
        "status": "ok",
        "world_revision": context.get("world_revision"),
        "logical_time": context.get("logical_time"),
        "slices": {},
    }
    summary_slices: dict[str, object] = {}
    for name in (
        "character_core",
        "current_situation",
        "recent_dialogue",
        "relationship_slice",
        "appraisals",
        "affect_episodes",
        "open_threads",
        "relevant_facts",
        "recent_experiences",
        "world_life",
        "active_memory_candidates",
        "private_impressions",
        "advisories",
    ):
        lane = slices.get(name)
        if not isinstance(lane, dict) or lane.get("availability") != "available":
            summary_slices[name] = {
                "availability": (
                    lane.get("availability")
                    if isinstance(lane, dict)
                    else "unavailable"
                ),
                "item_count": 0,
                "source_ref_count": 0,
            }
            continue
        items = lane.get("items")
        if not isinstance(items, list):
            items = []
        refs: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("item_ref", "source_ref"):
                value = item.get(key)
                if isinstance(value, str):
                    refs.add(value)
            bindings = item.get("source_bindings")
            if isinstance(bindings, list):
                refs.update(
                    binding["ref"]
                    for binding in bindings
                    if isinstance(binding, dict) and isinstance(binding.get("ref"), str)
                )
        value: dict[str, object] = {
            "availability": "available",
            "item_count": len(items),
            "source_ref_count": len(refs),
        }
        if name == "current_situation":
            activity_count = 0
            for item in items:
                item_value = item.get("value") if isinstance(item, dict) else None
                if isinstance(item_value, dict):
                    activities = item_value.get("activity_slices")
                    if isinstance(activities, list):
                        activity_count += len(activities)
            value["activity_count"] = activity_count
        summary_slices[name] = value
    result["slices"] = summary_slices
    return result


__all__ = [
    "compact_chat_model_facing_context",
    "compact_model_facing_context",
    "compact_recovery_model_facing_context",
    "mechanism_consumption_summary",
]
