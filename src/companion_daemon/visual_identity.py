from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class RelationshipVisualTier(BaseModel):
    prompt: str
    negative_prompt: str
    reference_profile: str


class VisualIdentity(BaseModel):
    name: str
    anchor_prompt: str
    selfie_style: str
    negative_prompt: str
    reference_asset: str | None = None
    reference_sets: dict[str, list[str]] = Field(default_factory=dict)
    relationship_tiers: dict[str, RelationshipVisualTier] = Field(default_factory=dict)
    consistency_notes: list[str] = Field(default_factory=list)

    def prompt_block(self, *, relationship_tier: str | None = None) -> str:
        tier = self.relationship_tiers.get(relationship_tier or "")
        parts = [
            "Character identity anchor:",
            self.anchor_prompt.strip(),
            "Selfie style:",
            self.selfie_style.strip(),
            "Avoid:",
            (tier.negative_prompt if tier else self.negative_prompt).strip(),
        ]
        if tier:
            parts.extend(["Relationship-media tier:", tier.prompt.strip()])
        if self.reference_asset:
            parts.append(f"Current reference asset for human review: {self.reference_asset}")
        return "\n".join(parts)

    def reference_assets(self, profile: str = "everyday_selfie") -> tuple[str, ...]:
        assets = self.reference_sets.get(profile) or self.reference_sets.get("everyday_selfie")
        if assets:
            return tuple(assets)
        return (self.reference_asset,) if self.reference_asset else ()

    def relationship_reference_assets(self, tier: str) -> tuple[str, ...]:
        profile = self.relationship_tiers.get(tier)
        return self.reference_assets(profile.reference_profile) if profile else self.reference_assets("relationship_private")


@lru_cache
def load_visual_identity(path: str) -> VisualIdentity:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return VisualIdentity(**data)
