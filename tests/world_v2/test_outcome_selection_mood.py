from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.outcome_selection_draft import (
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionOption,
)


class _Model:
    model = "test-outcome-mood"

    def __init__(self) -> None:
        self.materials: list[dict[str, object]] = []

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        material = json.loads(messages[-1]["content"])
        self.materials.append(material)
        return json.dumps(
            {"candidate_result_ref": material["candidates"][0]["candidate_result_ref"]}
        )


_OPTIONS = (
    OutcomeSelectionOption(candidate_result_ref="candidate:rest-restored", summary="安静歇了一阵，总算淡下去了。"),
    OutcomeSelectionOption(candidate_result_ref="candidate:rest-restless", summary="躺了一会儿还是没静下来。"),
)


@pytest.mark.asyncio
async def test_outcome_selection_supplies_mood_as_advisory_material() -> None:
    model = _Model()

    draft = await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=_OPTIONS, mood_summary="她此刻可感的情绪：低落(强)。"
    )

    assert draft.candidate_result_ref == "candidate:rest-restored"
    assert model.materials[0]["current_mood"] == "她此刻可感的情绪：低落(强)。"


@pytest.mark.asyncio
async def test_outcome_selection_omits_mood_material_when_calm() -> None:
    model = _Model()

    await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=_OPTIONS, mood_summary=None
    )

    assert "current_mood" not in model.materials[0]
