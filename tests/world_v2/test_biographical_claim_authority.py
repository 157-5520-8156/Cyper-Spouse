from __future__ import annotations

import json

from companion_daemon.world_v2.biographical_claim_authority import (
    biographical_coordinate_authorities,
)
from companion_daemon.world_v2.expression_draft import (
    world_claim_source_refs_by_scope,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
    compact_model_facing_context,
    compact_recovery_model_facing_context,
)


def _context() -> dict[str, object]:
    return {
        "world_id": "world:biography-coordinate-test",
        "actor_ref": "agent:companion",
        "world_revision": 12,
        "logical_time": "2026-07-30T06:08:00+00:00",
        "slices": {
            "world_life": {
                "availability": "available",
                "source_refs": [
                    "event:biography-configured",
                    "event:clock",
                    "event:arc-settled",
                    "event:arc-accepted",
                ],
                "items": [
                    {
                        "item_ref": "biography:summer-home",
                        "source_hash": "a" * 64,
                        "value_hash": "b" * 64,
                        "source_bindings": [
                            {
                                "source_kind": "committed_event",
                                "authority_type": "ClockAdvanced",
                                "ref": "event:clock",
                                "source_world_revision": 12,
                                "immutable_hash": "c" * 64,
                            }
                        ],
                        "value": {
                            "context_kind": "biographical_context",
                            "biography_id": "biography:summer-home",
                            "logical_at": "2026-07-30T06:08:00+00:00",
                            "age": 21,
                            "academic_phase": "summer_break",
                            "academic_year": 3,
                            "season": "summer",
                            "calendar_context_tags": [
                                "academic:summer_break",
                                "calendar:summer",
                            ],
                            "current_residence_context_tags": [
                                "residence:family_home_jiaxing"
                            ],
                            "active_life_arcs": [
                                {
                                    "arc_id": "arc:internship",
                                    "arc_kind": "employment",
                                    "context_pack_ref": "life-context:internship",
                                    "context_tags": ["employment:internship"],
                                    "started_at": "2026-07-01T00:00:00+00:00",
                                    "ends_at": "2026-08-31T00:00:00+00:00",
                                    "source_event_ref": "event:arc-settled",
                                    "accepted_event_ref": "event:arc-accepted",
                                    "context_summary_ref": "content:arc-summary",
                                    "context_summary_payload_hash": "d" * 64,
                                    "context_summary": "在出版社做暑期实习",
                                }
                            ],
                            "source_bindings": [
                                {
                                    "authority_event_ref": "event:clock",
                                    "authority_world_revision": 12,
                                    "authority_payload_hash": "c" * 64,
                                }
                            ],
                        },
                    }
                ],
            }
        },
    }


def _refs_by_path(raw: str) -> dict[str, str]:
    context = json.loads(raw)
    return {
        item.field_path: item.source_ref
        for item in biographical_coordinate_authorities(context)
    }


def test_biographical_coordinate_refs_survive_all_provider_compactions() -> None:
    raw = json.dumps(_context(), ensure_ascii=False)

    full = _refs_by_path(raw)
    ordinary = _refs_by_path(compact_model_facing_context(raw))
    chat = _refs_by_path(compact_chat_model_facing_context(raw))
    recovery = _refs_by_path(compact_recovery_model_facing_context(raw))

    assert full == ordinary == chat == recovery
    assert "/active_life_arcs/arc:internship" in full


def test_biography_parent_is_attention_only_while_exact_coordinates_are_current() -> None:
    context = _context()
    refs = world_claim_source_refs_by_scope(context=context)
    coordinate_refs = {
        item.source_ref for item in biographical_coordinate_authorities(context)
    }

    assert coordinate_refs
    assert coordinate_refs <= refs["current_world"]
    assert "biography:summer-home" not in refs["current_world"]
    assert "biography:summer-home" not in refs["past_world"]
    assert "biography:summer-home" not in refs["stable_identity"]
    assert coordinate_refs.isdisjoint(refs["past_world"])
    assert coordinate_refs.isdisjoint(refs["stable_identity"])
