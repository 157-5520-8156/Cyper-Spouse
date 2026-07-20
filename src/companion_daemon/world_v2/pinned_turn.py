"""Cursor-pinned Context → Deliberation → Proposal-Audit composition.

This is intentionally the first, non-authorizing WorldRuntime turn vertical.
It turns an already recorded Observation into a trusted Capsule and an audited
model result/proposal at one complete cursor.  Acceptance and Action remain
separate modules; this module never materializes an accepted world effect.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import logging
import time
from typing import Literal

from .advisory_compiler import (
    AdvisoryCompilation,
    AdvisoryCompileRequest,
    AdvisoryCompiler,
    ResolverProof as AdvisoryResolverProof,
    SnapshotMaterial,
    SourceAuthorityBinding,
    canonical_snapshot_hash,
    canonical_trigger_hash,
    source_authority_bindings_hash,
)
from .context_capsule import (
    ContextCapsuleCompiler,
    InnerAdvisoryCandidate,
    InnerAdvisoryProjection,
)
from .context_resolver import query_from_projection
from .deliberation import Deliberation, TriggerMessage
from .errors import ConcurrencyConflict, IdempotencyConflict
from .ledger import LedgerPort
from .model_facing_context import mechanism_consumption_summary
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .production_latency_trace import ProductionLatencyRecorder, TurnLatencyTrace
from .aspiration_view import active_aspiration_advisories
from .attention_view import phone_attention_advisories
from .change_phase_view import change_phase_advisories
from .local_chronology import LocalChronology
from .npc_relationship_view import npc_relationship_advisories
from .shared_private_invitation import pending_shared_private_invitation_advisories
from .response_expectation_view import (
    pending_response_expectation,
    response_expectation_advisory,
)
from .schemas import LedgerProjection, Observation, ProjectionCursor, WorldEvent


_LOG = logging.getLogger(__name__)


def _attempt_id(
    *, trigger_ref: str, cursor: ProjectionCursor, namespace: str | None = None
) -> str:
    material: dict[str, object] = {
        "contract": "pinned-turn.1",
        "trigger_ref": trigger_ref,
        "cursor": cursor.model_dump(mode="json"),
    }
    # Preserve pre-relationship attempt identities exactly.  Only a second
    # consumer of the same accepted appraisal needs a namespace.
    if namespace is not None:
        material["namespace"] = namespace
    material = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"attempt:pinned-turn:{hashlib.sha256(material).hexdigest()}"


class PinnedTurnCompiler:
    """Deep module for one cursor-consistent, audit-only Deliberation attempt."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        capsule_compiler: ContextCapsuleCompiler,
        deliberation: Deliberation,
        companion_actor_ref: str,
        advisory_compiler: AdvisoryCompiler | None = None,
        relationship_evaluation: bool = False,
        latency_recorder: ProductionLatencyRecorder | None = None,
        pending_expectation_advisory: bool = False,
        aspiration_advisory: bool = False,
        change_phase_advisory: bool = False,
        npc_relationship_advisory: bool = False,
        shared_private_invitation_advisory: bool = False,
        attention_advisory: bool = False,
        attention_chronology: LocalChronology | None = None,
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("Pinned turn companion actor is required")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref
        self._advisories = advisory_compiler
        self._relationship_evaluation = relationship_evaluation
        self._latency = latency_recorder
        # Only the interaction-appraisal lane opts in: when she was waiting
        # for a response she invited earlier, the appraisal model should know
        # what she hoped this message would be.
        self._pending_expectation_advisory = pending_expectation_advisory
        # The expression lanes opt in: her active aspirations (ledger-backed
        # low-stakes wishes) may surface naturally in what she says.
        self._aspiration_advisory = aspiration_advisory
        # Change Phase (CONTEXT.md): a projection-level reading of whether
        # she is departing from or returning toward baseline.  Advisory only.
        self._change_phase_advisory = change_phase_advisory
        # Per-NPC relationship reading derived from committed shared history;
        # like the others it is read-only texture, never a rule.
        self._npc_relationship_advisory = npc_relationship_advisory
        # A pending shared_private invitation plan she may still need to
        # voice (or is waiting on); read-only texture, never an obligation.
        self._shared_private_invitation_advisory = shared_private_invitation_advisory
        # Phone attention (attention_view): where her attention actually is
        # relative to the phone, derived from active Plans, the local civil
        # hour, and active Affect.  Advisory texture for timing_choice only;
        # it never schedules, delays, or vetoes anything by itself.
        self._attention_advisory = attention_advisory
        self._attention_chronology = attention_chronology or LocalChronology()

    async def audit_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
        skip_advisories: bool = False,
    ) -> ProposalAuditCommit:
        """Compile an audit at a cursor that includes the committed Observation.

        The audit is a deliberation-only commit.  Any world revision change
        between the read and write makes the attempt stale; callers must build
        a fresh turn rather than reusing its Capsule or proposal.  A background
        appraisal may legitimately run after the observation's trigger process
        was durably opened, so its source event need only be at-or-before the
        pinned cursor; the evidence retains that event's original revision.
        """

        if observation.world_id != self._ledger.world_id or observation_event.world_id != observation.world_id:
            raise ValueError("Pinned turn observation belongs to another world")
        if observation_event.event_type != "ObservationRecorded":
            raise ValueError("Pinned turn requires an ObservationRecorded event")
        stored = await self._lookup_event_commit(observation_event.event_id)
        if (
            stored is None
            or stored[0] != observation_event
            or stored[1].world_revision > cursor.world_revision
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("Pinned turn observation event is not the committed authority")
        try:
            committed_observation = Observation.model_validate_json(stored[0].payload_json)
        except ValueError as exc:
            raise ValueError("Pinned turn event has an invalid observation payload") from exc
        if committed_observation != observation:
            raise ValueError("Pinned turn observation does not match its committed authority")
        observation = committed_observation
        started = time.perf_counter()
        latency_trace = self._latency.get(observation.trace_id) if self._latency is not None else None
        if latency_trace is None:
            projection = await self._project_at(cursor)
        else:
            async with latency_trace.measure("snapshot"):
                projection = await self._project_at(cursor)
        _LOG.warning(
            "pinned turn phases trace=%s phase=snapshot_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=observation_event.event_id,
        )
        trigger_message = self._trigger_message(
            observation,
            observation_event,
            source_world_revision=stored[1].world_revision,
        )
        advisory_already_incorporated = skip_advisories or self._deliberation.main_has_precomputed_advisory(
            trigger_ref=observation_event.event_id,
            observation_ref=observation.observation_id,
            event_payload_hash=trigger_message.event_payload_hash,
        )
        try:
            capsule = await self._compile_capsule_with_advisories(
                query=query,
                projection=projection,
                observation=observation,
                observation_event=observation_event,
                latency_trace=latency_trace,
                skip_advisories=advisory_already_incorporated,
                expectation_advisories=(
                    *self._expectation_advisories(
                        projection,
                        observation_event=observation_event,
                        source_world_revision=stored[1].world_revision,
                    ),
                    *self._aspiration_advisories(projection),
                    *self._change_phase_view_advisories(projection),
                    *self._attention_view_advisories(projection),
                    *self._npc_relationship_view_advisories(projection),
                    *self._shared_private_invitation_view_advisories(projection),
                ),
            )
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        _LOG.warning(
            "pinned turn phases trace=%s phase=context_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        # Keep an operator-readable answer to the most important production
        # question: did the character mechanisms reach this turn at all?  The
        # summary contains only counts/statuses and never logs model-facing
        # prose or private memory values.
        _LOG.info(
            "pinned turn mechanism consumption trace=%s summary=%s",
            observation.trace_id,
            json.dumps(
                mechanism_consumption_summary(capsule.capsule.model_content_json),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        deliberate_kwargs = dict(
            attempt_id=_attempt_id(trigger_ref=observation_event.event_id, cursor=cursor),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=observation.observation_id,
                    evidence_kind="observed_message",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + observation_event.payload_hash,
                ),
            ),
            trigger_message=trigger_message,
        )
        if latency_trace is None:
            result = await self._deliberation.deliberate(capsule, **deliberate_kwargs)
        else:
            async with latency_trace.measure("model_completion"):
                result = await self._deliberation.deliberate(capsule, **deliberate_kwargs)
        _LOG.warning(
            "pinned turn phases trace=%s phase=model_ms value=%.1f",
            observation.trace_id,
            (time.perf_counter() - started) * 1000,
        )
        context = ProposalAuditContext(
            world_id=observation.world_id,
            trigger_ref=observation_event.event_id,
            logical_time=projection.logical_time or observation.logical_time,
            created_at=observation.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-turn",
            trace_id=observation.trace_id,
            causation_id=observation_event.event_id,
            correlation_id=observation.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            recorded = await self._record(result, context)
            _LOG.warning(
                "pinned turn phases trace=%s phase=record_ms value=%.1f",
                observation.trace_id,
                (time.perf_counter() - started) * 1000,
            )
            return recorded
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def audit_appraisal_accepted(
        self,
        *,
        appraisal_event: WorldEvent,
        cursor: ProjectionCursor,
        attempt_namespace: str | None = None,
    ) -> ProposalAuditCommit:
        """Audit one fresh affect deliberation after an accepted Appraisal.

        This deliberately has no classifier side path: Appraisal is already a
        source-bound interpretation.  The subsequent model is asked only
        whether that fresh state warrants an Affect proposal; it cannot reuse
        the stale user-message turn or fabricate a new appraisal source.
        """

        if attempt_namespace is not None and (not attempt_namespace or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
            for character in attempt_namespace
        )):
            raise ValueError("Pinned turn appraisal attempt namespace is invalid")
        if appraisal_event.world_id != self._ledger.world_id:
            raise ValueError("Pinned turn appraisal belongs to another world")
        if appraisal_event.event_type != "AppraisalAccepted":
            raise ValueError("Pinned turn affect trigger requires AppraisalAccepted")
        stored = await self._lookup_event_commit(appraisal_event.event_id)
        # The source Appraisal is immutable evidence, rather than the state
        # proposed by this turn. Opening/claiming its durable affect trigger
        # can legitimately advance the ledger before this worker runs, so the
        # source need only be present in the pinned projection. Ledger
        # sequence is the total order enforced by ``project_at``.
        if (
            stored is None
            or stored[0] != appraisal_event
            or stored[1].ledger_sequence > cursor.ledger_sequence
        ):
            raise ValueError("Pinned turn appraisal event is not the committed authority")
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=appraisal_event.event_id,
        )
        try:
            capsule = await self._compile_capsule(query)
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        result = await self._deliberation.deliberate(
            capsule,
            # Affect and relationship both deliberate after the same immutable
            # appraisal.  Their expensive calls must have distinct durable
            # attempt identities; otherwise the second lane aliases the first
            # lane's ModelResultRecorded audit on recovery.
            attempt_id=_attempt_id(
                trigger_ref=appraisal_event.event_id,
                cursor=cursor,
                namespace=attempt_namespace,
            ),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=appraisal_event.event_id,
                    evidence_kind="committed_world_event",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + appraisal_event.payload_hash,
                ),
            ),
        )
        context = ProposalAuditContext(
            world_id=appraisal_event.world_id,
            trigger_ref=appraisal_event.event_id,
            logical_time=projection.logical_time or appraisal_event.logical_time,
            created_at=appraisal_event.created_at,
            actor=self._companion_actor_ref,
            source="world-runtime:pinned-affect-turn",
            trace_id=appraisal_event.trace_id,
            causation_id=appraisal_event.event_id,
            correlation_id=appraisal_event.correlation_id,
            evaluated_world_revision=cursor.world_revision,
            expected_commit_world_revision=cursor.world_revision,
            expected_deliberation_revision=cursor.deliberation_revision,
        )
        try:
            return await self._record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def _raise_if_stale(self, cursor: ProjectionCursor, cause: Exception) -> None:
        current = await self._project()
        if (
            current.world_revision != cursor.world_revision
            or current.deliberation_revision != cursor.deliberation_revision
            or current.ledger_sequence != cursor.ledger_sequence
        ):
            raise ConcurrencyConflict("Pinned turn cursor became stale") from cause

    async def _project(self):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    async def _lookup_event_commit(self, event_id: str):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.lookup_event_commit, event_id)
        return self._ledger.lookup_event_commit(event_id)

    async def _project_at(self, cursor: ProjectionCursor):
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    async def _compile_capsule(self, query):
        if self._relationship_evaluation:
            compile_relationship = getattr(
                self._capsules, "compile_for_relationship_deliberation", None
            )
            if not callable(compile_relationship):
                raise ValueError("Context Capsule compiler lacks relationship deliberation support")
            return await asyncio.to_thread(compile_relationship, query)
        return await asyncio.to_thread(self._capsules.compile_for_deliberation, query)

    def _expectation_advisories(
        self,
        projection: LedgerProjection,
        *,
        observation_event: WorldEvent,
        source_world_revision: int,
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic pending-expectation advisory, if opted in.

        The revision bound keeps causality honest: an inbound message may
        only be weighed against a hope she declared before it arrived.
        """

        if not self._pending_expectation_advisory:
            return ()
        try:
            view = pending_response_expectation(
                projection, before_world_revision=source_world_revision
            )
        except (TypeError, ValueError):
            # Expectation advice is best-effort context.  A projection defect
            # must not make a normal user turn fail.
            return ()
        if view is None:
            return ()
        return (
            response_expectation_advisory(
                view,
                source_ref=observation_event.event_id,
                logical_time=projection.logical_time or observation_event.logical_time,
            ),
        )

    def _aspiration_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic active-wish advisory, if opted in.

        Best-effort context like the expectation advisory: a defect here must
        never make an ordinary turn fail, it only omits the wish texture.
        """

        if not self._aspiration_advisory:
            return ()
        try:
            return active_aspiration_advisories(projection)
        except (TypeError, ValueError):
            return ()

    def _change_phase_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic Change Phase advisory, if opted in.

        Best-effort context like the wish advisory: expression should feel
        the difference between "刚陷入低落" and "正在走出低落", but a defect
        here must never fail an ordinary turn.
        """

        if not self._change_phase_advisory:
            return ()
        try:
            return change_phase_advisories(projection)
        except (TypeError, ValueError):
            return ()

    def _attention_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic phone-attention advisory, if opted in.

        Best-effort context like the Change Phase reading: "深夜她睡着了" and
        "在自习室专注中" should reach the timing decision, but a defect here
        must never fail an ordinary turn.
        """

        if not self._attention_advisory:
            return ()
        try:
            return phone_attention_advisories(
                projection, chronology=self._attention_chronology
            )
        except (TypeError, ValueError):
            return ()

    def _npc_relationship_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the deterministic per-NPC relationship advisory, if opted in."""

        if not self._npc_relationship_advisory:
            return ()
        try:
            return npc_relationship_advisories(projection)
        except (TypeError, ValueError):
            return ()

    def _shared_private_invitation_view_advisories(
        self, projection: LedgerProjection
    ) -> tuple[InnerAdvisoryProjection, ...]:
        """Derive the pending shared_private invitation advisory, if opted in."""

        if not self._shared_private_invitation_advisory:
            return ()
        try:
            return pending_shared_private_invitation_advisories(projection)
        except (TypeError, ValueError):
            return ()

    async def _compile_capsule_with_extra(
        self, query, extra: tuple[InnerAdvisoryProjection, ...]
    ):
        if not extra:
            return await self._compile_capsule(query)
        return await asyncio.to_thread(
            self._capsules.compile_for_deliberation_with_advisories, query, extra
        )

    async def _compile_capsule_with_advisories(
        self,
        *,
        query,
        projection: LedgerProjection,
        observation: Observation,
        observation_event: WorldEvent,
        latency_trace: TurnLatencyTrace | None = None,
        skip_advisories: bool = False,
        expectation_advisories: tuple[InnerAdvisoryProjection, ...] = (),
    ):
        if self._advisories is None or skip_advisories:
            if latency_trace is None:
                return await self._compile_capsule_with_extra(query, expectation_advisories)
            async with latency_trace.measure("context"):
                return await self._compile_capsule_with_extra(query, expectation_advisories)
        try:
            request = self._advisory_request(
                query=query,
                observation=observation,
                observation_event=observation_event,
                projection=projection,
            )
        except (TypeError, ValueError):
            # Advisory input is deliberately best-effort.  A bounded-input
            # failure cannot make a normal user turn fail.
            if latency_trace is None:
                return await self._compile_capsule_with_extra(query, expectation_advisories)
            async with latency_trace.measure("context"):
                return await self._compile_capsule_with_extra(query, expectation_advisories)
        # Classifiers are a latency-bounded optional side path.  The Context
        # compiler and AdvisoryCompiler consume the same pinned cursor but do
        # not depend on each other's output, so run them concurrently.
        async def prepare():
            if latency_trace is None:
                return await asyncio.to_thread(
                    self._capsules.prepare_for_deliberation,
                    query,
                    relationship_evaluation=self._relationship_evaluation,
                )
            async with latency_trace.measure("context"):
                return await asyncio.to_thread(
                    self._capsules.prepare_for_deliberation,
                    query,
                    relationship_evaluation=self._relationship_evaluation,
                )

        async def classify():
            operation = self._advisories.compile(
                self._advisories.issue_authenticated_request(request)
            )
            if latency_trace is None:
                return await operation
            async with latency_trace.measure("advisor"):
                return await operation

        base_task = asyncio.create_task(prepare())
        advisory_task = asyncio.create_task(classify())
        try:
            prepared, compilation = await asyncio.gather(base_task, advisory_task)
        except (TypeError, ValueError):
            prepared = await base_task
            if latency_trace is None:
                return await self._finalize_prepared_with_extra(
                    prepared, expectation_advisories, query=query
                )
            async with latency_trace.measure("context"):
                return await self._finalize_prepared_with_extra(
                    prepared, expectation_advisories, query=query
                )
        inner = (*self._inner_advisories(compilation), *expectation_advisories)
        if not inner:
            if latency_trace is None:
                return self._capsules.finalize_prepared(prepared)
            async with latency_trace.measure("context"):
                return self._capsules.finalize_prepared(prepared)
        try:
            operation = asyncio.to_thread(
                self._capsules.compile_prepared_with_advisories, prepared, inner
            )
            if latency_trace is None:
                return await operation
            async with latency_trace.measure("context"):
                return await operation
        except (TypeError, ValueError) as exc:
            # The re-binding pass is defense in depth.  If it rejects a bad
            # advisory (rather than a stale cursor), retain the already frozen
            # authoritative capsule and let the main model continue.
            await self._raise_if_stale(query.cursor, exc)
            return self._capsules.finalize_prepared(prepared)

    async def _finalize_prepared_with_extra(
        self, prepared, extra: tuple[InnerAdvisoryProjection, ...], *, query
    ):
        if not extra:
            return self._capsules.finalize_prepared(prepared)
        try:
            return await asyncio.to_thread(
                self._capsules.compile_prepared_with_advisories, prepared, extra
            )
        except (TypeError, ValueError) as exc:
            # Same defense-in-depth stance as the classifier merge below: a
            # rejected advisory keeps the frozen authoritative capsule.
            await self._raise_if_stale(query.cursor, exc)
            return self._capsules.finalize_prepared(prepared)

    @staticmethod
    def _advisory_request(
        *,
        query,
        projection: LedgerProjection,
        observation: Observation,
        observation_event: WorldEvent,
    ) -> AdvisoryCompileRequest:
        """Build the classifier input from only the current cursor's authority."""

        logical_time = query.logical_time or observation.logical_time
        trigger = {
            "kind": observation.observation_kind,
            "observation_id": observation.observation_id,
            "actor": observation.actor,
            "channel": observation.channel,
            "payload_ref": observation.payload_ref,
            "payload_hash": observation.payload_hash,
            "text": observation.text,
            "reply_context": observation.reply_context,
            "attachment_refs": observation.attachment_refs,
        }
        source_authorities = (
            SourceAuthorityBinding(
                ref=observation_event.event_id,
                world_revision=query.world_revision,
                hash_kind="payload",
                authority_hash=observation_event.payload_hash,
                content_hash=canonical_trigger_hash(trigger),
            ),
        )
        snapshot_values = PinnedTurnCompiler._advisory_snapshot(projection)
        snapshot = SnapshotMaterial(
            world_revision=query.world_revision,
            values=snapshot_values,
            canonical_hash=canonical_snapshot_hash(snapshot_values),
        )
        return AdvisoryCompileRequest(
            world_id=query.world_id,
            snapshot_id=f"advisory-input:{query.snapshot_id}",
            snapshot_hash=snapshot.canonical_hash,
            world_revision=query.world_revision,
            logical_time=logical_time,
            trigger_ref=observation_event.event_id,
            expires_at=logical_time + timedelta(seconds=45),
            source_authorities=source_authorities,
            resolver_proof=AdvisoryResolverProof(
                snapshot_id=f"advisory-input:{query.snapshot_id}",
                snapshot_hash=snapshot.canonical_hash,
                world_revision=query.world_revision,
                completeness="full",
                policy_version="pinned-turn-advisory-input.1",
                source_bindings_hash=source_authority_bindings_hash(source_authorities),
                authentication_tag="0" * 64,
            ),
            trigger=trigger,
            recent_context=PinnedTurnCompiler._recent_context(observation),
            snapshot=snapshot,
        )

    @staticmethod
    def _advisory_snapshot(projection: LedgerProjection) -> dict[str, object]:
        """Small deterministic read model for advisory classifiers only.

        It deliberately exposes no ledger or mutation port.  ContextCapsule
        remains the richer authority-backed model input; this compact view lets
        advice run in parallel rather than adding another serial model wait.
        """

        def values(items, *, limit: int, active=None):
            selected = [item for item in items if active is None or active(item)]
            selected.sort(key=lambda item: str(getattr(item, "entity_revision", 0)))
            return tuple(item.model_dump(mode="json") for item in selected[-limit:])

        return {
            "cursor": {
                "world_revision": projection.world_revision,
                "deliberation_revision": projection.deliberation_revision,
                "ledger_sequence": projection.ledger_sequence,
            },
            "logical_time": projection.logical_time.isoformat()
            if projection.logical_time is not None
            else None,
            "character_core": (
                projection.character_core.model_dump(mode="json")
                if projection.character_core is not None
                else None
            ),
            "active_affect_episodes": values(
                projection.affect_episodes, limit=8, active=lambda item: item.status == "active"
            ),
            "relationship_states": values(projection.relationship_states, limit=8),
            "open_threads": values(
                projection.threads, limit=8, active=lambda item: item.values.status == "open"
            ),
            "recent_message_observations": values(projection.message_observations, limit=8),
        }

    @staticmethod
    def _recent_context(observation: Observation) -> tuple[dict[str, object], ...]:
        reply_context = observation.reply_context
        if not isinstance(reply_context, dict):
            return ()
        recent = reply_context.get("recent_messages")
        if isinstance(recent, list) and all(isinstance(item, dict) for item in recent):
            return tuple(recent)
        return (reply_context,)

    @staticmethod
    def _reply_target(observation: Observation) -> str:
        """Read the platform target from the immutable observation, never a model choice."""

        context = observation.reply_context
        target = context.get("target") if isinstance(context, dict) else None
        return target if isinstance(target, str) and target else observation.actor

    @classmethod
    def _trigger_message(
        cls,
        observation: Observation,
        observation_event: WorldEvent,
        *,
        source_world_revision: int,
    ) -> TriggerMessage | None:
        """Expose text and bounded attachment tokens, never fabricated contents."""

        if observation.text is None and not observation.attachment_refs:
            return None
        reply_context = observation.reply_context or {}
        platform_message_id = reply_context.get("platform_message_id")
        return TriggerMessage(
            event_ref=observation_event.event_id,
            event_payload_hash=f"sha256:{observation_event.payload_hash}",
            observation_ref=observation.observation_id,
            source_world_revision=source_world_revision,
            actor=observation.actor,
            channel=observation.channel,
            reply_target=cls._reply_target(observation),
            platform_message_id=(
                platform_message_id
                if isinstance(platform_message_id, str) and platform_message_id
                else None
            ),
            text=observation.text,
            attachment_refs=observation.attachment_refs,
            attachment_media_types=tuple(
                cls._attachment_media_type(item) for item in observation.attachment_refs
            ),
        )

    @staticmethod
    def _attachment_media_type(
        attachment_ref: str,
    ) -> Literal["image", "audio", "video", "file", "unknown"]:
        """Read only the provider-normalized type prefix; never dereference content."""

        tokens = tuple(token.lower() for token in attachment_ref.split(":"))
        if "image" in tokens:
            return "image"
        if "record" in tokens or "audio" in tokens:
            return "audio"
        if "video" in tokens:
            return "video"
        if "file" in tokens:
            return "file"
        return "unknown"

    @staticmethod
    def _inner_advisories(compilation: AdvisoryCompilation) -> tuple[InnerAdvisoryProjection, ...]:
        """Reduce classifier distributions to model-readable, non-authoritative hints."""

        return tuple(
            InnerAdvisoryProjection(
                advisory_id=item.advisory_id,
                kind=item.field_id,
                source_refs=item.source_refs,
                candidate_refs=tuple(
                    f"{item.advisory_id}:candidate:{index}"
                    for index, _ in enumerate(item.candidates, start=1)
                ),
                candidates=tuple(
                    InnerAdvisoryCandidate(
                        candidate_ref=f"{item.advisory_id}:candidate:{index}",
                        value=candidate.value,
                        weight_bp=candidate.weight,
                        confidence_bp=candidate.confidence,
                    )
                    for index, candidate in enumerate(item.candidates, start=1)
                ),
                confidence_bp=max(candidate.confidence for candidate in item.candidates),
                expiry=item.expires_at,
                producer_version=f"{item.producer}:{item.catalog_version}",
            )
            for item in compilation.advisories
        )

    async def _record(
        self, result, context: ProposalAuditContext
    ) -> ProposalAuditCommit:
        if self._ledger.blocks_event_loop:
            return await asyncio.to_thread(self._recorder.record, result, context)
        return self._recorder.record(result, context)


__all__ = ["PinnedTurnCompiler"]
