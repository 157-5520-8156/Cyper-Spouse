"""QQ C2C adapter for an already-authorized World v2 text Action.

The adapter is intentionally narrower than the legacy QQ coalescer.  It maps
an opaque, composition-owned World target to one configured QQ recipient, and
returns only evidence the provider actually supplied.  It cannot manufacture a
delivery confirmation or reach a World ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
from typing import Protocol

from companion_daemon.qq_delivery import QQDelivery

from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest


class QQC2CDelivery(Protocol):
    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]: ...


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


class QQC2CPlatformTransport:
    """Dispatch text to one of the explicitly registered QQ C2C targets.

    QQ's current send API acknowledges accepting a request but does not expose
    a durable delivery lookup.  Therefore a fresh response is recorded as
    ``provider_accepted`` (never ``delivered``).  If the process dies before
    settlement, an empty lookup lets Action recovery mark the effect unknown
    rather than duplicating an uncertain human-facing send.
    """

    provider = "qq:c2c"

    def __init__(
        self,
        *,
        delivery: QQC2CDelivery,
        recipients_by_target: Mapping[str, str],
        now: Callable[[], datetime],
    ) -> None:
        if not recipients_by_target or any(not key or not value for key, value in recipients_by_target.items()):
            raise ValueError("QQ C2C transport requires non-empty owned target mappings")
        self._delivery = delivery
        self._recipients_by_target = dict(recipients_by_target)
        self._now = now
        self._receipts: dict[str, PlatformDispatchReceipt] = {}

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        if request.kind != "reply" or request.content_type != "text/plain":
            raise ValueError("QQ C2C transport supports only authorized text replies")
        recipient_id = self._recipients_by_target.get(request.target)
        if recipient_id is None:
            raise ValueError("QQ C2C Action target is not owned by this transport")
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint:
                raise ValueError("QQ C2C idempotency key conflicts with the original request")
            return existing
        response = await self._delivery.send_text(recipient_id, request.body)
        receipt = self._receipt_for(request=request, response=response)
        self._receipts[request.idempotency_key] = receipt
        return receipt

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ValueError("QQ C2C lookup fingerprint conflicts with the original dispatch")
        # A new process has no provider-side lookup capability.  Returning
        # None is intentional: ActionPump will apply the existing recovery
        # policy instead of re-sending an uncertain reply.
        return receipt

    def _receipt_for(
        self, *, request: PlatformDispatchRequest, response: dict[str, object]
    ) -> PlatformDispatchReceipt:
        raw_payload_hash = "sha256:" + _digest(response)
        platform_ref = QQDelivery.receipt_candidate(response)
        identity = _digest(
            {
                "idempotency_key": request.idempotency_key,
                "request_fingerprint": request.fingerprint,
                "platform_ref": platform_ref,
                "raw_payload_hash": raw_payload_hash,
            }
        )
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:qq-c2c:{identity}",
            provider_ref=platform_ref or f"qq-c2c:unverified:{identity}",
            status="provider_accepted",
            received_at=self._now(),
            raw_payload_hash=raw_payload_hash,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )


__all__ = ["QQC2CDelivery", "QQC2CPlatformTransport"]
