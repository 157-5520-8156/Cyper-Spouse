import argparse
import asyncio
from pathlib import Path

from companion_daemon.config import get_settings
from companion_daemon.qq_client import QQOfficialClient
from companion_daemon.runtime import build_companion_engine


async def run(user_id: str, *, send: bool, sandbox: bool) -> None:
    engine = build_companion_engine()
    decision = await engine.proactive_tick(user_id)
    print(f"private: {decision.private_thought}")
    print(f"should_send: {decision.should_send}")
    print(f"platform: {decision.platform}")
    print(f"message_type: {decision.message_type}")
    print(f"message: {decision.message or ''}")
    if decision.sticker_path:
        print(f"sticker: {decision.sticker_path}")
    if decision.image_path:
        print(f"image: {decision.image_path}")

    if not send:
        return
    if not decision.should_send or decision.platform != "qq":
        print("not sent: decision did not produce a QQ message")
        return

    openid = engine.store.platform_user_id(user_id, "qq")
    if not openid:
        print("not sent: no QQ account mapping for this user")
        return

    settings = get_settings()
    if not settings.qq_bot_app_id or not settings.qq_bot_secret:
        print("not sent: QQ_BOT_APP_ID and QQ_BOT_SECRET are required")
        return

    api_base_url = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"
    client = QQOfficialClient(
        settings.qq_bot_app_id,
        settings.qq_bot_secret,
        api_base_url=api_base_url,
    )
    if decision.image_path:
        await client.send_c2c_local_image(
            openid,
            Path(decision.image_path),
            content=decision.message,
            is_wakeup=True,
        )
    elif decision.sticker_path:
        await client.send_c2c_local_image(
            openid,
            Path(decision.sticker_path),
            content=decision.message,
            is_wakeup=True,
        )
    elif decision.message:
        await client.send_c2c_text(openid, decision.message, is_wakeup=True)
    else:
        print("not sent: no text or sticker payload")
        return
    engine.store.record_proactive_delivery(user_id, "qq")
    print("sent: QQ proactive wakeup message")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one proactive companion decision.")
    parser.add_argument("--user", default="geoff", help="Canonical user id.")
    parser.add_argument("--send", action="store_true", help="Actually send if the decision allows it.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API for sending.")
    args = parser.parse_args()
    asyncio.run(run(args.user, send=args.send, sandbox=args.sandbox))
