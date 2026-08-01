from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraft,
    QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    materialize_expression_draft,
    qq_expression_capabilities,
)
from companion_daemon.world_v2.expression_plan_acceptance import (
    ExpressionPlanAcceptanceError,
    ExpressionPlanBudgetPolicy,
    derive_expression_plan_material,
)
from companion_daemon.world_v2.proposal_audit_schemas import (
    ProposalAuditProjection,
    canonical_json,
)
from companion_daemon.world_v2.schemas import BudgetAccount, Observation, ProjectionCursor


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _proposal():
    request = ModelInput(
        call_id="model-call:reaction:1",
        attempt_id="attempt:reaction:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:reaction:1",
        evaluated_world_revision=4,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        trigger_message=TriggerMessage(
            event_ref="event:observation:reaction:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:reaction:1",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message-1",
            text="终于做完啦。",
        ),
    )
    return materialize_expression_draft(
        value={
            "timing_choice": "now",
            "beats": [{"modality": "reaction", "reaction_id": "like"}],
            "stance": "acknowledge_briefly",
            "brief_rationale": "The model selected a brief non-text acknowledgement.",
        },
        request=request,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )


def test_subjective_claim_scope_remains_parseable_for_replay_but_is_not_authorable() -> None:
    """Historical drafts replay, but new authors cannot launder World facts.

    Present feelings and hypotheticals need no claim declaration at all.  A
    claim object with this legacy scope is therefore accepted by the wire
    schema for immutable-history compatibility and rejected by the authored
    draft materializer.
    """

    legacy = ExpressionDraft.model_validate(
        {
            "timing_choice": "now",
            "beats": ({"modality": "text", "text": "还不错，就是有点想你。"},),
            "stance": "warm",
            "brief_rationale": "Historical subjective claim wire.",
            "world_claims": (
                {
                    "claim_text": "我现在心情还不错",
                    "scope": "subjective_or_hypothetical",
                    "source_refs": (),
                },
            ),
        }
    )
    assert legacy.world_claims[0].scope == "subjective_or_hypothetical"

    request = ModelInput(
        call_id="model-call:claims:legacy-subjective",
        attempt_id="attempt:claims:legacy-subjective",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:claims:legacy-subjective",
        evaluated_world_revision=4,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        trigger_message=TriggerMessage(
            event_ref="event:observation:claims:legacy-subjective",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:claims:legacy-subjective",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message-legacy-subjective",
            text="你现在心情怎么样？",
        ),
    )
    with pytest.raises(ValueError, match="legacy subjective world-claim scope"):
        materialize_expression_draft(
            value=legacy.model_dump(mode="json"),
            request=request,
            capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        )


def test_world_claim_exact_source_refs_alias_is_repaired_not_rejected() -> None:
    """The prompt phrase "exact source_refs" gets echoed as a literal key.

    A fully valid reply must not collapse into the recovery lane over this
    one unambiguous rename; any other extra key still fails closed.
    """

    request = ModelInput(
        call_id="model-call:claims:1",
        attempt_id="attempt:claims:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:claims:1",
        evaluated_world_revision=4,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        trigger_message=TriggerMessage(
            event_ref="event:observation:claims:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:claims:1",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message-2",
            text="你现在心情怎么样？",
        ),
    )
    proposal = materialize_expression_draft(
        value={
            "timing_choice": "now",
            "beats": [{"modality": "text", "text": "还不错，就是有点想你。"}],
            "stance": "warm",
            "brief_rationale": "Subjective inner-life answer with echoed field name.",
            "world_claims": [],
        },
        request=request,
        capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
    )
    assert proposal is not None

    with pytest.raises(Exception):
        materialize_expression_draft(
            value={
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "好呀。"}],
                "stance": "warm",
                "brief_rationale": "Unknown extra key must still fail closed.",
                "world_claims": [
                    {
                        "claim_text": "对方说今天很高兴",
                        "scope": "counterpart_history",
                        "source_refs": ["observation:claims:1"],
                        "invented_field": True,
                    }
                ],
            },
            request=request,
            capabilities=QQ_NAPCAT_EXPRESSION_CAPABILITIES,
        )


def _audit() -> ProposalAuditProjection:
    proposal = _proposal()
    return ProposalAuditProjection(
        proposal_id=proposal.proposal_id,
        proposal_kind="decision",
        model_result_ref="model-result:reaction:1",
        deliberation_result_id="deliberation:reaction:1",
        model_call_id="model-call:reaction:1",
        attempt_id="attempt:reaction:1",
        capsule_id="a" * 64,
        trigger_ref=proposal.trigger_ref,
        evaluated_world_revision=4,
        proposal_json=canonical_json(proposal.model_dump(mode="json")),
        proposal_hash=proposal.proposal_hash,
        event_ref="event:proposal:reaction:1",
        event_payload_hash="c" * 64,
    )


def _observation(provider_message_id: str) -> Observation:
    return Observation(
        schema_version="world-v2.1",
        observation_id="observation:reaction:1",
        world_id="world:reaction",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:reaction:1",
        causation_id="qq-message-1",
        correlation_id="conversation:reaction:1",
        source="platform:qq",
        source_event_id="qq:10001:qq-message-1",
        actor="user:primary",
        channel="qq",
        payload_ref="ingress:reaction:1",
        payload_hash="d" * 64,
        text="终于做完啦。",
        received_at=NOW,
        reply_context={
            "target": "conversation:qq:c2c:10001",
            "platform_message_id": provider_message_id,
        },
    )


def _derive(observation: Observation):
    return derive_expression_plan_material(
        audit=_audit(),
        cursor=ProjectionCursor(
            world_revision=4, deliberation_revision=2, ledger_sequence=8
        ),
        world_id="world:reaction",
        policy=ExpressionPlanBudgetPolicy(
            account_id="account:chat",
            amount_limit_per_action=2,
            actor="agent:companion",
            allowed_targets=("conversation:qq:c2c:10001",),
            recovery_policy="effect_once",
        ),
        account=BudgetAccount(
            account_id="account:chat", category="chat", window_id="window:1", limit=10
        ),
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:reaction:1",
        correlation_id="conversation:reaction:1",
        source_observation=observation,
    )


def test_nontext_acceptance_reverse_checks_source_bound_provider_message() -> None:
    material = _derive(_observation("qq-message-1"))

    assert material.beats[0].action.kind == "reaction"
    assert material.beats[0].beat.payload.content_type.endswith("reaction+json")


def test_nontext_acceptance_rejects_model_envelope_redirecting_reaction_target() -> None:
    with pytest.raises(ExpressionPlanAcceptanceError, match="expression_payload_invalid"):
        _derive(_observation("qq-message-other"))


def test_private_turn_state_changes_audit_bytes_without_changing_effect_identity() -> None:
    request = ModelInput(
        call_id="model-call:private-state-effect:1",
        attempt_id="attempt:private-state-effect:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref="event:observation:private-state-effect:1",
        evaluated_world_revision=4,
        model_content_json=json.dumps({"logical_time": NOW.isoformat()}),
        trigger_message=TriggerMessage(
            event_ref="event:observation:private-state-effect:1",
            event_payload_hash="sha256:" + "b" * 64,
            observation_ref="observation:private-state-effect:1",
            source_world_revision=4,
            actor="user:primary",
            channel="qq",
            reply_target="conversation:qq:c2c:10001",
            platform_message_id="qq-message-private-state-effect-1",
            text="终于做完啦。",
        ),
    )
    common = {
        "timing_choice": "now",
        "beats": [
            {"modality": "text", "text": "那你现在可以好好歇一下了。"},
            {"modality": "text", "text": "我也替你松了口气。"},
        ],
        "stance": "warm",
        "brief_rationale": "Respond to the completed task with relief.",
    }
    proposals = tuple(
        materialize_expression_draft(
            value={
                "private_turn_state": {
                    "inner_state_summary": summary,
                    "attended_source_refs": [request.trigger_ref],
                },
                **common,
            },
            request=request,
            capabilities=qq_expression_capabilities("napcat"),
        )
        for summary in (
            "我先感到替对方松了一口气，想温柔地接住这份轻松。",
            "我有点跟着开心，也想把这份轻松直接说出来。",
        )
    )

    first, second = proposals
    assert first.proposal_hash != second.proposal_hash
    assert canonical_json(first.model_dump(mode="json")) != canonical_json(
        second.model_dump(mode="json")
    )
    assert first.proposal_id == second.proposal_id
    assert tuple(item.change_id for item in first.proposed_changes) == tuple(
        item.change_id for item in second.proposed_changes
    )
    assert tuple(item.target_id for item in first.proposed_changes) == tuple(
        item.target_id for item in second.proposed_changes
    )
    first_beats = first.proposed_changes[0].payload.value()["beat_drafts"]
    second_beats = second.proposed_changes[0].payload.value()["beat_drafts"]
    assert [item["beat_id"] for item in first_beats] == [
        item["beat_id"] for item in second_beats
    ]
    assert tuple(item.intent_id for item in first.action_intents) == tuple(
        item.intent_id for item in second.action_intents
    )

    observation = Observation(
        schema_version="world-v2.1",
        observation_id=request.trigger_message.observation_ref,
        world_id="world:private-state-effect",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:private-state-effect:1",
        causation_id="qq-message-private-state-effect-1",
        correlation_id="conversation:private-state-effect:1",
        source="platform:qq",
        source_event_id="qq:10001:qq-message-private-state-effect-1",
        actor="user:primary",
        channel="qq",
        payload_ref="ingress:private-state-effect:1",
        payload_hash="d" * 64,
        text="终于做完啦。",
        received_at=NOW,
        reply_context={
            "target": "conversation:qq:c2c:10001",
            "platform_message_id": "qq-message-private-state-effect-1",
        },
    )
    policy = ExpressionPlanBudgetPolicy(
        account_id="account:chat",
        amount_limit_per_action=2,
        actor="agent:companion",
        allowed_targets=("conversation:qq:c2c:10001",),
        recovery_policy="effect_once",
    )
    account = BudgetAccount(
        account_id="account:chat", category="chat", window_id="window:1", limit=10
    )

    def derive(proposal, suffix: str):
        audit = ProposalAuditProjection(
            proposal_id=proposal.proposal_id,
            proposal_kind="decision",
            model_result_ref=f"model-result:private-state-effect:{suffix}",
            deliberation_result_id=f"deliberation:private-state-effect:{suffix}",
            model_call_id=request.call_id,
            attempt_id=request.attempt_id,
            capsule_id=request.capsule_id,
            trigger_ref=proposal.trigger_ref,
            evaluated_world_revision=proposal.evaluated_world_revision,
            proposal_json=canonical_json(proposal.model_dump(mode="json")),
            proposal_hash=proposal.proposal_hash,
            event_ref=f"event:proposal:private-state-effect:{suffix}",
            event_payload_hash=suffix * 64,
        )
        return derive_expression_plan_material(
            audit=audit,
            cursor=ProjectionCursor(
                world_revision=4, deliberation_revision=2, ledger_sequence=8
            ),
            world_id="world:private-state-effect",
            policy=policy,
            account=account,
            logical_time=NOW,
            created_at=NOW,
            trace_id="trace:private-state-effect:1",
            correlation_id="conversation:private-state-effect:1",
            source_observation=observation,
        )

    first_material = derive(first, "a")
    second_material = derive(second, "b")
    assert tuple(item.action.action_id for item in first_material.beats) == tuple(
        item.action.action_id for item in second_material.beats
    )
