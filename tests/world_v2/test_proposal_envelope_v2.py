from __future__ import annotations

from copy import deepcopy
import ast
import inspect
import json

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.proposal_envelope_v2 import (
    FACT_COMMIT_PROPOSAL_REGISTRY_V2,
    MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES,
    MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES,
    FactCommitProposalDraftV2,
    FactCommitProposalEnvelopeV2,
    FactCommitProposalNormalizationContextV2,
    canonical_fact_commit_proposal_v2_hash,
    canonical_fact_commit_proposal_v2_json,
    canonical_full_change_authority_hash_v2,
    normalize_fact_commit_proposal_v2,
    validate_fact_commit_proposal_v2,
)


def _hex(character: str) -> str:
    return character * 64


def _use(ref: str, *, purpose: str = "current_fact", anchor: bool = True) -> dict[str, object]:
    return {"evidence_ref": ref, "purpose": purpose, "anchor": anchor}


def _intent(subject: str = "user:1", ref: str = "observation:1") -> dict[str, object]:
    return {
        "subject_ref": subject,
        "predicate_code": "profile.display_name",
        "value_ref": f"value:{subject}",
        "value_hash": "sha256:" + _hex("b"),
        "assertion_source_ref": ref,
        "evidence_uses": (_use(ref),),
        "confidence_bp": 8_500,
        "privacy_class": "personal",
    }


def _evidence(ref: str = "observation:1", kind: str = "observed_message") -> dict[str, object]:
    return {
        "ref_id": ref,
        "evidence_kind": kind,
        "source_world_revision": 12,
        "immutable_hash": "sha256:" + _hex("a"),
    }


def _draft(*intents: dict[str, object]) -> dict[str, object]:
    return {
        "fact_commit_intents": intents or (_intent(),),
        "confidence": 8_000,
        "brief_rationale": "The user stated a durable profile fact.",
    }


def _context(*evidence: dict[str, object]) -> dict[str, object]:
    return {
        "world_id": "world:1",
        "proposal_id": "proposal:1",
        "trigger_ref": "trigger:1",
        "evaluated_world_revision": 12,
        "evidence_refs": evidence or (_evidence(),),
        "policy_refs": ("policy:fact-commit.2",),
    }


def test_normalizer_builds_exact_inert_v2_proposal() -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())

    assert isinstance(proposal, FactCommitProposalEnvelopeV2)
    assert proposal.schema_registry_version == "world-v2-proposals.2"
    assert proposal.action_intents == ()
    assert proposal.proposed_changes[0].change_id.startswith("change:")
    assert proposal.proposed_changes[0].payload.payload_schema == "fact_commit_intent.v2"
    assert proposal.proposed_changes[0].payload.payload_version == 2
    assert proposal.proposed_changes[0].expected_entity_revision == 0
    assert proposal.proposed_changes[0].preconditions == ()
    assert proposal.proposed_changes[0].policy_refs == ("policy:fact-commit.2",)
    assert validate_fact_commit_proposal_v2(proposal, world_id="world:1") == proposal


def test_intent_hashes_are_deduplicated_then_changes_are_sorted_by_change_id() -> None:
    first = _intent("user:z", "observation:z")
    second = _intent("user:a", "observation:a")
    context = _context(_evidence("observation:a"), _evidence("observation:z"))
    proposal = normalize_fact_commit_proposal_v2(
        draft=_draft(first, second), context=context
    )
    change_ids = tuple(change.change_id for change in proposal.proposed_changes)
    hashes = tuple(change.payload.payload_hash for change in proposal.proposed_changes)
    assert change_ids == tuple(sorted(change_ids))
    assert len(hashes) == len(set(hashes))

    with pytest.raises((ValidationError, ValueError), match="duplicate intent hash"):
        normalize_fact_commit_proposal_v2(draft=_draft(first, deepcopy(first)), context=context)


def test_twenty_intents_prove_change_order_is_not_payload_hash_order() -> None:
    intents = tuple(_intent(f"user:{index:02}", f"observation:{index:02}") for index in range(20))
    context = _context(
        *(_evidence(f"observation:{index:02}") for index in range(20))
    )
    proposal = normalize_fact_commit_proposal_v2(
        draft=_draft(*intents), context=context
    )
    change_ids = tuple(item.change_id for item in proposal.proposed_changes)
    payload_hashes = tuple(item.payload.payload_hash for item in proposal.proposed_changes)
    assert change_ids == tuple(sorted(change_ids))
    assert payload_hashes != tuple(sorted(payload_hashes))


@pytest.mark.parametrize("count", [0, 65])
def test_normalizer_requires_one_to_sixty_four_intents(count: int) -> None:
    draft = _draft()
    draft["fact_commit_intents"] = tuple(_intent() for _ in range(count))
    with pytest.raises((ValidationError, ValueError)):
        normalize_fact_commit_proposal_v2(draft=draft, context=_context())


def test_change_and_fact_ids_are_domain_separated_stable_and_not_ordinal_based() -> None:
    first = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    second = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    change = first.proposed_changes[0]
    assert first == second
    assert change.change_id == (
        "change:4df86de97bba83ca7c1f8c4a3d5c8d05e536d828555c6ed235503bc49cb6667d"
    )
    assert change.target_id == (
        "fact:d16c144d7a521a23a1b2e1f42b21133ab15583c8b7f396609e5c08c71efd1c5d"
    )

    changed_world = normalize_fact_commit_proposal_v2(
        draft=_draft(), context={**_context(), "world_id": "world:2"}
    )
    assert changed_world.proposed_changes[0].change_id != change.change_id
    assert changed_world.proposed_changes[0].target_id != change.target_id


@pytest.mark.parametrize(
    "system_field",
    ["proposal_kind", "proposal_id", "world_id", "schema_registry_version", "action_intents"],
)
def test_draft_rejects_every_system_owned_field(system_field: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        normalize_fact_commit_proposal_v2(
            draft={**_draft(), system_field: "forged"}, context=_context()
        )


def test_context_evidence_can_be_larger_but_output_is_exact_used_union() -> None:
    operator = _intent("user:2", "operator:1")
    proposal = normalize_fact_commit_proposal_v2(
        draft=_draft(_intent(), operator),
        context=_context(
            _evidence(),
            _evidence("operator:1", "operator_observation"),
            _evidence("unused:1", "committed_fact"),
        ),
    )
    assert tuple(item.ref_id for item in proposal.evidence_refs) == (
        "observation:1",
        "operator:1",
    )


@pytest.mark.parametrize(
    "context_update",
    [
        {"evidence_refs": (_evidence(), _evidence())},
        {"evidence_refs": (_evidence("other:1"),)},
        {"evidence_refs": (_evidence("observation:1", "committed_fact"),)},
        {"policy_refs": ()},
        {"policy_refs": ("policy:z", "policy:a")},
        {"policy_refs": ("policy:a", "policy:a")},
    ],
)
def test_context_claims_are_canonical_unique_and_exactly_resolved(
    context_update: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        normalize_fact_commit_proposal_v2(
            draft=_draft(), context={**_context(), **context_update}
        )


def test_public_parser_revalidates_hostile_constructs_and_world_ids() -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    change = proposal.proposed_changes[0]
    hostile_intent = json.loads(change.payload.canonical_json)
    hostile_intent["confidence_bp"] = 99_999
    hostile_payload = type(change.payload).model_construct(
        **{
            **change.payload.__dict__,
            "canonical_json": json.dumps(
                hostile_intent, sort_keys=True, separators=(",", ":")
            ),
        }
    )
    hostile_change = type(change).model_construct(
        **{**change.__dict__, "payload": hostile_payload}
    )
    forged = FactCommitProposalEnvelopeV2.model_construct(
        **{**proposal.__dict__, "proposed_changes": (hostile_change,)}
    )
    with pytest.raises((ValidationError, ValueError)):
        validate_fact_commit_proposal_v2(forged, world_id="world:1")
    with pytest.raises(ValueError, match="world"):
        validate_fact_commit_proposal_v2(proposal, world_id="world:other")


@pytest.mark.parametrize(
    "evidence_update",
    [
        {"evidence_kind": "committed_fact"},
        {"source_world_revision": 13},
    ],
)
def test_public_parser_rechecks_resolved_evidence_claims(
    evidence_update: dict[str, object],
) -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    evidence = proposal.evidence_refs[0].model_copy(update=evidence_update)
    forged = proposal.model_copy(update={"evidence_refs": (evidence,)})
    with pytest.raises((ValidationError, ValueError)):
        validate_fact_commit_proposal_v2(forged, world_id="world:1")


def test_public_parser_requires_one_canonical_policy_claim_across_changes() -> None:
    proposal = normalize_fact_commit_proposal_v2(
        draft=_draft(
            _intent("user:1", "observation:1"),
            _intent("user:2", "observation:2"),
        ),
        context=_context(_evidence("observation:1"), _evidence("observation:2")),
    )
    second = proposal.proposed_changes[1].model_copy(
        update={"policy_refs": ("policy:other.2",)}
    )
    forged_changes = tuple(sorted((proposal.proposed_changes[0], second), key=lambda item: item.change_id))
    forged = proposal.model_copy(update={"proposed_changes": forged_changes})
    with pytest.raises(ValueError, match="same canonical policy"):
        validate_fact_commit_proposal_v2(forged, world_id="world:1")


def test_canonical_roundtrip_hash_and_full_change_authority_are_stable() -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    canonical = canonical_fact_commit_proposal_v2_json(proposal, world_id="world:1")
    assert json.dumps(json.loads(canonical), sort_keys=True, separators=(",", ":")) == canonical
    assert validate_fact_commit_proposal_v2(json.loads(canonical), world_id="world:1") == proposal
    assert canonical_fact_commit_proposal_v2_hash(proposal, world_id="world:1").startswith(
        "sha256:"
    )
    assert len(canonical_full_change_authority_hash_v2(proposal.proposed_changes[0])) == 64


def test_registry_is_one_frozen_joint_key_and_has_no_register_interface() -> None:
    assert len(FACT_COMMIT_PROPOSAL_REGISTRY_V2) == 1
    key = FACT_COMMIT_PROPOSAL_REGISTRY_V2[0]
    assert key.model_dump() == {
        "proposal_schema_registry": "world-v2-proposals.2",
        "change_kind": "fact_transition",
        "transition": "commit",
        "payload_schema": "fact_commit_intent.v2",
        "payload_version": 2,
    }
    assert not hasattr(FACT_COMMIT_PROPOSAL_REGISTRY_V2, "register")


def test_all_fields_are_required_and_action_schema_cannot_expose_any() -> None:
    schemas = (
        FactCommitProposalDraftV2.model_json_schema(),
        FactCommitProposalNormalizationContextV2.model_json_schema(),
        FactCommitProposalEnvelopeV2.model_json_schema(),
    )
    for schema in schemas:
        assert set(schema["required"]) == set(schema["properties"])
    action_schema = FactCommitProposalEnvelopeV2.model_json_schema()["properties"][
        "action_intents"
    ]
    assert action_schema == {
        "maxItems": 0,
        "minItems": 0,
        "title": "Action Intents",
        "type": "array",
    }


def test_payload_and_envelope_utf8_budgets_are_real_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import companion_daemon.world_v2.proposal_envelope_v2 as contracts

    proposal = normalize_fact_commit_proposal_v2(
        draft={**_draft(), "brief_rationale": "验" * 50}, context=_context()
    )
    canonical = canonical_fact_commit_proposal_v2_json(proposal, world_id="world:1")
    monkeypatch.setattr(contracts, "MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES", len(canonical.encode()))
    assert canonical_fact_commit_proposal_v2_json(proposal, world_id="world:1") == canonical
    monkeypatch.setattr(
        contracts, "MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES", len(canonical.encode()) - 1
    )
    with pytest.raises(ValueError, match="envelope.*byte budget"):
        canonical_fact_commit_proposal_v2_json(proposal, world_id="world:1")

    payload = proposal.proposed_changes[0].payload.canonical_json
    monkeypatch.setattr(
        contracts, "MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES", len(canonical.encode())
    )
    monkeypatch.setattr(contracts, "MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES", len(payload.encode()))
    assert validate_fact_commit_proposal_v2(proposal, world_id="world:1") == proposal
    monkeypatch.setattr(
        contracts, "MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES", len(payload.encode()) - 1
    )
    with pytest.raises(ValueError, match="payload.*byte budget"):
        validate_fact_commit_proposal_v2(proposal, world_id="world:1")


@pytest.mark.parametrize("attack", ["key", "string"])
def test_preflight_rejects_oversized_key_or_string_before_whole_document_encoding(
    attack: str,
) -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    raw = proposal.model_dump(mode="python")
    oversized = "界" * MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES
    if attack == "key":
        raw[oversized] = None
    else:
        raw["brief_rationale"] = oversized
    with pytest.raises(ValueError, match="preflight UTF-8 byte budget"):
        validate_fact_commit_proposal_v2(raw, world_id="world:1")


@pytest.mark.parametrize("storage", ["__dict__", "__pydantic_extra__"])
def test_preflight_rejects_hostile_model_storage_before_copying(storage: str) -> None:
    import companion_daemon.world_v2.proposal_envelope_v2 as contracts

    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    hostile = FactCommitProposalEnvelopeV2.model_construct(**proposal.__dict__)
    huge = {
        f"hostile:{index}": None
        for index in range(contracts.MAX_FACT_COMMIT_PROPOSAL_NODES + 1)
    }
    object.__setattr__(hostile, storage, huge)
    with pytest.raises(ValueError, match="node budget"):
        validate_fact_commit_proposal_v2(hostile, world_id="world:1")


def test_preflight_rejects_collision_between_model_fields_and_extras() -> None:
    proposal = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    hostile = FactCommitProposalEnvelopeV2.model_construct(**proposal.__dict__)
    object.__setattr__(hostile, "__pydantic_extra__", {"proposal_id": "forged"})
    with pytest.raises(ValueError, match="collision"):
        validate_fact_commit_proposal_v2(hostile, world_id="world:1")


def test_v1_contract_is_unchanged_and_v1_v2_are_mutually_rejected() -> None:
    from companion_daemon.world_v2.proposal_envelope import (
        CanonicalTypedPayload,
        DecisionProposal,
        FactPayload,
        PAYLOAD_MODEL_REGISTRY,
        PROPOSAL_SCHEMA_REGISTRY_VERSION,
        validate_proposal_envelope,
    )

    assert PROPOSAL_SCHEMA_REGISTRY_VERSION == "world-v2-proposals.1"
    assert PAYLOAD_MODEL_REGISTRY["fact_transition"] is FactPayload
    assert "fact_commit_intent.v2" not in PAYLOAD_MODEL_REGISTRY
    assert set(FactPayload.model_fields) == {
        "before_image",
        "after_image",
        "subject",
        "predicate",
        "cardinality",
        "conflict_key",
        "value_hash",
        "assertion_binding",
        "anchor_evidence",
        "source_evidence",
        "privacy",
    }
    with pytest.raises(ValidationError):
        CanonicalTypedPayload(
            payload_schema="fact_commit_intent.v2",
            payload_version=2,
            canonical_json="{}",
        )
    v2 = normalize_fact_commit_proposal_v2(draft=_draft(), context=_context())
    with pytest.raises((ValidationError, ValueError)):
        validate_proposal_envelope(v2.model_dump(mode="python"))
    with pytest.raises((ValidationError, ValueError)):
        validate_fact_commit_proposal_v2(
            {**v2.model_dump(mode="python"), "schema_registry_version": "world-v2-proposals.1"},
            world_id="world:1",
        )
    legal_v1 = DecisionProposal(
        proposal_id="proposal:v1",
        proposal_kind="decision",
        trigger_ref="trigger:v1",
        evaluated_world_revision=12,
        schema_registry_version="world-v2-proposals.1",
        evidence_refs=(),
        proposed_changes=(),
        action_intents=(),
        confidence=8_000,
        brief_rationale="A valid inert v1 decision proposal.",
        appraisals=(),
        affect_tendencies=(),
        drives=("maintain",),
        conflicts=(),
        activity_transition=None,
        behavior_tendency="maintain",
        variation_profile=None,
        stance="defer",
        display_strategy="quiet",
        conversation_thread_changes=(),
    )
    assert validate_proposal_envelope(legal_v1) == legal_v1
    with pytest.raises((ValidationError, ValueError)):
        validate_fact_commit_proposal_v2(legal_v1, world_id="world:1")


def test_v2_module_has_no_production_or_side_effect_imports() -> None:
    import companion_daemon.world_v2.proposal_envelope_v2 as contracts

    tree = ast.parse(inspect.getsource(contracts))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        forbidden in module
        for module in imported
        for forbidden in (
            "acceptance",
            "ledger",
            "compiler",
            "event_catalog",
            "reducers",
        )
    )


def test_v2_contract_is_inert_and_defaults_match_frozen_budgets() -> None:
    assert MAX_FACT_COMMIT_PROPOSAL_PAYLOAD_BYTES == 65_536
    assert MAX_FACT_COMMIT_PROPOSAL_ENVELOPE_BYTES == 262_144
    for model in (FactCommitProposalEnvelopeV2,):
        assert not hasattr(model, "commit")
        assert not hasattr(model, "materialize")
        assert not hasattr(model, "to_world_event")
