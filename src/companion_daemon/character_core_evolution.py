"""Rule-checked, evidence-backed evolution of mutable character tendencies."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal, Mapping


RULE_VERSION = "character-core-evolution-v1"
PROTECTED_FIELDS = frozenset({"id", "name", "kind", "location", "school", "background"})
MUTABLE_FIELDS = frozenset({"stable_traits", "values", "preferences", "speech_anchors"})


class CharacterCoreEvolutionError(ValueError):
    """A proposed core mutation violates an identity or evidence invariant."""


@dataclass(frozen=True)
class CoreChangeProposal:
    proposal_id: str
    operation: Literal["add_trait", "add_value", "add_preference", "add_speech_anchor", "replace"]
    field: str
    value: str
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class CoreChangeDecision:
    accepted: bool
    proposal_id: str
    field: str
    value: str
    evidence_ids: tuple[str, ...]
    reason: str
    updated_core: dict[str, object]
    rule_version: str = RULE_VERSION

    def event_payload(self) -> dict[str, object]:
        return {
            "proposal_id": self.proposal_id,
            "field": self.field,
            "value": self.value,
            "evidence_ids": list(self.evidence_ids),
            "accepted": self.accepted,
            "reason": self.reason,
            "rule_version": self.rule_version,
        }


def evaluate_core_change(
    current_core: Mapping[str, object],
    proposal: CoreChangeProposal,
    evidence_by_id: Mapping[str, Mapping[str, object]],
) -> CoreChangeDecision:
    """Accept only repeated coherent evidence or one significant settled result."""
    if proposal.field in PROTECTED_FIELDS or proposal.operation == "replace":
        raise CharacterCoreEvolutionError(f"protected character field cannot change: {proposal.field}")
    if proposal.field not in MUTABLE_FIELDS:
        raise CharacterCoreEvolutionError(f"unsupported mutable character field: {proposal.field}")
    expected_operation = {
        "stable_traits": "add_trait",
        "values": "add_value",
        "preferences": "add_preference",
        "speech_anchors": "add_speech_anchor",
    }[proposal.field]
    if proposal.operation != expected_operation:
        raise CharacterCoreEvolutionError("operation does not match mutable field")
    value = proposal.value.strip()
    if not value:
        raise CharacterCoreEvolutionError("core change value is required")
    if len(value) > 80:
        raise CharacterCoreEvolutionError("core change value is too long")
    if not proposal.proposal_id.strip() or not proposal.reason.strip():
        raise CharacterCoreEvolutionError("proposal id and reason are required")
    if len(set(proposal.evidence_ids)) != len(proposal.evidence_ids):
        raise CharacterCoreEvolutionError("evidence ids must be unique")

    evidence = [evidence_by_id.get(source_id) for source_id in proposal.evidence_ids]
    settled = [item for item in evidence if item and _is_settled(item)]
    significant = any(bool(item.get("significant")) for item in settled)
    signals = {str(item.get("core_signal") or "").strip() for item in settled}
    signals.discard("")
    coherent_repetition = len(settled) >= 3 and len(signals) == 1
    accepted = significant or coherent_repetition
    updated = json.loads(json.dumps(dict(current_core), ensure_ascii=False))
    reason = "evidence_threshold_met" if accepted else "insufficient_repeated_or_significant_evidence"
    if accepted:
        values = [str(item) for item in updated.get(proposal.field, [])]
        if value not in values:
            values.append(value)
        updated[proposal.field] = values[-8:]
    return CoreChangeDecision(
        accepted=accepted,
        proposal_id=proposal.proposal_id,
        field=proposal.field,
        value=value,
        evidence_ids=proposal.evidence_ids,
        reason=reason,
        updated_core=updated,
    )


def _is_settled(evidence: Mapping[str, object]) -> bool:
    source_type = str(evidence.get("source_type") or "")
    status = str(evidence.get("status") or "")
    return (
        source_type == "experience" and status == "committed"
    ) or (
        source_type in {"goal_outcome", "relationship_outcome"}
        and status in {"completed", "settled"}
    )

