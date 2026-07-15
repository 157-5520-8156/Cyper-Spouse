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


_MISSING = object()

_BASIS_ROOTS = {
    "relational_turn": "/relationship_media_context/active_exchange",
    "recipient_display": "/relationship_media_context/declared_display",
    "embodied_state": "/character/visible_physical_state",
    "private_transition": "/activity/private_transition",
    "shared_ritual": "/relationship_media_context/shared_ritual",
}


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
        return isinstance(root_value, list) and any(
            isinstance(cue, Mapping) and str(cue.get("cue_id") or "").strip() for cue in root_value
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
