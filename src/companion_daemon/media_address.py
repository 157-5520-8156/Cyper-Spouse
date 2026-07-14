"""Recipient-facing expression contracts for event media v5."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


ADDRESS_MODES = {
    "observational",
    "shared_attention",
    "direct_recipient",
    "consultative",
    "evidence_mediated",
    "photographer_relational",
    "memory_recall",
}
ENGAGEMENT_TACTICS = {
    "presence",
    "reveal",
    "demonstration",
    "question",
    "comparison",
    "contrast",
    "comic_hook",
    "celebration",
    "vulnerability",
    "reassurance",
    "coordination",
    "affection",
    "attraction",
    "nostalgia",
}
DISCLOSURE_MODES = {
    "open_context",
    "selective_focus",
    "partial_reveal",
    "unguarded_access",
    "polished_display",
    "evidence_first",
}
STAGING_DEGREES = {
    "unposed",
    "camera_aware",
    "lightly_arranged",
    "deliberately_posed",
    "privately_composed",
    "existing_artifact",
}
TEMPORAL_BEATS = {
    "anticipation",
    "mid_action",
    "just_after",
    "reaction",
    "held_for_response",
    "aftermath",
    "retrospective",
}
VISUAL_PRIORITIES = {
    "environment",
    "primary_evidence",
    "process",
    "character",
    "relationship",
}
EXPRESSION_CHARGES = {"none", "subtle", "charged", "veiled"}
ATTRACTION_MECHANISMS = {
    "direct_invitation",
    "playful_tease",
    "withheld_attention",
    "sensory_immediacy",
    "private_trust",
    "confident_display",
    "interrupted_transition",
    "close_proximity",
    "atmospheric_suggestion",
}

_BID_TACTICS = {
    "inform_status": {"presence", "demonstration"},
    "coordinate_next_step": {"coordination", "question", "demonstration"},
    "share_presence": {"presence"},
    "share_discovery": {"reveal", "demonstration", "comparison"},
    "invite_appreciation": {"reveal", "celebration"},
    "invite_opinion": {"question", "comparison"},
    "celebrate_together": {"celebration"},
    "invite_playful_exchange": {"comic_hook", "contrast"},
    "seek_validation": {"comic_hook", "contrast", "vulnerability"},
    "seek_care": {"vulnerability"},
    "offer_reassurance": {"reassurance"},
    "invite_closeness": {"affection"},
    "invite_desire": {"attraction"},
    "revisit_memory": {"nostalgia", "presence"},
}

MEDIA_ADDRESS_VERSION = "media-address-strategy-v1"


@dataclass(frozen=True)
class MediaAddressStrategy:
    address_mode: str
    engagement_tactic: str
    disclosure_mode: str
    staging_degree: str
    temporal_beat: str
    visual_priority: str
    expression_charge: str
    attraction_mechanism: str | None
    contract_signature: str
    version: str = MEDIA_ADDRESS_VERSION

    @classmethod
    def create(cls, **values: str | None) -> "MediaAddressStrategy":
        payload = {
            "address_mode": values.get("address_mode"),
            "engagement_tactic": values.get("engagement_tactic"),
            "disclosure_mode": values.get("disclosure_mode"),
            "staging_degree": values.get("staging_degree"),
            "temporal_beat": values.get("temporal_beat"),
            "visual_priority": values.get("visual_priority"),
            "expression_charge": values.get("expression_charge"),
            "attraction_mechanism": values.get("attraction_mechanism"),
        }
        _validate(payload)
        return cls(
            **payload,  # type: ignore[arg-type]
            contract_signature=_signature(payload),
        )

    def to_payload(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "MediaAddressStrategy":
        if not isinstance(value, dict):
            raise ValueError("media address strategy must be an object")
        payload = {
            "address_mode": str(value.get("address_mode") or ""),
            "engagement_tactic": str(value.get("engagement_tactic") or ""),
            "disclosure_mode": str(value.get("disclosure_mode") or ""),
            "staging_degree": str(value.get("staging_degree") or ""),
            "temporal_beat": str(value.get("temporal_beat") or ""),
            "visual_priority": str(value.get("visual_priority") or ""),
            "expression_charge": str(value.get("expression_charge") or ""),
            "attraction_mechanism": (
                str(value["attraction_mechanism"])
                if value.get("attraction_mechanism") is not None
                else None
            ),
        }
        _validate(payload)
        if str(value.get("version") or "") != MEDIA_ADDRESS_VERSION:
            raise ValueError("unsupported media address strategy version")
        if str(value.get("contract_signature") or "") != _signature(payload):
            raise ValueError("invalid media address strategy contract")
        return cls(
            **payload,  # type: ignore[arg-type]
            contract_signature=str(value["contract_signature"]),
        )

    def bid_compatibility_error(self, bid_id: str) -> str | None:
        tactics = _BID_TACTICS.get(bid_id)
        if tactics is None:
            return "unknown_interaction_bid"
        if self.engagement_tactic not in tactics:
            return "interaction_bid_address_conflict"
        return None


def _validate(value: dict[str, str | None]) -> None:
    fields = {
        "address_mode": ADDRESS_MODES,
        "engagement_tactic": ENGAGEMENT_TACTICS,
        "disclosure_mode": DISCLOSURE_MODES,
        "staging_degree": STAGING_DEGREES,
        "temporal_beat": TEMPORAL_BEATS,
        "visual_priority": VISUAL_PRIORITIES,
        "expression_charge": EXPRESSION_CHARGES,
    }
    for name, allowed in fields.items():
        if value.get(name) not in allowed:
            raise ValueError(f"invalid media address {name}")
    mechanism = value.get("attraction_mechanism")
    if mechanism is not None and mechanism not in ATTRACTION_MECHANISMS:
        raise ValueError("invalid attraction mechanism")
    if value["engagement_tactic"] == "attraction":
        if value["address_mode"] not in {"direct_recipient", "photographer_relational"}:
            raise ValueError("attraction requires recipient address")
        if value["expression_charge"] == "none" or mechanism is None:
            raise ValueError("attraction requires charged expression mechanism")
    elif mechanism is not None:
        raise ValueError("attraction mechanism requires attraction tactic")
    if value["expression_charge"] != "none" and value["engagement_tactic"] not in {
        "affection",
        "attraction",
    }:
        raise ValueError("expression charge requires intimate tactic")


def _signature(value: dict[str, str | None]) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
