"""Materialize a bounded affect draft after one accepted appraisal.

The model selects only whether an affect episode should open and its component
intensities.  Trusted code derives the exact accepted-appraisal binding,
evidence, decay selectors, identities, and proposal envelope.
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


_DIMENSIONS = frozenset(
    {"hurt", "anger", "sadness", "loneliness", "anxiety", "resentment", "warmth", "joy"}
)


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parse_object(raw: str) -> dict[str, object]:
    if not isinstance(raw, str):
        raise ValueError("affect model did not return text")
    value = raw.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("affect model returned an unclosed JSON fence")
        value = "\n".join(lines[1:-1]).strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("affect model did not return one JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError("affect model did not return one JSON object")
    return parsed


class AffectDraftDeliberationAdapter:
    VERSION = "affect-draft-adapter.1"

    def __init__(
        self, *, model: ChatCompletionModel, model_id: str | None = None, temperature: float = 0.2
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("affect adapter temperature must be between 0 and 2")
        self._model = model
        self._model_id = model_id or str(getattr(model, "model", "chat-affect"))
        self._temperature = temperature

    async def propose(self, request: ModelInput) -> ModelOutput:
        raw = await self._model.complete(self._messages(request), temperature=self._temperature)
        return ModelOutput(
            model_id=self._model_id,
            model_version=self.VERSION,
            raw_proposal=_proposal_from_draft(raw=raw, request=request),
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        return ModelOutput(
            model_id=self._model_id,
            model_version=self.VERSION,
            raw_proposal=_no_change(request=request, rationale=f"Affect model unavailable: {failure_code[:96]}"),
        )

    @staticmethod
    def _messages(request: ModelInput) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "You deliberate privately after one accepted appraisal for a virtual companion. Return exactly "
                    "one JSON object, never Markdown. Return AffectDraft with affect ('no_change' or 'open'), "
                    "brief_rationale, behavior_tendency, stance, display_strategy, and confidence (0-10000). "
                    "When affect is open, return components: 1-8 unique items with dimension one of "
                    + ", ".join(sorted(_DIMENSIONS))
                    + " and intensity_bp (1-10000). Do not return appraisal references, evidence, IDs, hashes, "
                    "decay policies, Actions, memories, or world mutations. An affect is fallible and not a fact "
                    "about the user; choose no_change when it should not persist."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"request": request.model_dump(mode="json")},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


def _proposal_from_draft(*, raw: str, request: ModelInput) -> dict[str, object]:
    draft = _parse_object(raw)
    affect = draft.get("affect")
    rationale, confidence, tendency, stance, display = _common(draft)
    if affect == "no_change":
        return _no_change(
            request=request,
            rationale=rationale,
            confidence=confidence,
            tendency=tendency,
            stance=stance,
            display=display,
        )
    if affect != "open":
        raise ValueError("AffectDraft affect must be no_change or open")
    evidence = _trigger_evidence(request)
    appraisal_change_id = _accepted_appraisal_change_id(request)
    components = _components(draft.get("components"))
    identity = _identity(
        request=request, affect="open", rationale=rationale, components=components
    )
    proposal_id = f"proposal:affect-draft:{identity}"
    change_id = f"change:affect-draft:{identity}"
    payload = {
        "episode_id": f"affect:affect-draft:{identity}",
        "appraisal_change_refs": [appraisal_change_id],
        "component_deltas": components,
        "decay_config": {
            "object_ref": "policy:decay:standard",
            "schema_version": "affect-decay.1",
            "payload_hash": "sha256:" + _digest("policy:decay:standard"),
        },
        "residue_config": {
            "object_ref": "policy:residue:standard",
            "schema_version": "affect-residue.1",
            "payload_hash": "sha256:" + _digest("policy:residue:standard"),
        },
    }
    proposal = DecisionProposal(
        proposal_id=proposal_id,
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(evidence,),
        proposed_changes=(
            TypedChange(
                change_id=change_id,
                kind="affect_transition",
                target_id=payload["episode_id"],
                expected_entity_revision=0,
                transition="open",
                evidence_refs=(evidence.ref_id,),
                payload=CanonicalTypedPayload.from_value(
                    payload_schema="affect_transition.v1", value=payload
                ),
            ),
        ),
        action_intents=(),
        confidence=confidence,
        brief_rationale=rationale,
        affect_decision="propose",
        behavior_tendency=tendency,
        stance=stance,
        display_strategy=display,
    )
    return proposal.model_dump(mode="json")


def _common(draft: dict[str, object]) -> tuple[str, int, str, str, str]:
    rationale = draft.get("brief_rationale")
    confidence = draft.get("confidence")
    values = (draft.get("behavior_tendency"), draft.get("stance"), draft.get("display_strategy"))
    if (
        not isinstance(rationale, str)
        or not 1 <= len(rationale) <= 240
        or isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 10_000
        or any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in values)
    ):
        raise ValueError("AffectDraft common fields are invalid")
    return rationale, confidence, values[0], values[1], values[2]  # type: ignore[return-value]


def _components(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= len(_DIMENSIONS):
        raise ValueError("AffectDraft components are invalid")
    result: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("AffectDraft component is invalid")
        dimension, intensity = item.get("dimension"), item.get("intensity_bp")
        if (
            not isinstance(dimension, str)
            or dimension not in _DIMENSIONS
            or isinstance(intensity, bool)
            or not isinstance(intensity, int)
            or not 1 <= intensity <= 10_000
        ):
            raise ValueError("AffectDraft component is invalid")
        result.append({"name": dimension, "value": intensity})
    if len({item["name"] for item in result}) != len(result):
        raise ValueError("AffectDraft dimensions must be unique")
    return result


def _trigger_evidence(request: ModelInput) -> ProposalEvidenceRef:
    values = tuple(
        item
        for item in request.trigger_evidence
        if item.ref_id == request.trigger_ref and item.evidence_kind == "committed_world_event"
    )
    if len(values) != 1:
        raise ValueError("AffectDraft requires one accepted-appraisal trigger evidence")
    return values[0]


def _accepted_appraisal_change_id(request: ModelInput) -> str:
    try:
        capsule = json.loads(request.model_content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("AffectDraft context is invalid") from exc
    candidates: set[str] = set()
    pending = [capsule]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            origin = value.get("origin")
            if isinstance(origin, dict) and origin.get("accepted_event_ref") == request.trigger_ref:
                change_id = origin.get("change_id")
                if isinstance(change_id, str) and change_id:
                    candidates.add(change_id)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if len(candidates) != 1:
        raise ValueError("AffectDraft requires exactly one active appraisal from its trigger")
    return next(iter(candidates))


def _identity(*, request: ModelInput, affect: str, rationale: str, components: object = ()) -> str:
    evidence = _trigger_evidence(request)
    return _digest(
        {
            "contract": "affect-draft-materialization.1",
            "call_id": request.call_id,
            "trigger_ref": request.trigger_ref,
            "world_revision": request.evaluated_world_revision,
            "trigger_hash": evidence.immutable_hash,
            "affect": affect,
            "rationale": rationale,
            "components": components,
        }
    )


def _no_change(
    *,
    request: ModelInput,
    rationale: str,
    confidence: int = 0,
    tendency: str = "observe",
    stance: str = "wait",
    display: str = "withhold",
) -> dict[str, object]:
    identity = _identity(request=request, affect="no_change", rationale=rationale)
    proposal = DecisionProposal(
        proposal_id=f"proposal:affect-draft:{identity}",
        trigger_ref=request.trigger_ref,
        evaluated_world_revision=request.evaluated_world_revision,
        evidence_refs=(),
        proposed_changes=(),
        action_intents=(),
        confidence=confidence,
        brief_rationale=rationale,
        affect_decision="no_change",
        behavior_tendency=tendency,
        stance=stance,
        display_strategy=display,
    )
    return proposal.model_dump(mode="json")


__all__ = ["AffectDraftDeliberationAdapter"]
