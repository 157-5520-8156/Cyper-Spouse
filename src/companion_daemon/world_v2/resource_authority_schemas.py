"""Immutable values for the pure `.16.0` ResourceAuthority seam."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .goal_situation_schemas import (
    ClockCauseAuthority,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    InternalIntentionBasis,
    SettledEventCauseAuthority,
)
from .resource_authority_contract import V2ResourceEventType, V2ResourceOperation
from .schema_core import EvidenceRef, FrozenModel, PrivacyClass, canonicalize_json_value


ResourceKind = Literal[
    "physical_energy",
    "cognitive_capacity",
    "social_capacity",
]
ResourceBand = Literal["depleted", "low", "moderate", "high", "full"]
ResourceAuthorityLane = Literal[
    "operator", "deliberative", "settlement", "clock_runtime", "compensation"
]


class V2ResourceValues(FrozenModel):
    value_bp: int = Field(ge=0, le=10000, strict=True)
    derived_band: ResourceBand
    band_policy_version: str = Field(min_length=1)
    band_policy_digest: str = Field(min_length=64, max_length=64)
    privacy_class: PrivacyClass


class V2ResourceOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def v2_resource_semantic_fingerprint(
    *,
    actor_ref: str,
    resource_kind: ResourceKind | str,
    values: V2ResourceValues,
    policy_refs: tuple[str, ...],
) -> str:
    material = {
        "actor_ref": actor_ref,
        "resource_kind": resource_kind,
        "values": values.model_dump(mode="json"),
        "policy_refs": sorted(policy_refs),
    }
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(material), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class V2ResourceProjection(FrozenModel):
    actor_ref: str = Field(min_length=1)
    resource_kind: ResourceKind
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: V2ResourceValues
    origin: V2ResourceOrigin
    updated_at: datetime


class ResourceCorrectionRationale(FrozenModel):
    text: str = Field(min_length=1, max_length=512)
    privacy_class: PrivacyClass

    @field_validator("text")
    @classmethod
    def text_is_canonical(cls, value: str) -> str:
        if (
            value != value.strip()
            or value != unicodedata.normalize("NFC", value)
            or any(unicodedata.category(character) == "Cc" for character in value)
        ):
            raise ValueError("resource correction rationale must be trimmed NFC text")
        return value


class ResourceOperatorCorrectionBasis(FrozenModel):
    basis_kind: Literal["resource_operator_correction"] = "resource_operator_correction"
    correction_class: Literal[
        "initialization_error",
        "resource_assignment_error",
        "resource_kind_error",
        "band_policy_application_error",
        "privacy_classification_error",
    ]
    privacy_class: PrivacyClass


class ResourceSelfAssessmentCorrectionBasis(FrozenModel):
    basis_kind: Literal["resource_self_assessment_correction"] = (
        "resource_self_assessment_correction"
    )
    correction_class: Literal[
        "self_assessment_revised",
        "source_interpretation_revised",
        "constraint_reassessed",
    ]
    new_intention: InternalIntentionBasis
    privacy_class: PrivacyClass


ResourceCorrectionBasis = Annotated[
    ResourceOperatorCorrectionBasis | ResourceSelfAssessmentCorrectionBasis,
    Field(discriminator="basis_kind"),
]


class ResourceCompensationCauseAuthority(FrozenModel):
    kind: Literal["resource_compensation"] = "resource_compensation"
    target_transition_id: str = Field(min_length=1)
    target_entity_revision: int = Field(ge=1)
    target_accepted_event_ref: str = Field(min_length=1)
    target_accepted_world_revision: int = Field(ge=1)
    target_accepted_payload_hash: str = Field(min_length=64, max_length=64)
    expected_target_lane: Literal["operator", "deliberative"] | None = None
    correction_basis: ResourceCorrectionBasis
    correction_rationale: ResourceCorrectionRationale
    operator_authority: DomainOperatorAuthorityBinding | None = None


ResourceCauseAuthority = Annotated[
    DomainOperatorAuthorityBinding
    | DeliberativeCauseAuthority
    | SettledEventCauseAuthority
    | ClockCauseAuthority
    | ResourceCompensationCauseAuthority,
    Field(discriminator="kind"),
]


class V2ResourceTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    resource_kind: ResourceKind
    entity_revision: int = Field(ge=1)
    operation: Literal["initialize", "adjust", "compensate"]
    adjust_kind: Literal["state_change", "reclassify"] | None = None
    authority_lane: ResourceAuthorityLane
    value_before: int | None = Field(default=None, ge=0, le=10000)
    delta_bp: int | None = None
    value_after: int = Field(ge=0, le=10000)
    band_before: ResourceBand | None = None
    band_after: ResourceBand
    values_before: V2ResourceValues | None = None
    values_after: V2ResourceValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    cause_authority: ResourceCauseAuthority
    compensates_transition_id: str | None = None


class V2ResourceProposedMutation(FrozenModel):
    event_type: V2ResourceEventType
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical_json_object(self) -> V2ResourceProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("Resource proposed mutation must be a JSON object")
        canonical = json.dumps(
            decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if self.payload_json != canonical:
            raise ValueError("Resource proposed mutation JSON must be canonical")
        return self


class V2ResourceProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["v2_resource_transition"] = "v2_resource_transition"
    proposal_encoding: Literal["typed-authority-v1"] = "typed-authority-v1"
    authority_contract_ref: Literal["proposal-contract:v2-resource.1"] = (
        "proposal-contract:v2-resource.1"
    )
    transition_kind: V2ResourceOperation
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    resource_kind: ResourceKind
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: V2ResourceProposedMutation

    @model_validator(mode="after")
    def transition_matches_payload(self) -> V2ResourceProposalProjection:
        from .resource_authority_contract import resource_event_for_operation
        from .resource_authority_events import V2_RESOURCE_CODEC

        if self.proposed_mutation.event_type != resource_event_for_operation(
            self.transition_kind
        ):
            raise ValueError("Resource proposal transition does not match event")
        mutation = V2_RESOURCE_CODEC.decode_payload(
            self.proposed_mutation.event_type,
            self.proposed_mutation.payload_json,
        )
        decoded = mutation.model_dump(mode="json")
        expected = {
            "proposal_id": self.proposal_id,
            "change_id": self.change_id,
            "transition_id": self.transition_id,
            "evaluated_world_revision": self.evaluated_world_revision,
            "expected_entity_revision": self.expected_entity_revision,
            "accepted_change_hash": self.proposed_change_hash,
        }
        if any(decoded.get(key) != value for key, value in expected.items()):
            raise ValueError("Resource proposal envelope does not match proposed mutation")
        after = decoded.get("resource_after")
        if not isinstance(after, dict) or (
            after.get("actor_ref") != self.actor_ref
            or after.get("resource_kind") != self.resource_kind
        ):
            raise ValueError("Resource proposal identity does not match after image")
        if (
            mutation.operation != self.transition_kind
            or mutation.evidence_refs != self.evidence_refs
            or mutation.policy_refs != self.policy_refs
            or json.dumps(
                decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            != self.proposed_mutation.payload_json
        ):
            raise ValueError("Resource proposal body does not match its exact index")
        return self


def validate_v2_resource_authority_state(
    resources: tuple[V2ResourceProjection, ...],
    transitions: tuple[V2ResourceTransitionProjection, ...],
    proposals: tuple[V2ResourceProposalProjection, ...],
    proposal_ids: tuple[str, ...],
    *,
    global_proposal_ids: tuple[str, ...],
    actor_authority_transitions: tuple[object, ...] = (),
    committed_events: tuple[object, ...] = (),
    logical_time: datetime | None = None,
    require_operator_bindings: bool = False,
) -> None:
    """Validate rebuilt Resource authority state without trusting tuple order."""

    identities = tuple((item.actor_ref, item.resource_kind) for item in resources)
    if len(identities) != len(set(identities)) or tuple(sorted(identities)) != identities:
        raise ValueError("Resource heads must have unique canonical identities")
    for values, label in (
        (tuple(item.transition_id for item in transitions), "transition ids"),
        (tuple(item.change_id for item in transitions), "change ids"),
        (tuple(item.accepted_event_ref for item in transitions), "accepted event refs"),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Resource {label} must be globally unique")
    by_identity: dict[tuple[str, str], list[V2ResourceTransitionProjection]] = {}
    for transition in transitions:
        by_identity.setdefault(
            (transition.actor_ref, transition.resource_kind), []
        ).append(transition)
    heads = {(item.actor_ref, item.resource_kind): item for item in resources}
    if transitions and (not committed_events or logical_time is None):
        raise ValueError("Resource history requires committed events and logical time")
    if transitions:
        global_revisions = tuple(
            int(getattr(event, "world_revision", 0))
            for transition in transitions
            for event in committed_events
            if getattr(event, "event_id", None) == transition.accepted_event_ref
        )
        if (
            len(global_revisions) != len(transitions)
            or tuple(sorted(global_revisions)) != global_revisions
            or len(set(global_revisions)) != len(global_revisions)
        ):
            raise ValueError("Resource history must follow canonical committed revision order")
    if set(by_identity) != set(heads):
        raise ValueError("each Resource lineage must have exactly one current head")
    privacy_rank = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
    for identity, lineage in by_identity.items():
        previous: V2ResourceTransitionProjection | None = None
        previous_world_revision = 0
        for expected_revision, transition in enumerate(lineage, start=1):
            if transition.entity_revision != expected_revision:
                raise ValueError("Resource lineage revisions must be contiguous and ordered")
            if logical_time is not None and transition.accepted_at > logical_time:
                raise ValueError("Resource transition cannot be later than state logical time")
            if committed_events:
                from .resource_authority_contract import resource_event_for_operation

                accepted_event = next(
                    (
                        item for item in committed_events
                        if getattr(item, "event_id", None) == transition.accepted_event_ref
                    ),
                    None,
                )
                if (
                    accepted_event is None
                    or getattr(accepted_event, "event_type", None)
                    != resource_event_for_operation(transition.operation)
                    or getattr(accepted_event, "logical_time", None) != transition.accepted_at
                ):
                    raise ValueError("Resource transition lacks exact committed mutation event")
                accepted_world_revision = int(
                    getattr(accepted_event, "world_revision", 0)
                )
                if accepted_world_revision <= previous_world_revision:
                    raise ValueError("Resource committed revisions must strictly increase")
                previous_world_revision = accepted_world_revision
            if transition.operation == "initialize":
                if (
                    previous is not None
                    or transition.values_before is not None
                    or transition.value_before is not None
                    or transition.band_before is not None
                    or transition.delta_bp is not None
                    or transition.adjust_kind is not None
                    or transition.authority_lane != "operator"
                    or not isinstance(transition.cause_authority, DomainOperatorAuthorityBinding)
                    or transition.compensates_transition_id is not None
                ):
                    raise ValueError("Resource initialize must begin its lineage")
            else:
                if previous is None or transition.values_before != previous.values_after:
                    raise ValueError("Resource transition before image must match prior lineage")
                if transition.accepted_at < previous.accepted_at:
                    raise ValueError("Resource accepted_at cannot move backwards")
                if privacy_rank[transition.values_after.privacy_class] < privacy_rank[previous.values_after.privacy_class]:
                    raise ValueError("Resource lineage privacy cannot weaken")
            values = transition.values_after
            expected_band = (
                "depleted" if values.value_bp < 1000 else
                "low" if values.value_bp < 3500 else
                "moderate" if values.value_bp < 6500 else
                "high" if values.value_bp < 9000 else "full"
            )
            expected_fingerprint = v2_resource_semantic_fingerprint(
                actor_ref=transition.actor_ref,
                resource_kind=transition.resource_kind,
                values=values,
                policy_refs=transition.policy_refs,
            )
            if (
                transition.value_after != values.value_bp
                or transition.band_after != values.derived_band
                or values.derived_band != expected_band
                or transition.semantic_fingerprint_after != expected_fingerprint
                or transition.policy_refs != ("policy:v2-resource-authority.1",)
                or transition.policy_version != "v2-resource-authority-policy.1"
                or transition.policy_digest
                != "4c0ab2146cf9d8986768a9029f7b07c371e834b32ec24926ec55c85a73220725"
                or values.band_policy_version != "resource-band-policy.1"
                or values.band_policy_digest
                != "fca79bf8359b73c5e52cfd1c0c0d429511a39ce40210b121f52b9a531a87f06d"
            ):
                raise ValueError("Resource transition redundant values or policy are invalid")
            if transition.values_before is None:
                if transition.value_before is not None or transition.band_before is not None:
                    raise ValueError("Resource transition before scalars require before values")
            elif (
                transition.value_before != transition.values_before.value_bp
                or transition.band_before != transition.values_before.derived_band
            ):
                raise ValueError("Resource transition before scalars are not exact")
            if transition.operation == "adjust":
                if (
                    transition.adjust_kind != "state_change"
                    or transition.authority_lane not in {"operator", "deliberative"}
                    or transition.delta_bp is None
                    or transition.delta_bp == 0
                    or transition.values_before is None
                    or transition.values_before.value_bp + transition.delta_bp
                    != transition.values_after.value_bp
                    or transition.values_before == transition.values_after
                    or transition.compensates_transition_id is not None
                ):
                    raise ValueError("Resource adjustment history is not conservative")
                expected_cause_type = {
                    "operator": DomainOperatorAuthorityBinding,
                    "deliberative": DeliberativeCauseAuthority,
                }[transition.authority_lane]
                if not isinstance(transition.cause_authority, expected_cause_type):
                    raise ValueError("Resource adjustment history has the wrong cause lane")
                if transition.authority_lane == "deliberative":
                    deliberation = transition.cause_authority
                    assert isinstance(deliberation, DeliberativeCauseAuthority)
                    basis = deliberation.basis
                    if (
                        not isinstance(basis, InternalIntentionBasis)
                        or basis.actor_ref != transition.actor_ref
                        or basis.logical_time != transition.accepted_at
                        or basis.intention_kind != "resource_self_regulation"
                        or basis.policy_version != "v2-resource-self-regulation.1"
                        or basis.policy_digest
                        != "8f0bfbefc0444c85c8d8c93b991ac616f2f7f0108c0c89ba43181889e486a125"
                    ):
                        raise ValueError("Resource deliberative history basis is not exact")
                    required_privacy = max(
                        (
                            transition.values_before.privacy_class,
                            basis.privacy_class,
                            basis.rationale.privacy_class,
                        ),
                        key=privacy_rank.__getitem__,
                    )
                    if privacy_rank[transition.values_after.privacy_class] < privacy_rank[required_privacy]:
                        raise ValueError("Resource deliberative history weakens privacy")
            if transition.operation == "compensate":
                if (
                    previous is None
                    or transition.adjust_kind is not None
                    or transition.delta_bp is not None
                    or transition.authority_lane != "compensation"
                    or not isinstance(transition.cause_authority, ResourceCompensationCauseAuthority)
                    or transition.compensates_transition_id != previous.transition_id
                    or transition.cause_authority.target_transition_id != previous.transition_id
                ):
                    raise ValueError("Resource compensation history must target prior latest")
                cause = transition.cause_authority
                target_event = next(
                    (
                        item for item in committed_events
                        if getattr(item, "event_id", None)
                        == cause.target_accepted_event_ref
                    ),
                    None,
                )
                if (
                    cause.target_entity_revision != previous.entity_revision
                    or cause.target_accepted_event_ref != previous.accepted_event_ref
                    or target_event is None
                    or getattr(target_event, "world_revision", None)
                    != cause.target_accepted_world_revision
                    or getattr(target_event, "payload_hash", None)
                    != cause.target_accepted_payload_hash
                    or getattr(target_event, "logical_time", None) != previous.accepted_at
                ):
                    raise ValueError("Resource compensation history target is not exact")
                privacy_sources = (
                    previous.values_before.privacy_class,
                    previous.values_after.privacy_class,
                    cause.correction_basis.privacy_class,
                    cause.correction_rationale.privacy_class,
                )
                if isinstance(cause.correction_basis, ResourceSelfAssessmentCorrectionBasis):
                    privacy_sources = (
                        *privacy_sources,
                        cause.correction_basis.new_intention.privacy_class,
                        cause.correction_basis.new_intention.rationale.privacy_class,
                    )
                required_privacy = max(privacy_sources, key=privacy_rank.__getitem__)
                expected_values = previous.values_before.model_copy(
                    update={"privacy_class": required_privacy}
                )
                if transition.values_after != expected_values:
                    raise ValueError("Resource compensation history restore is not exact")
                base = previous
                visited: set[str] = set()
                while base.operation == "compensate":
                    if base.transition_id in visited or base.compensates_transition_id is None:
                        raise ValueError("Resource compensation history lineage is cyclic")
                    visited.add(base.transition_id)
                    base = next(
                        (
                            item for item in lineage[: expected_revision - 1]
                            if item.transition_id == base.compensates_transition_id
                        ),
                        None,
                    )
                    if base is None:
                        raise ValueError("Resource compensation history lineage is incomplete")
                if base.authority_lane == "operator":
                    if (
                        not isinstance(cause.correction_basis, ResourceOperatorCorrectionBasis)
                        or cause.operator_authority is None
                    ):
                        raise ValueError("operator Resource history needs operator correction")
                elif base.authority_lane == "deliberative":
                    if (
                        not isinstance(cause.correction_basis, ResourceSelfAssessmentCorrectionBasis)
                        or cause.operator_authority is not None
                    ):
                        raise ValueError("deliberative Resource history needs self correction")
                    intention = cause.correction_basis.new_intention
                    if (
                        intention.actor_ref != transition.actor_ref
                        or intention.logical_time != transition.accepted_at
                        or intention.intention_kind != "resource_self_regulation"
                        or intention.policy_version != "v2-resource-self-regulation.1"
                        or intention.policy_digest
                        != "8f0bfbefc0444c85c8d8c93b991ac616f2f7f0108c0c89ba43181889e486a125"
                    ):
                        raise ValueError("Resource compensation intention is not exact")
                else:
                    raise ValueError("Resource history has unsupported effective lane")
            elif transition.compensates_transition_id is not None:
                raise ValueError("non-compensation Resource transition cannot claim a target")
            operator = None
            if isinstance(transition.cause_authority, DomainOperatorAuthorityBinding):
                operator = transition.cause_authority
            elif isinstance(transition.cause_authority, ResourceCompensationCauseAuthority):
                operator = transition.cause_authority.operator_authority
            if operator is not None and require_operator_bindings:
                authority = next(
                    (
                        item for item in actor_authority_transitions
                        if getattr(item, "authority_id", None) == operator.authority_id
                        and getattr(item, "authority_revision", None)
                        == operator.authority_revision
                    ),
                    None,
                )
                authority_event = next(
                    (
                        item for item in committed_events
                        if getattr(item, "event_id", None) == operator.authority_event_ref
                    ),
                    None,
                )
                authority_values = getattr(authority, "values_after", None)
                values_hash = (
                    hashlib.sha256(
                        json.dumps(
                            authority_values.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if authority_values is not None
                    else None
                )
                if (
                    authority is None
                    or authority_event is None
                    or getattr(authority, "accepted_event_ref", None)
                    != operator.authority_event_ref
                    or getattr(authority, "accepted_world_revision", None)
                    != operator.authority_world_revision
                    or getattr(authority, "accepted_payload_hash", None)
                    != operator.authority_payload_hash
                    or operator.required_operation != "v2_resource_governance"
                    or getattr(authority, "policy_version", None)
                    != "actor-authority-policy.2"
                    or getattr(authority, "policy_digest", None)
                    # ACTOR_AUTHORITY_V2_POLICY_DIGEST after the 2026-07-20 production-2 root rotation.
                    != "a4648e30df25bbb3b8a709ceb579ab5ea8cc2f292bfc1a5fbc02e3e34cbdf19f"
                    or getattr(authority, "policy_digest", None)
                    != operator.authority_policy_digest
                    or values_hash != operator.authority_values_hash
                    or authority_values.principal_ref != operator.principal_ref
                    or authority_values.principal_kind != "deployment_operator"
                    or "v2_resource_governance" not in authority_values.allowed_operations
                    or not set(authority_values.allowed_operations).issubset(
                        {
                            "actor_authority_rotation",
                            "capability_grant",
                            "character_core_governance",
                            "consent_grant",
                            "privacy_policy",
                            "v2_attention_governance",
                            "v2_goal_governance",
                            "v2_location_governance",
                            "v2_resource_governance",
                        }
                    )
                    or authority_values.status != "active"
                    or authority_values.valid_from > transition.accepted_at
                    or (
                        authority_values.expires_at is not None
                        and authority_values.expires_at <= transition.accepted_at
                    )
                    or getattr(authority_event, "world_revision", None)
                    != operator.authority_world_revision
                    or getattr(authority_event, "payload_hash", None)
                    != operator.authority_payload_hash
                    or getattr(authority_event, "event_type", None)
                    != {
                        "bootstrap": "ActorAuthorityBootstrapped",
                        "rotate": "ActorAuthorityRotated",
                        "compensate": "ActorAuthorityCompensated",
                        "revoke": "ActorAuthorityRevoked",
                    }.get(getattr(authority, "operation", None))
                    or getattr(authority_event, "logical_time", None)
                    != getattr(authority, "changed_at", None)
                    or getattr(authority_event, "world_revision", 0)
                    >= accepted_world_revision
                ):
                    raise ValueError("Resource history lacks exact operator authority")
            previous = transition
        latest = lineage[-1]
        head = heads[identity]
        if (
            head.entity_revision != latest.entity_revision
            or head.values != latest.values_after
            or head.semantic_fingerprint != latest.semantic_fingerprint_after
            or head.origin.change_id != latest.change_id
            or head.origin.transition_id != latest.transition_id
            or head.origin.policy_refs != latest.policy_refs
            or head.origin.accepted_event_ref != latest.accepted_event_ref
            or head.updated_at != latest.accepted_at
        ):
            raise ValueError("Resource head must equal the latest canonical transition")
    actual_ids = tuple(item.proposal_id for item in proposals)
    if proposal_ids != actual_ids or len(actual_ids) != len(set(actual_ids)):
        raise ValueError("Resource proposal index must exactly match unique proposals")
    if any(item not in global_proposal_ids for item in actual_ids):
        raise ValueError("Resource proposal ids must be present in the global index")
    if proposals and not committed_events:
        raise ValueError("pending Resource proposals require committed authority")
    if committed_events:
        committed_ids = {
            getattr(item, "event_id", None) for item in committed_events
        }
        for proposal in proposals:
            if any(item.ref_id not in committed_ids for item in proposal.evidence_refs):
                raise ValueError("Resource proposal evidence must resolve to committed events")
            if any(
                item.source_world_revision is not None
                and item.source_world_revision > proposal.evaluated_world_revision
                for item in proposal.evidence_refs
            ):
                raise ValueError("pending Resource proposal uses evidence after cutoff")
            from .resource_authority_events import V2_RESOURCE_CODEC
            from .resource_authority_reducers import (
                V2_RESOURCE_POLICY_DIGEST,
                V2_RESOURCE_POLICY_REFS,
                V2_RESOURCE_POLICY_VERSION,
            )

            payload = V2_RESOURCE_CODEC.decode_payload(
                proposal.proposed_mutation.event_type,
                proposal.proposed_mutation.payload_json,
            )
            current_world_revision = max(
                (int(getattr(item, "world_revision", 0)) for item in committed_events),
                default=0,
            )
            if proposal.evaluated_world_revision > current_world_revision:
                raise ValueError("pending Resource proposal evaluates a future revision")
            if (
                payload.policy_refs != V2_RESOURCE_POLICY_REFS
                or payload.policy_version != V2_RESOURCE_POLICY_VERSION
                or payload.policy_digest != V2_RESOURCE_POLICY_DIGEST
                or payload.selection_mode != "direct"
                or payload.random_draw_binding is not None
            ):
                raise ValueError("pending Resource proposal uses uninstalled authority")
            eligible = tuple(
                transition
                for transition in transitions
                if (
                    transition.actor_ref,
                    transition.resource_kind,
                )
                == (
                    payload.resource_after.actor_ref,
                    payload.resource_after.resource_kind,
                )
                and (
                    event := next(
                        (
                            item for item in committed_events
                            if getattr(item, "event_id", None)
                            == transition.accepted_event_ref
                        ),
                        None,
                    )
                )
                is not None
                and int(getattr(event, "world_revision", 0))
                <= proposal.evaluated_world_revision
            )
            cutoff = eligible[-1] if eligible else None
            cutoff_head = (
                V2ResourceProjection(
                    actor_ref=cutoff.actor_ref,
                    resource_kind=cutoff.resource_kind,
                    entity_revision=cutoff.entity_revision,
                    semantic_fingerprint=cutoff.semantic_fingerprint_after,
                    values=cutoff.values_after,
                    origin=V2ResourceOrigin(
                        change_id=cutoff.change_id,
                        transition_id=cutoff.transition_id,
                        policy_refs=cutoff.policy_refs,
                        accepted_event_ref=cutoff.accepted_event_ref,
                    ),
                    updated_at=cutoff.accepted_at,
                )
                if cutoff is not None
                else None
            )
            if payload.operation == "initialize":
                cas_valid = (
                    cutoff_head is None
                    and payload.resource_before is None
                    and payload.expected_entity_revision == 0
                    and payload.resource_after.entity_revision == 1
                )
            else:
                cas_valid = (
                    cutoff_head is not None
                    and payload.resource_before == cutoff_head
                    and payload.expected_entity_revision == cutoff_head.entity_revision
                    and payload.resource_after.entity_revision
                    == cutoff_head.entity_revision + 1
                )
            if not cas_valid:
                raise ValueError("pending Resource proposal has invalid cutoff CAS")
            cause = payload.cause_authority
            operator = (
                cause.operator_authority
                if isinstance(cause, ResourceCompensationCauseAuthority)
                else cause
                if isinstance(cause, DomainOperatorAuthorityBinding)
                else None
            )
            if operator is not None:
                authority = next(
                    (
                        item for item in actor_authority_transitions
                        if getattr(item, "authority_id", None) == operator.authority_id
                        and getattr(item, "authority_revision", None)
                        == operator.authority_revision
                    ),
                    None,
                )
                event = next(
                    (
                        item for item in committed_events
                        if getattr(item, "event_id", None)
                        == operator.authority_event_ref
                    ),
                    None,
                )
                values = getattr(authority, "values_after", None)
                values_hash = (
                    hashlib.sha256(
                        json.dumps(
                            values.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
                    if values is not None
                    else None
                )
                expected_event_type = {
                    "bootstrap": "ActorAuthorityBootstrapped",
                    "rotate": "ActorAuthorityRotated",
                    "revoke": "ActorAuthorityRevoked",
                    "compensate": "ActorAuthorityCompensated",
                }.get(getattr(authority, "operation", None))
                if (
                    authority is None
                    or event is None
                    or getattr(authority, "accepted_event_ref", None)
                    != operator.authority_event_ref
                    or getattr(authority, "accepted_world_revision", None)
                    != operator.authority_world_revision
                    or getattr(authority, "accepted_payload_hash", None)
                    != operator.authority_payload_hash
                    or getattr(authority, "policy_version", None)
                    != "actor-authority-policy.2"
                    or getattr(authority, "policy_digest", None)
                    # ACTOR_AUTHORITY_V2_POLICY_DIGEST after the 2026-07-20 production-2 root rotation.
                    != "a4648e30df25bbb3b8a709ceb579ab5ea8cc2f292bfc1a5fbc02e3e34cbdf19f"
                    or getattr(authority, "policy_digest", None)
                    != operator.authority_policy_digest
                    or values_hash != operator.authority_values_hash
                    or values.principal_ref != operator.principal_ref
                    or values.principal_kind != "deployment_operator"
                    or "v2_resource_governance" not in values.allowed_operations
                    or values.status != "active"
                    or values.valid_from > payload.resource_after.updated_at
                    or (
                        values.expires_at is not None
                        and values.expires_at <= payload.resource_after.updated_at
                    )
                    or operator.required_operation != "v2_resource_governance"
                    or getattr(event, "event_type", None) != expected_event_type
                    or getattr(event, "world_revision", None)
                    != operator.authority_world_revision
                    or getattr(event, "payload_hash", None)
                    != operator.authority_payload_hash
                    or getattr(event, "logical_time", None)
                    != getattr(authority, "changed_at", None)
                    or getattr(event, "world_revision", 0)
                    > proposal.evaluated_world_revision
                ):
                    raise ValueError("pending Resource proposal lacks exact operator authority")
