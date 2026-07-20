"""Crystallization: a supported wish may become one concrete future plan."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)

# Friday 09:30 Asia/Shanghai: past the only present-moment opening, so quiet
# wakes reach the aspiration lane (same anchor as test_aspiration.py).
NOW = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("crystallization tests do not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("crystallization tests do not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("crystallization tests do not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("crystallization lane must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _LifeModel:
    """Bounded life-lane stub: selects wishes and crystallization slots."""

    model = "test-crystallization"

    def __init__(self, *, crystallize_decision: str = "select") -> None:
        self.crystallize_decision = crystallize_decision
        self.crystallize_calls = 0
        self.last_crystallize_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "aspiration_candidate" in capsule:
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["aspiration_candidate"]["token"],
            })
        if "crystallization_candidate" in capsule:
            self.crystallize_calls += 1
            self.last_crystallize_payload = capsule
            if self.crystallize_decision == "no_op":
                return '{"decision":"no_op"}'
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["crystallization_candidate"]["token"],
            })
        return '{"decision":"no_op"}'


_SEED = """
world_id: crystallization-test
life_author_catalog:
  version: reviewed-life-test.9
  locations:
    - id: dorm-room
      location_ref: location:dorm-room
      privacy: private
      local_windows: ["00:00-23:59"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  openings:
    - id: morning-reading
      activity_kind: study.reading
      source: routine
      domain: study_class
      social_shape: alone
      deviation: persist
      visual_potential: object
      privacy: private
      location_id: dorm-room
      local_windows: ["07:00-08:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 30
      importance_bp: 4000
  future_openings:
    - id: future-lakeside-walk
      activity_kind: commute.lakeside_walk
      source: intentional_goal
      domain: commute_walk
      social_shape: alone
      deviation: persist
      visual_potential: place
      privacy: private
      location_id: dorm-room
      local_windows: ["16:30-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 50
      importance_bp: 4800
      advance_days_min: 1
      advance_days_max: 5
  aspiration_seeds:
    - id: aspire-liwa-seasons
      text: 想把丽娃河的四季拍全，凑成一组自己的小相册。
      privacy: shareable
      base_chance_bp: 10000
      crystallizes_into: future-lakeside-walk
"""


def _seed(path: Path) -> Path:
    path.write_text(_SEED.strip(), encoding="utf-8")
    return path


def _build(tmp_path: Path, model: _LifeModel, *, name: str, **overrides):
    return build_sqlite_world_v2_turn_application(
        path=tmp_path / f"{name}.sqlite",
        config=WorldV2TurnApplicationConfig(
            world_id="world:crystallization",
            companion_actor_ref="agent:companion",
            reply_target="user:user.1",
            action_pump_owner="pump:crystallization",
            local_timezone="Asia/Shanghai",
            life_ecology=LifeEcologyComposition.production_v1(
                seed_catalog_path=_seed(tmp_path / "seed.yaml")
            ),
            # Deterministic recorded draw: the gate under test is the
            # daily-once identity/idempotency mechanics, not luck.
            aspiration_crystallize_chance_bp=10_000,
            **overrides,
        ),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=NOW,
    )


async def _tick(app, *, tick_id: str, frm: datetime, to: datetime) -> None:
    await app.tick(
        tick_id=tick_id, logical_time_from=frm, logical_time_to=to, observed_at=to,
        trace_id=f"trace:{tick_id}", causation_id="scheduler:crystallization",
        correlation_id="correlation:crystallization", reason="crystallization-test",
    )


def _crystallize_checks(app):  # type: ignore[no-untyped-def]
    return [
        item.event for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "ProposalRecorded"
        and item.event.payload().get("proposal_kind") == "aspiration_crystallization"
    ]


@pytest.mark.asyncio
async def test_supported_wish_crystallizes_into_an_evidence_bound_future_plan(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(tmp_path, model, name="crystallize")
    try:
        # Day 1 plants the wish (certain seed, model selects).
        await _tick(app, tick_id="c:plant", frm=NOW, to=NOW + timedelta(minutes=5))
        projection = app._ledger.project()  # noqa: SLF001
        assert len(projection.aspirations) == 1
        assert projection.aspirations[0].status == "active"
        planted_ref = projection.aspirations[0].planted_event_ref

        # Day 2: the daily crystallization check draws (certain), the model
        # confirms, and one atomic batch lands snapshot + plan + crystallized.
        day2 = NOW + timedelta(days=1)
        await _tick(app, tick_id="c:day2", frm=NOW + timedelta(minutes=5), to=day2)
        projection = app._ledger.project()  # noqa: SLF001
        aspiration = projection.aspirations[0]
        assert aspiration.status == "crystallized"
        assert aspiration.crystallized_plan_ref is not None
        plan_id = aspiration.crystallized_plan_ref.removeprefix("plan:")
        plan = next(item for item in projection.plans if item.plan_id == plan_id)
        assert plan.status == "planned"
        assert plan.activity_kind == "commute.lakeside_walk"
        assert plan.owner_actor_ref == "agent:companion"
        assert projection.logical_time is not None
        assert plan.scheduled_window.opens_at > projection.logical_time
        # The plan's evidence chain points back at the wish's planting event.
        assert planted_ref in {ref.ref_id for ref in plan.evidence_refs}
        crystallized_event = app._ledger.lookup_event_commit(  # noqa: SLF001
            next(
                item.event_id
                for item in projection.committed_world_event_refs
                if item.event_type == "AspirationCrystallized"
            )
        )
        assert crystallized_event is not None
        assert crystallized_event[0].payload()["plan_ref"] == aspiration.crystallized_plan_ref
        assert model.crystallize_calls == 1
        assert model.last_crystallize_payload is not None
        eligibility = model.last_crystallize_payload["authoritative_eligibility"]
        assert eligibility["planted_event_ref"] == planted_ref

        # Later wakes of the same day (and later days) converge: the wish is
        # terminal, so no second check, draw, model call, or plan appears.
        await _tick(
            app, tick_id="c:day2b", frm=day2, to=day2 + timedelta(hours=2)
        )
        await _tick(
            app, tick_id="c:day3",
            frm=day2 + timedelta(hours=2), to=day2 + timedelta(days=1),
        )
        projection = app._ledger.project()  # noqa: SLF001
        assert model.crystallize_calls == 1
        assert len(_crystallize_checks(app)) == 1
        assert len([
            item for item in projection.committed_world_event_refs
            if item.event_type == "AspirationCrystallized"
        ]) == 1
        assert len(projection.plans) == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_no_op_keeps_the_wish_and_consumes_the_daily_check(
    tmp_path: Path,
) -> None:
    model = _LifeModel(crystallize_decision="no_op")
    app = _build(tmp_path, model, name="declined")
    try:
        await _tick(app, tick_id="d:plant", frm=NOW, to=NOW + timedelta(minutes=5))
        day2 = NOW + timedelta(days=1)
        await _tick(app, tick_id="d:day2", frm=NOW + timedelta(minutes=5), to=day2)
        projection = app._ledger.project()  # noqa: SLF001
        assert projection.aspirations[0].status == "active"
        assert projection.plans == ()
        checks = _crystallize_checks(app)
        assert len(checks) == 1
        assert checks[0].payload()["decision"] == "no_op"
        # The consumed daily check never re-asks the model within the day.
        await _tick(app, tick_id="d:day2b", frm=day2, to=day2 + timedelta(hours=3))
        assert model.crystallize_calls == 1
        assert len(_crystallize_checks(app)) == 1
    finally:
        app.close()


def test_catalog_rejects_an_unknown_crystallization_target(tmp_path: Path) -> None:
    bad = _SEED.replace("crystallizes_into: future-lakeside-walk", "crystallizes_into: nowhere")
    path = tmp_path / "bad-seed.yaml"
    path.write_text(bad.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown future opening"):
        ReviewedLifeSeedCatalog.from_yaml(
            path=path, chronology=LocalChronology("Asia/Shanghai")
        )
