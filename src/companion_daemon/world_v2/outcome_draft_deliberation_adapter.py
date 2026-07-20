"""Materialize a bounded outcome-candidate choice into an inert proposal.

Unlike a conversational reply, an outcome has no user message to answer.  A
generic chat adapter must therefore not be reused here: it would either fail
on the missing message or tempt a model to invent a full world mutation.  This
adapter gives the model only source-bound candidate excerpts and derives the
complete typed settlement from the pinned ledger authority after selection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from .deliberation import ModelInput, ModelOutput
from .ledger import LedgerPort
from .outcome_candidate_reader import OutcomeCandidateReader
from .outcome_selection_draft import (
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionModel,
    OutcomeSelectionOption,
)
from .proposal_envelope import CanonicalTypedPayload, DecisionProposal, TypedChange
from .schemas import OutcomeObservationProjection


_CONTRACT = "outcome-draft-materialization.1"


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


class OutcomeDraftDeliberationAdapter:
    """One model selection over the exact outcome matrix visible in Context."""

    VERSION = _CONTRACT

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        candidate_reader: OutcomeCandidateReader,
        model: OutcomeSelectionModel,
    ) -> None:
        self._ledger = ledger
        self._reader = candidate_reader
        self._draft = OutcomeSelectionDraftAdapter(model=model)
        self._model_id = (str(getattr(model, "model", "")).strip() or type(model).__name__)[:256]

    async def propose(self, request: ModelInput) -> ModelOutput:
        occurrence, source = await self._authority(request)
        visible = _visible_candidates(request)
        readable = self._reader.read(occurrence=occurrence, viewer_privacy_ceiling="private")
        by_ref = {item.candidate_result_ref: item for item in readable.candidates}
        options = tuple(
            OutcomeSelectionOption(candidate_result_ref=ref, summary=summary)
            for ref, summary in visible
            if ref in by_ref
        )
        if not options:
            raise ValueError("OutcomeDraft has no source-bound candidate visible in Context")
        draft = await self._draft.deliberate(options=options)
        candidate = by_ref.get(draft.candidate_result_ref)
        if candidate is None:
            raise ValueError("OutcomeDraft selected unavailable candidate")
        return ModelOutput(
            model_id=self._model_id,
            model_version=self.VERSION,
            raw_proposal=_proposal(
                request=request,
                occurrence=occurrence,
                source=source,
                candidate=candidate,
                observations=await self._observation_bindings(occurrence=occurrence),
            ).model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        """A recovery may not silently choose a different world result."""

        del request, failure_code
        raise ValueError("OutcomeDraft recovery cannot invent a candidate selection")

    async def _authority(self, request: ModelInput):
        if len(request.trigger_evidence) != 1:
            raise ValueError("OutcomeDraft requires exactly one trigger evidence binding")
        source = request.trigger_evidence[0]
        if source.ref_id != request.trigger_ref or source.evidence_kind != "committed_world_event":
            raise ValueError("OutcomeDraft trigger evidence is invalid")
        located = await self._lookup(request.trigger_ref)
        if located is None:
            raise ValueError("OutcomeDraft source observation is unavailable")
        event, commit = located
        if (
            event.event_type != "OutcomeObservationRecorded"
            or source.source_world_revision != commit.world_revision
            or source.immutable_hash.removeprefix("sha256:") != event.payload_hash.removeprefix("sha256:")
        ):
            raise ValueError("OutcomeDraft source observation binding is invalid")
        observation = OutcomeObservationProjection.model_validate_json(
            json.dumps(event.payload().get("observation"))
        )
        projection = await self._project()
        if projection.world_revision != request.evaluated_world_revision:
            raise ValueError("OutcomeDraft cursor is stale")
        occurrence = next(
            (item for item in projection.world_occurrences if item.occurrence_id == observation.occurrence_id),
            None,
        )
        if (
            occurrence is None
            or occurrence.status != "active"
            or observation.observation_id not in occurrence.observation_refs
        ):
            raise ValueError("OutcomeDraft occurrence is no longer active")
        return occurrence, source

    async def _lookup(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _observation_bindings(self, *, occurrence) -> tuple[dict[str, object], ...]:
        bindings: list[dict[str, object]] = []
        for observation_id in occurrence.observation_refs:
            located = await self._lookup(f"event:outcome-observation:{observation_id}")
            if located is None or located[0].event_type != "OutcomeObservationRecorded":
                raise ValueError("OutcomeDraft observation authority is unavailable")
            event, commit = located
            bindings.append(
                {
                    "ref_id": observation_id,
                    "source_world_revision": commit.world_revision,
                    "immutable_hash": "sha256:" + event.payload_hash.removeprefix("sha256:"),
                }
            )
        if not bindings:
            raise ValueError("OutcomeDraft occurrence has no observed outcome")
        return tuple(bindings)


def _visible_candidates(request: ModelInput) -> tuple[tuple[str, str], ...]:
    """Read only the outcome advisory material compiled into this request."""

    try:
        content = json.loads(request.model_content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("OutcomeDraft Context is invalid") from exc
    if not isinstance(content, dict):
        raise ValueError("OutcomeDraft Context is invalid")
    slices = content.get("slices")
    advisory_slice = slices.get("advisories") if isinstance(slices, dict) else None
    items = advisory_slice.get("items") if isinstance(advisory_slice, dict) else None
    if advisory_slice is None or advisory_slice.get("availability") != "available" or not isinstance(items, list):
        raise ValueError("OutcomeDraft advisory Context is unavailable")
    matches: list[tuple[str, str]] = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict) or value.get("kind") != "outcome_candidate_matrix":
            continue
        if request.trigger_ref not in value.get("source_refs", []):
            continue
        candidates = value.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("OutcomeDraft candidate advisory is malformed")
        for candidate in candidates:
            ref = candidate.get("candidate_ref") if isinstance(candidate, dict) else None
            summary = candidate.get("value") if isinstance(candidate, dict) else None
            if not isinstance(ref, str) or not ref or not isinstance(summary, str) or not summary:
                raise ValueError("OutcomeDraft candidate advisory is malformed")
            matches.append((ref, summary))
    if not matches or len({ref for ref, _ in matches}) != len(matches):
        raise ValueError("OutcomeDraft candidate advisory is missing or ambiguous")
    return tuple(matches)


def _proposal(*, request: ModelInput, occurrence, source, candidate, observations) -> DecisionProposal:
    identity = _digest(
        {
            "contract": _CONTRACT,
            "call_id": request.call_id,
            "occurrence_id": occurrence.occurrence_id,
            "candidate_result_ref": candidate.candidate_result_ref,
        }
    )
    return DecisionProposal(
        proposal_id=f"proposal:outcome-draft:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(source,),
        proposed_changes=(
            TypedChange(
                change_id=f"change:outcome-draft:{identity}",
                kind="outcome_settlement",
                target_id=occurrence.occurrence_id,
                transition="settle",
                expected_entity_revision=occurrence.entity_revision,
                evidence_refs=(source.ref_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="outcome_settlement.v1",
                    value={
                        "outcome_proposal_id": f"model-hint:outcome-draft:{identity}",
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
        brief_rationale="Selected one source-bound candidate from the observed outcome matrix.",
        behavior_tendency="settle_observed_outcome",
        stance="settle_source_bound_candidate",
        display_strategy="withhold",
    )


__all__ = ["OutcomeDraftDeliberationAdapter"]
