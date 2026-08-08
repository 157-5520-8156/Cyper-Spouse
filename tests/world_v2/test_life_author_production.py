from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
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


class _RelationshipConsequenceMemoryChat:
    model = "test-experience-memory-relationship-consequence"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_experience_text: str | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        assert temperature == 0.15
        payload = json.loads(messages[-1]["content"])
        assert payload["source_kind"] == "companion_lived_experience"
        source_text = payload["verified_experience_text"]
        assert isinstance(source_text, str)
        assert ("多了一层信任" in source_text) or ("关系需要之后修复" in source_text)
        self.seen_experience_text = source_text
        return json.dumps(
            {
                "retain": True,
                "cue_kind": "relationship",
                "retention_rationales": [
                    "relationship_continuity",
                    "emotional_salience",
                ],
                "salience": {
                    "autobiographical_relevance_bp": 7600,
                    "relationship_relevance_bp": 9200,
                    "emotional_residue_bp": 8500,
                    "unfinished_business_bp": 5400,
                    "recurrence_bp": 2800,
                    "novelty_bp": 6100,
                    "future_utility_bp": 6800,
                    "world_continuity_bp": 7900,
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


class _InvalidOnceThenRetainingMemoryChat(_MemoryChat):
    """Fail one complete classify+correction attempt, then recover."""

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= 2:
            return '{"retain":true,"cue_kind":"not-installed"}'
        assert temperature == 0.15
        assert "lived Experience from your own life" in messages[0]["content"]
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


def _relationship_consequence_seed(path: Path) -> Path:
    _social_seed(path)
    source = path.read_text(encoding="utf-8")
    source = source.replace(
        "和范予安把读书会书单顺了一遍，聊得比预想中轻松。",
        "和范予安谈开了之前的误会，这次相处让她对范予安多了一层信任。",
    ).replace(
        "和范予安核对书单时有点分歧，不过最后还是整理清楚了。",
        "和范予安核对书单时争执起来；事情虽做完，她仍觉得委屈，关系需要之后修复。",
    )
    path.write_text(source, encoding="utf-8")
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

    assert (
        catalog.candidates_at(
            instant=NOW,
            wake_event_ref="event:clock:open-life",
            plans=(),
        )
        == ()
    )
    assert catalog.reviewed_future_openings == ()
    assert catalog.reviewed_npc_initiated_events == ()
    assert catalog.reviewed_aspiration_seeds == ()
    assert tuple(item.location_ref for item in catalog.reviewed_locations) == (
        "location:shanghai-home",
    )


def test_production_story_candidates_are_explicitly_legacy_replay_fixtures() -> None:
    import yaml

    raw = yaml.safe_load(Path("configs/world_seed.yaml").read_text(encoding="utf-8"))
    assert raw["life_author_catalog"]["story_candidate_role"] == "legacy_replay_and_fixture"


def test_production_seed_does_not_contain_new_authored_job_travel_or_home_plots() -> None:
    import yaml

    raw = yaml.safe_load(Path("configs/world_seed.yaml").read_text(encoding="utf-8"))
    catalog = raw["life_author_catalog"]
    ids = {item["id"] for field in ("openings", "future_openings") for item in catalog[field]}
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
        SimpleNamespace(npc_id=item.npc_id, status="active") for item in catalog.reviewed_npcs
    )
    internship_at = datetime(2026, 7, 17, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
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
        item for item in internship if item.opening.id == "publishing-editor-check-in"
    )

    assert editor_opening.participant_ref == "npc:editor-qin"
    assert editor_opening.opening.requires_all_context_tags == (
        "role:intern",
        "workplace:publishing",
    )

    graduated_at = datetime(2028, 7, 3, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
    graduated = catalog.candidates_at(
        instant=graduated_at,
        wake_event_ref="event:clock:graduated-recruiter",
        plans=(),
        npcs=active_npcs,
    )
    recruiter_opening = next(item for item in graduated if item.opening.id == "graduate-job-search")

    assert recruiter_opening.participant_ref == "npc:recruiter-he"
    assert recruiter_opening.opening.requires_all_context_tags == ("academic:graduated",)
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
    instant = datetime(2028, 7, 6, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)

    present = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:shanghai-homecoming",
        plans=(),
    )
    future = catalog.future_candidates_at(
        instant=instant,
        plans=(),
    )

    assert all(item.opening.id != "family-home-morning-settle" for item in present)
    homecoming = next(item for item in future if item.opening.id == "future-jiaxing-homecoming")
    assert homecoming.location_ref == "location:jiaxing-family-home"
    assert homecoming.opening.location_id == "jiaxing-family-home"


def test_legacy_fixture_has_a_graduated_shanghai_home_opening(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(2028, 7, 3, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)

    candidates = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:graduated-shanghai-home",
        plans=(),
    )
    home = next(item for item in candidates if item.opening.id == "shanghai-home-evening-settle")

    assert home.location_ref == "location:shanghai-home"
    assert home.opening.requires_all_context_tags == ("residence:shanghai_home",)


def test_legacy_fixture_continues_completed_junior_editor_stage(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    instant = datetime(2029, 1, 10, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)
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
        SimpleNamespace(npc_id=item.npc_id, status="active") for item in catalog.reviewed_npcs
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

    assert {"role:editor", "workplace:city_publisher"} <= set(context.context_tags)
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
