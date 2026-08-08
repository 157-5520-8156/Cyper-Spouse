"""Typed CharacterInterior bridge for sourced external-attention opportunities.

The perception Hub owns acquisition, licensing, evidence windows and live
acceptance.  This bridge owns no provider and cannot recover a raw character
model from :class:`CharacterInterior`.  It offers one source-bound capability
to ``CharacterInterior.consider`` and translates the resulting typed decision
back to the existing coordinator contract.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import TypeAlias

from pydantic import ValidationError

from ..external_world_perception.contracts import (
    AuditedLiveCharacterAttentionResult,
    CharacterAttentionRequest,
    CharacterAttentionResult,
    CharacterAttentionTechnicalFailure,
    LiveCharacterAttentionRequest,
    LiveCharacterAttentionResult,
)
from ..external_perception_events import external_perception_value_hash
from ..proposal_audit_schemas import (
    ModelResultRecordedPayload,
    RecordedModelDecisionContext,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from ..schemas import ProjectionCursor
from .audit import recorded_character_interior_lineage
from .contracts import InnerDecision, InteriorOpportunity, _InteriorCapabilityManifest
from .core import CharacterInterior


_PURPOSE = "external_perception_attention"
_CAPABILITY_CONTRACT = "external-perception-attention-capability.1"
_DECISION_CONTRACT = "external-perception-attention-decision.1"

_AttentionRequest: TypeAlias = CharacterAttentionRequest | LiveCharacterAttentionRequest


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _technical(code: str) -> CharacterAttentionTechnicalFailure:
    return CharacterAttentionTechnicalFailure(code[:128])


def _opportunity_coordinates(
    request: _AttentionRequest,
) -> tuple[ProjectionCursor, datetime]:
    if isinstance(request, LiveCharacterAttentionRequest):
        return (
            request.current_context.pinned_world_cursor,
            request.current_context.world_logical_time,
        )
    try:
        raw_cursor = request.current_context.pinned_world_cursor.removeprefix("projection-cursor:")
        cursor = ProjectionCursor.model_validate_json(raw_cursor)
        logical_time = request.current_context.world_logical_time
    except (TypeError, ValueError, ValidationError) as exc:
        raise _technical("external_attention_coordinates_invalid") from exc
    if logical_time.tzinfo is None or logical_time.utcoffset() is None:
        raise _technical("external_attention_coordinates_invalid")
    return cursor, logical_time


def _capability_source_refs(request: _AttentionRequest) -> tuple[str, ...]:
    """Return only ledger-verifiable authority for the offered capability.

    Candidate/revision/draw/snapshot identifiers are sidecar identities.  The
    canonical manifest payload (and, in live mode, the complete durable
    snapshot hashes) binds those values, but they are not World event refs and
    therefore must never be presented to the production projection as ledger
    evidence.  Channel evidence is the committed authority proving that this
    actor may inspect the offered sources at the pinned cursor.
    """

    refs: list[str] = []
    for dossier in request.window.candidates:
        for channel in dossier.accessible_channels:
            refs.extend(channel.evidence_refs)
    normalized = tuple(dict.fromkeys(item for item in refs if item))
    if not normalized:
        raise _technical("external_attention_authority_sources_unavailable")
    if len(normalized) > 64:
        raise _technical("external_attention_source_closure_too_large")
    return normalized


def _manifest(request: _AttentionRequest) -> _InteriorCapabilityManifest:
    payload = {
        "contract": _CAPABILITY_CONTRACT,
        "deployment_mode": request.window.deployment_mode,
        "attention_attempt_id": request.attention_attempt_id,
        "retry_ordinal": request.retry_ordinal,
        "window_id": request.window.window_id,
        "opportunity_id": request.window.opportunity_id,
        "attention_policy_revision": request.window.attention_policy_revision,
        "deployment_mode_revision": request.window.deployment_mode_revision,
        "generated_at": request.window.generated_at.isoformat(),
        "expires_at": request.window.expires_at.isoformat(),
        "candidate_set_hash": request.window.candidate_set_hash,
        "exposure_draw_ref": request.window.exposure_draw_ref,
        "candidates": [
            {
                "candidate_token": item.candidate_ref,
                **item.model_dump(mode="json"),
            }
            for item in request.window.candidates
        ],
        **(
            {
                "durable_snapshots": [
                    item.model_dump(mode="json") for item in request.window.durable_snapshots
                ]
            }
            if isinstance(request, LiveCharacterAttentionRequest)
            else {}
        ),
        "pinned_cursor": _opportunity_coordinates(request)[0].model_dump(mode="json"),
    }
    payload_json = _canonical(payload)
    try:
        return _InteriorCapabilityManifest(
            capability_ref=(
                f"external-perception-attention:{request.attention_attempt_id}:"
                f"retry:{request.retry_ordinal}"
            ),
            capability_kind=_PURPOSE,
            payload_json=payload_json,
            payload_hash="sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
            source_refs=_capability_source_refs(request),
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise _technical("external_attention_capability_invalid") from exc


def _opportunity(request: _AttentionRequest) -> InteriorOpportunity:
    cursor, logical_time = _opportunity_coordinates(request)
    manifest = _manifest(request)
    source_refs = manifest.source_refs
    try:
        return InteriorOpportunity(
            opportunity_ref=request.window.opportunity_id,
            inner_turn_ref=(
                f"external-attention:{request.attention_attempt_id}:retry:{request.retry_ordinal}"
            ),
            world_id=request.window.world_id,
            actor_ref=request.window.actor_ref,
            trigger_ref=request.attention_attempt_id,
            cursor=cursor,
            logical_time=logical_time,
            purpose=_PURPOSE,
            source_refs=source_refs,
            capability_manifest=manifest,
        )
    except (ValidationError, TypeError, ValueError) as exc:
        raise _technical("external_attention_opportunity_invalid") from exc


def _decision_payload(raw: object) -> dict[str, object]:
    if (
        not isinstance(raw, dict)
        or raw.get("contract") != "character-interior-purpose-decision.1"
        or raw.get("purpose") != _PURPOSE
    ):
        raise _technical("external_attention_decision_contract_invalid")
    payload = raw.get("payload")
    if not isinstance(payload, dict) or payload.get("contract") != _DECISION_CONTRACT:
        raise _technical("external_attention_decision_contract_invalid")
    result = payload.get("selections")
    if not isinstance(result, (tuple, list)):
        raise _technical("external_attention_decision_result_missing")
    return payload


def _bare_hash(value: str) -> str:
    return value.removeprefix("sha256:")


def _live_model_result(
    *,
    request: LiveCharacterAttentionRequest,
    result: LiveCharacterAttentionResult,
    decision: InnerDecision,
) -> ModelResultRecordedPayload:
    lineage = getattr(decision, "author_lineage", None)
    if lineage is None:
        raise _technical("external_attention_author_lineage_missing")
    decision_value = result.model_dump(mode="json")
    opportunity = _opportunity(request)
    capability = opportunity.capability_manifest
    if capability is None:
        raise _technical("external_attention_capability_lineage_missing")
    decision_digest = external_perception_value_hash(decision_value)
    proposal_hash = "sha256:" + decision_digest
    response_hash = _bare_hash(lineage.response_hash)
    request_hash = _bare_hash(lineage.request_hash)
    model_result_ref = "model-result:" + sha256(
        canonical_json(
            {
                "model_call_id": lineage.model_call_id,
                "response_hash": response_hash,
            }
        )
    )
    audit = RecordedModelResultAudit(
        model_call_id=lineage.model_call_id,
        parent_model_call_id=lineage.parent_model_call_id,
        model_result_ref=model_result_ref,
        attempt_id=request.attention_attempt_id,
        route=RecordedModelRoute(
            tier="flash",
            reason_code="external_perception.character_attention",
            router_version="character-interior.1",
        ),
        model_id=lineage.model_id,
        model_version=lineage.model_version,
        request_hash=request_hash,
        response_hash=response_hash,
        decision_context=RecordedModelDecisionContext(
            decision_subject_hash=decision_digest,
            world_revision=request.window.pinned_world_cursor.world_revision,
            deliberation_revision=(request.window.pinned_world_cursor.deliberation_revision),
            ledger_sequence=request.window.pinned_world_cursor.ledger_sequence,
        ),
        character_interior_lineage=recorded_character_interior_lineage(
            decision,
            purpose=_PURPOSE,
            subject_ref=decision.opportunity_ref,
            capability_ref=capability.capability_ref,
        ),
        status="proposal_validated",
    )
    audit_json = model_audit_json(audit)
    identity = {
        "capsule_id": request.window.candidate_set_hash,
        "proposal_hash": proposal_hash,
        "attempt_audits": [json.loads(audit_json)],
    }
    return ModelResultRecordedPayload(
        audit_contract="model-result-audit.7",
        model_result_ref=model_result_ref,
        deliberation_result_id=f"deliberation:{sha256(canonical_json(identity))}",
        proposal_hash=proposal_hash,
        model_call_id=lineage.model_call_id,
        attempt_id=request.attention_attempt_id,
        capsule_id=request.window.candidate_set_hash,
        trigger_ref=request.attention_attempt_id,
        evaluated_world_revision=request.window.pinned_world_cursor.world_revision,
        attempt_index=lineage.attempt_ordinal,
        attempt_count=lineage.attempt_ordinal + 1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )


class _CharacterInteriorAttentionBase:
    model_id = "character-interior"

    def __init__(self, interior: CharacterInterior) -> None:
        self._interior = interior

    async def _consider(self, request: _AttentionRequest):
        # Semantic/shape correction belongs to the same CharacterInterior
        # author call.  Reaching the old coordinator reselection ordinal must
        # never open a second provider lane or a second character choice.
        if request.selection_ordinal != 0:
            raise _technical("reselection_owned_by_character_interior")
        opportunity = _opportunity(request)
        decision = await self._interior.consider(opportunity)
        if decision.status == "technical_failure":
            raise _technical(decision.failure_code or "character_interior_failure")
        if (
            decision.snapshot_id is None
            or decision.snapshot_hash is None
            or decision.cursor != opportunity.cursor
            or decision.opportunity_ref != opportunity.opportunity_ref
            or decision.actor_ref != opportunity.actor_ref
        ):
            raise _technical("character_interior_snapshot_mismatch")
        if decision.status == "model_silent":
            return None
        return _decision_payload(decision.decision), decision


class CharacterInteriorShadowAttentionPort(_CharacterInteriorAttentionBase):
    """Shadow attention through the sole character decision entry point."""

    async def consider_attention(self, request: CharacterAttentionRequest) -> object:
        considered = await self._consider(request)
        if considered is None:
            return CharacterAttentionResult(selections=())
        payload, _decision = considered
        try:
            return CharacterAttentionResult.model_validate_json(
                _canonical({"selections": payload["selections"]})
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise _technical("external_attention_decision_result_invalid") from exc


class CharacterInteriorLiveAttentionPort(_CharacterInteriorAttentionBase):
    """Live attention through CharacterInterior plus the existing audit boundary."""

    async def consider_attention(self, request: LiveCharacterAttentionRequest) -> object:
        considered = await self._consider(request)
        if considered is None:
            # A live no-selection is still a real provider result and must be
            # auditable.  The same-author Faculty should therefore encode an
            # empty selections decision; the bridge derives the immutable
            # audit from its trusted author lineage, never from model claims.
            raise _technical("live_model_silence_missing_audit")
        payload, decision = considered
        try:
            result = LiveCharacterAttentionResult.model_validate_json(
                _canonical({"selections": payload["selections"]})
            )
            return AuditedLiveCharacterAttentionResult(
                decision=result,
                model_result=_live_model_result(
                    request=request,
                    result=result,
                    decision=decision,
                ),
            )
        except (ValidationError, TypeError, ValueError) as exc:
            raise _technical("external_attention_live_audit_invalid") from exc


def character_interior_shadow_attention_port(
    interior: CharacterInterior,
) -> CharacterInteriorShadowAttentionPort:
    return CharacterInteriorShadowAttentionPort(interior)


def character_interior_live_attention_port(
    interior: CharacterInterior,
) -> CharacterInteriorLiveAttentionPort:
    return CharacterInteriorLiveAttentionPort(interior)


__all__: list[str] = []
