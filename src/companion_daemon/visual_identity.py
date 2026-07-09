from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class VisualIdentity(BaseModel):
    name: str
    anchor_prompt: str
    selfie_style: str
    negative_prompt: str
    reference_asset: str | None = None
    consistency_notes: list[str] = Field(default_factory=list)

    def prompt_block(self) -> str:
        parts = [
            "Character identity anchor:",
            self.anchor_prompt.strip(),
            "Selfie style:",
            self.selfie_style.strip(),
            "Avoid:",
            self.negative_prompt.strip(),
        ]
        if self.reference_asset:
            parts.append(f"Current reference asset for human review: {self.reference_asset}")
        return "\n".join(parts)


@lru_cache
def load_visual_identity(path: str) -> VisualIdentity:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return VisualIdentity(**data)
