from companion_daemon.media_eligibility import (
    MediaEligibilityRouter,
    MediaLaneRecommendation,
    PrivateExpressionBasis,
)


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


def test_structured_visible_physical_state_can_ground_private_expression() -> None:
    decision = MediaEligibilityRouter().classify(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot={
            "character": {
                "visible_physical_state": {
                    "schema_version": "visible-physical-state-v1",
                    "observed_at": "2026-07-15T20:00:00+08:00",
                    "source_event_ids": ["event:run"],
                    "cues": [{"cue_id": "perspiration"}],
                }
            }
        },
        private_expression_basis=PrivateExpressionBasis(
            kind="embodied_state",
            evidence_refs=("/character/visible_physical_state",),
            required_charge="charged",
        ),
        recipient_ref="user:1",
    )

    assert decision.allowed


def test_alluring_life_can_be_event_grounded_without_claiming_exclusive_access() -> None:
    decision = MediaEligibilityRouter().classify_recommendation(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot=_snapshot(),
        private_expression_basis=None,
        recipient_ref="user:1",
        recommendation=MediaLaneRecommendation(
            lane="alluring_life",
            recipient_access="recipient_directed",
            attraction_expression="feminine",
        ),
        selected_expression_charge="charged",
        selected_capture_mode="known_companion",
        selected_share_intent="intimate_signal",
        selected_privacy="intimate",
        selected_address_mode="photographer_relational",
    )

    assert decision.allowed
    assert decision.lane == "alluring_life"


def test_exclusive_private_requires_frozen_recipient_proof_and_self_authorship() -> None:
    recommendation = MediaLaneRecommendation(
        lane="exclusive_private",
        recipient_access="recipient_exclusive",
        attraction_expression="charged",
    )
    common = dict(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="charged",
        event_snapshot=_snapshot(),
        recipient_ref="user:1",
        recommendation=recommendation,
        selected_expression_charge="charged",
        selected_share_intent="intimate_signal",
        selected_privacy="intimate",
        selected_address_mode="direct_recipient",
    )
    no_proof = MediaEligibilityRouter().classify_recommendation(
        **common,
        private_expression_basis=None,
        selected_capture_mode="mirror",
    )
    third_party = MediaEligibilityRouter().classify_recommendation(
        **common,
        private_expression_basis=PrivateExpressionBasis(
            kind="recipient_display",
            evidence_refs=("/relationship_media_context/declared_display",),
            required_charge="charged",
        ),
        selected_capture_mode="known_companion",
    )

    assert not no_proof.allowed
    assert no_proof.reason == "private_lane_unsupported_by_event"
    assert not third_party.allowed
    assert third_party.reason == "exclusive_lane_visual_contract_invalid"


def test_explicit_reserved_is_always_non_renderable() -> None:
    decision = MediaEligibilityRouter().classify_recommendation(
        family="character_media",
        privacy_ceiling="intimate",
        expression_charge_ceiling="veiled",
        event_snapshot=_snapshot(),
        private_expression_basis=None,
        recipient_ref="user:1",
        recommendation=MediaLaneRecommendation(
            lane="explicit_reserved",
            recipient_access="recipient_exclusive",
            attraction_expression="explicit_reserved",
        ),
        selected_expression_charge="veiled",
        selected_capture_mode="mirror",
        selected_share_intent="intimate_signal",
        selected_privacy="intimate",
        selected_address_mode="direct_recipient",
    )

    assert not decision.allowed
    assert decision.reason == "explicit_media_capability_disabled"


def test_ordinary_lane_cannot_claim_recipient_exclusive_access() -> None:
    decision = MediaEligibilityRouter().classify_recommendation(
        family="character_media",
        privacy_ceiling="personal",
        expression_charge_ceiling="none",
        event_snapshot=_snapshot(),
        private_expression_basis=None,
        recipient_ref="user:1",
        recommendation=MediaLaneRecommendation(
            lane="ordinary_life",
            recipient_access="recipient_exclusive",
            attraction_expression="none",
        ),
        selected_expression_charge="none",
        selected_capture_mode="character_front_camera",
        selected_share_intent="record",
        selected_privacy="personal",
        selected_address_mode="shared_attention",
    )

    assert not decision.allowed
    assert decision.reason == "ordinary_lane_contains_attraction_expression"
