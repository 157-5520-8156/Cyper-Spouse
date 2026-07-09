from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from companion_daemon.budget import BudgetGate, ESTIMATES
from companion_daemon.models import MessageAttachment


@dataclass(frozen=True)
class AttachmentInsight:
    kind: str
    summary: str
    confidence: float


class MultimodalAnalyzer:
    async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
        if attachment.kind == "file":
            return await self._analyze_file(attachment)
        if attachment.kind == "image":
            return AttachmentInsight(
                kind="image",
                summary="用户发来了一张图片；当前已记录图片链接和元信息，内容识别需要配置视觉模型。",
                confidence=0.3,
            )
        if attachment.kind == "audio":
            return AttachmentInsight(
                kind="audio",
                summary="用户发来了一段语音；当前已记录语音链接和元信息，转写需要配置 STT 模型。",
                confidence=0.3,
            )
        return AttachmentInsight(
            kind=attachment.kind,
            summary="用户发来一个附件；当前只能识别类型和元信息。",
            confidence=0.2,
        )

    async def _analyze_file(self, attachment: MessageAttachment) -> AttachmentInsight:
        if not attachment.url:
            return AttachmentInsight("file", "用户发来一个文件，但没有可下载 URL。", 0.2)
        if not _looks_like_text_file(attachment):
            return AttachmentInsight(
                "file",
                f"用户发来文件 {attachment.filename or '未命名'}，当前暂未解析此文件类型。",
                0.35,
            )
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(attachment.url)
            response.raise_for_status()
        text = response.text.strip()
        if len(text) > 800:
            text = text[:800] + "..."
        return AttachmentInsight(
            "file",
            f"用户发来文本文件 {attachment.filename or '未命名'}，内容摘要片段：{text}",
            0.75,
        )


class OpenAIMultimodalAnalyzer(MultimodalAnalyzer):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        vision_model: str,
        transcription_model: str,
        budget_gate: BudgetGate,
        allow_vision: bool = True,
        allow_transcription: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.vision_model = vision_model
        self.transcription_model = transcription_model
        self.budget_gate = budget_gate
        self.allow_vision = allow_vision
        self.allow_transcription = allow_transcription
        self.transport = transport

    async def analyze(self, attachment: MessageAttachment) -> AttachmentInsight:
        if attachment.kind == "image" and attachment.url and self.allow_vision:
            return await self._analyze_image(attachment)
        if attachment.kind == "audio" and attachment.url and self.allow_transcription:
            return await self._transcribe_audio(attachment)
        return await super().analyze(attachment)

    async def _analyze_image(self, attachment: MessageAttachment) -> AttachmentInsight:
        estimate = ESTIMATES["vision"]
        decision = self.budget_gate.check(estimate, automatic=True)
        if not decision.allowed:
            return AttachmentInsight(
                "image",
                f"用户发来了一张图片；为了控制费用，本次没有调用视觉模型（{decision.reason}）。",
                0.25,
            )
        async with self._client(timeout=45) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={
                    "model": self.vision_model,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "你在帮沈知栀理解用户发来的图片。"
                                "用中文输出一句自然、克制的摘要，只描述可见内容和可能的情绪线索，"
                                "不要编造图片外的信息。"
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": "请用不超过80字概括这张图，方便后续聊天自然回应。",
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {"url": attachment.url},
                                },
                            ],
                        },
                    ],
                    "max_completion_tokens": 180,
                },
            )
            response.raise_for_status()
        summary = _chat_completion_text(response.json())
        self.budget_gate.record(estimate, note=f"image:{attachment.filename or attachment.url}")
        return AttachmentInsight("image", f"图片内容：{summary}", 0.82)

    async def _transcribe_audio(self, attachment: MessageAttachment) -> AttachmentInsight:
        estimate = ESTIMATES["transcription"]
        decision = self.budget_gate.check(estimate, automatic=True)
        if not decision.allowed:
            return AttachmentInsight(
                "audio",
                f"用户发来了一段语音；为了控制费用，本次没有调用转写模型（{decision.reason}）。",
                0.25,
            )
        async with self._client(timeout=90) as client:
            audio = await client.get(attachment.url)
            audio.raise_for_status()
            if len(audio.content) > 25 * 1024 * 1024:
                return AttachmentInsight("audio", "用户发来一段语音，但文件超过 25MB，已跳过转写。", 0.3)
            filename = attachment.filename or "voice-message.webm"
            content_type = attachment.content_type or "application/octet-stream"
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                data={"model": self.transcription_model, "response_format": "json"},
                files={"file": (filename, audio.content, content_type)},
            )
            response.raise_for_status()
        data = response.json()
        transcript = str(data.get("text", "")).strip()
        if not transcript:
            transcript = "语音内容转写为空。"
        self.budget_gate.record(estimate, note=f"audio:{attachment.filename or attachment.url}")
        return AttachmentInsight("audio", f"语音转写：{transcript}", 0.85)

    def _client(self, *, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }


def _chat_completion_text(payload: dict[str, Any]) -> str:
    choice = payload.get("choices", [{}])[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [part.get("text", "") for part in content if isinstance(part, dict)]
        return "".join(parts).strip()
    return ""


def _looks_like_text_file(attachment: MessageAttachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    filename = (attachment.filename or "").lower()
    return content_type.startswith("text/") or Path(filename).suffix in {
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".yaml",
        ".yml",
    }
