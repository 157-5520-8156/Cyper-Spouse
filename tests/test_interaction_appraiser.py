from __future__ import annotations

import json

import pytest

from companion_daemon.emotion_state import InteractionEvent
from companion_daemon.interaction_appraiser import (
    InteractionAppraiser,
    InteractionEvidence,
    TurnAppraisalInput,
    assess_appraisal_risk,
    appraise_user_affect,
)
from companion_daemon.models import IncomingMessage, SourceMessageObservation


class ProposalModel:
    def __init__(self, proposal: dict[str, object]) -> None:
        self.proposal = proposal
        self.calls = 0
        self.messages = None

    async def complete(self, messages, *, temperature):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.messages = messages
        return json.dumps(self.proposal, ensure_ascii=False)


def _ordinary() -> InteractionEvent:
    return InteractionEvent(
        "ordinary_message", 1, "chat", "", "自然回应", acts=(), target="general"
    )


@pytest.mark.asyncio
async def test_explicit_harm_is_decided_locally_without_a_model_call() -> None:
    model = ProposalModel({})
    appraiser = InteractionAppraiser(model)

    decision = await appraiser.assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="滚，你就是个废物", source_event_ids=("qq:17",)),
            fallback=InteractionEvent(
                "dehumanization", 4, "hostile", "受到贬低", "明确边界",
                acts=("insult",), target="companion",
            ),
            recent_messages=(),
            relationship_stage="acquaintance",
        )
    )

    assert decision.accepted.kind == "dehumanization"
    assert decision.provenance == "local_explicit"
    assert decision.evidence.source_event_ids == ("qq:17",)
    assert model.calls == 0


@pytest.mark.asyncio
async def test_multimodal_risk_gate_requests_a_bounded_contextual_proposal() -> None:
    model = ProposalModel(
        {
            "appraisal": "control_pressure",
            "literal_act": "要求回应",
            "implied_attitude": "否定拒绝权",
            "target": "companion",
            "agency": "user",
            "certainty": 88,
            "goal_congruence": -60,
            "controllability": 35,
            "norm_compatibility": -75,
            "power_delta": -65,
            "confidence": 0.91,
            "severity": 3,
            "acts": ["pressure"],
            "evidence_spans": ["现在就回答"],
            "alternative_appraisal": "也可能只是着急",
        }
    )
    appraiser = InteractionAppraiser(model)

    decision = await appraiser.assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(
                text="现在就回答",
                burst_count=5,
                reply_delay_seconds=1.0,
                reply_target="companion:boundary",
                source_event_ids=("qq:18", "qq:19"),
            ),
            fallback=_ordinary(),
            recent_messages=({"direction": "out", "text": "我现在不想回答"},),
            relationship_stage="stranger",
        )
    )

    assert decision.accepted.kind == "control_pressure"
    assert decision.provenance == "model_validated"
    assert decision.risk.reasons == ("turn_burst", "boundary_reply_target")
    assert model.calls == 1
    prompt = json.loads(model.messages[0]["content"])
    assert prompt["interaction_evidence"]["source_event_ids"] == ["qq:18", "qq:19"]


@pytest.mark.asyncio
async def test_high_recall_gate_catches_unlisted_imperative_pressure() -> None:
    model = ProposalModel(
        {
            "appraisal": "control_pressure",
            "literal_act": "命令解释",
            "implied_attitude": "不允许延迟或拒绝",
            "target": "companion",
            "agency": "user",
            "certainty": 85,
            "goal_congruence": -55,
            "controllability": 30,
            "norm_compatibility": -65,
            "power_delta": -70,
            "confidence": 0.87,
            "severity": 3,
            "acts": ["imperative"],
            "evidence_spans": ["立刻给我解释清楚"],
            "alternative_appraisal": "可能只是赶时间",
        }
    )
    decision = await InteractionAppraiser(model).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="还要我说几遍，立刻给我解释清楚"),
            fallback=_ordinary(),
            recent_messages=(),
            relationship_stage="stranger",
        )
    )
    assert decision.accepted.kind == "control_pressure"
    assert "imperative_pressure" in decision.risk.reasons


@pytest.mark.asyncio
async def test_appraiser_excludes_other_users_from_context_before_proposal() -> None:
    model = ProposalModel(
        {
            "appraisal": "ordinary_message",
            "literal_act": "催促",
            "implied_attitude": "着急",
            "target": "companion",
            "agency": "user",
            "certainty": 50,
            "goal_congruence": -10,
            "controllability": 80,
            "norm_compatibility": 0,
            "power_delta": 0,
            "confidence": 0.6,
            "severity": 1,
            "acts": ["request"],
            "evidence_spans": ["马上回答"],
            "alternative_appraisal": "普通催促",
        }
    )
    await InteractionAppraiser(model).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="马上回答"),
            fallback=_ordinary(),
            recent_messages=(
                {"user_id": "user:alice", "direction": "in", "text": "alice-private"},
                {"user_id": "user:bob", "direction": "in", "text": "bob-private"},
            ),
            relationship_stage="stranger",
            canonical_user_id="user:alice",
        )
    )
    prompt = json.loads(model.messages[0]["content"])
    assert [item["text"] for item in prompt["recent_context"]] == ["alice-private"]


@pytest.mark.asyncio
async def test_non_text_evidence_can_raise_salience_but_cannot_prove_strong_harm() -> None:
    model = ProposalModel({})
    decision = await InteractionAppraiser(model).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(
                text="🙂",
                emoji=("🙂",),
                sticker_kind="mocking_unknown",
                burst_count=7,
                source_event_ids=("qq:20",),
            ),
            fallback=_ordinary(),
            recent_messages=(),
            relationship_stage="stranger",
        )
    )

    assert decision.accepted.kind == "ordinary_message"
    assert decision.provenance == "local_low_risk"
    assert decision.risk.score > 0
    assert model.calls == 0


def test_interaction_evidence_rejects_unbounded_or_unsourced_observations() -> None:
    with pytest.raises(ValueError, match="source_event_ids"):
        InteractionEvidence(text="快回答", burst_count=3)
    with pytest.raises(ValueError, match="burst_count"):
        InteractionEvidence(text="快回答", burst_count=100, source_event_ids=("qq:1",))
    with pytest.raises(ValueError, match="emoji"):
        InteractionEvidence(
            text="🙂", emoji=("🙂",) * 30, source_event_ids=("qq:1",)
        )


def test_interaction_evidence_is_built_from_platform_observations() -> None:
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="qq:31",
        text="你所谓的‘开玩笑’就是这个？",
        emoji=["qq-face:178"],
        sticker_kind="[无语]",
        reply_target="boundary:9",
    )

    evidence = InteractionEvidence.from_message(
        message,
        source_event_ids=("qq:30", "qq:31"),
        burst_count=2,
        reply_delay_seconds=4.5,
    )

    assert evidence.emoji == ("qq-face:178",)
    assert evidence.sticker_kind == "[无语]"
    assert evidence.reply_target == "boundary:9"
    assert evidence.source_event_ids == ("qq:30", "qq:31")
    assert evidence.reply_delay_seconds == 4.5


def test_interaction_evidence_keeps_merged_sticker_and_reply_semantics_with_source() -> None:
    message = IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id="qq:32",
        text="第一句\n第二句",
        source_message_ids=["qq:31", "qq:32"],
        source_messages=[
            SourceMessageObservation(
                message_id="qq:31",
                text="第一句",
                sticker_kind="[无语]",
                reply_target="boundary:29",
            ),
            SourceMessageObservation(message_id="qq:32", text="第二句"),
        ],
    )

    evidence = InteractionEvidence.from_message(
        message, source_event_ids=("qq:31", "qq:32"), burst_count=2
    )

    assert [item.message_id for item in evidence.source_messages] == ["qq:31", "qq:32"]
    assert evidence.source_messages[0].sticker_kind == "[无语]"
    assert evidence.source_messages[0].reply_target == "boundary:29"
    assert assess_appraisal_risk(evidence, _ordinary()).reasons == (
        "boundary_reply_target",
        "non_text_tone",
    )


@pytest.mark.parametrize("cue", ["你挺忙的", "行吧", "呵呵"])
def test_hot_implicit_disappointment_requires_an_immediately_unattuned_share_reply(
    cue: str,
) -> None:
    appraisal = appraise_user_affect(
        cue,
        (
            {"direction": "in", "text": "今天我终于拿到那个一直在等的 offer 了，心里很复杂。"},
            {"direction": "out", "text": "刚看到，我在。"},
        ),
        cadence="hot",
    )

    assert appraisal is not None
    assert appraisal.kind == "disappointment"
    assert appraisal.intensity == 2
    assert appraisal.should_persist is True
    assert appraisal.evidence_spans == (cue, "刚看到，我在。")


@pytest.mark.parametrize(
    ("cue", "history", "cadence"),
    [
        (
            "行吧",
            (
                {"direction": "in", "text": "午饭吃了什么？"},
                {"direction": "out", "text": "我吃了面，你呢？"},
            ),
            "hot",
        ),
        (
            "呵呵",
            (
                {"direction": "in", "text": "今天我终于拿到那个一直在等的 offer 了，心里很复杂。"},
                {"direction": "out", "text": "刚看到，我在。"},
            ),
            "warm",
        ),
        ("你挺忙的", (), "hot"),
    ],
)
def test_implicit_disappointment_does_not_treat_terse_language_as_a_standalone_harm(
    cue: str, history: tuple[dict[str, str], ...], cadence: str,
) -> None:
    assert appraise_user_affect(cue, history, cadence=cadence) is None


@pytest.mark.asyncio
async def test_proposal_without_an_alternative_interpretation_is_rejected() -> None:
    proposal = {
        "appraisal": "control_pressure",
        "target": "companion",
        "agency": "user",
        "certainty": 90,
        "goal_congruence": -50,
        "controllability": 40,
        "norm_compatibility": -60,
        "power_delta": -50,
        "confidence": 0.9,
        "severity": 3,
        "evidence_spans": ["马上回答"],
    }
    decision = await InteractionAppraiser(ProposalModel(proposal)).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="马上回答"),
            fallback=_ordinary(),
            recent_messages=(),
            relationship_stage="stranger",
        )
    )
    assert decision.accepted.kind == "ordinary_message"
    assert decision.provenance == "proposal_rejected"


@pytest.mark.asyncio
async def test_proposal_evidence_and_acts_must_be_json_arrays() -> None:
    proposal = {
        "appraisal": "control_pressure",
        "target": "companion",
        "agency": "user",
        "certainty": 90,
        "goal_congruence": -50,
        "controllability": 40,
        "norm_compatibility": -60,
        "power_delta": -50,
        "confidence": 0.9,
        "severity": 3,
        "acts": "pressure",
        "evidence_spans": "马上回答",
        "alternative_appraisal": "可能只是着急",
    }
    decision = await InteractionAppraiser(ProposalModel(proposal)).assess(
        TurnAppraisalInput(
            evidence=InteractionEvidence(text="马上回答"),
            fallback=_ordinary(),
            recent_messages=(),
            relationship_stage="stranger",
        )
    )
    assert decision.provenance == "proposal_rejected"
