from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.production_turn_application import (
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.schemas import ProjectionCursor
from test_life_author_production import (
    _Identities,
    _MainModel,
    _MemoryChat,
    _QuickRecovery,
    _Router,
    _Transport,
    _config,
)


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
                openings[1]
                if self.lifecycle_calls > 1 and len(openings) > 1
                else openings[0]
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
    instant = datetime(
        2028, 7, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

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
    assert all(
        outcome.life_arc_effect is None for outcome in research.opening.outcomes
    )
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
    instant = datetime(
        2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

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


@pytest.mark.asyncio
async def test_locationless_destination_research_settles_to_experience_and_memory(
    tmp_path: Path,
    legacy_story_seed_path: Path,
) -> None:
    seed = legacy_story_seed_path
    planning_at = datetime(
        2028, 7, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    memory = _MemoryChat()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "production-destination-research.sqlite",
        config=_config(seed),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_LifecycleOnlyModel(),
        memory_model=memory,
        now=planning_at,
    )
    try:
        considered_at = planning_at + timedelta(minutes=10)
        await app.tick(
            tick_id="destination-research:consider",
            logical_time_from=planning_at,
            logical_time_to=considered_at,
            observed_at=considered_at,
            trace_id="trace:destination-research:consider",
            causation_id="scheduler:test",
            correlation_id="correlation:destination-research",
            reason="production-locationless-life-chain-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        wake = next(
            item
            for item in reversed(projection.committed_world_event_refs)
            if item.event_type == "ClockAdvanced"
        )
        future = app._life_ecology._future_life_author_followup  # noqa: SLF001
        candidate = next(
            item
            for item in future._catalog.future_candidates_at(  # noqa: SLF001
                instant=wake.logical_time,
                plans=projection.plans,
                npcs=projection.npcs,
                life_arcs=projection.life_arcs,
                max_candidates=100,
            )
            if item.opening.id == "future-contextual-destination-research"
        )
        assert candidate.location_ref is None
        future._accept_plan(  # noqa: SLF001
            candidate=candidate,
            wake_event_ref=wake.event_id,
            suffix="production-locationless-research-e2e",
            trace_id="trace:destination-research:plan",
            correlation_id="correlation:destination-research",
            expected_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
        )

        await app.tick(
            tick_id="destination-research:start",
            logical_time_from=considered_at,
            logical_time_to=candidate.opens_at,
            observed_at=candidate.opens_at,
            trace_id="trace:destination-research:start",
            causation_id="scheduler:test",
            correlation_id="correlation:destination-research",
            reason="production-locationless-life-chain-test",
        )
        settled_at = candidate.closes_at + timedelta(minutes=1)
        await app.tick(
            tick_id="destination-research:settle",
            logical_time_from=candidate.opens_at,
            logical_time_to=settled_at,
            observed_at=settled_at,
            trace_id="trace:destination-research:settle",
            causation_id="scheduler:test",
            correlation_id="correlation:destination-research",
            reason="production-locationless-life-chain-test",
        )

        projection = app._ledger.project()  # noqa: SLF001
        plan = next(
            item
            for item in projection.plans
            if item.activity_kind == "travel.destination_research"
        )
        occurrence = next(
            item
            for item in projection.world_occurrences
            if item.trigger_ref == plan.plan_id
        )
        experience = next(
            item
            for item in projection.experiences
            if item.values.source_bindings[0].authority_event_ref
            == occurrence.settlement_event_ref
        )
        memory_candidate = next(
            item
            for item in projection.memory_candidates
            if any(
                binding.source_id == experience.experience_id
                for binding in item.values.source_bindings
            )
        )
        summary = app._life_content_store.read_exact(  # noqa: SLF001
            content_ref=experience.values.summary_ref
        )

        assert plan.status == "completed"
        assert plan.location_ref is None
        assert occurrence.status == "settled"
        assert occurrence.location_ref is None
        assert summary is not None
        assert all(
            phrase not in summary.text
            for phrase in ("已经去", "到过", "抵达", "打卡")
        )
        assert memory_candidate.values.status in {"pending", "active"}
        assert memory.calls == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_homecoming_settles_to_experience_and_travel_residence_arc(
    tmp_path: Path,
    legacy_story_seed_path: Path,
) -> None:
    seed = legacy_story_seed_path
    planning_at = datetime(
        2026, 7, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    model = _LifecycleOnlyModel()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "production-homecoming.sqlite",
        config=_config(seed),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        outcome_draft_model=_StayInJiaxingOutcomeModel(),
        now=planning_at,
    )
    try:
        considered_at = planning_at + timedelta(minutes=10)
        await app.tick(
            tick_id="homecoming:consider",
            logical_time_from=planning_at,
            logical_time_to=considered_at,
            observed_at=considered_at,
            trace_id="trace:homecoming:consider",
            causation_id="scheduler:test",
            correlation_id="correlation:homecoming",
            reason="production-travel-chain-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        wake = next(
            item
            for item in reversed(projection.committed_world_event_refs)
            if item.event_type == "ClockAdvanced"
        )
        catalog = app._life_ecology._future_life_author_followup._catalog  # noqa: SLF001
        candidate = next(
            item
            for item in catalog.future_candidates_at(
                instant=wake.logical_time,
                plans=projection.plans,
                npcs=projection.npcs,
                life_arcs=projection.life_arcs,
                max_candidates=100,
            )
            if item.opening.id == "future-jiaxing-homecoming"
        )
        future = app._life_ecology._future_life_author_followup  # noqa: SLF001
        future._accept_plan(  # noqa: SLF001
            candidate=candidate,
            wake_event_ref=wake.event_id,
            suffix="production-homecoming-e2e",
            trace_id="trace:homecoming:plan",
            correlation_id="correlation:homecoming",
            expected_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
        )

        await app.tick(
            tick_id="homecoming:start",
            logical_time_from=considered_at,
            logical_time_to=candidate.opens_at,
            observed_at=candidate.opens_at,
            trace_id="trace:homecoming:start",
            causation_id="scheduler:test",
            correlation_id="correlation:homecoming",
            reason="production-travel-chain-test",
        )
        settled_at = candidate.closes_at + timedelta(minutes=1)
        await app.tick(
            tick_id="homecoming:settle",
            logical_time_from=candidate.opens_at,
            logical_time_to=settled_at,
            observed_at=settled_at,
            trace_id="trace:homecoming:settle",
            causation_id="scheduler:test",
            correlation_id="correlation:homecoming",
            reason="production-travel-chain-test",
        )

        projection = app._ledger.project()  # noqa: SLF001
        plan = next(
            item
            for item in projection.plans
            if item.activity_kind == "travel.jiaxing_homecoming"
        )
        occurrence = next(
            item
            for item in projection.world_occurrences
            if item.trigger_ref == plan.plan_id
        )
        arc = next(
            item
            for item in projection.life_arcs
            if item.context_pack_ref == "life-context:jiaxing-family-home-stay"
        )
        biography = catalog.biographical_context_at(
            instant=projection.logical_time,
            life_arcs=projection.life_arcs,
        )

        assert plan.status == "completed"
        assert plan.location_ref == "location:jiaxing-family-home"
        assert occurrence.status == "settled"
        assert any(
            item.values.source_bindings[0].authority_event_ref
            == occurrence.settlement_event_ref
            for item in projection.experiences
        )
        assert arc.status == "active"
        assert arc.arc_kind == "travel"
        assert arc.source_event_ref == occurrence.settlement_event_ref
        assert "residence:temporary_family_home_jiaxing" in biography.context_tags
        assert "travel:visiting_jiaxing" in biography.context_tags
    finally:
        app.close()
