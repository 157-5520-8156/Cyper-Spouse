from functools import lru_cache
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_CIVITAI_KREA2_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "configs" / "civitai-krea2-celia-realism-template.json"
)


def _macos_launchctl_env(name: str) -> str | None:
    """Read a GUI-session variable when the daemon was launched from macOS.

    Desktop applications inherit LaunchServices rather than the interactive
    shell environment.  This fallback makes a user-owned ``ARK_API_KEY``
    available to a local daemon without logging or persisting its value.
    A normal process environment value still wins through Pydantic's alias.
    """

    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["launchctl", "getenv", name],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_reply_model: str | None = Field(default=None, alias="DEEPSEEK_REPLY_MODEL")
    deepseek_expressive_model: str | None = Field(
        default=None, alias="DEEPSEEK_EXPRESSIVE_MODEL"
    )
    deepseek_expressive_thinking_enabled: bool = Field(
        default=False, alias="DEEPSEEK_EXPRESSIVE_THINKING_ENABLED"
    )
    deepseek_expressive_reasoning_effort: str = Field(
        default="high", alias="DEEPSEEK_EXPRESSIVE_REASONING_EFFORT"
    )
    deepseek_thinking_enabled: bool = Field(default=False, alias="DEEPSEEK_THINKING_ENABLED")
    deepseek_reasoning_effort: str = Field(default="high", alias="DEEPSEEK_REASONING_EFFORT")
    deepseek_deep_appraisal_model: str = Field(
        default="deepseek-v4-flash", alias="DEEPSEEK_DEEP_APPRAISAL_MODEL"
    )
    deepseek_deep_appraisal_thinking_enabled: bool = Field(
        # Opt-in after live World-v2 audits showed the high-effort route
        # repeatedly exceeded the interactive deadline. The routed adapter
        # remains installed for deployments with a measured low-latency
        # thinking provider; Flash plus same-turn appraisal is the safe default.
        default=False, alias="DEEPSEEK_DEEP_APPRAISAL_THINKING_ENABLED"
    )
    deepseek_deep_appraisal_reasoning_effort: str = Field(
        default="high", alias="DEEPSEEK_DEEP_APPRAISAL_REASONING_EFFORT"
    )
    local_appraisal_enabled: bool = Field(default=False, alias="LOCAL_APPRAISAL_ENABLED")
    local_appraisal_base_url: str = Field(
        default="http://127.0.0.1:8188/v1", alias="LOCAL_APPRAISAL_BASE_URL"
    )
    local_appraisal_model: str = Field(
        default="mlx-community/Qwen3-1.7B-4bit", alias="LOCAL_APPRAISAL_MODEL"
    )
    local_appraisal_api_key: str = Field(default="local", alias="LOCAL_APPRAISAL_API_KEY")
    world_v2_advisory_timeout_seconds: float = Field(
        default=1.25,
        ge=0.05,
        le=5.0,
        alias="WORLD_V2_ADVISORY_TIMEOUT_SECONDS",
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
    # Every scheduler pass advances the durable clock and performs several
    # ledger commits.  Message replies are ingress-driven and do not wait for
    # this interval; it only bounds background wake latency (proactive,
    # activity lifecycle, recovery), so 30s trades imperceptible staleness for
    # roughly half the background ledger churn of the old 15s default.
    qq_c2c_scheduler_interval_seconds: float = Field(
        default=30.0,
        alias="QQ_C2C_SCHEDULER_INTERVAL_SECONDS",
        gt=0,
    )
    # Where the daemon's dashboard reads the QQ world's read-only life state.
    # The adapter process owns that world's ledger; the daemon only relays.
    qq_c2c_adapter_url: str = Field(
        default="http://127.0.0.1:8787",
        alias="QQ_C2C_ADAPTER_URL",
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
    # HTTP World-v2 is a standalone simulator/room authority.  Keeping its
    # ledger separate prevents an old HTTP fixture from poisoning the QQ
    # archive while preserving that archive for provenance and migration.
    world_v2_http_database_path: Path | None = Field(
        default=None, alias="WORLD_V2_HTTP_DATABASE_PATH"
    )
    attachment_cache_path: Path = Field(
        default=Path("data/attachments"), alias="ATTACHMENT_CACHE_PATH"
    )
    world_seed_path: Path = Field(default=Path("configs/world_seed.yaml"), alias="WORLD_SEED_PATH")
    character_path: Path = Path("configs/character.yaml")
    stickers_path: Path = Path("configs/stickers.yaml")
    primary_user_id: str = Field(default="geoff", alias="PRIMARY_USER_ID")
    local_timezone: str = Field(default="Asia/Shanghai", alias="LOCAL_TIMEZONE")
    host: str = "127.0.0.1"
    port: int = 8765
    qq_bot_app_id: str | None = Field(default=None, alias="QQ_BOT_APP_ID")
    qq_bot_secret: str | None = Field(default=None, alias="QQ_BOT_SECRET")
    qq_verify_signatures: bool = Field(default=True, alias="QQ_VERIFY_SIGNATURES")
    qq_message_batch_seconds: float = Field(default=2.5, alias="QQ_MESSAGE_BATCH_SECONDS")
    qq_turn_observation_path: Path | None = Field(
        default=None, alias="QQ_TURN_OBSERVATION_PATH"
    )
    delivery_reconciliation_token: str | None = Field(
        default=None, alias="DELIVERY_RECONCILIATION_TOKEN"
    )
    proactive_interval_seconds: float = Field(default=900, alias="PROACTIVE_INTERVAL_SECONDS")
    proactive_min_cooldown_minutes: int = Field(default=45, alias="PROACTIVE_MIN_COOLDOWN_MINUTES")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_proxy_url: str | None = Field(default=None, alias="OPENAI_PROXY_URL")
    openrouter_api_key: str | None = Field(
        default_factory=lambda: _macos_launchctl_env("OPENROUTER_API_KEY"),
        alias="OPENROUTER_API_KEY",
    )
    openrouter_base_url: str = Field(
        default="https://openrouter.ai/api/v1", alias="OPENROUTER_BASE_URL"
    )
    hermes_private_prompt_enabled: bool = Field(
        default=True, alias="HERMES_PRIVATE_PROMPT_ENABLED"
    )
    hermes_private_prompt_model: str = Field(
        default="nousresearch/hermes-4-70b", alias="HERMES_PRIVATE_PROMPT_MODEL"
    )
    world_v2_fallback_model: str = Field(
        default="gpt-5.6-luna", alias="WORLD_V2_FALLBACK_MODEL"
    )
    multimodal_provider: str = Field(default="auto", alias="MULTIMODAL_PROVIDER")
    vision_model: str = Field(default="gpt-4o-mini", alias="VISION_MODEL")
    # World v2 QQ perception lane: the deployment's restrained analysis cap.
    # The value is both the perception budget account limit (frozen ledger
    # semantics: one full-limit reservation serializes in-flight analyses)
    # and the decision adapter's durable per-local-day dispatch ceiling.
    # ``0`` disables the lane; enabling additionally requires OPENAI_API_KEY
    # and one run of scripts/provision_world_v2_perception_authority.py.
    # Once a world is bootstrapped, changing this value requires a matching
    # ledger budget account, so treat it as a deployment constant.
    world_v2_perception_budget_limit: int = Field(
        default=12, alias="PERCEPTION_BUDGET_LIMIT", ge=0
    )
    transcription_model: str = Field(default="gpt-4o-mini-transcribe", alias="TRANSCRIPTION_MODEL")
    image_model: str = Field(default="gpt-image-2", alias="IMAGE_MODEL")
    ark_api_key: str | None = Field(
        default_factory=lambda: _macos_launchctl_env("ARK_API_KEY"),
        validation_alias=AliasChoices("ARK_API_KEY", "VOLCENGINE_ARK_API_KEY"),
    )
    ark_base_url: str = Field(
        default="https://ark.cn-beijing.volces.com/api/v3", alias="ARK_BASE_URL"
    )
    ark_image_model: str = Field(
        default="doubao-seedream-4-0-250828", alias="ARK_IMAGE_MODEL"
    )
    ark_image_size: str = Field(default="2K", alias="ARK_IMAGE_SIZE")
    civitai_api_key: str | None = Field(
        default_factory=lambda: _macos_launchctl_env("CIVITAI_API_KEY"),
        alias="CIVITAI_API_KEY",
    )
    civitai_base_url: str = Field(
        default="https://orchestration.civitai.com/v2/consumer",
        alias="CIVITAI_BASE_URL",
    )
    civitai_proxy_url: str | None = Field(
        default_factory=lambda: _macos_launchctl_env("CIVITAI_PROXY_URL"),
        alias="CIVITAI_PROXY_URL",
    )
    civitai_suggestive_image_model: str | None = Field(
        default=None,
        alias="CIVITAI_SUGGESTIVE_IMAGE_MODEL",
    )
    # Generic Civitai imageGen profile used by Krea2 LoRAs.  The raw
    # safetensors file stays local/account-scoped; only resolved AIR resource
    # identifiers are accepted by the cloud renderer.
    civitai_krea2_enabled: bool = Field(default=False, alias="CIVITAI_KREA2_ENABLED")
    civitai_krea2_template_path: Path | None = Field(
        default=DEFAULT_CIVITAI_KREA2_TEMPLATE_PATH,
        alias="CIVITAI_KREA2_TEMPLATE_PATH",
    )
    civitai_krea2_model: str = Field(default="turbo", alias="CIVITAI_KREA2_MODEL")
    civitai_krea2_capability_lora: str | None = Field(
        default=None, alias="CIVITAI_KREA2_CAPABILITY_LORA"
    )
    civitai_krea2_identity_lora: str | None = Field(
        default=None, alias="CIVITAI_KREA2_IDENTITY_LORA"
    )
    civitai_krea2_realism_lora: str | None = Field(
        default=None, alias="CIVITAI_KREA2_REALISM_LORA"
    )
    civitai_krea2_capability_weight: float = Field(
        default=1.0, alias="CIVITAI_KREA2_CAPABILITY_WEIGHT"
    )
    civitai_krea2_identity_weight: float = Field(
        default=1.0, alias="CIVITAI_KREA2_IDENTITY_WEIGHT"
    )
    civitai_krea2_realism_weight: float = Field(
        default=0.1, alias="CIVITAI_KREA2_REALISM_WEIGHT"
    )
    image_backend: Literal["auto", "openai", "comfyui", "ark"] = Field(
        default="auto", alias="IMAGE_BACKEND"
    )
    comfyui_base_url: str = Field(default="http://127.0.0.1:8188", alias="COMFYUI_BASE_URL")
    comfyui_workflow_path: Path | None = Field(default=None, alias="COMFYUI_WORKFLOW_PATH")
    comfyui_lora_path: str | None = Field(default=None, alias="COMFYUI_LORA_PATH")
    image_quality_gate_enabled: bool = Field(default=True, alias="IMAGE_QUALITY_GATE_ENABLED")
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
    # World v2 media preview lane (preview-only + operator approval).  It is
    # deliberately opt-in: the composition also requires DeepSeek + OpenAI
    # credentials and a provisioned media enforcement grant chain before any
    # provider Action can dispatch, and disables itself with one log line
    # when a prerequisite is missing.
    world_v2_media_preview_enabled: bool = Field(
        default=False, alias="WORLD_V2_MEDIA_PREVIEW_ENABLED"
    )
    # Planner model for the image machine's one bounded planning call.
    # Defaults to the flash chat model when unset.
    world_v2_media_planner_model: str | None = Field(
        default=None, alias="WORLD_V2_MEDIA_PLANNER_MODEL"
    )
    # Visual acceptance model for the media lane (at most a few calls per
    # day).  This is deliberately separate from the chat VISION_MODEL: the
    # v7 inspection contract needs the stronger model, and the smaller one
    # was observed to fail-close most honest life shots.
    world_v2_media_inspection_model: str = Field(
        default="gpt-4o", alias="WORLD_V2_MEDIA_INSPECTION_MODEL"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
