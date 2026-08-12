"""Exact authority coordinates for one effect-free audited change terminal."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from .proposal_audit_schemas import ProposalAuditProjection
from .proposal_envelope import (
    DecisionProposal,
    RelationshipCommitmentPayload,
    TypedChange,
    validate_proposal_envelope,
)
from .relationship_reducers import (
    RELATIONSHIP_COMMITMENT_STAGE_TRANSITIONS,
    RELATIONSHIP_POLICY_DIGEST,
    relationship_primary_id,
)
from .schemas import RelationshipStateProjection


AUDITED_CHANGE_TERMINAL_ADVISORY_KIND = "typed_change_terminal"
RELATIONSHIP_COMMITMENT_TERMINAL_REASON = (
    "relationship_proposal_compiler.commitment_stage_transition_not_installed"
)
AuditedChangeTerminalStatus = Literal["rejected", "stale"]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def terminal_relationship_commitment_payload(
    change: TypedChange,
) -> RelationshipCommitmentPayload:
    """Resolve the sole typed-change family owned by this terminal contract."""

    if (
        change.kind != "relationship_commitment"
        or change.transition != "commit"
        or change.payload.payload_schema != "relationship_commitment.v1"
    ):
        raise ValueError(
            "audited change terminal requires one relationship commitment"
        )
    return RelationshipCommitmentPayload.model_validate(
        change.payload.value(),
        strict=True,
    )


def validate_relationship_commitment_terminal_state(
    *,
    change: TypedChange,
    relationship_states: tuple[RelationshipStateProjection, ...],
) -> None:
    """Re-prove that the installed policy cannot compile this exact target."""

    authored = terminal_relationship_commitment_payload(change)
    matches = tuple(
        item
        for item in relationship_states
        if item.subject_ref == authored.subject_ref
    )
    if len(matches) > 1:
        raise ValueError("audited change terminal relationship state is ambiguous")
    if matches:
        current = matches[0]
        if (
            current.relationship_id
            != relationship_primary_id(subject_ref=authored.subject_ref)
            or current.policy_version != "relationship-policy.1"
            or current.policy_digest != RELATIONSHIP_POLICY_DIGEST
        ):
            raise ValueError(
                "audited change terminal relationship state policy is not installed"
            )
        stage_before = current.stage
    else:
        stage_before = "stranger"
    if authored.target_stage in RELATIONSHIP_COMMITMENT_STAGE_TRANSITIONS.get(
        stage_before,
        frozenset(),
    ):
        raise ValueError(
            "audited change terminal relationship transition is installed"
        )


def audited_change_authority_fingerprint(
    *,
    audit: ProposalAuditProjection,
    change: TypedChange,
) -> str:
    """Bind a source audit and one complete typed change, not its envelope peers."""

    terminal_relationship_commitment_payload(change)
    return _digest(
        {
            "contract": "audited-typed-change-authority.1",
            "proposal_event_ref": audit.event_ref,
            "proposal_event_payload_hash": audit.event_payload_hash,
            "change": change.model_dump(mode="json"),
        }
    )


def audited_change_terminal_proposal_id(
    *,
    audit: ProposalAuditProjection,
    change: TypedChange,
) -> str:
    return "proposal:audited-typed-change:" + audited_change_authority_fingerprint(
        audit=audit,
        change=change,
    )


def audited_change_terminal_event_id(
    *,
    audit: ProposalAuditProjection,
    change: TypedChange,
) -> str:
    return "event:audited-typed-change-terminal:" + _digest(
        {
            "contract": "audited-typed-change-terminal-event.1",
            "proposal_event_ref": audit.event_ref,
            "derived_proposal_id": audited_change_terminal_proposal_id(
                audit=audit,
                change=change,
            ),
        }
    )


def audited_change_terminal_payload(
    *,
    audit: ProposalAuditProjection,
    change: TypedChange,
    status: AuditedChangeTerminalStatus,
) -> dict[str, str]:
    return {
        "proposal_id": audited_change_terminal_proposal_id(
            audit=audit,
            change=change,
        ),
        "source_event_ref": audit.event_ref,
        "advisory_kind": AUDITED_CHANGE_TERMINAL_ADVISORY_KIND,
        "stage": status,
        "reason_code": RELATIONSHIP_COMMITMENT_TERMINAL_REASON,
        "failure_fingerprint": audited_change_authority_fingerprint(
            audit=audit,
            change=change,
        ),
    }


def validate_audited_change_terminal_payload(
    *,
    payload: dict[str, object],
    audit: ProposalAuditProjection,
    current_world_revision: int,
) -> TypedChange:
    """Re-resolve exactly one typed change from immutable ProposalAudit bytes."""

    if payload.get("reason_code") != RELATIONSHIP_COMMITMENT_TERMINAL_REASON:
        raise ValueError("audited change terminal reason is not installed")
    if (
        payload.get("advisory_kind") != AUDITED_CHANGE_TERMINAL_ADVISORY_KIND
        or payload.get("source_event_ref") != audit.event_ref
        or payload.get("stage") not in {"rejected", "stale"}
    ):
        raise ValueError("audited change terminal coordinates are invalid")
    try:
        proposal = validate_proposal_envelope(json.loads(audit.proposal_json))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("audited change terminal source proposal is invalid") from exc
    if not isinstance(proposal, DecisionProposal):
        raise ValueError("audited change terminal source is not a decision proposal")
    matches = tuple(
        change
        for change in proposal.proposed_changes
        if change.kind == "relationship_commitment"
        and change.transition == "commit"
        and payload.get("proposal_id")
        == audited_change_terminal_proposal_id(audit=audit, change=change)
        and payload.get("failure_fingerprint")
        == audited_change_authority_fingerprint(audit=audit, change=change)
    )
    if len(matches) != 1:
        raise ValueError("audited change terminal does not bind one exact typed change")
    status = payload["stage"]
    if status == "rejected" and audit.evaluated_world_revision != current_world_revision:
        raise ValueError("rejected audited change must evaluate the current world")
    if status == "stale" and audit.evaluated_world_revision >= current_world_revision:
        raise ValueError("stale audited change must evaluate an older world revision")
    return matches[0]


__all__ = [
    "AUDITED_CHANGE_TERMINAL_ADVISORY_KIND",
    "AuditedChangeTerminalStatus",
    "RELATIONSHIP_COMMITMENT_TERMINAL_REASON",
    "audited_change_authority_fingerprint",
    "audited_change_terminal_event_id",
    "audited_change_terminal_payload",
    "audited_change_terminal_proposal_id",
    "terminal_relationship_commitment_payload",
    "validate_relationship_commitment_terminal_state",
    "validate_audited_change_terminal_payload",
]
