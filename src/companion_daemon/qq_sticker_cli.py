import argparse
import asyncio
from pathlib import Path

from companion_daemon.config import get_settings
from companion_daemon.qq_client import QQOfficialClient
from companion_daemon.runtime import build_companion_engine
from companion_daemon.stickers import load_stickers


async def run(user_id: str, category: str, *, sandbox: bool) -> None:
    settings = get_settings()
    if not settings.qq_bot_app_id or not settings.qq_bot_secret:
        raise SystemExit("QQ_BOT_APP_ID and QQ_BOT_SECRET are required")

    engine = build_companion_engine()
    openid = engine.store.platform_user_id(user_id, "qq")
    if not openid:
        raise SystemExit(f"No QQ account mapping for canonical user {user_id!r}")

    catalog = load_stickers(str(settings.stickers_path))
    sticker = next((item for item in catalog.stickers if item.category == category), None)
    if not sticker:
        raise SystemExit(f"Unknown sticker category: {category}")

    api_base_url = "https://sandbox.api.sgroup.qq.com" if sandbox else "https://api.sgroup.qq.com"
    client = QQOfficialClient(
        settings.qq_bot_app_id,
        settings.qq_bot_secret,
        api_base_url=api_base_url,
    )
    await client.send_c2c_local_image(openid, Path(sticker.path))
    print(f"sent sticker: {sticker.category} -> {sticker.path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one local sticker image to a QQ C2C user.")
    parser.add_argument("--user", default="geoff", help="Canonical user id.")
    parser.add_argument("--category", default="happy", help="Sticker category.")
    parser.add_argument("--sandbox", action="store_true", help="Use QQ sandbox API.")
    args = parser.parse_args()
    asyncio.run(run(args.user, args.category, sandbox=args.sandbox))
