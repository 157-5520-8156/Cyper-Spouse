"""Runtime-owned Action dispatch with durable pre-dispatch recovery.

The pump owns only ledger transitions.  Executors own network/tool effects and
return an ``ExternalObservation`` for the normal settlement path; they never
receive a ledger writer or ``WorldRuntime`` reference.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
import hashlib
import inspect
import json
from typing import Literal, Protocol

from .errors import ConcurrencyConflict, IdempotencyConflict
from .event_identity import domain_idempotency_key
from .expression_reconsideration import expression_beat_is_gated
from .ledger import LedgerPort
from .schema_core import FrozenModel
from .schemas import (
    Action,
    ClaimLease,
    DispatchPending,
    ExternalObservation,
    ProviderReceipt,
    WorldEvent,
)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class ActionExecutor(Protocol):
    """Pure side-effect port; receipt settlement remains owned by Runtime."""

    async def dispatch(self, action: Action) -> ProviderReceipt | DispatchPending | None: ...

    async def lookup_result(self, action: Action) -> ProviderReceipt | DispatchPending | None: ...


class ActionPumpResult(FrozenModel):
    action_id: str | None = None
    status: Literal[
        "idle",
        "not_due",
        "owned_elsewhere",
        "dispatched",
        "pending",
        "settled",
        "marked_unknown",
        "expired",
    ]


class ActionPump:
    """Claim, persist dispatch start, then delegate exactly one Action effect."""

    def __init__(
        self,
        *,
        ledger: LedgerPort,
        executor: ActionExecutor,
        settle: Callable[[ExternalObservation], Awaitable[object]],
        owner_id: str,
        lease_seconds: int = 120,
        source: str = "world-v2:action-pump",
    ) -> None:
        if not owner_id or lease_seconds <= 0:
            raise ValueError("action pump needs owner and positive lease")
        self._ledger = ledger
        self._executor = executor
        self._settle = settle
        self._owner_id = owner_id
        self._lease_seconds = lease_seconds
        self._source = source

    async def drain_once(self) -> ActionPumpResult:
        """Advance one eligible Action or recover one started dispatch.

        A crash after ``ActionDispatchStarted`` is intentionally observable in
        the ledger.  ``none`` policy becomes ``unknown`` without re-dispatch;
        idempotent policies first query then reuse the same provider key.
        """

        for _attempt in range(3):
            try:
                return await self._drain_once()
            except (ConcurrencyConflict, IdempotencyConflict):
                # A different runtime won the ledger CAS. Re-read the single
                # authority before deciding whether there is work left; never
                # continue an external effect from a stale in-memory Action.
                continue
        raise ConcurrencyConflict("action pump did not converge after ledger contention")

    async def _drain_once(self) -> ActionPumpResult:
        projection = await self._project()
        expired = next(
            (
                item
                for item in projection.actions
                if item.state in {"authorized", "scheduled", "claimed"}
                and item.expires_at is not None
                and (projection.logical_time or item.logical_time) >= item.expires_at
            ),
            None,
        )
        if expired is not None:
            await self._settle_checked(
                action=expired, result=await self._expired_observation(expired)
            )
            return ActionPumpResult(action_id=expired.action_id, status="expired")
        action = next(
            (
                item
                for item in projection.actions
                if item.state == "authorized" and self._expression_dispatch_allowed(item, projection)
            ),
            None,
        )
        if action is not None:
            await self._schedule(action=action, projection=projection)
            projection = await self._project()
        action = next(
            (
                item
                for item in projection.actions
                if item.state == "scheduled"
                and self._is_due(action=item, logical_time=projection.logical_time)
                and self._dependencies_delivered(action=item, actions=projection.actions)
                and self._expression_dispatch_allowed(item, projection)
            ),
            None,
        )
        if action is not None:
            claimed = await self._claim_or_reclaim(action=action, projection=projection)
            if claimed is None:
                return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
            return await self._start_and_dispatch(claimed)
        blocked = next((item for item in projection.actions if item.state == "scheduled"), None)
        if blocked is not None:
            return ActionPumpResult(action_id=blocked.action_id, status="not_due")
        action = next(
            (
                item
                for item in projection.actions
                if item.state == "claimed" and self._expression_dispatch_allowed(item, projection)
            ),
            None,
        )
        if action is not None:
            claimed = await self._claim_or_reclaim(action=action, projection=projection)
            if claimed is None:
                return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
            return await self._start_and_dispatch(claimed)
        action = next((item for item in projection.actions if item.state == "dispatch_started"), None)
        if action is not None:
            return await self._recover_dispatch(action)
        action = next((item for item in projection.actions if item.state == "provider_accepted"), None)
        if action is not None:
            return await self._recover_provider_accepted(action)
        return ActionPumpResult(status="idle")

    async def _schedule(self, *, action: Action, projection) -> None:
        await self._commit_event(
            action=action,
            event_type="ActionScheduled",
            payload={"action_id": action.action_id},
            projection=projection,
            suffix="scheduled",
            at=projection.logical_time or action.logical_time,
        )

    async def _claim_or_reclaim(self, *, action: Action, projection) -> Action | None:
        at = projection.logical_time or action.logical_time
        lease = action.claim_lease
        if action.state == "claimed" and lease is not None:
            if at < lease.expires_at:
                return None
        attempt_id = "attempt:action-pump:" + _digest(
            [action.action_id, "initial" if lease is None else lease.attempt_id]
        )
        new_lease = ClaimLease(
            owner_id=self._owner_id,
            attempt_id=attempt_id,
            acquired_at=at,
            expires_at=at + timedelta(seconds=self._lease_seconds),
        )
        event_type = "ActionClaimed" if action.state == "scheduled" else "ActionReclaimed"
        await self._commit_event(
            action=action,
            event_type=event_type,
            payload={"action_id": action.action_id, "claim_lease": new_lease.model_dump(mode="json")},
            projection=projection,
            suffix=attempt_id,
            at=at,
        )
        return action.model_copy(update={"state": "claimed", "claim_lease": new_lease})

    async def _start_and_dispatch(self, action: Action) -> ActionPumpResult:
        assert action.claim_lease is not None
        projection = await self._project()
        current = next(
            (item for item in projection.actions if item.action_id == action.action_id), None
        )
        if current is None or current.state != "claimed":
            return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
        if not self._expression_dispatch_allowed(current, projection):
            # A new Observation may have committed between claim and the
            # executor call.  Never hand the frozen old payload to a provider
            # while its reconsideration gate is unresolved.
            return ActionPumpResult(action_id=action.action_id, status="not_due")
        await self._enforce_executor_authority(action=current, projection=projection)
        action = current
        at = projection.logical_time or action.logical_time
        if at >= action.claim_lease.expires_at:
            return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
        await self._commit_event(
            action=action,
            event_type="ActionDispatchStarted",
            payload={
                "action_id": action.action_id,
                "owner_id": action.claim_lease.owner_id,
                "attempt_id": action.claim_lease.attempt_id,
                "started_at": at.isoformat(),
            },
            projection=projection,
            suffix=f"dispatch:{action.claim_lease.attempt_id}",
            at=at,
        )
        result = await self._executor.dispatch(action)
        return await self._settle_or_pending(action=action, result=result, dispatched=True)

    async def _enforce_executor_authority(self, *, action: Action, projection) -> None:
        """Call an executor's optional, narrow pre-dispatch authority seam.

        The legacy platform executor intentionally has no such method, so
        ordinary message/reaction Actions retain their existing contract.  A
        provider-media executor implements it; the check happens after the
        final CAS re-read and before ``ActionDispatchStarted``, making a stale
        consent/privacy revision incapable of reaching the provider.
        """

        checker = getattr(self._executor, "assert_dispatch_authorized", None)
        if checker is None:
            return
        result = checker(action=action, projection=projection)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    def _expression_dispatch_allowed(action: Action, projection) -> bool:
        if action.expression_plan_id is None:
            return True
        beat_id = action.expression_beat_id
        assert beat_id is not None
        beat = next((item for item in projection.expression_beats if item.beat_id == beat_id), None)
        plan = next(
            (item for item in projection.expression_plans if item.plan_id == action.expression_plan_id),
            None,
        )
        if (
            beat is None
            or plan is None
            or beat.state != "authorized"
            or plan.state != "authorized"
            or beat.plan_id != action.expression_plan_id
            or beat.action_id != action.action_id
        ):
            return False
        return not expression_beat_is_gated(
            projection=projection,
            plan_id=action.expression_plan_id,
            beat_id=beat_id,
        )

    async def _recover_dispatch(self, action: Action) -> ActionPumpResult:
        current_time = (await self._project()).logical_time or action.logical_time
        pending = action.dispatch_pending
        if pending is not None:
            if current_time < pending.lookup_after:
                return ActionPumpResult(action_id=action.action_id, status="pending")
            if current_time >= pending.deadline:
                receipt = await self._unknown_receipt(
                    action, error_class="provider_pending_deadline_elapsed"
                )
                await self._settle_checked(
                    action=action,
                    result=self._external_observation(action=action, receipt=receipt),
                )
                return ActionPumpResult(action_id=action.action_id, status="marked_unknown")
            await self._enforce_executor_authority(action=action, projection=await self._project())
            result = await self._executor.lookup_result(action)
            if result is None:
                if action.recovery_policy == "none":
                    return ActionPumpResult(action_id=action.action_id, status="pending")
                await self._enforce_executor_authority(action=action, projection=await self._project())
                result = await self._executor.dispatch(action)
            return await self._settle_or_pending(action=action, result=result, dispatched=False)
        if action.claim_lease is not None and current_time < action.claim_lease.expires_at:
            # ``dispatch_started`` is the durable hand-off to an in-flight
            # executor. A second worker may recover only after that finite
            # lease, never merely because it shares the same owner id.
            return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
        if action.recovery_policy == "none":
            result = await self._unknown_receipt(action)
            await self._settle_checked(
                action=action, result=self._external_observation(action=action, receipt=result)
            )
            return ActionPumpResult(action_id=action.action_id, status="marked_unknown")
        if action.recovery_policy not in {"effect_once", "result_lookup"}:
            raise ValueError(f"unsupported Action recovery policy {action.recovery_policy!r}")
        await self._enforce_executor_authority(action=action, projection=await self._project())
        result = await self._executor.lookup_result(action)
        if result is None:
            await self._enforce_executor_authority(action=action, projection=await self._project())
            result = await self._executor.dispatch(action)
        return await self._settle_or_pending(action=action, result=result, dispatched=False)

    async def _recover_provider_accepted(self, action: Action) -> ActionPumpResult:
        await self._enforce_executor_authority(action=action, projection=await self._project())
        current_time = (await self._project()).logical_time or action.logical_time
        if action.claim_lease is not None and current_time < action.claim_lease.expires_at:
            return ActionPumpResult(action_id=action.action_id, status="owned_elsewhere")
        # A provider acknowledgement is not delivery. Once its recovery lease
        # has elapsed, preserve that fact but terminate the original Action as
        # unknown; any later provider result goes through reconciliation.
        receipt = await self._unknown_receipt(
            action, error_class="provider_accepted_without_terminal_receipt"
        )
        await self._settle_checked(
            action=action, result=self._external_observation(action=action, receipt=receipt)
        )
        return ActionPumpResult(action_id=action.action_id, status="marked_unknown")

    async def _settle_or_pending(
        self, *, action: Action, result: ProviderReceipt | DispatchPending | None, dispatched: bool
    ) -> ActionPumpResult:
        if result is None:
            return ActionPumpResult(
                action_id=action.action_id, status="pending" if dispatched else "dispatched"
            )
        if isinstance(result, DispatchPending):
            self._validate_pending(action=action, pending=result)
            await self._record_pending(action=action, pending=result)
            return ActionPumpResult(action_id=action.action_id, status="pending")
        await self._settle_checked(
            action=action, result=self._external_observation(action=action, receipt=result)
        )
        return ActionPumpResult(action_id=action.action_id, status="settled")

    async def _settle_checked(self, *, action: Action, result: ExternalObservation) -> None:
        if (
            result.world_id != self._ledger.world_id
            or result.action_id != action.action_id
            or result.idempotency_key != action.idempotency_key
        ):
            raise ValueError("action executor returned a receipt for another effect")
        await self._settle(result)

    async def _unknown_receipt(
        self, action: Action, *, error_class: str = "dispatch_started_without_idempotent_recovery"
    ) -> ProviderReceipt:
        at = (await self._project()).logical_time or action.logical_time
        source_event_id = "unknown:" + _digest([action.action_id, action.claim_lease.attempt_id if action.claim_lease else "none"])
        pending = action.dispatch_pending
        provider = pending.provider if pending is not None else self._source
        provider_ref = (
            (pending.provider_ref or source_event_id) if pending is not None else source_event_id
        )
        return ProviderReceipt(
            provider_receipt_id=f"receipt:action-unknown:{action.action_id}",
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider=provider,
            provider_ref=provider_ref,
            status="unknown",
            artifact_refs=(),
            cost_actual=0,
            error_class=error_class,
            received_at=at,
            raw_payload_hash="sha256:" + _digest([action.action_id, source_event_id]),
        )

    @staticmethod
    def _validate_pending(*, action: Action, pending: DispatchPending) -> None:
        if (
            pending.action_id != action.action_id
            or pending.idempotency_key != action.idempotency_key
            or pending.idempotency_mode != action.recovery_policy
        ):
                raise ValueError("action executor returned pending state for another effect")

    async def _record_pending(self, *, action: Action, pending: DispatchPending) -> None:
        projection = await self._project()
        at = projection.logical_time or action.logical_time
        await self._commit_event(
            action=action,
            event_type="ActionDispatchPending",
            payload={"pending": pending.model_dump(mode="json")},
            projection=projection,
            suffix=f"pending:{pending.provider}:{pending.provider_ref or 'unbound'}",
            at=at,
        )

    @staticmethod
    def _external_observation(*, action: Action, receipt: ProviderReceipt) -> ExternalObservation:
        if receipt.action_id != action.action_id or receipt.idempotency_key != action.idempotency_key:
            raise ValueError("action executor returned a receipt for another effect")
        pending = action.dispatch_pending
        if pending is not None and (
            receipt.provider != pending.provider
            or (pending.provider_ref is not None and receipt.provider_ref != pending.provider_ref)
        ):
            raise ValueError("action executor receipt does not bind the pending provider reference")
        return ExternalObservation(
            schema_version="world-v2.1",
            result_id=f"result:{receipt.provider}:{receipt.provider_receipt_id}",
            world_id=action.world_id,
            logical_time=receipt.received_at,
            created_at=receipt.received_at,
            trace_id=action.trace_id,
            causation_id=action.action_id,
            correlation_id=action.correlation_id,
            kind="execution_receipt",
            source=receipt.provider,
            source_event_id=receipt.provider_ref,
            action_id=receipt.action_id,
            idempotency_key=receipt.idempotency_key,
            status=receipt.status,
            provider_ref=receipt.provider_ref,
            artifact_refs=receipt.artifact_refs,
            cost_actual=receipt.cost_actual,
            error_class=receipt.error_class,
            observed_at=receipt.received_at,
            raw_payload_hash=receipt.raw_payload_hash,
        )

    async def _expired_observation(self, action: Action) -> ExternalObservation:
        at = (await self._project()).logical_time or action.logical_time
        source_event_id = "expired:" + _digest([action.action_id, action.expires_at.isoformat()])
        return ExternalObservation(
            schema_version="world-v2.1",
            result_id=f"result:action-expired:{action.action_id}",
            world_id=action.world_id,
            logical_time=at,
            created_at=at,
            trace_id=action.trace_id,
            causation_id=action.action_id,
            correlation_id=action.correlation_id,
            kind="execution_receipt",
            source=self._source,
            source_event_id=source_event_id,
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            status="expired",
            provider_ref=source_event_id,
            artifact_refs=(),
            cost_actual=0,
            error_class="action_deadline_elapsed_before_dispatch",
            observed_at=at,
            raw_payload_hash="sha256:" + _digest([action.action_id, source_event_id]),
        )

    async def _commit_event(
        self,
        *,
        action: Action,
        event_type: str,
        payload: dict[str, object],
        projection,
        suffix: str,
        at,
    ) -> None:
        # The event catalog gives action lifecycle events a stable identity
        # shape, but they intentionally have no public domain-id function:
        # claim ownership is runtime-private.  Bind that private identity to
        # the immutable action and exact event payload.
        identity = domain_idempotency_key(
            event_type=event_type, world_id=action.world_id, payload=payload
        ) or "world-v2:action-pump:" + _digest([event_type, action.world_id, payload])
        event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=f"event:action-pump:{event_type.lower()}:{_digest([action.action_id, suffix])}",
            world_id=action.world_id,
            event_type=event_type,
            logical_time=at,
            created_at=at,
            actor=self._owner_id,
            source=self._source,
            trace_id=action.trace_id,
            causation_id=action.causation_id,
            correlation_id=action.correlation_id,
            idempotency_key=identity,
            payload=payload,
        )
        commit_id = f"commit:action-pump:{event_type.lower()}:{_digest([action.action_id, suffix])}"
        if self._ledger.blocks_event_loop:
            import asyncio

            await asyncio.to_thread(
                self._ledger.commit,
                [event],
                expected_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
                commit_id=commit_id,
            )
            return
        self._ledger.commit(
            [event],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
            commit_id=commit_id,
        )

    async def _project(self):
        if self._ledger.blocks_event_loop:
            import asyncio

            return await asyncio.to_thread(self._ledger.project)
        return self._ledger.project()

    @staticmethod
    def _is_due(*, action: Action, logical_time) -> bool:
        return action.not_before is None or (logical_time is not None and action.not_before <= logical_time)

    @staticmethod
    def _dependencies_delivered(*, action: Action, actions: tuple[Action, ...]) -> bool:
        by_id = {item.action_id: item for item in actions}
        return all(by_id.get(dependency_id) is not None and by_id[dependency_id].state == "delivered" for dependency_id in action.dependencies)

__all__ = ["ActionExecutor", "ActionPump", "ActionPumpResult"]
