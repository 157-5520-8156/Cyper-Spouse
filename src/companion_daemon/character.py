from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class CharacterProfile(BaseModel):
    name: str = "凛"
    relationship: str | None = None
    base_prompt: str
    identity: dict[str, object] = Field(default_factory=dict)
    appearance: str | None = None
    background: str | None = None
    daily_life: list[str] = Field(default_factory=list)
    canonical_facts: list[str] = Field(default_factory=list)
    shared_history_facts: list[str] = Field(default_factory=list)
    counterpart_history_facts: list[str] = Field(default_factory=list)
    personality: str | None = None
    values: list[str] = Field(default_factory=list)
    speech: str | None = None
    relationship_policy: str | None = None
    first_message: str | None = None
    style_rules: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)


@lru_cache
def load_character(path: str) -> CharacterProfile:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return CharacterProfile(**data)
