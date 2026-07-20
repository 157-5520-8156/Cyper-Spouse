"""World-authorized media decisions, independent of the retired MoodState."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from companion_daemon.image_requests import ImageRequest


MediaKind = Literal["none", "creative_image", "selfie", "character_media", "relationship_private"]
IntimacyTier = Literal["soft", "tender", "bold"]
CaptureMode = Literal[
    "handheld_selfie",
    "check_in_timer",
    "check_in_helper",
    "mirror",
    "candid_life",
    "unfiltered",
]

_UNFILTERED_EVENT_MARKERS = ("刚跑完", "跑完步", "淋雨", "熬夜", "刚醒", "风吹", "赶路")


@dataclass(frozen=True)
class WorldMediaDecision:
    allowed: bool
    kind: MediaKind
    reason: str
    prompt_topic: str = ""
    requires_deliberation: bool = False
    intimacy_tier: IntimacyTier | None = None
    capture_mode: CaptureMode | None = None


class WorldMediaPolicy:
    """One pure seam for image and sticker authorization rules."""

    RULE_VERSION = "world-media-v4"
    _PERSONAL_MEDIA_MARKERS = (
        "自拍", "生活照", "随手拍", "照片", "看看你", "你长什么样", "你现在穿什么", "今天穿什么",
        "打卡", "穿搭", "全身", "镜子", "他拍", "抓拍", "丑照", "狼狈", "素颜", "搞怪",
    )
    _PRESSURE_MARKERS = ("必须", "立刻", "马上", "现在就", "快点", "不发", "证明", "听话")
    _PRIVATE_MARKERS = ("私密", "亲密", "暧昧")

    def image_decision(
        self,
        state: dict[str, Any],
        *,
        user_id: str,
        request: ImageRequest,
        user_text: str,
    ) -> WorldMediaDecision:
        if not request.triggered:
            return WorldMediaDecision(False, "none", "no_media_request")
        needs = _mapping(state.get("needs"))
        relation = _mapping(_mapping(state.get("relationships")).get(user_id))
        relationship_stage = str(relation.get("stage") or "stranger")
        boundary = int(needs.get("boundary", 0))
        security = int(needs.get("security", 50))
        affect = _mapping(state.get("emotion_modulation"))
        affect_vector = _mapping(affect.get("vector"))
        personal_media = self._is_personal_media_request(request, user_text)
        capture_mode = self._capture_mode(state, user_text) if personal_media else None
        relationship_private = personal_media and self._is_private_request(user_text)
        media_kind: MediaKind = (
            "relationship_private" if relationship_private else "character_media"
        )
        intimacy_tier = self._requested_intimacy_tier(user_text) if relationship_private else None
        topic = str(request.directive or user_text).strip()[:120]
        if relationship_private and relationship_stage != "lover":
            return WorldMediaDecision(False, media_kind, "relationship_stage_not_intimate")
        if relationship_private:
            tier_gate = self._relationship_tier_gate(
                intimacy_tier, relation=relation, needs=needs, affect=affect
            )
            if tier_gate:
                return WorldMediaDecision(
                    False,
                    media_kind,
                    tier_gate,
                    intimacy_tier=intimacy_tier,
                    capture_mode=capture_mode,
                )
        if personal_media and bool(affect.get("unresolved")) and int(affect_vector.get("hurt", 0)) >= 20:
            return WorldMediaDecision(
                True, media_kind, "unresolved_negative_affect", topic,
                requires_deliberation=True, intimacy_tier=intimacy_tier, capture_mode=capture_mode,
            )
        if personal_media and boundary >= 65 and self._is_pressure(user_text):
            return WorldMediaDecision(False, media_kind, "boundary_high_under_pressure")
        if personal_media and relationship_stage in {"stranger", "acquaintance", "friend"}:
            return WorldMediaDecision(False, media_kind, "relationship_stage_not_ready")
        if personal_media and (int(relation.get("respect", 0)) < -15 or int(relation.get("closeness", 0)) < 4):
            return WorldMediaDecision(False, media_kind, "relationship_not_ready")
        if personal_media and (boundary >= 45 or security <= 28):
            return WorldMediaDecision(False, media_kind, "boundary_or_security_not_ready")
        if personal_media:
            return WorldMediaDecision(
                True, media_kind, "world_relationship_allows_personal_media", topic,
                intimacy_tier=intimacy_tier, capture_mode=capture_mode,
            )
        if boundary >= 80 and self._is_pressure(user_text):
            return WorldMediaDecision(False, "creative_image", "boundary_high_under_pressure")
        return WorldMediaDecision(True, "creative_image", "user_requested_creative_image", topic)

    def sticker_intent(self, state: dict[str, Any], *, appraisal: str) -> str | None:
        """Return a display intent only; sticker delivery is still an Action."""
        modulation = _mapping(state.get("emotion_modulation"))
        mode = str(modulation.get("mode") or "calm")
        behavior_tendency = str(modulation.get("behavior_tendency") or "neutral")
        if appraisal == "user_vulnerable":
            return "comfort"
        if behavior_tendency == "withdraw":
            return "boundary"
        if mode == "guarded" or int(_mapping(state.get("needs")).get("boundary", 0)) >= 55:
            return "boundary"
        if mode in {"warm", "open", "softening"}:
            return "greeting"
        return None

    @staticmethod
    def proactive_capture_mode(topic: str) -> CaptureMode:
        """Reserve unfiltered sharing for a concrete, low-stakes life event."""
        normalized = topic.lower()
        if any(marker in normalized for marker in _UNFILTERED_EVENT_MARKERS):
            return "unfiltered"
        return "handheld_selfie"

    def _is_personal_media_request(self, request: ImageRequest, user_text: str) -> bool:
        text = f"{user_text} {request.directive or ''}".lower()
        return request.type == "offer_response" or any(marker in text for marker in self._PERSONAL_MEDIA_MARKERS)

    def _capture_mode(self, state: dict[str, Any], text: str) -> CaptureMode:
        normalized = text.lower()
        if any(marker in normalized for marker in ("丑照", "狼狈", "素颜", "搞怪", "刚跑完", "刚醒")):
            return "unfiltered"
        if "镜子" in normalized:
            return "mirror"
        if any(marker in normalized for marker in ("抓拍", "他拍", "室友拍", "朋友拍")):
            if _has_world_capture_evidence(state):
                return "candid_life"
            return "check_in_helper"
        if any(marker in normalized for marker in ("打卡", "到啦", "穿搭", "全身", "到此一游")):
            return "check_in_helper"
        return "handheld_selfie"

    def _is_pressure(self, text: str) -> bool:
        return any(marker in text for marker in self._PRESSURE_MARKERS)

    def _is_private_request(self, text: str) -> bool:
        return any(marker in text for marker in self._PRIVATE_MARKERS)

    def _requested_intimacy_tier(self, text: str) -> IntimacyTier:
        normalized = text.lower()
        if any(marker in normalized for marker in ("bold", "大胆", "更大胆", "浓一点")):
            return "bold"
        if any(marker in normalized for marker in ("tender", "更亲密", "靠近一点")):
            return "tender"
        return "soft"

    def _relationship_tier_gate(
        self,
        tier: IntimacyTier,
        *,
        relation: dict[str, Any],
        needs: dict[str, Any],
        affect: dict[str, Any],
    ) -> str | None:
        if tier == "soft":
            return None
        closeness = int(relation.get("closeness", 0))
        respect = int(relation.get("respect", 0))
        security = int(needs.get("security", 50))
        boundary = int(needs.get("boundary", 0))
        unresolved = bool(affect.get("unresolved"))
        if tier == "tender":
            if closeness < 12 or respect < 5 or security < 45 or boundary >= 35:
                return "relationship_tier_tender_not_ready"
            return None
        if closeness < 20 or respect < 12 or security < 60 or boundary > 20 or unresolved:
            return "relationship_tier_bold_not_ready"
        return None


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _has_world_capture_evidence(state: dict[str, Any]) -> bool:
    """Only permit a third-person viewpoint when the world records company."""
    companions = state.get("current_companions")
    if companions:
        return True
    agenda = state.get("agenda")
    if not isinstance(agenda, dict):
        return False
    return any(
        isinstance(item, dict)
        and item.get("status") == "active"
        and bool(item.get("companions"))
        for item in agenda.values()
    )
