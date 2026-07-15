from __future__ import annotations

import hashlib

import pytest

from companion_daemon.world_v2.ledger_prefix_proof import (
    AppendMmrV1,
    IncrementalMmrV1,
    IncrementalSparseMerkleMapV1,
    LedgerLeafV1,
    PrefixCheckpointLeafV1,
    SparseMerkleMapV1,
    observation_locator_key,
    verify_checkpoint_in_prefix,
)


def _hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def test_mmr_proves_event_and_checkpoint_leaves_under_one_prefix() -> None:
    event_one = LedgerLeafV1(
        world_id="world:proof",
        ledger_sequence=1,
        world_revision=1,
        deliberation_revision=0,
        commit_id="commit:one",
        event_id="event:one",
        idempotency_key="idem:one",
        event_envelope_hash=_hash("event-one").hex(),
    ).digest()
    checkpoint = PrefixCheckpointLeafV1(
        world_id="world:proof",
        commit_id="commit:one",
        first_ledger_sequence=1,
        last_ledger_sequence=1,
        world_revision=1,
        deliberation_revision=0,
        request_hash=_hash("request").hex(),
        result_hash=_hash("result").hex(),
        ordered_event_ids_hash=_hash("event:one").hex(),
        locator_root=_hash("locator-root").hex(),
        mmr_leaf_count=2,
    ).digest()
    mmr = AppendMmrV1().append(event_one).append(checkpoint)

    mmr.prove(0).verify(leaf_hash=event_one, expected_root=mmr.root)
    verify_checkpoint_in_prefix(
        checkpoint=PrefixCheckpointLeafV1(
            world_id="world:proof", commit_id="commit:one", first_ledger_sequence=1,
            last_ledger_sequence=1, world_revision=1, deliberation_revision=0,
            request_hash=_hash("request").hex(), result_hash=_hash("result").hex(),
            ordered_event_ids_hash=_hash("event:one").hex(), locator_root=_hash("locator-root").hex(),
            mmr_leaf_count=2,
        ),
        proof=mmr.prove(1), expected_root=mmr.root, expected_world_id="world:proof",
        expected_commit_id="commit:one", expected_cursor=(1, 0, 1),
    )


def test_mmr_rejects_swapped_leaf_and_wrong_prefix_root() -> None:
    mmr = AppendMmrV1()
    for value in ("one", "two", "three"):
        mmr = mmr.append(_hash(value))

    with pytest.raises(ValueError, match="does not verify"):
        mmr.prove(1).verify(leaf_hash=_hash("one"), expected_root=mmr.root)
    with pytest.raises(ValueError, match="does not verify"):
        mmr.prove(1).verify(leaf_hash=_hash("two"), expected_root=_hash("other-root"))


def test_sparse_locator_map_proves_membership_and_non_membership() -> None:
    message = observation_locator_key(
        world_id="world:proof", event_type="ObservationRecorded", idempotency_key="idem:m"
    )
    operator = observation_locator_key(
        world_id="world:proof", event_type="OperatorObservationRecorded", idempotency_key="idem:o"
    )
    missing = observation_locator_key(
        world_id="world:proof", event_type="ObservationRecorded", idempotency_key="idem:missing"
    )
    locator_map = SparseMerkleMapV1().put(key=message, value_hash=_hash("event:m")).put(
        key=operator, value_hash=_hash("event:o")
    )

    membership = locator_map.prove(message)
    assert membership.value_hash == _hash("event:m")
    membership.verify_membership(
        expected_root=locator_map.root, expected_key=message, expected_value_hash=_hash("event:m")
    )
    non_membership = locator_map.prove(missing)
    assert non_membership.value_hash is None
    non_membership.verify_nonmembership(expected_root=locator_map.root, expected_key=missing)


def test_sparse_locator_map_rejects_tampered_branch_or_key_overwrite() -> None:
    key = observation_locator_key(
        world_id="world:proof", event_type="ObservationRecorded", idempotency_key="idem:m"
    )
    locator_map = SparseMerkleMapV1().put(key=key, value_hash=_hash("event:m"))
    proof = locator_map.prove(key)
    with pytest.raises(ValueError, match="does not verify"):
        type(proof)(key=proof.key, value_hash=_hash("event:other"), siblings=proof.siblings).verify_membership(
            expected_root=locator_map.root, expected_key=key, expected_value_hash=_hash("event:other")
        )
    with pytest.raises(ValueError, match="append-only"):
        locator_map.put(key=key, value_hash=_hash("event:other"))
    other_key = observation_locator_key(
        world_id="world:proof", event_type="ObservationRecorded", idempotency_key="idem:other"
    )
    with pytest.raises(ValueError, match="requested locator"):
        proof.verify_nonmembership(expected_root=locator_map.root, expected_key=other_key)


def test_locator_key_is_domain_separated_by_world_and_event_type() -> None:
    common = {"idempotency_key": "idem:same"}
    assert observation_locator_key(world_id="world:a", event_type="ObservationRecorded", **common) != observation_locator_key(
        world_id="world:b", event_type="ObservationRecorded", **common
    )
    assert observation_locator_key(world_id="world:a", event_type="ObservationRecorded", **common) != observation_locator_key(
        world_id="world:a", event_type="OperatorObservationRecorded", **common
    )
    with pytest.raises(ValueError, match="observation event"):
        observation_locator_key(world_id="world:a", event_type="FactCommitted", **common)
    with pytest.raises(ValueError, match="observation event"):
        observation_locator_key(world_id="world:a", event_type=[] , **common)  # type: ignore[arg-type]


def test_proof_methods_reject_hostile_expected_values_before_comparison() -> None:
    key = observation_locator_key(
        world_id="world:proof", event_type="ObservationRecorded", idempotency_key="idem:m"
    )
    locator_map = SparseMerkleMapV1().put(key=key, value_hash=_hash("event:m"))
    proof = locator_map.prove(key)
    with pytest.raises(ValueError, match="expected SMT key"):
        proof.verify_membership(
            expected_root=locator_map.root, expected_key=b"too-short", expected_value_hash=_hash("event:m")
        )
    with pytest.raises(ValueError, match="SMT proof key"):
        type(proof)(key="not-bytes", value_hash=proof.value_hash, siblings=proof.siblings).verify_membership(  # type: ignore[arg-type]
            expected_root=locator_map.root, expected_key=key, expected_value_hash=_hash("event:m")
        )


def test_leaf_hash_contract_rejects_noncanonical_uppercase_hex() -> None:
    leaf = LedgerLeafV1(
        world_id="world:proof", ledger_sequence=1, world_revision=1, deliberation_revision=0,
        commit_id="commit:one", event_id="event:one", idempotency_key="idem:one",
        event_envelope_hash=("A" * 64),
    )
    with pytest.raises(ValueError, match="sha256 hex"):
        leaf.digest()


def test_incremental_builders_match_reference_proofs() -> None:
    leaves = tuple(_hash(f"leaf:{index}") for index in range(9))
    reference = AppendMmrV1(leaves)
    incremental = IncrementalMmrV1()
    for leaf in leaves:
        incremental.append(leaf)
    assert incremental.root == reference.root
    for index, leaf in enumerate(leaves):
        incremental.prove(index).verify(leaf_hash=leaf, expected_root=incremental.root)

    reference_map = SparseMerkleMapV1()
    incremental_map = IncrementalSparseMerkleMapV1()
    keys = tuple(_hash(f"key:{index}") for index in range(4))
    for index, key in enumerate(keys):
        value = _hash(f"value:{index}")
        reference_map = reference_map.put(key=key, value_hash=value)
        incremental_map.put(key=key, value_hash=value)
    assert incremental_map.root == reference_map.root
    for index, key in enumerate(keys):
        incremental_map.prove(key).verify_membership(
            expected_root=incremental_map.root, expected_key=key, expected_value_hash=_hash(f"value:{index}")
        )
