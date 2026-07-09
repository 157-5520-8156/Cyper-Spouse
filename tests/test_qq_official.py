import json

from companion_daemon.qq_official import (
    incoming_message_from_payload,
    sign_validation_response,
    verify_callback_signature,
)


def test_validation_signature_matches_official_example() -> None:
    signature = sign_validation_response(
        "DG5g3B4j9X2KOErG",
        "1725442341",
        "Arq0D5A61EgUu4OxUvOp",
    )

    assert (
        signature
        == "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"
    )


def test_callback_signature_verification_roundtrip() -> None:
    secret = "naOC0ocQE3shWLAfffVLB1rhYPG7"
    timestamp = "1725442341"
    body = json.dumps({"op": 0, "d": {}, "t": "GATEWAY_EVENT_NAME"}, separators=(",", ":")).encode(
        "utf-8"
    )
    signature = sign_validation_response(secret, timestamp, body.decode("utf-8"))

    assert verify_callback_signature(secret, timestamp, body, signature)


def test_parse_c2c_message() -> None:
    payload = {
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg-1",
            "content": "  在吗  ",
            "author": {"user_openid": "user-openid"},
        },
    }

    message = incoming_message_from_payload(payload)

    assert message is not None
    assert message.platform == "qq"
    assert message.platform_user_id == "user-openid"
    assert message.text == "在吗"
