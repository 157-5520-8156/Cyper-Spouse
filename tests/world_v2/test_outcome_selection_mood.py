from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.outcome_selection_draft import (
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionOption,
    ProposedLifeDirectionOption,
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
            {
                "candidate_result_ref": material["candidates"][0][
                    "candidate_result_ref"
                ],
                "adopt_proposed_life_direction": False,
            }
        )


class _RepairingModel:
    model = "test-outcome-repair"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        if len(self.calls) == 1:
            return (
                '{"candidate_result_ref":"candidate:not-offered",'
                '"adopt_proposed_life_direction":false}'
            )
        return (
            '{"candidate_result_ref":"candidate:rest-restless",'
            '"adopt_proposed_life_direction":false}'
        )


_OPTIONS = (
    OutcomeSelectionOption(candidate_result_ref="candidate:rest-restored", summary="安静歇了一阵，总算淡下去了。"),
    OutcomeSelectionOption(candidate_result_ref="candidate:rest-restless", summary="躺了一会儿还是没静下来。"),
)


def test_outcome_selection_accepts_the_world_author_full_outcome_bound() -> None:
    option = OutcomeSelectionOption(
        candidate_result_ref="candidate:long",
        summary="细" * 12_000,
    )

    assert len(option.summary) == 12_000


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


@pytest.mark.asyncio
async def test_outcome_selection_supplies_the_pinned_character_context_without_mapping_behavior() -> None:
    model = _Model()
    context = {
        "current_self_state": {"character_core": {"values": ["autonomy"]}},
        "relationships": [{"stage": "friend", "trust_bp": 7100}],
        "active_affect": [{"dimension": "sadness", "intensity_bp": 4200}],
        "active_memory_candidates": [{"candidate_id": "memory:user-encouragement"}],
        "aspirations": [{"aspiration_id": "aspiration:publishing"}],
        "commitments": [{"commitment_id": "commitment:user-call"}],
    }

    await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=_OPTIONS,
        mood_summary=None,
        decision_context=context,
    )

    assert model.materials[0]["current_character_context"] == context


@pytest.mark.asyncio
async def test_invalid_outcome_selection_gets_one_same_model_constrained_reselection() -> None:
    model = _RepairingModel()

    draft = await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=_OPTIONS,
        decision_context={"current_self_state": {"availability": "present"}},
    )

    assert draft.candidate_result_ref == "candidate:rest-restless"
    assert draft.attempt_raw_outputs == (
        (
            '{"candidate_result_ref":"candidate:not-offered",'
            '"adopt_proposed_life_direction":false}'
        ),
        (
            '{"candidate_result_ref":"candidate:rest-restless",'
            '"adopt_proposed_life_direction":false}'
        ),
    )
    assert len(model.calls) == 2
    repair = json.loads(model.calls[1][-1]["content"])
    assert repair["validation_failure"]["code"] == "invalid_outcome_selection"
    assert "unknown candidate" in repair["validation_failure"]["detail"]
    assert "same offered candidate_result_ref" in repair["instruction"]


@pytest.mark.asyncio
async def test_character_sees_and_independently_adopts_a_proposed_life_direction() -> None:
    class _DirectionModel:
        model = "test-direction-choice"

        def __init__(self) -> None:
            self.material: dict[str, object] = {}

        async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
            del temperature
            self.material = json.loads(messages[-1]["content"])
            return json.dumps(
                {
                    "candidate_result_ref": "candidate:bookshop",
                    "adopt_proposed_life_direction": True,
                }
            )

    model = _DirectionModel()
    direction = ProposedLifeDirectionOption(
        summary="她也许会把旧书店的偶遇发展成接下来几周持续参与的方向。",
        narrative_tags=("narrative:bookshop",),
        duration_days=21,
        privacy_class="personal",
    )
    draft = await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=(
            OutcomeSelectionOption(
                candidate_result_ref="candidate:bookshop",
                summary="这次在旧书店的帮忙告一段落。",
                proposed_life_direction=direction,
            ),
        )
    )

    assert draft.adopt_proposed_life_direction is True
    candidate = model.material["candidates"][0]  # type: ignore[index]
    assert candidate["proposed_life_direction"]["summary"] == direction.summary  # type: ignore[index]
