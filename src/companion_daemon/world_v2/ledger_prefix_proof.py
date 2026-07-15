"""Pure, domain-separated proof primitives for verified World v2 ledger prefixes.

This module deliberately knows no SQLite, reducer, Pydantic model or authority
handle.  Adapters persist/rebuild its derived state; higher layers decide when a
root is trusted.  A checkpoint is an MMR leaf, so an anchored later prefix can
authenticate a historical commit's locator-map root without replaying a chain.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from typing import Final


_HASH_BYTES: Final = 32
_SMT_DEPTH: Final = 256
# This immutable reference core retains every leaf/node to generate test vectors.
# Production adapters persist incremental MMR/SMT nodes and must not materialize it.
_MAX_I63: Final = (1 << 63) - 1
_MAX_REFERENCE_MMR_LEAVES: Final = 4_096
_MAX_REFERENCE_SMT_ENTRIES: Final = 256
_EMPTY_LEAF: Final = hashlib.sha256(b"world-v2-smt-empty-leaf.1").digest()


def _hash(domain: str, payload: bytes) -> bytes:
    encoded_domain = domain.encode("utf-8")
    return hashlib.sha256(
        len(encoded_domain).to_bytes(4, "big") + encoded_domain + payload
    ).digest()


def _field(name: str, value: str | int | bytes) -> bytes:
    encoded_name = name.encode("utf-8")
    if type(value) is str:
        tag, encoded_value = b"s", value.encode("utf-8")
    elif type(value) is int and value >= 0:
        tag = b"i"
        encoded_value = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    elif type(value) is bytes:
        tag, encoded_value = b"b", value
    else:
        raise TypeError(f"unsupported canonical field {name!r}")
    return (
        len(encoded_name).to_bytes(2, "big")
        + encoded_name
        + tag
        + len(encoded_value).to_bytes(4, "big")
        + encoded_value
    )


def _record(contract: str, fields: tuple[tuple[str, str | int | bytes], ...]) -> bytes:
    names = tuple(name for name, _ in fields)
    if tuple(sorted(names)) != names or len(set(names)) != len(names):
        raise ValueError("canonical record fields must be unique and sorted")
    return _field("contract", contract) + b"".join(_field(name, value) for name, value in fields)


def _hex_hash(value: str, *, label: str) -> bytes:
    if type(value) is not str or len(value) != _HASH_BYTES * 2 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be a sha256 hex string")
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a sha256 hex string") from exc
    if len(decoded) != _HASH_BYTES:
        raise ValueError(f"{label} must be a sha256 hex string")
    return decoded


def _require_hash_bytes(value: object, *, label: str) -> bytes:
    if type(value) is not bytes or len(value) != _HASH_BYTES:
        raise ValueError(f"{label} must be 32-byte hash bytes")
    return value


def _require_text(value: str, *, label: str) -> str:
    if type(value) is not str or not value or len(value) > 1024:
        raise ValueError(f"{label} must be a non-empty bounded string")
    return value


@dataclass(frozen=True, slots=True)
class LedgerLeafV1:
    world_id: str
    ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    commit_id: str
    event_id: str
    idempotency_key: str
    event_envelope_hash: str

    def digest(self) -> bytes:
        return _hash(
            "world-v2-ledger-leaf.1",
            _record(
                "world-v2-ledger-leaf.1",
                (
                    ("commit_id", _require_text(self.commit_id, label="commit_id")),
                    ("deliberation_revision", _require_nonnegative(self.deliberation_revision, label="deliberation_revision")),
                    ("event_envelope_hash", _hex_hash(self.event_envelope_hash, label="event_envelope_hash")),
                    ("event_id", _require_text(self.event_id, label="event_id")),
                    ("idempotency_key", _require_text(self.idempotency_key, label="idempotency_key")),
                    ("ledger_sequence", _require_positive(self.ledger_sequence, label="ledger_sequence")),
                    ("world_id", _require_text(self.world_id, label="world_id")),
                    ("world_revision", _require_nonnegative(self.world_revision, label="world_revision")),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class PrefixCheckpointLeafV1:
    world_id: str
    commit_id: str
    first_ledger_sequence: int
    last_ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    request_hash: str
    result_hash: str
    ordered_event_ids_hash: str
    locator_root: str
    mmr_leaf_count: int

    def digest(self) -> bytes:
        if self.last_ledger_sequence < self.first_ledger_sequence:
            raise ValueError("checkpoint sequence range is invalid")
        return _hash(
            "world-v2-prefix-checkpoint-leaf.1",
            _record(
                "world-v2-prefix-checkpoint-leaf.1",
                (
                    ("commit_id", _require_text(self.commit_id, label="commit_id")),
                    ("deliberation_revision", _require_nonnegative(self.deliberation_revision, label="deliberation_revision")),
                    ("first_ledger_sequence", _require_positive(self.first_ledger_sequence, label="first_ledger_sequence")),
                    ("last_ledger_sequence", _require_positive(self.last_ledger_sequence, label="last_ledger_sequence")),
                    ("locator_root", _hex_hash(self.locator_root, label="locator_root")),
                    ("mmr_leaf_count", _require_positive(self.mmr_leaf_count, label="mmr_leaf_count")),
                    ("ordered_event_ids_hash", _hex_hash(self.ordered_event_ids_hash, label="ordered_event_ids_hash")),
                    ("request_hash", _hex_hash(self.request_hash, label="request_hash")),
                    ("result_hash", _hex_hash(self.result_hash, label="result_hash")),
                    ("world_id", _require_text(self.world_id, label="world_id")),
                    ("world_revision", _require_nonnegative(self.world_revision, label="world_revision")),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class ObservationLocatorValueV1:
    """The value authenticated by an observation locator-map membership proof."""

    observation_id: str
    event_type: str
    event_id: str
    ledger_sequence: int
    world_revision: int
    deliberation_revision: int
    event_leaf_index: int
    event_leaf_hash: bytes

    def digest(self) -> bytes:
        if type(self.event_type) is not str or self.event_type not in {"ObservationRecorded", "OperatorObservationRecorded"}:
            raise ValueError("locator value event_type is not an observation event")
        return _hash(
            "world-v2-observation-locator-value.1",
            _record(
                "world-v2-observation-locator-value.1",
                (
                    ("deliberation_revision", _require_nonnegative(self.deliberation_revision, label="deliberation_revision")),
                    ("event_id", _require_text(self.event_id, label="event_id")),
                    ("event_leaf_hash", _require_hash_bytes(self.event_leaf_hash, label="event_leaf_hash")),
                    ("event_leaf_index", _require_nonnegative(self.event_leaf_index, label="event_leaf_index")),
                    ("event_type", self.event_type),
                    ("ledger_sequence", _require_positive(self.ledger_sequence, label="ledger_sequence")),
                    ("observation_id", _require_text(self.observation_id, label="observation_id")),
                    ("world_revision", _require_nonnegative(self.world_revision, label="world_revision")),
                ),
            ),
        )


def ordered_event_ids_hash_v1(event_ids: tuple[str, ...]) -> str:
    if not event_ids or len(event_ids) > 4_096:
        raise ValueError("checkpoint event ids must contain between 1 and 4096 entries")
    return _hash(
        "world-v2-ordered-event-ids.1",
        b"".join(
            _field("event_id", _require_text(event_id, label="event_id")) for event_id in event_ids
        ),
    ).hex()


def commit_result_hash_v1(
    *, world_revision: int, deliberation_revision: int, ledger_sequence: int, event_ids: tuple[str, ...]
) -> str:
    return _hash(
        "world-v2-commit-result.1",
        _record(
            "world-v2-commit-result.1",
            (
                ("deliberation_revision", _require_nonnegative(deliberation_revision, label="deliberation_revision")),
                ("event_ids_hash", bytes.fromhex(ordered_event_ids_hash_v1(event_ids))),
                ("ledger_sequence", _require_nonnegative(ledger_sequence, label="ledger_sequence")),
                ("world_revision", _require_nonnegative(world_revision, label="world_revision")),
            ),
        ),
    ).hex()


def _require_nonnegative(value: int, *, label: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_I63:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _require_positive(value: int, *, label: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_I63:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _mmr_parent(left: bytes, right: bytes) -> bytes:
    return _hash("world-v2-mmr-parent.1", left + right)


def _mmr_root(peaks: tuple[bytes, ...], leaf_count: int) -> bytes:
    if any(type(peak) is not bytes or len(peak) != _HASH_BYTES for peak in peaks):
        raise ValueError("MMR peaks must be hash bytes")
    return _hash(
        "world-v2-mmr-root.1",
        leaf_count.to_bytes(8, "big") + len(peaks).to_bytes(2, "big") + b"".join(peaks),
    )


def _peak_sizes(leaf_count: int) -> tuple[int, ...]:
    _require_nonnegative(leaf_count, label="leaf_count")
    sizes: list[int] = []
    bit = 1 << (leaf_count.bit_length() - 1) if leaf_count else 0
    remaining = leaf_count
    while bit:
        if remaining >= bit:
            sizes.append(bit)
            remaining -= bit
        bit >>= 1
    return tuple(sizes)


@dataclass(frozen=True, slots=True)
class MmrInclusionProofV1:
    leaf_index: int
    leaf_count: int
    peak_index: int
    siblings: tuple[bytes, ...]
    peaks: tuple[bytes, ...]

    def verify(self, *, leaf_hash: bytes, expected_root: bytes) -> None:
        _require_hash_bytes(leaf_hash, label="leaf hash")
        _require_hash_bytes(expected_root, label="MMR root")
        if not 0 <= self.leaf_index < self.leaf_count:
            raise ValueError("MMR proof leaf index is outside its prefix")
        sizes = _peak_sizes(self.leaf_count)
        if not 0 <= self.peak_index < len(sizes) or len(self.peaks) != len(sizes):
            raise ValueError("MMR proof peak layout is invalid")
        offset = sum(sizes[: self.peak_index])
        relative = self.leaf_index - offset
        size = sizes[self.peak_index]
        if not 0 <= relative < size or len(self.siblings) != size.bit_length() - 1:
            raise ValueError("MMR proof path is invalid")
        current = leaf_hash
        for level, sibling in enumerate(self.siblings):
            if type(sibling) is not bytes or len(sibling) != _HASH_BYTES:
                raise ValueError("MMR proof sibling is invalid")
            current = _mmr_parent(sibling, current) if (relative >> level) & 1 else _mmr_parent(current, sibling)
        if not hmac.compare_digest(current, self.peaks[self.peak_index]) or not hmac.compare_digest(_mmr_root(self.peaks, self.leaf_count), expected_root):
            raise ValueError("MMR inclusion proof does not verify")


@dataclass(frozen=True, slots=True)
class AppendMmrV1:
    """Immutable append MMR. Leaves are retained only for pure-core proof generation."""

    leaves: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if len(self.leaves) > _MAX_REFERENCE_MMR_LEAVES or any(type(leaf) is not bytes or len(leaf) != _HASH_BYTES for leaf in self.leaves):
            raise ValueError("MMR leaves must be hash bytes")

    @property
    def leaf_count(self) -> int:
        return len(self.leaves)

    @property
    def peaks(self) -> tuple[bytes, ...]:
        sizes = _peak_sizes(self.leaf_count)
        offset = 0
        roots: list[bytes] = []
        for size in sizes:
            roots.append(_complete_root(self.leaves[offset : offset + size]))
            offset += size
        return tuple(roots)

    @property
    def root(self) -> bytes:
        return _mmr_root(self.peaks, self.leaf_count)

    def append(self, leaf_hash: bytes) -> AppendMmrV1:
        if type(leaf_hash) is not bytes or len(leaf_hash) != _HASH_BYTES:
            raise ValueError("MMR leaf must be hash bytes")
        return AppendMmrV1(self.leaves + (leaf_hash,))

    def prove(self, leaf_index: int) -> MmrInclusionProofV1:
        if type(leaf_index) is not int or not 0 <= leaf_index < self.leaf_count:
            raise ValueError("MMR leaf index is outside its prefix")
        offset = 0
        for peak_index, size in enumerate(_peak_sizes(self.leaf_count)):
            if leaf_index < offset + size:
                relative = leaf_index - offset
                return MmrInclusionProofV1(
                    leaf_index=leaf_index,
                    leaf_count=self.leaf_count,
                    peak_index=peak_index,
                    siblings=_complete_siblings(self.leaves[offset : offset + size], relative),
                    peaks=self.peaks,
                )
            offset += size
        raise AssertionError("unreachable MMR peak lookup")


class IncrementalMmrV1:
    """Mutable O(log N) MMR builder; adapters persist ``nodes`` transactionally."""

    __slots__ = ("leaf_count", "nodes", "peaks")

    def __init__(self) -> None:
        self.leaf_count = 0
        self.nodes: dict[tuple[int, int], bytes] = {}
        self.peaks: dict[int, bytes] = {}

    @classmethod
    def restore(cls, *, leaf_count: int, nodes: dict[tuple[int, int], bytes]) -> IncrementalMmrV1:
        _require_nonnegative(leaf_count, label="MMR leaf_count")
        instance = cls()
        instance.leaf_count = leaf_count
        instance.nodes = dict(nodes)
        offset = 0
        for size in _peak_sizes(leaf_count):
            height = size.bit_length() - 1
            node = instance.nodes.get((height, offset >> height))
            if node is None:
                raise ValueError("persisted MMR peak is missing")
            instance.peaks[height] = _require_hash_bytes(node, label="persisted MMR node")
            offset += size
        return instance

    @property
    def root(self) -> bytes:
        return _mmr_root(tuple(self.peaks[height] for height in sorted(self.peaks, reverse=True)), self.leaf_count)

    def append(self, leaf_hash: bytes) -> int:
        _require_hash_bytes(leaf_hash, label="MMR leaf")
        if self.leaf_count >= _MAX_I63:
            raise ValueError("MMR leaf count exceeds the storage contract")
        leaf_index = self.leaf_count
        self.nodes[(0, leaf_index)] = leaf_hash
        carry = leaf_hash
        height = 0
        while height in self.peaks:
            left = self.peaks.pop(height)
            carry = _mmr_parent(left, carry)
            self.nodes[(height + 1, leaf_index >> (height + 1))] = carry
            height += 1
        self.peaks[height] = carry
        self.leaf_count += 1
        return leaf_index

    def prove(self, leaf_index: int) -> MmrInclusionProofV1:
        if type(leaf_index) is not int or not 0 <= leaf_index < self.leaf_count:
            raise ValueError("MMR leaf index is outside its prefix")
        offset = 0
        sizes = _peak_sizes(self.leaf_count)
        peaks = tuple(self.peaks[height] for height in sorted(self.peaks, reverse=True))
        for peak_index, size in enumerate(sizes):
            if leaf_index < offset + size:
                height = size.bit_length() - 1
                siblings = tuple(self.nodes[(level, (leaf_index >> level) ^ 1)] for level in range(height))
                return MmrInclusionProofV1(leaf_index, self.leaf_count, peak_index, siblings, peaks)
            offset += size
        raise AssertionError("unreachable MMR peak lookup")


def verify_checkpoint_in_prefix(
    *,
    checkpoint: PrefixCheckpointLeafV1,
    proof: MmrInclusionProofV1,
    expected_root: bytes,
    expected_world_id: str,
    expected_commit_id: str,
    expected_cursor: tuple[int, int, int],
) -> None:
    """Bind a checkpoint inclusion proof to one requested historical cursor."""

    if (
        checkpoint.world_id != expected_world_id
        or checkpoint.commit_id != expected_commit_id
        or (checkpoint.world_revision, checkpoint.deliberation_revision, checkpoint.last_ledger_sequence)
        != expected_cursor
    ):
        raise ValueError("checkpoint proof does not match the requested cursor")
    if checkpoint.mmr_leaf_count != proof.leaf_index + 1 or proof.leaf_count < checkpoint.mmr_leaf_count:
        raise ValueError("checkpoint proof position is not its committed prefix tail")
    proof.verify(leaf_hash=checkpoint.digest(), expected_root=expected_root)


def _complete_root(leaves: tuple[bytes, ...]) -> bytes:
    if len(leaves) < 1 or len(leaves) & (len(leaves) - 1):
        raise ValueError("MMR peak must contain a power of two leaves")
    level = leaves
    while len(level) > 1:
        level = tuple(_mmr_parent(level[index], level[index + 1]) for index in range(0, len(level), 2))
    return level[0]


def _complete_siblings(leaves: tuple[bytes, ...], index: int) -> tuple[bytes, ...]:
    siblings: list[bytes] = []
    level = leaves
    relative = index
    while len(level) > 1:
        siblings.append(level[relative ^ 1])
        level = tuple(_mmr_parent(level[position], level[position + 1]) for position in range(0, len(level), 2))
        relative //= 2
    return tuple(siblings)


def observation_locator_key(*, world_id: str, event_type: str, idempotency_key: str) -> bytes:
    if type(event_type) is not str or event_type not in {"ObservationRecorded", "OperatorObservationRecorded"}:
        raise ValueError("locator event_type is not an observation event")
    return _hash(
        "world-v2-observation-locator-key.1",
        _record(
            "world-v2-observation-locator-key.1",
            (
                ("event_type", _require_text(event_type, label="event_type")),
                ("idempotency_key", _require_text(idempotency_key, label="idempotency_key")),
                ("world_id", _require_text(world_id, label="world_id")),
            ),
        ),
    )


def _smt_empty_hashes() -> tuple[bytes, ...]:
    hashes = [_EMPTY_LEAF]
    for _depth in range(_SMT_DEPTH):
        hashes.append(_hash("world-v2-smt-parent.1", hashes[-1] + hashes[-1]))
    return tuple(hashes)


_SMT_EMPTY: Final = _smt_empty_hashes()


def _smt_leaf(key: bytes, value_hash: bytes) -> bytes:
    return _hash("world-v2-smt-leaf.1", key + value_hash)


def _smt_parent(left: bytes, right: bytes) -> bytes:
    return _hash("world-v2-smt-parent.1", left + right)


@dataclass(frozen=True, slots=True)
class SparseMerkleProofV1:
    key: bytes
    value_hash: bytes | None
    siblings: tuple[bytes, ...]

    def _verify(self, *, expected_root: bytes) -> None:
        if type(self.key) is not bytes or len(self.key) != _HASH_BYTES:
            raise ValueError("SMT proof key is invalid")
        if self.value_hash is not None and (type(self.value_hash) is not bytes or len(self.value_hash) != _HASH_BYTES):
            raise ValueError("SMT proof value is invalid")
        if len(self.siblings) != _SMT_DEPTH or any(type(item) is not bytes or len(item) != _HASH_BYTES for item in self.siblings):
            raise ValueError("SMT proof siblings are invalid")
        current = _smt_leaf(self.key, self.value_hash) if self.value_hash is not None else _SMT_EMPTY[0]
        key_int = int.from_bytes(self.key, "big")
        for depth in range(_SMT_DEPTH - 1, -1, -1):
            sibling = self.siblings[depth]
            current = _smt_parent(sibling, current) if (key_int >> (_SMT_DEPTH - 1 - depth)) & 1 else _smt_parent(current, sibling)
        if type(expected_root) is not bytes or len(expected_root) != _HASH_BYTES or not hmac.compare_digest(current, expected_root):
            raise ValueError("SMT proof does not verify")

    def verify_membership(
        self, *, expected_root: bytes, expected_key: bytes, expected_value_hash: bytes
    ) -> None:
        proof_key = _require_hash_bytes(self.key, label="SMT proof key")
        proof_value = _require_hash_bytes(self.value_hash, label="SMT proof value")
        expected_key = _require_hash_bytes(expected_key, label="expected SMT key")
        expected_value_hash = _require_hash_bytes(expected_value_hash, label="expected SMT value")
        _require_hash_bytes(expected_root, label="SMT root")
        if not hmac.compare_digest(proof_key, expected_key) or not hmac.compare_digest(proof_value, expected_value_hash):
            raise ValueError("SMT membership proof does not match the requested locator")
        self._verify(expected_root=expected_root)

    def verify_nonmembership(self, *, expected_root: bytes, expected_key: bytes) -> None:
        proof_key = _require_hash_bytes(self.key, label="SMT proof key")
        expected_key = _require_hash_bytes(expected_key, label="expected SMT key")
        _require_hash_bytes(expected_root, label="SMT root")
        if not hmac.compare_digest(proof_key, expected_key) or self.value_hash is not None:
            raise ValueError("SMT non-membership proof does not match the requested locator")
        self._verify(expected_root=expected_root)


@dataclass(frozen=True, slots=True)
class SparseMerkleMapV1:
    """Immutable fixed-depth map; storage adapters replace recomputation with persisted nodes."""

    entries: tuple[tuple[bytes, bytes], ...] = ()

    def __post_init__(self) -> None:
        if len(self.entries) > _MAX_REFERENCE_SMT_ENTRIES:
            raise ValueError("SMT entries exceed the proof contract")
        prior: bytes | None = None
        for key, value in self.entries:
            if type(key) is not bytes or len(key) != _HASH_BYTES or type(value) is not bytes or len(value) != _HASH_BYTES:
                raise ValueError("SMT entries must be 32-byte key/value hashes")
            if prior is not None and key <= prior:
                raise ValueError("SMT entries must be sorted and unique")
            prior = key

    @property
    def root(self) -> bytes:
        return self._nodes().get((0, 0), _SMT_EMPTY[_SMT_DEPTH])

    def put(self, *, key: bytes, value_hash: bytes) -> SparseMerkleMapV1:
        if type(key) is not bytes or len(key) != _HASH_BYTES or type(value_hash) is not bytes or len(value_hash) != _HASH_BYTES:
            raise ValueError("SMT key/value must be 32-byte hashes")
        if any(existing == key for existing, _ in self.entries):
            raise ValueError("SMT locator keys are append-only")
        return SparseMerkleMapV1(tuple(sorted(self.entries + ((key, value_hash),))))

    def prove(self, key: bytes) -> SparseMerkleProofV1:
        if type(key) is not bytes or len(key) != _HASH_BYTES:
            raise ValueError("SMT key must be a 32-byte hash")
        nodes = self._nodes()
        key_int = int.from_bytes(key, "big")
        siblings: list[bytes] = []
        for depth in range(_SMT_DEPTH):
            prefix = key_int >> (_SMT_DEPTH - depth)
            sibling_prefix = (prefix << 1) | (1 - ((key_int >> (_SMT_DEPTH - 1 - depth)) & 1))
            siblings.append(nodes.get((depth + 1, sibling_prefix), _SMT_EMPTY[_SMT_DEPTH - depth - 1]))
        value = next((candidate for candidate_key, candidate in self.entries if candidate_key == key), None)
        return SparseMerkleProofV1(key=key, value_hash=value, siblings=tuple(siblings))

    def _nodes(self) -> dict[tuple[int, int], bytes]:
        nodes = {( _SMT_DEPTH, int.from_bytes(key, "big")): _smt_leaf(key, value) for key, value in self.entries}
        for depth in range(_SMT_DEPTH - 1, -1, -1):
            parents = {prefix >> 1 for node_depth, prefix in nodes if node_depth == depth + 1}
            for prefix in parents:
                left = nodes.get((depth + 1, prefix << 1), _SMT_EMPTY[_SMT_DEPTH - depth - 1])
                right = nodes.get((depth + 1, (prefix << 1) | 1), _SMT_EMPTY[_SMT_DEPTH - depth - 1])
                nodes[(depth, prefix)] = _smt_parent(left, right)
        return nodes


class IncrementalSparseMerkleMapV1:
    """Mutable O(256) sparse map builder; ``nodes`` is adapter-persistable state."""

    __slots__ = ("nodes", "values")

    def __init__(self) -> None:
        self.nodes: dict[tuple[int, int], bytes] = {}
        self.values: dict[bytes, bytes] = {}

    @classmethod
    def restore(
        cls, *, nodes: dict[tuple[int, int], bytes], values: dict[bytes, bytes]
    ) -> IncrementalSparseMerkleMapV1:
        instance = cls()
        for (depth, prefix), node in nodes.items():
            if type(depth) is not int or not 0 <= depth <= _SMT_DEPTH or type(prefix) is not int or prefix < 0:
                raise ValueError("persisted SMT node address is invalid")
            instance.nodes[(depth, prefix)] = _require_hash_bytes(node, label="persisted SMT node")
        for key, value in values.items():
            instance.values[_require_hash_bytes(key, label="persisted SMT key")] = _require_hash_bytes(value, label="persisted SMT value")
        return instance

    @property
    def root(self) -> bytes:
        return self.nodes.get((0, 0), _SMT_EMPTY[_SMT_DEPTH])

    def put(self, *, key: bytes, value_hash: bytes) -> None:
        _require_hash_bytes(key, label="SMT key")
        _require_hash_bytes(value_hash, label="SMT value")
        if key in self.values:
            raise ValueError("SMT locator keys are append-only")
        self.values[key] = value_hash
        key_int = int.from_bytes(key, "big")
        self.nodes[(_SMT_DEPTH, key_int)] = _smt_leaf(key, value_hash)
        for depth in range(_SMT_DEPTH - 1, -1, -1):
            prefix = key_int >> (_SMT_DEPTH - depth)
            left = self.nodes.get((depth + 1, prefix << 1), _SMT_EMPTY[_SMT_DEPTH - depth - 1])
            right = self.nodes.get((depth + 1, (prefix << 1) | 1), _SMT_EMPTY[_SMT_DEPTH - depth - 1])
            self.nodes[(depth, prefix)] = _smt_parent(left, right)

    def prove(self, key: bytes) -> SparseMerkleProofV1:
        _require_hash_bytes(key, label="SMT key")
        key_int = int.from_bytes(key, "big")
        siblings = tuple(
            self.nodes.get(
                (depth + 1, (key_int >> (_SMT_DEPTH - depth) << 1) | (1 - ((key_int >> (_SMT_DEPTH - 1 - depth)) & 1))),
                _SMT_EMPTY[_SMT_DEPTH - depth - 1],
            )
            for depth in range(_SMT_DEPTH)
        )
        return SparseMerkleProofV1(key=key, value_hash=self.values.get(key), siblings=siblings)


def sparse_merkle_proof_from_nodes_v1(
    *, key: bytes, value_hash: bytes | None, nodes: dict[tuple[int, int], bytes]
) -> SparseMerkleProofV1:
    """Build one sparse-map proof from a persisted historical node path.

    Adapters use this narrow helper when a checkpoint authenticates an older
    locator root.  ``nodes`` may contain only the requested sibling path; a
    missing address has the canonical empty-subtree value.  This deliberately
    does not reconstruct a mutable map or grant callers access to its values.
    """

    _require_hash_bytes(key, label="SMT key")
    if value_hash is not None:
        _require_hash_bytes(value_hash, label="SMT value")
    key_int = int.from_bytes(key, "big")
    siblings: list[bytes] = []
    for depth in range(_SMT_DEPTH):
        sibling_address = (
            depth + 1,
            (key_int >> (_SMT_DEPTH - depth) << 1)
            | (1 - ((key_int >> (_SMT_DEPTH - 1 - depth)) & 1)),
        )
        sibling = nodes.get(sibling_address, _SMT_EMPTY[_SMT_DEPTH - depth - 1])
        siblings.append(_require_hash_bytes(sibling, label="persisted SMT sibling"))
    return SparseMerkleProofV1(key=key, value_hash=value_hash, siblings=tuple(siblings))
