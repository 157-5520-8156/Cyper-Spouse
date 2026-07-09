import binascii
from typing import Any

from nacl.signing import SigningKey

from companion_daemon.models import IncomingMessage

QQ_CALLBACK_VALIDATION_OP = 13
QQ_HTTP_CALLBACK_ACK_OP = 12


def _seed_from_secret(secret: str) -> bytes:
    repeated = secret
    while len(repeated.encode("utf-8")) < 32:
        repeated += repeated
    return repeated.encode("utf-8")[:32]


def sign_validation_response(secret: str, event_ts: str, plain_token: str) -> str:
    signing_key = SigningKey(_seed_from_secret(secret))
    signed = signing_key.sign(f"{event_ts}{plain_token}".encode("utf-8"))
    return signed.signature.hex()


def verify_callback_signature(
    secret: str,
    timestamp: str,
    raw_body: bytes,
    signature_hex: str,
) -> bool:
    try:
        signature = binascii.unhexlify(signature_hex)
    except (binascii.Error, ValueError):
        return False
    verify_key = SigningKey(_seed_from_secret(secret)).verify_key
    message = timestamp.encode("utf-8") + raw_body
    try:
        verify_key.verify(message, signature)
    except Exception:
        return False
    return True


def validation_response(secret: str, payload: dict[str, Any]) -> dict[str, str]:
    data = payload.get("d") or {}
    plain_token = str(data["plain_token"])
    event_ts = str(data["event_ts"])
    return {
        "plain_token": plain_token,
        "signature": sign_validation_response(secret, event_ts, plain_token),
    }


def ack_response() -> dict[str, int]:
    return {"op": QQ_HTTP_CALLBACK_ACK_OP}


def incoming_message_from_payload(payload: dict[str, Any]) -> IncomingMessage | None:
    event_type = payload.get("t")
    data = payload.get("d") or {}

    if event_type == "C2C_MESSAGE_CREATE":
        user_id = str(data.get("author", {}).get("user_openid") or data.get("author", {}).get("id"))
        text = str(data.get("content") or "").strip()
        if not user_id or not text:
            return None
        return IncomingMessage(
            platform="qq",
            platform_user_id=user_id,
            text=text,
            channel_id=str(data.get("id") or ""),
            message_id=str(data.get("id") or ""),
        )

    if event_type == "GROUP_AT_MESSAGE_CREATE":
        group_id = str(data.get("group_openid") or data.get("group_id") or "")
        user_id = str(data.get("author", {}).get("member_openid") or data.get("author", {}).get("id"))
        text = str(data.get("content") or "").strip()
        if not user_id or not text:
            return None
        return IncomingMessage(
            platform="qq",
            platform_user_id=user_id,
            text=text,
            channel_id=group_id,
            message_id=str(data.get("id") or ""),
        )

    return None
