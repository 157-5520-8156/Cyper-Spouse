"""Evidence-only lane eligibility for event media.

World owns whether an event is worth sharing.  This module only prevents an
ordinary event from being cosmetically re-labelled as private expression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from companion_daemon.media_suggestive_lane import (
    EXPLICIT_PRIVATE_LANE,
    PRIVATE_RENDER_LANES,
    SUGGESTIVE_PRIVATE_LANE,
)


PRIVATE_BASIS_KINDS = frozenset(
    {
        "relational_turn",
        "recipient_display",
        "embodied_state",
        "private_transition",
        "shared_ritual",
    }
)
CHARGE_RANK = {"none": 0, "subtle": 1, "charged": 2, "veiled": 3}

# These names describe what the recipient is being shown, rather than how
# much skin happens to be visible.  `explicit_reserved` is kept only to reject
# stale proposal payloads; current high private lanes are renderable through a
# separately configured provider capability.
MEDIA_LANES = frozenset(
    {
        "ordinary_life",
        "alluring_life",
        "exclusive_private",
        SUGGESTIVE_PRIVATE_LANE,
        EXPLICIT_PRIVATE_LANE,
        "explicit_reserved",
    }
)
RECIPIENT_ACCESS = frozenset({"ambient", "recipient_directed", "recipient_exclusive"})
ATTRACTION_EXPRESSIONS = frozenset(
    {
        "none",
        "feminine",
        "charged",
        "sexual_suggestive",
        "explicit_adult",
        "explicit_reserved",
    }
)
HIGH_PRIVATE_INTENT_BY_LANE = {
    SUGGESTIVE_PRIVATE_LANE: "sexual_suggestive",
    EXPLICIT_PRIVATE_LANE: "explicit_adult",
}


@dataclass(frozen=True)
class MediaLaneRecommendation:
    """The bounded semantic recommendation returned by the planning model."""

    lane: str
    recipient_access: str
    attraction_expression: str

    @classmethod
    def from_proposal(cls, proposal: Mapping[str, object]) -> "MediaLaneRecommendation":
        # MediaPlan payloads store this value as the compact contract itself;
        # planner proposals carry the same fields at top level.
        return cls(
            lane=str(proposal.get("lane") or proposal.get("media_lane") or ""),
            recipient_access=str(proposal.get("recipient_access") or ""),
            attraction_expression=str(proposal.get("attraction_expression") or ""),
        )

    def validate(self) -> str | None:
        if self.lane not in MEDIA_LANES:
            return "invalid_media_lane"
        if self.recipient_access not in RECIPIENT_ACCESS:
            return "invalid_recipient_access"
        if self.attraction_expression not in ATTRACTION_EXPRESSIONS:
            return "invalid_attraction_expression"
        return None


@dataclass(frozen=True)
class PrivateExpressionBasis:
    """One World-frozen, visual reason an event may enter private expression."""

    kind: str
    evidence_refs: tuple[str, ...]
    required_charge: str = "subtle"

    def validate(self, snapshot: Mapping[str, object], *, recipient_ref: str = "") -> str | None:
        if self.kind not in PRIVATE_BASIS_KINDS:
            return "unknown_private_expression_basis"
        if self.required_charge not in set(CHARGE_RANK) - {"none"}:
            return "invalid_private_expression_charge"
        if not self.evidence_refs:
            return "private_expression_basis_evidence_missing"
        roots = {
            "relational_turn": "/relationship_media_context/active_exchange",
            "recipient_display": "/relationship_media_context/declared_display",
            "embodied_state": "/character/visible_physical_state",
            "private_transition": "/activity/private_transition",
            "shared_ritual": "/relationship_media_context/shared_ritual",
        }
        root = roots[self.kind]
        if root not in self.evidence_refs:
            return "private_expression_basis_kind_conflict"
        root_value = _pointer(snapshot, root)
        if not _meaningful(root_value):
            return "private_expression_basis_evidence_missing"
        if any(not _meaningful(_pointer(snapshot, ref)) for ref in self.evidence_refs):
            return "private_expression_basis_evidence_missing"
        if not _basis_value_matches_kind(
            kind=self.kind,
            root_value=root_value,
            recipient_ref=recipient_ref,
        ):
            return "private_expression_basis_schema_invalid"
        return None

    def freeze(
        self, snapshot: Mapping[str, object], *, recipient_ref: str = ""
    ) -> "FrozenPrivateExpressionBasis":
        """Capture one validated, bounded proof for replay and inspection."""

        error = self.validate(snapshot, recipient_ref=recipient_ref)
        if error:
            raise ValueError(error)
        root = _BASIS_ROOTS[self.kind]
        return FrozenPrivateExpressionBasis(
            kind=self.kind,
            evidence_ref=root,
            evidence_value=_pointer(snapshot, root),
            required_charge=self.required_charge,
            recipient_ref=recipient_ref,
        )


@dataclass(frozen=True)
class FrozenPrivateExpressionBasis:
    """The exact event proof that justified a private v5 plan."""

    kind: str
    evidence_ref: str
    evidence_value: object
    required_charge: str
    recipient_ref: str

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "evidence_ref": self.evidence_ref,
            "evidence_value": self.evidence_value,
            "required_charge": self.required_charge,
            "recipient_ref": self.recipient_ref,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "FrozenPrivateExpressionBasis":
        if not isinstance(payload, dict):
            raise ValueError("private expression basis must be an object")
        result = cls(
            kind=str(payload.get("kind") or ""),
            evidence_ref=str(payload.get("evidence_ref") or ""),
            evidence_value=payload.get("evidence_value", _MISSING),
            required_charge=str(payload.get("required_charge") or ""),
            recipient_ref=str(payload.get("recipient_ref") or ""),
        )
        if result.validate_payload():
            raise ValueError("invalid frozen private expression basis")
        return result

    def validate_payload(self) -> str | None:
        if self.kind not in PRIVATE_BASIS_KINDS:
            return "unknown_private_expression_basis"
        if self.required_charge not in set(CHARGE_RANK) - {"none"}:
            return "invalid_private_expression_charge"
        root = _BASIS_ROOTS[self.kind]
        if self.evidence_ref != root:
            return "private_expression_basis_kind_conflict"
        if not _meaningful(self.evidence_value):
            return "private_expression_basis_evidence_missing"
        if not self.recipient_ref:
            return "private_expression_recipient_missing"
        if not _basis_value_matches_kind(
            kind=self.kind,
            root_value=self.evidence_value,
            recipient_ref=self.recipient_ref,
        ):
            return "private_expression_basis_schema_invalid"
        return None


@dataclass(frozen=True)
class MediaLaneDecision:
    lane: str
    allowed: bool
    reason: str = ""
    details: str = ""
    required_charge: str = "none"


class MediaEligibilityRouter:
    """Classify one frozen opportunity without selecting a shot or changing it."""

    def classify(
        self,
        *,
        family: str,
        privacy_ceiling: str,
        expression_charge_ceiling: str,
        event_snapshot: Mapping[str, object],
        private_expression_basis: PrivateExpressionBasis | None,
        recipient_ref: str = "",
    ) -> MediaLaneDecision:
        wants_private = privacy_ceiling == "intimate" or expression_charge_ceiling != "none"
        if family == "life_share":
            # The existing v5 intimate-life-share route is object/environment
            # only and is normalized before rendering; it is not character
            # private expression and therefore needs no presentation basis.
            return MediaLaneDecision(
                "intimate_life_share" if wants_private else "life_share",
                True,
            )
        if not wants_private:
            return MediaLaneDecision("personal_selfie", True)
        if not recipient_ref:
            return MediaLaneDecision(
                "private_expression", False, "private_expression_recipient_missing"
            )
        if private_expression_basis is None:
            return MediaLaneDecision(
                "private_expression",
                False,
                "private_lane_unsupported_by_event",
                "recommended_lane=personal_selfie",
            )
        error = private_expression_basis.validate(event_snapshot, recipient_ref=recipient_ref)
        if error:
            return MediaLaneDecision("private_expression", False, error)
        if (
            CHARGE_RANK[private_expression_basis.required_charge]
            > CHARGE_RANK[expression_charge_ceiling]
        ):
            return MediaLaneDecision(
                "private_expression", False, "private_expression_charge_ceiling_too_low"
            )
        return MediaLaneDecision(
            "private_expression", True, required_charge=private_expression_basis.required_charge
        )

    def classify_recommendation(
        self,
        *,
        family: str,
        privacy_ceiling: str,
        expression_charge_ceiling: str,
        event_snapshot: Mapping[str, object],
        private_expression_basis: PrivateExpressionBasis | None,
        recipient_ref: str,
        recommendation: MediaLaneRecommendation,
        selected_expression_charge: str,
        selected_capture_mode: str,
        selected_share_intent: str,
        selected_privacy: str,
        selected_address_mode: str,
        selected_interaction_bid: str = "",
        selected_attraction_mechanism: str | None = None,
        selected_coverage_mode: str | None = None,
    ) -> MediaLaneDecision:
        """Accept a proposed Lane only when its frozen visual contract supports it.

        This is deliberately called after the model has selected an indivisible
        v5 candidate: the router can verify the actual capture authorship and
        expression charge instead of trusting prose about them.
        """

        error = recommendation.validate()
        if error:
            return MediaLaneDecision(recommendation.lane or "unknown", False, error)
        if recommendation.lane == "explicit_reserved":
            return MediaLaneDecision(
                "explicit_reserved", False, "explicit_media_capability_disabled"
            )
        if recommendation.lane in PRIVATE_RENDER_LANES:
            high_lane = recommendation.lane
            if family != "character_media":
                return MediaLaneDecision(
                    high_lane, False, "media_lane_requires_character_media"
                )
            # A high provider route is not a cosmetic upgrade for an ordinary
            # selfie.  World must freeze a recipient-bound, event-grounded
            # private-expression basis before the planner may select one.
            # This is visual provenance, not the separate adult-authorization
            # decision owned by the upstream policy environment.
            private_grounding = self.classify(
                family=family,
                privacy_ceiling=privacy_ceiling,
                expression_charge_ceiling=expression_charge_ceiling,
                event_snapshot=event_snapshot,
                private_expression_basis=private_expression_basis,
                recipient_ref=recipient_ref,
            )
            if not private_grounding.allowed:
                return MediaLaneDecision(
                    high_lane,
                    False,
                    private_grounding.reason,
                    private_grounding.details,
                )
            intent_error = _high_private_intent_error(
                event_snapshot,
                lane=high_lane,
                recipient_ref=recipient_ref,
            )
            if intent_error:
                return MediaLaneDecision(high_lane, False, intent_error)
            expected_expression = (
                "sexual_suggestive"
                if high_lane == SUGGESTIVE_PRIVATE_LANE
                else "explicit_adult"
            )
            if (
                recommendation.recipient_access != "recipient_exclusive"
                or recommendation.attraction_expression != expected_expression
                or privacy_ceiling != "intimate"
                or selected_privacy != "intimate"
                or selected_share_intent != "intimate_signal"
                or selected_interaction_bid != "invite_desire"
                or selected_capture_mode not in {"character_front_camera", "mirror"}
                or selected_address_mode != "direct_recipient"
                or selected_expression_charge not in {"charged", "veiled"}
                or not selected_attraction_mechanism
                or selected_coverage_mode not in {"private_apparel", "strategic_cover"}
            ):
                return MediaLaneDecision(
                    high_lane, False, "private_render_lane_visual_contract_invalid"
                )
            if CHARGE_RANK[selected_expression_charge] < max(
                CHARGE_RANK["charged"], CHARGE_RANK[private_grounding.required_charge]
            ):
                return MediaLaneDecision(
                    high_lane, False, "private_render_charge_below_grounding_floor"
                )
            # Authorization and relation policy are intentionally upstream
            # responsibilities.  This router only verifies a coherent,
            # self-authored photographic contract; no model output can invent
            # its capture physics or recipient-directed visual semantics.
            return MediaLaneDecision(high_lane, True, required_charge="charged")
        if family != "character_media":
            return MediaLaneDecision(
                recommendation.lane, False, "media_lane_requires_character_media"
            )
        if selected_expression_charge not in CHARGE_RANK:
            return MediaLaneDecision(
                recommendation.lane, False, "invalid_candidate_expression_charge"
            )

        if recommendation.lane == "ordinary_life":
            if (
                recommendation.attraction_expression != "none"
                or recommendation.recipient_access == "recipient_exclusive"
                or selected_expression_charge != "none"
            ):
                return MediaLaneDecision(
                    "ordinary_life", False, "ordinary_lane_contains_attraction_expression"
                )
            return MediaLaneDecision("ordinary_life", True)

        if recommendation.lane == "alluring_life":
            if recommendation.recipient_access == "recipient_exclusive":
                return MediaLaneDecision(
                    "alluring_life", False, "alluring_lane_cannot_claim_exclusive_access"
                )
            if recommendation.attraction_expression not in {"feminine", "charged"}:
                return MediaLaneDecision(
                    "alluring_life", False, "alluring_lane_requires_attraction_expression"
                )
            if (
                not recipient_ref
                or privacy_ceiling != "intimate"
                or selected_privacy != "intimate"
                or selected_share_intent != "intimate_signal"
                or selected_expression_charge == "none"
                or CHARGE_RANK[selected_expression_charge] > CHARGE_RANK[expression_charge_ceiling]
            ):
                return MediaLaneDecision(
                    "alluring_life", False, "alluring_lane_visual_contract_invalid"
                )
            return MediaLaneDecision("alluring_life", True)

        # `exclusive_private` intentionally reuses the existing evidence-only
        # basis contract.  A private label is never granted by the model alone.
        legacy = self.classify(
            family=family,
            privacy_ceiling=privacy_ceiling,
            expression_charge_ceiling=expression_charge_ceiling,
            event_snapshot=event_snapshot,
            private_expression_basis=private_expression_basis,
            recipient_ref=recipient_ref,
        )
        if not legacy.allowed:
            return MediaLaneDecision("exclusive_private", False, legacy.reason, legacy.details)
        if recommendation.recipient_access != "recipient_exclusive":
            return MediaLaneDecision(
                "exclusive_private", False, "exclusive_lane_requires_recipient_access"
            )
        if recommendation.attraction_expression not in {"feminine", "charged"}:
            return MediaLaneDecision(
                "exclusive_private", False, "exclusive_lane_requires_attraction_expression"
            )
        if (
            selected_capture_mode not in {"character_front_camera", "mirror"}
            or selected_address_mode not in {"direct_recipient", "photographer_relational"}
            or selected_privacy != "intimate"
            or selected_share_intent != "intimate_signal"
            or CHARGE_RANK[selected_expression_charge] < CHARGE_RANK[legacy.required_charge]
        ):
            return MediaLaneDecision(
                "exclusive_private", False, "exclusive_lane_visual_contract_invalid"
            )
        return MediaLaneDecision("exclusive_private", True, required_charge=legacy.required_charge)


_MISSING = object()

_BASIS_ROOTS = {
    "relational_turn": "/relationship_media_context/active_exchange",
    "recipient_display": "/relationship_media_context/declared_display",
    "embodied_state": "/character/visible_physical_state",
    "private_transition": "/activity/private_transition",
    "shared_ritual": "/relationship_media_context/shared_ritual",
}


def _high_private_intent_error(
    snapshot: Mapping[str, object], *, lane: str, recipient_ref: str
) -> str | None:
    """Require an explicit world fact about *what* the private display means.

    ``recipient_display`` alone means that a character is intentionally
    showing something to one person; it does not imply sexual content.  The
    high rendering route therefore needs a discrete content intent alongside
    the event proof.  This is not an adult-consent check: upstream owns that
    separately.  It is a routing fact that prevents a normal affectionate
    image from being cosmetically re-labelled as high private media.
    """

    expected = HIGH_PRIVATE_INTENT_BY_LANE.get(lane)
    display = _pointer(snapshot, "/relationship_media_context/declared_display")
    if not isinstance(display, Mapping):
        return "high_private_intent_evidence_missing"
    if str(display.get("recipient_ref") or "").strip() != recipient_ref:
        return "high_private_intent_recipient_conflict"
    if str(display.get("media_intent") or "").strip() != expected:
        return "high_private_intent_mismatch"
    return None


def _meaningful(value: object) -> bool:
    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _basis_value_matches_kind(*, kind: str, root_value: object, recipient_ref: str) -> bool:
    if kind == "embodied_state":
        cues = root_value.get("cues") if isinstance(root_value, Mapping) else None
        return isinstance(cues, list) and any(
            isinstance(cue, Mapping) and str(cue.get("cue_id") or "").strip() for cue in cues
        )
    if not isinstance(root_value, Mapping):
        return False
    if kind in {"relational_turn", "recipient_display", "shared_ritual"}:
        if not str(root_value.get("event_id") or "").strip():
            return False
        linked_recipient = str(root_value.get("recipient_ref") or "").strip()
        return bool(linked_recipient) and linked_recipient == recipient_ref
    if kind == "private_transition":
        return bool(
            str(root_value.get("event_id") or "").strip()
            or str(root_value.get("kind") or "").strip()
        )
    return False


def _pointer(value: object, pointer: str) -> object:
    if not pointer.startswith("/"):
        return _MISSING
    current = value
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current
