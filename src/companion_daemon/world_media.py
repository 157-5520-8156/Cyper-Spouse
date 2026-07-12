"""World-authorized media decisions, independent of the retired MoodState."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from companion_daemon.image_requests import ImageRequest


MediaKind = Literal["none", "creative_image", "selfie", "relationship_private"]
IntimacyTier = Literal["soft", "tender", "bold"]


@dataclass(frozen=True)
class WorldMediaDecision:
    allowed: bool
    kind: MediaKind
    reason: str
    prompt_topic: str = ""
    requires_deliberation: bool = False
    intimacy_tier: IntimacyTier | None = None


class WorldMediaPolicy:
    """One pure seam for image and sticker authorization rules."""

    RULE_VERSION = "world-media-v3"
    _SELFIE_MARKERS = ("自拍", "生活照", "随手拍", "照片", "看看你", "你长什么样", "你现在穿什么", "今天穿什么")
    _PRESSURE_MARKERS = ("必须", "立刻", "马上", "现在就", "快点", "不发", "证明", "听话")
    _PRIVATE_MARKERS = ("私密", "亲密", "暧昧", "睡前")

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
        selfie = self._is_selfie_request(request, user_text)
        relationship_private = selfie and self._is_private_request(user_text)
        media_kind: MediaKind = "relationship_private" if relationship_private else "selfie"
        intimacy_tier = self._requested_intimacy_tier(user_text) if relationship_private else None
        topic = str(request.directive or user_text).strip()[:120]
        if relationship_private and relationship_stage != "lover":
            return WorldMediaDecision(False, media_kind, "relationship_stage_not_intimate")
        if relationship_private:
            tier_gate = self._relationship_tier_gate(
                intimacy_tier, relation=relation, needs=needs, affect=affect
            )
            if tier_gate:
                return WorldMediaDecision(False, media_kind, tier_gate, intimacy_tier=intimacy_tier)
        if selfie and bool(affect.get("unresolved")) and int(affect_vector.get("hurt", 0)) >= 20:
            return WorldMediaDecision(
                True, media_kind, "unresolved_negative_affect", topic,
                requires_deliberation=True, intimacy_tier=intimacy_tier,
            )
        if selfie and boundary >= 65 and self._is_pressure(user_text):
            return WorldMediaDecision(False, media_kind, "boundary_high_under_pressure")
        if selfie and relationship_stage in {"stranger", "acquaintance", "friend"}:
            return WorldMediaDecision(False, media_kind, "relationship_stage_not_ready")
        if selfie and (int(relation.get("respect", 0)) < -15 or int(relation.get("closeness", 0)) < 4):
            return WorldMediaDecision(False, media_kind, "relationship_not_ready")
        if selfie and (boundary >= 45 or security <= 28):
            return WorldMediaDecision(False, media_kind, "boundary_or_security_not_ready")
        if selfie:
            return WorldMediaDecision(
                True, media_kind, "world_relationship_allows_selfie", topic,
                intimacy_tier=intimacy_tier,
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

    def _is_selfie_request(self, request: ImageRequest, user_text: str) -> bool:
        text = f"{user_text} {request.directive or ''}".lower()
        return request.type == "offer_response" or any(marker in text for marker in self._SELFIE_MARKERS)

    def _is_pressure(self, text: str) -> bool:
        return any(marker in text for marker in self._PRESSURE_MARKERS)

    def _is_private_request(self, text: str) -> bool:
        return any(marker in text for marker in self._PRIVATE_MARKERS)

    def _requested_intimacy_tier(self, text: str) -> IntimacyTier:
        normalized = text.lower()
        if any(marker in normalized for marker in ("bold", "大胆", "更大胆", "浓一点")):
            return "bold"
        if any(marker in normalized for marker in ("tender", "温柔", "更亲密", "靠近一点")):
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
