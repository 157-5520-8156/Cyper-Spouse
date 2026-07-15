"""Evidence-only lane eligibility for event media.

World owns whether an event is worth sharing.  This module only prevents an
ordinary event from being cosmetically re-labelled as private expression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


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


@dataclass(frozen=True)
class PrivateExpressionBasis:
    """One World-frozen, visual reason an event may enter private expression."""

    kind: str
    evidence_refs: tuple[str, ...]
    required_charge: str = "subtle"

    def validate(self, snapshot: Mapping[str, object]) -> str | None:
        if self.kind not in PRIVATE_BASIS_KINDS:
            return "unknown_private_expression_basis"
        if self.required_charge not in set(CHARGE_RANK) - {"none"}:
            return "invalid_private_expression_charge"
        if not self.evidence_refs or any(
            _pointer(snapshot, ref) is _MISSING for ref in self.evidence_refs
        ):
            return "private_expression_basis_evidence_missing"
        roots = {
            "relational_turn": "/relationship_media_context/active_exchange",
            "recipient_display": "/relationship_media_context/declared_display",
            "embodied_state": "/character/visible_physical_state",
            "private_transition": "/activity/private_transition",
            "shared_ritual": "/relationship_media_context/shared_ritual",
        }
        if not any(ref.startswith(roots[self.kind]) for ref in self.evidence_refs):
            return "private_expression_basis_kind_conflict"
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
        if private_expression_basis is None:
            return MediaLaneDecision(
                "private_expression",
                False,
                "private_lane_unsupported_by_event",
                "recommended_lane=personal_selfie",
            )
        error = private_expression_basis.validate(event_snapshot)
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


_MISSING = object()


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
