"""Pinned, model-authored life development without a plot candidate library."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
from typing import Literal, Protocol

import httpx
from pydantic import Field

from .context_resolver import query_from_projection
from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .life_author_runtime import (
    LifeContextCapsuleCompiler,
    compile_life_decision_context,
)
from .life_content_store import (
    ImmutableLifeContentStore,
    LifeContentKind,
    StoredLifeContent,
    life_content_payload_hash,
)
from .life_development_draft import (
    CharacterChoiceAcceptDraft,
    CharacterChoiceNoOpDraft,
    LifeDevelopmentCapabilityManifest,
    LifeDevelopmentCapabilityManifestCompiler,
    LifeDevelopmentClaimDeclaration,
    LifeDevelopmentDraftError,
    LifeDevelopmentLocationCapability,
    LifeDevelopmentNoOpDraft,
    LifeDevelopmentPossibilityDraft,
    LifeDevelopmentVisualEvidenceDraft,
    LifeDevelopmentWorldDraft,
    parse_character_choice,
    parse_world_author_draft,
)
from .life_events import (
    ActivityPlannedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
)
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from .proposal_envelope import MinimalProposal
from .schema_core import FrozenModel
from .schemas import (
    DueWindow,
    DynamicLifeArcContextDescriptor,
    EvidenceRef,
    OutcomeCandidateDescriptor,
    PlanStateProjection,
    ProjectionCursor,
    ProvisionalNpcIntroductionDescriptor,
    WorldEvent,
    WorldOccurrenceProjection,
)


_LOG = logging.getLogger(__name__)


class LifeDevelopmentModel(Protocol):
    model: str

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str: ...


class LifeDevelopmentResult(FrozenModel):
    status: Literal[
        "no_op",
        "occurrence_committed",
        "plan_committed",
        "rejected",
        "stale_prefix",
        "technical_failure",
    ]
    reason_code: str
    proposal_event_ref: str | None = Field(default=None, min_length=1)
    occurrence_id: str | None = Field(default=None, min_length=1)
    plan_id: str | None = Field(default=None, min_length=1)


_AttemptStatus = Literal[
    "proposal_validated",
    "main_timeout",
    "main_invalid",
    "main_exception",
    "main_invalid_recovered",
    "recovery_failed",
]
_AttemptOutcome = Literal["winner", "invalid", "timeout", "exception"]


@dataclass(frozen=True)
class _LifeDevelopmentAttempt:
    request_hash: str
    raw_output: str | None
    status: _AttemptStatus
    failure_code: str | None = None
    slot: Literal["primary", "corrective"] | None = None
    outcome: _AttemptOutcome | None = None


@dataclass(frozen=True)
class _LifeDevelopmentModelRun:
    model_id: str
    parsed: LifeDevelopmentWorldDraft | CharacterChoiceAcceptDraft | CharacterChoiceNoOpDraft | None
    attempts: tuple[_LifeDevelopmentAttempt, ...]

    @property
    def succeeded(self) -> bool:
        return self.parsed is not None

    @property
    def final_raw(self) -> str | None:
        return self.attempts[-1].raw_output

    @property
    def repair_ordinal(self) -> int:
        return len(self.attempts) - 1


@dataclass(frozen=True)
class _RecordedDeliberation:
    role: Literal["world_author", "character_model"]
    capsule_id: str
    context_cursor: ProjectionCursor
    request_hashes: tuple[str, ...]
    response_hashes: tuple[str | None, ...]
    raw_content_refs: tuple[str | None, ...]
    model_result_event_refs: tuple[str, ...]
    model_result_event_hashes: tuple[str, ...]
    audit_proposal_event_ref: str | None
    audit_proposal_event_hash: str | None
    deliberation_result_id: str
    final_model_result_ref: str
    context_model_content_hash: str
    context_snapshot_hash: str
    decision_subject_hash: str
    capability_manifest: dict[str, object] | None = None
    capability_manifest_content_ref: str | None = None
    capability_manifest_content_hash: str | None = None

    def authority_payload(self) -> dict[str, object]:
        return {
            "role": self.role,
            "capsule_id": self.capsule_id,
            "context_cursor": self.context_cursor.model_dump(mode="json"),
            "request_hashes": list(self.request_hashes),
            "response_hashes": list(self.response_hashes),
            "raw_content_refs": list(self.raw_content_refs),
            "model_result_event_refs": list(self.model_result_event_refs),
            "model_result_event_hashes": list(self.model_result_event_hashes),
            "audit_proposal_event_ref": self.audit_proposal_event_ref,
            "audit_proposal_event_hash": self.audit_proposal_event_hash,
            "deliberation_result_id": self.deliberation_result_id,
            "final_model_result_ref": self.final_model_result_ref,
            "context_model_content_hash": self.context_model_content_hash,
            "context_snapshot_hash": self.context_snapshot_hash,
            "decision_subject_hash": self.decision_subject_hash,
            "capability_manifest": self.capability_manifest,
            "capability_manifest_content_ref": (
                self.capability_manifest_content_ref
            ),
            "capability_manifest_content_hash": (
                self.capability_manifest_content_hash
            ),
        }


@dataclass(frozen=True)
class _PinnedIdentity:
    capsule_id: str
    snapshot_hash: str
    world_revision: int
    deliberation_revision: int
    ledger_sequence: int
    model_content_json: str


class LifeDevelopmentReadableOutcome(FrozenModel):
    descriptor: OutcomeCandidateDescriptor
    text: str = Field(min_length=1, max_length=12_000)
    visual_evidence: LifeDevelopmentVisualEvidenceDraft | None = None


class LifeDevelopmentPlanMaterial(FrozenModel):
    plan_id: str = Field(min_length=1)
    proposal_event_ref: str = Field(min_length=1)
    causal_authority: Literal["character_choice"]
    premise: str = Field(min_length=1, max_length=12_000)
    claim_declarations: tuple[LifeDevelopmentClaimDeclaration, ...]
    outcomes: tuple[LifeDevelopmentReadableOutcome, ...] = Field(min_length=2, max_length=4)
    character_intention: str = Field(min_length=1, max_length=4_000)


class LifeDevelopmentProposalReader:
    """Rehydrate one accepted open Plan from its exact Proposal sidecars."""

    def __init__(self, *, ledger, content_store: ImmutableLifeContentStore) -> None:
        self._ledger = ledger
        self._store = content_store

    def read_for_plan(self, *, plan_id: str) -> LifeDevelopmentPlanMaterial | None:
        plan = next(
            (item for item in self._ledger.project().plans if item.plan_id == plan_id),
            None,
        )
        if plan is None or not plan_id.startswith("plan:life-development:"):
            return None
        suffix = plan_id.removeprefix("plan:life-development:")
        plan_event_commit = self._ledger.lookup_event_commit(
            "event:life-development:plan:" + suffix
        )
        if (
            plan_event_commit is None
            or plan_event_commit[0].event_type != "ActivityPlanned"
            or plan_event_commit[0].source != "world-v2:life-development"
            or plan_event_commit[0].payload().get("plan", {}).get("plan_id") != plan_id
        ):
            return None
        proposal_commit = self._ledger.lookup_event_commit(plan_event_commit[0].causation_id)
        if proposal_commit is None:
            return None
        proposal_event = proposal_commit[0]
        payload = proposal_event.payload()
        possibility = payload.get("possibility_authority")
        character_choice = payload.get("character_choice")
        if (
            payload.get("proposal_kind") != "life_development"
            or payload.get("effect_kind") != "character_plan"
            or payload.get("effect_ref") != plan_id
            or not isinstance(possibility, dict)
            or payload.get("possibility_authority_hash") != _digest(possibility)
            or possibility.get("causal_authority") != "character_choice"
            or not isinstance(character_choice, dict)
            or payload.get("character_choice_hash") != _digest(character_choice)
            or character_choice.get("decision") != "accept"
        ):
            raise ValueError("accepted life-development Plan has invalid Proposal authority")
        premise_descriptor = possibility.get("premise")
        outcome_values = possibility.get("outcomes")
        claims = possibility.get("claim_declarations")
        intention_descriptor = character_choice.get("intention")
        if (
            not isinstance(premise_descriptor, dict)
            or not isinstance(outcome_values, list)
            or not isinstance(claims, list)
            or not isinstance(intention_descriptor, dict)
        ):
            raise ValueError("life-development Plan material descriptor is incomplete")
        premise = self._read_bound_text(premise_descriptor)
        intention = self._read_bound_text(intention_descriptor)
        outcomes: list[LifeDevelopmentReadableOutcome] = []
        for item in outcome_values:
            if not isinstance(item, dict) or not isinstance(item.get("descriptor"), dict):
                raise ValueError("life-development outcome descriptor is malformed")
            descriptor = OutcomeCandidateDescriptor.model_validate_json(
                json.dumps(
                    item["descriptor"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            if descriptor.content_ref is None or descriptor.content_payload_hash is None:
                raise ValueError("life-development outcome has no readable sidecar binding")
            text = self._read_bound_text(
                {
                    "content_ref": descriptor.content_ref,
                    "content_payload_hash": descriptor.content_payload_hash,
                }
            )
            outcomes.append(
                LifeDevelopmentReadableOutcome(
                    descriptor=descriptor,
                    text=text,
                    visual_evidence=(
                        LifeDevelopmentVisualEvidenceDraft.model_validate_json(
                            json.dumps(
                                item["visual_evidence"],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
                        if item.get("visual_evidence") is not None
                        else None
                    ),
                )
            )
        return LifeDevelopmentPlanMaterial(
            plan_id=plan_id,
            proposal_event_ref=proposal_event.event_id,
            causal_authority="character_choice",
            premise=premise,
            claim_declarations=tuple(
                LifeDevelopmentClaimDeclaration.model_validate_json(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                for item in claims
            ),
            outcomes=tuple(outcomes),
            character_intention=intention,
        )

    def _read_bound_text(self, descriptor: dict[str, object]) -> str:
        content_ref = descriptor.get("content_ref")
        expected_hash = descriptor.get("content_payload_hash")
        if not isinstance(content_ref, str) or not isinstance(expected_hash, str):
            raise ValueError("life-development content descriptor is malformed")
        stored = self._store.read_exact(content_ref=content_ref)
        if stored is None or stored.content_payload_hash != expected_hash:
            raise ValueError("life-development content sidecar is unavailable")
        return stored.text


class LifeDevelopmentRuntime:
    """One deep entry point from exact wake to an admitted life possibility."""

    def __init__(
        self,
        *,
        ledger,
        content_store: ImmutableLifeContentStore,
        world_author: LifeDevelopmentModel,
        character_model: LifeDevelopmentModel,
        capsule_compiler: LifeContextCapsuleCompiler,
        capability_manifest_compiler: LifeDevelopmentCapabilityManifestCompiler,
        owner_actor_ref: str,
        actor: str = "worker:world-v2:life-development",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("Life Development requires owner and actor identities")
        self._ledger = ledger
        self._store = content_store
        self._world_author = world_author
        self._character_model = character_model
        self._capsule_compiler = capsule_compiler
        self._manifest_compiler = capability_manifest_compiler
        self._owner = owner_actor_ref
        self._actor = actor
        self._world_author_model = (
            str(getattr(world_author, "model", "")).strip() or type(world_author).__name__
        )
        self._character_model_id = (
            str(getattr(character_model, "model", "")).strip() or type(character_model).__name__
        )

    async def advance_once(
        self,
        *,
        wake_event_ref: str,
        trace_id: str,
        correlation_id: str,
    ) -> LifeDevelopmentResult:
        proposal_id = "proposal:life-development:" + _digest(
            {"world_id": self._ledger.world_id, "wake_event_ref": wake_event_ref}
        )
        proposal_event_id = "event:life-development:proposal:" + _digest(proposal_id)
        existing = self._ledger.lookup_event_commit(proposal_event_id)
        if existing is not None:
            return self._recovered_result(existing[0])

        projection = self._ledger.project()
        wake = self._exact_wake(projection=projection, wake_event_ref=wake_event_ref)
        if wake is None:
            return LifeDevelopmentResult(
                status="rejected",
                reason_code="life_development.wake_not_exact",
            )
        pinned = self._compile_pinned(projection=projection, wake=wake)
        if isinstance(pinned, LifeDevelopmentResult):
            return pinned
        world_capsule, world_cursor, world_context, world_manifest = pinned
        world_subject_hash = _digest(
            {
                "role": "world_author",
                "wake_event_ref": wake.event_id,
                "world_revision": world_cursor.world_revision,
            }
        )

        recovered_world = self._recover_successful_model_run(
            proposal_id=proposal_id,
            role="world_author",
            current_world_revision=projection.world_revision,
            expected_subject_hash=world_subject_hash,
        )
        if recovered_world is None:
            world_run = await self._world_author_draft(
                context=world_context,
                logical_time=wake.logical_time,
                manifest=world_manifest,
            )
            try:
                world_audit = self._record_model_run(
                    proposal_id=proposal_id,
                    role="world_author",
                    run=world_run,
                    wake=wake,
                    capsule=world_capsule,
                    manifest=world_manifest,
                    decision_subject_hash=world_subject_hash,
                    expected_cursor=world_cursor,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                return LifeDevelopmentResult(
                    status="stale_prefix",
                    reason_code="life_development.model_result_prefix_stale",
                )
            if not world_run.succeeded:
                return LifeDevelopmentResult(
                    status="technical_failure",
                    reason_code="life_development.world_author_unavailable",
                )
            draft = world_run.parsed
            raw = world_run.final_raw
            world_repair_ordinal = world_run.repair_ordinal
        else:
            (
                raw,
                world_repair_ordinal,
                world_audit,
                world_capsule,
                recovered_manifest,
            ) = recovered_world
            if recovered_manifest is None:
                raise ValueError("recovered World Author audit lacks its manifest")
            world_manifest = recovered_manifest
            world_cursor = world_audit.context_cursor
            draft = parse_world_author_draft(
                raw=raw,
                manifest=world_manifest,
                logical_time=wake.logical_time,
            )
        if (
            not isinstance(
                draft,
                (LifeDevelopmentNoOpDraft, LifeDevelopmentPossibilityDraft),
            )
            or raw is None
        ):
            raise ValueError("validated World Author run has no usable draft")

        # A model audit advances only Deliberation. The admitted effect may use
        # the original capsule iff no World fact changed in between; it must
        # never relabel these bytes as a decision made from a newer capsule.
        projection = self._ledger.project()
        acceptance_cursor = _cursor(projection)
        if projection.world_revision != world_cursor.world_revision:
            return LifeDevelopmentResult(
                status="stale_prefix",
                reason_code="life_development.world_author_result_stale",
            )
        if isinstance(draft, LifeDevelopmentNoOpDraft):
            proposal = self._proposal_event(
                proposal_event_id=proposal_event_id,
                proposal_id=proposal_id,
                wake=wake,
                context_cursor=world_cursor,
                capsule=world_capsule,
                manifest=world_manifest,
                draft=draft,
                raw=raw,
                repair_ordinal=world_repair_ordinal,
                trace_id=trace_id,
                correlation_id=correlation_id,
                world_author_deliberation=world_audit,
            )
            try:
                self._ledger.commit_at_cursor(
                    (proposal,),
                    expected_cursor=acceptance_cursor,
                    commit_id="commit:life-development:" + _digest(proposal_id),
                )
            except ConcurrencyConflict:
                existing = self._ledger.lookup_event_commit(proposal_event_id)
                if existing is not None:
                    return self._recovered_result(existing[0])
                return LifeDevelopmentResult(
                    status="stale_prefix",
                    reason_code="life_development.acceptance_prefix_stale",
                )
            return LifeDevelopmentResult(
                status="no_op",
                reason_code="life_development.world_author_no_op",
                proposal_event_ref=proposal.event_id,
            )
        if draft.causal_authority == "world_contingency":
            return self._commit_world_contingency(
                proposal_event_id=proposal_event_id,
                proposal_id=proposal_id,
                wake=wake,
                projection=projection,
                expected_cursor=acceptance_cursor,
                context_cursor=world_cursor,
                capsule=world_capsule,
                manifest=world_manifest,
                draft=draft,
                raw=raw,
                repair_ordinal=world_repair_ordinal,
                world_author_deliberation=world_audit,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        offered_window = draft.timing.resolve(
            logical_time=wake.logical_time,
            manifest=world_manifest,
        )
        character_subject_hash = _digest(
            {
                "external_opportunity": draft.model_dump(mode="json"),
                "offered_window": offered_window.model_dump(mode="json"),
            }
        )
        character_pinned = self._compile_pinned(projection=projection, wake=wake)
        if isinstance(character_pinned, LifeDevelopmentResult):
            return character_pinned
        (
            character_capsule,
            character_cursor,
            character_context,
            _character_manifest,
        ) = character_pinned
        recovered_character = self._recover_successful_model_run(
            proposal_id=proposal_id,
            role="character_model",
            current_world_revision=projection.world_revision,
            expected_subject_hash=character_subject_hash,
        )
        if recovered_character is None:
            character_run = await self._character_choice(
                context=character_context,
                draft=draft,
                offered_window=offered_window,
            )
            try:
                character_audit = self._record_model_run(
                    proposal_id=proposal_id,
                    role="character_model",
                    run=character_run,
                    wake=wake,
                    capsule=character_capsule,
                    manifest=None,
                    decision_subject_hash=character_subject_hash,
                    expected_cursor=character_cursor,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                return LifeDevelopmentResult(
                    status="stale_prefix",
                    reason_code="life_development.model_result_prefix_stale",
                )
            if not character_run.succeeded:
                return LifeDevelopmentResult(
                    status="technical_failure",
                    reason_code="life_development.character_model_unavailable",
                )
            character_choice = character_run.parsed
            character_raw = character_run.final_raw
            character_repair_ordinal = character_run.repair_ordinal
        else:
            (
                character_raw,
                character_repair_ordinal,
                character_audit,
                character_capsule,
                _unused_manifest,
            ) = recovered_character
            character_cursor = character_audit.context_cursor
            character_choice = parse_character_choice(
                raw=character_raw,
                offered=draft,
                offered_window=offered_window,
            )
        if (
            not isinstance(
                character_choice,
                (CharacterChoiceAcceptDraft, CharacterChoiceNoOpDraft),
            )
            or character_raw is None
        ):
            raise ValueError("validated Character run has no usable choice")
        projection = self._ledger.project()
        acceptance_cursor = _cursor(projection)
        if (
            projection.world_revision != world_cursor.world_revision
            or projection.world_revision != character_cursor.world_revision
        ):
            return LifeDevelopmentResult(
                status="stale_prefix",
                reason_code="life_development.character_result_stale",
            )
        if isinstance(character_choice, CharacterChoiceNoOpDraft):
            records, bindings, outcome_descriptors = self._materialize_content(
                proposal_id=proposal_id,
                draft=draft,
            )
            for record in records:
                self._store.put_if_absent(record)
            proposal = self._proposal_event(
                proposal_event_id=proposal_event_id,
                proposal_id=proposal_id,
                wake=wake,
                context_cursor=world_cursor,
                capsule=world_capsule,
                manifest=world_manifest,
                draft=draft,
                raw=raw,
                repair_ordinal=world_repair_ordinal,
                trace_id=trace_id,
                correlation_id=correlation_id,
                character_raw=character_raw,
                character_repair_ordinal=character_repair_ordinal,
                final_decision="no_op",
                content_bindings=bindings,
                outcome_descriptors=outcome_descriptors,
                character_choice=character_choice,
                world_author_deliberation=world_audit,
                character_deliberation=character_audit,
            )
            try:
                self._ledger.commit_at_cursor(
                    (proposal,),
                    expected_cursor=acceptance_cursor,
                    commit_id="commit:life-development:" + _digest(proposal_id),
                )
            except ConcurrencyConflict:
                existing = self._ledger.lookup_event_commit(proposal_event_id)
                if existing is not None:
                    return self._recovered_result(existing[0])
                return LifeDevelopmentResult(
                    status="stale_prefix",
                    reason_code="life_development.acceptance_prefix_stale",
                )
            return LifeDevelopmentResult(
                status="no_op",
                reason_code="life_development.character_declined",
                proposal_event_ref=proposal.event_id,
            )
        return self._commit_character_plan(
            proposal_event_id=proposal_event_id,
            proposal_id=proposal_id,
            wake=wake,
            projection=projection,
            expected_cursor=acceptance_cursor,
            context_cursor=world_cursor,
            capsule=world_capsule,
            manifest=world_manifest,
            draft=draft,
            world_author_raw=raw,
            world_author_repair_ordinal=world_repair_ordinal,
            world_author_deliberation=world_audit,
            character_choice=character_choice,
            character_raw=character_raw,
            character_repair_ordinal=character_repair_ordinal,
            character_deliberation=character_audit,
            offered_window=offered_window,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )

    def _commit_character_plan(
        self,
        *,
        proposal_event_id: str,
        proposal_id: str,
        wake: WorldEvent,
        projection,
        expected_cursor: ProjectionCursor,
        context_cursor: ProjectionCursor,
        capsule,
        manifest: LifeDevelopmentCapabilityManifest,
        draft: LifeDevelopmentPossibilityDraft,
        world_author_raw: str,
        world_author_repair_ordinal: int,
        world_author_deliberation: _RecordedDeliberation,
        character_choice: CharacterChoiceAcceptDraft,
        character_raw: str,
        character_repair_ordinal: int,
        character_deliberation: _RecordedDeliberation,
        offered_window: DueWindow,
        trace_id: str,
        correlation_id: str,
    ) -> LifeDevelopmentResult:
        plan_id = "plan:life-development:" + _digest(proposal_id)
        records, bindings, outcome_descriptors = self._materialize_content(
            proposal_id=proposal_id,
            draft=draft,
        )
        intention_ref = "content:life-development:character-intention:" + _digest(proposal_id)
        intention_hash = life_content_payload_hash(character_choice.intention_summary)
        records = (
            *records,
            StoredLifeContent(
                content_ref=intention_ref,
                content_kind="outcome_candidate",
                content_payload_hash=intention_hash,
                text=character_choice.intention_summary,
            ),
        )
        bindings = (
            *bindings,
            {
                "role": "character_intention",
                "content_ref": intention_ref,
                "content_payload_hash": intention_hash,
            },
        )
        for record in records:
            self._store.put_if_absent(record)
        selected_window = (
            DueWindow(
                opens_at=character_choice.opens_at,
                closes_at=character_choice.closes_at,
            )
            if character_choice.opens_at is not None and character_choice.closes_at is not None
            else offered_window
        )
        proposal = self._proposal_event(
            proposal_event_id=proposal_event_id,
            proposal_id=proposal_id,
            wake=wake,
            context_cursor=context_cursor,
            capsule=capsule,
            manifest=manifest,
            draft=draft,
            raw=world_author_raw,
            repair_ordinal=world_author_repair_ordinal,
            trace_id=trace_id,
            correlation_id=correlation_id,
            effect_kind="character_plan",
            effect_ref=plan_id,
            content_bindings=bindings,
            character_raw=character_raw,
            character_repair_ordinal=character_repair_ordinal,
            final_decision="accept",
            outcome_descriptors=outcome_descriptors,
            character_choice=character_choice,
            world_author_deliberation=world_author_deliberation,
            character_deliberation=character_deliberation,
        )
        location_capability = self._selected_location_capability(
            draft=draft,
            manifest=manifest,
        )
        evidence = self._evidence_refs(
            projection=projection,
            anchor_refs=draft.anchor_refs,
            location_authority_refs=(
                location_capability.authority_refs if location_capability is not None else ()
            ),
            claim_purpose="future_plan",
        )
        policy_refs = self._policy_refs(
            projection=projection,
            location_authority_refs=(
                location_capability.authority_refs if location_capability is not None else ()
            ),
        )
        plan = PlanStateProjection(
            plan_id=plan_id,
            activity_id="activity:life-development:" + _digest(proposal_id),
            entity_revision=1,
            activity_kind=("open_life." + _digest(character_choice.intention_summary)[:24]),
            evidence_refs=evidence,
            status="planned",
            importance_bp=character_choice.importance_bp,
            scheduled_window=selected_window,
            participant_refs=character_choice.participant_refs,
            location_ref=draft.location_ref,
            privacy_class=draft.privacy_class,
            owner_actor_ref=self._owner,
        )
        plan_payload = ActivityPlannedPayload(
            change_id="change:life-development:plan:" + _digest(proposal_id),
            transition_id="transition:life-development:plan:" + _digest(proposal_id),
            expected_entity_revision=0,
            evidence_refs=evidence,
            policy_refs=policy_refs,
            plan=plan,
        ).model_dump(mode="json")
        plan_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:life-development:plan:" + _digest(proposal_id),
            world_id=self._ledger.world_id,
            event_type="ActivityPlanned",
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:life-development",
            trace_id=trace_id or wake.trace_id,
            causation_id=proposal.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ActivityPlanned",
                    world_id=self._ledger.world_id,
                    payload=plan_payload,
                )
                or "life-development-plan:" + _digest(proposal_id)
            ),
            payload=plan_payload,
        )
        try:
            self._ledger.commit_at_cursor(
                (proposal, plan_event),
                expected_cursor=expected_cursor,
                commit_id="commit:life-development:" + _digest(proposal_id),
            )
        except ConcurrencyConflict:
            existing = self._ledger.lookup_event_commit(proposal_event_id)
            if existing is not None:
                return self._recovered_result(existing[0])
            return LifeDevelopmentResult(
                status="stale_prefix",
                reason_code="life_development.acceptance_prefix_stale",
            )
        return LifeDevelopmentResult(
            status="plan_committed",
            reason_code="life_development.character_plan_committed",
            proposal_event_ref=proposal.event_id,
            plan_id=plan_id,
        )

    def _commit_world_contingency(
        self,
        *,
        proposal_event_id: str,
        proposal_id: str,
        wake: WorldEvent,
        projection,
        expected_cursor: ProjectionCursor,
        context_cursor: ProjectionCursor,
        capsule,
        manifest: LifeDevelopmentCapabilityManifest,
        draft: LifeDevelopmentPossibilityDraft,
        raw: str,
        repair_ordinal: int,
        world_author_deliberation: _RecordedDeliberation,
        trace_id: str,
        correlation_id: str,
    ) -> LifeDevelopmentResult:
        occurrence_id = "occurrence:life-development:" + _digest(proposal_id)
        records, bindings, candidates = self._materialize_content(
            proposal_id=proposal_id,
            draft=draft,
        )
        for record in records:
            self._store.put_if_absent(record)
        window = draft.timing.resolve(
            logical_time=wake.logical_time,
            manifest=manifest,
        )
        proposal = self._proposal_event(
            proposal_event_id=proposal_event_id,
            proposal_id=proposal_id,
            wake=wake,
            context_cursor=context_cursor,
            capsule=capsule,
            manifest=manifest,
            draft=draft,
            raw=raw,
            repair_ordinal=repair_ordinal,
            trace_id=trace_id,
            correlation_id=correlation_id,
            effect_kind="world_occurrence",
            effect_ref=occurrence_id,
            content_bindings=bindings,
            outcome_descriptors=candidates,
            world_author_deliberation=world_author_deliberation,
        )
        location_capability = self._selected_location_capability(
            draft=draft,
            manifest=manifest,
        )
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id,
            entity_revision=1,
            trigger_ref=proposal.event_id,
            participant_refs=(self._owner, *draft.entity_refs),
            location_ref=draft.location_ref,
            time_window=window,
            candidate_outcome_refs=tuple(item.candidate_result_ref for item in candidates),
            candidate_outcomes=candidates,
            visibility=draft.privacy_class,
            status="committed",
        )
        evidence = self._evidence_refs(
            projection=projection,
            anchor_refs=draft.anchor_refs,
            location_authority_refs=(
                location_capability.authority_refs if location_capability is not None else ()
            ),
            claim_purpose=(
                "current_fact" if window.opens_at == wake.logical_time else "future_plan"
            ),
        )
        policy_refs = self._policy_refs(
            projection=projection,
            location_authority_refs=(
                location_capability.authority_refs if location_capability is not None else ()
            ),
        )
        occurrence_payload = WorldOccurrenceCommittedPayload(
            change_id="change:life-development:occurrence:" + _digest(proposal_id),
            transition_id="transition:life-development:occurrence:" + _digest(proposal_id),
            expected_entity_revision=0,
            evidence_refs=evidence,
            policy_refs=policy_refs,
            occurrence=occurrence,
        ).model_dump(mode="json")
        occurrence_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:life-development:occurrence:" + _digest(proposal_id),
            world_id=self._ledger.world_id,
            event_type="WorldOccurrenceCommitted",
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:life-development",
            trace_id=trace_id or wake.trace_id,
            causation_id=proposal.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="WorldOccurrenceCommitted",
                    world_id=self._ledger.world_id,
                    payload=occurrence_payload,
                )
                or "life-development-occurrence:" + _digest(proposal_id)
            ),
            payload=occurrence_payload,
        )
        events: tuple[WorldEvent, ...] = (proposal, occurrence_event)
        if draft.timing.mode == "now":
            activation_payload = WorldOccurrenceActivatedPayload(
                change_id="change:life-development:activate:" + _digest(proposal_id),
                transition_id="transition:life-development:activate:" + _digest(proposal_id),
                expected_entity_revision=1,
                evidence_refs=evidence,
                policy_refs=policy_refs,
                occurrence_id=occurrence_id,
                activated_at=wake.logical_time,
                satisfied_precondition_refs=(),
            ).model_dump(mode="json")
            activation_event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:life-development:activated:" + _digest(proposal_id),
                world_id=self._ledger.world_id,
                event_type="WorldOccurrenceActivated",
                logical_time=wake.logical_time,
                created_at=wake.created_at,
                actor=self._actor,
                source="world-v2:life-development",
                trace_id=trace_id or wake.trace_id,
                causation_id=occurrence_event.event_id,
                correlation_id=correlation_id or wake.correlation_id,
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="WorldOccurrenceActivated",
                        world_id=self._ledger.world_id,
                        payload=activation_payload,
                    )
                    or "life-development-activated:" + _digest(proposal_id)
                ),
                payload=activation_payload,
            )
            events = (*events, activation_event)
        try:
            self._ledger.commit_at_cursor(
                events,
                expected_cursor=expected_cursor,
                commit_id="commit:life-development:" + _digest(proposal_id),
            )
        except ConcurrencyConflict:
            existing = self._ledger.lookup_event_commit(proposal_event_id)
            if existing is not None:
                return self._recovered_result(existing[0])
            return LifeDevelopmentResult(
                status="stale_prefix",
                reason_code="life_development.acceptance_prefix_stale",
            )
        return LifeDevelopmentResult(
            status="occurrence_committed",
            reason_code="life_development.world_contingency_committed",
            proposal_event_ref=proposal.event_id,
            occurrence_id=occurrence_id,
        )

    def _compile_pinned(
        self,
        *,
        projection,
        wake: WorldEvent,
    ) -> (
        tuple[
            object,
            ProjectionCursor,
            dict[str, object],
            LifeDevelopmentCapabilityManifest,
        ]
        | LifeDevelopmentResult
    ):
        try:
            capsule = self._capsule_compiler.compile_for_deliberation(
                query_from_projection(
                    projection,
                    actor_ref=self._owner,
                    trigger_ref=wake.event_id,
                )
            ).capsule
            context_cursor = _capsule_cursor(capsule)
            if context_cursor != _cursor(projection):
                raise ConcurrencyConflict("Life Development Context prefix changed")
            context = compile_life_decision_context(capsule)
            manifest = self._manifest_compiler.compile(
                projection=projection,
                wake=wake,
                capsule=capsule,
            )
            if manifest.pinned_cursor != context_cursor:
                raise ConcurrencyConflict(
                    "Life Development capability manifest belongs to another prefix"
                )
            return capsule, context_cursor, context, manifest
        except ConcurrencyConflict:
            return LifeDevelopmentResult(
                status="stale_prefix",
                reason_code="life_development.context_prefix_stale",
            )
        except (TypeError, ValueError) as exc:
            _LOG.warning("Life Development Context unavailable: %s", exc)
            return LifeDevelopmentResult(
                status="technical_failure",
                reason_code="life_development.context_unavailable",
            )

    def _record_model_run(
        self,
        *,
        proposal_id: str,
        role: Literal["world_author", "character_model"],
        run: _LifeDevelopmentModelRun,
        wake: WorldEvent,
        capsule,
        manifest: LifeDevelopmentCapabilityManifest | None,
        decision_subject_hash: str,
        expected_cursor: ProjectionCursor,
        trace_id: str,
        correlation_id: str,
    ) -> _RecordedDeliberation:
        if not run.attempts:
            raise ValueError("Life Development model run has no attempts")
        suffix = _digest(
            {
                "proposal_id": proposal_id,
                "model_role": role,
            }
        )
        epoch = _digest(
            {
                "attempt_request_hashes": [attempt.request_hash for attempt in run.attempts],
                "capsule_id": capsule.capsule_id,
                "context_cursor": expected_cursor.model_dump(mode="json"),
                "proposal_id": proposal_id,
                "role": role,
            }
        )
        retry_ordinal = self._next_model_retry_ordinal(
            proposal_id=proposal_id,
            role=role,
        )
        attempt_id = f"attempt:life-development:{role}:{suffix}:epoch:{epoch}:retry:{retry_ordinal}"
        route = RecordedModelRoute(
            tier="flash",
            reason_code=f"life_development.{role}",
            router_version="life-development-router.2",
        )
        audits: list[RecordedModelResultAudit] = []
        raw_content_refs: list[str | None] = []
        for index, attempt in enumerate(run.attempts):
            response_hash = (
                life_content_payload_hash(attempt.raw_output)
                if attempt.raw_output is not None
                else None
            )
            if attempt.raw_output is not None and response_hash is not None:
                content_ref = (
                    "content:life-development:model-result:"
                    f"{suffix}:{epoch}:{index}:{response_hash}"
                )
                self._store.put_if_absent(
                    StoredLifeContent(
                        content_ref=content_ref,
                        content_kind="outcome_candidate",
                        content_payload_hash=response_hash,
                        text=attempt.raw_output,
                    )
                )
                raw_content_refs.append(content_ref)
            else:
                raw_content_refs.append(None)
            model_call_id = (
                f"model-call:life-development:{role}:{suffix}:epoch:{epoch}:"
                f"retry:{retry_ordinal}:call:{index}"
            )
            model_result_ref = "model-result:" + sha256(
                canonical_json(
                    {
                        "model_call_id": model_call_id,
                        "response_hash": response_hash,
                    }
                )
            )
            has_output = response_hash is not None
            audits.append(
                RecordedModelResultAudit(
                    model_call_id=model_call_id,
                    model_result_ref=model_result_ref,
                    attempt_id=attempt_id,
                    route=route,
                    model_id=run.model_id if has_output else None,
                    model_version=run.model_id if has_output else None,
                    attempted_model_id=None if has_output else run.model_id,
                    attempted_model_version=None if has_output else run.model_id,
                    request_hash=attempt.request_hash,
                    response_hash=response_hash,
                    status=attempt.status,
                    failure_code=attempt.failure_code,
                    slot=attempt.slot,
                    outcome=attempt.outcome,
                )
            )

        final = audits[-1]
        manifest_value = (
            manifest.model_dump(
                mode="json",
                exclude_computed_fields=True,
            )
            if run.succeeded and manifest is not None
            else None
        )
        manifest_text = (
            canonical_json(manifest_value)
            if manifest_value is not None
            else None
        )
        manifest_content_hash = (
            life_content_payload_hash(manifest_text)
            if manifest_text is not None
            else None
        )
        manifest_content_ref = (
            "content:life-development:capability-manifest:"
            f"{suffix}:{epoch}:{manifest_content_hash}"
            if manifest_content_hash is not None
            else None
        )
        if (
            manifest_text is not None
            and manifest_content_hash is not None
            and manifest_content_ref is not None
        ):
            self._store.put_if_absent(
                StoredLifeContent(
                    content_ref=manifest_content_ref,
                    content_kind="outcome_candidate",
                    content_payload_hash=manifest_content_hash,
                    text=manifest_text,
                )
            )
        audit_proposal: MinimalProposal | None = None
        proposal_hash: str | None = None
        if run.succeeded:
            audit_proposal = MinimalProposal(
                proposal_id=(f"proposal:life-development:model-output:{role}:{suffix}:{epoch}"),
                trigger_ref=wake.event_id,
                evaluated_world_revision=expected_cursor.world_revision,
                evidence_refs=(),
                proposed_changes=(),
                action_intents=(),
                confidence=10_000,
                brief_rationale="Persist validated life-development model output.",
                source_model_result=final.model_result_ref,
                response_text=canonical_json(
                    {
                        "final_response_hash": final.response_hash,
                        "model_role": role,
                        "decision_subject_hash": decision_subject_hash,
                        "request_hashes": [attempt.request_hash for attempt in run.attempts],
                        "response_hashes": [audit.response_hash for audit in audits],
                        "raw_content_refs": raw_content_refs,
                        "context_identity": {
                            "capsule_id": capsule.capsule_id,
                            "context_cursor": expected_cursor.model_dump(mode="json"),
                            "model_content_hash": hashlib.sha256(
                                capsule.model_content_json.encode("utf-8")
                            ).hexdigest(),
                            "snapshot_hash": capsule.snapshot_hash,
                        },
                        "capability_manifest_binding": (
                            {
                                "content_ref": manifest_content_ref,
                                "content_payload_hash": manifest_content_hash,
                            }
                            if manifest_content_ref is not None
                            and manifest_content_hash is not None
                            else None
                        ),
                    }
                ),
                stance="answer_without_world_claims",
            )
            proposal_hash = audit_proposal.proposal_hash
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": capsule.capsule_id,
                    "proposal_hash": proposal_hash,
                    "attempt_audits": [json.loads(model_audit_json(audit)) for audit in audits],
                }
            )
        )
        events: list[WorldEvent] = []
        for index, audit in enumerate(audits):
            audit_json = model_audit_json(audit)
            payload = ModelResultRecordedPayload(
                audit_contract=(
                    "model-result-audit.3" if not run.succeeded else "model-result-audit.1"
                ),
                model_result_ref=audit.model_result_ref,
                deliberation_result_id=deliberation_result_id,
                proposal_hash=proposal_hash,
                model_call_id=audit.model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule.capsule_id,
                trigger_ref=wake.event_id,
                evaluated_world_revision=expected_cursor.world_revision,
                attempt_index=index,
                attempt_count=len(audits),
                audit_json=audit_json,
                audit_hash=sha256(audit_json),
            ).model_dump(mode="json")
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=(
                    f"event:life-development:model-"
                    f"{'result' if run.succeeded else 'failure'}:"
                    f"{suffix}:{epoch}:{index}"
                ),
                world_id=self._ledger.world_id,
                event_type="ModelResultRecorded",
                logical_time=wake.logical_time,
                created_at=wake.created_at,
                actor=self._actor,
                source="world-v2:life-development",
                trace_id=trace_id or wake.trace_id,
                causation_id=wake.event_id if not events else events[-1].event_id,
                correlation_id=correlation_id or wake.correlation_id,
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="ModelResultRecorded",
                        world_id=self._ledger.world_id,
                        payload=payload,
                    )
                    or f"life-development-model-result:{suffix}:{epoch}:{index}"
                ),
                payload=payload,
            )
            events.append(event)

        audit_proposal_event: WorldEvent | None = None
        if audit_proposal is not None and proposal_hash is not None:
            proposal_payload = ProposalRecordedV2Payload(
                proposal_id=audit_proposal.proposal_id,
                proposal_kind=audit_proposal.proposal_kind,
                model_result_ref=final.model_result_ref,
                deliberation_result_id=deliberation_result_id,
                model_call_id=final.model_call_id,
                attempt_id=attempt_id,
                capsule_id=capsule.capsule_id,
                trigger_ref=wake.event_id,
                evaluated_world_revision=expected_cursor.world_revision,
                proposal_json=canonical_json(audit_proposal.model_dump(mode="json")),
                proposal_hash=proposal_hash,
            ).model_dump(mode="json")
            audit_proposal_event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=(f"event:life-development:model-proposal:{suffix}:{epoch}"),
                world_id=self._ledger.world_id,
                event_type="ProposalRecorded",
                logical_time=wake.logical_time,
                created_at=wake.created_at,
                actor=self._actor,
                source="world-v2:life-development",
                trace_id=trace_id or wake.trace_id,
                causation_id=events[-1].event_id,
                correlation_id=correlation_id or wake.correlation_id,
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="ProposalRecorded",
                        world_id=self._ledger.world_id,
                        payload=proposal_payload,
                    )
                    or f"life-development-model-proposal:{suffix}:{epoch}"
                ),
                payload=proposal_payload,
            )
            events.append(audit_proposal_event)
        self._ledger.commit_at_cursor(
            tuple(events),
            expected_cursor=expected_cursor,
            commit_id=f"commit:life-development:model-run:{suffix}:{epoch}",
        )
        return _RecordedDeliberation(
            role=role,
            capsule_id=capsule.capsule_id,
            context_cursor=expected_cursor,
            request_hashes=tuple(attempt.request_hash for attempt in run.attempts),
            response_hashes=tuple(audit.response_hash for audit in audits),
            raw_content_refs=tuple(raw_content_refs),
            model_result_event_refs=tuple(
                event.event_id for event in events if event.event_type == "ModelResultRecorded"
            ),
            model_result_event_hashes=tuple(
                event.payload_hash for event in events if event.event_type == "ModelResultRecorded"
            ),
            audit_proposal_event_ref=(
                audit_proposal_event.event_id if audit_proposal_event is not None else None
            ),
            audit_proposal_event_hash=(
                audit_proposal_event.payload_hash if audit_proposal_event is not None else None
            ),
            deliberation_result_id=deliberation_result_id,
            final_model_result_ref=final.model_result_ref,
            context_model_content_hash=hashlib.sha256(
                capsule.model_content_json.encode("utf-8")
            ).hexdigest(),
            context_snapshot_hash=capsule.snapshot_hash,
            decision_subject_hash=decision_subject_hash,
            capability_manifest=manifest_value,
            capability_manifest_content_ref=manifest_content_ref,
            capability_manifest_content_hash=manifest_content_hash,
        )

    def _next_model_retry_ordinal(
        self,
        *,
        proposal_id: str,
        role: Literal["world_author", "character_model"],
    ) -> int:
        suffix = _digest({"proposal_id": proposal_id, "model_role": role})
        prefix = f"attempt:life-development:{role}:{suffix}:"
        terminals = 0
        for item in self._ledger.project().model_result_audits:
            if (
                item.attempt_id.startswith(prefix)
                and item.attempt_index == item.attempt_count - 1
                and item.proposal_hash is None
            ):
                terminals += 1
        return terminals

    def _recover_successful_model_run(
        self,
        *,
        proposal_id: str,
        role: Literal["world_author", "character_model"],
        current_world_revision: int,
        expected_subject_hash: str,
    ) -> (
        tuple[
            str,
            int,
            _RecordedDeliberation,
            _PinnedIdentity,
            LifeDevelopmentCapabilityManifest | None,
        ]
        | None
    ):
        """Resume only an exact, already-audited deliberation at the same World."""

        suffix = _digest({"proposal_id": proposal_id, "model_role": role})
        prefix = f"attempt:life-development:{role}:{suffix}:"
        projection = self._ledger.project()
        terminals = [
            item
            for item in projection.model_result_audits
            if item.attempt_id.startswith(prefix)
            and item.attempt_index == item.attempt_count - 1
            and item.proposal_hash is not None
            and item.evaluated_world_revision == current_world_revision
        ]
        if not terminals:
            return None
        terminal = terminals[-1]
        attempts = tuple(
            sorted(
                (
                    item
                    for item in projection.model_result_audits
                    if item.deliberation_result_id == terminal.deliberation_result_id
                ),
                key=lambda item: item.attempt_index,
            )
        )
        if len(attempts) != terminal.attempt_count or tuple(
            item.attempt_index for item in attempts
        ) != tuple(range(terminal.attempt_count)):
            raise ValueError("recoverable Life Development audit is incomplete")
        proposal_audits = tuple(
            item
            for item in projection.proposal_audits
            if item.deliberation_result_id == terminal.deliberation_result_id
        )
        if len(proposal_audits) != 1:
            raise ValueError("recoverable Life Development audit lacks its exact proposal")
        audit_proposal = proposal_audits[0]
        proposal_value = json.loads(audit_proposal.proposal_json)
        response_text = proposal_value.get("response_text")
        if not isinstance(response_text, str):
            raise ValueError("recoverable Life Development metadata is absent")
        metadata = json.loads(response_text)
        context_identity = metadata.get("context_identity")
        raw_content_refs = metadata.get("raw_content_refs")
        request_hashes = metadata.get("request_hashes")
        response_hashes = metadata.get("response_hashes")
        if metadata.get("model_role") != role:
            raise ValueError(
                "recoverable Life Development metadata changed its role"
            )
        if metadata.get("decision_subject_hash") != expected_subject_hash:
            return None
        if (
            not isinstance(context_identity, dict)
            or not isinstance(raw_content_refs, list)
            or not isinstance(request_hashes, list)
            or not isinstance(response_hashes, list)
            or len(raw_content_refs) != len(attempts)
            or len(request_hashes) != len(attempts)
            or len(response_hashes) != len(attempts)
        ):
            raise ValueError("recoverable Life Development metadata changed its lineage")
        recorded_audits = tuple(
            RecordedModelResultAudit.model_validate_json(item.audit_json) for item in attempts
        )
        if (
            request_hashes != [item.request_hash for item in recorded_audits]
            or response_hashes != [item.response_hash for item in recorded_audits]
            or context_identity.get("capsule_id") != terminal.capsule_id
        ):
            raise ValueError("recoverable Life Development metadata is not audit-bound")
        cursor = ProjectionCursor.model_validate(context_identity.get("context_cursor"))
        if cursor.world_revision != current_world_revision:
            return None
        final_ref = raw_content_refs[-1]
        final_hash = recorded_audits[-1].response_hash
        if not isinstance(final_ref, str) or not isinstance(final_hash, str):
            raise ValueError("recoverable Life Development result has no bytes")
        stored = self._store.read_exact(content_ref=final_ref)
        if (
            stored is None
            or stored.content_payload_hash != final_hash
            or life_content_payload_hash(stored.text) != final_hash
        ):
            raise ValueError("recoverable Life Development result sidecar is unavailable")
        model_content_hash = context_identity.get("model_content_hash")
        snapshot_hash = context_identity.get("snapshot_hash")
        if not isinstance(model_content_hash, str) or not isinstance(
            snapshot_hash,
            str,
        ):
            raise ValueError("recoverable Life Development context identity is incomplete")
        manifest_binding = metadata.get("capability_manifest_binding")
        manifest_ref: str | None = None
        manifest_hash: str | None = None
        manifest_value: dict[str, object] | None = None
        manifest: LifeDevelopmentCapabilityManifest | None = None
        if manifest_binding is not None:
            if not isinstance(manifest_binding, dict):
                raise ValueError(
                    "recoverable capability manifest binding is invalid"
                )
            manifest_ref_value = manifest_binding.get("content_ref")
            manifest_hash_value = manifest_binding.get(
                "content_payload_hash"
            )
            if not isinstance(manifest_ref_value, str) or not isinstance(
                manifest_hash_value,
                str,
            ):
                raise ValueError(
                    "recoverable capability manifest binding is incomplete"
                )
            stored_manifest = self._store.read_exact(
                content_ref=manifest_ref_value
            )
            if (
                stored_manifest is None
                or stored_manifest.content_payload_hash
                != manifest_hash_value
                or life_content_payload_hash(stored_manifest.text)
                != manifest_hash_value
            ):
                raise ValueError(
                    "recoverable capability manifest sidecar is unavailable"
                )
            manifest = LifeDevelopmentCapabilityManifest.model_validate_json(
                stored_manifest.text
            )
            manifest_value = json.loads(stored_manifest.text)
            manifest_ref = manifest_ref_value
            manifest_hash = manifest_hash_value
        binding = _RecordedDeliberation(
            role=role,
            capsule_id=terminal.capsule_id,
            context_cursor=cursor,
            request_hashes=tuple(request_hashes),
            response_hashes=tuple(response_hashes),
            raw_content_refs=tuple(raw_content_refs),
            model_result_event_refs=tuple(item.event_ref for item in attempts),
            model_result_event_hashes=tuple(item.event_payload_hash for item in attempts),
            audit_proposal_event_ref=audit_proposal.event_ref,
            audit_proposal_event_hash=audit_proposal.event_payload_hash,
            deliberation_result_id=terminal.deliberation_result_id,
            final_model_result_ref=terminal.model_result_ref,
            context_model_content_hash=model_content_hash,
            context_snapshot_hash=snapshot_hash,
            decision_subject_hash=expected_subject_hash,
            capability_manifest=manifest_value,
            capability_manifest_content_ref=manifest_ref,
            capability_manifest_content_hash=manifest_hash,
        )
        capsule = _PinnedIdentity(
            capsule_id=terminal.capsule_id,
            snapshot_hash=snapshot_hash,
            world_revision=cursor.world_revision,
            deliberation_revision=cursor.deliberation_revision,
            ledger_sequence=cursor.ledger_sequence,
            model_content_json="",
        )
        return (
            stored.text,
            len(attempts) - 1,
            binding,
            capsule,
            manifest,
        )

    def _materialize_content(
        self,
        *,
        proposal_id: str,
        draft: LifeDevelopmentPossibilityDraft,
    ) -> tuple[
        tuple[StoredLifeContent, ...],
        tuple[dict[str, str], ...],
        tuple[OutcomeCandidateDescriptor, ...],
    ]:
        suffix = _digest(proposal_id)
        records: list[StoredLifeContent] = []
        bindings: list[dict[str, str]] = []

        def store_binding(
            *,
            role: str,
            ref: str,
            text: str,
            content_kind: LifeContentKind = "outcome_candidate",
        ) -> None:
            payload_hash = life_content_payload_hash(text)
            records.append(
                StoredLifeContent(
                    content_ref=ref,
                    content_kind=content_kind,
                    content_payload_hash=payload_hash,
                    text=text,
                )
            )
            bindings.append(
                {
                    "role": role,
                    "content_ref": ref,
                    "content_payload_hash": payload_hash,
                }
            )

        premise_ref = f"content:life-development:premise:{suffix}"
        store_binding(role="premise", ref=premise_ref, text=draft.premise)
        candidates: list[OutcomeCandidateDescriptor] = []
        for index, outcome in enumerate(draft.outcomes, start=1):
            outcome_ref = f"content:life-development:outcome:{suffix}:{index}"
            outcome_hash = life_content_payload_hash(outcome.text)
            store_binding(
                role=f"outcome:{index}",
                ref=outcome_ref,
                text=outcome.text,
            )
            provisional: list[ProvisionalNpcIntroductionDescriptor] = []
            for npc_index, npc in enumerate(outcome.provisional_npcs, start=1):
                summary_ref = (
                    f"content:life-development:provisional-npc:{suffix}:{index}:{npc_index}"
                )
                summary_hash = life_content_payload_hash(npc.summary)
                store_binding(
                    role=f"outcome:{index}:provisional_npc:{npc_index}",
                    ref=summary_ref,
                    text=npc.summary,
                    content_kind="provisional_npc_introduction",
                )
                provisional.append(
                    ProvisionalNpcIntroductionDescriptor.create(
                        provisional_entity_ref="provisional:npc:"
                        + _digest(
                            {
                                "world_id": self._ledger.world_id,
                                "proposal_id": proposal_id,
                                "candidate_index": index,
                                "local_ref": npc.local_ref,
                            }
                        ),
                        summary_content_ref=summary_ref,
                        summary_payload_hash=summary_hash,
                        narrative_tags=npc.narrative_tags,
                        privacy_class=npc.privacy_class,
                    )
                )
            dynamic = None
            if outcome.dynamic_life_direction is not None:
                direction = outcome.dynamic_life_direction
                summary_ref = f"content:life-development:dynamic-direction:{suffix}:{index}"
                summary_hash = life_content_payload_hash(direction.summary)
                store_binding(
                    role=f"outcome:{index}:dynamic_life_direction",
                    ref=summary_ref,
                    text=direction.summary,
                    content_kind="dynamic_life_arc_context",
                )
                dynamic = DynamicLifeArcContextDescriptor.create(
                    summary_content_ref=summary_ref,
                    summary_payload_hash=summary_hash,
                    narrative_tags=direction.narrative_tags,
                    duration_days=direction.duration_days,
                    privacy_class=direction.privacy_class,
                )
            candidates.append(
                OutcomeCandidateDescriptor(
                    candidate_result_ref=(f"candidate:life-development:{suffix}:{index}"),
                    result_id=f"result:life-development:{suffix}:{index}",
                    result_payload_ref=(f"content:life-development:result:{suffix}:{index}"),
                    result_payload_hash=outcome_hash,
                    privacy_class=outcome.privacy_class,
                    content_ref=outcome_ref,
                    content_payload_hash=outcome_hash,
                    causal_authority=draft.outcome_resolution_authority,
                    relative_plausibility_weight=outcome.relative_plausibility_weight,
                    provisional_npc_introductions=tuple(provisional),
                    dynamic_life_arc_context=dynamic,
                )
            )
        return tuple(records), tuple(bindings), tuple(candidates)

    @staticmethod
    def _evidence_refs(
        *,
        projection,
        anchor_refs: tuple[str, ...],
        location_authority_refs: tuple[str, ...] = (),
        claim_purpose: Literal["current_fact", "future_plan"],
    ) -> tuple[EvidenceRef, ...]:
        authority = {item.event_id: item for item in projection.committed_world_event_refs}
        return tuple(
            EvidenceRef(
                ref_id=ref,
                evidence_type="committed_world_event",
                claim_purpose=claim_purpose,
                source_world_revision=authority[ref].world_revision,
                immutable_hash=authority[ref].payload_hash,
            )
            for ref in tuple(
                sorted(
                    {
                        *anchor_refs,
                        *(ref for ref in location_authority_refs if ref in authority),
                    }
                )
            )
        )

    @staticmethod
    def _policy_refs(
        *,
        projection,
        location_authority_refs: tuple[str, ...],
    ) -> tuple[str, ...]:
        committed_refs = {item.event_id for item in projection.committed_world_event_refs}
        return tuple(
            sorted(
                {
                    "policy:life-development-v1",
                    *(ref for ref in location_authority_refs if ref not in committed_refs),
                }
            )
        )

    async def _world_author_draft(
        self,
        *,
        context: dict[str, object],
        logical_time: datetime,
        manifest: LifeDevelopmentCapabilityManifest,
    ) -> _LifeDevelopmentModelRun:
        messages = self._world_author_messages(
            context=context,
            logical_time=logical_time,
            manifest=manifest,
        )
        attempts: list[_LifeDevelopmentAttempt] = []
        for ordinal in range(2):
            request_hash = _messages_hash(messages)
            try:
                raw = await self._world_author.complete(messages, temperature=0.6)
            except (
                TimeoutError,
                ConnectionError,
                OSError,
                httpx.HTTPError,
                ValueError,
            ) as exc:
                _LOG.warning("World Author unavailable: %s", exc)
                status, failure_code, outcome = _model_provider_failure(
                    exc,
                    corrective=ordinal > 0,
                )
                if ordinal:
                    first = attempts[0]
                    attempts[0] = _LifeDevelopmentAttempt(
                        request_hash=first.request_hash,
                        raw_output=first.raw_output,
                        status=first.status,
                        failure_code=first.failure_code,
                        slot="primary",
                        outcome="invalid",
                    )
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=None,
                        status=status,
                        failure_code=failure_code,
                        slot="corrective" if ordinal else "primary",
                        outcome=outcome,
                    )
                )
                return _LifeDevelopmentModelRun(
                    model_id=self._world_author_model,
                    parsed=None,
                    attempts=tuple(attempts),
                )
            try:
                parsed = parse_world_author_draft(
                    raw=raw,
                    manifest=manifest,
                    logical_time=logical_time,
                )
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=raw,
                        status=("main_invalid_recovered" if ordinal else "proposal_validated"),
                        failure_code=("main_invalid_output" if ordinal else None),
                    )
                )
                return _LifeDevelopmentModelRun(
                    model_id=self._world_author_model,
                    parsed=parsed,
                    attempts=tuple(attempts),
                )
            except LifeDevelopmentDraftError as exc:
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=raw,
                        status=("recovery_failed" if ordinal else "main_invalid"),
                        failure_code=("corrective_invalid" if ordinal else "main_invalid_output"),
                        slot=("corrective" if ordinal else "primary") if ordinal else None,
                        outcome="invalid" if ordinal else None,
                    )
                )
                if ordinal == 1:
                    _LOG.warning("World Author returned two invalid drafts: %s", exc)
                    first = attempts[0]
                    attempts[0] = _LifeDevelopmentAttempt(
                        request_hash=first.request_hash,
                        raw_output=first.raw_output,
                        status=first.status,
                        failure_code=first.failure_code,
                        slot="primary",
                        outcome="invalid",
                    )
                    return _LifeDevelopmentModelRun(
                        model_id=self._world_author_model,
                        parsed=None,
                        attempts=tuple(attempts),
                    )
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "validation_failure": {
                                    "code": exc.code,
                                    "detail": exc.detail,
                                },
                                "instruction": (
                                    "Return one complete replacement using only the same "
                                    "pinned Context and capability manifest."
                                ),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ]
        raise AssertionError("World Author retry loop did not terminate")

    async def _character_choice(
        self,
        *,
        context: dict[str, object],
        draft: LifeDevelopmentPossibilityDraft,
        offered_window: DueWindow,
    ) -> _LifeDevelopmentModelRun:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Character Model, not the World Author. The external "
                    "opportunity below does not decide what you want. Freely return "
                    "no_op, or accept with your own intention_summary and importance_bp. "
                    "You may narrow timing and select any subset of offered entity refs. "
                    "Do not create new world facts, people, places or outcomes. Return "
                    "exactly JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "pinned_world_context": context,
                        "external_opportunity": draft.model_dump(mode="json"),
                        "executable_envelope": {
                            "opens_at": offered_window.opens_at.isoformat(),
                            "closes_at": offered_window.closes_at.isoformat(),
                            "participant_refs": list(draft.entity_refs),
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]
        attempts: list[_LifeDevelopmentAttempt] = []
        for ordinal in range(2):
            request_hash = _messages_hash(messages)
            try:
                raw = await self._character_model.complete(
                    messages,
                    temperature=0.6,
                )
            except (
                TimeoutError,
                ConnectionError,
                OSError,
                httpx.HTTPError,
                ValueError,
            ) as exc:
                _LOG.warning("Character Model unavailable for life choice: %s", exc)
                status, failure_code, outcome = _model_provider_failure(
                    exc,
                    corrective=ordinal > 0,
                )
                if ordinal:
                    first = attempts[0]
                    attempts[0] = _LifeDevelopmentAttempt(
                        request_hash=first.request_hash,
                        raw_output=first.raw_output,
                        status=first.status,
                        failure_code=first.failure_code,
                        slot="primary",
                        outcome="invalid",
                    )
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=None,
                        status=status,
                        failure_code=failure_code,
                        slot="corrective" if ordinal else "primary",
                        outcome=outcome,
                    )
                )
                return _LifeDevelopmentModelRun(
                    model_id=self._character_model_id,
                    parsed=None,
                    attempts=tuple(attempts),
                )
            try:
                parsed = parse_character_choice(
                    raw=raw,
                    offered=draft,
                    offered_window=offered_window,
                )
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=raw,
                        status=("main_invalid_recovered" if ordinal else "proposal_validated"),
                        failure_code=("main_invalid_output" if ordinal else None),
                    )
                )
                return _LifeDevelopmentModelRun(
                    model_id=self._character_model_id,
                    parsed=parsed,
                    attempts=tuple(attempts),
                )
            except LifeDevelopmentDraftError as exc:
                attempts.append(
                    _LifeDevelopmentAttempt(
                        request_hash=request_hash,
                        raw_output=raw,
                        status=("recovery_failed" if ordinal else "main_invalid"),
                        failure_code=("corrective_invalid" if ordinal else "main_invalid_output"),
                        slot="corrective" if ordinal else None,
                        outcome="invalid" if ordinal else None,
                    )
                )
                if ordinal == 1:
                    _LOG.warning(
                        "Character Model returned two invalid life choices: %s",
                        exc,
                    )
                    first = attempts[0]
                    attempts[0] = _LifeDevelopmentAttempt(
                        request_hash=first.request_hash,
                        raw_output=first.raw_output,
                        status=first.status,
                        failure_code=first.failure_code,
                        slot="primary",
                        outcome="invalid",
                    )
                    return _LifeDevelopmentModelRun(
                        model_id=self._character_model_id,
                        parsed=None,
                        attempts=tuple(attempts),
                    )
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "validation_failure": {
                                    "code": exc.code,
                                    "detail": exc.detail,
                                },
                                "instruction": (
                                    "Return one complete replacement choice within the "
                                    "same opportunity and executable envelope."
                                ),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                ]
        raise AssertionError("Character Model retry loop did not terminate")

    def _world_author_messages(
        self,
        *,
        context: dict[str, object],
        logical_time: datetime,
        manifest: LifeDevelopmentCapabilityManifest,
    ) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You are the World Author, not the Character Model. From the pinned "
                    "World Context, freely author no_op or one source-bound life "
                    "possibility. There is no plot menu. causal_authority must be "
                    "world_contingency for an environmental occurrence or "
                    "character_choice for something the character may choose. An "
                    "environmental contingency cannot give its outcome selection to "
                    "the Character Model; use a recorded world contingency or an "
                    "external observation. You may "
                    "write an ordinary, long-running, pleasant, difficult, or adverse "
                    "premise and 2-4 free outcome texts. Use only manifest-listed "
                    "existing refs. When using a location, copy both its location_ref "
                    "and the exact capability_ref whose privacy and complete time "
                    "window cover the proposal. An outcome may optionally include "
                    "visual_evidence when its settled result would contain concrete "
                    "visible facts. Bind every visual field through visual_evidence."
                    "claim_refs to claims already used by that outcome, and copy the "
                    "authorized location_ref exactly. Omit visual_evidence when the "
                    "outcome has no defensible visual slice; never infer one merely "
                    "because a picture might be appealing. Provisional NPC local refs "
                    "are allowed. "
                    "Author only the life possibilities of the owner_actor_ref named "
                    "in authored_subject. The user and user facts are context that may "
                    "affect that life; never author the user's choices, actions, inner "
                    "state, activities, commitments, or life direction. "
                    "A long direction is allowed only when outcome_resolution_authority "
                    "is character_choice, because only her later choice may establish "
                    "it. Do not decide the character's motive or "
                    "acceptance. For character-caused opportunities only, "
                    "outcome_resolution_authority independently states who may resolve "
                    "later outcomes; it is not implied by participation. "
                    "Return exactly JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "logical_time": logical_time.isoformat(),
                        "pinned_world_context": context,
                        "authored_subject": {
                            "owner_actor_ref": self._owner,
                            "user_authority": "context_only",
                        },
                        "output_contract": {
                            "no_op": {"decision": "no_op"},
                            "propose": LifeDevelopmentPossibilityDraft.model_json_schema(
                                mode="validation"
                            ),
                        },
                        "capability_manifest": {
                            **manifest.model_dump(mode="json"),
                            "manifest_hash": manifest.manifest_hash,
                        },
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]

    def _proposal_event(
        self,
        *,
        proposal_event_id: str,
        proposal_id: str,
        wake: WorldEvent,
        context_cursor: ProjectionCursor,
        capsule,
        manifest: LifeDevelopmentCapabilityManifest,
        draft: LifeDevelopmentWorldDraft,
        raw: str,
        repair_ordinal: int,
        trace_id: str,
        correlation_id: str,
        world_author_deliberation: _RecordedDeliberation,
        effect_kind: str | None = None,
        effect_ref: str | None = None,
        content_bindings: tuple[dict[str, str], ...] = (),
        character_raw: str | None = None,
        character_repair_ordinal: int | None = None,
        final_decision: str | None = None,
        outcome_descriptors: tuple[OutcomeCandidateDescriptor, ...] = (),
        character_choice: CharacterChoiceAcceptDraft | CharacterChoiceNoOpDraft | None = None,
        character_deliberation: _RecordedDeliberation | None = None,
    ) -> WorldEvent:
        if (
            world_author_deliberation.capsule_id != capsule.capsule_id
            or world_author_deliberation.context_cursor != context_cursor
        ):
            raise ValueError("life development Proposal changed the World Author pinned identity")
        self._validate_content_bindings(content_bindings)
        possibility_authority = (
            self._canonical_possibility(
                draft=draft,
                manifest=manifest,
                bindings=content_bindings,
                outcome_descriptors=outcome_descriptors,
            )
            if isinstance(draft, LifeDevelopmentPossibilityDraft)
            else None
        )
        character_choice_authority = self._canonical_character_choice(
            choice=character_choice,
            draft=draft,
            manifest=manifest,
            wake=wake,
            bindings=content_bindings,
        )
        payload = {
            "proposal_id": proposal_id,
            "proposal_kind": "life_development",
            "trigger_id": wake.event_id,
            "evaluated_world_revision": context_cursor.world_revision,
            "decision": final_decision or draft.decision,
            "world_author_decision": draft.decision,
            "causal_authority": getattr(draft, "causal_authority", None),
            "model_role": "world_author",
            "world_author_model": self._world_author_model,
            "world_author_raw_output_hash": _digest(raw),
            "character_model_role": ("character_model" if character_raw is not None else None),
            "character_model": (self._character_model_id if character_raw is not None else None),
            "character_raw_output_hash": (
                _digest(character_raw) if character_raw is not None else None
            ),
            "repair_ordinal": repair_ordinal,
            "character_repair_ordinal": character_repair_ordinal,
            "world_author_deliberation": (world_author_deliberation.authority_payload()),
            "world_author_deliberation_hash": _digest(
                world_author_deliberation.authority_payload()
            ),
            "character_deliberation": (
                character_deliberation.authority_payload()
                if character_deliberation is not None
                else None
            ),
            "character_deliberation_hash": (
                _digest(character_deliberation.authority_payload())
                if character_deliberation is not None
                else None
            ),
            "context_identity_version": "life-development-context.1",
            "context_capsule_id": capsule.capsule_id,
            "context_model_content_hash": (world_author_deliberation.context_model_content_hash),
            "context_snapshot_hash": (world_author_deliberation.context_snapshot_hash),
            "context_cursor": context_cursor.model_dump(mode="json"),
            "capability_manifest_version": manifest.version,
            "capability_manifest_hash": manifest.manifest_hash,
            "possibility_authority_version": (
                "life-development-possibility.3" if possibility_authority is not None else None
            ),
            "possibility_authority": possibility_authority,
            "possibility_authority_hash": (
                _digest(possibility_authority) if possibility_authority is not None else None
            ),
            "character_choice": character_choice_authority,
            "character_choice_hash": (
                _digest(character_choice_authority)
                if character_choice_authority is not None
                else None
            ),
            "content_bindings": list(content_bindings),
            "effect_kind": effect_kind,
            "effect_ref": effect_ref,
        }
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=proposal_event_id,
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:life-development",
            trace_id=trace_id or wake.trace_id,
            causation_id=wake.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ProposalRecorded",
                    world_id=self._ledger.world_id,
                    payload=payload,
                )
                or "life-development-proposal:" + _digest(proposal_id)
            ),
            payload=payload,
        )

    def _canonical_possibility(
        self,
        *,
        draft: LifeDevelopmentPossibilityDraft,
        manifest: LifeDevelopmentCapabilityManifest,
        bindings: tuple[dict[str, str], ...],
        outcome_descriptors: tuple[OutcomeCandidateDescriptor, ...],
    ) -> dict[str, object]:
        if len(outcome_descriptors) != len(draft.outcomes):
            raise ValueError("canonical life possibility requires every outcome descriptor")
        binding_by_role = {item["role"]: item for item in bindings}
        premise = binding_by_role.get("premise")
        if premise is None:
            raise ValueError("canonical life possibility requires premise sidecar")
        location_capability = self._selected_location_capability(
            draft=draft,
            manifest=manifest,
        )
        return {
            "authored_subject_ref": draft.authored_subject_ref,
            "causal_authority": draft.causal_authority,
            "outcome_resolution_authority": draft.outcome_resolution_authority,
            "premise": {
                "content_ref": premise["content_ref"],
                "content_payload_hash": premise["content_payload_hash"],
                "claim_refs": list(draft.premise_claim_refs),
            },
            "claim_declarations": [
                item.model_dump(mode="json") for item in draft.claim_declarations
            ],
            "timing": draft.timing.model_dump(mode="json"),
            "anchor_refs": list(draft.anchor_refs),
            "location_ref": draft.location_ref,
            "location_capability_ref": draft.location_capability_ref,
            "location_capability": (
                location_capability.model_dump(
                    mode="json",
                    exclude={"capability_ref"},
                )
                if location_capability is not None
                else None
            ),
            "entity_refs": list(draft.entity_refs),
            "privacy_class": draft.privacy_class,
            "outcomes": [
                {
                    "experienced_by_ref": outcome.experienced_by_ref,
                    "claim_refs": list(outcome.claim_refs),
                    "descriptor": descriptor.model_dump(mode="json"),
                    "visual_evidence": (
                        outcome.visual_evidence.model_dump(mode="json")
                        if outcome.visual_evidence is not None
                        else None
                    ),
                }
                for outcome, descriptor in zip(
                    draft.outcomes,
                    outcome_descriptors,
                    strict=True,
                )
            ],
        }

    @staticmethod
    def _selected_location_capability(
        *,
        draft: LifeDevelopmentPossibilityDraft,
        manifest: LifeDevelopmentCapabilityManifest,
    ) -> LifeDevelopmentLocationCapability | None:
        if draft.location_ref is None:
            return None
        capability = next(
            (
                item
                for item in manifest.location_capabilities
                if item.location_ref == draft.location_ref
                and item.capability_ref == draft.location_capability_ref
            ),
            None,
        )
        if capability is None:
            raise ValueError("life-development Proposal lost its selected location capability")
        return capability

    def _canonical_character_choice(
        self,
        *,
        choice: CharacterChoiceAcceptDraft | CharacterChoiceNoOpDraft | None,
        draft: LifeDevelopmentWorldDraft,
        manifest: LifeDevelopmentCapabilityManifest,
        wake: WorldEvent,
        bindings: tuple[dict[str, str], ...],
    ) -> dict[str, object] | None:
        if choice is None:
            return None
        if isinstance(choice, CharacterChoiceNoOpDraft):
            return {"decision": "no_op"}
        if not isinstance(draft, LifeDevelopmentPossibilityDraft):
            raise ValueError("Character choice requires a possibility")
        intention = next(
            (item for item in bindings if item["role"] == "character_intention"),
            None,
        )
        if intention is None:
            raise ValueError("accepted Character choice requires intention sidecar")
        offered = draft.timing.resolve(
            logical_time=wake.logical_time,
            manifest=manifest,
        )
        return {
            "decision": "accept",
            "intention": {
                "content_ref": intention["content_ref"],
                "content_payload_hash": intention["content_payload_hash"],
            },
            "importance_bp": choice.importance_bp,
            "opens_at": (choice.opens_at or offered.opens_at).isoformat(),
            "closes_at": (choice.closes_at or offered.closes_at).isoformat(),
            "participant_refs": list(choice.participant_refs),
        }

    def _validate_content_bindings(
        self,
        bindings: tuple[dict[str, str], ...],
    ) -> None:
        roles = tuple(item["role"] for item in bindings)
        refs = tuple(item["content_ref"] for item in bindings)
        if len(roles) != len(set(roles)) or len(refs) != len(set(refs)):
            raise ValueError("life development sidecar bindings must be unique")
        for binding in bindings:
            stored = self._store.read_exact(content_ref=binding["content_ref"])
            if stored is None or stored.content_payload_hash != binding["content_payload_hash"]:
                raise ValueError("life development sidecar binding is unavailable or changed")

    def _exact_wake(self, *, projection, wake_event_ref: str) -> WorldEvent | None:
        ref = next(
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == wake_event_ref
            ),
            None,
        )
        located = self._ledger.lookup_event_commit(wake_event_ref)
        if (
            ref is None
            or ref.event_type != "ClockAdvanced"
            or located is None
            or located[0].event_type != ref.event_type
            or located[0].payload_hash != ref.payload_hash
            or located[0].logical_time != ref.logical_time
            or located[0].world_id != self._ledger.world_id
            or located[0].event_id not in located[1].event_ids
        ):
            return None
        return located[0]

    @staticmethod
    def _recovered_result(event: WorldEvent) -> LifeDevelopmentResult:
        payload = event.payload()
        if payload.get("decision") == "no_op":
            return LifeDevelopmentResult(
                status="no_op",
                reason_code="life_development.world_author_no_op_recovered",
                proposal_event_ref=event.event_id,
            )
        if payload.get("effect_kind") == "world_occurrence":
            return LifeDevelopmentResult(
                status="occurrence_committed",
                reason_code="life_development.world_contingency_recovered",
                proposal_event_ref=event.event_id,
                occurrence_id=payload.get("effect_ref"),
            )
        if payload.get("effect_kind") == "character_plan":
            return LifeDevelopmentResult(
                status="plan_committed",
                reason_code="life_development.character_plan_recovered",
                proposal_event_ref=event.event_id,
                plan_id=payload.get("effect_ref"),
            )
        return LifeDevelopmentResult(
            status="technical_failure",
            reason_code="life_development.proposal_audit_incomplete",
            proposal_event_ref=event.event_id,
        )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _messages_hash(messages: list[dict[str, str]]) -> str:
    return _digest(messages)


def _model_provider_failure(
    exc: Exception,
    *,
    corrective: bool,
) -> tuple[_AttemptStatus, str, _AttemptOutcome]:
    timeout = isinstance(exc, (TimeoutError, httpx.TimeoutException))
    if corrective:
        return (
            "recovery_failed",
            "corrective_timeout" if timeout else "corrective_exception",
            "timeout" if timeout else "exception",
        )
    return (
        "main_timeout" if timeout else "main_exception",
        "main_timeout" if timeout else "main_exception",
        "timeout" if timeout else "exception",
    )


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _capsule_cursor(capsule) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=capsule.world_revision,
        deliberation_revision=capsule.deliberation_revision,
        ledger_sequence=capsule.ledger_sequence,
    )


__all__ = [
    "LifeDevelopmentModel",
    "LifeDevelopmentPlanMaterial",
    "LifeDevelopmentProposalReader",
    "LifeDevelopmentReadableOutcome",
    "LifeDevelopmentResult",
    "LifeDevelopmentRuntime",
]
