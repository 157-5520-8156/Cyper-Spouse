from __future__ import annotations

import pytest

from companion_daemon.world_v2.media_selection_draft import (
    MediaCandidateChoice,
    MediaSelectionCapsule,
    MediaSelectionDraftAdapter,
)


class _Model:
    model = "test-flash"

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def complete(self, _messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        assert temperature == 0.2
        return self.reply


@pytest.mark.asyncio
async def test_adapter_returns_only_an_offered_token_or_no_op() -> None:
    capsule = MediaSelectionCapsule(candidates=(MediaCandidateChoice(token="candidate-token", safe_summary="一件已确认的日常事件"),))
    selected = await MediaSelectionDraftAdapter(model=_Model('{"decision":"select","token":"candidate-token"}')).deliberate(capsule=capsule)
    declined = await MediaSelectionDraftAdapter(model=_Model('{"decision":"no_op"}')).deliberate(capsule=capsule)
    assert selected.token == "candidate-token"
    assert selected.raw_output_hash and selected.normalized_output_hash
    assert declined.decision == "no_op"


@pytest.mark.asyncio
async def test_adapter_rejects_an_unoffered_token() -> None:
    capsule = MediaSelectionCapsule(candidates=(MediaCandidateChoice(token="candidate-token", safe_summary="事件"),))
    with pytest.raises(ValueError, match="unknown"):
        await MediaSelectionDraftAdapter(model=_Model('{"decision":"select","token":"forged"}')).deliberate(capsule=capsule)
