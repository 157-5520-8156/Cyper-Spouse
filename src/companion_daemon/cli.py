import argparse
import asyncio
from datetime import UTC, datetime
from hashlib import sha256

from companion_daemon.config import get_settings
from companion_daemon.llm import DeepSeekChatModel, FakeCompanionModel
from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatCompletionModel,
    RoutedChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.appraisal_chat_model_adapter import AppraisalDraftDeliberationAdapter
from companion_daemon.world_v2.affect_chat_model_adapter import AffectDraftDeliberationAdapter
from companion_daemon.world_v2.deliberation import ModelRoute, RouteRequest
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
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
    if fake:
        flash_model: ChatCompletionModel = FakeCompanionModel()
        thinking_model: ChatCompletionModel | None = FakeCompanionModel() if thinking else None
    else:
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is required unless --fake is used")
        flash_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model="deepseek-v4-flash",
            thinking_enabled=False,
        )
        owned_models.append(flash_model)
        thinking_model = None
        if thinking:
            thinking_model = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                thinking_enabled=True,
                reasoning_effort=settings.deepseek_reasoning_effort,
            )
            owned_models.append(thinking_model)
    adapter = RoutedChatModelDeliberationAdapter(
        flash_model=flash_model,
        thinking_model=thinking_model,
        flash_model_id="deepseek-v4-flash" if not fake else "fake-world-v2-flash",
        thinking_model_id=settings.deepseek_model if thinking and not fake else "fake-world-v2-thinking",
    )
    app = build_sqlite_world_v2_turn_application(
        path=settings.database_path,
        config=WorldV2TurnApplicationConfig(
            world_id=f"world:companion-v2:{settings.primary_user_id}",
            companion_actor_ref="agent:companion",
            reply_target=f"user:{settings.primary_user_id}",
            action_pump_owner="pump:companion-simulator-v2",
        ),
        identities=SimulatorIdentityResolver(canonical_user_id=settings.primary_user_id),
        router=_SimulationRouter(thinking=thinking),
        main_model=adapter,
        quick_recovery=adapter,
        transport=transport,
        appraisal_model=AppraisalDraftDeliberationAdapter(model=flash_model),
        affect_model=AffectDraftDeliberationAdapter(model=flash_model),
        fact_model=flash_model,
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
