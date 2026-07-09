import base64
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

import httpx


@dataclass
class QQAccessToken:
    value: str
    expires_at: float


class QQOfficialClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        api_base_url: str = "https://api.sgroup.qq.com",
        token_url: str = "https://bots.qq.com/app/getAppAccessToken",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base_url = api_base_url.rstrip("/")
        self.token_url = token_url
        self.transport = transport
        self._token: QQAccessToken | None = None

    async def send_c2c_text(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
        is_wakeup: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
        if event_id:
            payload["event_id"] = event_id
        if is_wakeup is not None:
            payload["is_wakeup"] = is_wakeup
        return await self._post(f"/v2/users/{openid}/messages", payload)

    async def send_group_text(
        self,
        group_openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            payload["msg_id"] = msg_id
        if event_id:
            payload["event_id"] = event_id
        return await self._post(f"/v2/groups/{group_openid}/messages", payload)

    async def upload_c2c_file_data(
        self,
        openid: str,
        path: Path,
        *,
        file_type: int = 1,
        srv_send_msg: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "srv_send_msg": srv_send_msg,
        }
        return await self._post(f"/v2/users/{openid}/files", payload)

    async def upload_group_file_data(
        self,
        group_openid: str,
        path: Path,
        *,
        file_type: int = 1,
        srv_send_msg: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "file_type": file_type,
            "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "srv_send_msg": srv_send_msg,
        }
        return await self._post(f"/v2/groups/{group_openid}/files", payload)

    async def send_c2c_media(
        self,
        openid: str,
        file_info: str,
        *,
        content: str | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        is_wakeup: bool | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
        if content:
            payload["content"] = content
        if msg_id:
            payload["msg_id"] = msg_id
        if event_id:
            payload["event_id"] = event_id
        if is_wakeup is not None:
            payload["is_wakeup"] = is_wakeup
        return await self._post(f"/v2/users/{openid}/messages", payload)

    async def send_group_media(
        self,
        group_openid: str,
        file_info: str,
        *,
        content: str | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"msg_type": 7, "media": {"file_info": file_info}}
        if content:
            payload["content"] = content
        if msg_id:
            payload["msg_id"] = msg_id
        if event_id:
            payload["event_id"] = event_id
        return await self._post(f"/v2/groups/{group_openid}/messages", payload)

    async def send_c2c_local_image(
        self,
        openid: str,
        path: Path,
        *,
        content: str | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        srv_send_msg: bool = False,
        is_wakeup: bool | None = None,
    ) -> dict[str, Any]:
        media = await self.upload_c2c_file_data(
            openid,
            path,
            file_type=1,
            srv_send_msg=srv_send_msg,
        )
        if srv_send_msg:
            return media
        return await self.send_c2c_media(
            openid,
            str(media["file_info"]),
            content=content,
            msg_id=msg_id,
            event_id=event_id,
            is_wakeup=is_wakeup,
        )

    async def send_group_local_image(
        self,
        group_openid: str,
        path: Path,
        *,
        content: str | None = None,
        msg_id: str | None = None,
        event_id: str | None = None,
        srv_send_msg: bool = False,
    ) -> dict[str, Any]:
        media = await self.upload_group_file_data(
            group_openid,
            path,
            file_type=1,
            srv_send_msg=srv_send_msg,
        )
        if srv_send_msg:
            return media
        return await self.send_group_media(
            group_openid,
            str(media["file_info"]),
            content=content,
            msg_id=msg_id,
            event_id=event_id,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        token = await self._access_token()
        async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
            response = await client.post(
                f"{self.api_base_url}{path}",
                headers={"Authorization": f"QQBot {token}"},
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def _access_token(self) -> str:
        if self._token and self._token.expires_at - monotonic() > 60:
            return self._token.value
        async with httpx.AsyncClient(transport=self.transport, timeout=10) as client:
            response = await client.post(
                self.token_url,
                json={"appId": self.app_id, "clientSecret": self.app_secret},
            )
            response.raise_for_status()
            data = response.json()
        expires_in = int(data.get("expires_in", 7200))
        self._token = QQAccessToken(
            value=str(data["access_token"]),
            expires_at=monotonic() + expires_in,
        )
        return self._token.value
