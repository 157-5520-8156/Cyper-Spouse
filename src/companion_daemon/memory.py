import re
from dataclasses import dataclass

from companion_daemon.models import IncomingMessage, MessageAttachment


@dataclass(frozen=True)
class ExtractedMemory:
    kind: str
    content: str
    confidence: float = 0.7


def extract_memories(message: IncomingMessage) -> list[ExtractedMemory]:
    text = message.text.strip()
    memories: list[ExtractedMemory] = []

    patterns = [
        ("name", r"(?:我叫|叫我)([^，。！？\n]{1,16})"),
        ("preference", r"我(?:喜欢|爱)([^，。！？\n]{1,32})"),
        ("dislike", r"我(?:不喜欢|讨厌)([^，。！？\n]{1,32})"),
        ("status", r"我是([^，。！？\n]{1,24})"),
        ("schedule", r"我(?:明天|后天|今晚|今天|周末)([^，。！？\n]{1,40})"),
    ]
    for kind, pattern in patterns:
        for match in re.finditer(pattern, text):
            value = match.group(1).strip()
            if value:
                memories.append(ExtractedMemory(kind=kind, content=value))

    for attachment in message.attachments:
        summary = memory_from_attachment(attachment)
        if summary:
            memories.append(summary)

    return memories


def memory_from_attachment(attachment: MessageAttachment) -> ExtractedMemory | None:
    if attachment.kind == "image":
        detail = attachment.filename or attachment.content_type or "一张图片"
        return ExtractedMemory("shared_image", f"用户发过图片：{detail}", confidence=0.4)
    if attachment.kind == "audio":
        detail = attachment.filename or attachment.content_type or "一段语音"
        return ExtractedMemory("shared_audio", f"用户发过语音：{detail}", confidence=0.4)
    if attachment.kind == "file":
        detail = attachment.filename or attachment.content_type or "一个文件"
        return ExtractedMemory("shared_file", f"用户发过文件：{detail}", confidence=0.4)
    return None


def memory_lines(rows) -> list[str]:
    return [f"- [{row['kind']}] {row['content']}" for row in rows]
