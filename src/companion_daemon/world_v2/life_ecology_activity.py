"""Pure, source-bound activity openings for the first Life Ecology vertical.

The catalog is deliberately a read-only deep module.  A caller supplies one
already-pinned ledger projection and an immutable ``ClockAdvanced`` reference;
the module validates that authority, computes every legal successor for the
companion-owned abstract plans, and returns only opaque, deterministic tokens.
It cannot claim a trigger, call a model, or append an event.

Location, resource, and social availability are not installed authority
bindings yet.  Rather than treating a reference as proof, this first vertical
offers openings only for an abstract plan (no location and no other
participant).  The result keeps a capability block distinct from an ordinary
quiet/no-opening world so a scheduler cannot incorrectly report it as idle.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .clock_authority import clock_policy_is_installed
from .schema_core import FrozenModel
from .schemas import LedgerProjection, PlanStateProjection


ACTIVITY_OPENING_CATALOG_VERSION = "activity-opening.1"
"""Frozen semantics of the first, abstract-plan-only opening catalog."""

ActivityOpeningOperation = Literal["start", "pause", "resume", "complete", "abandon"]
ActivityOpeningCatalogStatus = Literal[
    "openings_available", "no_openings", "blocked_by_missing_capability", "rejected_wake"
]
MissingActivityCapability = Literal[
    "location_authority_binding", "npc_availability", "participant_availability"
]

_OPERATIONS_BY_STATUS: dict[str, tuple[ActivityOpeningOperation, ...]] = {
    "planned": ("start", "abandon"),
    "active": ("pause", "complete", "abandon"),
    "paused": ("resume", "abandon"),
}
_CAPABILITY_ORDER: tuple[MissingActivityCapability, ...] = (
    "location_authority_binding",
    "npc_availability",
    "participant_availability",
)


@dataclass(frozen=True, slots=True)
class _OpeningBinding:
    """Trusted catalog material that must never enter model output."""

    plan_id: str
    plan_revision: int
    operation: ActivityOpeningOperation

    def hash_material(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "operation": self.operation,
        }


class ActivityOpening(FrozenModel):
    """One model-selectable operation with no plan identity or evidence refs.

    ``opening_token`` is the only identity a deliberator may return.  The
    catalog hash binds the hidden plan identity/revision and is part of the
    token material, so a token cannot be transplanted between wakes or pins.
    """

    opening_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: ActivityOpeningOperation
    safe_summary: str = Field(min_length=1, max_length=160)


class ActivityOpeningCatalogResult(FrozenModel):
    """Complete immutable outcome of one catalog scan.

    ``blocked_plan_count`` and ``blocked_capabilities`` are aggregate only:
    they explain missing authority without leaking a plan, NPC, or location
    identifier into a model-visible result.
    """

    status: ActivityOpeningCatalogStatus
    catalog_version: str = Field(min_length=1)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    wake_event_ref: str = Field(min_length=1)
    openings: tuple[ActivityOpening, ...] = ()
    blocked_plan_count: int = Field(default=0, ge=0)
    blocked_capabilities: tuple[MissingActivityCapability, ...] = ()
    reason_code: str | None = None

    @model_validator(mode="after")
    def outcome_is_unambiguous(self) -> "ActivityOpeningCatalogResult":
        if self.status == "openings_available" and not self.openings:
            raise ValueError("opening result must contain at least one opening")
        if self.status != "openings_available" and self.openings:
            raise ValueError("non-opening result cannot contain openings")
        if self.status == "blocked_by_missing_capability" and (
            not self.blocked_plan_count or not self.blocked_capabilities
        ):
            raise ValueError("blocked result requires a blocked plan and capability")
        if self.status != "blocked_by_missing_capability" and self.blocked_plan_count and not self.blocked_capabilities:
            raise ValueError("blocked plans require explicit capabilities")
        if self.blocked_capabilities != tuple(
            capability for capability in _CAPABILITY_ORDER if capability in self.blocked_capabilities
        ):
            raise ValueError("blocked capabilities must be unique and canonical")
        if self.status == "rejected_wake" and not self.reason_code:
            raise ValueError("rejected wake requires a stable reason code")
        return self


class ActivityOpeningCatalog:
    """Enumerate safe successors through one pure public interface.

    The interface intentionally accepts neither a plan ID nor an operation;
    all candidate selection stays behind this seam.  It expects a projection
    that was already pinned by a future scheduler/trigger authority.  This
    module verifies the wake against that projection but does not read a
    ledger, so it has no unpinned read or write capability.
    """

    def __init__(
        self,
        *,
        owner_actor_ref: str,
        catalog_version: str = ACTIVITY_OPENING_CATALOG_VERSION,
    ) -> None:
        if not owner_actor_ref:
            raise ValueError("activity opening catalog needs a companion owner")
        if not catalog_version:
            raise ValueError("activity opening catalog version must not be empty")
        self._owner_actor_ref = owner_actor_ref
        self._catalog_version = catalog_version

    def openings_for(
        self, *, projection: LedgerProjection, wake_event_ref: str
    ) -> ActivityOpeningCatalogResult:
        """Return the frozen, deterministic catalog for one exact clock wake."""

        if not self._has_exact_clock_wake(projection=projection, wake_event_ref=wake_event_ref):
            return self._result(
                status="rejected_wake",
                projection=projection,
                wake_event_ref=wake_event_ref,
                catalog_material={"wake_validation": "rejected"},
                reason_code="activity_opening.wake_not_exact_clock_authority",
            )

        eligible_bindings: list[_OpeningBinding] = []
        blocked_capabilities: set[MissingActivityCapability] = set()
        blocked_plan_count = 0
        for plan in sorted(projection.plans, key=lambda item: item.plan_id):
            if not self._is_companion_live_plan(plan):
                continue
            missing = self._missing_capabilities(plan)
            if missing:
                blocked_plan_count += 1
                blocked_capabilities.update(missing)
                continue
            for operation in self._operations_for(plan, logical_time=projection.logical_time):
                eligible_bindings.append(
                    _OpeningBinding(
                        plan_id=plan.plan_id,
                        plan_revision=plan.entity_revision,
                        operation=operation,
                    )
                )

        catalog_material = {
            "catalog_version": self._catalog_version,
            "world_id": projection.world_id,
            "wake_event_ref": wake_event_ref,
            "cursor": {
                "world_revision": projection.world_revision,
                "deliberation_revision": projection.deliberation_revision,
                "ledger_sequence": projection.ledger_sequence,
            },
            "projection_semantic_hash": projection.semantic_hash,
            "logical_time": projection.logical_time.isoformat() if projection.logical_time else None,
            "eligible_bindings": [binding.hash_material() for binding in eligible_bindings],
            "blocked_plan_count": blocked_plan_count,
            "blocked_capabilities": [
                capability for capability in _CAPABILITY_ORDER if capability in blocked_capabilities
            ],
        }
        catalog_hash = _digest(catalog_material)
        openings = tuple(
            ActivityOpening(
                opening_token=_digest(
                    {
                        "world_id": projection.world_id,
                        "wake_event_ref": wake_event_ref,
                        "plan_id": binding.plan_id,
                        "plan_revision": binding.plan_revision,
                        "operation": binding.operation,
                        "catalog_version": self._catalog_version,
                        "catalog_hash": catalog_hash,
                    }
                ),
                operation=binding.operation,
                safe_summary=_safe_summary(binding.operation),
            )
            for binding in eligible_bindings
        )
        canonical_blocks = tuple(
            capability for capability in _CAPABILITY_ORDER if capability in blocked_capabilities
        )
        status: ActivityOpeningCatalogStatus
        if openings:
            status = "openings_available"
        elif blocked_plan_count:
            status = "blocked_by_missing_capability"
        else:
            status = "no_openings"
        return ActivityOpeningCatalogResult(
            status=status,
            catalog_version=self._catalog_version,
            catalog_hash=catalog_hash,
            wake_event_ref=wake_event_ref,
            openings=openings,
            blocked_plan_count=blocked_plan_count,
            blocked_capabilities=canonical_blocks,
        )

    def _result(
        self,
        *,
        status: ActivityOpeningCatalogStatus,
        projection: LedgerProjection,
        wake_event_ref: str,
        catalog_material: dict[str, object],
        reason_code: str | None = None,
    ) -> ActivityOpeningCatalogResult:
        return ActivityOpeningCatalogResult(
            status=status,
            catalog_version=self._catalog_version,
            catalog_hash=_digest(
                {
                    "catalog_version": self._catalog_version,
                    "world_id": projection.world_id,
                    "wake_event_ref": wake_event_ref,
                    "cursor": {
                        "world_revision": projection.world_revision,
                        "deliberation_revision": projection.deliberation_revision,
                        "ledger_sequence": projection.ledger_sequence,
                    },
                    "projection_semantic_hash": projection.semantic_hash,
                    **catalog_material,
                }
            ),
            wake_event_ref=wake_event_ref,
            reason_code=reason_code,
        )

    def _has_exact_clock_wake(self, *, projection: LedgerProjection, wake_event_ref: str) -> bool:
        if not wake_event_ref or projection.logical_time is None:
            return False
        committed = next(
            (item for item in projection.committed_world_event_refs if item.event_id == wake_event_ref),
            None,
        )
        transition = next(
            (item for item in projection.clock_transition_history if item.clock_event_ref == wake_event_ref),
            None,
        )
        return bool(
            committed is not None
            and transition is not None
            and committed.event_type == "ClockAdvanced"
            and committed.world_revision == transition.computed_world_revision
            and committed.payload_hash == transition.payload_hash
            and committed.logical_time == transition.logical_time_to
            and transition.logical_time_to <= projection.logical_time
            and clock_policy_is_installed(
                version=transition.installed_policy_version,
                digest=transition.installed_policy_digest,
            )
        )

    def _is_companion_live_plan(self, plan: PlanStateProjection) -> bool:
        return plan.owner_actor_ref == self._owner_actor_ref and plan.status in _OPERATIONS_BY_STATUS

    def _missing_capabilities(
        self, plan: PlanStateProjection
    ) -> tuple[MissingActivityCapability, ...]:
        missing: set[MissingActivityCapability] = set()
        if plan.location_ref is not None:
            missing.add("location_authority_binding")
        if any(ref.startswith("npc:") for ref in plan.participant_refs):
            missing.add("npc_availability")
        if any(
            ref != self._owner_actor_ref and not ref.startswith("npc:")
            for ref in plan.participant_refs
        ):
            missing.add("participant_availability")
        return tuple(capability for capability in _CAPABILITY_ORDER if capability in missing)

    @staticmethod
    def _operations_for(
        plan: PlanStateProjection, *, logical_time: datetime
    ) -> tuple[ActivityOpeningOperation, ...]:
        operations = _OPERATIONS_BY_STATUS[plan.status]
        if plan.status != "planned" or plan.scheduled_window is None:
            return operations
        if plan.scheduled_window.opens_at <= logical_time < plan.scheduled_window.closes_at:
            return operations
        # A missed/not-yet-open plan remains abandonnable, but may not be
        # falsely started outside its accepted time relation.
        return tuple(operation for operation in operations if operation != "start")


def _safe_summary(operation: ActivityOpeningOperation) -> str:
    return {
        "start": "begin an abstract planned activity",
        "pause": "pause the current abstract activity",
        "resume": "resume an abstract paused activity",
        "complete": "finish the current abstract activity",
        "abandon": "let go of an abstract activity",
    }[operation]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ACTIVITY_OPENING_CATALOG_VERSION",
    "ActivityOpening",
    "ActivityOpeningCatalog",
    "ActivityOpeningCatalogResult",
    "ActivityOpeningCatalogStatus",
    "ActivityOpeningOperation",
    "MissingActivityCapability",
]
