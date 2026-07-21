"""Typed mutation payload and codec for `.16.0` ResourceAuthority.

DORMANT — no producer: no production ledger holds a committed ``V2Resource*``
event and no runtime constructs these payloads.  Before wiring a producer,
read the Producer-First Authority rule in CONTEXT.md and record the
activation verdict in ``configs/mechanism_closure.yaml``
(``v16-situation-constituents``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, TypeAdapter, model_validator
from pydantic_core import to_jsonable_python

from .goal_situation_schemas import (
    ClockCauseAuthority,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    RandomDrawBinding,
    SettledEventCauseAuthority,
    V16AuthorizedMutationEnvelope,
)
from .resource_authority_contract import (
    V2_RESOURCE_EVENT_TYPES,
    V2ResourceOperation,
    require_resource_event_operation,
)
from .resource_authority_schemas import (
    ResourceCauseAuthority,
    ResourceCompensationCauseAuthority,
    V2ResourceProposalProjection,
    V2ResourceProjection,
    v2_resource_semantic_fingerprint,
)
from .schema_core import EvidenceRef, canonicalize_json_value
from .schema_core import FrozenModel
from .typed_proposals import AcceptedMutationBinding, ProposalAuthorityBinding


def v2_resource_mutation_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        material = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        material = to_jsonable_python(dict(value))  # type: ignore[arg-type]
    material.pop("accepted_change_hash", None)
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(material),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _event_evidence(
    *, ref: str, revision: int, payload_hash: str, purpose: str
) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref,
        evidence_type="committed_world_event",
        claim_purpose=purpose,
        source_world_revision=revision,
        immutable_hash=payload_hash,
    )


def _operator_evidence(binding: DomainOperatorAuthorityBinding) -> EvidenceRef:
    return _event_evidence(
        ref=binding.authority_event_ref,
        revision=binding.authority_world_revision,
        payload_hash=binding.authority_payload_hash,
        purpose="action_authorization",
    )


def v2_resource_evidence_refs(value: object) -> tuple[EvidenceRef, ...]:
    raw = (
        value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)  # type: ignore[arg-type]
    )
    cause = raw["cause_authority"]
    if not isinstance(
        cause,
        (
            DomainOperatorAuthorityBinding,
            DeliberativeCauseAuthority,
            SettledEventCauseAuthority,
            ResourceCompensationCauseAuthority,
        ),
    ):
        cause = TypeAdapter(ResourceCauseAuthority).validate_python(cause)
    refs: list[EvidenceRef] = []
    if isinstance(cause, DomainOperatorAuthorityBinding):
        refs.append(_operator_evidence(cause))
    elif isinstance(cause, SettledEventCauseAuthority):
        refs.append(
            _event_evidence(
                ref=cause.event_ref,
                revision=cause.world_revision,
                payload_hash=cause.payload_hash,
                purpose="conversation_continuity",
            )
        )
    elif isinstance(cause, ResourceCompensationCauseAuthority):
        refs.append(
            _event_evidence(
                ref=cause.target_accepted_event_ref,
                revision=cause.target_accepted_world_revision,
                payload_hash=cause.target_accepted_payload_hash,
                purpose="conversation_continuity",
            )
        )
        if cause.operator_authority is not None:
            refs.append(_operator_evidence(cause.operator_authority))
    return tuple(sorted(set(refs), key=lambda item: item.ref_id))


class V2ResourceChangedPayload(V16AuthorizedMutationEnvelope):
    operation: V2ResourceOperation
    authority_lane: Literal[
        "operator", "deliberative", "settlement", "compensation"
    ]
    selection_mode: Literal["direct", "random_draw"]
    random_draw_binding: RandomDrawBinding | None = None
    resource_before: V2ResourceProjection | None = None
    resource_after: V2ResourceProjection
    adjust_kind: Literal["state_change", "reclassify"] | None = None
    delta_bp: int | None = None
    cause_authority: ResourceCauseAuthority
    policy_version: str
    policy_digest: str

    @model_validator(mode="after")
    def envelope_is_exact(self) -> V2ResourceChangedPayload:
        after = self.resource_after
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("resource after origin does not match mutation envelope")
        if self.operation == "initialize":
            if self.resource_before is not None or self.expected_entity_revision != 0:
                raise ValueError("resource initialize must start at revision zero")
        elif self.resource_before is None or self.expected_entity_revision < 1:
            raise ValueError("resource transition requires an exact before image")
        expected_cause = {
            "operator": DomainOperatorAuthorityBinding,
            "deliberative": DeliberativeCauseAuthority,
            "settlement": SettledEventCauseAuthority,
            "compensation": ResourceCompensationCauseAuthority,
        }[self.authority_lane]
        if not isinstance(self.cause_authority, expected_cause):
            raise ValueError("resource cause does not match authority lane")
        if self.operation == "initialize" and self.authority_lane != "operator":
            raise ValueError("resource initialize is operator-only")
        if self.operation == "compensate" and self.authority_lane != "compensation":
            raise ValueError("resource compensation requires compensation lane")
        if self.operation == "adjust" and self.authority_lane not in {
            "operator", "deliberative", "settlement"
        }:
            raise ValueError("resource adjustment lane is invalid")
        if self.operation == "adjust":
            if self.adjust_kind is None or self.delta_bp is None:
                raise ValueError("resource adjustment requires kind and exact delta")
        elif self.adjust_kind is not None or self.delta_bp is not None:
            raise ValueError("resource operation cannot claim adjustment fields")
        if self.selection_mode == "random_draw" and self.random_draw_binding is None:
            raise ValueError("random Resource selection requires an exact draw binding")
        if self.selection_mode == "direct" and self.random_draw_binding is not None:
            raise ValueError("direct Resource selection cannot claim a random draw")
        if self.evidence_refs != v2_resource_evidence_refs(self):
            raise ValueError("resource EvidenceRefs are not exact cause authority")
        if self.accepted_change_hash != v2_resource_mutation_hash(self):
            raise ValueError("resource accepted change hash is invalid")
        return self


V2_RESOURCE_PAYLOAD_MODELS = {
    event_type: V2ResourceChangedPayload for event_type in V2_RESOURCE_EVENT_TYPES
}


class V2ResourceProposalCodec:
    """Canonical event-aware codec; registration remains an integration concern."""

    def encode_payload(self, payload: V2ResourceChangedPayload) -> str:
        if payload.selection_mode != "direct" or payload.random_draw_binding is not None:
            raise ValueError("random_authority_not_installed")
        return json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def decode_payload(self, event_type: str, payload_json: str) -> V2ResourceChangedPayload:
        if event_type not in V2_RESOURCE_PAYLOAD_MODELS:
            raise ValueError("event type is not owned by ResourceAuthority")
        decoded = json.loads(payload_json)
        if not isinstance(decoded, Mapping):
            raise ValueError("resource payload must be a JSON object")
        canonical = json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if payload_json != canonical:
            raise ValueError("resource payload JSON must be canonical")
        model = V2_RESOURCE_PAYLOAD_MODELS[event_type].model_validate_json(payload_json)
        if model.selection_mode != "direct" or model.random_draw_binding is not None:
            raise ValueError("random_authority_not_installed")
        require_resource_event_operation(event_type=event_type, operation=model.operation)
        return model

    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2ResourceProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("Resource codec only accepts ProposalRecorded")
        return V2ResourceProposalProjection.model_validate_json(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, V2ResourceProposalProjection):
            raise TypeError("Resource codec received an incompatible proposal")
        return ProposalAuthorityBinding(
            proposal_id=proposal.proposal_id,
            proposal_kind=proposal.proposal_kind,
            authority_contract_ref=proposal.authority_contract_ref,
            change_id=proposal.change_id,
            proposed_change_hash=proposal.proposed_change_hash,
            evaluated_world_revision=proposal.evaluated_world_revision,
            expected_entity_revision=proposal.expected_entity_revision,
            mutation_event_type=proposal.proposed_mutation.event_type,
        )

    def decode_mutation(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2ResourceChangedPayload:
        return self.decode_payload(
            event_type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        if not isinstance(mutation, V2ResourceChangedPayload):
            raise TypeError("Resource codec received an incompatible mutation")
        return AcceptedMutationBinding(
            proposal_id=mutation.proposal_id,
            acceptance_id=mutation.acceptance_id,
            evaluated_world_revision=mutation.evaluated_world_revision,
            change_id=mutation.change_id,
            accepted_change_hash=mutation.accepted_change_hash,
        )

    def record_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> tuple[object, ...]:
        return (
            world_id,
            payload.get("proposal_id"),
            payload.get("change_id"),
            payload.get("authority_contract_ref"),
        )

    def mutation_identity(
        self, *, world_id: str, event_type: str, payload: dict[str, object]
    ) -> tuple[object, ...]:
        after = payload.get("resource_after")
        return (
            world_id,
            after.get("actor_ref") if isinstance(after, dict) else None,
            after.get("resource_kind") if isinstance(after, dict) else None,
            payload.get("expected_entity_revision"),
            payload.get("transition_id"),
        )


V2_RESOURCE_CODEC = V2ResourceProposalCodec()


class V2SettledRecoveryInterval(FrozenModel):
    recovery_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    resource_kind: Literal["physical_energy", "cognitive_capacity", "social_capacity"]
    rest_class: Literal["sleep", "quiet_rest", "idle_rest"]
    interval_start: datetime
    interval_end: datetime
    source_event_ref: str = Field(min_length=1)
    source_world_revision: int = Field(ge=1)
    source_payload_hash: str = Field(min_length=64, max_length=64)
    source_entity_ref: str = Field(min_length=1)
    source_entity_revision: int = Field(ge=1)
    privacy_class: Literal["public", "shareable", "personal", "private", "withhold"]

    @model_validator(mode="after")
    def interval_is_exact(self) -> V2SettledRecoveryInterval:
        if self.interval_end <= self.interval_start:
            raise ValueError("recovery interval must move forward")
        return self


class V2ResourceClockAdjustedPayload(FrozenModel):
    """Mechanical wire payload; `.16.0` installs no recovery capability."""

    world_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=1)
    resource_before: V2ResourceProjection
    resource_after: V2ResourceProjection
    clock_authority: ClockCauseAuthority
    recovery_intervals: tuple[V2SettledRecoveryInterval, ...] = Field(min_length=1)
    raw_delta_bp: int = Field(gt=0)
    applied_delta_bp: int = Field(gt=0)
    input_digest: str = Field(min_length=64, max_length=64)
    recovery_policy_version: str = Field(min_length=1)
    recovery_policy_digest: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def mechanical_input_is_exact(self) -> V2ResourceClockAdjustedPayload:
        before, after = self.resource_before, self.resource_after
        if (
            not self.recovery_intervals
            or self.expected_entity_revision != before.entity_revision
            or after.entity_revision != before.entity_revision + 1
            or (after.actor_ref, after.resource_kind)
            != (before.actor_ref, before.resource_kind)
            or after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != before.origin.policy_refs
            or after.updated_at != self.clock_authority.logical_time_to
            or self.raw_delta_bp < self.applied_delta_bp
            or self.applied_delta_bp
            != min(self.raw_delta_bp, 10000 - before.values.value_bp)
            or after.values.value_bp
            != before.values.value_bp + self.applied_delta_bp
        ):
            raise ValueError("Resource mechanical recovery envelope is not exact")
        ordered = tuple(
            sorted(
                self.recovery_intervals,
                key=lambda item: (item.interval_start, item.interval_end, item.recovery_id),
            )
        )
        if ordered != self.recovery_intervals or len({item.recovery_id for item in ordered}) != len(ordered):
            raise ValueError("Resource recovery intervals must be canonical and unique")
        if any(left.interval_end > right.interval_start for left, right in zip(ordered, ordered[1:])):
            raise ValueError("Resource recovery intervals cannot overlap")
        if any(
            (item.actor_ref, item.resource_kind) != (before.actor_ref, before.resource_kind)
            or item.interval_start < self.clock_authority.logical_time_from
            or item.interval_end > self.clock_authority.logical_time_to
            or item.source_world_revision >= self.clock_authority.clock_world_revision
            for item in self.recovery_intervals
        ):
            raise ValueError("Resource recovery inputs are outside the exact Clock interval")
        expected_band = (
            "depleted" if after.values.value_bp < 1000 else
            "low" if after.values.value_bp < 3500 else
            "moderate" if after.values.value_bp < 6500 else
            "high" if after.values.value_bp < 9000 else "full"
        )
        if (
            after.values.derived_band != expected_band
            or after.semantic_fingerprint
            != v2_resource_semantic_fingerprint(
                actor_ref=after.actor_ref,
                resource_kind=after.resource_kind,
                values=after.values,
                policy_refs=after.origin.policy_refs,
            )
        ):
            raise ValueError("Resource recovery after image is invalid")
        material = self.model_dump(mode="json", exclude={"input_digest"})
        if self.input_digest != hashlib.sha256(
            json.dumps(
                canonicalize_json_value(material),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest():
            raise ValueError("Resource mechanical recovery input digest is invalid")
        return self


V2_RESOURCE_MECHANICAL_PAYLOAD_MODELS = {
    "V2ResourceClockAdjusted": V2ResourceClockAdjustedPayload
}


def reduce_v2_resource_clock_adjustment(
    resources: tuple[V2ResourceProjection, ...],
    payload: V2ResourceClockAdjustedPayload,
) -> tuple[V2ResourceProjection, ...]:
    del payload
    # The wire contract is parseable, but no rate/recovery policy artifact is
    # installed in `.16.0`; therefore it has no mutation authority.
    raise ValueError("resource_recovery_authority_not_installed")
