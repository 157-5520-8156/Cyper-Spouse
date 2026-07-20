from __future__ import annotations

import hashlib

import pytest

from companion_daemon.world_v2.expression_action_capabilities import (
    expression_action_capability,
    production_expression_action_kinds,
)
from companion_daemon.world_v2.http_capture_host import HttpCaptureTransport
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchRequest
from companion_daemon.world_v2.production_proposal_grammar import (
    ProductionProposalGrammarError,
    production_proposal_grammar,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    TypedChange,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _expression_with_adapter_only_reaction() -> DecisionProposal:
    payload = '{"reaction":"eyes"}'
    change = TypedChange(
        change_id="change:reaction:1",
        kind="expression_plan_transition",
        target_id="plan:reaction:1",
        transition="accept",
        payload=CanonicalTypedPayload.from_value(
            payload_schema="expression_plan_transition.v1",
            value={
                "plan_id": "plan:reaction:1",
                "overall_intent": "brief acknowledgement",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle",
                "beat_drafts": [
                    {
                        "beat_id": "beat:reaction:1",
                        "inline_text": payload,
                        "materialized_payload_ref": "payload:reaction:1",
                        "payload_hash": _hash(payload),
                        "content_type": "application/vnd.world-v2.reaction+json",
                        "dependency_beat_ids": [],
                        "delay_window": None,
                        "cancel_policy": "cancel-before-dispatch",
                        "reconsider_policy": "reconsider-on-new-observation",
                        "merge_policy": "never",
                    }
                ],
            },
        ),
    )
    return DecisionProposal(
        proposal_id="proposal:reaction:1",
        trigger_ref="trigger:reaction:1",
        evaluated_world_revision=1,
        evidence_refs=(),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:reaction:1",
                kind="reaction",
                layer="external_action",
                target="user:1",
                payload_ref="payload:reaction:1",
                payload_hash=_hash(payload),
                causal_change_id=change.change_id,
                beat_ref="beat:reaction:1",
            ),
        ),
        confidence=6_000,
        brief_rationale="a reaction could be appropriate",
        affect_decision="no_change",
        behavior_tendency="observe",
        stance="neutral",
        display_strategy="private",
    )


def test_expression_capability_status_requires_the_complete_vertical_not_matrix_vocabulary() -> None:
    assert production_expression_action_kinds() == {
        "reply", "followup", "proactive_message", "reaction", "typing", "sticker"
    }
    for action_kind in ("reaction", "typing", "sticker"):
        capability = expression_action_capability(action_kind)
        assert capability.availability == "production"
        assert set(capability.required_closure) == {
            "immutable_payload", "acceptance", "transport", "receipt_recovery"
        }


def test_text_only_deployment_grammar_rejects_reaction_even_when_global_vertical_is_installed() -> (
    None
):
    # The envelope deliberately validates: typed payloads remain usable by a
    # future platform adapter.  Production reachability is a separate fact.
    proposal = _expression_with_adapter_only_reaction()

    with pytest.raises(ProductionProposalGrammarError, match="action_not_reachable"):
        production_proposal_grammar(
            "chat_reply",
            expression_action_kinds=frozenset({"reply", "followup", "proactive_message"}),
        ).validate(proposal)

    production_proposal_grammar(
        "chat_reply", expression_action_kinds=production_expression_action_kinds()
    ).validate(proposal)


@pytest.mark.asyncio
async def test_http_capture_transport_records_nontext_expression_capability_failure() -> None:
    body = '{"state":"composing","version":"expression-typing.1"}'
    request = PlatformDispatchRequest(
        action_id="action:http:typing:1",
        kind="typing",
        target="user:primary",
        payload_ref="payload:http:typing:1",
        payload_hash=_hash(body),
        content_type="application/vnd.world-v2.typing+json",
        body=body,
        idempotency_key="idempotency:http:typing:1",
    )

    receipt = await HttpCaptureTransport().send(request)
    assert receipt.status == "failed"
    assert receipt.error_class == "http_capture_capability_unavailable"


@pytest.mark.asyncio
async def test_http_capture_transport_delivers_scheduler_text_messages() -> None:
    body = "我刚才又想了一下。"
    request = PlatformDispatchRequest(
        action_id="action:http:proactive:1",
        kind="proactive_message",
        target="user:primary",
        payload_ref="payload:http:proactive:1",
        payload_hash=_hash(body),
        content_type="text/plain",
        body=body,
        idempotency_key="idempotency:http:proactive:1",
    )

    receipt = await HttpCaptureTransport().send(request)
    assert receipt.status == "delivered"
