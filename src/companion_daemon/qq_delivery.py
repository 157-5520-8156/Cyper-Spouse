"""Outbound QQ routing shared by immediate and background companion work."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from companion_daemon.config import Settings
from companion_daemon.onebot_adapter import OneBotReplyTarget
from companion_daemon.qq_client import QQOfficialClient
from companion_daemon.emotion_reactions import qq_emoji_id


class QQDelivery:
    """Deliver one-to-one QQ messages through the configured adapter."""

    def __init__(self, settings: Settings, *, sandbox: bool = False):
        self.settings = settings
        self.sandbox = sandbox

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        if self._uses_onebot():
            return await self._onebot_target(recipient_id).reply(content=text)
        self._require_official_adapter()
        return await self._official_client().send_c2c_text(recipient_id, text, is_wakeup=True)

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        """Apply one accepted reaction token to the exact inbound message."""

        if not self._uses_onebot():
            raise ValueError("official QQ C2C reactions are not installed")
        emoji_id = qq_emoji_id(reaction_id)
        if emoji_id is None:
            raise ValueError("QQ reaction token is not in the deployment catalog")
        return await self._onebot_target(recipient_id).react_with_emoji(message_id, emoji_id)

    async def send_sticker(
        self, recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        """Send one catalogued standard QQ face segment."""

        if not self._uses_onebot():
            raise ValueError("official QQ C2C stickers are not installed")
        prefix = "qq-face:"
        if not sticker_id.startswith(prefix):
            raise ValueError("QQ sticker token is not a standard face reference")
        return await self._onebot_target(recipient_id).send_face(sticker_id.removeprefix(prefix))

    async def send_typing(
        self, recipient_id: str, *, state: str
    ) -> dict[str, object]:
        """Emit one NapCat input-status pulse selected by an accepted beat."""

        if self.settings.qq_adapter.lower() != "napcat":
            raise ValueError("typing status is installed only for NapCat")
        if state != "composing":
            raise ValueError("QQ typing state is not in the deployment catalog")
        return await self._onebot_target(recipient_id).set_input_status(event_type=1)

    @staticmethod
    def receipt_candidate(response: object | None) -> str | None:
        """Extract a persisted platform identifier without claiming final delivery."""
        if not response:
            return None
        candidates = [response]
        nested = response.get("data") if isinstance(response, Mapping) else getattr(response, "data", None)
        if nested:
            candidates.append(nested)
        for candidate in candidates:
            for key in ("message_id", "id", "msg_id"):
                value = (
                    candidate.get(key)
                    if isinstance(candidate, Mapping)
                    else getattr(candidate, key, None)
                )
                if value not in {None, ""}:
                    return f"platform:{key}:{value}"
        return None

    async def get_message(self, recipient_id: str, *, message_id: str) -> dict[str, object]:
        """Query one platform-persisted message by id (OneBot/NapCat only).

        A positive ``get_msg`` response is the durable delivery evidence that
        the synchronous send acknowledgement cannot provide by itself.
        """

        if not self._uses_onebot():
            raise ValueError("QQ message lookup is installed only for OneBot/NapCat")
        return await self._onebot_target(recipient_id).get_msg(message_id)

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

    async def send_image_message(
        self, recipient_id: str, *, image_path: Path
    ) -> dict[str, object]:
        """Send one image and return the provider's raw response envelope.

        The legacy ``send_image`` discards the response; the World v2 media
        delivery Action needs it as receipt evidence (message id, retcode).
        OneBot/NapCat only — the official adapter has no v2 media lane.
        """

        if not self._uses_onebot():
            raise ValueError("World v2 media delivery is installed for OneBot adapters only")
        return await self._onebot_target(recipient_id).send_image(image_path)

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
