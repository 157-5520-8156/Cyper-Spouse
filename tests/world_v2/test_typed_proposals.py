from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from companion_daemon.world_v2.typed_proposals import (
    AmbiguousTypedProposalAuthority,
    DuplicateTypedProposalRegistration,
    ProposalAuthorityBinding,
    RecordSelector,
    TYPED_PROPOSAL_ENCODING,
    TypedProposalRegistration,
    TypedProposalRegistry,
    TypedProposalRegistryError,
    UnknownTypedProposalContract,
)


class FakeCodec:
    def decode_record(self, *, event_type: str, payload: dict[str, object]) -> object:
        return payload

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        assert isinstance(proposal, dict)
        return ProposalAuthorityBinding(
            proposal_id=str(proposal["proposal_id"]),
            proposal_kind=str(proposal["proposal_kind"]),
            authority_contract_ref=str(proposal["authority_contract_ref"]),
            change_id=str(proposal["change_id"]),
            proposed_change_hash=str(proposal["proposed_change_hash"]),
            evaluated_world_revision=int(proposal["evaluated_world_revision"]),
            expected_entity_revision=int(proposal["expected_entity_revision"]),
            mutation_event_type=str(proposal["mutation_event_type"]),
        )

    def decode_mutation(self, *, event_type: str, payload: dict[str, object]) -> object:
        return payload


class ForgedBindingCodec(FakeCodec):
    def __init__(self, field_name: str, forged_value: object) -> None:
        self.field_name = field_name
        self.forged_value = forged_value

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        binding = super().bind(proposal)
        return replace(binding, **{self.field_name: self.forged_value})


class FakeStore:
    def validate_and_store(self, state: object, event: object, proposal: object) -> object:
        return state

    def find(self, state: object, proposal_id: str) -> object | None:
        return None

    def discard(self, state: object, proposal_id: str) -> object:
        return state


class IndexedFakeStore:
    def __init__(self, index_name: str) -> None:
        self.index_name = index_name

    def validate_and_store(self, state: object, event: object, proposal: object) -> object:
        return state

    def find(self, state: object, proposal_id: str) -> object | None:
        assert isinstance(state, dict)
        index = state.get(self.index_name, {})
        assert isinstance(index, dict)
        return index.get(proposal_id)

    def discard(self, state: object, proposal_id: str) -> object:
        assert isinstance(state, dict)
        updated = {key: dict(value) for key, value in state.items()}
        updated[self.index_name].pop(proposal_id, None)
        return updated


def registration(
    *,
    contract_ref: str = "proposal-contract:relationship.1",
    proposal_kind: str = "relationship_transition",
    mutation_event_types: tuple[str, ...] = ("RelationshipAdjusted",),
    codec: object | None = None,
    store: object | None = None,
) -> TypedProposalRegistration:
    return TypedProposalRegistration(
        contract_ref=contract_ref,
        selector=RecordSelector(
            event_type="ProposalRecorded",
            proposal_kind=proposal_kind,
        ),
        mutation_event_types=mutation_event_types,
        codec=codec or FakeCodec(),  # type: ignore[arg-type]
        store=store or FakeStore(),  # type: ignore[arg-type]
    )


def test_registry_exposes_a_frozen_canonical_manifest() -> None:
    registry = TypedProposalRegistry(
        (
            registration(
                contract_ref="proposal-contract:thread.1",
                proposal_kind="thread_transition",
                mutation_event_types=("ThreadOpened", "ThreadResolved"),
            ),
            registration(),
        )
    )

    assert tuple(item.contract_ref for item in registry.manifest) == (
        "proposal-contract:relationship.1",
        "proposal-contract:thread.1",
    )
    with pytest.raises(FrozenInstanceError):
        registry.manifest[0].contract_ref = "proposal-contract:changed.1"  # type: ignore[misc]


def test_record_routing_is_explicit_and_unknown_typed_contracts_fail_closed() -> None:
    installed = registration()
    registry = TypedProposalRegistry((installed,))
    selector_payload = {
        "proposal_kind": "relationship_transition",
        "authority_contract_ref": "proposal-contract:relationship.1",
    }

    assert registry.registration_for_record("ProposalRecorded", selector_payload) is None
    assert registry.registration_for_record(
        "ProposalRecorded",
        {**selector_payload, "proposal_encoding": TYPED_PROPOSAL_ENCODING},
    ) is installed

    with pytest.raises(
        UnknownTypedProposalContract,
        match="proposal-contract:not-installed.1",
    ):
        registry.registration_for_record(
            "ProposalRecorded",
            {
                **selector_payload,
                "proposal_encoding": TYPED_PROPOSAL_ENCODING,
                "authority_contract_ref": "proposal-contract:not-installed.1",
            },
        )


@pytest.mark.parametrize(
    ("registrations", "collision"),
    (
        (
            (registration(), registration()),
            "contract",
        ),
        (
            (
                registration(),
                registration(contract_ref="proposal-contract:relationship.2"),
            ),
            "record selector",
        ),
        (
            (
                registration(),
                registration(
                    contract_ref="proposal-contract:thread.1",
                    proposal_kind="thread_transition",
                ),
            ),
            "mutation event",
        ),
    ),
)
def test_registry_rejects_ambiguous_authority_ownership(
    registrations: tuple[TypedProposalRegistration, ...],
    collision: str,
) -> None:
    with pytest.raises(DuplicateTypedProposalRegistration, match=collision):
        TypedProposalRegistry(registrations)


def test_manifest_digest_is_stable_across_registration_order() -> None:
    relationship = registration()
    thread = registration(
        contract_ref="proposal-contract:thread.1",
        proposal_kind="thread_transition",
        mutation_event_types=("ThreadResolved", "ThreadOpened"),
    )

    assert TypedProposalRegistry((relationship, thread)).manifest_digest == (
        "c850bbfe2bc8697106ab29e7d66bdfd54bc5938d18848c124466c7c3c45c7dd4"
    )
    assert TypedProposalRegistry((thread, relationship)).manifest_digest == (
        "c850bbfe2bc8697106ab29e7d66bdfd54bc5938d18848c124466c7c3c45c7dd4"
    )


def test_explicit_typed_records_cannot_silently_downgrade_to_generic() -> None:
    registry = TypedProposalRegistry((registration(),))

    with pytest.raises(TypedProposalRegistryError, match="encoding"):
        registry.registration_for_record(
            "ProposalRecorded",
            {
                "proposal_encoding": "typed-authority-v2",
                "authority_contract_ref": "proposal-contract:relationship.1",
                "proposal_kind": "relationship_transition",
            },
        )
    with pytest.raises(TypedProposalRegistryError, match="does not own selector"):
        registry.registration_for_record(
            "WrongEvent",
            {
                "proposal_encoding": TYPED_PROPOSAL_ENCODING,
                "authority_contract_ref": "proposal-contract:relationship.1",
                "proposal_kind": "relationship_transition",
            },
        )


def test_mutation_routing_uses_the_registration_manifest_ownership() -> None:
    installed = registration()
    registry = TypedProposalRegistry((installed,))

    assert registry.registration_for_mutation("RelationshipAdjusted") is installed
    assert registry.registration_for_mutation("UnregisteredMutation") is None


def proposal(*, proposal_id: str = "proposal:relationship:1") -> dict[str, object]:
    return {
        "proposal_id": proposal_id,
        "proposal_kind": "relationship_transition",
        "authority_contract_ref": "proposal-contract:relationship.1",
        "change_id": "change:relationship:1",
        "proposed_change_hash": "a" * 64,
        "evaluated_world_revision": 7,
        "expected_entity_revision": 2,
        "mutation_event_type": "RelationshipAdjusted",
    }


def test_authority_for_returns_the_unique_registration_and_normalized_binding() -> None:
    installed = registration(store=IndexedFakeStore("relationships"))
    registry = TypedProposalRegistry((installed,))

    match = registry.authority_for(
        {"relationships": {"proposal:relationship:1": proposal()}},
        "proposal:relationship:1",
    )

    assert match == (
        installed,
        ProposalAuthorityBinding(
            proposal_id="proposal:relationship:1",
            proposal_kind="relationship_transition",
            authority_contract_ref="proposal-contract:relationship.1",
            change_id="change:relationship:1",
            proposed_change_hash="a" * 64,
            evaluated_world_revision=7,
            expected_entity_revision=2,
            mutation_event_type="RelationshipAdjusted",
        ),
    )


def test_authority_for_returns_none_when_no_typed_store_claims_the_proposal() -> None:
    registry = TypedProposalRegistry(
        (registration(store=IndexedFakeStore("relationships")),)
    )

    assert registry.authority_for({"relationships": {}}, "proposal:missing") is None


def test_authority_for_rejects_multiple_store_claims() -> None:
    relationship = registration(store=IndexedFakeStore("relationships"))
    thread = registration(
        contract_ref="proposal-contract:thread.1",
        proposal_kind="thread_transition",
        mutation_event_types=("ThreadOpened",),
        store=IndexedFakeStore("threads"),
    )
    registry = TypedProposalRegistry((relationship, thread))
    state = {
        "relationships": {"proposal:collision": proposal(proposal_id="proposal:collision")},
        "threads": {"proposal:collision": {"proposal_id": "proposal:collision"}},
    }

    with pytest.raises(AmbiguousTypedProposalAuthority, match="multiple typed stores"):
        registry.authority_for(state, "proposal:collision")


def test_discard_decided_uses_only_the_unique_claiming_store() -> None:
    registry = TypedProposalRegistry(
        (registration(store=IndexedFakeStore("relationships")),)
    )
    state = {"relationships": {"proposal:relationship:1": proposal()}}

    discarded = registry.discard_decided(state, "proposal:relationship:1")

    assert discarded == {"relationships": {}}
    untouched = registry.discard_decided(discarded, "proposal:missing")
    assert untouched is discarded


@pytest.mark.parametrize(
    ("field_name", "forged_value", "error_fragment"),
    (
        ("proposal_id", "proposal:other", "proposal identity"),
        ("authority_contract_ref", "proposal-contract:other.1", "contract"),
        ("proposal_kind", "other_transition", "proposal kind"),
        ("mutation_event_type", "OtherMutation", "mutation event"),
    ),
)
def test_authority_for_rejects_a_codec_binding_outside_its_registration(
    field_name: str,
    forged_value: object,
    error_fragment: str,
) -> None:
    registry = TypedProposalRegistry(
        (
            registration(
                codec=ForgedBindingCodec(field_name, forged_value),
                store=IndexedFakeStore("relationships"),
            ),
        )
    )

    with pytest.raises(TypedProposalRegistryError, match=error_fragment):
        registry.authority_for(
            {"relationships": {"proposal:relationship:1": proposal()}},
            "proposal:relationship:1",
        )
