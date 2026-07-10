from __future__ import annotations

import json
import logging
from datetime import datetime

from companion_daemon.db import CompanionStore
from companion_daemon.llm import ChatModel
from companion_daemon.models import MoodState
from companion_daemon.self_core import SelfCore, build_self_core_prompt, parse_self_core
from companion_daemon.time import utc_now

logger = logging.getLogger(__name__)

CONSOLIDATION_INTERVAL = 20
MAX_CONSOLIDATED_MEMORIES = 80

CONSOLIDATION_PROMPT = """你在整理自己的记忆。下面是你最近记住的一些零散事情。
请把相关的记忆合并成更精炼的条目，去掉重复和冗余。

规则：
- 合并相关的条目，比如"用户在成都"+"用户在成都理工上学" → "用户在成都理工大学读书"
- 保留具体细节，不要过度概括
- 不要把两个没有明确关系的事实硬合成一个新事实
- 不要新增原始条目里没有的学校、地点、朋友、群聊、经历或时间
- 如果两条记忆矛盾，保留更近期的
- 输出 JSON 数组，每个元素包含 kind 和 content 两个字段
- 最多输出 20 条合并后的记忆
- 如果记忆已经够精炼，可以原样保留

记忆条目：
{memories}

只输出 JSON 数组，不要其他文字。"""


def should_consolidate(store: CompanionStore, canonical_user_id: str) -> bool:
    since = _last_consolidation_time(store, canonical_user_id)
    if since is None:
        count = store.incoming_message_count(canonical_user_id)
        return count >= CONSOLIDATION_INTERVAL
    count = store.message_count_since(canonical_user_id, direction="in", since_iso=since)
    return count >= CONSOLIDATION_INTERVAL


async def consolidate_memories(
    store: CompanionStore,
    model: ChatModel,
    canonical_user_id: str,
) -> int:
    """Consolidate recent memories using LLM. Returns number of consolidated entries."""
    rows = store.memories(canonical_user_id, limit=40)
    consolidatable = [
        r for r in rows
        if r["kind"] not in {
            "self_core", "life_continuity", "tone_inertia",
            "inner_subtext", "proactive_response", "withheld_proactive_impulse",
            "generated_image", "image_request_blocked", "proactive_image_blocked",
            "own_question_answered", "own_question_skipped",
        }
    ]
    if len(consolidatable) < 5:
        return 0
    memory_lines = []
    for r in consolidatable:
        memory_lines.append(f"[{r['kind']}] {r['content']}")
    prompt = CONSOLIDATION_PROMPT.format(memories="\n".join(memory_lines))
    try:
        raw = await model.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception:
        logger.exception("failed to call LLM for memory consolidation")
        return 0
    consolidated = _parse_consolidation(raw)
    if not consolidated:
        return 0
    for item in consolidated[:20]:
        store.upsert_memory(
            canonical_user_id,
            kind="consolidated",
            content=item["content"],
            source="consolidation",
            confidence=0.82,
        )
    store.upsert_memory(
        canonical_user_id,
        kind="consolidation_log",
        content=f"consolidated {len(consolidated)} entries at {utc_now().isoformat()}",
        source="consolidation",
        confidence=1.0,
    )
    _trim_memories(store, canonical_user_id)
    logger.info("consolidated %d memories for %s", len(consolidated), canonical_user_id)
    return len(consolidated)


async def build_self_core(
    store: CompanionStore,
    model: ChatModel,
    canonical_user_id: str,
    state: MoodState,
) -> SelfCore | None:
    """Generate a self-core from consolidated memories and character state."""
    rows = store.memories(canonical_user_id, limit=40)
    relevant = [
        r for r in rows
        if r["kind"] in {
            "life_fact", "favorite_thing", "hobby", "person",
            "shared_moment", "recent_event", "consolidated",
            "name", "preference", "status", "life_event", "private_life_event",
            "key_relationship_event",
        }
    ]
    if len(relevant) < 3:
        return _store_minimal_self_core(store, canonical_user_id, state)
    memories_for_prompt = [
        {"kind": r["kind"], "content": r["content"]}
        for r in relevant[:30]
    ]
    prompt = build_self_core_prompt(
        memories_for_prompt,
        state.relationship_stage,
        state.mood,
    )
    try:
        raw = await model.complete(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
        )
    except Exception:
        logger.exception("failed to call LLM for self-core generation")
        return None
    core = parse_self_core(raw)
    if not core:
        logger.warning("self-core returned an invalid shape for %s; using grounded fallback", canonical_user_id)
        return _store_minimal_self_core(store, canonical_user_id, state)
    memory_text = "\n".join(item["content"] for item in memories_for_prompt)
    if _self_core_has_unsupported_specifics(core, memory_text):
        logger.warning("rejected self-core with unsupported specifics for %s", canonical_user_id)
        return _store_minimal_self_core(store, canonical_user_id, state)
    _store_self_core(store, canonical_user_id, core, source="self_core_builder")
    logger.info("built self-core for %s", canonical_user_id)
    return core


def load_self_core(store: CompanionStore, canonical_user_id: str) -> SelfCore | None:
    row = store.latest_memory(canonical_user_id, kind="self_core")
    if not row:
        return None
    try:
        return SelfCore.from_storage_text(str(row["content"]))
    except Exception:
        return None


def _store_minimal_self_core(
    store: CompanionStore,
    canonical_user_id: str,
    state: MoodState,
) -> SelfCore:
    """Persist a conservative core when the LLM summary cannot be trusted."""
    safe_user_kinds = {"name", "life_fact", "favorite_thing", "hobby", "preference", "status"}
    user_facts = [
        str(row["content"])
        for row in store.memories(canonical_user_id, limit=40)
        if str(row["kind"]) in safe_user_kinds
    ]
    core = SelfCore(
        identity="稳定身份由角色档案约束；没有新增的可验证个人经历。",
        user_profile="；".join(user_facts[:4]) or "还没有足够的已验证用户信息。",
        relationship=f"当前关系阶段：{state.relationship_stage}。",
        knowledge_boundary="除已验证用户事实和已记录互动外，其余个人经历、地点与关系细节都不确定。",
        active_threads=[],
    )
    _store_self_core(store, canonical_user_id, core, source="self_core_fallback")
    logger.info("stored grounded fallback self-core for %s", canonical_user_id)
    return core


def _store_self_core(
    store: CompanionStore,
    canonical_user_id: str,
    core: SelfCore,
    *,
    source: str,
) -> None:
    store.upsert_memory(
        canonical_user_id,
        kind="self_core",
        content=core.to_storage_text(),
        source=source,
        confidence=1.0,
    )


def _last_consolidation_time(
    store: CompanionStore, canonical_user_id: str
) -> str | None:
    row = store.latest_memory(canonical_user_id, kind="consolidation_log")
    if not row:
        return None
    content = str(row["content"])
    for part in content.split():
        try:
            datetime.fromisoformat(part)
            return part
        except ValueError:
            continue
    return None


def _parse_consolidation(raw: str) -> list[dict[str, str]]:
    raw = _strip_json_fence(raw)
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [
                {"kind": "consolidated", "content": str(item.get("content", ""))}
                for item in data
                if item.get("content")
            ]
    except json.JSONDecodeError:
        logger.warning("memory consolidation returned non-json output")
    return []


def _strip_json_fence(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return stripped


def _self_core_has_unsupported_specifics(core: SelfCore, memory_text: str) -> bool:
    combined = "\n".join(
        [
            core.identity,
            core.user_profile,
            core.relationship,
            "\n".join(core.active_threads),
        ]
    )
    risky_markers = (
        "我记得",
        "你之前",
        "之前听",
        "群里",
        "朋友",
        "同学",
        "室友",
        "听说",
        "刷到",
        "查过",
        "大学",
        "学校",
        "专业",
        "工作",
        "成都理工",
    )
    return any(marker in combined and marker not in memory_text for marker in risky_markers)


def _trim_memories(store: CompanionStore, canonical_user_id: str) -> None:
    with store.connect() as conn:
        conn.execute(
            """
            delete from memories
            where canonical_user_id = ?
              and id not in (
                select id from memories
                where canonical_user_id = ?
                order by updated_at desc
                limit ?
              )
              and kind not in ('self_core', 'consolidation_log')
            """,
            (canonical_user_id, canonical_user_id, MAX_CONSOLIDATED_MEMORIES),
        )
