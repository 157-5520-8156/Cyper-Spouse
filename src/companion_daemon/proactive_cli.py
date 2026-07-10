import argparse
import asyncio
from pathlib import Path

from companion_daemon.config import get_settings
from companion_daemon.qq_delivery import QQDelivery
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

    settings = get_settings()
    delivery = QQDelivery(settings, sandbox=sandbox)
    recipient_id = delivery.proactive_recipient_id() or engine.store.platform_user_id(user_id, "qq")
    if not recipient_id:
        print("not sent: no outbound QQ recipient configured for this user")
        return
    try:
        if decision.image_path:
            await delivery.send_image(recipient_id, Path(decision.image_path), content=decision.message)
        elif decision.sticker_path:
            await delivery.send_image(recipient_id, Path(decision.sticker_path), content=decision.message)
        elif decision.message:
            await delivery.send_text(recipient_id, decision.message)
        else:
            print("not sent: no text or sticker payload")
            engine.fail_proactive_delivery(decision, "empty proactive payload")
            return
    except Exception as exc:
        engine.fail_proactive_delivery(decision, str(exc))
        raise
    engine.confirm_proactive_delivery(decision)
    print("sent: QQ proactive wakeup message")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one proactive companion decision.")
    parser.add_argument("--user", default="geoff", help="Canonical user id.")
    parser.add_argument("--send", action="store_true", help="Actually send if the decision allows it.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API for sending.")
    args = parser.parse_args()
    asyncio.run(run(args.user, send=args.send, sandbox=args.sandbox))
