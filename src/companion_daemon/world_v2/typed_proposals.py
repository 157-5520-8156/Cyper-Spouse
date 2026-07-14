"""Deterministic registry contracts for explicitly typed proposals.

The registry is intentionally independent from reducers and domain schemas.  A
domain installs a codec and a store adapter, while the registry owns only
unambiguous routing and a stable manifest of that installed authority surface.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Protocol, TypeVar


StateT = TypeVar("StateT")
ProposalT = TypeVar("ProposalT")
TYPED_PROPOSAL_ENCODING = "typed-authority-v1"


class TypedProposalRegistryError(ValueError):
    """Base error for invalid or ambiguous typed-proposal routing."""


class UnknownTypedProposalContract(TypedProposalRegistryError):
    """An explicitly typed record names a contract that is not installed."""


class DuplicateTypedProposalRegistration(TypedProposalRegistryError):
    """Two registrations claim the same deterministic routing key."""


class AmbiguousTypedProposalAuthority(TypedProposalRegistryError):
    """More than one installed store claims the same proposal identity."""


@dataclass(frozen=True, slots=True)
class ProposalAuthorityBinding:
    proposal_id: str
    proposal_kind: str
    authority_contract_ref: str
    change_id: str
    proposed_change_hash: str
    evaluated_world_revision: int
    expected_entity_revision: int
    mutation_event_type: str


class ProposalCodec(Protocol[ProposalT]):
    def decode_record(self, *, event_type: str, payload: dict[str, object]) -> ProposalT: ...

    def bind(self, proposal: ProposalT) -> ProposalAuthorityBinding: ...

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object: ...


class ProposalStore(Protocol[StateT, ProposalT]):
    def validate_and_store(self, state: StateT, event: object, proposal: ProposalT) -> StateT: ...

    def find(self, state: StateT, proposal_id: str) -> ProposalT | None: ...

    def discard(self, state: StateT, proposal_id: str) -> StateT: ...


@dataclass(frozen=True, slots=True, order=True)
class RecordSelector:
    event_type: str
    proposal_kind: str


@dataclass(frozen=True, slots=True)
class TypedProposalRegistration:
    contract_ref: str
    selector: RecordSelector
    mutation_event_types: tuple[str, ...]
    codec: ProposalCodec[object]
    store: ProposalStore[object, object]


@dataclass(frozen=True, slots=True)
class RegistrationManifestEntry:
    contract_ref: str
    selector: RecordSelector
    mutation_event_types: tuple[str, ...]


class TypedProposalRegistry:
    def __init__(self, registrations: tuple[TypedProposalRegistration, ...]) -> None:
        ordered = sorted(registrations, key=lambda item: item.contract_ref)
        seen_contracts: set[str] = set()
        seen_selectors: set[RecordSelector] = set()
        mutation_owners: dict[str, str] = {}
        for item in ordered:
            if item.contract_ref in seen_contracts:
                raise DuplicateTypedProposalRegistration(
                    f"duplicate typed proposal contract {item.contract_ref!r}"
                )
            seen_contracts.add(item.contract_ref)
            if item.selector in seen_selectors:
                raise DuplicateTypedProposalRegistration(
                    f"duplicate typed proposal record selector {item.selector!r}"
                )
            seen_selectors.add(item.selector)
            for event_type in item.mutation_event_types:
                owner = mutation_owners.get(event_type)
                if owner is not None:
                    raise DuplicateTypedProposalRegistration(
                        f"mutation event {event_type!r} is owned by both {owner!r} "
                        f"and {item.contract_ref!r}"
                    )
                mutation_owners[event_type] = item.contract_ref
        self._by_contract = {item.contract_ref: item for item in ordered}
        self._registrations = tuple(ordered)
        self._by_mutation_event = {
            event_type: item
            for item in ordered
            for event_type in item.mutation_event_types
        }
        self._manifest = tuple(
            RegistrationManifestEntry(
                contract_ref=item.contract_ref,
                selector=item.selector,
                mutation_event_types=tuple(sorted(item.mutation_event_types)),
            )
            for item in ordered
        )
        manifest_json = json.dumps(
            {
                "manifest_version": "typed-proposal-registry.1",
                "proposal_encoding": TYPED_PROPOSAL_ENCODING,
                "registrations": [
                    {
                        "contract_ref": item.contract_ref,
                        "mutation_event_types": list(item.mutation_event_types),
                        "record_selector": {
                            "event_type": item.selector.event_type,
                            "proposal_kind": item.selector.proposal_kind,
                        },
                    }
                    for item in self._manifest
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self._manifest_digest = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()

    @property
    def manifest(self) -> tuple[RegistrationManifestEntry, ...]:
        return self._manifest

    @property
    def manifest_digest(self) -> str:
        return self._manifest_digest

    def registration_for_record(
        self,
        event_type: str,
        payload: dict[str, object],
    ) -> TypedProposalRegistration | None:
        """Resolve an explicitly typed record; leave unmarked records generic."""

        encoding = payload.get("proposal_encoding")
        if encoding is None:
            return None
        if encoding != TYPED_PROPOSAL_ENCODING:
            raise TypedProposalRegistryError(
                f"typed proposal encoding {encoding!r} is not installed"
            )
        contract_ref = payload.get("authority_contract_ref")
        if not isinstance(contract_ref, str) or contract_ref not in self._by_contract:
            raise UnknownTypedProposalContract(
                f"typed proposal contract {contract_ref!r} is not installed"
            )
        registration = self._by_contract[contract_ref]
        selector = RecordSelector(
            event_type=event_type,
            proposal_kind=str(payload.get("proposal_kind", "")),
        )
        if selector != registration.selector:
            raise TypedProposalRegistryError(
                f"typed proposal contract {contract_ref!r} does not own selector {selector!r}"
            )
        return registration

    def registration_for_mutation(
        self,
        event_type: str,
    ) -> TypedProposalRegistration | None:
        return self._by_mutation_event.get(event_type)

    def authority_for(
        self,
        state: object,
        proposal_id: str,
    ) -> tuple[TypedProposalRegistration, ProposalAuthorityBinding] | None:
        matches = self._store_claims(state, proposal_id)
        if not matches:
            return None
        registration, proposal = matches[0]
        binding = registration.codec.bind(proposal)
        self._validate_binding(registration, binding, requested_proposal_id=proposal_id)
        return registration, binding

    def discard_decided(self, state: object, proposal_id: str) -> object:
        matches = self._store_claims(state, proposal_id)
        if not matches:
            return state
        registration, _ = matches[0]
        return registration.store.discard(state, proposal_id)

    def _store_claims(
        self,
        state: object,
        proposal_id: str,
    ) -> list[tuple[TypedProposalRegistration, object]]:
        matches = [
            (registration, proposal)
            for registration in self._registrations
            if (proposal := registration.store.find(state, proposal_id)) is not None
        ]
        if len(matches) > 1:
            owners = tuple(item.contract_ref for item, _ in matches)
            raise AmbiguousTypedProposalAuthority(
                f"proposal {proposal_id!r} is claimed by multiple typed stores: {owners!r}"
            )
        return matches

    def _validate_binding(
        self,
        registration: TypedProposalRegistration,
        binding: ProposalAuthorityBinding,
        *,
        requested_proposal_id: str,
    ) -> None:
        if not isinstance(binding, ProposalAuthorityBinding):
            raise TypedProposalRegistryError("typed proposal codec returned an invalid binding")
        if binding.proposal_id != requested_proposal_id:
            raise TypedProposalRegistryError(
                "typed proposal binding does not match the requested proposal identity"
            )
        if binding.authority_contract_ref != registration.contract_ref:
            raise TypedProposalRegistryError(
                "typed proposal binding contract does not match its registration"
            )
        if binding.proposal_kind != registration.selector.proposal_kind:
            raise TypedProposalRegistryError(
                "typed proposal binding proposal kind does not match its registration"
            )
        if self._by_mutation_event.get(binding.mutation_event_type) is not registration:
            raise TypedProposalRegistryError(
                "typed proposal binding mutation event is not owned by its registration"
            )
