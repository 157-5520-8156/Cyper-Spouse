from pathlib import Path
import logging

import httpx

from companion_daemon.character import load_character
from companion_daemon.config import get_settings
from companion_daemon.conversation import SillyTavernConversationCore
from companion_daemon.budget import BudgetGate
from companion_daemon.attachment_cache import AttachmentCache
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.image_generation import (
    ComfyUIImageGenerator,
    FallbackImageGenerator,
    OpenAIImageQualityGate,
    OpenAIImageGenerator,
)
from companion_daemon.llm import (
    DeepSeekChatModel,
    FakeCompanionModel,
    ModelCallUsage,
    ProviderCircuitBreaker,
)
from companion_daemon.multimodal_analysis import MultimodalAnalyzer, OpenAIMultimodalAnalyzer
from companion_daemon.stickers import load_stickers
from companion_daemon.world import WorldKernel

logger = logging.getLogger(__name__)


def require_configured_model(model: str | None, *, setting: str) -> str:
    """Reject empty configuration without preventing explicitly routed models."""
    normalized = (model or "").strip()
    if not normalized:
        raise ValueError(f"{setting} must name a model")
    return normalized


# Kept as a compatibility import for local integrations.  Model selection is
# now a routing policy; Flash is the default rather than an artificial ban on
# strong/thinking models for high-value non-hot turns.
require_flash_model = require_configured_model


def build_companion_engine(use_fake_model: bool = False) -> CompanionEngine:
    settings = get_settings()
    store = CompanionStore(Path(settings.database_path), primary_user_id=settings.primary_user_id)
    # Production is world-only.  Persist the fail-closed write guard before
    # any startup work so initialization itself cannot seed the legacy mood,
    # calendar, social-task, message, or memory models.
    store.enable_world_mode()
    world_kernel = WorldKernel(store)
    world_id = world_kernel.ensure_seed_file(settings.world_seed_path).world_id
    world_kernel.recover_interrupted_outgoing_deliveries(world_id)
    world_kernel.import_verified_facts(world_id, store.active_fact_lines(settings.primary_user_id))
    character = load_character(str(settings.character_path))
    store.map_account("simulator", "geoff", settings.primary_user_id)
    stickers = load_stickers(str(settings.stickers_path))

    def record_model_usage(usage: ModelCallUsage) -> None:
        store.record_model_usage(
            purpose=usage.purpose,
            model=usage.model,
            status=usage.status,
            latency_ms=usage.latency_ms,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            cache_hit_tokens=usage.cache_hit_tokens,
            cache_miss_tokens=usage.cache_miss_tokens,
            total_tokens=usage.total_tokens,
            error=usage.error,
            world_id=usage.world_id,
            turn_id=usage.turn_id,
            action_id=usage.action_id,
            cadence=usage.cadence,
            attempt=usage.attempt,
        )

    interaction_appraisal_model = None
    interaction_deep_appraisal_model = None
    reply_repair_model = None
    expressive_model = None
    shared_model_client = None
    if settings.deepseek_api_key and not use_fake_model:
        provider_circuit = ProviderCircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=30,
        )
        shared_model_client = httpx.AsyncClient(timeout=45, trust_env=False)
        model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=require_configured_model(settings.deepseek_model, setting="DEEPSEEK_MODEL"),
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
            usage_observer=record_model_usage,
            circuit_breaker=provider_circuit,
            client=shared_model_client,
        )
        interaction_appraisal_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model="deepseek-v4-flash",
            thinking_enabled=False,
            reasoning_effort="high",
            usage_observer=record_model_usage,
            circuit_breaker=provider_circuit,
            client=shared_model_client,
        )
        interaction_deep_appraisal_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_deep_appraisal_model,
            thinking_enabled=settings.deepseek_deep_appraisal_thinking_enabled,
            reasoning_effort=settings.deepseek_deep_appraisal_reasoning_effort,
            usage_observer=record_model_usage,
            circuit_breaker=provider_circuit,
            client=shared_model_client,
        )
        reply_repair_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_repair_model,
            thinking_enabled=settings.deepseek_repair_thinking_enabled,
            reasoning_effort=settings.deepseek_repair_reasoning_effort,
            usage_observer=record_model_usage,
            circuit_breaker=provider_circuit,
            client=shared_model_client,
        )
        expressive_model_name = require_configured_model(
            settings.deepseek_expressive_model or settings.deepseek_model,
            setting="DEEPSEEK_EXPRESSIVE_MODEL",
        )
        if (
            expressive_model_name != settings.deepseek_model
            or settings.deepseek_expressive_thinking_enabled
            != settings.deepseek_thinking_enabled
            or settings.deepseek_expressive_reasoning_effort
            != settings.deepseek_reasoning_effort
        ):
            expressive_model = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=expressive_model_name,
                thinking_enabled=settings.deepseek_expressive_thinking_enabled,
                reasoning_effort=settings.deepseek_expressive_reasoning_effort,
                usage_observer=record_model_usage,
                circuit_breaker=provider_circuit,
                client=shared_model_client,
            )
    else:
        model = FakeCompanionModel()
    conversation_core = None
    if settings.conversation_core.lower() == "sillytavern":
        conversation_core = SillyTavernConversationCore(
            settings.sillytavern_base_url,
            character.system_prompt(),
        )
    logger.info(
        "building companion engine: core=%s reply_decision=%s reply_rewrite=%s fake_model=%s",
        settings.conversation_core,
        settings.enable_reply_decision,
        settings.enable_reply_rewrite,
        use_fake_model,
    )
    budget_gate = BudgetGate(
        store,
        monthly_budget_cny=settings.monthly_budget_cny,
        daily_budget_cny=settings.daily_budget_cny,
        soft_daily_budget_cny=settings.soft_daily_budget_cny,
        monthly_image_limit=settings.monthly_image_limit,
        monthly_vision_limit=settings.monthly_vision_limit,
        monthly_audio_limit=settings.monthly_audio_limit,
    )
    multimodal_analyzer: MultimodalAnalyzer = MultimodalAnalyzer()
    provider = settings.multimodal_provider.lower()
    if settings.openai_api_key and provider in {"auto", "openai"}:
        multimodal_analyzer = OpenAIMultimodalAnalyzer(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            vision_model=settings.vision_model,
            transcription_model=settings.transcription_model,
            budget_gate=budget_gate,
            allow_vision=settings.allow_auto_vision,
            allow_transcription=settings.allow_auto_transcription,
        )
    image_generator = None
    image_quality_gate = None
    if settings.allow_auto_image_generation:
        openai_generator = (
            OpenAIImageGenerator(
                settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.image_model,
            )
            if settings.openai_api_key
            else None
        )
        comfy_generator = (
            ComfyUIImageGenerator(
                base_url=settings.comfyui_base_url,
                workflow_path=settings.comfyui_workflow_path,
                lora_path=settings.comfyui_lora_path,
            )
            if settings.comfyui_workflow_path and settings.comfyui_workflow_path.is_file()
            else None
        )
        if settings.image_backend == "openai":
            image_generator = openai_generator
        elif settings.image_backend == "comfyui":
            image_generator = comfy_generator
        elif comfy_generator and openai_generator:
            image_generator = FallbackImageGenerator(comfy_generator, openai_generator)
        else:
            image_generator = comfy_generator or openai_generator
        if image_generator is None:
            logger.warning(
                "automatic image generation is enabled but no selected backend is configured"
            )
        if settings.image_quality_gate_enabled and settings.openai_api_key:
            image_quality_gate = OpenAIImageQualityGate(
                settings.openai_api_key,
                base_url=settings.openai_base_url,
                model=settings.vision_model,
            )
    rewrite_model = None
    if settings.enable_reply_rewrite and settings.deepseek_api_key and not use_fake_model:
        rewrite_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=require_configured_model(
                settings.deepseek_reply_model or settings.deepseek_model,
                setting="DEEPSEEK_REPLY_MODEL",
            ),
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
            usage_observer=record_model_usage,
            circuit_breaker=provider_circuit,
            client=shared_model_client,
        )
    return CompanionEngine(
        store,
        model,
        character.system_prompt(),
        stickers,
        multimodal_analyzer=multimodal_analyzer,
        conversation_core=conversation_core,
        character_profile=character,
        image_generator=image_generator,
        image_quality_gate=image_quality_gate,
        budget_gate=budget_gate,
        visual_identity_path=settings.visual_identity_path,
        rewrite_model=rewrite_model,
        world_kernel=world_kernel,
        world_id=world_id,
        world_grounding_audit_model=model if world_kernel else None,
        interaction_appraisal_model=interaction_appraisal_model,
        interaction_deep_appraisal_model=interaction_deep_appraisal_model,
        reply_repair_model=reply_repair_model,
        expressive_model=expressive_model,
        attachment_cache=AttachmentCache(settings.attachment_cache_path),
        managed_async_resources=(shared_model_client,) if shared_model_client else (),
    )
