from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import re
import tempfile

from companion_daemon.config import get_settings
from companion_daemon.db import CompanionStore
from companion_daemon.engine import seed_user
from companion_daemon.models import IncomingMessage
from companion_daemon.runtime import build_companion_engine
from companion_daemon.sanitize import sanitize_chat_text


_STAGE_DIRECTION_RE = re.compile(r"[（(][^（）()]{1,80}[）)]|\*[^*]{1,80}\*")
_ACQUAINTANCE_CRUTCH_RE = re.compile(
    r"(?:我(?:有|认识)(?:个|一个)?[^。！？]{0,10}(?:朋友|同学|室友|舍友)|"
    r"(?:朋友|同学|室友|舍友)(?:也|之前|跟我|和我))"
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


def evaluate_reply(text: str, *, recent_assistant_questions: int = 0) -> ReplyEval:
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
    if re.search(r"[吗呢呀么]。", cleaned) or re.search(r"(哪|怎么|为什么|什么时候|多少|谁)[^。！？]*。", cleaned):
        issues.append(ReplyIssue("flattened_question", "question particle was flattened into a period"))
    if question_count > 1:
        issues.append(ReplyIssue("too_many_questions", f"has {question_count} question marks"))
    if recent_assistant_questions and cleaned_question_count:
        issues.append(ReplyIssue("question_after_question", "asks again after recent assistant question"))
    if len(cleaned) > 95:
        issues.append(ReplyIssue("too_long", f"{len(cleaned)} chars is long for QQ private chat"))
    if line_count > 3:
        issues.append(ReplyIssue("too_many_lines", f"{line_count} lines feels like a composed answer"))
    return ReplyEval(cleaned, issues)


async def run_scenarios(*, live: bool = False, max_cases: int | None = None) -> list[tuple[str, str, ReplyEval]]:
    settings = get_settings()
    with tempfile.TemporaryDirectory() as tmp:
        temp_db = Path(tmp) / "eval.sqlite"
        original_db = settings.database_path
        settings.database_path = temp_db
        try:
            engine = build_companion_engine(use_fake_model=not live)
            seed_user(CompanionStore(temp_db, primary_user_id=settings.primary_user_id))
            results: list[tuple[str, str, ReplyEval]] = []
            scenarios = SCENARIOS[: max_cases or len(SCENARIOS)]
            for scenario in scenarios:
                recent_questions = 0
                for text in scenario.turns:
                    reply = await engine.handle_message(
                        IncomingMessage(platform="qq", platform_user_id="eval-user", text=text)
                    )
                    if not reply:
                        continue
                    evaluated = evaluate_reply(
                        reply.text, recent_assistant_questions=recent_questions
                    )
                    results.append((scenario.name, text, evaluated))
                    recent_questions = reply.text.count("？") + reply.text.count("?")
            return results
        finally:
            settings.database_path = original_db


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
