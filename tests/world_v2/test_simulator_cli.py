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


@pytest.mark.asyncio
async def test_simulator_cli_wires_an_independent_life_source_reviewer_when_configured(
    tmp_path, monkeypatch
) -> None:
    author = SimpleNamespace(model="deepseek-world-author")
    reviewer = SimpleNamespace(model="openai-independent-source-reviewer")
    built: dict[str, object] = {}

    class _Application:
        async def respond(self, _turn):  # type: ignore[no-untyped-def]
            return SimpleNamespace(status="observed_only")

        async def drain_actions_once(self):  # type: ignore[no-untyped-def]
            return None

        async def drain_background_once(self):  # type: ignore[no-untyped-def]
            return None

        def close(self) -> None:
            return None

    async def _close() -> None:
        return None

    author.aclose = _close
    reviewer.aclose = _close
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(
            database_path=tmp_path / "companion.sqlite",
            primary_user_id="sim-user",
            deepseek_api_key="deepseek-test-key",
            deepseek_base_url="https://deepseek.example.invalid",
            deepseek_model="deepseek-v4-thinking",
            deepseek_reasoning_effort="high",
            openai_api_key="openai-test-key",
            openai_base_url="https://openai.example.invalid",
            openai_proxy_url=None,
            world_v2_source_review_fallback_model="gpt-test-source-reviewer",
        ),
    )
    monkeypatch.setattr(cli, "DeepSeekChatModel", lambda **_kwargs: author)
    monkeypatch.setattr(cli, "OpenAICompatibleChatModel", lambda **_kwargs: reviewer)

    def _build(**kwargs):  # type: ignore[no-untyped-def]
        built.update(kwargs)
        return _Application()

    monkeypatch.setattr(cli, "build_sqlite_world_v2_turn_application", _build)

    await cli.run_simulation("只检查组合，不调用供应商。", fake=False)

    world_author = built["life_world_author_model"]
    source_rewriter = built["life_world_author_source_rewriter"]
    source_reviewer = built["life_source_closure_reviewer"]
    assert world_author.authority_origin is author
    assert source_rewriter.authority_origin is author
    assert source_reviewer.authority_origin is reviewer
