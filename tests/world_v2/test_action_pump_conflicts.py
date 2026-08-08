from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from companion_daemon.world_v2.action_pump import ActionPump, ActionPumpResult
from companion_daemon.world_v2.errors import ConcurrencyConflict, IdempotencyConflict


class _ScriptedActionPump(ActionPump):
    """Exercise the public retry contract without constructing an Action graph."""

    def __init__(
        self,
        outcomes: list[Exception | ActionPumpResult],
    ) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def _drain_once(
        self,
        *,
        target_action_id: str | None = None,
        provider_accepted_reconciliation_gate: object | None = None,
    ) -> ActionPumpResult:
        del target_action_id, provider_accepted_reconciliation_gate
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _entrypoint(
    *,
    targeted: bool,
) -> Callable[[_ScriptedActionPump], Awaitable[ActionPumpResult]]:
    if targeted:
        return lambda pump: pump.drain_action("action:test")
    return lambda pump: pump.drain_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.asyncio
async def test_idempotency_conflict_is_permanent_and_propagated_without_retry(
    targeted: bool,
) -> None:
    conflict = IdempotencyConflict("provider receipt identity has different content")
    pump = _ScriptedActionPump([conflict, ActionPumpResult(status="idle")])

    with pytest.raises(IdempotencyConflict) as raised:
        await _entrypoint(targeted=targeted)(pump)

    assert raised.value is conflict
    assert pump.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.asyncio
async def test_concurrency_conflict_remains_a_bounded_retry(
    targeted: bool,
) -> None:
    expected = ActionPumpResult(action_id="action:test", status="settled")
    pump = _ScriptedActionPump(
        [ConcurrencyConflict("stale cursor"), expected],
    )

    result = await _entrypoint(targeted=targeted)(pump)

    assert result == expected
    assert pump.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.asyncio
async def test_persistent_concurrency_conflict_stops_after_three_attempts(
    targeted: bool,
) -> None:
    pump = _ScriptedActionPump(
        [
            ConcurrencyConflict("stale cursor 1"),
            ConcurrencyConflict("stale cursor 2"),
            ConcurrencyConflict("stale cursor 3"),
        ],
    )

    with pytest.raises(ConcurrencyConflict, match="did not converge"):
        await _entrypoint(targeted=targeted)(pump)

    assert pump.calls == 3
