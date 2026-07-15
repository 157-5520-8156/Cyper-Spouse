"""Frozen, offline-only Phase-8 World v2 scenario corpus.

The corpus is an engineering fixture, not a human-likeness judgement.  It
freezes the input/fact hashes and the scenario-family/emotion-gold coverage
needed by the later blinded evaluation protocol.  A separate reviewed-run
bundle remains necessary before claiming that World v2 is natural to people.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal

from .evaluation_artifacts import ScenarioCorpusEntry, corpus_digest


SCENARIO_CORPUS_VERSION = "world-v2-scenario-corpus.1"
TEST_ECONOMY_PROFILE_VERSION = "test-economy-v1"
SCENARIO_CORPUS_SIZE = 120
MINIMUM_EMOTION_GOLD_SIZE = 40


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """One independently runnable World v2 scenario-turn.

    ``fact_set`` is deliberately a bounded, immutable fixture rather than a
    synthetic claim that the runner has authored those facts into the world.
    The first vertical uses the truthful empty-precondition setup; richer
    multi-turn facts must be represented by a dedicated setup capability.
    """

    entry: ScenarioCorpusEntry
    user_text: str
    fact_set: tuple[tuple[str, str], ...]
    fault: Literal["none", "provider_failed", "duplicate_ingress"] = "none"

    def __post_init__(self) -> None:
        if not self.user_text.strip():
            raise ValueError("scenario user text must not be empty")
        if _hash(self.user_text) != self.entry.input_hash:
            raise ValueError("scenario text does not match its frozen input hash")
        if _hash(self.fact_set) != self.entry.fact_set_hash:
            raise ValueError("scenario fact set does not match its frozen fact hash")


_FAMILY_PROMPTS: tuple[tuple[str, str, bool, tuple[str, ...]], ...] = (
    ("ordinary_share", "今天路过一家小店，突然想起你。", False, ()),
    ("question_loop", "我刚刚说的你是不是没听进去？", False, ()),
    ("mild_disappointment", "算了，感觉你也没太在意。", True, ("disappointment_noticed", "give_space")),
    ("explicit_offence", "你怎么这么像个没脑子的客服。", True, ("offence_noticed", "boundary")),
    ("subtext_sarcasm", "你当然最会及时回复啦。", True, ("sarcasm_noticed", "repair_or_boundary")),
    ("hurt_residue", "没事，我已经习惯你上次那样了。", True, ("hurt_residue_noticed", "continuity")),
    ("distant_relationship", "不用管我啦，我们也没有那么熟。", True, ("distance_noticed", "give_space")),
    ("repair", "刚才我语气有点冲，对不起。", True, ("repair_noticed", "accept_or_hold_boundary")),
    ("npc_world_impact", "你今天和室友那件事后来怎么样了？", True, ("world_impact_noticed", "subjectivity")),
    ("plan_change", "我临时改主意了，今晚不去了。", False, ()),
    ("procrastination", "我拖到现在还没开始做，烦死了。", False, ()),
    ("reply_later", "你先忙，晚点再回我也行。", False, ()),
    ("interruption", "等等，我又想到一件更重要的事。", False, ()),
    ("multi_segment", "今天其实还行。\n但后来又有点难受。", False, ()),
    ("media_opportunity", "刚才的晚霞好漂亮，想给你看。", False, ()),
    ("provider_timeout", "这条消息如果没送到，就当我白说了。", False, ()),
    ("projection_gap", "你现在是在做什么，还是我根本看不到？", False, ()),
)


def _fault_for(*, family: str, ordinal: int) -> Literal["none", "provider_failed", "duplicate_ingress"]:
    if family == "provider_timeout":
        return "provider_failed"
    if family == "interruption" and ordinal == 1:
        return "duplicate_ingress"
    return "none"


def _build_cases() -> tuple[ScenarioCase, ...]:
    cases: list[ScenarioCase] = []
    # 17 families × 7 turns plus one ordinary share = 120.  The seven
    # emotion families below yield 49 emotional-gold turns, exceeding the
    # frozen Phase-8 lower bound without padding the corpus with chat only.
    family_counts = {family: 7 for family, _, _, _ in _FAMILY_PROMPTS}
    family_counts["ordinary_share"] = 8
    for family, prompt, emotional_gold, tags in _FAMILY_PROMPTS:
        for ordinal in range(1, family_counts[family] + 1):
            scenario_id = f"{family}.scenario"
            turn_id = f"{family}.{ordinal:02d}"
            text = prompt if ordinal == 1 else f"{prompt}（场景固定变体 {ordinal}）"
            facts: tuple[tuple[str, str], ...] = (("preconditions", "none"),)
            entry = ScenarioCorpusEntry(
                scenario_turn_id=turn_id,
                scenario_id=scenario_id,
                scenario_family=family,
                emotional_gold=emotional_gold,
                acceptable_response_tags=tags,
                input_hash=_hash(text),
                fact_set_hash=_hash(facts),
            )
            cases.append(
                ScenarioCase(
                    entry=entry,
                    user_text=text,
                    fact_set=facts,
                    fault=_fault_for(family=family, ordinal=ordinal),
                )
            )
    if len(cases) != SCENARIO_CORPUS_SIZE:
        raise RuntimeError("scenario corpus cardinality drifted")
    if sum(case.entry.emotional_gold for case in cases) < MINIMUM_EMOTION_GOLD_SIZE:
        raise RuntimeError("scenario corpus emotion-gold coverage drifted")
    return tuple(cases)


SCENARIO_CASES = _build_cases()
SCENARIO_CORPUS = tuple(case.entry for case in SCENARIO_CASES)
# This is intentionally checked by ``verify_frozen_scenario_corpus``.  Update
# it only with an explicit corpus-version bump and new evaluation baseline.
FROZEN_SCENARIO_CORPUS_HASH = "b8ab6c3cc22b4c5e43223a1b79d0d70a7b0191c00db568fafc68b9cd933b4000"


def verify_frozen_scenario_corpus() -> tuple[ScenarioCase, ...]:
    """Return the corpus only if its versioned membership has not drifted."""

    actual = corpus_digest(SCENARIO_CORPUS)
    if actual != FROZEN_SCENARIO_CORPUS_HASH:
        raise RuntimeError(
            "scenario corpus digest drifted; bump its version and establish a new evaluation baseline"
        )
    return SCENARIO_CASES


__all__ = [
    "FROZEN_SCENARIO_CORPUS_HASH",
    "MINIMUM_EMOTION_GOLD_SIZE",
    "SCENARIO_CASES",
    "SCENARIO_CORPUS",
    "SCENARIO_CORPUS_SIZE",
    "SCENARIO_CORPUS_VERSION",
    "ScenarioCase",
    "TEST_ECONOMY_PROFILE_VERSION",
    "verify_frozen_scenario_corpus",
]
