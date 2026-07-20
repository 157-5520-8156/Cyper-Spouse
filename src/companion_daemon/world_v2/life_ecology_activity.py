"""Pure, source-bound activity openings for the first Life Ecology vertical.

The catalog is deliberately a read-only deep module.  A caller supplies one
already-pinned ledger projection and an immutable ``ClockAdvanced`` reference;
the module validates that authority, computes every legal successor for the
companion-owned abstract plans, and returns only opaque, deterministic tokens.
It cannot claim a trigger, call a model, or append an event.

Location and registered-NPC plans are eligible only when their accepted plan
binds the exact durable availability snapshot emitted by Life Author.  A bare
reference is still not proof and remains an explicit capability block.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .clock_authority import clock_policy_is_installed
from .activity_timing import (
    activity_completion_allowed,
    activity_transition_dwell_elapsed,
    activity_window_completion_allowed,
)
from .schema_core import FrozenModel
from .schemas import LedgerProjection, PlanStateProjection


ACTIVITY_OPENING_CATALOG_VERSION = "activity-opening.6"
"""Current frozen semantics of the abstract-plan opening catalog."""

# ``activity-opening.1`` and ``.2`` were already persisted by long-lived
# processes before the one-minute early-completion guard was installed.  Their
# event envelopes are immutable, so replay must retain the exact old operation
# matrix for those versions while new wakes use the bumped catalog version.
_LEGACY_ACTIVITY_OPENING_CATALOG_VERSIONS = frozenset(
    {"activity-opening.1", "activity-opening.2"}
)

# ``activity-opening.3`` guarded completion with only a one-minute floor, so
# a sixty-minute activity could truthfully be "completed" after one minute.
# Its committed proposals replay against that exact rule; every newer catalog
# version offers ordinary completion by the accepted schedule window instead.
_ELAPSED_ONLY_COMPLETION_CATALOG_VERSIONS = frozenset({"activity-opening.3"})

# ``activity-opening.4`` exposed not-yet-open future plans to every ordinary
# scheduler wake with "abandon" as their only legal operation, and offered
# pause/resume without any dwell time.  In production this abandoned a
# three-days-away commitment after an hour of repeated prompting and produced
# a pause/resume oscillation.  ``.5`` keeps future commitments out of the
# ordinary catalog entirely and requires a bounded dwell before a transition
# can be reversed; ``.4`` proposals replay against their exact old matrix.
_NO_FUTURE_SHIELD_CATALOG_VERSIONS = frozenset({"activity-opening.4"})

# ``activity-opening.5`` dwell-gated only the ordinary *pause*, leaving
# "abandon" as the sole ordinary operation offered in an activity's first
# minutes.  In production every started activity was therefore abandoned on
# the very next scheduler wake — the model was asked "abandon or nothing?"
# forty seconds into every plan and reliably picked the only forward-looking
# token.  ``.6`` dwell-gates the ordinary abandon exactly like the ordinary
# pause; ``.5`` proposals replay against their exact old matrix.
_NO_ABANDON_DWELL_CATALOG_VERSIONS = frozenset({"activity-opening.5"})

ActivityOpeningOperation = Literal["start", "pause", "resume", "complete", "abandon"]
ActivityOpeningKind = Literal[
    "ordinary", "user_influence", "interruption", "change_plan", "repair",
    "shared_private",
]
ActivityOpeningCauseKind = Literal[
    "message_observation", "clock_activity_conflict", "plan_authority",
]
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
    opening_kind: ActivityOpeningKind
    cause_kind: ActivityOpeningCauseKind | None = None
    cause_observation_id: str | None = None

    def hash_material(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "operation": self.operation,
            "opening_kind": self.opening_kind,
            "cause_kind": self.cause_kind,
            "cause_observation_id": self.cause_observation_id,
        }


class ActivityOpening(FrozenModel):
    """One model-selectable operation with no plan identity or evidence refs.

    ``opening_token`` is the only identity a deliberator may return.  The
    catalog hash binds the hidden plan identity/revision and is part of the
    token material, so a token cannot be transplanted between wakes or pins.
    """

    opening_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation: ActivityOpeningOperation
    opening_kind: ActivityOpeningKind
    cause_kind: ActivityOpeningCauseKind | None = None
    safe_summary: str = Field(min_length=1, max_length=160)


class ResolvedActivityOpening(FrozenModel):
    """Authority-only resolution of an opaque catalog token."""

    opening_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    plan_revision: int = Field(ge=1)
    operation: ActivityOpeningOperation
    opening_kind: ActivityOpeningKind
    cause_kind: ActivityOpeningCauseKind | None = None
    cause_observation_id: str | None = Field(default=None, min_length=1)
    catalog_version: str = Field(min_length=1)
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


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
            missing = self._missing_capabilities(plan, projection=projection)
            if missing:
                blocked_plan_count += 1
                blocked_capabilities.update(missing)
                continue
            for operation in self._operations_for(
                plan,
                logical_time=projection.logical_time,
                catalog_version=self._catalog_version,
            ):
                opening_kind, cause_kind, cause_observation_id = self._opening_authority_shape(
                    plan=plan, operation=operation, projection=projection
                )
                if self._dwell_suppressed(
                    plan=plan,
                    operation=operation,
                    opening_kind=opening_kind,
                    logical_time=projection.logical_time,
                ):
                    continue
                eligible_bindings.append(
                    _OpeningBinding(
                        plan_id=plan.plan_id,
                        plan_revision=plan.entity_revision,
                        operation=operation,
                        opening_kind=opening_kind,
                        cause_kind=cause_kind,
                        cause_observation_id=cause_observation_id,
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
                        "opening_kind": binding.opening_kind,
                        "cause_kind": binding.cause_kind,
                        "cause_observation_id": binding.cause_observation_id,
                        "catalog_version": self._catalog_version,
                        "catalog_hash": catalog_hash,
                    }
                ),
                operation=binding.operation,
                opening_kind=binding.opening_kind,
                cause_kind=binding.cause_kind,
                safe_summary=_safe_summary(binding.operation, binding.opening_kind),
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

    def resolve_opening(
        self,
        *,
        projection: LedgerProjection,
        wake_event_ref: str,
        opening_token: str,
    ) -> ResolvedActivityOpening | None:
        """Resolve an offered token without accepting a caller plan identity."""

        catalog = self.openings_for(projection=projection, wake_event_ref=wake_event_ref)
        if catalog.status != "openings_available" or opening_token not in {
            item.opening_token for item in catalog.openings
        }:
            return None
        for plan in sorted(projection.plans, key=lambda item: item.plan_id):
            if not self._is_companion_live_plan(plan) or self._missing_capabilities(
                plan, projection=projection
            ):
                continue
            for operation in self._operations_for(
                plan,
                logical_time=projection.logical_time,
                catalog_version=self._catalog_version,
            ):
                opening_kind, cause_kind, cause_observation_id = self._opening_authority_shape(
                    plan=plan, operation=operation, projection=projection
                )
                if self._dwell_suppressed(
                    plan=plan,
                    operation=operation,
                    opening_kind=opening_kind,
                    logical_time=projection.logical_time,
                ):
                    continue
                token = _digest(
                    {
                        "world_id": projection.world_id,
                        "wake_event_ref": wake_event_ref,
                        "plan_id": plan.plan_id,
                        "plan_revision": plan.entity_revision,
                        "operation": operation,
                        "opening_kind": opening_kind,
                        "cause_kind": cause_kind,
                        "cause_observation_id": cause_observation_id,
                        "catalog_version": self._catalog_version,
                        "catalog_hash": catalog.catalog_hash,
                    }
                )
                if token == opening_token:
                    return ResolvedActivityOpening(
                        opening_token=token,
                        plan_id=plan.plan_id,
                        plan_revision=plan.entity_revision,
                        operation=operation,
                        opening_kind=opening_kind,
                        cause_kind=cause_kind,
                        cause_observation_id=cause_observation_id,
                        catalog_version=catalog.catalog_version,
                        catalog_hash=catalog.catalog_hash,
                    )
        raise AssertionError("activity opening catalog advertised an unresolvable token")

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
        self, plan: PlanStateProjection, *, projection: LedgerProjection
    ) -> tuple[MissingActivityCapability, ...]:
        missing: set[MissingActivityCapability] = set()
        has_snapshot = self._has_exact_availability_snapshot(plan, projection=projection)
        if plan.location_ref is not None and not has_snapshot:
            missing.add("location_authority_binding")
        active_npcs = {f"npc:{item.npc_id}" for item in projection.npcs if item.status == "active"}
        npc_refs = tuple(ref for ref in plan.participant_refs if ref.startswith("npc:"))
        if npc_refs and (not has_snapshot or any(ref not in active_npcs for ref in npc_refs)):
            missing.add("npc_availability")
        if any(
            ref != self._owner_actor_ref
            and not ref.startswith("npc:")
            and not self._has_exact_observed_participant_scope(
                plan=plan, participant_ref=ref, projection=projection
            )
            for ref in plan.participant_refs
        ):
            missing.add("participant_availability")
        return tuple(capability for capability in _CAPABILITY_ORDER if capability in missing)

    @staticmethod
    def _observed_message_for_plan(
        plan: PlanStateProjection, *, projection: LedgerProjection
    ):
        observations = {
            item.observation_id: item for item in projection.message_observations
        }
        for evidence in plan.evidence_refs:
            observation = observations.get(evidence.ref_id)
            if (
                evidence.evidence_type == "observed_message"
                and observation is not None
                and evidence.source_world_revision == observation.world_revision
                and evidence.immutable_hash == observation.event_payload_hash
            ):
                return observation
        return None

    def _has_exact_observed_participant_scope(
        self, *, plan: PlanStateProjection, participant_ref: str,
        projection: LedgerProjection,
    ) -> bool:
        observation = self._observed_message_for_plan(plan, projection=projection)
        return bool(
            observation is not None
            and observation.actor is not None
            and observation.actor == participant_ref
        )

    def _opening_authority_shape(
        self, *, plan: PlanStateProjection, operation: ActivityOpeningOperation,
        projection: LedgerProjection,
    ) -> tuple[ActivityOpeningKind, ActivityOpeningCauseKind | None, str | None]:
        source_observation = self._observed_message_for_plan(
            plan, projection=projection
        )
        if operation == "pause":
            origin_revision = getattr(
                getattr(plan, "authority_origin", None),
                "accepted_world_revision", 0,
            )
            newer = tuple(
                item for item in projection.message_observations
                if item.world_revision > origin_revision
                and item.actor not in {None, self._owner_actor_ref}
            )
            if newer:
                selected = max(newer, key=lambda item: item.world_revision)
                return "interruption", "message_observation", selected.observation_id
            if (
                plan.scheduled_window is not None
                and projection.logical_time >= plan.scheduled_window.closes_at
            ):
                return "interruption", "clock_activity_conflict", None
        if operation == "resume":
            return "repair", "plan_authority", None
        if plan.supersedes_plan_id is not None:
            return "change_plan", (
                "message_observation" if source_observation is not None
                else "plan_authority"
            ), (
                source_observation.observation_id
                if source_observation is not None else None
            )
        if source_observation is not None:
            scoped_participant = (
                source_observation.actor is not None
                and source_observation.actor in plan.participant_refs
            )
            if plan.privacy_class in {"private", "withhold"} and scoped_participant:
                return (
                    "shared_private", "message_observation",
                    source_observation.observation_id,
                )
            return (
                "user_influence", "message_observation",
                source_observation.observation_id,
            )
        return "ordinary", None, None

    def _dwell_suppressed(
        self,
        *,
        plan: PlanStateProjection,
        operation: ActivityOpeningOperation,
        opening_kind: ActivityOpeningKind,
        logical_time: datetime,
    ) -> bool:
        """Withhold thrash-prone reversals until real time passed in a state.

        Reversing a fresh transition on every thirty-second wake is a
        scheduler artifact, not a lived choice.  Ordinary pause *and*
        ordinary abandon are dwell-gated: a just-started activity simply is
        not up for revision for a few minutes, so an idle wake offers no
        operation at all instead of "abandon or nothing".  Cause-bound
        transitions (a user message, a closed window, repair-resume) stay
        immediate — pausing because the user interrupted and picking the book
        back up a minute later is exactly the human sequence.  Runs only for
        the current catalog version so committed ``activity-opening.4``/
        ``.5`` proposals replay exactly.
        """

        if self._catalog_version in _LEGACY_ACTIVITY_OPENING_CATALOG_VERSIONS or (
            self._catalog_version in _ELAPSED_ONLY_COMPLETION_CATALOG_VERSIONS
            or self._catalog_version in _NO_FUTURE_SHIELD_CATALOG_VERSIONS
        ):
            return False
        if operation == "pause" and opening_kind == "ordinary":
            return not activity_transition_dwell_elapsed(plan, logical_time=logical_time)
        if (
            operation == "abandon"
            and opening_kind == "ordinary"
            and self._catalog_version not in _NO_ABANDON_DWELL_CATALOG_VERSIONS
            and plan.status in {"active", "paused"}
        ):
            return not activity_transition_dwell_elapsed(plan, logical_time=logical_time)
        return False

    @staticmethod
    def _has_exact_availability_snapshot(
        plan: PlanStateProjection, *, projection: LedgerProjection
    ) -> bool:
        committed = {item.event_id: item for item in projection.committed_world_event_refs}
        for evidence in plan.evidence_refs:
            item = committed.get(evidence.ref_id)
            if (
                evidence.evidence_type == "committed_world_event"
                and item is not None
                and item.event_type == "LifeAvailabilitySnapshotRecorded"
                and evidence.source_world_revision == item.world_revision
                and evidence.immutable_hash == item.payload_hash
            ):
                return True
        return False

    @staticmethod
    def _operations_for(
        plan: PlanStateProjection,
        *,
        logical_time: datetime,
        catalog_version: str = ACTIVITY_OPENING_CATALOG_VERSION,
    ) -> tuple[ActivityOpeningOperation, ...]:
        operations = _OPERATIONS_BY_STATUS[plan.status]
        legacy = catalog_version in _LEGACY_ACTIVITY_OPENING_CATALOG_VERSIONS
        pre_shield = legacy or (
            catalog_version in _ELAPSED_ONLY_COMPLETION_CATALOG_VERSIONS
            or catalog_version in _NO_FUTURE_SHIELD_CATALOG_VERSIONS
        )
        if not legacy and plan.status == "active":
            completion_rule = (
                activity_completion_allowed
                if catalog_version in _ELAPSED_ONLY_COMPLETION_CATALOG_VERSIONS
                else activity_window_completion_allowed
            )
            if not completion_rule(plan, logical_time=logical_time):
                operations = tuple(
                    operation for operation in operations if operation != "complete"
                )
        if plan.status != "planned" or plan.scheduled_window is None:
            return operations
        if plan.scheduled_window.opens_at <= logical_time < plan.scheduled_window.closes_at:
            return operations
        if not pre_shield and logical_time < plan.scheduled_window.opens_at:
            # A commitment whose time has not come is not up for casual
            # revision on an ordinary wake: offering only "abandon" every
            # thirty seconds eventually talks the model into it.  Disrupting
            # a future plan needs a cause-bound interruption/change opening.
            return ()
        # A missed plan remains abandonnable, but may not be falsely started
        # outside its accepted time relation.
        return tuple(operation for operation in operations if operation != "start")


def _safe_summary(
    operation: ActivityOpeningOperation, opening_kind: ActivityOpeningKind
) -> str:
    operation_summary = {
        "start": "begin an abstract planned activity",
        "pause": "pause the current abstract activity",
        "resume": "resume an abstract paused activity",
        "complete": "finish the current abstract activity",
        "abandon": "let go of an abstract activity",
    }[operation]
    qualifier = {
        "ordinary": "",
        "user_influence": " with verified user-influence authority",
        "interruption": " after a verified outside interruption or clock conflict",
        "change_plan": " as an accepted replacement plan",
        "repair": " as repair of a previously paused activity",
        "shared_private": " in verified private shared participant scope",
    }[opening_kind]
    return operation_summary + qualifier


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ACTIVITY_OPENING_CATALOG_VERSION",
    "ActivityOpening",
    "ActivityOpeningKind",
    "ActivityOpeningCauseKind",
    "ActivityOpeningCatalog",
    "ActivityOpeningCatalogResult",
    "ActivityOpeningCatalogStatus",
    "ActivityOpeningOperation",
    "MissingActivityCapability",
    "ResolvedActivityOpening",
]
