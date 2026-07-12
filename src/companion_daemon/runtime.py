from pathlib import Path
import logging

from companion_daemon.character import load_character
from companion_daemon.config import get_settings
from companion_daemon.conversation import SillyTavernConversationCore
from companion_daemon.budget import BudgetGate
from companion_daemon.attachment_cache import AttachmentCache
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.image_generation import OpenAIImageGenerator
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel
from companion_daemon.multimodal_analysis import MultimodalAnalyzer, OpenAIMultimodalAnalyzer
from companion_daemon.stickers import load_stickers
from companion_daemon.world import WorldKernel

logger = logging.getLogger(__name__)


def build_companion_engine(use_fake_model: bool = False) -> CompanionEngine:
    settings = get_settings()
    store = CompanionStore(Path(settings.database_path), primary_user_id=settings.primary_user_id)
    # Production is world-only.  Persist the fail-closed write guard before
    # any startup work so initialization itself cannot seed the legacy mood,
    # calendar, social-task, message, or memory models.
    store.enable_world_mode()
    world_kernel = WorldKernel(store)
    world_id = world_kernel.ensure_seed_file(settings.world_seed_path).world_id
    world_kernel.import_verified_facts(world_id, store.active_fact_lines(settings.primary_user_id))
    character = load_character(str(settings.character_path))
    store.map_account("simulator", "geoff", settings.primary_user_id)
    stickers = load_stickers(str(settings.stickers_path))
    if settings.deepseek_api_key and not use_fake_model:
        model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
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
    if settings.allow_auto_image_generation and settings.openai_api_key:
        image_generator = OpenAIImageGenerator(
            settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.image_model,
        )
    rewrite_model = None
    if settings.enable_reply_rewrite and settings.deepseek_api_key and not use_fake_model:
        rewrite_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_reply_model or settings.deepseek_model,
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
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
        budget_gate=budget_gate,
        visual_identity_path=settings.visual_identity_path,
        rewrite_model=rewrite_model,
        world_kernel=world_kernel,
        world_id=world_id,
        world_grounding_audit_model=model if world_kernel else None,
        attachment_cache=AttachmentCache(settings.attachment_cache_path),
    )
