from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class CharacterProfile(BaseModel):
    name: str = "凛"
    relationship: str = "恋人"
    base_prompt: str
    identity: dict[str, object] = Field(default_factory=dict)
    appearance: str | None = None
    background: str | None = None
    origin_story: str | None = None
    daily_life: list[str] = Field(default_factory=list)
    canonical_facts: list[str] = Field(default_factory=list)
    personality: str | None = None
    values: list[str] = Field(default_factory=list)
    speech: str | None = None
    relationship_policy: str | None = None
    first_message: str | None = None
    example_messages: list[dict[str, str]] = Field(default_factory=list)
    style_rules: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)

    def system_prompt(self) -> str:
        parts = [
            self.base_prompt.strip(),
            f"你和用户的关系: {self.relationship}",
        ]
        if self.identity:
            identity_lines = [f"{key}: {value}" for key, value in self.identity.items()]
            parts.append("人物身份:\n" + "\n".join(identity_lines))
        if self.appearance:
            parts.append("外貌气质:\n" + self.appearance.strip())
        if self.background:
            parts.append("成长背景:\n" + self.background.strip())
        if self.origin_story:
            parts.append("相识故事:\n" + self.origin_story.strip())
        if self.daily_life:
            parts.append("日常生活:\n" + "\n".join(f"- {item}" for item in self.daily_life))
        if self.canonical_facts:
            parts.append(
                "角色事实账本（用于可验证的自我事实；背景和日常只是气质参考，不能据此补写新经历）：\n"
                + "\n".join(f"- {item}" for item in self.canonical_facts)
            )
        if self.personality:
            parts.append("性格:\n" + self.personality.strip())
        if self.values:
            parts.append("价值观:\n" + "\n".join(f"- {value}" for value in self.values))
        if self.speech:
            parts.append("说话方式:\n" + self.speech.strip())
        if self.relationship_policy:
            parts.append("关系推进规则:\n" + self.relationship_policy.strip())
        if self.style_rules:
            parts.append("说话风格:\n" + "\n".join(f"- {rule}" for rule in self.style_rules))
        if self.boundaries:
            parts.append("边界:\n" + "\n".join(f"- {rule}" for rule in self.boundaries))
        if self.first_message:
            parts.append("第一次见面时的开场参考:\n" + self.first_message.strip())
        if self.example_messages:
            example_lines = []
            # Keep a few style anchors, not a second biography. Concrete
            # examples are especially easy for smaller models to reuse as if
            # they were facts about the current moment.
            for example in self.example_messages[:4]:
                if "user" in example and "assistant" in example:
                    example_lines.append(f"用户: {example['user']}\n沈知栀: {example['assistant']}")
            if example_lines:
                parts.append("示例对话:\n" + "\n\n".join(example_lines))
        return "\n\n".join(parts)


@lru_cache
def load_character(path: str) -> CharacterProfile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CharacterProfile(**data)
