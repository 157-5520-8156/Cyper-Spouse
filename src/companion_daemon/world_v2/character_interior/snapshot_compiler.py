"""Deterministic compiler for the one canonical ``InnerLifeSnapshot``.

The input is the verified Context Capsule's model material.  This module owns
the semantic join and snapshot identity; provider-specific views may only
redact its model view and must retain the same snapshot id and hash.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Mapping

from ..schemas import ProjectionCursor
from .contracts import (
    FACET_NAMES,
    InnerLifeSnapshot,
    _InteriorBinding,
    _InteriorContextView,
    _InteriorFacet,
    _InteriorSourceAuthorityBinding,
    _InteriorSourceInventoryItem,
)


SNAPSHOT_COMPILER_VERSION = "inner-life-snapshot-compiler.7"

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


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _semantic_value(value: object) -> object:
    if isinstance(value, list):
        return [_semantic_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _semantic_value(item)
        for key, item in value.items()
        if isinstance(key, str)
        and key not in _AUTHORITY_VALUE_KEYS
        and not key.endswith("_hash")
        and not key.endswith("_digest")
        and not key.endswith("_version")
    }


def _slice_items(slices: Mapping[str, object], name: str) -> list[dict[str, object]]:
    lane = slices.get(name)
    if not isinstance(lane, dict) or lane.get("availability") != "available":
        return []
    items = lane.get("items")
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_ref = item.get("source_ref")
        if not isinstance(source_ref, str):
            source_ref = item.get("item_ref")
        normalized.append(
            {
                **item,
                **({"source_ref": source_ref} if isinstance(source_ref, str) else {}),
            }
        )
    return normalized


def _state_entry(
    item: dict[str, object], *, fields: tuple[str, ...] | None = None
) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    value = item.get("value")
    if not isinstance(source_ref, str) or not source_ref or not isinstance(value, dict):
        return None
    semantic = (
        {key: value[key] for key in fields if key in value}
        if fields is not None
        else value
    )
    semantic = _semantic_value(semantic)
    if not isinstance(semantic, dict) or not semantic:
        return None
    return {**semantic, "source_ref": source_ref}


def _core_entry(item: dict[str, object]) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    value = item.get("value")
    values = value.get("values") if isinstance(value, dict) else None
    slow = values.get("slow_evolving") if isinstance(values, dict) else None
    if not isinstance(source_ref, str) or not isinstance(slow, dict):
        return None
    return {"slow_evolving": _semantic_value(slow), "source_ref": source_ref}


def _affect_entry(item: dict[str, object]) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    value = item.get("value")
    if not isinstance(source_ref, str) or not isinstance(value, dict):
        return None
    components: list[dict[str, object]] = []
    for component in value.get("components", []):
        if not isinstance(component, dict) or not isinstance(component.get("dimension"), str):
            continue
        continuity = {
            key: _semantic_value(component[key])
            for key in (
                "component_id",
                "dimension",
                "source_cluster_ref",
                "appraisal_refs",
                "intensity_bp",
                "decay_anchor_intensity_bp",
                "residue_bp",
                "opened_at",
                "decay_anchor_at",
                "last_stimulus_at",
                "last_updated_at",
                "decay_not_before",
            )
            if key in component
        }
        decay_profile = component.get("decay_profile")
        if isinstance(decay_profile, dict):
            # These are already accepted deterministic lifecycle parameters,
            # not a second mood verdict.  Keep the complete profile so the
            # character can perceive whether a feeling is rising, lingering,
            # or only resting on residue instead of seeing one flat number.
            continuity["decay_profile"] = {
                key: decay_profile[key]
                for key in (
                    "kind",
                    "half_life_seconds",
                    "floor_bp",
                    "delay_seconds",
                    "config_version",
                    "algorithm_version",
                    "table_digest",
                    "rounding_mode",
                    "config_digest",
                )
                if key in decay_profile
            }
        components.append(continuity)
    if not components:
        return None
    return {
        "components": components,
        **{
            key: _semantic_value(value[key])
            for key in (
                "episode_id",
                "entity_revision",
                "status",
                "opened_at",
                "updated_at",
                "expression_history_refs",
                "closed_at",
                "resolution_refs",
                "supersedes_episode_id",
                "superseded_by_episode_id",
            )
            if value.get(key) is not None
        },
        "source_ref": source_ref,
    }


def _recalled_entry(
    item: dict[str, object], *, kinds: frozenset[str]
) -> dict[str, object] | None:
    source_ref = item.get("source_ref")
    value = item.get("value")
    if (
        not isinstance(source_ref, str)
        or not isinstance(value, dict)
        or value.get("memory_kind") not in kinds
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
        **{key: value[key] for key in fields if value.get(key) is not None},
        "source_ref": source_ref,
    }


def _experience_entry(
    item: dict[str, object], *, lane: str
) -> dict[str, object] | None:
    recalled = _recalled_entry(item, kinds=frozenset({"episodic"}))
    if recalled is not None:
        return recalled
    source_ref = item.get("source_ref")
    value = item.get("value")
    if not isinstance(source_ref, str) or not isinstance(value, dict):
        return None
    if lane == "world_life":
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
        if not isinstance(values, dict):
            return None
        semantic = {
            **(
                {"experience_id": value["experience_id"]}
                if isinstance(value.get("experience_id"), str)
                else {}
            ),
            **{
                key: values[key]
                for key in (
                    "summary_ref",
                    "occurred_from",
                    "occurred_to",
                    "participant_refs",
                    "privacy_class",
                )
                if key in values
            },
        }
        content = value.get("content")
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            semantic["content"] = {
                key: content[key]
                for key in ("content_ref", "text", "truncated")
                if key in content
            }
    semantic = _semantic_value(semantic)
    return (
        {**semantic, "source_ref": source_ref}
        if isinstance(semantic, dict) and semantic
        else None
    )


def _material_refs(value: object) -> tuple[str, ...]:
    candidates = value.get("items") if isinstance(value, dict) else value
    if not isinstance(candidates, list):
        return ()
    return tuple(
        item["source_ref"]
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("source_ref"), str)
    )


def _inventory_text(item: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _unique_refs(value: object, *, keys: frozenset[str]) -> tuple[str, ...]:
    """Extract only explicitly typed ref fields, never arbitrary string values."""

    refs: list[str] = []

    def visit(item: object, field_name: str | None = None) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    continue
                if key in keys:
                    if isinstance(child, str) and child:
                        refs.append(child)
                    elif isinstance(child, list):
                        for candidate in child:
                            if isinstance(candidate, str) and candidate:
                                refs.append(candidate)
                            elif isinstance(candidate, dict):
                                ref = candidate.get("ref_id") or candidate.get("event_ref")
                                if isinstance(ref, str) and ref:
                                    refs.append(ref)
                    elif isinstance(child, dict):
                        ref = child.get("ref_id") or child.get("event_ref")
                        if isinstance(ref, str) and ref:
                            refs.append(ref)
                visit(child, key)
        elif isinstance(item, list):
            for child in item:
                visit(child, field_name)

    visit(value)
    return tuple(dict.fromkeys(refs))


_DIRECT_REF_FIELDS = frozenset(
    {
        "source_refs",
        "source_evidence_refs",
        "anchor_evidence_refs",
        "evidence_refs",
        "accepted_event_ref",
        "source_event_ref",
        "timeline_source_event_ref",
        "authority_event_ref",
        "event_ref",
    }
)
_PREDECESSOR_REF_FIELDS = frozenset(
    {
        "predecessor_refs",
        "predecessor_thread_refs",
        "predecessor_commitment_ref",
        "supersedes_episode_id",
        "supersedes_goal_id",
    }
)
_CONFLICT_REF_FIELDS = frozenset(
    {
        "conflict_refs",
        "contradiction_refs",
        "contradiction_group_ref",
        "conflict_key",
    }
)
_REVISION_REF_FIELDS = frozenset(
    {
        "revision_event_ref",
        "planted_event_ref",
        "superseded_by_episode_id",
        "superseded_by_thread_ref",
    }
)


def _entity_revision(value: object) -> int | None:
    if not isinstance(value, dict):
        return None
    for key in (
        "entity_revision",
        "occurrence_entity_revision",
        "source_entity_revision",
    ):
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 1:
            return candidate
    nested = value.get("values")
    return _entity_revision(nested)


def _source_envelope(
    item: Mapping[str, object],
    *,
    trusted: Mapping[str, object] | None,
) -> tuple[
    tuple[_InteriorSourceAuthorityBinding, ...],
    tuple[str, ...],
    int | None,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    envelope = trusted or item
    raw_bindings = envelope.get("source_bindings")
    bindings: list[_InteriorSourceAuthorityBinding] = []
    if isinstance(raw_bindings, (list, tuple)):
        for binding in raw_bindings:
            if not isinstance(binding, dict):
                continue
            bindings.append(_InteriorSourceAuthorityBinding.model_validate(binding))
    bindings.sort(
        key=lambda value: (
            value.source_kind,
            value.authority_type,
            value.ref,
            value.source_world_revision,
            value.immutable_hash,
        )
    )
    raw_value = envelope.get("value")
    if not isinstance(raw_value, dict):
        raw_value = item.get("value")
    authority_refs = [binding.ref for binding in bindings]
    direct = tuple(
        dict.fromkeys(
            (
                *authority_refs,
                *_unique_refs(raw_value, keys=_DIRECT_REF_FIELDS),
            )
        )
    )
    return (
        tuple(bindings),
        direct,
        _entity_revision(raw_value),
        _unique_refs(raw_value, keys=_PREDECESSOR_REF_FIELDS),
        _unique_refs(raw_value, keys=_CONFLICT_REF_FIELDS),
        _unique_refs(raw_value, keys=_REVISION_REF_FIELDS),
    )


def source_envelopes_from_capsule(capsule: object) -> dict[str, dict[str, object]]:
    """Extract full trusted item proofs without placing them in model material."""

    result: dict[str, dict[str, object]] = {}
    for lane in (
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
        "perception_results",
        "active_memory_candidates",
        "available_capabilities",
        "action_budget",
        "private_impressions",
        "advisories",
    ):
        bound = getattr(capsule, lane, None)
        for item in getattr(bound, "items", ()):
            dumped = item.model_dump(mode="json")
            item_ref = dumped.get("item_ref")
            if not isinstance(item_ref, str):
                raise ValueError("trusted Capsule item has no stable ref")
            value = json.loads(item.payload_json)
            envelope = {**dumped, "value": value}
            existing = result.get(item_ref)
            if existing is not None and existing != envelope:
                raise ValueError("Capsule reused an item ref with conflicting authority")
            result[item_ref] = envelope
    return result


def _view(
    materials: Mapping[str, object], keys: tuple[str, ...]
) -> _InteriorContextView:
    selected = {key: materials[key] for key in keys if key in materials}
    refs = tuple(
        dict.fromkeys(ref for value in selected.values() for ref in _material_refs(value))
    )
    if not refs:
        return _InteriorContextView.from_material(
            availability="unavailable", content={}, source_refs=()
        )
    return _InteriorContextView.from_material(
        availability="available", content=selected, source_refs=refs
    )


def _datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _cursor(context: Mapping[str, object]) -> ProjectionCursor | None:
    values = tuple(context.get(key) for key in (
        "world_revision", "deliberation_revision", "ledger_sequence"
    ))
    if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values):
        return None
    return ProjectionCursor(
        world_revision=values[0],
        deliberation_revision=values[1],
        ledger_sequence=values[2],
    )


def _binding(context: Mapping[str, object], key: str, reason: str) -> _InteriorBinding:
    value = context.get(key)
    return _InteriorBinding.available(value) if value is not None else _InteriorBinding.unavailable(reason)


def compile_inner_life_snapshot(
    context: Mapping[str, object],
    *,
    source_envelopes: Mapping[str, Mapping[str, object]] | None = None,
) -> InnerLifeSnapshot:
    """Compile the canonical typed snapshot from verified Capsule material."""

    raw_slices = context.get("slices")
    slices: Mapping[str, object] = raw_slices if isinstance(raw_slices, dict) else {}
    materials: dict[str, object] = {}
    logical_time = _datetime(context.get("logical_time"))
    if logical_time is not None:
        materials["logical_time"] = logical_time.isoformat()

    stable = [entry for item in _slice_items(slices, "character_core") if (entry := _core_entry(item))]
    if stable:
        materials["stable_self"] = stable
    biography = [
        entry
        for item in _slice_items(slices, "world_life")
        if isinstance(item.get("value"), dict)
        and item["value"].get("context_kind") == "biographical_context"
        and (entry := _state_entry(item, fields=(
            "reviewed_timeline_ref", "timeline_source_event_ref", "logical_at", "age",
            "academic_phase", "academic_year", "season", "calendar_context_tags",
            "current_residence_context_tags", "active_life_arcs",
            "settled_biographical_coordinates",
        )))
    ]
    if biography:
        materials["biographical_context"] = biography

    lanes = (
        ("situation", "current_situation", (
            "logical_time", "time_segment", "activity_slices", "goal_slices",
            "resource_pressure", "attention_slice", "social_environment",
            "plan_relation", "commitment_slices",
        )),
        ("relationship", "relationship_slice", (
            "subject_ref", "stage", "variables", "temperature", "hysteresis",
            "commitment_refs", "last_adjusted_at",
        )),
        (
            "protagonist_npc_relationships",
            "protagonist_npc_relationships",
            (
                "relationship_id",
                "direction",
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
            "npc_observable_attitudes",
            "npc_observable_attitudes",
            (
                "direction",
                "npc_ref",
                "toward_actor_ref",
                "epistemic_scope",
                "observable_act",
            ),
        ),
        (
            "interaction_acts",
            "interaction_acts",
            (
                "frame",
                "participant_statuses",
                "external_outcome",
            ),
        ),
        ("appraisals", "appraisals", (
            "subject_ref", "source_cluster_ref", "hypotheses", "evidence_refs",
            "confidence_bp", "accepted_at", "expires_at",
        )),
        ("unresolved", "open_threads", (
            "kind", "subject_ref", "importance_bp", "due_window",
            "window_closes_at", "expected_response_ref", "status",
        )),
        ("advisories", "advisories", (
            "kind", "candidate_refs", "candidates", "confidence_bp", "expiry",
            "producer_version",
        )),
        ("perception", "perception_results", None),
    )
    for output, lane, fields in lanes:
        entries = [entry for item in _slice_items(slices, lane) if (entry := _state_entry(item, fields=fields))]
        if entries:
            materials[output] = entries
    advisories = materials.get("advisories")
    if isinstance(advisories, list):
        interruption = [item for item in advisories if isinstance(item.get("kind"), str) and item["kind"].startswith("interruption.")]
        change_phase = [
            item for item in advisories if item.get("kind") == "change_phase"
        ]
        if interruption:
            materials["interruption"] = interruption
        if change_phase:
            materials["change_phase"] = change_phase
        extracted = {*map(id, interruption), *map(id, change_phase)}
        remaining = [item for item in advisories if id(item) not in extracted]
        if remaining:
            materials["advisories"] = remaining
        else:
            materials.pop("advisories")

    affect = [entry for item in _slice_items(slices, "affect_episodes") if (entry := _affect_entry(item))]
    if affect:
        materials["affect"] = affect
    remembered = [entry for item in _slice_items(slices, "active_memory_candidates") if (entry := _state_entry(item))][:1]
    if remembered:
        materials["remembered_material"] = remembered
    emotional = [entry for item in _slice_items(slices, "recalled_emotional_associations") if (entry := _recalled_entry(item, kinds=frozenset({"reflective"})))][:1]
    if emotional:
        materials["recalled_emotional_associations"] = emotional
    impressions = [entry for item in _slice_items(slices, "private_impressions") if (entry := _state_entry(item, fields=(
        "subject_ref", "reflection_summary", "confidence_bp", "first_seen",
        "last_supported", "expiry_condition", "contradiction_refs", "status",
    )))][:2]
    if impressions:
        materials["private_impressions"] = impressions

    # Short conversational continuity is not a second memory system.  It is
    # the source-bound working-memory edge of the same Interior snapshot.  In
    # particular, proactive and background turns must see what the counterpart
    # actually said through this canonical material instead of receiving a
    # consumer-specific context side channel.
    recent_dialogue = [
        entry
        for item in _slice_items(slices, "recent_dialogue")
        if (
            entry := _state_entry(
                item,
                fields=(
                    "dialogue_id",
                    "speaker",
                    "speaker_ref",
                    "text",
                    "occurred_at",
                    "delivery_state",
                    "sequence",
                    "acknowledges_observation_event_refs",
                    "continuity_reasons",
                ),
            )
        )
    ][-4:]
    if recent_dialogue:
        materials["recent_dialogue"] = recent_dialogue

    # Verified facts are memory material, not host-authored conclusions about
    # what the character should do.  Keeping them in the same snapshot lets
    # every purpose reason from one source closure while preserving the role's
    # freedom to ignore or reinterpret their relevance.
    relevant_facts = [
        entry
        for item in _slice_items(slices, "relevant_facts")
        if (entry := _state_entry(item))
    ][:3]
    if relevant_facts:
        materials["relevant_facts"] = relevant_facts

    experience_lanes = [
        [entry for item in _slice_items(slices, lane) if (entry := _experience_entry(item, lane=lane))]
        for lane in ("world_life", "recent_experiences")
    ]
    recent = [entries[0] for entries in experience_lanes if entries]
    if len(recent) < 2:
        recent.extend(entry for entries in experience_lanes for entry in entries[1:])
    materials["recent_self_experiences"] = (
        {"availability": "available", "items": recent[:2]}
        if recent
        else {"availability": "unavailable"}
    )

    facet_keys = {
        "private_self": ("stable_self", "biographical_context", "situation", "private_impressions", "recent_self_experiences"),
        "selective_memory": (
            "recent_dialogue",
            "relevant_facts",
            "remembered_material",
            "recalled_emotional_associations",
        ),
        "appraisal_affect": ("appraisals", "affect"),
        "emotional_continuity": (
            "appraisals",
            "affect",
            "change_phase",
            "interruption",
        ),
        "subjective_relationship": (
            "relationship",
            "protagonist_npc_relationships",
            "npc_observable_attitudes",
            "private_impressions",
            "recent_dialogue",
            "interaction_acts",
        ),
        "aspirations_conflicts": ("situation", "unresolved"),
        "autonomous_impulses": (
            "situation",
            "relationship",
            "protagonist_npc_relationships",
            "npc_observable_attitudes",
            "appraisals",
            "affect",
            "unresolved",
            "perception",
            "recent_self_experiences",
            "recent_dialogue",
            "relevant_facts",
            "interaction_acts",
        ),
        "expression_stance": (
            "stable_self",
            "situation",
            "relationship",
            "protagonist_npc_relationships",
            "npc_observable_attitudes",
            "appraisals",
            "affect",
            "private_impressions",
            "recent_dialogue",
            "relevant_facts",
            "interaction_acts",
        ),
    }
    facets: list[_InteriorFacet] = []
    for name in FACET_NAMES:
        keys = tuple(key for key in facet_keys[name] if key in materials)
        refs = tuple(dict.fromkeys(ref for key in keys for ref in _material_refs(materials[key])))
        view = _InteriorContextView.from_material(
            availability="available" if refs else "unavailable",
            content={"material_keys": list(keys)} if refs else {},
            source_refs=refs,
        )
        facets.append(_InteriorFacet(name=name, **view.model_dump(mode="python")))

    privacy_by_ref = {
        item["source_ref"]: item.get("privacy_class")
        for lane in slices
        for item in _slice_items(slices, lane)
        if isinstance(item.get("source_ref"), str)
    }
    inventory: list[_InteriorSourceInventoryItem] = []
    for scope, value in materials.items():
        candidates = value.get("items") if isinstance(value, dict) else value
        if not isinstance(candidates, list):
            continue
        for item in candidates:
            if not isinstance(item, dict) or not isinstance(item.get("source_ref"), str):
                continue
            raw_source = next(
                (
                    raw
                    for lane in slices
                    for raw in _slice_items(slices, lane)
                    if raw.get("source_ref") == item["source_ref"]
                ),
                item,
            )
            trusted = (
                source_envelopes.get(item["source_ref"])
                if source_envelopes is not None
                else None
            )
            (
                authority_bindings,
                direct_source_refs,
                entity_revision,
                predecessor_refs,
                conflict_refs,
                revision_refs,
            ) = _source_envelope(raw_source, trusted=trusted)
            raw_value = (
                trusted.get("value")
                if isinstance(trusted, Mapping)
                else raw_source.get("value")
            )
            provenance_value = raw_value if isinstance(raw_value, Mapping) else item
            inventory.append(_InteriorSourceInventoryItem(
                source_ref=item["source_ref"], scope=scope,
                content_hash=_digest(item),
                privacy_class=(
                    trusted.get("privacy_class")
                    if isinstance(trusted, Mapping)
                    and isinstance(trusted.get("privacy_class"), str)
                    else privacy_by_ref.get(item["source_ref"])
                    if isinstance(privacy_by_ref.get(item["source_ref"]), str)
                    else None
                ),
                authority_scope=_inventory_text(
                    provenance_value,
                    "authority",
                    "epistemic_scope",
                ),
                authority_bindings=authority_bindings,
                direct_source_refs=direct_source_refs,
                entity_revision=entity_revision,
                valid_from=_inventory_text(provenance_value, "valid_from"),
                valid_to=_inventory_text(provenance_value, "valid_to"),
                expires_at=_inventory_text(
                    provenance_value,
                    "expires_at",
                    "expiry",
                    "window_closes_at",
                ),
                predecessor_refs=predecessor_refs,
                conflict_refs=conflict_refs,
                revision_refs=revision_refs,
            ))
    inventory.sort(key=lambda item: (item.source_ref, item.scope))
    source_refs = tuple(dict.fromkeys(item.source_ref for item in inventory))

    capabilities = _slice_items(slices, "available_capabilities")
    capability_scope = (
        _InteriorBinding.available({
            "source_refs": [item["source_ref"] for item in capabilities if isinstance(item.get("source_ref"), str)],
            "content_hash": _digest([{"source_ref": item.get("source_ref"), "value": _semantic_value(item.get("value"))} for item in capabilities]),
        })
        if capabilities
        else _InteriorBinding.unavailable("capability_scope_unavailable")
    )
    situation = _view(materials, ("logical_time", "biographical_context", "situation"))
    continuity = _view(materials, tuple(key for key in materials if key not in {"logical_time", "biographical_context", "situation"}))
    world_id = context.get("world_id") if isinstance(context.get("world_id"), str) else None
    actor_ref = context.get("actor_ref") if isinstance(context.get("actor_ref"), str) else None
    cursor = _cursor(context)
    available = bool(source_refs)
    return InnerLifeSnapshot.create(
        availability="available" if available else "unavailable",
        world_id=world_id, actor_ref=actor_ref, cursor=cursor, logical_time=logical_time,
        situation=situation, continuity=continuity, facet_views=tuple(facets),
        materials=materials, source_refs=source_refs, source_inventory=tuple(inventory),
        viewer_scope=_binding(context, "consumer_scope", "viewer_scope_unavailable"),
        privacy_scope=_binding(context, "viewer_privacy_ceiling", "viewer_privacy_scope_unavailable"),
        capability_scope=capability_scope,
        context_compiler=_binding(context, "context_compiler_version", "context_compiler_unavailable"),
        snapshot_compiler=_InteriorBinding.available(SNAPSHOT_COMPILER_VERSION),
        truncation=_binding(context, "truncation", "truncation_metadata_unavailable"),
    )


def visible_source_refs(context: Mapping[str, object]) -> frozenset[str]:
    """Return the source tokens visible in one redacted provider Context."""

    slices = context.get("slices")
    if not isinstance(slices, dict):
        return frozenset()
    return frozenset(
        source_ref
        for lane in slices
        for item in _slice_items(slices, lane)
        if isinstance((source_ref := item.get("source_ref")), str)
    )


__all__ = [
    "SNAPSHOT_COMPILER_VERSION",
    "compile_inner_life_snapshot",
    "source_envelopes_from_capsule",
    "visible_source_refs",
]
