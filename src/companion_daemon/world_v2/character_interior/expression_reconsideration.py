"""Expression interruption review through the sole CharacterInterior author.

The durable reconsideration runtime still owns claim, CAS, cancellation and
Action settlement.  This private bridge only presents the exact interrupted
beat as a capability and translates the character's source-bound choice back
to that runtime's closed disposition contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping

from ..expression_reconsideration_runtime import ExpressionReconsiderationDecision
from ..proposal_envelope import (
    DecisionProposal,
    MinimalProposal,
    validate_proposal_envelope,
)
from ..schemas import ProjectionCursor, TriggerProcess, WorldEvent
from .contracts import InnerDecision, InteriorOpportunity, _InteriorCapabilityManifest
from .core import CharacterInterior
from .audit import recorded_character_interior_lineage
from .run_result import CausalOpportunityIdentity


_PURPOSE = "expression_reconsideration"
_DECISION_CONTRACT = "character-interior-purpose-decision.1"
_PAYLOAD_CONTRACT = "character-interior-expression-reconsideration-decision.1"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class CharacterInteriorExpressionReconsiderationReviewer:
    """Offer one already-gated expression beat to ``CharacterInterior``.

    No legacy reconsideration model, prompt, snapshot compiler or recall lane
    is used here.  A technical/no-payload result raises so the durable gate
    remains claimed and retryable; it can never be interpreted as continue.
    """

    def __init__(
        self,
        *,
        character_interior: CharacterInterior,
        ledger: object,
        actor_ref: str,
    ) -> None:
        if not callable(getattr(character_interior, "consider", None)):
            raise TypeError("expression reconsideration needs CharacterInterior")
        if not callable(getattr(ledger, "project_at", None)):
            raise TypeError("expression reconsideration needs a cursor projection")
        if not actor_ref:
            raise ValueError("expression reconsideration actor is required")
        self._interior = character_interior
        self._ledger = ledger
        self._actor_ref = actor_ref

    async def review(
        self,
        *,
        process: TriggerProcess,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
    ) -> ExpressionReconsiderationDecision:
        projection = await self._project(cursor)
        logical_time = getattr(projection, "logical_time", None)
        if logical_time is None:
            raise RuntimeError("expression reconsideration logical time is unavailable")
        lineage = self._lineage(process)
        beat = next(
            (
                item
                for item in getattr(projection, "expression_beats", ())
                if item.plan_id == lineage["plan_id"] and item.beat_id == lineage["beat_id"]
            ),
            None,
        )
        if beat is None:
            raise RuntimeError("expression reconsideration beat is unavailable")
        old_private_turn_state = self._old_private_turn_state_context(
            beat=beat,
            projection=projection,
        )
        old_private_turn_state_ref = (
            old_private_turn_state.get("proposal_ref")
            if old_private_turn_state is not None
            else None
        )
        source_refs = tuple(
            dict.fromkeys(
                item
                for item in (
                    observation_event.event_id,
                    beat.event_ref,
                    old_private_turn_state_ref,
                )
                if isinstance(item, str) and item
            )
        )
        capability = self._capability(
            process=process,
            observation_event=observation_event,
            cursor=cursor,
            beat=beat,
            projection=projection,
            source_refs=source_refs,
            old_private_turn_state=old_private_turn_state,
        )
        opportunity_identity = CausalOpportunityIdentity(
            world_id=observation_event.world_id,
            actor_ref=self._actor_ref,
            purpose=_PURPOSE,
            source_refs=tuple(sorted(source_refs)),
            epoch=observation_event.event_id,
        )
        opportunity = InteriorOpportunity(
            opportunity_ref=opportunity_identity.opportunity_ref,
            inner_turn_ref=(
                process.claim_lease.attempt_id
                if process.claim_lease is not None
                else process.trigger_id
            ),
            world_id=observation_event.world_id,
            actor_ref=self._actor_ref,
            trigger_ref=observation_event.event_id,
            cursor=cursor,
            logical_time=logical_time,
            purpose=_PURPOSE,
            source_refs=source_refs,
            capability_manifest=capability,
            context_note=(
                "A user observation arrived before an authorized expression beat was "
                "dispatched. The character owns whether that earlier expression still fits."
            ),
        )
        decision = await self._interior.consider(opportunity)
        disposition = self._disposition(decision, capability=capability)
        replacement_plan_ref = None
        if disposition in {"merge", "supersede", "new_beat"}:
            replacement_plan_ref = self._accepted_replacement(
                projection=projection,
                observation_event=observation_event,
            )
            if replacement_plan_ref is None:
                # The character requested replacement semantics, but this
                # worker has no authority to invent/accept replacement prose.
                # Retire the old beat at the hard boundary.
                disposition = "cancel"
        return ExpressionReconsiderationDecision(
            disposition=disposition,
            rationale_ref=decision.inner_turn_id,
            replacement_plan_ref=replacement_plan_ref,
            character_interior_lineage=recorded_character_interior_lineage(
                decision,
                purpose=_PURPOSE,
                subject_ref=opportunity.opportunity_ref,
                capability_ref=capability.capability_ref,
            ),
        )

    async def _project(self, cursor: ProjectionCursor) -> object:
        if bool(getattr(self._ledger, "blocks_event_loop", False)):
            return await asyncio.to_thread(self._ledger.project_at, cursor)
        return self._ledger.project_at(cursor)

    @staticmethod
    def _lineage(process: TriggerProcess) -> dict[str, str]:
        prefix = "expression-reconsideration:"
        if not process.trigger_ref.startswith(prefix):
            raise ValueError("expression reconsideration trigger lineage is invalid")
        value = json.loads(process.trigger_ref.removeprefix(prefix))
        if not isinstance(value, dict) or not all(
            isinstance(value.get(key), str) and value[key]
            for key in ("plan_id", "beat_id", "observation_id")
        ):
            raise ValueError("expression reconsideration trigger lineage is invalid")
        return value

    @staticmethod
    def _pending_text(*, beat: object, projection: object) -> str | None:
        payload_ref = getattr(beat, "payload_ref", None)
        return next(
            (
                item.text
                for item in getattr(projection, "stored_message_payloads", ())
                if item.payload_ref == payload_ref and isinstance(item.text, str)
            ),
            None,
        )

    @staticmethod
    def _old_private_turn_state_context(
        *,
        beat: object,
        projection: object,
    ) -> dict[str, object] | None:
        """Recover the prior turn-local audit without granting fact authority.

        This is projection material from the same pinned cursor, not a second
        role model or a compatibility author.  Malformed historical audit
        records are optional advisory context and therefore fail closed to no
        prior private state while the accepted beat remains reconsiderable.
        """

        proposal_id = getattr(beat, "proposal_id", None)
        if not isinstance(proposal_id, str) or not proposal_id:
            return None
        audit = next(
            (
                item
                for item in reversed(tuple(getattr(projection, "proposal_audits", ())))
                if getattr(item, "proposal_id", None) == proposal_id
            ),
            None,
        )
        proposal_json = getattr(audit, "proposal_json", None)
        proposal_ref = getattr(audit, "event_ref", None)
        if not isinstance(proposal_json, str) or not isinstance(proposal_ref, str):
            return None
        try:
            proposal = validate_proposal_envelope(json.loads(proposal_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(proposal, (DecisionProposal, MinimalProposal)):
            return None
        private_turn_state = proposal.private_turn_state
        if private_turn_state is None:
            return None
        return {
            "value": private_turn_state.model_dump(mode="json"),
            "proposal_id": proposal.proposal_id,
            "proposal_ref": proposal_ref,
            "authority": "turn_local_audit_only",
            "fact_authority": False,
        }

    @classmethod
    def _capability(
        cls,
        *,
        process: TriggerProcess,
        observation_event: WorldEvent,
        cursor: ProjectionCursor,
        beat: object,
        projection: object,
        source_refs: tuple[str, ...],
        old_private_turn_state: Mapping[str, object] | None,
    ) -> _InteriorCapabilityManifest:
        payload = {
            "contract": "character-interior-expression-reconsideration-capability.1",
            "allowed_dispositions": [
                "continue",
                "cancel",
                "defer",
                "merge",
                "supersede",
                "new_beat",
            ],
            "trigger_id": process.trigger_id,
            "new_observation": {
                "source_ref": observation_event.event_id,
                "payload_hash": observation_event.payload_hash,
                "value": observation_event.payload(),
            },
            "old_pending_expression": {
                "plan_id": getattr(beat, "plan_id"),
                "beat_id": getattr(beat, "beat_id"),
                "source_ref": getattr(beat, "event_ref"),
                "payload_ref": getattr(beat, "payload_ref"),
                "text": cls._pending_text(beat=beat, projection=projection),
            },
            "old_private_turn_state": old_private_turn_state,
            "pinned_cursor": cursor.model_dump(mode="json"),
            "replacement_authority": (
                "Only an already-accepted plan for the new observation can be referenced; "
                "otherwise replacement dispositions retire the old beat."
            ),
        }
        payload_json = _canonical(payload)
        return _InteriorCapabilityManifest(
            capability_ref="capability:expression-reconsideration:"
            + _digest(
                {
                    "trigger_id": process.trigger_id,
                    "attempt_ids": process.attempt_ids,
                    "payload": payload,
                }
            ),
            capability_kind=_PURPOSE,
            payload_json=payload_json,
            payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
            source_refs=source_refs,
        )

    @staticmethod
    def _disposition(
        decision: InnerDecision,
        *,
        capability: _InteriorCapabilityManifest,
    ) -> str:
        if decision.status == "technical_failure":
            raise RuntimeError(
                "character Interior reconsideration failure: "
                + (decision.failure_code or "unknown")
            )
        if decision.status != "decided" or decision.decision is None:
            raise RuntimeError("character Interior reconsideration lacks an explicit decision")
        outer = decision.decision
        if (
            not isinstance(outer, Mapping)
            or outer.get("contract") != _DECISION_CONTRACT
            or outer.get("purpose") != _PURPOSE
            or outer.get("capability_ref") != capability.capability_ref
            or outer.get("capability_payload_hash") != capability.payload_hash
            or tuple(outer.get("source_refs", ())) != capability.source_refs
        ):
            raise ValueError("expression reconsideration Interior envelope is invalid")
        payload = outer.get("payload")
        if (
            not isinstance(payload, Mapping)
            or payload.get("contract") != _PAYLOAD_CONTRACT
            or set(payload) != {"contract", "disposition"}
        ):
            raise ValueError("expression reconsideration Interior payload is invalid")
        disposition = payload.get("disposition")
        if disposition not in {
            "continue",
            "cancel",
            "defer",
            "merge",
            "supersede",
            "new_beat",
        }:
            raise ValueError("expression reconsideration disposition is invalid")
        return disposition

    @staticmethod
    def _accepted_replacement(
        *,
        projection: object,
        observation_event: WorldEvent,
    ) -> str | None:
        accepted = {
            item.proposal_id for item in getattr(projection, "expression_plan_manifests", ())
        }
        return next(
            (
                audit.event_ref
                for audit in reversed(tuple(getattr(projection, "proposal_audits", ())))
                if audit.trigger_ref == observation_event.event_id and audit.proposal_id in accepted
            ),
            None,
        )


__all__: list[str] = []
