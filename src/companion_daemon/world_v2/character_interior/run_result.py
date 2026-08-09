"""Small orchestration result owned by the unified CharacterInterior seam.

This type deliberately contains no model port, prompt, or semantic author. It
lets platform-neutral schedulers report one source-bound Interior work unit
without importing the retired independent Appraisal/Affect runtimes merely to
borrow their result classes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from ..schema_core import FrozenModel


CAUSAL_OPPORTUNITY_CONTRACT_VERSION = "causal-opportunity.1"


class CausalOpportunityIdentity(FrozenModel):
    """Canonical identity for one actor-visible source opportunity.

    This is a read-only identity, not a new ledger authority.  Source refs are
    retained as a set for identity purposes; the originating process and its
    immutable source events remain the authority for visibility and meaning.
    """

    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    source_refs: tuple[str, ...] = Field(min_length=1)
    epoch: str = Field(min_length=1)
    contract_version: str = CAUSAL_OPPORTUNITY_CONTRACT_VERSION

    @model_validator(mode="after")
    def source_set_is_canonical(self) -> "CausalOpportunityIdentity":
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("causal opportunity source refs must be unique")
        if tuple(sorted(self.source_refs)) != self.source_refs:
            raise ValueError("causal opportunity source refs must be canonicalized")
        if self.contract_version != CAUSAL_OPPORTUNITY_CONTRACT_VERSION:
            raise ValueError("unsupported causal opportunity contract version")
        return self

    @property
    def opportunity_ref(self) -> str:
        material = self.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"opportunity:causal:{digest}"


class CausalOpportunityHealth(FrozenModel):
    """Read-only health for one source-to-opportunity lane."""

    contract: Literal["causal-opportunity-health.1"] = "causal-opportunity-health.1"
    world_id: str = Field(min_length=1)
    actor_ref: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    open_count: int = Field(ge=0)
    claimed_count: int = Field(ge=0)
    terminal_count: int = Field(ge=0)
    deferred_count: int = Field(ge=0)
    opportunity_count: int = Field(ge=0)
    last_source_ref: str | None = None
    last_opportunity_ref: str | None = None


class CharacterInteriorRunResult(FrozenModel):
    """Outcome of one durable CharacterInterior background settlement unit."""

    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal[
        "no_proposal",
        "no_change",
        "accepted",
        "advisory_validation_rejected",
        "technical_failure",
    ] | None = None
    opportunity_ref: str | None = None
    source_refs: tuple[str, ...] = ()
    epoch: str | None = None
    contract_version: str | None = None


__all__ = [
    "CAUSAL_OPPORTUNITY_CONTRACT_VERSION",
    "CausalOpportunityHealth",
    "CausalOpportunityIdentity",
    "CharacterInteriorRunResult",
]
