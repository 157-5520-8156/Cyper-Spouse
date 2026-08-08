from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo


from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology


class _LifecycleOnlyModel:
    model = "test:travel-lifecycle-only"

    def __init__(self) -> None:
        self.lifecycle_calls = 0

    async def complete(  # type: ignore[no-untyped-def]
        self, messages, *, temperature: float = 0.2
    ) -> str:
        del temperature
        payload = json.loads(messages[-1]["content"])
        if "candidate" in payload:
            return '{"decision":"no_op"}'
        openings = payload.get("openings", ())
        if openings:
            self.lifecycle_calls += 1
            selected = (
                openings[1] if self.lifecycle_calls > 1 and len(openings) > 1 else openings[0]
            )
            return json.dumps(
                {
                    "decision": "select",
                    "opening_token": selected["opening_token"],
                }
            )
        return '{"decision":"no_op"}'


class _StayInJiaxingOutcomeModel:
    model = "test:jiaxing-stay-outcome"

    async def complete(  # type: ignore[no-untyped-def]
        self, messages, *, temperature: float = 0.2
    ) -> str:
        del temperature
        candidates = json.loads(messages[-1]["content"])["candidates"]
        selected = next(
            item
            for item in candidates
            if item["candidate_result_ref"].endswith(":jiaxing-homecoming-stay")
        )
        return json.dumps(
            {
                "candidate_result_ref": selected["candidate_result_ref"],
                "adopt_proposed_life_direction": False,
            }
        )


def test_legacy_story_fixture_reviews_research_without_claiming_a_visit(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(2028, 7, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    research = next(
        item
        for item in catalog.future_candidates_at(
            instant=instant,
            plans=(),
            npcs=(),
            life_arcs=(),
            max_candidates=100,
        )
        if item.opening.id == "future-contextual-destination-research"
    )

    assert research.location_ref is None
    assert research.opening.activity_kind == "travel.destination_research"
    assert all(outcome.life_arc_effect is None for outcome in research.opening.outcomes)
    assert all(
        "已经去" not in outcome.text and "到过" not in outcome.text
        for outcome in research.opening.outcomes
    )


def test_legacy_story_fixture_binds_exhibition_to_reviewed_museum_location(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    exhibition = next(
        item
        for item in catalog.future_candidates_at(
            instant=instant,
            plans=(),
            npcs=(
                type(
                    "Npc",
                    (),
                    {"npc_id": "literature-fan", "status": "active"},
                )(),
            ),
            life_arcs=(),
            max_candidates=100,
        )
        if item.opening.id == "future-fanyuan-exhibition"
    )

    assert exhibition.location_ref == "location:shanghai-city-art-museum"
    assert exhibition.participant_ref == "npc:literature-fan"
    assert exhibition.opening.location_id == "city-art-museum"
