from __future__ import annotations

from types import SimpleNamespace

import pytest

from companion_daemon import cli
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


@pytest.mark.asyncio
async def test_simulator_cli_uses_persistent_v2_turn_application_not_legacy_engine(
    tmp_path, monkeypatch, capsys
) -> None:
    database_path = tmp_path / "companion.sqlite"
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            database_path=database_path,
            primary_user_id="sim-user",
            deepseek_api_key=None,
            deepseek_base_url="https://example.invalid",
            deepseek_model="deepseek-v4-thinking",
            deepseek_reasoning_effort="high",
        ),
    )

    await cli.run_simulation("今天有点累。", fake=True)

    output = capsys.readouterr().out
    assert "[reply:action_authorized] 我在，刚刚这句我有接到。" in output
    ledger = SQLiteWorldLedger(
        path=database_path,
        world_id="world:companion-v2:sim-user",
    )
    try:
        event_types = [item.event.event_type for item in ledger.export_replay_evidence().events]
    finally:
        ledger.close()
    assert "ObservationRecorded" in event_types
    assert "ActionAuthorized" in event_types
    assert "ExternalObservationRecorded" in event_types
    assert "ExternalObservationProcessed" in event_types


@pytest.mark.asyncio
async def test_simulator_cli_can_exercise_the_configured_thinking_lane(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            database_path=tmp_path / "companion.sqlite",
            primary_user_id="sim-user",
            deepseek_api_key=None,
            deepseek_base_url="https://example.invalid",
            deepseek_model="deepseek-v4-thinking",
            deepseek_reasoning_effort="high",
        ),
    )

    await cli.run_simulation("我有个复杂的问题。", fake=True, thinking=True)

    assert "[reply:action_authorized]" in capsys.readouterr().out
