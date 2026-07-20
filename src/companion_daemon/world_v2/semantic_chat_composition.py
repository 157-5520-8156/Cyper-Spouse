"""Production composition for same-turn semantic advice and compute routing.

The Module keeps one small Interface for platform composition roots while it
owns model selection, advisory authentication, matrix versioning, Flash /
Thinking routing, and model lifecycle.  Classifier output remains advisory;
the returned deliberation adapter is still the only producer of reply drafts.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets

from companion_daemon.character import load_character
from companion_daemon.config import Settings
from companion_daemon.llm import (
    DeepSeekChatModel,
    FailoverChatModel,
    FakeCompanionModel,
    OpenAICompatibleChatModel,
)

from .advisory_compiler import AdvisoryCompiler
from .chat_model_deliberation_adapter import (
    ChatCompletionModel,
    CompanionIdentityFrame,
)
from .expression_draft import (
    ExpressionDraftCapabilities,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from .matrix_catalog import default_matrix_catalog
from .semantic_advisory_adapter import SemanticAdvisoryAdapter
from .semantic_compute_router import SemanticComputeRouter
from .single_call_inbound_cognition import (
    SingleCallAppraisalAdapter,
    SingleCallExpressionAdapter,
    SingleCallInboundCognition,
)


@dataclass(slots=True)
class SemanticChatComposition:
    """The complete capability-free semantic/model side of one chat host."""

    flash_model: ChatCompletionModel
    background_model: ChatCompletionModel
    main_model: SingleCallExpressionAdapter
    appraisal_model: SingleCallAppraisalAdapter
    router: SemanticComputeRouter
    advisory_compiler: AdvisoryCompiler
    identity_frame: CompanionIdentityFrame
    _owned_models: tuple[object, ...] = ()

    async def aclose(self) -> None:
        await self.advisory_compiler.aclose()
        for model in self._owned_models:
            close = getattr(model, "aclose", None)
            if callable(close):
                await close()


def build_semantic_chat_composition(
    *,
    settings: Settings,
    flash_model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    advisory_model: ChatCompletionModel | None = None,
    model_id_prefix: str,
    expression_capabilities: ExpressionDraftCapabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES,
) -> SemanticChatComposition:
    """Build one production/fake pair through the same deep Module Interface.

    Explicitly supplied models are caller-owned.  With provider settings, the
    Module owns a Flash client and, when enabled, a separate bounded Thinking
    client.  Quick recovery remains inside ``RoutedChatModelDeliberationAdapter``
    and therefore always uses Flash.
    """

    if not model_id_prefix:
        raise ValueError("semantic chat composition requires a model id prefix")
    owned: list[object] = []

    def provider_route(primary: DeepSeekChatModel) -> ChatCompletionModel:
        if not settings.openai_api_key:
            owned.append(primary)
            return primary
        fallback = OpenAICompatibleChatModel(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.world_v2_fallback_model,
            reasoning_effort="none",
            proxy_url=settings.openai_proxy_url,
        )
        route = FailoverChatModel(primary=primary, fallback=fallback)
        owned.append(route)
        return route

    auto_flash = flash_model is None
    if flash_model is None:
        if settings.deepseek_api_key:
            provider_flash = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                thinking_enabled=False,
            )
            flash_model = provider_route(provider_flash)
        else:
            flash_model = FakeCompanionModel()
    if (
        thinking_model is None
        and auto_flash
        and settings.deepseek_api_key
        and settings.deepseek_deep_appraisal_thinking_enabled
    ):
        provider_thinking = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_deep_appraisal_model,
            thinking_enabled=True,
            reasoning_effort=settings.deepseek_deep_appraisal_reasoning_effort,
        )
        thinking_model = provider_route(provider_thinking)

    local_appraisal_model: ChatCompletionModel | None = None
    local_advisory_model: ChatCompletionModel | None = None
    if settings.local_appraisal_enabled:
        # This endpoint is deliberately explicit and local-only by default.
        # The small model is used for a typed Appraisal draft, never for the
        # visible character voice or direct world mutation authority.
        local_appraisal_model = OpenAICompatibleChatModel(
            api_key=settings.local_appraisal_api_key,
            base_url=settings.local_appraisal_base_url,
            model=settings.local_appraisal_model,
            reasoning_effort="none",
            max_completion_tokens=96,
        )
        owned.append(local_appraisal_model)
        # The same local checkpoint also carries the same-turn semantic
        # advisory classification.  The advisory is non-authoritative and
        # fail-open, but when it rides the remote Flash provider its network
        # round trip (~1.2s) becomes the floor of every turn's Context
        # compilation; the local endpoint answers the same bounded matrix
        # choice in tens of milliseconds.  A separate client keeps a larger
        # completion budget for the multi-field distribution JSON.
        local_advisory_model = OpenAICompatibleChatModel(
            api_key=settings.local_appraisal_api_key,
            base_url=settings.local_appraisal_base_url,
            model=settings.local_appraisal_model,
            reasoning_effort="none",
            max_completion_tokens=384,
        )
        owned.append(local_advisory_model)

    catalog = default_matrix_catalog()
    character = load_character(str(settings.character_path))
    aliases_raw = character.identity.get("nicknames", ())
    aliases = (
        tuple(str(item) for item in aliases_raw if str(item).strip())
        if isinstance(aliases_raw, list)
        else ()
    )
    identity_frame = CompanionIdentityFrame(
        companion_name=character.name,
        companion_aliases=aliases,
        counterpart_name=settings.primary_user_id,
        relationship_frame=character.relationship,
        stable_identity_facts=tuple(character.canonical_facts),
        personality_frame=character.personality,
        values=tuple(character.values),
        speech_frame=character.speech,
        style_rules=tuple(character.style_rules),
        boundaries=tuple(character.boundaries),
    )
    semantic_advice = SemanticAdvisoryAdapter(
        model=advisory_model or local_advisory_model or flash_model,
        catalog=catalog,
    )
    advisory_compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(semantic_advice,),
        authority_key=secrets.token_bytes(32),
        timeout_seconds=settings.world_v2_advisory_timeout_seconds,
    )
    cognition = SingleCallInboundCognition(
        flash_model=flash_model,
        thinking_model=thinking_model,
        appraisal_model=local_appraisal_model,
        flash_model_id=str(getattr(flash_model, "model", f"{model_id_prefix}-flash")),
        thinking_model_id=(
            str(getattr(thinking_model, "model", f"{model_id_prefix}-thinking"))
            if thinking_model is not None
            else None
        ),
        expression_capabilities=expression_capabilities,
        identity_frame=identity_frame,
    )
    return SemanticChatComposition(
        flash_model=flash_model,
        background_model=advisory_model or flash_model,
        main_model=cognition.expression,
        appraisal_model=cognition.appraisal,
        router=SemanticComputeRouter(thinking_available=thinking_model is not None),
        advisory_compiler=advisory_compiler,
        identity_frame=identity_frame,
        _owned_models=tuple(owned),
    )


__all__ = ["SemanticChatComposition", "build_semantic_chat_composition"]
