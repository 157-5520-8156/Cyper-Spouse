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
from .model_json import extract_json_object_text
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
        value = json.loads(extract_json_object_text(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("FactDraft model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("FactDraft model did not return one JSON object")
    return value


# One short model-facing gloss per installed predicate.  The extraction model
# only ever sees these strings; authority stays with the reducer catalog.  A
# test keeps this map exactly in sync with INSTALLED_FACT_PREDICATE_CARDINALITY.
_PREDICATE_GUIDE: dict[str, str] = {
    "location.current": "where the user is right now",
    "profile.display_name": "the user's name or what they want to be called",
    "profile.timezone": "the user's timezone",
    "preference.likes": "a food, thing, or style the user says they like",
    "preference.dislikes": "a food, thing, or style the user says they dislike",
    "relationship.affiliation": "a group, school, company, or team the user belongs to",
    "profile.occupation": "the user's job or professional identity",
    "profile.education": "the user's study stage, school, or major",
    "location.home": "the city or area where the user lives",
    "location.hometown": "where the user is from",
    "schedule.commitment": "a dated or upcoming plan, appointment, contest, exam, or trip the user states",
    "situation.recent": "a recent life circumstance or notable thing that happened to the user",
    "activity.current": "what the user says they are doing right now",
    "relationship.person": "a family member, friend, or colleague in the user's life",
    "health.condition": "a health condition, allergy, or injury the user states",
    "routine.habit": "a recurring habit or sleep/wake routine the user states",
    "interest.activity": "a hobby or activity the user does or practices",
    "possession.item": "an item, device, or pet the user owns",
}


class FactObservationProposalAdapter:
    """Materialize at most one Fact-v2 proposal from an exact message event."""

    # Version 2 (2026-07-20): the version-1 policy retained only explicit,
    # formal self-assertions and, over a four-day production world, committed
    # zero facts from 63 user message batches.  Version 2 retains any clearly
    # stated personal fact (still never an inference) and teaches the model
    # the expanded predicate catalog.  The proposal identity contract below is
    # unchanged: the digest material and derivation are identical.
    VERSION = "fact-observation-draft.2"

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
        messages = self._messages(observation)
        raw = await self._complete(messages)
        try:
            return materialize_fact_observation_draft(
                raw=raw,
                observation=observation,
                observation_event=observation_event,
                source_world_revision=source_world_revision,
                evaluated_world_revision=evaluated_world_revision,
            )
        except ValueError as violation:
            # One bounded corrective pass.  A user identity fact stated once
            # ("my name is ...") never restates itself, so silently consuming
            # the trigger on a fixable format slip loses it forever.  The
            # retry only restates the violated contract; every field is still
            # strictly validated, and a second failure propagates unchanged.
            retry_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": (
                        "Your answer violated the contract: "
                        + str(violation)
                        + ". Return exactly one corrected JSON object now. Remember: value must "
                        "be an exact non-empty substring copied from the message text, confidence "
                        "is an integer 0..10000, and retain=false answers contain only "
                        '{"retain":false}.'
                    ),
                },
            ]
            corrected = await self._complete(retry_messages)
            return materialize_fact_observation_draft(
                raw=corrected,
                observation=observation,
                observation_event=observation_event,
                source_world_revision=source_world_revision,
                evaluated_world_revision=evaluated_world_revision,
            )

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        complete_json = getattr(self._model, "complete_json", None)
        return await (
            complete_json(messages, temperature=self._temperature)
            if callable(complete_json)
            else self._model.complete(messages, temperature=self._temperature)
        )

    @staticmethod
    def _messages(observation: Observation) -> list[dict[str, str]]:
        predicates = "\n".join(
            f"- {code} ({INSTALLED_FACT_PREDICATE_CARDINALITY[code]}): {_PREDICATE_GUIDE[code]}"
            for code in sorted(INSTALLED_FACT_PREDICATE_CARDINALITY)
        )
        return [
            {
                "role": "system",
                "content": (
                    "You maintain the long-term user-fact memory of a companion character. Assess one "
                    "verified user message for one personal fact about the user worth remembering. "
                    "Return exactly one JSON object. Retain a fact when the message clearly states "
                    "something about the user's life: their work or studies, schedule and commitments, "
                    "recent circumstances, what they are doing, family and friends, health and routines, "
                    "interests, possessions, or where they live. A casual sentence counts as clearly "
                    "stated; it does not need to be a formal self-introduction (\"明天还得打国赛\" states a "
                    "scheduled contest, \"在写代码\" states a current activity). Never infer, guess, or add "
                    "anything beyond the words: greetings, questions to the companion, jokes, emoji, bare "
                    "momentary feelings (\"有点紧张\"), and remarks about the companion are retain=false. "
                    "If several facts appear, keep the most durable and informative one. "
                    "Answer {\"retain\":false} when nothing qualifies. If retain=true return "
                    "predicate_code, value, privacy_class, confidence, rationale. confidence must be an "
                    "integer in basis points from 0 through 10000 (for example 9500, never 0.95). value must be an exact "
                    "non-empty substring of the message, never a paraphrase; choose the shortest substring "
                    "that still states the fact. subject is fixed to the message "
                    "author. A direct-message Fact must use personal, private, or withhold privacy; never public "
                    "or shareable. predicate_code must be one of:\n"
                    + predicates
                    + "\nDo not return ids, hashes, evidence refs, actions, memories, or world changes."
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
        isinstance(confidence, float)
        and not isinstance(confidence, bool)
        and 0.0 <= confidence <= 1.0
    ):
        confidence = round(confidence * 10_000)
    # A direct-message observation has a hard ``personal`` visibility floor
    # in Fact authority.  The classifier may choose a stricter class, but a
    # broad ``public``/``shareable`` suggestion is safely tightened here
    # before it can create an audit that the reducer must reject.
    if privacy in {"public", "shareable"}:
        privacy = "personal"
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
            # Deliberately still the version-1 contract label: the identity
            # material and derivation are unchanged in adapter version 2, and
            # keeping the label stable lets crash recovery join audits that
            # were recorded before the extraction-policy upgrade.
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
