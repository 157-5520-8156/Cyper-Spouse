"""Typed proposal mutation contract for `.16.0` LocationAuthority.

DORMANT — no producer: no production ledger holds a committed ``V2Location*``
event and no runtime constructs these payloads.  Before wiring a producer,
read the Producer-First Authority rule in CONTEXT.md and record the
activation verdict in ``configs/mechanism_closure.yaml``
(``v16-situation-constituents``).
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any, Literal

from pydantic import TypeAdapter, model_validator
from pydantic_core import to_jsonable_python

from .goal_situation_schemas import (
    DomainOperatorAuthorityBinding,
    RandomDrawBinding,
    V16AuthorizedMutationEnvelope,
)
from .location_authority_schemas import (
    LocationCompensationCauseAuthority,
    LocationCauseAuthority,
    V2LocationProjection,
)
from .location_authority_contract import require_location_event_operation
from .schemas import EvidenceRef


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    return value


def v2_location_mutation_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        material = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        material = to_jsonable_python(dict(value))  # type: ignore[arg-type]
    material.pop("accepted_change_hash", None)
    return hashlib.sha256(
        json.dumps(
            _canonicalize(material),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _authority_evidence(
    binding: DomainOperatorAuthorityBinding,
) -> EvidenceRef:
    return EvidenceRef(
        ref_id=binding.authority_event_ref,
        evidence_type="committed_world_event",
        claim_purpose="action_authorization",
        source_world_revision=binding.authority_world_revision,
        immutable_hash=binding.authority_payload_hash,
    )


def v2_location_evidence_refs(value: object) -> tuple[EvidenceRef, ...]:
    raw = (
        value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)  # type: ignore[arg-type]
    )
    cause = raw["cause_authority"]
    if not isinstance(cause, (DomainOperatorAuthorityBinding, LocationCompensationCauseAuthority)):
        cause = TypeAdapter(LocationCauseAuthority).validate_python(cause)
    refs: list[EvidenceRef] = []
    if isinstance(cause, DomainOperatorAuthorityBinding):
        refs.append(_authority_evidence(cause))
    else:
        refs.extend(
            (
                EvidenceRef(
                    ref_id=cause.target_accepted_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="conversation_continuity",
                    source_world_revision=cause.target_accepted_world_revision,
                    immutable_hash=cause.target_accepted_payload_hash,
                ),
                _authority_evidence(cause.operator_authority),
            )
        )
    return tuple(sorted(set(refs), key=lambda item: item.ref_id))


class V2LocationChangedPayload(V16AuthorizedMutationEnvelope):
    operation: Literal["establish", "change", "compensate"]
    authority_lane: Literal["operator", "settlement", "deliberative", "compensation"]
    selection_mode: Literal["direct", "random_draw"]
    random_draw_binding: RandomDrawBinding | None = None
    location_before: V2LocationProjection | None = None
    location_after: V2LocationProjection
    cause_authority: LocationCauseAuthority
    policy_version: str
    policy_digest: str

    @model_validator(mode="after")
    def authority_and_hash_are_exact(self) -> V2LocationChangedPayload:
        require_location_event_operation(
            event_type=(
                "V2LocationChangeCompensated"
                if self.operation == "compensate"
                else "V2LocationChanged"
            ),
            operation=self.operation,
        )
        expected_lane = "compensation" if self.operation == "compensate" else "operator"
        expected_cause = (
            LocationCompensationCauseAuthority
            if self.operation == "compensate"
            else DomainOperatorAuthorityBinding
        )
        if self.authority_lane != expected_lane or not isinstance(
            self.cause_authority, expected_cause
        ):
            raise ValueError("location operation is not allowed in authority lane")
        if self.selection_mode == "random_draw":
            if self.random_draw_binding is None:
                raise ValueError("random location selection requires an exact draw binding")
        elif self.random_draw_binding is not None:
            raise ValueError("direct location selection cannot claim a random draw")
        if self.evidence_refs != v2_location_evidence_refs(self):
            raise ValueError("location EvidenceRefs are not exact cause authority")
        if self.accepted_change_hash != v2_location_mutation_hash(self):
            raise ValueError("accepted location change hash is invalid")
        return self


V2_LOCATION_PAYLOAD_MODELS = {
    "V2LocationChanged": V2LocationChangedPayload,
    "V2LocationChangeCompensated": V2LocationChangedPayload,
}
