"""Materialize a bounded relationship-evaluation draft into a generic suggestion.

The adapter sits *before* relationship authority.  It can produce one audited
``relationship_signal/suggest`` candidate, but has no relationship state,
ledger, reducer, or acceptance dependency.  In particular, the model never
gets to name the counterpart: that subject is read from the pinned
``relationship_slice`` in the request capsule.
"""

from __future__ import annotations

import hashlib
import json

from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import ModelInput, ModelOutput
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalEvidenceRef,
    TypedChange,
)
from .relationship_evaluation_draft import (
    RelationshipEvaluationDraft,
    RelationshipEvaluationDraftAdapter,
    RelationshipEvaluationDraftCapsule,
)


_CONTRACT = "relationship-draft-materialization.1"


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class RelationshipDraftDeliberationAdapter:
    """Bridge a safe model draft to one inert generic DecisionProposal."""

    # Version 2 (2026-07-20): the draft capsule now carries bounded recent
    # dialogue, prior accepted appraisals, and active affect summaries in
    # addition to the trigger appraisal, so the draft model can distinguish a
    # connectivity ping from sustained warm conversation.  Proposal identity
    # material (_CONTRACT) is unchanged.
    VERSION = "relationship-draft-deliberation-adapter.2"

    def __init__(
        self, *, model: ChatCompletionModel, model_id: str | None = None, temperature: float = 0.2
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("relationship adapter temperature must be between 0 and 2")
        inferred = str(getattr(model, "model", "")).strip()
        self._model_id = (model_id or inferred or type(model).__name__)[:256]
        self._draft_adapter = RelationshipEvaluationDraftAdapter(
            model=model, model_id=self._model_id, temperature=temperature
        )

    async def propose(self, request: ModelInput) -> ModelOutput:
        material = _capsule(request)
        context = _context_summaries(material=material, trigger_ref=request.trigger_ref)
        evaluation = _relationship_evaluation(material=material, trigger_ref=request.trigger_ref)
        if evaluation is not None:
            subject_ref = evaluation["subject_ref"]
            draft = await self._draft_adapter.deliberate(
                capsule=RelationshipEvaluationDraftCapsule(
                    accepted_appraisal_summary=evaluation["appraisal_summary_json"],
                    relationship_summary=evaluation["relationship_summary_json"],
                    **context,
                )
            )
            return self._output(
                _proposal_from_draft(draft=draft, request=request, subject_ref=subject_ref)
            )
        appraisals = _slice_items(material, "appraisals")
        subject_ref = _counterpart_subject(
            material=material,
            appraisals=appraisals,
            trigger_ref=request.trigger_ref,
        )
        if subject_ref is None:
            return self._output(_no_change(request=request, rationale="relationship_data_unavailable"))
        draft = await self._draft_adapter.deliberate(
            capsule=_draft_capsule(material=material, request=request, context=context)
        )
        return self._output(_proposal_from_draft(draft=draft, request=request, subject_ref=subject_ref))

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        """A failed background interpretation may not invent a relationship trace."""

        return self._output(
            _no_change(request=request, rationale=f"relationship_model_unavailable:{failure_code[:96]}")
        )

    def _output(self, raw_proposal: dict[str, object]) -> ModelOutput:
        return ModelOutput(
            model_id=self._model_id,
            model_version=self.VERSION,
            raw_proposal=raw_proposal,
        )


def _capsule(request: ModelInput) -> dict[str, object]:
    try:
        value = json.loads(request.model_content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("RelationshipDraft context is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("RelationshipDraft context is invalid")
    return value


def _slice_items(material: dict[str, object], name: str) -> tuple[dict[str, object], ...] | None:
    slices = material.get("slices")
    if not isinstance(slices, dict):
        raise ValueError("RelationshipDraft context has no slices")
    slice_ = slices.get(name)
    if not isinstance(slice_, dict):
        return None
    if slice_.get("availability") != "available":
        return None
    items = slice_.get("items")
    if not isinstance(items, list):
        raise ValueError(f"RelationshipDraft {name} slice items are invalid")
    values: list[dict[str, object]] = []
    for item in items:
        value = item.get("value") if isinstance(item, dict) else None
        if not isinstance(value, dict):
            raise ValueError(f"RelationshipDraft {name} slice item is invalid")
        values.append(value)
    return tuple(values)


def _relationship_evaluation(
    *, material: dict[str, object], trigger_ref: str
) -> dict[str, str] | None:
    """Read the dedicated compact relation view, if this lane requested one.

    Its source descriptors and full-value hashes are bound into the trusted
    capsule.  The adapter uses it only to keep the exact triggering appraisal
    and current relationship head available when generic slices are budgeted
    away; relationship authority remains compiler/reducer-owned.
    """

    value = material.get("relationship_evaluation")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("RelationshipDraft compact relationship view is invalid")
    required = (
        "subject_ref",
        "trigger_appraisal_id",
        "appraisal_summary_json",
        "relationship_summary_json",
        "appraisal_source",
    )
    if any(not isinstance(value.get(name), str) or not value[name] for name in required[:-1]):
        raise ValueError("RelationshipDraft compact relationship view is incomplete")
    if not isinstance(value["appraisal_source"], dict):
        raise ValueError("RelationshipDraft compact relationship evidence is invalid")
    source_bindings = value["appraisal_source"].get("source_bindings")
    if not isinstance(source_bindings, list) or not source_bindings:
        raise ValueError("RelationshipDraft compact relationship evidence is missing")
    # The view is valid only for the exact accepted appraisal that triggered
    # this background job.  Binding the source event prevents a compact view
    # from accidentally becoming a generic, stale relationship summary.
    if not any(binding.get("ref") == trigger_ref for binding in source_bindings if isinstance(binding, dict)):
        raise ValueError("RelationshipDraft compact relationship view is not trigger-bound")
    return {
        "subject_ref": str(value["subject_ref"]),
        "appraisal_summary_json": str(value["appraisal_summary_json"]),
        "relationship_summary_json": str(value["relationship_summary_json"]),
    }


def _trimmed(value: str, *, limit: int = 300) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _lenient_slice_values(
    material: dict[str, object], name: str
) -> tuple[dict[str, object], ...]:
    """Read one slice's item values without widening any failure envelope.

    Context enrichment is strictly optional: a missing, unavailable, or
    oddly shaped slice contributes nothing rather than failing a draft that
    version 1 would have completed on the trigger appraisal alone.
    """

    slices = material.get("slices")
    if not isinstance(slices, dict):
        return ()
    slice_ = slices.get(name)
    if not isinstance(slice_, dict) or slice_.get("availability") != "available":
        return ()
    items = slice_.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(
        item["value"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("value"), dict)
    )


def _context_summaries(
    *, material: dict[str, object], trigger_ref: str
) -> dict[str, tuple[str, ...]]:
    """Harvest bounded, read-only conversational texture from capsule slices.

    Everything here is already resolver-verified capsule material; this only
    re-shapes it into short summaries.  A slice that the capsule budget marked
    unavailable simply contributes an empty tuple - the draft still runs on
    the trigger appraisal alone, exactly like draft version 1.
    """

    dialogue: list[str] = []
    for item in _lenient_slice_values(material, "recent_dialogue"):
        speaker = item.get("speaker")
        text = item.get("text")
        if speaker in {"counterpart", "companion"} and isinstance(text, str) and text:
            dialogue.append(_trimmed(f"{speaker}: {text}", limit=200))
    appraisal_lines: list[str] = []
    for item in _lenient_slice_values(material, "appraisals"):
        origin = item.get("origin")
        if isinstance(origin, dict) and origin.get("accepted_event_ref") == trigger_ref:
            continue  # The trigger appraisal is already the capsule's centrepiece.
        hypotheses = item.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses:
            continue
        parts = [
            f"{hypothesis.get('meaning')}({hypothesis.get('severity')})"
            for hypothesis in hypotheses
            if isinstance(hypothesis, dict) and hypothesis.get("meaning")
        ]
        if not parts:
            continue
        accepted_at = item.get("accepted_at")
        stamp = f"{accepted_at[:16]} " if isinstance(accepted_at, str) else ""
        appraisal_lines.append(_trimmed(f"{stamp}{' '.join(parts)}"))
    affect_lines: list[str] = []
    for item in _lenient_slice_values(material, "affect_episodes"):
        if item.get("status") != "active":
            continue
        components = item.get("components")
        if not isinstance(components, list):
            continue
        parts = [
            f"{component.get('dimension')} {component.get('intensity_bp')}bp"
            for component in components
            if isinstance(component, dict) and component.get("dimension")
        ]
        if parts:
            affect_lines.append(_trimmed(", ".join(parts)))
    return {
        "recent_dialogue_summaries": tuple(dialogue[-12:]),
        "recent_appraisal_summaries": tuple(appraisal_lines[-8:]),
        "active_affect_summaries": tuple(affect_lines[-8:]),
    }


def _counterpart_subject(
    *,
    material: dict[str, object],
    appraisals: tuple[dict[str, object], ...] | None,
    trigger_ref: str,
) -> str | None:
    """Read the one counterpart from the exact relationship capsule slice.

    An empty or unavailable relationship slice is a normal no-change outcome;
    malformed or ambiguous available authority is not silently guessed.
    """

    relationships = _slice_items(material, "relationship_slice")
    actor = material.get("actor_ref")
    if not isinstance(actor, str) or not actor:
        raise ValueError("RelationshipDraft context actor is invalid")
    if not relationships:
        # A missing persisted head means only that this is the first accepted
        # signal for this counterpart, not that the relation is unknowable.
        # The appraisal trigger is already the exact source of the current
        # interaction, so it is the only safe place to derive a virtual
        # stranger view. This writes no relationship state and never guesses a
        # counterpart from prose or a model response.
        matches = []
        for appraisal in appraisals or ():
            origin = appraisal.get("origin")
            subject = appraisal.get("subject_ref")
            if (
                isinstance(origin, dict)
                and origin.get("accepted_event_ref") == trigger_ref
                and isinstance(subject, str)
                and subject
            ):
                matches.append(subject)
        if len(matches) != 1 or matches[0] == actor:
            return None
        return matches[0]
    if len(relationships) != 1:
        raise ValueError("RelationshipDraft requires exactly one counterpart subject")
    subjects = {
        value.get("subject_ref")
        for value in relationships
        if isinstance(value.get("subject_ref"), str) and value.get("subject_ref")
    }
    if len(subjects) != 1:
        raise ValueError("RelationshipDraft requires exactly one counterpart subject")
    subject_ref = next(iter(subjects))
    if subject_ref == actor:
        raise ValueError("RelationshipDraft counterpart subject cannot be the companion actor")
    return subject_ref


def _draft_capsule(
    *,
    material: dict[str, object],
    request: ModelInput,
    context: dict[str, tuple[str, ...]],
) -> RelationshipEvaluationDraftCapsule:
    relationships = _slice_items(material, "relationship_slice")
    appraisals = _slice_items(material, "appraisals")
    if appraisals is None:
        raise ValueError("RelationshipDraft requires an available appraisal slice")
    matches = []
    for appraisal in appraisals:
        origin = appraisal.get("origin")
        if isinstance(origin, dict) and origin.get("accepted_event_ref") == request.trigger_ref:
            matches.append(appraisal)
    if len(matches) != 1:
        raise ValueError("RelationshipDraft requires exactly one accepted appraisal from its trigger")
    relationship = (
        relationships[0]
        if relationships and len(relationships) == 1
        else {
            "stage": "stranger",
            "variables": {
                "trust_bp": 0,
                "closeness_bp": 0,
                "respect_bp": 0,
                "reliability_bp": 0,
                "mutuality_bp": 0,
                "repair_confidence_bp": 0,
            },
            "temperature": "ordinary",
        }
    )
    # IDs, accepted-event refs, policies, and revisions remain compiler-only.
    relationship_summary = {
        key: relationship[key]
        for key in ("stage", "variables", "temperature")
        if key in relationship
    }
    appraisal = matches[0]
    appraisal_summary = {
        key: appraisal[key]
        for key in ("hypotheses", "confidence_bp", "expires_at", "status")
        if key in appraisal
    }
    if not relationship_summary or not appraisal_summary:
        raise ValueError("RelationshipDraft capsule summaries are incomplete")
    return RelationshipEvaluationDraftCapsule(
        accepted_appraisal_summary=_canonical(appraisal_summary),
        relationship_summary=_canonical(relationship_summary),
        **context,
    )


def _trigger_evidence(request: ModelInput) -> ProposalEvidenceRef:
    values = tuple(
        item
        for item in request.trigger_evidence
        if item.ref_id == request.trigger_ref and item.evidence_kind == "committed_world_event"
    )
    if len(values) != 1:
        raise ValueError("RelationshipDraft requires one accepted-appraisal trigger evidence")
    return values[0]


def _proposal_from_draft(
    *, draft: RelationshipEvaluationDraft, request: ModelInput, subject_ref: str
) -> dict[str, object]:
    if draft.decision == "no_change":
        return _no_change(request=request, rationale="relationship_no_change")
    evidence = _trigger_evidence(request)
    assert draft.signal_code is not None
    assert draft.confidence_bp is not None
    assert draft.persistence is not None
    assert draft.rationale_code is not None
    assert draft.suggested_deltas is not None
    payload = {
        "subject_ref": subject_ref,
        "signal_code": draft.signal_code,
        "confidence_bp": draft.confidence_bp,
        "persistence": draft.persistence,
        "rationale_code": draft.rationale_code,
        "suggested_deltas": draft.suggested_deltas.model_dump(mode="json"),
    }
    identity = _digest(
        {
            "contract": _CONTRACT,
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "trigger_hash": evidence.immutable_hash,
            "subject_ref": subject_ref,
            "draft": json.loads(draft.normalized_json),
        }
    )
    proposal = DecisionProposal(
        proposal_id=f"proposal:relationship-draft:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(evidence,),
        proposed_changes=(
            TypedChange(
                change_id=f"change:relationship-draft:{identity}",
                kind="relationship_signal",
                target_id=f"relationship-subject:{subject_ref}",
                transition="suggest",
                evidence_refs=(evidence.ref_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="relationship_signal.v1", value=payload
                ),
            ),
        ),
        action_intents=(),
        confidence=draft.confidence_bp,
        brief_rationale=draft.rationale_code,
        affect_decision="no_change",
        behavior_tendency="observe",
        stance="relationship_signal_pending",
        display_strategy="withhold",
    )
    return proposal.model_dump(mode="json")


def _no_change(*, request: ModelInput, rationale: str) -> dict[str, object]:
    identity = _digest(
        {
            "contract": _CONTRACT,
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "decision": "no_change",
            "rationale": rationale,
        }
    )
    return DecisionProposal(
        proposal_id=f"proposal:relationship-draft:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(),
        proposed_changes=(),
        action_intents=(),
        confidence=0,
        brief_rationale=rationale,
        affect_decision="no_change",
        behavior_tendency="observe",
        stance="wait",
        display_strategy="withhold",
    ).model_dump(mode="json")


__all__ = ["RelationshipDraftDeliberationAdapter"]
