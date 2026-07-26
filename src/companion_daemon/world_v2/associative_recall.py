"""Replay-stable lexical prefetch over already source-bound Context candidates.

This is the first, deliberately narrow Recall Index adapter.  It never reads
the ledger, invents semantic labels, or recommends behavior.  Its input has
already passed cursor, privacy, supersession, and source-proof checks; it only
selects a small evidence set whose surface form is reactivated by the current
message.  Dense, temporal-query, and structured-link adapters can later replace
or join it behind the same compiler seam.
"""

from __future__ import annotations

from dataclasses import dataclass
import unicodedata


ASSOCIATIVE_PREFETCH_POLICY_VERSION = "associative-prefetch.lexical-ngram.1"


@dataclass(frozen=True, slots=True)
class AssociativeRecallCandidate:
    """One source-bound Context item eligible for non-authoritative prefetch."""

    slice_name: str
    item_ref: str
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.slice_name not in {
            "open_threads",
            "relevant_facts",
            "active_memory_candidates",
        }:
            raise ValueError("associative recall candidate has an unsupported slice")
        if not self.item_ref or not self.texts or any(not text for text in self.texts):
            raise ValueError("associative recall candidate requires bounded source text")


@dataclass(frozen=True, slots=True)
class AssociativeRecallSelection:
    """Stable item identities to promote inside the existing Context budget."""

    item_refs: frozenset[tuple[str, str]]


def _flush_feature_run(
    features: set[str],
    *,
    run: list[str],
    kind: str | None,
) -> None:
    if not run or kind is None:
        return
    value = "".join(run)
    if kind == "cjk":
        # Character n-grams avoid pretending whitespace is a word boundary in
        # Chinese/Japanese text.  Two-grams preserve exact short names while
        # three-grams provide the specificity threshold used below.
        for width in (2, 3):
            features.update(
                value[offset : offset + width]
                for offset in range(max(0, len(value) - width + 1))
            )
        return
    if len(value) >= 3:
        features.add(value)


def _recall_features(texts: tuple[str, ...]) -> frozenset[str]:
    """Build language-light lexical cues without interpreting their meaning."""

    features: set[str] = set()
    for raw in texts:
        text = unicodedata.normalize("NFKC", raw).casefold()
        run: list[str] = []
        kind: str | None = None
        for character in text:
            codepoint = ord(character)
            next_kind = (
                "cjk"
                if (
                    0x3400 <= codepoint <= 0x4DBF
                    or 0x4E00 <= codepoint <= 0x9FFF
                    or 0x3040 <= codepoint <= 0x30FF
                )
                else "word"
                if character.isalnum()
                else None
            )
            if next_kind != kind:
                _flush_feature_run(features, run=run, kind=kind)
                run = []
                kind = next_kind
            if next_kind is not None:
                run.append(character)
        _flush_feature_run(features, run=run, kind=kind)
    return frozenset(features)


class AssociativeRecallCompiler:
    """Select a bounded evidence-only prefetch set behind one small Interface."""

    def __init__(self, *, max_items: int = 4) -> None:
        if not 1 <= max_items <= 8:
            raise ValueError("associative recall item budget is invalid")
        self._max_items = max_items

    def compile(
        self,
        *,
        cue_text: str,
        candidates: tuple[AssociativeRecallCandidate, ...],
    ) -> AssociativeRecallSelection:
        cue_features = _recall_features((cue_text,))
        ranked: list[tuple[int, str, str]] = []
        for candidate in candidates:
            overlap = cue_features & _recall_features(candidate.texts)
            # One generic two-character collision ("今天", for example) is
            # not enough to spend scarce working-context space.  A longer
            # exact cue or two independently overlapping n-grams is a
            # high-precision first phase; dense association can later widen
            # recall behind this same Interface.
            if not overlap or (
                not any(len(feature) >= 3 for feature in overlap)
                and len(overlap) < 2
            ):
                continue
            score = sum(len(feature) * len(feature) for feature in overlap)
            ranked.append((score, candidate.slice_name, candidate.item_ref))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        return AssociativeRecallSelection(
            item_refs=frozenset(
                (slice_name, item_ref)
                for _, slice_name, item_ref in ranked[: self._max_items]
            )
        )


__all__ = [
    "ASSOCIATIVE_PREFETCH_POLICY_VERSION",
    "AssociativeRecallCandidate",
    "AssociativeRecallCompiler",
    "AssociativeRecallSelection",
]
