from __future__ import annotations

import inspect
from pathlib import Path

from companion_daemon.world_v2.life_ecology_runtime import (
    LifeEcologyRunResult,
    LifeEcologyRuntime,
)
from companion_daemon.world_v2.pinned_turn import PinnedTurnCompiler
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2 import shared_private_invitation


REPOSITORY_ROOT = Path(__file__).parents[2]


def test_retired_protagonist_life_author_modules_are_physically_absent() -> None:
    source = REPOSITORY_ROOT / "src/companion_daemon/world_v2"

    assert not (source / "aspiration_runtime.py").exists()
    assert not (source / "contextual_life_inspiration.py").exists()
    assert not (source / "future_life_author.py").exists()
    assert not (source / "life_author_runtime.py").exists()
    assert not hasattr(shared_private_invitation, "SharedPrivateInvitationRuntime")


def test_life_ecology_has_no_retired_character_author_followup_surface() -> None:
    runtime_parameters = inspect.signature(LifeEcologyRuntime).parameters
    result_fields = LifeEcologyRunResult.model_fields

    retired = {
        "life_author_followup",
        "future_life_author_followup",
        "aspiration_followup",
        "shared_private_followup",
    }
    assert not retired & set(runtime_parameters)
    assert not {
        "life_author_followup_status",
        "future_life_author_followup_status",
        "aspiration_followup_status",
        "shared_private_followup_status",
    } & set(result_fields)


def test_production_config_has_no_retired_life_author_switches() -> None:
    fields = set(inspect.signature(WorldV2TurnApplicationConfig).parameters)

    assert not {
        "future_life_author_enabled",
        "aspiration_enabled",
        "aspiration_fade_idle_days",
        "aspiration_fade_chance_bp",
        "aspiration_crystallize_chance_bp",
        "shared_private_invitation_enabled",
        "shared_private_invite_chance_bp",
    } & fields


def test_active_aspirations_are_not_hidden_behind_a_pinned_turn_switch() -> None:
    fields = set(inspect.signature(PinnedTurnCompiler).parameters)

    assert "aspiration_advisory" not in fields
