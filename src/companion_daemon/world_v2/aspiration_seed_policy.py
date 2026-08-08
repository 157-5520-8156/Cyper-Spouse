"""Historical replay policy for reviewed aspiration-seed draws.

The daily Aspiration Runtime was retired.  These immutable candidate and
weight contracts remain only so old RandomAuthority evidence can be inspected
without restoring a character-authoring route.
"""

from __future__ import annotations

from pydantic import Field

from .life_author_seed import ReviewedAspirationSeed
from .schema_core import FrozenModel


NOTHING_CANDIDATE_REF = "nothing:aspiration"


class AspirationSeedCandidate(FrozenModel):
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    seed: ReviewedAspirationSeed
    witness_event_ref: str | None = None


class AspirationWeightPolicy:
    version = "aspiration-seed-weight.1"

    def compile(
        self,
        *,
        candidates: tuple[AspirationSeedCandidate, ...],
    ) -> dict[str, int]:
        weights: dict[str, int] = {}
        total = 0
        for candidate in candidates:
            weights[candidate.token] = candidate.seed.base_chance_bp
            total += candidate.seed.base_chance_bp
        weights[NOTHING_CANDIDATE_REF] = max(10_000 - total, 0)
        return weights


__all__ = [
    "NOTHING_CANDIDATE_REF",
    "AspirationSeedCandidate",
    "AspirationWeightPolicy",
]
