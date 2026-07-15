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
from .proposal_audit import ProposalAuditCommit, ProposalAuditContext, ProposalAuditRecorder
from .proposal_envelope import ProposalEvidenceRef
from .schemas import LedgerProjection, Observation, ProjectionCursor, WorldEvent


def _attempt_id(*, trigger_ref: str, cursor: ProjectionCursor) -> str:
    material = json.dumps(
        {
            "contract": "pinned-turn.1",
            "trigger_ref": trigger_ref,
            "cursor": cursor.model_dump(mode="json"),
        },
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
    ) -> None:
        if not companion_actor_ref:
            raise ValueError("Pinned turn companion actor is required")
        self._ledger = ledger
        self._capsules = capsule_compiler
        self._deliberation = deliberation
        self._recorder = ProposalAuditRecorder(ledger=ledger)
        self._companion_actor_ref = companion_actor_ref
        self._advisories = advisory_compiler

    async def audit_observation(
        self,
        *,
        observation: Observation,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ProposalAuditCommit:
        """Compile and audit only if the Observation's exact cursor is current.

        The audit is a deliberation-only commit.  Any world revision change
        between the read and write makes the attempt stale; callers must build
        a fresh turn rather than reusing its Capsule or proposal.
        """

        if observation.world_id != self._ledger.world_id or observation_event.world_id != observation.world_id:
            raise ValueError("Pinned turn observation belongs to another world")
        if observation_event.event_type != "ObservationRecorded":
            raise ValueError("Pinned turn requires an ObservationRecorded event")
        stored = await self._lookup_event_commit(observation_event.event_id)
        if (
            stored is None
            or stored[0] != observation_event
            or stored[1].world_revision != cursor.world_revision
            or stored[1].ledger_sequence != cursor.ledger_sequence
        ):
            raise ValueError("Pinned turn observation event is not the committed authority")
        try:
            committed_observation = Observation.model_validate_json(stored[0].payload_json)
        except ValueError as exc:
            raise ValueError("Pinned turn event has an invalid observation payload") from exc
        if committed_observation != observation:
            raise ValueError("Pinned turn observation does not match its committed authority")
        observation = committed_observation
        projection = await self._project_at(cursor)
        query = query_from_projection(
            projection,
            actor_ref=self._companion_actor_ref,
            trigger_ref=observation_event.event_id,
        )
        try:
            capsule = await self._compile_capsule_with_advisories(
                query=query,
                projection=projection,
                observation=observation,
                observation_event=observation_event,
            )
        except ValueError as exc:
            await self._raise_if_stale(cursor, exc)
            raise
        result = await self._deliberation.deliberate(
            capsule,
            attempt_id=_attempt_id(trigger_ref=observation_event.event_id, cursor=cursor),
            trigger_evidence=(
                ProposalEvidenceRef(
                    ref_id=observation.observation_id,
                    evidence_kind="observed_message",
                    source_world_revision=stored[1].world_revision,
                    immutable_hash="sha256:" + observation_event.payload_hash,
                ),
            ),
            trigger_message=self._trigger_message(observation, observation_event),
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
            return await self._record(result, context)
        except (ConcurrencyConflict, IdempotencyConflict) as exc:
            await self._raise_if_stale(cursor, exc)
            raise

    async def audit_appraisal_accepted(
        self,
        *,
        appraisal_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ProposalAuditCommit:
        """Audit one fresh affect deliberation after an accepted Appraisal.

        This deliberately has no classifier side path: Appraisal is already a
        source-bound interpretation.  The subsequent model is asked only
        whether that fresh state warrants an Affect proposal; it cannot reuse
        the stale user-message turn or fabricate a new appraisal source.
        """

        if appraisal_event.world_id != self._ledger.world_id:
            raise ValueError("Pinned turn appraisal belongs to another world")
        if appraisal_event.event_type != "AppraisalAccepted":
            raise ValueError("Pinned turn affect trigger requires AppraisalAccepted")
        stored = await self._lookup_event_commit(appraisal_event.event_id)
        if (
            stored is None
            or stored[0] != appraisal_event
            or stored[1].world_revision != cursor.world_revision
            or stored[1].ledger_sequence != cursor.ledger_sequence
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
            attempt_id=_attempt_id(trigger_ref=appraisal_event.event_id, cursor=cursor),
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
        return await asyncio.to_thread(self._capsules.compile_for_deliberation, query)

    async def _compile_capsule_with_advisories(
        self,
        *,
        query,
        projection: LedgerProjection,
        observation: Observation,
        observation_event: WorldEvent,
    ):
        if self._advisories is None:
            return await self._compile_capsule(query)
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
            return await self._compile_capsule(query)
        # Classifiers are a latency-bounded optional side path.  The Context
        # compiler and AdvisoryCompiler consume the same pinned cursor but do
        # not depend on each other's output, so run them concurrently.
        base_task = asyncio.create_task(self._compile_capsule(query))
        advisory_task = asyncio.create_task(
            self._advisories.compile(self._advisories.issue_authenticated_request(request))
        )
        try:
            base, compilation = await asyncio.gather(base_task, advisory_task)
        except (TypeError, ValueError):
            return await base_task
        inner = self._inner_advisories(compilation)
        if not inner:
            return base
        try:
            return await asyncio.to_thread(
                self._capsules.compile_for_deliberation_with_advisories,
                query,
                inner,
            )
        except (TypeError, ValueError) as exc:
            # The re-binding pass is defense in depth.  If it rejects a bad
            # advisory (rather than a stale cursor), retain the already frozen
            # authoritative capsule and let the main model continue.
            await self._raise_if_stale(query.cursor, exc)
            return base

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
        cls, observation: Observation, observation_event: WorldEvent
    ) -> TriggerMessage | None:
        """Expose actual inbound text, never a fabricated attachment description."""

        if observation.text is None:
            return None
        return TriggerMessage(
            event_ref=observation_event.event_id,
            event_payload_hash=f"sha256:{observation_event.payload_hash}",
            observation_ref=observation.observation_id,
            actor=observation.actor,
            channel=observation.channel,
            reply_target=cls._reply_target(observation),
            text=observation.text,
        )

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
