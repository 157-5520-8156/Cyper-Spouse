"""Typed payload and canonical proposal codec for `.16.0` AttentionAuthority.

DORMANT — no producer: no production ledger holds a committed
``V2Attention*`` event and no runtime constructs these payloads.  The live
phone-attention need is served by the ``attention_view`` advisory (a pure
projection, never an event writer).  Before wiring a producer, read the
Producer-First Authority rule in CONTEXT.md and record the activation
verdict in ``configs/mechanism_closure.yaml`` (``v16-situation-constituents``).
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Literal

from pydantic import TypeAdapter, model_validator
from pydantic_core import to_jsonable_python

from .attention_authority_contract import (
    V2_ATTENTION_MUTATION_EVENT_TYPES,
    V2AttentionOperation,
    require_attention_event_operation,
)
from .attention_authority_schemas import (
    AttentionCauseAuthority,
    AttentionCompensationCauseAuthority,
    V2AttentionProjection,
    V2AttentionProposalProjection,
)
from .goal_situation_schemas import (
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    RandomDrawBinding,
    SettledEventCauseAuthority,
    V16AuthorizedMutationEnvelope,
)
from .schema_core import EvidenceRef, canonicalize_json_value
from .typed_proposals import AcceptedMutationBinding, ProposalAuthorityBinding


def v2_attention_mutation_hash(value: object) -> str:
    material = (
        value.model_dump(mode="json")
        if hasattr(value, "model_dump")
        else to_jsonable_python(dict(value))  # type: ignore[arg-type]
    )
    material.pop("accepted_change_hash", None)
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(material),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _evidence(*, ref: str, revision: int, payload_hash: str, purpose: str) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref,
        evidence_type="committed_world_event",
        claim_purpose=purpose,  # type: ignore[arg-type]
        source_world_revision=revision,
        immutable_hash=payload_hash,
    )


def _operator_evidence(binding: DomainOperatorAuthorityBinding) -> EvidenceRef:
    return _evidence(
        ref=binding.authority_event_ref,
        revision=binding.authority_world_revision,
        payload_hash=binding.authority_payload_hash,
        purpose="action_authorization",
    )


def v2_attention_evidence_refs(value: object) -> tuple[EvidenceRef, ...]:
    raw = value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)  # type: ignore[arg-type]
    cause = raw["cause_authority"]
    if not isinstance(
        cause,
        (
            DomainOperatorAuthorityBinding,
            DeliberativeCauseAuthority,
            SettledEventCauseAuthority,
            AttentionCompensationCauseAuthority,
        ),
    ):
        cause = TypeAdapter(AttentionCauseAuthority).validate_python(cause)
    refs: list[EvidenceRef] = []
    if isinstance(cause, DomainOperatorAuthorityBinding):
        refs.append(_operator_evidence(cause))
    elif isinstance(cause, SettledEventCauseAuthority):
        refs.append(
            _evidence(
                ref=cause.event_ref,
                revision=cause.world_revision,
                payload_hash=cause.payload_hash,
                purpose="conversation_continuity",
            )
        )
    elif isinstance(cause, AttentionCompensationCauseAuthority):
        refs.append(
            _evidence(
                ref=cause.target_accepted_event_ref,
                revision=cause.target_accepted_world_revision,
                payload_hash=cause.target_accepted_payload_hash,
                purpose="conversation_continuity",
            )
        )
        if cause.operator_authority is not None:
            refs.append(_operator_evidence(cause.operator_authority))
    return tuple(sorted(set(refs), key=lambda item: item.ref_id))


class V2AttentionChangedPayload(V16AuthorizedMutationEnvelope):
    operation: V2AttentionOperation
    authority_lane: Literal["operator", "deliberative", "settlement", "compensation"]
    selection_mode: Literal["direct", "random_draw"]
    random_draw_binding: RandomDrawBinding | None = None
    attention_before: V2AttentionProjection | None = None
    attention_after: V2AttentionProjection
    cause_authority: AttentionCauseAuthority
    policy_version: str
    policy_digest: str

    @model_validator(mode="after")
    def envelope_is_exact(self) -> V2AttentionChangedPayload:
        after = self.attention_after
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("Attention after origin does not match mutation envelope")
        expected_cause = {
            "operator": DomainOperatorAuthorityBinding,
            "deliberative": DeliberativeCauseAuthority,
            "settlement": SettledEventCauseAuthority,
            "compensation": AttentionCompensationCauseAuthority,
        }[self.authority_lane]
        if not isinstance(self.cause_authority, expected_cause):
            raise ValueError("Attention cause does not match authority lane")
        if self.operation == "establish" and (
            self.authority_lane != "operator"
            or self.attention_before is not None
            or self.expected_entity_revision != 0
        ):
            raise ValueError("Attention establish is operator-only from revision zero")
        if self.operation == "change" and (
            self.authority_lane not in {"operator", "deliberative"}
            or self.attention_before is None
            or self.expected_entity_revision < 1
        ):
            raise ValueError("Attention change requires an installed authority and before image")
        if self.operation == "compensate" and (
            self.authority_lane != "compensation" or self.attention_before is None
        ):
            raise ValueError("Attention compensation requires exact before image")
        if self.selection_mode == "random_draw":
            raise ValueError("random_authority_not_installed")
        if self.random_draw_binding is not None:
            raise ValueError("direct Attention selection cannot claim a random draw")
        if self.evidence_refs != v2_attention_evidence_refs(self):
            raise ValueError("Attention EvidenceRefs are not exact cause authority")
        if self.accepted_change_hash != v2_attention_mutation_hash(self):
            raise ValueError("accepted Attention change hash is invalid")
        return self


V2_ATTENTION_PAYLOAD_MODELS = {
    event_type: V2AttentionChangedPayload
    for event_type in V2_ATTENTION_MUTATION_EVENT_TYPES
}


class V2AttentionProposalCodec:
    def encode_payload(self, payload: V2AttentionChangedPayload) -> str:
        return json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def decode_payload(self, event_type: str, payload_json: str) -> V2AttentionChangedPayload:
        if event_type not in V2_ATTENTION_PAYLOAD_MODELS:
            raise ValueError("event type is not owned by AttentionAuthority")
        decoded = json.loads(payload_json)
        if not isinstance(decoded, Mapping):
            raise ValueError("Attention payload must be a JSON object")
        if payload_json != json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ):
            raise ValueError("Attention payload JSON must be canonical")
        model = V2_ATTENTION_PAYLOAD_MODELS[event_type].model_validate_json(payload_json)
        require_attention_event_operation(event_type=event_type, operation=model.operation)
        return model

    def decode_record(
        self, *, event_type: str, payload: dict[str, object]
    ) -> V2AttentionProposalProjection:
        if event_type != "ProposalRecorded":
            raise ValueError("Attention codec only accepts ProposalRecorded")
        return V2AttentionProposalProjection.model_validate_json(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def bind(self, proposal: object) -> ProposalAuthorityBinding:
        if not isinstance(proposal, V2AttentionProposalProjection):
            raise TypeError("Attention codec received incompatible proposal")
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
    ) -> V2AttentionChangedPayload:
        return self.decode_payload(
            event_type,
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

    def bind_mutation(self, mutation: object) -> AcceptedMutationBinding:
        if not isinstance(mutation, V2AttentionChangedPayload):
            raise TypeError("Attention codec received incompatible mutation")
        return AcceptedMutationBinding(
            proposal_id=mutation.proposal_id,
            acceptance_id=mutation.acceptance_id,
            evaluated_world_revision=mutation.evaluated_world_revision,
            change_id=mutation.change_id,
            accepted_change_hash=mutation.accepted_change_hash,
        )


V2_ATTENTION_CODEC = V2AttentionProposalCodec()
