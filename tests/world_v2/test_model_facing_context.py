from __future__ import annotations

import json

from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
    compact_model_facing_context,
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


def test_chat_view_derives_a_source_bound_current_self_state_without_flattening_affect() -> None:
    raw = json.dumps(
        {
            "world_id": "world:self-state",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:current",
            "world_revision": 15,
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "character_core": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "character-core:1",
                            "value": {"values": {"slow_evolving": {"axes": []}}},
                        }
                    ],
                },
                "current_situation": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "situation:current",
                            "value": {
                                "time_segment": "morning",
                                "activity_slices": [{"activity_kind": "study.reading"}],
                                "attention_slice": {"availability": "available"},
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
                                "stage": "close_friend",
                                "temperature": "strained",
                                "variables": {"trust_bp": 7800, "closeness_bp": 8500},
                            },
                        }
                    ],
                },
                "appraisals": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "appraisal:current",
                            "value": {
                                "subject_ref": "user:primary",
                                "source_cluster_ref": "cluster:repair",
                                "confidence_bp": 8600,
                                "expires_at": "2026-07-29T08:00:00+08:00",
                                "hypotheses": [
                                    {
                                        "meaning": "dismissal",
                                        "attribution": "user",
                                        "severity": "moderate",
                                        "weight_bp": 10000,
                                    }
                                ],
                            },
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:mixed",
                            "value": {
                                "opened_at": "2026-07-28T07:30:00+08:00",
                                "updated_at": "2026-07-28T07:50:00+08:00",
                                "components": [
                                    {
                                        "dimension": "hurt",
                                        "source_cluster_ref": "cluster:repair",
                                        "appraisal_refs": [
                                            {
                                                "appraisal_id": "appraisal:current",
                                                "hypothesis_id": "hypothesis:dismissal",
                                            }
                                        ],
                                        "intensity_bp": 6200,
                                        "residue_bp": 1800,
                                    },
                                    {
                                        "dimension": "warmth",
                                        "source_cluster_ref": "cluster:warmth",
                                        "appraisal_refs": [
                                            {
                                                "appraisal_id": "appraisal:warmth",
                                                "hypothesis_id": "hypothesis:warmth",
                                            }
                                        ],
                                        "intensity_bp": 4300,
                                        "residue_bp": 1200,
                                    },
                                ],
                            },
                        }
                    ],
                },
                "open_threads": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "thread:repair",
                            "value": {
                                "kind": "repair_open",
                                "importance_bp": 9000,
                                "status": "open",
                            },
                        }
                    ],
                },
                "advisories": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "advisory:attention",
                            "value": {
                                "kind": "attention_candidate",
                                "candidate_refs": ["thread:repair"],
                                "candidates": [
                                    {
                                        "candidate_ref": "thread:repair",
                                        "value": "continue_if_she_wants",
                                        "weight_bp": 7200,
                                        "confidence_bp": 8100,
                                    }
                                ],
                            },
                        },
                        {
                            "item_ref": "advisory:interruption",
                            "value": {
                                "kind": "interruption.cost",
                                "candidate_refs": ["cost:low"],
                                "candidates": [
                                    {
                                        "candidate_ref": "cost:low",
                                        "value": "low",
                                        "weight_bp": 8400,
                                        "confidence_bp": 7900,
                                    }
                                ],
                            },
                        }
                    ],
                },
            },
        }
    )

    general = json.loads(compact_model_facing_context(raw))
    assert general["current_self_state"]["affect"][0]["source_ref"] == "affect:mixed"

    compact = json.loads(compact_chat_model_facing_context(raw))
    current = compact["current_self_state"]

    assert current["contract"] == "current-self-state.1"
    assert current["authority"] == "derived_from_verified_context"
    assert current["logical_time"] == "2026-07-28T08:00:00+08:00"
    assert current["relationship"][0]["source_ref"] == "relationship:user"
    assert current["appraisals"][0]["source_ref"] == "appraisal:current"
    assert current["appraisals"][0]["source_cluster_ref"] == "cluster:repair"
    assert current["affect"][0]["source_ref"] == "affect:mixed"
    assert [item["dimension"] for item in current["affect"][0]["components"]] == [
        "hurt",
        "warmth",
    ]
    assert current["affect"][0]["components"][0]["source_cluster_ref"] == "cluster:repair"
    assert current["affect"][0]["components"][0]["appraisal_refs"][0][
        "appraisal_id"
    ] == "appraisal:current"
    assert current["unresolved"][0]["source_ref"] == "thread:repair"
    assert current["advisories"][0]["source_ref"] == "advisory:attention"
    assert current["advisories"][0]["candidates"][0]["value"] == "continue_if_she_wants"
    assert current["interruption"][0]["source_ref"] == "advisory:interruption"
    assert current["interruption"][0]["candidates"][0]["value"] == "low"

    recovery = json.loads(compact_recovery_model_facing_context(raw))
    assert "advisories" not in recovery["current_self_state"]


def test_current_self_state_omits_entries_without_a_capsule_source_token() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "relationship_slice": {
                    "availability": "available",
                    "items": [
                        {
                            "value": {
                                "subject_ref": "user:primary",
                                "stage": "friend",
                            }
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "value": {
                                "components": [
                                    {"dimension": "warmth", "intensity_bp": 5000}
                                ]
                            }
                        }
                    ],
                },
            },
        }
    )

    current = json.loads(compact_chat_model_facing_context(raw))["current_self_state"]

    assert "relationship" not in current
    assert "affect" not in current


def test_recalled_dialogue_supplements_without_evicting_latest_working_turns() -> None:
    ordinary = [
        {
            "item_ref": f"dialogue:{index}",
            "privacy_class": "private",
            "value": {
                "speaker": "counterpart",
                "text": f"current {index}",
                "sequence": index,
            },
        }
        for index in range(8)
    ]
    recalled = {
        "item_ref": "dialogue:remembered",
        "privacy_class": "private",
        "recall_injected": True,
        "value": {
            "speaker": "counterpart",
            "text": "older remembered turn",
            "sequence": 1,
        },
    }
    raw = json.dumps(
        {
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00Z",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "items": [recalled, *ordinary],
                }
            },
        }
    )

    compact = json.loads(compact_chat_model_facing_context(raw))
    texts = [
        item["value"]["text"] for item in compact["slices"]["recent_dialogue"]["items"]
    ]

    assert texts == [
        "current 2",
        "current 3",
        "current 4",
        "current 5",
        "current 6",
        "current 7",
        "older remembered turn",
    ]


def test_recalled_facts_supplement_capsule_ranked_facts() -> None:
    ordinary = [
        {
            "item_ref": f"fact:ordinary:{index}",
            "privacy_class": "personal",
            "value": {
                "predicate_code": "preference.likes",
                "semantic_value": f"ordinary {index}",
            },
        }
        for index in range(6)
    ]
    recalled = [
        {
            "item_ref": f"fact:recalled:{index}",
            "privacy_class": "personal",
            "recall_injected": True,
            "value": {
                "predicate_code": "profile.note",
                "semantic_value": f"recalled {index}",
            },
        }
        for index in range(2)
    ]
    raw = json.dumps(
        {
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00Z",
            "slices": {
                "relevant_facts": {
                    "availability": "available",
                    "items": [*recalled, *ordinary],
                }
            },
        }
    )

    compact = json.loads(compact_chat_model_facing_context(raw))
    values = [
        item["value"]["semantic_value"]
        for item in compact["slices"]["relevant_facts"]["items"]
    ]

    assert values == [
        "recalled 0",
        "recalled 1",
        "ordinary 0",
        "ordinary 1",
        "ordinary 2",
        "ordinary 3",
        "ordinary 4",
        "ordinary 5",
    ]


def test_every_audited_injected_hit_survives_small_lane_compaction() -> None:
    injected = [
        {
            "item_ref": f"impression:recalled:{index}",
            "privacy_class": "withhold",
            "recall_injected": True,
            "value": {"reflection_summary": f"reflection {index}"},
        }
        for index in range(6)
    ]
    ordinary = [
        {
            "item_ref": f"impression:ordinary:{index}",
            "privacy_class": "withhold",
            "value": {"reflection_summary": f"ordinary {index}"},
        }
        for index in range(2)
    ]
    raw = json.dumps(
        {
            "world_revision": 9,
            "logical_time": "2026-07-17T00:00:00Z",
            "slices": {
                "private_impressions": {
                    "availability": "available",
                    "items": [*injected, *ordinary],
                }
            },
        }
    )

    compact = json.loads(compact_chat_model_facing_context(raw))
    summaries = [
        item["value"]["reflection_summary"]
        for item in compact["slices"]["private_impressions"]["items"]
    ]

    assert summaries == [
        "reflection 0",
        "reflection 1",
        "reflection 2",
        "reflection 3",
        "reflection 4",
        "reflection 5",
        "ordinary 0",
        "ordinary 1",
    ]


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
