"""Background producer for Private Impressions (CONTEXT.md).

A Private Impression is "the companion's fallible, source-bound
interpretation of a user, relationship, or event".  The typed authority
(``private_impression_transition`` proposal family, ``PrivateImpressionAccepted``
event, pure reducer, capsule ``private_impressions`` slice) has existed since
the impression reducers landed; this module adds the missing *producer*
vertical, following the interaction-fact worker's discipline:

* a deterministic opener leaves at most one recoverable trigger per accepted
  appraisal (the anchor is the committed ``AppraisalAccepted`` event);
* a bounded model may only *consolidate* already-accepted appraisal
  hypotheses into one private reading — it selects hypothesis ids, a
  confidence, and an expiry condition from a closed set, and can always
  decline.  It never writes prose, evidence, subjects, or identities;
* the runtime materializes the typed proposal, records it as an immutable
  audit, and drives the existing acceptance authority.  The accepted
  impression then reaches later turns only through the capsule's private
  slice (privacy class ``withhold``); it is never shown to the user.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from typing import Literal, Protocol

from .batch_invariants import private_impression_trigger_identity
from .event_identity import domain_idempotency_key
from .ledger import LedgerPort
from .model_json import extract_json_object_text
from .private_impression_events import (
    PRIVATE_IMPRESSION_POLICY_REFS,
    private_impression_mutation_hash,
)
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


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PrivateImpressionChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class PrivateImpressionDraft(FrozenModel):
    """The model's entire authority: a selection over accepted hypotheses."""

    hypothesis_ids: tuple[str, ...]
    confidence_bp: int
    expiry_condition: Literal[
        "until_appraisal_contradicted",
        "until_counter_evidence",
        "until_relationship_stage_changes",
        "one_month_without_support",
    ]


class PrivateImpressionDraftAdapter:
    """Bounded consolidation of one accepted appraisal into a private reading."""

    VERSION = "private-impression-draft.1"

    def __init__(self, *, model: PrivateImpressionChatModel, temperature: float = 0.1) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("private impression temperature must be between 0 and 2")
        self._model = model
        self._temperature = temperature

    async def classify(self, *, appraisal: AppraisalProjection) -> PrivateImpressionDraft | None:
        messages = self._messages(appraisal)
        raw = await self._model.complete(messages, temperature=self._temperature)
        try:
            return _materialize_draft(raw, appraisal=appraisal)
        except ValueError as violation:
            # One bounded corrective pass, mirroring the Fact draft adapter:
            # the retry restates the violated contract, every field is still
            # strictly validated, and a second failure propagates unchanged.
            corrected = await self._model.complete(
                [
                    *messages,
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": (
                            "Your answer violated the contract: "
                            + str(violation)
                            + ". Return exactly one corrected JSON object now. Remember: "
                            'retain=false answers contain only {"retain":false}; retain=true '
                            "answers contain hypothesis_ids (a non-empty subset of the offered "
                            "ids), confidence (integer 0..10000), and expiry_condition (one of "
                            + ", ".join(EXPIRY_CONDITIONS)
                            + ")."
                        ),
                    },
                ],
                temperature=self._temperature,
            )
            return _materialize_draft(corrected, appraisal=appraisal)

    @staticmethod
    def _messages(appraisal: AppraisalProjection) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Decide whether one accepted appraisal of an interaction is worth keeping as a "
                    "private impression: the companion's fallible, internal-only working reading of "
                    "the user or the relationship. It is never shown to the user and is never a "
                    "fact. Return exactly one JSON object. Use retain=false for ordinary, "
                    "already-covered, or too-uncertain material. If retain=true return "
                    "hypothesis_ids (a non-empty subset of the offered hypothesis ids that together "
                    "form the reading), confidence (an integer in basis points 0..10000 reflecting "
                    "how tentatively she should hold it), and expiry_condition, one of: "
                    + ", ".join(EXPIRY_CONDITIONS)
                    + ". Do not return prose, ids you were not offered, evidence, facts, actions, "
                    "or world changes."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "subject_ref": appraisal.subject_ref,
                        "appraisal_confidence_bp": appraisal.confidence_bp,
                        "hypotheses": [
                            {
                                "hypothesis_id": item.hypothesis_id,
                                "meaning": item.meaning,
                                "attribution": item.attribution,
                                "severity": item.severity,
                                "weight_bp": item.weight_bp,
                            }
                            for item in appraisal.hypotheses
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


def _materialize_draft(
    raw: str, *, appraisal: AppraisalProjection
) -> PrivateImpressionDraft | None:
    try:
        value = json.loads(extract_json_object_text(raw))
    except json.JSONDecodeError as exc:
        raise ValueError("private impression model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("private impression model did not return one JSON object")
    retain = value.get("retain")
    if not isinstance(retain, bool):
        raise ValueError("private impression retain must be boolean")
    if not retain:
        if set(value) != {"retain"}:
            raise ValueError("private impression no-change may contain only retain")
        return None
    hypothesis_ids = value.get("hypothesis_ids")
    confidence = value.get("confidence")
    expiry = value.get("expiry_condition")
    if (
        isinstance(confidence, float)
        and not isinstance(confidence, bool)
        and 0.0 <= confidence <= 1.0
    ):
        confidence = round(confidence * 10_000)
    offered = {item.hypothesis_id for item in appraisal.hypotheses}
    if (
        not isinstance(hypothesis_ids, list)
        or not hypothesis_ids
        or len(hypothesis_ids) != len(set(hypothesis_ids))
        or any(not isinstance(item, str) or item not in offered for item in hypothesis_ids)
        or isinstance(confidence, bool)
        or not isinstance(confidence, int)
        or not 0 <= confidence <= 10_000
        or expiry not in EXPIRY_CONDITIONS
    ):
        raise ValueError("private impression fields are invalid or not appraisal-grounded")
    # Preserve the appraisal's own hypothesis order so the derived identity
    # and interpretation refs are deterministic across retries.
    ordered = tuple(
        item.hypothesis_id for item in appraisal.hypotheses if item.hypothesis_id in set(hypothesis_ids)
    )
    return PrivateImpressionDraft(
        hypothesis_ids=ordered,
        confidence_bp=confidence,
        expiry_condition=expiry,  # type: ignore[arg-type]
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
            (
                item
                for item in projection.committed_world_event_refs
                if item.event_id == source_ref
            ),
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
    work_status: Literal["no_change", "accepted"] | None = None


class PrivateImpressionTriggerRuntime:
    """Drain one claimed-or-open ``private_impression_deliberation`` trigger."""

    def __init__(
        self,
        *,
        ledger,
        adapter: PrivateImpressionDraftAdapter,
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:private-impression-trigger-runtime",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("private impression runtime needs an owner and positive lease")
        self._ledger = ledger
        self._adapter = adapter
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_one(self) -> PrivateImpressionRunResult:
        projection = await _project(self._ledger)
        process = next(
            (
                item
                for item in projection.trigger_processes
                if item.process_kind == "private_impression_deliberation"
                and item.state != "terminal"
            ),
            None,
        )
        if process is None:
            return PrivateImpressionRunResult(trigger_id="", status="idle")
        source_event = await self._source_event(process)
        active = await self._claim_or_reclaim(
            process=process, source_event=source_event, projection=projection
        )
        if active is None:
            return PrivateImpressionRunResult(
                trigger_id=process.trigger_id, status="owned_elsewhere"
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
                process=active, source_event=source_event, cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:no-source",
            )
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        try:
            draft = await self._adapter.classify(appraisal=appraisal)
        except ValueError:
            # A malformed or overreaching model draft has no durable meaning.
            # Consume the opportunity; a later appraisal opens its own anchor.
            await self._complete(
                process=active, source_event=source_event, cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:invalid-draft",
            )
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )
        if draft is None:
            await self._complete(
                process=active, source_event=source_event, cursor=cursor,
                outcome_ref=f"outcome:{active.trigger_id}:no-change",
            )
            return PrivateImpressionRunResult(
                trigger_id=active.trigger_id, status="processed", work_status="no_change"
            )

        accepted_ref = await self._accept(
            appraisal=appraisal, draft=draft, source_event=source_event, before=before,
        )
        completion_cursor = _cursor(await _project(self._ledger))
        await self._complete(
            process=active, source_event=source_event, cursor=completion_cursor,
            outcome_ref=f"outcome:{active.trigger_id}:accepted:{accepted_ref}",
        )
        return PrivateImpressionRunResult(
            trigger_id=active.trigger_id, status="processed", work_status="accepted"
        )

    async def _accept(
        self, *, appraisal: AppraisalProjection, draft: PrivateImpressionDraft,
        source_event: WorldEvent, before,
    ) -> str:
        """Record the typed proposal, then drive the existing acceptance seam."""

        cursor = _cursor(before)
        logical_time = before.logical_time
        if logical_time is None:
            raise ValueError("private impression acceptance requires authoritative time")
        source_committed = next(
            item
            for item in before.committed_world_event_refs
            if item.event_id == source_event.event_id
        )
        # The proposal identity is cursor-pinned: an acceptance stranded by an
        # interleaved commit leaves only an inert audit, and the next pass
        # derives a fresh proposal at the new cursor instead of force-fitting
        # stale frozen timestamps.
        identity = _digest({
            "contract": PrivateImpressionDraftAdapter.VERSION,
            "world_id": self._ledger.world_id,
            "source_event_ref": source_event.event_id,
            "evaluated_world_revision": cursor.world_revision,
        })
        proposal_id = f"proposal:private-impression:{identity}"
        change_id = f"change:private-impression:{identity}"
        transition_id = f"transition:private-impression:{identity}"
        acceptance_id = f"acceptance:private-impression:{identity}"
        accepted_event_id = f"event:private-impression:accepted:{identity}"
        appraisal_refs = tuple(
            AppraisalMeaningRef(
                appraisal_id=appraisal.appraisal_id,
                hypothesis_id=hypothesis_id,
                source_cluster_ref=appraisal.source_cluster_ref,
                accepted_change_id=appraisal.origin.change_id,
                accepted_transition_id=appraisal.origin.transition_id,
            )
            for hypothesis_id in draft.hypothesis_ids
        )
        evidence = EvidenceRef(
            ref_id=source_event.event_id,
            evidence_type="committed_world_event",
            claim_purpose="private_hypothesis",
            source_world_revision=source_committed.world_revision,
            immutable_hash=source_committed.payload_hash,
        )
        impression = PrivateImpressionProjection(
            impression_id="impression:" + _digest({
                "world_id": self._ledger.world_id,
                "appraisal_id": appraisal.appraisal_id,
                "hypothesis_ids": list(draft.hypothesis_ids),
            }),
            entity_revision=1,
            subject_ref=appraisal.subject_ref,
            interpretation_refs=tuple(
                f"appraisal:{item.appraisal_id}:{item.hypothesis_id}" for item in appraisal_refs
            ),
            source_refs=(source_event.event_id,),
            confidence_bp=draft.confidence_bp,
            first_seen=logical_time,
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
            "expected_entity_revision": 0,
            "evidence_refs": [evidence.model_dump(mode="json")],
            "appraisal_refs": [item.model_dump(mode="json") for item in appraisal_refs],
            "policy_refs": list(PRIVATE_IMPRESSION_POLICY_REFS),
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
                "transition_kind": "open",
                "change_id": change_id,
                "transition_id": transition_id,
                "evaluated_world_revision": payload["evaluated_world_revision"],
                "expected_entity_revision": 0,
                "proposed_change_hash": payload["accepted_change_hash"],
                "evidence_refs": payload["evidence_refs"],
                "appraisal_refs": payload["appraisal_refs"],
                "policy_refs": payload["policy_refs"],
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

    def _event(
        self, *, event_id: str, event_type: str, logical_time, source_event: WorldEvent,
        payload: dict, fallback_identity: str,
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
            ) or fallback_identity,
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
            if (
                process.claim_lease.owner_id == self._owner_id
                and at <= process.claim_lease.expires_at
            ):
                return process
            if at < process.claim_lease.expires_at:
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
            commit_id="commit:private-impression:claim:" + _digest([process.trigger_id, attempt_id]),
        )
        return claimed

    async def _complete(
        self, *, process: TriggerProcess, source_event: WorldEvent,
        cursor: ProjectionCursor, outcome_ref: str,
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
    "PrivateImpressionDraftAdapter",
    "PrivateImpressionRunResult",
    "PrivateImpressionTriggerOpener",
    "PrivateImpressionTriggerRuntime",
    "private_impression_opportunity",
]
