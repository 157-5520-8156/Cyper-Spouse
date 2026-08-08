from __future__ import annotations

import json

from companion_daemon.world_v2.character_interior import InnerLifeSnapshot
from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
    compact_model_facing_context,
    compact_recovery_model_facing_context,
)


_FACULTIES = {
    "private_self",
    "selective_memory",
    "appraisal_affect",
    "emotional_continuity",
    "subjective_relationship",
    "aspirations_conflicts",
    "autonomous_impulses",
    "expression_stance",
}


def _context() -> str:
    return json.dumps(
        {
            "world_id": "world:inner-context",
            "actor_ref": "agent:companion",
            "trigger_ref": "observation:latest",
            "world_revision": 12,
            "deliberation_revision": 7,
            "ledger_sequence": 31,
            "logical_time": "2026-08-04T12:00:00+08:00",
            "slices": {
                "character_core": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "character-core:stable",
                            "value": {"values": {"slow_evolving": {"axes": []}}},
                        }
                    ],
                },
                "current_situation": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "situation:now",
                            "value": {
                                "time_segment": "noon",
                                "activity_slices": [{"activity_kind": "reading"}],
                                "goal_slices": [{"goal_ref": "goal:finish-book"}],
                            },
                        }
                    ],
                },
                "relationship_slice": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "relationship:user",
                            "value": {
                                "subject_ref": "user:primary",
                                "stage": "friend",
                                "variables": {"trust_bp": 7200},
                            },
                        }
                    ],
                },
                "appraisals": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "appraisal:message",
                            "value": {
                                "subject_ref": "user:primary",
                                "confidence_bp": 7600,
                            },
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:warmth",
                            "value": {
                                "components": [
                                    {
                                        "dimension": "warmth",
                                        "intensity_bp": 4100,
                                        "residue_bp": 900,
                                    }
                                ]
                            },
                        }
                    ],
                },
                "active_memory_candidates": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "memory:walk",
                            "value": {"summary": "a rainy walk"},
                        }
                    ],
                },
                "open_threads": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "thread:book",
                            "value": {"kind": "shared_interest", "status": "open"},
                        }
                    ],
                },
            },
        }
    )


def _compiled_snapshot(raw: str) -> dict[str, object]:
    return compile_inner_life_snapshot(json.loads(raw)).model_view()


def test_generic_model_views_do_not_mint_a_second_inner_life_snapshot() -> None:
    for compile_view in (
        compact_model_facing_context,
        compact_chat_model_facing_context,
        compact_recovery_model_facing_context,
    ):
        context = json.loads(compile_view(_context()))

        assert "current_self_state" not in context
        assert "inner_life_snapshot" not in context

    snapshot = _compiled_snapshot(_context())
    assert snapshot["contract"] == "inner-life-snapshot.1"
    assert set(snapshot["faculties"]) == _FACULTIES
    assert snapshot["snapshot_id"] == (
        "inner-life-snapshot:sha256:" + snapshot["snapshot_hash"]
    )
    assert snapshot["cursor"] == {
        "world_revision": 12,
        "deliberation_revision": 7,
        "ledger_sequence": 31,
        "logical_time": "2026-08-04T12:00:00+08:00",
    }
    assert snapshot["world_id"] == "world:inner-context"
    assert snapshot["actor_ref"] == "agent:companion"
    assert "situation:now" in snapshot["source_refs"]
    assert "relationship:user" in snapshot["source_refs"]


def test_faculties_reference_the_same_source_bound_material_without_behavior_verdicts() -> None:
    snapshot = _compiled_snapshot(_context())

    assert snapshot["faculties"]["selective_memory"]["material_keys"] == [
        "remembered_material"
    ]
    assert snapshot["faculties"]["appraisal_affect"]["material_keys"] == [
        "appraisals",
        "affect",
    ]
    assert snapshot["faculties"]["subjective_relationship"]["material_keys"] == [
        "relationship"
    ]
    serialized = json.dumps(snapshot, ensure_ascii=False)
    for forbidden in ("should_reply", "must_ask", "act_or_hold", "required_stance"):
        assert forbidden not in serialized


def test_emotional_continuity_keeps_episode_decay_lineage_and_change_phase() -> None:
    raw = json.loads(_context())
    raw["slices"]["affect_episodes"]["items"][0]["value"] = {
        "episode_id": "affect-episode:warmth",
        "entity_revision": 4,
        "status": "active",
        "opened_at": "2026-08-04T09:00:00+08:00",
        "updated_at": "2026-08-04T11:30:00+08:00",
        "expression_history_refs": ["expression-plan:morning"],
        "supersedes_episode_id": "affect-episode:guarded",
        "components": [
            {
                "component_id": "affect-component:warmth",
                "dimension": "warmth",
                "source_cluster_ref": "appraisal-cluster:message",
                "appraisal_refs": [
                    {"appraisal_id": "appraisal:message", "meaning_id": "shared_joy"}
                ],
                "intensity_bp": 4100,
                "decay_anchor_intensity_bp": 5200,
                "residue_bp": 900,
                "opened_at": "2026-08-04T09:00:00+08:00",
                "decay_anchor_at": "2026-08-04T10:00:00+08:00",
                "decay_not_before": "2026-08-04T10:30:00+08:00",
                "last_stimulus_at": "2026-08-04T11:00:00+08:00",
                "last_updated_at": "2026-08-04T11:30:00+08:00",
                "decay_profile": {
                    "kind": "exponential_half_life",
                    "half_life_seconds": 7200,
                    "floor_bp": 600,
                    "delay_seconds": 1800,
                    "config_version": "affect-decay.3",
                    "algorithm_version": "affect-decay-exp2-q48-binary-rhe-v1",
                    "table_digest": "a" * 64,
                    "rounding_mode": "round-half-even",
                    "config_digest": "b" * 64,
                },
            }
        ],
    }
    raw["slices"]["advisories"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "advisory:change-phase:warmth",
                "value": {
                    "kind": "change_phase",
                    "candidate_refs": ["change-phase:warmth:rising"],
                    "candidates": [
                        {
                            "candidate_ref": "change-phase:warmth:rising",
                            "value": "暖意还在上升",
                            "weight_bp": 10000,
                            "confidence_bp": 10000,
                        }
                    ],
                    "confidence_bp": 6000,
                    "expiry": "2026-08-04T18:00:00+08:00",
                    "producer_version": "change-phase-view.1",
                },
            }
        ],
    }

    snapshot = compile_inner_life_snapshot(raw).model_view()
    episode = snapshot["materials"]["affect"][0]
    component = episode["components"][0]

    assert episode["episode_id"] == "affect-episode:warmth"
    assert episode["entity_revision"] == 4
    assert episode["status"] == "active"
    assert episode["expression_history_refs"] == ["expression-plan:morning"]
    assert episode["supersedes_episode_id"] == "affect-episode:guarded"
    assert component["component_id"] == "affect-component:warmth"
    assert component["decay_anchor_intensity_bp"] == 5200
    assert component["decay_anchor_at"] == "2026-08-04T10:00:00+08:00"
    assert component["decay_profile"]["half_life_seconds"] == 7200
    assert snapshot["materials"]["change_phase"][0]["kind"] == "change_phase"
    assert snapshot["faculties"]["emotional_continuity"]["material_keys"] == [
        "appraisals",
        "affect",
        "change_phase",
    ]


def test_recent_dialogue_is_source_bound_into_the_one_inner_life_snapshot() -> None:
    raw = json.loads(_context())
    raw["slices"]["recent_dialogue"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "dialogue:observation:message:earlier",
                "privacy_class": "private",
                "value": {
                    "dialogue_id": "dialogue:observation:message:earlier",
                    "speaker": "counterpart",
                    "speaker_ref": "user:primary",
                    "text": "我下午再告诉你后续。",
                    "occurred_at": "2026-08-04T08:00:00+08:00",
                    "delivery_state": "observed",
                    "sequence": 1100,
                },
            },
            {
                "item_ref": "dialogue:observation:message:latest",
                "privacy_class": "private",
                "value": {
                    "dialogue_id": "dialogue:observation:message:latest",
                    "speaker": "counterpart",
                    "speaker_ref": "user:primary",
                    "text": "我先去忙一会儿。",
                    "occurred_at": "2026-08-04T11:59:00+08:00",
                    "delivery_state": "observed",
                    "sequence": 1200,
                },
            },
        ],
    }
    raw["slices"]["relevant_facts"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "fact:user:afternoon-plan",
                "privacy_class": "private",
                "value": {
                    "predicate_code": "user.plan.afternoon",
                    "semantic_value": "下午会继续说这件事",
                    "status": "active",
                },
            }
        ],
    }

    snapshot = compile_inner_life_snapshot(raw).model_view()

    dialogue = snapshot["materials"]["recent_dialogue"]
    assert [item["text"] for item in dialogue] == [
        "我下午再告诉你后续。",
        "我先去忙一会儿。",
    ]
    assert [item["source_ref"] for item in dialogue] == [
        "dialogue:observation:message:earlier",
        "dialogue:observation:message:latest",
    ]
    assert "recent_dialogue" in snapshot["faculties"]["selective_memory"][
        "material_keys"
    ]
    assert snapshot["materials"]["relevant_facts"][0]["semantic_value"] == (
        "下午会继续说这件事"
    )
    assert "relevant_facts" in snapshot["faculties"]["selective_memory"][
        "material_keys"
    ]
    assert "recent_dialogue" in snapshot["faculties"]["expression_stance"][
        "material_keys"
    ]
    assert "dialogue:observation:message:latest" in snapshot["source_refs"]


def test_snapshot_hash_binds_material_content_even_when_source_ref_is_unchanged() -> None:
    original = json.loads(_context())
    changed = json.loads(_context())
    changed["slices"]["current_situation"]["items"][0]["value"]["activity_slices"] = [
        {"activity_kind": "walking"}
    ]

    first = compile_inner_life_snapshot(original).model_view()
    second = compile_inner_life_snapshot(changed).model_view()

    assert first["source_refs"] == second["source_refs"]
    assert first["snapshot_hash"] != second["snapshot_hash"]


def test_all_provider_views_must_redact_the_one_typed_snapshot_without_minting_identity() -> None:
    raw = json.loads(_context())
    raw["consumer_scope"] = "deliberation_internal"
    raw["context_compiler_version"] = "context-capsule-compiler.1"
    raw["truncation"] = {
        "availability": "available",
        "truncated_slices": ["recent_dialogue"],
    }
    raw["slices"]["available_capabilities"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "capability:send-text",
                "privacy_class": "private",
                "value": {"kind": "send_text", "enabled": True},
            }
        ],
    }

    typed = compile_inner_life_snapshot(raw)
    all_refs = frozenset(typed.source_refs)
    general = typed.model_view(visible_source_refs=all_refs)
    chat = typed.model_view(
        visible_source_refs=all_refs - {"capability:send-text"}
    )
    recovery = typed.model_view(
        visible_source_refs=frozenset(
            ref for ref in all_refs if ref != "memory:walk"
        )
    )

    for compile_view in (
        compact_model_facing_context,
        compact_chat_model_facing_context,
        compact_recovery_model_facing_context,
    ):
        assert "inner_life_snapshot" not in json.loads(
            compile_view(json.dumps(raw))
        )

    assert isinstance(typed, InnerLifeSnapshot)
    assert {general["snapshot_id"], chat["snapshot_id"], recovery["snapshot_id"]} == {
        typed.snapshot_id
    }
    assert {
        general["snapshot_hash"],
        chat["snapshot_hash"],
        recovery["snapshot_hash"],
    } == {typed.snapshot_hash}
    assert general["cursor"] == {
        "world_revision": 12,
        "deliberation_revision": 7,
        "ledger_sequence": 31,
        "logical_time": "2026-08-04T12:00:00+08:00",
    }
    assert general["viewer_scope"]["value"] == "deliberation_internal"
    assert general["privacy_scope"] == {"availability": "unavailable"}
    assert general["capability_scope"]["availability"] == "available"
    assert general["context_compiler"]["value"] == "context-capsule-compiler.1"
    assert general["snapshot_compiler"]["value"].startswith(
        "inner-life-snapshot-compiler."
    )
    assert general["truncation"]["value"]["truncated_slices"] == [
        "recent_dialogue"
    ]
    assert len(general["faculties"]) == 8
    assert all(
        item["availability"] in {"available", "unavailable"}
        for item in general["faculties"].values()
    )


def test_source_inventory_binds_content_and_privacy_without_trusting_ref_identity() -> None:
    original = json.loads(_context())
    changed = json.loads(_context())
    for value in (original, changed):
        value["slices"]["current_situation"]["items"][0]["privacy_class"] = (
            "personal"
        )
    changed["slices"]["current_situation"]["items"][0]["value"][
        "time_segment"
    ] = "afternoon"

    first = compile_inner_life_snapshot(original).model_view()
    second = compile_inner_life_snapshot(changed).model_view()
    first_item = next(
        item
        for item in first["source_inventory"]
        if item["source_ref"] == "situation:now"
    )
    second_item = next(
        item
        for item in second["source_inventory"]
        if item["source_ref"] == "situation:now"
    )

    assert first_item["privacy_class"] == "personal"
    assert first_item["content_hash"] != second_item["content_hash"]
    assert first["snapshot_hash"] != second["snapshot_hash"]


def test_source_inventory_keeps_authority_and_temporal_validity() -> None:
    raw = json.loads(_context())
    raw["slices"]["relevant_facts"] = {
        "availability": "available",
        "items": [
            {
                "item_ref": "fact:user:temporary-plan",
                "privacy_class": "personal",
                "value": {
                    "semantic_value": "下午可能继续这件事",
                    "authority": "counterpart_report",
                    "valid_from": "2026-08-04T08:00:00+08:00",
                    "valid_to": "2026-08-04T18:00:00+08:00",
                    "expires_at": "2026-08-04T18:00:00+08:00",
                },
            }
        ],
    }

    snapshot = compile_inner_life_snapshot(raw).model_view()
    inventory = next(
        item
        for item in snapshot["source_inventory"]
        if item["source_ref"] == "fact:user:temporary-plan"
    )

    assert inventory == {
        "source_ref": "fact:user:temporary-plan",
        "scope": "relevant_facts",
        "content_hash": inventory["content_hash"],
        "privacy_class": "personal",
        "authority_scope": "counterpart_report",
        "valid_from": "2026-08-04T08:00:00+08:00",
        "valid_to": "2026-08-04T18:00:00+08:00",
        "expires_at": "2026-08-04T18:00:00+08:00",
    }


def test_source_inventory_hashes_full_authority_and_exposes_compact_lifecycle_lineage() -> None:
    raw = json.loads(_context())
    situation = raw["slices"]["current_situation"]["items"][0]
    situation["privacy_class"] = "private"
    situation["source_bindings"] = [
        {
            "source_kind": "committed_event",
            "authority_type": "SituationProjection",
            "ref": "event:situation:accepted:4",
            "source_world_revision": 12,
            "immutable_hash": "a" * 64,
        }
    ]
    situation["value"].update(
        {
            "entity_revision": 4,
            "source_event_ref": "event:situation:accepted:4",
            "predecessor_refs": ["event:situation:accepted:3"],
            "contradiction_refs": ["event:situation:counterevidence:1"],
            "revision_event_ref": "event:situation:accepted:4",
            "valid_from": "2026-08-04T08:00:00+08:00",
            "expires_at": "2026-08-04T18:00:00+08:00",
        }
    )

    typed = compile_inner_life_snapshot(raw)
    view = typed.model_view()
    inventory = next(
        item
        for item in view["source_inventory"]
        if item["source_ref"] == "situation:now"
    )

    assert inventory["authority_refs"] == ["event:situation:accepted:4"]
    assert inventory["direct_source_refs"] == ["event:situation:accepted:4"]
    assert inventory["entity_revision"] == 4
    assert inventory["predecessor_refs"] == ["event:situation:accepted:3"]
    assert inventory["conflict_refs"] == ["event:situation:counterevidence:1"]
    assert inventory["revision_refs"] == ["event:situation:accepted:4"]
    assert inventory["valid_from"] == "2026-08-04T08:00:00+08:00"
    assert inventory["expires_at"] == "2026-08-04T18:00:00+08:00"
    assert "immutable_hash" not in json.dumps(inventory)

    changed = json.loads(json.dumps(raw))
    changed["slices"]["current_situation"]["items"][0]["source_bindings"][0][
        "immutable_hash"
    ] = "b" * 64
    assert compile_inner_life_snapshot(changed).snapshot_hash != typed.snapshot_hash


def test_unknown_capsule_scopes_are_explicitly_unavailable() -> None:
    snapshot = _compiled_snapshot(_context())

    assert snapshot["viewer_scope"]["availability"] == "unavailable"
    assert snapshot["privacy_scope"]["availability"] == "unavailable"
    assert snapshot["context_compiler"]["availability"] == "unavailable"
    assert snapshot["truncation"]["availability"] == "unavailable"
