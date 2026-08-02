from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.outcome_selection_draft import (
    OutcomeSelectionDraftAdapter,
    OutcomeSelectionFailure,
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
async def test_character_freely_forms_a_life_direction_without_a_world_author_menu() -> None:
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
                    "character_life_direction": {
                        "coordinate_ref": "biography:direction.work",
                        "summary": "她想认真试试把修书和旧书经营变成自己的工作。",
                        "context_tags": ["direction.work:independent_bookseller"],
                        "replaces_context_tag_prefixes": ["direction.work:"],
                        "privacy_class": "personal",
                    },
                }
            )

    model = _DirectionModel()
    draft = await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=(
            OutcomeSelectionOption(
                candidate_result_ref="candidate:bookshop",
                summary="这次在旧书店的帮忙告一段落。",
            ),
        )
    )

    assert draft.character_life_direction is not None
    assert draft.character_life_direction.coordinate_ref == "biography:direction.work"
    candidate = model.material["candidates"][0]  # type: ignore[index]
    assert "proposed_life_direction" not in candidate  # type: ignore[operator]


@pytest.mark.asyncio
async def test_character_direction_cannot_claim_an_objective_biographical_fact() -> None:
    class _ObjectiveFactModel:
        model = "test-direction-objective-fact"

        async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
            del messages, temperature
            return json.dumps(
                {
                    "candidate_result_ref": "candidate:bookshop",
                    "character_life_direction": {
                        "coordinate_ref": "biography:work",
                        "summary": "她已经成为独立书店店主。",
                        "context_tags": ["occupation:independent_bookseller"],
                        "replaces_context_tag_prefixes": ["occupation:"],
                        "privacy_class": "personal",
                    },
                }
            )

    with pytest.raises(OutcomeSelectionFailure) as exc_info:
        await OutcomeSelectionDraftAdapter(model=_ObjectiveFactModel()).deliberate(
            options=(
                OutcomeSelectionOption(
                    candidate_result_ref="candidate:bookshop",
                    summary="这次在旧书店的帮忙告一段落。",
                ),
            )
        )

    assert "corrective_invalid" in str(exc_info.value)


@pytest.mark.asyncio
async def test_existing_coordinate_identity_is_visible_and_must_be_reused() -> None:
    class _CoordinateRepairModel:
        model = "test-coordinate-repair"

        def __init__(self) -> None:
            self.calls: list[list[dict[str, str]]] = []

        async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
            del temperature
            self.calls.append(messages)
            coordinate_ref = (
                "biography:direction.new-work"
                if len(self.calls) == 1
                else "biography:direction.work"
            )
            return json.dumps(
                {
                    "candidate_result_ref": "candidate:bookshop",
                    "character_life_direction": {
                        "coordinate_ref": coordinate_ref,
                        "summary": "她想继续沿着独立书店这条路试一阵。",
                        "context_tags": ["direction.work:independent_bookseller"],
                        "replaces_context_tag_prefixes": ["direction.work:"],
                        "privacy_class": "personal",
                    },
                }
            )

    model = _CoordinateRepairModel()
    current = type(
        "Coordinate",
        (),
        {
            "coordinate_ref": "biography:direction.work",
            "entity_revision": 2,
            "summary": "她还在摸索把旧书经营变成长期方向。",
            "context_tags": ("direction.work:exploring",),
            "replaces_context_tag_prefixes": ("direction.work:",),
            "settlement_event_ref": "event:settlement:earlier",
        },
    )()

    draft = await OutcomeSelectionDraftAdapter(model=model).deliberate(
        options=(
            OutcomeSelectionOption(
                candidate_result_ref="candidate:bookshop",
                summary="这次在旧书店的帮忙告一段落。",
            ),
        ),
        current_coordinates=(current,),
    )

    material = json.loads(model.calls[0][-1]["content"])
    assert material["current_biographical_coordinates"][0]["coordinate_ref"] == (
        "biography:direction.work"
    )
    assert len(model.calls) == 2
    assert draft.character_life_direction is not None
    assert draft.character_life_direction.coordinate_ref == "biography:direction.work"
