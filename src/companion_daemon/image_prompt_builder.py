import re
from dataclasses import dataclass
from pathlib import Path

from companion_daemon.character import CharacterProfile
from companion_daemon.image_requests import detect_image_request, detect_style_tags
from companion_daemon.visual_identity import VisualIdentity, load_visual_identity


@dataclass(frozen=True)
class ChatImageMessage:
    text: str
    is_user: bool


@dataclass(frozen=True)
class ImagePromptPayload:
    prompt: str
    mode: str
    directive: str
    style_tags: str
    used_context: bool = False


def build_image_prompt(
    user_message: str,
    *,
    character: CharacterProfile,
    recent_messages: list[ChatImageMessage] | None = None,
    visual_identity_path: Path | None = Path("configs/visual_identity.yaml"),
) -> ImagePromptPayload:
    recent_messages = recent_messages or []
    request = detect_image_request(
        user_message,
        [message.text for message in recent_messages if not message.is_user],
    )
    directive = request.directive or _strip_image_trigger_words(user_message)
    context_directive = _resolve_context_directive(user_message, recent_messages)
    used_context = False
    if context_directive and _needs_context(user_message, directive):
        directive = context_directive
        used_context = True
    directive = directive or "natural casual selfie"
    style_tags = request.style_tags or detect_style_tags(user_message)
    mode = _infer_mode(user_message, directive)
    prompt = _compose_prompt(
        mode=mode,
        directive=directive,
        style_tags=style_tags,
        character=character,
        visual_identity=_load_optional_identity(visual_identity_path),
    )
    return ImagePromptPayload(
        prompt=prompt,
        mode=mode,
        directive=directive,
        style_tags=style_tags,
        used_context=used_context,
    )


def _compose_prompt(
    *,
    mode: str,
    directive: str,
    style_tags: str,
    character: CharacterProfile,
    visual_identity: VisualIdentity | None,
) -> str:
    identity_block = visual_identity.prompt_block() if visual_identity else ""
    character_visual = ", ".join(
        part.strip()
        for part in [character.appearance, character.background]
        if part and part.strip()
    )
    if mode == "character":
        return (
            f"Create an original fictional image featuring {character.name} / Celia Shen. "
            "Keep her identity consistent across images. "
            f"Character visual notes: {character_visual}. "
            f"User directive: {directive}. Style: {style_tags}. "
            "No text, no watermark, no real public figure likeness.\n"
            f"{identity_block}"
        ).strip()
    if mode == "creative":
        return (
            "Create an original illustration or stylized image requested in private chat. "
            f"Subject/directive: {directive}. Style: {style_tags}. "
            "No text, no watermark, avoid copying existing copyrighted characters."
        )
    return (
        "Create an original phone-photo style life image that could naturally be shared in private chat. "
        f"Subject/directive: {directive}. Style: {style_tags}. "
        "Natural lighting, no text, no watermark."
    )


def _infer_mode(user_message: str, directive: str) -> str:
    text = f"{user_message} {directive}".lower()
    if re.search(r"(漫画|动漫|二次元|画|生成|头像|表情包|comic|anime|manga|sketch|illustration)", text):
        if re.search(r"(自拍|你|沈知栀|celia|本人|今天穿|穿什么)", text):
            return "character"
        return "creative"
    if re.search(r"(自拍|你|沈知栀|celia|本人|今天穿|穿什么|生活照)", text):
        return "character"
    return "object"


def _needs_context(user_message: str, directive: str) -> bool:
    text = user_message.lower()
    if re.search(r"(刚刚|刚才|那个|这个|这张|那张|你说的|你提到的|that|this|it)", text):
        return True
    return len(directive.strip()) < 4


def _resolve_context_directive(
    user_message: str,
    recent_messages: list[ChatImageMessage],
) -> str | None:
    if not _needs_context(user_message, _strip_image_trigger_words(user_message)):
        return None
    for message in reversed(recent_messages[-8:]):
        if message.is_user:
            continue
        candidate = _visual_sentence(message.text)
        if candidate:
            return candidate
    return None


def _visual_sentence(text: str) -> str | None:
    sentences = [segment.strip() for segment in re.split(r"[。！？!?；;\n]", text) if segment.strip()]
    visual_tokens = [
        "穿",
        "衣",
        "裙",
        "外套",
        "发夹",
        "照片",
        "拍",
        "图书馆",
        "路灯",
        "梧桐",
        "咖啡",
        "颜色",
        "自拍",
    ]
    for sentence in reversed(sentences):
        if any(token in sentence for token in visual_tokens):
            return sentence[:120]
    return None


def _strip_image_trigger_words(text: str) -> str:
    cleaned = re.sub(
        r"(能不能|可以不可以|可以|麻烦|请|发|给我|给|拍|来|看看|想看|画|生成|做|一张|一个|图片|照片|自拍|图|表情包)",
        "",
        text,
    )
    return re.sub(r"[？?！!。,.，]", "", cleaned).strip()[:120]


def _load_optional_identity(path: Path | None) -> VisualIdentity | None:
    if not path or not path.exists():
        return None
    return load_visual_identity(str(path))
