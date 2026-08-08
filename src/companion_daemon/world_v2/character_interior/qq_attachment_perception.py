"""Source-bound QQ attachment perception through the sole CharacterInterior.

The QQ boundary owns archived bytes, duplicate evidence and deployment budget.
It offers only eligible opaque attachment capabilities to ``CharacterInterior``;
the character owns whether to use one.  Provider, projection and schema failures
raise technical failures and can never be converted into a local decline.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from ..deliberation import ModelInput, ModelOutput
from ..perception_executor import PerceptionTransport
from ..perception_input_source import PerceptionInputDescriptor, PerceptionInputSource
from ..perception_proposal_compiler import perception_input_ref
from ..proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    TypedChange,
)
from ..schemas import ProjectionCursor
from .contracts import InnerDecision, InteriorOpportunity, _InteriorCapabilityManifest
from .core import CharacterInterior
from .audit import recorded_character_interior_lineage


_PURPOSE = "qq_attachment_perception"
_CAPABILITY_CONTRACT = "qq-attachment-perception-capability.1"
_DECISION_CONTRACT = "character-interior-purpose-decision.1"
_PAYLOAD_CONTRACT = "character-interior-qq-attachment-perception-decision.1"


class QQAttachmentPerceptionTechnicalFailure(RuntimeError):
    """A retryable technical terminal, never a character choice."""

    def __init__(self, code: str) -> None:
        self.code = code[:128]
        super().__init__(self.code)


def _technical(code: str) -> QQAttachmentPerceptionTechnicalFailure:
    return QQAttachmentPerceptionTechnicalFailure(code)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class CharacterInteriorQQAttachmentPerceptionPort:
    """Deliberation adapter whose only semantic author is CharacterInterior."""

    model_id = "character-interior"
    model_version = "character-interior-qq-attachment-perception.1"

    def __init__(
        self,
        *,
        character_interior: CharacterInterior,
        input_source: PerceptionInputSource,
        dispatch_evidence: PerceptionTransport,
        budget_account_id: str,
        budget_limit: int,
        daily_limit: int,
        local_timezone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not callable(getattr(character_interior, "consider", None))
            or not callable(getattr(input_source, "describe", None))
            or not callable(getattr(dispatch_evidence, "dispatched_count_since", None))
            or not callable(getattr(dispatch_evidence, "has_result_for_input", None))
        ):
            raise TypeError("QQ perception needs CharacterInterior and durable capability ports")
        if not budget_account_id or budget_limit <= 0 or daily_limit <= 0:
            raise ValueError("QQ perception needs a positive deployment budget")
        try:
            zone = ZoneInfo(local_timezone)
        except (KeyError, ValueError) as exc:
            raise ValueError("QQ perception local timezone is invalid") from exc
        self._interior = character_interior
        self._inputs = input_source
        self._evidence = dispatch_evidence
        self._budget_account_id = budget_account_id
        self._budget_limit = budget_limit
        self._daily_limit = daily_limit
        self._zone = zone
        self._now = now or (lambda: datetime.now(zone))

    async def propose(self, request: ModelInput) -> ModelOutput:
        trigger = request.trigger_message
        if trigger is None:
            return self._boundary_no_change(request, "trigger_message_unavailable")
        candidates = self._eligible_candidates(request)
        if not candidates:
            return self._boundary_no_change(request, "eligible_attachment_unavailable")
        dispatched_count = self._daily_dispatched_count()
        if dispatched_count >= self._daily_limit:
            return self._boundary_no_change(request, "daily_budget_exhausted")

        opportunity = self._opportunity(
            request,
            candidates=candidates,
            dispatched_count=dispatched_count,
        )
        try:
            decision = await self._interior.consider(opportunity)
        except QQAttachmentPerceptionTechnicalFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - preserve retryable role/projection faults
            raise _technical("character_interior_perception_exception") from exc
        if not isinstance(decision, InnerDecision):
            raise _technical("character_interior_perception_result_invalid")
        if decision.status == "technical_failure":
            raise _technical(decision.failure_code or "character_interior_perception_failure")
        # The canonical snapshot is compiled exactly once by CharacterInterior
        # from this cursor-bound opportunity.  This bridge validates only the
        # final coordinates and lets the Interior own snapshot identity.
        if (
            decision.cursor != opportunity.cursor
            or decision.actor_ref != opportunity.actor_ref
            or decision.opportunity_ref != opportunity.opportunity_ref
            or decision.snapshot_id is None
            or decision.snapshot_hash is None
        ):
            raise _technical("character_interior_perception_snapshot_mismatch")
        if decision.status == "model_silent":
            return self._character_no_change(request, decision, opportunity=opportunity)
        attachment_ref = self._selected_attachment(
            decision,
            opportunity=opportunity,
            candidates=candidates,
        )
        return self._request_proposal(
            request,
            decision=decision,
            opportunity=opportunity,
            attachment_ref=attachment_ref,
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        """Keep Deliberation's recovery lane technical for a later durable retry.

        ``CharacterInterior`` already owns its one same-author structural
        correction. Re-entering it here with the same pinned attempt would
        either read the effect-once terminal or fabricate a second authoring
        opportunity, so the trigger worker performs the later retry instead.
        """

        del request, failure_code
        raise _technical("qq_attachment_perception_retry_deferred")

    def _eligible_candidates(
        self,
        request: ModelInput,
    ) -> tuple[tuple[str, str, PerceptionInputDescriptor], ...]:
        trigger = request.trigger_message
        if trigger is None:
            return ()
        eligible: list[tuple[str, str, PerceptionInputDescriptor]] = []
        for attachment_ref, media_type in zip(
            trigger.attachment_refs,
            trigger.attachment_media_types,
        ):
            if media_type != "image":
                continue
            try:
                descriptor = self._inputs.describe(
                    attachment_ref=attachment_ref,
                    analysis_kind="vision",
                )
            except ValueError:
                # No archived/supported bytes means that no capability exists.
                continue
            except Exception as exc:  # noqa: BLE001 - storage faults are retryable
                raise _technical("perception_input_description_unavailable") from exc
            if descriptor.attachment_ref != attachment_ref or descriptor.analysis_kind != "vision":
                raise _technical("perception_input_description_mismatch")
            try:
                already_dispatched = self._evidence.has_result_for_input(
                    input_hash=descriptor.content_hash
                )
            except Exception as exc:  # noqa: BLE001 - receipt store outage is not silence
                raise _technical("perception_dispatch_evidence_unavailable") from exc
            if already_dispatched:
                continue
            eligible.append((attachment_ref, media_type, descriptor))
        return tuple(eligible)

    def _daily_dispatched_count(self) -> int:
        local_now = self._now()
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            local_now = local_now.replace(tzinfo=self._zone)
        local_now = local_now.astimezone(self._zone)
        midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            count = self._evidence.dispatched_count_since(midnight)
        except Exception as exc:  # noqa: BLE001 - receipt store outage is not silence
            raise _technical("perception_dispatch_evidence_unavailable") from exc
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise _technical("perception_dispatch_evidence_invalid")
        return count

    @staticmethod
    def _context(request: ModelInput) -> Mapping[str, object]:
        try:
            context = json.loads(request.model_content_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise _technical("canonical_inner_life_context_invalid") from exc
        if not isinstance(context, dict):
            raise _technical("canonical_inner_life_context_invalid")
        return context

    @classmethod
    def _opportunity_coordinates(
        cls,
        request: ModelInput,
    ) -> tuple[str, str, datetime, tuple[str, ...]]:
        context = cls._context(request)
        world_id = context.get("world_id")
        actor_ref = context.get("actor_ref")
        logical_time_raw = context.get("logical_time")
        if (
            not isinstance(world_id, str)
            or not isinstance(actor_ref, str)
            or not isinstance(logical_time_raw, str)
        ):
            raise _technical("canonical_inner_life_coordinates_invalid")
        try:
            logical_time = datetime.fromisoformat(logical_time_raw)
        except (TypeError, ValueError, ValidationError) as exc:
            raise _technical("canonical_inner_life_coordinates_invalid") from exc
        if logical_time.tzinfo is None or logical_time.utcoffset() is None:
            raise _technical("canonical_inner_life_coordinates_invalid")
        trigger = request.trigger_message
        if trigger is None or not request.trigger_evidence:
            raise _technical("qq_attachment_perception_source_evidence_missing")
        # ``trigger_evidence.ref_id`` is the domain Observation identity, not a
        # committed event ID.  Capability evidence crosses the canonical
        # snapshot authority by the exact immutable event that carried that
        # Observation; opaque attachment refs stay only in the hashed payload.
        if trigger.event_ref != request.trigger_ref:
            raise _technical("qq_attachment_perception_trigger_event_mismatch")
        source_refs = (trigger.event_ref,)
        return world_id, actor_ref, logical_time, source_refs

    def _manifest(
        self,
        request: ModelInput,
        *,
        candidates: tuple[tuple[str, str, PerceptionInputDescriptor], ...],
        dispatched_count: int,
        source_refs: tuple[str, ...],
    ) -> _InteriorCapabilityManifest:
        trigger = request.trigger_message
        if trigger is None:
            raise _technical("perception_trigger_message_missing")
        attachments = [
            {
                "attachment_token": attachment_ref,
                "attachment_ref": attachment_ref,
                "media_type": media_type,
                "analysis_kind": descriptor.analysis_kind,
                "input_hash": descriptor.content_hash,
            }
            for attachment_ref, media_type, descriptor in candidates
        ]
        payload = {
            "contract": _CAPABILITY_CONTRACT,
            "offered_tokens": [item[0] for item in candidates],
            "attachments": attachments,
            "source_observation_ref": trigger.observation_ref,
            "source_event_ref": trigger.event_ref,
            "authorization": {
                "analysis_kind": "vision",
                "content_privacy_class": "private",
                "budget_account_id": self._budget_account_id,
                "budget_limit": self._budget_limit,
                "daily_limit": self._daily_limit,
                "daily_dispatched_count": dispatched_count,
            },
            "pinned_cursor": {
                "world_revision": request.evaluated_world_revision,
                "deliberation_revision": request.evaluated_deliberation_revision,
                "ledger_sequence": request.evaluated_ledger_sequence,
            },
        }
        payload_json = _canonical(payload)
        # Only committed trigger evidence crosses the projection authority.
        # Opaque attachment keys remain offered tokens in the hashed payload;
        # pretending those byte-store keys are ledger facts would create a
        # second source authority.
        try:
            return _InteriorCapabilityManifest(
                capability_ref=(
                    "qq-attachment-perception:"
                    + _digest(
                        {
                            "attempt": request.attempt_id,
                            "cursor": payload["pinned_cursor"],
                            "payload_hash": hashlib.sha256(payload_json.encode()).hexdigest(),
                        }
                    )
                ),
                capability_kind=_PURPOSE,
                payload_json=payload_json,
                payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
                source_refs=source_refs,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise _technical("qq_attachment_perception_capability_invalid") from exc

    def _opportunity(
        self,
        request: ModelInput,
        *,
        candidates: tuple[tuple[str, str, PerceptionInputDescriptor], ...],
        dispatched_count: int,
    ) -> InteriorOpportunity:
        world_id, actor_ref, logical_time, source_refs = self._opportunity_coordinates(request)
        manifest = self._manifest(
            request,
            candidates=candidates,
            dispatched_count=dispatched_count,
            source_refs=source_refs,
        )
        identity = {
            "attempt": request.attempt_id,
            "trigger": request.trigger_ref,
            "cursor": request.evaluated_ledger_sequence,
            "capability": manifest.capability_ref,
        }
        try:
            return InteriorOpportunity(
                opportunity_ref="opportunity:qq-attachment-perception:" + _digest(identity),
                inner_turn_ref="qq-attachment-perception:" + request.attempt_id,
                world_id=world_id,
                actor_ref=actor_ref,
                trigger_ref=request.trigger_ref,
                cursor=ProjectionCursor(
                    world_revision=request.evaluated_world_revision,
                    deliberation_revision=request.evaluated_deliberation_revision,
                    ledger_sequence=request.evaluated_ledger_sequence,
                ),
                logical_time=logical_time,
                purpose=_PURPOSE,
                source_refs=source_refs,
                capability_manifest=manifest,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise _technical("qq_attachment_perception_opportunity_invalid") from exc

    @staticmethod
    def _selected_attachment(
        decision: InnerDecision,
        *,
        opportunity: InteriorOpportunity,
        candidates: tuple[tuple[str, str, PerceptionInputDescriptor], ...],
    ) -> str:
        raw = decision.decision
        manifest = opportunity.capability_manifest
        if (
            not isinstance(raw, dict)
            or manifest is None
            or raw.get("contract") != _DECISION_CONTRACT
            or raw.get("purpose") != _PURPOSE
            or raw.get("capability_ref") != manifest.capability_ref
            or raw.get("capability_payload_hash") != manifest.payload_hash
        ):
            raise _technical("qq_attachment_perception_decision_contract_invalid")
        source_refs = raw.get("source_refs")
        payload = raw.get("payload")
        if (
            not isinstance(source_refs, list)
            or not source_refs
            or not isinstance(payload, dict)
            or payload.get("contract") != _PAYLOAD_CONTRACT
        ):
            raise _technical("qq_attachment_perception_decision_payload_invalid")
        allowed_sources = set(opportunity.source_refs) | set(manifest.source_refs)
        if any(not isinstance(item, str) or item not in allowed_sources for item in source_refs):
            raise _technical("qq_attachment_perception_decision_source_unpinned")
        selected = payload.get("selected_token")
        offered = {item[0] for item in candidates}
        if not isinstance(selected, str) or selected not in offered:
            raise _technical("qq_attachment_perception_selection_not_offered")
        return selected

    def _boundary_no_change(self, request: ModelInput, code: str) -> ModelOutput:
        return self._no_change(
            request,
            rationale=f"Perception capability boundary: {code}",
            identity_kind=code,
            model_id="character-interior-capability-boundary",
            model_version="character-interior-capability-boundary.1",
        )

    def _character_no_change(
        self,
        request: ModelInput,
        decision: InnerDecision,
        *,
        opportunity: InteriorOpportunity,
    ) -> ModelOutput:
        lineage = decision.author_lineage
        manifest = opportunity.capability_manifest
        if lineage is None or manifest is None:
            raise _technical("qq_attachment_perception_lineage_missing")
        return self._no_change(
            request,
            rationale=decision.summary or "Character-authored silence.",
            identity_kind=decision.inner_turn_id,
            model_id=lineage.model_id,
            model_version=lineage.model_version,
            character_interior_lineage=recorded_character_interior_lineage(
                decision,
                purpose=_PURPOSE,
                subject_ref=opportunity.opportunity_ref,
                capability_ref=manifest.capability_ref,
            ),
            winning_model_call_id=lineage.model_call_id,
            winning_request_hash=lineage.request_hash.removeprefix("sha256:"),
        )

    def _no_change(
        self,
        request: ModelInput,
        *,
        rationale: str,
        identity_kind: str,
        model_id: str,
        model_version: str,
        character_interior_lineage: object | None = None,
        winning_model_call_id: str | None = None,
        winning_request_hash: str | None = None,
    ) -> ModelOutput:
        proposal = DecisionProposal(
            proposal_id="proposal:perception:"
            + _digest(
                {
                    "capsule": request.capsule_id,
                    "attempt": request.attempt_id,
                    "kind": identity_kind,
                }
            ),
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=request.trigger_evidence,
            proposed_changes=(),
            action_intents=(),
            confidence=10_000,
            brief_rationale=rationale[:240],
            behavior_tendency="character_interior_terminal",
            stance="character_interior_terminal",
            display_strategy="private_capability",
        )
        return ModelOutput(
            model_id=model_id,
            model_version=model_version,
            raw_proposal=proposal.model_dump(mode="json"),
            character_interior_lineage=character_interior_lineage,
            winning_model_call_id=winning_model_call_id,
            winning_request_hash=winning_request_hash,
        )

    def _request_proposal(
        self,
        request: ModelInput,
        *,
        decision: InnerDecision,
        opportunity: InteriorOpportunity,
        attachment_ref: str,
    ) -> ModelOutput:
        identity = {
            "capsule": request.capsule_id,
            "attempt": request.attempt_id,
            "attachment": attachment_ref,
            "inner_turn": decision.inner_turn_id,
        }
        proposal_id = "proposal:perception:" + _digest(identity)
        change_id = "change:perception:" + _digest(identity)
        change = TypedChange(
            change_id=change_id,
            kind="perception_request",
            target_id="perception:vision",
            transition="request",
            evidence_refs=tuple(item.ref_id for item in request.trigger_evidence),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="perception_request.v1",
                value={
                    "analysis_kind": "vision",
                    "attachment_ref": attachment_ref,
                    "content_privacy_class": "private",
                    "budget_account_id": self._budget_account_id,
                    "budget_limit": self._budget_limit,
                },
            ),
        )
        proposal = DecisionProposal(
            proposal_id=proposal_id,
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=request.trigger_evidence,
            proposed_changes=(change,),
            action_intents=(
                ProposalActionIntent(
                    intent_id="intent:perception:" + _digest(identity),
                    kind="vision",
                    layer="perception_tool",
                    target="perception:vision",
                    payload_ref=perception_input_ref(
                        proposal_id=proposal_id,
                        change_id=change_id,
                    ),
                    payload_hash=("sha256:" + hashlib.sha256(attachment_ref.encode()).hexdigest()),
                    causal_change_id=change_id,
                ),
            ),
            confidence=10_000,
            brief_rationale=(decision.summary or "Character-authored capability choice")[:240],
            behavior_tendency="character_interior_decision",
            stance="character_interior_decision",
            display_strategy="private_capability",
        )
        lineage = decision.author_lineage
        manifest = opportunity.capability_manifest
        if lineage is None or manifest is None:
            raise _technical("qq_attachment_perception_lineage_missing")
        return ModelOutput(
            model_id=lineage.model_id,
            model_version=lineage.model_version,
            raw_proposal=proposal.model_dump(mode="json"),
            winning_model_call_id=lineage.model_call_id,
            winning_request_hash=lineage.request_hash.removeprefix("sha256:"),
            character_interior_lineage=recorded_character_interior_lineage(
                decision,
                purpose=_PURPOSE,
                subject_ref=opportunity.opportunity_ref,
                capability_ref=manifest.capability_ref,
            ),
        )


__all__: list[str] = []
