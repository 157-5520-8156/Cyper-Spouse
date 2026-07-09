import re
from dataclasses import dataclass

from companion_daemon.image_requests import ImageRequest
from companion_daemon.models import MoodState


@dataclass(frozen=True)
class ImageAgencyDecision:
    allow_generation: bool
    kind: str
    reason: str
    prompt_hint: str


SELFIE_PATTERNS = [
    r"(自拍|生活照|随手拍|照片)",
    r"(看看你|想看你|你长什么样|你现在穿什么|今天穿什么)",
]

PUSHY_PATTERNS = [
    r"(必须|立刻|马上|现在就|快点|不发|证明|听话)",
]


def decide_image_agency(
    request: ImageRequest,
    state: MoodState,
    user_message: str,
) -> ImageAgencyDecision:
    if not request.triggered:
        return ImageAgencyDecision(False, "none", "no_image_request", "本轮没有图片请求。")

    wants_selfie = _is_selfie_like(request, user_message)
    if not wants_selfie:
        return ImageAgencyDecision(True, "creative_image", "non_selfie_request", "这不是索要她本人照片，可以正常处理图片请求。")

    if _is_pushy(user_message):
        return ImageAgencyDecision(
            False,
            "selfie_refused",
            "pushy_selfie_request",
            "用户索要自拍的语气有压迫感；沈知栀应该守住边界，可以短句拒绝或转移话题，不要生成图片。",
        )

    if state.relationship_stage in {"stranger", "acquaintance"} and (state.trust < 35 or state.intimacy < 18):
        return ImageAgencyDecision(
            False,
            "selfie_deferred",
            "early_relationship",
            "你们还不够熟；沈知栀不要立刻发自拍，可以自然地说以后熟一点再给你看。",
        )

    if state.mood in {"guarded", "hurt", "sulking", "jealous_soft"} or state.boundary_level >= 35:
        return ImageAgencyDecision(
            False,
            "selfie_deferred",
            "bad_mood_or_boundary",
            "她现在心情不适合被索要自拍；回复可以有一点小脾气或委屈，但不要生成图片。",
        )

    if state.security < 30 and state.trust < 50:
        return ImageAgencyDecision(
            False,
            "selfie_deferred",
            "low_security",
            "她安全感偏低，不想马上发自己的照片；可以轻轻拒绝，并把话题带回聊天本身。",
        )

    return ImageAgencyDecision(
        True,
        "selfie_allowed",
        "relationship_allows_selfie",
        "关系和情绪允许她考虑发一张自然的虚拟生活自拍；语气仍要像她自愿分享，而不是执行命令。",
    )


def image_agency_prompt_line(decision: ImageAgencyDecision) -> str:
    return f"图片边界: {decision.prompt_hint} 决策={decision.kind}；原因={decision.reason}。"


def _is_selfie_like(request: ImageRequest, user_message: str) -> bool:
    text = f"{user_message} {request.directive or ''}".lower()
    if request.type == "offer_response" and re.search(r"(自拍|生活照|随手拍|照片|看看你)", text):
        return True
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in SELFIE_PATTERNS)


def _is_pushy(user_message: str) -> bool:
    return any(re.search(pattern, user_message, re.IGNORECASE) for pattern in PUSHY_PATTERNS)
