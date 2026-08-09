"""Private production composition for :class:`CharacterInterior`.

Only this package may know the historical provider Adapter shapes while they
are used as Faculty implementations.  Hosts, Runtime and the application
builder receive one ``CharacterInterior`` and cannot construct or call those
Adapters independently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import json

from ..accepted_ledger_batch import AcceptedLedgerBatchIssuer
from ..immediate_emotion_proposal_worker import ImmediateEmotionProposalWorker
from ..relationship_acceptance_runtime import RelationshipAcceptanceRuntime
from ..relationship_proposal_compiler import RelationshipProposalCompiler
from ..aspiration_events import AspirationPlantedPayload, AspirationRevisedPayload
from ..companion_identity import CompanionIdentityFrame
from ..model_completion import ChatCompletionModel
from ..source_closure_lane import SourceClosureReselectionLane
from ..context_capsule import ContextCapsuleCompiler
from ..context_resolver import query_from_projection
from ..deliberation import ModelRouterAdapter
from ..errors import ConcurrencyConflict, IdempotencyConflict
from ..expression_draft import (
    ExpressionDraftCapabilities,
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from ..expression_plan_acceptance import ExpressionPlanBudgetPolicy
from ..expression_reconsideration_runtime import ExpressionReconsiderationRuntime
from ..interactive_turn_budget import InteractiveTurnBudgetPolicy
from ..ledger import LedgerPort
from ..private_impression_producer import (
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
    _PrivateImpressionInteriorAuthorityHandler,
)
from ..plan_disruption_appraisal_trigger import PlanDisruptionAppraisalTriggerOpener
from ..perception_result_context import PerceptionResultReader
from ..proactive_action import (
    ProactiveActionRuntime,
    ProactiveDeliberationTurn,
)
from ..recall_runtime import (
    PresentedPrefetchTrace,
    RecallCoordinator,
    TrustedRecallTrace,
)
from ..recall_audit import CharacterRecallRequest
from ..recall_index import RecallCursor
from ..recall_runtime import verify_trusted_recall_trace
from .inbound_author import _InboundCharacterAuthor
from ..social_initiative import SocialInitiativeCompiler, SocialInitiativePolicy
from ..silence_appraisal_trigger import SilenceAppraisalTriggerOpener
from ..schemas import LedgerProjection
from .authority import _DeferredInteriorAuthority
from .core import CharacterInterior
from .contracts import (
    InnerLifeSnapshot,
    InteriorOpportunity,
    InteriorStimulus,
    _InteriorContextView,
    _InteriorFacet,
    _InteriorSourceInventoryItem,
)
from .inbound_turn import InboundTurnFaculty
from .expression_reconsideration import (
    CharacterInteriorExpressionReconsiderationReviewer,
)
from .relationship_context import (
    build_relationship_context_join,
    install_relationship_context,
)
from .snapshot_compiler import (
    compile_inner_life_snapshot,
    source_envelopes_from_capsule,
)
from .structured_role import StructuredCharacterRoleFaculty
from .turn_store import _CharacterInteriorTurnStore
from .world_stimulus import (
    CharacterInteriorWorldStimulusRuntime,
    _WorldStimulusInteriorAuthorityHandler,
    _WorldStimulusRelationshipSignalSettlement,
)


class _DeferredProjection:
    """One-shot late binding from provider composition to the ledger Capsule."""

    def __init__(self) -> None:
        self._delegate: object | None = None

    def bind(self, delegate: object) -> None:
        if self._delegate is not None:
            raise RuntimeError("CharacterInterior projection is already bound")
        if not callable(getattr(delegate, "project", None)):
            raise TypeError("CharacterInterior projection delegate must provide project")
        self._delegate = delegate

    @property
    def is_bound(self) -> bool:
        return self._delegate is not None

    async def project(
        self,
        *,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        if self._delegate is None:
            raise RuntimeError("CharacterInterior production projection is not bound")
        return await self._delegate.project(subject=subject)


@dataclass(frozen=True, slots=True)
class _LedgerCapsuleInteriorProjection:
    ledger: LedgerPort
    capsules: ContextCapsuleCompiler
    companion_actor_ref: str

    async def project(
        self,
        *,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        if subject.world_id != self.ledger.world_id:
            raise ValueError("character interior subject belongs to another world")
        if subject.actor_ref != self.companion_actor_ref:
            raise ValueError("character interior subject belongs to another actor")
        projection = (
            await asyncio.to_thread(self.ledger.project_at, subject.cursor)
            if self.ledger.blocks_event_loop
            else self.ledger.project_at(subject.cursor)
        )
        query = query_from_projection(
            projection,
            actor_ref=subject.actor_ref,
            trigger_ref=subject.trigger_ref,
        )
        capsule = (
            await asyncio.to_thread(self.capsules.compile, query)
            if self.ledger.blocks_event_loop
            else self.capsules.compile(query)
        )
        context = json.loads(capsule.model_content_json)
        if not isinstance(context, dict):
            raise ValueError("character interior Capsule is not an object")
        relationship_join = await build_relationship_context_join(
            ledger=self.ledger,
            projection=projection,
            actor_ref=subject.actor_ref,
            cursor=subject.cursor,
        )
        context = install_relationship_context(context, relationship_join)
        source_envelopes = source_envelopes_from_capsule(capsule)
        for item_ref, envelope in relationship_join.source_envelopes.items():
            existing = source_envelopes.get(item_ref)
            if existing is not None and existing != envelope:
                raise ValueError("relationship context reused a Capsule source ref")
            source_envelopes[item_ref] = dict(envelope)
        snapshot = compile_inner_life_snapshot(
            context,
            source_envelopes=source_envelopes,
        )
        if snapshot.logical_time != subject.logical_time:
            raise ValueError("character interior Capsule logical time is stale")
        snapshot = await self._bind_projection_aspirations(
            snapshot=snapshot,
            projection=projection,
            subject=subject,
        )
        return await self._bind_capability_evidence(
            snapshot=snapshot,
            subject=subject,
        )

    async def _bind_projection_aspirations(
        self,
        *,
        snapshot: InnerLifeSnapshot,
        projection: LedgerProjection,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        """Join active wishes through their latest meaning-bearing authority.

        Reinforcement advances the lifecycle revision without rewriting the
        character's direction.  The source for model-visible meaning is
        therefore the latest explicit revision, or the planting event when no
        revision exists; counters remain reducer-derived state.
        """

        privacy_rank = {
            "public": 0,
            "shareable": 1,
            "personal": 2,
            "private": 3,
            "withhold": 4,
        }
        aspirations = tuple(
            sorted(
                (
                    item
                    for item in projection.aspirations
                    if item.owner_actor_ref == subject.actor_ref
                    and item.status == "active"
                    and privacy_rank[item.privacy_class] <= privacy_rank[subject.privacy_ceiling]
                ),
                key=lambda item: (
                    item.last_revised_at or item.last_reinforced_at or item.planted_at,
                    item.aspiration_id,
                ),
                reverse=True,
            )[:8]
        )
        if not aspirations:
            return snapshot
        lookup = getattr(self.ledger, "lookup_event_commit", None)
        if not callable(lookup):
            raise ValueError("aspiration authority lookup is unavailable")
        committed = {item.event_id: item for item in projection.committed_world_event_refs}
        entries: list[dict[str, object]] = []
        inventory = list(snapshot.source_inventory)
        for aspiration in aspirations:
            source_ref = aspiration.revision_event_ref or aspiration.planted_event_ref
            located = (
                await asyncio.to_thread(lookup, source_ref)
                if self.ledger.blocks_event_loop
                else lookup(source_ref)
            )
            recorded = committed.get(source_ref)
            if located is None or recorded is None:
                raise ValueError("active aspiration authority is not committed")
            event, commit = located
            if (
                event.world_id != subject.world_id
                or event.event_id != source_ref
                or event.event_type not in {"AspirationPlanted", "AspirationRevised"}
                or event.event_id not in commit.event_ids
                or recorded.event_type != event.event_type
                or recorded.payload_hash != event.payload_hash
                or recorded.world_revision != commit.world_revision
                or commit.world_revision > subject.cursor.world_revision
                or commit.deliberation_revision > subject.cursor.deliberation_revision
                or commit.ledger_sequence > subject.cursor.ledger_sequence
            ):
                raise ValueError("active aspiration authority mismatches pinned prefix")
            if event.event_type == "AspirationRevised":
                revised = AspirationRevisedPayload.model_validate_json(
                    event.payload_json
                ).aspiration_after
                semantic_fields = (
                    "aspiration_id",
                    "owner_actor_ref",
                    "seed_id",
                    "origin_kind",
                    "text",
                    "privacy_class",
                    "status",
                    "planted_at",
                    "planted_event_ref",
                    "source_event_ref",
                    "tension_summary",
                    "tension_source_refs",
                    "last_revised_at",
                    "revision_event_ref",
                )
                if (
                    any(
                        getattr(revised, name) != getattr(aspiration, name)
                        for name in semantic_fields
                    )
                    or revised.revision_event_ref != source_ref
                    or aspiration.entity_revision < revised.entity_revision
                    or aspiration.reinforcement_count < revised.reinforcement_count
                ):
                    raise ValueError("active aspiration projection changed revised meaning")
            else:
                planted = AspirationPlantedPayload.model_validate_json(
                    event.payload_json
                ).aspiration
                semantic_fields = (
                    "aspiration_id",
                    "owner_actor_ref",
                    "seed_id",
                    "origin_kind",
                    "text",
                    "privacy_class",
                    "status",
                    "planted_at",
                    "planted_event_ref",
                    "source_event_ref",
                    "tension_summary",
                    "tension_source_refs",
                )
                if any(
                    getattr(planted, name) != getattr(aspiration, name) for name in semantic_fields
                ):
                    raise ValueError("active aspiration projection changed planted meaning")
            entry: dict[str, object] = {
                "aspiration_id": aspiration.aspiration_id,
                "entity_revision": aspiration.entity_revision,
                "origin_kind": aspiration.origin_kind,
                "text": aspiration.text,
                "status": aspiration.status,
                "planted_at": aspiration.planted_at.isoformat(),
                "source_event_ref": aspiration.source_event_ref,
                "planted_event_ref": aspiration.planted_event_ref,
                "privacy_class": aspiration.privacy_class,
                "reinforcement_count": aspiration.reinforcement_count,
                "source_ref": source_ref,
            }
            if aspiration.last_reinforced_at is not None:
                entry["last_reinforced_at"] = aspiration.last_reinforced_at.isoformat()
            if aspiration.last_revised_at is not None:
                entry["last_revised_at"] = aspiration.last_revised_at.isoformat()
            if aspiration.tension_summary is not None:
                entry["tension_summary"] = aspiration.tension_summary
                entry["tension_source_refs"] = list(aspiration.tension_source_refs)
            entries.append(entry)
            inventory.append(
                _InteriorSourceInventoryItem(
                    source_ref=source_ref,
                    scope="aspirations",
                    content_hash=hashlib.sha256(
                        json.dumps(
                            entry,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    privacy_class=aspiration.privacy_class,
                )
            )

        aspiration_refs = tuple(str(item["source_ref"]) for item in entries)
        source_refs = tuple(dict.fromkeys((*snapshot.source_refs, *aspiration_refs)))
        materials = dict(snapshot.materials)
        materials["aspirations"] = entries
        continuity_content = dict(snapshot.continuity.content)
        continuity_content["aspirations"] = entries
        continuity = _InteriorContextView.from_material(
            availability="available",
            content=continuity_content,
            source_refs=tuple(dict.fromkeys((*snapshot.continuity.source_refs, *aspiration_refs))),
        )
        facets: list[_InteriorFacet] = []
        for facet in snapshot.facet_views:
            if facet.name not in {"aspirations_conflicts", "autonomous_impulses"}:
                facets.append(facet)
                continue
            raw_keys = facet.content.get("material_keys")
            material_keys = (
                [item for item in raw_keys if isinstance(item, str)]
                if isinstance(raw_keys, list)
                else []
            )
            if "aspirations" not in material_keys:
                material_keys.append("aspirations")
            view = _InteriorContextView.from_material(
                availability="available",
                content={"material_keys": material_keys},
                source_refs=tuple(dict.fromkeys((*facet.source_refs, *aspiration_refs))),
            )
            facets.append(_InteriorFacet(name=facet.name, **view.model_dump(mode="python")))
        return InnerLifeSnapshot.create(
            availability="available",
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            logical_time=snapshot.logical_time,
            situation=snapshot.situation,
            continuity=continuity,
            facet_views=tuple(facets),
            materials=materials,
            source_refs=source_refs,
            source_inventory=tuple(inventory),
            viewer_scope=snapshot.viewer_scope,
            privacy_scope=snapshot.privacy_scope,
            capability_scope=snapshot.capability_scope,
            context_compiler=snapshot.context_compiler,
            snapshot_compiler=snapshot.snapshot_compiler,
            truncation=snapshot.truncation,
        )

    async def _bind_capability_evidence(
        self,
        *,
        snapshot: InnerLifeSnapshot,
        subject: InteriorStimulus | InteriorOpportunity,
    ) -> InnerLifeSnapshot:
        manifest = subject.capability_manifest
        if manifest is None:
            return snapshot
        known = set(snapshot.source_refs)
        missing = tuple(ref for ref in manifest.source_refs if ref not in known)
        if not missing:
            return snapshot
        lookup = getattr(self.ledger, "lookup_event_commit", None)
        if not callable(lookup):
            raise ValueError("capability evidence authority lookup is unavailable")
        evidence: list[dict[str, object]] = []
        inventory = list(snapshot.source_inventory)
        for source_ref in missing:
            located = (
                await asyncio.to_thread(lookup, source_ref)
                if self.ledger.blocks_event_loop
                else lookup(source_ref)
            )
            if located is None:
                raise ValueError("capability evidence is not committed")
            event, commit = located
            if event.world_id != subject.world_id:
                raise ValueError("capability evidence belongs to another world")
            if (
                commit.world_revision > subject.cursor.world_revision
                or commit.deliberation_revision > subject.cursor.deliberation_revision
                or commit.ledger_sequence > subject.cursor.ledger_sequence
            ):
                raise ValueError("capability evidence is newer than the pinned cursor")
            content_hash = str(event.payload_hash).removeprefix("sha256:")
            if len(content_hash) != 64:
                raise ValueError("capability evidence payload hash is invalid")
            evidence.append(
                {
                    "source_ref": source_ref,
                    "event_type": event.event_type,
                    "payload_hash": event.payload_hash,
                    "source_world_revision": commit.world_revision,
                }
            )
            inventory.append(
                _InteriorSourceInventoryItem(
                    source_ref=source_ref,
                    scope="capability_evidence",
                    content_hash=content_hash,
                )
            )
        materials = dict(snapshot.materials)
        materials["capability_evidence"] = evidence
        source_refs = tuple(dict.fromkeys((*snapshot.source_refs, *missing)))
        return InnerLifeSnapshot.create(
            availability="available",
            world_id=snapshot.world_id,
            actor_ref=snapshot.actor_ref,
            cursor=snapshot.cursor,
            logical_time=snapshot.logical_time,
            situation=snapshot.situation,
            continuity=snapshot.continuity,
            facet_views=snapshot.facet_views,
            materials=materials,
            source_refs=source_refs,
            source_inventory=tuple(inventory),
            viewer_scope=snapshot.viewer_scope,
            privacy_scope=snapshot.privacy_scope,
            capability_scope=snapshot.capability_scope,
            context_compiler=snapshot.context_compiler,
            snapshot_compiler=snapshot.snapshot_compiler,
            truncation=snapshot.truncation,
        )


@dataclass(slots=True)
class _CoordinatorRecallPort:
    coordinator: RecallCoordinator
    _prefetch_tokens: dict[str, object] = field(default_factory=dict)

    @staticmethod
    def _trace_result(
        trace: TrustedRecallTrace,
        *,
        request: object,
        trace_field: str,
    ) -> dict[str, object]:
        audit = verify_trusted_recall_trace(trace)
        documents = tuple(hit.document for hit in audit.hits)
        return {
            "world_id": request.world_id,
            "actor_ref": request.actor_ref,
            "cursor": request.cursor,
            "content": {
                "items": [
                    {
                        "source_ref": document.source_item_ref,
                        "memory_kind": document.memory_kind,
                        "source_slice": document.source_slice,
                        "authority": document.authority,
                        "epistemic_scope": document.effective_epistemic_scope,
                        "text": document.text,
                        "occurred_from": document.occurred_from.isoformat(),
                        "occurred_to": (
                            document.occurred_to.isoformat()
                            if document.occurred_to is not None
                            else None
                        ),
                        "privacy_class": document.privacy_class,
                    }
                    for document in documents
                ]
            },
            "source_refs": tuple(dict.fromkeys(document.source_item_ref for document in documents)),
            trace_field: json.dumps(
                trace.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        }

    async def prefetch(self, request: object) -> dict[str, object] | None:
        cursor = RecallCursor(
            world_revision=request.cursor.world_revision,
            deliberation_revision=request.cursor.deliberation_revision,
            ledger_sequence=request.cursor.ledger_sequence,
        )
        token = self.coordinator.scheduled_prefetch_token(
            expected_cursor=cursor,
            trigger_ref=request.trigger_ref,
        )
        if token is None:
            return None
        self._prefetch_tokens[request.inner_turn_id] = token
        trace = await self.coordinator.await_scheduled_prefetch(
            expected_cursor=cursor,
            trigger_ref=request.trigger_ref,
            timeout_seconds=request.join_seconds,
            job_token=token,
        )
        if trace is None:
            return None
        return self._trace_result(
            trace,
            request=request,
            trace_field="prefetch_trace_json",
        )

    async def recall(self, request: object) -> dict[str, object] | None:
        cursor = RecallCursor(
            world_revision=request.cursor.world_revision,
            deliberation_revision=request.cursor.deliberation_revision,
            ledger_sequence=request.cursor.ledger_sequence,
        )
        token = self.coordinator.scheduled_prefetch_token(
            expected_cursor=cursor,
            trigger_ref=request.trigger_ref,
        )
        prefetched = None
        if token is not None:
            self._prefetch_tokens[request.inner_turn_id] = token
            automatic_trace = await self.coordinator.await_scheduled_prefetch(
                expected_cursor=cursor,
                trigger_ref=request.trigger_ref,
                timeout_seconds=0,
                job_token=token,
            )
            if automatic_trace is not None:
                prefetched = self._trace_result(
                    automatic_trace,
                    request=request,
                    trace_field="prefetch_trace_json",
                )
        trace = await asyncio.to_thread(
            self.coordinator.recall,
            request=CharacterRecallRequest(
                query_text=request.query,
                memory_kinds=(),
                limit=6,
            ),
            accessibility_seed=request.inner_turn_id,
            expected_cursor=cursor,
            trigger_ref=request.trigger_ref,
        )
        result = self._trace_result(
            trace,
            request=request,
            trace_field="recall_trace_json",
        )
        if prefetched is not None:
            result["prefetch"] = prefetched
        return result

    def record_prefetch_presentation(
        self,
        *,
        phase: str,
        model_call_id: str,
        trace_json: str,
    ) -> None:
        trace = TrustedRecallTrace.model_validate_json(trace_json)
        self.coordinator.record_prefetch_presentation(
            PresentedPrefetchTrace(
                phase=phase,
                model_call_id=model_call_id,
                trace=trace,
            )
        )

    def finish_turn(
        self,
        *,
        inner_turn_id: str,
        cursor: object,
        trigger_ref: str,
    ) -> None:
        token = self._prefetch_tokens.pop(inner_turn_id, None)
        if token is None:
            return
        recall_cursor = RecallCursor(
            world_revision=cursor.world_revision,
            deliberation_revision=cursor.deliberation_revision,
            ledger_sequence=cursor.ledger_sequence,
        )
        self.coordinator.discard_scheduled_prefetch(
            recall_cursor,
            trigger_ref=trigger_ref,
            job_token=token,
        )


@dataclass(slots=True)
class _CharacterInteriorBackgroundDriver:
    """Private scheduler implementation installed into CharacterInterior.

    It owns no character semantics: every such choice crosses the Module's
    ``consider``/``experience`` seam.  The contained workers retain only their
    durable scheduling, claim, CAS, Action and receipt authorities.
    """

    _ledger: LedgerPort
    _world_stimulus: CharacterInteriorWorldStimulusRuntime | None = None
    _silence_opener: SilenceAppraisalTriggerOpener | None = None
    _plan_disruption_opener: PlanDisruptionAppraisalTriggerOpener | None = None
    _proactive: ProactiveActionRuntime | None = None
    _reconsideration: ExpressionReconsiderationRuntime | None = None
    _private_impression_opener: PrivateImpressionTriggerOpener | None = None
    _private_impression: PrivateImpressionTriggerRuntime | None = None

    def is_bound_to(self, ledger: LedgerPort) -> bool:
        return self._ledger is ledger

    async def drain_world_stimulus_once(self) -> object | None:
        if self._world_stimulus is None:
            return None
        opened_trigger_ids: list[str] = []
        for opener in (self._silence_opener, self._plan_disruption_opener):
            if opener is None:
                continue
            try:
                trigger_id = await opener.open_once()
                if trigger_id is not None:
                    opened_trigger_ids.append(trigger_id)
            except (ConcurrencyConflict, IdempotencyConflict):
                # Both opportunities are deterministically derived again on
                # the next pass; a concurrent ledger winner is ordinary CAS.
                pass
        # A newly accepted source is routed through the source-bound L4 seam
        # before falling back to historical/recovery selection.  The seam only
        # narrows which durable trigger may be claimed; the same Interior
        # consumer, model contract and authorities still do all semantic work.
        if opened_trigger_ids:
            projection = (
                await asyncio.to_thread(self._ledger.project)
                if self._ledger.blocks_event_loop
                else self._ledger.project()
            )
            for trigger_id in opened_trigger_ids:
                process = next(
                    (
                        item
                        for item in projection.trigger_processes
                        if item.trigger_id == trigger_id
                    ),
                    None,
                )
                if process is None or process.source_evidence_ref is None:
                    raise RuntimeError("opened world stimulus lacks source evidence")
                result = await self._world_stimulus.advance_once(
                    process.source_evidence_ref
                )
                if result.status not in {"idle", "owned_elsewhere"}:
                    return result
        result = await self._world_stimulus.drain_one()
        return None if result.status in {"idle", "owned_elsewhere"} else result

    async def drain_reconsideration_once(self) -> object | None:
        if self._reconsideration is None:
            return None
        result = await self._reconsideration.drain_one()
        return None if result.status == "idle" else result

    async def drain_proactive_once(self) -> object | None:
        if self._proactive is None:
            return None
        result = await self._proactive.drain_one()
        return None if result.status in {"idle", "retry_wait"} else result

    async def drain_private_impression_once(self) -> object | None:
        if self._private_impression is None:
            return None
        assert self._private_impression_opener is not None
        try:
            await self._private_impression_opener.open_once()
        except (ConcurrencyConflict, IdempotencyConflict):
            # The next pass derives the same trigger from the accepted
            # appraisal. Losing this cursor race is ordinary effect-once
            # scheduling, not a new character decision.
            pass
        result = await self._private_impression.drain_one()
        return None if result.status in {"idle", "owned_elsewhere"} else result


def _bind_production_character_interior(
    *,
    interior: CharacterInterior,
    ledger: LedgerPort,
    proactive_capsules: ContextCapsuleCompiler,
    router: ModelRouterAdapter,
    recall_coordinator: RecallCoordinator,
    batch_issuer: AcceptedLedgerBatchIssuer,
    companion_actor_ref: str,
    reply_target: str,
    expression_capabilities: ExpressionDraftCapabilities,
    proactive_source_closure_model: ChatCompletionModel | None,
    proactive_candidate_external_proposition_inventory_model: ChatCompletionModel | None,
    interactive_turn_budget_policy: InteractiveTurnBudgetPolicy,
    proactive_account_id: str,
    proactive_amount_per_action: int,
    reply_recovery_policy: str,
    proactive_worker_owner: str,
    social_initiative_policy: SocialInitiativePolicy,
    private_impression_worker_owner: str,
    private_reflection_content_reader: Callable[[str], str | None],
    expression_reconsideration_owner: str,
    immediate_emotion_worker: ImmediateEmotionProposalWorker | None,
    inner_state_settlement_owner: str,
    silence_appraisal_idle_seconds: int | None,
    plan_disruption_appraisal_enabled: bool,
    perception_result_reader: PerceptionResultReader | None,
) -> None:
    """Bind ledger authorities and private background scheduling exactly once."""

    projection = interior._projection  # noqa: SLF001 - same deep Module package
    if not isinstance(projection, _DeferredProjection):
        raise RuntimeError("production CharacterInterior lacks its deferred projection")
    projection.bind(
        _LedgerCapsuleInteriorProjection(
            ledger=ledger,
            capsules=proactive_capsules,
            companion_actor_ref=companion_actor_ref,
        )
    )
    interior._install_recall_port(  # noqa: SLF001 - same deep Module package
        _CoordinatorRecallPort(recall_coordinator)
    )
    health = interior.runtime_health()
    topology_evidence = health["topology_evidence"]
    if (
        not isinstance(interior._registry.primary, StructuredCharacterRoleFaculty)  # noqa: SLF001
        or health["primary_author_faculty"] != "structured-character-role"
        or health["projection_contract"] != "subject_bound"
        or health["projection_bound"] is not True
        or health["recall_bound"] is not True
        or health["automatic_prefetch_bound"] is not True
        or topology_evidence["duplicate_purpose_owner_count"] != 0
        or topology_evidence["legacy_compatibility_route_installed"] is not False
        or health["semantic_author_count"] != 1
        or topology_evidence["unverified_author_faculty_names"]
        or expression_capabilities.private_turn_state_mode != "required"
    ):
        raise RuntimeError("production CharacterInterior topology is incomplete")

    purpose_faculties = set(health["purpose_faculties"])
    identity_frame = getattr(interior, "_production_identity_frame", None)

    proactive_runtime = None
    if "proactive_contact" in purpose_faculties:
        proactive_runtime = ProactiveActionRuntime(
            ledger=ledger,
            turn=ProactiveDeliberationTurn(
                ledger=ledger,
                capsule_compiler=proactive_capsules,
                character_interior=interior,
                router=router,
                target=reply_target,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                source_closure_reviewer=proactive_source_closure_model,
                report_relative_reviewer=proactive_source_closure_model,
                candidate_external_proposition_inventory_model=(
                    proactive_candidate_external_proposition_inventory_model
                ),
                companion_actor_ref=companion_actor_ref,
                budget_policy=interactive_turn_budget_policy,
            ),
            batch_issuer=batch_issuer,
            policy=ExpressionPlanBudgetPolicy(
                account_id=proactive_account_id,
                amount_limit_per_action=proactive_amount_per_action,
                actor=companion_actor_ref,
                allowed_targets=(reply_target,),
                recovery_policy=reply_recovery_policy,
                category="proactive",
            ),
            owner_id=proactive_worker_owner,
            social_initiative=SocialInitiativeCompiler(
                ledger=ledger,
                actor_ref=companion_actor_ref,
                policy=social_initiative_policy,
            ),
        )

    reconsideration_reviewer = (
        CharacterInteriorExpressionReconsiderationReviewer(
            character_interior=interior,
            ledger=ledger,
            actor_ref=companion_actor_ref,
        )
        if "expression_reconsideration" in purpose_faculties
        else None
    )
    reconsideration_runtime = (
        ExpressionReconsiderationRuntime(
            ledger=ledger,
            owner_id=expression_reconsideration_owner,
            reviewer=reconsideration_reviewer,
        )
        if reconsideration_reviewer is not None
        else None
    )

    private_opener: PrivateImpressionTriggerOpener | None = None
    private_runtime: PrivateImpressionTriggerRuntime | None = None
    authority_handlers: list[object] = []
    if "private_impression_reflection" in purpose_faculties:
        private_opener = PrivateImpressionTriggerOpener(
            ledger=ledger,
            owner_id=private_impression_worker_owner,
        )
        private_runtime = PrivateImpressionTriggerRuntime(
            ledger=ledger,
            character_interior=interior,
            companion_actor_ref=companion_actor_ref,
            identity_frame=identity_frame,
            content_reader=private_reflection_content_reader,
            owner_id=private_impression_worker_owner,
        )
        authority_handlers.append(_PrivateImpressionInteriorAuthorityHandler(private_runtime))

    world_stimulus_runtime: CharacterInteriorWorldStimulusRuntime | None = None
    silence_opener: SilenceAppraisalTriggerOpener | None = None
    disruption_opener: PlanDisruptionAppraisalTriggerOpener | None = None
    if "world_stimulus_appraisal" in purpose_faculties:
        if immediate_emotion_worker is None:
            raise RuntimeError(
                "world stimulus purpose requires combined Appraisal/Affect authority"
            )
        authority_handlers.append(
            _WorldStimulusInteriorAuthorityHandler(
                ledger=ledger,
                owner_id=inner_state_settlement_owner,
                perception_result_reader=perception_result_reader,
            )
        )
        world_stimulus_runtime = CharacterInteriorWorldStimulusRuntime(
            ledger=ledger,
            character_interior=interior,
            emotion_worker=immediate_emotion_worker,
            owner_id=inner_state_settlement_owner,
            companion_actor_ref=companion_actor_ref,
            perception_result_reader=perception_result_reader,
            relationship_settlement=_WorldStimulusRelationshipSignalSettlement(
                ledger=ledger,
                compiler=RelationshipProposalCompiler(ledger=ledger),
                acceptance=RelationshipAcceptanceRuntime(
                    ledger=ledger,
                    batch_issuer=batch_issuer,
                ),
                owner_id=inner_state_settlement_owner,
            ),
        )
        if silence_appraisal_idle_seconds is not None and silence_appraisal_idle_seconds > 0:
            silence_opener = SilenceAppraisalTriggerOpener(
                ledger=ledger,
                owner_id=inner_state_settlement_owner,
                idle_seconds_threshold=silence_appraisal_idle_seconds,
            )
        if plan_disruption_appraisal_enabled:
            disruption_opener = PlanDisruptionAppraisalTriggerOpener(
                ledger=ledger,
                owner_id=inner_state_settlement_owner,
            )

    authority = interior._authority  # noqa: SLF001 - same deep Module package
    if authority_handlers:
        if not isinstance(authority, _DeferredInteriorAuthority):
            raise RuntimeError(
                "production CharacterInterior with typed purposes lacks deferred authority"
            )
        authority.bind(tuple(authority_handlers))

    driver = _CharacterInteriorBackgroundDriver(
        _ledger=ledger,
        _world_stimulus=world_stimulus_runtime,
        _silence_opener=silence_opener,
        _plan_disruption_opener=disruption_opener,
        _proactive=proactive_runtime,
        _reconsideration=reconsideration_runtime,
        _private_impression_opener=private_opener,
        _private_impression=private_runtime,
    )
    interior._install_background_driver(driver)  # noqa: SLF001 - same deep Module
    bound_health = interior.runtime_health()
    if authority_handlers and bound_health["authority_bound"] is not True:
        raise RuntimeError("production CharacterInterior typed authority is unbound")


def compose_production_character_interior(
    *,
    flash_model: ChatCompletionModel,
    thinking_model: ChatCompletionModel | None,
    source_closure_model: ChatCompletionModel | None,
    report_relative_source_closure_model: ChatCompletionModel | None,
    candidate_external_proposition_inventory_model: ChatCompletionModel | None,
    source_closure_reselection_lane: SourceClosureReselectionLane | None,
    expression_episode_observer_model: ChatCompletionModel | None,
    flash_model_id: str,
    thinking_model_id: str | None,
    expression_capabilities: ExpressionDraftCapabilities,
    identity_frame: CompanionIdentityFrame,
    review_claim_free_candidates: bool = False,
    turn_store: _CharacterInteriorTurnStore | None = None,
    turn_owner_id: str = "character-interior:production",
) -> CharacterInterior:
    """Build and freeze every protagonist author Faculty exactly once."""

    if expression_capabilities.private_turn_state_mode != "required":
        raise ValueError(
            "production expression requires a final PrivateTurnState; "
            "legacy_optional is historical replay/test only"
        )

    author = _InboundCharacterAuthor(
        flash_model=flash_model,
        thinking_model=thinking_model,
        source_closure_model=source_closure_model,
        report_relative_source_closure_model=report_relative_source_closure_model,
        candidate_external_proposition_inventory_model=(
            candidate_external_proposition_inventory_model
        ),
        source_closure_reselection_lane=source_closure_reselection_lane,
        expression_episode_observer_model=expression_episode_observer_model,
        contextual_failsafe_model=None,
        contextual_failsafe_reviewer_model=None,
        contextual_failsafe_enabled=False,
        flash_model_id=flash_model_id,
        thinking_model_id=thinking_model_id,
        expression_capabilities=expression_capabilities,
        identity_frame=identity_frame,
        review_claim_free_candidates=review_claim_free_candidates,
        require_explicit_authored_decision_fields=True,
    )
    inbound_turn = InboundTurnFaculty(
        author=author,
    )
    interior = CharacterInterior(
        projection=_DeferredProjection(),
        role=StructuredCharacterRoleFaculty(
            model=flash_model,
            model_id=flash_model_id,
        ),
        faculties=(inbound_turn,),
        authority=_DeferredInteriorAuthority(),
        turn_store=turn_store,
        turn_owner_id=turn_owner_id,
    )
    # Immutable composition material used only by source-closure/capsule
    # authorities after the ledger is bound.  It is not an extractable role
    # adapter and cannot author a second semantic path.
    interior._production_identity_frame = identity_frame  # type: ignore[attr-defined]  # noqa: SLF001
    return interior


def compose_fixture_character_interior(
    *,
    model: ChatCompletionModel,
    projection: object | None = None,
    recall_coordinator: RecallCoordinator | None = None,
) -> CharacterInterior:
    """Build and optionally bind the production Module topology for fixtures.

    ``projection`` and ``recall_coordinator`` are boundary fixtures, not
    alternate character authors.  Supplying them exercises the same deferred
    Capsule projection and source-bound Recall ports installed in production.
    """

    interior = compose_production_character_interior(
        flash_model=model,
        thinking_model=None,
        source_closure_model=None,
        report_relative_source_closure_model=None,
        candidate_external_proposition_inventory_model=None,
        source_closure_reselection_lane=None,
        expression_episode_observer_model=None,
        flash_model_id=str(getattr(model, "model", "fixture-character-author")),
        thinking_model_id=None,
        expression_capabilities=PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame=CompanionIdentityFrame(
            companion_name="枝枝",
            counterpart_name="fixture counterpart",
            personality_frame="有自己的生活、判断和表达选择。",
        ),
    )
    if projection is not None:
        deferred = interior._projection  # noqa: SLF001 - same deep Module package
        if not isinstance(deferred, _DeferredProjection):
            raise RuntimeError("fixture CharacterInterior lacks its deferred projection")
        deferred.bind(projection)
    if recall_coordinator is not None:
        interior._install_recall_port(  # noqa: SLF001 - same deep Module package
            _CoordinatorRecallPort(recall_coordinator)
        )
    return interior


__all__: list[str] = []
