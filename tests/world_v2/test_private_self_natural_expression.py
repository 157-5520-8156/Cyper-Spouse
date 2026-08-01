from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.expression_draft import QQ_NAPCAT_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallSourceBinding,
)
from companion_daemon.world_v2.recall_runtime import RecallCoordinator


NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
EXPERIENCE_EVENT_REF = "event:experience:vendor-argument"


class _RecallThenSelfShareRole:
    model = "fixture:recall-then-self-share"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            supplied = json.loads(messages[1]["content"])
            current = supplied["current_self_state"]
            assert current["affect"][0]["source_ref"] == "affect:irritated-residue"
            assert current["situation"][0]["source_ref"] == "situation:late-evening"
            return json.dumps(
                {
                    "private_turn_state": {
                        "contract": "private-turn-state.1",
                        "inner_state_summary": (
                            "听见她说回去以后还越想越气，我自己也被那股憋闷勾了一下；"
                            "好像有件相似的事浮上来，但我想先把它想清楚。"
                        ),
                        "attended_source_refs": [
                            "observation:qq:current",
                            "affect:irritated-residue",
                        ],
                    },
                    "recall_request": {
                        "query_text": "和摊贩争执以后仍然憋闷的自己的经历",
                        "memory_kinds": ["episodic"],
                        "limit": 3,
                    },
                },
                ensure_ascii=False,
            )

        recalled = json.loads(messages[-1]["content"].splitlines()[-1])
        chosen = recalled["character_chosen_recall"]["candidates"]
        assert (
            chosen[0]["text"]
            == "上周也和摊贩争过一次，回去以后越想越堵，后来找朋友骂了半天。"
        )
        return json.dumps(
            {
                "private_turn_state": {
                    "contract": "private-turn-state.1",
                    "inner_state_summary": (
                        "这下想起来了。我不是只想把话再丢回给她，"
                        "我确实想把自己那次相似的狼狈说出来，和她站在一起。"
                    ),
                    "attended_source_refs": [
                        EXPERIENCE_EVENT_REF,
                        "affect:irritated-residue",
                    ],
                },
                "timing_choice": "now",
                "beats": [
                    {
                        "modality": "text",
                        "text": "我懂，最烦的就是人都走远了，脑子里还在重播。",
                    },
                    {
                        "modality": "text",
                        "text": "我上周也跟一个摊贩争过，回去以后越想越堵，后来干脆找朋友骂了半天。",
                    },
                ],
                "stance": "带着自己的相似经历陪她一起生气",
                "brief_rationale": "我选择共情并分享刚刚想起的真实经历，不把继续说话的责任交还给她。",
                "confidence": 8_600,
                "world_claims": [
                    {
                        "claim_text": "我上周也和一个摊贩争过，后来找朋友骂了半天",
                        "scope": "past_world",
                        "source_refs": [EXPERIENCE_EVENT_REF],
                    }
                ],
            },
            ensure_ascii=False,
        )


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:private-self-natural-expression",
        attempt_id="attempt:private-self-natural-expression",
        route=ModelRoute(
            tier="flash",
            reason_code="test",
            router_version="test.1",
        ),
        capsule_id="a" * 64,
        trigger_ref="event:observation:qq:current",
        evaluated_world_revision=3,
        evaluated_deliberation_revision=0,
        evaluated_ledger_sequence=5,
        model_content_json=json.dumps(
            {
                "world_revision": 3,
                "deliberation_revision": 0,
                "ledger_sequence": 5,
                "logical_time": NOW.isoformat(),
                "slices": {
                    "affect_episodes": {
                        "availability": "available",
                        "items": [
                            {
                                "item_ref": "affect:irritated-residue",
                                "privacy_class": "private",
                                "value": {
                                    "components": [
                                        {
                                            "dimension": "anger",
                                            "intensity_bp": 3_600,
                                            "residue_bp": 2_100,
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                    "current_situation": {
                        "availability": "available",
                        "items": [
                            {
                                "item_ref": "situation:late-evening",
                                "privacy_class": "private",
                                "value": {
                                    "logical_time": NOW.isoformat(),
                                    "time_segment": "late_evening",
                                    "attention_slice": {"mode": "available"},
                                    "social_environment": {
                                        "availability": "available",
                                        "participant_refs": [],
                                    },
                                },
                            }
                        ],
                    },
                },
            },
            ensure_ascii=False,
        ),
        trigger_message=TriggerMessage(
            event_ref="event:observation:qq:current",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:qq:current",
            source_world_revision=3,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:owner",
            platform_message_id="qq-message-current",
            text="就是回去以后越想越气，想找个人吐槽。",
        ),
    )


@pytest.mark.asyncio
async def test_role_can_select_own_recall_then_empathize_and_self_share_without_a_question() -> None:
    cursor = RecallCursor(
        world_revision=3,
        deliberation_revision=0,
        ledger_sequence=5,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(
        cursor=cursor,
        documents=(
            RecallDocument(
                document_id="recall:experience:vendor-argument",
                memory_kind="episodic",
                source_item_ref="experience:vendor-argument",
                source_slice="recent_experiences",
                source_refs=(EXPERIENCE_EVENT_REF,),
                source_bindings=(
                    RecallSourceBinding(
                        source_kind="committed_event",
                        authority_type="ExperienceCommitted",
                        ref=EXPERIENCE_EVENT_REF,
                        source_world_revision=2,
                        immutable_hash="c" * 64,
                    ),
                ),
                source_world_revision=2,
                text="上周也和摊贩争过一次，回去以后越想越堵，后来找朋友骂了半天。",
                actor_ref="agent:companion",
                subject_refs=("agent:companion", "npc:vendor"),
                occurred_from=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
                privacy_class="personal",
            ),
        ),
    )
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=cursor,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="event:observation:qq:current",
    )
    role = _RecallThenSelfShareRole()
    capabilities = QQ_NAPCAT_EXPRESSION_CAPABILITIES.model_copy(
        update={"private_turn_state_mode": "required"}
    )
    try:
        output = await ChatModelDeliberationAdapter(
            model=role,
            recall_coordinator=coordinator,
            expression_capabilities=capabilities,
        ).propose(_request())
    finally:
        coordinator.close()

    assert len(role.calls) == 2
    assert output.recall_trace is not None
    proposal = output.raw_proposal
    assert proposal["private_turn_state"]["attended_source_refs"] == [
        EXPERIENCE_EVENT_REF,
        "affect:irritated-residue",
    ]
    plan = json.loads(proposal["proposed_changes"][0]["payload"]["canonical_json"])
    texts = [beat["inline_text"] for beat in plan["beat_drafts"]]
    assert len(texts) == 2
    assert all("?" not in text and "？" not in text for text in texts)
    assert "我懂" in texts[0]
    assert "我上周也跟一个摊贩争过" in texts[1]
    assert plan["world_claims"][0]["source_refs"] == [EXPERIENCE_EVENT_REF]
    system = role.calls[0][0]["content"]
    assert "You own the motive, tone, timing" in system
    assert "ask fewer questions" not in system.lower()
    assert "question quota" not in system.lower()
