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
from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .proposal_envelope import ActionLayer, ProposalInput, ProposalKind


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
        "invalid_revision",
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
    proposal_kind: ProposalKind
    audit_contract: Literal["proposal-envelope-audit.1"] = "proposal-envelope-audit.1"
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: HexDigest = Field(min_length=64, max_length=64)
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    evaluated_world_revision: int = Field(ge=0)
    changes: tuple["AcceptanceChangeAuthorityV2", ...] = Field(default=(), max_length=64)
    action_intents: tuple["AcceptanceActionAuthorityV2", ...] = Field(
        default=(), max_length=64
    )

    @field_validator("proposal_event_payload_hash")
    @classmethod
    def event_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="proposal event payload hash")


class AcceptanceChangeAuthorityV2(_FrozenModel):
    change_id: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=512)
    transition: str = Field(min_length=1, max_length=64)
    expected_entity_revision: int | None = Field(default=None, ge=0)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=64)
    preconditions: tuple[str, ...] = Field(default=(), max_length=64)
    policy_refs: tuple[str, ...] = Field(default=(), max_length=64)
    payload_schema: str = Field(min_length=1, max_length=128)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    full_change_authority_hash: HexDigest = Field(min_length=64, max_length=64)

    @field_validator("full_change_authority_hash")
    @classmethod
    def hashes_are_exact(cls, value: str) -> str:
        return _validate_digest(value, label="change authority hash")


class AcceptanceActionAuthorityV2(_FrozenModel):
    intent_id: str = Field(min_length=1, max_length=256)
    kind: str = Field(min_length=1, max_length=64)
    layer: ActionLayer
    target: str = Field(min_length=1, max_length=512)
    causal_change_id: str | None = Field(default=None, max_length=256)
    beat_ref: str | None = Field(default=None, max_length=512)
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    due_window: tuple[datetime, datetime] | None = None
    payload_ref: str = Field(min_length=1, max_length=512)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    full_action_authority_hash: HexDigest = Field(min_length=64, max_length=64)

    @field_validator("full_action_authority_hash")
    @classmethod
    def authority_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="action authority hash")

    @model_validator(mode="after")
    def due_window_is_forward(self) -> AcceptanceActionAuthorityV2:
        if self.due_window is not None and self.due_window[1] <= self.due_window[0]:
            raise _model_error("invalid_role_shape", "action due window must move forward")
        return self


_PROPOSAL_ADAPTER = TypeAdapter(ProposalInput)


def derive_acceptance_manifest_proposal_v2(
    *,
    proposal_json: str,
    proposal_event_ref: str,
    proposal_event_payload_hash: str,
) -> AcceptanceManifestProposalV2:
    """Re-derive the complete inert authority summary from canonical Proposal bytes."""

    proposal = _PROPOSAL_ADAPTER.validate_json(proposal_json, strict=True)
    changes = tuple(
        AcceptanceChangeAuthorityV2(
            change_id=change.change_id,
            kind=change.kind,
            target_id=change.target_id,
            transition=change.transition,
            expected_entity_revision=change.expected_entity_revision,
            evidence_refs=change.evidence_refs,
            preconditions=change.preconditions,
            policy_refs=change.policy_refs,
            payload_schema=change.payload.payload_schema,
            payload_hash=change.payload.payload_hash,
            full_change_authority_hash=hashlib.sha256(
                _canonical_json(
                    {
                        "contract": "manifest-change-authority.1",
                        "change": change.model_dump(mode="json"),
                    }
                ).encode()
            ).hexdigest(),
        )
        for change in proposal.proposed_changes
    )
    actions = tuple(
        AcceptanceActionAuthorityV2(
            intent_id=intent.intent_id,
            kind=intent.kind,
            layer=intent.layer,
            target=intent.target,
            causal_change_id=intent.causal_change_id,
            beat_ref=intent.beat_ref,
            dependencies=intent.dependencies,
            due_window=intent.due_window,
            payload_ref=intent.payload_ref,
            payload_hash=intent.payload_hash,
            full_action_authority_hash=hashlib.sha256(
                _canonical_json(
                    {
                        "contract": "manifest-action-authority.1",
                        "action_intent": intent.model_dump(mode="json"),
                    }
                ).encode()
            ).hexdigest(),
        )
        for intent in proposal.action_intents
    )
    return AcceptanceManifestProposalV2(
        proposal_id=proposal.proposal_id,
        proposal_kind=proposal.proposal_kind,
        proposal_event_ref=proposal_event_ref,
        proposal_event_payload_hash=proposal_event_payload_hash,
        proposal_hash=proposal.proposal_hash,
        evaluated_world_revision=proposal.evaluated_world_revision,
        changes=changes,
        action_intents=actions,
    )


class EffectAuthorityRefV2(_FrozenModel):
    proposal_id: str = Field(min_length=1, max_length=256)
    authority_kind: Literal["change", "action_intent"]
    authority_id: str = Field(min_length=1, max_length=256)
    authority_hash: HexDigest = Field(min_length=64, max_length=64)

    @field_validator("authority_hash")
    @classmethod
    def authority_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="effect authority hash")


class AcceptanceAuthorizedEffectV2(_FrozenModel):
    ordinal: int = Field(ge=0, lt=MAX_MANIFEST_EFFECTS)
    role: AuthorizedEffectRole
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1, max_length=128)
    payload_hash: HexDigest = Field(min_length=64, max_length=64)
    authority_refs: tuple[EffectAuthorityRefV2, ...] = Field(
        min_length=1, max_length=MAX_EFFECT_PROPOSAL_REFS
    )

    @field_validator("payload_hash")
    @classmethod
    def payload_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="authorized effect payload hash")

    @model_validator(mode="after")
    def role_contract_is_closed(self) -> AcceptanceAuthorizedEffectV2:
        keys = tuple(
            (ref.proposal_id, ref.authority_kind, ref.authority_id)
            for ref in self.authority_refs
        )
        if keys != tuple(sorted(set(keys))):
            raise _model_error(
                "noncanonical_proposal_refs",
                "effect authority refs must be sorted and unique",
            )
        if self.role == "domain_mutation":
            if len(self.authority_refs) != 1 or self.authority_refs[0].authority_kind != "change":
                raise _model_error(
                    "invalid_role_shape",
                    "domain mutation requires one proposal and complete change authority",
                )
            if self.event_type in {"BudgetReserved", "ActionAuthorized"}:
                raise _model_error(
                    "invalid_role_shape",
                    "budget/action event types cannot claim the domain mutation role",
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
        if self.role == "action_authorization" and (
            len(self.authority_refs) != 1
            or self.authority_refs[0].authority_kind != "action_intent"
        ):
            raise _model_error("invalid_role_shape", "action effect requires one action intent")
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
        if any(
            proposal.evaluated_world_revision != self.evaluated_world_revision
            for proposal in self.proposals
        ):
            raise _model_error("invalid_revision", "manifest proposal revisions must agree")
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
        authorities: dict[tuple[str, str, str], str] = {}
        for proposal in self.proposals:
            authorities.update(
                {
                    (proposal.proposal_id, "change", change.change_id):
                    change.full_change_authority_hash
                    for change in proposal.changes
                }
            )
            authorities.update(
                {
                    (proposal.proposal_id, "action_intent", action.intent_id):
                    action.full_action_authority_hash
                    for action in proposal.action_intents
                }
            )
        used_domain: set[tuple[str, str, str]] = set()
        used_action: set[tuple[str, str, str]] = set()
        for effect in self.authorized_effects:
            for ref in effect.authority_refs:
                key = (ref.proposal_id, ref.authority_kind, ref.authority_id)
                if authorities.get(key) != ref.authority_hash:
                    raise _model_error(
                        "mutation_binding_mismatch",
                        "effect authority ref does not exactly match the proposal summary",
                    )
                used = used_domain if effect.role == "domain_mutation" else used_action
                if effect.role in {"domain_mutation", "action_authorization"}:
                    if key in used:
                        raise _model_error(
                            "mutation_binding_mismatch",
                            "effect authority cannot be authorized twice for one role",
                        )
                    used.add(key)
        if self.status in {"rejected", "stale"} and self.authorized_effects:
            raise _model_error(
                "effects_for_nonaccepted",
                "rejected or stale manifest cannot authorize effects",
            )
        if self.status == "accepted" and not self.authorized_effects:
            raise _model_error(
                "accepted_without_effects", "accepted manifest requires authorized effects"
            )
        if self.manifest_hash != canonical_acceptance_manifest_hash(self):
            raise _model_error("hash_mismatch", "manifest hash is not canonical")
        return self


class AcceptanceManifestRefV2(_FrozenModel):
    acceptance_event_ref: str = Field(min_length=1)
    acceptance_event_payload_hash: HexDigest = Field(min_length=64, max_length=64)
    recorded_at_world_revision: int = Field(ge=1)
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

    @field_validator("acceptance_event_payload_hash")
    @classmethod
    def event_payload_hash_is_exact(cls, value: str) -> str:
        return _validate_digest(value, label="acceptance event payload hash")

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
        acceptance_event_payload_hash: str,
        recorded_at_world_revision: int,
    ) -> AcceptanceManifestRefV2:
        return cls(
            acceptance_event_ref=acceptance_event_ref,
            acceptance_event_payload_hash=acceptance_event_payload_hash,
            recorded_at_world_revision=recorded_at_world_revision,
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
        refs = effect.get("authority_refs")
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
    if (
        len(location) == 3
        and location[0] in {"proposals", "authorized_effects"}
        and type(location[1]) is int
        and location[2]
        in {"proposal_event_payload_hash", "proposal_hash", "payload_hash"}
    ):
        return True
    return (
        len(location) == 5
        and location[0] in {"proposals", "authorized_effects"}
        and type(location[1]) is int
        and location[2] in {"changes", "action_intents", "authority_refs"}
        and type(location[3]) is int
        and location[4]
        in {
            "payload_hash",
            "full_change_authority_hash",
            "full_action_authority_hash",
            "authority_hash",
        }
    )
