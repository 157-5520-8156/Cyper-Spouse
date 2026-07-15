"""Small platform-edge adapters for the standalone World v2 simulator.

They deliberately contain no world, ledger, prompt, or acceptance logic.  The
simulator is a real host of the same action interface used by a network
platform, which makes it suitable for exercising a persistent v2 turn without
constructing ``CompanionEngine`` as a second authority.
"""

from __future__ import annotations

from datetime import datetime
import hashlib

from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest


class SimulatorIdentityResolver:
    """Resolve one simulator account into stable World v2 actor references."""

    def __init__(self, *, canonical_user_id: str) -> None:
        if not canonical_user_id:
            raise ValueError("simulator canonical_user_id must not be empty")
        self._canonical_user_id = canonical_user_id

    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        if platform != "simulator" or not platform_user_id:
            raise ValueError("simulator ingress requires the simulator platform and a user id")
        reference = f"user:{self._canonical_user_id}"
        return reference, reference


class CaptureSimulatorTransport:
    """An idempotent transport that captures settled replies for a CLI host."""

    provider = "simulator:stdout"

    def __init__(self, *, received_at: datetime) -> None:
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("simulator receipt time must be timezone-aware")
        self._received_at = received_at
        self.bodies: list[str] = []
        self._receipts: dict[str, PlatformDispatchReceipt] = {}

    async def send(self, request: PlatformDispatchRequest) -> PlatformDispatchReceipt:
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            return existing
        identity = hashlib.sha256(request.fingerprint.encode("utf-8")).hexdigest()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:simulator:{identity}",
            provider_ref=f"message:simulator:{identity}",
            status="delivered",
            received_at=self._received_at,
            raw_payload_hash="sha256:" + hashlib.sha256(request.body.encode("utf-8")).hexdigest(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._receipts[request.idempotency_key] = receipt
        self.bodies.append(request.body)
        return receipt

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        receipt = self._receipts.get(idempotency_key)
        if receipt is not None and receipt.request_fingerprint != request_fingerprint:
            raise ValueError("simulator lookup fingerprint conflicts with the original dispatch")
        return receipt


__all__ = ["CaptureSimulatorTransport", "SimulatorIdentityResolver"]
