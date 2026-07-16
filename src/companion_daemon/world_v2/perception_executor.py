"""Injection-only provider adapter for accepted perception Actions."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Protocol

from .action_pump import ActionExecutor
from .perception_authorization import require_perception_authorization
from .schemas import Action, DispatchPending, LedgerProjection, ProviderReceipt


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class PerceptionInputReader(Protocol):
    async def resolve(self, action: Action) -> tuple[str, str, str]: ...


class PerceptionTransport(Protocol):
    provider: str

    async def analyze(
        self,
        *,
        analysis_kind: str,
        input_ref: str,
        input_hash: str,
        body: str,
        idempotency_key: str,
    ) -> tuple[str, str, str, int, datetime]: ...
    async def lookup(
        self, *, idempotency_key: str
    ) -> tuple[str, str, str, int, datetime] | None: ...


class PerceptionActionExecutor(ActionExecutor):
    def __init__(self, *, inputs: PerceptionInputReader, transport: PerceptionTransport) -> None:
        if not transport.provider:
            raise ValueError("perception transport provider is required")
        self._inputs, self._transport = inputs, transport
        self._authorizations: set[tuple[str, str, int]] = set()

    async def assert_dispatch_authorized(
        self, *, action: Action, projection: LedgerProjection
    ) -> None:
        binding = require_perception_authorization(
            action=action,
            projection=projection,
            logical_time=projection.logical_time or action.logical_time,
        )
        self._authorizations.add(
            (action.action_id, binding.capability_grant_id, binding.capability_grant_revision)
        )

    async def dispatch(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        input_ref, input_hash, body = await self._input(action)
        result_ref, result_hash, provider_ref, cost, received_at = await self._transport.analyze(
            analysis_kind=action.kind,
            input_ref=input_ref,
            input_hash=input_hash,
            body=body,
            idempotency_key=action.idempotency_key,
        )
        return self._receipt(action, result_ref, result_hash, provider_ref, cost, received_at)

    async def lookup_result(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        result = await self._transport.lookup(idempotency_key=action.idempotency_key)
        if result is None:
            return None
        return self._receipt(action, *result)

    async def _input(self, action: Action) -> tuple[str, str, str]:
        if action.kind not in {"vision", "transcription"} or action.layer != "perception_tool":
            raise ValueError("perception executor received another Action kind")
        binding = action.perception_authorization
        if binding is None:
            raise ValueError("perception Action lacks authorization")
        key = (action.action_id, binding.capability_grant_id, binding.capability_grant_revision)
        if key not in self._authorizations:
            raise ValueError("perception dispatch was not authorized by ActionPump")
        self._authorizations.remove(key)
        input_ref, input_hash, body = await self._inputs.resolve(action)
        if (
            input_ref != action.payload_ref
            or input_hash != action.payload_hash
            or "sha256:" + hashlib.sha256(body.encode()).hexdigest() != input_hash
        ):
            raise ValueError("resolved perception input does not bind authorized Action payload")
        return input_ref, input_hash, body

    def _receipt(
        self,
        action: Action,
        result_ref: str,
        result_hash: str,
        provider_ref: str,
        cost: int,
        received_at: datetime,
    ) -> ProviderReceipt:
        if not result_ref or not result_hash or not provider_ref or cost < 0:
            raise ValueError("perception transport returned incomplete result evidence")
        identity = _digest(
            {
                "action": action.action_id,
                "provider_ref": provider_ref,
                "result_ref": result_ref,
                "result_hash": result_hash,
            }
        )
        return ProviderReceipt(
            provider_receipt_id="receipt:perception:" + identity,
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider=self._transport.provider,
            provider_ref=provider_ref,
            status="delivered",
            cost_actual=cost,
            received_at=received_at,
            raw_payload_hash="sha256:" + identity,
            result_ref=result_ref,
            result_hash=result_hash,
        )


__all__ = ["PerceptionActionExecutor", "PerceptionInputReader", "PerceptionTransport"]
