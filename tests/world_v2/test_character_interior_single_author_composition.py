from __future__ import annotations

import inspect

import pytest

from companion_daemon.config import Settings
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)


class _UnusedModel:
    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        raise AssertionError("architecture test must not invoke a provider")


def test_production_interfaces_expose_no_secondary_semantic_appraisal_author() -> None:
    assert "advisory_compiler" not in inspect.signature(PinnedTurnCompiler).parameters
    assert "advisory_compiler" not in inspect.signature(
        build_sqlite_world_v2_turn_application
    ).parameters
    assert "advisory_model" not in inspect.signature(
        build_semantic_chat_composition
    ).parameters
    assert "world_support_model" in inspect.signature(
        build_semantic_chat_composition
    ).parameters
    assert not hasattr(PinnedTurnCompiler, "_advisory_snapshot")
    assert not hasattr(PinnedTurnCompiler, "_advisory_request")


@pytest.mark.asyncio
async def test_production_composition_keeps_world_support_outside_character_interior() -> None:
    character = _UnusedModel()
    world_support = _UnusedModel()
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
            OPENROUTER_API_KEY=None,
        ),
        flash_model=character,
        world_support_model=world_support,
        model_id_prefix="single-character-author",
    )
    try:
        assert composition.world_support_model is world_support
        assert not hasattr(composition, "advisory_compiler")
        health = composition.character_interior.runtime_health()
        assert health["parallel_character_author_conflicts"] == 0
        assert health["legacy_interface_invocations"] == 0
    finally:
        await composition.aclose()
