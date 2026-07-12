from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile

from companion_daemon.config import get_settings
from companion_daemon.context_orchestrator import build_context_package
from companion_daemon.emotion_state import interpret_interaction
from companion_daemon.interaction_appraiser import InteractionEvidence, assess_appraisal_risk
from companion_daemon.models import IncomingMessage, MoodState
from companion_daemon.reply_decision import classify_message
from companion_daemon.runtime import build_companion_engine
from companion_daemon.sanitize import sanitize_chat_text


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
    PragmaticAdversarialCase("quoted_third_party", "他说“滚开”，我听着很难受。", False, "third_party", 0),
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
        sum(case.target == pred.target for case, pred in harmful) / len(harmful)
        if harmful
        else 0.0
    )
    severity_mae = (
        sum(abs(case.severity - pred.severity) for case, pred in harmful) / len(harmful)
        if harmful
        else 0.0
    )
    return PragmaticMetrics(
        len(pairs), tp, fp, fn, tn, precision, recall, f1, ece,
        target_accuracy, severity_mae,
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
                        reply = await engine.handle_message(
                            IncomingMessage(
                                platform="qq",
                                platform_user_id=platform_user_id,
                                message_id=f"{scenario.name}:{turn_index}",
                                text=text,
                            )
                        )
                        if reply is None:
                            evaluated = _evaluate_no_reply(text)
                            results.append((scenario.name, text, evaluated))
                            recent_questions = 0
                            continue
                        reply_text = reply.text.strip()
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
    summary = asyncio.run(run_scenario_suite(live=args.live, max_cases=args.max_cases))
    print(format_results(summary.results))
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
