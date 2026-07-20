"""shared_private invitations: gate, plan, advisory, and expiry abandonment."""

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
from companion_daemon.world_v2.shared_private_invitation import (
    pending_shared_private_invitation_advisories,
)

# Friday 09:30 Asia/Shanghai (quiet-wake anchor shared with the other lanes).
NOW = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)
USER_REF = "user:user.1"


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invitation tests do not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invitation tests do not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("invitation tests do not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("invitation lane must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _LifeModel:
    model = "test-shared-private"

    def __init__(self, *, decision: str = "select") -> None:
        self.decision = decision
        self.invitation_calls = 0
        self.last_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "shared_private_candidate" in capsule:
            self.invitation_calls += 1
            self.last_payload = capsule
            if self.decision == "no_op":
                return '{"decision":"no_op"}'
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["shared_private_candidate"]["token"],
            })
        return '{"decision":"no_op"}'


_SEED = """
world_id: shared-private-test
life_author_catalog:
  version: reviewed-life-test.10
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
    - id: future-shared-movie-call
      activity_kind: shared.movie_call
      source: social
      domain: digital_leisure
      social_shape: shared_private
      deviation: persist
      visual_potential: none
      privacy: private
      location_id: dorm-room
      local_windows: ["20:00-22:30"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 120
      importance_bp: 5200
      advance_days_min: 1
      advance_days_max: 3
      requires_relationship_closeness_bp: 0
"""


def _seed(path: Path, *, closeness_floor: int = 0) -> Path:
    text = _SEED.replace(
        "requires_relationship_closeness_bp: 0",
        f"requires_relationship_closeness_bp: {closeness_floor}",
    )
    path.write_text(text.strip(), encoding="utf-8")
    return path


def _build(tmp_path: Path, model: _LifeModel, *, name: str, seed: Path, **overrides):
    return build_sqlite_world_v2_turn_application(
        path=tmp_path / f"{name}.sqlite",
        config=WorldV2TurnApplicationConfig(
            world_id="world:shared-private",
            companion_actor_ref="agent:companion",
            reply_target="user:user.1",
            counterpart_actor_ref=USER_REF,
            action_pump_owner="pump:shared-private",
            local_timezone="Asia/Shanghai",
            life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed),
            # Deterministic recorded draw; the semantics under test are the
            # gate, consent shape, identity, and expiry — not luck.
            shared_private_invite_chance_bp=10_000,
            **overrides,
        ),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=NOW,
    )


async def _tick(app, *, tick_id: str, frm: datetime, to: datetime) -> None:
    await app.tick(
        tick_id=tick_id, logical_time_from=frm, logical_time_to=to, observed_at=to,
        trace_id=f"trace:{tick_id}", causation_id="scheduler:shared-private",
        correlation_id="correlation:shared-private", reason="shared-private-test",
    )


def _shared_plans(app):  # type: ignore[no-untyped-def]
    return [
        plan for plan in app._ledger.project().plans  # noqa: SLF001
        if plan.participant_refs == (USER_REF,)
    ]


@pytest.mark.asyncio
async def test_invitation_plans_a_consent_shaped_user_activity_once_per_day(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, model, name="invite", seed=_seed(tmp_path / "seed.yaml")
    )
    try:
        await _tick(app, tick_id="i:a", frm=NOW, to=NOW + timedelta(minutes=5))
        plans = _shared_plans(app)
        assert len(plans) == 1
        plan = plans[0]
        assert plan.status == "planned"
        assert plan.activity_kind == "shared.movie_call"
        assert plan.privacy_class == "private"
        assert plan.participant_refs == (USER_REF,)
        projection = app._ledger.project()  # noqa: SLF001
        assert plan.scheduled_window.opens_at > projection.logical_time
        assert model.invitation_calls == 1
        eligibility = (model.last_payload or {})["authoritative_eligibility"]
        assert eligibility["participant_ref"] == USER_REF

        # The pending invitation is visible to the expression lanes as a
        # ledger-backed advisory (this is how "问出口" reaches the model).
        advisories = pending_shared_private_invitation_advisories(projection)
        assert len(advisories) == 1
        assert advisories[0].kind == "pending_shared_private_invitation"
        assert plan.authority_origin is not None
        assert advisories[0].source_refs == (plan.authority_origin.accepted_event_ref,)
        assert "shared.movie_call" in advisories[0].candidates[0].value

        # Same day converges; one pending invitation blocks a second ask even
        # on the next day.
        await _tick(
            app, tick_id="i:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=2),
        )
        await _tick(
            app, tick_id="i:c",
            frm=NOW + timedelta(hours=2), to=NOW + timedelta(days=1),
        )
        assert len(_shared_plans(app)) == 1
        assert model.invitation_calls == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_unanswered_invitation_is_abandoned_after_its_window(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, model, name="expiry", seed=_seed(tmp_path / "seed.yaml")
    )
    try:
        await _tick(app, tick_id="e:a", frm=NOW, to=NOW + timedelta(minutes=5))
        plan = _shared_plans(app)[0]
        closes_at = plan.scheduled_window.closes_at

        # Nobody answered and the reviewed window closed: the invitation is
        # deterministically withdrawn as an ordinary ActivityAbandoned.
        await _tick(
            app, tick_id="e:b", frm=NOW + timedelta(minutes=5), to=closes_at
        )
        await _tick(
            app, tick_id="e:c", frm=closes_at, to=closes_at + timedelta(minutes=30)
        )
        abandoned = next(
            item for item in app._ledger.project().plans  # noqa: SLF001
            if item.plan_id == plan.plan_id
        )
        assert abandoned.status == "abandoned"
        assert abandoned.terminal_reason_ref == "reason:shared-private-invitation-expired"
        # The withdrawn invitation no longer surfaces as pending texture (a
        # *new* day may legitimately plan a fresh ask; the certain test
        # chance makes that deterministic here).
        for advisory in pending_shared_private_invitation_advisories(
            app._ledger.project()  # noqa: SLF001
        ):
            assert plan.authority_origin is not None
            assert plan.authority_origin.accepted_event_ref not in advisory.source_refs
    finally:
        app.close()


@pytest.mark.asyncio
async def test_relationship_floor_gates_the_ask(tmp_path: Path) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, model, name="gated",
        seed=_seed(tmp_path / "seed.yaml", closeness_floor=9_000),
    )
    try:
        # A fresh world has no relationship warmth at all: below the reviewed
        # floor there is no candidate, no draw, and no model call.
        await _tick(app, tick_id="g:a", frm=NOW, to=NOW + timedelta(hours=1))
        assert _shared_plans(app) == []
        assert model.invitation_calls == 0
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_no_op_consumes_the_day_without_a_plan(tmp_path: Path) -> None:
    model = _LifeModel(decision="no_op")
    app = _build(
        tmp_path, model, name="no-op", seed=_seed(tmp_path / "seed.yaml")
    )
    try:
        await _tick(app, tick_id="n:a", frm=NOW, to=NOW + timedelta(minutes=5))
        assert _shared_plans(app) == []
        assert model.invitation_calls == 1
        await _tick(
            app, tick_id="n:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=3),
        )
        assert model.invitation_calls == 1
    finally:
        app.close()


def test_ordinary_future_author_never_sees_shared_private_openings(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    default_shapes = catalog.future_candidates_at(
        instant=NOW, plans=(),
    )
    assert default_shapes == ()
    invitation_only = catalog.future_candidates_at(
        instant=NOW, plans=(), social_shapes=frozenset({"shared_private"}),
    )
    assert invitation_only
    assert all(
        item.opening.social_shape == "shared_private" for item in invitation_only
    )


def test_catalog_rejects_unreviewed_shared_private_shapes(tmp_path: Path) -> None:
    # Present-moment openings must not claim shared_private.
    bad_present = _SEED.replace(
        "      social_shape: alone\n", "      social_shape: shared_private\n", 1
    )
    path = tmp_path / "bad-present.yaml"
    path.write_text(bad_present.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match="not reviewed for this catalog section"):
        ReviewedLifeSeedCatalog.from_yaml(
            path=path, chronology=LocalChronology("Asia/Shanghai")
        )

    # A shared_private future opening without a closeness floor fails closed.
    bad_floor = _SEED.replace("      requires_relationship_closeness_bp: 0\n", "")
    path = tmp_path / "bad-floor.yaml"
    path.write_text(bad_floor.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match="closeness floor"):
        ReviewedLifeSeedCatalog.from_yaml(
            path=path, chronology=LocalChronology("Asia/Shanghai")
        )
