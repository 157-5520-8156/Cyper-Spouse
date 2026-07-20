from __future__ import annotations

import json

from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
    compact_recovery_model_facing_context,
    mechanism_consumption_summary,
)


def test_chat_view_keeps_semantics_but_omits_authority_and_accounting_noise() -> None:
    dialogue = [
        {
            "item_ref": f"dialogue:{index}",
            "privacy_class": "private",
            "value": {"speaker": "counterpart", "text": f"message {index}"},
        }
        for index in range(12)
    ]
    raw = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:current",
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00Z",
            "slices": {
                "recent_dialogue": {"availability": "available", "items": dialogue},
                "current_situation": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "situation:current",
                            "value": {
                                "time_segment": "late_night",
                                "activity_slices": [],
                                "authority_snapshot_hash": "a" * 64,
                                "policy_versions": ["situation.16"],
                                "source_revisions": [{"event_ref": "event:proof"}],
                            },
                        }
                    ],
                },
                "relevant_facts": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "fact:user:name",
                            "value": {
                                "predicate_code": "profile.display_name",
                                "semantic_value": "Geoff",
                                "value_hash": "b" * 64,
                                "origin": {"accepted_event_ref": "event:fact"},
                            },
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:hurt",
                            "value": {"components": [{"dimension": "hurt", "value": 6200}]},
                        }
                    ],
                },
                "advisories": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "advisory:current",
                            "value": {"field_id": "user_affect.signal", "value": "disappointed"},
                        }
                    ],
                },
                "world_life": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "occurrence:tea",
                            "value": {"summary": "刚泡了一杯茶", "result_payload_hash": "c" * 64},
                        }
                    ],
                },
                "available_capabilities": {
                    "availability": "available",
                    "items": [{"item_ref": "cap:send", "value": {"kind": "send"}}],
                },
                "action_budget": {
                    "availability": "available",
                    "items": [{"item_ref": "budget:chat", "value": {"remaining": 99}}],
                },
                "open_threads": {"availability": "unavailable"},
            },
            "relationship_evaluation": {
                "subject_ref": "user:geoff",
                "appraisal_summary_json": "{}",
                "relationship_summary_json": "{\"stage\":\"stranger\"}",
            },
        }
    )

    compact = json.loads(compact_chat_model_facing_context(raw))

    assert set(compact["slices"]) == {
        "recent_dialogue",
        "current_situation",
        "relevant_facts",
        "affect_episodes",
        "advisories",
        "world_life",
    }
    assert [
        item["value"]["text"] for item in compact["slices"]["recent_dialogue"]["items"]
    ] == [f"message {index}" for index in range(6, 12)]
    situation = compact["slices"]["current_situation"]["items"][0]["value"]
    assert situation == {"activity_slices": [], "time_segment": "late_night"}
    fact = compact["slices"]["relevant_facts"]["items"][0]
    assert fact["source_ref"] == "fact:user:name"
    assert fact["value"] == {
        "predicate_code": "profile.display_name",
        "semantic_value": "Geoff",
    }
    assert compact["slices"]["advisories"]["items"][0]["value"]["value"] == "disappointed"
    assert compact["slices"]["world_life"]["items"][0]["value"] == {
        "summary": "刚泡了一杯茶"
    }
    assert compact["relationship_evaluation"]["subject_ref"] == "user:geoff"

    recovery = json.loads(compact_recovery_model_facing_context(raw))
    assert "advisories" not in recovery["slices"]
    assert "affect_episodes" in recovery["slices"]
    assert recovery["slices"]["relevant_facts"]["items"][0]["source_ref"] == "fact:user:name"
    assert recovery["slices"]["recent_dialogue"]["items"][-1]["value"]["text"] == "message 11"
    assert len(json.dumps(recovery, ensure_ascii=False)) < len(raw)


def test_mechanism_consumption_summary_reports_available_lanes_without_values() -> None:
    raw = json.dumps(
        {
            "world_revision": 12,
            "logical_time": "2026-07-18T00:00:00Z",
            "slices": {
                "current_situation": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "situation:current",
                            "value": {"activity_slices": [{"activity_kind": "study"}]},
                            "source_bindings": [{"ref": "event:activity"}],
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [{"item_ref": "affect:1", "value": {}}],
                },
                "relevant_facts": {"availability": "unavailable"},
            },
        }
    )

    summary = mechanism_consumption_summary(raw)

    assert summary["status"] == "ok"
    slices = summary["slices"]
    assert slices["current_situation"] == {
        "availability": "available",
        "item_count": 1,
        "source_ref_count": 2,
        "activity_count": 1,
    }
    assert slices["affect_episodes"]["item_count"] == 1
    assert slices["relevant_facts"] == {
        "availability": "unavailable",
        "item_count": 0,
        "source_ref_count": 0,
    }
