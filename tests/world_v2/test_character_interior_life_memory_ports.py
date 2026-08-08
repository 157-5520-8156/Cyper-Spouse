"""Architecture regression for the direct CharacterInterior memory cutover."""

from __future__ import annotations

import inspect

from companion_daemon.world_v2.character_interior import life_memory
from companion_daemon.world_v2.interaction_fact_trigger_runtime import (
    InteractionFactTriggerRuntime,
)
from companion_daemon.world_v2.life_aftermath_runtime import LifeAftermathRuntime
from companion_daemon.world_v2.memory_withdrawal_review import (
    MemoryWithdrawalReviewRuntime,
)


def test_memory_runtimes_depend_on_character_interior_without_purpose_facades() -> None:
    fact_parameters = inspect.signature(InteractionFactTriggerRuntime).parameters
    life_parameters = inspect.signature(LifeAftermathRuntime).parameters
    withdrawal_parameters = inspect.signature(MemoryWithdrawalReviewRuntime).parameters

    assert "character_interior" in fact_parameters
    assert "memory_adapter" not in fact_parameters
    assert "character_interior" in life_parameters
    assert "memory_adapter" not in life_parameters
    assert "outcome_selection_model" not in life_parameters
    assert "character_interior" in withdrawal_parameters
    assert "reviewer" not in withdrawal_parameters
    assert life_memory.__all__ == []
    assert not any(
        name.startswith("CharacterInterior") and name.endswith("MemoryPort")
        for name in vars(life_memory)
    )
