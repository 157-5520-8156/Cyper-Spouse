"""LLM-authored but authority-bounded temporary world events.

This is the missing bridge between a living situation and the existing
occurrence/aftermath pipeline.  The source adapter exposes only currently
verified situations.  The model chooses one opaque situation and writes a
short subjective moment; the runtime owns every identity, time, participant,
location, privacy and ledger mutation.
"""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
import logging
from typing import Protocol

from .event_identity import domain_idempotency_key
from .life_content_store import ImmutableLifeContentStore, StoredLifeContent, life_content_payload_hash
from .life_events import WorldOccurrenceActivatedPayload, WorldOccurrenceCommittedPayload
from .occurrence_content_coordinator import OutcomeCandidateContent
from .open_world_event_draft import (
    OpenWorldEventModel,
    OpenWorldEventSituation,
    parse_open_world_event_draft,
)
from .schema_core import FrozenModel
from .schemas import DueWindow, EvidenceRef, ProjectionCursor, WorldEvent, WorldOccurrenceProjection


_LOG = logging.getLogger(__name__)
_POLICY = "policy:open-world-event-v1"


class OpenWorldSituationSource(Protocol):
    def situations(self, *, projection: object, wake_event_ref: str) -> tuple[OpenWorldEventSituation, ...]: ...


class OpenWorldEventRunResult(FrozenModel):
    status: str
    reason_code: str
    proposal_id: str | None = None
    occurrence_id: str | None = None


class ActivePlanSituationSource:
    """Derive safe temporary-event situations from the current active plan."""

    def __init__(self, *, owner_actor_ref: str) -> None:
        if not owner_actor_ref:
            raise ValueError("open-world situation source needs an owner")
        self._owner = owner_actor_ref

    def situations(self, *, projection: object, wake_event_ref: str) -> tuple[OpenWorldEventSituation, ...]:
        plans = tuple(
            item for item in getattr(projection, "plans", ())
            if item.owner_actor_ref == self._owner and item.status == "active" and item.location_ref
        )
        situations: list[OpenWorldEventSituation] = []
        for plan in sorted(plans, key=lambda item: item.plan_id)[:4]:
            participants = tuple(
                ref for ref in plan.participant_refs
                if ref and ref != self._owner
            )
            common = {
                "location_token": plan.location_ref,
                "privacy": "personal" if participants else "shareable",
                "duration_minutes": min(60, max(5, int((plan.ends_at - plan.starts_at).total_seconds() // 60)))
                if getattr(plan, "ends_at", None) and getattr(plan, "starts_at", None)
                else 15,
            }
            situations.extend(
                (
                    OpenWorldEventSituation(
                        token=_token(plan.plan_id, wake_event_ref, "noticed_small_thing"),
                        event_kind="noticed_small_thing",
                        safe_summary="在当前已确认的活动处境里，注意到一个具体的小变化。",
                        participant_tokens=participants,
                        **common,
                    ),
                    OpenWorldEventSituation(
                        token=_token(plan.plan_id, wake_event_ref, "minor_setback"),
                        event_kind="minor_setback",
                        safe_summary="当前活动出现一个轻微、不危险的阻滞，需要临时调整。",
                        participant_tokens=participants,
                        **common,
                    ),
                )
            )
            if participants:
                situations.extend(
                    (
                        OpenWorldEventSituation(
                            token=_token(plan.plan_id, wake_event_ref, "npc_friction"),
                            event_kind="npc_friction",
                            safe_summary="与当前已确认在场的一位 NPC 有一次短暂摩擦。",
                            participant_tokens=participants,
                            **common,
                        ),
                        OpenWorldEventSituation(
                            token=_token(plan.plan_id, wake_event_ref, "unexpected_help"),
                            event_kind="unexpected_help",
                            safe_summary="当前在场的一位 NPC 提供了一个小小的帮助。",
                            participant_tokens=participants,
                            **common,
                        ),
                    )
                )
        return tuple(situations)


class OpenWorldEventRuntime:
    def __init__(
        self,
        *,
        ledger,
        content_store: ImmutableLifeContentStore,
        model: OpenWorldEventModel,
        situation_source: OpenWorldSituationSource,
        owner_actor_ref: str,
        actor: str = "worker:world-v2:open-world-event",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("open-world event runtime requires owner and actor")
        self._ledger = ledger
        self._store = content_store
        self._model = model
        self._source = situation_source
        self._owner = owner_actor_ref
        self._actor = actor
        self._model_id = str(getattr(model, "model", "")).strip() or type(model).__name__

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> OpenWorldEventRunResult:
        projection = self._ledger.project()
        wake = self._wake(projection, wake_event_ref)
        if wake is None:
            return OpenWorldEventRunResult(
                status="rejected", reason_code="open_world_event.wake_unavailable"
            )
        situations = self._source.situations(projection=projection, wake_event_ref=wake_event_ref)
        if not situations:
            proposal_id = "proposal:open-world-event:" + _digest(
                {"world": self._ledger.world_id, "wake": wake_event_ref}
            )
            if self._proposal_event(proposal_id) is None:
                self._record_no_op(
                    projection=projection,
                    wake=wake,
                    proposal_id=proposal_id,
                    model="runtime:no-verified-situation",
                    raw_output='{"decision":"no_op","reason":"no_verified_situation"}',
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            return OpenWorldEventRunResult(
                status="no_op", reason_code="open_world_event.no_verified_situation",
                proposal_id=proposal_id,
            )
        proposal_id = "proposal:open-world-event:" + _digest(
            {"world": self._ledger.world_id, "wake": wake_event_ref}
        )
        existing = self._proposal_event(proposal_id)
        if existing is None:
            try:
                draft = parse_open_world_event_draft(
                    raw=await self._model.complete(self._messages(situations), temperature=0.4),
                    offered=situations,
                    model=self._model_id,
                )
            except (TimeoutError, ConnectionError, OSError, ValueError) as exc:
                _LOG.warning("open-world event deliberation unavailable: %s", exc)
                return OpenWorldEventRunResult(
                    status="deferred", reason_code="open_world_event.model_unavailable"
                )
            if draft.decision == "no_op":
                self._record_no_op(
                    projection=projection,
                    wake=wake,
                    proposal_id=proposal_id,
                    model=draft.model,
                    raw_output=draft.raw_output,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
                return OpenWorldEventRunResult(
                    status="no_op", reason_code="open_world_event.model_declined", proposal_id=proposal_id
                )
            selected = next(item for item in situations if item.token == draft.situation_token)
            proposal_event = self._record_proposal(
                projection=projection,
                wake=wake,
                proposal_id=proposal_id,
                selected=selected,
                moment=draft.moment or "",
                model=draft.model,
                raw_output=draft.raw_output,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
        else:
            proposal_event = existing
            if proposal_event.payload().get("decision") == "no_op":
                return OpenWorldEventRunResult(
                    status="no_op", reason_code="open_world_event.model_declined_recovered",
                    proposal_id=proposal_id,
                )
            selected = next(
                (item for item in situations if item.token == proposal_event.payload().get("situation_token")),
                None,
            )
            if selected is None:
                return OpenWorldEventRunResult(
                    status="rejected", reason_code="open_world_event.proposal_situation_stale",
                    proposal_id=proposal_id,
                )
        occurrence_id = "occurrence:open-world:" + _digest(
            {"world": self._ledger.world_id, "proposal": proposal_id}
        )
        occurrence = self._existing_occurrence(occurrence_id)
        if occurrence is None:
            moment = self._proposal_moment(proposal_event)
            occurrence = self._commit_occurrence(
                projection=self._ledger.project(), wake=wake, proposal_event=proposal_event,
                occurrence_id=occurrence_id, situation=selected, moment=moment,
                trace_id=trace_id, correlation_id=correlation_id,
            )
        if occurrence.status == "committed":
            self._activate(
                occurrence=occurrence, wake=wake, trace_id=trace_id, correlation_id=correlation_id
            )
        return OpenWorldEventRunResult(
            status="recovered" if existing is not None else "committed",
            reason_code="open_world_event.accepted",
            proposal_id=proposal_id,
            occurrence_id=occurrence_id,
        )

    def _record_proposal(
        self, *, projection, wake: WorldEvent, proposal_id: str,
        selected: OpenWorldEventSituation, moment: str, model: str,
        raw_output: str, trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        moment_ref = "content:open-world-moment:" + _digest({"proposal": proposal_id})
        moment_hash = life_content_payload_hash(moment)
        self._store.put_if_absent(
            StoredLifeContent(
                content_ref=moment_ref,
                content_kind="outcome_candidate",
                content_payload_hash=moment_hash,
                text=moment,
            )
        )
        payload = {
            "proposal_id": proposal_id,
            "proposal_kind": "open_world_event",
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "situation_token": selected.token,
            "event_kind": selected.event_kind,
            "participant_tokens": list(selected.participant_tokens),
            "location_token": selected.location_token,
            "privacy": selected.privacy,
            "moment_ref": moment_ref,
            "moment_hash": moment_hash,
            "moment_scope": "subjective",
            "model": model,
            "raw_output_hash": "sha256:" + hashlib.sha256(raw_output.encode()).hexdigest(),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:open-world-event:proposal:" + _digest(proposal_id),
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:open-world-event",
            trace_id=trace_id or wake.trace_id,
            causation_id=wake.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "open-world-event-proposal:" + _digest(proposal_id),
            payload=payload,
        )
        cursor = _cursor(projection)
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=cursor,
            commit_id="commit:open-world-event:proposal:" + _digest(proposal_id),
        )
        return event

    def _record_no_op(
        self, *, projection, wake: WorldEvent, proposal_id: str, model: str,
        raw_output: str, trace_id: str, correlation_id: str,
    ) -> WorldEvent:
        payload = {
            "proposal_id": proposal_id,
            "proposal_kind": "open_world_event",
            "decision": "no_op",
            "trigger_id": wake.event_id,
            "evaluated_world_revision": projection.world_revision,
            "wake_event_ref": wake.event_id,
            "wake_event_payload_hash": wake.payload_hash,
            "model": model,
            "raw_output_hash": "sha256:" + hashlib.sha256(raw_output.encode()).hexdigest(),
        }
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:open-world-event:proposal:" + _digest(proposal_id),
            event_type="ProposalRecorded",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:open-world-event",
            trace_id=trace_id or wake.trace_id,
            causation_id=wake.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="ProposalRecorded", world_id=self._ledger.world_id, payload=payload
            ) or "open-world-event-proposal:" + _digest(proposal_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:open-world-event:proposal:" + _digest(proposal_id),
        )
        return event

    def _commit_occurrence(
        self, *, projection, wake: WorldEvent, proposal_event: WorldEvent,
        occurrence_id: str, situation: OpenWorldEventSituation, moment: str,
        trace_id: str, correlation_id: str,
    ) -> WorldOccurrenceProjection:
        suffix = _digest({"occurrence": occurrence_id, "moment": moment})
        texts = (
            moment,
            "这件事没有继续扩大，后来很快恢复了原本的节奏。",
            "她没有马上处理，但在晚些时候还会想起这个小插曲。",
        )
        candidates = tuple(
            OutcomeCandidateContent(
                candidate_result_ref=f"candidate:open-world:{suffix}:{index}",
                result_id=f"result:open-world:{suffix}:{index}",
                result_payload_ref=f"content:open-world-result:{suffix}:{index}",
                result_payload_hash=life_content_payload_hash(text),
                privacy_class=situation.privacy,
                content_ref=f"content:open-world-result:{suffix}:{index}",
                text=text,
            )
            for index, text in enumerate(texts, start=1)
        )
        for item in candidates:
            self._store.put_if_absent(item.sidecar_record())
        refs = tuple(item.candidate_result_ref for item in candidates)
        occurrence = WorldOccurrenceProjection(
            occurrence_id=occurrence_id,
            entity_revision=1,
            trigger_ref=f"open-world:{wake.event_id}",
            participant_refs=(self._owner, *situation.participant_tokens),
            location_ref=situation.location_token,
            time_window=DueWindow(
                opens_at=wake.logical_time,
                closes_at=wake.logical_time + timedelta(minutes=situation.duration_minutes),
            ),
            candidate_outcome_refs=refs,
            candidate_outcomes=tuple(item.descriptor() for item in candidates),
            visibility=situation.privacy,
            status="committed",
        )
        evidence = EvidenceRef(
            ref_id=wake.event_id,
            evidence_type="committed_world_event",
            claim_purpose="current_fact",
            source_world_revision=next(
                item.world_revision for item in projection.committed_world_event_refs
                if item.event_id == wake.event_id
            ),
            immutable_hash=wake.payload_hash,
        )
        payload = WorldOccurrenceCommittedPayload(
            change_id="change:open-world-event:commit:" + suffix,
            transition_id="transition:open-world-event:commit:" + suffix,
            expected_entity_revision=0,
            evidence_refs=(evidence,),
            policy_refs=(_POLICY, f"event-kind:{situation.event_kind}", f"proposal:{proposal_event.event_id}"),
            occurrence=occurrence,
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:open-world-event:committed:" + suffix,
            event_type="WorldOccurrenceCommitted",
            world_id=self._ledger.world_id,
            logical_time=wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:open-world-event",
            trace_id=trace_id or wake.trace_id,
            causation_id=proposal_event.event_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="WorldOccurrenceCommitted", world_id=self._ledger.world_id, payload=payload
            ) or "open-world-event-committed:" + suffix,
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:open-world-event:committed:" + suffix,
        )
        return occurrence

    def _activate(self, *, occurrence, wake: WorldEvent, trace_id: str, correlation_id: str) -> None:
        projection = self._ledger.project()
        current = next(item for item in projection.world_occurrences if item.occurrence_id == occurrence.occurrence_id)
        payload = WorldOccurrenceActivatedPayload(
            change_id="change:open-world-event:activate:" + _digest(current.occurrence_id),
            transition_id="transition:open-world-event:activate:" + _digest(current.occurrence_id),
            expected_entity_revision=current.entity_revision,
            evidence_refs=(EvidenceRef(
                ref_id=wake.event_id,
                evidence_type="committed_world_event",
                claim_purpose="current_fact",
                source_world_revision=next(
                    item.world_revision for item in projection.committed_world_event_refs
                    if item.event_id == wake.event_id
                ),
                immutable_hash=wake.payload_hash,
            ),),
            policy_refs=(_POLICY,),
            occurrence_id=current.occurrence_id,
            activated_at=projection.logical_time or wake.logical_time,
            satisfied_precondition_refs=(),
        ).model_dump(mode="json")
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id="event:open-world-event:activated:" + _digest(current.occurrence_id),
            event_type="WorldOccurrenceActivated",
            world_id=self._ledger.world_id,
            logical_time=projection.logical_time or wake.logical_time,
            created_at=wake.created_at,
            actor=self._actor,
            source="world-v2:open-world-event",
            trace_id=trace_id or wake.trace_id,
            causation_id=current.occurrence_id,
            correlation_id=correlation_id or wake.correlation_id,
            idempotency_key=domain_idempotency_key(
                event_type="WorldOccurrenceActivated", world_id=self._ledger.world_id, payload=payload
            ) or "open-world-event-activated:" + _digest(current.occurrence_id),
            payload=payload,
        )
        self._ledger.commit_at_cursor(
            (event,), expected_cursor=_cursor(projection),
            commit_id="commit:open-world-event:activated:" + _digest(current.occurrence_id),
        )

    def _proposal_event(self, proposal_id: str) -> WorldEvent | None:
        located = self._ledger.lookup_event_commit(
            "event:open-world-event:proposal:" + _digest(proposal_id)
        )
        if located is None or located[0].event_type != "ProposalRecorded":
            return None
        if located[0].payload().get("proposal_id") != proposal_id:
            return None
        return located[0]

    def _existing_occurrence(self, occurrence_id: str):
        return next(
            (item for item in self._ledger.project().world_occurrences if item.occurrence_id == occurrence_id),
            None,
        )

    def _proposal_moment(self, proposal_event: WorldEvent) -> str:
        ref = proposal_event.payload().get("moment_ref")
        expected = proposal_event.payload().get("moment_hash")
        if not isinstance(ref, str) or not isinstance(expected, str):
            raise ValueError("open-world proposal has no moment binding")
        stored = self._store.read_exact(content_ref=ref)
        if stored is None or stored.content_payload_hash != expected:
            raise ValueError("open-world proposal moment sidecar is unavailable")
        return stored.text

    @staticmethod
    def _messages(situations: tuple[OpenWorldEventSituation, ...]) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Choose whether one small temporary event happens in an already verified situation. "
                    "Return exactly JSON. You may choose only an offered situation_token and write one short "
                    "subjective moment (include moment_scope=subjective). This prose is an impression, not "
                    "external evidence. Do not invent a person, place, time, fact, action authority, event id, "
                    "hash, recipient, or policy. If nothing stands out, return {\"decision\":\"no_op\"}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"situations": [item.model_dump(mode="json") for item in situations]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]

    def _wake(self, projection, wake_event_ref: str) -> WorldEvent | None:
        located = projection.committed_world_event_refs
        ref = next((item for item in located if item.event_id == wake_event_ref), None)
        if ref is None or ref.event_type not in {
            "ClockAdvanced", "ActivityStarted", "ActivityResumed", "ActivityCompleted"
        }:
            return None
        event_commit = self._ledger.lookup_event_commit(wake_event_ref)
        if event_commit is None:
            return None
        event, commit = event_commit
        if (
            event.world_id != self._ledger.world_id
            or event.event_type != ref.event_type
            or event.payload_hash != ref.payload_hash
            or event.logical_time != ref.logical_time
            or ref.world_revision > projection.world_revision
            or event.event_id not in commit.event_ids
        ):
            return None
        return event


def _token(plan_id: str, wake_event_ref: str, kind: str) -> str:
    return "situation:" + _digest({"plan": plan_id, "wake": wake_event_ref, "kind": kind})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cursor(projection) -> ProjectionCursor:
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


__all__ = [
    "ActivePlanSituationSource",
    "OpenWorldEventRunResult",
    "OpenWorldEventRuntime",
    "OpenWorldSituationSource",
]
