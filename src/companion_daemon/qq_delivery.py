"""Outbound QQ routing shared by immediate and background companion work."""
from __future__ import annotations

from pathlib import Path

from companion_daemon.config import Settings
from companion_daemon.onebot_adapter import OneBotReplyTarget
from companion_daemon.qq_client import QQOfficialClient


class QQDelivery:
    """Deliver one-to-one QQ messages through the configured adapter."""

    def __init__(self, settings: Settings, *, sandbox: bool = False):
        self.settings = settings
        self.sandbox = sandbox

    async def send_text(self, recipient_id: str, text: str) -> None:
        if self.settings.qq_adapter.lower() == "napcat":
            await self._napcat_target(recipient_id).reply(content=text)
            return
        await self._official_client().send_c2c_text(recipient_id, text, is_wakeup=True)

    async def send_image(self, recipient_id: str, image_path: Path, *, content: str | None = None) -> None:
        if self.settings.qq_adapter.lower() == "napcat":
            target = self._napcat_target(recipient_id)
            if content:
                await target.reply(content=content)
            await target.send_image(image_path)
            return
        await self._official_client().send_c2c_local_image(
            recipient_id, image_path, content=content, is_wakeup=True
        )

    def proactive_recipient_id(self) -> str | None:
        if self.settings.qq_adapter.lower() == "napcat":
            return self.settings.napcat_proactive_user_id
        return None

    def _napcat_target(self, recipient_id: str) -> OneBotReplyTarget:
        try:
            user_id = int(recipient_id)
        except ValueError as exc:
            raise ValueError("NAPCAT_PROACTIVE_USER_ID must be a numeric QQ account id") from exc
        return OneBotReplyTarget(
            api_url=self.settings.napcat_api_url,
            user_id=user_id,
            access_token=self.settings.napcat_access_token or None,
        )

    def _official_client(self) -> QQOfficialClient:
        if not self.settings.qq_bot_app_id or not self.settings.qq_bot_secret:
            raise RuntimeError("QQ_BOT_APP_ID and QQ_BOT_SECRET are required for QQ_ADAPTER=official")
        api_base_url = (
            "https://sandbox.api.sgroup.qq.com" if self.sandbox else "https://api.sgroup.qq.com"
        )
        return QQOfficialClient(
            self.settings.qq_bot_app_id,
            self.settings.qq_bot_secret,
            api_base_url=api_base_url,
        )
