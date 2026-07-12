from types import SimpleNamespace

import pytest

from companion_daemon import proactive_cli


@pytest.mark.asyncio
async def test_proactive_run_closes_engine_after_early_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        closed = False

        async def proactive_tick(self, _user_id: str) -> SimpleNamespace:
            return SimpleNamespace(
                private_thought="不发。",
                should_send=False,
                platform=None,
                message_type="none",
                message=None,
                sticker_path=None,
                image_path=None,
            )

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    monkeypatch.setattr(proactive_cli, "build_companion_engine", lambda: engine)

    await proactive_cli.run("geoff", send=False, sandbox=True)

    assert engine.closed is True


@pytest.mark.asyncio
async def test_proactive_run_closes_engine_when_generation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        closed = False

        async def proactive_tick(self, _user_id: str) -> None:
            raise RuntimeError("provider failed")

        async def aclose(self) -> None:
            self.closed = True

    engine = FakeEngine()
    monkeypatch.setattr(proactive_cli, "build_companion_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="provider failed"):
        await proactive_cli.run("geoff", send=False, sandbox=True)

    assert engine.closed is True
