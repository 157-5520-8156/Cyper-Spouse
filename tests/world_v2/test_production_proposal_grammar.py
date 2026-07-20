"""Executable closure gate for production ``DecisionProposal`` grammar."""

from __future__ import annotations

import ast
import hashlib
import inspect
from types import MappingProxyType

import pytest

from companion_daemon.world_v2 import production_turn_application
from companion_daemon.world_v2 import production_proposal_grammar as grammar_module
from companion_daemon.world_v2.production_proposal_grammar import (
    PRODUCTION_PROPOSAL_GRAMMARS,
    ProductionProposalGrammarError,
    assert_production_proposal_grammar_coverage,
    production_proposal_grammar,
)
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    MinimalProposal,
    ProposalActionIntent,
    TypedChange,
)


def _hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _change(
    kind: str,
    transition: str,
    *,
    appraisal_change_refs: list[str] | None = None,
    change_id: str | None = None,
) -> TypedChange:
    payloads: dict[str, dict[str, object]] = {
        "expression_plan_transition": {
            "plan_id": "plan:1",
            "overall_intent": "reply",
            "ordering_policy": "dependencies",
            "terminal_policy": "settle",
            "beat_drafts": [
                {
                    "beat_id": "beat:1",
                    "inline_text": "hi",
                    "materialized_payload_ref": "payload:1",
                    "payload_hash": _hash("hi"),
                    "content_type": "text/plain",
                    "dependency_beat_ids": [],
                    "delay_window": None,
                    "cancel_policy": "cancel",
                    "reconsider_policy": "reconsider",
                    "merge_policy": "never",
                }
            ],
        },
        "appraisal_transition": {
            "appraisal_id": "appraisal:1",
            "meaning_candidates": [{"meaning": "disappointment", "confidence": 5000}],
            "attribution": "user",
            "severity": 5000,
            "confidence": 5000,
            "expiry": None,
        },
        "affect_transition": {
            "episode_id": "affect:1",
            "appraisal_change_refs": (
                appraisal_change_refs
                if appraisal_change_refs is not None
                else ["change:appraisal_transition"]
            ),
            "component_deltas": [{"name": "hurt", "value": 5000}],
            "decay_config": {
                "object_ref": "policy:decay",
                "schema_version": "test.1",
                "payload_hash": _hash("decay"),
            },
            "residue_config": {
                "object_ref": "policy:residue",
                "schema_version": "test.1",
                "payload_hash": _hash("residue"),
            },
        },
        "outcome_settlement": {
            "outcome_proposal_id": "proposal:outcome",
            "candidate_result_ref": "candidate:1",
            "result_id": "result:1",
            "entity_id": "occurrence:1",
            "entity_revision": 0,
            "observations": [
                {
                    "ref_id": "observation:1",
                    "source_world_revision": 1,
                    "immutable_hash": _hash("obs"),
                }
            ],
            "result_payload": {
                "object_ref": "payload:result",
                "schema_version": "test.1",
                "payload_hash": _hash("result"),
            },
        },
    }
    target = {
        "expression_plan_transition": "plan:1",
        "appraisal_transition": "appraisal:1",
        "affect_transition": "affect:1",
        "outcome_settlement": "occurrence:1",
    }[kind]
    return TypedChange(
        change_id=change_id or f"change:{kind}",
        kind=kind,
        target_id=target,
        transition=transition,
        payload=CanonicalTypedPayload.from_value(payload_schema=f"{kind}.v1", value=payloads[kind]),
    )


def _decision(
    change: TypedChange | None, *, action: bool = False, affect: bool = False
) -> DecisionProposal:
    changes = () if change is None else (change,)
    actions = ()
    if action:
        actions = (
            ProposalActionIntent(
                intent_id="intent:1",
                kind="reply",
                layer="external_action",
                target="user:1",
                payload_ref="payload:1",
                payload_hash=_hash("hi"),
                causal_change_id=change.change_id,
                beat_ref="beat:1" if change.kind == "expression_plan_transition" else None,
            ),
        )
    return DecisionProposal(
        proposal_id="proposal:1",
        trigger_ref="trigger:1",
        evaluated_world_revision=1,
        evidence_refs=(),
        proposed_changes=changes,
        action_intents=actions,
        confidence=5000,
        brief_rationale="bounded test decision",
        affect_decision="propose" if affect else "no_change",
        behavior_tendency="observe",
        stance="neutral",
        display_strategy="private",
    )


def _minimal_reply() -> MinimalProposal:
    change = _change("expression_plan_transition", "accept")
    return MinimalProposal(
        proposal_id="proposal:minimal",
        trigger_ref="trigger:1",
        evaluated_world_revision=1,
        evidence_refs=(),
        proposed_changes=(change,),
        action_intents=(
            ProposalActionIntent(
                intent_id="intent:minimal",
                kind="reply",
                layer="external_action",
                target="user:1",
                payload_ref="payload:1",
                payload_hash=_hash("hi"),
                causal_change_id=change.change_id,
                beat_ref="beat:1",
            ),
        ),
        confidence=5000,
        brief_rationale="minimal reply",
        source_model_result="model-result:1",
        response_text="hi",
        stance="acknowledge_briefly",
    )


def _interaction_decision(
    changes: tuple[TypedChange, ...],
    *,
    affect_decision: str | None = None,
    action: bool = False,
) -> DecisionProposal:
    actions = (
        (
            ProposalActionIntent(
                intent_id="intent:interaction-smuggle",
                kind="tool_query",
                layer="read_only_tool",
                target="user:1",
                payload_ref="payload:1",
                payload_hash=_hash("hi"),
                causal_change_id=changes[0].change_id if changes else None,
            ),
        )
        if action
        else ()
    )
    resolved_affect_decision = affect_decision or (
        "propose" if any(item.kind == "affect_transition" for item in changes) else "no_change"
    )
    return DecisionProposal(
        proposal_id="proposal:interaction-composite",
        trigger_ref="trigger:1",
        evaluated_world_revision=1,
        evidence_refs=(),
        proposed_changes=changes,
        action_intents=actions,
        confidence=7000,
        brief_rationale="bounded same-turn appraisal and optional affect",
        affect_decision=resolved_affect_decision,
        behavior_tendency="consider",
        stance="neutral",
        display_strategy="private",
    )


def test_catalogue_is_closed_and_every_reachable_change_has_all_three_authority_descriptors() -> (
    None
):
    assert_production_proposal_grammar_coverage()
    assert set(PRODUCTION_PROPOSAL_GRAMMARS) == {
        "chat_reply",
        "interaction_appraisal",
        "settled_world_appraisal",
        "silence_appraisal",
        "plan_disruption_appraisal",
        "affect",
        "relationship",
        "outcome",
        "interaction_bid",
        "proactive",
        "quick_reaction",
    }
    for grammar in PRODUCTION_PROPOSAL_GRAMMARS.values():
        for capability in grammar.capabilities:
            assert capability.compiler_ref
            assert capability.manifest_ref
            assert capability.reverse_verifier_ref


def test_catalogue_is_read_only_and_replacing_its_public_view_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(TypeError):
        PRODUCTION_PROPOSAL_GRAMMARS["chat_reply"] = PRODUCTION_PROPOSAL_GRAMMARS["outcome"]  # type: ignore[index]
    monkeypatch.setattr(grammar_module, "PRODUCTION_PROPOSAL_GRAMMARS", MappingProxyType({}))
    with pytest.raises(RuntimeError, match="public view was replaced"):
        assert_production_proposal_grammar_coverage()


@pytest.mark.parametrize(
    ("lane", "change", "action", "affect"),
    [
        ("chat_reply", _change("expression_plan_transition", "accept"), True, False),
        ("interaction_appraisal", _change("appraisal_transition", "activate"), False, False),
        ("settled_world_appraisal", _change("appraisal_transition", "activate"), False, False),
        ("silence_appraisal", _change("appraisal_transition", "activate"), False, False),
        ("plan_disruption_appraisal", _change("appraisal_transition", "activate"), False, False),
        ("affect", _change("affect_transition", "open"), False, True),
        ("outcome", _change("outcome_settlement", "settle"), False, False),
    ],
)
def test_each_production_lane_accepts_only_its_specialized_change(
    lane: str, change: TypedChange, action: bool, affect: bool
) -> None:
    production_proposal_grammar(lane).validate(_decision(change, action=action, affect=affect))  # type: ignore[arg-type]


def test_interaction_appraisal_accepts_one_appraisal_with_one_exactly_bound_affect() -> None:
    appraisal = _change("appraisal_transition", "activate")
    affect = _change(
        "affect_transition",
        "open",
        appraisal_change_refs=[appraisal.change_id],
    )
    production_proposal_grammar("interaction_appraisal").validate(
        _interaction_decision((appraisal, affect))
    )


def test_interaction_appraisal_preserves_appraisal_only_no_affect_decision() -> None:
    appraisal = _change("appraisal_transition", "activate")
    production_proposal_grammar("interaction_appraisal").validate(
        _interaction_decision((appraisal,))
    )


@pytest.mark.parametrize(
    ("changes", "action", "error"),
    [
        (
            (_change("affect_transition", "open"),),
            False,
            "interaction_appraisal_count_invalid",
        ),
        (
            (
                _change("appraisal_transition", "activate"),
                _change(
                    "affect_transition",
                    "open",
                    appraisal_change_refs=["change:wrong-appraisal"],
                ),
            ),
            False,
            "interaction_affect_appraisal_binding_invalid",
        ),
        (
            (
                _change("appraisal_transition", "activate"),
                _change(
                    "affect_transition",
                    "open",
                    appraisal_change_refs=[
                        "change:appraisal_transition",
                        "change:unrelated-appraisal",
                    ],
                ),
            ),
            False,
            "interaction_affect_appraisal_binding_invalid",
        ),
        (
            (
                _change("appraisal_transition", "activate"),
                _change(
                    "appraisal_transition",
                    "activate",
                    change_id="change:appraisal_transition:duplicate",
                ),
            ),
            False,
            "interaction_appraisal_count_invalid",
        ),
        (
            (
                _change("appraisal_transition", "activate"),
                _change("expression_plan_transition", "accept"),
            ),
            False,
            "interaction_change_not_reachable",
        ),
        (
            (_change("appraisal_transition", "activate"),),
            True,
            "action_not_reachable",
        ),
    ],
)
def test_interaction_appraisal_rejects_every_unbound_or_unauthorized_composite(
    changes: tuple[TypedChange, ...],
    action: bool,
    error: str,
) -> None:
    with pytest.raises(ProductionProposalGrammarError, match=error):
        production_proposal_grammar("interaction_appraisal").validate(
            _interaction_decision(changes, action=action)
        )


def test_interaction_appraisal_rejects_affect_decision_mismatches_defensively() -> None:
    appraisal = _change("appraisal_transition", "activate")
    affect = _change("affect_transition", "open", appraisal_change_refs=[appraisal.change_id])
    with_affect = _interaction_decision((appraisal, affect)).model_copy(
        update={"affect_decision": "no_change"}
    )
    without_affect = _interaction_decision((appraisal,)).model_copy(
        update={"affect_decision": "propose"}
    )
    for proposal in (with_affect, without_affect):
        with pytest.raises(
            ProductionProposalGrammarError,
            match="interaction_affect_decision_invalid",
        ):
            production_proposal_grammar("interaction_appraisal").validate(proposal)


def test_interaction_appraisal_rejects_multiple_affects_defensively() -> None:
    appraisal = _change("appraisal_transition", "activate")
    affect = _change("affect_transition", "open", appraisal_change_refs=[appraisal.change_id])
    proposal = _interaction_decision((appraisal, affect)).model_copy(
        update={
            "proposed_changes": (
                appraisal,
                affect,
                _change(
                    "affect_transition",
                    "open",
                    appraisal_change_refs=[appraisal.change_id],
                    change_id="change:affect_transition:duplicate",
                ),
            )
        }
    )
    with pytest.raises(
        ProductionProposalGrammarError,
        match="interaction_affect_count_invalid",
    ):
        production_proposal_grammar("interaction_appraisal").validate(proposal)


def test_every_other_lane_rejects_the_source_bound_appraisal_affect_composite() -> None:
    appraisal = _change("appraisal_transition", "activate")
    affect = _change("affect_transition", "open", appraisal_change_refs=[appraisal.change_id])
    proposal = _interaction_decision((appraisal, affect))
    for lane in (
        "chat_reply",
        "settled_world_appraisal",
        "affect",
        "relationship",
        "outcome",
        "interaction_bid",
        "proactive",
    ):
        with pytest.raises(ProductionProposalGrammarError, match="change_count_not_reachable"):
            production_proposal_grammar(lane).validate(proposal)  # type: ignore[arg-type]


def test_grammar_rejects_valid_but_unreachable_changes_and_no_change() -> None:
    with pytest.raises(ProductionProposalGrammarError, match="change_not_reachable"):
        production_proposal_grammar("interaction_appraisal").validate(
            _decision(_change("expression_plan_transition", "accept"), action=True)
        )
    with pytest.raises(ProductionProposalGrammarError, match="no_change_not_reachable"):
        production_proposal_grammar("outcome").validate(_decision(None))
    with pytest.raises(ProductionProposalGrammarError, match="minimal_proposal_not_reachable"):
        production_proposal_grammar("affect").validate(_minimal_reply())


def test_production_composition_cannot_construct_an_ungrammared_deliberation() -> None:
    """Static gate: all five production calls must go through the factory."""

    tree = ast.parse(inspect.getsource(production_turn_application))
    direct_deliberation_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "Deliberation"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "Deliberation"
        )
    ]
    assert not direct_deliberation_calls
    direct_deliberation_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "deliberation"
        and any(item.name == "Deliberation" for item in node.names)
    ]
    assert not direct_deliberation_imports
    lanes = {
        keyword.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "compose_production_deliberation"
        for keyword in node.keywords
        if keyword.arg == "lane_id"
        and isinstance(keyword.value, ast.Constant)
        and isinstance(keyword.value.value, str)
    }
    # ``quick_reaction`` deliberately owns no Deliberation/capsule stack: its
    # bounded local gate produces the proposal and QuickReactionWorker
    # validates it against production_proposal_grammar("quick_reaction")
    # before the audit is recorded (covered by the quick-reaction vertical).
    assert lanes == set(PRODUCTION_PROPOSAL_GRAMMARS) - {"quick_reaction"}
