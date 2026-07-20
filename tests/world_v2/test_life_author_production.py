from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.random_authority import RandomDrawRecordedPayload
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
NOW = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)


def test_production_seed_offers_a_real_clock_bound_sleep_wake_opening() -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    local = datetime(2026, 7, 20, 23, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    candidates = catalog.candidates_at(
        instant=local.astimezone(UTC), wake_event_ref="event:clock:bedtime",
        plans=(), npcs=tuple(
            SimpleNamespace(npc_id=item.npc_id, status="active")
            for item in catalog.reviewed_npcs
        ),
    )

    sleep = next(
        item for item in candidates
        if item.opening.activity_kind == "sleep.prepare_for_bed"
    )
    assert sleep.opening.source == "routine"
    assert sleep.opening.domain == "sleep_wake"
    assert sleep.opening.visual_potential == "private_transition"
    assert sleep.opening.privacy == "private"
    assert len(sleep.opening.outcomes) == 2


def test_production_seed_has_continuous_reviewed_night_coverage_after_midnight() -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    active_npcs = tuple(
        SimpleNamespace(npc_id=item.npc_id, status="active")
        for item in catalog.reviewed_npcs
    )

    by_hour = {
        hour: {
            item.opening.activity_kind
            for item in catalog.candidates_at(
                instant=datetime(
                    2026, 7, 20, hour, 15, tzinfo=ZoneInfo("Asia/Shanghai")
                ).astimezone(UTC),
                wake_event_ref=f"event:clock:night:{hour}", plans=(), npcs=active_npcs,
            )
        }
        for hour in range(0, 8)
    }

    assert "sleep.prepare_for_bed" in by_hour[0]
    assert all("sleep.late_wind_down" in by_hour[hour] for hour in (1, 2, 3))
    assert all("sleep.early_morning_wake" in by_hour[hour] for hour in (4, 5, 6))
    assert "routine.morning_settle" in by_hour[7]


def test_after_midnight_bedtime_does_not_consume_the_next_evenings_quota() -> None:
    """Last night's 00:01 bedtime must not freeze tonight's prepare-for-bed.

    The 22:30-00:30 window wraps midnight, so its acceptance often lands on
    the next civil day.  Charging that acceptance to the new day used to
    exhaust ``max_per_local_day`` before the evening even began.
    """

    tz = ZoneInfo("Asia/Shanghai")
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    last_night_bedtime = SimpleNamespace(
        activity_kind="sleep.prepare_for_bed",
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
            instant=tonight, wake_event_ref="event:clock:tonight",
            plans=(last_night_bedtime,), npcs=(),
        )
    }
    assert "sleep.prepare_for_bed" in offered

    # An acceptance genuinely made this evening still counts for today.
    tonight_bedtime = SimpleNamespace(
        activity_kind="sleep.prepare_for_bed",
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
            plans=(tonight_bedtime,), npcs=(),
        )
    }
    assert "sleep.prepare_for_bed" not in still_offered


@pytest.mark.asyncio
async def test_post_midnight_scheduler_ticks_produce_a_real_lived_sleep_event(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 20, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / "post-midnight-life.sqlite",
        config=_config(Path("configs/world_seed.yaml")),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(), now=start,
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
                tick_id=f"post-midnight:{phase}", logical_time_from=previous,
                logical_time_to=at, observed_at=at,
                trace_id=f"trace:post-midnight:{phase}", causation_id="scheduler:test",
                correlation_id="correlation:post-midnight", reason="night-coverage-test",
            )
            previous = at

        projection = app._ledger.project()  # noqa: SLF001
        assert len(projection.plans) == 1
        assert projection.plans[0].activity_kind == "sleep.late_wind_down"
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
        return json.dumps({"candidate_result_ref": options[-1]["candidate_result_ref"]})


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


def test_reviewed_candidates_compile_soft_daypart_fit_from_local_window(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "daypart-seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )

    edge = catalog.candidates_at(
        instant=datetime(2026, 7, 17, 7, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC),
        wake_event_ref="event:clock:edge", plans=(),
    )[0]
    middle = catalog.candidates_at(
        instant=datetime(2026, 7, 17, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC),
        wake_event_ref="event:clock:middle", plans=(),
    )[0]

    assert edge.daypart_fit_bp == 6_000
    assert middle.daypart_fit_bp == 10_000


def test_production_catalog_proves_every_opening_has_a_joint_availability_witness() -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=Path("configs/world_seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )

    report = catalog.reachability_report()

    # reviewed-life.11: 34 present-moment openings, 9 future openings (one
    # shared_private invitation), and 8 NPC-initiated events (each must have
    # a legal in-presence start).
    assert len(report) == 51
    assert all(item.reachable for item in report)
    assert all(item.witness_weekday is not None for item in report)
    assert all(item.witness_minute is not None for item in report)
    assert {item.opening_id for item in report} == {
        "settle-morning-routine", "prepare-for-bed", "focused-reading",
        "make-a-drink", "edit-photo-notes", "short-walk",
        "tidy-small-things", "quiet-recovery",
        "unhurried-digital-leisure", "literature-club-reading-list",
        "late-night-wind-down", "early-morning-wake",
        "write-reading-notes", "attend-lecture", "essay-deadline-push",
        "library-self-study", "write-short-essay", "scan-film-photos",
        "write-diary", "do-laundry", "pick-up-parcel", "buy-fruit-snacks",
        "canteen-meal", "dorm-cooking-experiment", "evening-stretch",
        "listen-podcast", "window-daydream", "afternoon-nap",
        "call-home-bookstore", "roommate-evening-chat",
        "literature-club-admin", "campus-cycling", "print-shop-run",
        "browse-old-book-stall",
        "future-literature-club-meetup", "future-lakeside-walk",
        "future-photo-batch-sort", "future-shared-movie-call",
        "future-jiaxing-bookstore-help", "future-fanyuan-exhibition",
        "future-bund-night-photo", "future-library-seminar-room",
        "future-book-market-hunt",
        "npc-fan-yuan-borrow-book", "npc-fan-yuan-impromptu-reading-list",
        "npc-fan-yuan-reading-list-disagreement",
        "npc-fan-yuan-share-manuscript", "npc-fan-yuan-lecture-pull",
        "npc-fan-yuan-book-recommend", "npc-lin-wan-late-snack",
        "npc-lin-wan-borrow-charger",
    }


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
        assert "not a choice between having a life and staying empty" in (
            model.last_author_system or ""
        )
        assert model.last_author_payload is not None
        assert model.last_author_payload["authoritative_eligibility"]["logical_time"]
        assert next(
            item.runtime_outcome_ref
            for item in projection.trigger_processes
            if item.source_evidence_ref == wake
        ) == "life-ecology:author_planned"

        events = app._ledger.export_replay_evidence().events  # noqa: SLF001
        assert [item.event.event_type for item in events].count("RandomDrawRecorded") == 1
        assert [item.event.event_type for item in events].count("LifeAuthorDecisionRecorded") == 1
        draw_record = RandomDrawRecordedPayload.model_validate_json(next(
            item.event.payload_json
            for item in events if item.event.event_type == "RandomDrawRecorded"
        ))
        assert draw_record.sampler_version == "random-authority.2"
        assert draw_record.weight_policy_version == "life-author-weight.4"
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
        assert "accepted life plan can actually progress" in (
            model.last_lifecycle_system or ""
        )
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
        assert restarted_model.author_calls == 0
        assert len(restarted._ledger.project().plans) == 1  # noqa: SLF001
        assert restarted._ledger.project().semantic_hash == semantic_before_restart  # noqa: SLF001
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_life_author_uses_companion_local_time_not_utc_hour(tmp_path: Path) -> None:
    database = tmp_path / "life-author-local-time.sqlite"
    seed_path = _seed(tmp_path / "world-seed.yaml")
    model = _SelectingLifeModel()
    app = build_sqlite_world_v2_turn_application(
        path=database,
        config=_config(seed_path),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=NOW,
    )
    try:
        # 01:00 UTC is 09:00 Asia/Shanghai and therefore eligible for 07:00-12:00.
        await app.tick(
            tick_id="local-morning",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1),
            observed_at=NOW + timedelta(hours=1),
            trace_id="trace:local-morning", causation_id="scheduler:life-author",
            correlation_id="correlation:local-morning", reason="production-test",
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
    tmp_path: Path, failure: Exception, blocked: bool,
) -> None:
    seed_path = _seed(tmp_path / "world-seed.yaml")
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"life-author-failure-{blocked}.sqlite",
        config=_config(seed_path), identities=_Identities(), router=_Router(),
        main_model=_MainModel(), quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=_SelectingLifeModel(), now=NOW,
    )
    ecology = app._life_ecology  # noqa: SLF001 - composition error-boundary assertion
    assert ecology is not None
    author = ecology._life_author_followup  # noqa: SLF001
    assert author is not None
    app._life_ecology = None  # noqa: SLF001 - commit only the public clock wake
    wake = "event:trigger:clock:life-author-failure"
    try:
        await app.tick(
            tick_id="life-author-failure", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1), observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author-failure", causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-failure", reason="production-test",
        )
        author._model = _FailingLifeModel(failure)  # noqa: SLF001
        if blocked:
            result = await author.advance_once(
                wake_event_ref=wake, trace_id="trace:failure",
                correlation_id="correlation:failure",
            )
            assert (result.status, result.reason_code) == (
                "blocked", "life_author.model_unavailable"
            )
        else:
            with pytest.raises(RuntimeError, match="programming bug"):
                await author.advance_once(
                    wake_event_ref=wake, trace_id="trace:failure",
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
        path=database, config=config, identities=_Identities(), router=_Router(),
        main_model=_MainModel(), quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=NOW,
    )
    semantic_before_restart = ""
    try:
        bootstrap = app._ledger.project()  # noqa: SLF001
        assert [(npc.npc_id, npc.current_location_ref) for npc in bootstrap.npcs] == [
            ("fan-yuan", "location:campus-library")
        ]

        await app.tick(
            tick_id="life-author-social", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1), observed_at=NOW + timedelta(hours=1),
            trace_id="trace:life-author-social", causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-social", reason="production-test",
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
            tick_id="life-author-social-start", logical_time_from=NOW + timedelta(hours=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=1),
            observed_at=NOW + timedelta(hours=1, minutes=1),
            trace_id="trace:life-author-social-start", causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-social", reason="production-test",
        )
        assert model.lifecycle_calls == 1
        semantic_before_restart = app._ledger.project().semantic_hash  # noqa: SLF001
    finally:
        app.close()

    restarted = build_sqlite_world_v2_turn_application(
        path=database, config=config, identities=_Identities(), router=_Router(),
        main_model=_MainModel(), quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=_SelectingLifeModel(), now=NOW,
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
        path=tmp_path / "life-author-unavailable.sqlite", config=_config(seed_path),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=NOW,
    )
    try:
        # 02:00 UTC is 10:00 Asia/Shanghai: the opening and place are open,
        # but the reviewed NPC availability ended at 09:30.
        await app.tick(
            tick_id="life-author-unavailable", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=2), observed_at=NOW + timedelta(hours=2),
            trace_id="trace:life-author-unavailable", causation_id="scheduler:life-author",
            correlation_id="correlation:life-author-unavailable", reason="production-test",
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
        path=database, config=config, identities=_Identities(), router=_Router(),
        main_model=_MainModel(), quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, outcome_draft_model=outcome_model, now=NOW,
    )
    semantic = ""
    try:
        await app.tick(
            tick_id="aftermath-plan", logical_time_from=NOW,
            logical_time_to=NOW + timedelta(hours=1), observed_at=NOW + timedelta(hours=1),
            trace_id="trace:aftermath-plan", causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath", reason="production-test",
        )
        assert app._ledger.project().world_occurrences == ()  # noqa: SLF001

        await app.tick(
            tick_id="aftermath-start", logical_time_from=NOW + timedelta(hours=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=1),
            observed_at=NOW + timedelta(hours=1, minutes=1),
            trace_id="trace:aftermath-start", causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath", reason="production-test",
        )
        opened = app._ledger.project()  # noqa: SLF001
        assert opened.world_occurrences[0].status == "active"
        assert opened.experiences == ()

        # Ordinary completion tracks the accepted 45-minute window, so the
        # settling wake arrives only after that window has closed.
        await app.tick(
            tick_id="aftermath-settle", logical_time_from=NOW + timedelta(hours=1, minutes=1),
            logical_time_to=NOW + timedelta(hours=1, minutes=46),
            observed_at=NOW + timedelta(hours=1, minutes=46),
            trace_id="trace:aftermath-settle", causation_id="scheduler:life-author",
            correlation_id="correlation:aftermath", reason="production-test",
        )
        projection = app._ledger.project()  # noqa: SLF001
        occurrence = projection.world_occurrences[0]
        assert occurrence.status == "settled"
        assert outcome_model.calls == 1
        assert occurrence.result_payload_ref is not None
        assert occurrence.result_payload_ref.endswith(":list-had-friction")
        assert occurrence.activated_at == NOW + timedelta(hours=1, minutes=1)
        assert occurrence.settled_at == NOW + timedelta(hours=1, minutes=46)
        assert len(projection.experiences) == 1
        assert len(projection.life_content_descriptors) == 2
        assert {item.content_kind for item in projection.life_content_descriptors} == {
            "occurrence_result", "experience_summary"
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
        activated = next(item.event for item in events if item.event.event_type == "WorldOccurrenceActivated")
        settled = next(item.event for item in events if item.event.event_type == "WorldOccurrenceSettled")
        assert activated.logical_time < settled.logical_time
        semantic = projection.semantic_hash
    finally:
        app.close()

    restarted = build_sqlite_world_v2_turn_application(
        path=database, config=config, identities=_Identities(), router=_Router(),
        main_model=_MainModel(), quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=_SelectingAuthorAndLifecycleModel(), now=NOW,
    )
    try:
        assert restarted._ledger.project().semantic_hash == semantic  # noqa: SLF001
        assert len(restarted._ledger.project().experiences) == 1  # noqa: SLF001
    finally:
        restarted.close()
