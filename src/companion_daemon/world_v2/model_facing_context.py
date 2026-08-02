"""Compact, non-authoritative model view of a verified Context Capsule.

The complete Capsule remains the only acceptance/audit authority.  Provider
models need its typed values and copyable semantic source refs, but not the
cryptographic transport envelopes that the host has already verified.
"""

from __future__ import annotations

import hashlib
import json


_CHAT_OMITTED_SLICES = frozenset({"action_budget", "available_capabilities"})
CHAT_RECENT_DIALOGUE_ITEM_LIMIT = 6
_PINNED_TIME_CONTRACT = "pinned-time-context.1"
_PINNED_TIME_SLICE = "pinned_time"
_CHAT_ITEM_LIMITS = {
    "recent_dialogue": CHAT_RECENT_DIALOGUE_ITEM_LIMIT,
    _PINNED_TIME_SLICE: 1,
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
        "proposal_source",
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
    _PINNED_TIME_SLICE: 1,
    "relevant_facts": 12,
    "active_memory_candidates": 8,
    # A selected private impression may be the exact memory the primary role
    # chose before a later truth-review failure. Technical recovery receives
    # that same pinned attention result instead of reconstructing a new motive
    # or answering from a thinner Context.
    "private_impressions": 4,
    "recent_experiences": 6,
    "current_situation": 1,
    "world_life": 6,
    "character_core": 2,
    "relationship_slice": 2,
    "affect_episodes": 4,
    "appraisals": 4,
    "open_threads": 4,
}


def _pinned_time_view(
    *,
    context: dict[str, object],
    slices: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Derive one replay-stable attention source from verified clock coordinates.

    ``logical_time`` is a top-level Capsule coordinate rather than a slice item,
    so it previously had no copyable source token.  The derived token is
    intentionally rebuilt here instead of trusting any caller-supplied
    ``pinned_time`` field or slice.  Production reaches this view only through a
    verified Context Capsule; the digest binds the exact turn coordinates and
    the verified current-situation segment without granting World mutation
    authority.
    """

    logical_time = context.get("logical_time")
    if not isinstance(logical_time, str) or not logical_time:
        return None

    time_segment: str | None = None
    local_logical_time: str | None = None
    situation_source_ref: str | None = None
    situation_slice = slices.get("current_situation")
    if (
        isinstance(situation_slice, dict)
        and situation_slice.get("availability") == "available"
        and isinstance(situation_slice.get("items"), list)
    ):
        for item in situation_slice["items"]:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, dict):
                continue
            candidate = value.get("time_segment")
            if isinstance(candidate, str) and candidate:
                time_segment = candidate
                local_candidate = value.get("logical_time")
                if isinstance(local_candidate, str) and local_candidate:
                    local_logical_time = local_candidate
                raw_source_ref = item.get("source_ref")
                if isinstance(raw_source_ref, str) and raw_source_ref:
                    situation_source_ref = raw_source_ref
                break

    source_material: dict[str, object] = {
        "contract": _PINNED_TIME_CONTRACT,
        "logical_time": logical_time,
    }
    for key in ("world_id", "actor_ref", "trigger_ref", "world_revision"):
        value = context.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            source_material[key] = value
    if time_segment is not None:
        source_material["time_segment"] = time_segment
    if local_logical_time is not None:
        source_material["local_logical_time"] = local_logical_time
    if situation_source_ref is not None:
        source_material["current_situation_source_ref"] = situation_source_ref
    source_ref = "pinned-time:sha256:" + hashlib.sha256(
        json.dumps(
            source_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    value: dict[str, object] = {
        "contract": _PINNED_TIME_CONTRACT,
        "authority": "derived_from_verified_context",
        "logical_time": logical_time,
    }
    if local_logical_time is not None:
        value["local_logical_time"] = local_logical_time
    if time_segment is not None:
        value["time_segment"] = time_segment
    return (
        {**value, "source_ref": source_ref},
        {
            "availability": "available",
            "items": [
                {
                    "value": value,
                    "source_ref": source_ref,
                    "attention_source_refs": [source_ref],
                }
            ],
        },
    )


def _attention_source_refs(
    *,
    slice_name: str,
    item_ref: str | None,
    value: object,
) -> list[str]:
    """Name the exact non-authorizing refs a model may cite as attended.

    Inbound dialogue uses a semantic item ref wrapped around the durable
    Observation ref. Models can see both inside the verified dialogue value,
    so the compact view names both aliases explicitly instead of leaving one
    visible-but-rejected. These refs authorize no factual claim or World
    mutation; claim lanes continue to use their separate source closure.
    """

    refs = [item_ref] if isinstance(item_ref, str) else []
    if slice_name != "recent_dialogue" or not isinstance(value, dict):
        return refs
    dialogue_id = value.get("dialogue_id")
    prefix = "dialogue:observation:"
    if dialogue_id == item_ref and isinstance(dialogue_id, str) and dialogue_id.startswith(prefix):
        observation_ref = dialogue_id.removeprefix(prefix)
        if observation_ref.startswith("observation:"):
            refs.append(observation_ref)
    return list(dict.fromkeys(refs))


def _context_items_for_chat(name: str, items: list[object], limit: int) -> list[object]:
    """Keep the newest dialogue and highest-ranked items without guessing values."""

    if name != "recent_dialogue":
        injected = [
            item for item in items if isinstance(item, dict) and item.get("recall_injected") is True
        ]
        if not injected:
            return items[:limit]
        # Audited recall hits supplement the slice instead of competing with
        # it for the ordinary budget.  The previous shared cap meant a
        # successful retrieval evicted the capsule's own ranked items, so net
        # remembered material stayed flat even when the recall channel worked.
        # Injection is already bounded upstream (prefetch/pull limit <= 6), so
        # the provider view grows by at most that many verified items.
        injected_ids = {id(item) for item in injected}
        remainder = [item for item in items if id(item) not in injected_ids]
        return injected + remainder[:limit]
    injected = [
        item for item in items if isinstance(item, dict) and item.get("recall_injected") is True
    ]
    injected_ids = {id(item) for item in injected}
    ordinary = [item for item in items if id(item) not in injected_ids]
    keyed: list[tuple[tuple[int, str, str], object]] = []
    for index, item in enumerate(ordinary):
        if not isinstance(item, dict):
            return ordinary[-limit:] + injected
        value = item.get("value")
        if not isinstance(value, dict):
            return ordinary[-limit:] + injected
        occurred_at = value.get("occurred_at")
        sequence = value.get("sequence")
        if not isinstance(occurred_at, str) and not isinstance(sequence, int):
            # Small synthetic/legacy packets may not expose chronology; keep
            # their established tail behavior rather than inventing an order.
            return ordinary[-limit:] + injected
        keyed.append(
            (
                (
                    sequence if isinstance(sequence, int) else -1,
                    occurred_at if isinstance(occurred_at, str) else "",
                    str(item.get("item_ref") or index),
                ),
                item,
            )
        )
    return [item for _, item in sorted(keyed, key=lambda pair: pair[0])[-limit:]] + injected


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
                attention_refs = _attention_source_refs(
                    slice_name=name,
                    item_ref=item_ref if isinstance(item_ref, str) else None,
                    value=item.get("value"),
                )
                if attention_refs:
                    material["attention_source_refs"] = attention_refs
                privacy = item.get("privacy_class")
                if isinstance(privacy, str):
                    material["privacy_class"] = privacy
                if item.get("recall_injected") is True:
                    material["recall_injected"] = True
                compact_items.append(material)
        compact_slices[name] = {
            "availability": "available",
            "items": compact_items,
        }
    pinned_time = _pinned_time_view(context=compact, slices=compact_slices)
    if pinned_time is not None:
        compact["pinned_time"], compact_slices[_PINNED_TIME_SLICE] = pinned_time
    compact["slices"] = compact_slices
    compact["current_self_state"] = _build_current_self_state(
        slices=compact_slices,
        logical_time=compact.get("logical_time"),
    )
    relationship = context.get("relationship_evaluation")
    if isinstance(relationship, dict):
        compact["relationship_evaluation"] = {
            key: relationship[key]
            for key in (
                "subject_ref",
                "trigger_appraisal_id",
                "appraisal_summary_json",
                "interaction_source_summary_json",
                "relationship_summary_json",
            )
            if key in relationship and relationship[key] is not None
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


def _slice_items(slices: dict[str, object], name: str) -> list[dict[str, object]]:
    lane = slices.get(name)
    if not isinstance(lane, dict) or lane.get("availability") != "available":
        return []
    items = lane.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _state_entry(
    item: dict[str, object],
    *,
    fields: tuple[str, ...] | None = None,
) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return None
    value = item.get("value")
    if not isinstance(value, dict):
        return None
    semantic = {key: value[key] for key in fields if key in value} if fields is not None else value
    if not semantic:
        return None
    result: dict[str, object] = dict(semantic)
    result["source_ref"] = source_ref
    return result


def _affect_state_entry(item: dict[str, object]) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return None
    value = item.get("value")
    if not isinstance(value, dict):
        return None
    components: list[dict[str, object]] = []
    raw_components = value.get("components")
    if isinstance(raw_components, list):
        for component in raw_components:
            if not isinstance(component, dict) or not isinstance(component.get("dimension"), str):
                continue
            material = {
                key: component[key]
                for key in (
                    "dimension",
                    "source_cluster_ref",
                    "appraisal_refs",
                    "intensity_bp",
                    "residue_bp",
                    "opened_at",
                    "last_stimulus_at",
                    "last_updated_at",
                    "decay_not_before",
                )
                if key in component
            }
            components.append(material)
    if not components:
        return None
    result: dict[str, object] = {
        "components": components,
        **{key: value[key] for key in ("opened_at", "updated_at") if key in value},
    }
    result["source_ref"] = source_ref
    return result


def _core_state_entry(item: dict[str, object]) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return None
    value = item.get("value")
    values = value.get("values") if isinstance(value, dict) else None
    if not isinstance(values, dict):
        return None
    slow = values.get("slow_evolving")
    if not isinstance(slow, dict):
        return None
    return {"slow_evolving": slow, "source_ref": source_ref}


def _recalled_document_state_entry(
    item: dict[str, object],
    *,
    memory_kinds: frozenset[str],
) -> dict[str, object] | None:
    """Project one trusted, already-selected RecallDocument into Current Self."""

    source_ref = item.get("source_ref")
    value = item.get("value")
    if (
        not isinstance(source_ref, str)
        or not source_ref
        or not isinstance(value, dict)
        or value.get("memory_kind") not in memory_kinds
        or not isinstance(value.get("actor_ref"), str)
        or not isinstance(value.get("text"), str)
        or not value["text"].strip()
    ):
        return None
    fields = (
        "memory_kind",
        "authority",
        "epistemic_scope",
        "actor_ref",
        "speaker_ref",
        "subject_refs",
        "text",
        "occurred_from",
        "occurred_to",
        "valid_from",
        "valid_to",
        "status",
    )
    return {
        **{
            key: value[key]
            for key in fields
            if key in value and value[key] is not None
        },
        "source_ref": source_ref,
    }


def _recent_experience_state_entry(
    item: dict[str, object],
    *,
    lane: str,
) -> dict[str, object] | None:
    """Keep a small semantic, source-bound view of the companion's own past.

    ``recent_experiences`` contains the committed Experience projection while
    ``world_life`` can additionally carry descriptor-bound prose from the
    immutable life-content store. Neither lane is interpreted here: this view
    only removes proof/accounting material and retains the Capsule item token.
    """

    source_ref = item.get("source_ref")
    value = item.get("value")
    if not isinstance(source_ref, str) or not source_ref or not isinstance(value, dict):
        return None
    recalled = _recalled_document_state_entry(
        item,
        memory_kinds=frozenset({"episodic"}),
    )
    if recalled is not None:
        # A trusted RecallTrace is injected as a compact, flat document view
        # rather than either nested live projection below. Keep the selected
        # first-person episode in the working-self view instead of burying it
        # back in a generic Context lane after the character's own attention
        # mechanism surfaced it.
        return recalled
    elif lane == "world_life":
        if value.get("context_kind") == "biographical_context":
            return None
        fields = (
            (
                "occurrence_id",
                "occurrence_entity_revision",
                "participant_refs",
                "location_ref",
                "time_window",
                "activated_at",
                "status",
                "privacy_class",
                "premise",
            )
            if value.get("context_kind") == "active_world_occurrence"
            else (
                "occurrence_id",
                "occurrence_entity_revision",
                "participant_refs",
                "location_ref",
                "result_id",
                "settled_at",
                "privacy_class",
                "content",
            )
        )
        semantic = {key: value[key] for key in fields if key in value}
    else:
        values = value.get("values")
        if isinstance(values, dict):
            fields = (
                "summary_ref",
                "occurred_from",
                "occurred_to",
                "participant_refs",
                "privacy_class",
            )
            semantic = {
                **(
                    {"experience_id": value["experience_id"]}
                    if isinstance(value.get("experience_id"), str)
                    else {}
                ),
                **{key: values[key] for key in fields if key in values},
            }
            content = value.get("content")
            if isinstance(content, dict):
                content_fields = ("content_ref", "text", "truncated")
                compact_content = {
                    key: content[key] for key in content_fields if key in content
                }
                if isinstance(compact_content.get("text"), str):
                    semantic["content"] = compact_content
        else:
            return None
    if not semantic:
        return None
    return {**semantic, "source_ref": source_ref}


def _state_source_refs(state: dict[str, object]) -> list[str]:
    """Collect only the semantic Capsule item refs represented in this view."""

    refs: set[str] = set()
    for value in state.values():
        candidates: object = value
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            candidates = value["items"]
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if isinstance(item, dict) and isinstance(item.get("source_ref"), str):
                refs.add(item["source_ref"])
    return sorted(refs)


def _build_current_self_state(
    *,
    slices: dict[str, object],
    logical_time: object,
) -> dict[str, object]:
    """Derive one compact working-self view from already verified slices.

    It deliberately preserves concurrent Affect components and Appraisal
    alternatives.  This is a salience/view layer only: source refs continue to
    point at the authoritative Capsule items and no derived prose is created.
    """

    state: dict[str, object] = {
        "contract": "current-self-state.1",
        "authority": "derived_from_verified_context",
    }
    if isinstance(logical_time, str):
        state["logical_time"] = logical_time
    stable_self = [
        entry
        for item in _slice_items(slices, "character_core")
        for entry in (_core_state_entry(item),)
        if entry is not None
    ]
    if stable_self:
        state["stable_self"] = stable_self
    biographical_context = [
        entry
        for item in _slice_items(slices, "world_life")
        if isinstance(item.get("value"), dict)
        and item["value"].get("context_kind") == "biographical_context"
        for entry in (
            _state_entry(
                item,
                fields=(
                    "reviewed_timeline_ref",
                    "timeline_source_event_ref",
                    "logical_at",
                    "age",
                    "academic_phase",
                    "academic_year",
                    "season",
                    "calendar_context_tags",
                    "current_residence_context_tags",
                    "active_life_arcs",
                    "settled_biographical_coordinates",
                ),
            ),
        )
        if entry is not None
    ]
    if biographical_context:
        state["biographical_context"] = biographical_context
    lane_contracts = (
        (
            "situation",
            "current_situation",
            (
                "logical_time",
                "time_segment",
                "activity_slices",
                "goal_slices",
                "resource_pressure",
                "attention_slice",
                "social_environment",
                "plan_relation",
                "commitment_slices",
            ),
        ),
        (
            "relationship",
            "relationship_slice",
            (
                "subject_ref",
                "stage",
                "variables",
                "temperature",
                "hysteresis",
                "commitment_refs",
                "last_adjusted_at",
            ),
        ),
        (
            "appraisals",
            "appraisals",
            (
                "subject_ref",
                "source_cluster_ref",
                "hypotheses",
                "evidence_refs",
                "confidence_bp",
                "accepted_at",
                "expires_at",
            ),
        ),
        (
            "unresolved",
            "open_threads",
            (
                "kind",
                "subject_ref",
                "importance_bp",
                "due_window",
                "window_closes_at",
                "expected_response_ref",
                "status",
            ),
        ),
        (
            "advisories",
            "advisories",
            (
                "kind",
                "candidate_refs",
                "candidates",
                "confidence_bp",
                "expiry",
                "producer_version",
            ),
        ),
    )
    for output_name, slice_name, fields in lane_contracts:
        entries = [
            entry
            for item in _slice_items(slices, slice_name)
            for entry in (_state_entry(item, fields=fields),)
            if entry is not None
        ]
        if entries:
            state[output_name] = entries
    advisories = state.get("advisories")
    if isinstance(advisories, list):
        interruption = [
            item
            for item in advisories
            if isinstance(item, dict)
            and isinstance(item.get("kind"), str)
            and item["kind"].startswith("interruption.")
        ]
        if interruption:
            state["interruption"] = interruption
            remaining = [item for item in advisories if item not in interruption]
            if remaining:
                state["advisories"] = remaining
            else:
                state.pop("advisories", None)
    affect = [
        entry
        for item in _slice_items(slices, "affect_episodes")
        for entry in (_affect_state_entry(item),)
        if entry is not None
    ]
    if affect:
        state["affect"] = affect
    remembered_material = [
        entry
        for item in _slice_items(slices, "active_memory_candidates")
        for entry in (_state_entry(item),)
        if entry is not None
    ][:2]
    if remembered_material:
        # These are already source-bound retrieval excerpts, not a host-authored
        # suggestion to mention them.  Keeping a tiny working set beside Affect
        # and current life makes memory available to the character's own
        # attention without inventing a memory-to-response rule.
        state["remembered_material"] = remembered_material
    recalled_emotional_associations = [
        entry
        for item in _slice_items(slices, "recalled_emotional_associations")
        for entry in (
            _recalled_document_state_entry(
                item,
                memory_kinds=frozenset({"reflective"}),
            ),
        )
        if entry is not None
    ][:2]
    if recalled_emotional_associations:
        # These are defeasible memories of prior feeling/interpretation, not a
        # host-generated mood or a fact about the counterpart. Their source
        # labels remain visible so the role can notice or ignore them.
        state["recalled_emotional_associations"] = recalled_emotional_associations
    private_impressions = [
        entry
        for item in _slice_items(slices, "private_impressions")
        for entry in (
            _state_entry(
                item,
                fields=(
                    "subject_ref",
                    "reflection_summary",
                    "confidence_bp",
                    "first_seen",
                    "last_supported",
                    "expiry_condition",
                    "contradiction_refs",
                    "status",
                ),
            ),
        )
        if entry is not None
    ][:2]
    if private_impressions:
        state["private_impressions"] = private_impressions
    experience_lanes = [
        [
            entry
            for item in _slice_items(slices, lane)
            for entry in (_recent_experience_state_entry(item, lane=lane),)
            if entry is not None
        ]
        for lane in ("world_life", "recent_experiences")
    ]
    # Keep the tiny current-self budget representative of both immediate life
    # occurrences and committed experiences.  A busy world-life lane must not
    # crowd durable personal experience out of the character's own attention.
    recent_self_experiences = [
        entries[0] for entries in experience_lanes if entries
    ]
    if len(recent_self_experiences) < 2:
        recent_self_experiences.extend(
            entry
            for entries in experience_lanes
            for entry in entries[1:]
        )
    recent_self_experiences = recent_self_experiences[:2]
    state["recent_self_experiences"] = (
        {
            "availability": "available",
            "items": recent_self_experiences,
        }
        if recent_self_experiences
        else {"availability": "unavailable"}
    )
    source_refs = _state_source_refs(state)
    state["source_refs"] = source_refs
    state["availability"] = "available" if source_refs else "unavailable"
    return state


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
            attention_refs = item.get("attention_source_refs")
            if isinstance(attention_refs, list):
                retained_attention_refs = [ref for ref in attention_refs if isinstance(ref, str)]
                if retained_attention_refs:
                    material["attention_source_refs"] = retained_attention_refs
            semantic_items.append(material)
        if semantic_items:
            slices[name] = {"availability": "available", "items": semantic_items}
    context["slices"] = slices
    context["current_self_state"] = _build_current_self_state(
        slices=slices,
        logical_time=context.get("logical_time"),
    )
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
    # Recovery keeps the source-bearing slice because validators and the
    # reviewer resolve attention refs through slice items.  Its duplicate
    # top-level convenience view is unnecessary on this deliberately tiny
    # fallback path.
    context.pop("pinned_time", None)
    context["current_self_state"] = _build_current_self_state(
        slices=retained,
        logical_time=context.get("logical_time"),
    )
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
                    lane.get("availability") if isinstance(lane, dict) else "unavailable"
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
