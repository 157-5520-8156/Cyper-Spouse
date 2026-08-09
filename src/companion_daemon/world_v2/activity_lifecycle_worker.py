"""Explicit Life Ecology adapter for bounded activity deliberation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from .activity_lifecycle_draft import (
    ActivityLifecycleModelDraft,
)
from .activity_lifecycle_proposal import ActivityLifecycleProposalCompiler
from .activity_lifecycle_runtime import (
    ActivityLifecycleAcceptanceRuntime,
    ActivityLifecycleProposalRecorder,
)
from .character_interior import CharacterInterior, InteriorOpportunity
from .character_interior.audit import recorded_character_interior_model_result
from .character_interior.contracts import _InteriorCapabilityManifest
from .character_interior.run_result import CausalOpportunityRuntime
from .life_ecology_activity import ActivityOpeningCatalog
from .schema_core import FrozenModel
from .proposal_audit_schemas import ModelResultRecordedPayload
from .schemas import ProjectionCursor


class ActivityLifecycleFollowupResult(FrozenModel):
    status: Literal["transitioned", "no_op", "blocked", "technical_failure"]
    reason_code: str | None = None
    proposal_event_ref: str | None = None
    character_interior_model_result: ModelResultRecordedPayload | None = None


class ActivityLifecycleWorker:
    """Turn one claimed ecology wake into at most one accepted transition.

    The worker owns orchestration only.  The model receives a safe capsule,
    the compiler derives authority, the recorder persists the proposal, and
    the acceptance runtime materializes the effect.  No fallback selection is
    made when the model declines or the catalog has no legal opening.
    """

    def __init__(
        self,
        *,
        ledger,
        catalog: ActivityOpeningCatalog,
        character_interior: CharacterInterior,
        owner_actor_ref: str,
        proposal_recorder: ActivityLifecycleProposalRecorder,
        acceptance_runtime: ActivityLifecycleAcceptanceRuntime,
        ecology_catalog_version: str,
        source: str = "world-v2:activity-lifecycle",
    ) -> None:
        if not ecology_catalog_version or not source or not owner_actor_ref:
            raise ValueError(
                "activity lifecycle worker requires catalog version, source and owner actor"
            )
        self._ledger = ledger
        self._catalog = catalog
        self._character_interior = character_interior
        self._owner_actor_ref = owner_actor_ref
        self._proposal_recorder = proposal_recorder
        self._acceptance_runtime = acceptance_runtime
        self._compiler = ActivityLifecycleProposalCompiler(
            catalog=catalog, ecology_catalog_version=ecology_catalog_version
        )
        self._source = source

    async def advance_once(
        self,
        *,
        wake_event_ref: str,
        trigger_id: str,
        logical_time: datetime,
        actor: str,
        trace_id: str,
        correlation_id: str,
    ) -> ActivityLifecycleFollowupResult:
        projection = self._ledger.project()
        if projection.logical_time != logical_time:
            return ActivityLifecycleFollowupResult(
                status="blocked", reason_code="activity_lifecycle.logical_time_not_current"
            )
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        catalog = self._catalog.openings_for(
            projection=projection, wake_event_ref=wake_event_ref
        )
        if catalog.status != "openings_available":
            return ActivityLifecycleFollowupResult(
                status="blocked" if catalog.status == "blocked_by_missing_capability" else "no_op",
                reason_code=catalog.reason_code or f"activity_lifecycle.{catalog.status}",
            )
        draft, failure_code = await self._character_choice(
            projection=projection,
            catalog=catalog,
            wake_event_ref=wake_event_ref,
            trigger_id=trigger_id,
        )
        if draft is None:
            return ActivityLifecycleFollowupResult(
                status="technical_failure",
                reason_code="activity_lifecycle." + (failure_code or "unknown"),
            )
        proposal = self._compiler.compile(
            projection=projection,
            wake_event_ref=wake_event_ref,
            ecology_trigger_id=trigger_id,
            draft=draft,
        )
        if proposal is None:
            return ActivityLifecycleFollowupResult(
                status="no_op",
                reason_code="activity_lifecycle.model_declined",
                character_interior_model_result=draft.character_interior_model_result,
            )
        recorded = self._proposal_recorder.record(
            cursor=cursor,
            proposal=proposal,
            actor=actor,
            source=self._source,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        accepted_cursor = ProjectionCursor(
            world_revision=recorded.commit.world_revision,
            deliberation_revision=recorded.commit.deliberation_revision,
            ledger_sequence=recorded.commit.ledger_sequence,
        )
        self._acceptance_runtime.accept(
            handle=self._acceptance_runtime.pin_proposal(
                cursor=accepted_cursor, proposal_event_ref=recorded.proposal_event_ref
            ),
            actor=actor,
            source=self._source,
            logical_time=logical_time,
            created_at=logical_time,
            trace_id=trace_id,
            correlation_id=correlation_id,
        )
        return ActivityLifecycleFollowupResult(
            status="transitioned", proposal_event_ref=recorded.proposal_event_ref
        )

    async def _character_choice(
        self,
        *,
        projection,
        catalog,
        wake_event_ref: str,
        trigger_id: str,
    ) -> tuple[ActivityLifecycleModelDraft | None, str | None]:
        """Ask the sole protagonist author to choose one already-legal token.

        The activity catalog and acceptance runtime retain all plan authority.
        This method contributes only a source-bound capability view to the same
        ``CharacterInterior`` used by Chat, Proactive and open Life.
        """

        capability = {
            "contract": "character-interior-activity-lifecycle-capability.2",
            "catalog_version": catalog.catalog_version,
            "catalog_hash": catalog.catalog_hash,
            "offered_tokens": [item.opening_token for item in catalog.openings],
            "openings": [
                {
                    "opening_token": item.opening_token,
                    "safe_summary": item.safe_summary,
                }
                for item in catalog.openings
            ],
        }
        payload_json = json.dumps(
            capability,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        capability_hash = "sha256:" + hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        manifest = _InteriorCapabilityManifest(
            capability_ref=(
                "capability:activity-lifecycle:"
                + hashlib.sha256(
                    (trigger_id + ":" + capability_hash).encode("utf-8")
                ).hexdigest()
            ),
            capability_kind="activity_lifecycle_choice",
            payload_json=payload_json,
            payload_hash=capability_hash,
            source_refs=(wake_event_ref,),
        )
        opportunity_identity = CausalOpportunityRuntime(
            world_id=self._ledger.world_id,
            actor_ref=self._owner_actor_ref,
            purpose="activity_lifecycle_choice",
        ).identity_for_refs((wake_event_ref,), epoch=wake_event_ref)
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        result = await self._character_interior.consider(
            InteriorOpportunity(
                opportunity_ref=opportunity_identity.opportunity_ref,
                inner_turn_ref="inner-turn:activity-lifecycle:" + trigger_id,
                world_id=self._ledger.world_id,
                actor_ref=self._owner_actor_ref,
                trigger_ref=wake_event_ref,
                cursor=cursor,
                logical_time=projection.logical_time,
                purpose="activity_lifecycle_choice",
                source_refs=(wake_event_ref,),
                capability_manifest=manifest,
                context_note=(
                    "One exact clock wake offers already-authorized activity transitions. "
                    "The character owns select or no-op; the system owns only token authority."
                ),
            )
        )
        if result.status == "technical_failure":
            return None, result.failure_code or "character_interior_technical_failure"
        if result.status != "decided" or not isinstance(result.decision, dict):
            return None, "character_interior_decision_missing"
        decision = result.decision
        if (
            decision.get("contract") != "character-interior-purpose-decision.1"
            or decision.get("purpose") != "activity_lifecycle_choice"
            or decision.get("capability_ref") != manifest.capability_ref
            or decision.get("capability_payload_hash") != manifest.payload_hash
            or tuple(decision.get("source_refs", ())) != manifest.source_refs
        ):
            return None, "character_interior_decision_binding_invalid"
        payload = decision.get("payload")
        if not isinstance(payload, dict):
            return None, "character_interior_decision_payload_invalid"
        if payload.get("contract") != "character-interior-activity-lifecycle-choice.1":
            return None, "character_interior_decision_contract_invalid"
        choice = payload.get("decision")
        if choice == "no_op" and set(payload) == {"contract", "decision"}:
            token = None
            draft_decision = "no_op"
        elif choice == "select" and set(payload) == {
            "contract",
            "decision",
            "selected_token",
        }:
            token = payload.get("selected_token")
            if not isinstance(token, str) or token not in capability["offered_tokens"]:
                return None, "character_interior_selected_token_invalid"
            draft_decision = "opening_token"
        else:
            return None, "character_interior_decision_payload_invalid"
        lineage = result.author_lineage
        model_id = getattr(lineage, "model_id", None)
        if not isinstance(model_id, str) or not model_id:
            return None, "character_interior_author_lineage_missing"
        normalized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        model_result_audit = recorded_character_interior_model_result(
            result,
            purpose="activity_lifecycle_choice",
            subject_ref=result.opportunity_ref,
            trigger_ref=wake_event_ref,
            capability_ref=manifest.capability_ref,
            route_tier="flash",
            route_reason_code="activity_lifecycle.character_choice",
            router_version="character-interior-activity-lifecycle-capability.2",
        )
        return (
            ActivityLifecycleModelDraft(
                decision=draft_decision,
                opening_token=token,
                model=model_id,
                raw_output=normalized,
                raw_output_hash=digest,
                normalized_json=normalized,
                normalized_output_hash=digest,
                character_interior_model_result=model_result_audit,
            ),
            None,
        )


__all__ = ["ActivityLifecycleFollowupResult", "ActivityLifecycleWorker"]
