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
        if self._uses_onebot():
            await self._onebot_target(recipient_id).reply(content=text)
            return
        self._require_official_adapter()
        await self._official_client().send_c2c_text(recipient_id, text, is_wakeup=True)

    async def send_image(self, recipient_id: str, image_path: Path, *, content: str | None = None) -> None:
        if self._uses_onebot():
            target = self._onebot_target(recipient_id)
            if content:
                await target.reply(content=content)
            await target.send_image(image_path)
            return
        self._require_official_adapter()
        await self._official_client().send_c2c_local_image(
            recipient_id, image_path, content=content, is_wakeup=True
        )

    def proactive_recipient_id(self) -> str | None:
        adapter = self.settings.qq_adapter.lower()
        if adapter == "napcat":
            return self.settings.napcat_proactive_user_id
        if adapter == "onebot":
            return self.settings.onebot_proactive_user_id
        return None

    def supports_delivery_receipts(self) -> bool:
        """Whether this adapter can later prove an interrupted outbound send.

        Current official and OneBot send APIs return a synchronous acceptance
        response, not a durable delivery-query capability.  Keep this false
        until an adapter implements a real receipt lookup instead of guessing.
        """
        return False

    def _uses_onebot(self) -> bool:
        return self.settings.qq_adapter.lower() in {"napcat", "onebot"}

    def _onebot_target(self, recipient_id: str) -> OneBotReplyTarget:
        try:
            user_id = int(recipient_id)
        except ValueError as exc:
            raise ValueError("OneBot private recipients must be numeric QQ account ids") from exc
        if self.settings.qq_adapter.lower() == "onebot":
            return OneBotReplyTarget(
                api_url=self.settings.onebot_api_url,
                user_id=user_id,
                access_token=self.settings.onebot_access_token or None,
            )
        return OneBotReplyTarget(
            api_url=self.settings.napcat_api_url,
            user_id=user_id,
            access_token=self.settings.napcat_access_token or None,
        )

    def _require_official_adapter(self) -> None:
        adapter = self.settings.qq_adapter.lower()
        if adapter != "official":
            raise ValueError(f"unsupported QQ_ADAPTER: {adapter}")

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
