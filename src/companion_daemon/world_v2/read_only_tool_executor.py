"""Provider adapter for already-authorized non-mutating tool Actions."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Protocol

from .action_pump import ActionExecutor
from .read_only_tool import ToolQueryReader
from .schemas import Action, DispatchPending, ProviderReceipt


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReadOnlyToolTransport(Protocol):
    provider: str

    async def execute(
        self,
        *,
        target: str,
        tool_name: str,
        query_ref: str,
        query_hash: str,
        body: str,
        idempotency_key: str,
    ) -> tuple[str, str, str, int, datetime]: ...

    async def lookup(
        self, *, idempotency_key: str
    ) -> tuple[str, str, str, int, datetime] | None: ...


class ReadOnlyToolActionExecutor(ActionExecutor):
    """Read one frozen request, call one tool, return an immutable result descriptor.

    The executor cannot obtain a ledger or settle its own receipt.  The result
    bytes live in a provider-owned/sidecar ref; only their ref/hash cross this
    seam.  Settlement decides whether that descriptor becomes a World v2
    ``ToolResultAccepted`` event.
    """

    def __init__(self, *, queries: ToolQueryReader, transport: ReadOnlyToolTransport) -> None:
        if not transport.provider:
            raise ValueError("read-only tool transport provider is required")
        self._queries = queries
        self._transport = transport

    async def dispatch(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        tool_name, query_ref, query_hash, body = await self._query(action)
        result_ref, result_hash, provider_ref, cost, received_at = await self._transport.execute(
            target=action.target,
            tool_name=tool_name,
            query_ref=query_ref,
            query_hash=query_hash,
            body=body,
            idempotency_key=action.idempotency_key,
        )
        return self._receipt(
            action=action,
            result_ref=result_ref,
            result_hash=result_hash,
            provider_ref=provider_ref,
            cost_actual=cost,
            received_at=received_at,
        )

    async def lookup_result(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        result = await self._transport.lookup(idempotency_key=action.idempotency_key)
        if result is None:
            return None
        result_ref, result_hash, provider_ref, cost, received_at = result
        return self._receipt(
            action=action,
            result_ref=result_ref,
            result_hash=result_hash,
            provider_ref=provider_ref,
            cost_actual=cost,
            received_at=received_at,
        )

    async def _query(self, action: Action) -> tuple[str, str, str, str]:
        if action.kind != "read_only_tool" or action.layer != "read_only_tool":
            raise ValueError("read-only tool executor received another Action kind")
        tool_name, query_ref, query_hash, body = await self._queries.resolve(action)
        if query_ref != action.payload_ref or query_hash != action.payload_hash:
            raise ValueError("resolved query does not bind authorized Action payload")
        actual = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
        if actual != action.payload_hash:
            raise ValueError("resolved query bytes do not bind authorized Action payload")
        return tool_name, query_ref, query_hash, body

    def _receipt(
        self,
        *,
        action: Action,
        result_ref: str,
        result_hash: str,
        provider_ref: str,
        cost_actual: int,
        received_at: datetime,
    ) -> ProviderReceipt:
        if not result_ref or not result_hash or not provider_ref or cost_actual < 0:
            raise ValueError("read-only tool transport returned incomplete result evidence")
        identity = _digest(
            {
                "action_id": action.action_id,
                "idempotency_key": action.idempotency_key,
                "provider_ref": provider_ref,
                "result_ref": result_ref,
                "result_hash": result_hash,
            }
        )
        return ProviderReceipt(
            provider_receipt_id="receipt:read-only-tool:" + identity,
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider=self._transport.provider,
            provider_ref=provider_ref,
            status="delivered",
            artifact_refs=(),
            cost_actual=cost_actual,
            received_at=received_at,
            raw_payload_hash="sha256:" + identity,
            result_ref=result_ref,
            result_hash=result_hash,
        )


__all__ = ["ReadOnlyToolActionExecutor", "ReadOnlyToolTransport"]
