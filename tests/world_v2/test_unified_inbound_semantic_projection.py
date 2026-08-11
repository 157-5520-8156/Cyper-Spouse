from __future__ import annotations

import hashlib
import json

import pytest

from companion_daemon.world_v2.character_interior.inbound_appraisal_wire import (
    _pinned_slice_values,
    _proposal_from_draft,
)
from companion_daemon.world_v2.character_interior.inbound_author import (
    _merge_cognition_outputs,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelOutput,
    ModelRoute,
    TriggerMessage,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    TypedChange,
)
from companion_daemon.world_v2.unified_inbound_decision import (
    inspect_unified_inbound_decision,
)


USER = "user:primary"
COMPANION = "actor:companion"
OBSERVATION_EVENT = "event:observation:semantic-projection"
OBSERVATION_HASH = "sha256:" + "b" * 64
INTERACTION_ACT_REF = "interaction-act:sha256:" + "1" * 64
INTERACTION_OBJECT_REF = "interaction-object:sha256:" + "2" * 64


def _request(*, text: str) -> ModelInput:
    return ModelInput(
        call_id="call:semantic-projection",
        attempt_id="attempt:semantic-projection",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=OBSERVATION_EVENT,
        evaluated_world_revision=3,
        evaluated_deliberation_revision=0,
        evaluated_ledger_sequence=3,
        model_content_json=json.dumps(
            {
                "world_id": "world:test",
                "actor_ref": COMPANION,
                "logical_time": "2026-08-11T12:00:00+08:00",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        trigger_message=TriggerMessage(
            event_ref=OBSERVATION_EVENT,
            event_payload_hash=OBSERVATION_HASH,
            observation_ref="observation:semantic-projection",
            source_world_revision=3,
            actor=USER,
            channel="qq:c2c",
            reply_target="qq:user:primary",
            text=text,
        ),
    )


def _request_with_context(
    *,
    text: str,
    relationship_stage: str | None = None,
    interaction_act: dict[str, object] | None = None,
) -> ModelInput:
    request = _request(text=text)
    context = json.loads(request.model_content_json)
    materials: dict[str, object] = {}
    if relationship_stage is not None:
        materials["relationship"] = [
            {
                "source_ref": "relationship:user:primary",
                "subject_ref": USER,
                "stage": relationship_stage,
            }
        ]
    if interaction_act is not None:
        materials["interaction_acts"] = [
            {
                **interaction_act,
                "source_ref": f"interior:interaction-act:{INTERACTION_ACT_REF}",
            }
        ]
    context["inner_life_snapshot"] = {"materials": materials}
    return request.model_copy(
        update={
            "model_content_json": json.dumps(
                context,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }
    )


def _interaction_act_context(*, act_kind: str) -> dict[str, object]:
    return {
        "frame": {
            "subject_ref": USER,
            "counterparty_refs": [COMPANION],
            "act_kind": act_kind,
            "object": {
                "object_ref": INTERACTION_OBJECT_REF,
                "object_label": "那本书",
                "epistemic_scope": "report_only",
            },
        },
        "participant_statuses": [],
        "external_outcome": "not_established",
    }


def test_pinned_snapshot_wins_over_conflicting_legacy_slice() -> None:
    request = _request_with_context(
        text="我们继续成为更亲近的朋友吧。",
        relationship_stage="friend",
    )
    context = json.loads(request.model_content_json)
    context["slices"] = {
        "relationship_slice": {
            "availability": "available",
            "items": [
                {
                    "item_ref": "relationship:stale",
                    "value": {"subject_ref": USER, "stage": "stranger"},
                }
            ],
        }
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )

    assert _pinned_slice_values(request, "relationship_slice")[0]["stage"] == "friend"


def test_legacy_slice_fallback_preserves_exact_item_source_ref() -> None:
    request = _request(text="那本书我愿意收下。")
    context = json.loads(request.model_content_json)
    context["slices"] = {
        "interaction_acts": {
            "availability": "available",
            "items": [
                {
                    "item_ref": f"interior:interaction-act:{INTERACTION_ACT_REF}",
                    "value": _interaction_act_context(
                        act_kind="角色自由定义的跨轮动作"
                    ),
                }
            ],
        }
    }
    request = request.model_copy(
        update={"model_content_json": json.dumps(context, ensure_ascii=False)}
    )

    assert _pinned_slice_values(request, "interaction_acts")[0]["source_ref"] == (
        f"interior:interaction-act:{INTERACTION_ACT_REF}"
    )


def _base_appraisal(**extra: object) -> dict[str, object]:
    return {
        "appraise": False,
        "affect": "no_change",
        "brief_rationale": "她明确区分当前消息中的动作与自己的关系选择。",
        "behavior_tendency": "按自己的判断回应",
        "stance": "清楚",
        "display_strategy": "直接但不替对方完成动作",
        "confidence": 7800,
        **extra,
    }


def _expression(*, text: str) -> ModelOutput:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    payload_ref = f"payload:semantic:{digest}"
    plan_id = f"plan:semantic:{digest}"
    beat_id = f"beat:semantic:{digest}"
    change_id = f"change:semantic:{digest}"
    proposal = DecisionProposal(
        proposal_id=f"proposal:semantic:{digest}",
        trigger_ref=OBSERVATION_EVENT,
        evaluated_world_revision=3,
        evidence_refs=(
            ProposalEvidenceRef(
                ref_id="observation:semantic-projection",
                evidence_kind="observed_message",
                source_world_revision=3,
                immutable_hash=OBSERVATION_HASH,
            ),
        ),
        proposed_changes=(
            TypedChange(
                change_id=change_id,
                kind="expression_plan_transition",
                target_id=plan_id,
                transition="accept",
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="expression_plan_transition.v1",
                    value={
                        "plan_id": plan_id,
                        "overall_intent": "reply",
                        "ordering_policy": "dependencies",
                        "terminal_policy": "settle",
                        "beat_drafts": [
                            {
                                "beat_id": beat_id,
                                "inline_text": text,
                                "materialized_payload_ref": payload_ref,
                                "payload_hash": "sha256:" + digest,
                                "content_type": "text/plain",
                                "dependency_beat_ids": [],
                                "delay_window": None,
                                "cancel_policy": "cancel-before-dispatch",
                                "reconsider_policy": "reconsider-on-new-observation",
                                "merge_policy": "never",
                            }
                        ],
                    },
                ),
            ),
        ),
        action_intents=(
            ProposalActionIntent(
                intent_id=f"intent:semantic:{digest}",
                kind="reply",
                layer="external_action",
                target="qq:user:primary",
                payload_ref=payload_ref,
                payload_hash="sha256:" + digest,
                causal_change_id=change_id,
                beat_ref=beat_id,
            ),
        ),
        confidence=7800,
        brief_rationale="她选择把这项决定明确说出来。",
        behavior_tendency="明确回应",
        stance="接纳",
        display_strategy="直接表达",
        timing_choice="now",
    )
    return ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=proposal.model_dump(mode="json"),
    )


def test_role_authored_relationship_commitment_is_bound_to_verified_counterpart() -> None:
    request = _request(text="我们可以成为好朋友吗？")
    appraisal = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=_proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    relationship_commitment={
                        "target_stage": "friend",
                        "commitment_code": "mutual_friendship",
                        "persistence": "durable",
                        "visible_text_span": "你是我朋友了",
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        ),
    )

    merged = DecisionProposal.model_validate_json(
        json.dumps(
            _merge_cognition_outputs(
                appraisal=appraisal,
                expression=_expression(text="好呀，那说好了，你是我朋友了。"),
            ).raw_proposal
        )
    )
    commitment = inspect_unified_inbound_decision(merged).relationship_commitment

    assert commitment is not None
    assert commitment.payload.value() == {
        "commitment_code": "mutual_friendship",
        "persistence": "durable",
        "subject_ref": USER,
        "target_stage": "friend",
        "visible_text_span": "你是我朋友了",
    }


def test_relationship_commitment_rejects_uninstalled_transition_before_proposal() -> None:
    request = _request(text="我们直接成为最亲近的朋友吧。")

    with pytest.raises(
        ValueError,
        match="relationship commitment target stage is not installed from pinned head",
    ):
        _proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    relationship_commitment={
                        "target_stage": "close_friend",
                        "commitment_code": "role_authored_close_friendship",
                        "persistence": "durable",
                        "visible_text_span": "我们是最亲近的朋友了",
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        )


def test_relationship_commitment_accepts_installed_transition_from_pinned_head() -> None:
    request = _request_with_context(
        text="我们继续成为更亲近的朋友吧。",
        relationship_stage="friend",
    )
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(
                    _base_appraisal(
                        relationship_commitment={
                            "target_stage": "close_friend",
                            "commitment_code": "role_authored_close_friendship",
                            "persistence": "durable",
                            "visible_text_span": "我们是更亲近的朋友了",
                        }
                    ),
                    ensure_ascii=False,
                ),
                request=request,
            )
        )
    )

    commitment = next(
        change
        for change in proposal.proposed_changes
        if change.kind == "relationship_commitment"
    )
    assert commitment is not None
    assert commitment.payload.value()["target_stage"] == "close_friend"


def test_relationship_commitment_fails_when_visible_span_is_not_in_expression() -> None:
    request = _request(text="我们可以成为好朋友吗？")
    appraisal = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=_proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    relationship_commitment={
                        "target_stage": "friend",
                        "commitment_code": "mutual_friendship",
                        "persistence": "durable",
                        "visible_text_span": "你是我朋友了",
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        ),
    )

    with pytest.raises(ValueError, match="visible span"):
        _merge_cognition_outputs(
            appraisal=appraisal,
            expression=_expression(text="我想再认真想想。"),
        )


def test_generic_non_proposal_act_uses_typed_roles_and_exact_observed_object_span() -> None:
    request = _request(text="我已经开始整理那份会议记录，下轮继续核对。")
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(
                    _base_appraisal(
                        interaction_act={
                            "operation": "declare",
                            "status_code": "整理中",
                            "source_scope": "current_message",
                            "source_text_span": "已经开始整理那份会议记录",
                            "interaction_act_ref": None,
                            "act_kind": "整理会议记录",
                            "subject_role": "current_counterpart",
                            "counterparty_roles": ["self"],
                            "object_ref": None,
                            "object_label": "那份会议记录",
                        }
                    ),
                    ensure_ascii=False,
                ),
                request=request,
            )
        )
    )
    proposal = proposal.model_copy(update={"timing_choice": "silent"})
    interaction = inspect_unified_inbound_decision(proposal).interaction_act

    assert interaction is not None
    assert interaction.payload.value()["subject_ref"] == USER
    assert interaction.payload.value()["counterparty_refs"] == [COMPANION]
    assert interaction.payload.value()["act_kind"] == "整理会议记录"
    assert interaction.payload.value()["object_label"] == "那份会议记录"
    assert interaction.transition == "declare"
    assert interaction.payload.value()["status_code"] == "整理中"


def test_observed_interaction_act_rejects_ambiguous_repeated_source_span() -> None:
    request = _request(text="一本书，一本书，我想寄给你。")

    with pytest.raises(ValueError, match="source span"):
        _proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    interaction_act={
                        "operation": "declare",
                        "status_code": "等待后续安排",
                        "source_scope": "current_message",
                        "source_text_span": "一本书",
                        "interaction_act_ref": None,
                        "act_kind": "提出后续安排",
                        "subject_role": "current_counterpart",
                        "counterparty_roles": ["self"],
                        "object_ref": None,
                        "object_label": "一本书",
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        )


def test_interaction_act_does_not_exist_when_the_role_omits_it() -> None:
    proposal = DecisionProposal.model_validate_json(
        json.dumps(
            _proposal_from_draft(
                raw=json.dumps(_base_appraisal(), ensure_ascii=False),
                request=_request(text="我想把一本稀有古董莎士比亚寄给你。"),
            )
        )
    )
    proposal = proposal.model_copy(update={"timing_choice": "silent"})

    shape = inspect_unified_inbound_decision(proposal)
    assert shape.interaction_act is None
    assert shape.relationship_commitment is None


def test_existing_interaction_act_revision_selects_object_ref_without_reauthoring_label() -> None:
    request = _request_with_context(
        text="那本书我愿意收下。",
        interaction_act=_interaction_act_context(
            act_kind="offer_to_transfer_possession"
        ),
    )
    appraisal = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=_proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    interaction_act={
                        "operation": "revise",
                        "status_code": "愿意继续这个安排",
                        "source_scope": "delivered_expression",
                        "source_text_span": "好呀，你寄给我吧",
                        "interaction_act_ref": INTERACTION_ACT_REF,
                        "act_kind": "offer_to_transfer_possession",
                        "subject_role": "current_counterpart",
                        "counterparty_roles": ["self"],
                        "object_ref": INTERACTION_OBJECT_REF,
                        "object_label": None,
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        ),
    )

    merged = DecisionProposal.model_validate_json(
        json.dumps(
            _merge_cognition_outputs(
                appraisal=appraisal,
                expression=_expression(text="好呀，你寄给我吧。"),
            ).raw_proposal
        )
    )
    interaction = inspect_unified_inbound_decision(merged).interaction_act

    assert interaction is not None
    assert interaction.payload.value()["object_ref"] == INTERACTION_OBJECT_REF
    assert interaction.payload.value()["object_label"] is None


def test_interaction_act_revision_rejects_unpinned_ref_before_proposal() -> None:
    request = _request(text="那本书我愿意收下。")

    with pytest.raises(
        ValueError,
        match="interaction act revision did not select exactly one pinned act",
    ):
        _proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    interaction_act={
                        "operation": "revise",
                        "status_code": "愿意继续这个安排",
                        "source_scope": "delivered_expression",
                        "source_text_span": "好呀，你寄给我吧",
                        "interaction_act_ref": "interaction-act:sha256:" + "9" * 64,
                        "act_kind": "任意角色自定义动作",
                        "subject_role": "current_counterpart",
                        "counterparty_roles": ["self"],
                        "object_ref": "interaction-object:sha256:" + "8" * 64,
                        "object_label": None,
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        )


@pytest.mark.parametrize("coordinate", ["participants", "act_kind", "object_ref"])
def test_interaction_act_revision_rejects_changed_pinned_coordinates(
    coordinate: str,
) -> None:
    request = _request_with_context(
        text="那本书我愿意收下。",
        interaction_act=_interaction_act_context(act_kind="角色自由定义的跨轮动作"),
    )
    authored = {
        "operation": "revise",
        "status_code": "角色自由书写的新状态",
        "source_scope": "delivered_expression",
        "source_text_span": "我会继续记着这件事",
        "interaction_act_ref": INTERACTION_ACT_REF,
        "act_kind": "角色自由定义的跨轮动作",
        "subject_role": "current_counterpart",
        "counterparty_roles": ["self"],
        "object_ref": INTERACTION_OBJECT_REF,
        "object_label": None,
    }
    if coordinate == "participants":
        authored["subject_role"] = "self"
        authored["counterparty_roles"] = ["current_counterpart"]
    elif coordinate == "act_kind":
        authored["act_kind"] = "另一个同样自由命名的动作"
    else:
        authored["object_ref"] = "interaction-object:sha256:" + "3" * 64

    with pytest.raises(
        ValueError,
        match="interaction act revision changed pinned act coordinates",
    ):
        _proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(interaction_act=authored),
                ensure_ascii=False,
            ),
            request=request,
        )


def test_delivered_expression_interaction_act_span_must_occur_exactly_once() -> None:
    request = _request_with_context(
        text="那本书我愿意收下。",
        interaction_act=_interaction_act_context(
            act_kind="offer_to_transfer_possession"
        ),
    )
    appraisal = ModelOutput(
        model_id="same-character",
        model_version="test.1",
        raw_proposal=_proposal_from_draft(
            raw=json.dumps(
                _base_appraisal(
                    interaction_act={
                        "operation": "revise",
                        "status_code": "愿意继续这个安排",
                        "source_scope": "delivered_expression",
                        "source_text_span": "好呀",
                        "interaction_act_ref": INTERACTION_ACT_REF,
                        "act_kind": "offer_to_transfer_possession",
                        "subject_role": "current_counterpart",
                        "counterparty_roles": ["self"],
                        "object_ref": INTERACTION_OBJECT_REF,
                        "object_label": None,
                    }
                ),
                ensure_ascii=False,
            ),
            request=request,
        ),
    )

    with pytest.raises(ValueError, match="visible span"):
        _merge_cognition_outputs(
            appraisal=appraisal,
            expression=_expression(text="好呀，好呀。"),
        )
