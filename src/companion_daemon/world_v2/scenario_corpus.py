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


SCENARIO_CORPUS_VERSION = "world-v2-scenario-corpus.3"
TEST_ECONOMY_PROFILE_VERSION = "test-economy-v1"
SCENARIO_CORPUS_SIZE = 120
MINIMUM_EMOTION_GOLD_SIZE = 40
ScenarioFault = Literal[
    "none", "provider_failed", "provider_unknown", "duplicate_ingress", "restart_before_dispatch"
]
ScenarioExecution = Literal[
    "chat",
    "interruption",
    "seeded_world_outcome",
    "seeded_world_outcome_affect",
    "seeded_activity_plan",
    "seeded_expression_delay",
]


@dataclass(frozen=True, slots=True)
class ScenarioTurnStep:
    """One immutable user turn in a seeded offline fixture.

    The corpus deliberately stores the whole script instead of using a family
    label as a proxy for history.  A runner therefore cannot silently turn a
    plan/interruption/world scenario back into an isolated chat ingress.
    """

    step_id: str
    text: str

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.text.strip():
            raise ValueError("scenario turn step requires an id and text")


def _hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScenarioCase:
    """One independently runnable World v2 scenario fixture.

    ``fact_set`` is deliberately a bounded, immutable fixture rather than a
    synthetic claim that the runner has authored those facts into the world.
    The first vertical uses the truthful empty-precondition setup; richer
    multi-turn facts must be represented by a dedicated setup capability.
    """

    entry: ScenarioCorpusEntry
    user_text: str
    fact_set: tuple[tuple[str, str], ...]
    fault: ScenarioFault = "none"
    turns: tuple[ScenarioTurnStep, ...] = ()
    execution: ScenarioExecution = "chat"
    required_event_types: tuple[str, ...] = ()
    forbidden_event_types: tuple[str, ...] = ()
    required_trigger_kinds: tuple[str, ...] = ()
    forbidden_room_view_values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.user_text.strip():
            raise ValueError("scenario user text must not be empty")
        if _hash(self.user_text) != self.entry.input_hash:
            raise ValueError("scenario text does not match its frozen input hash")
        if _hash(self.fact_set) != self.entry.fact_set_hash:
            raise ValueError("scenario fact set does not match its frozen fact hash")
        turns = self.turns or (ScenarioTurnStep(step_id="turn.01", text=self.user_text),)
        if turns[0].text != self.user_text:
            raise ValueError("first scripted scenario turn must bind entry input text")
        if len({item.step_id for item in turns}) != len(turns):
            raise ValueError("scenario turn step ids must be unique")
        if self.execution == "interruption" and len(turns) < 2:
            raise ValueError("interruption fixture requires at least two turns")
        if self.execution in {"seeded_world_outcome", "seeded_world_outcome_affect"} and len(turns) < 2:
            raise ValueError("seeded outcome fixture requires a follow-up turn")
        if self.execution == "seeded_expression_delay" and len(turns) < 2:
            raise ValueError("seeded delayed expression fixture requires an interruption turn")
        if set(self.required_event_types).intersection(self.forbidden_event_types):
            raise ValueError("scenario cannot require and forbid the same event")
        if any(not value for value in self.forbidden_room_view_values):
            raise ValueError("forbidden room view values must be non-empty")
        object.__setattr__(self, "turns", turns)


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


def _script_for(
    *, family: str, ordinal: int, prompt: str
) -> tuple[
    tuple[ScenarioTurnStep, ...],
    ScenarioExecution,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Return the explicit mechanism script for the non-chat seed of a family.

    Remaining corpus members stay intentionally small one-turn controls.  The
    first member of these families is a *real*, multi-turn regression fixture,
    not a wording variant carrying an aspirational label.
    """

    if ordinal != 1:
        return (), "chat", (), (), ()
    if family == "npc_world_impact":
        return (
            (
                ScenarioTurnStep("turn.01", prompt),
                ScenarioTurnStep("turn.02", "那件事后来有变化吗？你还会在意吗？"),
            ),
            "seeded_world_outcome_affect",
            (
                "WorldOccurrenceCommitted",
                "OutcomeObservationRecorded",
                "WorldOccurrenceSettled",
                "AppraisalAccepted",
                "AffectEpisodeOpened",
                "ActionAuthorized",
            ),
            (),
            ("outcome_deliberation", "npc_world_appraisal", "affect_deliberation"),
        )
    if family == "plan_change":
        return (
            (
                ScenarioTurnStep("turn.01", "我们原本说好明天一起去看展。"),
                ScenarioTurnStep("turn.02", prompt),
            ),
            "seeded_activity_plan",
            ("ActivityPlanned", "ExpressionPlanAccepted", "ExpressionBeatAuthorized"),
            (),
            (),
        )
    if family == "reply_later":
        return (
            (
                ScenarioTurnStep("turn.01", prompt),
                ScenarioTurnStep("turn.02", "我回来啦，刚才那件事你还记得吗？"),
            ),
            "seeded_expression_delay",
            (
                "ExpressionPlanAccepted",
                "ExpressionBeatAuthorized",
                "ActionScheduled",
                "ClockAdvanced",
                "ExpressionBeatSettled",
                "ExpressionPlanCompleted",
            ),
            (),
            ("expression_reconsideration",),
        )
    if family == "interruption":
        return (
            (
                ScenarioTurnStep("turn.01", "我本来想慢慢和你说今天的事。"),
                ScenarioTurnStep("turn.02", prompt),
            ),
            "interruption",
            ("TriggerProcessOpened", "ActionAuthorized"),
            (),
            ("expression_reconsideration",),
        )
    if family == "media_opportunity":
        return (
            (
                ScenarioTurnStep("turn.01", prompt),
                ScenarioTurnStep("turn.02", "先别急着发图，你先接住我刚刚的话。"),
            ),
            "interruption",
            ("ObservationRecorded", "ActionAuthorized"),
            ("MediaPreviewGenerated",),
            ("expression_reconsideration",),
        )
    if family == "projection_gap":
        return (
            (
                ScenarioTurnStep("turn.01", prompt),
                ScenarioTurnStep("turn.02", "如果不能公开就直接说不能公开，不要编一个状态。"),
            ),
            "chat",
            ("ObservationRecorded", "ActionDelivered"),
            ("MediaPreviewGenerated",),
            (),
        )
    return (), "chat", (), (), ()


def _fault_for(*, family: str, ordinal: int) -> ScenarioFault:
    if family == "provider_timeout":
        return {
            1: "provider_failed",
            2: "provider_unknown",
            3: "restart_before_dispatch",
        }.get(ordinal, "none")
    if family == "interruption" and ordinal == 1:
        return "duplicate_ingress"
    return "none"


def _room_redactions_for(*, family: str, ordinal: int, turn_id: str) -> tuple[str, ...]:
    """Private fixture authority that may never become a room-view string."""

    if family == "npc_world_impact" and ordinal == 1:
        return (
            f"occurrence:phase8:{turn_id}",
            f"result:phase8:{turn_id}:settled",
            "room:scenario-private",
        )
    if family == "projection_gap" and ordinal == 1:
        return ("preview:", "user:scenario")
    return ()


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
            turns, execution, required, forbidden, trigger_kinds = _script_for(
                family=family, ordinal=ordinal, prompt=text
            )
            if turns:
                text = turns[0].text
            effective_turns = turns or (ScenarioTurnStep(step_id="turn.01", text=text),)
            facts: tuple[tuple[str, str], ...] = (
                (
                    "preconditions",
                    "seeded"
                    if execution in {"seeded_world_outcome", "seeded_world_outcome_affect"}
                    else "none",
                ),
                ("execution", execution),
                # Bind the exact script that the runner will execute, including
                # a one-turn control's otherwise implicit default step.
                ("turn_script", _hash(tuple((item.step_id, item.text) for item in effective_turns))),
                ("required_events", ",".join(required)),
                ("forbidden_events", ",".join(forbidden)),
                ("required_triggers", ",".join(trigger_kinds)),
            )
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
                    turns=effective_turns,
                    execution=execution,
                    required_event_types=required,
                    forbidden_event_types=forbidden,
                    required_trigger_kinds=trigger_kinds,
                    forbidden_room_view_values=_room_redactions_for(
                        family=family, ordinal=ordinal, turn_id=turn_id
                    ),
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
FROZEN_SCENARIO_CORPUS_HASH = "c2f5e671e211d26a4854928ec165a5c58e0d5908e60979df669ed9d32fa9b9dc"
FROZEN_SCENARIO_CASES_HASH = "9dd942e3e585275a044c8abdd5872ae1ea9d003152f3096e48915a4eebaa0192"


def scenario_cases_digest(cases: tuple[ScenarioCase, ...]) -> str:
    """Hash the executable scripts and assertions, not merely their labels."""

    return _hash(
        [
            {
                "entry": item.entry.scenario_turn_id,
                "turns": tuple((turn.step_id, turn.text) for turn in item.turns),
                "execution": item.execution,
                "fault": item.fault,
                "required_event_types": item.required_event_types,
                "forbidden_event_types": item.forbidden_event_types,
                "required_trigger_kinds": item.required_trigger_kinds,
                "forbidden_room_view_values": item.forbidden_room_view_values,
                "fact_set": item.fact_set,
            }
            for item in cases
        ]
    )


def verify_frozen_scenario_corpus() -> tuple[ScenarioCase, ...]:
    """Return the corpus only if its versioned membership has not drifted."""

    actual = corpus_digest(SCENARIO_CORPUS)
    if actual != FROZEN_SCENARIO_CORPUS_HASH:
        raise RuntimeError(
            "scenario corpus digest drifted; bump its version and establish a new evaluation baseline"
        )
    if scenario_cases_digest(SCENARIO_CASES) != FROZEN_SCENARIO_CASES_HASH:
        raise RuntimeError(
            "scenario fixture script digest drifted; bump its version and establish a new evaluation baseline"
        )
    return SCENARIO_CASES


__all__ = [
    "FROZEN_SCENARIO_CORPUS_HASH",
    "FROZEN_SCENARIO_CASES_HASH",
    "MINIMUM_EMOTION_GOLD_SIZE",
    "SCENARIO_CASES",
    "SCENARIO_CORPUS",
    "SCENARIO_CORPUS_SIZE",
    "SCENARIO_CORPUS_VERSION",
    "ScenarioFault",
    "ScenarioCase",
    "ScenarioExecution",
    "ScenarioTurnStep",
    "TEST_ECONOMY_PROFILE_VERSION",
    "verify_frozen_scenario_corpus",
    "scenario_cases_digest",
]
