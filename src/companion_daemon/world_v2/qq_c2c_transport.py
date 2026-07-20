"""QQ C2C adapter for an already-authorized World v2 text Action.

The adapter is intentionally narrower than the legacy QQ coalescer.  It maps
an opaque, composition-owned World target to one configured QQ recipient, and
returns only evidence the provider actually supplied.  It cannot manufacture a
delivery confirmation or reach a World ledger.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Protocol

from pydantic import Field

from companion_daemon.qq_delivery import QQDelivery

from .platform_action_executor import PlatformDispatchReceipt, PlatformDispatchRequest
from .schema_core import FrozenModel


class QQC2CDelivery(Protocol):
    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]: ...

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]: ...

    async def send_sticker(
        self, recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]: ...

    async def send_typing(
        self, recipient_id: str, *, state: str
    ) -> dict[str, object]: ...


_MEDIA_DELIVERY_OUTBOX = Path("output/media-delivered")


class _ReactionPayload(FrozenModel):
    provider_message_id: str = Field(min_length=1, max_length=256)
    reaction_id: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^expression-reaction\.1$")


class _StickerPayload(FrozenModel):
    sticker_id: str = Field(pattern=r"^qq-face:[0-9]{1,10}$")
    version: str = Field(pattern=r"^expression-sticker\.1$")


class _TypingPayload(FrozenModel):
    state: str = Field(pattern=r"^composing$")
    version: str = Field(pattern=r"^expression-typing\.1$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _platform_message_id(provider_ref: str) -> str | None:
    """Extract the raw message id from a ``platform:<key>:<value>`` ack ref."""

    parts = provider_ref.split(":", 2)
    if len(parts) != 3 or parts[0] != "platform" or parts[1] not in {"message_id", "id", "msg_id"}:
        return None
    return parts[2] or None


def _get_msg_confirms(response: object, message_id: str) -> bool:
    """Whether one ``get_msg`` response positively identifies the message."""

    if not isinstance(response, Mapping):
        return False
    retcode = response.get("retcode")
    try:
        if retcode is not None and int(str(retcode)) != 0:
            return False
    except (TypeError, ValueError):
        return False
    status = str(response.get("status") or "").strip().lower()
    if status and status not in {"ok", "async"}:
        return False
    data = response.get("data")
    if not isinstance(data, Mapping):
        return False
    for key in ("message_id", "id", "msg_id"):
        value = data.get(key)
        if value not in {None, ""} and str(value) == message_id:
            return True
    return False


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
        recipient_id = self._recipients_by_target.get(request.target)
        if recipient_id is None:
            raise ValueError("QQ C2C Action target is not owned by this transport")
        existing = self._receipts.get(request.idempotency_key)
        if existing is not None:
            if existing.request_fingerprint != request.fingerprint:
                raise ValueError("QQ C2C idempotency key conflicts with the original request")
            return existing
        response = await self._dispatch(request=request, recipient_id=recipient_id)
        receipt = self._receipt_for(request=request, response=response)
        self._receipts[request.idempotency_key] = receipt
        return receipt

    async def _dispatch(
        self, *, request: PlatformDispatchRequest, recipient_id: str
    ) -> dict[str, object]:
        if (
            request.kind in {"reply", "followup", "proactive_message"}
            and request.content_type == "text/plain"
        ):
            return await self._delivery.send_text(recipient_id, request.body)
        if (
            request.kind == "reaction"
            and request.content_type == "application/vnd.world-v2.reaction+json"
        ):
            try:
                payload = _ReactionPayload.model_validate_json(request.body, strict=True)
            except ValueError as exc:
                raise ValueError("QQ reaction payload is invalid") from exc
            return await self._delivery.send_reaction(
                recipient_id,
                message_id=payload.provider_message_id,
                reaction_id=payload.reaction_id,
            )
        if (
            request.kind == "sticker"
            and request.content_type == "application/vnd.world-v2.sticker+json"
        ):
            try:
                payload = _StickerPayload.model_validate_json(request.body, strict=True)
            except ValueError as exc:
                raise ValueError("QQ sticker payload is invalid") from exc
            return await self._delivery.send_sticker(recipient_id, sticker_id=payload.sticker_id)
        if (
            request.kind == "typing"
            and request.content_type == "application/vnd.world-v2.typing+json"
        ):
            try:
                payload = _TypingPayload.model_validate_json(request.body, strict=True)
            except ValueError as exc:
                raise ValueError("QQ typing payload is invalid") from exc
            return await self._delivery.send_typing(recipient_id, state=payload.state)
        if (
            request.kind == "media_delivery"
            and request.content_type == "application/vnd.world-v2.media-artifact+json"
        ):
            return await self._dispatch_media(request=request, recipient_id=recipient_id)
        raise ValueError("QQ C2C transport does not support this Action payload")

    async def _dispatch_media(
        self, *, request: PlatformDispatchRequest, recipient_id: str
    ) -> dict[str, object]:
        """Send one operator-approved immutable artifact as a NapCat image.

        The Action executor has already verified the payload hash against the
        authorized Action, and the media-delivery approval gate ran on the
        final ledger projection.  This adapter only decodes the exact frozen
        bytes and forwards them; it cannot substitute or re-render an image.
        """

        send_image = getattr(self._delivery, "send_image_message", None)
        if not callable(send_image):
            raise ValueError("QQ delivery adapter does not support image messages")
        try:
            payload = json.loads(request.body)
            if not isinstance(payload, dict) or payload.get("encoding") != "base64":
                raise ValueError("unsupported media artifact encoding")
            image = base64.b64decode(str(payload.get("bytes") or ""), validate=True)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("QQ media artifact payload is invalid") from exc
        if not image:
            raise ValueError("QQ media artifact payload is empty")
        # A durable on-disk copy doubles as the audit trail of what was sent.
        _MEDIA_DELIVERY_OUTBOX.mkdir(parents=True, exist_ok=True)
        image_path = _MEDIA_DELIVERY_OUTBOX / (
            hashlib.sha256(image).hexdigest()[:24] + ".png"
        )
        if not image_path.exists():
            image_path.write_bytes(image)
        return await send_image(recipient_id, image_path=image_path)

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

    async def verify_delivery(
        self, *, idempotency_key: str, target: str, provider_ref: str
    ) -> PlatformDispatchReceipt | None:
        """Upgrade one acknowledged send to ``delivered`` with provider evidence.

        The synchronous send response only proves acceptance, but NapCat/OneBot
        expose ``get_msg``: a positive lookup of the acknowledged message id
        proves the platform durably persisted the message.  Without that
        evidence (no message id, no lookup capability, provider unreachable,
        or an unknown/negative response) this returns ``None`` and the caller
        keeps its existing uncertainty policy.  It never re-sends anything.
        """

        recipient_id = self._recipients_by_target.get(target)
        message_id = _platform_message_id(provider_ref)
        get_message = getattr(self._delivery, "get_message", None)
        if recipient_id is None or message_id is None or not callable(get_message):
            return None
        try:
            response = await get_message(recipient_id, message_id=message_id)
        except Exception:
            # An unreachable provider is uncertainty, not evidence.
            return None
        if not _get_msg_confirms(response, message_id):
            return None
        identity = _digest(
            {
                "idempotency_key": idempotency_key,
                "provider_ref": provider_ref,
                "verification": "get_msg",
            }
        )
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:qq-c2c:verified:{identity}",
            # A distinct reference keeps this terminal receipt from colliding
            # with the earlier ack receipt that shares the platform id.
            provider_ref=f"{provider_ref}:verified",
            status="delivered",
            received_at=self._now(),
            raw_payload_hash="sha256:" + _digest(response),
            idempotency_key=idempotency_key,
        )

    def _receipt_for(
        self, *, request: PlatformDispatchRequest, response: dict[str, object]
    ) -> PlatformDispatchReceipt:
        raw_payload_hash = "sha256:" + _digest(response)
        platform_ref = QQDelivery.receipt_candidate(response)
        provider_status = str(response.get("status") or "").strip().lower()
        retcode = response.get("retcode")
        try:
            rejected = provider_status == "failed" or (
                retcode is not None and int(str(retcode)) != 0
            )
        except (TypeError, ValueError):
            rejected = provider_status == "failed"
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
            provider_ref=(
                f"qq-c2c:rejected:{identity}"
                if rejected
                else platform_ref or f"qq-c2c:unverified:{identity}"
            ),
            status="failed" if rejected else "provider_accepted",
            error_class="provider_rejected" if rejected else None,
            received_at=self._now(),
            raw_payload_hash=raw_payload_hash,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )


__all__ = ["QQC2CDelivery", "QQC2CPlatformTransport"]
