"""Deterministic conversational attention over source-bound dialogue.

The ledger remains the history authority.  This module only decides which
verified dialogue items deserve scarce working-context space for one turn.
It models three distinct signals instead of treating recency as continuity:
the current turn, unacknowledged counterpart messages, and older dialogue
whose language is reactivated by the current message.
"""

from __future__ import annotations

from dataclasses import dataclass

from .associative_recall import AssociativeRecallCandidate, AssociativeRecallCompiler
from .recent_dialogue import RecentDialogueItem


@dataclass(frozen=True, slots=True)
class ConversationContinuitySelection:
    dialogue: tuple[RecentDialogueItem, ...]
    rank_overrides: frozenset[tuple[str, str]] = frozenset()


ContinuityRetrievalCandidate = AssociativeRecallCandidate


class ConversationContinuityCompiler:
    """Select bounded working dialogue behind one small deterministic interface."""

    def __init__(
        self,
        *,
        max_items: int = 12,
        max_pending_items: int = 2,
        max_reactivated_items: int = 4,
        max_companion_items: int = 4,
    ) -> None:
        if not 4 <= max_items <= 16:
            raise ValueError("conversation continuity item budget is invalid")
        if not 1 <= max_pending_items <= 4:
            raise ValueError("conversation continuity pending budget is invalid")
        if not 1 <= max_reactivated_items <= 6:
            raise ValueError("conversation continuity reactivation budget is invalid")
        if not 0 <= max_companion_items <= 4:
            raise ValueError("conversation continuity companion budget is invalid")
        self._max_items = max_items
        self._max_pending = max_pending_items
        self._max_companion = max_companion_items
        self._associative_recall = AssociativeRecallCompiler(
            max_items=max_reactivated_items
        )

    def compile(
        self,
        *,
        dialogue: tuple[RecentDialogueItem, ...],
        trigger_ref: str,
        retrieval_candidates: tuple[ContinuityRetrievalCandidate, ...] = (),
    ) -> ConversationContinuitySelection:
        if not dialogue:
            return ConversationContinuitySelection(dialogue=())

        ordered = tuple(
            sorted(
                dialogue,
                key=lambda item: (item.occurred_at, item.sequence, item.dialogue_id),
            )
        )
        current = next(
            (
                item
                for item in reversed(ordered)
                if item.speaker == "counterpart"
                and any(
                    claim.authority_event_ref == trigger_ref for claim in item.source_claims
                )
            ),
            None,
        )
        if current is None:
            return ConversationContinuitySelection(
                dialogue=tuple(reversed(ordered[-self._max_items :]))
            )

        selected: dict[str, tuple[RecentDialogueItem, set[str], int]] = {}

        def retain(item: RecentDialogueItem, reason: str, score: int) -> None:
            existing = selected.get(item.dialogue_id)
            if existing is None:
                selected[item.dialogue_id] = (item, {reason}, score)
                return
            existing[1].add(reason)
            selected[item.dialogue_id] = (existing[0], existing[1], max(existing[2], score))

        retain(current, "current_turn", 10_000)
        companions_before = tuple(
            item
            for item in ordered
            if item.speaker == "companion"
            and (item.occurred_at, item.sequence) < (current.occurred_at, current.sequence)
        )
        acknowledged_event_refs = {
            ref
            for item in companions_before
            for ref in item.acknowledges_observation_event_refs
        }
        pending = tuple(
            item
            for item in ordered
            if item.speaker == "counterpart"
            and item.dialogue_id != current.dialogue_id
            and (item.occurred_at, item.sequence) < (current.occurred_at, current.sequence)
            and not any(
                claim.authority_event_ref in acknowledged_event_refs
                for claim in item.source_claims
            )
        )[-self._max_pending :]
        for offset, item in enumerate(reversed(pending)):
            retain(item, "pending_interaction", 9_850 - offset * 50)

        for offset, item in enumerate(reversed(companions_before[-self._max_companion :])):
            retain(item, "recent_companion", 9_400 - offset * 50)

        counterpart_by_source_ref = {
            claim.authority_event_ref: item
            for item in ordered
            if item.speaker == "counterpart"
            for claim in item.source_claims
        }
        recent_companions = companions_before[-self._max_companion :]
        for offset, companion in enumerate(reversed(recent_companions[-2:])):
            for acknowledged_ref in companion.acknowledges_observation_event_refs:
                acknowledged = counterpart_by_source_ref.get(acknowledged_ref)
                if acknowledged is not None:
                    retain(
                        acknowledged,
                        "acknowledged_context",
                        9_750 - offset * 50,
                    )

        for offset, item in enumerate(reversed(ordered)):
            if len(selected) >= self._max_items:
                break
            retain(item, "recent", max(1, 8_500 - offset * 25))

        ranked = sorted(
            selected.values(),
            key=lambda value: (
                -value[2],
                -value[0].occurred_at.timestamp(),
                -value[0].sequence,
                value[0].dialogue_id,
            ),
        )[: self._max_items]
        selected_dialogue = tuple(
            item.model_copy(
                update={"continuity_reasons": tuple(sorted(reasons))}
            )
            for item, reasons, _ in ranked
        )
        rank_overrides = self._associative_recall.compile(
            cue_text=current.text,
            candidates=retrieval_candidates,
        ).item_refs
        return ConversationContinuitySelection(
            dialogue=selected_dialogue,
            rank_overrides=rank_overrides,
        )


__all__ = [
    "ContinuityRetrievalCandidate",
    "ConversationContinuityCompiler",
    "ConversationContinuitySelection",
]
