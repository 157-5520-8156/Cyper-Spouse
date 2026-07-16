"""Typed proposal and mechanical event contracts for `.16` GoalAuthority."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_core import to_jsonable_python

from .goal_authority_contract import V2_GOAL_EVENT_TYPES, V2GoalOperation
from .goal_situation_schemas import (
    CompensationCauseAuthority,
    ClockCauseAuthority,
    CommittedEvidenceBasis,
    DeliberativeCauseAuthority,
    DomainOperatorAuthorityBinding,
    GoalExpiryCorrectionBasis,
    RandomDrawBinding,
    SettledEventCauseAuthority,
    V16AuthorizedMutationEnvelope,
    V16CauseAuthority,
    V16AuthorityLane,
    V2GoalCompletionEvidence,
    V2GoalBlockerResolution,
    V2GoalFactCompletionEvidence,
    V2GoalLifecycleReason,
    V2GoalProgressAssessment,
    V2GoalTerminalReason,
    V2GoalProjection,
)
from .schema_core import EvidenceRef, FrozenModel, canonicalize_json_value


class V2GoalChangedPayload(V16AuthorizedMutationEnvelope):
    operation: V2GoalOperation
    authority_lane: V16AuthorityLane
    selection_mode: Literal["direct", "random_draw"]
    goal_before: V2GoalProjection | None = None
    goal_after: V2GoalProjection
    cause_authority: V16CauseAuthority
    revise_kind: Literal["reprioritize", "reschedule", "recontract"] | None = None
    progress_delta_bp: int | None = None
    progress_assessment: V2GoalProgressAssessment | None = None
    lifecycle_reason: V2GoalLifecycleReason | None = None
    blocker_resolutions: tuple[V2GoalBlockerResolution, ...] = ()
    completion_evidence: V2GoalCompletionEvidence | None = None
    terminal_reason: V2GoalTerminalReason | None = None
    removed_blocker_fingerprints: tuple[str, ...] = ()
    random_draw_binding: RandomDrawBinding | None = None
    compensation_target: CompensationCauseAuthority | None = None
    policy_version: str
    policy_digest: str

    @model_validator(mode="after")
    def mutation_is_canonical(self) -> V2GoalChangedPayload:
        if self.accepted_change_hash != v2_goal_mutation_hash(self):
            raise ValueError("goal accepted change hash is invalid")
        after = self.goal_after
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
        ):
            raise ValueError("goal after origin does not match mutation authority")
        if self.operation == "open":
            if self.goal_before is not None or self.expected_entity_revision != 0:
                raise ValueError("goal open must create from revision zero")
        elif self.goal_before is None or self.expected_entity_revision < 1:
            raise ValueError("goal transition requires a before image")
        allowed_lanes = {
            "open": {"deliberative", "operator"},
            "revise": {"deliberative", "operator"},
            "progress": {"deliberative"},
            "pause": {"deliberative"},
            "resume": {"deliberative"},
            "block": {"deliberative"},
            "unblock": {"deliberative"},
            # Settlement is evidence, not a Goal write authority.  A
            # completion is either an accepted deliberative recognition of
            # that evidence, or an operator re-authorised strict completion.
            # Keeping the settlement wire shape in the shared cause union
            # must not accidentally install a direct Goal mutation lane.
            "complete": {"deliberative", "operator"},
            "abandon": {"deliberative"},
            "compensate": {"compensation"},
        }
        if self.authority_lane not in allowed_lanes[self.operation]:
            raise ValueError("goal operation is not allowed in authority lane")
        expected_cause = {
            "deliberative": DeliberativeCauseAuthority,
            "operator": DomainOperatorAuthorityBinding,
            "settlement": SettledEventCauseAuthority,
            "clock_runtime": ClockCauseAuthority,
            "compensation": CompensationCauseAuthority,
        }[self.authority_lane]
        if not isinstance(self.cause_authority, expected_cause):
            raise ValueError("goal cause kind does not match authority lane")
        if self.selection_mode == "random_draw":
            if self.random_draw_binding is None:
                raise ValueError("random goal selection requires exact draw binding")
            if self.authority_lane != "deliberative":
                raise ValueError("only deliberative Goal selection may use a random draw")
        elif self.random_draw_binding is not None:
            raise ValueError("direct goal selection cannot claim a random draw")
        if self.operation == "revise" and self.revise_kind is None:
            raise ValueError("goal revision requires an explicit revise kind")
        if self.operation != "revise" and self.revise_kind is not None:
            raise ValueError("non-revision goal mutation cannot claim revise kind")
        if self.operation == "progress":
            if self.progress_delta_bp is None or self.progress_delta_bp <= 0:
                raise ValueError("goal progress requires a positive exact delta")
            if self.progress_assessment is None:
                raise ValueError("goal progress requires a typed subjective assessment")
            cause = self.cause_authority
            basis = self.progress_assessment.basis
            if isinstance(cause, DeliberativeCauseAuthority) and cause.basis != basis:
                raise ValueError("goal progress cause and assessment basis must be identical")
            if self.progress_assessment.contribution_class == "operator_correction":
                raise ValueError("deliberation cannot claim operator progress correction")
            allowed_progress_sources = {
                "settled_world_event",
                "fact",
                "experience",
            }
            if any(
                item.source_kind not in allowed_progress_sources
                for item in basis.sources
            ):
                raise ValueError("goal progress basis kind is not capable of progress")
        elif self.progress_delta_bp is not None or self.progress_assessment is not None:
            raise ValueError("non-progress goal mutation cannot claim progress assessment")
        reason_kinds = {
            "pause": {
                "priority_shift",
                "resource_constraint",
                "uncertainty",
                "relationship_consideration",
                "context_changed",
            },
            "resume": {
                "priority_restored",
                "constraint_resolved",
                "renewed_intent",
                "context_changed",
            },
            "abandon": {
                "no_longer_desired",
                "superseded",
                "infeasible",
                "values_changed",
                "context_changed",
            },
        }
        if self.operation in reason_kinds:
            if self.lifecycle_reason is None or (
                self.lifecycle_reason.reason_kind not in reason_kinds[self.operation]
            ):
                raise ValueError("goal lifecycle transition requires an allowed typed reason")
            if self.lifecycle_reason.reason_kind == "operator_correction":
                raise ValueError("deliberation cannot claim operator correction")
            cause = self.cause_authority
            if not isinstance(cause, DeliberativeCauseAuthority) or (
                self.lifecycle_reason.basis != cause.basis
            ):
                raise ValueError("goal lifecycle reason basis must match deliberation")
        elif self.lifecycle_reason is not None:
            raise ValueError("goal operation cannot claim a lifecycle reason")
        if self.operation in {"block", "unblock"}:
            identities = tuple(item.blocker_id for item in self.blocker_resolutions)
            if identities != tuple(sorted(set(identities))) or (
                self.operation == "unblock" and not identities
            ):
                raise ValueError("goal blocker resolutions must be canonical and non-empty")
        elif self.blocker_resolutions:
            raise ValueError("non-unblock goal mutation cannot claim blocker resolutions")
        if self.operation == "complete":
            if self.completion_evidence is None:
                raise ValueError("goal completion requires typed completion evidence")
            if isinstance(self.cause_authority, DeliberativeCauseAuthority):
                basis = self.cause_authority.basis
                if not isinstance(basis, CommittedEvidenceBasis) or not any(
                    item.event_ref == self.completion_evidence.evidence_ref
                    and item.world_revision
                    == self.completion_evidence.evidence_world_revision
                    and item.payload_hash
                    == self.completion_evidence.evidence_payload_hash
                    for item in basis.sources
                ):
                    raise ValueError("goal recognition basis must include completion evidence")
        elif self.completion_evidence is not None:
            raise ValueError("non-completion mutation cannot claim completion evidence")
        if self.operation in {"complete", "abandon"}:
            if (
                self.terminal_reason is None
                or self.goal_after.values.terminal_reason != self.terminal_reason
            ):
                raise ValueError("terminal Goal mutation requires one exact structured reason")
        elif self.terminal_reason is not None:
            raise ValueError("non-terminal Goal mutation cannot claim terminal reason")
        if self.operation == "complete":
            if self.removed_blocker_fingerprints != tuple(
                sorted(set(self.removed_blocker_fingerprints))
            ):
                raise ValueError("removed blocker fingerprints must be canonical")
        elif self.removed_blocker_fingerprints:
            raise ValueError("only Goal completion may clear blockers objectively")
        if self.operation == "compensate":
            if self.compensation_target is None or (
                self.compensation_target != self.cause_authority
            ):
                raise ValueError("goal compensation requires one exact target authority")
        elif self.compensation_target is not None:
            raise ValueError("ordinary goal mutation cannot name compensation target")
        if self.evidence_refs != v2_goal_evidence_refs(self):
            raise ValueError("goal EvidenceRefs are not exact cause authority")
        return self


V2_GOAL_PAYLOAD_MODELS = {
    event_type: V2GoalChangedPayload for event_type in V2_GOAL_EVENT_TYPES
}


def v2_goal_expiry_hash(value: object) -> str:
    if hasattr(value, "model_dump"):
        material = value.model_dump(mode="json")  # type: ignore[attr-defined]
    else:
        material = to_jsonable_python(dict(value))  # type: ignore[arg-type]
        material.setdefault("operation", "expire")
        material.setdefault("authority_lane", "clock_runtime")
        material.setdefault("removed_blocker_fingerprints", [])
    material.pop("mechanical_change_hash", None)
    return hashlib.sha256(
        json.dumps(
            canonicalize_json_value(material),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def v2_goal_expiry_id(
    *,
    world_id: str,
    goal_id: str,
    expected_entity_revision: int,
    clock_event_ref: str,
    policy_digest: str,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "world_id": world_id,
                "goal_id": goal_id,
                "expected_entity_revision": expected_entity_revision,
                "clock_event_ref": clock_event_ref,
                "policy_digest": policy_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return f"v2-goal-expiry:{digest}"


class V2GoalExpiredPayload(FrozenModel):
    operation: Literal["expire"] = "expire"
    authority_lane: Literal["clock_runtime"] = "clock_runtime"
    world_id: str = Field(min_length=1)
    expiry_id: str = Field(min_length=1)
    change_id: str = Field(min_length=1)
    transition_id: str = Field(min_length=1)
    expected_entity_revision: int = Field(ge=1)
    evaluated_world_revision: int = Field(ge=1)
    policy_refs: tuple[str, ...] = Field(min_length=1)
    goal_before: V2GoalProjection
    goal_after: V2GoalProjection
    cause_authority: ClockCauseAuthority
    terminal_reason: V2GoalTerminalReason
    removed_blocker_fingerprints: tuple[str, ...] = ()
    policy_version: str = Field(min_length=1)
    policy_digest: str = Field(min_length=64, max_length=64)
    mechanical_change_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def mechanical_expiry_is_exact(self) -> V2GoalExpiredPayload:
        after = self.goal_after
        expected_expiry_id = v2_goal_expiry_id(
            world_id=self.world_id,
            goal_id=after.goal_id,
            expected_entity_revision=self.expected_entity_revision,
            clock_event_ref=self.cause_authority.clock_event_ref,
            policy_digest=self.policy_digest,
        )
        if self.expiry_id != expected_expiry_id:
            raise ValueError("goal expiry id is not deterministically derived")
        if self.removed_blocker_fingerprints != tuple(
            sorted(set(self.removed_blocker_fingerprints))
        ):
            raise ValueError("goal expiry removed blockers must be canonical")
        if (
            after.origin.change_id != self.change_id
            or after.origin.transition_id != self.transition_id
            or after.origin.policy_refs != self.policy_refs
            or self.goal_before.goal_id != after.goal_id
            or self.goal_before.actor_ref != after.actor_ref
            or self.expected_entity_revision != self.goal_before.entity_revision
            or after.entity_revision != self.goal_before.entity_revision + 1
            or after.values.terminal_reason != self.terminal_reason
        ):
            raise ValueError("goal expiry envelope does not match its exact images")
        if self.mechanical_change_hash != v2_goal_expiry_hash(self):
            raise ValueError("goal expiry mechanical change hash is invalid")
        return self


V2_GOAL_MECHANICAL_PAYLOAD_MODELS = {"V2GoalExpired": V2GoalExpiredPayload}


def v2_goal_evidence_refs(
    payload: V2GoalChangedPayload | Mapping[str, Any],
) -> tuple[EvidenceRef, ...]:
    value = (
        payload
        if isinstance(payload, V2GoalChangedPayload)
        else V2GoalChangedPayload.model_construct(**dict(payload))
    )
    cause = value.cause_authority
    if isinstance(cause, DeliberativeCauseAuthority):
        refs: list[EvidenceRef] = []
        if isinstance(cause.basis, CommittedEvidenceBasis):
            for item in cause.basis.sources:
                evidence_type = {
                    "settled_world_event": "settled_world_event",
                    "fact": "committed_fact",
                    "experience": "committed_experience",
                }.get(item.source_kind, "committed_world_event")
                refs.append(
                    EvidenceRef(
                        ref_id=item.event_ref,
                        evidence_type=evidence_type,
                        claim_purpose="future_plan",
                        source_world_revision=item.world_revision,
                        immutable_hash=item.payload_hash,
                    )
                )
        if value.random_draw_binding is not None:
            draw = value.random_draw_binding
            refs.append(
                EvidenceRef(
                    ref_id=draw.draw_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="action_authorization",
                    source_world_revision=draw.draw_world_revision,
                    immutable_hash=draw.draw_payload_hash,
                )
            )
        if value.progress_assessment is not None and not isinstance(
            cause.basis, CommittedEvidenceBasis
        ):
            raise ValueError("goal progress assessment basis is not authorized")
        supersedes = value.goal_after.values.supersedes_goal_authority
        if supersedes is not None:
            refs.append(
                EvidenceRef(
                    ref_id=supersedes.accepted_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="future_plan",
                    source_world_revision=supersedes.accepted_world_revision,
                    immutable_hash=supersedes.accepted_payload_hash,
                )
            )
        if value.completion_evidence is not None:
            completion_ref = _completion_evidence_ref(value.completion_evidence)
            if completion_ref not in refs:
                refs.append(completion_ref)
        for resolution in value.blocker_resolutions:
            for ref in _deliberative_basis_evidence_refs(resolution.basis):
                if ref not in refs:
                    refs.append(ref)
        before_ids = (
            {item.blocker_id for item in value.goal_before.values.blockers}
            if value.goal_before is not None
            else set()
        )
        for blocker in value.goal_after.values.blockers:
            if blocker.blocker_id not in before_ids:
                refs.extend(_deliberative_basis_evidence_refs(blocker.basis))
        return _canonical_evidence_refs(refs)
    if isinstance(cause, DomainOperatorAuthorityBinding):
        refs = [
            EvidenceRef(
                ref_id=cause.authority_event_ref,
                evidence_type="committed_world_event",
                claim_purpose="action_authorization",
                source_world_revision=cause.authority_world_revision,
                immutable_hash=cause.authority_payload_hash,
            )
        ]
        if value.progress_assessment is not None:
            refs.extend(_basis_evidence_refs(value.progress_assessment.basis))
        supersedes = value.goal_after.values.supersedes_goal_authority
        if supersedes is not None:
            refs.append(_supersedes_evidence_ref(supersedes))
        if value.completion_evidence is not None:
            refs.append(_completion_evidence_ref(value.completion_evidence))
        return _canonical_evidence_refs(refs)
    if isinstance(cause, SettledEventCauseAuthority):
        refs = [
            EvidenceRef(
                ref_id=cause.event_ref,
                evidence_type="settled_world_event",
                claim_purpose="future_plan",
                source_world_revision=cause.world_revision,
                immutable_hash=cause.payload_hash,
            )
        ]
        if value.completion_evidence is not None:
            completion_ref = _completion_evidence_ref(value.completion_evidence)
            if completion_ref not in refs:
                refs.append(completion_ref)
        return _canonical_evidence_refs(refs)
    if isinstance(cause, ClockCauseAuthority):
        return (
            EvidenceRef(
                ref_id=cause.clock_event_ref,
                evidence_type="committed_world_event",
                claim_purpose="future_plan",
                source_world_revision=cause.clock_world_revision,
                immutable_hash=cause.clock_payload_hash,
            ),
        )
    if isinstance(cause, CompensationCauseAuthority):
        refs = list(_deliberative_basis_evidence_refs(cause.correction_basis))
        if isinstance(cause.correction_basis, GoalExpiryCorrectionBasis):
            original_clock = cause.correction_basis.original_clock
            refs.append(
                EvidenceRef(
                    ref_id=original_clock.clock_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="future_plan",
                    source_world_revision=original_clock.clock_world_revision,
                    immutable_hash=original_clock.clock_payload_hash,
                )
            )
        refs.append(
            EvidenceRef(
                ref_id=cause.target_accepted_event_ref,
                evidence_type="committed_world_event",
                claim_purpose="conversation_continuity",
                source_world_revision=cause.target_accepted_world_revision,
                immutable_hash=cause.target_accepted_payload_hash,
            )
        )
        if cause.operator_authority is not None:
            refs.append(
                EvidenceRef(
                    ref_id=cause.operator_authority.authority_event_ref,
                    evidence_type="committed_world_event",
                    claim_purpose="action_authorization",
                    source_world_revision=(
                        cause.operator_authority.authority_world_revision
                    ),
                    immutable_hash=cause.operator_authority.authority_payload_hash,
                )
            )
        return _canonical_evidence_refs(refs)
    raise TypeError("clock authority is not a typed Goal proposal cause")


def _basis_evidence_refs(basis: CommittedEvidenceBasis) -> tuple[EvidenceRef, ...]:
    refs = []
    for item in basis.sources:
        evidence_type = {
            "settled_world_event": "settled_world_event",
            "fact": "committed_fact",
            "experience": "committed_experience",
        }.get(item.source_kind, "committed_world_event")
        refs.append(
            EvidenceRef(
                ref_id=item.event_ref,
                evidence_type=evidence_type,
                claim_purpose="future_plan",
                source_world_revision=item.world_revision,
                immutable_hash=item.payload_hash,
            )
        )
    return _canonical_evidence_refs(refs)


def _canonical_evidence_refs(refs: list[EvidenceRef] | tuple[EvidenceRef, ...]) -> tuple[EvidenceRef, ...]:
    unique = {item.model_dump_json(): item for item in refs}
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.evidence_type,
                item.ref_id,
                item.claim_purpose,
                item.source_world_revision or -1,
                item.immutable_hash or "",
            ),
        )
    )


def _deliberative_basis_evidence_refs(
    basis: object,
) -> tuple[EvidenceRef, ...]:
    if isinstance(basis, GoalExpiryCorrectionBasis):
        basis = basis.sources
    return _basis_evidence_refs(basis) if isinstance(basis, CommittedEvidenceBasis) else ()


def _supersedes_evidence_ref(authority: object) -> EvidenceRef:
    return EvidenceRef(
        ref_id=authority.accepted_event_ref,  # type: ignore[attr-defined]
        evidence_type="committed_world_event",
        claim_purpose="future_plan",
        source_world_revision=authority.accepted_world_revision,  # type: ignore[attr-defined]
        immutable_hash=authority.accepted_payload_hash,  # type: ignore[attr-defined]
    )


def _completion_evidence_ref(evidence: object) -> EvidenceRef:
    return EvidenceRef(
        ref_id=evidence.evidence_ref,  # type: ignore[attr-defined]
        evidence_type=(
            "committed_fact"
            if isinstance(evidence, V2GoalFactCompletionEvidence)
            else "settled_world_event"
        ),
        claim_purpose=(
            "current_fact"
            if isinstance(evidence, V2GoalFactCompletionEvidence)
            else "future_plan"
        ),
        source_world_revision=evidence.evidence_world_revision,  # type: ignore[attr-defined]
        immutable_hash=evidence.evidence_payload_hash,  # type: ignore[attr-defined]
    )


def v2_goal_mutation_hash(
    payload: V2GoalChangedPayload | Mapping[str, Any],
) -> str:
    material = (
        payload.model_dump(mode="json")
        if isinstance(payload, V2GoalChangedPayload)
        else to_jsonable_python(dict(payload))
    )
    material.pop("accepted_change_hash", None)
    encoded = json.dumps(
        canonicalize_json_value(material),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
