from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.affect_target_bounds import (
    AFFECT_DIMENSIONS,
    AffectTargetBelowMinimumError,
    AffectTargetDimensionLowerBound,
    AffectTargetLowerBounds,
)
from companion_daemon.world_v2.character_interior.inbound_appraisal_wire import (
    _appraisal_draft_messages,
    _proposal_from_draft,
    canonicalize_appraisal_draft_wire,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    ProposalEvidenceRef,
)
from companion_daemon.world_v2.unified_inbound_decision import (
    inspect_unified_inbound_decision,
)


def _bounds(*, hurt_minimum_bp: int) -> AffectTargetLowerBounds:
    return AffectTargetLowerBounds(
        source_world_revision=3,
        source_deliberation_revision=0,
        source_ledger_sequence=0,
        bounds=tuple(
            AffectTargetDimensionLowerBound(
                dimension=dimension,
                baseline_bp=hurt_minimum_bp if dimension == "hurt" else 0,
                installed_decay_floor_bp=300,
                installed_residue_bp=500,
                minimum_target_intensity_bp=(
                    hurt_minimum_bp if dimension == "hurt" else 500
                ),
                baseline_calibration_revision=2 if dimension == "hurt" else None,
                baseline_policy_version=(
                    "affect-baseline-policy.1" if dimension == "hurt" else None
                ),
                baseline_basis_hash="c" * 64 if dimension == "hurt" else None,
            )
            for dimension in AFFECT_DIMENSIONS
        ),
    )


def _request(
    *,
    hurt_minimum_bp: int | None = None,
    active_affect: bool = False,
) -> ModelInput:
    model_content: dict[str, object] = {"capsule": "authoritative"}
    if active_affect:
        model_content = {
            "world_id": "world:test",
            "world_revision": 3,
            "slices": {
                "affect_episodes": {
                    "availability": "available",
                    "items": [
                        {
                            "source_ref": "affect:existing:1",
                            "value": {
                                "episode_id": "affect:existing:1",
                                "entity_revision": 4,
                                "status": "active",
                                "origin": {
                                    "accepted_event_ref": "event:affect:existing:1"
                                },
                                "opened_at": "2026-07-16T20:00:00+00:00",
                                "updated_at": "2026-07-17T00:00:00+00:00",
                                "components": [
                                    {
                                        "component_id": "component:hurt:1",
                                        "dimension": "hurt",
                                        "intensity_bp": 3100,
                                        "source_cluster_ref": "cluster:earlier",
                                        "decay_profile": {"floor_bp": 300},
                                        "residue_bp": 500,
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    return ModelInput(
        call_id="call:appraisal:1",
        attempt_id="attempt:appraisal:1",
        route=ModelRoute(tier="flash", reason_code="background", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:1",
        evaluated_world_revision=3,
        model_content_json=json.dumps(model_content, ensure_ascii=False),
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id="observation:1",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:1",
            source_world_revision=3,
            actor="user:primary",
            channel="simulator",
            reply_target="user:primary",
            text="你刚刚的回复让我有点失望。",
        ),
        affect_target_bounds=(
            _bounds(hurt_minimum_bp=hurt_minimum_bp)
            if hurt_minimum_bp is not None
            else None
        ),
    )


def _materialize(value: dict[str, object], *, request: ModelInput | None = None) -> DecisionProposal:
    return DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(value, ensure_ascii=False),
                request=request or _request(),
            )
        )
    )


def _appraisal(**changes: object) -> dict[str, object]:
    return {
        "appraise": True,
        "brief_rationale": "The wording may carry a real but fallible relational meaning.",
        "behavior_tendency": "hold_space",
        "stance": "attend",
        "display_strategy": "restrained_acknowledgement",
        "confidence": 7600,
        "meanings": [
            {"meaning": "disappointment", "confidence": 7200},
            {"meaning": "misunderstanding", "confidence": 2800},
        ],
        "attribution": "user",
        "severity": 5800,
        **changes,
    }


def _affect_component() -> dict[str, object]:
    return {
        "dimension": "hurt",
        "target_intensity_bp": 2400,
    }


def test_appraisal_prompt_is_an_inert_compiler_for_the_unified_author() -> None:
    messages = _appraisal_draft_messages(_request())

    assert "AppraisalDraft" in messages[0]["content"]
    assert "before the visible reply" in messages[0]["content"]
    assert "virtual companion" not in messages[0]["content"].lower()
    assert "sustained ordinary interaction" in messages[0]["content"]
    assert "no message count" in messages[0]["content"]
    assert "may still choose appraise=false" in messages[0]["content"]
    assert '"trigger_evidence"' in messages[1]["content"]


def test_appraisal_prompt_matches_the_canonical_live_wire_bounds() -> None:
    prompt = _appraisal_draft_messages(_request())[0]["content"]

    assert "brief_rationale (1-240 characters)" in prompt
    assert "each 1-128 characters" in prompt
    assert "1-3 objects" in prompt
    assert "1-128 characters" in prompt
    assert "affect must be explicit on every live result" in prompt
    assert "omitting affect means no_change" not in prompt


@pytest.mark.parametrize(
    ("affect", "changes"),
    [
        ("open", {}),
        ("update", {"components": [_affect_component()]}),
        ("update", {"episode_id": "affect:existing:1"}),
        ("resolve", {"episode_id": "affect:existing:1"}),
        ("supersede", {"components": [_affect_component()]}),
        ("supersede", {"episode_id": "affect:existing:1"}),
    ],
)
def test_canonical_appraisal_rejects_incomplete_affect_lifecycle(
    affect: str,
    changes: dict[str, object],
) -> None:
    draft = _appraisal(affect=affect, **changes)

    with pytest.raises(ValueError, match="AppraisalDraft wire is invalid"):
        canonicalize_appraisal_draft_wire(draft)


def test_appraisal_prompt_keeps_values_but_omits_capsule_proof_noise() -> None:
    noisy_context = json.dumps(
        {
            "world_id": "world:test",
            "actor_ref": "agent:companion",
            "trigger_ref": "event:observation:1",
            "world_revision": 3,
            "logical_time": "2026-07-17T00:00:00+00:00",
            "slices": {
                "recent_dialogue": {
                    "availability": "available",
                    "source_refs": ["event:observation:1"],
                    "source_hash": "a" * 64,
                    "resolver_proof": {"large": "x" * 4_000},
                    "items": [
                        {
                            "item_ref": "dialogue:user:1",
                            "privacy_class": "private",
                            "source_hash": "b" * 64,
                            "value_hash": "c" * 64,
                            "source_bindings": [
                                {"ref": "event:observation:1", "hash": "d" * 64}
                            ],
                            "value": {
                                "speaker": "user",
                                "text": "你刚刚的回复让我有点失望。",
                            },
                        }
                    ],
                }
            },
        },
        ensure_ascii=False,
    )
    request = _request().model_copy(update={"model_content_json": noisy_context})

    supplied = json.loads(_appraisal_draft_messages(request)[1]["content"])["request"]
    compact = json.loads(supplied["model_content_json"])
    dialogue = compact["slices"]["recent_dialogue"]

    assert dialogue["items"][0]["value"]["text"] == "你刚刚的回复让我有点失望。"
    assert dialogue["items"][0]["source_ref"] == "dialogue:user:1"
    assert "resolver_proof" not in dialogue
    assert len(json.dumps(compact, ensure_ascii=False)) < len(noisy_context) // 2
    assert request.model_content_json == noisy_context


def test_appraisal_prompt_exposes_exact_active_affect_head_authority() -> None:
    supplied = json.loads(
        _appraisal_draft_messages(_request(active_affect=True))[1]["content"]
    )["request"]

    assert supplied["active_affect_heads"] == [
        {
            "episode_id": "affect:existing:1",
            "episode_source_ref": "affect:existing:1",
            "entity_revision": 4,
            "origin_event_ref": "event:affect:existing:1",
            "opened_at": "2026-07-16T20:00:00+00:00",
            "updated_at": "2026-07-17T00:00:00+00:00",
            "components": [
                {
                    "component_id": "component:hurt:1",
                    "dimension": "hurt",
                    "current_intensity_bp": 3100,
                    "minimum_target_intensity_bp": 500,
                    "source_cluster_ref": "cluster:earlier",
                }
            ],
        }
    ]


def test_materializer_binds_fallible_appraisal_to_verified_observation() -> None:
    proposal = _materialize(_appraisal())

    assert proposal.proposal_kind == "decision"
    assert proposal.appraisals[0].change_ref == proposal.proposed_changes[0].change_id
    assert proposal.evidence_refs[0].ref_id == "observation:1"
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["meaning_candidates"][0]["meaning"] == "disappointment"
    assert proposal.affect_decision == "no_change"


def test_materializer_preserves_source_bound_free_text_meaning() -> None:
    proposal = _materialize(
        _appraisal(
            meanings=[
                {
                    "meaning": "她把这句话理解成对方在认真修补刚才的疏远",
                    "confidence": 8100,
                }
            ]
        )
    )

    payload = proposal.proposed_changes[0].payload.value()
    assert payload["meaning_candidates"] == [
        {
            "meaning": "她把这句话理解成对方在认真修补刚才的疏远",
            "confidence": 8100,
        }
    ]


def test_materializer_binds_same_turn_appraisal_and_affect() -> None:
    proposal = _materialize(
        _appraisal(
            affect="open",
            attribution="companion",
            components=[
                {"dimension": "hurt", "target_intensity_bp": 3600},
                {"dimension": "sadness", "target_intensity_bp": 1800},
            ],
        )
    )

    assert [change.kind for change in proposal.proposed_changes] == [
        "appraisal_transition",
        "affect_transition",
    ]
    appraisal_change, affect_change = proposal.proposed_changes
    assert affect_change.payload.value()["appraisal_change_refs"] == [
        appraisal_change.change_id
    ]
    assert affect_change.payload.value()["component_targets"] == [
        {"dimension": "hurt", "target_intensity_bp": 3600},
        {"dimension": "sadness", "target_intensity_bp": 1800},
    ]
    assert proposal.affect_decision == "propose"


@pytest.mark.parametrize(
    ("affect_fields", "expected_transition", "expected_payload"),
    (
        (
            {
                "affect": "update",
                "episode_id": "affect:existing:1",
                "components": [
                    {
                        "component_id": "component:hurt:1",
                        "dimension": "hurt",
                        "target_intensity_bp": 1700,
                    }
                ],
            },
            "update",
            {
                "episode_id": "affect:existing:1",
                "component_targets": [
                    {
                        "component_id": "component:hurt:1",
                        "dimension": "hurt",
                        "target_intensity_bp": 1700,
                    }
                ],
            },
        ),
        (
            {
                "affect": "resolve",
                "episode_id": "affect:existing:1",
                "resolution_summary": "The new exchange changed how the earlier hurt is held.",
            },
            "resolve",
            {
                "episode_id": "affect:existing:1",
                "resolution_summary": (
                    "The new exchange changed how the earlier hurt is held."
                ),
            },
        ),
        (
            {
                "affect": "supersede",
                "episode_id": "affect:existing:1",
                "components": [
                    {"dimension": "warmth", "target_intensity_bp": 2400}
                ],
            },
            "supersede",
            {
                "episode_id": "affect:existing:1",
                "component_targets": [
                    {"dimension": "warmth", "target_intensity_bp": 2400}
                ],
            },
        ),
    ),
)
def test_materializer_can_author_exact_existing_affect_lifecycle_transition(
    affect_fields: dict[str, object],
    expected_transition: str,
    expected_payload: dict[str, object],
) -> None:
    proposal = _materialize(
        _appraisal(**affect_fields),
        request=_request(active_affect=True),
    )

    affect_change = proposal.proposed_changes[1]
    assert affect_change.transition == expected_transition
    assert affect_change.target_id == "affect:existing:1"
    assert affect_change.expected_entity_revision == 4
    payload = affect_change.payload.value()
    assert payload["appraisal_change_refs"] == [
        proposal.proposed_changes[0].change_id
    ]
    for key, value in expected_payload.items():
        assert payload[key] == value
    assert proposal.affect_decision == "propose"
    assert (
        inspect_unified_inbound_decision(
            proposal.model_copy(update={"timing_choice": "silent"})
        ).affect
        == affect_change
    )


def test_materializer_degrades_unoffered_existing_affect_identity_to_no_change() -> None:
    # A broken episode reference must not kill the turn. resolve carries no
    # affect coordinates, so it degrades to the explicit no_change; update
    # keeps its components and degrades to a new episode.
    resolved = _materialize(
        _appraisal(
            affect="resolve",
            episode_id="affect:not-offered",
            resolution_summary="done",
        ),
        request=_request(active_affect=True),
    )
    assert resolved.affect_decision == "no_change"

    updated = _materialize(
        _appraisal(
            affect="update",
            episode_id="affect:not-offered",
            components=[{"dimension": "warmth", "target_intensity_bp": 3000}],
        ),
        request=_request(active_affect=True),
    )
    assert updated.affect_decision == "propose"


def test_materialized_fields_are_part_of_proposal_identity() -> None:
    first = _materialize(_appraisal(display_strategy="restrained_acknowledgement"))
    second = _materialize(_appraisal(display_strategy="direct_acknowledgement"))

    assert first.proposal_id != second.proposal_id
    assert first.proposed_changes[0].change_id != second.proposed_changes[0].change_id


def test_materializer_accepts_probability_scale_meaning_confidence() -> None:
    proposal = _materialize(
        _appraisal(
            meanings=[
                {"meaning": "he cares", "confidence": 0.7},
                {"meaning": "he is curious", "confidence": 0.3},
            ],
        )
    )

    payload = proposal.proposed_changes[0].payload.value()
    assert payload["meaning_candidates"] == [
        {"meaning": "he cares", "confidence": 7_000},
        {"meaning": "he is curious", "confidence": 3_000},
    ]


def test_materializer_rejects_out_of_range_probability_confidence() -> None:
    with pytest.raises(ValueError, match="meaning is invalid"):
        _materialize(
            _appraisal(
                meanings=[{"meaning": "he cares", "confidence": 1.5}],
            )
        )


def test_materializer_can_represent_character_chosen_no_change() -> None:
    proposal = _materialize(
        {
            "appraise": False,
            "brief_rationale": "No material relational signal.",
            "behavior_tendency": "observe",
            "stance": "wait",
            "display_strategy": "withhold",
            "confidence": 3000,
        }
    )

    assert proposal.proposed_changes == ()
    assert proposal.affect_decision == "no_change"


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (
            {
                "appraise": False,
                "affect": "open",
                "brief_rationale": "carry it",
                "behavior_tendency": "withdraw",
                "stance": "wait",
                "display_strategy": "withhold",
                "confidence": 5000,
                "components": [
                    {"dimension": "hurt", "target_intensity_bp": 3000}
                ],
            },
            "requires appraise=true",
        ),
        (
            _appraisal(
                affect="open",
                components=[
                    {"dimension": "jealousy", "target_intensity_bp": 3000}
                ],
            ),
            "component",
        ),
        (
            _appraisal(
                meanings=[{"meaning": "   ", "confidence": 5000}],
            ),
            "meaning",
        ),
    ),
)
def test_materializer_fails_closed_for_illegal_appraisal_shape(
    value: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _materialize(value)


def test_materializer_never_clamps_a_target_below_pinned_bound() -> None:
    with pytest.raises(AffectTargetBelowMinimumError):
        _materialize(
            _appraisal(
                affect="open",
                components=[{"dimension": "hurt", "target_intensity_bp": 100}],
            ),
            request=_request(hurt_minimum_bp=4200),
        )
