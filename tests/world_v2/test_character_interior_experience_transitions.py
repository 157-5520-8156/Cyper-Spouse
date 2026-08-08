from __future__ import annotations

import json

import pytest
from pydantic import TypeAdapter, ValidationError

from companion_daemon.world_v2.character_interior.experience_transitions import (
    ExperienceTransitionCapability,
    ExperienceTransitionDraft,
    validate_experience_transition_draft,
)


SOURCE = "event:stimulus"
HEAD = "event:head"
HASH = "a" * 64


def _capability() -> ExperienceTransitionCapability:
    return ExperienceTransitionCapability.model_validate_json(
        json.dumps(
            {
                "contract": "character-interior-experience-transitions.1",
                "current_source_ref": SOURCE,
                "goal_open_available": False,
                "goal_open_unavailable_reason": "goal_content_authority_unavailable",
                "goal_heads": [
                    {
                        "target_id": "goal:existing",
                        "entity_revision": 3,
                        "status": "active",
                        "authority_source_ref": HEAD,
                        "allowed_operations": ["pause", "abandon"],
                    }
                ],
                "thread_open_available": True,
                "thread_heads": [
                    {
                        "target_id": "thread:existing",
                        "entity_revision": 2,
                        "authority_source_ref": HEAD,
                        "kind": "topic_open",
                        "importance_bp": 5000,
                        "due_at": None,
                        "expires_at": None,
                        "allowed_operations": ["update", "resolve", "cancel"],
                    }
                ],
                "commitment_open_threads": [
                    {
                        "thread_id": "thread:existing",
                        "entity_revision": 2,
                        "authority_source_ref": HEAD,
                        "resolution_contract_ref": "resolution:thread:existing",
                        "privacy_class": "private",
                    }
                ],
                "commitment_heads": [
                    {
                        "target_id": "commitment:existing",
                        "entity_revision": 4,
                        "authority_source_ref": HEAD,
                        "importance_bp": 6000,
                        "due_at": "2026-08-06T12:00:00Z",
                        "persistence": "durable",
                        "allowed_operations": ["release"],
                    }
                ],
                "memory_sources": [
                    {
                        "source_token": "memory-source:exact",
                        "source_kind": "experience",
                        "source_id": "experience:exact",
                        "source_entity_revision": 1,
                        "authority_event_ref": SOURCE,
                        "authority_world_revision": 7,
                        "authority_payload_hash": HASH,
                        "source_values_hash": HASH,
                        "privacy_ceiling": "private",
                    }
                ],
            }
        )
    )


def _draft(value: dict[str, object]):  # type: ignore[no-untyped-def]
    return TypeAdapter(ExperienceTransitionDraft).validate_json(json.dumps(value))


@pytest.mark.parametrize(
    "value",
    [
        {
            "domain": "goal",
            "operation": "pause",
            "target_id": "goal:existing",
            "expected_entity_revision": 3,
            "reason_kind": "priority_shift",
            "source_refs": [SOURCE, HEAD],
            "reason_summary": "她暂时不想把精力继续压在这里。",
        },
        {
            "domain": "thread",
            "operation": "open",
            "target_id": None,
            "expected_entity_revision": 0,
            "thread_kind": "topic_open",
            "importance_bp": 4200,
            "due_at": None,
            "expires_at": None,
            "resolution_kind": None,
            "cancellation_reason_code": None,
            "source_refs": [SOURCE],
            "reason_summary": "她想给这件事留一个以后再想的入口。",
        },
        {
            "domain": "commitment",
            "operation": "open",
            "target_id": None,
            "expected_entity_revision": 0,
            "thread_id": "thread:existing",
            "importance_bp": 6200,
            "due_at": "2026-08-06T12:00:00Z",
            "persistence": "durable",
            "release_reason_code": None,
            "source_refs": [SOURCE, HEAD],
            "reason_summary": "她愿意把这个开放事项变成自己的承诺。",
        },
        {
            "domain": "memory_candidate",
            "operation": "retain",
            "source_token": "memory-source:exact",
            "cue_kind": "world_continuity",
            "retention_rationales": ["world_continuity", "emotional_salience"],
            "salience": {
                "autobiographical_relevance_bp": 5000,
                "relationship_relevance_bp": 2000,
                "emotional_residue_bp": 6000,
                "unfinished_business_bp": 1000,
                "recurrence_bp": 1000,
                "novelty_bp": 5000,
                "future_utility_bp": 4000,
                "world_continuity_bp": 8000,
            },
            "source_refs": [SOURCE],
            "reason_summary": "她觉得这段经历以后仍会改变自己理解生活的方式。",
        },
    ],
)
def test_four_long_horizon_drafts_are_exact_capability_choices(
    value: dict[str, object],
) -> None:
    validate_experience_transition_draft(_draft(value), capability=_capability())


def test_stale_revision_and_unoffered_memory_source_fail_closed() -> None:
    stale = _draft(
        {
            "domain": "goal",
            "operation": "pause",
            "target_id": "goal:existing",
            "expected_entity_revision": 2,
            "reason_kind": "priority_shift",
            "source_refs": [SOURCE, HEAD],
            "reason_summary": "这个选择绑定了旧 revision。",
        }
    )
    with pytest.raises(ValueError, match="outside the offered head"):
        validate_experience_transition_draft(stale, capability=_capability())

    invented_memory = _draft(
        {
            "domain": "memory_candidate",
            "operation": "retain",
            "source_token": "memory-source:invented",
            "cue_kind": "world_continuity",
            "retention_rationales": ["world_continuity"],
            "salience": {
                "autobiographical_relevance_bp": 1000,
                "relationship_relevance_bp": 1000,
                "emotional_residue_bp": 1000,
                "unfinished_business_bp": 1000,
                "recurrence_bp": 1000,
                "novelty_bp": 1000,
                "future_utility_bp": 1000,
                "world_continuity_bp": 1000,
            },
            "source_refs": [SOURCE],
            "reason_summary": "这个来源并未被提供。",
        }
    )
    with pytest.raises(ValueError, match="not an offered authority"):
        validate_experience_transition_draft(
            invented_memory,
            capability=_capability(),
        )


def test_goal_creation_is_not_a_parseable_character_choice() -> None:
    with pytest.raises(ValidationError):
        _draft(
            {
                "domain": "goal",
                "operation": "open",
                "target_id": "goal:new",
                "expected_entity_revision": 0,
                "reason_kind": "renewed_intent",
                "source_refs": [SOURCE],
                "reason_summary": "没有内容权威时不能创建 Goal。",
            }
        )


def test_model_capability_view_hides_memory_proof_hashes() -> None:
    view = _capability().model_view()

    assert view["goal_open_available"] is False
    assert view["goal_open_unavailable_reason"] == "goal_content_authority_unavailable"
    assert "authority_payload_hash" not in view["memory_sources"][0]
    assert "source_values_hash" not in view["memory_sources"][0]
