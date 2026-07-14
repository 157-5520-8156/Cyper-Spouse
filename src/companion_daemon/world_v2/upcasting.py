"""Deterministic, side-effect-free event upcasting for ledger replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from typing import Any

from .errors import LedgerIntegrityError
from .event_catalog import event_contract
from .schemas import WorldEvent


UPCASTER_BUNDLE_VERSION = "world-v2-upcasters.1"
CURRENT_SCHEMA_VERSION = "world-v2.1"

EventDocument = dict[str, Any]
UpcastStep = Callable[[EventDocument], EventDocument]


def _v20_to_v21(document: EventDocument) -> EventDocument:
    upgraded = deepcopy(document)
    payload = upgraded["payload"]
    if upgraded["event_type"] == "ObservationRecorded":
        legacy_ref = payload.pop("observation_ref", None)
        if legacy_ref is not None:
            if "observation_id" in payload:
                raise ValueError(
                    "legacy ObservationRecorded contains conflicting observation identity"
                )
            payload["observation_id"] = legacy_ref
    upgraded["payload"] = payload
    upgraded["schema_version"] = CURRENT_SCHEMA_VERSION
    return upgraded


_UPCAST_STEPS: Mapping[tuple[str, str], tuple[str, UpcastStep]] = {
    ("*", "world-v2.0"): (CURRENT_SCHEMA_VERSION, _v20_to_v21),
}


def require_target_schema(version: str) -> None:
    """Select an installed canonical schema artifact or fail closed."""

    if version != CURRENT_SCHEMA_VERSION:
        raise ValueError(f"target schema {version!r} is not installed")


def _payload_from_verified_bytes(document: EventDocument) -> dict[str, Any]:
    payload_json = document.get("payload_json")
    payload_hash = document.get("payload_hash")
    if not isinstance(payload_json, str) or not isinstance(payload_hash, str):
        raise LedgerIntegrityError("event is missing immutable payload bytes or hash")
    actual_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if actual_hash != payload_hash:
        raise LedgerIntegrityError("event payload hash does not match immutable bytes")
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise LedgerIntegrityError("event payload bytes are not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LedgerIntegrityError("event payload must decode to an object")
    return payload


def _materialize(document: EventDocument) -> WorldEvent:
    payload = document.pop("payload")
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    document["payload_json"] = payload_json
    document["payload_hash"] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_encode_json_value,
    )
    return WorldEvent.model_validate_json(encoded)


def _encode_json_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__} in an event envelope")


def upcast_event(
    raw_event: Mapping[str, Any], *, target_schema_version: str = CURRENT_SCHEMA_VERSION
) -> WorldEvent:
    """Verify and deterministically upgrade one persisted event for replay.

    The input mapping is never mutated. Every hop is selected from the frozen
    registry, and no model, clock, random source, provider, or filesystem is
    reachable through this interface.
    """

    document = deepcopy(dict(raw_event))
    document["payload"] = _payload_from_verified_bytes(document)
    event_type = document.get("event_type")
    version = document.get("schema_version")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("event_type is required for upcasting")
    if not isinstance(version, str) or not version:
        raise ValueError("schema_version is required for upcasting")
    event_contract(event_type)

    while version != target_schema_version:
        step_definition = _UPCAST_STEPS.get((event_type, version))
        if step_definition is None:
            step_definition = _UPCAST_STEPS.get(("*", version))
        if step_definition is None:
            raise ValueError(
                f"no deterministic upcaster for {event_type!r} from {version!r} "
                f"to {target_schema_version!r}"
            )
        next_version, step = step_definition
        document = step(document)
        if document.get("schema_version") != next_version:
            raise LedgerIntegrityError("upcaster produced an unexpected schema version")
        version = next_version

    return _materialize(document)
