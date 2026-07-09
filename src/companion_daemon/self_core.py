from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from companion_daemon.time import utc_now


@dataclass(frozen=True)
class SelfCore:
    """The companion's internal self-concept, grounded in real memories.

    This is NOT the character profile (which is static). This is a dynamic,
    periodically-updated narrative that reflects what has actually happened
    in the conversation. It serves as a grounding anchor to prevent
    hallucination and maintain identity consistency.

    Inspired by Character-LLM's experience-based identity approach:
    identity is built from lived experiences, not just descriptions.
    """

    identity: str
    user_profile: str
    relationship: str
    knowledge_boundary: str
    active_threads: list[str] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)

    def to_prompt_block(self) -> str:
        lines = ["自我认知（基于真实记忆，不是设定）："]
        if self.identity:
            lines.append(f"我是谁：{self.identity}")
        if self.user_profile:
            lines.append(f"我了解的用户：{self.user_profile}")
        if self.relationship:
            lines.append(f"我们的关系：{self.relationship}")
        if self.knowledge_boundary:
            lines.append(f"我不确定的：{self.knowledge_boundary}")
        if self.active_threads:
            lines.append("还在想的：" + "；".join(self.active_threads[:3]))
        return "\n".join(lines)

    def to_storage_text(self) -> str:
        threads = "||".join(self.active_threads) if self.active_threads else ""
        return (
            f"{self.identity}\n"
            f"---\n{self.user_profile}\n"
            f"---\n{self.relationship}\n"
            f"---\n{self.knowledge_boundary}\n"
            f"---\n{threads}"
        )

    @classmethod
    def from_storage_text(cls, text: str) -> SelfCore:
        parts = text.split("\n---\n")
        identity = parts[0].strip() if len(parts) > 0 else ""
        user_profile = parts[1].strip() if len(parts) > 1 else ""
        relationship = parts[2].strip() if len(parts) > 2 else ""
        knowledge_boundary = parts[3].strip() if len(parts) > 3 else ""
        threads_raw = parts[4].strip() if len(parts) > 4 else ""
        active_threads = [t.strip() for t in threads_raw.split("||") if t.strip()] if threads_raw else []
        return cls(
            identity=identity,
            user_profile=user_profile,
            relationship=relationship,
            knowledge_boundary=knowledge_boundary,
            active_threads=active_threads,
        )

    @classmethod
    def initial(cls, character_name: str = "沈知栀") -> SelfCore:
        return cls(
            identity=f"我叫{character_name}，刚通过读书群认识了用户。",
            user_profile="还不了解用户，只知道他可能在成都。",
            relationship="刚认识，还没聊过几次。",
            knowledge_boundary="不知道用户的真名、学校、职业、家庭情况。",
            active_threads=[],
        )


SELF_CORE_BUILD_PROMPT = """你在整理自己的记忆，形成一个简短的自我认知。你是沈知栀，20岁女大学生。

规则：
- 只写你确实知道的事，来自下面列出的真实记忆
- 明确写出你不确定或不知道的事
- 用第一人称
- 每个部分不超过两句话
- 不要编造任何记忆里没有的细节

请按以下格式输出（每行一个部分，用 --- 分隔）：

[你对自己的认知：名字、身份、和用户怎么认识的]
---
[你对用户的了解：来自记忆的真实信息]
---
[你们的关系现状：聊过什么、关系阶段]
---
[你不确定的事：记忆里没有但容易瞎编的]
---
[你还在想的事：最近对话里悬而未决的线索，用 || 分隔多条]

记忆条目（按时间倒序）：
{memories}

当前关系阶段：{relationship_stage}
当前心情：{mood}

只输出上述格式的内容，不要加其他说明。"""


def build_self_core_prompt(
    memories: list[dict[str, str]],
    relationship_stage: str,
    mood: str,
) -> str:
    memory_lines = []
    for m in memories[:30]:
        kind = m.get("kind", "")
        content = m.get("content", "")
        memory_lines.append(f"[{kind}] {content}")
    memories_text = "\n".join(memory_lines) if memory_lines else "暂无记忆。"
    return SELF_CORE_BUILD_PROMPT.format(
        memories=memories_text,
        relationship_stage=relationship_stage,
        mood=mood,
    )


def parse_self_core(raw: str) -> SelfCore | None:
    parts = raw.strip().split("---")
    if len(parts) < 4:
        return None
    identity = parts[0].strip()
    user_profile = parts[1].strip()
    relationship = parts[2].strip()
    knowledge_boundary = parts[3].strip()
    threads_raw = parts[4].strip() if len(parts) > 4 else ""
    active_threads = [t.strip() for t in threads_raw.split("||") if t.strip()]
    if not identity:
        return None
    return SelfCore(
        identity=identity,
        user_profile=user_profile,
        relationship=relationship,
        knowledge_boundary=knowledge_boundary,
        active_threads=active_threads,
    )
