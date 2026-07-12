from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_reply_model: str | None = Field(default=None, alias="DEEPSEEK_REPLY_MODEL")
    deepseek_thinking_enabled: bool = Field(default=False, alias="DEEPSEEK_THINKING_ENABLED")
    deepseek_reasoning_effort: str = Field(default="high", alias="DEEPSEEK_REASONING_EFFORT")
    deepseek_deep_appraisal_model: str = Field(
        default="deepseek-v4-flash", alias="DEEPSEEK_DEEP_APPRAISAL_MODEL"
    )
    deepseek_deep_appraisal_thinking_enabled: bool = Field(
        default=True, alias="DEEPSEEK_DEEP_APPRAISAL_THINKING_ENABLED"
    )
    deepseek_deep_appraisal_reasoning_effort: str = Field(
        default="high", alias="DEEPSEEK_DEEP_APPRAISAL_REASONING_EFFORT"
    )
    deepseek_repair_model: str = Field(
        default="deepseek-v4-flash", alias="DEEPSEEK_REPAIR_MODEL"
    )
    deepseek_repair_thinking_enabled: bool = Field(
        default=True, alias="DEEPSEEK_REPAIR_THINKING_ENABLED"
    )
    deepseek_repair_reasoning_effort: str = Field(
        default="high", alias="DEEPSEEK_REPAIR_REASONING_EFFORT"
    )
    enable_reply_rewrite: bool = Field(default=False, alias="ENABLE_REPLY_REWRITE")
    enable_reply_decision: bool = Field(default=True, alias="ENABLE_REPLY_DECISION")
    qq_adapter: Literal["official", "napcat", "onebot"] = Field(
        default="official", alias="QQ_ADAPTER"
    )
    wechat_adapter: Literal["disabled", "fake"] = Field(
        default="disabled", alias="WECHAT_ADAPTER"
    )
    napcat_api_url: str = Field(
        default="http://127.0.0.1:3000",
        validation_alias=AliasChoices("NAPCAT_API_URL"),
    )
    napcat_access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("NAPCAT_ACCESS_TOKEN"),
    )
    napcat_allow_group_messages: bool = Field(default=False, alias="NAPCAT_ALLOW_GROUP_MESSAGES")
    napcat_allowed_private_user_ids: str = Field(
        default="",
        alias="NAPCAT_ALLOWED_PRIVATE_USER_IDS",
    )
    napcat_proactive_user_id: str | None = Field(
        default=None,
        alias="NAPCAT_PROACTIVE_USER_ID",
    )
    napcat_accept_unauthenticated_local_events: bool = Field(
        default=True,
        alias="NAPCAT_ACCEPT_UNAUTHENTICATED_LOCAL_EVENTS",
    )
    onebot_api_url: str = Field(
        default="http://127.0.0.1:5700",
        validation_alias=AliasChoices("ONEBOT_API_URL", "SNOWLUMA_API_URL"),
    )
    onebot_access_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ONEBOT_ACCESS_TOKEN", "SNOWLUMA_ACCESS_TOKEN"),
    )
    onebot_proactive_user_id: str | None = Field(
        default=None,
        alias="ONEBOT_PROACTIVE_USER_ID",
    )
    conversation_core: str = Field(default="prompt", alias="CONVERSATION_CORE")
    sillytavern_base_url: str = Field(default="http://127.0.0.1:8000", alias="SILLYTAVERN_BASE_URL")
    database_path: Path = Path("data/companion.sqlite")
    attachment_cache_path: Path = Field(
        default=Path("data/attachments"), alias="ATTACHMENT_CACHE_PATH"
    )
    world_seed_path: Path = Field(default=Path("configs/world_seed.yaml"), alias="WORLD_SEED_PATH")
    character_path: Path = Path("configs/character.yaml")
    stickers_path: Path = Path("configs/stickers.yaml")
    primary_user_id: str = Field(default="geoff", alias="PRIMARY_USER_ID")
    host: str = "127.0.0.1"
    port: int = 8765
    qq_bot_app_id: str | None = Field(default=None, alias="QQ_BOT_APP_ID")
    qq_bot_secret: str | None = Field(default=None, alias="QQ_BOT_SECRET")
    qq_verify_signatures: bool = Field(default=True, alias="QQ_VERIFY_SIGNATURES")
    qq_message_batch_seconds: float = Field(default=2.5, alias="QQ_MESSAGE_BATCH_SECONDS")
    delivery_reconciliation_token: str | None = Field(
        default=None, alias="DELIVERY_RECONCILIATION_TOKEN"
    )
    proactive_interval_seconds: float = Field(default=900, alias="PROACTIVE_INTERVAL_SECONDS")
    proactive_min_cooldown_minutes: int = Field(default=45, alias="PROACTIVE_MIN_COOLDOWN_MINUTES")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    multimodal_provider: str = Field(default="auto", alias="MULTIMODAL_PROVIDER")
    vision_model: str = Field(default="gpt-4o-mini", alias="VISION_MODEL")
    transcription_model: str = Field(default="gpt-4o-mini-transcribe", alias="TRANSCRIPTION_MODEL")
    image_model: str = Field(default="gpt-image-2", alias="IMAGE_MODEL")
    image_backend: Literal["auto", "openai", "comfyui"] = Field(
        default="auto", alias="IMAGE_BACKEND"
    )
    comfyui_base_url: str = Field(default="http://127.0.0.1:8188", alias="COMFYUI_BASE_URL")
    comfyui_workflow_path: Path | None = Field(default=None, alias="COMFYUI_WORKFLOW_PATH")
    comfyui_lora_path: str | None = Field(default=None, alias="COMFYUI_LORA_PATH")
    image_quality_gate_enabled: bool = Field(default=False, alias="IMAGE_QUALITY_GATE_ENABLED")
    visual_identity_path: Path = Field(
        default=Path("configs/visual_identity.yaml"),
        alias="VISUAL_IDENTITY_PATH",
    )
    monthly_budget_cny: float = Field(default=80.0, alias="MONTHLY_BUDGET_CNY")
    daily_budget_cny: float = Field(default=3.0, alias="DAILY_BUDGET_CNY")
    soft_daily_budget_cny: float = Field(default=2.0, alias="SOFT_DAILY_BUDGET_CNY")
    monthly_image_limit: int = Field(default=20, alias="MONTHLY_IMAGE_LIMIT")
    monthly_vision_limit: int = Field(default=120, alias="MONTHLY_VISION_LIMIT")
    monthly_audio_limit: int = Field(default=60, alias="MONTHLY_AUDIO_LIMIT")
    allow_auto_image_generation: bool = Field(default=False, alias="ALLOW_AUTO_IMAGE_GENERATION")
    allow_auto_vision: bool = Field(default=True, alias="ALLOW_AUTO_VISION")
    allow_auto_transcription: bool = Field(default=True, alias="ALLOW_AUTO_TRANSCRIPTION")


@lru_cache
def get_settings() -> Settings:
    return Settings()
