from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_reply_model: str | None = Field(default=None, alias="DEEPSEEK_REPLY_MODEL")
    enable_reply_rewrite: bool = Field(default=False, alias="ENABLE_REPLY_REWRITE")
    enable_reply_decision: bool = Field(default=True, alias="ENABLE_REPLY_DECISION")
    qq_adapter: str = Field(default="official", alias="QQ_ADAPTER")
    snowluma_api_url: str = Field(default="http://127.0.0.1:5700", alias="SNOWLUMA_API_URL")
    snowluma_access_token: str | None = Field(default=None, alias="SNOWLUMA_ACCESS_TOKEN")
    conversation_core: str = Field(default="prompt", alias="CONVERSATION_CORE")
    sillytavern_base_url: str = Field(default="http://127.0.0.1:8000", alias="SILLYTAVERN_BASE_URL")
    database_path: Path = Path("data/companion.sqlite")
    character_path: Path = Path("configs/character.yaml")
    stickers_path: Path = Path("configs/stickers.yaml")
    primary_user_id: str = Field(default="geoff", alias="PRIMARY_USER_ID")
    host: str = "127.0.0.1"
    port: int = 8765
    qq_bot_app_id: str | None = Field(default=None, alias="QQ_BOT_APP_ID")
    qq_bot_secret: str | None = Field(default=None, alias="QQ_BOT_SECRET")
    qq_verify_signatures: bool = Field(default=True, alias="QQ_VERIFY_SIGNATURES")
    qq_message_batch_seconds: float = Field(default=2.5, alias="QQ_MESSAGE_BATCH_SECONDS")
    proactive_interval_seconds: float = Field(default=900, alias="PROACTIVE_INTERVAL_SECONDS")
    proactive_min_cooldown_minutes: int = Field(default=45, alias="PROACTIVE_MIN_COOLDOWN_MINUTES")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    multimodal_provider: str = Field(default="auto", alias="MULTIMODAL_PROVIDER")
    vision_model: str = Field(default="gpt-4o-mini", alias="VISION_MODEL")
    transcription_model: str = Field(default="gpt-4o-mini-transcribe", alias="TRANSCRIPTION_MODEL")
    image_model: str = Field(default="gpt-image-2", alias="IMAGE_MODEL")
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
