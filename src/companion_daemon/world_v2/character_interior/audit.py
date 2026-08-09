"""Durable audit material for one completed CharacterInterior turn."""

from __future__ import annotations

import hashlib
import json

from ..proposal_audit_schemas import (
    ModelResultRecordedPayload,
    RecordedCharacterInteriorTurnLineage,
    RecordedModelDecisionContext,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from ..schema_core import canonicalize_json_value
from .contracts import InnerDecision, InnerTransition
from .run_result import CausalOpportunityIdentity


def _canonical(value: object) -> str:
    return json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def causal_opportunity_lineage_fields(
    identity: CausalOpportunityIdentity,
    *,
    subject_ref: str,
) -> dict[str, object]:
    """Return the durable identity fields for a CharacterInterior audit."""

    if subject_ref != identity.opportunity_ref:
        raise ValueError("CharacterInterior subject is not the causal opportunity")
    return {
        "causal_world_id": identity.world_id,
        "causal_source_refs": identity.source_refs,
        "causal_epoch": identity.epoch,
        "causal_actor_ref": identity.actor_ref,
        "causal_contract_version": identity.contract_version,
    }


def recorded_character_interior_lineage(
    result: InnerDecision | InnerTransition,
    *,
    purpose: str,
    subject_ref: str,
    capability_ref: str,
    causal_opportunity: CausalOpportunityIdentity | None = None,
) -> RecordedCharacterInteriorTurnLineage:
    """Close a successful turn over its snapshot, author and private state.

    The returned record is inert audit evidence.  Domain authority continues
    to come only from the typed proposal and its Acceptance runtime.
    """

    author = result.author_lineage
    private = result.private_self_lineage
    if (
        result.status == "technical_failure"
        or result.snapshot_id is None
        or result.snapshot_hash is None
        or author is None
        or private is None
    ):
        raise ValueError("successful CharacterInterior turn lacks durable lineage")
    causal_fields = (
        causal_opportunity_lineage_fields(causal_opportunity, subject_ref=subject_ref)
        if causal_opportunity is not None
        else {}
    )
    return RecordedCharacterInteriorTurnLineage(
        inner_turn_id=result.inner_turn_id,
        purpose=purpose,
        opportunity_ref=subject_ref,
        **causal_fields,
        snapshot_id=result.snapshot_id,
        snapshot_hash=result.snapshot_hash,
        capability_ref=capability_ref,
        author_model_id=author.model_id,
        author_model_version=author.model_version,
        author_model_call_id=author.model_call_id,
        author_request_hash=author.request_hash,
        author_response_hash=author.response_hash,
        author_attempt_ordinal=author.attempt_ordinal,
        author_parent_model_call_id=author.parent_model_call_id,
        private_self_lineage_hash=_sha256(private.model_dump(mode="json")),
        decision_hash=_sha256(result.model_dump(mode="json")),
    )


def recorded_character_interior_model_result(
    result: InnerDecision | InnerTransition,
    *,
    purpose: str,
    subject_ref: str,
    trigger_ref: str,
    capability_ref: str,
    route_tier: str,
    route_reason_code: str,
    router_version: str,
    proposal_hash: str | None = None,
    causal_opportunity: CausalOpportunityIdentity | None = None,
) -> ModelResultRecordedPayload:
    """Materialize the canonical durable audit for one completed inner turn.

    Callers may embed this immutable payload in their own typed decision event
    when emitting a second ``ModelResultRecorded`` event would split one
    effect-once/CAS boundary.  Historical domain codecs keep this field
    optional, while every new CharacterInterior call site is expected to
    persist the returned ``model-result-audit.7`` bytes.
    """

    lineage = recorded_character_interior_lineage(
        result,
        purpose=purpose,
        subject_ref=subject_ref,
        capability_ref=capability_ref,
        causal_opportunity=causal_opportunity,
    )
    author = result.author_lineage
    if author is None or result.snapshot_hash is None:
        raise ValueError("successful CharacterInterior turn lacks model-result lineage")
    response_hash = author.response_hash.removeprefix("sha256:")
    request_hash = author.request_hash.removeprefix("sha256:")
    model_result_ref = "model-result:" + sha256(
        canonical_json(
            {
                "model_call_id": author.model_call_id,
                "response_hash": response_hash,
            }
        )
    )
    audit = RecordedModelResultAudit(
        model_call_id=author.model_call_id,
        parent_model_call_id=author.parent_model_call_id,
        model_result_ref=model_result_ref,
        attempt_id=result.inner_turn_id,
        route=RecordedModelRoute(
            tier=route_tier,
            reason_code=route_reason_code,
            router_version=router_version,
        ),
        model_id=author.model_id,
        model_version=author.model_version,
        attempted_model_id=author.model_id,
        attempted_model_version=author.model_version,
        request_hash=request_hash,
        response_hash=response_hash,
        decision_context=RecordedModelDecisionContext(
            decision_subject_hash=lineage.decision_hash.removeprefix("sha256:"),
            world_revision=result.cursor.world_revision,
            deliberation_revision=result.cursor.deliberation_revision,
            ledger_sequence=result.cursor.ledger_sequence,
        ),
        character_interior_lineage=lineage,
        presented_prefetch_traces=result.presented_prefetch_traces,
        status="proposal_validated",
    )
    audit_json = model_audit_json(audit)
    result_identity = {
        "capsule_id": result.snapshot_hash,
        "proposal_hash": proposal_hash,
        "attempt_audits": [json.loads(audit_json)],
    }
    return ModelResultRecordedPayload(
        audit_contract="model-result-audit.7",
        model_result_ref=model_result_ref,
        deliberation_result_id="deliberation:" + sha256(canonical_json(result_identity)),
        proposal_hash=proposal_hash,
        model_call_id=author.model_call_id,
        parent_model_call_id=author.parent_model_call_id,
        attempt_id=result.inner_turn_id,
        capsule_id=result.snapshot_hash,
        trigger_ref=trigger_ref,
        evaluated_world_revision=result.cursor.world_revision,
        # CharacterInterior returns the final same-author result as one
        # durable unit. Correction ordinal/parent remain in the nested
        # lineage; the outer payload must not claim an unpersisted attempt.
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )


__all__ = [
    "causal_opportunity_lineage_fields",
    "recorded_character_interior_lineage",
    "recorded_character_interior_model_result",
]
