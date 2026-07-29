from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.life_author_runtime import (
    LifeAuthorDecisionRecordedPayload,
)
from companion_daemon.world_v2.experience_memory_decision import (
    experience_memory_decision_event_id,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.random_authority import RandomDrawRecordedPayload
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)

NOW = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)


class _MemoryChat:
    model = "test-experience-memory"

    def __init__(self, *, retain: bool = True) -> None:
        self.retain = retain
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert temperature == 0.15
        assert "lived Experience from your own life" in messages[0]["content"]
        if not self.retain:
            return '{"retain":false}'
        return json.dumps(
            {
                "retain": True,
                "cue_kind": "world_continuity",
                "retention_rationales": ["world_continuity"],
                "salience": {
                    "autobiographical_relevance_bp": 7000,
                    "relationship_relevance_bp": 2000,
                    "emotional_residue_bp": 2000,
                    "unfinished_business_bp": 1000,
                    "recurrence_bp": 3000,
                    "novelty_bp": 6000,
                    "future_utility_bp": 5000,
                    "world_continuity_bp": 9000,
                },
            }
        )


class _NeverMemoryChat:
    model = "test-experience-memory-never"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError("durable Experience-memory decision must be rejoined")


def test_legacy_story_fixture_offers_a_real_clock_bound_sleep_wake_opening(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    local = datetime(2026, 7, 20, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    candidates = catalog.candidates_at(
        instant=local.astimezone(UTC),
        wake_event_ref="event:clock:bedtime",
        plans=(),
        npcs=tuple(
            SimpleNamespace(npc_id=item.npc_id, status="active") for item in catalog.reviewed_npcs
        ),
    )

    sleep = next(
        item
        for item in candidates
        if item.opening.activity_kind == "sleep.family_home_prepare_for_bed"
    )
    assert sleep.opening.source == "routine"
    assert sleep.opening.domain == "sleep_wake"
    assert sleep.opening.visual_potential == "ambient"
    assert sleep.opening.privacy == "private"
    assert len(sleep.opening.outcomes) == 2


def test_legacy_story_fixture_has_continuous_reviewed_night_coverage_after_midnight(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    active_npcs = tuple(
        SimpleNamespace(npc_id=item.npc_id, status="active") for item in catalog.reviewed_npcs
    )

    by_hour = {
        hour: {
            item.opening.activity_kind
            for item in catalog.candidates_at(
                instant=datetime(
                    2026, 7, 20, hour, 15, tzinfo=ZoneInfo("Asia/Shanghai")
                ).astimezone(UTC),
                wake_event_ref=f"event:clock:night:{hour}",
                plans=(),
                npcs=active_npcs,
            )
        }
        for hour in range(0, 8)
    }

    assert "sleep.family_home_prepare_for_bed" in by_hour[0]
    assert all("sleep.family_home_late_wind_down" in by_hour[hour] for hour in (1, 2, 3))
    assert all("sleep.family_home_early_wake" in by_hour[hour] for hour in (4, 5, 6))
    assert "routine.family_home_morning_settle" in by_hour[7]


def test_after_midnight_bedtime_does_not_consume_the_next_evenings_quota(
    legacy_story_seed_path: Path,
) -> None:
    """Last night's 00:01 bedtime must not freeze tonight's prepare-for-bed.

    The 22:30-00:30 window wraps midnight, so its acceptance often lands on
    the next civil day.  Charging that acceptance to the new day used to
    exhaust ``max_per_local_day`` before the evening even began.
    """

    tz = ZoneInfo("Asia/Shanghai")
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    last_night_bedtime = SimpleNamespace(
        activity_kind="sleep.family_home_prepare_for_bed",
        status="completed",
        scheduled_window=None,
        authority_origin=SimpleNamespace(
            accepted_at=datetime(2026, 7, 20, 0, 1, tzinfo=tz).astimezone(UTC)
        ),
    )
    tonight = datetime(2026, 7, 20, 23, 0, tzinfo=tz).astimezone(UTC)

    offered = {
        item.opening.activity_kind
        for item in catalog.candidates_at(
            instant=tonight,
            wake_event_ref="event:clock:tonight",
            plans=(last_night_bedtime,),
            npcs=(),
        )
    }
    assert "sleep.family_home_prepare_for_bed" in offered

    # An acceptance genuinely made this evening still counts for today.
    tonight_bedtime = SimpleNamespace(
        activity_kind="sleep.family_home_prepare_for_bed",
        status="completed",
        scheduled_window=None,
        authority_origin=SimpleNamespace(
            accepted_at=datetime(2026, 7, 20, 22, 40, tzinfo=tz).astimezone(UTC)
        ),
    )
    still_offered = {
        item.opening.activity_kind
        for item in catalog.candidates_at(
            instant=datetime(2026, 7, 20, 23, 30, tzinfo=tz).astimezone(UTC),
            wake_event_ref="event:clock:tonight-later",
            plans=(tonight_bedtime,),
            npcs=(),
        )
    }
    assert "sleep.family_home_prepare_for_bed" not in still_offered


@pytest.mark.asyncio
async def test_post_midnight_scheduler_ticks_produce_a_real_lived_sleep_event(
    tmp_path: Path,
    legacy_story_seed_path: Path,
) -> None:
    start = datetime(2026, 7, 20, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "post-midnight-life.sqlite",
        config=_config(legacy_story_seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        now=start,
    )
    previous = start
    try:
        for phase, local in (
            ("plan", datetime(2026, 7, 20, 1, 5)),
            ("start", datetime(2026, 7, 20, 1, 20)),
            # Ordinary completion tracks the accepted 40-minute window
            # (01:05-01:45): a wake before the window closes may pause or
            # abandon but must not finish the activity early.
            ("settle", datetime(2026, 7, 20, 1, 50)),
        ):
            at = local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
            await app.tick(
                tick_id=f"post-midnight:{phase}",
                logical_time_from=previous,
                logical_time_to=at,
                observed_at=at,
                trace_id=f"trace:post-midnight:{phase}",
                causation_id="scheduler:test",
                correlation_id="correlation:post-midnight",
                reason="night-coverage-test",
            )
            previous = at

        projection = app._ledger.project()  # noqa: SLF001
        assert len(projection.plans) == 1
        assert projection.plans[0].activity_kind == "sleep.family_home_late_wind_down"
        assert projection.plans[0].status == "completed"
        assert len(projection.world_occurrences) == 1
        assert projection.world_occurrences[0].status == "settled"
        assert len(projection.experiences) == 1
        assert projection.photo_candidates == ()
    finally:
        app.close()


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("this production ecology test does not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("this production ecology test does not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("this production ecology test does not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("life author must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _SelectingLifeModel:
    model = "test-life-author"

    def __init__(self) -> None:
        self.author_calls = 0
        self.lifecycle_calls = 0
        self.last_author_system: str | None = None
        self.last_author_payload: dict[str, object] | None = None
        self.last_lifecycle_system: str | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        capsule = json.loads(messages[-1]["content"])
        if "candidate" in capsule:
            self.author_calls += 1
            self.last_author_system = messages[0]["content"]
            self.last_author_payload = capsule
            return json.dumps(
                {"decision": "select", "candidate_token": capsule["candidate"]["token"]}
            )
        self.lifecycle_calls += 1
        self.last_lifecycle_system = messages[0]["content"]
        return '{"decision":"no_op"}'


class _FailingLifeModel(_SelectingLifeModel):
    def __init__(self, failure: Exception) -> None:
        super().__init__()
        self.failure = failure

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del messages, temperature
        raise self.failure


class _CursorRacingLifeModel(_SelectingLifeModel):
    def __init__(self) -> None:
        super().__init__()
        self.ledger = None
        self.injected = False

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        capsule = json.loads(messages[-1]["content"])
        if "candidate" in capsule and not self.injected:
            assert self.ledger is not None
            self.injected = True
            projection = self.ledger.project()
            payload = {
                "observation_id": "operator:life-author-context-race",
                "observation_hash": "a" * 64,
            }
            event = WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:operator:life-author-context-race",
                world_id=projection.world_id,
                event_type="OperatorObservationRecorded",
                logical_time=projection.logical_time,
                created_at=projection.logical_time,
                actor="operator:test",
                source="test:life-author-context-race",
                trace_id="trace:life-author-context-race",
                causation_id="cause:life-author-context-race",
                correlation_id="correlation:life-author-context-race",
                idempotency_key=(
                    domain_idempotency_key(
                        event_type="OperatorObservationRecorded",
                        world_id=projection.world_id,
                        payload=payload,
                    )
                    or "operator-observation:life-author-context-race"
                ),
                payload=payload,
            )
            self.ledger.commit(
                (event,),
                expected_world_revision=projection.world_revision,
                expected_deliberation_revision=projection.deliberation_revision,
            )
        return await super().complete(messages, temperature=temperature)


class _SelectingAuthorAndLifecycleModel(_SelectingLifeModel):
    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        capsule = json.loads(messages[-1]["content"])
        if "candidate" in capsule:
            return await super().complete(messages, temperature=temperature)
        self.lifecycle_calls += 1
        openings = capsule.get("openings", [])
        if not openings:
            return '{"decision":"no_op"}'
        selected = openings[1] if self.lifecycle_calls > 1 and len(openings) > 1 else openings[0]
        return json.dumps({"decision": "select", "opening_token": selected["opening_token"]})


class _SelectingOutcomeModel:
    model = "test-life-outcome-selection"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        options = json.loads(messages[-1]["content"])["candidates"]
        return json.dumps(
            {
                "candidate_result_ref": options[-1]["candidate_result_ref"],
                "adopt_proposed_life_direction": False,
            }
        )


def _seed(path: Path) -> Path:
    path.write_text(
        """
world_id: reviewed-test-world
life_author_catalog:
  version: reviewed-life.1
  openings:
    - id: morning-reading
      activity_kind: study.reading
      source: routine
      domain: study_class
      social_shape: alone
      deviation: persist
      visual_potential: object
      privacy: personal
      local_windows: ["07:00-12:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 45
      importance_bp: 4200
""".strip(),
        encoding="utf-8",
    )
    return path


def _social_seed(path: Path) -> Path:
    path.write_text(
        """
world_id: reviewed-test-world
life_author_catalog:
  version: reviewed-life.2
  locations:
    - id: campus-library
      location_ref: location:campus-library
      privacy: shareable
      local_windows: ["07:00-22:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  npcs:
    - id: fan-yuan
      npc_id: fan-yuan
      stable_identity_ref: reviewed-person:fan-yuan
      known_trait_refs: [trait:literature-club]
      privacy: personal
      location_id: campus-library
      local_windows: ["08:00-09:30"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  openings:
    - id: reading-list
      activity_kind: social.reading_list
      source: social
      domain: family_roommate_friend
      social_shape: npc
      npc_id: fan-yuan
      location_id: campus-library
      deviation: persist
      visual_potential: social
      privacy: personal
      local_windows: ["08:00-12:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 45
      importance_bp: 5200
      outcomes:
        - id: list-felt-easy
          text: 和范予安把读书会书单顺了一遍，聊得比预想中轻松。
          privacy: personal
        - id: list-had-friction
          text: 和范予安核对书单时有点分歧，不过最后还是整理清楚了。
          privacy: personal
""".strip(),
        encoding="utf-8",
    )
    return path


def _config(seed_path: Path) -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:life-author-production",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:life-author-production",
        local_timezone="Asia/Shanghai",
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed_path),
    )


def test_open_life_catalog_can_install_without_legacy_story_candidates(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "open-life-affordances.yaml"
    seed.write_text(
        """
world_id: open-life-affordances
life_author_catalog:
  version: open-life.1
  locations:
    - id: shanghai-home
      location_ref: location:shanghai-home
      privacy: private
      local_windows: ["00:00-23:59"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  npcs: []
  openings: []
  future_openings: []
  npc_initiated_events: []
  aspiration_seeds: []
""".strip(),
        encoding="utf-8",
    )

    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=seed,
        chronology=LocalChronology("Asia/Shanghai"),
    )

    assert catalog.candidates_at(
        instant=NOW,
        wake_event_ref="event:clock:open-life",
        plans=(),
    ) == ()
    assert catalog.reviewed_future_openings == ()
    assert catalog.reviewed_npc_initiated_events == ()
    assert catalog.reviewed_aspiration_seeds == ()
    assert tuple(item.location_ref for item in catalog.reviewed_locations) == (
        "location:shanghai-home",
    )


def test_production_story_candidates_are_explicitly_legacy_replay_fixtures() -> None:
    import yaml

    raw = yaml.safe_load(Path("configs/world_seed.yaml").read_text(encoding="utf-8"))
    assert (
        raw["life_author_catalog"]["story_candidate_role"]
        == "legacy_replay_and_fixture"
    )


def test_production_seed_does_not_contain_new_authored_job_travel_or_home_plots() -> None:
    import yaml

    raw = yaml.safe_load(Path("configs/world_seed.yaml").read_text(encoding="utf-8"))
    catalog = raw["life_author_catalog"]
    ids = {
        item["id"]
        for field in ("openings", "future_openings")
        for item in catalog[field]
    }
    assert not ids & {
        "family-home-morning-settle",
        "family-home-prepare-for-bed",
        "family-home-late-wind-down",
        "family-home-early-wake",
        "shanghai-home-evening-settle",
        "publishing-intern-shift",
        "publishing-editor-check-in",
        "publishing-intern-interview",
        "graduate-job-search",
        "junior-editor-workday",
        "city-publisher-editor-workday",
        "future-contextual-destination-research",
        "future-jiaxing-homecoming",
    }
    assert {
        "editor-qin",
        "recruiter-he",
        "senior-editor-luo",
    }.isdisjoint(item["npc_id"] for item in catalog["npcs"])


def test_reviewed_candidates_compile_soft_daypart_fit_from_local_window(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "daypart-seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )

    edge = catalog.candidates_at(
        instant=datetime(2026, 7, 17, 7, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC),
        wake_event_ref="event:clock:edge",
        plans=(),
    )[0]
    middle = catalog.candidates_at(
        instant=datetime(2026, 7, 17, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC),
        wake_event_ref="event:clock:middle",
        plans=(),
    )[0]

    assert edge.daypart_fit_bp == 6_000
    assert middle.daypart_fit_bp == 10_000


def test_legacy_story_catalog_proves_every_opening_has_a_joint_availability_witness(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )

    report = catalog.reachability_report()

    # reviewed-life.16: 43 present-moment openings, 11 future openings (one
    # shared_private invitation), and 8 NPC-initiated events (each must have
    # a legal in-presence start).
    assert len(report) == 64
    assert all(item.reachable for item in report)
    assert all(item.witness_weekday is not None for item in report)
    assert all(item.witness_minute is not None for item in report)
    assert {item.opening_id for item in report} == {
        "settle-morning-routine",
        "prepare-for-bed",
        "family-home-morning-settle",
        "family-home-prepare-for-bed",
        "focused-reading",
        "make-a-drink",
        "edit-photo-notes",
        "short-walk",
        "tidy-small-things",
        "quiet-recovery",
        "unhurried-digital-leisure",
        "literature-club-reading-list",
        "late-night-wind-down",
        "early-morning-wake",
        "family-home-late-wind-down",
        "family-home-early-wake",
        "shanghai-home-evening-settle",
        "write-reading-notes",
        "attend-lecture",
        "publishing-intern-shift",
        "publishing-editor-check-in",
        "publishing-intern-interview",
        "graduate-job-search",
        "junior-editor-workday",
        "city-publisher-editor-workday",
        "essay-deadline-push",
        "library-self-study",
        "write-short-essay",
        "scan-film-photos",
        "write-diary",
        "do-laundry",
        "pick-up-parcel",
        "buy-fruit-snacks",
        "canteen-meal",
        "dorm-cooking-experiment",
        "evening-stretch",
        "listen-podcast",
        "window-daydream",
        "afternoon-nap",
        "call-home-bookstore",
        "roommate-evening-chat",
        "literature-club-admin",
        "campus-cycling",
        "print-shop-run",
        "browse-old-book-stall",
        "future-literature-club-meetup",
        "future-lakeside-walk",
        "future-photo-batch-sort",
        "future-shared-movie-call",
        "future-jiaxing-bookstore-help",
        "future-fanyuan-exhibition",
        "future-bund-night-photo",
        "future-library-seminar-room",
        "future-book-market-hunt",
        "future-contextual-destination-research",
        "future-jiaxing-homecoming",
        "npc-fan-yuan-borrow-book",
        "npc-fan-yuan-impromptu-reading-list",
        "npc-fan-yuan-reading-list-disagreement",
        "npc-fan-yuan-share-manuscript",
        "npc-fan-yuan-lecture-pull",
        "npc-fan-yuan-book-recommend",
        "npc-lin-wan-late-snack",
        "npc-lin-wan-borrow-charger",
    }


def test_phase_specific_work_npcs_participate_in_legacy_fixture_openings(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    active_npcs = tuple(
        SimpleNamespace(npc_id=item.npc_id, status="active")
        for item in catalog.reviewed_npcs
    )
    internship_at = datetime(
        2026, 7, 17, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    internship = catalog.candidates_at(
        instant=internship_at,
        wake_event_ref="event:clock:internship-npc",
        plans=(),
        npcs=active_npcs,
        life_arcs=(
            SimpleNamespace(
                arc_id="life-arc:test:publishing",
                status="active",
                started_at=internship_at - timedelta(days=1),
                ends_at=internship_at + timedelta(days=29),
                context_tags=("role:intern", "workplace:publishing"),
            ),
        ),
    )
    editor_opening = next(
        item
        for item in internship
        if item.opening.id == "publishing-editor-check-in"
    )

    assert editor_opening.participant_ref == "npc:editor-qin"
    assert editor_opening.opening.requires_all_context_tags == (
        "role:intern",
        "workplace:publishing",
    )

    graduated_at = datetime(
        2028, 7, 3, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    graduated = catalog.candidates_at(
        instant=graduated_at,
        wake_event_ref="event:clock:graduated-recruiter",
        plans=(),
        npcs=active_npcs,
    )
    recruiter_opening = next(
        item for item in graduated if item.opening.id == "graduate-job-search"
    )

    assert recruiter_opening.participant_ref == "npc:recruiter-he"
    assert recruiter_opening.opening.requires_all_context_tags == (
        "academic:graduated",
    )
    assert recruiter_opening.opening.excludes_context_tags == (
        "role:junior_editor",
        "workplace:city_publisher",
    )


def test_legacy_fixture_can_plan_jiaxing_homecoming_without_already_being_there(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(
        2028, 7, 6, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)

    present = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:shanghai-homecoming",
        plans=(),
    )
    future = catalog.future_candidates_at(
        instant=instant,
        plans=(),
    )

    assert all(
        item.opening.id != "family-home-morning-settle" for item in present
    )
    homecoming = next(
        item for item in future if item.opening.id == "future-jiaxing-homecoming"
    )
    assert homecoming.location_ref == "location:jiaxing-family-home"
    assert homecoming.opening.location_id == "jiaxing-family-home"


def test_legacy_fixture_has_a_graduated_shanghai_home_opening(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(
        2028, 7, 3, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)

    candidates = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:graduated-shanghai-home",
        plans=(),
    )
    home = next(
        item for item in candidates if item.opening.id == "shanghai-home-evening-settle"
    )

    assert home.location_ref == "location:shanghai-home"
    assert home.opening.requires_all_context_tags == ("residence:shanghai_home",)


def test_legacy_fixture_continues_completed_junior_editor_stage(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(
        2029, 1, 10, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")
    ).astimezone(UTC)
    completed_arc = SimpleNamespace(
        arc_id="life-arc:city-publisher-junior-editor",
        status="completed",
        context_pack_ref="life-context:city-publisher-junior-editor",
        context_tags=("role:junior_editor", "workplace:city_publisher"),
        started_at=instant - timedelta(days=181),
        ends_at=instant - timedelta(days=1),
        closed_at=instant - timedelta(days=1),
    )
    active_npcs = tuple(
        SimpleNamespace(npc_id=item.npc_id, status="active")
        for item in catalog.reviewed_npcs
    )

    context = catalog.biographical_context_at(
        instant=instant,
        life_arcs=(completed_arc,),
    )
    candidates = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:post-junior-editor",
        plans=(),
        npcs=active_npcs,
        life_arcs=(completed_arc,),
    )
    opening_ids = {item.opening.id for item in candidates}

    assert {"role:editor", "workplace:city_publisher"} <= set(
        context.context_tags
    )
    assert "graduate-job-search" not in opening_ids
    assert "city-publisher-editor-workday" in opening_ids


def test_catalog_reports_an_opening_whose_authorities_never_overlap(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "unreachable-seed.yaml"
    seed.write_text(
        """
world_id: unreachable-test
life_author_catalog:
  version: unreachable.1
  locations:
    - id: library
      location_ref: location:library
      privacy: shareable
      local_windows: ["07:00-08:00"]
      weekdays: [0]
  npcs:
    - id: friend
      npc_id: friend
      stable_identity_ref: person:friend
      privacy: personal
      location_id: library
      local_windows: ["09:00-10:00"]
      weekdays: [0]
  openings:
    - id: social-reading
      activity_kind: social.reading
      source: social
      domain: family_roommate_friend
      social_shape: npc
      npc_id: friend
      location_id: library
      deviation: persist
      visual_potential: social
      privacy: personal
      local_windows: ["09:00-10:00"]
      weekdays: [0]
      duration_minutes: 30
      importance_bp: 4000
""".strip(),
        encoding="utf-8",
    )
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=seed, chronology=LocalChronology("Asia/Shanghai")
    )

    assert catalog.reachability_report()[0].model_dump() == {
        "opening_id": "social-reading",
        "activity_kind": "social.reading",
        "reachable": False,
        "witness_weekday": None,
        "witness_minute": None,
        "reason_code": "no_joint_reviewed_availability",
    }


@pytest.mark.asyncio
async def test_production_life_author_creates_one_clock_bound_abstract_plan_and_replays_once(
    tmp_path: Path,
) -> None:
    database = tmp_path / "life-author.sqlite"
    seed_path = _seed(tmp_path / "world-seed.yaml")
    model = _SelectingLifeModel()
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=_config(seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=NOW,
    )
    wake = "event:trigger:clock:life-author:1"
    semantic_before_restart = ""
    try:
        await app.tick(
            tick_id="life-author:1",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author:1",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author",
            reason="production-test",
        )
        projection = app._ledger.project()  # noqa: SLF001 - production seam assertion
        assert len(projection.plans) == 1
        plan = projection.plans[0]
        assert plan.activity_kind == "study.reading"
        assert plan.status == "planned"
        assert plan.location_ref is None
        assert plan.participant_refs == ()
        assert plan.evidence_refs[0].ref_id == wake
        assert plan.evidence_refs[0].evidence_type == "committed_world_event"
        assert plan.authority_origin is not None
        assert model.author_calls == 1
        assert "already verified its local-time window" in (model.last_author_system or "")
        assert "opportunity, not an instruction" in (model.last_author_system or "")
        assert model.last_author_payload is not None
        assert model.last_author_payload["authoritative_eligibility"]["logical_time"]
        assert projection.life_ecology_schedule is not None
        assert projection.life_ecology_schedule.last_wake_event_ref == wake
        assert projection.life_ecology_schedule.last_outcome_ref == "life-ecology:author_planned"

        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        assert [item.event.event_type for item in events].count("RandomDrawRecorded") == 1
        assert [item.event.event_type for item in events].count("LifeAuthorDecisionRecorded") == 1
        decision_record = LifeAuthorDecisionRecordedPayload.model_validate_json(
            next(
                item.event.payload_json
                for item in events
                if item.event.event_type == "LifeAuthorDecisionRecorded"
            )
        )
        assert decision_record.context_identity_version == "life-author-context.1"
        assert decision_record.context_capsule_id is not None
        assert decision_record.context_model_content_hash is not None
        assert decision_record.context_snapshot_hash is not None
        assert decision_record.context_cursor is not None
        assert model.last_author_payload is not None
        presented_context = json.dumps(
            model.last_author_payload["current_character_context"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        assert (
            decision_record.context_model_content_hash
            == hashlib.sha256(presented_context.encode("utf-8")).hexdigest()
        )
        assert decision_record.context_cursor.ledger_sequence < projection.ledger_sequence
        draw_record = RandomDrawRecordedPayload.model_validate_json(
            next(
                item.event.payload_json
                for item in events
                if item.event.event_type == "RandomDrawRecorded"
            )
        )
        assert draw_record.sampler_version == "random-authority.2"
        assert draw_record.weight_policy_version == "life-author-weight.5"
        assert sum(item.weight_ppm for item in draw_record.weight_vector) == 1_000_000
        assert draw_record.weight_vector_hash is not None
        planned = next(item.event for item in events if item.event.event_type == "ActivityPlanned")
        assert planned.causation_id == wake

        await app.tick(
            tick_id="life-author:2",
            logical_time_from=NOW + timedelta(hours=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=15),
            observed_at=NOW + timedelta(hours=1, minutes=15),
            trace_id="trace:life-author:2",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author",
            reason="production-test",
        )
        assert len(app._ledger.project().plans) == 1  # noqa: SLF001
        assert model.author_calls == 1
        assert "accepted life plan can actually progress" in (model.last_lifecycle_system or "")
        semantic_before_restart = app._ledger.project().semantic_hash  # noqa: SLF001
    finally:
        app.close()

    restarted_model = _SelectingLifeModel()
    restarted = build_sqlite_world_v2_turn_application(
        path=database,
        config=_config(seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=restarted_model,
        now=NOW,
    )
    try:
        joined = await restarted.advance_life_ecology_once(
            wake_event_ref=wake,
            trace_id="trace:life-author:restart",
            correlation_id="correlation:life-author",
        )
        assert joined.status == "joined_existing"
        assert joined.reason_code == "life_ecology.run_completed"
        assert restarted_model.author_calls == 0
        assert len(restarted._ledger.project().plans) == 1  # noqa: SLF001
        assert restarted._ledger.project().semantic_hash == semantic_before_restart  # noqa: SLF001
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_life_author_discards_model_result_when_pinned_context_cursor_changes(
    tmp_path: Path,
) -> None:
    model = _CursorRacingLifeModel()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "life-author-context-race.sqlite",
        config=_config(_seed(tmp_path / "world-seed-context-race.yaml")),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=NOW,
    )
    model.ledger = app._ledger  # noqa: SLF001 - deliberate concurrent writer
    try:
        await app.tick(
            tick_id="life-author:context-race",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author:context-race",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-context-race",
            reason="production-test",
        )

        projection = app._ledger.project()  # noqa: SLF001
        event_types = [
            item.event.event_type
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        ]
        assert model.author_calls == 1
        assert model.injected is True
        assert projection.plans == ()
        assert "LifeAuthorDecisionRecorded" not in event_types
        assert "ActivityPlanned" not in event_types
    finally:
        app.close()


@pytest.mark.asyncio
async def test_life_author_uses_companion_local_time_not_utc_hour(tmp_path: Path) -> None:
    database = tmp_path / "life-author-local-time.sqlite"
    seed_path = _seed(tmp_path / "world-seed.yaml")
    model = _SelectingLifeModel()
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=_config(seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=NOW,
    )
    try:
        # 01:00 UTC is 09:00 Asia/Shanghai and therefore eligible for 07:00-12:00.
        await app.tick(
            tick_id="local-morning",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:local-morning",
            causation_id="scheduler:life-author",
            correlation_id="correlation:local-morning",
            reason="production-test",
        )
        assert app._ledger.project().plans[0].activity_kind == "study.reading"  # noqa: SLF001
    finally:
        app.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "blocked"),
    [(ConnectionError("provider offline"), True), (RuntimeError("programming bug"), False)],
)
async def test_life_author_only_fail_closes_explicit_model_provider_failures(
    tmp_path: Path,
    failure: Exception,
    blocked: bool,
) -> None:
    seed_path = _seed(tmp_path / "world-seed.yaml")
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"life-author-failure-{blocked}.sqlite",
        config=_config(seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingLifeModel(),
        now=NOW,
    )
    ecology = app._life_ecology  # noqa: SLF001 - composition error-boundary assertion
    assert ecology is not None
    author = ecology._life_author_followup  # noqa: SLF001
    assert author is not None
    app._life_ecology = None  # noqa: SLF001 - commit only the public clock wake
    wake = "event:trigger:clock:life-author-failure"
    try:
        await app.tick(
            tick_id="life-author-failure",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author-failure",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-failure",
            reason="production-test",
        )
        author._model = _FailingLifeModel(failure)  # noqa: SLF001
        if blocked:
            result = await author.advance_once(
                wake_event_ref=wake,
                trace_id="trace:failure",
                correlation_id="correlation:failure",
            )
            assert (result.status, result.reason_code) == (
                "blocked",
                "life_author.model_unavailable",
            )
        else:
            with pytest.raises(RuntimeError, match="programming bug"):
                await author.advance_once(
                    wake_event_ref=wake,
                    trace_id="trace:failure",
                    correlation_id="correlation:failure",
                )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_life_author_bootstraps_reviewed_npc_and_atomically_binds_available_place(
    tmp_path: Path,
) -> None:
    database = tmp_path / "life-author-social.sqlite"
    seed_path = _social_seed(tmp_path / "world-seed-social.yaml")
    model = _SelectingLifeModel()
    config = _config(seed_path)
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=NOW,
    )
    semantic_before_restart = ""
    try:
        bootstrap = app._ledger.project()  # noqa: SLF001
        assert [(npc.npc_id, npc.current_location_ref) for npc in bootstrap.npcs] == [
            ("fan-yuan", "location:campus-library")
        ]

        await app.tick(
            tick_id="life-author-social",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author-social",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-social",
            reason="production-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        plan = projection.plans[0]
        assert plan.location_ref == "location:campus-library"
        assert plan.participant_refs == ("npc:fan-yuan",)
        assert all(item.evidence_type == "committed_world_event" for item in plan.evidence_refs)
        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        types = [item.event.event_type for item in events]
        assert types.count("NpcRegistered") == 1
        assert types.count("LifeAvailabilitySnapshotRecorded") == 1
        snapshot_index = types.index("LifeAvailabilitySnapshotRecorded")
        plan_index = types.index("ActivityPlanned")
        assert plan_index == snapshot_index + 1

        # The lifecycle catalog must consume this authority rather than report
        # the previously hard-coded location/NPC capability gap.
        await app.tick(
            tick_id="life-author-social-start",
            logical_time_from=NOW + timedelta(hours=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=1),
            observed_at=NOW + timedelta(hours=1, minutes=1),
            trace_id="trace:life-author-social-start",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-social",
            reason="production-test",
        )
        assert model.lifecycle_calls == 1
        semantic_before_restart = app._ledger.project().semantic_hash  # noqa: SLF001
    finally:
        app.close()

    restarted = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingLifeModel(),
        now=NOW,
    )
    try:
        projection = restarted._ledger.project()  # noqa: SLF001
        assert len(projection.npcs) == 1
        assert len(projection.plans) == 1
        assert projection.semantic_hash == semantic_before_restart
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_production_life_author_does_not_offer_reviewed_npc_outside_availability(
    tmp_path: Path,
) -> None:
    seed_path = _social_seed(tmp_path / "world-seed-unavailable.yaml")
    model = _SelectingLifeModel()
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "life-author-unavailable.sqlite",
        config=_config(seed_path),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=NOW,
    )
    try:
        # 02:00 UTC is 10:00 Asia/Shanghai: the opening and place are open,
        # but the reviewed NPC availability ended at 09:30.
        await app.tick(
            tick_id="life-author-unavailable",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=2),
            observed_at=NOW + timedelta(hours=2),
            trace_id="trace:life-author-unavailable",
            causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-unavailable",
            reason="production-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        assert projection.plans == ()
        assert len(projection.npcs) == 1
        assert model.author_calls == 0
        assert not any(
            item.event_type == "LifeAvailabilitySnapshotRecorded"
            for item in projection.committed_world_event_refs
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_life_aftermath_requires_later_wake_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "life-aftermath.sqlite"
    seed_path = _social_seed(tmp_path / "world-seed-aftermath.yaml")
    model = _SelectingAuthorAndLifecycleModel()
    outcome_model = _SelectingOutcomeModel()
    config = _config(seed_path)
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        outcome_draft_model=outcome_model,
        now=NOW,
    )
    semantic = ""
    try:
        await app.tick(
            tick_id="aftermath-plan",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:aftermath-plan",
            causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath",
            reason="production-test",
        )
        assert app._ledger.project().world_occurrences == ()  # noqa: SLF001

        await app.tick(
            tick_id="aftermath-start",
            logical_time_from=NOW + timedelta(hours=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=1),
            observed_at=NOW + timedelta(hours=1, minutes=1),
            trace_id="trace:aftermath-start",
            causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath",
            reason="production-test",
        )
        opened = app._ledger.project()  # noqa: SLF001
        assert opened.world_occurrences[0].status == "active"
        assert opened.experiences == ()

        # Ordinary completion tracks the accepted 45-minute window, so the
        # settling wake arrives only after that window has closed.
        await app.tick(
            tick_id="aftermath-settle",
            logical_time_from=NOW + timedelta(hours=1, minutes=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=46),
            observed_at=NOW + timedelta(hours=1, minutes=46),
            trace_id="trace:aftermath-settle",
            causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath",
            reason="production-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        occurrence = projection.world_occurrences[0]
        assert occurrence.status == "settled"
        # Ordinary objective aftermath is a world contingency: its frozen
        # weighted RandomAuthority draw is final and the character model
        # cannot override the result.
        assert outcome_model.calls == 0
        assert projection.outcome_proposals[0].decision_authority == (
            "recorded_world_draw"
        )
        assert occurrence.result_payload_ref is not None
        assert occurrence.settled_outcome_ref in {
            item.candidate_result_ref for item in occurrence.candidate_outcomes
        }
        assert occurrence.result_payload_ref == next(
            item.result_payload_ref
            for item in occurrence.candidate_outcomes
            if item.candidate_result_ref == occurrence.settled_outcome_ref
        )
        assert occurrence.activated_at == NOW + timedelta(hours=1, minutes=1)
        assert occurrence.settled_at == NOW + timedelta(hours=1, minutes=46)
        assert len(projection.experiences) == 1
        assert len(projection.life_content_descriptors) == 2
        assert {item.content_kind for item in projection.life_content_descriptors} == {
            "occurrence_result",
            "experience_summary",
        }
        assert any(
            item.process_kind == "npc_world_appraisal" and item.state == "open"
            for item in projection.trigger_processes
        )
        taxonomy = app.event_ecology_source_taxonomy()
        assert taxonomy
        result_taxon = next(item for item in taxonomy if item.category == "activity_result")
        assert result_taxon.event_source == "social"
        assert result_taxon.domain == "family_roommate_friend"
        assert result_taxon.social_shape == "npc"
        assert result_taxon.deviation == "persist"
        assert result_taxon.visual_potential == "social"
        assert all(item.source_event_refs for item in taxonomy)
        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        activated = next(
            item.event for item in events if item.event.event_type == "WorldOccurrenceActivated"
        )
        settled = next(
            item.event for item in events if item.event.event_type == "WorldOccurrenceSettled"
        )
        assert activated.logical_time < settled.logical_time
        semantic = projection.semantic_hash
    finally:
        app.close()

    memory_chat = _MemoryChat()
    restarted = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        now=NOW,
        memory_model=memory_chat,
    )
    try:
        assert restarted._ledger.project().semantic_hash == semantic  # noqa: SLF001
        assert len(restarted._ledger.project().experiences) == 1  # noqa: SLF001
        assert restarted._ledger.project().memory_candidates == ()  # noqa: SLF001
        aftermath = restarted._life_ecology._aftermath_followup  # noqa: SLF001
        lifecycle = aftermath._experience_memory_lifecycle  # noqa: SLF001
        wake_ref = next(
            item.event_id
            for item in reversed(
                restarted._ledger.project().committed_world_event_refs  # noqa: SLF001
            )
            if item.event_type == "ClockAdvanced"
        )
        original_accept = lifecycle.accept

        def crash_after_decision(**_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated crash after durable Experience-memory decision")

        lifecycle.accept = crash_after_decision
        with pytest.raises(RuntimeError, match="durable Experience-memory decision"):
            await aftermath.advance_once(
                wake_event_ref=wake_ref,
                trace_id="trace:aftermath-memory-decision-crash",
                correlation_id="correlation:aftermath",
            )
        lifecycle.accept = original_accept
        assert memory_chat.calls == 1
        assert restarted._ledger.project().memory_candidates == ()  # noqa: SLF001
        experience = restarted._ledger.project().experiences[0]  # noqa: SLF001
        assert (
            restarted._ledger.lookup_event_commit(  # noqa: SLF001
                experience_memory_decision_event_id(
                    experience_authority_event_ref=(experience.origin.accepted_event_ref)
                )
            )
            is not None
        )
    finally:
        restarted.close()

    resumed_memory = _NeverMemoryChat()
    resumed = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        now=NOW,
        memory_model=resumed_memory,
    )
    try:
        await resumed.tick(
            tick_id="aftermath-memory-backfill",
            logical_time_from=NOW + timedelta(hours=1, minutes=46),
            logical_time_to=NOW + timedelta(hours=1, minutes=56),
            observed_at=NOW + timedelta(hours=1, minutes=56),
            trace_id="trace:aftermath-memory-backfill",
            causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath",
            reason="production-test",
        )
        assert resumed_memory.calls == 0
        remembered = resumed._ledger.project().memory_candidates  # noqa: SLF001
        assert len(remembered) == 1
        assert remembered[0].values.status == "active"
        assert {binding.source_kind for binding in remembered[0].values.source_bindings} == {
            "experience"
        }
        memory_health = (await resumed.world_health_diagnostics())["mechanisms"]["memory"]
        assert memory_health["candidate_status_counts"] == {"active": 1}
        assert memory_health["candidate_source_counts"] == {"experience": 1}
        assert memory_health["last_candidate_transition_at"] is not None
    finally:
        resumed.close()


@pytest.mark.asyncio
async def test_experience_memory_no_change_is_durable_and_never_reclassified(
    tmp_path: Path,
) -> None:
    database = tmp_path / "life-aftermath-no-change.sqlite"
    seed_path = _social_seed(tmp_path / "world-seed-no-change.yaml")
    config = _config(seed_path)
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        outcome_draft_model=_SelectingOutcomeModel(),
        now=NOW,
    )
    try:
        for tick_id, start, end in (
            ("no-change-plan", NOW, NOW + timedelta(hours=1)),
            (
                "no-change-start",
                NOW + timedelta(hours=1),
                NOW + timedelta(hours=1, minutes=1),
            ),
            (
                "no-change-settle",
                NOW + timedelta(hours=1, minutes=1),
                NOW + timedelta(hours=1, minutes=46),
            ),
        ):
            await app.tick(
                tick_id=tick_id,
                logical_time_from=start,
                logical_time_to=end,
                observed_at=end,
                trace_id=f"trace:{tick_id}",
                causation_id="scheduler:life-author",
                correlation_id="correlation:no-change",
                reason="production-test",
            )
        assert len(app._ledger.project().experiences) == 1  # noqa: SLF001
    finally:
        app.close()

    memory_chat = _MemoryChat(retain=False)
    restarted = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        now=NOW,
        memory_model=memory_chat,
    )
    try:
        original_commit = restarted._ledger.commit  # noqa: SLF001
        injected_conflict = False

        def commit_winner_then_raise(events, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal injected_conflict
            if (
                not injected_conflict
                and len(events) == 1
                and events[0].event_type == "ExperienceMemoryDecisionRecorded"
            ):
                injected_conflict = True
                original_commit(events, **kwargs)
                raise IdempotencyConflict("simulated concurrent decision winner")
            return original_commit(events, **kwargs)

        restarted._ledger.commit = commit_winner_then_raise  # type: ignore[method-assign] # noqa: SLF001
        aftermath = restarted._life_ecology._aftermath_followup  # noqa: SLF001
        wake_ref = next(
            item.event_id
            for item in reversed(
                restarted._ledger.project().committed_world_event_refs  # noqa: SLF001
            )
            if item.event_type == "ClockAdvanced"
        )
        await asyncio.gather(
            aftermath.advance_once(
                wake_event_ref=wake_ref,
                trace_id="trace:no-change-memory:a",
                correlation_id="correlation:no-change",
            ),
            aftermath.advance_once(
                wake_event_ref=wake_ref,
                trace_id="trace:no-change-memory:b",
                correlation_id="correlation:no-change",
            ),
        )
        restarted._ledger.commit = original_commit  # type: ignore[method-assign] # noqa: SLF001
        await restarted.tick(
            tick_id="no-change-memory-again",
            logical_time_from=NOW + timedelta(hours=1, minutes=46),
            logical_time_to=NOW + timedelta(hours=1, minutes=56),
            observed_at=NOW + timedelta(hours=1, minutes=56),
            trace_id="trace:no-change-memory-again",
            causation_id="scheduler:life-author",
            correlation_id="correlation:no-change",
            reason="production-test",
        )
        assert memory_chat.calls == 1
        assert injected_conflict
        assert restarted._ledger.project().memory_candidates == ()  # noqa: SLF001
        experience = restarted._ledger.project().experiences[0]  # noqa: SLF001
        decision_event, _ = restarted._ledger.lookup_event_commit(  # noqa: SLF001
            experience_memory_decision_event_id(
                experience_authority_event_ref=(experience.origin.accepted_event_ref)
            )
        )
        assert decision_event.payload()["decision_kind"] == "no_change"
        memory_health = (await restarted.world_health_diagnostics())["mechanisms"]["memory"]
        assert memory_health["experience_decision_counts"] == {"no_change": 1}
        before_rebuild = restarted._ledger.project()  # noqa: SLF001
        rebuilt = restarted._ledger.rebuild()  # noqa: SLF001
        assert rebuilt.semantic_hash == before_rebuild.semantic_hash
        assert rebuilt.reducer_bundle_version == "world-v2-reducers.44"
    finally:
        restarted.close()

    never = _NeverMemoryChat()
    resumed = build_sqlite_world_v2_turn_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(),
        now=NOW,
        memory_model=never,
    )
    try:
        await resumed.tick(
            tick_id="no-change-after-restart",
            logical_time_from=NOW + timedelta(hours=1, minutes=56),
            logical_time_to=NOW + timedelta(hours=2, minutes=6),
            observed_at=NOW + timedelta(hours=2, minutes=6),
            trace_id="trace:no-change-after-restart",
            causation_id="scheduler:life-author",
            correlation_id="correlation:no-change",
            reason="production-test",
        )
        assert never.calls == 0
        assert resumed._ledger.project().memory_candidates == ()  # noqa: SLF001
    finally:
        resumed.close()
