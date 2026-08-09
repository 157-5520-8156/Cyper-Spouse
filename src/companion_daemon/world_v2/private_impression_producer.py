"""Background producer for Private Impressions (CONTEXT.md).

A Private Impression is "the companion's fallible, source-bound
interpretation of a user, relationship, or event".  The typed authority
(``private_impression_transition`` proposal family, ``PrivateImpressionAccepted``
event, pure reducer, capsule ``private_impressions`` slice) has existed since
the impression reducers landed; this module adds the missing *producer*
vertical, following the interaction-fact worker's discipline:

* a deterministic opener leaves at most one recoverable trigger per accepted
  appraisal (the anchor is the committed ``AppraisalAccepted`` event);
* the unified Character Interior may *reflect on* already-accepted appraisal
  hypotheses and write its own tentative private understanding.  It selects
  the exact source hypotheses, confidence and lifecycle, while immutable
  source identities and evidence remain outside model authority;
* the runtime offers one source-bound capability and drives the existing typed
  acceptance authority.  The accepted
  impression then reaches later turns only through the capsule's private
  slice (privacy class ``withhold``); it is never shown to the user.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import Field
from pydantic_core import to_jsonable_python

from .batch_invariants import private_impression_trigger_identity
from .companion_identity import CompanionIdentityFrame
from .character_interior import CharacterInterior, InteriorStimulus
from .character_interior.audit import recorded_character_interior_model_result
from .character_interior.contracts import (
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
)
from .character_interior.ports import _AuthorityRequest
from .character_interior.run_result import CausalOpportunityIdentity
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .model_json import extract_json_object_text
from .private_impression_events import (
    PRIVATE_IMPRESSION_POLICY_REFS,
    PrivateImpressionPredecessorRef,
    offered_private_impression_reflection_bindings,
    private_impression_mutation_hash,
    private_impression_reflection_value_digest,
)
from .proposal_audit_schemas import (
    ModelResultRecordedPayload,
    ProposalRecordedV2Payload,
    RecordedModelResultAudit,
    RecordedModelRoute,
    RecordedCharacterInteriorTurnLineage,
    canonical_json,
    model_audit_json,
    sha256,
)
from .proposal_envelope import DecisionProposal
from .schema_core import FrozenModel
from .schemas import (
    AppraisalMeaningRef,
    AppraisalProjection,
    ClaimLease,
    EvidenceRef,
    PrivateImpressionOrigin,
    PrivateImpressionProjection,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


EXPIRY_CONDITIONS = (
    "until_appraisal_contradicted",
    "until_counter_evidence",
    "until_relationship_stage_changes",
    "one_month_without_support",
)

# Bound the number of times one private-reflection trigger may be reclaimed
# after its model output failed authority validation.  Lease expiry alone
# would otherwise retry the same failing provider call forever, burning
# unbounded tokens on a reflection that never succeeds; after this many
# attempts the process is terminal and the next opener pass derives a fresh
# trigger if the appraisal is still meaningful.
_PRIVATE_IMPRESSION_MAX_ATTEMPTS = 4


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()




class PrivateImpressionDraft(FrozenModel):
    """One role-authored, non-factual reflection over accepted hypotheses."""

    decision: Literal["retain", "consolidate", "supersede"] = "retain"
    predecessor_refs: tuple[str, ...] = ()
    source_refs: tuple[str, ...]
    reflection_summary: str = Field(min_length=1, max_length=1_200)
    confidence_bp: int
    expiry_condition: Literal[
        "until_appraisal_contradicted",
        "until_counter_evidence",
        "until_relationship_stage_changes",
        "one_month_without_support",
    ]


def _reflection_draft_digest(draft: PrivateImpressionDraft) -> str:
    return private_impression_reflection_value_digest(
        decision=draft.decision,
        predecessor_refs=draft.predecessor_refs,
        source_refs=draft.source_refs,
        reflection_summary=draft.reflection_summary,
        confidence_bp=draft.confidence_bp,
        expiry_condition=draft.expiry_condition,
    )


class PrivateImpressionReflectionSource(FrozenModel):
    """One source-bound item in the pinned private reflection capsule."""

    source_ref: str = Field(min_length=1, max_length=512)
    source_kind: Literal[
        "appraisal",
        "character_core",
        "relationship",
        "affect",
        "experience",
        "existing_impression",
    ]
    authority_event_ref: str = Field(min_length=1, max_length=512)
    value_json: str = Field(min_length=2, max_length=8_192)


class PrivateImpressionReflectionCapsule(FrozenModel):
    """Cursor-pinned, multi-layer context offered to the role model."""

    capsule_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    world_id: str = Field(min_length=1)
    world_revision: int = Field(ge=0)
    deliberation_revision: int = Field(ge=0)
    ledger_sequence: int = Field(ge=0)
    logical_time: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    anchor_appraisal_id: str = Field(min_length=1)
    identity_frame: CompanionIdentityFrame
    sources: tuple[PrivateImpressionReflectionSource, ...] = Field(
        min_length=1,
        max_length=48,
    )








def _materialize_draft(
    raw: str,
    *,
    capsule: PrivateImpressionReflectionCapsule,
) -> PrivateImpressionDraft | None:
    try:
        value = json.loads(extract_json_object_text(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("private impression model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("private impression model did not return one JSON object")
    decision = value.get("decision")
    legacy_retain = value.get("retain")
    if decision is None and isinstance(legacy_retain, bool):
        decision = "retain" if legacy_retain else "no_change"
        legacy_shape = True
    else:
        legacy_shape = False
    if decision not in {"no_change", "retain", "consolidate", "supersede"}:
        raise ValueError("private impression decision is invalid")
    if decision == "no_change":
        expected = {"retain"} if legacy_shape else {"decision"}
        if set(value) != expected:
            raise ValueError("private impression no-change may contain only its decision")
        return None
    predecessor_refs = value.get("predecessor_refs", [])
    source_refs = value.get("source_refs")
    reflection_summary = value.get("reflection_summary")
    confidence = value.get("confidence")
    expiry = value.get("expiry_condition")
    if (
        isinstance(confidence, float)
        and not isinstance(confidence, bool)
        and 0.0 <= confidence <= 1.0
    ):
        confidence = round(confidence * 10_000)
    # Map any short capability tokens ("s0", "s1", ...) back to the real
    # source refs.  The capability offers short tokens so any provider can
    # select a source without echoing a very long hash; the mapping here is
    # deterministic from capsule source order and idempotent (real refs are
    # not keys of the map).
    short_token_map = {
        f"s{index}": item.source_ref
        for index, item in enumerate(capsule.sources)
    }
    if isinstance(source_refs, list):
        source_refs = [short_token_map.get(item, item) for item in source_refs]
    if isinstance(predecessor_refs, list):
        predecessor_refs = [
            short_token_map.get(item, item) for item in predecessor_refs
        ]
    offered = {item.source_ref for item in capsule.sources}
    anchor_refs = {
        item.source_ref
        for item in capsule.sources
        if item.source_kind == "appraisal"
        and json.loads(item.value_json).get("appraisal_id") == capsule.anchor_appraisal_id
    }
    existing_refs = {
        item.source_ref for item in capsule.sources if item.source_kind == "existing_impression"
    }
    expected_fields = {
        "source_refs",
        "reflection_summary",
        "confidence",
        "expiry_condition",
        "retain" if legacy_shape else "decision",
    }
    if decision in {"consolidate", "supersede"}:
        expected_fields.add("predecessor_refs")
    if (
        set(value) != expected_fields
        or not isinstance(source_refs, list)
        or not source_refs
        or any(not isinstance(item, str) or item not in offered for item in source_refs)
        or len(source_refs) != len(set(source_refs))
        or not set(source_refs) & anchor_refs
        or not isinstance(predecessor_refs, list)
        or any(not isinstance(item, str) for item in predecessor_refs)
        or len(predecessor_refs) != len(set(predecessor_refs))
        or (
            decision in {"consolidate", "supersede"}
            and (
                not predecessor_refs
                or any(
                    not isinstance(item, str)
                    or item not in existing_refs
                    or item not in source_refs
                    for item in predecessor_refs
                )
            )
        )
        or (decision == "retain" and predecessor_refs)
        or not isinstance(reflection_summary, str)
        or not reflection_summary.strip()
        or len(reflection_summary) > 1_200
        or isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 10_000
        or expiry not in EXPIRY_CONDITIONS
    ):
        raise ValueError("private impression fields are invalid or not appraisal-grounded")
    # Preserve capsule source order so proposal identity is deterministic
    # across retries and provider output ordering cannot alter replay.
    ordered = tuple(
        item.source_ref for item in capsule.sources if item.source_ref in set(source_refs)
    )
    ordered_predecessors = tuple(
        item.source_ref for item in capsule.sources if item.source_ref in set(predecessor_refs)
    )
    return PrivateImpressionDraft(
        decision=decision,  # type: ignore[arg-type]
        predecessor_refs=ordered_predecessors,
        source_refs=ordered,
        reflection_summary=reflection_summary.strip(),
        confidence_bp=confidence,
        expiry_condition=expiry,  # type: ignore[arg-type]
    )


def compile_private_impression_reflection_capsule(
    *,
    projection: Any,
    appraisal: AppraisalProjection,
    identity_frame: CompanionIdentityFrame,
    world_id: str,
    content_reader: Callable[[str], str | None] | None = None,
) -> PrivateImpressionReflectionCapsule:
    """Compile bounded layered context without granting it mutation authority."""

    cursor = _cursor(projection)
    logical_time = projection.logical_time
    if logical_time is None:
        raise ValueError("private impression reflection requires authoritative time")
    sources: list[PrivateImpressionReflectionSource] = []

    def add(
        *,
        source_ref: str,
        source_kind: str,
        authority_event_ref: str,
        value: object,
    ) -> None:
        if not authority_event_ref or any(item.source_ref == source_ref for item in sources):
            return
        sources.append(
            PrivateImpressionReflectionSource(
                source_ref=source_ref,
                source_kind=source_kind,  # type: ignore[arg-type]
                authority_event_ref=authority_event_ref,
                value_json=canonical_json(to_jsonable_python(value)),
            )
        )

    related_appraisals = [
        item
        for item in projection.appraisals
        if item.status == "active" and item.subject_ref == appraisal.subject_ref
    ]
    ordered_appraisals = [
        appraisal,
        *(
            item
            for item in reversed(related_appraisals)
            if item.appraisal_id != appraisal.appraisal_id
        ),
    ][:8]
    for item in ordered_appraisals:
        for hypothesis in item.hypotheses:
            add(
                source_ref=f"appraisal:{item.appraisal_id}:{hypothesis.hypothesis_id}",
                source_kind="appraisal",
                authority_event_ref=item.origin.accepted_event_ref,
                value={
                    "appraisal_id": item.appraisal_id,
                    "subject_ref": item.subject_ref,
                    "source_cluster_ref": item.source_cluster_ref,
                    "confidence_bp": item.confidence_bp,
                    "hypothesis_id": hypothesis.hypothesis_id,
                    "meaning": hypothesis.meaning,
                    "attribution": hypothesis.attribution,
                    "severity": hypothesis.severity,
                    "weight_bp": hypothesis.weight_bp,
                    "accepted_change_id": item.origin.change_id,
                    "accepted_transition_id": item.origin.transition_id,
                },
            )

    core = projection.character_core
    if core is not None and core.origin is not None:
        add(
            source_ref=f"character-core:{core.core_id}:{core.entity_revision}",
            source_kind="character_core",
            authority_event_ref=core.origin.accepted_event_ref,
            value={
                "actor_ref": core.actor_ref,
                "values": core.values.model_dump(mode="json"),
            },
        )

    for relationship in reversed(projection.relationship_states):
        if relationship.subject_ref != appraisal.subject_ref or relationship.origin is None:
            continue
        add(
            source_ref=(
                f"relationship:{relationship.relationship_id}:{relationship.entity_revision}"
            ),
            source_kind="relationship",
            authority_event_ref=relationship.origin.accepted_event_ref,
            value={
                "stage": relationship.stage,
                "variables": relationship.variables.model_dump(mode="json"),
                "temperature": relationship.temperature,
                "commitment_refs": relationship.commitment_refs,
                "last_adjusted_at": relationship.last_adjusted_at,
            },
        )
        break

    appraisal_ids = {item.appraisal_id for item in related_appraisals}
    affect = [
        item
        for item in projection.affect_episodes
        if item.status == "active"
        and any(
            ref.appraisal_id in appraisal_ids
            for component in item.components
            for ref in component.appraisal_refs
        )
    ][-4:]
    for episode in reversed(affect):
        add(
            source_ref=f"affect:{episode.episode_id}:{episode.entity_revision}",
            source_kind="affect",
            authority_event_ref=episode.origin.accepted_event_ref,
            value={
                "components": [
                    {
                        "dimension": component.dimension,
                        "intensity_bp": component.intensity_bp,
                        "residue_bp": component.residue_bp,
                        "last_updated_at": component.last_updated_at,
                    }
                    for component in episode.components
                ],
                "updated_at": episode.updated_at,
            },
        )

    experiences = [
        item
        for item in projection.experiences
        if getattr(item, "status", None) == "committed"
        and appraisal.subject_ref in item.values.participant_refs
    ][-4:]
    for experience in reversed(experiences):
        add(
            source_ref=f"experience:{experience.experience_id}",
            source_kind="experience",
            authority_event_ref=experience.origin.accepted_event_ref,
            value={
                "summary_ref": experience.values.summary_ref,
                "summary_text": (
                    content_reader(experience.values.summary_ref)
                    if content_reader is not None
                    else None
                ),
                "occurred_from": experience.values.occurred_from,
                "occurred_to": experience.values.occurred_to,
                "participant_refs": experience.values.participant_refs,
            },
        )

    impressions = [
        item
        for item in projection.private_impressions
        if item.status == "active"
        and item.subject_ref == appraisal.subject_ref
        and item.origin is not None
    ][-6:]
    for impression in reversed(impressions):
        add(
            source_ref=f"private-impression:{impression.impression_id}",
            source_kind="existing_impression",
            authority_event_ref=impression.origin.accepted_event_ref,
            value={
                "reflection_summary": impression.reflection_summary,
                "confidence_bp": impression.confidence_bp,
                "last_supported": impression.last_supported,
                "expiry_condition": impression.expiry_condition,
                "interpretation_refs": impression.interpretation_refs,
            },
        )

    offered = offered_private_impression_reflection_bindings(
        projection,
        appraisal=appraisal,
    )
    by_ref = {item.source_ref: item for item in sources}
    if any(item.source_ref not in by_ref for item in offered):
        raise ValueError("private impression source manifest is incomplete")
    sources = [by_ref[item.source_ref] for item in offered]

    material = {
        "world_id": world_id,
        "world_revision": cursor.world_revision,
        "deliberation_revision": cursor.deliberation_revision,
        "ledger_sequence": cursor.ledger_sequence,
        "logical_time": logical_time.isoformat(),
        "subject_ref": appraisal.subject_ref,
        "anchor_appraisal_id": appraisal.appraisal_id,
        "identity_frame": identity_frame.model_dump(mode="json"),
        "sources": [item.model_dump(mode="json") for item in sources],
    }
    return PrivateImpressionReflectionCapsule(
        capsule_id=sha256(canonical_json(material)),
        world_id=world_id,
        world_revision=cursor.world_revision,
        deliberation_revision=cursor.deliberation_revision,
        ledger_sequence=cursor.ledger_sequence,
        logical_time=logical_time.isoformat(),
        subject_ref=appraisal.subject_ref,
        anchor_appraisal_id=appraisal.appraisal_id,
        identity_frame=identity_frame,
        sources=tuple(sources),
    )


def private_impression_opportunity(projection) -> tuple[str, str] | None:
    """Derive the newest open-able appraisal anchor from committed state.

    Returns ``(trigger_id, appraisal_accepted_event_ref)`` or ``None``.  An
    anchor is eligible while its appraisal is active, no trigger was ever
    opened for it (in any state), and no impression already interprets it.
    """

    if projection.logical_time is None:
        return None
    interpreted = {
        ref.split(":", 2)[1]
        for impression in projection.private_impressions
        for ref in impression.interpretation_refs
        if ref.startswith("appraisal:")
    }
    existing_triggers = {item.trigger_id for item in projection.trigger_processes}
    candidates = []
    for appraisal in projection.appraisals:
        if appraisal.status != "active" or appraisal.appraisal_id in interpreted:
            continue
        source_ref = appraisal.origin.accepted_event_ref
        committed = next(
            (item for item in projection.committed_world_event_refs if item.event_id == source_ref),
            None,
        )
        if committed is None or committed.event_type != "AppraisalAccepted":
            continue
        trigger_id = private_impression_trigger_identity(projection.world_id, source_ref)
        if trigger_id in existing_triggers:
            continue
        candidates.append((committed.world_revision, trigger_id, source_ref))
    if not candidates:
        return None
    _, trigger_id, source_ref = max(candidates)
    return trigger_id, source_ref


class PrivateImpressionTriggerOpener:
    """Commit at most one ``TriggerProcessOpened`` per accepted appraisal."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        owner_id: str,
        source: str = "world-v2:private-impression-trigger-opener",
    ) -> None:
        if not owner_id:
            raise ValueError("private impression opener needs an owner")
        self._ledger = ledger
        self._owner_id = owner_id
        self._source = source

    async def open_once(self) -> str | None:
        projection = await _project(self._ledger)
        opportunity = private_impression_opportunity(projection)
        if opportunity is None:
            return None
        trigger_id, source_ref = opportunity
        located = await _lookup(self._ledger, source_ref)
        if located is None or located[0].event_type != "AppraisalAccepted":
            raise ValueError("private impression anchor authority is unavailable")
        source_event = located[0]
        process = TriggerProcess(
            trigger_id=trigger_id,
            trigger_ref=f"impression:{source_ref}",
            process_kind="private_impression_deliberation",
            source_evidence_ref=source_ref,
            state="open",
        )
        payload = {"process": process.model_dump(mode="json")}
        identity = domain_idempotency_key(
            event_type="TriggerProcessOpened", world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("private impression trigger has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:private-impression:opened:"
            + _digest({"world_id": self._ledger.world_id, "trigger_id": trigger_id}),
            world_id=self._ledger.world_id,
            event_type="TriggerProcessOpened",
            logical_time=projection.logical_time,
            created_at=source_event.created_at,
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        await _commit(
            self._ledger,
            (event,),
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:private-impression:opened:" + _digest(trigger_id),
        )
        return trigger_id


class PrivateImpressionRunResult(FrozenModel):
    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "processed"]
    work_status: Literal["no_change", "accepted", "technical_failure"] | None = None


def _private_impression_capability(
    capsule: PrivateImpressionReflectionCapsule,
) -> _InteriorCapabilityManifest:
    anchor_source_refs = [
        item.source_ref
        for item in capsule.sources
        if item.source_kind == "appraisal"
        and json.loads(item.value_json).get("appraisal_id")
        == capsule.anchor_appraisal_id
    ]
    # The model must select exact source hypotheses.  The real source refs are
    # very long (compiled appraisal hashes), and asking a provider to echo one
    # verbatim is a reliable way to fail validation on any model.  Instead we
    # hand the model short, position-stable tokens ("s0", "s1", ...) and map
    # them back to the real refs at the validation boundary.  The map is
    # derived deterministically from capsule source order, so proposal
    # identity is stable across retries and provider output ordering.
    short_tokens = [f"s{index}" for index in range(len(capsule.sources))]
    existing_impression_short_tokens = [
        token
        for token, item in zip(short_tokens, capsule.sources, strict=True)
        if item.source_kind == "existing_impression"
    ]
    token_map = {
        token: item.source_ref
        for token, item in zip(short_tokens, capsule.sources, strict=True)
    }
    payload = {
        "contract": "character-interior-private-impression-capability.1",
        "reflection_capsule": capsule.model_dump(mode="json"),
        "reflection_sources": [
            {
                **item.model_dump(mode="json"),
                "short_token": short_tokens[index],
            }
            for index, item in enumerate(capsule.sources)
        ],
        "short_tokens": short_tokens,
        # Predecessors are a narrower role-owned choice than the offered
        # evidence set: only existing private impressions may be retired or
        # consolidated.  Keep that distinction in the provider contract so a
        # structurally valid tool call cannot select an appraisal/affect token
        # and wait for the host to reject it later.
        "existing_impression_short_tokens": existing_impression_short_tokens,
        "token_map": token_map,
        "anchor_source_refs": anchor_source_refs,
        "anchor_short_tokens": [
            token for token, ref in token_map.items() if ref in anchor_source_refs
        ],
        "expiry_conditions": list(EXPIRY_CONDITIONS),
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _InteriorCapabilityManifest(
        capability_ref=f"capability:private-impression:{capsule.capsule_id}",
        capability_kind="private_impression_reflection",
        payload_json=payload_json,
        payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        source_refs=tuple(
            dict.fromkeys(item.authority_event_ref for item in capsule.sources)
        ),
    )


class PrivateImpressionTriggerRuntime:
    """Drain one claimed-or-open ``private_impression_deliberation`` trigger."""

    def __init__(
        self,
        *,
        ledger,
        character_interior: CharacterInterior,
        companion_actor_ref: str,
        identity_frame: CompanionIdentityFrame | None = None,
        content_reader: Callable[[str], str | None] | None = None,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:private-impression-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("private impression runtime needs an owner and positive lease")
        if not isinstance(character_interior, CharacterInterior):
            raise TypeError("private impression runtime requires CharacterInterior")
        if not companion_actor_ref:
            raise ValueError("private impression Interior path needs the companion actor ref")
        self._ledger = ledger
        self._character_interior = character_interior
        self._companion_actor_ref = companion_actor_ref
        self._identity_frame = (
            identity_frame
            or CompanionIdentityFrame(
                companion_name="沈知栀",
                counterpart_name="对方",
            )
        )
        self._content_reader = content_reader
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> PrivateImpressionRunResult:
        projection = await _project(self._ledger)
        pending = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == "private_impression_deliberation"
            and item.state != "terminal"
        )
        if not pending:
            return PrivateImpressionRunResult(trigger_id="", status="idle")
        active = None
        source_event = None
        for process in pending:
            candidate_source = await self._source_event(process)
            candidate = await self._claim_or_reclaim(
                process=process,
                source_event=candidate_source,
                projection=projection,
            )
            if candidate is not None:
                active = candidate
                source_event = candidate_source
                break
        if active is None or source_event is None:
            return PrivateImpressionRunResult(
                trigger_id=pending[0].trigger_id, status="owned_elsewhere"
            )

        before = await _project(self._ledger)
        cursor = _cursor(before)
        appraisal = next(
            (
                item
                for item in before.appraisals
                if item.origin.accepted_event_ref == source_event.event_id
            ),
            None,
        )
        already_interpreted = appraisal is not None and any(
            impression.status == "active"
            and any(
                ref.startswith(f"appraisal:{appraisal.appraisal_id}:")
                for ref in impression.interpretation_refs
            )
            for impression in before.private_impressions
        )
        if appraisal is None or appraisal.status != "active" or already_interpreted:
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:no-source",
            )
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        capsule = compile_private_impression_reflection_capsule(
            projection=before,
            appraisal=appraisal,
            identity_frame=self._identity_frame,
            world_id=self._ledger.world_id,
            content_reader=self._content_reader,
        )
        attempt_id = active.claim_lease.attempt_id
        capability_manifest = _private_impression_capability(capsule)
        opportunity_identity = CausalOpportunityIdentity(
            world_id=self._ledger.world_id,
            actor_ref=self._companion_actor_ref,
            purpose="private_impression_reflection",
            source_refs=(source_event.event_id,),
            epoch=source_event.event_id,
        )
        transition = await self._character_interior.experience(
            InteriorStimulus(
                stimulus_ref=opportunity_identity.opportunity_ref,
                inner_turn_ref=attempt_id,
                world_id=self._ledger.world_id,
                actor_ref=self._companion_actor_ref,
                trigger_ref=source_event.event_id,
                cursor=cursor,
                logical_time=before.logical_time or source_event.logical_time,
                purpose="private_impression_reflection",
                source_refs=(source_event.event_id,),
                capability_manifest=capability_manifest,
                context_note=(
                    "One accepted appraisal is available for private, defeasible "
                    "reflection; durable interpretation remains optional."
                ),
            )
        )
        if transition.status == "technical_failure":
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        after_interior = await _project(self._ledger)
        if transition.status == "model_no_change":
            model_result_audit = recorded_character_interior_model_result(
                transition,
                purpose="private_impression_reflection",
                subject_ref=transition.stimulus_ref,
                trigger_ref=source_event.event_id,
                capability_ref=capability_manifest.capability_ref,
                route_tier="thinking",
                route_reason_code="character_interior_private_impression",
                router_version="character-interior-private-impression-transition.1",
            )
            await self._complete(
                process=active,
                source_event=source_event,
                cursor=_cursor(after_interior),
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
                model_result_audit=model_result_audit,
            )
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if len(transition.proposal_refs) != 1:
            # A transitioned result without exactly one accepted typed
            # proposal is an authority failure, never model no-change.
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id,
                status="processed",
                work_status="technical_failure",
            )
        accepted_ref = transition.proposal_refs[0]
        await self._complete(
            process=active,
            source_event=source_event,
            cursor=_cursor(after_interior),
            outcome_ref=f"outcome:{active.trigger_id}:accepted:{accepted_ref}",
        )
        return PrivateImpressionRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _accept(
        self,
        *,
        appraisal: AppraisalProjection,
        draft: PrivateImpressionDraft,
        capsule: PrivateImpressionReflectionCapsule,
        model_result_ref: str,
        source_event: WorldEvent,
        before,
        attempt_id: str,
        author_lineage: _InteriorAuthorLineage,
        character_interior_lineage: RecordedCharacterInteriorTurnLineage,
        reflection_contract: str = "character-interior-private-impression-transition.1",
    ) -> str:
        """Record the typed proposal, then drive the existing acceptance seam."""

        before = await self._record_character_interior_reflection_audit(
            draft=draft,
            capsule=capsule,
            source_event=source_event,
            before=before,
            attempt_id=attempt_id,
            author_lineage=author_lineage,
            character_interior_lineage=character_interior_lineage,
        )
        cursor = _cursor(before)
        logical_time = before.logical_time
        if logical_time is None:
            raise ValueError("private impression acceptance requires authoritative time")
        source_by_ref = {item.source_ref: item for item in capsule.sources}
        selected = tuple(source_by_ref[item] for item in draft.source_refs)
        predecessor_by_ref = {
            f"private-impression:{item.impression_id}": item
            for item in before.private_impressions
            if item.status == "active" and item.subject_ref == appraisal.subject_ref
        }
        predecessors = tuple(predecessor_by_ref[item] for item in draft.predecessor_refs)
        transition_kind = "open" if draft.decision == "retain" else draft.decision
        # This is the identity of one exact role-model decision, not merely of
        # its World facts.  A deliberation-only commit can strand the typed
        # proposal before Acceptance.  A later model pass at the same World
        # revision must therefore derive a fresh proposal instead of silently
        # reusing the old decision authority.
        identity = _digest(
            {
                "contract": reflection_contract,
                "world_id": self._ledger.world_id,
                "source_event_ref": source_event.event_id,
                "transition_kind": transition_kind,
                "predecessor_refs": list(draft.predecessor_refs),
                "reflection_source_refs": list(draft.source_refs),
                "evaluated_cursor": cursor.model_dump(mode="json"),
                "source_capsule_id": capsule.capsule_id,
                "source_model_result": model_result_ref,
                "reflection_digest": _reflection_draft_digest(draft),
            }
        )
        proposal_id = f"proposal:private-impression:{identity}"
        change_id = f"change:private-impression:{identity}"
        transition_id = f"transition:private-impression:{identity}"
        acceptance_id = f"acceptance:private-impression:{identity}"
        accepted_event_id = f"event:private-impression:accepted:{identity}"
        direct_appraisal_refs = tuple(
            AppraisalMeaningRef(
                appraisal_id=value["appraisal_id"],
                hypothesis_id=value["hypothesis_id"],
                source_cluster_ref=value["source_cluster_ref"],
                accepted_change_id=value["accepted_change_id"],
                accepted_transition_id=value["accepted_transition_id"],
            )
            for item in selected
            if item.source_kind == "appraisal"
            for value in (json.loads(item.value_json),)
        )
        appraisal_refs = direct_appraisal_refs
        selected_event_refs = tuple(dict.fromkeys(item.authority_event_ref for item in selected))
        committed_by_ref = {item.event_id: item for item in before.committed_world_event_refs}
        evidence_refs = tuple(
            EvidenceRef(
                ref_id=event_ref,
                evidence_type="committed_world_event",
                claim_purpose="private_hypothesis",
                source_world_revision=committed_by_ref[event_ref].world_revision,
                immutable_hash=committed_by_ref[event_ref].payload_hash,
            )
            for event_ref in selected_event_refs
        )
        impression = PrivateImpressionProjection(
            impression_id="impression:"
            + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "appraisal_id": appraisal.appraisal_id,
                    "transition_kind": transition_kind,
                    "predecessor_refs": list(draft.predecessor_refs),
                    "reflection_source_refs": list(draft.source_refs),
                }
            ),
            entity_revision=1,
            subject_ref=appraisal.subject_ref,
            interpretation_refs=tuple(
                f"appraisal:{item.appraisal_id}:{item.hypothesis_id}" for item in appraisal_refs
            ),
            source_refs=selected_event_refs,
            reflection_summary=draft.reflection_summary,
            confidence_bp=draft.confidence_bp,
            first_seen=(
                min(item.first_seen for item in predecessors)
                if draft.decision == "consolidate"
                else logical_time
            ),
            last_supported=logical_time,
            expiry_condition=draft.expiry_condition,
            status="active",
            origin=PrivateImpressionOrigin(
                change_id=change_id,
                transition_id=transition_id,
                policy_refs=PRIVATE_IMPRESSION_POLICY_REFS,
                accepted_event_ref=accepted_event_id,
            ),
        )
        payload: dict[str, object] = {
            "change_id": change_id,
            "transition_id": transition_id,
            "transition_kind": transition_kind,
            "expected_entity_revision": 0,
            "predecessor_refs": [
                PrivateImpressionPredecessorRef(
                    impression_id=item.impression_id,
                    expected_entity_revision=item.entity_revision,
                ).model_dump(mode="json")
                for item in predecessors
            ],
            "evidence_refs": [item.model_dump(mode="json") for item in evidence_refs],
            "appraisal_refs": [item.model_dump(mode="json") for item in appraisal_refs],
            "policy_refs": list(PRIVATE_IMPRESSION_POLICY_REFS),
            "reflection_contract": reflection_contract,
            "reflection_decision": draft.decision,
            "reflection_source_refs": list(draft.source_refs),
            "source_model_result": model_result_ref,
            "source_capsule_id": capsule.capsule_id,
            "acceptance_id": acceptance_id,
            "proposal_id": proposal_id,
            "evaluated_world_revision": cursor.world_revision,
            "accepted_change_hash": "0" * 64,
            "impression": impression.model_dump(mode="json"),
        }
        payload["accepted_change_hash"] = private_impression_mutation_hash(payload)
        proposal_event_id = "event:private-impression:proposed:" + identity
        if await _lookup(self._ledger, proposal_event_id) is None:
            proposal_payload = {
                "proposal_id": proposal_id,
                "proposal_kind": "private_impression_transition",
                "proposal_encoding": "typed-authority-v1",
                "authority_contract_ref": "proposal-contract:private-impression.1",
                "transition_kind": transition_kind,
                "change_id": change_id,
                "transition_id": transition_id,
                "evaluated_world_revision": payload["evaluated_world_revision"],
                "expected_entity_revision": 0,
                "proposed_change_hash": payload["accepted_change_hash"],
                "evidence_refs": payload["evidence_refs"],
                "appraisal_refs": payload["appraisal_refs"],
                "policy_refs": payload["policy_refs"],
                "reflection_contract": payload["reflection_contract"],
                "reflection_source_refs": payload["reflection_source_refs"],
                "source_model_result": payload["source_model_result"],
                "source_capsule_id": payload["source_capsule_id"],
                "proposed_mutation": {
                    "event_type": "PrivateImpressionAccepted",
                    "payload_json": json.dumps(
                        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                },
            }
            proposal_event = self._event(
                event_id=proposal_event_id,
                event_type="ProposalRecorded",
                logical_time=logical_time,
                source_event=source_event,
                payload=proposal_payload,
                fallback_identity="private-impression-proposal:" + identity,
            )
            await _commit_at_cursor(
                self._ledger,
                (proposal_event,),
                cursor=cursor,
                commit_id="commit:private-impression:proposed:" + identity,
            )
        if await _lookup(self._ledger, accepted_event_id) is None:
            after_proposal = await _project(self._ledger)
            acceptance_event = self._event(
                event_id="event:private-impression:acceptance:" + identity,
                event_type="AcceptanceRecorded",
                logical_time=logical_time,
                source_event=source_event,
                payload={
                    "status": "accepted",
                    "acceptance_id": acceptance_id,
                    "proposal_id": proposal_id,
                    "evaluated_world_revision": payload["evaluated_world_revision"],
                    "accepted_change_id": change_id,
                    "accepted_change_hash": payload["accepted_change_hash"],
                },
                fallback_identity="private-impression-acceptance:" + identity,
            )
            accepted_event = self._event(
                event_id=accepted_event_id,
                event_type="PrivateImpressionAccepted",
                logical_time=logical_time,
                source_event=source_event,
                payload=payload,
                fallback_identity="private-impression-accepted:" + identity,
            )
            await _commit_at_cursor(
                self._ledger,
                (acceptance_event, accepted_event),
                cursor=_cursor(after_proposal),
                commit_id="commit:private-impression:accepted:" + identity,
            )
        return accepted_event_id

    async def _record_character_interior_reflection_audit(
        self,
        *,
        draft: PrivateImpressionDraft,
        capsule: PrivateImpressionReflectionCapsule,
        source_event: WorldEvent,
        before,
        attempt_id: str,
        author_lineage: _InteriorAuthorLineage,
        character_interior_lineage: RecordedCharacterInteriorTurnLineage,
    ):
        """Persist the unified role lineage required by immutable V2 authority."""

        reflection_digest = _reflection_draft_digest(draft)
        decision = DecisionProposal(
            proposal_id=(
                "proposal:private-reflection-decision:"
                + _digest(
                    {
                        "capsule_id": capsule.capsule_id,
                        "reflection_digest": reflection_digest,
                    }
                )
            ),
            trigger_ref=source_event.event_id,
            evaluated_world_revision=capsule.world_revision,
            confidence=draft.confidence_bp,
            brief_rationale="private-reflection-draft:" + reflection_digest,
            behavior_tendency="reflect_privately",
            stance="tentative_internal_reading",
            display_strategy="withhold",
            timing_choice="silent",
        )
        model_result_ref = "model-result:" + _digest(
            {
                "model_call_id": author_lineage.model_call_id,
                "response_hash": author_lineage.response_hash.removeprefix("sha256:"),
            }
        )
        if await _lookup(self._ledger, f"event:private-impression:model-result:{model_result_ref}") is not None:
            return await _project(self._ledger)
        audit = RecordedModelResultAudit(
            model_call_id=author_lineage.model_call_id,
            parent_model_call_id=author_lineage.parent_model_call_id,
            model_result_ref=model_result_ref,
            attempt_id=attempt_id,
            route=RecordedModelRoute(
                tier="thinking",
                reason_code="character_interior_private_impression",
                router_version="character-interior-private-impression-transition.1",
            ),
            model_id=author_lineage.model_id,
            model_version=author_lineage.model_version,
            attempted_model_id=author_lineage.model_id,
            attempted_model_version=author_lineage.model_version,
            request_hash=author_lineage.request_hash.removeprefix("sha256:"),
            response_hash=author_lineage.response_hash.removeprefix("sha256:"),
            character_interior_lineage=character_interior_lineage,
            status="proposal_validated",
        )
        audit_json = model_audit_json(audit)
        deliberation_result_id = "deliberation:" + sha256(
            canonical_json(
                {
                    "capsule_id": capsule.capsule_id,
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
            model_call_id=author_lineage.model_call_id,
            parent_model_call_id=author_lineage.parent_model_call_id,
            attempt_id=attempt_id,
            capsule_id=capsule.capsule_id,
            trigger_ref=source_event.event_id,
            evaluated_world_revision=capsule.world_revision,
            # CharacterInterior exposes one final semantic result.  Its
            # same-author correction ordinal and parent are closed inside the
            # nested lineage; this outer transaction therefore contains one
            # persisted result, not an invented missing first attempt.
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
            model_call_id=author_lineage.model_call_id,
            attempt_id=attempt_id,
            capsule_id=capsule.capsule_id,
            trigger_ref=source_event.event_id,
            evaluated_world_revision=capsule.world_revision,
            proposal_json=proposal_json,
            proposal_hash=decision.proposal_hash,
        )
        events = (
            self._event(
                event_id=f"event:private-impression:model-result:{model_result_ref}",
                event_type="ModelResultRecorded",
                logical_time=source_event.logical_time,
                source_event=source_event,
                payload=model_payload.model_dump(mode="json"),
                fallback_identity=f"private-impression-model-result:{model_result_ref}",
            ),
            self._event(
                event_id=f"event:private-impression:model-proposal:{decision.proposal_id}",
                event_type="ProposalRecorded",
                logical_time=source_event.logical_time,
                source_event=source_event,
                payload=proposal_payload.model_dump(mode="json"),
                fallback_identity=f"private-impression-model-proposal:{decision.proposal_id}",
            ),
        )
        await _commit_at_cursor(
            self._ledger,
            events,
            cursor=_cursor(before),
            commit_id="commit:private-impression:character-interior-audit:"
            + _digest([model_result_ref, decision.proposal_id]),
        )
        return await _project(self._ledger)


    def _event(
        self,
        *,
        event_id: str,
        event_type: str,
        logical_time,
        source_event: WorldEvent,
        payload: dict,
        fallback_identity: str,
    ) -> WorldEvent:
        return WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=event_id,
            event_type=event_type,
            world_id=self._ledger.world_id,
            logical_time=logical_time,
            created_at=max(source_event.created_at, logical_time),
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type=event_type, world_id=self._ledger.world_id, payload=payload
            )
            or fallback_identity,
            payload=payload,
        )

    async def _source_event(self, process: TriggerProcess) -> WorldEvent:
        if process.source_evidence_ref is None:
            raise ValueError("private impression trigger has no appraisal source")
        stored = await _lookup(self._ledger, process.source_evidence_ref)
        if stored is None or stored[0].event_type != "AppraisalAccepted":
            raise ValueError("private impression appraisal authority is unavailable")
        if process.trigger_ref != f"impression:{process.source_evidence_ref}":
            raise ValueError("private impression trigger does not bind its appraisal")
        return stored[0]

    async def _claim_or_reclaim(
        self, *, process: TriggerProcess, source_event: WorldEvent, projection
    ) -> TriggerProcess | None:
        at = projection.logical_time or source_event.logical_time
        if process.state == "claimed" and process.claim_lease is not None:
            # A provider crossing happens only in the same drain that first
            # commits this claim.  Any later drain observes an already-claimed
            # process and must not resume that external call identity, even
            # when the owner string matches after a daemon restart.  Lease
            # expiry opens a new reclaim attempt instead.
            if at < process.claim_lease.expires_at:
                return None
        if len(process.attempt_ids) >= _PRIVATE_IMPRESSION_MAX_ATTEMPTS:
            # A reflection whose model output repeatedly fails authority
            # validation must not burn unbounded provider calls.  Terminal the
            # process after a bounded number of attempts; the next opener pass
            # derives a fresh trigger if the appraisal is still meaningful.
            # Completion requires a live claim lease, so first reclaim with a
            # fresh lease at the current logical time, then complete it.
            exhausted_attempt_id = "attempt:private-impression:exhausted:" + _digest(
                process.trigger_id
            )
            exhausted = process.model_copy(
                update={
                    "state": "claimed",
                    "claim_lease": ClaimLease(
                        owner_id=self._owner_id,
                        attempt_id=exhausted_attempt_id,
                        acquired_at=at,
                        expires_at=at + timedelta(seconds=self._lease_seconds),
                    ),
                    "attempt_ids": (*process.attempt_ids, exhausted_attempt_id),
                }
            )
            exhausted_event_type = (
                "TriggerProcessClaimed"
                if process.state == "open"
                else "TriggerProcessReclaimed"
            )
            exhausted_payload = {"process": exhausted.model_dump(mode="json")}
            exhausted_identity = domain_idempotency_key(
                event_type=exhausted_event_type,
                world_id=self._ledger.world_id,
                payload=exhausted_payload,
            )
            if exhausted_identity is None:
                raise ValueError("private impression exhausted claim has no domain identity")
            exhausted_event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=(
                    "event:private-impression:"
                    + exhausted_event_type.lower()
                    + ":"
                    + _digest([process.trigger_id, exhausted_attempt_id])
                ),
                world_id=self._ledger.world_id,
                event_type=exhausted_event_type,
                logical_time=at,
                created_at=source_event.created_at,
                actor=self._owner_id,
                source=self._source,
                trace_id=source_event.trace_id,
                causation_id=source_event.event_id,
                correlation_id=source_event.correlation_id,
                idempotency_key=exhausted_identity,
                payload=exhausted_payload,
            )
            await _commit(
                self._ledger,
                (exhausted_event,),
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                commit_id="commit:private-impression:exhausted-claim:"
                + _digest([process.trigger_id, exhausted_attempt_id]),
            )
            # The reclaim advanced the ledger; complete against the cursor
            # that includes it so the completion CAS is not stale.
            after_exhausted = await _project(self._ledger)
            await self._complete(
                process=exhausted,
                source_event=source_event,
                cursor=_cursor(after_exhausted),
                outcome_ref=f"outcome:{process.trigger_id}:attempts-exhausted",
            )
            return None
        attempt_id = "attempt:private-impression:" + _digest(
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
            event_type=event_type, world_id=self._ledger.world_id, payload=payload
        )
        if identity is None:
            raise ValueError("private impression claim has no domain identity")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=(
                "event:private-impression:"
                + event_type.lower()
                + ":"
                + _digest([process.trigger_id, attempt_id])
            ),
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
        await _commit(
            self._ledger,
            (event,),
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            commit_id="commit:private-impression:claim:"
            + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    async def _complete(
        self,
        *,
        process: TriggerProcess,
        source_event: WorldEvent,
        cursor: ProjectionCursor,
        outcome_ref: str,
        model_result_audit: ModelResultRecordedPayload | None = None,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("private impression completion requires a claimed process")
        projection = await _project_at(self._ledger, cursor)
        at = max(
            projection.logical_time or source_event.logical_time,
            process.claim_lease.acquired_at,
        )
        if at > process.claim_lease.expires_at:
            raise ValueError("private impression lease expired before completion")
        payload = {
            "trigger_id": process.trigger_id,
            "owner_id": process.claim_lease.owner_id,
            "attempt_id": process.claim_lease.attempt_id,
            "completed_at": at.isoformat(),
            "runtime_outcome_ref": outcome_ref,
            **(
                {
                    "character_interior_model_result": (
                        model_result_audit.model_dump(mode="json")
                    )
                }
                if model_result_audit is not None
                else {}
            ),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:private-impression:completed:"
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
            idempotency_key="world-v2:private-impression:completion:"
            + _digest([self._ledger.world_id, process.trigger_id, process.claim_lease.attempt_id]),
            payload=payload,
        )
        await _commit_at_cursor(
            self._ledger,
            (event,),
            cursor=cursor,
            commit_id="commit:private-impression:completed:"
            + _digest([process.trigger_id, process.claim_lease.attempt_id, outcome_ref]),
        )


class _PrivateImpressionInteriorAuthorityHandler:
    """Validate and accept one Interior-authored private reflection proposal."""

    proposal_type = "private_impression_transition"

    def __init__(self, runtime: PrivateImpressionTriggerRuntime) -> None:
        self._runtime = runtime

    async def prepare(
        self,
        request: _AuthorityRequest,
        proposal: dict[str, object],
    ) -> object:
        manifest = request.capability_manifest
        if (
            request.purpose != "private_impression_reflection"
            or manifest is None
            or manifest.capability_kind != "private_impression_reflection"
            or proposal.get("contract") != "character-interior-typed-proposal.1"
            or proposal.get("proposal_type") != self.proposal_type
            or proposal.get("purpose") != request.purpose
            or proposal.get("capability_ref") != manifest.capability_ref
            or proposal.get("capability_payload_hash") != manifest.payload_hash
            or proposal.get("source_refs") != list(manifest.source_refs)
        ):
            raise ValueError("private impression proposal authority binding is invalid")
        raw_capsule = manifest.payload.get("reflection_capsule")
        if not isinstance(raw_capsule, dict):
            raise ValueError("private impression capability lacks its reflection capsule")
        capsule = PrivateImpressionReflectionCapsule.model_validate_json(
            json.dumps(
                raw_capsule,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if (
            capsule.world_id != request.world_id
            or capsule.world_revision != request.cursor.world_revision
            or capsule.deliberation_revision != request.cursor.deliberation_revision
            or capsule.ledger_sequence != request.cursor.ledger_sequence
        ):
            raise ValueError("private impression capsule is not pinned to the InnerTurn")
        payload = proposal.get("payload")
        if not isinstance(payload, dict) or payload.get("contract") != (
            "character-interior-private-impression-transition.1"
        ):
            raise ValueError("private impression transition payload is invalid")
        expected = {
            "contract",
            "decision",
            "predecessor_refs",
            "source_refs",
            "reflection_summary",
            "confidence_bp",
            "expiry_condition",
        }
        if set(payload) != expected:
            raise ValueError("private impression transition has unsupported fields")
        raw_draft = {
            "decision": payload["decision"],
            "predecessor_refs": payload["predecessor_refs"],
            "source_refs": payload["source_refs"],
            "reflection_summary": payload["reflection_summary"],
            "confidence": payload["confidence_bp"],
            "expiry_condition": payload["expiry_condition"],
        }
        if raw_draft["decision"] == "retain":
            raw_draft.pop("predecessor_refs")
        draft = _materialize_draft(
            json.dumps(raw_draft, ensure_ascii=False, separators=(",", ":")),
            capsule=capsule,
        )
        if draft is None:
            raise ValueError("private impression transition cannot be no-change")
        source = await _lookup(self._runtime._ledger, request.trigger_ref)  # noqa: SLF001
        if source is None or source[0].event_type != "AppraisalAccepted":
            raise ValueError("private impression trigger authority is unavailable")
        before = await _project_at(self._runtime._ledger, request.cursor)  # noqa: SLF001
        appraisal = next(
            (
                item
                for item in before.appraisals
                if item.appraisal_id == capsule.anchor_appraisal_id
                and item.origin.accepted_event_ref == source[0].event_id
            ),
            None,
        )
        if appraisal is None or appraisal.status != "active":
            raise ValueError("private impression anchor appraisal is no longer active")
        lineage = request.author_lineage
        if lineage is None:
            raise ValueError("private impression transition lacks character author lineage")
        model_result_ref = "model-result:" + _digest(
            {
                "model_call_id": lineage.model_call_id,
                "response_hash": lineage.response_hash.removeprefix("sha256:"),
            }
        )
        return _PreparedPrivateImpressionAuthority(
            appraisal=appraisal,
            draft=draft,
            capsule=capsule,
            model_result_ref=model_result_ref,
            source_event=source[0],
            before=before,
            attempt_id=request.inner_turn_id,
            author_lineage=lineage,
            character_interior_lineage=RecordedCharacterInteriorTurnLineage(
                inner_turn_id=request.inner_turn_id,
                purpose=request.purpose,
                opportunity_ref=request.subject_ref,
                snapshot_id=request.snapshot_id,
                snapshot_hash=request.snapshot_hash,
                capability_ref=manifest.capability_ref,
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
            reflection_contract="character-interior-private-impression-transition.1",
        )

    async def submit(
        self,
        request: _AuthorityRequest,
        prepared: tuple[object, ...],
    ) -> tuple[str, ...]:
        del request
        if len(prepared) != 1 or not isinstance(
            prepared[0], _PreparedPrivateImpressionAuthority
        ):
            raise ValueError("private impression authority needs one prepared transition")
        item = prepared[0]
        accepted = await self._runtime._accept(  # noqa: SLF001 - exact typed authority
            appraisal=item.appraisal,
            draft=item.draft,
            capsule=item.capsule,
            model_result_ref=item.model_result_ref,
            source_event=item.source_event,
            before=item.before,
            attempt_id=item.attempt_id,
            author_lineage=item.author_lineage,
            character_interior_lineage=item.character_interior_lineage,
            reflection_contract=item.reflection_contract,
        )
        return (accepted,)


@dataclass(frozen=True, slots=True)
class _PreparedPrivateImpressionAuthority:
    appraisal: AppraisalProjection
    draft: PrivateImpressionDraft
    capsule: PrivateImpressionReflectionCapsule
    model_result_ref: str
    source_event: WorldEvent
    before: object
    attempt_id: str
    author_lineage: _InteriorAuthorLineage
    character_interior_lineage: RecordedCharacterInteriorTurnLineage
    reflection_contract: str


async def _project(ledger):
    if getattr(ledger, "blocks_event_loop", False):
        return await asyncio.to_thread(ledger.project)
    return ledger.project()


async def _project_at(ledger, cursor: ProjectionCursor):
    if getattr(ledger, "blocks_event_loop", False):
        return await asyncio.to_thread(ledger.project_at, cursor)
    return ledger.project_at(cursor)


async def _lookup(ledger, event_id: str):
    if getattr(ledger, "blocks_event_loop", False):
        return await asyncio.to_thread(ledger.lookup_event_commit, event_id)
    return ledger.lookup_event_commit(event_id)


async def _commit(ledger, events, *, world_revision, deliberation_revision, commit_id):
    if getattr(ledger, "blocks_event_loop", False):
        return await asyncio.to_thread(
            ledger.commit,
            events,
            expected_world_revision=world_revision,
            expected_deliberation_revision=deliberation_revision,
            commit_id=commit_id,
        )
    return ledger.commit(
        events,
        expected_world_revision=world_revision,
        expected_deliberation_revision=deliberation_revision,
        commit_id=commit_id,
    )


async def _commit_at_cursor(ledger, events, *, cursor, commit_id):
    if getattr(ledger, "blocks_event_loop", False):
        return await asyncio.to_thread(
            ledger.commit_at_cursor, events, expected_cursor=cursor, commit_id=commit_id
        )
    return ledger.commit_at_cursor(events, expected_cursor=cursor, commit_id=commit_id)


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


__all__ = [
    "EXPIRY_CONDITIONS",
    "PrivateImpressionDraft",
    "PrivateImpressionReflectionCapsule",
    "PrivateImpressionReflectionSource",
    "PrivateImpressionRunResult",
    "PrivateImpressionTriggerOpener",
    "PrivateImpressionTriggerRuntime",
    "compile_private_impression_reflection_capsule",
    "private_impression_opportunity",
]
