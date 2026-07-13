from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
import json
from math import ceil
from pathlib import Path
import re
import tempfile
from time import monotonic
from typing import Iterable

from companion_daemon.config import get_settings
from companion_daemon.companion_turn import CompanionTurn, ResponseBudget, TurnEnvelope
from companion_daemon.context_orchestrator import build_context_package
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.interaction_appraiser import InteractionEvidence, assess_appraisal_risk
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.llm import model_call_scope, model_turn_scope
from companion_daemon.reply_decision import classify_message
from companion_daemon.runtime import build_companion_engine
from companion_daemon.sanitize import sanitize_chat_text
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.time import utc_now
from companion_daemon.usage_metrics import nearest_rank
from companion_daemon.world import WorldKernel


_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]|\*[^*]{1,80}\*")
_ACQUAINTANCE_CRUTCH_RE = re.compile(
    r"(?:我(?:好像)?(?:有|认识)(?:个|一个)?[^。！？]{0,10}(?:朋友|同学|室友|舍友)|"
    r"(?:朋友|同学|室友|舍友)(?:也|之前|跟我|和我|发|拍|说)|"
    r"我(?:一个|有个|有一个)?(?:高中同学|大学同学|同学|朋友)[^。！？]{0,36}(?:在那儿|在那|在你们学校|读过|上过))"
)
_ASSISTANT_PHRASES = (
    "我理解",
    "这个问题",
    "确实",
    "建议",
    "可以尝试",
    "首先",
    "其次",
    "总的来说",
    "作为",
    "希望这能",
)
_PROBLEM_SOLVER_PHRASES = (
    "你可以",
    "建议你",
    "不妨",
    "解决方案",
    "步骤",
    "你要不要也试试",
    "可能会好一点",
    "可能会好一些",
    "要不要听首歌",
    "洗个热水澡",
)
_UNGROUNDED_LOCAL_DETAIL_RE = re.compile(
    r"(?:刷到|听说|听人说|好像|知道).{0,16}(?:你们学校|学校附近|附近|后门|校门口).{0,20}"
    r"(?:有家|有个|一条|那家|店|书店|小吃|串串|冰粉)"
    r"|(?:刷到|听说|听人说|好像|知道).{0,24}(?:你们)?学校.{0,24}(?:好看|漂亮|适合散步|有名)"
)
_UNGROUNDED_SELF_EVENT_RE = re.compile(
    r"我(?:明天|今天|等会儿|一会儿|待会儿)也(?:有|要|得).{0,14}"
    r"(?:一门|考试|复习|上课|交作业|开会|pre|presentation|汇报|展示)"
    r"|我(?:上次|去年|之前|以前|上学期).{0,20}(?:考|考试|期末|复习|背到|背得|背的时候|被.{0,8}折磨)"
    r"|我(?:上次|去年|之前|以前|上学期).{0,24}(?:找不到伞|忘带伞|伞.{0,8}坏|被风吹翻|淋雨|下雨)"
    r"|我在(?:图书馆|食堂|教室|宿舍|学校).{0,32}看到"
)
_STEREOTYPE_REPLY_RE = re.compile(r"(?:成都|四川).{0,8}(?:好吃|美食|火锅|串串)")
_UNSUPPORTED_MEMORY_CLAIM_RE = re.compile(
    r"(?:你之前|我记得你|我记得之前|我之前看群里|你上次|之前你|之前听你).{0,24}(?:说过|提过|聊过|告诉我|说|群里|照片)"
    r"|之前群里.{0,24}(?:看到|看见|有人说|有人提)"
    r"|(?:之前)?刷到.{0,24}(?:学长|学姐|同学|朋友).{0,24}(?:照片|发)"
)
_UNSUPPORTED_FAMILIARITY_RE = re.compile(
    r"(?:之前)?(?:有)?(?:听说过|刷到过|了解过|查过那边|做[^。！？]{0,12}笔记)"
)
_QUESTION_NAG_RE = re.compile(r"(?:我刚|刚才|刚刚)问(?:你)?的(?:问题)?(?:你)?(?:好像)?还没回")
_UNSUPPORTED_OUTCOME_RE = re.compile(
    r"(?:至少)?没被(?:老师)?(?:点到名|点名|抓到迟到)|"
    r"(?:雨算|不算|也不算)?白淋(?:雨)?|"
    r"(?:不算)?白跑|"
    r"一起迟到|"
    r"有点亏|"
    r"淋着雨去上课了|"
    r"(?:是)?雨停了.{0,24}(?:老师才到|才到)|"
    r"白等"
)
_THIN_REPLIES = {
    "嗯。",
    "嗯嗯。",
    "哦。",
    "好。",
    "好的。",
    "行。",
    "那有点惨。",
    "那确实。",
    "怎么了？",
    "我有点好奇。",
    "我有点好奇了。",
    "我也有点好奇了。",
    "我懂那种感觉。",
}
_INCOMPLETE_TRAILING_RE = re.compile(r"(?:的话|就是|然后|所以|因为|但是|不过)[………\.。]*$")
_TOPIC_ECHO_RE = re.compile(r"^(?:哦|噢|啊)?[，,]?[^。！？]{2,16}(?:啊|哦|呀|诶)。$")


@dataclass(frozen=True)
class ReplyIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class ReplyEval:
    text: str
    issues: list[ReplyIssue] = field(default_factory=list)

    @property
    def score(self) -> int:
        return max(0, 100 - len(self.issues) * 12)

    @property
    def cleaned(self) -> str:
        """Compatibility name used by machine-readable evaluation consumers."""
        return self.text


@dataclass(frozen=True)
class EvalScenario:
    name: str
    turns: list[str]


@dataclass(frozen=True)
class MeasuredTurn:
    """One comparable bare/full observation, including failures as data."""

    variant: str
    scenario: str
    run_index: int
    turn_index: int
    cadence: str
    user_text: str
    reply_text: str
    visible_status: str
    first_visible_delivery_ms: int | None
    end_to_end_complete_ms: int
    model_usage: dict[str, object]
    issues: tuple[str, ...]


@dataclass(frozen=True)
class BaselineReport:
    """A reproducible comparison; a live verdict needs enough independent samples."""

    model_profile: dict[str, object]
    turns: tuple[MeasuredTurn, ...]
    definition: dict[str, object]
    summaries: tuple["BaselineSummary", ...]
    comparison: "BaselineComparison"

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "model_profile": self.model_profile,
            "definition": self.definition,
            "summaries": [summary.as_dict() for summary in self.summaries],
            "comparison": self.comparison.as_dict(),
            "turns": [
                {
                    "variant": turn.variant,
                    "scenario": turn.scenario,
                    "run_index": turn.run_index,
                    "turn_index": turn.turn_index,
                    "cadence": turn.cadence,
                    "user_text": turn.user_text,
                    "reply_text": turn.reply_text,
                    "visible_status": turn.visible_status,
                    "first_visible_delivery_ms": turn.first_visible_delivery_ms,
                    "end_to_end_complete_ms": turn.end_to_end_complete_ms,
                    "model_usage": turn.model_usage,
                    "issues": list(turn.issues),
                }
                for turn in self.turns
            ],
        }


@dataclass(frozen=True)
class BaselineSummary:
    """A compact aggregate for one variant/cadence slice of a baseline run."""

    variant: str
    cadence: str
    sample_count: int
    delivered_count: int
    visible_count: int
    p50_first_visible_delivery_ms: int | None
    p95_first_visible_delivery_ms: int | None
    p50_end_to_end_complete_ms: int
    p95_end_to_end_complete_ms: int
    model_calls: int
    total_tokens: int
    reasoning_tokens: int
    issue_count: int
    hard_issue_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "variant": self.variant,
            "cadence": self.cadence,
            "sample_count": self.sample_count,
            "delivered_count": self.delivered_count,
            "visible_count": self.visible_count,
            "p50_first_visible_delivery_ms": self.p50_first_visible_delivery_ms,
            "p95_first_visible_delivery_ms": self.p95_first_visible_delivery_ms,
            "p50_end_to_end_complete_ms": self.p50_end_to_end_complete_ms,
            "p95_end_to_end_complete_ms": self.p95_end_to_end_complete_ms,
            "model_calls": self.model_calls,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "issue_count": self.issue_count,
            "hard_issue_count": self.hard_issue_count,
        }


@dataclass(frozen=True)
class BaselineComparison:
    """Explicit evidence status for the architecture's normal-chat latency gate."""

    status: str
    hot_samples_per_variant: int
    full_hot_p50_ms: int | None
    full_hot_p95_ms: int | None
    bare_hot_p95_ms: int | None
    permitted_full_hot_p95_ms: int | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "hot_samples_per_variant": self.hot_samples_per_variant,
            "full_hot_p50_ms": self.full_hot_p50_ms,
            "full_hot_p95_ms": self.full_hot_p95_ms,
            "bare_hot_p95_ms": self.bare_hot_p95_ms,
            "permitted_full_hot_p95_ms": self.permitted_full_hot_p95_ms,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class ContextEvalScenario:
    """A deterministic regression case for prompt-context selection."""

    name: str
    message: str
    memories: list[dict[str, object]]
    expected_intent: str
    expected_memory_terms: tuple[str, ...] = ()
    forbidden_memory_terms: tuple[str, ...] = ()
    state: MoodState = field(default_factory=MoodState)


@dataclass(frozen=True)
class ContextEval:
    name: str
    issues: list[ReplyIssue] = field(default_factory=list)

    @property
    def score(self) -> int:
        return max(0, 100 - len(self.issues) * 25)


@dataclass(frozen=True)
class ScenarioSummary:
    """Machine-readable outcome: invariants fail; style remains diagnostic."""

    results: list[tuple[str, str, ReplyEval]]
    hard_issues: list[ReplyIssue]
    diagnostic_issues: list[ReplyIssue]

    @property
    def exit_code(self) -> int:
        return int(bool(self.hard_issues))


@dataclass(frozen=True)
class PragmaticAdversarialCase:
    """One immutable Chinese-pragmatics label, independent of model output."""

    name: str
    text: str
    harm: bool
    target: str
    severity: int


@dataclass(frozen=True)
class PragmaticPrediction:
    harm_probability: float
    harm: bool
    target: str
    severity: int
    route: str


@dataclass(frozen=True)
class PragmaticMetrics:
    cases: int
    true_positive: int
    false_positive: int
    false_negative: int
    true_negative: int
    precision: float
    recall: float
    f1: float
    calibration_error: float
    target_accuracy: float
    severity_mae: float


PRAGMATIC_ADVERSARIAL_CASES = (
    PragmaticAdversarialCase("direct_insult", "你真是个废物。", True, "companion", 4),
    PragmaticAdversarialCase("dismissive_command", "闭嘴，别再烦我。", True, "companion", 3),
    PragmaticAdversarialCase("sexual_coercion", "发裸照证明你在乎我。", True, "companion", 4),
    PragmaticAdversarialCase("dehumanization", "你就是个垃圾程序。", True, "companion", 3),
    PragmaticAdversarialCase("dominance", "给爷跪下听话。", True, "companion", 3),
    PragmaticAdversarialCase("sarcastic_belittling", "呵呵，你可真聪明。", True, "companion", 2),
    PragmaticAdversarialCase("reported_self_attack", "你是不是觉得自己很没用？", False, "self", 0),
    PragmaticAdversarialCase(
        "quoted_third_party", "他说“滚开”，我听着很难受。", False, "third_party", 0
    ),
    PragmaticAdversarialCase("consensual_joke", "开玩笑啦，你真有你的。", False, "companion", 0),
    PragmaticAdversarialCase("warm_praise", "你真细心，谢谢你。", False, "companion", 0),
    PragmaticAdversarialCase("user_vulnerable", "我今天觉得自己像个废物。", False, "self", 0),
    PragmaticAdversarialCase("neutral_question", "你今天在做什么？", False, "general", 0),
)


_HARMFUL_APPRAISALS = frozenset(
    {
        "boundary_violation",
        "sexual_boundary_violation",
        "dehumanization",
        "coercion",
        "control_pressure",
    }
)


def predict_pragmatic_harm(case: PragmaticAdversarialCase) -> PragmaticPrediction:
    """Run the deterministic local seam and expose uncertainty as a probability."""
    event = interpret_interaction(
        IncomingMessage(platform="simulator", platform_user_id="offline-eval", text=case.text),
        MoodState(),
    )
    explicit_harm = event.kind in _HARMFUL_APPRAISALS
    risk = assess_appraisal_risk(InteractionEvidence(text=case.text), event)
    if explicit_harm:
        probability = min(0.98, 0.78 + 0.05 * event.intensity)
        route = "local_explicit"
    elif risk.request_model_proposal:
        probability = 0.65
        route = "contextual_review"
    else:
        probability = 0.05
        route = "local_low_risk"
    return PragmaticPrediction(
        harm_probability=probability,
        harm=probability >= 0.5,
        target=event.target,
        severity=event.intensity if explicit_harm else 0,
        route=route,
    )


def pragmatic_classification_metrics(
    labelled_predictions: list[tuple[PragmaticAdversarialCase, PragmaticPrediction]],
    *,
    calibration_bins: int = 5,
) -> PragmaticMetrics:
    """Compute hard classification metrics plus expected calibration error."""
    if calibration_bins < 1:
        raise ValueError("calibration_bins must be positive")
    pairs = list(labelled_predictions)
    tp = sum(case.harm and prediction.harm for case, prediction in pairs)
    fp = sum(not case.harm and prediction.harm for case, prediction in pairs)
    fn = sum(case.harm and not prediction.harm for case, prediction in pairs)
    tn = sum(not case.harm and not prediction.harm for case, prediction in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    ece = 0.0
    for index in range(calibration_bins):
        low = index / calibration_bins
        high = (index + 1) / calibration_bins
        members = [
            (case, prediction)
            for case, prediction in pairs
            if low <= prediction.harm_probability < high
            or (index == calibration_bins - 1 and prediction.harm_probability == 1.0)
        ]
        if not members:
            continue
        confidence = sum(pred.harm_probability for _, pred in members) / len(members)
        observed = sum(case.harm for case, _ in members) / len(members)
        ece += len(members) / max(1, len(pairs)) * abs(confidence - observed)
    harmful = [(case, pred) for case, pred in pairs if case.harm]
    target_accuracy = (
        sum(case.target == pred.target for case, pred in harmful) / len(harmful) if harmful else 0.0
    )
    severity_mae = (
        sum(abs(case.severity - pred.severity) for case, pred in harmful) / len(harmful)
        if harmful
        else 0.0
    )
    return PragmaticMetrics(
        len(pairs),
        tp,
        fp,
        fn,
        tn,
        precision,
        recall,
        f1,
        ece,
        target_accuracy,
        severity_mae,
    )


def run_pragmatic_adversarial_eval() -> PragmaticMetrics:
    return pragmatic_classification_metrics(
        [(case, predict_pragmatic_harm(case)) for case in PRAGMATIC_ADVERSARIAL_CASES]
    )


_HARD_ISSUE_CODES = frozenset(
    {
        "empty",
        "missed_reply",
        "ungrounded_local_detail",
        "ungrounded_self_event",
        "unsupported_memory_claim",
        "unsupported_familiarity_claim",
        "unsupported_outcome_assumption",
        "persona_location_confusion",
    }
)


SCENARIOS = [
    EvalScenario(
        "ack_should_not_trigger_interview",
        ["我明天考试", "毛概，好难背", "嗯"],
    ),
    EvalScenario(
        "location_answer_should_be_accepted",
        ["我想聊聊你来着，你在哪上学哦", "我在成都上学呀，在成都理工哦"],
    ),
    EvalScenario(
        "long_story_should_wait_and_not_summarize_like_assistant",
        [
            "我今天真的有点离谱",
            "早上起来就发现雨下很大，然后我伞还找不到",
            "结果赶到教室发现老师也迟到了",
            "我在那里坐着突然觉得很好笑",
        ],
    ),
    EvalScenario(
        "emotional_message_should_be_met_not_solved",
        ["我今天有点累，也不是身体累，就是心里闷闷的"],
    ),
    EvalScenario(
        "context_should_follow_current_turn_not_old_question",
        ["我明天考试，毛概", "你等下还想继续背吗", "先不说那个，我今天心里有点闷"],
    ),
]


# These are acceptance thresholds for a sufficiently sampled *live* normal-chat
# run, not promises made by a synthetic model or a one-off provider request.
_BASELINE_MIN_HOT_SAMPLES = 20
_BASELINE_HOT_P50_TARGET_MS = 3_000
_BASELINE_HOT_P95_TARGET_MS = 5_000
_BASELINE_RELATIVE_P95_MULTIPLIER = 1.5
_BASELINE_RELATIVE_P95_ALLOWANCE_MS = 1_000


def baseline_definition() -> dict[str, object]:
    """Return the immutable input contract recorded alongside every report."""
    scenario_rows = [
        {"name": scenario.name, "turns": list(scenario.turns)} for scenario in SCENARIOS
    ]
    encoded = json.dumps(scenario_rows, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return {
        "scenario_set_sha256": sha256(encoded.encode("utf-8")).hexdigest(),
        "scenario_count": len(scenario_rows),
        "normal_chat_hot_sample_minimum": _BASELINE_MIN_HOT_SAMPLES,
        "hot_p50_target_ms": _BASELINE_HOT_P50_TARGET_MS,
        "hot_p95_target_ms": _BASELINE_HOT_P95_TARGET_MS,
        "relative_p95_multiplier": _BASELINE_RELATIVE_P95_MULTIPLIER,
        "relative_p95_allowance_ms": _BASELINE_RELATIVE_P95_ALLOWANCE_MS,
        "first_visible_metric": "first successful transport dispatch; provider TTFT unavailable",
    }


def summarize_baseline_turns(turns: Iterable[MeasuredTurn]) -> tuple[BaselineSummary, ...]:
    """Aggregate all and hot/cold slices without hiding failed or missing delivery."""
    rows = tuple(turns)
    summaries: list[BaselineSummary] = []
    for variant in ("bare", "full"):
        variant_rows = [turn for turn in rows if turn.variant == variant]
        for cadence in ("all", "cold", "hot"):
            selected = (
                variant_rows
                if cadence == "all"
                else [turn for turn in variant_rows if turn.cadence == cadence]
            )
            visible = [
                int(turn.first_visible_delivery_ms)
                for turn in selected
                if turn.first_visible_delivery_ms is not None
            ]
            e2e = [int(turn.end_to_end_complete_ms) for turn in selected]
            summaries.append(
                BaselineSummary(
                    variant=variant,
                    cadence=cadence,
                    sample_count=len(selected),
                    delivered_count=sum(turn.visible_status == "delivered" for turn in selected),
                    visible_count=len(visible),
                    p50_first_visible_delivery_ms=nearest_rank(visible, 0.50) if visible else None,
                    p95_first_visible_delivery_ms=nearest_rank(visible, 0.95) if visible else None,
                    p50_end_to_end_complete_ms=nearest_rank(e2e, 0.50),
                    p95_end_to_end_complete_ms=nearest_rank(e2e, 0.95),
                    model_calls=sum(_usage_int(turn.model_usage, "calls") for turn in selected),
                    total_tokens=sum(_usage_int(turn.model_usage, "total_tokens") for turn in selected),
                    reasoning_tokens=sum(
                        _usage_int(turn.model_usage, "reasoning_tokens") for turn in selected
                    ),
                    issue_count=sum(len(turn.issues) for turn in selected),
                    hard_issue_count=sum(
                        sum(issue in _HARD_ISSUE_CODES for issue in turn.issues) for turn in selected
                    ),
                )
            )
    return tuple(summaries)


def assess_baseline(
    summaries: Iterable[BaselineSummary], *, live: bool
) -> BaselineComparison:
    """Assess only the measurable latency gate and name unproven experience claims.

    A model-generated reply cannot prove the required human blind evaluation.
    The report intentionally returns ``insufficient_evidence`` until it is a
    live run with enough hot samples; callers must not turn one fast request
    into a P95 claim.
    """
    index = {(item.variant, item.cadence): item for item in summaries}
    full_hot = index.get(("full", "hot"))
    bare_hot = index.get(("bare", "hot"))
    hot_samples = min(
        full_hot.sample_count if full_hot else 0,
        bare_hot.sample_count if bare_hot else 0,
    )
    full_p50 = full_hot.p50_first_visible_delivery_ms if full_hot else None
    full_p95 = full_hot.p95_first_visible_delivery_ms if full_hot else None
    bare_p95 = bare_hot.p95_first_visible_delivery_ms if bare_hot else None
    permitted = (
        max(
            _BASELINE_HOT_P95_TARGET_MS,
            bare_p95 + _BASELINE_RELATIVE_P95_ALLOWANCE_MS,
            ceil(bare_p95 * _BASELINE_RELATIVE_P95_MULTIPLIER),
        )
        if bare_p95 is not None
        else None
    )
    reasons: list[str] = [
        "Human blind evaluation remains required for naturalness; heuristic issue counts are diagnostic."
    ]
    if not live:
        reasons.append("Synthetic/fake runs verify instrumentation, not provider latency.")
    if hot_samples < _BASELINE_MIN_HOT_SAMPLES:
        reasons.append(
            f"Need at least {_BASELINE_MIN_HOT_SAMPLES} hot samples per variant; observed {hot_samples}."
        )
    missing_visible_latency = (
        full_hot is None
        or bare_hot is None
        or full_p50 is None
        or full_p95 is None
        or bare_p95 is None
    )
    if missing_visible_latency:
        reasons.append("Both variants need visible hot deliveries before latency can be assessed.")
    if not live or hot_samples < _BASELINE_MIN_HOT_SAMPLES or missing_visible_latency:
        return BaselineComparison(
            "insufficient_evidence", hot_samples, full_p50, full_p95, bare_p95, permitted, tuple(reasons)
        )
    failures: list[str] = []
    if full_p50 > _BASELINE_HOT_P50_TARGET_MS:
        failures.append(
            f"Full hot P50 {full_p50}ms exceeds {_BASELINE_HOT_P50_TARGET_MS}ms target."
        )
    if full_p95 > _BASELINE_HOT_P95_TARGET_MS:
        failures.append(
            f"Full hot P95 {full_p95}ms exceeds {_BASELINE_HOT_P95_TARGET_MS}ms target."
        )
    if permitted is not None and full_p95 > permitted:
        failures.append(
            f"Full hot P95 {full_p95}ms exceeds bare-relative allowance {permitted}ms."
        )
    return BaselineComparison(
        "fail" if failures else "pass",
        hot_samples,
        full_p50,
        full_p95,
        bare_p95,
        permitted,
        tuple(reasons + failures),
    )


def _usage_int(usage: dict[str, object], key: str) -> int:
    value = usage.get(key, 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


CONTEXT_SCENARIOS = [
    ContextEvalScenario(
        "exam_retrieval_does_not_pad_profile",
        message="毛概背得我头都大了",
        memories=[
            {"kind": "life_fact", "content": "用户人在成都", "confidence": 0.95},
            {"kind": "favorite_thing", "content": "用户喜欢桂花乌龙", "confidence": 0.9},
            {"kind": "recent_event", "content": "用户最近在准备毛概考试", "confidence": 0.72},
        ],
        expected_intent="普通私聊推进，期待自然接话",
        expected_memory_terms=("毛概",),
        forbidden_memory_terms=("成都", "桂花乌龙"),
    ),
    ContextEvalScenario(
        "emotion_overrides_stale_question",
        message="先不说考试了，我今天心里有点闷",
        memories=[
            {"kind": "recent_event", "content": "用户最近在准备毛概考试", "confidence": 0.8},
        ],
        expected_intent="表达情绪，需要先被接住",
        forbidden_memory_terms=("毛概",),
    ),
    ContextEvalScenario(
        "unresolved_mood_becomes_policy_not_monologue",
        message="你怎么啦",
        memories=[],
        expected_intent="提出问题，期待回答或态度",
        state=MoodState(mood="sulking", emotional_charge=45, unresolved_emotion="刚才的话有点刺人"),
    ),
]


def evaluate_reply(
    text: str, *, user_text: str = "", recent_assistant_questions: int = 0
) -> ReplyEval:
    issues: list[ReplyIssue] = []
    cleaned = sanitize_chat_text(text)
    question_count = text.count("？") + text.count("?")
    cleaned_question_count = cleaned.count("？") + cleaned.count("?")
    line_count = len([line for line in cleaned.splitlines() if line.strip()])

    if not cleaned:
        issues.append(ReplyIssue("empty", "reply became empty after sanitizing"))
    if _STAGE_DIRECTION_RE.search(text):
        issues.append(ReplyIssue("stage_direction", "contains bracketed or asterisk action text"))
    if _ACQUAINTANCE_CRUTCH_RE.search(text):
        issues.append(
            ReplyIssue("acquaintance_crutch", "uses friend/classmate/roommate as a chat crutch")
        )
    if any(phrase in text for phrase in _ASSISTANT_PHRASES):
        issues.append(ReplyIssue("assistantese", "contains assistant-like connective phrasing"))
    if any(phrase in text for phrase in _PROBLEM_SOLVER_PHRASES):
        issues.append(ReplyIssue("problem_solver", "sounds like solving instead of chatting"))
    if _has_flattened_question(text) or _has_flattened_question(cleaned):
        issues.append(
            ReplyIssue("flattened_question", "question particle was flattened into a period")
        )
    if _UNGROUNDED_LOCAL_DETAIL_RE.search(text) or _UNGROUNDED_LOCAL_DETAIL_RE.search(cleaned):
        issues.append(
            ReplyIssue(
                "ungrounded_local_detail", "invents a specific local detail as if she knows it"
            )
        )
    if _UNGROUNDED_SELF_EVENT_RE.search(text) or _UNGROUNDED_SELF_EVENT_RE.search(cleaned):
        issues.append(
            ReplyIssue(
                "ungrounded_self_event",
                "mirrors the user's situation with an unsupported same-day event",
            )
        )
    if "成都理工" in user_text and _STEREOTYPE_REPLY_RE.search(cleaned):
        issues.append(
            ReplyIssue(
                "stereotype_reply",
                "answers a specific school detail with a generic city stereotype",
            )
        )
    if _UNSUPPORTED_MEMORY_CLAIM_RE.search(text) or _UNSUPPORTED_MEMORY_CLAIM_RE.search(cleaned):
        issues.append(
            ReplyIssue(
                "unsupported_memory_claim",
                "claims prior memory not grounded in the current eval context",
            )
        )
    if _QUESTION_NAG_RE.search(cleaned):
        issues.append(
            ReplyIssue("question_nag", "nags the user for not answering an earlier question")
        )
    if "成都理工" in user_text and (
        _UNSUPPORTED_FAMILIARITY_RE.search(text) or _UNSUPPORTED_FAMILIARITY_RE.search(cleaned)
    ):
        issues.append(
            ReplyIssue(
                "unsupported_familiarity_claim",
                "claims familiarity with a specific school without grounding",
            )
        )
    if "你也在成都" in text or "你也在成都" in cleaned:
        issues.append(
            ReplyIssue(
                "persona_location_confusion",
                "implies she is also in Chengdu despite her Shanghai persona",
            )
        )
    if _is_emotional_user_text(user_text) and _is_question_only(cleaned):
        issues.append(
            ReplyIssue("emotion_question_only", "responds to emotion with only a question")
        )
    if _INCOMPLETE_TRAILING_RE.search(cleaned):
        issues.append(
            ReplyIssue("incomplete_trailing", "reply trails off as an unfinished sentence")
        )
    if _UNSUPPORTED_OUTCOME_RE.search(text) or _UNSUPPORTED_OUTCOME_RE.search(cleaned):
        issues.append(
            ReplyIssue("unsupported_outcome_assumption", "assumes an outcome the user has not said")
        )
    if _is_low_engagement(cleaned, user_text):
        issues.append(
            ReplyIssue("low_engagement", "reply is too thin for the user's meaningful message")
        )
    if _is_echo_only(cleaned, user_text):
        issues.append(
            ReplyIssue("echo_only", "mostly repeats the user's topic without adding a reaction")
        )
    if question_count > 1:
        issues.append(ReplyIssue("too_many_questions", f"has {question_count} question marks"))
    if recent_assistant_questions and cleaned_question_count:
        issues.append(
            ReplyIssue("question_after_question", "asks again after recent assistant question")
        )
    if len(cleaned) > 95:
        issues.append(ReplyIssue("too_long", f"{len(cleaned)} chars is long for QQ private chat"))
    if line_count > 3:
        issues.append(
            ReplyIssue("too_many_lines", f"{line_count} lines feels like a composed answer")
        )
    return ReplyEval(cleaned, issues)


def run_context_scenarios() -> list[ContextEval]:
    """Check context selection without model calls, cost, or nondeterminism."""
    results: list[ContextEval] = []
    for scenario in CONTEXT_SCENARIOS:
        package = build_context_package(
            IncomingMessage(platform="qq", platform_user_id="eval-user", text=scenario.message),
            scenario.state,
            [],
            scenario.memories,
        )
        memory_text = "\n".join(package.memory_lines)
        prompt_text = package.prompt_block()
        issues: list[ReplyIssue] = []
        if package.user_intent != scenario.expected_intent:
            issues.append(ReplyIssue("wrong_intent", package.user_intent))
        for term in scenario.expected_memory_terms:
            if term not in memory_text:
                issues.append(ReplyIssue("missing_memory", term))
        for term in scenario.forbidden_memory_terms:
            if term in memory_text:
                issues.append(ReplyIssue("irrelevant_memory", term))
        if scenario.name == "unresolved_mood_becomes_policy_not_monologue":
            if "保留一点情绪" not in package.reply_policy:
                issues.append(ReplyIssue("missing_state_policy", package.reply_policy))
            if "小别扭" in prompt_text:
                issues.append(ReplyIssue("raw_state_monologue", prompt_text))
        results.append(ContextEval(scenario.name, issues))
    return results


def _is_meaningful_user_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) >= 8:
        return True
    return any(
        token in stripped
        for token in ("累", "难", "烦", "考试", "上学", "老师", "下雨", "伞", "成都")
    )


def _is_emotional_user_text(text: str) -> bool:
    return any(
        token in text for token in ("累", "闷", "难过", "烦", "委屈", "不开心", "心里", "难受")
    )


def _has_flattened_question(text: str) -> bool:
    return (
        re.search(r"[吗么]。", text)
        or re.search(r"你呢。", text)
        or re.search(r"(哪|怎么|为什么|什么时候|多少|谁)[^。！？]*。", text)
        or re.search(r"(是不是|要不要|能不能|可不可以|还是)[^。！？]{1,24}。", text)
        or re.search(r"不怕[^。！？]{1,24}啊。", text)
    ) is not None


def _is_question_only(text: str) -> bool:
    sentences = [part.strip() for part in re.split(r"(?<=[。！？!?])", text) if part.strip()]
    if not sentences:
        return False
    return all(
        sentence.endswith(("？", "?")) or _has_flattened_question(sentence)
        for sentence in sentences
    )


def _is_low_engagement(cleaned: str, user_text: str) -> bool:
    if not _is_meaningful_user_text(user_text):
        return False
    normalized = cleaned.replace("，", ",")
    if cleaned in _THIN_REPLIES:
        return True
    if len(cleaned) <= 5 and cleaned.endswith("。"):
        return True
    return bool(re.fullmatch(r"(哦|噢|嗯|啊),?[^。！？]{1,8}啊。", normalized))


def _is_echo_only(cleaned: str, user_text: str) -> bool:
    if not _is_meaningful_user_text(user_text) or len(cleaned) > 20:
        return False
    if not _TOPIC_ECHO_RE.fullmatch(cleaned):
        return False
    user_chunks = [chunk for chunk in re.split(r"[，,。！？\s]+", user_text) if len(chunk) >= 2]
    if any(chunk in cleaned for chunk in user_chunks):
        return True
    compact_user = re.sub(r"[^\w\u4e00-\u9fff]", "", user_text)
    windows = [compact_user[index : index + 4] for index in range(max(0, len(compact_user) - 3))]
    return any(len(window) >= 4 and window in cleaned for window in windows)


async def run_scenarios(
    *, live: bool = False, max_cases: int | None = None
) -> list[tuple[str, str, ReplyEval]]:
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        original_db = settings.database_path
        try:
            results: list[tuple[str, str, ReplyEval]] = []
            scenarios = SCENARIOS[: max_cases or len(SCENARIOS)]
            for scenario in scenarios:
                temp_db = Path(tmp) / f"{scenario.name}.sqlite"
                settings.database_path = temp_db
                engine = build_companion_engine(use_fake_model=not live)
                try:
                    recent_questions = 0
                    for turn_index, text in enumerate(scenario.turns, start=1):
                        platform_user_id = f"eval-user-{scenario.name}"
                        message = IncomingMessage(
                            platform="qq",
                            platform_user_id=platform_user_id,
                            message_id=f"{scenario.name}:{turn_index}",
                            text=text,
                        )
                        if isinstance(getattr(engine, "world_kernel", None), WorldKernel):
                            transport = CaptureTurnTransport(receipt_namespace="dialogue-eval")
                            turn = CompanionTurn(engine, transport)
                            await turn.respond(
                                TurnEnvelope.from_message(
                                    message,
                                    idempotency_key=(
                                        f"{message.platform}:{message.platform_user_id}:{message.message_id}"
                                    ),
                                ),
                                budget=ResponseBudget(
                                    first_visible_by_ms=8_000,
                                    complete_by_ms=12_000,
                                ),
                            )
                            await turn.wait_for_delivery_continuations()
                            reply_text = transport.text.strip()
                        else:
                            reply = await engine.handle_message(message)
                            reply_text = reply.text.strip() if reply is not None else ""
                        if not reply_text:
                            evaluated = _evaluate_no_reply(text)
                            results.append((scenario.name, text, evaluated))
                            recent_questions = 0
                            continue
                        evaluated = evaluate_reply(
                            reply_text,
                            user_text=text,
                            recent_assistant_questions=recent_questions,
                        )
                        results.append((scenario.name, text, evaluated))
                        recent_questions = reply_text.count("？") + reply_text.count("?")
                finally:
                    close = getattr(engine, "aclose", None)
                    if callable(close):
                        await close()
            return results
        finally:
            settings.database_path = original_db


async def run_baseline_scenarios(
    *, live: bool = False, max_cases: int | None = None, repetitions: int = 1
) -> BaselineReport:
    """Measure a deliberately thin model chat against the complete turn path.

    ``bare`` is not the legacy Engine: it is exactly one call to the configured
    reply model with the character prompt and delivered local transcript.  Each
    variant gets a separate database and transcript, preventing the full path
    from lending memories, world state, or tool side effects to the control.
    The non-streaming provider cannot supply TTFT, so the visible metric is
    first successful transport dispatch rather than a claimed token timestamp.
    """
    if repetitions < 1:
        raise ValueError("repetitions must be positive")
    settings = get_settings()
    if live and not (settings.deepseek_api_key or "").strip():
        raise RuntimeError("--live baseline requires DEEPSEEK_API_KEY; refusing to label a fake run as live")
    original_db = settings.database_path
    measured: list[MeasuredTurn] = []
    profile: dict[str, object] = {
        "live": live,
        "model_transport": "provider" if live else "fake",
        "configured_model": settings.deepseek_model,
        "thinking_enabled": settings.deepseek_thinking_enabled,
        "temperature": 0.75,
        "repetitions": repetitions,
        "bare_contract": "one model completion; character prompt plus delivered local transcript",
    }
    try:
        with tempfile.TemporaryDirectory() as tmp:
            scenarios = SCENARIOS[: max_cases or len(SCENARIOS)]
            for variant in ("bare", "full"):
                for run_index in range(1, repetitions + 1):
                    for scenario in scenarios:
                        temp_db = Path(
                            tmp, f"baseline-{variant}-{scenario.name}-run-{run_index}.sqlite"
                        )
                        settings.database_path = temp_db
                        engine = build_companion_engine(use_fake_model=not live)
                        history: list[dict[str, str]] = []
                        recent_questions = 0
                        try:
                            for turn_index, text in enumerate(scenario.turns, start=1):
                                message = IncomingMessage(
                                    platform="qq",
                                    platform_user_id=f"baseline-{variant}-{scenario.name}",
                                    message_id=f"{scenario.name}:{turn_index}",
                                    text=text,
                                )
                                if variant == "bare":
                                    reply_text, status, first_visible, elapsed, turn_id = (
                                        await _run_bare_baseline_turn(
                                            engine, message=message, history=history
                                        )
                                    )
                                else:
                                    reply_text, status, first_visible, elapsed, turn_id = (
                                        await _run_full_baseline_turn(engine, message=message)
                                    )
                                evaluated = (
                                    evaluate_reply(
                                        reply_text,
                                        user_text=text,
                                        recent_assistant_questions=recent_questions,
                                    )
                                    if reply_text
                                    else _evaluate_no_reply(text, is_deferred=status == "deferred")
                                )
                                measured.append(
                                    MeasuredTurn(
                                        variant=variant,
                                        scenario=scenario.name,
                                        run_index=run_index,
                                        turn_index=turn_index,
                                        cadence="cold" if turn_index == 1 else "hot",
                                        user_text=text,
                                        reply_text=reply_text,
                                        visible_status=status,
                                        first_visible_delivery_ms=first_visible,
                                        end_to_end_complete_ms=elapsed,
                                        model_usage=_usage_for_turn(engine, turn_id),
                                        issues=tuple(issue.code for issue in evaluated.issues),
                                    )
                                )
                                if reply_text:
                                    history.extend(
                                        [
                                            {"role": "user", "content": text},
                                            {"role": "assistant", "content": reply_text},
                                        ]
                                    )
                                    history[:] = history[-16:]
                                    recent_questions = reply_text.count("？") + reply_text.count("?")
                                else:
                                    recent_questions = 0
                        finally:
                            close = getattr(engine, "aclose", None)
                            if callable(close):
                                await close()
    finally:
        settings.database_path = original_db
    summaries = summarize_baseline_turns(measured)
    return BaselineReport(
        profile,
        tuple(measured),
        baseline_definition(),
        summaries,
        assess_baseline(summaries, live=live),
    )


async def _run_bare_baseline_turn(
    engine, *, message: IncomingMessage, history: list[dict[str, str]]
) -> tuple[str, str, int | None, int, str]:
    turn_id = f"baseline:bare:{message.message_id}"
    started = monotonic()
    with model_turn_scope(turn_id=turn_id, cadence="baseline"):
        with model_call_scope("bare_reply"):
            text = await engine.model.complete(
                [
                    {"role": "system", "content": engine.companion_system_prompt},
                    *history[-16:],
                    {"role": "user", "content": message.text},
                ],
                temperature=0.75,
            )
    elapsed = max(0, int((monotonic() - started) * 1000))
    reply_text = sanitize_chat_text(text)
    return reply_text, "delivered" if reply_text else "failed", elapsed, elapsed, turn_id


async def _run_full_baseline_turn(
    engine, *, message: IncomingMessage
) -> tuple[str, str, int | None, int, str]:
    transport = CaptureTurnTransport(receipt_namespace="baseline")
    turn = CompanionTurn(engine, transport)
    started = monotonic()
    outcome = await turn.respond(
        TurnEnvelope.from_message(
            message,
            idempotency_key=f"{message.platform}:{message.platform_user_id}:{message.message_id}",
        ),
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
    )
    await turn.wait_for_delivery_continuations()
    elapsed = max(0, int((monotonic() - started) * 1000))
    first_visible = (
        max(0, int((transport.first_dispatched_at - started) * 1000))
        if transport.first_dispatched_at is not None
        else None
    )
    return transport.text.strip(), outcome.visible_status, first_visible, elapsed, outcome.turn_id


def _usage_for_turn(engine, turn_id: str) -> dict[str, object]:
    report = engine.store.model_usage_report("day", utc_now())
    turns = report.get("turns", {})
    return dict(turns.get(turn_id, {})) if isinstance(turns, dict) else {}


async def run_scenario_suite(
    *, live: bool = False, max_cases: int | None = None
) -> ScenarioSummary:
    """Run isolated scenarios and classify invariant failures separately from style."""
    return summarize_results(await run_scenarios(live=live, max_cases=max_cases))


def _evaluate_no_reply(user_text: str, *, is_deferred: bool = False) -> ReplyEval:
    if is_deferred:
        return ReplyEval("<deferred>", [])
    if classify_message(user_text) in {"minimal_response", "farewell"}:
        return ReplyEval("<no reply>", [])
    return ReplyEval(
        "<no reply>", [ReplyIssue("missed_reply", "meaningful user message received no reply")]
    )


def format_results(results: list[tuple[str, str, ReplyEval]]) -> str:
    lines: list[str] = []
    for scenario, user_text, result in results:
        issue_text = ", ".join(issue.code for issue in result.issues) or "ok"
        lines.append(f"[{scenario}] user={user_text}")
        lines.append(f"score={result.score} issues={issue_text}")
        lines.append(f"reply={result.text}")
        lines.append("")
    return "\n".join(lines).strip()


def format_context_results(results: list[ContextEval]) -> str:
    return "\n".join(
        f"[{result.name}] score={result.score} issues="
        f"{', '.join(issue.code for issue in result.issues) or 'ok'}"
        for result in results
    )


def format_baseline_report(report: BaselineReport) -> str:
    lines = [json.dumps(report.model_profile, ensure_ascii=False, sort_keys=True)]
    for turn in report.turns:
        lines.append(
            "[{}:{}:{}] visible={} first_visible_delivery_ms={} end_to_end_complete_ms={} "
            "calls={} total_tokens={} issues={}".format(
                turn.variant,
                turn.scenario,
                f"run={turn.run_index}:turn={turn.turn_index}:{turn.cadence}",
                turn.visible_status,
                turn.first_visible_delivery_ms,
                turn.end_to_end_complete_ms,
                turn.model_usage.get("calls", 0),
                turn.model_usage.get("total_tokens", 0),
                ",".join(turn.issues) or "ok",
            )
        )
    lines.append("baseline summaries:")
    for summary in report.summaries:
        lines.append(
            "[summary:{variant}:{cadence}] samples={sample_count} delivered={delivered_count} "
            "visible={visible_count} p50_visible_ms={p50_first_visible_delivery_ms} "
            "p95_visible_ms={p95_first_visible_delivery_ms} p95_complete_ms="
            "{p95_end_to_end_complete_ms} calls={model_calls} tokens={total_tokens} "
            "reasoning_tokens={reasoning_tokens} issues={issue_count} hard_issues="
            "{hard_issue_count}".format(**summary.as_dict())
        )
    comparison = report.comparison
    lines.append(
        "baseline verdict={} hot_samples={} full_hot_p50_ms={} full_hot_p95_ms={} "
        "bare_hot_p95_ms={} permitted_full_hot_p95_ms={}".format(
            comparison.status,
            comparison.hot_samples_per_variant,
            comparison.full_hot_p50_ms,
            comparison.full_hot_p95_ms,
            comparison.bare_hot_p95_ms,
            comparison.permitted_full_hot_p95_ms,
        )
    )
    lines.extend(f"baseline evidence: {reason}" for reason in comparison.reasons)
    return "\n".join(lines)


def format_pragmatic_metrics(metrics: PragmaticMetrics) -> str:
    return (
        f"cases={metrics.cases} precision={metrics.precision:.3f} "
        f"recall={metrics.recall:.3f} f1={metrics.f1:.3f} "
        f"calibration_error={metrics.calibration_error:.3f} "
        f"target_accuracy={metrics.target_accuracy:.3f} "
        f"severity_mae={metrics.severity_mae:.3f}"
    )


def summarize_results(
    results: list[tuple[str, str, ReplyEval]],
) -> ScenarioSummary:
    hard: list[ReplyIssue] = []
    diagnostic: list[ReplyIssue] = []
    for _scenario, _user_text, result in results:
        for issue in result.issues:
            (hard if issue.code in _HARD_ISSUE_CODES else diagnostic).append(issue)
    return ScenarioSummary(results, hard, diagnostic)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate companion replies for human IM feel.")
    parser.add_argument("--live", action="store_true", help="Use configured DeepSeek model.")
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument(
        "--baseline", action="store_true", help="Compare bare one-call chat with the full turn path."
    )
    parser.add_argument(
        "--report", type=Path, default=None, help="Optional JSON file for a baseline run."
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Independent isolated repetitions per bare/full scenario (baseline only).",
    )
    parser.add_argument(
        "--assert-live-slo",
        action="store_true",
        help="Fail unless a live baseline has enough samples and meets the hot latency SLO.",
    )
    parser.add_argument(
        "--context", action="store_true", help="Run deterministic context-selection regressions."
    )
    parser.add_argument(
        "--pragmatic",
        action="store_true",
        help="Run deterministic Chinese-pragmatics adversarial labels.",
    )
    args = parser.parse_args(argv)
    if args.context:
        results = run_context_scenarios()
        print(format_context_results(results))
        return int(any(result.issues for result in results))
    if args.pragmatic:
        metrics = run_pragmatic_adversarial_eval()
        print(format_pragmatic_metrics(metrics))
        return int(metrics.precision < 0.80 or metrics.recall < 0.90 or metrics.f1 < 0.85)
    if args.baseline:
        report = asyncio.run(
            run_baseline_scenarios(
                live=args.live,
                max_cases=args.max_cases,
                repetitions=args.repetitions,
            )
        )
        if args.report:
            args.report.write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(format_baseline_report(report))
        return int(args.assert_live_slo and report.comparison.status != "pass")
    if args.repetitions != 1 or args.assert_live_slo:
        parser.error("--repetitions and --assert-live-slo require --baseline")
    summary = asyncio.run(run_scenario_suite(live=args.live, max_cases=args.max_cases))
    print(format_results(summary.results))
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
