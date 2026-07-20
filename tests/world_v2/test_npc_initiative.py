from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.npc_initiative import (
    NOTHING_CANDIDATE_REF,
    NpcInitiativeWeightPolicy,
)
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)

# 2026-07-17 is a Friday; 01:30 UTC is 09:30 Asia/Shanghai — inside 范予安's
# reviewed presence window, past the only present-moment opening (07:00-08:00
# local), with no future_openings, so scheduler wakes are quiet for every
# other life family and the NPC-initiative lane gets its chance.
NOW = datetime(2026, 7, 17, 1, 30, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("npc initiative tests do not run a chat turn")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("npc initiative tests do not run a chat turn")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("npc initiative tests do not run a chat turn")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("npc initiative must not dispatch platform actions")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _LifeModel:
    """One stub for every bounded life lane, keyed by capsule shape."""

    model = "test-npc-initiative"

    def __init__(self, *, npc_decision: str = "select") -> None:
        self.npc_decision = npc_decision
        self.npc_calls = 0
        self.last_npc_system: str | None = None
        self.last_npc_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "npc_initiative_candidate" in capsule:
            self.npc_calls += 1
            self.last_npc_system = messages[0]["content"]
            self.last_npc_payload = capsule
            if self.npc_decision == "no_op":
                return '{"decision":"no_op"}'
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["npc_initiative_candidate"]["token"],
            })
        if "candidate" in capsule or "future_candidate" in capsule:
            return '{"decision":"no_op"}'
        return '{"decision":"no_op"}'


_SEED_HEADER = """
world_id: npc-initiative-test
life_author_catalog:
  version: reviewed-life-test.6
  locations:
    - id: campus-library
      location_ref: location:campus-library
      privacy: shareable
      local_windows: ["08:00-22:00"]
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

# One reviewed event whose base chance fills the whole 10_000 probability
# space: the recorded draw then deterministically selects the event, so tests
# exercise identity/settlement mechanics instead of luck.  Production bases
# stay small (600-1200bp); randomness itself is covered by the weight tests.
_CERTAIN_EVENT = """
  npc_initiated_events:
    - id: npc-borrow-book
      initiative_kind: small_favor
      npc_id: fan-yuan
      location_id: campus-library
      summary: 范予安忽然来找她借一本读书会提到的书。
      privacy: personal
      local_windows: ["09:30-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 10
      base_chance_bp: 10000
      outcomes:
        - {id: borrow-warm, text: 范予安忽然来借书，顺带聊了两句，气氛轻松。, privacy: personal}
        - {id: borrow-reluctant, text: 那本书她还没读完，犹豫了一下还是借了。, privacy: personal}
"""

_THREE_EVENTS = """
  npc_initiated_events:
    - id: npc-borrow-book
      initiative_kind: small_favor
      npc_id: fan-yuan
      location_id: campus-library
      summary: 范予安忽然来找她借一本读书会提到的书。
      privacy: personal
      local_windows: ["09:30-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 10
      base_chance_bp: 1200
      outcomes:
        - {id: borrow-warm, text: 范予安忽然来借书，顺带聊了两句，气氛轻松。, privacy: personal}
        - {id: borrow-reluctant, text: 那本书她还没读完，犹豫了一下还是借了。, privacy: personal}
    - id: npc-impromptu-list
      initiative_kind: shared_time
      npc_id: fan-yuan
      location_id: campus-library
      summary: 范予安临时拉她一起过一遍读书会书单。
      privacy: personal
      local_windows: ["10:00-12:00", "14:00-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 30
      base_chance_bp: 900
      outcomes:
        - {id: list-flowed, text: 临时对书单，聊开之后顺得出乎意料。, privacy: personal}
        - {id: list-drained, text: 临时看书单挤掉了原本的安排，人有点耗神。, privacy: personal}
    - id: npc-list-disagreement
      initiative_kind: friction
      npc_id: fan-yuan
      location_id: campus-library
      summary: 为一本书要不要进书单，和范予安起了点小分歧。
      privacy: personal
      local_windows: ["10:00-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 25
      base_chance_bp: 600
      outcomes:
        - {id: disagreement-cleared, text: 争了几句，反而把选书标准聊清楚了。, privacy: personal}
        - {id: disagreement-shelved, text: 谁都没说服谁，先搁下了。, privacy: personal}
"""


def _seed(path: Path, events: str = _CERTAIN_EVENT) -> Path:
    path.write_text((_SEED_HEADER + events).strip(), encoding="utf-8")
    return path


def _config(seed_path: Path, **overrides) -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id="world:npc-initiative",
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:npc-initiative",
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
        trace_id=f"trace:{tick_id}", causation_id="scheduler:npc-initiative",
        correlation_id="correlation:npc-initiative", reason="npc-initiative-test",
    )


def _npc_occurrences(projection):  # type: ignore[no-untyped-def]
    return [
        item for item in projection.world_occurrences
        if item.occurrence_id.startswith("occurrence:npc-initiative:")
    ]


def _npc_draw_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "RandomDrawRecorded"
        and item.event.source == "world-v2:npc-initiative-random"
    ]


def _npc_check_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "ProposalRecorded"
        and item.event.payload().get("proposal_kind") == "npc_initiative"
    ]


@pytest.mark.asyncio
async def test_npc_initiated_event_settles_into_appraisal_and_experience(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="end-to-end")
    try:
        await _tick(app, tick_id="e2e:occur", frm=NOW, to=NOW + timedelta(minutes=5))
        projection = app._ledger.project()  # noqa: SLF001 - production seam assertion
        occurrences = _npc_occurrences(projection)
        assert len(occurrences) == 1
        occurrence = occurrences[0]
        assert occurrence.status == "active"
        assert occurrence.location_ref == "location:campus-library"
        assert set(occurrence.participant_refs) == {"agent:companion", "npc:fan-yuan"}
        assert occurrence.visibility == "personal"
        assert model.npc_calls == 1
        assert "NPC-initiated moment" in (model.last_npc_system or "")
        assert model.last_npc_payload is not None
        eligibility = model.last_npc_payload["authoritative_eligibility"]
        assert eligibility["npc_ref"] == "npc:fan-yuan"

        # A later wake settles through the ordinary aftermath path: the
        # mandatory npc_world_appraisal trigger opens and the settled outcome
        # becomes a referencable Committed Experience plus life content.
        await _tick(
            app, tick_id="e2e:settle",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(minutes=30),
        )
        settled = app._ledger.project()  # noqa: SLF001
        occurrence = _npc_occurrences(settled)[0]
        assert occurrence.status == "settled"
        assert any(
            item.process_kind == "npc_world_appraisal" and item.state == "open"
            for item in settled.trigger_processes
        )
        assert len(settled.experiences) == 1
        assert any(
            item.source_kind == "occurrence_settlement"
            and item.source_entity_id == occurrence.occurrence_id
            for item in settled.life_content_descriptors
        )
        result = app._life_content_store.read_exact(  # noqa: SLF001
            content_ref=occurrence.result_payload_ref
        )
        assert result is not None
        assert result.content_payload_hash == occurrence.result_payload_hash
        assert result.text in {
            "范予安忽然来借书，顺带聊了两句，气氛轻松。",
            "那本书她还没读完，犹豫了一下还是借了。",
        }
    finally:
        app.close()


@pytest.mark.asyncio
async def test_at_most_two_checks_and_one_occurrence_per_local_day(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="daily-budget")
    try:
        # Morning slot: the certain event occurs on the first quiet wake.
        await _tick(app, tick_id="day1:a", frm=NOW, to=NOW + timedelta(minutes=5))
        assert len(_npc_occurrences(app._ledger.project())) == 1  # noqa: SLF001
        assert model.npc_calls == 1

        # The next wake settles the active occurrence (aftermath owns that
        # wake); every following wake of the same local day converges on the
        # already-occurred day identity instead of re-rolling.
        await _tick(
            app, tick_id="day1:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=1),
        )
        await _tick(
            app, tick_id="day1:c",
            frm=NOW + timedelta(hours=1), to=NOW + timedelta(hours=2),
        )
        # Afternoon slot of the same day: still no second occurrence.
        await _tick(
            app, tick_id="day1:d",
            frm=NOW + timedelta(hours=2), to=NOW + timedelta(hours=6),
        )
        assert len(_npc_occurrences(app._ledger.project())) == 1  # noqa: SLF001
        assert model.npc_calls == 1
        assert len(_npc_draw_events(app)) == 1
        assert len(_npc_check_events(app)) == 1

        # The next companion-local day gets its own chance.
        await _tick(
            app, tick_id="day2:a",
            frm=NOW + timedelta(hours=6), to=NOW + timedelta(days=1),
        )
        assert len(_npc_occurrences(app._ledger.project())) == 2  # noqa: SLF001
        assert model.npc_calls == 2
    finally:
        app.close()


@pytest.mark.asyncio
async def test_model_no_op_consumes_the_check_slot_without_an_event(
    tmp_path: Path,
) -> None:
    model = _LifeModel(npc_decision="no_op")
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="model-no-op")
    try:
        # Morning slot: the draw selects the certain event, the model says
        # "范予安今天没来找她" — always a legitimate answer.
        await _tick(app, tick_id="noop:a", frm=NOW, to=NOW + timedelta(minutes=5))
        assert _npc_occurrences(app._ledger.project()) == []  # noqa: SLF001
        assert model.npc_calls == 1
        checks = _npc_check_events(app)
        assert len(checks) == 1
        assert checks[0].payload()["decision"] == "no_op"

        # A second morning wake finds the slot consumed: no new draw, no new
        # model call, still no occurrence.
        await _tick(
            app, tick_id="noop:b",
            frm=NOW + timedelta(minutes=5), to=NOW + timedelta(hours=1),
        )
        assert _npc_occurrences(app._ledger.project()) == []  # noqa: SLF001
        assert model.npc_calls == 1
        assert len(_npc_draw_events(app)) == 1
        assert len(_npc_check_events(app)) == 1

        # The afternoon slot is a fresh, durable second (and last) check.
        await _tick(
            app, tick_id="noop:c",
            frm=NOW + timedelta(hours=1), to=NOW + timedelta(hours=6),
        )
        await _tick(
            app, tick_id="noop:d",
            frm=NOW + timedelta(hours=6), to=NOW + timedelta(hours=7),
        )
        assert _npc_occurrences(app._ledger.project()) == []  # noqa: SLF001
        assert model.npc_calls == 2
        assert len(_npc_check_events(app)) == 2
    finally:
        app.close()


@pytest.mark.asyncio
async def test_nothing_happens_outside_the_npc_presence_window(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    early = NOW - timedelta(minutes=80)  # 08:10 local
    app = _build(tmp_path, _seed(tmp_path / "seed.yaml"), model, name="absent", now=early)
    try:
        # 08:10-09:20 local: the library is open but 范予安 is not there yet
        # (presence starts 09:00, event window 09:30).  No candidate exists,
        # so no draw happens and no check slot is consumed.
        await _tick(app, tick_id="absent:a", frm=early, to=early + timedelta(minutes=30))
        assert _npc_occurrences(app._ledger.project()) == []  # noqa: SLF001
        assert model.npc_calls == 0
        assert _npc_draw_events(app) == []
        assert _npc_check_events(app) == []

        # The same morning slot is still fully available once the reviewed
        # window opens: absence never burns the daily budget.
        await _tick(app, tick_id="absent:b", frm=early + timedelta(minutes=30), to=NOW)
        assert len(_npc_occurrences(app._ledger.project())) == 1  # noqa: SLF001
    finally:
        app.close()


@pytest.mark.asyncio
async def test_npc_initiative_can_be_disabled_by_composition(
    tmp_path: Path,
) -> None:
    model = _LifeModel()
    app = _build(
        tmp_path, _seed(tmp_path / "seed.yaml"), model, name="disabled",
        npc_initiative_enabled=False,
    )
    try:
        await _tick(app, tick_id="disabled:a", frm=NOW, to=NOW + timedelta(hours=1))
        assert _npc_occurrences(app._ledger.project()) == []  # noqa: SLF001
        assert model.npc_calls == 0
        assert _npc_draw_events(app) == []
    finally:
        app.close()


def test_mood_and_relationship_tilt_the_recorded_weights_without_gating(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml", events=_THREE_EVENTS),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    # 11:00 local: all three reviewed events are eligible.
    candidates = catalog.npc_initiative_candidates_at(
        instant=datetime(2026, 7, 17, 3, 0, tzinfo=UTC),
        npcs=(SimpleNamespace(npc_id="fan-yuan", status="active"),),
    )
    assert len(candidates) == 3
    by_kind = {item.event.initiative_kind: item for item in candidates}
    policy = NpcInitiativeWeightPolicy()

    def episode(dimension: str, intensity: int = 8_000) -> SimpleNamespace:
        return SimpleNamespace(
            status="active",
            components=(SimpleNamespace(dimension=dimension, intensity_bp=intensity),),
        )

    baseline = policy.compile(candidates=candidates)
    lonely = policy.compile(candidates=candidates, affect_episodes=(episode("loneliness"),))
    warm = policy.compile(candidates=candidates, affect_episodes=(episode("warmth"),))
    unresolved = policy.compile(candidates=candidates, affect_episodes=(episode("resentment"),))

    # Loneliness raises every NPC-initiated event — the friend showing up is
    # exactly what is quietly needed — and shrinks only the nothing mass.
    for kind in ("small_favor", "shared_time", "friction"):
        token = by_kind[kind].token
        assert lonely[token] > baseline[token]
    assert lonely[NOTHING_CANDIDATE_REF] < baseline[NOTHING_CANDIDATE_REF]

    # Warmth (the phase-one closeness reading) reaches toward being asked
    # along and small favors, and leaves the disagreement prior alone.
    assert warm[by_kind["shared_time"].token] > baseline[by_kind["shared_time"].token]
    assert warm[by_kind["small_favor"].token] > baseline[by_kind["small_favor"].token]
    assert warm[by_kind["friction"].token] == baseline[by_kind["friction"].token]

    # Unresolved friction slightly raises only the disagreement event.
    assert unresolved[by_kind["friction"].token] > baseline[by_kind["friction"].token]
    assert unresolved[by_kind["shared_time"].token] == baseline[by_kind["shared_time"].token]

    # The tilt is a bounded tendency, never a gate: every candidate keeps
    # positive mass and no multiplier exceeds +/-40% of the reviewed base.
    saturated = policy.compile(
        candidates=candidates,
        affect_episodes=(episode("loneliness", 10_000), episode("warmth", 10_000)),
    )
    for candidate in candidates:
        base = candidate.event.base_chance_bp
        assert 0 < saturated[candidate.token] <= base * 14_000 // 10_000
        assert baseline[candidate.token] == base
    assert baseline[NOTHING_CANDIDATE_REF] == 10_000 - 1_200 - 900 - 600
