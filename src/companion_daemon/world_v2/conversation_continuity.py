"""Deterministic conversational attention over source-bound dialogue.

The ledger remains the history authority.  This module only decides which
verified dialogue items deserve scarce working-context space for one turn.
It models three distinct signals instead of treating recency as continuity:
the current turn, unacknowledged counterpart messages, and older dialogue
whose language is reactivated by the current message.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re

from .recent_dialogue import RecentDialogueItem


_ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LOW_INFORMATION_CJK_UNITS = frozenset(
    {
        "今天",
        "现在",
        "事情",
        "这个",
        "那个",
        "后来",
        "还是",
        "可以",
        "觉得",
        "有点",
    }
)


def _semantic_units(text: str) -> frozenset[str]:
    """Return replay-stable lexical units without language-specific topics."""

    normalized = text.casefold()
    units = set(_ASCII_TOKEN.findall(normalized))
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            units.add(run)
            continue
        units.update(
            unit
            for index in range(len(run) - 1)
            if (unit := run[index : index + 2]) not in _LOW_INFORMATION_CJK_UNITS
        )
    return frozenset(units)


@dataclass(frozen=True, slots=True)
class ConversationContinuitySelection:
    dialogue: tuple[RecentDialogueItem, ...]
    rank_overrides: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class ContinuityRetrievalCandidate:
    """One source-bound Context item that can be reactivated by dialogue cues."""

    slice_name: str
    item_ref: str
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.slice_name not in {
            "open_threads",
            "relevant_facts",
            "active_memory_candidates",
        }:
            raise ValueError("continuity retrieval candidate has an unsupported slice")
        if not self.item_ref or not self.texts or any(not text for text in self.texts):
            raise ValueError("continuity retrieval candidate requires bounded source text")


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
        self._max_reactivated = max_reactivated_items
        self._max_companion = max_companion_items

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

        counterpart = tuple(item for item in ordered if item.speaker == "counterpart")
        units_by_id = {item.dialogue_id: _semantic_units(item.text) for item in counterpart}
        document_frequency = Counter(
            unit for units in units_by_id.values() for unit in units
        )
        current_units = units_by_id.get(current.dialogue_id, frozenset())
        candidates: list[tuple[float, RecentDialogueItem]] = []
        if current_units:
            document_count = max(1, len(counterpart))
            current_weight = sum(
                1.0 + math.log((document_count + 1) / (document_frequency[unit] + 1))
                for unit in current_units
            )
            for item in counterpart:
                if item.dialogue_id == current.dialogue_id:
                    continue
                shared = current_units & units_by_id[item.dialogue_id]
                if not shared:
                    continue
                shared_weight = sum(
                    1.0
                    + math.log((document_count + 1) / (document_frequency[unit] + 1))
                    for unit in shared
                )
                relatedness = shared_weight / max(1.0, current_weight)
                if relatedness >= 0.08:
                    candidates.append((relatedness, item))
        candidates.sort(
            key=lambda pair: (
                -pair[0],
                -pair[1].occurred_at.timestamp(),
                -pair[1].sequence,
                pair[1].dialogue_id,
            )
        )
        for relatedness, item in candidates[: self._max_reactivated]:
            retain(
                item,
                "topic_reactivation",
                min(9_700, 9_000 + int(relatedness * 700)),
            )

        for offset, item in enumerate(reversed(companions_before[-self._max_companion :])):
            retain(item, "recent_companion", 9_400 - offset * 50)

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
        cue_units = set(current_units)
        for item in selected_dialogue:
            if "pending_interaction" in item.continuity_reasons:
                cue_units.update(_semantic_units(item.text))
        rank_overrides: set[tuple[str, str]] = set()
        if cue_units and retrieval_candidates:
            candidate_units = {
                (candidate.slice_name, candidate.item_ref): frozenset(
                    unit
                    for text in candidate.texts
                    for unit in _semantic_units(text)
                )
                for candidate in retrieval_candidates
            }
            document_frequency = Counter(
                unit for units in candidate_units.values() for unit in units
            )
            document_count = max(1, len(candidate_units))
            cue_weight = sum(
                1.0 + math.log((document_count + 1) / (document_frequency[unit] + 1))
                for unit in cue_units
            )
            for identity, units in candidate_units.items():
                shared = cue_units & units
                shared_weight = sum(
                    1.0
                    + math.log((document_count + 1) / (document_frequency[unit] + 1))
                    for unit in shared
                )
                if shared_weight / max(1.0, cue_weight) >= 0.06:
                    rank_overrides.add(identity)
        return ConversationContinuitySelection(
            dialogue=selected_dialogue,
            rank_overrides=frozenset(rank_overrides),
        )


__all__ = [
    "ContinuityRetrievalCandidate",
    "ConversationContinuityCompiler",
    "ConversationContinuitySelection",
]
