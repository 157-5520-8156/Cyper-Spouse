from __future__ import annotations

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.expression_draft import (
    ExpressionDraftCapabilities,
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)


class _UnusedCharacterModel:
    model = "fixture:single-character-author"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        raise AssertionError("composition invariant must fail before provider use")


def test_production_config_defaults_to_required_private_self() -> None:
    config = WorldV2TurnApplicationConfig(
        world_id="world:required-private-self-default",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:test",
    )

    assert config.expression_capabilities == PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES
    assert config.expression_capabilities.private_turn_state_mode == "required"


def test_production_semantic_builder_rejects_legacy_optional_private_self() -> None:
    legacy_capability = ExpressionDraftCapabilities(
        profile_id="expression:historical-replay-only.1",
        modalities=("text",),
        private_turn_state_mode="legacy_optional",
    )

    with pytest.raises(
        ValueError,
        match="production expression requires a final PrivateTurnState",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY=None,
                OPENAI_API_KEY=None,
                OPENROUTER_API_KEY=None,
            ),
            flash_model=_UnusedCharacterModel(),
            model_id_prefix="production-private-self-invariant",
            expression_capabilities=legacy_capability,
        )
