"""Platform-neutral Action executor for World v2.

This Adapter is intentionally outside ledger authority.  It reads only a
previously authorized payload, delegates one idempotent provider operation,
and returns raw receipt material for :class:`ActionPump` to settle.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Literal, Protocol

from .action_pump import ActionExecutor
from .schema_core import FrozenModel
from .schemas import Action, DispatchPending, ProviderReceipt


SUPPORTED_PLATFORM_ACTION_KINDS = frozenset({"reply", "reaction", "typing", "sticker"})
CONTENT_TYPES_BY_KIND = {
    "reply": frozenset({"text/plain"}),
    "reaction": frozenset({"application/vnd.world-v2.reaction+json"}),
    "typing": frozenset({"application/vnd.world-v2.typing+json"}),
    "sticker": frozenset({"application/vnd.world-v2.sticker+json"}),
}


class ResolvedActionPayload(FrozenModel):
    """Opaque payload bytes resolved from an accepted World v2 payload ref."""

    payload_ref: str
    payload_hash: str
    content_type: str
    body: str


class PlatformDispatchRequest(FrozenModel):
    action_id: str
    kind: Literal["reply", "reaction", "typing", "sticker"]
    target: str
    payload_ref: str
    payload_hash: str
    content_type: str
    body: str
    idempotency_key: str

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PlatformDispatchReceipt(FrozenModel):
    """Provider result without World identity; the executor binds that identity."""

    provider_receipt_id: str
    provider_ref: str
    status: Literal["provider_accepted", "delivered", "failed", "unknown"]
    artifact_refs: tuple[str, ...] = ()
    cost_actual: int = 0
    error_class: str | None = None
    received_at: datetime
    raw_payload_hash: str
    idempotency_key: str
    request_fingerprint: str | None = None


class AuthorizedPayloadReader(Protocol):
    async def resolve(self, action: Action) -> ResolvedActionPayload: ...


class PlatformTransport(Protocol):
    provider: str

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt | DispatchPending: ...

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | DispatchPending | None: ...


class PlatformActionExecutor(ActionExecutor):
    """Deep Adapter hiding payload verification and provider receipt binding.

    Its two public methods are the exact ``ActionExecutor`` Interface used by
    ``ActionPump``.  The executor never receives a ledger, runtime, reducer or
    viewer projection, so a platform migration cannot create a second write
    path.
    """

    def __init__(self, *, payloads: AuthorizedPayloadReader, transport: PlatformTransport) -> None:
        if not transport.provider:
            raise ValueError("platform transport provider is required")
        self._payloads = payloads
        self._transport = transport

    async def dispatch(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        request = await self._request_for(action)
        result = await self._transport.send(request)
        return self._bind_result(action=action, result=result, request=request)

    async def lookup_result(self, action: Action) -> ProviderReceipt | DispatchPending | None:
        request = await self._request_for(action)
        result = await self._transport.lookup(
            idempotency_key=action.idempotency_key, request_fingerprint=request.fingerprint
        )
        return self._bind_result(action=action, result=result, request=request)

    async def _request_for(self, action: Action) -> PlatformDispatchRequest:
        kind = self._kind(action)
        payload = await self._payloads.resolve(action)
        self._validate_payload(action=action, payload=payload)
        return PlatformDispatchRequest(
            action_id=action.action_id,
            kind=kind,
            target=action.target,
            payload_ref=payload.payload_ref,
            payload_hash=payload.payload_hash,
            content_type=payload.content_type,
            body=payload.body,
            idempotency_key=action.idempotency_key,
        )

    @staticmethod
    def _kind(action: Action) -> Literal["reply", "reaction", "typing", "sticker"]:
        if action.layer != "external_action" or action.kind not in SUPPORTED_PLATFORM_ACTION_KINDS:
            raise ValueError(f"platform executor does not support action kind {action.kind!r}")
        return action.kind  # type: ignore[return-value]

    @staticmethod
    def _validate_payload(*, action: Action, payload: ResolvedActionPayload) -> None:
        if payload.payload_ref != action.payload_ref or payload.payload_hash != action.payload_hash:
            raise ValueError("resolved payload does not bind the authorized Action")
        if payload.content_type not in CONTENT_TYPES_BY_KIND[action.kind]:
            raise ValueError("resolved payload content type is not allowed for Action kind")
        actual = "sha256:" + hashlib.sha256(payload.body.encode("utf-8")).hexdigest()
        if action.payload_hash != actual:
            raise ValueError("resolved payload hash does not match authorized payload bytes")

    def _bind_result(
        self,
        *,
        action: Action,
        result: PlatformDispatchReceipt | DispatchPending | None,
        request: PlatformDispatchRequest | None,
    ) -> ProviderReceipt | DispatchPending | None:
        if result is None:
            return None
        if isinstance(result, DispatchPending):
            if result.action_id != action.action_id or result.idempotency_key != action.idempotency_key:
                raise ValueError("platform pending result does not bind the Action")
            if result.provider != self._transport.provider:
                raise ValueError("platform pending result provider mismatch")
            return result
        if result.idempotency_key != action.idempotency_key:
            raise ValueError("platform receipt idempotency key does not bind the Action")
        if request is not None and result.request_fingerprint != request.fingerprint:
            raise ValueError("platform receipt request fingerprint does not bind dispatched payload")
        return ProviderReceipt(
            provider_receipt_id=result.provider_receipt_id,
            action_id=action.action_id,
            idempotency_key=action.idempotency_key,
            provider=self._transport.provider,
            provider_ref=result.provider_ref,
            status=result.status,
            artifact_refs=result.artifact_refs,
            cost_actual=result.cost_actual,
            error_class=result.error_class,
            received_at=result.received_at,
            raw_payload_hash=result.raw_payload_hash,
        )


__all__ = [
    "AuthorizedPayloadReader",
    "PlatformActionExecutor",
    "PlatformDispatchReceipt",
    "PlatformDispatchRequest",
    "PlatformTransport",
    "ResolvedActionPayload",
    "SUPPORTED_PLATFORM_ACTION_KINDS",
]
