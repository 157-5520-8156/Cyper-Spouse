from companion_daemon.models import MessageAttachment


def attachment_kind(content_type: str | None, filename: str | None = None) -> str:
    content_type = (content_type or "").lower()
    filename = (filename or "").lower()
    if content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return "image"
    if content_type.startswith("audio/") or filename.endswith((".mp3", ".wav", ".flac", ".silk")):
        return "audio"
    if content_type.startswith("video/") or filename.endswith((".mp4", ".mov")):
        return "video"
    if filename or content_type:
        return "file"
    return "unknown"


def summarize_attachments(attachments: list[MessageAttachment]) -> list[str]:
    lines: list[str] = []
    for index, attachment in enumerate(attachments, start=1):
        name = attachment.filename or "未命名"
        detail = attachment.content_type or attachment.kind
        size = f", {attachment.size} bytes" if attachment.size else ""
        shape = (
            f", {attachment.width}x{attachment.height}"
            if attachment.width and attachment.height
            else ""
        )
        lines.append(f"{index}. {attachment.kind}: {name} ({detail}{size}{shape})")
    return lines


def attachment_memory_text(attachment: MessageAttachment) -> str:
    base = attachment.filename or attachment.content_type or attachment.kind
    if attachment.kind == "image":
        return f"用户发过图片：{base}"
    if attachment.kind == "audio":
        return f"用户发过语音：{base}"
    if attachment.kind == "file":
        return f"用户发过文件：{base}"
    return f"用户发过附件：{base}"
