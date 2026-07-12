from datetime import datetime, timezone

from companion_daemon.conversation_cadence import ConversationCadence, FrozenTurnContext
from companion_daemon.model_call_policy import (
    CandidateGroundingSignals,
    GroundingAuditRisk,
    ModelCallRequest,
    ProviderCircuitState,
    TurnModelCallBudget,
)


def _turn(heat: str) -> FrozenTurnContext:
    return FrozenTurnContext(
        turn_id=f"turn-{heat}",
        world_id="world-1",
        user_id="user-1",
        observed_at=datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc),
        cadence=ConversationCadence(
            heat=heat,
            observed_gap_seconds=10.0 if heat == "hot" else None,
            alternating_turns=4 if heat == "hot" else 0,
            reason="test_fixture",
        ),
    )


def test_hot_fact_free_reply_has_one_fast_model_call_without_independent_audit() -> None:
    signals = CandidateGroundingSignals(reply_text="嗯，我在听。")
    risk = GroundingAuditRisk().assess(signals)

    decision = TurnModelCallBudget().decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="reply", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert decision.allowed is True
    assert decision.max_calls == 1
    assert decision.soft_timeout_seconds == 6.0
    assert decision.requires_independent_audit is False
    assert decision.hard_invariants_required is True
    assert decision.reason == "hot_fact_free_reply"


def test_factual_candidate_requires_independent_audit_and_a_second_bounded_call() -> None:
    risk = GroundingAuditRisk().assess(
        CandidateGroundingSignals(
            reply_text="我昨天下午去上课了。",
            claims=("昨天下午去上课",),
            mentioned_event_ids=("outcome:class",),
            has_factual_language=True,
        )
    )

    budget = TurnModelCallBudget()
    generation = budget.decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="reply", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )
    decision = budget.decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="reply_audit", calls_used=1),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert risk.risk == "elevated"
    assert risk.reasons == ("claims", "mentioned_events", "factual_language")
    assert generation.reason == "grounded_reply_generation_within_budget"
    assert decision.allowed is True
    assert decision.max_calls == 2
    assert decision.requires_independent_audit is True
    assert decision.reason == "grounding_audit_within_budget"


def test_ambiguous_factual_turn_bounds_appraisal_reply_and_audit_to_three_calls() -> None:
    risk = GroundingAuditRisk().assess(
        CandidateGroundingSignals(
            reply_text="你上次说周五要交稿。",
            mentioned_event_ids=("message:deadline",),
            has_factual_language=True,
        )
    )
    budget = TurnModelCallBudget()

    appraisal = budget.decide(
        turn=_turn("warm"),
        request=ModelCallRequest(
            purpose="interaction_appraisal", calls_used=0, ambiguous=True
        ),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )
    audit = budget.decide(
        turn=_turn("warm"),
        request=ModelCallRequest(purpose="reply_audit", calls_used=2, ambiguous=True),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )
    exhausted = budget.decide(
        turn=_turn("warm"),
        request=ModelCallRequest(purpose="reply_repair", calls_used=3, ambiguous=True),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert appraisal.allowed is True
    assert appraisal.max_calls == 3
    assert audit.allowed is True
    assert audit.max_calls == 3
    assert exhausted.allowed is False
    assert exhausted.reason == "turn_model_call_budget_exhausted"


def test_frozen_hot_warm_and_cold_cadence_choose_distinct_soft_timeouts() -> None:
    policy = TurnModelCallBudget()
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="好。"))

    decisions = [
        policy.decide(
            turn=_turn(heat),
            request=ModelCallRequest(purpose="reply", calls_used=0),
            grounding=risk,
            circuit=ProviderCircuitState.closed(),
        )
        for heat in ("hot", "warm", "cold")
    ]

    assert [item.soft_timeout_seconds for item in decisions] == [6.0, 10.0, 15.0]
    assert [item.max_calls for item in decisions] == [1, 1, 1]


def test_fact_free_candidate_cannot_spend_an_unnecessary_independent_audit_call() -> None:
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="我在听。"))

    decision = TurnModelCallBudget().decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="reply_audit", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert decision.allowed is False
    assert decision.reason == "independent_audit_not_required"
    assert decision.hard_invariants_required is True


def test_open_provider_circuit_skips_every_call_during_consecutive_outage_turns() -> None:
    policy = TurnModelCallBudget()
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="我在听。"))

    decisions = [
        policy.decide(
            turn=_turn("hot"),
            request=ModelCallRequest(purpose="reply", calls_used=0),
            grounding=risk,
            circuit=ProviderCircuitState.open(),
        )
        for _ in range(5)
    ]

    assert all(item.allowed is False for item in decisions)
    assert all(item.soft_timeout_seconds == 0.0 for item in decisions)
    assert all(item.reason == "provider_circuit_open_use_local_fallback" for item in decisions)
    assert all(item.hard_invariants_required is True for item in decisions)


def test_half_open_provider_allows_only_one_explicit_recovery_probe() -> None:
    policy = TurnModelCallBudget()
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="好。"))

    ordinary = policy.decide(
        turn=_turn("warm"),
        request=ModelCallRequest(purpose="reply", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.half_open(),
    )
    probe = policy.decide(
        turn=_turn("warm"),
        request=ModelCallRequest(purpose="reply", calls_used=0, recovery_probe=True),
        grounding=risk,
        circuit=ProviderCircuitState.half_open(),
    )

    assert ordinary.allowed is False
    assert ordinary.reason == "provider_recovery_probe_required"
    assert probe.allowed is True
    assert probe.reason == "provider_recovery_probe"


def test_appraisal_call_requires_an_ambiguous_turn_and_unknown_purposes_fail_closed() -> None:
    policy = TurnModelCallBudget()
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="好。"))

    unnecessary_appraisal = policy.decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="interaction_appraisal", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )
    unknown = policy.decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="unclassified", calls_used=0),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert unnecessary_appraisal.allowed is False
    assert unnecessary_appraisal.reason == "appraisal_not_required"
    assert unknown.allowed is False
    assert unknown.reason == "unsupported_model_call_purpose"


def test_proposed_external_action_alone_requires_independent_audit() -> None:
    risk = GroundingAuditRisk().assess(
        CandidateGroundingSignals(
            reply_text="我可以帮你发出去。",
            proposed_action_ids=("action:send-message",),
        )
    )

    assert risk.requires_independent_audit is True
    assert risk.reasons == ("proposed_actions",)
    assert risk.hard_invariants_required is True


def test_real_spend_remaining_budget_can_force_a_hard_invariant_fallback() -> None:
    risk = GroundingAuditRisk().assess(
        CandidateGroundingSignals(reply_text="我昨天去上课了。", has_factual_language=True)
    )

    decision = TurnModelCallBudget().decide(
        turn=_turn("hot"),
        request=ModelCallRequest(
            purpose="reply_audit",
            calls_used=1,
            remaining_budget_cny=0.001,
            estimated_call_cost_cny=0.01,
        ),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert decision.allowed is False
    assert decision.reason == "monetary_budget_exhausted_use_local_fallback"
    assert decision.hard_invariants_required is True


def test_wording_repair_is_available_cold_but_never_adds_a_second_hot_call() -> None:
    risk = GroundingAuditRisk().assess(CandidateGroundingSignals(reply_text="好。"))
    policy = TurnModelCallBudget()

    hot = policy.decide(
        turn=_turn("hot"),
        request=ModelCallRequest(purpose="reply_repair", calls_used=1),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )
    cold = policy.decide(
        turn=_turn("cold"),
        request=ModelCallRequest(purpose="reply_repair", calls_used=1),
        grounding=risk,
        circuit=ProviderCircuitState.closed(),
    )

    assert hot.allowed is False
    assert hot.max_calls == 1
    assert cold.allowed is True
    assert cold.max_calls == 2
