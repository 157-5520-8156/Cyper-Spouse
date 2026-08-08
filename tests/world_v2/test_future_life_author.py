from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo


from companion_daemon.world_v2.future_life_weight_policy import (
    FutureLifeAuthorWeightPolicy,
)
from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
)

# 2026-07-17 is a Friday; 00:00 UTC is 08:00 Asia/Shanghai, one hour past the
# only present-moment opening window, so scheduler wakes are "quiet" for the
# present life author and the future calendar lane gets its chance.
NOW = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("future life author tests do not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("future life author tests do not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("future life author tests do not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("future life author must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _LifeModel:
    """One stub for all three bounded lanes, keyed by capsule shape."""

    model = "test-future-life-author"

    def __init__(self, *, future_decision: str = "select") -> None:
        self.future_decision = future_decision
        self.future_calls = 0
        self.present_calls = 0
        self.lifecycle_calls = 0
        self.last_future_system: str | None = None
        self.last_future_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "future_candidate" in capsule:
            self.future_calls += 1
            self.last_future_system = messages[0]["content"]
            self.last_future_payload = capsule
            if self.future_decision == "no_op":
                return '{"decision":"no_op"}'
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": capsule["future_candidate"]["token"],
                }
            )
        if "candidate" in capsule:
            self.present_calls += 1
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": capsule["candidate"]["token"],
                }
            )
        self.lifecycle_calls += 1
        openings = capsule.get("openings", [])
        # Progress a due plan (start/finish); never abandon a waiting one.
        for phrase in ("begin", "finish"):
            selected = next(
                (item for item in openings if str(item.get("safe_summary", "")).startswith(phrase)),
                None,
            )
            if selected is not None:
                return json.dumps(
                    {
                        "decision": "select",
                        "opening_token": selected["opening_token"],
                    }
                )
        return '{"decision":"no_op"}'


_SEED_HEADER = """
world_id: future-life-test
life_author_catalog:
  version: reviewed-life-test.5
  locations:
    - id: campus-library
      location_ref: location:campus-library
      privacy: shareable
      local_windows: ["08:00-22:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
    - id: campus-path
      location_ref: location:campus-path
      privacy: shareable
      local_windows: ["06:30-22:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  npcs:
    - id: fan-yuan
      npc_id: fan-yuan
      stable_identity_ref: reviewed-person:fan-yuan
      privacy: personal
      location_id: campus-library
      local_windows: ["09:00-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  openings:
    - id: morning-reading
      activity_kind: study.reading
      source: routine
      domain: study_class
      social_shape: alone
      deviation: persist
      visual_potential: object
      privacy: personal
      local_windows: ["07:00-08:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 30
      importance_bp: 4000
"""


def _seed(path: Path) -> Path:
    path.write_text(
        (
            _SEED_HEADER
            + """
  future_openings:
    - id: future-club-meetup
      activity_kind: social.club_meetup
      source: social
      domain: family_roommate_friend
      social_shape: npc
      npc_id: fan-yuan
      location_id: campus-library
      deviation: persist
      visual_potential: social
      privacy: personal
      local_windows: ["14:00-16:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 45
      importance_bp: 5000
      advance_days_min: 1
      advance_days_max: 3
      outcomes:
        - {id: meetup-good, text: 和范予安把社团接下来的安排理顺了。, privacy: personal}
        - {id: meetup-long, text: 碰头聊得比预计久，事情定了，人有点累。, privacy: personal}
    - id: future-lake-walk
      activity_kind: commute.lake_walk
      source: intentional_goal
      domain: commute_walk
      social_shape: alone
      deviation: persist
      visual_potential: place
      privacy: shareable
      location_id: campus-path
      local_windows: ["17:00-19:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 40
      importance_bp: 5000
      advance_days_min: 1
      advance_days_max: 3
      outcomes:
        - {id: walk-nice, text: 沿校园水边走了一段，风很舒服。, privacy: shareable}
        - {id: walk-short, text: 走到一半有点凉，提前折返了。, privacy: shareable}
"""
        ).strip(),
        encoding="utf-8",
    )
    return path


def _single_slot_seed(path: Path) -> Path:
    """Exactly one legal future slot: tomorrow 14:00-14:45 local."""

    path.write_text(
        (
            _SEED_HEADER
            + """
  future_openings:
    - id: future-club-meetup
      activity_kind: social.club_meetup
      source: social
      domain: family_roommate_friend
      social_shape: npc
      npc_id: fan-yuan
      location_id: campus-library
      deviation: persist
      visual_potential: social
      privacy: personal
      local_windows: ["14:00-16:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 45
      importance_bp: 5000
      advance_days_min: 1
      advance_days_max: 1
      outcomes:
        - {id: meetup-good, text: 和范予安把社团接下来的安排理顺了。, privacy: personal}
        - {id: meetup-long, text: 碰头聊得比预计久，事情定了，人有点累。, privacy: personal}
"""
        ).strip(),
        encoding="utf-8",
    )
    return path


def _config(seed_path: Path, **overrides) -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:future-life-author",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:future-life-author",
        local_timezone="Asia/Shanghai",
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed_path),
        **overrides,
    )


def _future_plans(projection):  # type: ignore[no-untyped-def]
    return [
        item for item in projection.plans if item.plan_id.startswith("plan:future-life-author:")
    ]


def test_future_plans_do_not_freeze_present_candidates(tmp_path: Path) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    # 07:30 local: inside the present morning-reading window.
    instant = datetime(2026, 7, 17, 7, 30, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)

    def plan(*, status: str, opens_in: timedelta) -> SimpleNamespace:
        return SimpleNamespace(
            status=status,
            activity_kind="social.club_meetup",
            participant_refs=("npc:fan-yuan",),
            scheduled_window=SimpleNamespace(
                opens_at=instant + opens_in,
                closes_at=instant + opens_in + timedelta(minutes=45),
            ),
            authority_origin=SimpleNamespace(accepted_at=instant - timedelta(hours=1)),
        )

    future = plan(status="planned", opens_in=timedelta(days=2))
    overlapping = plan(status="planned", opens_in=timedelta(minutes=10))
    active = plan(status="active", opens_in=timedelta(minutes=-10))

    with_future = catalog.candidates_at(
        instant=instant,
        wake_event_ref="event:clock:test",
        plans=(future,),
    )
    assert [item.opening.activity_kind for item in with_future] == ["study.reading"]
    assert (
        catalog.candidates_at(
            instant=instant,
            wake_event_ref="event:clock:test",
            plans=(overlapping,),
        )
        == ()
    )
    assert (
        catalog.candidates_at(
            instant=instant,
            wake_event_ref="event:clock:test",
            plans=(active,),
        )
        == ()
    )


def test_future_candidates_respect_the_seven_day_horizon_and_candidate_cap(
    tmp_path: Path,
) -> None:
    seed = tmp_path / "cap-seed.yaml"
    seed.write_text(
        (
            _SEED_HEADER
            + """
  future_openings:
    - id: future-many-slots
      activity_kind: errand.many_slots
      source: routine
      domain: errand_household
      social_shape: alone
      deviation: persist
      visual_potential: none
      privacy: private
      local_windows: ["09:00-10:00", "11:00-12:00", "13:00-14:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 30
      importance_bp: 4000
      advance_days_min: 1
      advance_days_max: 7
"""
        ).strip(),
        encoding="utf-8",
    )
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=seed, chronology=LocalChronology("Asia/Shanghai")
    )

    candidates = catalog.future_candidates_at(instant=NOW, plans=())

    # 3 windows x 7 days = 21 legal slots, deterministically capped at 16.
    assert len(candidates) == 16
    assert all(1 <= item.day_offset <= 7 for item in candidates)
    assert all(item.opens_at > NOW for item in candidates)
    # The cap keeps the nearest days rather than an arbitrary sample.
    assert [item.day_offset for item in candidates] == sorted(
        item.day_offset for item in candidates
    )


def test_mood_tilts_future_social_commitments_without_gating_them(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    candidates = catalog.future_candidates_at(
        instant=NOW,
        plans=(),
        npcs=(SimpleNamespace(npc_id="fan-yuan", status="active"),),
    )
    social = next(item for item in candidates if item.opening.id == "future-club-meetup")
    walk = next(item for item in candidates if item.opening.id == "future-lake-walk")
    policy = FutureLifeAuthorWeightPolicy()

    def episode(dimension: str) -> SimpleNamespace:
        return SimpleNamespace(
            status="active",
            components=(SimpleNamespace(dimension=dimension, intensity_bp=8_000),),
        )

    baseline = policy.compile(candidates=(social, walk))
    lonely = policy.compile(candidates=(social, walk), affect_episodes=(episode("loneliness"),))
    heavy = policy.compile(candidates=(social, walk), affect_episodes=(episode("sadness"),))

    # Loneliness reaches toward future company; non-lonely heaviness pulls
    # away from promising company days ahead.  Every candidate keeps positive
    # mass: mood is a tendency, never an eligibility rule.
    assert lonely[social.token] > baseline[social.token]
    assert heavy[social.token] < baseline[social.token]
    assert heavy[walk.token] < baseline[walk.token]
    assert all(value > 0 for value in (*lonely.values(), *heavy.values()))
