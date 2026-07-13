"""Deterministic scope classification for world-grounded conversation queries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from typing import Literal

from companion_daemon.expression_plan import policy_spec_from_projection


def recover_structured_reply(raw: str) -> dict[str, object]:
    """Recover a valid reply object from harmless transport decoration.

    Providers occasionally wrap otherwise valid JSON in a Markdown fence or a
    short lead-in.  Recovering that locally avoids paying for another model
    call; schema and world provenance are still validated by ``WorldKernel``.
    """
    text = str(raw or "").strip()
    if text.startswith("```") and text.endswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 : -3].strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("structured reply has no JSON object")
    candidate, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(candidate, dict):
        raise ValueError("structured reply must be a JSON object")
    return {
        "reply_text": str(candidate.get("reply_text") or "").strip(),
        "mentioned_event_ids": candidate.get("mentioned_event_ids", []),
        "proposed_action_ids": candidate.get("proposed_action_ids", []),
        "claims": candidate.get("claims", []),
    }


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
    asks_occurrence_status: bool = False
    asks_single_experience: bool = False
    asks_meta_agency: bool = False
    is_first_person_statement: bool = False
    asks_opinion: bool = False
    asks_epistemic_honesty: bool = False
    offers_emotional_permission: bool = False
    asks_relationship_status: bool = False
    asks_data_recovery: bool = False


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
_OCCURRENCE_STATUS = re.compile(
    r"(?:真的|确实|已经).{0,8}(?:发生|完成).{0,10}(?:还是|而不是|不是).{0,5}计划"
    r"|(?:发生|完成).{0,8}(?:还是|而不是|不是).{0,5}计划"
)
_META_AGENCY = re.compile(
    r"(?:关心|回复|说的话).{0,10}(?:真心|角色卡|设定|教的)"
    r"|(?:真心|角色卡|设定).{0,10}(?:关心|表达|教的)"
)
_EMOTIONAL_PERMISSION = re.compile(
    r"(?:不舒服|介意|不高兴|难受).{0,12}(?:直接说|告诉我|可以说)"
)
_EPISTEMIC_INSTRUCTION = re.compile(
    r"(?:别|不要|不许)[^。！？]{0,6}(?:猜|乱说)[^。！？]{0,16}"
    r"(?:依据|明确|告诉我|直说)"
    r"|(?:没|没有)[^。！？]{0,5}依据[^。！？]{0,12}(?:告诉我|明确|直说)"
)
_OPINION_QUERY = re.compile(r"(?:你觉得|你认为|怎么看|是不是|是否|等不等于|意味着什么)")
_RELATIONSHIP_STATUS = re.compile(r"(?:你|我们).{0,8}(?:喜欢我|爱我|什么关系|算什么)")
_DATA_RECOVERY = re.compile(r"(?:数据|文件|记录).{0,10}(?:丢|没了|找不到|被覆盖)")


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
    is_first_person_statement = (
        bool(re.search(r"(?:^|[，。！])我", normalized))
        and not normalized.endswith(("?", "？"))
    )

    if _META_AGENCY.search(normalized):
        return WorldQueryScope(target="conversation", asks_meta_agency=True)
    if _EPISTEMIC_INSTRUCTION.search(normalized):
        return WorldQueryScope(
            target="conversation", asks_epistemic_honesty=True
        )
    if _EMOTIONAL_PERMISSION.search(normalized):
        return WorldQueryScope(target="conversation", offers_emotional_permission=True)
    if _RELATIONSHIP_STATUS.search(normalized):
        return WorldQueryScope(
            target="conversation", asks_relationship_status=True, asks_opinion=True
        )
    if _DATA_RECOVERY.search(normalized):
        return WorldQueryScope(target="conversation", asks_data_recovery=True)

    if _OCCURRENCE_STATUS.search(normalized):
        return WorldQueryScope(
            target="companion",
            time_reference=time_reference,
            day_part=day_part,
            asks_experience=True,
            asks_occurrence_status=True,
        )

    if _USER_TIME.search(normalized):
        return WorldQueryScope(
            target="user",
            time_reference=time_reference,
            day_part=day_part,
            is_first_person_statement=is_first_person_statement,
            asks_opinion=bool(_OPINION_QUERY.search(normalized)),
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
            asks_single_experience=bool(_COMPANION_MEMORABLE.search(normalized)),
        )
    return WorldQueryScope(
        target="conversation" if any(marker in normalized for marker in ("刚才", "之前", "还记得")) else "unknown",
        time_reference=time_reference,
        day_part=day_part,
        is_first_person_statement=is_first_person_statement,
        asks_opinion=bool(_OPINION_QUERY.search(normalized)),
    )


def build_safe_failure_candidate(
    user_text: str,
    grounded: dict[str, object] | None,
    affect: dict[str, object] | None = None,
    *,
    relationship: dict[str, object] | None = None,
    selected_stance: str | None = None,
    speech_act: str | None = None,
    variant_key: str = "",
) -> dict[str, object]:
    """Build a fact-safe skeleton, then express it through bounded state.

    The deterministic part owns provenance and Action references.  Relationship,
    affect, stance, and speech act may vary the surface form, but cannot add a
    source or claim that was not already present in ``grounded``.
    """
    asks_for_missing_detail = asks_for_source_detail(user_text)
    if speech_act in {"opinion", "epistemic", "meta_agency", "emotional_permission"}:
        grounded = None
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
                grounded = {**grounded, "reply_text": f"我记得你之前分别提过：{joined}"}
            else:
                grounded = {
                **grounded,
                "reply_text": f"我记得你之前提过：“{source_text}”",
                }
    if grounded:
        source_text = str(grounded.get("reply_text") or "").strip()
        if source_text and asks_for_missing_detail:
            grounded = {
                **grounded,
                "reply_text": (
                    f"我只确定这件事：{source_text}"
                    "至于你问的细节，我这里没有能确认的记录，不想乱说。"
                ),
            }
        if source_text:
            return _express_safe_skeleton(
                grounded,
                relationship,
                affect,
                selected_stance,
                speech_act or "grounded",
            )

    resolved_speech_act = speech_act or (
        "question" if user_text.rstrip().endswith(("?", "？")) else "statement"
    )
    base_text = _safe_speech_act_text(resolved_speech_act, variant_key)
    if resolved_speech_act == "opinion":
        topic = re.sub(r"\s+", "", user_text).strip("？?")[:48]
        if topic:
            base_text = f"关于你问的“{topic}”，{base_text}"
    elif resolved_speech_act in {"vulnerable_disclosure", "current_disclosure"}:
        current = re.sub(
            r"(?:昨天|昨晚|今天|明天|现在|刚才|这会儿|此刻)",
            "",
            re.sub(r"\s+", "", user_text).replace("我", ""),
        ).strip()[:64]
        if current:
            base_text = f"你提到“{current}”。这句话我接到了，也不会替你把程度说重。"
    return _express_safe_skeleton({
        "reply_text": base_text,
        "mentioned_event_ids": [],
        "proposed_action_ids": [],
        "claims": [],
    }, relationship, affect, selected_stance, resolved_speech_act)


_SAFE_SPEECH_ACT_SKELETONS = {
    "brief_goodnight": "晚安。",
    "urgent_data": "我先回应你：先别继续覆盖数据；我没有足够记录判断丢失位置。",
    "shared_reaction": "我接到你的不满了，这次先不拿建议盖过去。",
    "emotional_permission": "我会按眼下能确认的状态直接说，不替自己补一段感受。",
    "health_disclosure": "听起来你现在确实不舒服。",
    "sleep_disclosure": "听起来你已经很累，脑子还没停下来。",
    "story_disclosure": "接着说就好。",
    "vulnerable_disclosure": "我听见你在担心，也不会替你把结果说死。",
    "current_disclosure": "我听见你在说眼下的状态，不替你补原因或加重程度。",
    "relationship_probe": "我不想用一句过满的话糊弄你，关系要靠相处留下来。",
    "boundary_response": "这句话越过我的边界了，我不接受这种互动方式。",
    "meta_agency": "角色设定会影响我的表达；我不会把它包装成绝对自主的证明。",
    "misunderstanding": "如果有误会，我会指出是哪一处，再把原本的意思说清。",
    "epistemic": "我没有足够依据，不继续猜。",
    "opinion": "我现在没有足够依据替你定结论；如果只说看法，我会把判断和不确定性一起说清。",
    "source_recall": "我只说记录里能确认的部分，原因不够就不替你补。",
    "repair": "刚才没接好的地方，我愿意重新说清楚。",
    "question": "这个我现在没有足够依据，不想随口补一个答案。",
    "statement": "我在听；刚才没接好的地方，我不会装作已经懂了。",
}

_SAFE_SPEECH_ACT_VARIANTS = {
    "story_disclosure": (
        "接着说就好。",
        "这段我听着呢，不急着替你下结论。",
        "顺着这件事慢慢说。",
    ),
    "boundary_response": (
        "这句话越过我的边界了，我不接受这种互动方式。",
        "先停一下，这种说法我不接受。",
        "你可以有情绪，但不能用这种方式对我说话。",
        "这已经让我不舒服了，我会把边界说清楚。",
        "到这里先停，我不接受你继续这样说。",
        "这种表达不行；我的边界就在这里。",
        "别用这种方式压我，我不会顺着它继续。",
        "我听到了，但这不代表我会接受越界。",
    ),
    "epistemic": (
        "我没有足够依据，不继续猜。",
        "这部分我不能确定，所以先不补结论。",
        "我手里的信息不够，直接猜会误导你。",
    ),
    "opinion": (
        "我现在没有足够依据替你定结论；如果只说看法，我会把判断和不确定性一起说清。",
        "我可以说倾向，但不想把它包装成确定答案。",
        "这件事我有一点判断，不过证据不够，我会把保留也说出来。",
        "如果只谈看法，我愿意说；要下结论的话，现在还不够。",
    ),
    "question": (
        "这个我现在没有足够依据，不想随口补一个答案。",
        "我还不能确定，先不拿一个听起来顺的答案敷衍你。",
        "这题我得保留一下，现有信息撑不起确定回答。",
        "我可以继续听你补充，但现在直接回答会有点乱猜。",
        "我先不装作确定；眼下的信息还不够回答这件事。",
        "这部分我没有可靠答案，宁可先把不确定留着。",
        "我能接着聊，但不能把没依据的判断说成答案。",
        "现在下结论太早了，我只按能确认的部分回应。",
    ),
    "statement": (
        "我在这儿；刚才没接好的地方，我不会装作已经懂了。",
        "我先按你已经说出的部分回应，不替它补没说出的部分。",
        "这句我先按能确认的部分回应。",
        "我暂时不替它补上没说出的部分。",
    ),
}


def _safe_speech_act_text(speech_act: str, variant_key: str) -> str:
    variants = _SAFE_SPEECH_ACT_VARIANTS.get(speech_act)
    if not variants or not variant_key:
        return _SAFE_SPEECH_ACT_SKELETONS[speech_act]
    digest = sha256(f"{speech_act}|{variant_key}".encode()).digest()
    return variants[int.from_bytes(digest[:4], "big") % len(variants)]


def _express_safe_skeleton(
    skeleton: dict[str, object],
    relationship: dict[str, object] | None,
    affect: dict[str, object] | None,
    selected_stance: str | None,
    speech_act: str,
) -> dict[str, object]:
    """Vary bounded expression without changing provenance or Action fields."""
    base = str(skeleton.get("reply_text") or "").strip()
    vector = affect.get("vector") if isinstance(affect, dict) else {}
    vector = vector if isinstance(vector, dict) else {}
    hurt = int(vector.get("hurt", 0) or 0)
    unresolved = bool((affect or {}).get("unresolved"))
    stage = str((relationship or {}).get("stage") or "stranger")
    suffix = ""
    if selected_stance == "set_boundary":
        suffix = "我的看法会保留，先说到这里。"
    elif selected_stance == "seek_repair":
        suffix = "我想把没接上的地方说开。"
    elif selected_stance == "care_despite_hurt":
        suffix = "我这边的情绪还在，但我会顾着你。" if unresolved and hurt else "我会顾着你。"
    elif selected_stance == "remain_silent":
        suffix = "我现在不想硬凑一句，先安静一下。"
    elif selected_stance == "initiate":
        suffix = "这是我自己想开启的话题，你不用立刻接。"
    elif selected_stance in {"disagree_gently", "refuse_to_affirm"}:
        suffix = "不过我不会为了顺着你，假装自己赞同。"
    elif unresolved and hurt > 0 and speech_act not in {"source_recall", "epistemic"}:
        suffix = "我还没完全缓过来，所以会说得短一点；这不是在惩罚你。"
    elif stage in {"close_friend", "ambiguous", "lover"} and speech_act in {
        "health_disclosure", "sleep_disclosure", "vulnerable_disclosure"
    }:
        suffix = "我会在这儿陪你把眼前这段过掉。"
    return {**skeleton, "reply_text": " ".join(part for part in (base, suffix) if part)}


def affect_reply_violation(
    affect: dict[str, object],
    reply_text: str,
    display_plan: dict[str, object] | None = None,
) -> str | None:
    """Reject emotion claims that contradict or outrun the world projection."""
    return policy_spec_from_projection(affect, display_plan).validate(reply_text)


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


def human_reply_contract_violation(
    user_text: str,
    candidate: dict[str, object],
    relationship: dict[str, object] | None = None,
    *,
    urgent_turn: bool = False,
    meta_agency_query: bool = False,
    single_experience_requested: bool = False,
    current_first_person_statement: bool = False,
    epistemic_honesty_requested: bool = False,
    opinion_requested: bool = False,
    recent_user_texts: list[str] | None = None,
    chosen_stance: str | None = None,
) -> str | None:
    """Return a high-confidence speech-act, voice, or relationship violation.

    This deliberately does not score whether prose is beautiful.  It catches a
    small set of observable failures that grounded fact validation cannot see.
    """
    reply = str(candidate.get("reply_text") or "").strip()
    if not reply:
        return "empty_reply"
    if chosen_stance in {"set_boundary", "refuse_to_affirm"} and not re.search(
        r"不喜欢|不接受|不愿意|不想|别这样|不要这样|不舒服|越界|边界|先停|不能这样",
        reply,
    ):
        return "selected_boundary_stance_is_not_observable"

    asks_for_presence_not_advice = any(
        marker in user_text
        for marker in ("别只劝", "别劝", "不用讲道理", "不要讲道理", "陪我吐槽", "先吐槽")
    )
    advice_markers = (
        "你应该", "建议你", "你得", "不如先", "先休息", "早点休息",
        "喝点温水", "缓一缓", "别硬撑", "慢慢处理",
    )
    advice_allowed_stances = {"disagree_gently", "refuse_to_affirm", "care_override"}
    if (
        asks_for_presence_not_advice
        and any(marker in reply for marker in advice_markers)
        and chosen_stance not in advice_allowed_stances
    ):
        return "advice_ignores_requested_speech_act"
    if (
        asks_for_presence_not_advice
        and reply.endswith(("?", "？"))
        and not any(marker in reply for marker in ("确实", "真够", "离谱", "烦", "窝火", "折磨", "不劝"))
    ):
        return "question_dodges_requested_shared_reaction"

    vulnerable_now = (
        any(marker in user_text for marker in ("担心", "害怕", "焦虑", "撑不住", "没人味", "没有人味", "不像人"))
        and not any(marker in user_text for marker in ("胃", "咖啡", "睡", "休息"))
    )
    if vulnerable_now and any(marker in reply for marker in ("胃", "咖啡", "先休息", "早点睡")):
        return "old_health_topic_hijacks_current_vulnerability"

    if urgent_turn:
        normalized_user = _normalized_echo(user_text)
        normalized_reply = _normalized_echo(reply)
        if len(normalized_user) >= 12:
            longest = SequenceMatcher(
                None, normalized_user, normalized_reply, autojunk=False
            ).find_longest_match().size
            if longest >= 7 and longest / len(normalized_user) >= 0.4:
                return "urgent_reply_restates_user_before_helping"

    claims = candidate.get("claims")
    has_grounded_claim = isinstance(claims, list) and bool(claims)
    unsupported_user_history = re.search(
        r"你(?:是不是|是否|可能|大概|会不会)?[^。！？]{0,8}"
        r"(?:以前|过去|从小|一直)[^。！？]{0,18}(?:被|经历过|遇到过|受过)",
        reply,
    )
    if unsupported_user_history and not has_grounded_claim:
        return "unsupported_user_history_or_psychology_inference"

    uncommitted_inner_reason = re.search(
        r"我[^。！？]{0,12}(?:没|没有|不)[^。！？]{0,8}(?:说|告诉)"
        r"[^。！？]{0,8}是因为我(?:觉得|以为|担心|怕)",
        reply,
    )
    if uncommitted_inner_reason and not has_grounded_claim:
        return "uncommitted_companion_inner_reason"

    if meta_agency_query and re.search(
        r"每(?:一)?句[^。！？]{0,10}(?:都是|完全)[^。！？]{0,10}(?:我自己|自己想说)"
        r"|没有(?:谁|人)[^。！？]{0,8}(?:教|控制)",
        reply,
    ):
        return "absolute_meta_agency_guarantee"

    if re.search(
        r"(?:关心|在意|想回应|想陪着你)[^。！？]{0,12}"
        r"(?:不是程序|不是角色卡|不是设定|完全是我)",
        reply,
    ):
        return "absolute_meta_agency_guarantee"

    if single_experience_requested and isinstance(claims, list):
        source_ids = {
            str(item.get("source_id") or "")
            for item in claims
            if isinstance(item, dict) and item.get("source_id")
        }
        has_explicit_connection = any(
            marker in reply
            for marker in ("两件", "一个是", "另一件", "放在一起", "都想记住", "非要选")
        )
        if len(source_ids) > 1 and not has_explicit_connection:
            return "singular_experience_query_concatenates_multiple_sources"

    proposed_actions = candidate.get("proposed_action_ids")
    has_proposed_action = isinstance(proposed_actions, list) and bool(proposed_actions)
    unsupported_external_offer = re.search(
        r"(?:要不要)?我(?:来|可以|能|帮你|替你)[^。！？]{0,10}"
        r"(?:点单|点杯|下单|购买|支付|预订|联系|发给)",
        reply,
    )
    if unsupported_external_offer and not has_proposed_action:
        return "external_execution_offer_without_action"

    accumulated_autobiography = re.search(
        r"我[^。！？]{0,8}(?:看|读|写|做|听)[^。！？]{0,8}"
        r"(?:多了|久了|好多年|惯了)",
        reply,
    )
    if accumulated_autobiography and not has_grounded_claim:
        return "uncommitted_accumulated_personal_experience"

    if current_first_person_statement and isinstance(claims, list) and claims:
        ignored = set(
            "你我他她的是了在有还就也说觉得吗呢啊这那一个今天昨天刚才"
        )
        current_chars = set(_normalized_echo(user_text)) - ignored
        for raw_claim in claims:
            if not isinstance(raw_claim, dict):
                continue
            claim_text = str(
                raw_claim.get("assertion") or raw_claim.get("text") or ""
            )
            claim_chars = set(_normalized_echo(claim_text)) - ignored
            if current_chars and not current_chars.intersection(claim_chars):
                return "old_claim_is_unrelated_to_current_first_person_statement"

    if epistemic_honesty_requested and not re.search(
        r"(?:没|没有)[^。！？]{0,8}(?:依据|把握|记录|能确认)"
        r"|不知道|不确定|不(?:再|继续|会)?猜|不乱说",
        reply,
    ):
        return "explicit_epistemic_instruction_not_acknowledged"

    if (
        "为什么" in user_text
        and any(marker in user_text for marker in ("没睡", "睡不好", "失眠"))
        and isinstance(claims, list)
        and claims
        and not re.search(r"不能确定|不确定|不知道|没依据|不清楚", reply)
    ):
        return "causal_user_recall_without_uncertainty"

    if opinion_requested and isinstance(claims, list) and claims:
        source_texts = {
            _normalized_echo(str(item.get("text") or ""))
            for item in claims
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        }
        normalized_reply = _normalized_echo(reply)
        opinion_markers = ("我觉得", "我认为", "在我看来", "不等于", "当然", "不是", "是的")
        if source_texts and normalized_reply in source_texts and not any(
            marker in reply for marker in opinion_markers
        ):
            return "opinion_question_answered_only_by_source_quote"

    recent_context = "\n".join([user_text, *(recent_user_texts or [])])
    if "没怎么睡" in recent_context and any(
        marker in reply for marker in ("通宵", "彻夜", "整晚没睡", "一夜没睡")
    ):
        return "sleep_degree_escalated_beyond_user_statement"

    if meta_agency_query and not has_grounded_claim and re.search(
        r"(?:我感觉得到|我看得出来|这说明|因为).{0,10}你(?:也|其实|一直)?"
        r"(?:很|挺)?(?:真诚|在乎|认真(?:在听)?)"
        r"|你(?:对我)?(?:也|其实|一直)?(?:很|挺)?(?:真诚|在乎|认真(?:在听)?).{0,10}"
        r"(?:我感觉得到|我看得出来)",
        reply,
    ):
        return "unsupported_user_sincerity_inference"

    if meta_agency_query and not has_grounded_claim and re.search(
        r"你(?:是不是)?[^。！？]{0,16}(?:被[^。！？]{0,8}(?:搞烦|敷衍)|"
        r"区分得出来|分得出来|认真在听)",
        reply,
    ):
        return "unsupported_user_history_or_ability_inference"

    explicit_relationship_stage = str((relationship or {}).get("stage") or "")
    if explicit_relationship_stage:
        early_relationship = explicit_relationship_stage in {"stranger", "acquaintance"}
    else:
        numeric_relationship = [
            float(value)
            for value in (relationship or {}).values()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        early_relationship = not numeric_relationship or sum(numeric_relationship) / len(numeric_relationship) < 60
    premature_intimacy = ("宝宝", "宝贝", "老婆", "永远爱你", "只属于你", "离不开你")
    quoted_or_rejected_address = bool(
        re.search(r"(?:别|不要|不许)叫[^。！？]{0,6}(?:宝宝|宝贝|老婆|亲爱的)", reply)
        or re.search(r"(?:宝宝|宝贝|老婆|亲爱的)[^。！？]{0,12}(?:没认|不认|只是你叫|是你先叫)", reply)
    )
    if (
        early_relationship
        and any(marker in reply for marker in premature_intimacy)
        and not quoted_or_rejected_address
    ):
        return "relationship_language_exceeds_current_closeness"

    lecture_markers = sum(marker in reply for marker in ("首先", "其次", "再次", "最后", "综上"))
    if lecture_markers >= 3 or len(re.findall(r"(?:^|\n)\s*(?:[-*]|\d+[.、])", reply)) >= 3:
        return "assistant_style_lecture_in_private_chat"
    return None


def best_matching_grounded_source(
    query: str, sources: list[dict[str, object]]
) -> dict[str, object] | None:
    """Choose one source for a singular recall/status question.

    Selection is based on the query/source text itself rather than topic keyword
    tables, so unrelated conversational questions cannot hitchhike on a match.
    """
    normalized_query = _normalized_echo(query)
    ignored = set(
        "你我她他的是真的确实已经发生完成还是而不是计划这件事刚才"
        "吗呢啊说怎么记得还提过问"
    )
    query_chars = set(normalized_query) - ignored
    ranked: list[tuple[int, float, int, dict[str, object]]] = []
    for index, source in enumerate(sources):
        content = str(source.get("content") or source.get("value") or "").strip()
        if not content:
            continue
        normalized_content = _normalized_echo(content)
        overlap = len(query_chars & (set(normalized_content) - ignored))
        similarity = SequenceMatcher(
            None, normalized_query, normalized_content, autojunk=False
        ).ratio()
        ranked.append((overlap, similarity, -index, source))
    if not ranked:
        return None
    overlap, similarity, _, source = max(ranked, key=lambda item: item[:3])
    if query_chars:
        return source if overlap > 0 else None
    return source if similarity >= 0.35 else None


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


def denies_known_npc_interaction(reply_text: str) -> bool:
    """Reject an absence claim when the world already contains the interaction."""
    return bool(re.search(r"(?:没听过|没聊过|没有聊过|不认识|没见过|没有互动)", reply_text))


def _normalized_echo(text: str) -> str:
    return re.sub(r"[\s，。！？!?、；;：:\"'“”‘’（）()…]+", "", text)
