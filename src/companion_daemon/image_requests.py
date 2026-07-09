import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageRequest:
    triggered: bool
    type: str = "none"
    confidence: float = 0.0
    directive: str | None = None
    style_tags: str | None = None


AFFIRMATIVE_PATTERNS = [
    r"^(好呀?|可以|要|想看|发吧|来|发我|给我看|当然|嗯嗯|行)",
    r"(想看|给我看看|发出来|可以发|看看你的)",
]

OFFER_VERB_PATTERNS = [
    r"(要不要看|想不想看|我可以发|给你发|拍给你|发给你|给你看看)",
]

OFFER_MEDIA_PATTERNS = [
    r"(自拍|照片|图片|图|表情包|生活照|随手拍)",
]

DIRECT_REQUEST_PATTERNS = [
    r"(发|给|拍|来|看看|想看|能不能|可以不可以).{0,20}(自拍|照片|图片|图|生活照|随手拍|表情包)",
    r"(自拍|照片|图片|图|生活照|随手拍|表情包).{0,12}(发|给|看看|来一张)",
    r"(你长什么样|想看看你|看看你现在|你现在穿什么|今天穿什么)",
    r"(画|生成|做).{0,20}(图片|头像|表情包|照片|自拍)",
]


def detect_image_request(
    user_message: str,
    recent_assistant_messages: list[str] | None = None,
) -> ImageRequest:
    text = user_message.strip()
    if not text:
        return ImageRequest(False)
    normalized = text.lower()
    recent_assistant_messages = recent_assistant_messages or []

    is_affirmative = any(re.search(pattern, normalized, re.IGNORECASE) for pattern in AFFIRMATIVE_PATTERNS)
    has_recent_offer = any(
        any(re.search(verb, message, re.IGNORECASE) for verb in OFFER_VERB_PATTERNS)
        and any(re.search(media, message, re.IGNORECASE) for media in OFFER_MEDIA_PATTERNS)
        for message in recent_assistant_messages[-3:]
    )
    if is_affirmative and has_recent_offer:
        return ImageRequest(
            True,
            "offer_response",
            0.95,
            directive="回应最近的图片/自拍邀约",
            style_tags=detect_style_tags(text),
        )

    for pattern in DIRECT_REQUEST_PATTERNS:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            return ImageRequest(
                True,
                "direct_request",
                0.9,
                directive=_extract_directive(text),
                style_tags=detect_style_tags(text),
            )

    return ImageRequest(False)


def _extract_directive(text: str) -> str:
    directive = re.sub(r"(能不能|可以不可以|可以|能|麻烦|请|发|给我|给|拍|来|看看|想看)", "", text)
    directive = re.sub(r"[？?！!。,.，]", "", directive).strip()
    return directive[:80] or text[:80]


def detect_style_tags(user_message: str) -> str:
    text = user_message.lower()
    if re.search(r"(漫画|comic|美漫)", text):
        return "comic book style, bold ink lines, vivid colors, dynamic composition"
    if re.search(r"(动漫|二次元|anime|manga)", text):
        return "anime style, clean cel shading, expressive but consistent character design"
    if re.search(r"(Q版|q版|chibi)", text):
        return "chibi style, cute rounded proportions, colorful"
    if re.search(r"(水彩|watercolou?r)", text):
        return "watercolor painting, soft edges, flowing colors"
    if re.search(r"(油画|oil[ -]?paint)", text):
        return "oil painting, rich color, textured brushwork"
    if re.search(r"(像素|pixel[ -]?art)", text):
        return "pixel art, crisp pixels, retro style"
    if re.search(r"(写实|真实|照片感|realistic|photorealistic)", text):
        return "natural phone photo, realistic lighting, natural skin texture"
    if re.search(r"(速写|素描|sketch)", text):
        return "pencil sketch, detailed linework"
    return "natural phone photo, consistent virtual character identity"
