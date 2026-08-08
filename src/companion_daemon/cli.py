import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256

from companion_daemon.config import get_settings
from companion_daemon.llm import (
    DeepSeekChatModel,
    FakeCompanionModel,
    OpenAICompatibleChatModel,
)
from companion_daemon.world_v2.model_completion import ChatCompletionModel
from companion_daemon.world_v2.expression_draft import (
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.life_development_model_adapter import (
    RoleBoundLifeDevelopmentModelAdapter,
)
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)
from companion_daemon.world_v2.simulator_adapters import (
    CaptureSimulatorTransport,
    SimulatorIdentityResolver,
)
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


class _SimulationRouter:
    """The CLI makes an explicit model-tier choice; production routing stays separate."""

    def __init__(self, *, thinking: bool) -> None:
        self._thinking = thinking

    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(
            tier="thinking" if self._thinking else "flash",
            reason_code="simulator_explicit_tier",
            router_version="world-v2-simulator-router.1",
        )


async def run_simulation(text: str, fake: bool, *, thinking: bool = False) -> None:
    """Exercise the persistent, platform-neutral World v2 turn vertically.

    The former simulator constructed the legacy ``CompanionEngine``.  Keeping
    it as a v2-only host prevents local validation from hiding a second world
    write path behind a convenient command-line tool.
    """

    settings = get_settings()
    now = datetime.now(UTC)
    transport = CaptureSimulatorTransport(received_at=now)
    owned_models: list[DeepSeekChatModel] = []
    source_reviewer: ChatCompletionModel | None = None
    life_source_reviewer: ChatCompletionModel | None = None
    if fake:
        flash_model: ChatCompletionModel = FakeCompanionModel()
        thinking_model: ChatCompletionModel | None = FakeCompanionModel() if thinking else None
    else:
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required unless --fake is used")
        flash_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking_enabled=False,
        )
        owned_models.append(flash_model)
        if settings.openai_api_key:
            # The World Author may invent proposal-scoped life material, but
            # it cannot review its own existing-world claims.  The simulator
            # installs the independently configured OpenAI lane when present;
            # without it, factful life proposals fail closed while no-op
            # ecology remains available.
            reviewer_options = {
                "api_key": settings.openai_api_key,
                "base_url": settings.openai_base_url,
                "model": settings.world_v2_source_review_fallback_model,
                # GPT-4.1/4o Chat Completions reject this optional field.
                # Empty means omit it at the transport boundary.
                "reasoning_effort": "",
                "max_completion_tokens": 1_200,
                "proxy_url": settings.openai_proxy_url,
            }
            # These are separate semantic authorities even when they use the
            # same provider/model configuration. Sharing one client here made
            # simulator validation violate the production self-review boundary.
            source_reviewer_client = OpenAICompatibleChatModel(**reviewer_options)
            life_source_reviewer_client = OpenAICompatibleChatModel(**reviewer_options)
            source_reviewer = source_reviewer_client
            life_source_reviewer = life_source_reviewer_client
            owned_models.extend((source_reviewer_client, life_source_reviewer_client))
        thinking_model = None
        if thinking:
            thinking_model = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_character_thinking_model,
                thinking_enabled=True,
                reasoning_effort=settings.deepseek_character_thinking_reasoning_effort,
            )
            owned_models.append(thinking_model)
    semantic_chat = build_semantic_chat_composition(
        settings=settings,
        flash_model=flash_model,
        thinking_model=thinking_model,
        source_closure_model=source_reviewer,
        life_source_closure_model=life_source_reviewer,
        model_id_prefix="world-v2-simulator",
    )
    app = build_sqlite_world_v2_turn_application(
        path=settings.database_path,
        config=WorldV2TurnApplicationConfig(
            world_id=f"world:companion-v2:{settings.primary_user_id}",
            companion_actor_ref="agent:companion",
            reply_target=f"user:{settings.primary_user_id}",
            action_pump_owner="pump:companion-simulator-v2",
            expression_capabilities=PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
            life_ecology=LifeEcologyComposition.production_v1(),
        ),
        identities=SimulatorIdentityResolver(canonical_user_id=settings.primary_user_id),
        router=_SimulationRouter(thinking=thinking),
        character_interior=semantic_chat.character_interior,
        transport=transport,
        fact_model=flash_model,
        npc_actor_model=flash_model,
        life_world_author_model=RoleBoundLifeDevelopmentModelAdapter(
            model=flash_model,
            role="world_author",
        ),
        life_world_author_source_rewriter=RoleBoundLifeDevelopmentModelAdapter(
            model=flash_model,
            role="world_author",
        ),
        life_source_closure_reviewer=(
            RoleBoundLifeDevelopmentModelAdapter(
                model=life_source_reviewer,
                role="world_author_source_reviewer",
            )
            if life_source_reviewer is not None
            else None
        ),
        now=now,
    )
    try:
        message_id = f"simulation:{sha256((text + now.isoformat()).encode()).hexdigest()[:20]}"
        outcome = await app.respond(
            InboundTurn(
                platform="simulator",
                platform_user_id=settings.primary_user_id,
                platform_message_id=message_id,
                text=text,
                observed_at=now,
                trace_id=f"trace:{message_id}",
            )
        )
        delivery = await app.drain_actions_once()
        await app.drain_background_once()
        if not transport.bodies:
            print(f"[reply:{outcome.status}] no settled reply ({delivery.status if delivery else 'idle'})")
            return
        print(f"[reply:{outcome.status}] {transport.bodies[-1]}")
    finally:
        app.close()
        await semantic_chat.aclose()
        for model in owned_models:
            await model.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a companion chat turn.")
    parser.add_argument("text", help="Incoming user text")
    parser.add_argument("--fake", action="store_true", help="Do not call DeepSeek")
    parser.add_argument(
        "--thinking",
        action="store_true",
        help="Route this explicit simulator turn to the configured thinking model.",
    )
    args = parser.parse_args()
    asyncio.run(run_simulation(args.text, args.fake, thinking=args.thinking))
