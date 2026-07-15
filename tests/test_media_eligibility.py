from companion_daemon.media_eligibility import MediaEligibilityRouter, PrivateExpressionBasis


def _snapshot():
    return {
        "activity": {"description": "回家后整理衣服"},
        "character": {"visible_physical_state": [{"cue_id": "post_run_sweat"}]},
        "relationship_media_context": {
            "active_exchange": {"event_id": "exchange:1"},
            "declared_display": {"event_id": "display:1", "recipient_ref": "user:1"},
        },
    }


def test_ordinary_event_cannot_be_cosmetically_promoted_to_private_expression() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot=_snapshot(),
        private_expression_basis=None,
        recipient_ref="user:1",
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
        recipient_ref="user:1",
    )

    assert decision.allowed
    assert decision.lane == "private_expression"
    assert decision.required_charge == "charged"


def test_lookalike_evidence_path_cannot_bypass_private_basis_root() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot={
            "relationship_media_context": {
                "declared_display_unrelated": {"event_id": "not-a-display"}
            }
        },
        private_expression_basis=PrivateExpressionBasis(
            kind="recipient_display",
            evidence_refs=("/relationship_media_context/declared_display_unrelated",),
            required_charge="charged",
        ),
        recipient_ref="user:1",
    )

    assert not decision.allowed
    assert decision.reason == "private_expression_basis_kind_conflict"


def test_empty_or_false_private_basis_evidence_cannot_open_the_private_lane() -> None:
    for value in (False, {}, None):
        decision = MediaEligibilityRouter().classify(
            family="character_media",
            privacy_ceiling="intimate",
            expression_charge_ceiling="charged",
            event_snapshot={"relationship_media_context": {"declared_display": value}},
            private_expression_basis=PrivateExpressionBasis(
                kind="recipient_display",
                evidence_refs=("/relationship_media_context/declared_display",),
                required_charge="charged",
            ),
            recipient_ref="user:1",
        )

        assert not decision.allowed
        assert decision.reason == "private_expression_basis_evidence_missing"


def test_recipient_display_must_link_to_the_frozen_recipient() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot={
            "relationship_media_context": {
                "declared_display": {"event_id": "display:1", "recipient_ref": "user:other"}
            }
        },
        private_expression_basis=PrivateExpressionBasis(
            kind="recipient_display",
            evidence_refs=("/relationship_media_context/declared_display",),
            required_charge="charged",
        ),
        recipient_ref="user:1",
    )

    assert not decision.allowed
    assert decision.reason == "private_expression_basis_schema_invalid"


def test_relational_basis_cannot_use_an_unlinked_private_exchange() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="subtle",
        event_snapshot={
            "relationship_media_context": {"active_exchange": {"event_id": "exchange:other"}}
        },
        private_expression_basis=PrivateExpressionBasis(
            kind="relational_turn",
            evidence_refs=("/relationship_media_context/active_exchange",),
        ),
        recipient_ref="user:1",
    )

    assert not decision.allowed
    assert decision.reason == "private_expression_basis_schema_invalid"
