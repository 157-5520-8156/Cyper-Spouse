"""Frozen P3 relationship context for private-media qualification.

This is intentionally a *read-only* domain seam.  It does not open a media
candidate, select a lane, or authorize generation.  Given a pinned projection
it can only expose the relationship stage for the intended recipient and one
currently-positive ``VisiblePhysicalStateProjection``.  More expressive bases
(conversation turns, rituals, transitions, or recipient displays) have no
source-bound World v2 authority yet and therefore fail closed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from .schema_core import FrozenModel, PrivacyClass, canonicalize_json_value
from .visible_physical_state import visible_physical_state_at


RelationshipStageV1 = Literal[
    "stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"
]
PrivateExpressionBasisKindV1 = Literal["embodied_state"]
PrivateExpressionChargeV1 = Literal["subtle", "charged", "veiled"]

_EMBODIED_STATE_POINTER = "/character/visible_physical_state"


def _canonical_digest(value: object) -> str:
    payload = json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AudienceContextV1(FrozenModel):
    """The narrow relationship fact an image planner may eventually receive."""

    schema_version: Literal["relationship-media-audience-v1"] = "relationship-media-audience-v1"
    relationship_id: str = Field(min_length=1, max_length=256)
    relationship_revision: int = Field(ge=1)
    recipient_ref: str = Field(min_length=1, max_length=256)
    character_ref: str = Field(min_length=1, max_length=256)
    relationship_stage: RelationshipStageV1
    relationship_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    relationship_origin_event_ref: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def recipient_is_not_the_character(self) -> "AudienceContextV1":
        if self.recipient_ref == self.character_ref:
            raise ValueError("relationship media recipient must not equal character subject")
        return self


class PrivateExpressionBasisV1(FrozenModel):
    """One source-bound, positive embodied-state basis.

    ``evidence_ref`` is deliberately a fixed pointer instead of a caller
    supplied path.  This prevents the basis from being reinterpreted as an
    arbitrary relationship fact before those facts have their own authority.
    """

    schema_version: Literal["private-expression-basis-v1"] = "private-expression-basis-v1"
    basis_id: str = Field(min_length=1, max_length=256)
    basis_revision: int = Field(ge=1)
    kind: PrivateExpressionBasisKindV1 = "embodied_state"
    required_charge: PrivateExpressionChargeV1 = "subtle"
    subject_ref: str = Field(min_length=1, max_length=256)
    recipient_ref: str = Field(min_length=1, max_length=256)
    evidence_ref: Literal["/character/visible_physical_state"] = _EMBODIED_STATE_POINTER
    physical_state_id: str = Field(min_length=1, max_length=256)
    physical_state_revision: int = Field(ge=1)
    source_event_ref: str = Field(min_length=1, max_length=512)
    source_event_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_visibility: PrivacyClass
    valid_until: datetime
    basis_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def basis_is_recipient_specific_and_hashed(self) -> "PrivateExpressionBasisV1":
        if self.subject_ref == self.recipient_ref:
            raise ValueError("private expression basis recipient must not equal subject")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"basis_digest"}))
        if self.basis_digest != expected:
            raise ValueError("private expression basis digest does not bind its contents")
        return self


class RelationshipMediaContextV1(FrozenModel):
    """Complete P3 context slice, frozen independently of any image prompt."""

    schema_version: Literal["relationship-media-context-v1"] = "relationship-media-context-v1"
    audience: AudienceContextV1
    private_expression_basis: PrivateExpressionBasisV1
    resolved_at: datetime
    expires_at: datetime
    authority_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def context_is_consistent_and_hashed(self) -> "RelationshipMediaContextV1":
        if self.audience.character_ref != self.private_expression_basis.subject_ref:
            raise ValueError("relationship context character subject does not match basis")
        if self.audience.recipient_ref != self.private_expression_basis.recipient_ref:
            raise ValueError("relationship context recipient does not match basis")
        if self.expires_at != self.private_expression_basis.valid_until:
            raise ValueError("relationship context expiry must equal its basis validity")
        if self.expires_at <= self.resolved_at:
            raise ValueError("relationship context must expire after resolution")
        expected = _canonical_digest(self.model_dump(mode="json", exclude={"authority_digest"}))
        if self.authority_digest != expected:
            raise ValueError("relationship media context digest does not bind its contents")
        return self


@dataclass(frozen=True)
class RelationshipMediaContextResolution:
    """Fail-closed result of resolving a P3 context from a pinned projection."""

    context: RelationshipMediaContextV1 | None
    reason_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.context is not None


class RelationshipMediaContextResolver:
    """Compile the currently-supported, source-bound P3 context.

    The resolver accepts an object with ``relationship_states`` and
    ``visible_physical_states`` attributes so it can operate on either a full
    projection or a pinned projection slice without importing ``schemas``.
    """

    def resolve(
        self,
        *,
        projection: object,
        character_ref: str,
        recipient_ref: str,
        at_logical_time: datetime,
        basis_kind: str = "embodied_state",
        required_charge: PrivateExpressionChargeV1 = "subtle",
    ) -> RelationshipMediaContextResolution:
        if basis_kind != "embodied_state":
            return RelationshipMediaContextResolution(None, "unsupported_private_expression_basis")
        if character_ref == recipient_ref:
            return RelationshipMediaContextResolution(None, "recipient_character_subject_mismatch")
        if at_logical_time.tzinfo is None or at_logical_time.utcoffset() is None:
            return RelationshipMediaContextResolution(None, "logical_time_not_timezone_aware")

        states = tuple(getattr(projection, "relationship_states", ()))
        recipient_states = tuple(state for state in states if getattr(state, "subject_ref", None) == recipient_ref)
        if not recipient_states:
            reason = "relationship_recipient_mismatch" if states else "relationship_not_found"
            return RelationshipMediaContextResolution(None, reason)
        if len(recipient_states) != 1:
            return RelationshipMediaContextResolution(None, "relationship_ambiguous")
        relationship = recipient_states[0]

        physical_states = tuple(getattr(projection, "visible_physical_states", ()))
        active = visible_physical_state_at(
            physical_states, subject_ref=character_ref, at_logical_time=at_logical_time
        )
        if active is None:
            mismatched = bool(physical_states)
            return RelationshipMediaContextResolution(
                None,
                "visible_physical_subject_mismatch" if mismatched else "visible_physical_state_missing",
            )
        if not active.has_positive_cues:
            return RelationshipMediaContextResolution(None, "visible_physical_negative_only")
        if active.visibility == "withhold":
            return RelationshipMediaContextResolution(None, "visible_physical_visibility_withheld")

        audience = AudienceContextV1(
            relationship_id=relationship.relationship_id,
            relationship_revision=relationship.entity_revision,
            recipient_ref=recipient_ref,
            character_ref=character_ref,
            relationship_stage=relationship.stage,
            relationship_policy_digest=relationship.policy_digest,
            relationship_origin_event_ref=(
                relationship.origin.accepted_event_ref if relationship.origin is not None else None
            ),
        )
        basis_body = {
            "schema_version": "private-expression-basis-v1",
            "basis_id": f"basis:embodied:{active.physical_state_id}:{active.entity_revision}",
            "basis_revision": active.entity_revision,
            "kind": "embodied_state",
            "required_charge": required_charge,
            "subject_ref": character_ref,
            "recipient_ref": recipient_ref,
            "evidence_ref": _EMBODIED_STATE_POINTER,
            "physical_state_id": active.physical_state_id,
            "physical_state_revision": active.entity_revision,
            "source_event_ref": active.source_event_ref,
            "source_event_payload_hash": active.source_event_payload_hash,
            "source_visibility": active.visibility,
            "valid_until": active.valid_until,
        }
        basis = PrivateExpressionBasisV1(
            **basis_body,
            basis_digest=_canonical_digest(basis_body),
        )
        context_body = {
            "schema_version": "relationship-media-context-v1",
            "audience": audience.model_dump(mode="json"),
            "private_expression_basis": basis.model_dump(mode="json"),
            "resolved_at": at_logical_time,
            "expires_at": active.valid_until,
        }
        return RelationshipMediaContextResolution(
            RelationshipMediaContextV1(
                audience=audience,
                private_expression_basis=basis,
                resolved_at=at_logical_time,
                expires_at=active.valid_until,
                authority_digest=_canonical_digest(context_body),
            )
        )


__all__ = [
    "AudienceContextV1",
    "PrivateExpressionBasisV1",
    "RelationshipMediaContextResolution",
    "RelationshipMediaContextResolver",
    "RelationshipMediaContextV1",
]
