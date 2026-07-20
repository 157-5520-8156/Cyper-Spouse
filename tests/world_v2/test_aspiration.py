from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.aspiration_events import (
    AspirationCrystallizedPayload,
    AspirationReinforcedPayload,
)
from companion_daemon.world_v2.aspiration_reducers import (
    crystallize_aspiration,
    reinforce_aspiration,
)
from companion_daemon.world_v2.aspiration_runtime import (
    NOTHING_CANDIDATE_REF,
    AspirationWeightPolicy,
)
from companion_daemon.world_v2.aspiration_view import active_aspiration_advisories
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger_context_resolver import (
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import AspirationProjection

# 2026-07-17 is a Friday; 01:30 UTC is 09:30 Asia/Shanghai — past the only
# present-moment opening (07:00-08:00 local) with no future_openings and no
# NPC events, so scheduler wakes are quiet for every other life family and
# the aspiration lane gets its daily chance.
NOW = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("aspiration tests do not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("aspiration tests do not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("aspiration tests do not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("aspiration lane must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _LifeModel:
    """One stub for every bounded life lane, keyed by capsule shape."""

    model = "test-aspiration"

    def __init__(self, *, aspiration_decision: str = "select") -> None:
        self.aspiration_decision = aspiration_decision
        self.aspiration_calls = 0
        self.last_system: str | None = None
        self.last_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "aspiration_candidate" in capsule:
            self.aspiration_calls += 1
            self.last_system = messages[0]["content"]
            self.last_payload = capsule
            if self.aspiration_decision == "no_op":
                return '{"decision":"no_op"}'
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["aspiration_candidate"]["token"],
            })
        return '{"decision":"no_op"}'


_SEED_HEADER = """
world_id: aspiration-test
life_author_catalog:
  version: reviewed-life-test.7
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
"""

# One reviewed seed whose base chance fills the whole 10_000 probability
# space and needs no witness: the recorded draw then deterministically
# selects it, so tests exercise identity/idempotency mechanics instead of
# luck.  Production bases stay small (600-800bp); the probability gate itself
# is covered by the weight-policy test below.
_CERTAIN_SEED = """
  aspiration_seeds:
    - id: aspire-japan-trip
      text: 明年毕业想去日本玩一趟。
      privacy: personal
      base_chance_bp: 10000
"""

_WITNESS_GATED_SEED = """
  aspiration_seeds:
    - id: aspire-finish-reading
      text: 想把那份书单真正读完一遍。
      privacy: personal
      base_chance_bp: 10000
      requires_recent_activity_kinds: [study.reading]
"""

_THREE_SEEDS = """
  aspiration_seeds:
    - id: aspire-japan-trip
      text: 明年毕业想去日本玩一趟。
      privacy: personal
      base_chance_bp: 700
    - id: aspire-liwa-seasons
      text: 想把丽娃河的四季拍全。
      privacy: shareable
      base_chance_bp: 800
    - id: aspire-finish-reading
      text: 想把那份书单真正读完一遍。
      privacy: personal
      base_chance_bp: 600
"""


def _seed(path: Path, seeds: str = _CERTAIN_SEED) -> Path:
    path.write_text((_SEED_HEADER + seeds).strip(), encoding="utf-8")
    return path


def _config(seed_path: Path, **overrides) -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:aspiration",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:aspiration",
        local_timezone="Asia/Shanghai",
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed_path),
        **overrides,
    )


def _build(
    tmp_path: Path, seed_path: Path, model: _LifeModel, *, name: str,
    now: datetime = NOW, **overrides,
):
    return build_sqlite_world_v2_turn_application(
        path=tmp_path / f"{name}.sqlite",
        config=_config(seed_path, **overrides),
        identities=_Identities(), router=_Router(), main_model=_MainModel(),
        quick_recovery=_QuickRecovery(), transport=_Transport(),
        activity_lifecycle_model=model, now=now,
    )


async def _tick(app, *, tick_id: str, frm: datetime, to: datetime) -> None:
    await app.tick(
        tick_id=tick_id, logical_time_from=frm, logical_time_to=to, observed_at=to,
        trace_id=f"trace:{tick_id}", causation_id="scheduler:aspiration",
        correlation_id="correlation:aspiration", reason="aspiration-test",
    )


def _draw_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "RandomDrawRecorded"
        and item.event.source == "world-v2:aspiration-random"
    ]


def _check_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "ProposalRecorded"
        and item.event.payload().get("proposal_kind") == "aspiration"
    ]


@pytest.mark.asyncio
async def test_planting_is_idempotent_and_ledger_backed(tmp_path: Path) -> None:
    model = _LifeModel()
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="plant")
    try:
        await _tick(app, tick_id="plant:a", frm=NOW, to=NOW + timedelta(minutes=5))
        projection = app._ledger.project()  # noqa: SLF001 - production seam assertion
        assert len(projection.aspirations) == 1
        aspiration = projection.aspirations[0]
        assert aspiration.status == "active"
        assert aspiration.seed_id == "aspire-japan-trip"
        assert aspiration.text == "明年毕业想去日本玩一趟。"
        assert aspiration.owner_actor_ref == "agent:companion"
        assert aspiration.reinforcement_count == 0
        # The wish is ledger-backed: its planting event is committed authority.
        planted = app._ledger.lookup_event_commit(aspiration.planted_event_ref)  # noqa: SLF001
        assert planted is not None and planted[0].event_type == "AspirationPlanted"
        assert model.aspiration_calls == 1
        assert "aspiration seed" in (model.last_system or "")

        # Every later wake of the same local day converges on the consumed
        # check instead of re-rolling: no second draw, model call, or wish.
        await _tick(
            app, tick_id="plant:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=1),
        )
        await _tick(
            app, tick_id="plant:c",
            frm=NOW + timedelta(hours=1), to=NOW + timedelta(hours=6),
        )
        assert len(app._ledger.project().aspirations) == 1  # noqa: SLF001
        assert model.aspiration_calls == 1
        assert len(_draw_events(app)) == 1
        assert len(_check_events(app)) == 1

        # The next local day cannot replant an exhausted seed: no candidates
        # means no draw and no consumed check.
        await _tick(
            app, tick_id="plant:d",
            frm=NOW + timedelta(hours=6), to=NOW + timedelta(days=1),
        )
        assert len(app._ledger.project().aspirations) == 1  # noqa: SLF001
        assert len(_draw_events(app)) == 1
        assert len(_check_events(app)) == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_no_op_consumes_the_daily_check(tmp_path: Path) -> None:
    model = _LifeModel(aspiration_decision="no_op")
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="no-op")
    try:
        # The draw selects the certain seed; the model says "今天没有冒出什么
        # 念头" — always a legitimate answer that still consumes the day.
        await _tick(app, tick_id="noop:a", frm=NOW, to=NOW + timedelta(minutes=5))
        assert app._ledger.project().aspirations == ()  # noqa: SLF001
        assert model.aspiration_calls == 1
        checks = _check_events(app)
        assert len(checks) == 1
        assert checks[0].payload()["decision"] == "no_op"

        await _tick(
            app, tick_id="noop:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=2),
        )
        assert app._ledger.project().aspirations == ()  # noqa: SLF001
        assert model.aspiration_calls == 1
        assert len(_draw_events(app)) == 1
        assert len(_check_events(app)) == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_unmet_eligibility_never_consumes_the_check(tmp_path: Path) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, _seed(tmp_path / "seed.yaml", seeds=_WITNESS_GATED_SEED), model,
        name="eligibility",
    )
    try:
        # 09:30 local: the study.reading opening窗口 (07:00-08:00) already
        # passed, so no witness plan exists and the seed stays dormant — no
        # draw, no model call, no consumed check slot.
        await _tick(app, tick_id="gate:a", frm=NOW, to=NOW + timedelta(minutes=5))
        assert app._ledger.project().aspirations == ()  # noqa: SLF001
        assert model.aspiration_calls == 0
        assert _draw_events(app) == []
        assert _check_events(app) == []
    finally:
        app.close()


def test_weight_policy_keeps_planting_a_rare_recorded_gate(tmp_path: Path) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml", seeds=_THREE_SEEDS),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    seeds = {item.id: item for item in catalog.reviewed_aspiration_seeds}
    assert set(seeds) == {"aspire-japan-trip", "aspire-liwa-seasons", "aspire-finish-reading"}

    from companion_daemon.world_v2.aspiration_runtime import AspirationSeedCandidate

    candidates = tuple(
        AspirationSeedCandidate(token="a" * 63 + str(index), seed=seed)
        for index, seed in enumerate(seeds.values())
    )
    weights = AspirationWeightPolicy().compile(candidates=candidates)
    # Reviewed bases pass through untouched and the always-legal nothing
    # candidate absorbs the rest: planting stays a ~7% per-check event
    # (700+800+600 of 10_000), never a schedule.
    for candidate in candidates:
        assert weights[candidate.token] == candidate.seed.base_chance_bp
    assert weights[NOTHING_CANDIDATE_REF] == 10_000 - 700 - 800 - 600


@pytest.mark.asyncio
async def test_fade_requires_fourteen_idle_days_before_the_recorded_roll(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    # A certain fade chance isolates the idle-day gate: before 14 idle days
    # there must be no fade draw at all; on the first eligible check the
    # recorded roll then deterministically fades the wish.
    app = _build(
        tmp_path, _seed(tmp_path / "seed.yaml"), model, name="fade",
        aspiration_fade_chance_bp=10_000,
    )
    try:
        await _tick(app, tick_id="fade:plant", frm=NOW, to=NOW + timedelta(minutes=5))
        assert len(app._ledger.project().aspirations) == 1  # noqa: SLF001

        # Day 10: idle for only ~10 days — not fade-eligible, so the day's
        # check records no fade draw and the wish stays active.
        await _tick(
            app, tick_id="fade:day10",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(days=10),
        )
        projection = app._ledger.project()  # noqa: SLF001
        assert projection.aspirations[0].status == "active"
        assert not [
            event for event in _draw_events(app)
            if any(ref.startswith("fade:") for ref in event.payload()["candidate_refs"])
        ]

        # Day 15: past the 14-idle-day threshold, the certain roll fades it.
        await _tick(
            app, tick_id="fade:day15",
            frm=NOW + timedelta(days=10), to=NOW + timedelta(days=15),
        )
        faded = app._ledger.project().aspirations[0]  # noqa: SLF001
        assert faded.status == "faded"
        assert faded.faded_at is not None
        fade_draws = [
            event for event in _draw_events(app)
            if any(ref.startswith("fade:") for ref in event.payload()["candidate_refs"])
        ]
        assert len(fade_draws) == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_active_wish_is_visible_in_the_compiled_capsule(tmp_path: Path) -> None:
    model = _LifeModel()
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="capsule")
    try:
        await _tick(app, tick_id="capsule:a", frm=NOW, to=NOW + timedelta(minutes=5))
        projection = app._ledger.project()  # noqa: SLF001
        aspiration = projection.aspirations[0]

        advisories = active_aspiration_advisories(projection)
        assert len(advisories) == 1
        advisory = advisories[0]
        assert advisory.kind == "active_aspirations"
        # The advisory's only authority is the committed planting event.
        assert advisory.source_refs == (aspiration.planted_event_ref,)
        assert "明年毕业想去日本玩一趟。" in advisory.candidates[0].value
        assert len(advisory.candidates[0].value) <= 256

        # The Context Capsule re-verifies the source ref against committed
        # authority and surfaces the wish in the model-visible content: this
        # is the chat lane's exact injection path.
        compiler = context_capsule_compiler_from_ledger(ledger=app._ledger)  # noqa: SLF001
        query = query_from_projection(
            projection,
            actor_ref="agent:companion",
            trigger_ref=aspiration.planted_event_ref,
        )
        handle = compiler.compile_for_deliberation_with_advisories(query, advisories)
        assert "明年毕业想去日本玩一趟。" in handle.capsule.model_content_json
    finally:
        app.close()


@pytest.mark.asyncio
async def test_aspiration_lane_can_be_disabled_by_composition(tmp_path: Path) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, _seed(tmp_path / "seed.yaml"), model, name="disabled",
        aspiration_enabled=False,
    )
    try:
        await _tick(app, tick_id="disabled:a", frm=NOW, to=NOW + timedelta(hours=1))
        assert app._ledger.project().aspirations == ()  # noqa: SLF001
        assert model.aspiration_calls == 0
        assert _draw_events(app) == []
    finally:
        app.close()


def _active(planted_at: datetime, **overrides) -> AspirationProjection:
    values = {
        "aspiration_id": "aspiration:test",
        "entity_revision": 1,
        "owner_actor_ref": "agent:companion",
        "seed_id": "aspire-japan-trip",
        "text": "明年毕业想去日本玩一趟。",
        "privacy_class": "personal",
        "status": "active",
        "planted_at": planted_at,
        "planted_event_ref": "event:aspiration:planted:test",
        "source_event_ref": "event:witness",
        **overrides,
    }
    return AspirationProjection.model_validate(values)


def _evidence(ref_id: str) -> EvidenceRef:
    return EvidenceRef(
        ref_id=ref_id,
        evidence_type="committed_world_event",
        claim_purpose="past_experience",
        source_world_revision=1,
        immutable_hash="0" * 64,
    )


def test_reinforcement_reducer_counts_and_resets_the_idle_clock() -> None:
    planted = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    later = planted + timedelta(days=3)
    payload = AspirationReinforcedPayload(
        change_id="change:r1",
        transition_id="transition:r1",
        expected_entity_revision=1,
        evidence_refs=(_evidence("event:witness-2"),),
        aspiration_id="aspiration:test",
        reinforced_at=later,
        reinforcement_evidence_ref="event:witness-2",
    )
    updated = reinforce_aspiration((_active(planted),), payload, logical_time=later)
    assert updated[0].entity_revision == 2
    assert updated[0].reinforcement_count == 1
    assert updated[0].last_reinforced_at == later
    # Stale compare-and-swap and terminal states fail closed.
    with pytest.raises(ValueError, match="stale aspiration revision"):
        reinforce_aspiration((_active(planted),), payload.model_copy(
            update={"expected_entity_revision": 2}
        ), logical_time=later)


def test_crystallization_interface_requires_an_existing_plan() -> None:
    planted = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    later = planted + timedelta(days=30)
    payload = AspirationCrystallizedPayload(
        change_id="change:c1",
        transition_id="transition:c1",
        expected_entity_revision=1,
        evidence_refs=(_evidence("event:plan-accept"),),
        aspiration_id="aspiration:test",
        crystallized_at=later,
        plan_ref="plan:japan-trip",
    )
    with pytest.raises(ValueError, match="unknown plan"):
        crystallize_aspiration((_active(planted),), (), payload, logical_time=later)

    class _Plan:
        plan_id = "japan-trip"

    updated = crystallize_aspiration(
        (_active(planted),), (_Plan(),), payload, logical_time=later
    )
    assert updated[0].status == "crystallized"
    assert updated[0].crystallized_plan_ref == "plan:japan-trip"
