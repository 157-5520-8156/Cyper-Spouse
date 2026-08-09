"""Small orchestration result owned by the unified CharacterInterior seam.

This type deliberately contains no model port, prompt, or semantic author. It
lets platform-neutral schedulers report one source-bound Interior work unit
without importing the retired independent Appraisal/Affect runtimes merely to
borrow their result classes.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ..schema_core import FrozenModel


CAUSAL_OPPORTUNITY_CONTRACT_VERSION = "causal-opportunity.1"
CAUSAL_OPPORTUNITY_POLICY_VERSION = "causal-opportunity-policy.1"


def canonical_source_refs(source_refs: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Return the complete, deterministic source set for one opportunity."""

    canonical = tuple(sorted(set(source_refs)))
    if not canonical or any(not isinstance(ref, str) or not ref for ref in canonical):
        raise ValueError("causal opportunity source refs must be non-empty strings")
    return canonical


class CausalOpportunityPolicy(FrozenModel):
    """Immutable merge/expiry policy carried by one opportunity identity."""

    policy_version: Literal["causal-opportunity-policy.1"] = CAUSAL_OPPORTUNITY_POLICY_VERSION
    merge_window_seconds: int = Field(ge=0)
    expiry_seconds: int = Field(gt=0)

    @property
    def policy_ref(self) -> str:
        """Return the canonical, restart-readable policy coordinates."""

        return (
            f"{self.policy_version}:merge={self.merge_window_seconds}"
            f":expiry={self.expiry_seconds}"
        )

    @classmethod
    def from_ref(cls, policy_ref: str) -> "CausalOpportunityPolicy":
        """Decode the durable policy coordinates without consulting config."""

        parts = policy_ref.split(":")
        if len(parts) != 3 or not parts[1].startswith("merge=") or not parts[2].startswith(
            "expiry="
        ):
            raise ValueError("causal opportunity policy ref is not canonical")
        try:
            merge_window_seconds = int(parts[1].removeprefix("merge="))
            expiry_seconds = int(parts[2].removeprefix("expiry="))
            policy = cls(
                policy_version=parts[0],
                merge_window_seconds=merge_window_seconds,
                expiry_seconds=expiry_seconds,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("causal opportunity policy ref is invalid") from exc
        if policy.policy_ref != policy_ref:
            raise ValueError("causal opportunity policy ref is not canonical")
        return policy


DEFAULT_CAUSAL_OPPORTUNITY_POLICY = CausalOpportunityPolicy(
    merge_window_seconds=300,
    expiry_seconds=7 * 24 * 60 * 60,
)


def causal_opportunity_policy_from_attempt_id(
    attempt_id: str,
) -> CausalOpportunityPolicy | None:
    """Recover a claimed opportunity's policy from durable lease identity."""

    marker = ":policy="
    if marker not in attempt_id:
        return None
    return CausalOpportunityPolicy.from_ref(attempt_id.rsplit(marker, 1)[1])


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
    policy_version: str = CAUSAL_OPPORTUNITY_POLICY_VERSION
    policy_ref: str = DEFAULT_CAUSAL_OPPORTUNITY_POLICY.policy_ref

    @model_validator(mode="after")
    def source_set_is_canonical(self) -> "CausalOpportunityIdentity":
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("causal opportunity source refs must be unique")
        if tuple(sorted(self.source_refs)) != self.source_refs:
            raise ValueError("causal opportunity source refs must be canonicalized")
        if self.contract_version != CAUSAL_OPPORTUNITY_CONTRACT_VERSION:
            raise ValueError("unsupported causal opportunity contract version")
        try:
            policy = CausalOpportunityPolicy.from_ref(self.policy_ref)
        except ValueError as exc:
            raise ValueError("invalid causal opportunity policy") from exc
        if policy.policy_version != self.policy_version:
            raise ValueError("causal opportunity policy version is inconsistent")
        return self

    @classmethod
    def from_source_refs(
        cls,
        *,
        world_id: str,
        actor_ref: str,
        purpose: str,
        source_refs: tuple[str, ...] | list[str],
        epoch: str,
        contract_version: str = CAUSAL_OPPORTUNITY_CONTRACT_VERSION,
        policy: CausalOpportunityPolicy | None = None,
    ) -> "CausalOpportunityIdentity":
        """Construct an identity while making source-set canonicalization explicit.

        ``epoch`` is deliberately required.  A wall-clock tick can affect due
        or expiry, but it cannot silently create a new causal epoch.
        """

        selected_policy = policy or DEFAULT_CAUSAL_OPPORTUNITY_POLICY
        return cls(
            world_id=world_id,
            actor_ref=actor_ref,
            purpose=purpose,
            source_refs=canonical_source_refs(source_refs),
            epoch=epoch,
            contract_version=contract_version,
            policy_version=selected_policy.policy_version,
            policy_ref=selected_policy.policy_ref,
        )

    @property
    def opportunity_policy(self) -> CausalOpportunityPolicy:
        return CausalOpportunityPolicy.from_ref(self.policy_ref)

    def merge(self, other: "CausalOpportunityIdentity") -> "CausalOpportunityIdentity":
        """Merge source evidence without changing the causal identity scope."""

        if not isinstance(other, CausalOpportunityIdentity):
            raise TypeError("causal opportunity merge needs another identity")
        if self.world_id != other.world_id:
            raise ValueError("causal opportunities must belong to the same world")
        if self.actor_ref != other.actor_ref:
            raise ValueError("causal opportunities must use the same actor")
        if self.purpose != other.purpose:
            raise ValueError("causal opportunities must use the same purpose")
        if self.epoch != other.epoch:
            raise ValueError("causal opportunities must use the same epoch")
        if self.contract_version != other.contract_version:
            raise ValueError("causal opportunities must use the same contract version")
        if self.policy_version != other.policy_version or self.policy_ref != other.policy_ref:
            raise ValueError("causal opportunities must use the same policy")
        return self.model_copy(
            update={
                "source_refs": canonical_source_refs(
                    (*self.source_refs, *other.source_refs)
                )
            }
        )

    @property
    def opportunity_ref(self) -> str:
        # Merge/expiry coordinates are routing metadata, not causal identity.
        # Keep the L4 identity contract exactly actor + purpose + canonical
        # source set + epoch + contract version so a policy reload cannot
        # manufacture a second semantic opportunity.
        material = {
            "world_id": self.world_id,
            "actor_ref": self.actor_ref,
            "purpose": self.purpose,
            "source_refs": self.source_refs,
            "epoch": self.epoch,
            "contract_version": self.contract_version,
        }
        digest = hashlib.sha256(
            json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return f"opportunity:causal:{digest}"


def merge_causal_opportunity_identities(
    identities: tuple[CausalOpportunityIdentity, ...] | list[CausalOpportunityIdentity],
) -> CausalOpportunityIdentity:
    """Merge a non-empty identity batch and retain every source reference."""

    if not identities:
        raise ValueError("causal opportunity merge needs at least one identity")
    merged = identities[0]
    for identity in identities[1:]:
        merged = merged.merge(identity)
    return merged


class CausalOpportunityWindow(FrozenModel):
    """The temporal boundary around an opportunity, separate from identity."""

    due_at: datetime | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def expiry_follows_due_time(self) -> "CausalOpportunityWindow":
        if (
            self.due_at is not None
            and self.expires_at is not None
            and self.expires_at < self.due_at
        ):
            raise ValueError("causal opportunity expiry cannot precede due time")
        return self

    def is_expired(self, at: datetime) -> bool:
        if self.expires_at is None:
            return False
        return at >= self.expires_at


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
    no_change_count: int = Field(default=0, ge=0)
    ignored_count: int = Field(default=0, ge=0)
    expired_count: int = Field(default=0, ge=0)
    accepted_count: int = Field(default=0, ge=0)
    technical_failure_count: int = Field(default=0, ge=0)


class CharacterInteriorRunResult(FrozenModel):
    """Outcome of one durable CharacterInterior background settlement unit."""

    trigger_id: str
    status: Literal["idle", "owned_elsewhere", "completed_existing", "processed"]
    work_status: Literal[
        "no_proposal",
        "no_change",
        "ignored",
        "expired",
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
    "CAUSAL_OPPORTUNITY_POLICY_VERSION",
    "CausalOpportunityWindow",
    "CausalOpportunityHealth",
    "CausalOpportunityIdentity",
    "CausalOpportunityPolicy",
    "CharacterInteriorRunResult",
    "DEFAULT_CAUSAL_OPPORTUNITY_POLICY",
    "canonical_source_refs",
    "causal_opportunity_policy_from_attempt_id",
    "merge_causal_opportunity_identities",
]
