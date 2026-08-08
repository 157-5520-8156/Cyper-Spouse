from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
    compact_model_facing_context,
    compact_recovery_model_facing_context,
    mechanism_consumption_summary,
)


def _inner_life_view(
    raw: str,
    *,
    compactor=compact_chat_model_facing_context,
) -> tuple[dict[str, object], dict[str, object]]:
    """Tests compile the canonical view explicitly through its owning Module.

    Generic provider compaction must never mint a second current self.  The
    production CharacterInterior projection performs this compilation before
    a purpose Faculty receives the resulting model view.
    """

    compact = json.loads(compactor(raw))
    assert "inner_life_snapshot" not in compact
    snapshot = compile_inner_life_snapshot(compact).model_view()
    return compact, snapshot


def test_chat_compaction_preserves_interior_view_and_consumed_recall_budget() -> None:
    compact = json.loads(
        compact_chat_model_facing_context(
            json.dumps(
                {
                    "world_revision": 7,
                    "logical_time": "2026-08-04T12:00:00+08:00",
                    "slices": {},
                    "inner_life_snapshot": {
                        "contract": "inner-life-snapshot.1",
                        "snapshot_hash": "a" * 64,
                        "materials": {
                            "selected_recall": {
                                "items": [
                                    {
                                        "source_ref": "memory:one",
                                        "text": "remembered material",
                                    }
                                ]
                            }
                        },
                        "source_refs": ["memory:one"],
                    },
                    "recall_control": {"remaining_character_pulls": 0},
                },
                ensure_ascii=False,
            )
        )
    )

    assert compact["inner_life_snapshot"] == {
        "contract": "inner-life-snapshot.1",
        "materials": {
            "selected_recall": {
                "items": [
                    {
                        "source_ref": "memory:one",
                        "text": "remembered material",
                    }
                ]
            }
        },
        "source_refs": ["memory:one"],
    }
    assert compact["recall_control"] == {"remaining_character_pulls": 0}


def test_chat_view_pins_authoritative_time_as_a_copyable_replayable_source() -> None:
    raw_context = {
        "world_id": "world:time-source",
        "actor_ref": "agent:companion",
        "trigger_ref": "event:current",
        "world_revision": 23,
        "logical_time": "2026-07-30T01:12:00+08:00",
        # Neither a caller-supplied top-level token nor a same-named slice is
        # authority.  Both must be rebuilt from the pinned context coordinates.
        "pinned_time": {
            "contract": "pinned-time-context.1",
            "logical_time": "2026-07-30T08:00:00+08:00",
            "time_segment": "morning",
            "source_ref": "forged:time",
        },
        "slices": {
            "pinned_time": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "forged:time",
                        "value": {
                            "logical_time": "2026-07-30T08:00:00+08:00",
                            "time_segment": "morning",
                        },
                    }
                ],
            },
            "current_situation": {
                "availability": "available",
                "items": [
                    {
                        "item_ref": "situation:late-night",
                        "value": {
                            "logical_time": "2026-07-30T01:12:00+08:00",
                            "time_segment": "late_night",
                        },
                    }
                ],
            },
        },
    }

    first = json.loads(compact_chat_model_facing_context(json.dumps(raw_context)))
    replay = json.loads(compact_chat_model_facing_context(json.dumps(raw_context)))
    changed_context = json.loads(json.dumps(raw_context))
    changed_context["slices"]["current_situation"]["items"][0]["value"][
        "time_segment"
    ] = "early_morning"
    changed = json.loads(
        compact_chat_model_facing_context(json.dumps(changed_context))
    )

    pinned_time = first["pinned_time"]
    assert pinned_time == {
        "authority": "derived_from_verified_context",
        "contract": "pinned-time-context.1",
        "logical_time": "2026-07-30T01:12:00+08:00",
        "local_logical_time": "2026-07-30T01:12:00+08:00",
        "source_ref": pinned_time["source_ref"],
        "time_segment": "late_night",
    }
    assert pinned_time["source_ref"].startswith("pinned-time:sha256:")
    assert pinned_time["source_ref"] != "forged:time"
    assert replay["pinned_time"]["source_ref"] == pinned_time["source_ref"]
    assert changed["pinned_time"]["source_ref"] != pinned_time["source_ref"]
    assert first["slices"]["pinned_time"] == {
        "availability": "available",
        "items": [
            {
                "attention_source_refs": [pinned_time["source_ref"]],
                "source_ref": pinned_time["source_ref"],
                "value": {
                    "authority": "derived_from_verified_context",
                    "contract": "pinned-time-context.1",
                    "logical_time": "2026-07-30T01:12:00+08:00",
                    "local_logical_time": "2026-07-30T01:12:00+08:00",
                    "time_segment": "late_night",
                },
            }
        ],
    }


@pytest.mark.parametrize("dimension", ("anger", "warmth"))
def test_raw_pinned_environment_does_not_translate_affect_into_reply_behavior(
    dimension: str,
) -> None:
    raw = json.dumps(
        {
            "world_id": "world:raw-attention-environment",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:current",
            "world_revision": 9,
            "logical_time": "2026-07-30T01:12:00+08:00",
            "slices": {
                "current_situation": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "situation:current",
                            "value": {
                                "logical_time": "2026-07-30T01:12:00+08:00",
                                "time_segment": "late_night",
                                "activity_slices": [],
                                "attention_slice": {
                                    "availability": "unavailable",
                                    "reason": "no_authority",
                                },
                            },
                        }
                    ],
                },
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect:current",
                            "value": {
                                "components": [
                                    {
                                        "dimension": dimension,
                                        "source_cluster_ref": "cluster:current",
                                        "intensity_bp": 7_000,
                                    }
                                ]
                            },
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )

    compact, snapshot = _inner_life_view(raw)

    assert compact["pinned_time"]["time_segment"] == "late_night"
    assert compact["pinned_time"]["source_ref"].startswith("pinned-time:sha256:")
    assert compact["slices"]["current_situation"]["items"][0]["source_ref"] == (
        "situation:current"
    )
    assert compact["slices"]["affect_episodes"]["items"][0]["source_ref"] == (
        "affect:current"
    )
    materials = snapshot["materials"]
    assert materials["situation"][0]["activity_slices"] == []
    assert materials["situation"][0]["attention_slice"] == {
        "availability": "unavailable",
        "reason": "no_authority",
    }
    assert materials["affect"][0]["components"][0] == {
        "dimension": dimension,
        "source_cluster_ref": "cluster:current",
        "intensity_bp": 7_000,
    }
    serialized = json.dumps(compact, ensure_ascii=False)
    for host_authored_conclusion in (
        "phone_attention",
        "attention-view.",
        "withdrawal_affect",
        "do_not_disturb",
        "reply_timing",
        "手机扣着",
        "看到通知也可能先放着",
        "多半要忙完这一段才会点开看",
    ):
        assert host_authored_conclusion not in serialized


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
        "pinned_time",
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
    assert len(json.dumps(recovery, ensure_ascii=False)) < len(raw) * 1.5


def test_chat_view_uses_causal_sequence_before_coarse_dialogue_timestamps() -> None:
    same_second = "2026-07-30T13:07:05Z"
    dialogue = [
        {
            "item_ref": item_ref,
            "value": {
                "speaker": speaker,
                "text": text,
                "occurred_at": occurred_at,
                "sequence": sequence,
            },
        }
        for item_ref, speaker, text, occurred_at, sequence in (
            (
                "dialogue:user:2",
                "counterpart",
                "第二句用户消息",
                same_second,
                300,
            ),
            (
                "dialogue:user:1",
                "counterpart",
                "第一句用户消息",
                same_second,
                100,
            ),
            (
                "dialogue:companion:2",
                "companion",
                "第二句角色回复",
                "2026-07-30T13:07:05.840000Z",
                401,
            ),
            (
                "dialogue:companion:1",
                "companion",
                "第一句角色回复",
                "2026-07-30T13:07:05.280000Z",
                201,
            ),
        )
    ]
    raw = json.dumps(
        {
            "world_id": "world:burst-dialogue",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:user:2",
            "world_revision": 5,
            "logical_time": same_second,
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "items": dialogue,
                }
            },
        }
    )

    compact = json.loads(compact_chat_model_facing_context(raw))

    assert [
        item["value"]["text"]
        for item in compact["slices"]["recent_dialogue"]["items"]
    ] == [
        "第一句用户消息",
        "第一句角色回复",
        "第二句用户消息",
        "第二句角色回复",
    ]


def test_chat_view_derives_a_source_bound_inner_life_snapshot_without_flattening_affect() -> None:
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

    _general, general_snapshot = _inner_life_view(
        raw,
        compactor=compact_model_facing_context,
    )
    assert general_snapshot["materials"]["affect"][0]["source_ref"] == (
        "affect:mixed"
    )

    _compact, current = _inner_life_view(raw)
    materials = current["materials"]

    assert current["contract"] == "inner-life-snapshot.1"
    assert current["authority"] == "derived_from_verified_context"
    assert current["cursor"]["logical_time"] == "2026-07-28T08:00:00+08:00"
    assert materials["relationship"][0]["source_ref"] == "relationship:user"
    assert materials["appraisals"][0]["source_ref"] == "appraisal:current"
    assert materials["appraisals"][0]["source_cluster_ref"] == "cluster:repair"
    assert materials["affect"][0]["source_ref"] == "affect:mixed"
    assert [item["dimension"] for item in materials["affect"][0]["components"]] == [
        "hurt",
        "warmth",
    ]
    assert materials["affect"][0]["components"][0]["source_cluster_ref"] == "cluster:repair"
    assert materials["affect"][0]["components"][0]["appraisal_refs"][0][
        "appraisal_id"
    ] == "appraisal:current"
    assert materials["unresolved"][0]["source_ref"] == "thread:repair"
    assert materials["advisories"][0]["source_ref"] == "advisory:attention"
    assert materials["advisories"][0]["candidates"][0]["value"] == (
        "continue_if_she_wants"
    )
    assert materials["interruption"][0]["source_ref"] == "advisory:interruption"
    assert materials["interruption"][0]["candidates"][0]["value"] == "low"

    _recovery, recovery_snapshot = _inner_life_view(
        raw,
        compactor=compact_recovery_model_facing_context,
    )
    assert "advisories" not in recovery_snapshot["materials"]


def test_inner_life_snapshot_omits_entries_without_a_capsule_source_token() -> None:
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

    _compact, current = _inner_life_view(raw)
    assert "relationship" not in current
    assert "affect" not in current


def test_chat_view_keeps_source_bound_recent_self_experience_in_inner_life_snapshot() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "character_core": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "character-core:1",
                            "value": {
                                "values": {
                                    "slow_evolving": {
                                        "trait_axes": [
                                            {"axis_code": "curiosity", "value_bp": 6400}
                                        ],
                                        "autonomy_style": "self_directed",
                                    }
                                }
                            },
                        }
                    ],
                },
                "recent_experiences": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "experience:walk",
                            "value": {
                                "experience_id": "experience:walk",
                                "values": {
                                    "summary_ref": "content:experience:walk",
                                    "occurred_from": "2026-07-28T06:30:00+08:00",
                                    "occurred_to": "2026-07-28T07:00:00+08:00",
                                    "participant_refs": ["actor:companion"],
                                    "privacy_class": "personal",
                                },
                                "content": {
                                    "content_ref": "content:experience:walk",
                                    "text": "后来把这场雨记成了夏天傍晚很安静的一小段。",
                                },
                            },
                        }
                    ],
                },
                "world_life": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "occurrence:walk",
                            "value": {
                                "occurrence_id": "occurrence:walk",
                                "settled_at": "2026-07-28T07:00:00+08:00",
                                "location_ref": "location:riverside",
                                "content": {
                                    "content_ref": "content:experience:walk",
                                    "text": "沿河走了一会儿，看到雨后积水反光。",
                                },
                            },
                        }
                    ],
                },
            },
        }
    )

    _compact, current = _inner_life_view(raw)
    materials = current["materials"]

    assert current["availability"] == "available"
    assert current["source_refs"] == [
        "character-core:1",
        "experience:walk",
        "occurrence:walk",
    ]
    recent = materials["recent_self_experiences"]
    assert recent["availability"] == "available"
    assert recent["items"][0]["source_ref"] == "occurrence:walk"
    assert (
        recent["items"][0]["content"]["text"]
        == "沿河走了一会儿，看到雨后积水反光。"
    )
    assert recent["items"][1]["source_ref"] == "experience:walk"
    assert (
        recent["items"][1]["content"]["text"]
        == "后来把这场雨记成了夏天傍晚很安静的一小段。"
    )
    assert materials["stable_self"][0]["source_ref"] == "character-core:1"


def test_world_life_cannot_starve_committed_experience_from_inner_life_snapshot() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "world_life": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "occurrence:breakfast",
                            "value": {
                                "occurrence_id": "occurrence:breakfast",
                                "settled_at": "2026-07-28T07:30:00+08:00",
                                "content": {"text": "吃了早饭。"},
                            },
                        },
                        {
                            "item_ref": "occurrence:walk",
                            "value": {
                                "occurrence_id": "occurrence:walk",
                                "settled_at": "2026-07-28T07:00:00+08:00",
                                "content": {"text": "沿河走了一会儿。"},
                            },
                        },
                    ],
                },
                "recent_experiences": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "experience:argument",
                            "value": {
                                "experience_id": "experience:argument",
                                "values": {
                                    "summary_ref": "content:experience:argument",
                                    "occurred_from": "2026-07-27T18:00:00+08:00",
                                    "occurred_to": "2026-07-27T18:20:00+08:00",
                                    "participant_refs": ["actor:companion", "npc:vendor"],
                                    "privacy_class": "personal",
                                },
                                "content": {
                                    "content_ref": "content:experience:argument",
                                    "text": "傍晚和摊贩起了争执，回来后心里还堵着。",
                                },
                            },
                        }
                    ],
                },
            },
        }
    )

    _compact, current = _inner_life_view(raw)
    recent = current["materials"]["recent_self_experiences"]["items"]

    assert [item["source_ref"] for item in recent] == [
        "occurrence:breakfast",
        "experience:argument",
    ]
    assert (
        recent[1]["content"]["text"]
        == "傍晚和摊贩起了争执，回来后心里还堵着。"
    )


@pytest.mark.parametrize("source_lane", ["recent_experiences", "world_life"])
def test_recalled_own_experience_enters_current_self_as_remembered_experience(
    source_lane: str,
) -> None:
    """A selected recall must not stay buried in the generic Context slice."""

    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                source_lane: {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "experience:vendor-argument",
                            "privacy_class": "personal",
                            "recall_injected": True,
                            "value": {
                                "memory_kind": "episodic",
                                "authority": "world_fact",
                                "epistemic_scope": "world_fact",
                                "actor_ref": "actor:companion",
                                "subject_refs": ["actor:companion", "npc:vendor"],
                                "text": "上周也和摊贩争过一次，回去以后越想越堵。",
                                "source_refs": [
                                    "event:experience:vendor-argument",
                                    "event:content:vendor-argument",
                                ],
                                "occurred_from": "2026-07-20T18:00:00+08:00",
                                "occurred_to": "2026-07-20T18:20:00+08:00",
                                "valid_from": None,
                                "valid_to": None,
                                "status": "active",
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )

    _compact, current = _inner_life_view(raw)

    assert current["materials"]["recent_self_experiences"] == {
        "availability": "available",
        "items": [
            {
                "memory_kind": "episodic",
                "authority": "world_fact",
                "epistemic_scope": "world_fact",
                "actor_ref": "actor:companion",
                "subject_refs": ["actor:companion", "npc:vendor"],
                "text": "上周也和摊贩争过一次，回去以后越想越堵。",
                "occurred_from": "2026-07-20T18:00:00+08:00",
                "occurred_to": "2026-07-20T18:20:00+08:00",
                "status": "active",
                "source_ref": "experience:vendor-argument",
            }
        ],
    }
    assert current["source_refs"] == ["experience:vendor-argument"]


def test_inner_life_snapshot_reports_unavailable_without_sourced_persona_or_experience() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "character_core": {"availability": "unavailable"},
                "recent_experiences": {"availability": "unavailable"},
                "world_life": {"availability": "unavailable"},
            },
        }
    )

    _compact, current = _inner_life_view(raw)

    assert current["availability"] == "unavailable"
    assert current["source_refs"] == []
    assert current["materials"]["recent_self_experiences"] == {
        "availability": "unavailable"
    }


def test_inner_life_snapshot_exposes_source_bound_memory_and_private_impression() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "active_memory_candidates": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "memory:project-anxiety",
                            "value": {
                                "candidate_id": "memory:project-anxiety",
                                "cue_kind": "emotional_resonance",
                                "retrieval_strength_bp": 7200,
                                "source_excerpts": [
                                    {
                                        "source_kind": "fact",
                                        "source_id": "fact:project-anxiety",
                                        "excerpt_ref": "observation:project-anxiety",
                                        "text": "我其实挺怕最后做砸的。",
                                    }
                                ],
                            },
                        }
                    ],
                },
                "private_impressions": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "impression:honesty-matters",
                            "value": {
                                "subject_ref": "user:primary",
                                "reflection_summary": "他更在意真实回应，不喜欢被套话安慰。",
                                "confidence_bp": 6800,
                                "last_supported": "2026-07-28T07:59:00+08:00",
                                "status": "active",
                            },
                        }
                    ],
                },
            },
        },
        ensure_ascii=False,
    )

    _compact, current = _inner_life_view(raw)
    materials = current["materials"]

    assert materials["remembered_material"][0]["source_ref"] == "memory:project-anxiety"
    assert (
        materials["remembered_material"][0]["source_excerpts"][0]["text"]
        == "我其实挺怕最后做砸的。"
    )
    assert materials["private_impressions"][0] == {
        "subject_ref": "user:primary",
        "reflection_summary": "他更在意真实回应，不喜欢被套话安慰。",
        "confidence_bp": 6800,
        "last_supported": "2026-07-28T07:59:00+08:00",
        "status": "active",
        "source_ref": "impression:honesty-matters",
    }
    assert current["source_refs"] == [
        "impression:honesty-matters",
        "memory:project-anxiety",
    ]


def test_recalled_emotional_association_enters_current_self_without_becoming_fact() -> None:
    raw = json.dumps(
        {
            "logical_time": "2026-07-28T08:00:00+08:00",
            "slices": {
                "recalled_emotional_associations": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "affect-opening:vendor-frustration",
                            "privacy_class": "withhold",
                            "recall_injected": True,
                            "value": {
                                "memory_kind": "reflective",
                                "authority": "defeasible_interpretation",
                                "epistemic_scope": "private_interpretation",
                                "actor_ref": "actor:companion",
                                "subject_refs": ["actor:companion", "npc:vendor"],
                                "text": (
                                    "Emotional episode at opening — "
                                    "anger=4200bp | resentment=3600bp"
                                ),
                                "source_refs": [
                                    "event:affect:vendor-frustration",
                                    "event:appraisal:vendor-frustration",
                                ],
                                "occurred_from": "2026-07-20T18:00:00+08:00",
                                "occurred_to": "2026-07-20T19:00:00+08:00",
                                "status": "historical",
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )

    _compact, current = _inner_life_view(raw)

    assert current["materials"]["recalled_emotional_associations"] == [
        {
            "memory_kind": "reflective",
            "authority": "defeasible_interpretation",
            "epistemic_scope": "private_interpretation",
            "actor_ref": "actor:companion",
            "subject_refs": ["actor:companion", "npc:vendor"],
            "text": (
                "Emotional episode at opening — "
                "anger=4200bp | resentment=3600bp"
            ),
            "occurred_from": "2026-07-20T18:00:00+08:00",
            "occurred_to": "2026-07-20T19:00:00+08:00",
            "status": "historical",
            "source_ref": "affect-opening:vendor-frustration",
        }
    ]
    assert current["source_refs"] == ["affect-opening:vendor-frustration"]


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
