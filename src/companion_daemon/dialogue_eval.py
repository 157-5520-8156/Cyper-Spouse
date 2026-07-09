from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile

from companion_daemon.config import get_settings
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.models import IncomingMessage
from companion_daemon.qq_websocket import QQMessageCoalescer
from companion_daemon.reply_decision import classify_message
from companion_daemon.runtime import build_companion_engine
from companion_daemon.sanitize import sanitize_chat_text
from companion_daemon.turn_taking import TurnTakingPolicy


_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]|\*[^*]{1,80}\*")
_ACQUAINTANCE_CRUTCH_RE = re.compile(
    r"(?:我(?:好像)?(?:有|认识)(?:个|一个)?[^。！？]{0,10}(?:朋友|同学|室友|舍友)|"
    r"(?:朋友|同学|室友|舍友)(?:也|之前|跟我|和我|发|拍|说))"
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
_PROBLEM_SOLVER_PHRASES = ("你可以", "建议你", "不妨", "解决方案", "步骤")
_UNGROUNDED_LOCAL_DETAIL_RE = re.compile(
    r"(?:刷到|听说|听人说|好像).{0,16}(?:你们学校|学校附近|附近|后门|校门口).{0,20}"
    r"(?:有家|有个|一条|那家|店|书店|小吃|串串|冰粉)"
)
_UNGROUNDED_SELF_EVENT_RE = re.compile(
    r"我(?:明天|今天|等会儿|一会儿|待会儿)也(?:有|要|得).{0,14}"
    r"(?:一门|考试|复习|上课|交作业|开会|pre|presentation|汇报|展示)"
)
_STEREOTYPE_REPLY_RE = re.compile(r"(?:成都|四川).{0,8}(?:好吃|美食|火锅|串串)")
_UNSUPPORTED_MEMORY_CLAIM_RE = re.compile(
    r"(?:你之前|我记得你|我记得之前|你上次|之前你|之前听你).{0,24}(?:说过|提过|聊过|告诉我|说|群里)"
)
_UNSUPPORTED_FAMILIARITY_RE = re.compile(r"(?:之前)?(?:有)?(?:听说过|刷到过|了解过|查过那边|做[^。！？]{0,12}笔记)")
_QUESTION_NAG_RE = re.compile(r"(?:我刚|刚才|刚刚)问(?:你)?的(?:问题)?(?:你)?(?:好像)?还没回")
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


@dataclass(frozen=True)
class EvalScenario:
    name: str
    turns: list[str]


class _EvalReplyTarget:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, **kwargs: object) -> None:
        content = kwargs.get("content")
        if isinstance(content, str) and content:
            self.replies.append(content)


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
]


def evaluate_reply(text: str, *, user_text: str = "", recent_assistant_questions: int = 0) -> ReplyEval:
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
        issues.append(ReplyIssue("acquaintance_crutch", "uses friend/classmate/roommate as a chat crutch"))
    if any(phrase in text for phrase in _ASSISTANT_PHRASES):
        issues.append(ReplyIssue("assistantese", "contains assistant-like connective phrasing"))
    if any(phrase in text for phrase in _PROBLEM_SOLVER_PHRASES):
        issues.append(ReplyIssue("problem_solver", "sounds like solving instead of chatting"))
    if _has_flattened_question(text) or _has_flattened_question(cleaned):
        issues.append(ReplyIssue("flattened_question", "question particle was flattened into a period"))
    if _UNGROUNDED_LOCAL_DETAIL_RE.search(cleaned):
        issues.append(ReplyIssue("ungrounded_local_detail", "invents a specific local detail as if she knows it"))
    if _UNGROUNDED_SELF_EVENT_RE.search(cleaned):
        issues.append(ReplyIssue("ungrounded_self_event", "mirrors the user's situation with an unsupported same-day event"))
    if "成都理工" in user_text and _STEREOTYPE_REPLY_RE.search(cleaned):
        issues.append(ReplyIssue("stereotype_reply", "answers a specific school detail with a generic city stereotype"))
    if _UNSUPPORTED_MEMORY_CLAIM_RE.search(text) or _UNSUPPORTED_MEMORY_CLAIM_RE.search(cleaned):
        issues.append(ReplyIssue("unsupported_memory_claim", "claims prior memory not grounded in the current eval context"))
    if _QUESTION_NAG_RE.search(cleaned):
        issues.append(ReplyIssue("question_nag", "nags the user for not answering an earlier question"))
    if "成都理工" in user_text and (_UNSUPPORTED_FAMILIARITY_RE.search(text) or _UNSUPPORTED_FAMILIARITY_RE.search(cleaned)):
        issues.append(ReplyIssue("unsupported_familiarity_claim", "claims familiarity with a specific school without grounding"))
    if "你也在成都" in text or "你也在成都" in cleaned:
        issues.append(ReplyIssue("persona_location_confusion", "implies she is also in Chengdu despite her Shanghai persona"))
    if _is_emotional_user_text(user_text) and _is_question_only(cleaned):
        issues.append(ReplyIssue("emotion_question_only", "responds to emotion with only a question"))
    if _INCOMPLETE_TRAILING_RE.search(cleaned):
        issues.append(ReplyIssue("incomplete_trailing", "reply trails off as an unfinished sentence"))
    if _is_low_engagement(cleaned, user_text):
        issues.append(ReplyIssue("low_engagement", "reply is too thin for the user's meaningful message"))
    if _is_echo_only(cleaned, user_text):
        issues.append(ReplyIssue("echo_only", "mostly repeats the user's topic without adding a reaction"))
    if question_count > 1:
        issues.append(ReplyIssue("too_many_questions", f"has {question_count} question marks"))
    if recent_assistant_questions and cleaned_question_count:
        issues.append(ReplyIssue("question_after_question", "asks again after recent assistant question"))
    if len(cleaned) > 95:
        issues.append(ReplyIssue("too_long", f"{len(cleaned)} chars is long for QQ private chat"))
    if line_count > 3:
        issues.append(ReplyIssue("too_many_lines", f"{line_count} lines feels like a composed answer"))
    return ReplyEval(cleaned, issues)


def _is_meaningful_user_text(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) >= 8:
        return True
    return any(token in stripped for token in ("累", "难", "烦", "考试", "上学", "老师", "下雨", "伞", "成都"))


def _is_emotional_user_text(text: str) -> bool:
    return any(token in text for token in ("累", "闷", "难过", "烦", "委屈", "不开心", "心里", "难受"))


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
    return all(sentence.endswith(("？", "?")) or _has_flattened_question(sentence) for sentence in sentences)


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


async def run_scenarios(*, live: bool = False, max_cases: int | None = None) -> list[tuple[str, str, ReplyEval]]:
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
                seed_user(CompanionStore(temp_db, primary_user_id=settings.primary_user_id))
                recent_questions = 0
                target = _EvalReplyTarget()
                coalescer = QQMessageCoalescer(
                    engine,
                    delay_seconds=0.01,
                    turn_policy=TurnTakingPolicy(short_wait_seconds=0.01, long_wait_seconds=0.01),
                    enable_reply_decision=True,
                )
                for text in scenario.turns:
                    before = len(target.replies)
                    platform_user_id = f"eval-user-{scenario.name}"
                    await coalescer.add(
                        f"c2c:{platform_user_id}",
                        IncomingMessage(platform="qq", platform_user_id=platform_user_id, text=text),
                        target,
                    )
                    task = coalescer._tasks.get(f"c2c:{platform_user_id}")
                    if task:
                        with suppress(asyncio.CancelledError):
                            await task
                    reply_text = "\n".join(target.replies[before:]).strip()
                    if not reply_text:
                        evaluated = _evaluate_no_reply(text)
                        results.append((scenario.name, text, evaluated))
                        recent_questions = 0
                        continue
                    evaluated = evaluate_reply(
                        reply_text, user_text=text, recent_assistant_questions=recent_questions
                    )
                    results.append((scenario.name, text, evaluated))
                    recent_questions = reply_text.count("？") + reply_text.count("?")
            return results
        finally:
            settings.database_path = original_db


def _evaluate_no_reply(user_text: str) -> ReplyEval:
    if classify_message(user_text) == "ack":
        return ReplyEval("<no reply>", [])
    return ReplyEval("<no reply>", [ReplyIssue("missed_reply", "meaningful user message received no reply")])


def format_results(results: list[tuple[str, str, ReplyEval]]) -> str:
    lines: list[str] = []
    for scenario, user_text, result in results:
        issue_text = ", ".join(issue.code for issue in result.issues) or "ok"
        lines.append(f"[{scenario}] user={user_text}")
        lines.append(f"score={result.score} issues={issue_text}")
        lines.append(f"reply={result.text}")
        lines.append("")
    return "\n".join(lines).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate companion replies for human IM feel.")
    parser.add_argument("--live", action="store_true", help="Use configured DeepSeek model.")
    parser.add_argument("--max-cases", type=int, default=None)
    args = parser.parse_args()
    results = asyncio.run(run_scenarios(live=args.live, max_cases=args.max_cases))
    print(format_results(results))


if __name__ == "__main__":
    main()
