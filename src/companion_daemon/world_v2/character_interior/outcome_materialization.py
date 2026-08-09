"""Materialize an external outcome through the sole CharacterInterior author."""

from __future__ import annotations

import asyncio
import hashlib
import json

from ..deliberation import ModelInput, ModelOutput
from ..ledger import LedgerPort
from ..outcome_candidate_reader import OutcomeCandidateReader
from ..proposal_envelope import CanonicalTypedPayload, DecisionProposal, TypedChange
from ..schemas import OutcomeObservationProjection, ProjectionCursor
from .contracts import InteriorOpportunity, _InteriorCapabilityManifest
from .core import CharacterInterior
from .audit import recorded_character_interior_lineage
from .run_result import CausalOpportunityIdentity


_CONTRACT = "character-interior-outcome-materialization.1"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


class _CharacterInteriorOutcomeMaterializer:
    """One external-observation choice; settlement bytes remain deterministic."""

    VERSION = _CONTRACT

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        candidate_reader: OutcomeCandidateReader,
        character_interior: CharacterInterior,
        actor_ref: str,
    ) -> None:
        self._ledger = ledger
        self._reader = candidate_reader
        self._interior = character_interior
        self._actor_ref = actor_ref

    async def propose(self, request: ModelInput) -> ModelOutput:
        occurrence, source, projection = await self._authority(request)
        readable = self._reader.read(
            occurrence=occurrence,
            viewer_privacy_ceiling="private",
        )
        if not readable.candidates:
            raise ValueError("outcome materialization has no source-bound candidate")
        candidates = [
            {
                "token": item.candidate_result_ref,
                "summary": item.text,
                "privacy_class": item.privacy_class,
            }
            for item in readable.candidates
        ]
        capability_value = {
            "occurrence_id": occurrence.occurrence_id,
            "observation_event_ref": request.trigger_ref,
            "offered_tokens": [item["token"] for item in candidates],
            "candidates": candidates,
            "allow_character_life_direction": False,
            "current_coordinates": [],
        }
        payload_json = json.dumps(
            capability_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        payload_hash = "sha256:" + hashlib.sha256(payload_json.encode()).hexdigest()
        capability = _InteriorCapabilityManifest(
            capability_ref=(
                "capability:outcome-observation:"
                + _digest(
                    {
                        "trigger_ref": request.trigger_ref,
                        "cursor": request.evaluated_world_revision,
                        "payload_hash": payload_hash,
                    }
                )
            ),
            capability_kind="outcome_selection",
            payload_json=payload_json,
            payload_hash=payload_hash,
            source_refs=(request.trigger_ref,),
        )
        cursor = ProjectionCursor(
            world_revision=request.evaluated_world_revision,
            deliberation_revision=request.evaluated_deliberation_revision,
            ledger_sequence=request.evaluated_ledger_sequence,
        )
        opportunity_ref = CausalOpportunityIdentity.from_source_refs(
            world_id=self._ledger.world_id,
            actor_ref=self._actor_ref,
            purpose="outcome_selection",
            source_refs=(request.trigger_ref,),
            epoch=request.trigger_ref,
        ).opportunity_ref
        decision = await self._interior.consider(
            InteriorOpportunity(
                opportunity_ref=opportunity_ref,
                inner_turn_ref="inner-turn:outcome-observation:" + request.attempt_id,
                world_id=self._ledger.world_id,
                actor_ref=self._actor_ref,
                trigger_ref=request.trigger_ref,
                cursor=cursor,
                logical_time=projection.logical_time,
                purpose="outcome_selection",
                source_refs=(request.trigger_ref,),
                capability_manifest=capability,
                context_note=(
                    "An external result was observed. Choose one exact authorized "
                    "candidate; this lane cannot create a subjective life direction."
                ),
            )
        )
        if decision.status == "technical_failure":
            raise RuntimeError(decision.failure_code or "character_interior_outcome_failure")
        outer = decision.decision
        payload = outer.get("payload") if isinstance(outer, dict) else None
        if (
            decision.status != "decided"
            or not isinstance(outer, dict)
            or outer.get("contract") != "character-interior-purpose-decision.1"
            or outer.get("purpose") != "outcome_selection"
            or outer.get("capability_ref") != capability.capability_ref
            or outer.get("capability_payload_hash") != capability.payload_hash
            or tuple(outer.get("source_refs", ())) != capability.source_refs
            or not isinstance(payload, dict)
            or payload.get("contract") != "character-interior-outcome-selection-decision.1"
            or payload.get("character_life_direction") is not None
        ):
            raise ValueError("CharacterInterior outcome decision is not closed")
        selected_ref = payload.get("selected_token")
        candidate = next(
            (item for item in readable.candidates if item.candidate_result_ref == selected_ref),
            None,
        )
        if candidate is None:
            raise ValueError("CharacterInterior selected an unavailable outcome")
        lineage = decision.author_lineage
        if lineage is None:
            raise ValueError("CharacterInterior outcome decision lacks author lineage")
        proposal = _proposal(
            request=request,
            occurrence=occurrence,
            source=source,
            candidate=candidate,
            observations=await self._observation_bindings(occurrence=occurrence),
        )
        return ModelOutput(
            model_id=lineage.model_id,
            model_version=lineage.model_version,
            raw_proposal=proposal.model_dump(mode="json"),
            winning_model_call_id=lineage.model_call_id,
            winning_request_hash=lineage.request_hash.removeprefix("sha256:"),
            character_interior_lineage=recorded_character_interior_lineage(
                decision,
                purpose="outcome_selection",
                subject_ref=decision.opportunity_ref,
                capability_ref=capability.capability_ref,
            ),
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        del request, failure_code
        raise ValueError("outcome materialization has no second character author")

    async def _authority(self, request: ModelInput):
        if len(request.trigger_evidence) != 1:
            raise ValueError("outcome materialization requires one trigger binding")
        source = request.trigger_evidence[0]
        if source.ref_id != request.trigger_ref or source.evidence_kind != "committed_world_event":
            raise ValueError("outcome materialization trigger binding is invalid")
        located = await self._lookup(request.trigger_ref)
        if located is None:
            raise ValueError("outcome materialization observation is unavailable")
        event, commit = located
        if (
            event.event_type != "OutcomeObservationRecorded"
            or source.source_world_revision != commit.world_revision
            or source.immutable_hash.removeprefix("sha256:")
            != event.payload_hash.removeprefix("sha256:")
        ):
            raise ValueError("outcome materialization observation binding is invalid")
        projection = await self._project_at(
            ProjectionCursor(
                world_revision=request.evaluated_world_revision,
                deliberation_revision=request.evaluated_deliberation_revision,
                ledger_sequence=request.evaluated_ledger_sequence,
            )
        )
        observation = OutcomeObservationProjection.model_validate_json(
            json.dumps(event.payload().get("observation"))
        )
        occurrence = next(
            (
                item
                for item in projection.world_occurrences
                if item.occurrence_id == observation.occurrence_id
            ),
            None,
        )
        if (
            occurrence is None
            or occurrence.status != "active"
            or observation.observation_id not in occurrence.observation_refs
        ):
            raise ValueError("outcome materialization occurrence is no longer active")
        return occurrence, source, projection

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _observation_bindings(self, *, occurrence) -> tuple[dict[str, object], ...]:
        bindings: list[dict[str, object]] = []
        for observation_id in occurrence.observation_refs:
            located = await self._lookup(f"event:outcome-observation:{observation_id}")
            if located is None or located[0].event_type != "OutcomeObservationRecorded":
                raise ValueError("outcome observation authority is unavailable")
            event, commit = located
            bindings.append(
                {
                    "ref_id": observation_id,
                    "source_world_revision": commit.world_revision,
                    "immutable_hash": "sha256:" + event.payload_hash.removeprefix("sha256:"),
                }
            )
        if not bindings:
            raise ValueError("outcome occurrence has no observation")
        return tuple(bindings)


def _proposal(*, request, occurrence, source, candidate, observations) -> DecisionProposal:
    identity = _digest(
        {
            "contract": _CONTRACT,
            "call_id": request.call_id,
            "occurrence_id": occurrence.occurrence_id,
            "candidate_result_ref": candidate.candidate_result_ref,
        }
    )
    return DecisionProposal(
        proposal_id=f"proposal:outcome-interior:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(source,),
        proposed_changes=(
            TypedChange(
                change_id=f"change:outcome-interior:{identity}",
                kind="outcome_settlement",
                target_id=occurrence.occurrence_id,
                transition="settle",
                expected_entity_revision=occurrence.entity_revision,
                evidence_refs=(source.ref_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="outcome_settlement.v1",
                    value={
                        "outcome_proposal_id": f"model-hint:outcome-interior:{identity}",
                        "candidate_result_ref": candidate.candidate_result_ref,
                        "result_id": candidate.result_id,
                        "entity_id": occurrence.occurrence_id,
                        "entity_revision": occurrence.entity_revision,
                        "observations": list(observations),
                        "result_payload": {
                            "object_ref": candidate.result_payload_ref,
                            "schema_version": "outcome-result.1",
                            "payload_hash": candidate.result_payload_hash,
                        },
                    },
                ),
            ),
        ),
        action_intents=(),
        confidence=7_000,
        brief_rationale="Selected one source-bound observed outcome.",
        behavior_tendency="settle_observed_outcome",
        stance="settle_source_bound_candidate",
        display_strategy="withhold",
    )


__all__: list[str] = []
