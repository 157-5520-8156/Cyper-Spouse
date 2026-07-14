from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.proposal_envelope import (
    AppraisalSummary,
    CanonicalTypedPayload,
    ContinuationProposal,
    DecisionProposal,
    MinimalProposal,
    ProposalActionIntent,
    ProposalEvidenceRef,
    ReferencedSummary,
    TypedChange,
    validate_proposal_envelope,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _payload(schema: str, value: dict[str, object]) -> CanonicalTypedPayload:
    return CanonicalTypedPayload.from_value(payload_schema=schema, value=value)


def _binding(ref: str) -> dict[str, object]:
    return {
        "object_ref": ref,
        "schema_version": "test.1",
        "payload_hash": _hash(ref),
    }


def _source(ref: str) -> dict[str, object]:
    return {
        "ref_id": ref,
        "source_world_revision": 7,
        "immutable_hash": _hash(ref),
    }


def _evidence(
    ref_id: str = "event:message:1",
    evidence_kind: str = "observed_message",
) -> ProposalEvidenceRef:
    return ProposalEvidenceRef(
        ref_id=ref_id,
        evidence_kind=evidence_kind,
        source_world_revision=7,
        immutable_hash=_hash(ref_id),
    )


def _expression_change() -> TypedChange:
    return TypedChange(
        change_id="change:expression:1",
        kind="expression_plan_transition",
        target_id="expression-plan:1",
        transition="accept",
        evidence_refs=("event:message:1",),
        payload=_payload(
            "expression_plan_transition.v1",
            {
                "plan_id": "expression-plan:1",
                "overall_intent": "reply naturally",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle_after_terminal_beats",
                "beat_drafts": [
                    {
                        "beat_id": "beat:1",
                        "payload_ref": "payload:beat:1",
                        "payload_hash": _hash("hello"),
                        "content_type": "text/plain",
                        "dependency_beat_ids": [],
                        "delay_window": None,
                        "cancel_policy": "allowed_before_dispatch",
                        "reconsider_policy": "on_context_change",
                        "merge_policy": "never",
                    }
                ],
            },
        ),
    )


def _reply_intent() -> ProposalActionIntent:
    return ProposalActionIntent(
        intent_id="intent:reply:1",
        kind="reply",
        layer="external_action",
        target="user:1",
        payload_ref="payload:beat:1",
        payload_hash=_hash("hello"),
        causal_change_id="change:expression:1",
        beat_ref="beat:1",
        dependencies=(),
        due_window=(NOW, NOW + timedelta(minutes=2)),
    )


def _minimal_expression_change(response_text: str) -> TypedChange:
    payload_hash = _hash(response_text)
    return TypedChange(
        change_id="change:expression:1",
        kind="expression_plan_transition",
        target_id="expression-plan:1",
        transition="accept",
        evidence_refs=("event:message:1",),
        payload=_payload(
            "expression_plan_transition.v1",
            {
                "plan_id": "expression-plan:1",
                "overall_intent": "minimal recovery reply",
                "ordering_policy": "dependencies",
                "terminal_policy": "settle_after_terminal_beats",
                "beat_drafts": [
                    {
                        "beat_id": "beat:1",
                        "inline_text": response_text,
                        "materialized_payload_ref": "payload:minimal:1",
                        "payload_hash": payload_hash,
                        "content_type": "text/plain",
                        "dependency_beat_ids": [],
                        "delay_window": None,
                        "cancel_policy": "allowed_before_dispatch",
                        "reconsider_policy": "on_context_change",
                        "merge_policy": "never",
                    }
                ],
            },
        ),
    )


def _minimal_reply_intent(response_text: str) -> ProposalActionIntent:
    return _reply_intent().model_copy(
        update={"payload_ref": "payload:minimal:1", "payload_hash": _hash(response_text)}
    )


def _decision(**overrides: object) -> DecisionProposal:
    values: dict[str, object] = {
        "proposal_id": "proposal:decision:1",
        "trigger_ref": "trigger:1",
        "evaluated_world_revision": 7,
        "schema_registry_version": "world-v2-proposals.1",
        "evidence_refs": (_evidence(),),
        "proposed_changes": (_expression_change(),),
        "action_intents": (_reply_intent(),),
        "confidence": 8500,
        "brief_rationale": "The observed message warrants a direct reply.",
        "appraisals": (),
        "affect_tendencies": (),
        "drives": ("continue_conversation",),
        "conflicts": (),
        "behavior_tendency": "engage",
        "stance": "warm",
        "display_strategy": "direct",
        "conversation_thread_changes": (),
    }
    values.update(overrides)
    return DecisionProposal(**values)


def test_decision_is_frozen_extra_forbid_and_has_stable_canonical_hash() -> None:
    proposal = _decision()
    rebuilt = DecisionProposal.model_validate_json(proposal.model_dump_json())
    assert rebuilt.proposal_hash == proposal.proposal_hash
    assert proposal.proposal_hash.startswith("sha256:")

    with pytest.raises(ValidationError):
        DecisionProposal.model_validate({**proposal.model_dump(), "secret_reasoning": "chain"})
    with pytest.raises(ValidationError):
        DecisionProposal.model_validate({**proposal.model_dump(), "brief_rationale": "x" * 241})


@pytest.mark.parametrize(
    ("kind", "transition", "payload_schema"),
    [
        ("unknown_transition", "commit", "unknown_transition.v1"),
        ("thread_transition", "invent", "thread_transition.v1"),
        ("thread_transition", "open", "thread_transition.v2"),
    ],
)
def test_typed_change_rejects_unknown_kind_transition_or_payload_version(
    kind: str, transition: str, payload_schema: str
) -> None:
    with pytest.raises(ValidationError):
        TypedChange(
            change_id="change:bad",
            kind=kind,
            target_id="target:bad",
            transition=transition,
            payload=CanonicalTypedPayload(
                payload_schema=payload_schema,
                payload_version=1,
                canonical_json="{}",
            ),
        )


def test_payload_requires_canonical_json_and_records_a_stable_hash() -> None:
    payload = _payload("thread_transition.v1", {"z": 1, "a": [2, 3]})
    assert payload.canonical_json == '{"a":[2,3],"z":1}'
    assert payload.payload_hash == _hash(payload.canonical_json)
    with pytest.raises(ValidationError, match="canonical"):
        CanonicalTypedPayload(
            payload_schema="thread_transition.v1",
            payload_version=1,
            canonical_json=json.dumps({"z": 1, "a": [2, 3]}),
        )
    with pytest.raises(ValidationError, match="nested typed schema"):
        TypedChange(
            change_id="change:empty-thread",
            kind="thread_transition",
            target_id="thread:empty",
            transition="open",
            payload=_payload("thread_transition.v1", {}),
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("assertion_binding", {}),
        ("anchor_evidence", [True, {}]),
    ],
)
def test_fact_payload_rejects_untyped_nested_objects_and_list_items(
    field: str, invalid: object
) -> None:
    value: dict[str, object] = {
        "before_image": None,
        "after_image": _binding("fact-image:after"),
        "subject": "character:1",
        "predicate": "likes",
        "cardinality": "one",
        "conflict_key": "fact:likes:tea",
        "value_hash": _hash("tea"),
        "assertion_binding": _binding("assertion:1"),
        "anchor_evidence": [_source("event:anchor:1")],
        "source_evidence": [_source("event:source:1")],
        "privacy": "private",
    }
    value[field] = invalid
    with pytest.raises(ValidationError, match="nested typed schema"):
        TypedChange(
            change_id="change:fact:invalid",
            kind="fact_transition",
            target_id="fact:1",
            transition="commit",
            payload=_payload("fact_transition.v1", value),
        )


@pytest.mark.parametrize(
    ("field", "invalid"),
    [("blockers", [True, {}]), ("completion_contract", {})],
)
def test_goal_payload_rejects_untyped_blockers_and_empty_contract(
    field: str, invalid: object
) -> None:
    value: dict[str, object] = {
        "before_image": None,
        "after_image": _binding("goal-image:after"),
        "goal_id": "goal:1",
        "outcome_ref": None,
        "importance": 5000,
        "progress": 0,
        "due": None,
        "blockers": [],
        "completion_contract": _binding("goal-contract:1"),
    }
    value[field] = invalid
    with pytest.raises(ValidationError, match="nested typed schema"):
        TypedChange(
            change_id="change:goal:invalid",
            kind="goal_transition",
            target_id="goal:1",
            transition="open",
            payload=_payload("goal_transition.v1", value),
        )


def test_proposal_rejects_duplicate_ids_refs_and_unbound_summaries() -> None:
    change = _expression_change()
    with pytest.raises(ValidationError, match="duplicate change_id"):
        _decision(proposed_changes=(change, change))
    evidence = _evidence()
    with pytest.raises(ValidationError, match="duplicate evidence"):
        _decision(evidence_refs=(evidence, evidence))
    intent = _reply_intent()
    with pytest.raises(ValidationError, match="duplicate intent_id"):
        _decision(action_intents=(intent, intent))
    with pytest.raises(ValidationError, match="appraisal summary"):
        _decision(appraisals=(AppraisalSummary(change_ref="missing", summary="possible hurt"),))
    with pytest.raises(ValidationError, match="thread summary"):
        _decision(
            conversation_thread_changes=(
                ReferencedSummary(change_ref="change:expression:1", summary="wrong kind"),
            )
        )


def test_expression_actions_must_match_their_beat_and_payload_hash() -> None:
    intent = _reply_intent()
    with pytest.raises(ValidationError, match="payload hash"):
        _decision(action_intents=(intent.model_copy(update={"payload_hash": _hash("other")}),))
    with pytest.raises(ValidationError, match="beat_ref"):
        _decision(action_intents=(intent.model_copy(update={"beat_ref": "beat:missing"}),))
    with pytest.raises(ValidationError, match="expression action"):
        _decision(action_intents=(intent.model_copy(update={"beat_ref": None}),))
    with pytest.raises(ValidationError, match="payload_ref"):
        _decision(action_intents=(intent.model_copy(update={"payload_ref": "payload:other"}),))


def test_intent_dependencies_must_exist_and_form_a_dag() -> None:
    first = ProposalActionIntent(
        intent_id="intent:one",
        kind="internal_note",
        layer="internal_state_transition",
        target="internal:one",
        payload_ref="payload:one",
        payload_hash=_hash("one"),
    )
    missing = first.model_copy(
        update={"intent_id": "intent:missing", "dependencies": ("intent:absent",)}
    )
    with pytest.raises(ValidationError, match="reference an intent"):
        _decision(proposed_changes=(), action_intents=(missing,))
    second = first.model_copy(update={"intent_id": "intent:two", "dependencies": ("intent:one",)})
    cyclic_first = first.model_copy(update={"dependencies": ("intent:two",)})
    with pytest.raises(ValidationError, match="acyclic"):
        _decision(proposed_changes=(), action_intents=(cyclic_first, second))


def test_continuation_is_restricted_to_registered_continuation_changes() -> None:
    next_payload_hash = _hash("inspection-action")
    change = TypedChange(
        change_id="change:media:1",
        kind="media_continuation",
        target_id="workflow:1",
        transition="render_to_inspect",
        evidence_refs=("result:render:1",),
        payload=_payload(
            "media_continuation.v1",
            {
                "workflow_step_id": "step:2",
                "opportunity_ref": "opportunity:1",
                "plan_ref": "plan:1",
                "artifact_ref": "artifact:1",
                "inspection_ref": None,
                "next_action_payload_hash": next_payload_hash,
            },
        ),
    )
    proposal = ContinuationProposal(
        proposal_id="proposal:continuation:1",
        trigger_ref="trigger:media:1",
        evaluated_world_revision=9,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(_evidence("result:render:1", "settled_external_result"),),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:inspect:1",
                kind="media_inspection",
                layer="media_action",
                target="media:inspector",
                payload_ref="payload:inspection:1",
                payload_hash=next_payload_hash,
                causal_change_id="change:media:1",
            ),
        ),
        confidence=10000,
        brief_rationale="Continue the frozen media workflow.",
        workflow_kind="media_continuation",
        upstream_result_refs=("result:render:1",),
        continuation_step="render_to_inspect",
    )
    assert proposal.proposal_kind == "continuation"
    with pytest.raises(ValidationError):
        ContinuationProposal.model_validate(
            {**proposal.model_dump(), "appraisals": [{"change_ref": "x", "summary": "x"}]}
        )
    with pytest.raises(ValidationError, match="media_continuation"):
        ContinuationProposal.model_validate(
            {
                **proposal.model_dump(),
                "proposed_changes": (
                    _expression_change()
                    .model_copy(update={"evidence_refs": ("result:render:1",)})
                    .model_dump(),
                ),
                "action_intents": (_reply_intent().model_dump(),),
            }
        )
    with pytest.raises(ValidationError, match="permits only media_inspection"):
        ContinuationProposal.model_validate(
            {
                **proposal.model_dump(),
                "action_intents": (
                    proposal.action_intents[0]
                    .model_copy(update={"kind": "read_only_lookup", "layer": "read_only_tool"})
                    .model_dump(),
                ),
            }
        )
    with pytest.raises(ValidationError, match="settled result"):
        ContinuationProposal.model_validate(
            {
                **proposal.model_dump(),
                "evidence_refs": (_evidence("result:render:1").model_dump(),),
            }
        )


def test_minimal_proposal_cannot_smuggle_world_changes_or_non_reply_actions() -> None:
    response_text = "I saw this; give me a moment."
    proposal = MinimalProposal(
        proposal_id="proposal:minimal:1",
        trigger_ref="trigger:timeout:1",
        evaluated_world_revision=7,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(_evidence(),),
        proposed_changes=(_minimal_expression_change(response_text),),
        action_intents=(_minimal_reply_intent(response_text),),
        confidence=4000,
        brief_rationale="Fallback acknowledges the message without world claims.",
        source_model_result="model-result:timeout:1",
        response_text=response_text,
        stance="defer",
        fact_claims=(),
    )
    assert proposal.proposal_kind == "minimal"
    with pytest.raises(ValidationError, match="fact_claims"):
        MinimalProposal.model_validate({**proposal.model_dump(), "fact_claims": ["fact:1"]})
    with pytest.raises(ValidationError, match="only expression"):
        MinimalProposal.model_validate(
            {
                **proposal.model_dump(),
                "action_intents": (),
                "proposed_changes": (
                    TypedChange(
                        change_id="change:thread:1",
                        kind="thread_transition",
                        target_id="thread:1",
                        transition="open",
                        payload=_payload(
                            "thread_transition.v1",
                            {
                                "thread_id": "thread:1",
                                "thread_kind": "conversation",
                                "importance": 5000,
                                "due": None,
                            },
                        ),
                    ).model_dump(),
                ),
            }
        )
    with pytest.raises(ValidationError, match="reply or followup"):
        MinimalProposal.model_validate(
            {
                **proposal.model_dump(),
                "action_intents": (
                    _minimal_reply_intent(response_text)
                    .model_copy(update={"kind": "proactive_message"})
                    .model_dump(),
                ),
            }
        )
    with pytest.raises(ValidationError, match="response_text must equal"):
        MinimalProposal.model_validate({**proposal.model_dump(), "response_text": "tampered"})
    with pytest.raises(ValidationError):
        MinimalProposal.model_validate({**proposal.model_dump(), "response_text": "x" * 4_097})


def test_public_validation_seam_revalidates_model_construct_instances() -> None:
    valid = MinimalProposal(
        proposal_id="proposal:minimal:constructed",
        trigger_ref="trigger:timeout:constructed",
        evaluated_world_revision=7,
        evidence_refs=(_evidence(),),
        proposed_changes=(),
        action_intents=(),
        confidence=1000,
        brief_rationale="Defer without creating an action.",
        source_model_result="model-result:timeout:constructed",
        response_text="One moment.",
        stance="defer",
    )
    raw_fields = {name: getattr(valid, name) for name in type(valid).model_fields}
    raw_fields["fact_claims"] = ("fact:smuggled",)
    bypassed = MinimalProposal.model_construct(**raw_fields)
    with pytest.raises(ValidationError, match="fact_claims"):
        validate_proposal_envelope(bypassed)
    with pytest.raises(ValidationError):
        validate_proposal_envelope({**valid.model_dump(), "proposal_kind": "unknown"})
    incomplete = MinimalProposal.model_construct(proposal_kind="minimal")
    with pytest.raises(ValueError, match="missing required fields"):
        validate_proposal_envelope(incomplete)


def test_canonical_payload_bounds_nesting_and_node_count_before_acceptance() -> None:
    deeply_nested = '{"a":' * 33 + "0" + "}" * 33
    with pytest.raises(ValidationError, match="nesting depth"):
        CanonicalTypedPayload(
            payload_schema="thread_transition.v1",
            payload_version=1,
            canonical_json=deeply_nested,
        )
    with pytest.raises(ValidationError, match="node count"):
        CanonicalTypedPayload.from_value(
            payload_schema="thread_transition.v1",
            value={"items": list(range(4_096))},
        )


def test_public_validation_preflights_total_size_and_due_time_awareness() -> None:
    valid = _decision()
    with pytest.raises(ValueError, match="maximum UTF-8 size"):
        validate_proposal_envelope({**valid.model_dump(), "brief_rationale": "x" * 270_000})
    with pytest.raises(ValidationError, match="timezone-aware"):
        ProposalActionIntent(
            intent_id="intent:naive",
            kind="internal_note",
            layer="internal_state_transition",
            target="internal:naive",
            payload_ref="payload:naive",
            payload_hash=_hash("naive"),
            due_window=(datetime(2026, 7, 15, 8, 0), NOW),
        )
