"""Fail-closed compiler contracts for future accepted-manifest execution.

Domain adapters may compile domain payload bytes, but only the trusted effect
planner owns ledger-event identity and envelope provenance.  Nothing here
enables accepted manifests or mutates reducer state.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, TypeAdapter, ValidationError, computed_field, model_validator

from .acceptance_manifest import (
    EffectAuthorityRefV2,
    MAX_MANIFEST_EFFECTS,
    derive_acceptance_manifest_proposal_v2,
)
from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import (
    CHANGE_TRANSITION_REGISTRY,
    PROPOSAL_SCHEMA_REGISTRY_VERSION,
    ProposalInput,
    TypedChange,
)
from .schema_core import FrozenModel
from .ledger import LedgerPort
from .projection import InternalAuthorityReader
from .schemas import ProjectionCursor


ACCEPTANCE_COMPILER_REGISTRY_VERSION = "acceptance-domain-compilers.1"
ACCEPTANCE_COMPILER_ERROR_PREFIX = "acceptance_compiler."
MAX_COMPILED_PAYLOAD_BYTES = 262_144
MAX_COMPILED_PAYLOAD_DEPTH = 32
MAX_COMPILED_PAYLOAD_NODES = 4_096
MAX_COMPILED_INTEGER_BITS = 128
MAX_EXECUTION_PLAN_PAYLOAD_BYTES = 1_048_576
MAX_COMPILER_DEPENDENCIES = 16
MAX_COMPILER_METADATA_BYTES = 262_144


class AcceptanceCompilerError(ValueError):
    """Stable failure at the inert accepted-effect compilation boundary."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = f"{ACCEPTANCE_COMPILER_ERROR_PREFIX}{code}"
        super().__init__(f"{self.code}: {detail}")


class DomainCompilerKey(FrozenModel):
    proposal_schema_registry: Literal["world-v2-proposals.1"]
    change_kind: str = Field(min_length=1, max_length=64)
    transition: str = Field(min_length=1, max_length=64)
    payload_schema: str = Field(min_length=1, max_length=128)
    payload_version: Literal[1] = 1

    @model_validator(mode="after")
    def binds_exact_proposal_contract(self) -> DomainCompilerKey:
        transitions = CHANGE_TRANSITION_REGISTRY.get(self.change_kind)
        if transitions is None or self.transition not in transitions:
            raise ValueError("compiler key does not name a registered change transition")
        if self.payload_schema != f"{self.change_kind}.v1":
            raise ValueError("compiler key payload schema does not match its change kind")
        return self


class AcceptanceCompilationContext(FrozenModel):
    """Pinned, trusted envelope authority supplied after domain compilation."""

    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_id: str = Field(min_length=1)
    cursor: ProjectionCursor
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1)
    source: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)

    @property
    def pre_world_revision(self) -> int:
        return self.cursor.world_revision


class PinnedProposalAuthorityHandle:
    """Process-local proof that ProposalAudit came from one exact ledger cursor."""

    __slots__ = ("__audit", "__cursor", "__world_id", "__issuer")

    def __init__(
        self,
        audit: ProposalAuditProjection,
        cursor: ProjectionCursor,
        world_id: str,
        *,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is None:
            raise ValueError("pinned Proposal authority handles are reader-issued")
        self.__audit = ProposalAuditProjection.model_validate(
            dict(object.__getattribute__(audit, "__dict__")), strict=True
        )
        self.__cursor = ProjectionCursor.model_validate(
            dict(object.__getattribute__(cursor, "__dict__")), strict=True
        )
        self.__world_id = world_id
        self.__issuer = _issuer

    @property
    def audit(self) -> ProposalAuditProjection:
        return self.__audit

    @property
    def cursor(self) -> ProjectionCursor:
        return self.__cursor

    @property
    def world_id(self) -> str:
        return self.__world_id

    def issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def __reduce__(self) -> object:
        raise TypeError("pinned Proposal authority handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("pinned Proposal authority handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("pinned Proposal authority handles cannot be copied")


class TrustedProposalAuthorityReader:
    """Issue Proposal authority only after exact historical ledger verification."""

    __slots__ = ("__authority_reader", "__ledger", "__issuer")

    def __init__(self, *, ledger: LedgerPort) -> None:
        self.__ledger = ledger
        self.__authority_reader = InternalAuthorityReader(ledger=ledger)
        self.__issuer = object()

    def pin(
        self, *, world_id: str, cursor: ProjectionCursor, proposal_id: str
    ) -> PinnedProposalAuthorityHandle:
        if world_id != self.__ledger.world_id:
            raise AcceptanceCompilerError("authority_mismatch", "reader belongs to another world")
        current = self.__authority_reader.current_cursor(world_id=world_id)
        if (
            cursor.world_revision > current.world_revision
            or cursor.deliberation_revision > current.deliberation_revision
            or cursor.ledger_sequence > current.ledger_sequence
        ):
            raise AcceptanceCompilerError("authority_mismatch", "cursor is after ledger head")
        audit = self.__authority_reader.proposal_audit_by_id(
            world_id=world_id, cursor=cursor, proposal_id=proposal_id
        )
        if audit is None:
            raise AcceptanceCompilerError("authority_mismatch", "ProposalAudit does not exist")
        located = self.__ledger.lookup_event_commit(audit.event_ref)
        if located is None:
            raise AcceptanceCompilerError("authority_mismatch", "ProposalAudit event is missing")
        event, commit = located
        if (
            event.world_id != world_id
            or event.event_type != "ProposalRecorded"
            or event.payload_hash != audit.event_payload_hash
            or commit.world_revision > cursor.world_revision
            or commit.deliberation_revision > cursor.deliberation_revision
            or commit.ledger_sequence > cursor.ledger_sequence
        ):
            raise AcceptanceCompilerError("authority_mismatch", "ProposalAudit event is not pinned")
        return PinnedProposalAuthorityHandle(
            audit, cursor, world_id, _issuer=self.__issuer
        )

    def owns(self, handle: PinnedProposalAuthorityHandle) -> bool:
        return handle.issued_by(self.__issuer)


class PlannedEventProvenance(FrozenModel):
    world_id: str = Field(min_length=1)
    logical_time: datetime
    created_at: datetime
    actor: str = Field(min_length=1)
    source: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    causation_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)


def _strict_key(value: DomainCompilerKey) -> DomainCompilerKey:
    try:
        stored = object.__getattribute__(value, "__dict__")
        if type(stored) is not dict:
            raise TypeError("compiler key storage is invalid")
        return DomainCompilerKey.model_validate(dict(stored), strict=True)
    except (AttributeError, TypeError, ValidationError, ValueError) as exc:
        raise AcceptanceCompilerError("unknown_key", "compiler key is not installed") from exc


def _validate_json_material(value: object) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > MAX_COMPILED_PAYLOAD_NODES or depth > MAX_COMPILED_PAYLOAD_DEPTH:
            raise ValueError("compiled payload exceeds structural limits")
        if type(item) is int and item.bit_length() > MAX_COMPILED_INTEGER_BITS:
            raise ValueError("compiled payload integer exceeds limit")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ValueError("compiled payload keys must be strings")
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("compiled payload contains an unsupported value")


def _canonical_payload_hash(payload_json: str) -> str:
    try:
        encoded = payload_json.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("compiled payload must contain valid Unicode") from exc
    if len(encoded) > MAX_COMPILED_PAYLOAD_BYTES:
        raise ValueError("compiled payload exceeds byte limit")
    try:
        value = json.loads(payload_json)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("compiled payload must be valid JSON") from exc
    if type(value) is not dict:
        raise ValueError("compiled payload must be a JSON object")
    _validate_json_material(value)
    try:
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (UnicodeError, ValueError) as exc:
        raise ValueError("compiled payload is not canonical JSON material") from exc
    if canonical != payload_json:
        raise ValueError("compiled payload JSON is not canonical")
    return hashlib.sha256(encoded).hexdigest()


class DependencyDigest(FrozenModel):
    name: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DomainPayloadDraft(FrozenModel):
    """The only value a domain adapter may choose."""

    event_type: str = Field(min_length=1, max_length=128)
    payload_json: str = Field(min_length=2, max_length=MAX_COMPILED_PAYLOAD_BYTES)

    @model_validator(mode="after")
    def payload_is_canonical(self) -> DomainPayloadDraft:
        _canonical_payload_hash(self.payload_json)
        return self

    @computed_field
    @property
    def payload_hash(self) -> str:
        return _canonical_payload_hash(self.payload_json)


class CompiledDomainPayload(FrozenModel):
    """Registry-wrapped domain bytes; inert and incapable of ledger materialization."""

    role: Literal["domain_mutation"] = "domain_mutation"
    event_type: str = Field(min_length=1, max_length=128)
    payload_json: str = Field(min_length=2, max_length=MAX_COMPILED_PAYLOAD_BYTES)
    authority_refs: tuple[EffectAuthorityRefV2, ...] = Field(min_length=1, max_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_key: DomainCompilerKey
    compiler_ref: str = Field(min_length=1, max_length=256)
    compiler_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reverse_verifier_ref: str = Field(min_length=1, max_length=256)
    reverse_verifier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_payload_contract_ref: str = Field(min_length=1, max_length=256)
    output_payload_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_digests: tuple[DependencyDigest, ...] = Field(
        default=(), max_length=MAX_COMPILER_DEPENDENCIES
    )
    registry_version: Literal["acceptance-domain-compilers.1"]
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bytes_and_authority_are_exact(self) -> CompiledDomainPayload:
        _canonical_payload_hash(self.payload_json)
        authority = self.authority_refs[0]
        if not isinstance(authority, EffectAuthorityRefV2) or authority.authority_kind != "change":
            raise ValueError("domain payload requires one typed change authority")
        dependency_names = tuple(item.name for item in self.dependency_digests)
        if dependency_names != tuple(sorted(set(dependency_names))):
            raise ValueError("compiled dependencies must be sorted and unique")
        return self

    @computed_field
    @property
    def payload_hash(self) -> str:
        return _canonical_payload_hash(self.payload_json)


def _strict_payload(value: CompiledDomainPayload) -> CompiledDomainPayload:
    stored = object.__getattribute__(value, "__dict__")
    if type(stored) is not dict:
        raise TypeError("compiled payload storage is invalid")
    raw = dict(stored)
    refs = raw.get("authority_refs")
    dependencies = raw.get("dependency_digests")
    if type(refs) is not tuple or type(dependencies) is not tuple:
        raise TypeError("compiled payload nested authority is invalid")
    raw["authority_refs"] = tuple(
        EffectAuthorityRefV2.model_validate(
            dict(object.__getattribute__(item, "__dict__")), strict=True
        )
        for item in refs
    )
    raw["dependency_digests"] = tuple(
        DependencyDigest.model_validate(
            dict(object.__getattribute__(item, "__dict__")), strict=True
        )
        for item in dependencies
    )
    key = raw.get("compiler_key")
    raw["compiler_key"] = DomainCompilerKey.model_validate(
        dict(object.__getattribute__(key, "__dict__")), strict=True
    )
    return CompiledDomainPayload.model_validate(raw, strict=True)


class CompiledDomainAuthorityHandle:
    """Non-durable proof that one installed registry compiled and verified a payload."""

    __slots__ = (
        "__authority",
        "__change",
        "__context",
        "__payload",
        "__proposal_authority",
    )

    def __init__(
        self,
        payload: CompiledDomainPayload,
        *,
        proposal_authority: PinnedProposalAuthorityHandle,
        change: TypedChange,
        context: AcceptanceCompilationContext,
        _authority: object | None = None,
    ) -> None:
        if _authority is None:
            raise ValueError("compiled domain authority handles are registry-issued")
        self.__payload = _strict_payload(payload)
        self.__proposal_authority = proposal_authority
        self.__change = change
        self.__context = context
        self.__authority = _authority

    @property
    def payload(self) -> CompiledDomainPayload:
        return self.__payload

    def issued_by(self, authority: object) -> bool:
        return self.__authority is authority

    def verification_inputs(
        self,
    ) -> tuple[PinnedProposalAuthorityHandle, TypedChange, AcceptanceCompilationContext]:
        return self.__proposal_authority, self.__change, self.__context

    def __reduce__(self) -> object:
        raise TypeError("compiled domain authority handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("compiled domain authority handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("compiled domain authority handles cannot be copied")


class ManifestDomainMutationAdapter(Protocol):
    """Compile and reverse-verify domain bytes without seeing an event envelope."""

    def compile(
        self,
        *,
        proposal_audit: ProposalAuditProjection,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> DomainPayloadDraft: ...

    def reverse_verify(
        self,
        actual: DomainPayloadDraft,
        *,
        proposal_audit: ProposalAuditProjection,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> None: ...


class UnsupportedDomainMutationAdapter:
    """Explicit fail-closed placeholder; registries must never install it as supported."""

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def compile(
        self,
        *,
        proposal_audit: ProposalAuditProjection,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> DomainPayloadDraft:
        del proposal_audit, change, context
        raise AcceptanceCompilerError("unsupported_key", self._reason)

    def reverse_verify(
        self,
        actual: DomainPayloadDraft,
        *,
        proposal_audit: ProposalAuditProjection,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> None:
        del actual, proposal_audit, change, context
        raise AcceptanceCompilerError("unsupported_key", self._reason)


@dataclass(frozen=True, slots=True)
class DomainCompilerRegistration:
    key: DomainCompilerKey
    compiler_ref: str
    compiler_digest: str
    reverse_verifier_ref: str
    reverse_verifier_digest: str
    output_payload_contract_ref: str
    output_payload_contract_digest: str
    dependency_digests: tuple[DependencyDigest, ...]
    mutation_event_types: tuple[str, ...]
    adapter: ManifestDomainMutationAdapter


class DomainCompilerCoverage(FrozenModel):
    key: DomainCompilerKey
    status: Literal["supported", "unsupported"]
    compiler_ref: str | None = Field(default=None, min_length=1, max_length=256)
    compiler_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reverse_verifier_ref: str | None = Field(default=None, min_length=1, max_length=256)
    reverse_verifier_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_payload_contract_ref: str | None = Field(default=None, min_length=1, max_length=256)
    output_payload_contract_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    dependency_digests: tuple[DependencyDigest, ...] = Field(
        default=(), max_length=MAX_COMPILER_DEPENDENCIES
    )
    mutation_event_types: tuple[str, ...] = ()
    reason_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def status_has_exact_metadata(self) -> DomainCompilerCoverage:
        if self.status == "supported":
            required = (
                self.compiler_ref,
                self.compiler_digest,
                self.reverse_verifier_ref,
                self.reverse_verifier_digest,
                self.output_payload_contract_ref,
                self.output_payload_contract_digest,
            )
            if any(value is None for value in required) or not self.mutation_event_types or self.reason_code:
                raise ValueError("supported compiler coverage metadata is incomplete")
        elif any(
            value is not None
            for value in (
                self.compiler_ref,
                self.compiler_digest,
                self.reverse_verifier_ref,
                self.reverse_verifier_digest,
                self.output_payload_contract_ref,
                self.output_payload_contract_digest,
            )
        ) or self.mutation_event_types or self.dependency_digests or not self.reason_code:
            raise ValueError("unsupported compiler coverage must contain only a reason")
        keys = tuple(item.name for item in self.dependency_digests)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("dependency digests must be sorted and uniquely named")
        return self


class DomainCompilerOwnershipContract(FrozenModel):
    key: DomainCompilerKey
    status: Literal["unsupported"] = "unsupported"
    allowed_event_types: tuple[str, ...]
    descriptor_ref: str = Field(min_length=1, max_length=256)


_KNOWN_EVENT_OWNERSHIP: dict[tuple[str, str], tuple[str, ...]] = {
    ("fact_transition", "commit"): ("FactCommitted",),
    ("fact_transition", "correct"): ("FactCorrected",),
    ("fact_transition", "withdraw"): ("FactWithdrawn",),
    ("fact_transition", "compensate"): ("FactCorrectionCompensated",),
    ("experience_transition", "commit"): ("ExperienceCommitted",),
    ("character_core_revision", "initialize"): ("CharacterCoreInitialized",),
    ("character_core_revision", "revise"): ("CharacterCoreRevised",),
    ("character_core_revision", "compensate"): ("CharacterCoreRevisionCompensated",),
    ("outcome_settlement", "settle"): ("WorldOccurrenceSettled",),
}


DOMAIN_COMPILER_COVERAGE_CATALOG = tuple(
    DomainCompilerCoverage(
        key=DomainCompilerKey(
            proposal_schema_registry=PROPOSAL_SCHEMA_REGISTRY_VERSION,
            change_kind=kind,
            transition=transition,
            payload_schema=f"{kind}.v1",
            payload_version=1,
        ),
        status="unsupported",
        reason_code="adapter_not_installed",
    )
    for kind in sorted(CHANGE_TRANSITION_REGISTRY)
    for transition in sorted(CHANGE_TRANSITION_REGISTRY[kind])
)
_CATALOG_KEYS = frozenset(item.key for item in DOMAIN_COMPILER_COVERAGE_CATALOG)
DOMAIN_COMPILER_OWNERSHIP_CONTRACTS = tuple(
    DomainCompilerOwnershipContract(
        key=item.key,
        allowed_event_types=_KNOWN_EVENT_OWNERSHIP.get(
            (item.key.change_kind, item.key.transition), ()
        ),
        descriptor_ref=f"ownership:{item.key.change_kind}:{item.key.transition}.1",
    )
    for item in DOMAIN_COMPILER_COVERAGE_CATALOG
)
_OWNERSHIP_BY_KEY = {item.key: item for item in DOMAIN_COMPILER_OWNERSHIP_CONTRACTS}
_PROPOSAL_ADAPTER = TypeAdapter(ProposalInput)


class DomainCompilerRegistry:
    """Fail-closed registry; production installs are sealed and currently unsupported."""

    __slots__ = (
        "__test_scope",
        "_authority_reader",
        "_manifest",
        "_manifest_digest",
        "_registrations",
        "_registry_capability",
    )

    def __init__(
        self,
        registrations: Sequence[DomainCompilerRegistration] = (),
        *,
        authority_reader: TrustedProposalAuthorityReader | None = None,
        _test_scope: bool = False,
    ) -> None:
        self.__test_scope = _test_scope
        self._authority_reader = authority_reader
        self._registry_capability = object()
        by_key: dict[DomainCompilerKey, DomainCompilerRegistration] = {}
        event_owners: dict[str, DomainCompilerKey] = {}
        for raw in registrations:
            key = _strict_key(raw.key)
            ownership = _OWNERSHIP_BY_KEY.get(key)
            if ownership is None:
                raise AcceptanceCompilerError("unknown_key", "compiler key is not owned")
            if not _test_scope:
                if not set(raw.mutation_event_types).issubset(
                    set(ownership.allowed_event_types)
                ):
                    raise AcceptanceCompilerError(
                        "invalid_event_owner", "event is not owned by this compiler key"
                    )
                raise AcceptanceCompilerError(
                    "unsupported_key", "production compiler ownership is not installed"
                )
            if key in by_key:
                raise AcceptanceCompilerError("duplicate_key", "compiler key has two owners")
            if isinstance(raw.adapter, UnsupportedDomainMutationAdapter):
                raise AcceptanceCompilerError(
                    "invalid_registration", "unsupported adapter cannot claim support"
                )
            if not raw.compiler_ref or len(raw.compiler_ref) > 256:
                raise AcceptanceCompilerError("invalid_registration", "compiler ref is invalid")
            descriptor_strings = (
                raw.reverse_verifier_ref,
                raw.output_payload_contract_ref,
            )
            descriptor_digests = (
                raw.compiler_digest,
                raw.reverse_verifier_digest,
                raw.output_payload_contract_digest,
            )
            if any(not value or len(value) > 256 for value in descriptor_strings) or any(
                len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
                for value in descriptor_digests
            ):
                raise AcceptanceCompilerError(
                    "invalid_registration", "compiler implementation descriptor is invalid"
                )
            try:
                if len(raw.dependency_digests) > MAX_COMPILER_DEPENDENCIES:
                    raise ValueError("too many compiler dependencies")
                dependencies = tuple(
                    DependencyDigest.model_validate(
                        dict(object.__getattribute__(item, "__dict__")), strict=True
                    )
                    for item in raw.dependency_digests
                )
            except (ValidationError, ValueError) as exc:
                raise AcceptanceCompilerError(
                    "invalid_registration", "compiler dependency digest is invalid"
                ) from exc
            dependency_names = tuple(item.name for item in dependencies)
            if dependency_names != tuple(sorted(set(dependency_names))):
                raise AcceptanceCompilerError(
                    "invalid_registration", "compiler dependencies must be sorted and unique"
                )
            if not raw.mutation_event_types:
                raise AcceptanceCompilerError(
                    "invalid_registration", "supported compiler owns no mutation events"
                )
            for event_type in raw.mutation_event_types:
                if not event_type or len(event_type) > 128:
                    raise AcceptanceCompilerError(
                        "invalid_registration", "mutation event type is invalid"
                    )
                try:
                    from .event_catalog import event_contract
                    from .event_identity import domain_idempotency_key
                    from .reducers import RevisionClass, event_definition

                    contract = event_contract(event_type)
                    definition = event_definition(event_type)
                    machine_identity = domain_idempotency_key(
                        event_type=event_type, world_id="compiler-registry-probe", payload={}
                    )
                except (ImportError, RuntimeError, ValueError) as exc:
                    raise AcceptanceCompilerError(
                        "invalid_event_owner", "mutation event is not installed"
                    ) from exc
                reserved = event_type in {
                    "AcceptanceRecorded",
                    "ProposalRecorded",
                    "ModelResultRecorded",
                } or event_type.startswith(("Legacy", "Trigger"))
                if (
                    reserved
                    or contract.revision_class != "world"
                    or definition.revision_class is not RevisionClass.WORLD
                    or not contract.idempotency_identity
                    or machine_identity is None
                ):
                    raise AcceptanceCompilerError(
                        "invalid_event_owner", "event is not a domain world mutation"
                    )
                if event_type in event_owners:
                    raise AcceptanceCompilerError(
                        "duplicate_event_owner", "mutation event type has two compiler owners"
                    )
                event_owners[event_type] = key
            by_key[key] = DomainCompilerRegistration(
                key=key,
                compiler_ref=raw.compiler_ref,
                compiler_digest=raw.compiler_digest,
                reverse_verifier_ref=raw.reverse_verifier_ref,
                reverse_verifier_digest=raw.reverse_verifier_digest,
                output_payload_contract_ref=raw.output_payload_contract_ref,
                output_payload_contract_digest=raw.output_payload_contract_digest,
                dependency_digests=dependencies,
                mutation_event_types=tuple(sorted(set(raw.mutation_event_types))),
                adapter=raw.adapter,
            )
        if not set(by_key).issubset(_CATALOG_KEYS):
            raise AcceptanceCompilerError("unknown_key", "registration key is not catalogued")
        self._registrations = by_key
        self._manifest = tuple(
            DomainCompilerCoverage(
                key=item.key,
                status="supported",
                compiler_ref=by_key[item.key].compiler_ref,
                compiler_digest=by_key[item.key].compiler_digest,
                reverse_verifier_ref=by_key[item.key].reverse_verifier_ref,
                reverse_verifier_digest=by_key[item.key].reverse_verifier_digest,
                output_payload_contract_ref=by_key[item.key].output_payload_contract_ref,
                output_payload_contract_digest=(
                    by_key[item.key].output_payload_contract_digest
                ),
                dependency_digests=by_key[item.key].dependency_digests,
                mutation_event_types=by_key[item.key].mutation_event_types,
            )
            if item.key in by_key
            else item
            for item in DOMAIN_COMPILER_COVERAGE_CATALOG
        )
        material = {
            "manifest_version": ACCEPTANCE_COMPILER_REGISTRY_VERSION,
            "coverage": [item.model_dump(mode="json") for item in self._manifest],
        }
        encoded = json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > MAX_COMPILER_METADATA_BYTES:
            raise AcceptanceCompilerError(
                "metadata_limit_exceeded", "compiler registry metadata exceeds its budget"
            )
        self._manifest_digest = hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _for_test(
        cls, registrations: Sequence[DomainCompilerRegistration]
    ) -> DomainCompilerRegistry:
        """Sealed test registry; its handles and plans are permanently test-only."""

        return cls(registrations, _test_scope=True)

    def _pin_test_authority(
        self,
        *,
        audit: ProposalAuditProjection,
        cursor: ProjectionCursor,
        world_id: str,
    ) -> PinnedProposalAuthorityHandle:
        if not self.__test_scope:
            raise AcceptanceCompilerError(
                "authority_mismatch", "production registry cannot mint test authority"
            )
        return PinnedProposalAuthorityHandle(
            audit, cursor, world_id, _issuer=self._registry_capability
        )

    @property
    def manifest_version(self) -> str:
        return ACCEPTANCE_COMPILER_REGISTRY_VERSION

    @property
    def is_test_scope(self) -> bool:
        return self.__test_scope

    @property
    def manifest(self) -> tuple[DomainCompilerCoverage, ...]:
        return self._manifest

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    def _registration_for(self, key: DomainCompilerKey) -> DomainCompilerRegistration:
        strict = _strict_key(key)
        registration = self._registrations.get(strict)
        if registration is None:
            if strict in _CATALOG_KEYS:
                raise AcceptanceCompilerError(
                    "unsupported_key", "change transition has no installed compiler"
                )
            raise AcceptanceCompilerError("unknown_key", "compiler key is not installed")
        return registration

    def coverage_for(self, key: DomainCompilerKey) -> DomainCompilerCoverage:
        strict = _strict_key(key)
        return next(item for item in self._manifest if item.key == strict)

    def _bound_inputs(
        self,
        *,
        key: DomainCompilerKey,
        proposal_audit: ProposalAuditProjection,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> tuple[
        DomainCompilerRegistration,
        ProposalAuditProjection,
        TypedChange,
        AcceptanceCompilationContext,
        EffectAuthorityRefV2,
    ]:
        registration = self._registration_for(key)
        try:
            audit = ProposalAuditProjection.model_validate(
                dict(object.__getattribute__(proposal_audit, "__dict__")), strict=True
            )
            strict_context = AcceptanceCompilationContext.model_validate(
                dict(object.__getattribute__(context, "__dict__")), strict=True
            )
            strict_change = TypedChange.model_validate(
                dict(object.__getattribute__(change, "__dict__")), strict=True
            )
            proposal = _PROPOSAL_ADAPTER.validate_json(audit.proposal_json, strict=True)
        except AcceptanceCompilerError:
            raise
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise AcceptanceCompilerError(
                "authority_mismatch", "compiler inputs are not pinned authority"
            ) from exc
        audited = next(
            (item for item in proposal.proposed_changes if item.change_id == strict_change.change_id),
            None,
        )
        expected_key = DomainCompilerKey(
            proposal_schema_registry=proposal.schema_registry_version,
            change_kind=strict_change.kind,
            transition=strict_change.transition,
            payload_schema=strict_change.payload.payload_schema,
            payload_version=strict_change.payload.payload_version,
        )
        if (
            audited != strict_change
            or expected_key != registration.key
            or proposal.proposal_id != audit.proposal_id
            or proposal.evaluated_world_revision != strict_context.cursor.world_revision
        ):
            raise AcceptanceCompilerError(
                "authority_mismatch", "change does not exactly bind ProposalAudit and cursor"
            )
        summary = derive_acceptance_manifest_proposal_v2(
            proposal_json=audit.proposal_json,
            proposal_event_ref=audit.event_ref,
            proposal_event_payload_hash=audit.event_payload_hash,
        )
        change_summary = next(item for item in summary.changes if item.change_id == strict_change.change_id)
        authority = EffectAuthorityRefV2(
            proposal_id=proposal.proposal_id,
            authority_kind="change",
            authority_id=strict_change.change_id,
            authority_hash=change_summary.full_change_authority_hash,
        )
        return registration, audit, strict_change, strict_context, authority

    def compile(
        self,
        key: DomainCompilerKey,
        *,
        authority: PinnedProposalAuthorityHandle,
        change: TypedChange,
        context: AcceptanceCompilationContext,
    ) -> CompiledDomainAuthorityHandle:
        owned = isinstance(authority, PinnedProposalAuthorityHandle) and (
            authority.issued_by(self._registry_capability)
            if self.is_test_scope
            else self._authority_reader is not None
            and self._authority_reader.owns(authority)
        )
        if not isinstance(authority, PinnedProposalAuthorityHandle) or not owned or (
            authority.cursor != context.cursor or authority.world_id != context.world_id
        ):
            raise AcceptanceCompilerError(
                "authority_mismatch", "Proposal authority is not pinned to this cursor"
            )
        registration, audit, strict_change, strict_context, authority_ref = self._bound_inputs(
            key=key,
            proposal_audit=authority.audit,
            change=change,
            context=context,
        )
        try:
            raw = registration.adapter.compile(
                proposal_audit=audit,
                change=strict_change,
                context=strict_context,
            )
            draft = DomainPayloadDraft.model_validate(
                dict(object.__getattribute__(raw, "__dict__")), strict=True
            )
        except Exception as exc:
            raise AcceptanceCompilerError("invalid_output", "adapter output is invalid") from exc
        if draft.event_type not in registration.mutation_event_types:
            raise AcceptanceCompilerError("invalid_output", "adapter returned an unowned event")
        try:
            registration.adapter.reverse_verify(
                draft,
                proposal_audit=audit,
                change=strict_change,
                context=strict_context,
            )
        except Exception as exc:
            raise AcceptanceCompilerError(
                "reverse_verification_failed", "adapter rejected its compiled payload"
            ) from exc
        compiled = CompiledDomainPayload(
            event_type=draft.event_type,
            payload_json=draft.payload_json,
            authority_refs=(authority_ref,),
            proposal_event_ref=audit.event_ref,
            proposal_event_payload_hash=audit.event_payload_hash,
            proposal_hash=audit.proposal_hash,
            compiler_key=registration.key,
            compiler_ref=registration.compiler_ref,
            compiler_digest=registration.compiler_digest,
            reverse_verifier_ref=registration.reverse_verifier_ref,
            reverse_verifier_digest=registration.reverse_verifier_digest,
            output_payload_contract_ref=registration.output_payload_contract_ref,
            output_payload_contract_digest=registration.output_payload_contract_digest,
            dependency_digests=registration.dependency_digests,
            registry_version=self.manifest_version,
            registry_digest=self.manifest_digest,
        )
        try:
            from .event_catalog import event_contract
            from .event_identity import domain_idempotency_key

            decoded = json.loads(compiled.payload_json)
            contract = event_contract(compiled.event_type)
            contract.validate_payload(decoded)
            decoded_model = contract.payload_model.model_validate_json(
                compiled.payload_json, strict=True
            )
            canonical_decoded = json.dumps(
                decoded_model.model_dump(mode="json", by_alias=False),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if canonical_decoded != compiled.payload_json:
                raise ValueError("typed event bytes contain defaults or aliases")
            identity = domain_idempotency_key(
                event_type=compiled.event_type,
                world_id=strict_context.world_id,
                payload=decoded,
            )
        except (TypeError, ValidationError, ValueError) as exc:
            raise AcceptanceCompilerError(
                "invalid_event_identity", "compiled payload has no valid domain identity"
            ) from exc
        if identity is None:
            raise AcceptanceCompilerError(
                "invalid_event_identity", "compiled payload has no machine domain identity"
            )
        return CompiledDomainAuthorityHandle(
            compiled,
            proposal_authority=authority,
            change=strict_change,
            context=strict_context,
            _authority=self._registry_capability,
        )

    def reverse_verify(
        self,
        actual: CompiledDomainAuthorityHandle,
        *,
        authority: PinnedProposalAuthorityHandle | None = None,
        change: TypedChange | None = None,
        context: AcceptanceCompilationContext | None = None,
    ) -> None:
        if not isinstance(actual, CompiledDomainAuthorityHandle) or not actual.issued_by(
            self._registry_capability
        ):
            raise AcceptanceCompilerError(
                "authority_mismatch", "compiled handle belongs to another registry"
            )
        stored_authority, stored_change, stored_context = actual.verification_inputs()
        authority = authority or stored_authority
        change = change or stored_change
        context = context or stored_context
        if not isinstance(authority, PinnedProposalAuthorityHandle) or (
            authority.cursor != context.cursor
        ):
            raise AcceptanceCompilerError(
                "authority_mismatch", "Proposal authority is not pinned to this cursor"
            )
        try:
            strict_actual = _strict_payload(actual.payload)
        except Exception as exc:
            raise AcceptanceCompilerError(
                "invalid_output", "compiled payload is structurally invalid"
            ) from exc
        registration, audit, strict_change, strict_context, authority_ref = self._bound_inputs(
            key=strict_actual.compiler_key,
            proposal_audit=authority.audit,
            change=change,
            context=context,
        )
        expected = (
            registration.compiler_ref,
            registration.compiler_digest,
            registration.reverse_verifier_ref,
            registration.reverse_verifier_digest,
            registration.output_payload_contract_ref,
            registration.output_payload_contract_digest,
            registration.dependency_digests,
            self.manifest_version,
            self.manifest_digest,
            (authority_ref,),
            audit.event_ref,
            audit.event_payload_hash,
            audit.proposal_hash,
        )
        actual_binding = (
            strict_actual.compiler_ref,
            strict_actual.compiler_digest,
            strict_actual.reverse_verifier_ref,
            strict_actual.reverse_verifier_digest,
            strict_actual.output_payload_contract_ref,
            strict_actual.output_payload_contract_digest,
            strict_actual.dependency_digests,
            strict_actual.registry_version,
            strict_actual.registry_digest,
            strict_actual.authority_refs,
            strict_actual.proposal_event_ref,
            strict_actual.proposal_event_payload_hash,
            strict_actual.proposal_hash,
        )
        if actual_binding != expected or strict_actual.event_type not in registration.mutation_event_types:
            raise AcceptanceCompilerError("authority_mismatch", "compiled payload binding changed")
        try:
            registration.adapter.reverse_verify(
                DomainPayloadDraft(
                    event_type=strict_actual.event_type,
                    payload_json=strict_actual.payload_json,
                ),
                proposal_audit=audit,
                change=strict_change,
                context=strict_context,
            )
        except Exception as exc:
            raise AcceptanceCompilerError(
                "reverse_verification_failed", "adapter reverse verification failed"
            ) from exc


class PlannedEffect(FrozenModel):
    """Trusted planner output containing exact ledger-event material."""

    ordinal: int = Field(ge=0, lt=MAX_MANIFEST_EFFECTS)
    role: Literal["domain_mutation"] = "domain_mutation"
    event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1, max_length=128)
    payload_json: str = Field(min_length=2, max_length=MAX_COMPILED_PAYLOAD_BYTES)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_refs: tuple[EffectAuthorityRefV2, ...] = Field(min_length=1, max_length=1)
    proposal_event_ref: str = Field(min_length=1)
    proposal_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    proposal_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_key: DomainCompilerKey
    compiler_ref: str = Field(min_length=1, max_length=256)
    compiler_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reverse_verifier_ref: str = Field(min_length=1, max_length=256)
    reverse_verifier_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_payload_contract_ref: str = Field(min_length=1, max_length=256)
    output_payload_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_digests: tuple[DependencyDigest, ...] = Field(
        default=(), max_length=MAX_COMPILER_DEPENDENCIES
    )
    registry_version: Literal["acceptance-domain-compilers.1"]
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: PlannedEventProvenance

    @model_validator(mode="after")
    def payload_bytes_remain_exact(self) -> PlannedEffect:
        if _canonical_payload_hash(self.payload_json) != self.payload_hash:
            raise ValueError("planned payload hash does not match immutable bytes")
        if not isinstance(self.authority_refs[0], EffectAuthorityRefV2) or (
            self.authority_refs[0].authority_kind != "change"
        ):
            raise ValueError("planned domain effect requires change authority")
        return self

def _strict_planned_effect(value: PlannedEffect) -> PlannedEffect:
    stored = object.__getattribute__(value, "__dict__")
    if type(stored) is not dict:
        raise TypeError("planned effect storage is invalid")
    raw = dict(stored)
    refs = raw.get("authority_refs")
    dependencies = raw.get("dependency_digests")
    if type(refs) is not tuple or type(dependencies) is not tuple:
        raise TypeError("planned effect nested authority is invalid")
    raw["authority_refs"] = tuple(
        EffectAuthorityRefV2.model_validate(
            dict(object.__getattribute__(item, "__dict__")), strict=True
        )
        for item in refs
    )
    raw["dependency_digests"] = tuple(
        DependencyDigest.model_validate(
            dict(object.__getattribute__(item, "__dict__")), strict=True
        )
        for item in dependencies
    )
    raw["compiler_key"] = DomainCompilerKey.model_validate(
        dict(object.__getattribute__(raw.get("compiler_key"), "__dict__")), strict=True
    )
    raw["provenance"] = PlannedEventProvenance.model_validate(
        dict(object.__getattribute__(raw.get("provenance"), "__dict__")), strict=True
    )
    return PlannedEffect.model_validate(raw, strict=True)


def _identity_digest(contract: str, material: dict[str, object]) -> str:
    encoded = json.dumps(
        {"contract": contract, **material},
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _compiler_metadata_bytes(payloads: Sequence[CompiledDomainPayload]) -> int:
    material = [
        {
            "authority_refs": [ref.model_dump(mode="json") for ref in payload.authority_refs],
            "proposal_event_ref": payload.proposal_event_ref,
            "proposal_event_payload_hash": payload.proposal_event_payload_hash,
            "proposal_hash": payload.proposal_hash,
            "compiler_key": payload.compiler_key.model_dump(mode="json"),
            "compiler_ref": payload.compiler_ref,
            "compiler_digest": payload.compiler_digest,
            "reverse_verifier_ref": payload.reverse_verifier_ref,
            "reverse_verifier_digest": payload.reverse_verifier_digest,
            "output_payload_contract_ref": payload.output_payload_contract_ref,
            "output_payload_contract_digest": payload.output_payload_contract_digest,
            "dependency_digests": [
                item.model_dump(mode="json") for item in payload.dependency_digests
            ],
            "registry_version": payload.registry_version,
            "registry_digest": payload.registry_digest,
        }
        for payload in payloads
    ]
    return len(
        json.dumps(
            material,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def _planned_identity(
    *,
    context: AcceptanceCompilationContext,
    payload: CompiledDomainPayload,
    ordinal: int,
    authority_scope: Literal["production", "test_only"],
) -> tuple[str, str]:
    authority = [item.model_dump(mode="json") for item in payload.authority_refs]
    common: dict[str, object] = {
        "acceptance_id": context.acceptance_id,
        "acceptance_event_id": context.acceptance_event_id,
        "pre_world_revision": context.pre_world_revision,
        "cursor": context.cursor.model_dump(mode="json"),
        "world_id": context.world_id,
        "ordinal": ordinal,
        "role": payload.role,
        "event_type": payload.event_type,
        "payload_hash": payload.payload_hash,
        "authority_refs": authority,
        "compiler_key": payload.compiler_key.model_dump(mode="json"),
        "compiler_ref": payload.compiler_ref,
        "compiler_digest": payload.compiler_digest,
        "reverse_verifier_ref": payload.reverse_verifier_ref,
        "reverse_verifier_digest": payload.reverse_verifier_digest,
        "output_payload_contract_ref": payload.output_payload_contract_ref,
        "output_payload_contract_digest": payload.output_payload_contract_digest,
        "dependency_digests": [
            item.model_dump(mode="json") for item in payload.dependency_digests
        ],
        "registry_version": payload.registry_version,
        "registry_digest": payload.registry_digest,
        "authority_scope": authority_scope,
        "proposal_event_ref": payload.proposal_event_ref,
        "proposal_event_payload_hash": payload.proposal_event_payload_hash,
        "proposal_hash": payload.proposal_hash,
    }
    from .event_identity import domain_idempotency_key
    from .event_catalog import event_contract

    decoded = json.loads(payload.payload_json)
    event_contract(payload.event_type).validate_payload(decoded)
    identity = domain_idempotency_key(
        event_type=payload.event_type,
        world_id=context.world_id,
        payload=decoded,
    )
    if identity is None:
        raise AcceptanceCompilerError(
            "invalid_event_identity", "planned payload has no machine domain identity"
        )
    return f"effect:{_identity_digest('accepted-effect-event.1', common)}", identity


class AcceptedExecutionPlan(FrozenModel):
    """Ordered pre-manifest effects; it intentionally does not build a manifest."""

    pre_world_revision: int = Field(ge=0)
    cursor: ProjectionCursor
    acceptance_id: str = Field(min_length=1, max_length=256)
    acceptance_event_id: str = Field(min_length=1)
    world_id: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    registry_version: Literal["acceptance-domain-compilers.1"]
    registry_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_scope: Literal["production", "test_only"]
    durable_metadata_contract: Literal["accepted-execution-plan.1"] = (
        "accepted-execution-plan.1"
    )
    ordered_effects: tuple[PlannedEffect, ...] = Field(
        min_length=1, max_length=MAX_MANIFEST_EFFECTS
    )

    @model_validator(mode="after")
    def order_identity_and_causation_are_closed(self) -> AcceptedExecutionPlan:
        strict_effects = tuple(_strict_planned_effect(effect) for effect in self.ordered_effects)
        if self.cursor.world_revision != self.pre_world_revision:
            raise ValueError("execution plan cursor does not match its pre-world revision")
        total_payload_bytes = sum(
            len(effect.payload_json.encode("utf-8")) for effect in strict_effects
        )
        if total_payload_bytes > MAX_EXECUTION_PLAN_PAYLOAD_BYTES:
            raise ValueError("execution plan exceeds its total payload budget")
        context = AcceptanceCompilationContext(
            acceptance_id=self.acceptance_id,
            acceptance_event_id=self.acceptance_event_id,
            cursor=self.cursor,
            world_id=self.world_id,
            logical_time=strict_effects[0].provenance.logical_time,
            created_at=strict_effects[0].provenance.created_at,
            actor=strict_effects[0].provenance.actor,
            source=strict_effects[0].provenance.source,
            trace_id=self.trace_id,
            correlation_id=self.correlation_id,
        )
        expected_cause = self.acceptance_event_id
        event_ids: set[str] = set()
        idempotency_keys: set[str] = set()
        authority_identities: set[tuple[str, str, str]] = set()
        for ordinal, effect in enumerate(strict_effects):
            payload = CompiledDomainPayload(
                event_type=effect.event_type,
                payload_json=effect.payload_json,
                authority_refs=effect.authority_refs,
                proposal_event_ref=effect.proposal_event_ref,
                proposal_event_payload_hash=effect.proposal_event_payload_hash,
                proposal_hash=effect.proposal_hash,
                compiler_key=effect.compiler_key,
                compiler_ref=effect.compiler_ref,
                compiler_digest=effect.compiler_digest,
                reverse_verifier_ref=effect.reverse_verifier_ref,
                reverse_verifier_digest=effect.reverse_verifier_digest,
                output_payload_contract_ref=effect.output_payload_contract_ref,
                output_payload_contract_digest=effect.output_payload_contract_digest,
                dependency_digests=effect.dependency_digests,
                registry_version=effect.registry_version,
                registry_digest=effect.registry_digest,
            )
            event_id, idempotency_key = _planned_identity(
                context=context,
                payload=payload,
                ordinal=ordinal,
                authority_scope=self.authority_scope,
            )
            provenance = effect.provenance
            if (
                effect.ordinal != ordinal
                or effect.event_id != event_id
                or provenance.idempotency_key != idempotency_key
                or provenance.world_id != self.world_id
                or provenance.trace_id != self.trace_id
                or provenance.correlation_id != self.correlation_id
                or provenance.causation_id != expected_cause
                or provenance.logical_time != context.logical_time
                or provenance.created_at != context.created_at
                or provenance.actor != context.actor
                or provenance.source != context.source
                or effect.registry_version != self.registry_version
                or effect.registry_digest != self.registry_digest
            ):
                raise ValueError("planned effect leaves its deterministic Acceptance chain")
            if event_id in event_ids or idempotency_key in idempotency_keys:
                raise ValueError("planned effect identities must be unique")
            authority_identity = (
                effect.authority_refs[0].proposal_id,
                effect.authority_refs[0].authority_kind,
                effect.authority_refs[0].authority_id,
            )
            if authority_identity in authority_identities:
                raise ValueError("planned authority identities must be exact-once")
            event_ids.add(event_id)
            idempotency_keys.add(idempotency_key)
            authority_identities.add(authority_identity)
            expected_cause = event_id
        if _compiler_metadata_bytes(
            tuple(
                CompiledDomainPayload(
                    event_type=effect.event_type,
                    payload_json=effect.payload_json,
                    authority_refs=effect.authority_refs,
                    proposal_event_ref=effect.proposal_event_ref,
                    proposal_event_payload_hash=effect.proposal_event_payload_hash,
                    proposal_hash=effect.proposal_hash,
                    compiler_key=effect.compiler_key,
                    compiler_ref=effect.compiler_ref,
                    compiler_digest=effect.compiler_digest,
                    reverse_verifier_ref=effect.reverse_verifier_ref,
                    reverse_verifier_digest=effect.reverse_verifier_digest,
                    output_payload_contract_ref=effect.output_payload_contract_ref,
                    output_payload_contract_digest=effect.output_payload_contract_digest,
                    dependency_digests=effect.dependency_digests,
                    registry_version=effect.registry_version,
                    registry_digest=effect.registry_digest,
                )
                for effect in strict_effects
            )
        ) > MAX_COMPILER_METADATA_BYTES:
            raise ValueError("execution plan exceeds compiler metadata budget")
        return self

class AcceptedEffectPlanner:
    """The sole owner of deterministic effect identities and event provenance."""

    def __init__(self, *, registry: DomainCompilerRegistry) -> None:
        self._registry = registry

    def plan(
        self,
        *,
        context: AcceptanceCompilationContext,
        authorities: Sequence[CompiledDomainAuthorityHandle],
    ) -> AcceptedExecutionPlan:
        if not 1 <= len(authorities) <= MAX_MANIFEST_EFFECTS:
            raise AcceptanceCompilerError("effect_limit", "effect count is out of bounds")
        strict_payloads: list[CompiledDomainPayload] = []
        try:
            strict_context = AcceptanceCompilationContext.model_validate(
                dict(object.__getattribute__(context, "__dict__")), strict=True
            )
            for handle in authorities:
                if not isinstance(handle, CompiledDomainAuthorityHandle) or not handle.issued_by(
                    self._registry._registry_capability
                ):
                    raise AcceptanceCompilerError(
                        "authority_mismatch", "planner received untrusted compiled authority"
                    )
                stored_authority, _change, stored_context = handle.verification_inputs()
                if (
                    stored_context != strict_context
                    or stored_authority.cursor != strict_context.cursor
                    or stored_authority.world_id != strict_context.world_id
                ):
                    raise AcceptanceCompilerError(
                        "authority_mismatch", "compiled authority belongs to another context"
                    )
                self._registry.reverse_verify(handle)
                strict_payloads.append(_strict_payload(handle.payload))
        except AcceptanceCompilerError:
            raise
        except (AttributeError, TypeError, ValidationError, ValueError) as exc:
            raise AcceptanceCompilerError(
                "invalid_payload", "compiled domain payload is invalid"
            ) from exc
        if sum(len(payload.payload_json.encode("utf-8")) for payload in strict_payloads) > (
            MAX_EXECUTION_PLAN_PAYLOAD_BYTES
        ):
            raise AcceptanceCompilerError(
                "plan_limit_exceeded", "execution plan payload budget exceeded"
            )
        if _compiler_metadata_bytes(strict_payloads) > MAX_COMPILER_METADATA_BYTES:
            raise AcceptanceCompilerError(
                "plan_limit_exceeded", "execution plan metadata budget exceeded"
            )
        registries = {
            (payload.registry_version, payload.registry_digest) for payload in strict_payloads
        }
        if len(registries) != 1:
            raise AcceptanceCompilerError(
                "registry_mismatch", "execution plan mixes compiler registries"
            )
        registry_version, registry_digest = next(iter(registries))
        authority_keys = [
            (
                ref.proposal_id,
                ref.authority_kind,
                ref.authority_id,
            )
            for payload in strict_payloads
            for ref in payload.authority_refs
        ]
        if len(authority_keys) != len(set(authority_keys)):
            raise AcceptanceCompilerError(
                "authority_reused", "one typed authority cannot produce two effects"
            )
        effects: list[PlannedEffect] = []
        cause = strict_context.acceptance_event_id
        for ordinal, payload in enumerate(strict_payloads):
            try:
                event_id, idempotency_key = _planned_identity(
                    context=strict_context,
                    payload=payload,
                    ordinal=ordinal,
                    authority_scope=(
                        "test_only" if self._registry.is_test_scope else "production"
                    ),
                )
            except (TypeError, ValidationError, ValueError) as exc:
                if isinstance(exc, AcceptanceCompilerError):
                    raise
                raise AcceptanceCompilerError(
                    "invalid_event_identity", "payload has no valid machine event identity"
                ) from exc
            effects.append(
                PlannedEffect(
                    ordinal=ordinal,
                    event_id=event_id,
                    event_type=payload.event_type,
                    payload_json=payload.payload_json,
                    payload_hash=payload.payload_hash,
                    authority_refs=payload.authority_refs,
                    proposal_event_ref=payload.proposal_event_ref,
                    proposal_event_payload_hash=payload.proposal_event_payload_hash,
                    proposal_hash=payload.proposal_hash,
                    compiler_key=payload.compiler_key,
                    compiler_ref=payload.compiler_ref,
                    compiler_digest=payload.compiler_digest,
                    reverse_verifier_ref=payload.reverse_verifier_ref,
                    reverse_verifier_digest=payload.reverse_verifier_digest,
                    output_payload_contract_ref=payload.output_payload_contract_ref,
                    output_payload_contract_digest=payload.output_payload_contract_digest,
                    dependency_digests=payload.dependency_digests,
                    registry_version=payload.registry_version,
                    registry_digest=payload.registry_digest,
                    provenance=PlannedEventProvenance(
                        world_id=strict_context.world_id,
                        logical_time=strict_context.logical_time,
                        created_at=strict_context.created_at,
                        actor=strict_context.actor,
                        source=strict_context.source,
                        trace_id=strict_context.trace_id,
                        causation_id=cause,
                        correlation_id=strict_context.correlation_id,
                        idempotency_key=idempotency_key,
                    ),
                )
            )
            cause = event_id
        return AcceptedExecutionPlan(
            pre_world_revision=strict_context.pre_world_revision,
            cursor=strict_context.cursor,
            acceptance_id=strict_context.acceptance_id,
            acceptance_event_id=strict_context.acceptance_event_id,
            world_id=strict_context.world_id,
            trace_id=strict_context.trace_id,
            correlation_id=strict_context.correlation_id,
            registry_version=registry_version,
            registry_digest=registry_digest,
            authority_scope=("test_only" if self._registry.is_test_scope else "production"),
            ordered_effects=tuple(effects),
        )


__all__ = [
    "ACCEPTANCE_COMPILER_REGISTRY_VERSION",
    "DOMAIN_COMPILER_COVERAGE_CATALOG",
    "AcceptedEffectPlanner",
    "AcceptedExecutionPlan",
    "AcceptanceCompilationContext",
    "AcceptanceCompilerError",
    "CompiledDomainPayload",
    "DomainCompilerCoverage",
    "DomainCompilerKey",
    "DomainCompilerRegistration",
    "DomainCompilerRegistry",
    "ManifestDomainMutationAdapter",
    "PlannedEffect",
    "PlannedEventProvenance",
    "UnsupportedDomainMutationAdapter",
]
