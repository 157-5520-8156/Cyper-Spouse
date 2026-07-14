"""Pure Acceptance Manifest v2 value objects.

The models in this module are deliberately inert.  They define and hash the
future multi-proposal acceptance authority, but do not authorize reducers or
event-catalog transitions.  Accepted-manifest integration therefore remains
disabled at the public parser unless a test or future composition layer opts in
explicitly.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


ACCEPTANCE_MANIFEST_VERSION = "acceptance-manifest.2"
ACCEPTANCE_MANIFEST_ERROR_PREFIX = "acceptance_manifest."
ACCEPTED_MANIFEST_INTEGRATION_ENABLED = False
MAX_MANIFEST_PROPOSALS = 32
MAX_MANIFEST_EFFECTS = 64
MAX_EFFECT_PROPOSAL_REFS = 32
MAX_MANIFEST_MATERIAL_NODES = 4_096
MAX_MANIFEST_MATERIAL_BYTES = 256_000
MAX_MANIFEST_MATERIAL_DEPTH = 8
MAX_MANIFEST_INTEGER_BITS = 128

_MODEL_ERROR_CODES = frozenset(
    {
        "invalid_digest",
        "noncanonical_proposal_refs",
        "invalid_role_shape",
        "noncanonical_proposals",
        "duplicate_change_id",
        "effect_order_invalid",
        "duplicate_effect_event",
        "unknown_effect_proposal",
        "mutation_binding_mismatch",
        "effects_for_nonaccepted",
        "accepted_without_effects",
        "domain_authority_incomplete",
        "hash_mismatch",
    }
)

HexDigest = str
AcceptanceStatus = Literal["accepted", "rejected", "stale"]
AuthorizedEffectRole = Literal[
    "domain_mutation",
    "budget_reservation",
    "action_authorization",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AcceptanceManifestError(ValueError):
    """Stable integration-facing failure with a machine-readable code."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = f"{ACCEPTANCE_MANIFEST_ERROR_PREFIX}{code}"
        super().__init__(f"{self.code}: {detail}")


def _model_error(code: str, detail: str) -> ValueError:
    return ValueError(f"{ACCEPTANCE_MANIFEST_ERROR_PREFIX}{code}: {detail}")


def _is_hex_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _validate_digest(value: str, *, label: str) -> str:
    if not _is_hex_digest(value):
        raise _model_error("invalid_digest", f"{label} must be lowercase SHA-256 hex")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_string_bytes(value: str) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode())
    except UnicodeError as exc:
        raise AcceptanceManifestError(
            "invalid_shape", "manifest strings must be valid UTF-8 material"
        ) from exc


class AcceptanceManifestProposalV2(_FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=256)
    proposal_kind: str = Field(min_length=1, max_length=128)
    authority_contract_ref: str = Field(min_length=1, max_length=256)
    change_id: str = Field(min_length=1, max_length=256)
    proposed_change_hash: HexDigest = Field(min_length=64, max_length=64)
    mutation_event_type: str = Field(min_length=1, max_length=128)

    @field_validator("proposed_change_hash")
    @classmethod
    def change_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="proposed change hash")


class AcceptanceAuthorizedEffectV2(_FrozenModel):
    ordinal: int = Field(ge=0, lt=MAX_MANIFEST_EFFECTS)
    role: AuthorizedEffectRole
    event_id: str = Field(min_length=1, max_length=256)
    event_type: str = Field(min_length=1, max_length=128)
    payload_hash: HexDigest = Field(min_length=64, max_length=64)
    proposal_refs: tuple[str, ...] = Field(min_length=1, max_length=MAX_EFFECT_PROPOSAL_REFS)
    change_id: str | None = Field(default=None, min_length=1, max_length=256)
    accepted_change_hash: HexDigest | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("payload_hash")
    @classmethod
    def payload_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="authorized effect payload hash")

    @field_validator("accepted_change_hash")
    @classmethod
    def accepted_hash_is_exact(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_digest(value, label="accepted change hash")
        return value

    @model_validator(mode="after")
    def role_contract_is_closed(self) -> AcceptanceAuthorizedEffectV2:
        if self.proposal_refs != tuple(sorted(set(self.proposal_refs))):
            raise _model_error(
                "noncanonical_proposal_refs",
                "effect proposal refs must be sorted and unique",
            )
        if self.role == "domain_mutation":
            if (
                len(self.proposal_refs) != 1
                or self.change_id is None
                or self.accepted_change_hash is None
            ):
                raise _model_error(
                    "invalid_role_shape",
                    "domain mutation requires one proposal and complete change authority",
                )
            if self.event_type in {"BudgetReserved", "ActionAuthorized"}:
                raise _model_error(
                    "invalid_role_shape",
                    "budget/action event types cannot claim the domain mutation role",
                )
        elif self.change_id is not None or self.accepted_change_hash is not None:
            raise _model_error(
                "invalid_role_shape",
                "non-domain effects cannot carry accepted change authority",
            )
        if self.role == "budget_reservation" and self.event_type != "BudgetReserved":
            raise _model_error(
                "invalid_role_shape",
                "budget reservation role requires BudgetReserved",
            )
        if self.role == "action_authorization" and self.event_type != "ActionAuthorized":
            raise _model_error(
                "invalid_role_shape",
                "action authorization role requires ActionAuthorized",
            )
        return self


def canonical_acceptance_manifest_hash(value: BaseModel | dict[str, object]) -> str:
    """Hash the complete manifest authority except its self-referential digest."""

    raw = (
        value.model_dump(mode="json", warnings=False)
        if isinstance(value, BaseModel)
        else dict(value)
    )
    raw.pop("manifest_hash", None)
    raw.setdefault("manifest_version", ACCEPTANCE_MANIFEST_VERSION)
    raw.setdefault("authorized_effects", ())
    effects = raw.get("authorized_effects")
    if isinstance(effects, (list, tuple)):
        normalized_effects = []
        for effect in effects:
            if isinstance(effect, BaseModel):
                normalized = effect.model_dump(mode="json")
            elif isinstance(effect, dict):
                normalized = dict(effect)
            else:
                normalized_effects.append(effect)
                continue
            normalized.setdefault("change_id", None)
            normalized.setdefault("accepted_change_hash", None)
            normalized_effects.append(normalized)
        raw["authorized_effects"] = tuple(normalized_effects)
    try:
        encoded = _canonical_json(raw).encode()
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AcceptanceManifestError(
            "invalid_shape", "manifest hash material is not canonical JSON"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


class AcceptanceManifestV2(_FrozenModel):
    manifest_version: Literal["acceptance-manifest.2"] = ACCEPTANCE_MANIFEST_VERSION
    acceptance_id: str = Field(min_length=1, max_length=256)
    status: AcceptanceStatus
    evaluated_world_revision: int = Field(ge=0)
    proposals: tuple[AcceptanceManifestProposalV2, ...] = Field(
        min_length=1, max_length=MAX_MANIFEST_PROPOSALS
    )
    authorized_effects: tuple[AcceptanceAuthorizedEffectV2, ...] = Field(
        default=(), max_length=MAX_MANIFEST_EFFECTS
    )
    manifest_hash: HexDigest = Field(min_length=64, max_length=64)

    @field_validator("manifest_hash")
    @classmethod
    def manifest_hash_is_digest(cls, value: str) -> str:
        return _validate_digest(value, label="manifest hash")

    @model_validator(mode="after")
    def authority_is_canonical_and_complete(self) -> AcceptanceManifestV2:
        proposal_ids = tuple(item.proposal_id for item in self.proposals)
        if proposal_ids != tuple(sorted(set(proposal_ids))):
            raise _model_error(
                "noncanonical_proposals", "manifest proposals must be sorted and unique"
            )
        change_ids = tuple(item.change_id for item in self.proposals)
        if len(change_ids) != len(set(change_ids)):
            raise _model_error(
                "duplicate_change_id",
                "manifest proposals cannot alias the same change ID",
            )
        ordinals = tuple(item.ordinal for item in self.authorized_effects)
        if ordinals != tuple(range(len(self.authorized_effects))):
            raise _model_error(
                "effect_order_invalid", "authorized effect ordinals must be contiguous"
            )
        event_ids = tuple(item.event_id for item in self.authorized_effects)
        if len(event_ids) != len(set(event_ids)):
            raise _model_error(
                "duplicate_effect_event", "authorized effect event IDs must be unique"
            )
        proposal_id_set = set(proposal_ids)
        if any(
            not set(effect.proposal_refs).issubset(proposal_id_set)
            for effect in self.authorized_effects
        ):
            raise _model_error(
                "unknown_effect_proposal",
                "authorized effect references a proposal outside the manifest",
            )
        proposal_by_id = {item.proposal_id: item for item in self.proposals}
        for effect in self.authorized_effects:
            if effect.role != "domain_mutation":
                continue
            proposal = proposal_by_id[effect.proposal_refs[0]]
            if (
                effect.change_id != proposal.change_id
                or effect.accepted_change_hash != proposal.proposed_change_hash
                or effect.event_type != proposal.mutation_event_type
            ):
                raise _model_error(
                    "mutation_binding_mismatch",
                    "domain effect does not match its proposal authority",
                )
        if self.status in {"rejected", "stale"} and self.authorized_effects:
            raise _model_error(
                "effects_for_nonaccepted",
                "rejected or stale manifest cannot authorize effects",
            )
        if self.status == "accepted" and not self.authorized_effects:
            raise _model_error(
                "accepted_without_effects", "accepted manifest requires authorized effects"
            )
        if self.status == "accepted":
            domain_proposal_ids = tuple(
                effect.proposal_refs[0]
                for effect in self.authorized_effects
                if effect.role == "domain_mutation"
            )
            if tuple(sorted(domain_proposal_ids)) != proposal_ids:
                raise _model_error(
                    "domain_authority_incomplete",
                    "accepted manifest requires exactly one domain mutation per proposal",
                )
        if self.manifest_hash != canonical_acceptance_manifest_hash(self):
            raise _model_error("hash_mismatch", "manifest hash is not canonical")
        return self


class AcceptanceManifestRefV2(_FrozenModel):
    acceptance_event_ref: str = Field(min_length=1, max_length=256)
    accepted_at_world_revision: int = Field(ge=1)
    acceptance_id: str = Field(min_length=1, max_length=256)
    status: AcceptanceStatus
    evaluated_world_revision: int = Field(ge=0)
    proposals: tuple[AcceptanceManifestProposalV2, ...] = Field(
        min_length=1, max_length=MAX_MANIFEST_PROPOSALS
    )
    authorized_effects: tuple[AcceptanceAuthorizedEffectV2, ...] = Field(
        default=(), max_length=MAX_MANIFEST_EFFECTS
    )
    manifest_hash: HexDigest = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def retained_manifest_is_still_valid(self) -> AcceptanceManifestRefV2:
        AcceptanceManifestV2(
            acceptance_id=self.acceptance_id,
            status=self.status,
            evaluated_world_revision=self.evaluated_world_revision,
            proposals=self.proposals,
            authorized_effects=self.authorized_effects,
            manifest_hash=self.manifest_hash,
        )
        return self

    @classmethod
    def from_manifest(
        cls,
        manifest: AcceptanceManifestV2,
        *,
        acceptance_event_ref: str,
        accepted_at_world_revision: int,
    ) -> AcceptanceManifestRefV2:
        return cls(
            acceptance_event_ref=acceptance_event_ref,
            accepted_at_world_revision=accepted_at_world_revision,
            acceptance_id=manifest.acceptance_id,
            status=manifest.status,
            evaluated_world_revision=manifest.evaluated_world_revision,
            proposals=manifest.proposals,
            authorized_effects=manifest.authorized_effects,
            manifest_hash=manifest.manifest_hash,
        )


class _MaterialBudget:
    __slots__ = ("bytes", "nodes", "visiting")

    def __init__(self) -> None:
        self.bytes = 0
        self.nodes = 0
        self.visiting: set[int] = set()

    def consume(self, *, byte_count: int = 0) -> None:
        self.nodes += 1
        self.bytes += byte_count
        if self.nodes > MAX_MANIFEST_MATERIAL_NODES or self.bytes > MAX_MANIFEST_MATERIAL_BYTES:
            raise AcceptanceManifestError(
                "material_limit_exceeded", "manifest material budget exceeded"
            )


def _base_model_fields(value: BaseModel) -> dict[str, object]:
    """Read Pydantic storage without invoking overridable serialization code."""

    stored = object.__getattribute__(value, "__dict__")
    if type(stored) is not dict:
        raise AcceptanceManifestError("invalid_shape", "model storage must be a plain object")
    material = dict(stored)
    extra = object.__getattribute__(value, "__pydantic_extra__")
    if extra is not None:
        if type(extra) is not dict:
            raise AcceptanceManifestError("invalid_shape", "model extras must be a plain object")
        material.update(extra)
    return material


def _safe_material(
    value: object,
    *,
    budget: _MaterialBudget,
    depth: int = 0,
) -> object:
    if depth > MAX_MANIFEST_MATERIAL_DEPTH:
        raise AcceptanceManifestError(
            "material_limit_exceeded", "manifest material nesting is too deep"
        )
    if value is None or type(value) is bool:
        budget.consume(byte_count=8)
        return value
    if type(value) is int:
        if value.bit_length() > MAX_MANIFEST_INTEGER_BITS:
            raise AcceptanceManifestError(
                "material_limit_exceeded", "manifest integer exceeds the material budget"
            )
        budget.consume(byte_count=max(2, (value.bit_length() * 30_103) // 100_000 + 2))
        return value
    if type(value) is str:
        budget.consume(byte_count=_json_string_bytes(value))
        return value
    if isinstance(value, BaseModel):
        source: object = _base_model_fields(value)
    else:
        source = value
    if type(source) not in {dict, list, tuple}:
        raise AcceptanceManifestError(
            "invalid_shape", "manifest contains an unsupported material value"
        )
    identity = id(source)
    if identity in budget.visiting:
        raise AcceptanceManifestError("cyclic_material", "manifest material contains a cycle")
    budget.visiting.add(identity)
    budget.consume(byte_count=2)
    try:
        if type(source) is dict:
            output: dict[str, object] = {}
            for key, child in source.items():
                if type(key) is not str:
                    raise AcceptanceManifestError(
                        "invalid_shape", "manifest object keys must be strings"
                    )
                budget.consume(byte_count=_json_string_bytes(key) + 1)
                output[key] = _safe_material(child, budget=budget, depth=depth + 1)
            return output
        return tuple(_safe_material(child, budget=budget, depth=depth + 1) for child in source)
    finally:
        budget.visiting.remove(identity)


def _extract_untrusted_material(payload: object) -> dict[str, object]:
    material = _safe_material(payload, budget=_MaterialBudget())
    if type(material) is not dict:
        raise AcceptanceManifestError("invalid_shape", "manifest payload must be an object")
    try:
        encoded_size = len(_canonical_json(material).encode())
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AcceptanceManifestError(
            "invalid_shape", "manifest material is not canonical JSON"
        ) from exc
    if encoded_size > MAX_MANIFEST_MATERIAL_BYTES:
        raise AcceptanceManifestError(
            "material_limit_exceeded", "canonical manifest material is too large"
        )
    return material


def _preflight_limits(raw: object) -> None:
    if not isinstance(raw, dict):
        raise AcceptanceManifestError("invalid_shape", "manifest payload must be an object")
    proposals = raw.get("proposals")
    effects = raw.get("authorized_effects", ())
    if not isinstance(proposals, (list, tuple)) or len(proposals) > MAX_MANIFEST_PROPOSALS:
        raise AcceptanceManifestError("limit_exceeded", "manifest proposal limit exceeded")
    if not isinstance(effects, (list, tuple)) or len(effects) > MAX_MANIFEST_EFFECTS:
        raise AcceptanceManifestError("limit_exceeded", "manifest effect limit exceeded")
    for effect in effects:
        if not isinstance(effect, dict):
            raise AcceptanceManifestError("invalid_shape", "manifest effect must be an object")
        refs = effect.get("proposal_refs")
        if isinstance(refs, (list, tuple)) and len(refs) > MAX_EFFECT_PROPOSAL_REFS:
            raise AcceptanceManifestError("limit_exceeded", "effect proposal-ref limit exceeded")


def parse_acceptance_manifest_v2(
    payload: AcceptanceManifestV2 | BaseModel | dict[str, object],
    *,
    accepted_integration_enabled: bool = ACCEPTED_MANIFEST_INTEGRATION_ENABLED,
) -> AcceptanceManifestV2:
    """Revalidate untrusted bytes and enforce the current integration gate."""

    if type(accepted_integration_enabled) is not bool:
        raise AcceptanceManifestError(
            "invalid_gate", "accepted integration gate must be an exact boolean"
        )
    raw = _extract_untrusted_material(payload)
    _preflight_limits(raw)
    try:
        manifest = AcceptanceManifestV2.model_validate(raw)
    except ValidationError as exc:
        code = _trusted_validation_error_code(exc)
        raise AcceptanceManifestError(code, "manifest validation failed") from exc
    if manifest.status == "accepted" and accepted_integration_enabled is not True:
        raise AcceptanceManifestError(
            "accepted_not_enabled", "accepted manifest reducer integration is not installed"
        )
    return manifest


def _trusted_validation_error_code(exc: ValidationError) -> str:
    """Map installed validators/locations without parsing rendered error text."""

    for error in exc.errors():
        context_error = error.get("ctx", {}).get("error")
        if isinstance(context_error, ValueError) and context_error.args:
            detail = context_error.args[0]
            if type(detail) is str and detail.startswith(ACCEPTANCE_MANIFEST_ERROR_PREFIX):
                candidate = detail[len(ACCEPTANCE_MANIFEST_ERROR_PREFIX) :].split(":", 1)[0]
                if candidate in _MODEL_ERROR_CODES:
                    return candidate
        location = error.get("loc", ())
        if _is_installed_hash_location(location):
            return "invalid_digest"
    return "invalid_shape"


def _is_installed_hash_location(location: tuple[object, ...]) -> bool:
    if location == ("manifest_hash",):
        return True
    return (
        len(location) == 3
        and location[0] in {"proposals", "authorized_effects"}
        and type(location[1]) is int
        and location[2] in {"proposed_change_hash", "payload_hash", "accepted_change_hash"}
    )
