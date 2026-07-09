from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from companion_daemon.time import utc_now

Platform = Literal["qq", "wechat", "simulator"]
AttachmentKind = Literal["image", "audio", "video", "file", "unknown"]
RelationshipStage = Literal["stranger", "acquaintance", "friend", "close_friend", "ambiguous", "lover"]
Mood = Literal[
    "calm",
    "happy",
    "sulking",
    "miss_you",
    "worried",
    "jealous_soft",
    "sleepy",
    "guarded",
    "hurt",
    "affectionate",
    "curious",
]


class MessageAttachment(BaseModel):
    kind: AttachmentKind = "unknown"
    url: str | None = None
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    width: int | None = None
    height: int | None = None


class IncomingMessage(BaseModel):
    platform: Platform
    platform_user_id: str
    text: str
    channel_id: str | None = None
    message_id: str | None = None
    attachments: list[MessageAttachment] = Field(default_factory=list)
    sent_at: datetime = Field(default_factory=utc_now)


class CompanionReply(BaseModel):
    canonical_user_id: str
    mood: Mood
    text: str
    platform_context: str | None = None
    sticker_path: str | None = None
    image_path: str | None = None
    suggested_reaction: str | None = None


class ProactiveDecision(BaseModel):
    canonical_user_id: str
    private_thought: str
    should_send: bool
    platform: Platform | None = None
    message_type: Literal["none", "text", "sticker", "text_sticker", "image", "text_image"] = "none"
    message: str | None = None
    sticker_category: str | None = None
    sticker_path: str | None = None
    image_path: str | None = None
    trigger_type: str | None = None
    cooldown_minutes: int = 30


class MoodState(BaseModel):
    mood: Mood = "calm"
    intimacy: int = 5
    trust: int = 15
    attachment: int = 0
    patience: int = 70
    security: int = 45
    curiosity: int = 40
    initiative: int = 20
    emotional_charge: int = 0
    boundary_level: int = 0
    relationship_stage: RelationshipStage = "stranger"
    unresolved_emotion: str | None = None
    last_user_intent: str | None = None
    last_interaction_event: str | None = None
    reply_style_hint: str | None = None
    emotion_vector: dict[str, float] = Field(default_factory=dict)
    emotion_baseline: dict[str, float] = Field(default_factory=dict)
    emotion_affinity: dict[str, float] = Field(default_factory=dict)
    last_emotion_impact: dict[str, float] = Field(default_factory=dict)
    last_emotion_source: str | None = None
    last_platform: Platform | None = None
    updated_at: datetime = Field(default_factory=utc_now)
