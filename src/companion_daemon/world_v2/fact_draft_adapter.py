"""Bounded model extraction of one source-backed Fact-v2 proposal.

The model decides only whether an explicit user assertion is worth retaining
and how to classify it under an installed predicate.  All authority-bearing
identities, evidence, hashes, policy refs and Fact-v2 envelope fields are
derived by this adapter from one committed observation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from .fact_reducers import INSTALLED_FACT_PREDICATE_CARDINALITY
from .proposal_envelope_v2 import (
    FactCommitProposalDraftV2,
    FactCommitProposalEnvelopeV2,
    FactCommitProposalNormalizationContextV2,
    normalize_fact_commit_proposal_v2,
)
from .schemas import Observation, WorldEvent


class FactDraftChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _parse(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("FactDraft model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("FactDraft model did not return one JSON object")
    return value


class FactObservationProposalAdapter:
    """Materialize at most one Fact-v2 proposal from an exact message event."""

    VERSION = "fact-observation-draft.1"

    def __init__(self, *, model: FactDraftChatModel, temperature: float = 0.1) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("FactDraft temperature must be between 0 and 2")
        self._model = model
        self._temperature = temperature

    async def propose(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        source_world_revision: int,
        evaluated_world_revision: int | None = None,
    ) -> FactCommitProposalEnvelopeV2 | None:
        raw = await self._model.complete(
            self._messages(observation), temperature=self._temperature
        )
        return materialize_fact_observation_draft(
            raw=raw,
            observation=observation,
            observation_event=observation_event,
            source_world_revision=source_world_revision,
            evaluated_world_revision=evaluated_world_revision,
        )

    @staticmethod
    def _messages(observation: Observation) -> list[dict[str, str]]:
        predicates = ", ".join(sorted(INSTALLED_FACT_PREDICATE_CARDINALITY))
        return [
            {
                "role": "system",
                "content": (
                    "Assess one verified user message for one durable factual assertion. "
                    "Return exactly one JSON object. Use retain=false for ordinary chat, temporary "
                    "feelings, speculation, or anything not explicitly stated. If retain=true return "
                    "predicate_code, value, privacy_class, confidence, rationale. value must be an exact "
                    "non-empty substring of the message, never a paraphrase. subject is fixed to the message "
                    "author. Allowed predicates: "
                    + predicates
                    + ". Do not return ids, hashes, evidence refs, actions, memories, or world changes."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "observation_id": observation.observation_id,
                        "actor": observation.actor,
                        "text": observation.text,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


def materialize_fact_observation_draft(
    *,
    raw: str,
    observation: Observation,
    observation_event: WorldEvent,
    source_world_revision: int,
    evaluated_world_revision: int | None = None,
) -> FactCommitProposalEnvelopeV2 | None:
    """Derive a closed Fact-v2 proposal from one exact model draft and event."""

    if (
        observation_event.event_type != "ObservationRecorded"
        or observation_event.world_id != observation.world_id
        or observation.text is None
        or source_world_revision < 1
    ):
        raise ValueError("FactDraft requires an exact committed message observation")
    if evaluated_world_revision is None:
        evaluated_world_revision = source_world_revision
    if evaluated_world_revision < source_world_revision:
        raise ValueError("FactDraft evaluation cannot precede its source observation")
    draft = _parse(raw)
    retain = draft.get("retain")
    if not isinstance(retain, bool):
        raise ValueError("FactDraft retain must be boolean")
    if not retain:
        if set(draft) != {"retain"}:
            raise ValueError("FactDraft no-change may contain only retain")
        return None
    predicate = draft.get("predicate_code")
    value = draft.get("value")
    privacy = draft.get("privacy_class")
    confidence = draft.get("confidence")
    rationale = draft.get("rationale")
    if (
        not isinstance(predicate, str)
        or predicate not in INSTALLED_FACT_PREDICATE_CARDINALITY
        or not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value not in observation.text
        or privacy not in {"public", "shareable", "personal", "private", "withhold"}
        or isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 10_000
        or not isinstance(rationale, str)
        or not 1 <= len(rationale) <= 240
    ):
        raise ValueError("FactDraft fields are invalid or not source-grounded")
    identity = _digest(
        {
            "contract": "fact-observation-draft.1",
            "world_id": observation.world_id,
            "event_id": observation_event.event_id,
            "event_hash": observation_event.payload_hash,
            "predicate": predicate,
            "value": value,
        }
    )
    proposal_id = f"proposal:fact-observation:{identity}"
    value_digest = hashlib.sha256(value.encode()).hexdigest()
    draft_value = FactCommitProposalDraftV2.model_validate(
        {
            "fact_commit_intents": (
                {
                    "subject_ref": observation.actor,
                    "predicate_code": predicate,
                    "value_ref": f"value:observation:{value_digest}",
                    "value_hash": f"sha256:{value_digest}",
                    "assertion_source_ref": observation.observation_id,
                    "evidence_uses": (
                        {
                            "evidence_ref": observation.observation_id,
                            "purpose": "current_fact",
                            "anchor": True,
                        },
                    ),
                    "confidence_bp": confidence,
                    "privacy_class": privacy,
                },
            ),
            "confidence": confidence,
            "brief_rationale": rationale,
        },
        strict=True,
    )
    context = FactCommitProposalNormalizationContextV2.model_validate(
        {
            "world_id": observation.world_id,
            "proposal_id": proposal_id,
            "trigger_ref": observation_event.event_id,
            "evaluated_world_revision": evaluated_world_revision,
            "evidence_refs": (
                {
                    "ref_id": observation.observation_id,
                    "evidence_kind": "observed_message",
                    "source_world_revision": source_world_revision,
                    "immutable_hash": f"sha256:{observation_event.payload_hash}",
                },
            ),
            "policy_refs": ("policy:fact-commit.2",),
        },
        strict=True,
    )
    return normalize_fact_commit_proposal_v2(draft=draft_value, context=context)


__all__ = ["FactObservationProposalAdapter", "FactDraftChatModel", "materialize_fact_observation_draft"]
