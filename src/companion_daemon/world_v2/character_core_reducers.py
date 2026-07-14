"""Pure CharacterCore C1 reducers and exact authority resolvers."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json

from .character_core_events import CharacterCoreChangedPayload
from .fact_events import FACT_PAYLOAD_MODELS
from .schemas import (
    ActorAuthorityProjection,
    CharacterCoreEvidenceBinding,
    CharacterCoreProjection,
    CharacterCoreTransitionProjection,
    CharacterCoreValues,
    CommittedWorldEventRef,
    ExperienceProjection,
    ExperienceTransitionProjection,
    FactProjection,
    FactTransitionProjection,
    PrivacyClass,
    WorldOccurrenceProjection,
    character_core_evidence_authority_id,
)


CHARACTER_CORE_POLICY_REFS = ("policy:character-core-v1",)
CHARACTER_CORE_POLICY_VERSION = "character-core-policy.1"
CHARACTER_CORE_OPERATOR_OPERATION = "character_core_governance"
MIN_LONGITUDINAL_WINDOW = timedelta(days=14)
ROLLING_DRIFT_WINDOW = timedelta(days=90)
MAX_TRANSITION_AXIS_DELTA_BP = 800
MAX_ROLLING_AXIS_DRIFT_BP = 1600
MAX_TRANSITION_TOTAL_VARIATION_BP = 1200
MAX_ROLLING_TOTAL_VARIATION_BP = 2400
MAX_PREFERENCE_CHANGES = 2
MAX_ROLLING_PREFERENCE_CHURN = 4
MAX_ROLLING_STYLE_CHANGES = 2
_FIELD_LANE_MATRIX = {
    "operator_initialize": (
        "immutable_identity",
        "operator_governed",
        "privacy_class",
        "slow_evolving",
    ),
    "operator_revision": ("operator_governed", "privacy_class", "slow_evolving"),
    "longitudinal_evolution": ("privacy_class", "slow_evolving"),
    "compensation": ("operator_governed", "privacy_class", "slow_evolving"),
}
_POLICY_ARTIFACT = {
    "version": CHARACTER_CORE_POLICY_VERSION,
    "stable_short_term_split": {
        "core_fields": ["immutable_identity", "operator_governed", "slow_evolving"],
        "excluded": ["mood", "affect", "goal", "attention", "relationship", "user_impression", "situation"],
    },
    "field_lane_matrix": _FIELD_LANE_MATRIX,
    "operator_authority": {
        "contract": "deployment-actor-authority:character-core.1",
        "principal_kind": "deployment_operator",
        "required_operation": CHARACTER_CORE_OPERATOR_OPERATION,
        "shadow_capability_is_enforcement_eligible": False,
    },
    "longitudinal": {
        "source_kinds": ["fact", "experience.1"],
        "minimum_supporting_sources": 2,
        "minimum_distinct_scenes": 2,
        "minimum_distinct_trigger_kinds": 1,
        "minimum_window_seconds": int(MIN_LONGITUDINAL_WINDOW.total_seconds()),
        "max_transition_axis_delta_bp": MAX_TRANSITION_AXIS_DELTA_BP,
        "rolling_window_seconds": int(ROLLING_DRIFT_WINDOW.total_seconds()),
        "max_rolling_axis_drift_bp": MAX_ROLLING_AXIS_DRIFT_BP,
        "max_transition_total_variation_bp": MAX_TRANSITION_TOTAL_VARIATION_BP,
        "max_rolling_total_variation_bp": MAX_ROLLING_TOTAL_VARIATION_BP,
        "max_preference_changes": MAX_PREFERENCE_CHANGES,
        "max_rolling_preference_churn": MAX_ROLLING_PREFERENCE_CHURN,
        "max_rolling_style_changes": MAX_ROLLING_STYLE_CHANGES,
        "evidence_authority_reuse": "forbidden",
        "evidence_cluster_reuse": "forbidden",
    },
    "compensation": {
        "target": "exact_latest_non_initialize_transition",
        "restoration": "exact_semantic_values_before_with_monotonic_privacy_floor",
        "operator_revision_requires_current_operator_authority": True,
        "redo_preserves_effective_authority_provenance": True,
    },
    "privacy": "lifetime-evidence-floor-and-no-loosening",
    "zero_cascade": True,
}
CHARACTER_CORE_POLICY_DIGEST = hashlib.sha256(
    json.dumps(_POLICY_ARTIFACT, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
_EVENT_OPERATION = {
    "CharacterCoreInitialized": "initialize",
    "CharacterCoreRevised": "revise",
    "CharacterCoreRevisionCompensated": "compensate",
}


def reduce_character_core(
    current: CharacterCoreProjection | None,
    history: tuple[CharacterCoreTransitionProjection, ...],
    payload: CharacterCoreChangedPayload,
    *,
    event_type: str,
    event_id: str,
    logical_time: datetime,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> tuple[CharacterCoreProjection, tuple[CharacterCoreTransitionProjection, ...]]:
    if _EVENT_OPERATION.get(event_type) != payload.operation:
        raise ValueError("character core event type does not match operation")
    after, before = payload.core_after, payload.core_before
    if payload.policy_refs != CHARACTER_CORE_POLICY_REFS or (
        payload.policy_version != CHARACTER_CORE_POLICY_VERSION
        or payload.policy_digest != CHARACTER_CORE_POLICY_DIGEST
    ):
        raise ValueError("character core mutation references an uninstalled policy artifact")
    if after.origin.accepted_event_ref != event_id or after.updated_at != logical_time:
        raise ValueError("character core after image is not pinned to mutation event time")
    if payload.operation == "initialize":
        if after.created_at != logical_time:
            raise ValueError("character core initialization time must match logical time")
    elif before is not None and after.created_at != before.created_at:
        raise ValueError("character core creation time is immutable")
    if after.origin.policy_refs != CHARACTER_CORE_POLICY_REFS:
        raise ValueError("character core origin policy is not installed")
    if any(item.transition_id == payload.transition_id for item in history):
        raise ValueError("character core transition identity already exists")

    operator_required = payload.authority_lane in {"operator_initialize", "operator_revision"}
    if payload.operation == "compensate":
        target = _exact_compensation_target(payload, history, committed_events)
        effective_lane = _effective_compensation_authority_lane(target, history)
        operator_required = effective_lane in {"operator_initialize", "operator_revision"}
    if operator_required:
        _resolve_operator_authority(
            payload,
            actor_authorities=actor_authorities,
            committed_events=committed_events,
            logical_time=logical_time,
        )
    elif payload.operator_authority is not None:
        raise ValueError("longitudinal authority cannot be upgraded by an unrelated operator claim")

    source_privacies: tuple[PrivacyClass, ...] = ()
    if payload.evidence_window is not None:
        source_privacies = _validate_evidence_window(
            payload,
            facts=facts,
            fact_history=fact_history,
            experiences=experiences,
            experience_history=experience_history,
            world_occurrences=world_occurrences,
            committed_events=committed_events,
            target_actor_ref=after.actor_ref,
        )
        _reject_evidence_reuse(payload, history)

    if payload.operation == "initialize":
        if current is not None or before is not None or after.entity_revision != 1:
            raise ValueError("character core initialize is unique revision one")
        if payload.changed_field_classes != (
            "immutable_identity",
            "operator_governed",
            "privacy_class",
            "slow_evolving",
        ):
            raise ValueError("character core initialize must declare all stable field classes")
    else:
        if current is None or before != current:
            raise ValueError("character core before image does not match current head")
        if payload.expected_entity_revision != current.entity_revision or (
            after.entity_revision != current.entity_revision + 1
        ):
            raise ValueError("character core entity revision compare-and-swap failed")
        if after.core_id != current.core_id or after.actor_ref != current.actor_ref or (
            after.values.immutable_identity != current.values.immutable_identity
        ):
            raise ValueError("character core immutable identity cannot change")
        changed = _changed_field_classes(current.values, after.values)
        if payload.changed_field_classes != changed:
            raise ValueError("character core declared field diff does not match before/after")
        allowed = set(_FIELD_LANE_MATRIX[payload.authority_lane])
        if not set(changed).issubset(allowed):
            raise ValueError("character core field class is not writable in authority lane")
        if payload.operation == "revise":
            if payload.authority_lane == "longitudinal_evolution":
                _validate_longitudinal_delta(current.values, after.values, history, logical_time)
            elif payload.authority_lane != "operator_revision":
                raise ValueError("character core revision has an invalid authority lane")
        else:
            target = _exact_compensation_target(payload, history, committed_events)
            if target.values_before is None or (
                after.values.model_dump(exclude={"privacy_class"})
                != target.values_before.model_dump(exclude={"privacy_class"})
            ):
                raise ValueError(
                    "character core compensation must restore exact prior semantic values"
                )
            expected_fields = tuple(
                item for item in target.changed_field_classes if item != "privacy_class"
            )
            if payload.changed_field_classes != expected_fields:
                raise ValueError("character core compensation field classes must reverse target")

    lifetime_floor = max(
        (_PRIVACY_RANK[item.evidence_window.privacy_floor] for item in history if item.evidence_window),
        default=0,
    )
    if source_privacies:
        required = max(_PRIVACY_RANK[item] for item in source_privacies)
        if payload.evidence_window is None or _PRIVACY_RANK[payload.evidence_window.privacy_floor] != required:
            raise ValueError("character evidence privacy floor is not exact")
        lifetime_floor = max(lifetime_floor, required)
    if before is not None:
        lifetime_floor = max(lifetime_floor, _PRIVACY_RANK[before.values.privacy_class])
    if _PRIVACY_RANK[after.values.privacy_class] < lifetime_floor:
        raise ValueError("character core privacy class weakens lifetime authority floor")

    transition = CharacterCoreTransitionProjection(
        transition_id=payload.transition_id,
        core_id=after.core_id,
        entity_revision=after.entity_revision,
        operation=payload.operation,
        authority_lane=payload.authority_lane,
        changed_field_classes=payload.changed_field_classes,
        values_before=before.values if before else None,
        values_after=after.values,
        evidence_window=payload.evidence_window,
        operator_authority=payload.operator_authority,
        change_id=payload.change_id,
        policy_refs=payload.policy_refs,
        policy_version=payload.policy_version,
        policy_digest=payload.policy_digest,
        accepted_event_ref=event_id,
        accepted_at=logical_time,
        compensates_transition_id=(
            payload.compensation_target.transition_id if payload.compensation_target else None
        ),
    )
    return after, (*history, transition)


def _changed_field_classes(
    before: CharacterCoreValues, after: CharacterCoreValues
) -> tuple[str, ...]:
    changed = []
    if before.immutable_identity != after.immutable_identity:
        changed.append("immutable_identity")
    if before.operator_governed != after.operator_governed:
        changed.append("operator_governed")
    if before.privacy_class != after.privacy_class:
        changed.append("privacy_class")
    if before.slow_evolving != after.slow_evolving:
        changed.append("slow_evolving")
    if changed == ["privacy_class"]:
        raise ValueError("character core privacy-only revision is not a personality mutation")
    if not changed:
        raise ValueError("character core mutation is a semantic no-op")
    return tuple(changed)


def _resolve_operator_authority(
    payload: CharacterCoreChangedPayload,
    *,
    actor_authorities: tuple[ActorAuthorityProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    logical_time: datetime,
) -> None:
    binding = payload.operator_authority
    if binding is None:
        raise ValueError("character core operator lane lacks actor authority")
    authority = next((item for item in actor_authorities if item.authority_id == binding.authority_id), None)
    committed = next((item for item in committed_events if item.event_id == binding.authority_event_ref), None)
    if (
        authority is None
        or authority.entity_revision != binding.authority_revision
        or authority.values.principal_ref != binding.principal_ref
        or authority.values.principal_kind != "deployment_operator"
        or authority.values.status != "active"
        or CHARACTER_CORE_OPERATOR_OPERATION not in authority.values.allowed_operations
        or authority.origin.event_ref != binding.authority_event_ref
        or authority.policy_digest != binding.authority_policy_digest
        or _canonical_hash(authority.values) != binding.authority_values_hash
        or committed is None
        or committed.world_revision != binding.authority_world_revision
        or committed.payload_hash != binding.authority_payload_hash
        or authority.values.valid_from > logical_time
        or (authority.values.expires_at is not None and logical_time >= authority.values.expires_at)
    ):
        raise ValueError("character core operator authority does not resolve current root authority")


def _validate_evidence_window(
    payload: CharacterCoreChangedPayload,
    **authority,
) -> tuple[PrivacyClass, ...]:
    window = payload.evidence_window
    if window is None:
        raise ValueError("character core longitudinal revision lacks evidence window")
    if window.policy_version != CHARACTER_CORE_POLICY_VERSION:
        raise ValueError("character evidence window policy is not installed")
    privacies = tuple(_resolve_evidence_source(item, **authority) for item in window.source_bindings)
    evolution_sources = tuple(
        item for item in window.source_bindings
        if item.source_kind == "experience" and item.polarity == "supporting"
    )
    if len(evolution_sources) < 2:
        raise ValueError("character evolution requires multiple supporting Experiences")
    occurrence_sources = tuple(
        item
        for item in evolution_sources
        if item.scene_ref.startswith("scene:occurrence:")
    )
    if len({item.scene_ref for item in occurrence_sources}) < 2:
        raise ValueError("character evolution requires cross-scene Experience authority")
    if max(item.observed_to for item in occurrence_sources) - min(
        item.observed_to for item in occurrence_sources
    ) < MIN_LONGITUDINAL_WINDOW:
        raise ValueError("character evolution evidence window is too short")
    return privacies


def _resolve_evidence_source(
    binding: CharacterCoreEvidenceBinding,
    *,
    facts: tuple[FactProjection, ...],
    fact_history: tuple[FactTransitionProjection, ...],
    experiences: tuple[object, ...],
    experience_history: tuple[ExperienceTransitionProjection, ...],
    world_occurrences: tuple[WorldOccurrenceProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
    target_actor_ref: str,
) -> PrivacyClass:
    committed = next((item for item in committed_events if item.event_id == binding.authority_event_ref), None)
    if committed is None or committed.world_revision != binding.authority_world_revision or (
        committed.payload_hash != binding.authority_payload_hash
    ):
        raise ValueError("character evidence does not resolve exact committed event")
    if binding.source_kind == "fact":
        transition = next(
            (
                item for item in fact_history
                if item.fact_id == binding.source_id
                and item.entity_revision == binding.source_entity_revision
                and item.accepted_event_ref == binding.authority_event_ref
            ),
            None,
        )
        current = next((item for item in facts if item.fact_id == binding.source_id), None)
        if transition is None or current is None or current.entity_revision != binding.source_entity_revision or (
            current.values.status != "active"
        ) or committed.event_type not in FACT_PAYLOAD_MODELS or (
            _canonical_hash(transition.values_after) != binding.source_values_hash
        ):
            raise ValueError("character evidence does not resolve current active Fact")
        expected_scene = f"scene:fact:{transition.values_after.assertion_binding.source_kind}:{transition.values_after.assertion_binding.source_ref}"
        expected_trigger = f"fact:{transition.values_after.predicate_code}"
        if binding.scene_ref != expected_scene or binding.trigger_kind != expected_trigger or (
            binding.observed_from != transition.accepted_at or binding.observed_to != transition.accepted_at
        ):
            raise ValueError("character Fact evidence classification is not source-derived")
        return transition.values_after.privacy_class
    transition = next(
        (
            item for item in experience_history
            if item.experience_id == binding.source_id
            and item.entity_revision == binding.source_entity_revision
            and item.accepted_event_ref == binding.authority_event_ref
        ),
        None,
    )
    current = next(
        (
            item for item in experiences
            if isinstance(item, ExperienceProjection) and item.experience_id == binding.source_id
        ),
        None,
    )
    if transition is None or current is None or current.authority_contract_version != "experience.1" or (
        current.entity_revision != binding.source_entity_revision
    ) or committed.event_type != "ExperienceCommitted" or (
        _canonical_hash(transition.values_after) != binding.source_values_hash
    ):
        raise ValueError("character evidence does not resolve hardened Experience")
    if target_actor_ref not in current.values.participant_refs:
        raise ValueError("character evolution Experience does not involve target actor")
    source = transition.values_after.source_bindings[0]
    if source.source_kind == "occurrence_settlement":
        occurrence = next(
            (
                item
                for item in world_occurrences
                if item.occurrence_id == source.occurrence_id
                and item.entity_revision == source.occurrence_entity_revision
            ),
            None,
        )
        if (
            occurrence is None
            or occurrence.status != "settled"
            or occurrence.settlement_event_ref != source.authority_event_ref
            or occurrence.settlement_world_revision != source.authority_world_revision
            or occurrence.settlement_payload_hash != source.authority_payload_hash
            or target_actor_ref not in occurrence.participant_refs
        ):
            raise ValueError(
                "character Experience lacks exact settled occurrence scene authority"
            )
        scene = (
            f"scene:occurrence:{occurrence.location_ref}:"
            f"{occurrence.time_window.opens_at.date().isoformat()}"
        )
        trigger = f"occurrence:{occurrence.trigger_ref}"
    else:
        scene = f"scene:receipt-unscoped:{source.action_id}"
        trigger = "experience:execution_receipt"
    if binding.scene_ref != scene or binding.trigger_kind != trigger or (
        binding.observed_from != transition.values_after.occurred_from
        or binding.observed_to != transition.values_after.occurred_to
    ):
        raise ValueError("character Experience evidence classification is not source-derived")
    return transition.values_after.privacy_class


def _reject_evidence_reuse(
    payload: CharacterCoreChangedPayload,
    history: tuple[CharacterCoreTransitionProjection, ...],
) -> None:
    window = payload.evidence_window
    if window is None:
        return
    new_ids = {character_core_evidence_authority_id(item) for item in window.source_bindings}
    new_lineages = {(item.source_kind, item.source_id) for item in window.source_bindings}
    new_cluster = hashlib.sha256(
        "|".join(
            sorted(f"{source_kind}:{source_id}" for source_kind, source_id in new_lineages)
        ).encode()
    ).hexdigest()
    for transition in history:
        old = transition.evidence_window
        if old is None:
            continue
        old_ids = {character_core_evidence_authority_id(item) for item in old.source_bindings}
        if new_ids & old_ids:
            raise ValueError("character evolution evidence authority is already consumed")
        old_lineages = {(item.source_kind, item.source_id) for item in old.source_bindings}
        if new_lineages & old_lineages:
            raise ValueError("character evolution stable source lineage is already consumed")
        old_cluster = hashlib.sha256(
            "|".join(
                sorted(
                    f"{source_kind}:{source_id}"
                    for source_kind, source_id in old_lineages
                )
            ).encode()
        ).hexdigest()
        if old_cluster == new_cluster:
            raise ValueError("character evolution evidence cluster is already consumed")


def _validate_longitudinal_delta(
    before: CharacterCoreValues,
    after: CharacterCoreValues,
    history: tuple[CharacterCoreTransitionProjection, ...],
    logical_time: datetime,
) -> None:
    old_axes = {item.axis_code: item.value_bp for item in before.slow_evolving.trait_axes}
    new_axes = {item.axis_code: item.value_bp for item in after.slow_evolving.trait_axes}
    old_values = {item.value_ref: item.priority_bp for item in before.slow_evolving.value_priorities}
    new_values = {item.value_ref: item.priority_bp for item in after.slow_evolving.value_priorities}
    if old_axes.keys() != new_axes.keys() or old_values.keys() != new_values.keys():
        raise ValueError("longitudinal evolution cannot add or remove numeric coordinates")
    deltas = {
        **{f"trait:{key}": abs(new_axes[key] - value) for key, value in old_axes.items()},
        **{f"value:{key}": abs(new_values[key] - value) for key, value in old_values.items()},
    }
    if any(value > MAX_TRANSITION_AXIS_DELTA_BP for value in deltas.values()):
        raise ValueError("character evolution exceeds per-transition drift limit")
    if sum(deltas.values()) > MAX_TRANSITION_TOTAL_VARIATION_BP:
        raise ValueError("character evolution exceeds total variation budget")
    preference_changes = len(
        set(before.slow_evolving.preference_refs) ^ set(after.slow_evolving.preference_refs)
    )
    if preference_changes > MAX_PREFERENCE_CHANGES:
        raise ValueError("character evolution changes too many preferences at once")
    style_fields = ("autonomy_style", "attachment_tendency", "conflict_style", "privacy_tendency")
    style_changes = sum(
        getattr(before.slow_evolving, field) != getattr(after.slow_evolving, field)
        for field in style_fields
    )
    if style_changes > 1:
        raise ValueError("character evolution changes too many categorical styles at once")
    rolling = {key: value for key, value in deltas.items()}
    rolling_total_variation = sum(deltas.values())
    rolling_preference_churn = preference_changes
    rolling_style_changes = style_changes
    cutoff = logical_time - ROLLING_DRIFT_WINDOW
    for transition in history:
        if transition.authority_lane != "longitudinal_evolution" or transition.accepted_at < cutoff or (
            transition.values_before is None
        ):
            continue
        previous = transition.values_before.slow_evolving
        current = transition.values_after.slow_evolving
        rolling_preference_churn += len(
            set(previous.preference_refs) ^ set(current.preference_refs)
        )
        rolling_style_changes += sum(
            getattr(previous, field) != getattr(current, field) for field in style_fields
        )
        for prefix, old_items, new_items, key_name, value_name in (
            ("trait", previous.trait_axes, current.trait_axes, "axis_code", "value_bp"),
            ("value", previous.value_priorities, current.value_priorities, "value_ref", "priority_bp"),
        ):
            old_map = {getattr(item, key_name): getattr(item, value_name) for item in old_items}
            new_map = {getattr(item, key_name): getattr(item, value_name) for item in new_items}
            for key in old_map.keys() & new_map.keys():
                coordinate = f"{prefix}:{key}"
                variation = abs(new_map[key] - old_map[key])
                rolling[coordinate] = rolling.get(coordinate, 0) + variation
                rolling_total_variation += variation
    if any(value > MAX_ROLLING_AXIS_DRIFT_BP for value in rolling.values()):
        raise ValueError("character evolution exceeds rolling drift limit")
    if rolling_total_variation > MAX_ROLLING_TOTAL_VARIATION_BP:
        raise ValueError("character evolution exceeds rolling total variation budget")
    if rolling_preference_churn > MAX_ROLLING_PREFERENCE_CHURN:
        raise ValueError("character evolution exceeds rolling preference churn limit")
    if rolling_style_changes > MAX_ROLLING_STYLE_CHANGES:
        raise ValueError("character evolution exceeds rolling style drift limit")


def _exact_compensation_target(
    payload: CharacterCoreChangedPayload,
    history: tuple[CharacterCoreTransitionProjection, ...],
    committed_events: tuple[CommittedWorldEventRef, ...],
) -> CharacterCoreTransitionProjection:
    binding = payload.compensation_target
    if binding is None:
        raise ValueError("character core compensation target is missing")
    target = next((item for item in history if item.transition_id == binding.transition_id), None)
    committed = next((item for item in committed_events if item.event_id == binding.accepted_event_ref), None)
    if (
        target is None
        or not history
        or history[-1] != target
        or target.operation == "initialize"
        or target.entity_revision != binding.entity_revision
        or target.accepted_event_ref != binding.accepted_event_ref
        or committed is None
        or committed.world_revision != binding.accepted_world_revision
        or committed.payload_hash != binding.accepted_payload_hash
    ):
        raise ValueError("character core compensation target is not exact latest authority")
    return target


def _effective_compensation_authority_lane(
    target: CharacterCoreTransitionProjection,
    history: tuple[CharacterCoreTransitionProjection, ...],
) -> str:
    by_id = {item.transition_id: item for item in history}
    current = target
    visited: set[str] = set()
    while current.authority_lane == "compensation":
        if current.transition_id in visited:
            raise ValueError("character core compensation authority lineage contains a cycle")
        visited.add(current.transition_id)
        parent_id = current.compensates_transition_id
        if parent_id is None or parent_id not in by_id:
            raise ValueError("character core compensation authority lineage is incomplete")
        current = by_id[parent_id]
    return current.authority_lane


def _canonical_hash(value: object) -> str:
    material = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return hashlib.sha256(
        json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
