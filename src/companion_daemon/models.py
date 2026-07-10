from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from companion_daemon.time import utc_now

Platform = Literal["qq", "wechat", "simulator"]
AttachmentKind = Literal["image", "audio", "video", "file", "unknown"]
PhoneAttention = Literal["away", "notified", "glanced", "reading", "typing", "do_not_disturb"]
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
    text_parts: list[str] = Field(default_factory=list)
    platform_context: str | None = None
    sticker_path: str | None = None
    image_path: str | None = None
    suggested_reaction: str | None = None
    delivery_id: int | None = None


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
    delivery_id: int | None = None
    social_task_id: int | None = None


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
    perceived_respect: int = 50
    perceived_reliability: int = 50
    perceived_responsiveness: int = 50
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
    has_unread: bool = False
    updated_at: datetime = Field(default_factory=utc_now)


class LifeRuntimeState(BaseModel):
    """Private, advancing life context; never shown verbatim to the user."""

    activity: str = "在自己的日常里"
    activity_kind: str = "between"
    base_attention_demand: int = 35
    attention_demand: int = 35
    interruptible: bool = True
    started_at: datetime = Field(default_factory=utc_now)
    ends_at: datetime = Field(default_factory=utc_now)
    phone_attention: PhoneAttention = "away"
    notification_count: int = 0
    last_notification_at: datetime | None = None
    last_read_at: datetime | None = None
    user_event_effect: str | None = None
    user_event_effect_until: datetime | None = None
    user_event_attention_delta: int = 0
    state_effect: str | None = None
    updated_at: datetime = Field(default_factory=utc_now)
