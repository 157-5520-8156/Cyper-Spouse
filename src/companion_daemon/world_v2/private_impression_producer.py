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
from datetime import UTC, datetime, timedelta
import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import Field
from pydantic_core import to_jsonable_python

from .batch_invariants import private_impression_trigger_identity
from .companion_identity import CompanionIdentityFrame
from .character_interior import CharacterInterior, InteriorStimulus
from .character_interior.audit import (
    causal_opportunity_lineage_fields,
    recorded_character_interior_model_result,
    technical_character_interior_model_result,
)
from .character_interior.contracts import (
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
)
from .character_interior.ports import _AuthorityRequest
from .character_interior.run_result import (
    CAUSAL_OPPORTUNITY_CONTRACT_VERSION,
    CausalOpportunityHealth,
    CausalOpportunityIdentity,
    CausalOpportunityPolicy,
    CausalOpportunityRuntime,
    CausalOpportunitySource,
    DEFAULT_CAUSAL_OPPORTUNITY_POLICY,
    causal_opportunity_policy_from_attempt_id,
)
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
PRIVATE_IMPRESSION_PURPOSE = "private_impression_reflection"
_NON_ATTEMPT_TECHNICAL_FAILURES = frozenset({"required_tool_choice_unsupported"})

# Bound the number of times one private-reflection trigger may be reclaimed
# after its model output failed authority validation.  Lease expiry alone
# would otherwise retry the same failing provider call forever, burning
# unbounded tokens on a reflection that never succeeds; after this many
# attempts the process is terminal. A fresh epoch requires new accepted
# appraisal evidence; the opener does not re-derive the same source trigger.
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
    work_status: Literal[
        "no_change", "ignored", "expired", "accepted", "technical_failure"
    ] | None = None
    opportunity_ref: str | None = None
    source_refs: tuple[str, ...] = ()
    epoch: str | None = None
    contract_version: str | None = None


@dataclass(frozen=True, slots=True)
class _PrivateImpressionLocatedSource:
    process: TriggerProcess
    source_event: WorldEvent
    policy: CausalOpportunityPolicy

    @property
    def route_source(self) -> CausalOpportunitySource:
        return CausalOpportunitySource(
            source_ref=self.source_event.event_id,
            process_ref=self.process.trigger_id,
            process_kind=self.process.process_kind,
            causal_key=self.source_event.correlation_id,
            logical_time=self.source_event.logical_time,
            policy=self.policy,
        )


@dataclass(frozen=True, slots=True)
class _PrivateImpressionOpportunityBatch:
    processes: tuple[TriggerProcess, ...]
    source_events: tuple[WorldEvent, ...]
    identity: CausalOpportunityIdentity


def _private_impression_opportunity_identity(
    *,
    world_id: str,
    actor_ref: str,
    source_ref: str | None = None,
    source_refs: tuple[str, ...] = (),
    epoch: str | None = None,
    policy: CausalOpportunityPolicy | None = None,
) -> CausalOpportunityIdentity:
    refs = tuple(source_refs)
    if source_ref:
        refs += (source_ref,)
    refs = tuple(dict.fromkeys(refs))
    canonical_refs = tuple(sorted(refs))
    if not canonical_refs:
        raise ValueError("private impression opportunity requires source refs")
    selected_policy = policy or DEFAULT_CAUSAL_OPPORTUNITY_POLICY
    runtime = CausalOpportunityRuntime(
        world_id=world_id,
        actor_ref=actor_ref,
        purpose=PRIVATE_IMPRESSION_PURPOSE,
    )
    return runtime.identity_for_refs(
        canonical_refs,
        epoch=epoch or canonical_refs[0],
        policy=selected_policy,
    )


def _private_impression_capability(
    capsule: PrivateImpressionReflectionCapsule,
    *,
    opportunity_identity: CausalOpportunityIdentity | None = None,
) -> _InteriorCapabilityManifest:
    if opportunity_identity is None:
        anchor_source_ref = next(
            (
                item.source_ref
                for item in capsule.sources
                if item.source_kind == "appraisal"
                and json.loads(item.value_json).get("appraisal_id")
                == capsule.anchor_appraisal_id
            ),
            None,
        )
        if anchor_source_ref is None:
            raise ValueError("private impression capability lacks an anchor source")
        opportunity_identity = _private_impression_opportunity_identity(
            world_id=capsule.world_id,
            actor_ref="actor:companion",
            source_ref=anchor_source_ref,
        )
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
        "causal_opportunity": opportunity_identity.model_dump(mode="json"),
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
        merge_window_seconds: int = DEFAULT_CAUSAL_OPPORTUNITY_POLICY.merge_window_seconds,
        expiry_seconds: int = DEFAULT_CAUSAL_OPPORTUNITY_POLICY.expiry_seconds,
        source: str = "world-v2:private-impression-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0 or merge_window_seconds < 0 or expiry_seconds <= 0:
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
        self._policy = CausalOpportunityPolicy(
            merge_window_seconds=merge_window_seconds,
            expiry_seconds=expiry_seconds,
        )
        self._opportunity_runtime = CausalOpportunityRuntime(
            world_id=ledger.world_id,
            actor_ref=companion_actor_ref,
            purpose=PRIVATE_IMPRESSION_PURPOSE,
            contract_version=CAUSAL_OPPORTUNITY_CONTRACT_VERSION,
        )
        self._source = source

    def _route_groups(
        self,
        located_sources: tuple[_PrivateImpressionLocatedSource, ...],
    ) -> tuple[tuple[_PrivateImpressionLocatedSource, ...], ...]:
        by_process_ref = {
            item.process.trigger_id: item
            for item in located_sources
        }
        return tuple(
            tuple(by_process_ref[item.process_ref] for item in group)
            for group in self._opportunity_runtime.group_sources(
                tuple(item.route_source for item in located_sources)
            )
        )

    async def advance_due_once(self) -> PrivateImpressionRunResult:
        """Route one due private-impression opportunity through this seam."""

        result = await self._drain_one_impl()
        return await self._attach_opportunity_lineage(result)

    async def drain_one(self) -> PrivateImpressionRunResult:
        """Compatibility entry point for replay/tests; production uses ``advance_due_once``."""

        return await self.advance_due_once()

    async def _drain_one_impl(self) -> PrivateImpressionRunResult:
        projection = await _project(self._ledger)
        pending = tuple(
            sorted(
                (
                    item
                    for item in projection.trigger_processes
                    if item.process_kind == "private_impression_deliberation"
                    and item.state != "terminal"
                ),
                key=self._health_process_sort_key,
            )
        )
        if not pending:
            return PrivateImpressionRunResult(trigger_id="", status="idle")
        process = pending[0]
        batch = await self._opportunity_batch(process=process, projection=projection)
        opportunity_identity = batch.identity
        source_events_by_trigger = {
            candidate.trigger_id: source_event
            for candidate, source_event in zip(
                batch.processes,
                batch.source_events,
                strict=True,
            )
        }
        source_event = source_events_by_trigger[process.trigger_id]

        def result(
            *,
            work_status: str | None = None,
            status: str = "processed",
        ) -> PrivateImpressionRunResult:
            return PrivateImpressionRunResult(
                trigger_id=process.trigger_id,
                status=status,
                work_status=work_status,  # type: ignore[arg-type]
                opportunity_ref=opportunity_identity.opportunity_ref,
                source_refs=opportunity_identity.source_refs,
                epoch=opportunity_identity.epoch,
                contract_version=opportunity_identity.contract_version,
            )

        active_processes: list[TriggerProcess] = []
        for candidate in batch.processes:
            current_projection = await _project(self._ledger)
            current_process = next(
                item
                for item in current_projection.trigger_processes
                if item.trigger_id == candidate.trigger_id
            )
            active_candidate = await self._claim_or_reclaim(
                process=current_process,
                source_event=source_events_by_trigger[candidate.trigger_id],
                projection=current_projection,
            )
            if active_candidate is None:
                return result(status="owned_elsewhere")
            active_processes.append(active_candidate)
        active = next(
            item for item in active_processes if item.trigger_id == process.trigger_id
        )
        before = await _project(self._ledger)
        cursor = _cursor(before)
        policy = opportunity_identity.opportunity_policy
        durable_identity = self._durable_opportunity_identity(
            before,
            source_ref=source_event.event_id,
        )
        if durable_identity is not None:
            accepted = any(
                impression.status == "active"
                and set(impression.source_refs).intersection(durable_identity.source_refs)
                for impression in before.private_impressions
            )
            await self._complete_opportunity_processes(
                processes=active_processes,
                source_events=source_events_by_trigger,
                outcome_ref=(
                    f"outcome:{process.trigger_id}:replay:"
                    f"{('accepted' if accepted else 'no-change')}"
                ),
            )
            return result(work_status="accepted" if accepted else "no_change")
        if self._opportunity_runtime.is_expired(
            tuple(
                CausalOpportunitySource(
                    source_ref=source_event_item.event_id,
                    process_ref=process_item.trigger_id,
                    process_kind=process_item.process_kind,
                    causal_key=source_event_item.correlation_id,
                    logical_time=source_event_item.logical_time,
                    policy=policy,
                )
                for process_item, source_event_item in zip(
                    batch.processes,
                    batch.source_events,
                    strict=True,
                )
            ),
            at=before.logical_time or source_event.logical_time,
        ):
            await self._complete_opportunity_processes(
                processes=active_processes,
                source_events=source_events_by_trigger,
                outcome_ref=(
                    f"outcome:{process.trigger_id}:expired:{opportunity_identity.opportunity_ref}"
                ),
            )
            return result(work_status="expired")
        appraisals = tuple(
            next(
                (
                    item
                    for item in before.appraisals
                    if item.origin.accepted_event_ref == source_event_item.event_id
                ),
                None,
            )
            for source_event_item in batch.source_events
        )
        already_interpreted = any(
            appraisal is not None
            and any(
                impression.status == "active"
                and any(
                    ref.startswith(f"appraisal:{appraisal.appraisal_id}:")
                    for ref in impression.interpretation_refs
                )
                for impression in before.private_impressions
            )
            for appraisal in appraisals
        )
        if (
            any(appraisal is None or appraisal.status != "active" for appraisal in appraisals)
            or already_interpreted
        ):
            await self._complete_opportunity_processes(
                processes=active_processes,
                source_events=source_events_by_trigger,
                outcome_ref=f"outcome:{process.trigger_id}:ignored",
            )
            return result(work_status="ignored")
        appraisal = appraisals[0]
        assert appraisal is not None
        capsule = compile_private_impression_reflection_capsule(
            projection=before,
            appraisal=appraisal,
            identity_frame=self._identity_frame,
            world_id=self._ledger.world_id,
            content_reader=self._content_reader,
        )
        attempt_id = active.claim_lease.attempt_id
        capability_manifest = _private_impression_capability(
            capsule,
            opportunity_identity=opportunity_identity,
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
                purpose=PRIVATE_IMPRESSION_PURPOSE,
                source_refs=opportunity_identity.source_refs,
                capability_manifest=capability_manifest,
                context_note=(
                    "One or more accepted appraisals are available for private, defeasible "
                    "reflection; durable interpretation remains optional."
                ),
            )
        )
        if transition.status == "technical_failure":
            failure_code = transition.failure_code or "interior_technical_failure"
            # No provider/model result exists when the required tool is not
            # supported. Keep the typed technical outcome and retry lease, but
            # do not manufacture a ModelResultRecorded audit that pollutes an
            # unrelated replay sequence. CharacterInterior topology health is
            # the authority for this preflight qualification failure.
            if failure_code not in _NON_ATTEMPT_TECHNICAL_FAILURES:
                await self._record_technical_failure(
                    process=active,
                    source_event=source_event,
                    failure_code=failure_code,
                )
            return result(work_status="technical_failure")
        if transition.status == "model_no_change":
            model_result_audit = recorded_character_interior_model_result(
                transition,
                purpose=PRIVATE_IMPRESSION_PURPOSE,
                subject_ref=transition.stimulus_ref,
                trigger_ref=source_event.event_id,
                capability_ref=capability_manifest.capability_ref,
                route_tier="thinking",
                route_reason_code="character_interior_private_impression",
                router_version="character-interior-private-impression-transition.1",
                causal_opportunity=opportunity_identity,
            )
            await self._complete_opportunity_processes(
                processes=active_processes,
                source_events=source_events_by_trigger,
                outcome_ref=f"outcome:{process.trigger_id}:no-change",
                model_result_audit=model_result_audit,
            )
            return result(work_status="no_change")
        if len(transition.proposal_refs) != 1:
            # A transitioned result without exactly one accepted typed
            # proposal is an authority failure, never model no-change.
            await self._record_technical_failure(
                process=active,
                source_event=source_event,
                failure_code="invalid_proposal_count",
            )
            return result(work_status="technical_failure")
        accepted_ref = transition.proposal_refs[0]
        await self._complete_opportunity_processes(
            processes=active_processes,
            source_events=source_events_by_trigger,
            outcome_ref=f"outcome:{process.trigger_id}:accepted:{accepted_ref}",
        )
        return result(work_status="accepted")

    async def _opportunity_batch(
        self,
        *,
        process: TriggerProcess,
        projection,
    ) -> _PrivateImpressionOpportunityBatch:
        policy = self._policy_for_process(process)
        candidates = tuple(
            item
            for item in projection.trigger_processes
            if item.process_kind == process.process_kind
            and item.state != "terminal"
            and item.source_evidence_ref is not None
            and self._policy_for_process(item).policy_ref == policy.policy_ref
        )
        located_items: list[_PrivateImpressionLocatedSource] = []
        for candidate in candidates:
            located_items.append(
                _PrivateImpressionLocatedSource(
                    process=candidate,
                    source_event=await self._source_event(candidate),
                    policy=policy,
                )
            )
        located = tuple(located_items)
        groups = self._route_groups(located)
        selected = next(
            (
                group
                for group in groups
                if any(item.process.trigger_id == process.trigger_id for item in group)
            ),
            None,
        )
        if selected is None:
            raise ValueError("private impression process is absent from its source groups")
        source_refs = tuple(item.source_event.event_id for item in selected)
        identity = _private_impression_opportunity_identity(
            world_id=self._ledger.world_id,
            actor_ref=self._companion_actor_ref,
            source_refs=source_refs,
            epoch=source_refs[0],
            policy=policy,
        )
        return _PrivateImpressionOpportunityBatch(
            processes=tuple(item.process for item in selected),
            source_events=tuple(item.source_event for item in selected),
            identity=identity,
        )

    async def _complete_opportunity_processes(
        self,
        *,
        processes: list[TriggerProcess],
        source_events: dict[str, WorldEvent],
        outcome_ref: str,
        model_result_audit: ModelResultRecordedPayload | None = None,
    ) -> None:
        """Terminalize every claimed appraisal source exactly once."""

        for process in processes:
            current = await _project(self._ledger)
            current_process = next(
                item for item in current.trigger_processes if item.trigger_id == process.trigger_id
            )
            if current_process.state == "terminal":
                continue
            await self._complete(
                process=current_process,
                source_event=source_events[process.trigger_id],
                cursor=_cursor(current),
                outcome_ref=outcome_ref,
                model_result_audit=model_result_audit if process is processes[0] else None,
            )

    def _durable_opportunity_identity(
        self,
        projection,
        *,
        source_ref: str | None,
    ) -> CausalOpportunityIdentity | None:
        if source_ref is None:
            return None
        identities: dict[str, CausalOpportunityIdentity] = {}
        for model_audit in reversed(projection.model_result_audits):
            try:
                recorded = RecordedModelResultAudit.model_validate_json(model_audit.audit_json)
            except ValueError:
                continue
            lineage = recorded.character_interior_lineage
            if (
                lineage is None
                or source_ref not in lineage.causal_source_refs
                or lineage.causal_actor_ref != self._companion_actor_ref
                or lineage.purpose != PRIVATE_IMPRESSION_PURPOSE
                or lineage.causal_policy_ref is None
            ):
                continue
            try:
                policy = (
                    CausalOpportunityPolicy.from_ref(lineage.causal_policy_ref)
                    if lineage.causal_policy_ref is not None
                    else DEFAULT_CAUSAL_OPPORTUNITY_POLICY
                )
                identity = CausalOpportunityRuntime(
                    world_id=lineage.causal_world_id,
                    actor_ref=lineage.causal_actor_ref,
                    purpose=lineage.purpose,
                    contract_version=lineage.causal_contract_version,
                ).identity_for_refs(
                    lineage.causal_source_refs,
                    epoch=lineage.causal_epoch,
                    policy=policy,
                )
            except (TypeError, ValueError):
                continue
            if (
                (
                    lineage.causal_policy_version is not None
                    and identity.policy_version != lineage.causal_policy_version
                )
                or identity.opportunity_ref != lineage.opportunity_ref
            ):
                continue
            identities[identity.opportunity_ref] = identity
        if len(identities) != 1:
            return None
        return next(iter(identities.values()))

    def _health_opportunity_identities(
        self,
        projection,
        processes: tuple[TriggerProcess, ...],
    ) -> dict[str, CausalOpportunityIdentity]:
        identities: dict[str, CausalOpportunityIdentity] = {}
        unresolved: list[_PrivateImpressionLocatedSource] = []
        for process in processes:
            source_ref = process.source_evidence_ref
            durable = self._durable_opportunity_identity(projection, source_ref=source_ref)
            if durable is not None:
                identities[process.trigger_id] = durable
                continue
            if source_ref is None:
                continue
            located = self._ledger.lookup_event_commit(source_ref)
            if located is None:
                identities[process.trigger_id] = _private_impression_opportunity_identity(
                    world_id=self._ledger.world_id,
                    actor_ref=self._companion_actor_ref,
                    source_ref=source_ref,
                    policy=self._policy_for_process(process),
                )
                continue
            unresolved.append(
                _PrivateImpressionLocatedSource(
                    process=process,
                    source_event=located[0],
                    policy=self._policy_for_process(process),
                )
            )
        by_policy: dict[str, list[_PrivateImpressionLocatedSource]] = {}
        for item in unresolved:
            by_policy.setdefault(item.policy.policy_ref, []).append(item)
        for policy_ref in sorted(by_policy):
            policy_sources = tuple(by_policy[policy_ref])
            for group in self._route_groups(tuple(policy_sources)):
                source_refs = tuple(item.source_event.event_id for item in group)
                identity = _private_impression_opportunity_identity(
                    world_id=self._ledger.world_id,
                    actor_ref=self._companion_actor_ref,
                    source_refs=source_refs,
                    epoch=source_refs[0],
                    policy=group[0].policy,
                )
                for item in group:
                    identities[item.process.trigger_id] = identity
        return identities

    async def _attach_opportunity_lineage(
        self,
        result: PrivateImpressionRunResult,
    ) -> PrivateImpressionRunResult:
        if not result.trigger_id or result.opportunity_ref is not None:
            return result
        projection = await _project(self._ledger)
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.trigger_id == result.trigger_id
            ),
            None,
        )
        if process is None or process.source_evidence_ref is None:
            raise RuntimeError("private impression result has no source-bound opportunity")
        identity = self._health_opportunity_identities(projection, (process,)).get(
            process.trigger_id
        )
        if identity is None:
            raise RuntimeError("private impression result has no recoverable opportunity")
        return result.model_copy(
            update={
                "opportunity_ref": identity.opportunity_ref,
                "source_refs": identity.source_refs,
                "epoch": identity.epoch,
                "contract_version": identity.contract_version,
            }
        )

    def health_snapshot(self, world_id: str) -> CausalOpportunityHealth:
        if world_id != self._ledger.world_id:
            raise ValueError("private impression health world does not match the ledger")
        projection = self._ledger.project()
        processes = tuple(
            sorted(
                (
                    item
                    for item in projection.trigger_processes
                    if item.process_kind == "private_impression_deliberation"
                    and item.source_evidence_ref is not None
                ),
                key=self._health_process_sort_key,
            )
        )
        identity_by_trigger = self._health_opportunity_identities(projection, processes)
        identities = tuple(identity_by_trigger[item.trigger_id] for item in processes)
        outcomes = tuple(item.runtime_outcome_ref or "" for item in processes)
        last = processes[-1] if processes else None
        last_identity = identities[-1] if identities else None
        return CausalOpportunityHealth(
            world_id=world_id,
            actor_ref=self._companion_actor_ref,
            purpose=PRIVATE_IMPRESSION_PURPOSE,
            open_count=sum(item.state == "open" for item in processes),
            claimed_count=sum(item.state == "claimed" for item in processes),
            terminal_count=sum(item.state == "terminal" for item in processes),
            deferred_count=0,
            opportunity_count=len({item.opportunity_ref for item in identities}),
            last_source_ref=last.source_evidence_ref if last is not None else None,
            last_opportunity_ref=last_identity.opportunity_ref if last_identity else None,
            no_change_count=sum(item.endswith(":no-change") for item in outcomes),
            ignored_count=sum(":ignored" in item or ":no-source" in item for item in outcomes),
            expired_count=sum(":expired:" in item for item in outcomes),
            accepted_count=sum(
                process.state == "terminal"
                and (outcome.endswith(":accepted") or ":accepted:" in outcome)
                for process, outcome in zip(processes, outcomes, strict=True)
            ),
            technical_failure_count=sum(
                self._process_has_technical_failure(projection, item)
                for item in processes
            ),
        )

    def _health_process_sort_key(
        self,
        process,
    ) -> tuple[datetime, str, str]:
        source_event = self._ledger.lookup_event_commit(process.source_evidence_ref)
        event = source_event[0] if source_event is not None else None
        return (
            event.logical_time if event is not None else datetime.min.replace(tzinfo=UTC),
            event.event_id if event is not None else "",
            process.trigger_id,
        )

    def _policy_for_process(self, process) -> CausalOpportunityPolicy:
        lease = process.claim_lease
        if lease is None:
            return self._policy
        try:
            persisted = causal_opportunity_policy_from_attempt_id(lease.attempt_id)
        except ValueError as exc:
            raise ValueError("private impression claim has an invalid durable policy") from exc
        return persisted or self._policy

    @staticmethod
    def _process_has_technical_failure(projection, process) -> bool:  # type: ignore[no-untyped-def]
        attempt_ids = set(process.attempt_ids)
        for item in projection.model_result_audits:
            if item.trigger_ref != process.source_evidence_ref or item.attempt_id not in attempt_ids:
                continue
            try:
                audit = RecordedModelResultAudit.model_validate_json(item.audit_json)
            except ValueError:
                continue
            if audit.failure_code is not None:
                return True
        return False

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
            # process after a bounded number of attempts. A new epoch can only
            # come from a later accepted appraisal source, not from retrying
            # this same source or advancing the clock.
            # Completion requires a live claim lease, so first reclaim with a
            # fresh lease at the current logical time, then complete it.
            exhausted_attempt_id = (
                "attempt:private-impression:exhausted:"
                + _digest(process.trigger_id)
                + ":policy="
                + self._policy_for_process(process).policy_ref
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
        attempt_id = (
            "attempt:private-impression:"
            + _digest({"trigger_id": process.trigger_id, "attempt": len(process.attempt_ids) + 1})
            + ":policy="
            + self._policy_for_process(process).policy_ref
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

    async def _record_technical_failure(
        self,
        *,
        process,
        source_event: WorldEvent,
        failure_code: str,
    ) -> None:
        if process.claim_lease is None:
            raise ValueError("private impression technical audit requires a claim")
        current = await _project(self._ledger)
        model_payload = technical_character_interior_model_result(
            purpose=PRIVATE_IMPRESSION_PURPOSE,
            trigger_ref=source_event.event_id,
            attempt_id=process.claim_lease.attempt_id,
            evaluated_world_revision=current.world_revision,
            failure_code=failure_code,
        )
        if any(
            item.model_result_ref == model_payload.model_result_ref
            for item in current.model_result_audits
        ):
            return
        at = current.logical_time or source_event.logical_time
        payload = model_payload.model_dump(mode="json")
        identity = domain_idempotency_key(
            event_type="ModelResultRecorded",
            world_id=self._ledger.world_id,
            payload=payload,
        )
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:private-impression:technical:" + model_payload.model_result_ref,
            world_id=self._ledger.world_id,
            event_type="ModelResultRecorded",
            logical_time=at,
            created_at=max(source_event.created_at, at),
            actor=self._owner_id,
            source=self._source,
            trace_id=source_event.trace_id,
            causation_id=source_event.event_id,
            correlation_id=source_event.correlation_id,
            idempotency_key=identity or "world-v2:private-impression:technical:" + model_payload.model_result_ref,
            payload=payload,
        )
        await _commit_at_cursor(
            self._ledger,
            (event,),
            cursor=_cursor(current),
            commit_id="commit:private-impression:technical:" + model_payload.model_result_ref,
        )

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
        raw_identity = manifest.payload.get("causal_opportunity")
        try:
            if isinstance(raw_identity, dict) and isinstance(raw_identity.get("source_refs"), list):
                raw_identity = {
                    **raw_identity,
                    "source_refs": tuple(raw_identity["source_refs"]),
                }
            opportunity_identity = CausalOpportunityIdentity.model_validate(raw_identity)
        except ValueError as exc:
            raise ValueError("private impression capability lacks a valid causal opportunity") from exc
        if (
            opportunity_identity.world_id != request.world_id
            or opportunity_identity.actor_ref != request.actor_ref
            or opportunity_identity.purpose != request.purpose
            or opportunity_identity.opportunity_ref != request.subject_ref
            or opportunity_identity.source_refs != request.subject_source_refs
            or request.trigger_ref not in opportunity_identity.source_refs
            or any(source_ref not in manifest.source_refs for source_ref in opportunity_identity.source_refs)
        ):
            raise ValueError("private impression InnerTurn is not actor-bound to its opportunity")
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
        for source_ref in opportunity_identity.source_refs:
            located = await _lookup(self._runtime._ledger, source_ref)  # noqa: SLF001
            if located is None or located[0].event_type != "AppraisalAccepted":
                raise ValueError("private impression merged appraisal authority is unavailable")
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
                **causal_opportunity_lineage_fields(
                    opportunity_identity,
                    subject_ref=request.subject_ref,
                ),
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
