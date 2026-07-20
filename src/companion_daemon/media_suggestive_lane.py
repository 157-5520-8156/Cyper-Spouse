"""Frozen contract for the dedicated, adult-suggestive media lane.

This module intentionally does not contain a model adapter or a free-form
prompt.  It verifies that a World-authorized, adult fictional private moment
may use the dedicated rendering route, while reusing the ordinary media
planner's address, camera, subject, embodiment and inspection contracts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import yaml


SUGGESTIVE_PRIVATE_LANE = "suggestive_private"
EXPLICIT_PRIVATE_LANE = "explicit_private"
PRIVATE_RENDER_LANES = frozenset({SUGGESTIVE_PRIVATE_LANE, EXPLICIT_PRIVATE_LANE})
SPECIALIZED_SUGGESTIVE_ROUTE = "adult_suggestive"
SPECIALIZED_EXPLICIT_ROUTE = "adult_explicit"
SUGGESTIVE_AUTHORIZATION_VERSION = "suggestive-media-authorization-v1"
SUGGESTIVE_CONTRACT_VERSION = "suggestive-private-contract-v1"
PRIVATE_RENDER_CONTRACT_VERSION = "private-render-contract-v1"
PRIVATE_FLAIR_VERSION = "private-flair-v1"
SUGGESTIVE_CATALOG_VERSION = "suggestive-private-matrix-v1"
DEFAULT_SUGGESTIVE_CATALOG = Path("configs/media_suggestive_private_templates.yaml")

# These are semantic, mutually-exclusive principal explanations.  The visual
# implementation still comes from the existing address/embodiment matrices.
SUGGESTIVE_GROUNDING_KINDS = frozenset(
    {
        "relational_escalation",
        "recipient_display",
        "private_attire",
        "embodied_aftereffect",
        "private_transition",
        "shared_ritual",
        "private_environment",
    }
)
SUGGESTIVE_MECHANISMS = frozenset(
    {
        "direct_invitation",
        "playful_tease",
        "withheld_attention",
        "sensory_immediacy",
        "private_trust",
        "confident_display",
        "interrupted_transition",
        "close_proximity",
    }
)
SUGGESTIVE_FRAMING_MODES = frozenset(
    {"conversational_close", "contextual_body", "whole_person_private"}
)
SUGGESTIVE_COVERAGE_MODES = frozenset({"private_apparel", "strategic_cover"})
PRIVATE_FACIAL_PROFILES = frozenset(
    {
        "natural_private",
        "playful_sensual",
        "withheld_desire",
        "breathless_desire",
        "heightened_ecstasy",
    }
)


@dataclass(frozen=True)
class PrivateRenderContract:
    """Frozen rendering intent for a high private Media Lane.

    This is deliberately narrower than an upstream policy or consent decision.
    The image machine receives an already permitted lane and only freezes the
    visual candidate and renderer capability necessary to replay it.  The
    legacy ``SuggestivePrivateContract`` below remains readable for historical
    plans that embedded an old World authorization.
    """

    lane: str
    attraction_mechanism: str
    framing_mode: str
    coverage_mode: str
    visibility_tier: str
    render_route: str
    version: str = PRIVATE_RENDER_CONTRACT_VERSION
    contract_signature: str = ""

    @classmethod
    def create(
        cls,
        *,
        lane: str,
        attraction_mechanism: str,
        framing_mode: str,
        coverage_mode: str,
    ) -> "PrivateRenderContract":
        visibility_tier, render_route = _private_route_for(lane)
        candidate = cls(
            lane=lane,
            attraction_mechanism=attraction_mechanism,
            framing_mode=framing_mode,
            coverage_mode=coverage_mode,
            visibility_tier=visibility_tier,
            render_route=render_route,
        )
        error = candidate.validate()
        if error:
            raise ValueError(error)
        return cls(**{**candidate.__dict__, "contract_signature": _private_signature(candidate)})

    def validate(self) -> str | None:
        if self.version != PRIVATE_RENDER_CONTRACT_VERSION:
            return "unsupported_private_render_contract_version"
        if self.lane not in PRIVATE_RENDER_LANES:
            return "invalid_private_render_lane"
        expected_tier, expected_route = _private_route_for(self.lane)
        if self.visibility_tier != expected_tier or self.render_route != expected_route:
            return "private_render_contract_route_conflict"
        if self.attraction_mechanism not in SUGGESTIVE_MECHANISMS:
            return "invalid_private_render_mechanism"
        if self.framing_mode not in SUGGESTIVE_FRAMING_MODES:
            return "invalid_private_render_framing_mode"
        if self.coverage_mode not in SUGGESTIVE_COVERAGE_MODES:
            return "invalid_private_render_coverage_mode"
        return None

    def to_payload(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "attraction_mechanism": self.attraction_mechanism,
            "framing_mode": self.framing_mode,
            "coverage_mode": self.coverage_mode,
            "visibility_tier": self.visibility_tier,
            "render_route": self.render_route,
            "version": self.version,
            "contract_signature": self.contract_signature,
        }

    @classmethod
    def from_payload(cls, value: object) -> "PrivateRenderContract":
        if not isinstance(value, dict):
            raise ValueError("private render contract must be an object")
        result = cls(
            lane=str(value.get("lane") or ""),
            attraction_mechanism=str(value.get("attraction_mechanism") or ""),
            framing_mode=str(value.get("framing_mode") or ""),
            coverage_mode=str(value.get("coverage_mode") or ""),
            visibility_tier=str(value.get("visibility_tier") or ""),
            render_route=str(value.get("render_route") or ""),
            version=str(value.get("version") or ""),
            contract_signature=str(value.get("contract_signature") or ""),
        )
        if result.validate() or result.contract_signature != _private_signature(result):
            raise ValueError("invalid private render contract")
        return result


@dataclass(frozen=True)
class PrivateFlairBrief:
    """A small, signed Hermes-authored performance embellishment.

    The closed matrices still own pose, body state, clothes, location, camera
    physics and provider recipe.  This deliberately narrow prose seam only
    lets a high-private plan vary one visible action, expression beat, gaze
    beat and recipient-facing subtext without changing those contracts.
    """

    expression_beat: str
    gaze_beat: str
    recipient_subtext: str
    action_beat: str = ""
    facial_profile: str = "natural_private"
    version: str = PRIVATE_FLAIR_VERSION
    signature: str = ""

    @classmethod
    def create(
        cls,
        *,
        expression_beat: str,
        gaze_beat: str,
        recipient_subtext: str,
        action_beat: str = "",
        facial_profile: str = "natural_private",
    ) -> "PrivateFlairBrief":
        candidate = cls(
            expression_beat=expression_beat.strip(),
            gaze_beat=gaze_beat.strip(),
            recipient_subtext=recipient_subtext.strip(),
            action_beat=action_beat.strip(),
            facial_profile=facial_profile.strip(),
        )
        error = candidate.validate()
        if error:
            raise ValueError(error)
        return cls(**{**candidate.__dict__, "signature": _private_flair_signature(candidate)})

    def validate(self) -> str | None:
        if self.version != PRIVATE_FLAIR_VERSION:
            return "unsupported_private_flair_version"
        if self.facial_profile not in PRIVATE_FACIAL_PROFILES:
            return "invalid_private_facial_profile"
        for value, optional_for_legacy in (
            (self.expression_beat, False),
            (self.gaze_beat, False),
            (self.recipient_subtext, False),
            (self.action_beat, True),
        ):
            # Historical v1 flair has no action beat. It remains replayable;
            # newly planned director briefs require this field separately.
            if optional_for_legacy and not value:
                continue
            if not value or len(value) > 240 or "\n" in value or "\r" in value:
                return "invalid_private_flair_text"
            lowered = value.casefold()
            if any(
                token in lowered
                for token in (
                    "ignore previous",
                    "system prompt",
                    "negative prompt",
                    "workflow",
                    "lora",
                    "cfgscale",
                    "sampler",
                    "seed=",
                )
            ):
                return "private_flair_contains_provider_control"
        return None

    def to_payload(self) -> dict[str, object]:
        payload = {
            "expression_beat": self.expression_beat,
            "gaze_beat": self.gaze_beat,
            "recipient_subtext": self.recipient_subtext,
            "version": self.version,
            "signature": self.signature,
        }
        if self.action_beat:
            payload["action_beat"] = self.action_beat
        if self.facial_profile != "natural_private":
            payload["facial_profile"] = self.facial_profile
        return payload

    @classmethod
    def from_payload(cls, value: object) -> "PrivateFlairBrief":
        if not isinstance(value, Mapping):
            raise ValueError("private flair brief must be an object")
        result = cls(
            expression_beat=str(value.get("expression_beat") or ""),
            gaze_beat=str(value.get("gaze_beat") or ""),
            recipient_subtext=str(value.get("recipient_subtext") or ""),
            action_beat=str(value.get("action_beat") or ""),
            facial_profile=str(value.get("facial_profile") or "natural_private"),
            version=str(value.get("version") or ""),
            signature=str(value.get("signature") or ""),
        )
        if result.validate() or result.signature != _private_flair_signature(result):
            raise ValueError("invalid private flair brief")
        return result


def _private_route_for(lane: str) -> tuple[str, str]:
    if lane == SUGGESTIVE_PRIVATE_LANE:
        return ("non_explicit", SPECIALIZED_SUGGESTIVE_ROUTE)
    if lane == EXPLICIT_PRIVATE_LANE:
        return ("upstream_permitted", SPECIALIZED_EXPLICIT_ROUTE)
    return ("", "")


def _private_signature(value: PrivateRenderContract) -> str:
    payload = {
        "lane": value.lane,
        "attraction_mechanism": value.attraction_mechanism,
        "framing_mode": value.framing_mode,
        "coverage_mode": value.coverage_mode,
        "visibility_tier": value.visibility_tier,
        "render_route": value.render_route,
        "version": value.version,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private_flair_signature(value: PrivateFlairBrief) -> str:
    payload = {
        "expression_beat": value.expression_beat,
        "gaze_beat": value.gaze_beat,
        "recipient_subtext": value.recipient_subtext,
        "version": value.version,
    }
    if value.action_beat:
        payload["action_beat"] = value.action_beat
    if value.facial_profile != "natural_private":
        payload["facial_profile"] = value.facial_profile
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=8)
def load_suggestive_catalog(path: Path = DEFAULT_SUGGESTIVE_CATALOG) -> Mapping[str, object]:
    """Load and validate the matrix once; callers only need this deep seam."""

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != SUGGESTIVE_CATALOG_VERSION:
        raise ValueError("unsupported suggestive private catalog")
    grounding = raw.get("principal_grounding")
    mechanisms = raw.get("mechanisms")
    facial_profiles = raw.get("facial_profiles")
    contract = raw.get("hard_contract")
    if not isinstance(grounding, dict) or set(grounding) != set(SUGGESTIVE_GROUNDING_KINDS):
        raise ValueError("suggestive catalog grounding matrix mismatch")
    if not isinstance(mechanisms, dict) or set(mechanisms) != set(SUGGESTIVE_MECHANISMS):
        raise ValueError("suggestive catalog mechanism matrix mismatch")
    if not isinstance(facial_profiles, dict) or set(facial_profiles) != set(PRIVATE_FACIAL_PROFILES):
        raise ValueError("suggestive catalog facial profile matrix mismatch")
    for profile, values in facial_profiles.items():
        if not isinstance(values, dict):
            raise ValueError("suggestive catalog facial profile must be an object")
        lanes = values.get("lanes")
        captures = values.get("capture_modes")
        distances = values.get("shot_distances")
        charges = values.get("expression_charges")
        if (
            not isinstance(lanes, list)
            or not lanes
            or any(item not in PRIVATE_RENDER_LANES for item in lanes)
            or not isinstance(captures, list)
            or not captures
            or not isinstance(distances, list)
            or not distances
            or not isinstance(charges, list)
            or not charges
            or not isinstance(values.get("author_contract"), str)
            or not isinstance(values.get("render_suffix"), str)
        ):
            raise ValueError(f"invalid suggestive facial profile: {profile}")
    if not isinstance(contract, dict) or contract.get("render_route") != SPECIALIZED_SUGGESTIVE_ROUTE:
        raise ValueError("suggestive catalog route mismatch")
    return raw


def private_facial_profile_contract(profile: str) -> Mapping[str, object]:
    """Return one reviewed facial-profile contract from the high-lane matrix."""

    if profile not in PRIVATE_FACIAL_PROFILES:
        raise ValueError("invalid_private_facial_profile")
    profiles = load_suggestive_catalog().get("facial_profiles")
    if not isinstance(profiles, Mapping) or not isinstance(profiles.get(profile), Mapping):
        raise ValueError("suggestive catalog facial profile matrix mismatch")
    return profiles[profile]


def private_facial_profile_compatibility_error(
    profile: str,
    *,
    lane: str,
    capture_mode: str,
    shot_distance: str,
    expression_charge: str,
) -> str | None:
    """Check a frozen camera contract before a profile reaches Hermes."""

    try:
        contract = private_facial_profile_contract(profile)
    except ValueError as exc:
        return str(exc)
    if lane not in contract["lanes"]:
        return "private_facial_profile_lane_conflict"
    if capture_mode not in contract["capture_modes"]:
        return "private_facial_profile_capture_conflict"
    if shot_distance not in contract["shot_distances"]:
        return "private_facial_profile_framing_conflict"
    if expression_charge not in contract["expression_charges"]:
        return "private_facial_profile_charge_conflict"
    return None


@dataclass(frozen=True)
class SuggestiveMediaAuthorization:
    """World-frozen permission for one adult, recipient-exclusive opportunity.

    The authorization is evidence-bound and cannot be inferred from a bedroom,
    relationship label, or a planner's prose alone.  It is deliberately a
    request input, not a lasting permission stored by the renderer.
    """

    authorization_id: str
    recipient_ref: str
    relationship_stage: str
    grounding_kind: str
    evidence_refs: tuple[str, ...]
    allowed_mechanisms: tuple[str, ...]
    version: str = SUGGESTIVE_AUTHORIZATION_VERSION

    def validate(self, snapshot: Mapping[str, object]) -> str | None:
        if self.version != SUGGESTIVE_AUTHORIZATION_VERSION:
            return "unsupported_suggestive_authorization_version"
        if not self.authorization_id or not self.recipient_ref:
            return "suggestive_authorization_missing_identity"
        if self.relationship_stage != "lover":
            return "suggestive_authorization_relationship_conflict"
        if self.grounding_kind not in SUGGESTIVE_GROUNDING_KINDS:
            return "invalid_suggestive_grounding_kind"
        if not self.evidence_refs or any(not ref.startswith("/") for ref in self.evidence_refs):
            return "suggestive_authorization_evidence_missing"
        if not self.allowed_mechanisms or any(
            item not in SUGGESTIVE_MECHANISMS for item in self.allowed_mechanisms
        ):
            return "invalid_suggestive_mechanism_authorization"
        if any(not _meaningful(_pointer(snapshot, ref)) for ref in self.evidence_refs):
            return "suggestive_authorization_evidence_missing"
        return None

    def freeze(self, snapshot: Mapping[str, object]) -> "FrozenSuggestiveMediaAuthorization":
        error = self.validate(snapshot)
        if error:
            raise ValueError(error)
        values = {ref: _pointer(snapshot, ref) for ref in self.evidence_refs}
        return FrozenSuggestiveMediaAuthorization(
            authorization_id=self.authorization_id,
            recipient_ref=self.recipient_ref,
            relationship_stage=self.relationship_stage,
            grounding_kind=self.grounding_kind,
            evidence_values=values,
            allowed_mechanisms=self.allowed_mechanisms,
        )


@dataclass(frozen=True)
class FrozenSuggestiveMediaAuthorization:
    authorization_id: str
    recipient_ref: str
    relationship_stage: str
    grounding_kind: str
    evidence_values: dict[str, object]
    allowed_mechanisms: tuple[str, ...]
    version: str = SUGGESTIVE_AUTHORIZATION_VERSION

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["allowed_mechanisms"] = list(self.allowed_mechanisms)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "FrozenSuggestiveMediaAuthorization":
        if not isinstance(value, dict):
            raise ValueError("suggestive authorization must be an object")
        result = cls(
            authorization_id=str(value.get("authorization_id") or ""),
            recipient_ref=str(value.get("recipient_ref") or ""),
            relationship_stage=str(value.get("relationship_stage") or ""),
            grounding_kind=str(value.get("grounding_kind") or ""),
            evidence_values=dict(value.get("evidence_values") or {}),
            allowed_mechanisms=tuple(str(item) for item in value.get("allowed_mechanisms", [])),
            version=str(value.get("version") or ""),
        )
        if result.validate_payload():
            raise ValueError("invalid frozen suggestive authorization")
        return result

    def validate_payload(self) -> str | None:
        if self.version != SUGGESTIVE_AUTHORIZATION_VERSION:
            return "unsupported_suggestive_authorization_version"
        if not self.authorization_id or not self.recipient_ref or self.relationship_stage != "lover":
            return "suggestive_authorization_relationship_conflict"
        if self.grounding_kind not in SUGGESTIVE_GROUNDING_KINDS:
            return "invalid_suggestive_grounding_kind"
        if not self.evidence_values or any(
            not ref.startswith("/") or not _meaningful(value)
            for ref, value in self.evidence_values.items()
        ):
            return "suggestive_authorization_evidence_missing"
        if not self.allowed_mechanisms or any(
            item not in SUGGESTIVE_MECHANISMS for item in self.allowed_mechanisms
        ):
            return "invalid_suggestive_mechanism_authorization"
        return None


@dataclass(frozen=True)
class SuggestivePrivateContract:
    """The high-lane contract persisted with a MediaPlan.

    ``render_route`` is intentionally a capability key, not a vendor or model
    name.  The renderer must fail closed if no dedicated adapter is configured.
    """

    authorization: FrozenSuggestiveMediaAuthorization
    attraction_mechanism: str
    framing_mode: str
    coverage_mode: str
    render_route: str = SPECIALIZED_SUGGESTIVE_ROUTE
    version: str = SUGGESTIVE_CONTRACT_VERSION
    contract_signature: str = ""

    @classmethod
    def create(
        cls,
        *,
        authorization: FrozenSuggestiveMediaAuthorization,
        attraction_mechanism: str,
        framing_mode: str,
        coverage_mode: str,
        render_route: str = SPECIALIZED_SUGGESTIVE_ROUTE,
    ) -> "SuggestivePrivateContract":
        candidate = cls(
            authorization=authorization,
            attraction_mechanism=attraction_mechanism,
            framing_mode=framing_mode,
            coverage_mode=coverage_mode,
            render_route=render_route,
        )
        error = candidate.validate()
        if error:
            raise ValueError(error)
        return cls(**{**candidate.__dict__, "contract_signature": _signature(candidate)})

    def validate(self) -> str | None:
        if self.version != SUGGESTIVE_CONTRACT_VERSION:
            return "unsupported_suggestive_contract_version"
        if self.authorization.validate_payload():
            return "invalid_frozen_suggestive_authorization"
        if self.attraction_mechanism not in self.authorization.allowed_mechanisms:
            return "suggestive_mechanism_not_authorized"
        if self.framing_mode not in SUGGESTIVE_FRAMING_MODES:
            return "invalid_suggestive_framing_mode"
        if self.coverage_mode not in SUGGESTIVE_COVERAGE_MODES:
            return "invalid_suggestive_coverage_mode"
        if self.render_route != SPECIALIZED_SUGGESTIVE_ROUTE:
            return "invalid_suggestive_render_route"
        return None

    def to_payload(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.to_payload(),
            "attraction_mechanism": self.attraction_mechanism,
            "framing_mode": self.framing_mode,
            "coverage_mode": self.coverage_mode,
            "render_route": self.render_route,
            "version": self.version,
            "contract_signature": self.contract_signature,
        }

    @classmethod
    def from_payload(cls, value: object) -> "SuggestivePrivateContract":
        if not isinstance(value, dict):
            raise ValueError("suggestive private contract must be an object")
        result = cls(
            authorization=FrozenSuggestiveMediaAuthorization.from_payload(value.get("authorization")),
            attraction_mechanism=str(value.get("attraction_mechanism") or ""),
            framing_mode=str(value.get("framing_mode") or ""),
            coverage_mode=str(value.get("coverage_mode") or ""),
            render_route=str(value.get("render_route") or ""),
            version=str(value.get("version") or ""),
            contract_signature=str(value.get("contract_signature") or ""),
        )
        if result.validate() or result.contract_signature != _signature(result):
            raise ValueError("invalid suggestive private contract")
        return result


def _signature(value: SuggestivePrivateContract) -> str:
    payload = {
        "authorization": value.authorization.to_payload(),
        "attraction_mechanism": value.attraction_mechanism,
        "framing_mode": value.framing_mode,
        "coverage_mode": value.coverage_mode,
        "render_route": value.render_route,
        "version": value.version,
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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


def _meaningful(value: object) -> bool:
    if value is _MISSING or value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True
