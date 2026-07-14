"""Immutable value objects for the `.16.0` LocationAuthority pure seam."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .goal_situation_schemas import DomainOperatorAuthorityBinding
from .location_authority_contract import location_event_for_operation
from .schema_core import EvidenceRef, FrozenModel, PrivacyClass


SceneVisibility = Literal["private", "shareable", "public"]


class V2LocationValues(FrozenModel):
    location_ref: str = Field(min_length=1)
    zone_ref: str | None = Field(default=None, min_length=1)
    scene_visibility: SceneVisibility
    privacy_class: PrivacyClass
    since: datetime


class V2LocationOrigin(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)


def v2_location_semantic_fingerprint(
    *, actor_ref: str, values: V2LocationValues, policy_refs: tuple[str, ...]
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "actor_ref": actor_ref,
                "values": values.model_dump(mode="json"),
                "policy_refs": sorted(policy_refs),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class V2LocationProjection(FrozenModel):
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    semantic_fingerprint: str = Field(min_length=64, max_length=64)
    values: V2LocationValues
    origin: V2LocationOrigin
    updated_at: datetime


class LocationCorrectionRationale(FrozenModel):
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
            raise ValueError("location correction rationale must be trimmed NFC text")
        return value


class LocationOperatorCorrectionBasis(FrozenModel):
    basis_kind: Literal["location_operator_correction"] = "location_operator_correction"
    correction_class: Literal[
        "location_assignment_error",
        "zone_assignment_error",
        "scene_classification_error",
        "privacy_classification_error",
    ]
    privacy_class: PrivacyClass


class LocationCompensationCauseAuthority(FrozenModel):
    kind: Literal["location_compensation"] = "location_compensation"
    target_transition_id: str = Field(min_length=1)
    target_entity_revision: int = Field(ge=1)
    target_accepted_event_ref: str = Field(min_length=1)
    target_accepted_world_revision: int = Field(ge=1)
    target_accepted_payload_hash: str = Field(min_length=64, max_length=64)
    expected_target_lane: Literal["operator"] | None = None
    correction_basis: LocationOperatorCorrectionBasis
    correction_rationale: LocationCorrectionRationale
    operator_authority: DomainOperatorAuthorityBinding


LocationCauseAuthority = Annotated[
    DomainOperatorAuthorityBinding | LocationCompensationCauseAuthority,
    Field(discriminator="kind"),
]


class V2LocationTransitionProjection(FrozenModel):
    transition_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    entity_revision: int = Field(ge=1)
    operation: Literal["establish", "change", "compensate"]
    authority_lane: Literal["operator", "compensation"]
    values_before: V2LocationValues | None = None
    values_after: V2LocationValues
    semantic_fingerprint_after: str = Field(min_length=64, max_length=64)
    change_id: str = Field(min_length=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    accepted_event_ref: str = Field(min_length=1)
    accepted_at: datetime
    cause_authority: LocationCauseAuthority
    compensates_transition_id: str | None = None


class V2LocationProposedMutation(FrozenModel):
    event_type: Literal["V2LocationChanged", "V2LocationChangeCompensated"]
    payload_json: str = Field(min_length=2)

    @model_validator(mode="after")
    def payload_is_canonical_json_object(self) -> V2LocationProposedMutation:
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict):
            raise ValueError("location proposed mutation must be a JSON object")
        canonical = json.dumps(decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self.payload_json != canonical:
            raise ValueError("location proposed mutation JSON must be canonical")
        return self


class V2LocationProposalProjection(FrozenModel):
    proposal_id: str = Field(min_length=1)
    proposal_kind: Literal["v2_location_transition"] = "v2_location_transition"
    proposal_encoding: Literal["typed-authority-v1"] = "typed-authority-v1"
    authority_contract_ref: Literal["proposal-contract:v2-location.1"] = (
        "proposal-contract:v2-location.1"
    )
    transition_kind: Literal["establish", "change", "compensate"]
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    expected_entity_revision: int = Field(ge=0)
    proposed_change_hash: str = Field(min_length=64, max_length=64)
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)
    proposed_mutation: V2LocationProposedMutation

    @model_validator(mode="after")
    def transition_matches_event_and_payload(self) -> V2LocationProposalProjection:
        expected = location_event_for_operation(self.transition_kind)
        if self.proposed_mutation.event_type != expected:
            raise ValueError("location proposal transition does not match event")
        decoded = json.loads(self.proposed_mutation.payload_json)
        expected_fields = {
            "proposal_id": self.proposal_id,
            "change_id": self.change_id,
            "transition_id": self.transition_id,
            "evaluated_world_revision": self.evaluated_world_revision,
            "expected_entity_revision": self.expected_entity_revision,
            "accepted_change_hash": self.proposed_change_hash,
        }
        if any(decoded.get(key) != value for key, value in expected_fields.items()):
            raise ValueError("location proposal envelope does not match proposed mutation")
        return self


def validate_v2_location_authority_state(
    locations: tuple[V2LocationProjection, ...],
    transitions: tuple[V2LocationTransitionProjection, ...],
    proposals: tuple[V2LocationProposalProjection, ...],
    proposal_ids: tuple[str, ...],
    *,
    global_proposal_ids: tuple[str, ...],
    actor_authority_transitions: tuple[object, ...] = (),
    committed_events: tuple[object, ...] = (),
    logical_time: datetime | None = None,
) -> None:
    """Validate a rebuilt Location projection without trusting reducer history."""

    actor_refs = tuple(item.actor_ref for item in locations)
    if len(actor_refs) != len(set(actor_refs)):
        raise ValueError("Location heads must contain at most one head per actor")
    transition_ids = tuple(item.transition_id for item in transitions)
    change_ids = tuple(item.change_id for item in transitions)
    event_refs = tuple(item.accepted_event_ref for item in transitions)
    for values, label in (
        (transition_ids, "transition ids"),
        (change_ids, "change ids"),
        (event_refs, "accepted event refs"),
    ):
        if len(values) != len(set(values)):
            raise ValueError(f"Location {label} must be globally unique")

    by_actor: dict[str, list[V2LocationTransitionProjection]] = {}
    for transition in transitions:
        by_actor.setdefault(transition.actor_ref, []).append(transition)
    heads_by_actor = {item.actor_ref: item for item in locations}
    if set(by_actor) != set(heads_by_actor):
        raise ValueError("each Location lineage must have exactly one current head")
    privacy_rank = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}
    for actor_ref, lineage in by_actor.items():
        previous: V2LocationTransitionProjection | None = None
        previous_world_revision = 0
        for expected_revision, transition in enumerate(lineage, start=1):
            if transition.entity_revision != expected_revision:
                raise ValueError("Location lineage entity revisions must be contiguous")
            if transition.policy_refs != ("policy:v2-location-authority.1",):
                raise ValueError("Location transition references an uninstalled policy")
            if transition.values_after.since > transition.accepted_at:
                raise ValueError("Location since cannot be later than its accepted transition")
            accepted_event = next(
                (
                    item
                    for item in committed_events
                    if getattr(item, "event_id", None) == transition.accepted_event_ref
                ),
                None,
            )
            expected_event_type = location_event_for_operation(transition.operation)
            if (
                accepted_event is None
                or getattr(accepted_event, "event_type", None) != expected_event_type
                or getattr(accepted_event, "logical_time", None) != transition.accepted_at
            ):
                raise ValueError("Location transition lacks its exact committed mutation event")
            accepted_world_revision = int(getattr(accepted_event, "world_revision", 0))
            if accepted_world_revision <= previous_world_revision:
                raise ValueError("Location committed revisions must be strictly increasing")
            previous_world_revision = accepted_world_revision
            if transition.operation == "establish":
                if (
                    previous is not None
                    or transition.values_before is not None
                    or transition.authority_lane != "operator"
                    or not isinstance(
                        transition.cause_authority, DomainOperatorAuthorityBinding
                    )
                    or transition.values_after.since != transition.accepted_at
                ):
                    raise ValueError("Location establish must begin its actor lineage")
            else:
                if previous is None or transition.values_before != previous.values_after:
                    raise ValueError("Location transition before image must match prior lineage")
                if transition.accepted_at < previous.accepted_at:
                    raise ValueError("Location accepted_at cannot move backwards")
                if privacy_rank[transition.values_after.privacy_class] < privacy_rank[
                    previous.values_after.privacy_class
                ]:
                    raise ValueError("Location lineage privacy cannot weaken")
            if transition.operation == "change":
                assert previous is not None
                identity_changed = (
                    previous.values_after.location_ref
                    != transition.values_after.location_ref
                    or previous.values_after.zone_ref != transition.values_after.zone_ref
                )
                expected_since = (
                    transition.accepted_at
                    if identity_changed
                    else previous.values_after.since
                )
                if (
                    transition.authority_lane != "operator"
                    or not isinstance(
                        transition.cause_authority, DomainOperatorAuthorityBinding
                    )
                    or transition.values_after == previous.values_after
                    or transition.values_after.since != expected_since
                ):
                    raise ValueError("Location change violates authority or chronology")
            if transition.operation == "compensate":
                if (
                    previous is None
                    or transition.compensates_transition_id != previous.transition_id
                    or transition.authority_lane != "compensation"
                    or not isinstance(
                        transition.cause_authority,
                        LocationCompensationCauseAuthority,
                    )
                ):
                    raise ValueError("Location compensation must target the then-latest transition")
                cause = transition.cause_authority
                if (
                    cause.target_transition_id != previous.transition_id
                    or cause.target_entity_revision != previous.entity_revision
                    or cause.target_accepted_event_ref != previous.accepted_event_ref
                    or previous.values_before is None
                    or cause.expected_target_lane not in {None, "operator"}
                ):
                    raise ValueError("Location compensation target binding is not exact")
                target_event = next(
                    (
                        item
                        for item in committed_events
                        if getattr(item, "event_id", None)
                        == cause.target_accepted_event_ref
                    ),
                    None,
                )
                expected_target_type = location_event_for_operation(previous.operation)
                if (
                    target_event is None
                    or getattr(target_event, "event_type", None)
                    != expected_target_type
                    or getattr(target_event, "world_revision", None)
                    != cause.target_accepted_world_revision
                    or getattr(target_event, "payload_hash", None)
                    != cause.target_accepted_payload_hash
                    or getattr(target_event, "logical_time", None)
                    != previous.accepted_at
                ):
                    raise ValueError("Location compensation target committed event is not exact")
                required_privacy = max(
                    (
                        previous.values_before.privacy_class,
                        previous.values_after.privacy_class,
                        cause.correction_basis.privacy_class,
                        cause.correction_rationale.privacy_class,
                    ),
                    key=privacy_rank.__getitem__,
                )
                expected_values = previous.values_before.model_copy(
                    update={"privacy_class": required_privacy}
                )
                if (
                    transition.values_after != expected_values
                    or transition.values_after == previous.values_after
                ):
                    raise ValueError("Location compensation restore is not exact")
                base = previous
                visited: set[str] = set()
                while base.operation == "compensate":
                    if (
                        base.transition_id in visited
                        or base.compensates_transition_id is None
                    ):
                        raise ValueError("Location compensation lineage is invalid or cyclic")
                    visited.add(base.transition_id)
                    match = next(
                        (
                            candidate
                            for candidate in lineage[: expected_revision - 1]
                            if candidate.transition_id == base.compensates_transition_id
                        ),
                        None,
                    )
                    if match is None:
                        raise ValueError("Location compensation lineage is incomplete")
                    base = match
                if base.authority_lane != "operator":
                    raise ValueError("Location compensation effective lane is not operator")
            elif transition.compensates_transition_id is not None:
                raise ValueError("non-compensation Location transition cannot claim a target")
            if isinstance(transition.cause_authority, DomainOperatorAuthorityBinding):
                operator = transition.cause_authority
            else:
                operator = transition.cause_authority.operator_authority
            if operator.required_operation != "v2_location_governance":
                raise ValueError("Location transition lacks its exact operator operation")
            authority = next(
                (
                    item
                    for item in actor_authority_transitions
                    if getattr(item, "authority_id", None) == operator.authority_id
                    and getattr(item, "authority_revision", None)
                    == operator.authority_revision
                ),
                None,
            )
            committed = next(
                (
                    item
                    for item in committed_events
                    if getattr(item, "event_id", None) == operator.authority_event_ref
                ),
                None,
            )
            authority_values = getattr(authority, "values_after", None)
            authority_values_hash = (
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
                or committed is None
                or authority_values_hash != operator.authority_values_hash
                or getattr(authority, "policy_version", None)
                != "actor-authority-policy.2"
                or getattr(authority, "policy_digest", None)
                != operator.authority_policy_digest
                or authority_values.principal_ref != operator.principal_ref
                or authority_values.principal_kind != "deployment_operator"
                or "v2_location_governance" not in authority_values.allowed_operations
                or authority_values.status != "active"
                or authority_values.valid_from > transition.accepted_at
                or (
                    authority_values.expires_at is not None
                    and authority_values.expires_at <= transition.accepted_at
                )
                or getattr(committed, "world_revision", None)
                != operator.authority_world_revision
                or getattr(committed, "payload_hash", None)
                != operator.authority_payload_hash
                or getattr(committed, "event_type", None)
                not in {
                    "ActorAuthorityBootstrapped",
                    "ActorAuthorityRotated",
                    "ActorAuthorityCompensated",
                }
                or getattr(committed, "world_revision", 0)
                >= getattr(accepted_event, "world_revision", 0)
            ):
                raise ValueError("Location history lacks exact operator authority")
            if transition.semantic_fingerprint_after != v2_location_semantic_fingerprint(
                actor_ref=actor_ref,
                values=transition.values_after,
                policy_refs=transition.policy_refs,
            ):
                raise ValueError("Location transition semantic fingerprint is invalid")
            previous = transition
        latest = lineage[-1]
        head = heads_by_actor[actor_ref]
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
            raise ValueError("Location head must exactly match its latest transition")

    if len(proposal_ids) != len(set(proposal_ids)):
        raise ValueError("Location proposal ids must be globally unique")
    actual_ids = tuple(item.proposal_id for item in proposals)
    if actual_ids != proposal_ids:
        raise ValueError("Location proposal ids must exactly index pending proposals")
    if any(proposal_id not in global_proposal_ids for proposal_id in proposal_ids):
        raise ValueError("pending Location proposal is absent from global proposal index")
    from .location_authority_events import V2LocationChangedPayload
    from .location_authority_reducers import (
        V2_LOCATION_POLICY_DIGEST,
        V2_LOCATION_POLICY_REFS,
        V2_LOCATION_POLICY_VERSION,
    )

    for proposal in proposals:
        payload = V2LocationChangedPayload.model_validate_json(
            proposal.proposed_mutation.payload_json
        )
        if (
            payload.operation != proposal.transition_kind
            or payload.proposal_id != proposal.proposal_id
            or payload.change_id != proposal.change_id
            or payload.transition_id != proposal.transition_id
            or payload.evaluated_world_revision != proposal.evaluated_world_revision
            or payload.expected_entity_revision != proposal.expected_entity_revision
            or payload.accepted_change_hash != proposal.proposed_change_hash
            or payload.evidence_refs != proposal.evidence_refs
            or payload.policy_refs != proposal.policy_refs
            or payload.model_dump(mode="json")
            != json.loads(proposal.proposed_mutation.payload_json)
        ):
            raise ValueError("pending Location proposal does not exactly bind its payload")
        current_world_revision = max(
            (int(getattr(item, "world_revision", 0)) for item in committed_events),
            default=0,
        )
        if proposal.evaluated_world_revision > current_world_revision:
            raise ValueError("pending Location proposal evaluates a future world revision")
        if (
            proposal.policy_refs != V2_LOCATION_POLICY_REFS
            or payload.policy_refs != V2_LOCATION_POLICY_REFS
            or payload.policy_version != V2_LOCATION_POLICY_VERSION
            or payload.policy_digest != V2_LOCATION_POLICY_DIGEST
        ):
            raise ValueError("pending Location proposal references an uninstalled policy")
        if payload.selection_mode != "direct" or payload.random_draw_binding is not None:
            raise ValueError("pending Location proposal requires uninstalled RandomAuthority")
        actor_history = tuple(
            item
            for item in transitions
            if item.actor_ref == payload.location_after.actor_ref
        )
        eligible = tuple(
            item
            for item in actor_history
            if (
                event := next(
                    (
                        ref
                        for ref in committed_events
                        if getattr(ref, "event_id", None) == item.accepted_event_ref
                    ),
                    None,
                )
            )
            is not None
            and getattr(event, "world_revision", proposal.evaluated_world_revision + 1)
            <= proposal.evaluated_world_revision
        )
        cutoff_transition = eligible[-1] if eligible else None
        cutoff_head = (
            V2LocationProjection(
                actor_ref=cutoff_transition.actor_ref,
                entity_revision=cutoff_transition.entity_revision,
                semantic_fingerprint=cutoff_transition.semantic_fingerprint_after,
                values=cutoff_transition.values_after,
                origin=V2LocationOrigin(
                    change_id=cutoff_transition.change_id,
                    transition_id=cutoff_transition.transition_id,
                    policy_refs=cutoff_transition.policy_refs,
                    accepted_event_ref=cutoff_transition.accepted_event_ref,
                ),
                updated_at=cutoff_transition.accepted_at,
            )
            if cutoff_transition is not None
            else None
        )
        before = payload.location_before
        if payload.operation == "establish":
            cas_valid = (
                cutoff_head is None
                and before is None
                and payload.expected_entity_revision == 0
                and payload.location_after.entity_revision == 1
                and payload.location_after.values.since
                == payload.location_after.updated_at
            )
        else:
            cas_valid = (
                cutoff_head is not None
                and before == cutoff_head
                and payload.expected_entity_revision == before.entity_revision
                and payload.location_after.entity_revision == before.entity_revision + 1
                and payload.location_after.actor_ref == before.actor_ref
                and payload.location_after.updated_at >= before.updated_at
            )
        if not cas_valid:
            raise ValueError("pending Location proposal has an invalid embedded CAS contract")
        if payload.operation == "change":
            assert before is not None
            identity_changed = (
                before.values.location_ref != payload.location_after.values.location_ref
                or before.values.zone_ref != payload.location_after.values.zone_ref
            )
            expected_since = (
                payload.location_after.updated_at if identity_changed else before.values.since
            )
            if (
                payload.location_after.values == before.values
                or payload.location_after.values.since != expected_since
                or privacy_rank[payload.location_after.values.privacy_class]
                < privacy_rank[before.values.privacy_class]
            ):
                raise ValueError("pending Location change violates chronology or privacy")
        if payload.operation == "compensate":
            assert before is not None
            cause = payload.cause_authority
            if not isinstance(cause, LocationCompensationCauseAuthority):
                raise ValueError("pending Location compensation lacks typed authority")
            target = eligible[-1] if eligible else None
            target_event = next(
                (
                    item
                    for item in committed_events
                    if getattr(item, "event_id", None)
                    == cause.target_accepted_event_ref
                ),
                None,
            )
            if (
                target is None
                or target.transition_id != cause.target_transition_id
                or target.entity_revision != cause.target_entity_revision
                or target.accepted_event_ref != cause.target_accepted_event_ref
                or target.values_before is None
                or target.values_after != before.values
                or target_event is None
                or getattr(target_event, "world_revision", None)
                != cause.target_accepted_world_revision
                or getattr(target_event, "payload_hash", None)
                != cause.target_accepted_payload_hash
                or getattr(target_event, "event_type", None)
                != location_event_for_operation(target.operation)
            ):
                raise ValueError("pending Location compensation target is not exact latest")
            required_privacy = max(
                (
                    target.values_before.privacy_class,
                    before.values.privacy_class,
                    cause.correction_basis.privacy_class,
                    cause.correction_rationale.privacy_class,
                ),
                key=privacy_rank.__getitem__,
            )
            expected_values = target.values_before.model_copy(
                update={"privacy_class": required_privacy}
            )
            if (
                payload.location_after.values != expected_values
                or payload.location_after.values == before.values
            ):
                raise ValueError("pending Location compensation restore is not exact")
        cause = payload.cause_authority
        operator = (
            cause.operator_authority
            if isinstance(cause, LocationCompensationCauseAuthority)
            else cause
        )
        authority = next(
            (
                item
                for item in actor_authority_transitions
                if getattr(item, "authority_id", None) == operator.authority_id
                and getattr(item, "authority_revision", None)
                == operator.authority_revision
            ),
            None,
        )
        committed = next(
            (
                item
                for item in committed_events
                if getattr(item, "event_id", None) == operator.authority_event_ref
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
        at = payload.location_after.updated_at
        if (
            authority is None
            or committed is None
            or values_hash != operator.authority_values_hash
            or getattr(authority, "policy_version", None)
            != "actor-authority-policy.2"
            or getattr(authority, "policy_digest", None)
            != operator.authority_policy_digest
            or values.principal_ref != operator.principal_ref
            or values.principal_kind != "deployment_operator"
            or values.status != "active"
            or "v2_location_governance" not in values.allowed_operations
            or values.valid_from > at
            or (values.expires_at is not None and values.expires_at <= at)
            or getattr(committed, "world_revision", None)
            != operator.authority_world_revision
            or getattr(committed, "payload_hash", None)
            != operator.authority_payload_hash
            or getattr(committed, "world_revision", payload.evaluated_world_revision + 1)
            > payload.evaluated_world_revision
            or getattr(committed, "event_type", None)
            not in {
                "ActorAuthorityBootstrapped",
                "ActorAuthorityRotated",
                "ActorAuthorityCompensated",
            }
        ):
            raise ValueError("pending Location proposal lacks exact operator authority")
