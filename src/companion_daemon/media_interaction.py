"""Replayable social invitations attached to planned personal media."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

import yaml

from companion_daemon.media_domain import PRIVACY_RANK


DEFAULT_INTERACTION_CONFIG = Path("configs/media_interaction_templates.yaml")


@dataclass(frozen=True)
class MediaInteractionBid:
    """One instance of a hoped-for response, never a claim or obligation."""

    bid_id: str
    communicative_goal: str
    hoped_response: str
    response_pressure: str
    audience_ref: str = ""
    minimum_privacy: str = "ordinary"
    contract_signature: str = ""

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        *,
        bid_id: str,
        communicative_goal: str,
        hoped_response: str,
        response_pressure: str,
        audience_ref: str,
        minimum_privacy: str,
    ) -> "MediaInteractionBid":
        signature = _bid_signature(
            bid_id,
            communicative_goal,
            hoped_response,
            response_pressure,
            audience_ref,
            minimum_privacy,
        )
        return cls(
            bid_id=bid_id,
            communicative_goal=communicative_goal,
            hoped_response=hoped_response,
            response_pressure=response_pressure,
            audience_ref=audience_ref,
            minimum_privacy=minimum_privacy,
            contract_signature=signature,
        )

    @classmethod
    def from_payload(cls, value: object) -> "MediaInteractionBid":
        if not isinstance(value, dict):
            raise ValueError("media interaction bid must be an object")
        bid = cls(
            bid_id=str(value.get("bid_id") or "").strip(),
            communicative_goal=str(value.get("communicative_goal") or "").strip(),
            hoped_response=str(value.get("hoped_response") or "").strip(),
            response_pressure=str(value.get("response_pressure") or "").strip(),
            audience_ref=str(value.get("audience_ref") or "").strip(),
            minimum_privacy=str(value.get("minimum_privacy") or "ordinary").strip(),
            contract_signature=str(value.get("contract_signature") or "").strip(),
        )
        if not all((bid.bid_id, bid.communicative_goal, bid.hoped_response)):
            raise ValueError("invalid media interaction bid identity")
        if bid.response_pressure not in {"none", "low", "medium"}:
            raise ValueError("invalid media interaction bid pressure")
        if bid.minimum_privacy not in PRIVACY_RANK:
            raise ValueError("invalid media interaction bid privacy")
        if bid.contract_signature != _bid_signature(
            bid.bid_id,
            bid.communicative_goal,
            bid.hoped_response,
            bid.response_pressure,
            bid.audience_ref,
            bid.minimum_privacy,
        ):
            raise ValueError("invalid media interaction bid contract")
        return bid


@lru_cache(maxsize=8)
def load_interaction_catalog(
    path: Path = DEFAULT_INTERACTION_CONFIG,
) -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    bids = raw.get("interaction_bids")
    if not isinstance(bids, dict):
        raise ValueError("invalid media interaction catalog")
    catalog = {
        str(key): dict(value)
        for key, value in bids.items()
        if isinstance(value, dict)
    }
    for goal, value in catalog.items():
        if not goal or not str(value.get("hoped_response") or ""):
            raise ValueError(f"incomplete media interaction template: {goal}")
        if str(value.get("minimum_privacy") or "ordinary") not in PRIVACY_RANK:
            raise ValueError(f"invalid media interaction privacy: {goal}")
        if str(value.get("response_pressure") or "") not in {"none", "low", "medium"}:
            raise ValueError(f"media interaction contract mismatch: {goal}")
    return catalog


def _bid_signature(
    bid_id: str,
    communicative_goal: str,
    hoped_response: str,
    response_pressure: str,
    audience_ref: str,
    minimum_privacy: str,
) -> str:
    payload = (
        bid_id,
        communicative_goal,
        hoped_response,
        response_pressure,
        audience_ref,
        minimum_privacy,
    )
    return sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
