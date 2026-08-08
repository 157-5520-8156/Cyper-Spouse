"""One CharacterInterior lane for committed life, silence, and plan stimuli.

The module preserves the three historical durable trigger identities while
removing their three independent semantic authors.  Scheduling, claims, CAS,
typed Appraisal authority and downstream Affect opening remain deterministic;
only the character decides whether a pinned stimulus changes her private
appraisal and what that appraisal means.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
import time
from typing import Protocol

from ..affect_target_bounds import (
    STANDARD_DECAY_OBJECT_REF,
    STANDARD_DECAY_SCHEMA_VERSION,
    STANDARD_RESIDUE_OBJECT_REF,
    STANDARD_RESIDUE_SCHEMA_VERSION,
    lower_bounds_from_projection,
)
from ..aspiration_events import (
    ASPIRATION_POLICY_REF,
    AspirationAbandonedPayload,
    AspirationPlantedPayload,
    AspirationReinforcedPayload,
    AspirationRevisedPayload,
)
from ..event_identity import domain_idempotency_key
from ..errors import ConcurrencyConflict
from ..immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from ..perception import PerceptionResultAcceptedPayload, perception_result_trigger_id
from ..perception_result_context import PerceptionResultReader
from ..proposal_audit_schemas import (
    ModelResultRecordedPayload,
    RecordedCharacterInteriorTurnLineage,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from ..proposal_envelope import (
    AspirationTransitionPayload,
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
    validate_proposal_envelope,
)
from ..schemas import (
    AffectProposalProjection,
    AspirationProjection,
    ClaimLease,
    ExperienceOccurrenceSettlementBinding,
    ExperienceProjection,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)
from ..relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from ..relationship_proposal_compiler import RelationshipProposalCompiler
from ..schema_core import EvidenceRef, FrozenModel
from .contracts import (
    InteriorAffectOpenTransition,
    InteriorAffectResolveTransition,
    InteriorAffectSupersedeTransition,
    InteriorAffectUpdateTransition,
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
    InteriorStimulus,
)
from .run_result import CharacterInteriorRunResult
from .core import CharacterInterior
from .experience_transitions import (
    ExperienceTransitionSettlement,
    ExperienceTransitionCapability,
    build_experience_transition_capability,
    materialize_experience_transition_change,
    validate_experience_transition_draft,
)
from .ports import _AuthorityRequest
from .relationship_context import relationship_transition_subject_refs
from .structured_role import _WorldStimulusAppraisalResult


_LOG = logging.getLogger(__name__)


PURPOSE = "world_stimulus_appraisal"
PROPOSAL_TYPE = "world_stimulus_appraisal_result"
PAYLOAD_CONTRACT = "character-interior-world-stimulus-appraisal-result.1"
_PROCESS_PRIORITY = (
    "perception_result_deliberation",
    "npc_world_appraisal",
    "silence_appraisal",
    "plan_disruption_appraisal",
    "life_reflection",
)
_SOURCE_EVENT_TYPE = {
    "perception_result_deliberation": "PerceptionResultAccepted",
    "npc_world_appraisal": "WorldOccurrenceSettled",
    "silence_appraisal": "ExecutionReceiptRecorded",
    "plan_disruption_appraisal": "ActivityAbandoned",
    "life_reflection": "AppraisalAccepted",
}
_STIMULUS_KIND = {
    "perception_result_deliberation": "attended_perception_result",
    "npc_world_appraisal": "settled_world_occurrence",
    "silence_appraisal": "unanswered_visible_expression",
    "plan_disruption_appraisal": "abandoned_activity_plan",
    "life_reflection": "settled_world_occurrence_reflection",
}
_EVIDENCE_KIND = {
    "perception_result_deliberation": "committed_world_event",
    "npc_world_appraisal": "settled_world_event",
    "silence_appraisal": "committed_world_event",
    "plan_disruption_appraisal": "committed_world_event",
    "life_reflection": "committed_world_event",
}

_PERCEPTION_RESULT_CONTEXT_LIMIT = 720
_TECHNICAL_FAILURE_DEFER_SECONDS = 30.0


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _cursor(projection: object) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _perception_result_material(
    *,
    source_event: WorldEvent,
    reader: PerceptionResultReader | None,
) -> dict[str, object]:
    """Resolve a provider descriptor without promoting it to a World fact."""

    if source_event.event_type != "PerceptionResultAccepted" or reader is None:
        raise ValueError("perception result content authority is unavailable")
    result = PerceptionResultAcceptedPayload.model_validate_json(source_event.payload_json).result
    content = reader.read_exact(result_ref=result.result_ref)
    if (
        content is None
        or content.result_ref != result.result_ref
        or content.result_hash != result.result_hash
        or "sha256:" + hashlib.sha256(content.text.encode("utf-8")).hexdigest()
        != result.result_hash
    ):
        raise ValueError("perception result content does not bind its accepted descriptor")
    text = content.text[:_PERCEPTION_RESULT_CONTEXT_LIMIT]
    return {
        "kind": "external_perception_descriptor",
        "epistemic_status": "provider_observation_not_world_fact",
        "result_id": result.result_id,
        "request_id": result.request_id,
        "analysis_kind": result.analysis_kind,
        "content_privacy_class": result.content_privacy_class,
        "accepted_at": result.accepted_at.isoformat(),
        "result_ref": result.result_ref,
        "result_hash": result.result_hash,
        "receipt_event_ref": result.receipt_event_ref,
        "receipt_event_payload_hash": result.receipt_event_payload_hash,
        "text": text,
        "truncated": text != content.text,
    }


def _active_aspiration_capability(
    projection: object,
    *,
    actor_ref: str,
) -> list[dict[str, object]]:
    """Exact active heads offered as capability, never as a direction menu."""

    active = sorted(
        (
            item
            for item in projection.aspirations
            if item.owner_actor_ref == actor_ref and item.status == "active"
        ),
        key=lambda item: (
            item.last_revised_at or item.last_reinforced_at or item.planted_at,
            item.aspiration_id,
        ),
        reverse=True,
    )[:16]
    return [
        {
            "aspiration_id": item.aspiration_id,
            "entity_revision": item.entity_revision,
            "text": item.text,
            "privacy_class": item.privacy_class,
            "planted_event_ref": item.planted_event_ref,
            "authority_source_ref": item.revision_event_ref or item.planted_event_ref,
            "origin_kind": item.origin_kind,
            "tension_summary": item.tension_summary,
            "tension_source_refs": list(item.tension_source_refs),
        }
        for item in active
    ]


def _active_affect_capability(projection: object) -> list[dict[str, object]]:
    """Expose exact mutable Affect heads without suggesting a transition."""

    baseline_by_dimension = {
        item.dimension: item.baseline_bp for item in projection.affect_baselines
    }
    active = sorted(
        (item for item in projection.affect_episodes if item.status == "active"),
        key=lambda item: (item.updated_at, item.episode_id),
        reverse=True,
    )[:16]
    return [
        {
            "episode_id": episode.episode_id,
            "entity_revision": episode.entity_revision,
            "origin_event_ref": episode.origin.accepted_event_ref,
            "opened_at": episode.opened_at.isoformat(),
            "updated_at": episode.updated_at.isoformat(),
            "components": [
                {
                    "component_id": component.component_id,
                    "dimension": component.dimension,
                    "current_intensity_bp": component.intensity_bp,
                    "minimum_target_intensity_bp": max(
                        component.decay_profile.floor_bp,
                        component.residue_bp,
                        baseline_by_dimension.get(component.dimension, 0),
                    ),
                    "source_cluster_ref": component.source_cluster_ref,
                }
                for component in episode.components
            ],
        }
        for episode in active
    ]


@dataclass(frozen=True, slots=True)
class _PreparedWorldStimulusAudit:
    request: _AuthorityRequest
    source_event: WorldEvent
    decision: DecisionProposal
    lineage: _InteriorAuthorLineage


class _WorldStimulusRelationshipSettlement(Protocol):
    """Optional deterministic consumer of this exact authored audit."""

    @property
    def ledger(self) -> object: ...

    async def process(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> object: ...

    def is_pending(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> bool: ...


class _WorldStimulusRelationshipWorkResult(FrozenModel):
    status: str
    source_proposal_id: str
    typed_proposal_id: str | None = None
    replayed: bool = False


class _WorldStimulusAspirationWorkResult(FrozenModel):
    status: str
    source_proposal_id: str
    event_ref: str | None = None
    replayed: bool = False


class _WorldStimulusRelationshipSignalSettlement:
    """Deterministically settle one signal from the same CharacterInterior audit.

    There is intentionally no model port here.  The source purpose has already
    authored and validated the optional signal; this object only re-proves its
    durable source/capability closure, records typed authority and accepts it.
    """

    def __init__(
        self,
        *,
        ledger: object,
        compiler: RelationshipProposalCompiler,
        acceptance: RelationshipAcceptanceRuntime,
        owner_id: str,
        source: str = "world-v2:character-interior-world-relationship",
    ) -> None:
        if not owner_id:
            raise ValueError("world stimulus relationship settlement needs an owner")
        if compiler.ledger is not ledger or acceptance.ledger is not ledger:
            raise ValueError("world stimulus relationship dependencies must own the exact ledger")
        self._ledger = ledger
        self._compiler = compiler
        self._acceptance = acceptance
        self._owner_id = owner_id
        self._source = source

    @property
    def ledger(self) -> object:
        return self._ledger

    async def process(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> _WorldStimulusRelationshipWorkResult:
        accepted = await self._accepted_descendant(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
            source_event=source_event,
        )
        if accepted is not None:
            return _WorldStimulusRelationshipWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=accepted,
                replayed=True,
            )
        compiled = await self._compile(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
            source_event=source_event,
        )
        if compiled.status == "no_change":
            return _WorldStimulusRelationshipWorkResult(
                status="no_change",
                source_proposal_id=proposal_id,
            )
        if compiled.typed_proposal_id is None or compiled.acceptance_cursor is None:
            raise RuntimeError("world stimulus relationship candidate is incomplete")

        def accept():
            return self._acceptance.accept_runtime_owned(
                handle=self._acceptance.pin_proposal(
                    cursor=compiled.acceptance_cursor,
                    proposal_id=compiled.typed_proposal_id,
                ),
                actor=self._owner_id,
                source=self._source,
            )

        try:
            if self._ledger.blocks_event_loop:
                await asyncio.to_thread(accept)
            else:
                accept()
        except ConcurrencyConflict:
            head = await self._current_cursor()
            accepted = await self._accepted_descendant(
                world_id=world_id,
                audit_cursor=audit_cursor,
                current_cursor=head,
                proposal_id=proposal_id,
                source_event=source_event,
            )
            if accepted is None:
                raise
            return _WorldStimulusRelationshipWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                typed_proposal_id=accepted,
                replayed=True,
            )
        return _WorldStimulusRelationshipWorkResult(
            status="accepted",
            source_proposal_id=proposal_id,
            typed_proposal_id=compiled.typed_proposal_id,
        )

    def is_pending(
        self,
        *,
        world_id: str,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> bool:
        accepted = self._compiler.accepted_world_stimulus_descendant(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
            source_event_id=source_event.event_id,
        )
        if accepted is not None:
            return False
        return self._compiler.world_stimulus_signal_present(
            world_id=world_id,
            audit_cursor=audit_cursor,
            current_cursor=current_cursor,
            proposal_id=proposal_id,
            source_event_id=source_event.event_id,
        )

    async def _compile(self, **kwargs):  # type: ignore[no-untyped-def]
        source_event = kwargs.pop("source_event")

        def run():
            return self._compiler.record_world_stimulus_rebased(
                **kwargs,
                source_event_id=source_event.event_id,
            )

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(run)
        return run()

    async def _accepted_descendant(self, **kwargs):  # type: ignore[no-untyped-def]
        source_event = kwargs.pop("source_event")

        def run():
            return self._compiler.accepted_world_stimulus_descendant(
                **kwargs,
                source_event_id=source_event.event_id,
            )

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(run)
        return run()

    async def _current_cursor(self) -> ProjectionCursor:
        projection = (
            await asyncio.to_thread(self._ledger.project)
            if self._ledger.blocks_event_loop
            else self._ledger.project()
        )
        return _cursor(projection)


class _WorldStimulusInteriorAuthorityHandler:
    """Persist one role-authored result in the existing proposal authority."""

    proposal_type = PROPOSAL_TYPE

    def __init__(
        self,
        *,
        ledger: object,
        owner_id: str,
        perception_result_reader: PerceptionResultReader | None = None,
        source: str = "world-v2:character-interior-world-stimulus-authority",
    ) -> None:
        if not owner_id:
            raise ValueError("world stimulus authority needs an owner")
        self._ledger = ledger
        self._owner_id = owner_id
        self._perception_result_reader = perception_result_reader
        self._source = source

    async def prepare(
        self,
        request: _AuthorityRequest,
        proposal: dict[str, object],
    ) -> object:
        manifest = request.capability_manifest
        if (
            request.purpose != PURPOSE
            or manifest is None
            or manifest.capability_kind != PURPOSE
            or proposal.get("contract") != "character-interior-typed-proposal.1"
            or proposal.get("proposal_type") != PROPOSAL_TYPE
            or proposal.get("purpose") != PURPOSE
            or proposal.get("capability_ref") != manifest.capability_ref
            or proposal.get("capability_payload_hash") != manifest.payload_hash
            or proposal.get("source_refs") != list(manifest.source_refs)
        ):
            raise ValueError("world stimulus proposal authority binding is invalid")
        payload = proposal.get("payload")
        if not isinstance(payload, dict) or payload.get("contract") != PAYLOAD_CONTRACT:
            raise ValueError("world stimulus proposal payload is invalid")
        raw = dict(payload)
        raw.pop("contract")
        raw["proposal_type"] = PROPOSAL_TYPE
        result = _WorldStimulusAppraisalResult.model_validate(raw)
        if len(manifest.source_refs) != 1:
            raise ValueError("world stimulus capability must bind one source event")
        source_ref = manifest.source_refs[0]
        if request.trigger_ref != source_ref or request.subject_source_refs != (source_ref,):
            raise ValueError("world stimulus InnerTurn is not bound to its source")
        located = await self._lookup(source_ref)
        if located is None:
            raise ValueError("world stimulus source event is unavailable")
        source_event, source_commit = located
        pinned = await self._project_at(request.cursor)
        source_authority = next(
            (item for item in pinned.committed_world_event_refs if item.event_id == source_ref),
            None,
        )
        source_view = manifest.payload.get("source_event")
        process_kind = manifest.payload.get("process_kind")
        if (
            not isinstance(source_view, dict)
            or process_kind not in _PROCESS_PRIORITY
            or source_event.event_type != _SOURCE_EVENT_TYPE[process_kind]
            or source_view
            != {
                "event_id": source_event.event_id,
                "event_type": source_event.event_type,
                "logical_time": source_event.logical_time.isoformat(),
                "payload": json.loads(source_event.payload_json),
                "payload_hash": source_event.payload_hash,
            }
            or source_commit.world_revision > request.cursor.world_revision
            or source_commit.ledger_sequence > request.cursor.ledger_sequence
            or source_authority is None
            or source_authority.event_type != source_event.event_type
            or source_authority.payload_hash != source_event.payload_hash
        ):
            raise ValueError("world stimulus source material is not exact authority")
        expected_perception_result = (
            _perception_result_material(
                source_event=source_event,
                reader=self._perception_result_reader,
            )
            if process_kind == "perception_result_deliberation"
            else None
        )
        if manifest.payload.get("perception_result") != expected_perception_result:
            raise ValueError("world stimulus perception material is not exact authority")
        if expected_perception_result is not None:
            receipt_ref = expected_perception_result["receipt_event_ref"]
            receipt = next(
                (
                    item
                    for item in pinned.committed_world_event_refs
                    if item.event_id == receipt_ref
                ),
                None,
            )
            if (
                receipt is None
                or receipt.event_type != "ExecutionReceiptRecorded"
                or receipt.payload_hash != expected_perception_result["receipt_event_payload_hash"]
            ):
                raise ValueError("world stimulus perception receipt is not pinned authority")
        pinned_affect_bounds = lower_bounds_from_projection(pinned)
        if manifest.payload.get("affect_target_lower_bounds") != pinned_affect_bounds.model_dump(
            mode="json"
        ):
            raise ValueError("world stimulus Affect capability is not cursor exact")
        active_affect_heads = manifest.payload.get("active_affect_heads")
        if active_affect_heads != _active_affect_capability(pinned):
            raise ValueError("world stimulus active Affect heads are not cursor exact")
        relationship_subject_refs = manifest.payload.get("relationship_subject_refs")
        known_relationship_subjects = relationship_transition_subject_refs(
            projection=pinned,
            source_event=source_event,
        )
        if not isinstance(relationship_subject_refs, list) or relationship_subject_refs not in (
            [],
            list(known_relationship_subjects),
        ):
            raise ValueError("world stimulus relationship capability is not cursor exact")
        active_aspirations = manifest.payload.get("active_aspirations")
        if active_aspirations != _active_aspiration_capability(
            pinned,
            actor_ref=request.actor_ref,
        ):
            raise ValueError("world stimulus aspiration capability is not cursor exact")
        expected_experience_capability = build_experience_transition_capability(
            projection=pinned,
            actor_ref=request.actor_ref,
            source_event=source_event,
        )
        raw_experience_capability = manifest.payload.get("experience_transitions")
        try:
            experience_capability = ExperienceTransitionCapability.model_validate_json(
                _canonical(raw_experience_capability)
            )
        except ValueError as exc:
            raise ValueError("world stimulus experience capability is invalid") from exc
        if experience_capability != expected_experience_capability:
            raise ValueError("world stimulus experience capability is not cursor exact")
        lineage = request.author_lineage
        if lineage is None:
            raise ValueError("world stimulus proposal lacks character author lineage")
        considered_at = manifest.payload.get("considered_at")
        if not isinstance(considered_at, str):
            raise ValueError("world stimulus capability lacks authoritative time")
        at = datetime.fromisoformat(considered_at)
        if result.expiry is not None and result.expiry <= at:
            raise ValueError("world stimulus appraisal expiry must be future")
        affect_transition = result.affect_transition
        if affect_transition is not None:
            heads_by_id = {
                item["episode_id"]: item
                for item in active_affect_heads
                if isinstance(item, dict) and isinstance(item.get("episode_id"), str)
            }
            if isinstance(affect_transition, InteriorAffectOpenTransition):
                for target in affect_transition.component_targets:
                    if target.target_intensity_bp < pinned_affect_bounds.minimum_for(
                        target.dimension
                    ):
                        raise ValueError("world stimulus Affect target is outside authority")
            else:
                head = heads_by_id.get(affect_transition.episode_id)
                if head is None:
                    raise ValueError("world stimulus Affect target head is outside authority")
                if isinstance(affect_transition, InteriorAffectUpdateTransition):
                    components = {
                        item["component_id"]: item
                        for item in head["components"]
                        if isinstance(item, dict) and isinstance(item.get("component_id"), str)
                    }
                    for target in affect_transition.component_targets:
                        offered = components.get(target.component_id)
                        if (
                            offered is None
                            or offered.get("dimension") != target.dimension
                            or target.target_intensity_bp
                            < offered.get("minimum_target_intensity_bp", 10_001)
                        ):
                            raise ValueError(
                                "world stimulus Affect component target is outside authority"
                            )
                elif isinstance(affect_transition, InteriorAffectSupersedeTransition):
                    for target in affect_transition.component_targets:
                        if target.target_intensity_bp < pinned_affect_bounds.minimum_for(
                            target.dimension
                        ):
                            raise ValueError(
                                "world stimulus Affect successor target is outside authority"
                            )
        if (
            result.relationship_signal is not None
            and result.relationship_signal.subject_ref not in relationship_subject_refs
        ):
            raise ValueError("world stimulus relationship subject is outside authority")
        if result.aspiration_transition is not None:
            transition = result.aspiration_transition
            active_by_id = {item["aspiration_id"]: item for item in active_aspirations}
            selected_sources = set(transition.source_refs)
            offered_sources = (
                {source_ref}
                | {str(item["authority_source_ref"]) for item in active_aspirations}
                | {str(item["planted_event_ref"]) for item in active_aspirations}
            )
            if source_ref not in selected_sources or not selected_sources.issubset(offered_sources):
                raise ValueError("world stimulus aspiration evidence is outside authority")
            if transition.operation != "plant":
                selected = active_by_id.get(transition.aspiration_id)
                if (
                    selected is None
                    or selected["authority_source_ref"] not in selected_sources
                    or selected["planted_event_ref"] not in selected_sources
                ):
                    raise ValueError("world stimulus aspiration target is outside authority")
        if result.experience_transition is not None:
            validate_experience_transition_draft(
                result.experience_transition,
                capability=experience_capability,
            )
        identity = _digest(
            {
                "contract": PAYLOAD_CONTRACT,
                "inner_turn_id": request.inner_turn_id,
                "source_ref": source_ref,
                "payload": result.model_dump(mode="json"),
            }
        )
        evidence_refs = [source_ref]
        if result.aspiration_transition is not None:
            evidence_refs.extend(result.aspiration_transition.source_refs)
        if result.experience_transition is not None:
            evidence_refs.extend(result.experience_transition.source_refs)
        evidence_refs = list(dict.fromkeys(evidence_refs))
        committed_by_ref = {item.event_id: item for item in pinned.committed_world_event_refs}
        evidence: list[ProposalEvidenceRef] = []
        for ref in evidence_refs:
            authority = committed_by_ref.get(ref)
            if authority is None:
                raise ValueError("world stimulus aspiration evidence is not committed")
            evidence.append(
                ProposalEvidenceRef(
                    ref_id=ref,
                    evidence_kind=(
                        _EVIDENCE_KIND[process_kind]
                        if ref == source_ref
                        else "committed_world_event"
                    ),
                    source_world_revision=authority.world_revision,
                    immutable_hash="sha256:" + authority.payload_hash,
                )
            )
        changes: list[TypedChange] = []
        if result.decision == "activate":
            appraisal_change_id = f"change:character-interior-world-stimulus:appraisal:{identity}"
            appraisal_id = f"appraisal:character-interior-world-stimulus:{identity}"
            changes.append(
                TypedChange(
                    change_id=appraisal_change_id,
                    kind="appraisal_transition",
                    target_id=appraisal_id,
                    transition="activate",
                    expected_entity_revision=0,
                    evidence_refs=(source_ref,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="appraisal_transition.v1",
                        value={
                            "appraisal_id": appraisal_id,
                            "meaning_candidates": [
                                item.model_dump(mode="json")
                                for item in result.meaning_candidates or ()
                            ],
                            "attribution": result.attribution,
                            "severity": result.severity,
                            "confidence": result.confidence,
                            "expiry": (
                                result.expiry.isoformat() if result.expiry is not None else None
                            ),
                        },
                    ),
                )
            )
            if result.affect_transition is not None:
                affect_transition = result.affect_transition
                affect_change_id = f"change:character-interior-world-stimulus:affect:{identity}"
                if isinstance(affect_transition, InteriorAffectOpenTransition):
                    affect_episode_id = "affect:character-interior-world-stimulus:" + identity
                    expected_affect_revision = 0
                else:
                    affect_episode_id = affect_transition.episode_id
                    selected_head = next(
                        item
                        for item in active_affect_heads
                        if item["episode_id"] == affect_episode_id
                    )
                    expected_affect_revision = int(selected_head["entity_revision"])
                affect_payload: dict[str, object] = {
                    "episode_id": affect_episode_id,
                    "appraisal_change_refs": [appraisal_change_id],
                }
                if isinstance(affect_transition, InteriorAffectResolveTransition):
                    affect_payload["resolution_summary"] = affect_transition.resolution_summary
                else:
                    affect_payload["component_targets"] = [
                        item.model_dump(mode="json") for item in affect_transition.component_targets
                    ]
                if isinstance(
                    affect_transition,
                    (InteriorAffectOpenTransition, InteriorAffectSupersedeTransition),
                ):
                    affect_payload.update(
                        {
                            "decay_config": {
                                "object_ref": STANDARD_DECAY_OBJECT_REF,
                                "schema_version": STANDARD_DECAY_SCHEMA_VERSION,
                                "payload_hash": "sha256:" + _digest(STANDARD_DECAY_OBJECT_REF),
                            },
                            "residue_config": {
                                "object_ref": STANDARD_RESIDUE_OBJECT_REF,
                                "schema_version": STANDARD_RESIDUE_SCHEMA_VERSION,
                                "payload_hash": "sha256:" + _digest(STANDARD_RESIDUE_OBJECT_REF),
                            },
                        }
                    )
                changes.append(
                    TypedChange(
                        change_id=affect_change_id,
                        kind="affect_transition",
                        target_id=affect_episode_id,
                        transition=affect_transition.operation,
                        expected_entity_revision=expected_affect_revision,
                        evidence_refs=(source_ref,),
                        payload=CanonicalTypedPayload.from_value(
                            payload_schema="affect_transition.v1",
                            value=affect_payload,
                        ),
                    )
                )
            if result.relationship_signal is not None:
                signal = result.relationship_signal
                changes.append(
                    TypedChange(
                        change_id=(
                            "change:character-interior-world-stimulus:relationship:" + identity
                        ),
                        kind="relationship_signal",
                        target_id=(
                            "relationship-signal:character-interior-world-stimulus:" + identity
                        ),
                        transition="suggest",
                        expected_entity_revision=0,
                        evidence_refs=(source_ref,),
                        payload=CanonicalTypedPayload.from_value(
                            payload_schema="relationship_signal.v1",
                            value=signal.model_dump(mode="json"),
                        ),
                    )
                )
        if result.aspiration_transition is not None:
            aspiration = result.aspiration_transition
            existing = {
                item.aspiration_id: item
                for item in pinned.aspirations
                if item.owner_actor_ref == request.actor_ref and item.status == "active"
            }
            current = (
                existing.get(aspiration.aspiration_id)
                if aspiration.aspiration_id is not None
                else None
            )
            target_id = (
                current.aspiration_id
                if current is not None
                else f"aspiration:character-interior:{identity}"
            )
            changes.append(
                TypedChange(
                    change_id=("change:character-interior-world-stimulus:aspiration:" + identity),
                    kind="aspiration_transition",
                    target_id=target_id,
                    transition=aspiration.operation,
                    expected_entity_revision=(
                        current.entity_revision if current is not None else 0
                    ),
                    evidence_refs=tuple(aspiration.source_refs),
                    policy_refs=(ASPIRATION_POLICY_REF,),
                    payload=CanonicalTypedPayload.from_value(
                        payload_schema="aspiration_transition.v1",
                        value=aspiration.model_dump(mode="json"),
                    ),
                )
            )
        if result.experience_transition is not None:
            changes.append(
                materialize_experience_transition_change(
                    result.experience_transition,
                    capability=experience_capability,
                    projection=pinned,
                    identity=identity,
                )
            )
        decision = DecisionProposal(
            proposal_id=f"proposal:character-interior-world-stimulus:{identity}",
            trigger_ref=source_ref,
            evaluated_world_revision=request.cursor.world_revision,
            evidence_refs=tuple(evidence) if changes else (),
            proposed_changes=tuple(changes),
            action_intents=(),
            confidence=result.confidence,
            brief_rationale=result.brief_rationale,
            affect_decision=("propose" if result.affect_transition is not None else "no_change"),
            behavior_tendency=result.behavior_tendency,
            stance=result.stance,
            display_strategy=result.display_strategy,
            timing_choice="silent",
        )
        return _PreparedWorldStimulusAudit(
            request=request,
            source_event=source_event,
            decision=decision,
            lineage=lineage,
        )

    async def submit(
        self,
        request: _AuthorityRequest,
        prepared: tuple[object, ...],
    ) -> tuple[str, ...]:
        if (
            len(prepared) != 1
            or not isinstance(prepared[0], _PreparedWorldStimulusAudit)
            or prepared[0].request != request
        ):
            raise ValueError("world stimulus authority needs one prepared result")
        item = prepared[0]
        decision = item.decision
        lineage = item.lineage
        model_result_ref = "model-result:" + _digest(
            {
                "model_call_id": lineage.model_call_id,
                "response_hash": lineage.response_hash.removeprefix("sha256:"),
            }
        )
        model_event_id = f"event:character-interior-world-stimulus:model:{model_result_ref}"
        proposal_event_id = (
            "event:character-interior-world-stimulus:proposal:" + decision.proposal_id
        )
        existing_model = await self._lookup(model_event_id)
        existing_proposal = await self._lookup(proposal_event_id)
        if existing_model is not None or existing_proposal is not None:
            if existing_model is None or existing_proposal is None:
                raise ValueError("world stimulus author audit is only partly durable")
            return (decision.proposal_id,)
        audit = RecordedModelResultAudit(
            model_call_id=lineage.model_call_id,
            parent_model_call_id=lineage.parent_model_call_id,
            model_result_ref=model_result_ref,
            attempt_id=request.inner_turn_id,
            route=RecordedModelRoute(
                tier="flash",
                reason_code="character_interior_world_stimulus",
                router_version=PAYLOAD_CONTRACT,
            ),
            model_id=lineage.model_id,
            model_version=lineage.model_version,
            attempted_model_id=lineage.model_id,
            attempted_model_version=lineage.model_version,
            request_hash=lineage.request_hash.removeprefix("sha256:"),
            response_hash=lineage.response_hash.removeprefix("sha256:"),
            character_interior_lineage=RecordedCharacterInteriorTurnLineage(
                inner_turn_id=request.inner_turn_id,
                purpose=request.purpose,
                opportunity_ref=request.subject_ref,
                snapshot_id=request.snapshot_id,
                snapshot_hash=request.snapshot_hash,
                capability_ref=(
                    request.capability_manifest.capability_ref
                    if request.capability_manifest is not None
                    else "character-interior-capability:absent"
                ),
                author_model_id=lineage.model_id,
                author_model_version=lineage.model_version,
                author_model_call_id=lineage.model_call_id,
                author_request_hash=lineage.request_hash,
                author_response_hash=lineage.response_hash,
                author_attempt_ordinal=lineage.attempt_ordinal,
                author_parent_model_call_id=lineage.parent_model_call_id,
                private_self_lineage_hash=request.private_self_lineage_hash,
                decision_hash=request.decision_hash,
            ),
            status="proposal_validated",
        )
        audit_json = model_audit_json(audit)
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": request.snapshot_hash,
                    "proposal_hash": decision.proposal_hash,
                    "attempt_audits": [json.loads(audit_json)],
                }
            )
        )
        model_payload = ModelResultRecordedPayload(
            audit_contract="model-result-audit.7",
            model_result_ref=model_result_ref,
            deliberation_result_id=deliberation_result_id,
            proposal_hash=decision.proposal_hash,
            model_call_id=lineage.model_call_id,
            parent_model_call_id=lineage.parent_model_call_id,
            attempt_id=request.inner_turn_id,
            capsule_id=request.snapshot_hash,
            trigger_ref=item.source_event.event_id,
            evaluated_world_revision=request.cursor.world_revision,
            attempt_index=0,
            attempt_count=1,
            audit_json=audit_json,
            audit_hash=sha256(audit_json),
        )
        proposal_json = canonical_json(decision.model_dump(mode="json"))
        proposal_payload = ProposalRecordedV2Payload(
            proposal_id=decision.proposal_id,
            proposal_kind="decision",
            model_result_ref=model_result_ref,
            deliberation_result_id=deliberation_result_id,
            model_call_id=lineage.model_call_id,
            attempt_id=request.inner_turn_id,
            capsule_id=request.snapshot_hash,
            trigger_ref=item.source_event.event_id,
            evaluated_world_revision=request.cursor.world_revision,
            proposal_json=proposal_json,
            proposal_hash=decision.proposal_hash,
        )
        events = (
            self._event(
                event_id=model_event_id,
                event_type="ModelResultRecorded",
                source_event=item.source_event,
                payload=model_payload.model_dump(mode="json"),
            ),
            self._event(
                event_id=proposal_event_id,
                event_type="ProposalRecorded",
                source_event=item.source_event,
                payload=proposal_payload.model_dump(mode="json"),
            ),
        )
        await self._commit_at_cursor(
            events,
            cursor=request.cursor,
            commit_id="commit:character-interior-world-stimulus:audit:"
            + _digest([model_result_ref, decision.proposal_id]),
        )
        return (decision.proposal_id,)

    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        source_event: WorldEvent,
        payload: dict[str, object],
    ) -> WorldEvent:
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=payload,
        )
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=source_event.logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity
            or "world-v2:character-interior-world-stimulus:" + _digest([event_type, payload]),
            payload=payload,
        )

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _commit_at_cursor(self, events, *, cursor, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor,
                events,
                expected_cursor=cursor,
                commit_id=commit_id,
            )
        return self._ledger.commit_at_cursor(
            events,
            expected_cursor=cursor,
            commit_id=commit_id,
        )


class CharacterInteriorWorldStimulusRuntime:
    """Drain every committed protagonist stimulus through one Interior purpose."""

    def __init__(
        self,
        *,
        ledger: object,
        character_interior: CharacterInterior,
        emotion_worker: ImmediateEmotionProposalWorker,
        owner_id: str,
        companion_actor_ref: str,
        perception_result_reader: PerceptionResultReader | None = None,
        relationship_settlement: _WorldStimulusRelationshipSettlement | None = None,
        experience_settlement: ExperienceTransitionSettlement | None = None,
        lease_seconds: int = 120,
        source: str = "world-v2:character-interior-world-stimulus-runtime",
    ) -> None:
        if not owner_id or not companion_actor_ref or lease_seconds <= 0:
            raise ValueError("world stimulus runtime composition is incomplete")
        if emotion_worker.ledger is not ledger:
            raise ValueError("world stimulus emotion worker must own the exact ledger")
        if relationship_settlement is not None and relationship_settlement.ledger is not ledger:
            raise ValueError("world stimulus relationship worker must own the exact ledger")
        if experience_settlement is not None and experience_settlement.ledger is not ledger:
            raise ValueError("world stimulus experience worker must own the exact ledger")
        self._ledger = ledger
        self._interior = character_interior
        self._emotion_worker = emotion_worker
        self._owner_id = owner_id
        self._companion_actor_ref = companion_actor_ref
        self._perception_result_reader = perception_result_reader
        self._relationship_settlement = relationship_settlement
        self._experience_settlement = experience_settlement or ExperienceTransitionSettlement(
            ledger=ledger,
            owner_id=owner_id,
            companion_actor_ref=companion_actor_ref,
        )
        self._lease_seconds = lease_seconds
        self._source = source
        # Technical scheduling state only.  A malformed/provider-failed
        # trigger must not monopolize the host's bounded background budget;
        # the immutable trigger remains open/claimed and is retried after this
        # short wall-clock defer.  No semantic choice or world fact is stored
        # here, and a restart simply reuses the durable trigger lease.
        self._technical_failure_deferred_until: dict[str, float] = {}

    async def drain_one(self) -> CharacterInteriorRunResult:
        result = await self._drain_one_impl()
        if result.work_status == "technical_failure" and result.trigger_id:
            self._technical_failure_deferred_until[result.trigger_id] = (
                time.monotonic() + _TECHNICAL_FAILURE_DEFER_SECONDS
            )
        elif result.trigger_id:
            self._technical_failure_deferred_until.pop(result.trigger_id, None)
        return result

    async def _drain_one_impl(self) -> CharacterInteriorRunResult:
        projection = await self._project()
        process = self._next_process(projection)
        if process is None:
            return CharacterInteriorRunResult(trigger_id="", status="idle")
        source_event = await self._source_event(process, cursor=_cursor(projection))
        terminal_recovery = process.state == "terminal"
        if terminal_recovery:
            active, newly_claimed = process, False
        else:
            active, newly_claimed = await self._claim_or_reclaim(
                process=process,
                source_event=source_event,
                projection=projection,
            )
        if active is None:
            return CharacterInteriorRunResult(
                trigger_id=process.trigger_id,
                status="owned_elsewhere",
            )
        current = await self._project()
        audit = self._existing_audit(current, source_ref=source_event.event_id)
        if audit is None and not newly_claimed:
            return CharacterInteriorRunResult(
                trigger_id=process.trigger_id,
                status="owned_elsewhere",
            )
        if audit is None:
            cursor = _cursor(current)
            manifest = await self._manifest(
                process=active,
                source_event=source_event,
                projection=current,
            )
            transition = await self._interior.experience(
                InteriorStimulus(
                    inner_turn_ref=f"world-stimulus:{active.trigger_id}:{active.claim_lease.attempt_id}",
                    world_id=self._ledger.world_id,
                    actor_ref=self._companion_actor_ref,
                    trigger_ref=source_event.event_id,
                    cursor=cursor,
                    logical_time=current.logical_time or source_event.logical_time,
                    purpose=PURPOSE,
                    source_refs=(source_event.event_id,),
                    stimulus_ref=f"stimulus:{active.trigger_id}:{active.claim_lease.attempt_id}",
                    capability_manifest=manifest,
                    context_note=(
                        "A committed change is available for the character's own private "
                        "interpretation. It may matter in any way or not change her at all."
                    ),
                )
            )
            if transition.status == "technical_failure":
                _LOG.warning(
                    "world stimulus technical failure trigger=%s code=%s",
                    active.trigger_id,
                    transition.failure_code,
                )
                return CharacterInteriorRunResult(
                    trigger_id=active.trigger_id,
                    status="processed",
                    work_status="technical_failure",
                )
            if len(transition.proposal_refs) != 1:
                return CharacterInteriorRunResult(
                    trigger_id=active.trigger_id,
                    status="processed",
                    work_status="technical_failure",
                )
            proposal_id = transition.proposal_refs[0]
            after = await self._project()
            audit = next(
                (
                    item
                    for item in after.proposal_audits
                    if item.proposal_id == proposal_id and item.trigger_ref == source_event.event_id
                ),
                None,
            )
            if audit is None:
                raise RuntimeError("world stimulus result audit was not durably recorded")
        located_audit = await self._lookup(audit.event_ref)
        if located_audit is None:
            raise RuntimeError("world stimulus result audit event is unavailable")
        authored_proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if not isinstance(authored_proposal, DecisionProposal):
            raise RuntimeError("world stimulus result audit is not a decision proposal")
        audit_cursor = ProjectionCursor(
            world_revision=located_audit[1].world_revision,
            deliberation_revision=located_audit[1].deliberation_revision,
            ledger_sequence=located_audit[1].ledger_sequence,
        )
        try:
            experience = await self._experience_settlement.process(
                audit_cursor=audit_cursor,
                current_cursor=_cursor(await self._project()),
                proposal_id=audit.proposal_id,
                source_event=source_event,
            )
        except (ConcurrencyConflict, ValueError):
            # A stale head or incomplete content authority is a technical
            # failure, never a character choice.  The source trigger remains
            # claimed/recoverable and no local after-image is invented.
            return CharacterInteriorRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        aspiration = await self._process_aspiration(
            audit_cursor=audit_cursor,
            current_cursor=_cursor(await self._project()),
            proposal_id=audit.proposal_id,
            source_event=source_event,
        )
        if aspiration.status not in {"no_change", "accepted"}:
            return CharacterInteriorRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        # Settle the optional relationship signal while the exact historical
        # source trigger is still claimed. Appraisal acceptance terminalizes
        # that trigger, so doing this first removes the crash gap instead of
        # teaching the generic relationship reducer a second rebase bypass.
        relationship_status = "no_change"
        if self._relationship_settlement is not None:
            relationship = await self._relationship_settlement.process(
                world_id=self._ledger.world_id,
                audit_cursor=audit_cursor,
                current_cursor=_cursor(await self._project()),
                proposal_id=audit.proposal_id,
                source_event=source_event,
            )
            relationship_status = getattr(relationship, "status", "")
            if relationship_status not in {"no_change", "accepted"}:
                # The exact source trigger stays claimed/retryable. Contention
                # and settlement errors are never converted into a choice.
                return CharacterInteriorRunResult(
                    trigger_id=active.trigger_id,
                    status="processed",
                    work_status="technical_failure",
                )
        current_cursor = _cursor(await self._project())
        try:
            emotion = await self._process_emotion(
                audit_cursor=audit_cursor,
                current_cursor=current_cursor,
                proposal_id=audit.proposal_id,
            )
        except (ConcurrencyConflict, ValueError) as exc:
            # A compiler/acceptance failure is technical work, never a role
            # choice.  In particular, do not infer ``appraisal_only`` merely
            # because the exception happened while processing the combined
            # emotion lane: before Appraisal acceptance there is no durable
            # semantic result to settle.  The claimed trigger remains
            # recoverable and the next scheduler pass can retry it.
            _LOG.warning(
                "world stimulus affect leg failed; keeping trigger retryable "
                "trigger=%s error=%s",
                active.trigger_id,
                exc,
            )
            return CharacterInteriorRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        emotion_status = emotion.status
        if emotion_status not in {"no_change", "appraisal_only", "accepted"}:
            raise RuntimeError("world stimulus emotion settlement returned invalid status")
        completed_status = (
            "no-change"
            if emotion_status == "no_change"
            and relationship_status == "no_change"
            and aspiration.status == "no_change"
            and experience.status == "no_change"
            else "accepted"
        )
        # ``appraisal_only`` means the Appraisal acceptance already
        # terminalized this source trigger in its own atomic batch.  Only a
        # genuine no-change appraisal needs this runtime's completion event.
        if not terminal_recovery and emotion_status == "no_change":
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=_cursor(await self._project()),
                outcome_ref=f"outcome:{active.trigger_id}:{completed_status}",
            )
        elif not self._process_is_terminal(await self._project(), trigger_id=active.trigger_id):
            raise RuntimeError("accepted world stimulus did not terminalize its source trigger")
        return CharacterInteriorRunResult(
            trigger_id=active.trigger_id,
            status="processed",
            work_status=("no_change" if completed_status == "no-change" else "accepted"),
        )

    def _next_process(self, projection: object) -> TriggerProcess | None:
        now = time.monotonic()
        self._technical_failure_deferred_until = {
            trigger_id: deferred_until
            for trigger_id, deferred_until in self._technical_failure_deferred_until.items()
            if deferred_until > now
        }

        def eligible(process: TriggerProcess) -> bool:
            return (
                process.state != "terminal"
                and process.trigger_id not in self._technical_failure_deferred_until
            )

        for kind in _PROCESS_PRIORITY:
            match = next(
                (
                    item
                    for item in projection.trigger_processes
                    if item.process_kind == kind and eligible(item)
                ),
                None,
            )
            if match is not None:
                return match
        # Appraisal Acceptance terminalizes the historical source trigger in
        # the same batch. If the process crashes before its authored Affect is
        # accepted, resume from the immutable audit instead of silently losing
        # that part of the same CharacterInterior decision.
        for kind in _PROCESS_PRIORITY:
            for process in projection.trigger_processes:
                if (
                    process.process_kind != kind
                    or process.state != "terminal"
                    or process.trigger_id in self._technical_failure_deferred_until
                ):
                    continue
                source_ref = process.source_evidence_ref
                audit = (
                    self._existing_audit(projection, source_ref=source_ref)
                    if source_ref is not None
                    else None
                )
                if audit is not None and (
                    self._affect_is_pending(projection, audit=audit)
                    or self._relationship_is_pending(
                        projection,
                        audit=audit,
                        source_ref=source_ref,
                    )
                    or self._experience_is_pending(
                        projection,
                        audit=audit,
                        source_ref=source_ref,
                    )
                ):
                    return process
        return None

    @staticmethod
    def _process_is_terminal(projection: object, *, trigger_id: str) -> bool:
        return any(
            item.trigger_id == trigger_id and item.state == "terminal"
            for item in projection.trigger_processes
        )

    def _affect_is_pending(self, projection: object, *, audit: object) -> bool:
        try:
            proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(proposal, DecisionProposal):
            return False
        affect_changes = tuple(
            item for item in proposal.proposed_changes if item.kind == "affect_transition"
        )
        if not affect_changes:
            return False
        authored_change_ids = {item.change_id for item in affect_changes}
        for decision in projection.acceptance_decisions:
            if (
                decision.status != "accepted"
                or decision.manifest_version != "affect-acceptance.1"
                or decision.acceptance_event_ref is None
            ):
                continue
            accepted = self._ledger.lookup_event_commit(decision.acceptance_event_ref)
            proposal_event_ref = (
                accepted[0].payload().get("proposal_event_ref") if accepted is not None else None
            )
            recorded = (
                self._ledger.lookup_event_commit(proposal_event_ref)
                if isinstance(proposal_event_ref, str)
                else None
            )
            if recorded is None or recorded[0].event_type != "ProposalRecorded":
                continue
            try:
                typed = AffectProposalProjection.model_validate_json(recorded[0].payload_json)
            except ValueError:
                continue
            if (
                typed.source_audit is not None
                and typed.source_audit.proposal_event_ref == audit.event_ref
                and typed.source_audit.change_id in authored_change_ids
            ):
                return False
        return True

    def _relationship_is_pending(
        self,
        projection: object,
        *,
        audit: object,
        source_ref: str | None,
    ) -> bool:
        if self._relationship_settlement is None or source_ref is None:
            return False
        located_audit = self._ledger.lookup_event_commit(audit.event_ref)
        located_source = self._ledger.lookup_event_commit(source_ref)
        if located_audit is None or located_source is None:
            raise RuntimeError("world stimulus relationship recovery authority is unavailable")
        audit_commit = located_audit[1]
        return self._relationship_settlement.is_pending(
            world_id=self._ledger.world_id,
            audit_cursor=ProjectionCursor(
                world_revision=audit_commit.world_revision,
                deliberation_revision=audit_commit.deliberation_revision,
                ledger_sequence=audit_commit.ledger_sequence,
            ),
            current_cursor=_cursor(projection),
            proposal_id=audit.proposal_id,
            source_event=located_source[0],
        )

    def _experience_is_pending(
        self,
        projection: object,
        *,
        audit: object,
        source_ref: str | None,
    ) -> bool:
        if source_ref is None:
            return False
        located_audit = self._ledger.lookup_event_commit(audit.event_ref)
        located_source = self._ledger.lookup_event_commit(source_ref)
        if located_audit is None or located_source is None:
            raise RuntimeError("world stimulus experience recovery authority is unavailable")
        commit = located_audit[1]
        return self._experience_settlement.is_pending(
            audit_cursor=ProjectionCursor(
                world_revision=commit.world_revision,
                deliberation_revision=commit.deliberation_revision,
                ledger_sequence=commit.ledger_sequence,
            ),
            current_cursor=_cursor(projection),
            proposal_id=audit.proposal_id,
            source_event=located_source[0],
        )

    async def _process_aspiration(
        self,
        *,
        audit_cursor: ProjectionCursor,
        current_cursor: ProjectionCursor,
        proposal_id: str,
        source_event: WorldEvent,
    ) -> _WorldStimulusAspirationWorkResult:
        """Settle the optional aspiration change from this exact InnerTurn.

        This is deterministic typed authority, not another character author.
        The immutable DecisionProposal supplies the free direction and tension;
        this method only re-proves evidence, materializes the lifecycle event,
        and crosses one cursor-CAS boundary.
        """

        audit_projection = await self._project_at(audit_cursor)
        audit = next(
            (
                item
                for item in audit_projection.proposal_audits
                if item.proposal_id == proposal_id and item.trigger_ref == source_event.event_id
            ),
            None,
        )
        if audit is None:
            raise ValueError("world stimulus aspiration source audit is unavailable")
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
        if not isinstance(proposal, DecisionProposal):
            raise ValueError("world stimulus aspiration source is not a decision")
        changes = tuple(
            item for item in proposal.proposed_changes if item.kind == "aspiration_transition"
        )
        if not changes:
            return _WorldStimulusAspirationWorkResult(
                status="no_change",
                source_proposal_id=proposal_id,
            )
        if len(changes) != 1:
            raise ValueError("world stimulus authored multiple aspiration transitions")
        change = changes[0]
        transition = AspirationTransitionPayload.model_validate(
            change.payload.value(),
            strict=True,
        )
        if transition.operation != change.transition:
            raise ValueError("aspiration transition changed across typed authority")
        evidence_by_ref = {item.ref_id: item for item in proposal.evidence_refs}
        if set(change.evidence_refs) != set(transition.source_refs) or any(
            ref not in evidence_by_ref for ref in change.evidence_refs
        ):
            raise ValueError("aspiration transition evidence is not source closed")

        event_id = "event:character-interior:aspiration:" + _digest(
            [self._ledger.world_id, change.change_id]
        )
        existing = await self._lookup(event_id)
        if existing is not None:
            if (
                existing[0].causation_id != audit.event_ref
                or existing[0].payload().get("change_id") != change.change_id
            ):
                raise ValueError("aspiration effect identity collides with another authority")
            return _WorldStimulusAspirationWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                event_ref=event_id,
                replayed=True,
            )

        projection = await self._project_at(current_cursor)
        logical_time = projection.logical_time or source_event.logical_time
        evidence = tuple(
            EvidenceRef(
                ref_id=ref,
                evidence_type=evidence_by_ref[ref].evidence_kind,
                claim_purpose="private_hypothesis",
                source_world_revision=evidence_by_ref[ref].source_world_revision,
                immutable_hash=evidence_by_ref[ref].immutable_hash.removeprefix("sha256:"),
            )
            for ref in change.evidence_refs
        )
        common = {
            "change_id": change.change_id,
            "transition_id": "transition:character-interior:aspiration:"
            + _digest([proposal_id, change.change_id]),
            "expected_entity_revision": change.expected_entity_revision,
            "evidence_refs": evidence,
            "policy_refs": change.policy_refs,
        }
        current = next(
            (
                item
                for item in projection.aspirations
                if item.aspiration_id == change.target_id
                and item.owner_actor_ref == self._companion_actor_ref
            ),
            None,
        )
        if transition.operation == "plant":
            if current is not None or change.expected_entity_revision != 0:
                raise ValueError("aspiration planting target already exists")
            assert transition.text is not None
            assert transition.privacy_class is not None
            after = AspirationProjection(
                aspiration_id=change.target_id,
                entity_revision=1,
                owner_actor_ref=self._companion_actor_ref,
                seed_id="character-interior:"
                + _digest([proposal_id, change.change_id, transition.text]),
                origin_kind="character_authored",
                text=transition.text,
                privacy_class=transition.privacy_class,
                status="active",
                planted_at=logical_time,
                planted_event_ref=event_id,
                source_event_ref=source_event.event_id,
                tension_summary=transition.tension_summary,
                tension_source_refs=tuple(transition.tension_source_refs),
            )
            payload = AspirationPlantedPayload(
                **common,
                aspiration=after,
            )
            event_type = "AspirationPlanted"
        else:
            if (
                current is None
                or current.status != "active"
                or current.entity_revision != change.expected_entity_revision
                or transition.aspiration_id != current.aspiration_id
            ):
                raise ValueError("aspiration transition target is stale")
            if transition.operation == "reinforce":
                payload = AspirationReinforcedPayload(
                    **common,
                    aspiration_id=current.aspiration_id,
                    reinforced_at=logical_time,
                    reinforcement_evidence_ref=source_event.event_id,
                )
                event_type = "AspirationReinforced"
            elif transition.operation == "revise":
                assert transition.text is not None
                assert transition.privacy_class is not None
                after = current.model_copy(
                    update={
                        "entity_revision": current.entity_revision + 1,
                        "text": transition.text,
                        "privacy_class": transition.privacy_class,
                        "tension_summary": transition.tension_summary,
                        "tension_source_refs": tuple(transition.tension_source_refs),
                        "last_revised_at": logical_time,
                        "revision_event_ref": event_id,
                    }
                )
                payload = AspirationRevisedPayload(
                    **common,
                    aspiration_before=current,
                    aspiration_after=after,
                )
                event_type = "AspirationRevised"
            else:
                after = current.model_copy(
                    update={
                        "entity_revision": current.entity_revision + 1,
                        "status": "abandoned",
                        "abandoned_at": logical_time,
                        "abandonment_event_ref": event_id,
                        "abandonment_summary": transition.reason_summary,
                        "abandonment_source_refs": tuple(transition.source_refs),
                    }
                )
                payload = AspirationAbandonedPayload(
                    **common,
                    aspiration_before=current,
                    aspiration_after=after,
                )
                event_type = "AspirationAbandoned"

        raw_payload = payload.model_dump(mode="json")
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=raw_payload,
        )
        if identity is None:
            raise ValueError("aspiration transition identity is incomplete")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=audit.event_ref,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=raw_payload,
        )
        try:
            await self._commit(
                (event,),
                cursor=current_cursor,
                commit_id="commit:character-interior:aspiration:"
                + _digest([proposal_id, change.change_id]),
            )
        except ConcurrencyConflict:
            existing = await self._lookup(event_id)
            if existing is None:
                raise
            return _WorldStimulusAspirationWorkResult(
                status="accepted",
                source_proposal_id=proposal_id,
                event_ref=event_id,
                replayed=True,
            )
        return _WorldStimulusAspirationWorkResult(
            status="accepted",
            source_proposal_id=proposal_id,
            event_ref=event_id,
        )

    async def _source_event(
        self,
        process: TriggerProcess,
        *,
        cursor: ProjectionCursor,
    ) -> WorldEvent:
        source_ref = process.source_evidence_ref
        if source_ref is None:
            raise ValueError("world stimulus trigger has no source evidence")
        located = await self._lookup(source_ref)
        expected_type = _SOURCE_EVENT_TYPE.get(process.process_kind)
        if located is None or expected_type is None or located[0].event_type != expected_type:
            raise ValueError("world stimulus source authority is unavailable")
        event, commit = located
        if process.process_kind == "perception_result_deliberation":
            expected_trigger_ref = self._perception_result_trigger_ref(event)
        elif process.process_kind == "npc_world_appraisal":
            expected_trigger_ref = process.trigger_id
        elif process.process_kind == "silence_appraisal":
            expected_trigger_ref = f"silence:{source_ref}"
        elif process.process_kind == "plan_disruption_appraisal":
            expected_trigger_ref = f"plan-disruption:{source_ref}"
        elif process.process_kind == "life_reflection":
            expected_trigger_ref = f"reflection:{source_ref}"
        else:  # pragma: no cover - guarded by _SOURCE_EVENT_TYPE above
            raise ValueError("unsupported world stimulus trigger kind")
        if (
            commit.world_revision > cursor.world_revision
            or process.trigger_ref != expected_trigger_ref
            or (
                process.process_kind == "perception_result_deliberation"
                and process.trigger_id
                != perception_result_trigger_id(
                    world_id=self._ledger.world_id,
                    result_id=PerceptionResultAcceptedPayload.model_validate_json(
                        event.payload_json
                    ).result.result_id,
                )
            )
        ):
            raise ValueError("world stimulus trigger does not bind its source event")
        return event

    async def _claim_or_reclaim(self, *, process, source_event, projection):
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            lease = process.claim_lease
            if at <= lease.expires_at:
                if lease.owner_id == self._owner_id:
                    return process, False
                return None, False
        attempt_id = "attempt:character-interior-world-stimulus:" + _digest(
            {"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1}
        )
        claimed = process.model_copy(
            update={
                "state": "claimed",
                "claim_lease": ClaimLease(
                    owner_id=self._owner_id,
                    attempt_id=attempt_id,
                    acquired_at=at,
                    expires_at=at + timedelta(seconds=self._lease_seconds),
                ),
                "attempt_ids": (*process.attempt_ids, attempt_id),
            }
        )
        event_type = (
            "TriggerProcessClaimed" if process.state == "open" else "TriggerProcessReclaimed"
        )
        payload = {"process": claimed.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type=event_type,
            world_id=self._ledger.world_id,
            payload=payload,
        )
        if identity is None:
            raise ValueError("world stimulus claim identity is missing")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:character-interior-world-stimulus:claim:"
            + _digest([process.trigger_id, attempt_id]),
            world_id=self._ledger.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        await self._commit(
            (event,),
            cursor=_cursor(projection),
            commit_id="commit:character-interior-world-stimulus:claim:"
            + _digest([process.trigger_id, attempt_id]),
        )
        return claimed, True

    def _stimulus_source_refs(
        self, *, projection, source_event
    ) -> tuple[str, ...]:
        """Return the source-event plus every Experience the character can
        legitimately cite from this settlement.

        The role model naturally attends to the committed Experience and its
        memory candidate that a settlement produced.  Pinning only the bare
        settlement event made every such citation an unpinned-ref failure.
        These refs stay source-bound: they are exactly the experiences whose
        settlement binding names this source event.
        """

        refs: list[str] = [source_event.event_id]
        occurrence_id = None
        for ref in getattr(projection, "committed_world_event_refs", ()):
            if ref.event_id == source_event.event_id:
                continue
        for occurrence in getattr(projection, "world_occurrences", ()):
            if getattr(occurrence, "settlement_event_ref", None) == source_event.event_id:
                occurrence_id = occurrence.occurrence_id
                break
        if occurrence_id is not None:
            for exp in getattr(projection, "experiences", ()):
                if not isinstance(exp, ExperienceProjection):
                    continue
                bindings = getattr(exp.values, "source_bindings", ())
                if any(
                    isinstance(binding, ExperienceOccurrenceSettlementBinding)
                    and binding.occurrence_id == occurrence_id
                    for binding in bindings
                ):
                    refs.append(exp.origin.accepted_event_ref)
        return tuple(dict.fromkeys(refs))

    async def _manifest(self, *, process, source_event, projection):
        logical_time = projection.logical_time or source_event.logical_time
        payload = {
            "contract": "character-interior-world-stimulus-capability.1",
            "process_kind": process.process_kind,
            "stimulus_kind": _STIMULUS_KIND[process.process_kind],
            "considered_at": logical_time.isoformat(),
            "source_event": {
                "event_id": source_event.event_id,
                "event_type": source_event.event_type,
                "logical_time": source_event.logical_time.isoformat(),
                "payload": json.loads(source_event.payload_json),
                "payload_hash": source_event.payload_hash,
            },
            "result_choices": ["no_change", "activate"],
            "affect_target_lower_bounds": lower_bounds_from_projection(projection).model_dump(
                mode="json"
            ),
            "active_affect_heads": _active_affect_capability(projection),
            # The deterministic settlement capability, not the system, decides
            # whether relationship effects are available. The model remains
            # free to omit the signal even when subjects are offered.
            "relationship_subject_refs": (
                list(
                    relationship_transition_subject_refs(
                        projection=projection,
                        source_event=source_event,
                    )
                )
                if self._relationship_settlement is not None
                else []
            ),
            "active_aspirations": _active_aspiration_capability(
                projection,
                actor_ref=self._companion_actor_ref,
            ),
            "experience_transitions": build_experience_transition_capability(
                projection=projection,
                actor_ref=self._companion_actor_ref,
                source_event=source_event,
            ).model_dump(mode="json"),
        }
        if process.process_kind == "perception_result_deliberation":
            payload["perception_result"] = await self._read_perception_result(source_event)
        payload_json = _canonical(payload)
        return _InteriorCapabilityManifest(
            capability_ref=f"capability:world-stimulus:{process.trigger_id}:{process.claim_lease.attempt_id}",
            capability_kind=PURPOSE,
            payload_json=payload_json,
            payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
            source_refs=(source_event.event_id,),
        )

    async def _read_perception_result(
        self,
        source_event: WorldEvent,
    ) -> dict[str, object]:
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                _perception_result_material,
                source_event=source_event,
                reader=self._perception_result_reader,
            )
        return _perception_result_material(
            source_event=source_event,
            reader=self._perception_result_reader,
        )

    @staticmethod
    def _perception_result_trigger_ref(source_event: WorldEvent) -> str:
        result = PerceptionResultAcceptedPayload.model_validate_json(
            source_event.payload_json
        ).result
        return f"perception-result:{result.result_id}"

    @staticmethod
    def _existing_audit(projection, *, source_ref: str):
        matches = tuple(
            item
            for item in projection.proposal_audits
            if item.proposal_kind == "decision"
            and item.trigger_ref == source_ref
            and item.proposal_id.startswith("proposal:character-interior-world-stimulus:")
        )
        return matches[-1] if matches else None

    async def _process_emotion(self, *, audit_cursor, current_cursor, proposal_id):
        def run():
            return self._emotion_worker.process(
                world_id=self._ledger.world_id,
                audit_cursor=audit_cursor,
                proposal_id=proposal_id,
                current_cursor=(current_cursor if current_cursor != audit_cursor else None),
            )

        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(run)
        return run()

    async def _complete(self, *, process, source_event, cursor, outcome_ref):
        if process.claim_lease is None:
            raise ValueError("world stimulus completion needs a claimed process")
        projection = await self._project_at(cursor)
        at = max(
            projection.logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        if at > process.claim_lease.expires_at:
            raise ValueError("world stimulus lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:character-interior-world-stimulus:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id]),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessCompleted",
            logical_time=at,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key="world-v2:character-interior-world-stimulus:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await self._commit(
            (event,),
            cursor=cursor,
            commit_id="commit:character-interior-world-stimulus:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _project_at(self, cursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _lookup(self, event_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _commit(self, events, *, cursor, commit_id):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(
                self._ledger.commit_at_cursor,
                events,
                expected_cursor=cursor,
                commit_id=commit_id,
            )
        return self._ledger.commit_at_cursor(
            events,
            expected_cursor=cursor,
            commit_id=commit_id,
        )


__all__ = ["CharacterInteriorWorldStimulusRuntime"]
