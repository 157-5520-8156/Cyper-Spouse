"""Inert wire contracts for accepted-manifest v3 compiler authority.

This module deliberately has no ledger, reducer, event-catalog, compiler, or
``WorldEvent`` dependency.  Values defined here are evidence-bearing DTOs, not
capabilities, and cannot authorize or materialize a commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ACCEPTANCE_MANIFEST_V3_VERSION = "acceptance-manifest.3"
EFFECT_AUTHORITY_VERSION = "effect-authority.1"
COMPILER_AUTHORITY_VERSION = "effect-compiler-authority.1"

MAX_TYPED_COMPILER_DEPENDENCIES = 16
MAX_COMPILER_AUTHORITY_MATERIAL_BYTES = 20_000
MAX_MANIFEST_V3_PROPOSALS = 32
MAX_MANIFEST_V3_EFFECTS = 64
MAX_EFFECT_AUTHORITY_REFS = 32
MAX_MANIFEST_V3_MATERIAL_BYTES = 512_000
MAX_MANIFEST_V3_MATERIAL_NODES = 12_000
MAX_MANIFEST_V3_MATERIAL_DEPTH = 20
MAX_MANIFEST_V3_INTEGER_BITS = 128
MAX_REF_LENGTH = 512
MAX_ID_LENGTH = 256

_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_PREFIXED_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"

DependencyKind = Literal[
    "proposal_schema",
    "payload_schema",
    "policy_contract",
    "hash_contract",
    "canonicalizer",
    "domain_authority",
]
AcceptanceStatus = Literal["accepted", "rejected", "stale"]
AuthorizedEffectRole = Literal[
    "domain_mutation", "budget_reservation", "action_authorization"
]
ProposalKind = Literal["decision", "continuation", "minimal"]
ActionLayer = Literal[
    "internal_state_transition",
    "world_event",
    "external_action",
    "media_action",
    "read_only_tool",
]


class _ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class AcceptedEffectContractError(ValueError):
    """Stable public-boundary error for inert accepted-effect contracts."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = f"accepted_effect_contract.{code}"
        super().__init__(f"{self.code}: {detail}")


class DurableDomainCompilerKeyV1(_ContractModel):
    """Closed v1 wire key for the first Fact commit vertical.

    This is intentionally independent of ``acceptance_compilers.DomainCompilerKey``:
    that runtime key is tied to the old proposal/payload v1 registry and importing it
    here would create a dependency cycle once compilers consume these contracts.
    """

    proposal_schema_registry: Literal["world-v2-proposals.2"]
    change_kind: Literal["fact_transition"]
    transition: Literal["commit"]
    payload_schema: Literal["fact_commit_intent.v2"]
    payload_version: Literal[2]


# The longer name is the canonical public spelling; the shorter alias preserves
# the name used by the initial design draft without introducing a second model.
DomainCompilerKeyV1 = DurableDomainCompilerKeyV1


class TypedCompilerDependencyV1(_ContractModel):
    dependency_kind: DependencyKind
    dependency_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    dependency_digest: str = Field(pattern=_DIGEST_PATTERN)


class DurableEffectCompilerAuthorityV1(_ContractModel):
    authority_version: Literal["effect-compiler-authority.1"]
    install_descriptor_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    install_descriptor_digest: str = Field(pattern=_DIGEST_PATTERN)
    registry_version: str = Field(min_length=1, max_length=128)
    registry_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    registry_digest: str = Field(pattern=_DIGEST_PATTERN)
    compiler_key: DurableDomainCompilerKeyV1
    compiler_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    compiler_digest: str = Field(pattern=_DIGEST_PATTERN)
    reverse_verifier_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    reverse_verifier_digest: str = Field(pattern=_DIGEST_PATTERN)
    canonical_codec_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    canonical_codec_digest: str = Field(pattern=_DIGEST_PATTERN)
    output_contract_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    output_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    resolver_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    resolver_digest: str = Field(pattern=_DIGEST_PATTERN)
    predicate_matrix_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    predicate_matrix_digest: str = Field(pattern=_DIGEST_PATTERN)
    evidence_use_matrix_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    evidence_use_matrix_digest: str = Field(pattern=_DIGEST_PATTERN)
    privacy_matrix_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    privacy_matrix_digest: str = Field(pattern=_DIGEST_PATTERN)
    observation_authority_contract_ref: str = Field(
        min_length=1, max_length=MAX_REF_LENGTH
    )
    observation_authority_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    event_catalog_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    event_catalog_digest: str = Field(pattern=_DIGEST_PATTERN)
    domain_identity_contract_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    domain_identity_contract_digest: str = Field(pattern=_DIGEST_PATTERN)
    reducer_bundle_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    reducer_bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    typed_dependencies: tuple[TypedCompilerDependencyV1, ...] = Field(
        max_length=MAX_TYPED_COMPILER_DEPENDENCIES
    )
    proposal_event_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    proposal_event_payload_hash: str = Field(pattern=_DIGEST_PATTERN)
    proposal_hash: str = Field(pattern=_PREFIXED_DIGEST_PATTERN)

    @model_validator(mode="after")
    def dependencies_and_material_are_canonical(
        self,
    ) -> DurableEffectCompilerAuthorityV1:
        dependency_keys = tuple(
            (item.dependency_kind, item.dependency_ref) for item in self.typed_dependencies
        )
        if dependency_keys != tuple(sorted(dependency_keys)):
            raise ValueError("typed compiler dependencies must be sorted")
        if len(dependency_keys) != len(set(dependency_keys)):
            raise ValueError("typed compiler dependencies must be unique")
        material = _safe_material(
            self,
            max_bytes=MAX_COMPILER_AUTHORITY_MATERIAL_BYTES,
            max_nodes=2_048,
            max_depth=8,
        )
        if len(_canonical_json(material).encode("utf-8")) > MAX_COMPILER_AUTHORITY_MATERIAL_BYTES:
            raise ValueError("compiler authority material exceeds byte limit")
        return self


DurableEffectCompilerAuthority = DurableEffectCompilerAuthorityV1


class AcceptanceChangeAuthorityV3(_ContractModel):
    change_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    kind: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    transition: str = Field(min_length=1, max_length=64)
    expected_entity_revision: int | None = Field(ge=0)
    evidence_refs: tuple[str, ...] = Field(max_length=64)
    preconditions: tuple[str, ...] = Field(max_length=64)
    policy_refs: tuple[str, ...] = Field(max_length=64)
    payload_schema: str = Field(min_length=1, max_length=128)
    payload_version: Literal[2]
    payload_hash: str = Field(pattern=_PREFIXED_DIGEST_PATTERN)
    full_change_authority_hash: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> AcceptanceChangeAuthorityV3:
        for name in ("evidence_refs", "preconditions", "policy_refs"):
            refs = getattr(self, name)
            if refs != tuple(sorted(set(refs))):
                raise ValueError(f"{name} must be sorted and unique")
            if any(not ref or len(ref) > MAX_REF_LENGTH for ref in refs):
                raise ValueError(f"{name} contains an invalid ref")
        return self


class AcceptanceActionAuthorityV3(_ContractModel):
    intent_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    kind: str = Field(min_length=1, max_length=64)
    layer: ActionLayer
    target: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    causal_change_id: str | None = Field(max_length=MAX_ID_LENGTH)
    beat_ref: str | None = Field(max_length=MAX_REF_LENGTH)
    dependencies: tuple[str, ...] = Field(max_length=64)
    due_window: tuple[datetime, datetime] | None
    payload_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    payload_hash: str = Field(pattern=_PREFIXED_DIGEST_PATTERN)
    full_action_authority_hash: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def action_is_canonical(self) -> AcceptanceActionAuthorityV3:
        if self.dependencies != tuple(sorted(set(self.dependencies))):
            raise ValueError("action dependencies must be sorted and unique")
        if any(not item or len(item) > MAX_REF_LENGTH for item in self.dependencies):
            raise ValueError("action dependency contains an invalid ref")
        if self.due_window is not None:
            start, end = self.due_window
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("action due window must be timezone-aware")
            if end <= start:
                raise ValueError("action due window must move forward")
        return self


class AcceptanceManifestProposalV3(_ContractModel):
    proposal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    proposal_kind: ProposalKind
    proposal_schema_registry: Literal["world-v2-proposals.2"]
    audit_contract: Literal[
        "proposal-envelope-audit.1", "fact-commit-proposal-audit.2"
    ]
    proposal_event_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    proposal_event_payload_hash: str = Field(pattern=_DIGEST_PATTERN)
    proposal_hash: str = Field(pattern=_PREFIXED_DIGEST_PATTERN)
    evaluated_world_revision: int = Field(ge=0)
    changes: tuple[AcceptanceChangeAuthorityV3, ...] = Field(max_length=64)
    action_intents: tuple[AcceptanceActionAuthorityV3, ...] = Field(max_length=64)

    @model_validator(mode="after")
    def authorities_are_canonical(self) -> AcceptanceManifestProposalV3:
        change_ids = tuple(item.change_id for item in self.changes)
        action_ids = tuple(item.intent_id for item in self.action_intents)
        if change_ids != tuple(sorted(set(change_ids))):
            raise ValueError("proposal changes must be sorted and unique")
        if action_ids != tuple(sorted(set(action_ids))):
            raise ValueError("proposal action intents must be sorted and unique")
        return self


class EffectAuthorityRefV3(_ContractModel):
    proposal_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    authority_kind: Literal["change", "action_intent"]
    authority_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    authority_hash: str = Field(pattern=_DIGEST_PATTERN)


class AcceptanceAuthorizedEffectV3(_ContractModel):
    effect_authority_version: Literal["effect-authority.1"]
    ordinal: int = Field(ge=0, lt=MAX_MANIFEST_V3_EFFECTS)
    role: AuthorizedEffectRole
    event_id: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    event_type: str = Field(min_length=1, max_length=128)
    payload_hash: str = Field(pattern=_DIGEST_PATTERN)
    authority_refs: tuple[EffectAuthorityRefV3, ...] = Field(
        min_length=1, max_length=MAX_EFFECT_AUTHORITY_REFS
    )
    domain_compiler_authority: DurableEffectCompilerAuthorityV1 | None

    @model_validator(mode="after")
    def role_contract_is_closed(self) -> AcceptanceAuthorizedEffectV3:
        keys = tuple(
            (ref.proposal_id, ref.authority_kind, ref.authority_id)
            for ref in self.authority_refs
        )
        if keys != tuple(sorted(keys)):
            raise ValueError("effect authority refs must be sorted")
        if len(keys) != len(set(keys)):
            raise ValueError("effect authority refs must be unique")
        if self.role == "domain_mutation":
            if len(self.authority_refs) != 1 or self.authority_refs[0].authority_kind != "change":
                raise ValueError("domain mutation requires exactly one change authority ref")
            if self.domain_compiler_authority is None:
                raise ValueError("domain mutation requires compiler authority")
            if self.event_type in {"BudgetReserved", "ActionAuthorized"}:
                raise ValueError("domain mutation cannot claim budget/action event type")
        elif self.domain_compiler_authority is not None:
            raise ValueError("non-domain effects must not carry domain compiler authority")
        if self.role == "budget_reservation" and self.event_type != "BudgetReserved":
            raise ValueError("budget reservation role requires BudgetReserved")
        if self.role == "action_authorization":
            if self.event_type != "ActionAuthorized":
                raise ValueError("action authorization role requires ActionAuthorized")
            if (
                len(self.authority_refs) != 1
                or self.authority_refs[0].authority_kind != "action_intent"
            ):
                raise ValueError("action authorization requires one action intent")
        return self


class AcceptanceManifestV3(_ContractModel):
    manifest_version: Literal["acceptance-manifest.3"]
    acceptance_id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    status: AcceptanceStatus
    evaluated_world_revision: int = Field(ge=0)
    proposals: tuple[AcceptanceManifestProposalV3, ...] = Field(
        min_length=1, max_length=MAX_MANIFEST_V3_PROPOSALS
    )
    authorized_effects: tuple[AcceptanceAuthorizedEffectV3, ...] = Field(
        max_length=MAX_MANIFEST_V3_EFFECTS
    )
    manifest_hash: str = Field(pattern=_DIGEST_PATTERN)

    @model_validator(mode="after")
    def manifest_is_canonical_and_complete(self) -> AcceptanceManifestV3:
        proposal_ids = tuple(item.proposal_id for item in self.proposals)
        if proposal_ids != tuple(sorted(proposal_ids)):
            raise ValueError("manifest proposals must be sorted")
        if len(proposal_ids) != len(set(proposal_ids)):
            raise ValueError("manifest proposals must be unique")
        if any(
            proposal.evaluated_world_revision != self.evaluated_world_revision
            for proposal in self.proposals
        ):
            raise ValueError("manifest proposal revisions must agree")
        ordinals = tuple(item.ordinal for item in self.authorized_effects)
        if ordinals != tuple(range(len(self.authorized_effects))):
            raise ValueError("effect ordinals must be contiguous")
        event_ids = tuple(item.event_id for item in self.authorized_effects)
        if len(event_ids) != len(set(event_ids)):
            raise ValueError("effect event IDs must be unique")

        proposal_by_id = {proposal.proposal_id: proposal for proposal in self.proposals}
        authorities: dict[tuple[str, str, str], str] = {}
        for proposal in self.proposals:
            for change in proposal.changes:
                key = (proposal.proposal_id, "change", change.change_id)
                if key in authorities:
                    raise ValueError("proposal authority identity must be unique")
                authorities[key] = change.full_change_authority_hash
            for action in proposal.action_intents:
                key = (proposal.proposal_id, "action_intent", action.intent_id)
                if key in authorities:
                    raise ValueError("proposal authority identity must be unique")
                authorities[key] = action.full_action_authority_hash

        used: set[tuple[str, str, str]] = set()
        for effect in self.authorized_effects:
            for ref in effect.authority_refs:
                key = (ref.proposal_id, ref.authority_kind, ref.authority_id)
                if authorities.get(key) != ref.authority_hash:
                    raise ValueError("effect authority does not match proposal summary")
                if effect.role in {"domain_mutation", "action_authorization"}:
                    if key in used:
                        raise ValueError("proposal authority cannot be consumed twice")
                    used.add(key)
            compiler = effect.domain_compiler_authority
            if compiler is not None:
                proposal = proposal_by_id.get(effect.authority_refs[0].proposal_id)
                if proposal is None or (
                    compiler.proposal_event_ref != proposal.proposal_event_ref
                    or compiler.proposal_event_payload_hash
                    != proposal.proposal_event_payload_hash
                    or compiler.proposal_hash != proposal.proposal_hash
                ):
                    raise ValueError("compiler authority does not match proposal summary")
                change = next(
                    (
                        item
                        for item in proposal.changes
                        if item.change_id == effect.authority_refs[0].authority_id
                    ),
                    None,
                )
                if change is None or (
                    compiler.compiler_key.proposal_schema_registry
                    != proposal.proposal_schema_registry
                    or compiler.compiler_key.change_kind != change.kind
                    or compiler.compiler_key.transition != change.transition
                    or compiler.compiler_key.payload_schema != change.payload_schema
                    or compiler.compiler_key.payload_version != change.payload_version
                ):
                    raise ValueError("compiler key does not match proposal change")

        if self.status in {"rejected", "stale"} and self.authorized_effects:
            raise ValueError("nonaccepted manifest cannot authorize effects")
        if self.status == "accepted" and not self.authorized_effects:
            raise ValueError("accepted manifest requires authorized effects")
        if self.manifest_hash != canonical_acceptance_manifest_v3_hash(self):
            raise ValueError("manifest hash is not canonical")
        return self


class AcceptanceManifestRefV3(_ContractModel):
    """Immutable projection-safe retention of a recorded v3 acceptance manifest.

    This remains a value object: the event reference proves where the manifest
    was observed, but does not grant event-materialization or ledger authority.
    Keeping the complete manifest nested avoids a second, independently
    serializable authority summary that could drift from the hash-bound v3
    contract.
    """

    acceptance_event_ref: str = Field(min_length=1, max_length=MAX_REF_LENGTH)
    acceptance_event_payload_hash: str = Field(pattern=_DIGEST_PATTERN)
    recorded_at_world_revision: int = Field(ge=1)
    manifest: AcceptanceManifestV3

    @model_validator(mode="after")
    def retained_manifest_is_still_valid(self) -> AcceptanceManifestRefV3:
        # ``model_construct`` can forge a nested Pydantic instance.  Rehydrate
        # from its stored material so a retained reference never turns that
        # escape hatch into a projection authority.
        rehydrate_acceptance_manifest_v3(self.manifest)
        return self

    @classmethod
    def from_manifest(
        cls,
        manifest: AcceptanceManifestV3,
        *,
        acceptance_event_ref: str,
        acceptance_event_payload_hash: str,
        recorded_at_world_revision: int,
    ) -> AcceptanceManifestRefV3:
        """Retain an already validated manifest with its acceptance event proof."""

        return cls(
            acceptance_event_ref=acceptance_event_ref,
            acceptance_event_payload_hash=acceptance_event_payload_hash,
            recorded_at_world_revision=recorded_at_world_revision,
            manifest=rehydrate_acceptance_manifest_v3(manifest),
        )


class _MaterialBudget:
    __slots__ = ("bytes", "max_bytes", "max_depth", "max_nodes", "nodes", "visiting")

    def __init__(self, *, max_bytes: int, max_nodes: int, max_depth: int) -> None:
        self.bytes = 0
        self.nodes = 0
        self.max_bytes = max_bytes
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.visiting: set[int] = set()

    def consume(self, byte_count: int = 0) -> None:
        self.nodes += 1
        self.bytes += byte_count
        if self.nodes > self.max_nodes or self.bytes > self.max_bytes:
            raise ValueError("accepted effect contract material limit exceeded")


def _stored_model_fields(value: BaseModel) -> dict[str, object]:
    stored = object.__getattribute__(value, "__dict__")
    if type(stored) is not dict:
        raise ValueError("contract model storage must be a plain object")
    material = dict(stored)
    extra = object.__getattribute__(value, "__pydantic_extra__")
    if extra is not None:
        if type(extra) is not dict:
            raise ValueError("contract model extras must be a plain object")
        material.update(extra)
    return material


def _walk_material(
    value: object,
    *,
    budget: _MaterialBudget,
    depth: int,
    field_path: tuple[str, ...],
) -> object:
    if depth > budget.max_depth:
        raise ValueError("accepted effect contract material is too deep")
    if value is None or type(value) is bool:
        budget.consume(8)
        return value
    if type(value) is int:
        if value.bit_length() > MAX_MANIFEST_V3_INTEGER_BITS:
            raise ValueError("accepted effect contract integer is too large")
        budget.consume(max(2, len(str(value))))
        return value
    if type(value) is str:
        canonical = value
        if field_path and field_path[-1] == "due_window":
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("accepted effect contract due_window is not datetime") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("accepted effect contract datetime must be timezone-aware")
            canonical = parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        budget.consume(len(json.dumps(canonical, ensure_ascii=False).encode("utf-8")))
        return canonical
    if isinstance(value, datetime):
        if not field_path or field_path[-1] != "due_window":
            raise ValueError("datetime material is only allowed in typed due_window fields")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("accepted effect contract datetime must be timezone-aware")
        canonical = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        budget.consume(len(json.dumps(canonical).encode("utf-8")))
        return canonical
    source: object = _stored_model_fields(value) if isinstance(value, BaseModel) else value
    if type(source) not in {dict, tuple, list}:
        raise ValueError("accepted effect contract contains unsupported material")
    identity = id(source)
    if identity in budget.visiting:
        raise ValueError("accepted effect contract material is cyclic")
    budget.visiting.add(identity)
    budget.consume(2)
    try:
        if type(source) is dict:
            output: dict[str, object] = {}
            for key, child in source.items():
                if type(key) is not str:
                    raise ValueError("accepted effect contract keys must be strings")
                budget.consume(len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1)
                output[key] = _walk_material(
                    child,
                    budget=budget,
                    depth=depth + 1,
                    field_path=(*field_path, key),
                )
            return output
        return tuple(
            _walk_material(
                child,
                budget=budget,
                depth=depth + 1,
                field_path=field_path,
            )
            for child in source
        )
    finally:
        budget.visiting.remove(identity)


def _safe_material(
    value: object,
    *,
    max_bytes: int = MAX_MANIFEST_V3_MATERIAL_BYTES,
    max_nodes: int = MAX_MANIFEST_V3_MATERIAL_NODES,
    max_depth: int = MAX_MANIFEST_V3_MATERIAL_DEPTH,
) -> object:
    return _walk_material(
        value,
        budget=_MaterialBudget(
            max_bytes=max_bytes,
            max_nodes=max_nodes,
            max_depth=max_depth,
        ),
        depth=0,
        field_path=(),
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_compiler_authority_hash(
    value: DurableEffectCompilerAuthorityV1 | dict[str, object],
) -> str:
    material = _safe_material(
        value,
        max_bytes=MAX_COMPILER_AUTHORITY_MATERIAL_BYTES,
        max_nodes=2_048,
        max_depth=8,
    )
    encoded = _canonical_json(
        {"contract": "effect-compiler-authority-hash.1", "authority": material}
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_acceptance_manifest_v3_hash(
    value: AcceptanceManifestV3 | BaseModel | dict[str, object],
) -> str:
    material = _safe_material(value)
    if type(material) is not dict:
        raise ValueError("acceptance manifest v3 must be an object")
    material.pop("manifest_hash", None)
    material.setdefault("manifest_version", ACCEPTANCE_MANIFEST_V3_VERSION)
    material.setdefault("authorized_effects", ())
    encoded = _canonical_json(
        {"contract": "acceptance-manifest-hash.3", "manifest": material}
    ).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_V3_MATERIAL_BYTES:
        raise ValueError("acceptance manifest v3 material exceeds byte limit")
    return hashlib.sha256(encoded).hexdigest()


def _preflight_manifest_v3(payload: object) -> dict[str, object]:
    raw = _safe_material(payload)
    if type(raw) is not dict:
        raise ValueError("acceptance manifest v3 payload must be an object")
    proposals = raw.get("proposals")
    effects = raw.get("authorized_effects")
    if type(proposals) not in {list, tuple} or len(proposals) > MAX_MANIFEST_V3_PROPOSALS:
        raise ValueError("acceptance manifest v3 proposal limit exceeded")
    if type(effects) not in {list, tuple} or len(effects) > MAX_MANIFEST_V3_EFFECTS:
        raise ValueError("acceptance manifest v3 effect limit exceeded")
    return raw


def rehydrate_acceptance_manifest_v3(
    payload: AcceptanceManifestV3 | BaseModel | dict[str, object],
) -> AcceptanceManifestV3:
    """Strictly rehydrate an inert DTO without granting integration authority."""

    raw = _preflight_manifest_v3(payload)
    return AcceptanceManifestV3.model_validate_json(_canonical_json(raw), strict=True)


def rehydrate_acceptance_manifest_v3_json(payload_json: str) -> AcceptanceManifestV3:
    """Strict JSON entry for inert DTOs, including timezone-aware datetime decoding."""

    if type(payload_json) is not str:
        raise TypeError("acceptance manifest v3 JSON must be a string")
    encoded = payload_json.encode("utf-8")
    if len(encoded) > MAX_MANIFEST_V3_MATERIAL_BYTES:
        raise ValueError("acceptance manifest v3 material exceeds byte limit")
    try:
        decoded = json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("acceptance manifest v3 JSON is invalid") from exc
    raw = _preflight_manifest_v3(decoded)
    canonical_json = _canonical_json(raw)
    return AcceptanceManifestV3.model_validate_json(canonical_json, strict=True)


def parse_acceptance_manifest_v3(
    payload: AcceptanceManifestV3 | BaseModel | dict[str, object],
    *,
    accepted_integration_enabled: bool = False,
) -> AcceptanceManifestV3:
    """Public parser whose accepted path remains fail-closed by default."""

    if type(accepted_integration_enabled) is not bool:
        raise AcceptedEffectContractError(
            "invalid_gate", "accepted integration gate must be an exact boolean"
        )
    manifest = rehydrate_acceptance_manifest_v3(payload)
    if manifest.status == "accepted" and accepted_integration_enabled is not True:
        raise AcceptedEffectContractError(
            "accepted_not_enabled", "accepted manifest v3 integration is not installed"
        )
    return manifest


__all__ = [
    "ACCEPTANCE_MANIFEST_V3_VERSION",
    "COMPILER_AUTHORITY_VERSION",
    "EFFECT_AUTHORITY_VERSION",
    "MAX_COMPILER_AUTHORITY_MATERIAL_BYTES",
    "MAX_EFFECT_AUTHORITY_REFS",
    "MAX_MANIFEST_V3_EFFECTS",
    "MAX_MANIFEST_V3_MATERIAL_BYTES",
    "MAX_MANIFEST_V3_PROPOSALS",
    "MAX_TYPED_COMPILER_DEPENDENCIES",
    "AcceptedEffectContractError",
    "AcceptanceActionAuthorityV3",
    "AcceptanceAuthorizedEffectV3",
    "AcceptanceChangeAuthorityV3",
    "AcceptanceManifestProposalV3",
    "AcceptanceManifestRefV3",
    "AcceptanceManifestV3",
    "DomainCompilerKeyV1",
    "DurableDomainCompilerKeyV1",
    "DurableEffectCompilerAuthority",
    "DurableEffectCompilerAuthorityV1",
    "EffectAuthorityRefV3",
    "TypedCompilerDependencyV1",
    "canonical_acceptance_manifest_v3_hash",
    "canonical_compiler_authority_hash",
    "parse_acceptance_manifest_v3",
    "rehydrate_acceptance_manifest_v3",
    "rehydrate_acceptance_manifest_v3_json",
]
