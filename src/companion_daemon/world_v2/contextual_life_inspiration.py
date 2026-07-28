"""Source-bound interaction influence over the character's future life.

This lane does not look for place words, requests, or any other semantic
category in deterministic code.  A recent user observation, committed fact,
or accepted memory merely opens one consideration.  The character model may
form a free-text aspiration or decline it.  A later consideration lets the
same character choose among *reviewed and currently feasible* future
openings.  Consequently an arbitrary place name can influence her intentions,
but cannot become a visited-place fact without an executable reviewed opening
and the ordinary activity settlement pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Literal

import httpx
from pydantic import Field, model_validator

from .aspiration_events import ASPIRATION_POLICY_REF, AspirationPlantedPayload
from .aspiration_runtime import AspirationRuntime
from .context_resolver import query_from_projection
from .contextual_life_source_material import (
    ContextualLifeSourceMaterial,
    ContextualLifeSourceMaterialCompiler,
)
from .contextual_life_retry import (
    record_invisible_source_disposition,
    record_technical_failure,
    retry_is_due,
)
from .errors import ConcurrencyConflict
from .event_identity import domain_idempotency_key
from .life_author_runtime import (
    LifeAuthorModel,
    LifeAuthorModelFailure,
    LifeContextCapsuleCompiler,
    compile_life_decision_context,
)
from .life_author_seed import ReviewedLifeSeedCatalog, ReviewedLifeSeedFutureCandidate
from .schema_core import FrozenModel, PrivacyClass
from .schemas import (
    AspirationProjection,
    CommittedWorldEventRef,
    EvidenceRef,
    ProjectionCursor,
    WorldEvent,
)


_LOG = logging.getLogger(__name__)
_POLICY = "policy:contextual-life-inspiration.1"
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


class ContextualLifeInspirationResult(FrozenModel):
    status: Literal[
        "formed",
        "planned",
        "declined",
        "no_source",
        "no_opening",
        "deferred",
        "recovered",
    ]
    reason_code: str
    source_event_ref: str | None = None
    aspiration_id: str | None = None
    plan_event_ref: str | None = None
    reviewed_followup_status: str | None = None


class _FormationDecision(FrozenModel):
    decision: Literal["form", "no_op"]
    impulse_text: str | None = Field(default=None, min_length=1, max_length=240)
    source_event_refs: tuple[str, ...] = ()
    privacy: PrivacyClass | None = None

    @model_validator(mode="after")
    def shape_matches_decision(self) -> "_FormationDecision":
        formed = self.decision == "form"
        if formed != bool(
            self.impulse_text is not None and self.source_event_refs and self.privacy is not None
        ):
            raise ValueError("formed inspiration requires text, sources, and privacy")
        if not formed and (
            self.impulse_text is not None or self.source_event_refs or self.privacy is not None
        ):
            raise ValueError("declined inspiration cannot carry a candidate")
        if len(self.source_event_refs) != len(set(self.source_event_refs)):
            raise ValueError("inspiration source references must be unique")
        return self


class _PlanDecision(FrozenModel):
    decision: Literal["select", "no_op"]
    candidate_token: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def shape_matches_decision(self) -> "_PlanDecision":
        if (self.decision == "select") != (self.candidate_token is not None):
            raise ValueError("plan selection must bind exactly one candidate")
        return self


class ContextualLifeInspirationRuntime:
    """Turn interaction evidence into character-owned, executable influence."""

    def __init__(
        self,
        *,
        ledger,
        catalog: ReviewedLifeSeedCatalog,
        model: LifeAuthorModel,
        capsule_compiler: LifeContextCapsuleCompiler,
        source_material_compiler: ContextualLifeSourceMaterialCompiler,
        plan_committer: AspirationRuntime,
        owner_actor_ref: str,
        reviewed_followup: AspirationRuntime | None = None,
        actor: str = "worker:world-v2:contextual-life-inspiration",
    ) -> None:
        if not owner_actor_ref or not actor:
            raise ValueError("contextual life inspiration requires owner and actor")
        self._ledger = ledger
        self._catalog = catalog
        self._model = model
        self._capsules = capsule_compiler
        self._source_material = source_material_compiler
        self._plan_committer = plan_committer
        self._reviewed_followup = reviewed_followup
        self._owner = owner_actor_ref
        self._actor = actor
        self._model_id = str(getattr(model, "model", "")).strip() or type(model).__name__

    async def advance_once(
        self, *, wake_event_ref: str, trace_id: str, correlation_id: str
    ) -> ContextualLifeInspirationResult:
        projection = self._ledger.project()
        located_wake = self._ledger.lookup_event_commit(wake_event_ref)
        wake_event = located_wake[0] if located_wake is not None else None
        wake_commit = located_wake[1] if located_wake is not None else None
        transition = (
            next(
                (
                    item
                    for item in reversed(projection.clock_transition_history)
                    if item.clock_event_ref == wake_event_ref
                ),
                None,
            )
        )
        if (
            wake_event is None
            or wake_commit is None
            or wake_event.event_type != "ClockAdvanced"
            or transition is None
            or transition.clock_event_ref != wake_event_ref
            or transition.payload_hash != wake_event.payload_hash
        ):
            return ContextualLifeInspirationResult(
                status="deferred",
                reason_code="contextual_life_inspiration.wake_not_exact_clock",
            )
        wake = CommittedWorldEventRef(
            event_id=wake_event.event_id,
            event_type=wake_event.event_type,
            world_revision=transition.computed_world_revision,
            payload_hash=wake_event.payload_hash,
            logical_time=wake_event.logical_time,
        )

        # Existing inspirations get their character-owned planning chance
        # before a new source is considered.  This prevents a stream of user
        # messages from starving older intentions.
        planned = await self._plan_one(wake=wake, trace_id=trace_id, correlation_id=correlation_id)
        if planned is not None and planned.status == "planned":
            return planned

        formed = await self._form_one(wake=wake, trace_id=trace_id, correlation_id=correlation_id)
        if formed.status in {"formed", "deferred"}:
            return formed

        if self._reviewed_followup is not None:
            reviewed = await self._reviewed_followup.advance_once(
                wake_event_ref=wake_event_ref,
                trace_id=trace_id,
                correlation_id=correlation_id,
            )
            reviewed_status = getattr(reviewed, "status", None)
            if isinstance(reviewed_status, str) and reviewed_status:
                return formed.model_copy(update={"reviewed_followup_status": reviewed_status})
        return planned or formed

    async def _form_one(
        self, *, wake, trace_id: str, correlation_id: str
    ) -> ContextualLifeInspirationResult:
        projection = self._ledger.project()
        source = self._next_source(projection)
        if source is None:
            return ContextualLifeInspirationResult(
                status="no_source",
                reason_code="contextual_life_inspiration.no_unconsidered_source",
            )
        _, ref = source
        if not retry_is_due(
            projection,
            lane="formation",
            source_event_ref=ref.event_id,
        ):
            return ContextualLifeInspirationResult(
                status="deferred",
                reason_code="contextual_life_inspiration.retry_wait",
                source_event_ref=ref.event_id,
            )
        proposal_id = "proposal:contextual-life-inspiration:" + _digest(
            {"world_id": self._ledger.world_id, "source_event_ref": ref.event_id}
        )
        proposal_event_id = "event:" + proposal_id
        existing = self._ledger.lookup_event_commit(proposal_event_id)
        if existing is not None:
            payload = existing[0].payload()
            aspiration_id = payload.get("aspiration_id")
            return ContextualLifeInspirationResult(
                status="recovered" if aspiration_id else "declined",
                reason_code="contextual_life_inspiration.decision_recovered",
                source_event_ref=ref.event_id,
                aspiration_id=aspiration_id if isinstance(aspiration_id, str) else None,
            )

        query = query_from_projection(
            projection,
            actor_ref=self._owner,
            trigger_ref=ref.event_id,
        )
        capsule = self._capsules.compile_for_deliberation(query).capsule
        context_cursor = ProjectionCursor(
            world_revision=capsule.world_revision,
            deliberation_revision=capsule.deliberation_revision,
            ledger_sequence=capsule.ledger_sequence,
        )
        source_material = self._source_material.compile(
            cursor=context_cursor,
            source_event_ref=ref.event_id,
            owner_actor_ref=self._owner,
        )
        if source_material is None:
            try:
                record_invisible_source_disposition(
                    ledger=self._ledger,
                    projection=projection,
                    source_event_ref=ref.event_id,
                    source_payload_hash=ref.payload_hash,
                    context_cursor=context_cursor,
                    actor=self._actor,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.context_cursor_stale",
                    source_event_ref=ref.event_id,
                )
            return ContextualLifeInspirationResult(
                status="declined",
                reason_code="contextual_life_inspiration.source_not_character_visible",
                source_event_ref=ref.event_id,
            )
        try:
            decision, raw = await self._deliberate_formation(
                source_material=source_material,
                context=compile_life_decision_context(capsule),
            )
        except LifeAuthorModelFailure:
            try:
                record_technical_failure(
                    ledger=self._ledger,
                    projection=projection,
                    lane="formation",
                    source_event_ref=ref.event_id,
                    source_payload_hash=ref.payload_hash,
                    context_cursor=context_cursor,
                    failure_code="formation_model_unavailable",
                    actor=self._actor,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.context_cursor_stale",
                    source_event_ref=ref.event_id,
                )
            return ContextualLifeInspirationResult(
                status="deferred",
                reason_code="contextual_life_inspiration.model_unavailable",
                source_event_ref=ref.event_id,
            )

        aspiration_id = (
            "aspiration:contextual:"
            + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "source_event_refs": decision.source_event_refs,
                    "impulse_text": decision.impulse_text,
                }
            )
            if decision.decision == "form"
            else None
        )
        proposal_payload = {
            "proposal_id": proposal_id,
            "proposal_kind": "contextual_life_inspiration",
            "decision": decision.decision,
            "trigger_id": ref.event_id,
            "evaluated_world_revision": context_cursor.world_revision,
            "source_event_refs": list(decision.source_event_refs)
            if decision.decision == "form"
            else [ref.event_id],
            "impulse_text": decision.impulse_text,
            "privacy": decision.privacy,
            "aspiration_id": aspiration_id,
            "context_identity_version": "contextual-life-inspiration-context.1",
            "context_capsule_id": capsule.capsule_id,
            "context_model_content_hash": hashlib.sha256(
                capsule.model_content_json.encode()
            ).hexdigest(),
            "context_snapshot_hash": capsule.snapshot_hash,
            "context_cursor": context_cursor.model_dump(mode="json"),
            "model": self._model_id,
            "raw_output_hash": _digest(raw),
        }
        proposal_event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=proposal_event_id,
            world_id=self._ledger.world_id,
            event_type="ProposalRecorded",
            logical_time=projection.logical_time,
            created_at=projection.logical_time,
            actor=self._actor,
            source="world-v2:contextual-life-inspiration",
            trace_id=trace_id,
            causation_id=ref.event_id,
            correlation_id=correlation_id,
            idempotency_key=(
                domain_idempotency_key(
                    event_type="ProposalRecorded",
                    world_id=self._ledger.world_id,
                    payload=proposal_payload,
                )
                or "contextual-life-inspiration:" + _digest(proposal_id)
            ),
            payload=proposal_payload,
        )
        events = [proposal_event]
        if decision.decision == "form":
            assert aspiration_id is not None
            assert decision.impulse_text is not None
            assert decision.privacy is not None
            evidence = tuple(
                self._event_evidence(projection, source_ref)
                for source_ref in decision.source_event_refs
            )
            suffix = aspiration_id.removeprefix("aspiration:contextual:")
            planted_event_id = "event:aspiration:contextual:" + suffix
            aspiration = AspirationProjection(
                aspiration_id=aspiration_id,
                entity_revision=1,
                owner_actor_ref=self._owner,
                seed_id="contextual:" + suffix,
                text=decision.impulse_text,
                privacy_class=decision.privacy,
                planted_at=projection.logical_time,
                planted_event_ref=planted_event_id,
                source_event_ref=decision.source_event_refs[0],
            )
            planted_payload = AspirationPlantedPayload(
                change_id="change:aspiration:contextual:" + suffix,
                transition_id="transition:aspiration:contextual:" + suffix,
                expected_entity_revision=0,
                evidence_refs=evidence,
                policy_refs=(ASPIRATION_POLICY_REF, _POLICY),
                aspiration=aspiration,
            ).model_dump(mode="json")
            events.append(
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id=planted_event_id,
                    world_id=self._ledger.world_id,
                    event_type="AspirationPlanted",
                    logical_time=projection.logical_time,
                    created_at=projection.logical_time,
                    actor=self._actor,
                    source="world-v2:contextual-life-inspiration",
                    trace_id=trace_id,
                    causation_id=proposal_event.event_id,
                    correlation_id=correlation_id,
                    idempotency_key=(
                        domain_idempotency_key(
                            event_type="AspirationPlanted",
                            world_id=self._ledger.world_id,
                            payload=planted_payload,
                        )
                        or "contextual-aspiration:" + suffix
                    ),
                    payload=planted_payload,
                )
            )
        try:
            self._ledger.commit_at_cursor(
                tuple(events),
                expected_cursor=context_cursor,
                commit_id="commit:" + proposal_event_id,
            )
        except ConcurrencyConflict:
            # A stale capsule is never silently accepted.  A later wake
            # recompiles against the new prefix and retries the same source.
            return ContextualLifeInspirationResult(
                status="deferred",
                reason_code="contextual_life_inspiration.context_cursor_stale",
                source_event_ref=ref.event_id,
            )
        return ContextualLifeInspirationResult(
            status="formed" if aspiration_id else "declined",
            reason_code=(
                "contextual_life_inspiration.formed"
                if aspiration_id
                else "contextual_life_inspiration.model_declined"
            ),
            source_event_ref=ref.event_id,
            aspiration_id=aspiration_id,
        )

    async def _plan_one(
        self, *, wake, trace_id: str, correlation_id: str
    ) -> ContextualLifeInspirationResult | None:
        projection = self._ledger.project()
        aspirations = tuple(
            item
            for item in projection.aspirations
            if item.owner_actor_ref == self._owner
            and item.status == "active"
            and item.seed_id.startswith("contextual:")
        )
        for aspiration in sorted(aspirations, key=lambda item: item.planted_at):
            local_date = self._catalog.localize(wake.logical_time).date().isoformat()
            check_id = "event:contextual-life-plan-check:" + _digest(
                {
                    "world_id": self._ledger.world_id,
                    "aspiration_id": aspiration.aspiration_id,
                    "local_date": local_date,
                    "catalog_version": self._catalog.version,
                    "catalog_hash": self._catalog.catalog_hash,
                }
            )
            existing_check = self._ledger.lookup_event_commit(check_id)
            if existing_check is None:
                # Replay compatibility for decisions written before catalog
                # identity joined the consideration epoch.  A legacy decision
                # is reusable only under its exact catalog; after a reviewed
                # catalog upgrade the character gets a fresh bounded choice
                # instead of being stuck in deferred for the rest of the day.
                legacy_check_id = "event:contextual-life-plan-check:" + _digest(
                    {
                        "world_id": self._ledger.world_id,
                        "aspiration_id": aspiration.aspiration_id,
                        "local_date": local_date,
                    }
                )
                legacy = self._ledger.lookup_event_commit(legacy_check_id)
                if legacy is not None:
                    legacy_payload = legacy[0].payload()
                    if (
                        legacy_payload.get("catalog_version") == self._catalog.version
                        and legacy_payload.get("catalog_hash") == self._catalog.catalog_hash
                    ):
                        existing_check = legacy
            if existing_check is not None:
                existing_payload = existing_check[0].payload()
                if existing_payload.get("decision") != "select":
                    continue
                if (
                    existing_payload.get("catalog_version") != self._catalog.version
                    or existing_payload.get("catalog_hash") != self._catalog.catalog_hash
                ):
                    return ContextualLifeInspirationResult(
                        status="deferred",
                        reason_code="contextual_life_inspiration.recorded_catalog_changed",
                        aspiration_id=aspiration.aspiration_id,
                    )
                slot = existing_payload.get("slot")
                if not isinstance(slot, dict):
                    return ContextualLifeInspirationResult(
                        status="deferred",
                        reason_code="contextual_life_inspiration.recorded_plan_is_incomplete",
                        aspiration_id=aspiration.aspiration_id,
                    )
                try:
                    committed = self._plan_committer.commit_reviewed_crystallization(
                        aspiration_id=aspiration.aspiration_id,
                        slot=slot,
                        wake=wake,
                        check_event_ref=existing_check[0].event_id,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                except ConcurrencyConflict:
                    committed = False
                if not committed:
                    return ContextualLifeInspirationResult(
                        status="deferred",
                        reason_code="contextual_life_inspiration.plan_commit_deferred",
                        aspiration_id=aspiration.aspiration_id,
                    )
                crystallized = next(
                    item
                    for item in self._ledger.project().aspirations
                    if item.aspiration_id == aspiration.aspiration_id
                )
                return ContextualLifeInspirationResult(
                    status="planned",
                    reason_code="contextual_life_inspiration.recorded_plan_recovered",
                    aspiration_id=aspiration.aspiration_id,
                    plan_event_ref=crystallized.crystallized_plan_ref,
                )
            planted = self._ledger.lookup_event_commit(aspiration.planted_event_ref)
            if planted is None or planted[0].event_type != "AspirationPlanted":
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.plan_source_unavailable",
                    aspiration_id=aspiration.aspiration_id,
                )
            if not retry_is_due(
                projection,
                lane="planning",
                source_event_ref=aspiration.planted_event_ref,
            ):
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.plan_retry_wait",
                    aspiration_id=aspiration.aspiration_id,
                )
            candidates = self._catalog.future_candidates_at(
                instant=wake.logical_time,
                plans=tuple(
                    item for item in projection.plans if item.owner_actor_ref == self._owner
                ),
                npcs=projection.npcs,
                life_arcs=projection.life_arcs,
            )
            if not candidates:
                return ContextualLifeInspirationResult(
                    status="no_opening",
                    reason_code="contextual_life_inspiration.no_reviewed_future_opening",
                    aspiration_id=aspiration.aspiration_id,
                )
            query = query_from_projection(
                projection,
                actor_ref=self._owner,
                trigger_ref=aspiration.planted_event_ref,
            )
            capsule = self._capsules.compile_for_deliberation(query).capsule
            context_cursor = ProjectionCursor(
                world_revision=capsule.world_revision,
                deliberation_revision=capsule.deliberation_revision,
                ledger_sequence=capsule.ledger_sequence,
            )
            try:
                decision, raw = await self._deliberate_plan(
                    aspiration=aspiration,
                    candidates=candidates,
                    context=compile_life_decision_context(capsule),
                )
            except LifeAuthorModelFailure:
                try:
                    record_technical_failure(
                        ledger=self._ledger,
                        projection=projection,
                        lane="planning",
                        source_event_ref=aspiration.planted_event_ref,
                        source_payload_hash=planted[0].payload_hash,
                        context_cursor=context_cursor,
                        failure_code="planning_model_unavailable",
                        actor=self._actor,
                        trace_id=trace_id,
                        correlation_id=correlation_id,
                    )
                except ConcurrencyConflict:
                    return ContextualLifeInspirationResult(
                        status="deferred",
                        reason_code=(
                            "contextual_life_inspiration.plan_context_cursor_stale"
                        ),
                        aspiration_id=aspiration.aspiration_id,
                    )
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.plan_model_unavailable",
                    aspiration_id=aspiration.aspiration_id,
                )
            selected = next(
                (item for item in candidates if item.token == decision.candidate_token),
                None,
            )
            payload = {
                "proposal_id": check_id.removeprefix("event:"),
                "proposal_kind": "contextual_life_plan",
                "decision": decision.decision,
                "trigger_id": aspiration.planted_event_ref,
                "evaluated_world_revision": context_cursor.world_revision,
                "aspiration_id": aspiration.aspiration_id,
                "aspiration_source_event_ref": aspiration.source_event_ref,
                "candidate_token": decision.candidate_token,
                "reviewed_opening_id": selected.opening.id if selected else None,
                "slot": self._slot(selected) if selected else None,
                "catalog_version": self._catalog.version,
                "catalog_hash": self._catalog.catalog_hash,
                "context_identity_version": "contextual-life-plan-context.1",
                "context_capsule_id": capsule.capsule_id,
                "context_model_content_hash": hashlib.sha256(
                    capsule.model_content_json.encode()
                ).hexdigest(),
                "context_snapshot_hash": capsule.snapshot_hash,
                "context_cursor": context_cursor.model_dump(mode="json"),
                "model": self._model_id,
                "raw_output_hash": _digest(raw),
            }
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=check_id,
                world_id=self._ledger.world_id,
                event_type="ProposalRecorded",
                logical_time=projection.logical_time,
                created_at=projection.logical_time,
                actor=self._actor,
                source="world-v2:contextual-life-inspiration",
                trace_id=trace_id,
                causation_id=aspiration.planted_event_ref,
                correlation_id=correlation_id,
                idempotency_key="contextual-life-plan:" + _digest(check_id),
                payload=payload,
            )
            try:
                self._ledger.commit_at_cursor(
                    (event,),
                    expected_cursor=context_cursor,
                    commit_id="commit:" + check_id,
                )
            except ConcurrencyConflict:
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.plan_context_cursor_stale",
                    aspiration_id=aspiration.aspiration_id,
                )
            if selected is None:
                return ContextualLifeInspirationResult(
                    status="declined",
                    reason_code="contextual_life_inspiration.plan_declined",
                    aspiration_id=aspiration.aspiration_id,
                )
            slot = self._slot(selected)
            try:
                committed = self._plan_committer.commit_reviewed_crystallization(
                    aspiration_id=aspiration.aspiration_id,
                    slot=slot,
                    wake=wake,
                    check_event_ref=event.event_id,
                    trace_id=trace_id,
                    correlation_id=correlation_id,
                )
            except ConcurrencyConflict:
                committed = False
            if not committed:
                return ContextualLifeInspirationResult(
                    status="deferred",
                    reason_code="contextual_life_inspiration.plan_commit_deferred",
                    aspiration_id=aspiration.aspiration_id,
                )
            crystallized = next(
                item
                for item in self._ledger.project().aspirations
                if item.aspiration_id == aspiration.aspiration_id
            )
            return ContextualLifeInspirationResult(
                status="planned",
                reason_code="contextual_life_inspiration.reviewed_plan_accepted",
                aspiration_id=aspiration.aspiration_id,
                plan_event_ref=crystallized.crystallized_plan_ref,
            )
        return None

    def _next_source(self, projection) -> tuple[WorldEvent, object] | None:
        # Oldest-unconsidered is deliberate: new traffic cannot starve an
        # already-open source. Reducers maintain this compact ledger-ordered
        # queue, so a wake never rescans the immutable World history.
        for source in projection.pending_contextual_life_sources:
            located = self._ledger.lookup_event_commit(source.source_event_ref)
            if located is None:
                continue
            event = located[0]
            if (
                event.event_type != source.source_event_type
                or event.payload_hash != source.source_payload_hash
            ):
                continue
            return (
                event,
                CommittedWorldEventRef(
                    event_id=source.source_event_ref,
                    event_type=source.source_event_type,
                    world_revision=source.source_world_revision,
                    payload_hash=source.source_payload_hash,
                    logical_time=source.logical_time,
                ),
            )
        return None

    async def _deliberate_formation(
        self,
        *,
        source_material: ContextualLifeSourceMaterial,
        context: dict[str, object],
    ) -> tuple[_FormationDecision, str]:
        raw = await self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "A source-bound interaction has become available to the character's "
                        "attention. Decide as the character whether it genuinely gives her a "
                        "new personal life inspiration worth carrying forward. This is not a "
                        "request-following rule: curiosity, disagreement, mood, practical limits, "
                        "or no impulse are all legitimate. The source is inspiration, never proof "
                        "that she visited a place or did an activity. Return exactly "
                        '{"decision":"no_op"} or '
                        '{"decision":"form","impulse_text":"...",'
                        '"source_event_refs":["offered ref"],'
                        '"privacy":"private|personal|shareable"}. '
                        "Use only offered source refs and do not invent a completed event."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "contextual_life_inspiration_sources": [
                                source_material.model_dump(mode="json")
                            ],
                            "current_character_context": context,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
        )
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("source_event_refs"), list):
                parsed = {
                    **parsed,
                    "source_event_refs": tuple(parsed["source_event_refs"]),
                }
            decision = _FormationDecision.model_validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LifeAuthorModelFailure(
                "contextual inspiration model returned an invalid decision"
            ) from exc
        if decision.decision == "form" and not set(decision.source_event_refs) <= {
            source_material.event_ref
        }:
            raise LifeAuthorModelFailure("contextual inspiration selected an unoffered source")
        return decision, raw

    async def _deliberate_plan(
        self,
        *,
        aspiration: AspirationProjection,
        candidates: tuple[ReviewedLifeSeedFutureCandidate, ...],
        context: dict[str, object],
    ) -> tuple[_PlanDecision, str]:
        raw = await self._complete(
            [
                {
                    "role": "system",
                    "content": (
                        "Decide as the character whether one source-bound inspiration should now "
                        "become one concrete future plan. Every offered opening and location is "
                        "reviewed and currently feasible; anything not offered is not executable "
                        "yet. You may select any one offered token or no_op. Do not substitute a "
                        "place, claim a visit, or invent an outcome. Return exactly "
                        '{"decision":"no_op"} or '
                        '{"decision":"select","candidate_token":"offered token"}.'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "contextual_life_inspiration": {
                                "aspiration_id": aspiration.aspiration_id,
                                "text": aspiration.text,
                                "source_event_ref": aspiration.source_event_ref,
                                "held_since": aspiration.planted_at.isoformat(),
                            },
                            "reviewed_future_candidates": [
                                {
                                    "token": item.token,
                                    "opening_id": item.opening.id,
                                    "activity_kind": item.opening.activity_kind,
                                    "location_ref": item.location_ref,
                                    "target_local_date": item.target_local_date.isoformat(),
                                    "local_window": item.local_window,
                                    "privacy": item.opening.privacy,
                                }
                                for item in candidates
                            ],
                            "current_character_context": context,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
        )
        try:
            parsed = json.loads(raw)
            decision = _PlanDecision.model_validate(parsed)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LifeAuthorModelFailure(
                "contextual inspiration planning returned an invalid decision"
            ) from exc
        if decision.candidate_token not in {
            None,
            *(item.token for item in candidates),
        }:
            raise LifeAuthorModelFailure(
                "contextual inspiration selected an unoffered future opening"
            )
        return decision, raw

    async def _complete(self, messages: list[dict[str, str]]) -> str:
        try:
            raw = await self._model.complete(messages, temperature=0.3)
        except (TimeoutError, ConnectionError, OSError, httpx.HTTPError) as exc:
            _LOG.warning("contextual life inspiration model unavailable: %s", exc)
            raise LifeAuthorModelFailure("contextual life inspiration model unavailable") from exc
        if not isinstance(raw, str) or len(raw.encode()) > 32_768:
            raise LifeAuthorModelFailure("contextual life inspiration response is not bounded text")
        return raw

    @staticmethod
    def _event_evidence(projection, event_ref: str) -> EvidenceRef:
        source = next(
            item
            for item in projection.pending_contextual_life_sources
            if item.source_event_ref == event_ref
        )
        return EvidenceRef(
            ref_id=source.source_event_ref,
            evidence_type="committed_world_event",
            claim_purpose="conversation_continuity",
            source_world_revision=source.source_world_revision,
            immutable_hash=source.source_payload_hash,
        )

    def _slot(self, candidate: ReviewedLifeSeedFutureCandidate) -> dict[str, object]:
        return {
            "opening_id": candidate.opening.id,
            "activity_kind": candidate.opening.activity_kind,
            "candidate_token": candidate.token,
            "target_local_date": candidate.target_local_date.isoformat(),
            "local_window": candidate.local_window,
            "opens_at": candidate.opens_at.isoformat(),
            "closes_at": candidate.closes_at.isoformat(),
            "location_ref": candidate.location_ref,
            "participant_ref": candidate.participant_ref,
            "availability_hash": candidate.availability_hash,
            "importance_bp": candidate.opening.importance_bp,
            "duration_minutes": candidate.opening.duration_minutes,
            "privacy": candidate.opening.privacy,
            "policy_refs": list(
                candidate.opening.policy_refs(catalog_version=self._catalog.version)
            ),
        }


__all__ = [
    "ContextualLifeInspirationResult",
    "ContextualLifeInspirationRuntime",
]
