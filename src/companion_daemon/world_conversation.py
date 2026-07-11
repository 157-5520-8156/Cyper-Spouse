"""Deterministic scope classification for world-grounded conversation queries."""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal


QueryTarget = Literal["user", "companion", "conversation", "unknown"]
TimeReference = Literal["昨天", "今天", "上次"]


@dataclass(frozen=True)
class WorldQueryScope:
    """The subject and world read model a user utterance is asking about."""

    target: QueryTarget
    time_reference: TimeReference | None = None
    day_part: Literal["上午", "下午"] | None = None
    asks_current_scene: bool = False
    asks_experience: bool = False
    asks_availability: bool = False


_USER_TIME = re.compile(r"(?:我|我的).{0,8}(?:昨天|今天|上午|下午|上次|刚才)")
_COMPANION_CURRENT = re.compile(
    r"(?:你|知栀).{0,5}(?:现在|这会儿|此刻|在哪|在做什么|干嘛|忙吗|方便说话)"
)
_IMPLICIT_CURRENT = re.compile(r"(?:现在|这会儿|此刻)?.{0,3}(?:在哪|在做什么|在干嘛|干嘛|忙吗|方便说话)")
_COMPANION_EXPERIENCE = re.compile(
    r"(?:你|知栀).{0,6}(?:昨天|今天|上午|下午|上次).{0,12}"
    r"(?:做|干|去|忙|经历|发生|过得|完成|见|聊|看)"
)
_COMPANION_EXPERIENCE_REVERSED = re.compile(
    r"(?:昨天|今天|上午|下午|上次).{0,8}(?:你|知栀).{0,8}"
    r"(?:做|干|去|忙|经历|发生|过得|完成|见|聊|看)"
)
_COMPANION_MEMORABLE = re.compile(r"(?:你|知栀).{0,8}(?:今天|昨天).{0,8}(?:想记住|印象最深|最难忘)")


def classify_world_query(text: str) -> WorldQueryScope:
    """Classify only explicit subjects; ambiguity must not authorize a world fallback."""
    normalized = "".join(text.split())
    time_reference: TimeReference | None = None
    if "昨天" in normalized:
        time_reference = "昨天"
    elif any(marker in normalized for marker in ("今天", "上午", "下午")):
        time_reference = "今天"
    elif "上次" in normalized:
        time_reference = "上次"
    day_part = "上午" if "上午" in normalized else "下午" if "下午" in normalized else None

    if _USER_TIME.search(normalized):
        return WorldQueryScope(
            target="user",
            time_reference=time_reference,
            day_part=day_part,
        )
    if _COMPANION_CURRENT.search(normalized) or (
        "我现在" not in normalized and _IMPLICIT_CURRENT.fullmatch(normalized.rstrip("？?"))
    ):
        return WorldQueryScope(
            target="companion",
            asks_current_scene=True,
            asks_availability=any(marker in normalized for marker in ("忙吗", "方便说话")),
        )
    if (
        _COMPANION_EXPERIENCE.search(normalized)
        or _COMPANION_EXPERIENCE_REVERSED.search(normalized)
        or _COMPANION_MEMORABLE.search(normalized)
    ):
        return WorldQueryScope(
            target="companion",
            time_reference=time_reference,
            day_part=day_part,
            asks_experience=True,
        )
    return WorldQueryScope(
        target="conversation" if any(marker in normalized for marker in ("刚才", "之前", "还记得")) else "unknown",
        time_reference=time_reference,
        day_part=day_part,
    )


def build_safe_failure_candidate(
    user_text: str,
    grounded: dict[str, object] | None,
) -> dict[str, object]:
    """Preserve the user's speech act when both bounded model attempts fail."""
    asks_for_missing_detail = asks_for_source_detail(user_text)
    if "晚安" in user_text and any(marker in user_text for marker in ("就好", "一句", "只")):
        return {
            "reply_text": "晚安。", "mentioned_event_ids": [],
            "proposed_action_ids": [], "claims": [],
        }
    if "数据" in user_text and any(marker in user_text for marker in ("找回", "恢复", "没丢")):
        return {
            "reply_text": "找回来就好，先缓口气。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if "数据" in user_text and any(marker in user_text for marker in ("丢", "没了", "找不到")):
        return {
            "reply_text": "在，我先回你。先别继续覆盖数据，告诉我你最后一次确认它还在是什么时候。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if any(marker in user_text for marker in ("脑子还停不下来", "脑子停不下来", "睡不着")):
        return {
            "reply_text": "那就先别逼自己马上睡着。慢慢缓一会儿，我陪你安静一下。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if "角色卡" in user_text and any(marker in user_text for marker in ("真心", "关心")):
        return {
            "reply_text": "角色卡会影响我怎么表达，但不该成为糊弄你的借口。你有没有被接住，比我怎么自证更重要。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if "活人" in user_text and any(marker in user_text for marker in ("像", "聊天")):
        return {
            "reply_text": "有些时候像，有些时候还是会露出很重的机制感。至少这段里，我确实有几次没接好。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if grounded:
        source_text = str(grounded.get("reply_text") or "").strip()
        if _normalized_echo(source_text) == _normalized_echo(user_text):
            grounded = None
        elif source_text and (
            bool(grounded.get("_user_sourced"))
            or any(
                str(source_id).startswith("message:")
                for source_id in grounded.get("mentioned_event_ids", [])  # type: ignore[union-attr]
            )
        ):
            claim_texts = [
                str(item.get("text") or "").strip()
                for item in grounded.get("claims", [])  # type: ignore[union-attr]
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            if len(claim_texts) > 1:
                joined = "，以及".join(f"“{text}”" for text in claim_texts)
                return {**grounded, "reply_text": f"我记得你之前分别提过：{joined}"}
            return {
                **grounded,
                "reply_text": f"我记得你之前提过：“{source_text}”",
            }
    if grounded:
        source_text = str(grounded.get("reply_text") or "").strip()
        if source_text and asks_for_missing_detail:
            return {
                **grounded,
                "reply_text": (
                    f"我只确定这件事：{source_text}"
                    "至于你问的细节，我这里没有能确认的记录，不想乱说。"
                ),
            }
        if source_text:
            return grounded
    if any(marker in user_text for marker in ("不舒服", "胃疼", "肚子疼", "头疼", "难受")):
        return {
            "reply_text": "听着就挺难受的。先别硬撑，缓一会儿。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if any(marker in user_text for marker in ("没怎么睡", "没睡", "失眠", "熬夜")):
        return {
            "reply_text": "听着强度不小。先顾眼前最要紧的，别一直硬扛。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if any(marker in user_text for marker in ("担心", "害怕", "焦虑", "怕最后")):
        return {
            "reply_text": "我明白你在担心什么。先别急着给它判死刑，我们一点点看。",
            "mentioned_event_ids": [], "proposed_action_ids": [], "claims": [],
        }
    if any(marker in user_text for marker in ("？", "?", "吗", "什么", "怎么", "为什么", "觉得")):
        return {
            "reply_text": "这个我现在没有把握，不想随口糊弄你。",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        }
    return {
        "reply_text": "我在听，刚才那句我没有接好。",
        "mentioned_event_ids": [],
        "proposed_action_ids": [],
        "claims": [],
    }


def asks_for_source_detail(user_text: str) -> bool:
    return any(
        marker in user_text
        for marker in ("是谁", "为什么", "顺利吗", "怎么认识", "具体怎么", "说了什么")
    )


def conversation_fact_candidate(user_text: str) -> str | None:
    """Select a direct high-signal user statement for cross-day recall."""
    text = user_text.strip()
    if (
        not text
        or len(text) > 180
        or not re.search(r"(?:^|[，。！？])我", text)
        or text.endswith(("?", "？"))
    ):
        return None
    high_signal = (
        "因为", "所以", "项目", "工作", "赶工", "加班", "睡", "失眠", "熬夜",
        "胃", "医院", "生病", "难过", "焦虑", "明天", "后天", "今晚", "计划",
    )
    return text if any(marker in text for marker in high_signal) else None


def only_repeats_claimed_sources(user_text: str, candidate: dict[str, object]) -> bool:
    """Detect a provenance-correct quote that does not answer a detail question."""
    if not asks_for_source_detail(user_text):
        return False
    reply_text = str(candidate.get("reply_text") or "").strip()
    claims = candidate.get("claims")
    if not isinstance(claims, list) or not claims:
        return False
    quoted = [
        str(item.get("text") or "").strip()
        for item in claims
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    return bool(quoted) and reply_text in {"".join(quoted), "。".join(quoted)}


def only_recites_irrelevant_sources(
    user_text: str, candidate: dict[str, object]
) -> bool:
    """Catch a grounded fact dump that does not answer the current speech act."""
    claims = candidate.get("claims")
    if not isinstance(claims, list) or not claims:
        return False
    reply = _normalized_echo(str(candidate.get("reply_text") or ""))
    claimed = "".join(
        _normalized_echo(str(item.get("assertion") or item.get("text") or ""))
        for item in claims
        if isinstance(item, dict)
    )
    if not reply or not claimed or len(claimed) / len(reply) < 0.65:
        return False
    speech_act_markers = (
        "你觉得", "怎么看", "陪我", "吐槽", "担心", "害怕", "如果", "是不是",
        "安慰", "别劝", "不用讲道理", "怎么告诉",
    )
    if any(marker in user_text for marker in speech_act_markers):
        return True
    ignored = set("你我他她的是了在有还就也说觉得吗呢啊这那一个")
    query_chars = set(_normalized_echo(user_text)) - ignored
    claim_chars = set(claimed) - ignored
    return len(query_chars & claim_chars) < 4


def only_echoes_user_message(user_text: str, candidate: dict[str, object]) -> bool:
    """Reject punctuation-only mirroring of the current inbound message."""
    return bool(user_text.strip()) and _normalized_echo(
        str(candidate.get("reply_text") or "")
    ) == _normalized_echo(user_text)


def repeats_recent_companion_reply(
    candidate: dict[str, object], recent_messages: list[dict[str, object]]
) -> bool:
    """Reject a near-copy of either of the last two delivered companion turns."""
    reply = _normalized_echo(str(candidate.get("reply_text") or ""))
    if not reply:
        return False
    outgoing = [
        _normalized_echo(str(item.get("text") or ""))
        for item in recent_messages
        if item.get("direction") == "out" and str(item.get("text") or "").strip()
    ][-2:]
    return any(
        previous
        and (
            reply == previous
            or SequenceMatcher(None, reply, previous, autojunk=False).ratio() >= 0.9
        )
        for previous in outgoing
    )


def _normalized_echo(text: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:\"'“”‘’（）()…]+", "", text)
