from __future__ import annotations

import inspect

from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)


def test_production_builder_exposes_no_legacy_activity_character_model() -> None:
    parameters = inspect.signature(build_sqlite_world_v2_turn_application).parameters

    assert "activity_lifecycle_model" not in parameters
    assert "npc_actor_model" in parameters
