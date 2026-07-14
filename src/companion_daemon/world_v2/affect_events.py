"""Typed event payloads for deterministic Affect transitions.

Appraisal records accepted meaning; these payloads independently record the
accepted numerical Affect change.  Keeping the two mutation families separate
prevents an appraisal reducer from silently inventing an emotional response.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .schemas import (
    AffectCalibrationEpisodeRef,
    AffectComponentProjection,
    AffectEpisodeProjection,
    AppraisalMeaningRef,
    EvidenceRef,
    FrozenModel,
)


class AffectMutationPayload(FrozenModel):
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=0)
    evidence_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = ()
    policy_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def transition_inputs_are_unique(self) -> AffectMutationPayload:
        if len(self.policy_refs) != len(set(self.policy_refs)):
            raise ValueError("affect policy refs must be unique")
        if len(self.evidence_refs) != len({item.ref_id for item in self.evidence_refs}):
            raise ValueError("affect evidence refs must be unique")
        if len(self.appraisal_refs) != len(
            {_meaning_identity(item) for item in self.appraisal_refs}
        ):
            raise ValueError("affect appraisal refs must be unique")
        return self


class AffectAuthorizedMutationPayload(AffectMutationPayload):
    acceptance_id: str = Field(min_length=1)
    proposal_id: str = Field(min_length=1)
    evaluated_world_revision: int = Field(ge=0)
    accepted_change_hash: str = Field(min_length=64, max_length=64)


class AffectEpisodeOpenedPayload(AffectAuthorizedMutationPayload):
    episode: AffectEpisodeProjection

    @model_validator(mode="after")
    def creates_active_revision_one(self) -> AffectEpisodeOpenedPayload:
        _validate_authorized_hash(self)
        if self.expected_entity_revision != 0:
            raise ValueError("AffectEpisodeOpened must create a new entity")
        if self.episode.entity_revision != 1 or self.episode.status != "active":
            raise ValueError("AffectEpisodeOpened requires active entity revision one")
        _validate_episode_origin(self, self.episode)
        if self.evidence_refs != self.episode.evidence_refs:
            raise ValueError("payload evidence must equal affect episode evidence")
        if self.appraisal_refs != _episode_appraisal_refs(self.episode):
            raise ValueError("payload appraisal refs must equal component appraisal refs")
        if not self.appraisal_refs:
            raise ValueError("opening an affect episode requires an appraisal meaning")
        return self


class AffectComponentUpdate(FrozenModel):
    component_id: str = Field(min_length=1)
    operation: Literal["stimulus", "materialize"] = "stimulus"
    before_intensity_bp: int = Field(ge=0, le=10_000)
    proposed_delta_bp: int = Field(ge=-10_000, le=10_000)
    accepted_delta_bp: int = Field(ge=-10_000, le=10_000)
    after_intensity_bp: int = Field(ge=0, le=10_000)
    appraisal_refs: tuple[AppraisalMeaningRef, ...] = ()
    updated_component: AffectComponentProjection

    @model_validator(mode="after")
    def delta_matches_updated_component(self) -> AffectComponentUpdate:
        if self.operation == "stimulus" and not self.appraisal_refs:
            raise ValueError("stimulus affect update requires appraisal refs")
        if self.operation == "materialize" and (
            self.appraisal_refs or self.proposed_delta_bp != 0 or self.accepted_delta_bp != 0
        ):
            raise ValueError("materialization cannot introduce stimulus or delta")
        expected = min(10_000, max(0, self.before_intensity_bp + self.accepted_delta_bp))
        if self.after_intensity_bp != expected:
            raise ValueError("affect after intensity does not match accepted delta")
        if self.updated_component.component_id != self.component_id:
            raise ValueError("updated affect component identity changed")
        if self.updated_component.intensity_bp != self.after_intensity_bp:
            raise ValueError("updated component must carry the accepted intensity")
        if (
            self.operation == "stimulus"
            and self.updated_component.decay_anchor_intensity_bp != self.after_intensity_bp
        ):
            raise ValueError("stimulus update must anchor the accepted intensity")
        if any(
            item.source_cluster_ref != self.updated_component.source_cluster_ref
            for item in self.appraisal_refs
        ):
            raise ValueError("updated component appraisal source cluster changed")
        return self


class AffectEpisodeUpdatedPayload(AffectAuthorizedMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    episode_id: str = Field(min_length=1)
    updated_at: datetime
    component_updates: tuple[AffectComponentUpdate, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def update_is_complete_and_authorized(self) -> AffectEpisodeUpdatedPayload:
        _validate_authorized_hash(self)
        _require_aware(self.updated_at, "updated_at")
        identities = [item.component_id for item in self.component_updates]
        if len(identities) != len(set(identities)):
            raise ValueError("affect update repeats a component identity")
        if self.appraisal_refs != _update_appraisal_refs(self.component_updates):
            raise ValueError("payload appraisal refs must equal update appraisal refs")
        for update in self.component_updates:
            component = update.updated_component
            if update.operation == "stimulus" and not (
                component.decay_anchor_at
                == component.last_stimulus_at
                == component.last_updated_at
                == self.updated_at
            ):
                raise ValueError("updated component times must equal updated_at")
            if update.operation == "materialize" and component.last_updated_at != self.updated_at:
                raise ValueError("materialized component time must equal updated_at")
        return self


class AffectComponentDecay(FrozenModel):
    component_id: str = Field(min_length=1)
    before_intensity_bp: int = Field(ge=0, le=10_000)
    after_intensity_bp: int = Field(ge=0, le=10_000)
    config_version: str = Field(min_length=1)
    table_digest: str = Field(min_length=64, max_length=64)
    config_digest: str = Field(min_length=64, max_length=64)


class AffectEpisodeDecayedPayload(AffectMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    episode_id: str = Field(min_length=1)
    from_logical_time: datetime
    to_logical_time: datetime
    component_results: tuple[AffectComponentDecay, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def decay_window_moves_forward(self) -> AffectEpisodeDecayedPayload:
        _require_aware(self.from_logical_time, "from_logical_time")
        _require_aware(self.to_logical_time, "to_logical_time")
        if self.to_logical_time <= self.from_logical_time:
            raise ValueError("affect decay logical time must move forwards")
        if self.appraisal_refs:
            raise ValueError("mechanical affect decay cannot introduce appraisals")
        if not all(
            item.evidence_type == "clock_observation" and item.claim_purpose == "current_fact"
            for item in self.evidence_refs
        ):
            raise ValueError("affect decay requires authoritative clock evidence")
        identities = [item.component_id for item in self.component_results]
        if len(identities) != len(set(identities)):
            raise ValueError("affect decay repeats a component identity")
        return self


class AffectEpisodeResolvedPayload(AffectAuthorizedMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    episode_id: str = Field(min_length=1)
    resolved_at: datetime
    resolution_refs: tuple[EvidenceRef, ...] = Field(min_length=1)
    reason_code: str = Field(min_length=1)

    @model_validator(mode="after")
    def resolution_is_explicit_and_sourced(self) -> AffectEpisodeResolvedPayload:
        _validate_authorized_hash(self)
        _require_aware(self.resolved_at, "resolved_at")
        if self.evidence_refs != self.resolution_refs:
            raise ValueError("resolution refs must equal transition evidence")
        return self


class AffectEpisodeSupersededPayload(AffectAuthorizedMutationPayload):
    expected_entity_revision: int = Field(ge=1)
    episode_id: str = Field(min_length=1)
    superseded_at: datetime
    successor: AffectEpisodeProjection

    @model_validator(mode="after")
    def successor_is_new_linked_and_sourced(self) -> AffectEpisodeSupersededPayload:
        _validate_authorized_hash(self)
        _require_aware(self.superseded_at, "superseded_at")
        if self.successor.episode_id == self.episode_id:
            raise ValueError("successor affect episode requires a new identity")
        if self.successor.entity_revision != 1 or self.successor.status != "active":
            raise ValueError("successor affect episode must be active revision one")
        if self.successor.supersedes_episode_id != self.episode_id:
            raise ValueError("successor affect episode must link its predecessor")
        _validate_episode_origin(self, self.successor)
        if self.evidence_refs != self.successor.evidence_refs:
            raise ValueError("payload evidence must equal successor evidence")
        if self.appraisal_refs != _episode_appraisal_refs(self.successor):
            raise ValueError("payload appraisal refs must equal successor appraisal refs")
        return self


class AffectBaselineAdjustedPayload(AffectAuthorizedMutationPayload):
    dimension: Literal[
        "hurt",
        "anger",
        "sadness",
        "loneliness",
        "anxiety",
        "resentment",
        "warmth",
        "joy",
    ]
    baseline_before_bp: int = Field(ge=0, le=10_000)
    proposed_delta_bp: int = Field(ge=-10_000, le=10_000)
    accepted_delta_bp: int = Field(ge=-10_000, le=10_000)
    baseline_after_bp: int = Field(ge=0, le=10_000)
    calibration_policy_version: str = Field(min_length=1)
    calibration_window_from: datetime
    calibration_window_to: datetime
    basis_episode_refs: tuple[AffectCalibrationEpisodeRef, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def baseline_change_is_explicit_and_long_term(self) -> AffectBaselineAdjustedPayload:
        _validate_authorized_hash(self)
        expected = self.baseline_before_bp + self.accepted_delta_bp
        if not 0 <= expected <= 10_000:
            raise ValueError("affect baseline delta exceeds the valid range")
        if self.baseline_after_bp != expected or self.baseline_after_bp == self.baseline_before_bp:
            raise ValueError("affect baseline does not match accepted delta")
        if self.appraisal_refs:
            raise ValueError("baseline calibration cannot use single-appraisal causation")
        _require_aware(self.calibration_window_from, "calibration_window_from")
        _require_aware(self.calibration_window_to, "calibration_window_to")
        if self.calibration_window_to <= self.calibration_window_from:
            raise ValueError("baseline calibration window must move forwards")
        if self.accepted_delta_bp == 0:
            raise ValueError("baseline calibration cannot be a no-op")
        if (
            self.proposed_delta_bp == 0
            or (self.accepted_delta_bp > 0) != (self.proposed_delta_bp > 0)
            or abs(self.accepted_delta_bp) > abs(self.proposed_delta_bp)
        ):
            raise ValueError("accepted baseline delta must refine the proposal direction")
        identities = {(item.episode_id, item.component_id) for item in self.basis_episode_refs}
        if len(identities) != len(self.basis_episode_refs):
            raise ValueError("baseline calibration basis refs must be unique")
        return self


AFFECT_PAYLOAD_MODELS = {
    "AffectEpisodeOpened": AffectEpisodeOpenedPayload,
    "AffectEpisodeUpdated": AffectEpisodeUpdatedPayload,
    "AffectEpisodeDecayed": AffectEpisodeDecayedPayload,
    "AffectEpisodeResolved": AffectEpisodeResolvedPayload,
    "AffectEpisodeSuperseded": AffectEpisodeSupersededPayload,
    "AffectBaselineAdjusted": AffectBaselineAdjustedPayload,
}


def affect_mutation_hash(
    payload: AffectAuthorizedMutationPayload | Mapping[str, Any],
) -> str:
    """Hash the complete proposed mutation without its later authorization IDs."""

    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, AffectAuthorizedMutationPayload)
        else to_jsonable_python(dict(payload))
    )
    for field in ("acceptance_id", "proposal_id", "accepted_change_hash"):
        material.pop(field, None)
    encoded = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_authorized_hash(payload: AffectAuthorizedMutationPayload) -> None:
    if payload.accepted_change_hash != affect_mutation_hash(payload):
        raise ValueError("accepted change hash does not match affect transition")


def _validate_episode_origin(
    payload: AffectAuthorizedMutationPayload, episode: AffectEpisodeProjection
) -> None:
    if (
        episode.origin.change_id != payload.change_id
        or episode.origin.transition_id != payload.transition_id
        or episode.origin.policy_refs != payload.policy_refs
    ):
        raise ValueError("affect episode origin does not match its transition")


def _episode_appraisal_refs(
    episode: AffectEpisodeProjection,
) -> tuple[AppraisalMeaningRef, ...]:
    return _unique_meaning_refs(
        ref for component in episode.components for ref in component.appraisal_refs
    )


def _update_appraisal_refs(
    updates: tuple[AffectComponentUpdate, ...],
) -> tuple[AppraisalMeaningRef, ...]:
    return _unique_meaning_refs(ref for update in updates for ref in update.appraisal_refs)


def _unique_meaning_refs(values) -> tuple[AppraisalMeaningRef, ...]:
    result: list[AppraisalMeaningRef] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for value in values:
        identity = _meaning_identity(value)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return tuple(result)


def _meaning_identity(value: AppraisalMeaningRef) -> tuple[str, str, str, str, str]:
    return (
        value.appraisal_id,
        value.hypothesis_id,
        value.source_cluster_ref,
        value.accepted_change_id,
        value.accepted_transition_id,
    )


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
