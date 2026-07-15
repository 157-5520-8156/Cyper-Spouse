from companion_daemon.media_eligibility import MediaEligibilityRouter, PrivateExpressionBasis


def _snapshot():
    return {
        "activity": {"description": "回家后整理衣服"},
        "character": {"visible_physical_state": [{"cue_id": "post_run_sweat"}]},
        "relationship_media_context": {
            "active_exchange": {"event_id": "exchange:1"},
            "declared_display": {"event_id": "display:1"},
        },
    }


def test_ordinary_event_cannot_be_cosmetically_promoted_to_private_expression() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot=_snapshot(),
        private_expression_basis=None,
    )

    assert not decision.allowed
    assert decision.reason == "private_lane_unsupported_by_event"
    assert decision.details == "recommended_lane=personal_selfie"


def test_explicit_recipient_display_can_enter_private_expression_at_its_frozen_floor() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot=_snapshot(),
        private_expression_basis=PrivateExpressionBasis(
            kind="recipient_display",
            evidence_refs=("/relationship_media_context/declared_display",),
            required_charge="charged",
        ),
    )

    assert decision.allowed
    assert decision.lane == "private_expression"
    assert decision.required_charge == "charged"
