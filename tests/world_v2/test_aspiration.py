from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
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
    AspirationRuntime,
    AspirationWeightPolicy,
)
from companion_daemon.world_v2.aspiration_view import active_aspiration_advisories
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.event_identity import domain_idempotency_key
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
from companion_daemon.world_v2.schemas import AspirationProjection, Observation, WorldEvent

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
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": capsule["aspiration_candidate"]["token"],
                }
            )
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

_CONTEXTUAL_FUTURE = """
  future_openings:
    - id: future-destination-research
      activity_kind: travel.destination_research
      source: intentional_goal
      domain: study_class
      social_shape: alone
      deviation: persist
      visual_potential: object
      privacy: private
      location_id: dorm-room
      local_windows: ["14:00-17:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 45
      importance_bp: 5000
      advance_days_min: 1
      advance_days_max: 3
      outcomes:
        - {id: research-useful, text: 把一个想去的地方的路线和现实条件查清楚了一些。, privacy: private}
        - {id: research-paused, text: 查了一阵发现还缺不少信息，先把资料收了起来。, privacy: private}
"""


def _seed(path: Path, seeds: str = _CERTAIN_SEED) -> Path:
    path.write_text((_SEED_HEADER + seeds).strip(), encoding="utf-8")
    return path


class _ContextualLifeModel(_LifeModel):
    def __init__(self, *, fail_first: bool = False, fail_times: int = 0) -> None:
        super().__init__(aspiration_decision="no_op")
        self.fail_times = max(fail_times, 1 if fail_first else 0)
        self.contextual_calls = 0
        self.planning_calls = 0
        self.last_contextual_payload: dict[str, object] | None = None

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        payload = json.loads(messages[-1]["content"])
        if "contextual_life_inspiration_sources" in payload:
            self.contextual_calls += 1
            self.last_contextual_payload = payload
            if self.contextual_calls <= self.fail_times:
                raise TimeoutError("provider unavailable")
            source = payload["contextual_life_inspiration_sources"][0]
            return json.dumps(
                {
                    "decision": "form",
                    "impulse_text": "想先查查深圳最近的展和路线，合适的话以后自己去看看。",
                    "source_event_refs": [source["event_ref"]],
                    "privacy": "personal",
                },
                ensure_ascii=False,
            )
        if "contextual_life_inspiration" in payload:
            self.planning_calls += 1
            candidate = payload["reviewed_future_candidates"][0]
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": candidate["token"],
                }
            )
        if "openings" in payload:
            for phrase in ("begin", "finish"):
                selected = next(
                    (
                        item
                        for item in payload["openings"]
                        if str(item.get("safe_summary", "")).startswith(phrase)
                    ),
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
        return await super().complete(messages, temperature=temperature)


def _record_user_observation(app, *, event_id: str, text: str, at: datetime) -> None:  # type: ignore[no-untyped-def]
    projection = app._ledger.project()  # noqa: SLF001
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    observation = Observation(
        schema_version="world-v2.1",
        observation_id=event_id.removeprefix("event:"),
        world_id=app._ledger.world_id,  # noqa: SLF001
        logical_time=projection.logical_time,
        created_at=at,
        trace_id="trace:contextual-life-inspiration",
        causation_id="cause:contextual-life-inspiration",
        correlation_id="conversation:contextual-life-inspiration",
        source="test:contextual-life-inspiration",
        source_event_id=event_id,
        actor="user:user.1",
        channel="qq:c2c",
        payload_ref="payload:" + event_id,
        payload_hash=text_hash,
        text=text,
        received_at=at,
    )
    payload = observation.model_dump(mode="json")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=app._ledger.world_id,  # noqa: SLF001
        event_type="ObservationRecorded",
        logical_time=projection.logical_time,
        created_at=at,
        actor="user:user.1",
        source="test:contextual-life-inspiration",
        trace_id="trace:contextual-life-inspiration",
        causation_id="cause:contextual-life-inspiration",
        correlation_id="conversation:contextual-life-inspiration",
        idempotency_key=(
            domain_idempotency_key(
                event_type="ObservationRecorded",
                world_id=app._ledger.world_id,  # noqa: SLF001
                payload=payload,
            )
            or "contextual-life-inspiration:" + event_id
        ),
        payload=payload,
    )
    app._ledger.commit(  # noqa: SLF001
        [event],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


@pytest.mark.asyncio
async def test_user_place_can_become_source_bound_inspiration_then_reviewed_plan(
    tmp_path: Path,
) -> None:
    seed = _seed(
        tmp_path / "contextual-seed.yaml",
        _CERTAIN_SEED + _CONTEXTUAL_FUTURE,
    )
    model = _ContextualLifeModel()
    app = _build(tmp_path, seed, model, name="contextual")
    source_ref = "event:observation:user-mentioned-shenzhen"
    try:
        _record_user_observation(
            app,
            event_id=source_ref,
            text="我下午在说深圳那边最近有个展。",
            at=NOW,
        )
        await _tick(
            app,
            tick_id="contextual:form",
            frm=NOW,
            to=NOW + timedelta(minutes=5),
        )
        inspiration = next(
            item
            for item in app._ledger.project().aspirations  # noqa: SLF001
            if item.seed_id.startswith("contextual:")
        )
        assert inspiration.source_event_ref == source_ref
        assert "深圳" in inspiration.text
        proposal = next(
            item.event
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
            if item.event.event_type == "ProposalRecorded"
            and item.event.payload().get("proposal_kind") == "contextual_life_inspiration"
        )
        assert proposal.payload()["source_event_refs"] == [source_ref]
        assert proposal.payload()["context_capsule_id"]
        assert proposal.payload()["context_model_content_hash"]
        assert proposal.payload()["context_cursor"]["ledger_sequence"] > 0
        assert model.last_contextual_payload is not None
        offered_source = model.last_contextual_payload[
            "contextual_life_inspiration_sources"
        ][0]
        assert offered_source["contents"][0]["text"] == "我下午在说深圳那边最近有个展。"
        assert offered_source["authority_bindings"][0]["event_ref"] == source_ref
        assert offered_source["authority_bindings"][0]["payload_hash"] == (
            app._ledger.lookup_event_commit(source_ref)[0].payload_hash  # noqa: SLF001
        )

        await _tick(
            app,
            tick_id="contextual:plan",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(days=1, minutes=5),
        )
        projection = app._ledger.project()  # noqa: SLF001
        crystallized = next(
            item
            for item in projection.aspirations
            if item.aspiration_id == inspiration.aspiration_id
        )
        assert crystallized.status == "crystallized"
        plan = next(
            item
            for item in projection.plans
            if item.plan_id == crystallized.crystallized_plan_ref.removeprefix("plan:")
        )
        assert plan.activity_kind == "travel.destination_research"
        assert plan.location_ref == "location:dorm-room"
        assert inspiration.planted_event_ref in {evidence.ref_id for evidence in plan.evidence_refs}
        assert model.contextual_calls == 1
        assert model.planning_calls == 1

        assert plan.scheduled_window is not None
        opens_wake = plan.scheduled_window.opens_at + timedelta(minutes=5)
        await _tick(
            app,
            tick_id="contextual:start",
            frm=NOW + timedelta(days=1, minutes=5),
            to=opens_wake,
        )
        assert (
            next(
                item
                for item in app._ledger.project().plans  # noqa: SLF001
                if item.plan_id == plan.plan_id
            ).status
            == "active"
        )

        settle_wake = plan.scheduled_window.closes_at + timedelta(minutes=5)
        await _tick(
            app,
            tick_id="contextual:settle",
            frm=opens_wake,
            to=settle_wake,
        )
        settled = app._ledger.project()  # noqa: SLF001
        assert (
            next(item for item in settled.plans if item.plan_id == plan.plan_id).status
            == "completed"
        )
        occurrence = next(item for item in settled.world_occurrences if item.status == "settled")
        assert occurrence.status == "settled"
        assert any(
            item.values.source_bindings[0].authority_event_ref == occurrence.settlement_event_ref
            for item in settled.experiences
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_contextual_inspiration_provider_failure_retries_same_source(
    tmp_path: Path,
) -> None:
    seed = _seed(tmp_path / "contextual-retry-seed.yaml", _CONTEXTUAL_FUTURE)
    model = _ContextualLifeModel(fail_first=True)
    app = _build(tmp_path, seed, model, name="contextual-retry")
    source_ref = "event:observation:user-mentioned-place-retry"
    try:
        _record_user_observation(
            app,
            event_id=source_ref,
            text="今天提到一个以后也许值得去的地方。",
            at=NOW,
        )
        await _tick(
            app,
            tick_id="contextual:retry:first",
            frm=NOW,
            to=NOW + timedelta(minutes=5),
        )
        failed = app._ledger.project()  # noqa: SLF001
        assert not failed.aspirations
        retry = next(
            item
            for item in failed.contextual_life_retries
            if item.lane == "formation" and item.source_event_ref == source_ref
        )
        assert retry.retry_ordinal == 1
        assert retry.next_retry_at == NOW + timedelta(minutes=15)

        await _tick(
            app,
            tick_id="contextual:retry:too-early",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(minutes=14),
        )
        assert model.contextual_calls == 1
        app.close()
        app = _build(
            tmp_path,
            seed,
            model,
            name="contextual-retry",
            now=NOW + timedelta(minutes=14),
        )

        await _tick(
            app,
            tick_id="contextual:retry:second",
            frm=NOW + timedelta(minutes=14),
            to=NOW + timedelta(minutes=15),
        )
        recovered = app._ledger.project()  # noqa: SLF001
        assert any(
            item.source_event_ref == source_ref
            for item in recovered.aspirations
        )
        assert not recovered.contextual_life_retries
        assert model.contextual_calls == 2
    finally:
        app.close()


@pytest.mark.asyncio
async def test_contextual_retry_is_source_scoped_and_not_reset_by_unrelated_success(
    tmp_path: Path,
) -> None:
    seed = _seed(tmp_path / "contextual-scoped-retry.yaml", _CONTEXTUAL_FUTURE)
    model = _ContextualLifeModel(fail_first=True)
    app = _build(tmp_path, seed, model, name="contextual-scoped-retry")
    first = "event:observation:contextual-retry:first"
    second = "event:observation:contextual-retry:second"
    try:
        _record_user_observation(app, event_id=first, text="第一个来源。", at=NOW)
        await _tick(
            app,
            tick_id="contextual:scoped:first",
            frm=NOW,
            to=NOW + timedelta(minutes=5),
        )
        _record_user_observation(
            app,
            event_id=second,
            text="后来出现的另一个来源。",
            at=NOW + timedelta(minutes=6),
        )
        await _tick(
            app,
            tick_id="contextual:scoped:unrelated-ecology-success",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(minutes=10),
        )
        projection = app._ledger.project()  # noqa: SLF001
        retry = next(item for item in projection.contextual_life_retries if item.lane == "formation")
        assert retry.source_event_ref == first
        assert retry.retry_ordinal == 1
        assert retry.next_retry_at == NOW + timedelta(minutes=15)
        assert tuple(item.source_event_ref for item in projection.pending_contextual_life_sources) == (
            first,
            second,
        )
        assert model.contextual_calls == 1
    finally:
        app.close()


@pytest.mark.asyncio
async def test_contextual_retry_uses_ten_thirty_one_twenty_minute_backoff(
    tmp_path: Path,
) -> None:
    seed = _seed(tmp_path / "contextual-backoff.yaml", _CONTEXTUAL_FUTURE)
    model = _ContextualLifeModel(fail_times=3)
    app = _build(tmp_path, seed, model, name="contextual-backoff")
    source_ref = "event:observation:contextual-backoff"
    try:
        _record_user_observation(app, event_id=source_ref, text="一个来源。", at=NOW)
        boundaries = (
            NOW + timedelta(minutes=5),
            NOW + timedelta(minutes=15),
            NOW + timedelta(minutes=45),
        )
        expected_next = (
            NOW + timedelta(minutes=15),
            NOW + timedelta(minutes=45),
            NOW + timedelta(minutes=165),
        )
        current = NOW
        for ordinal, (boundary, next_retry) in enumerate(
            zip(boundaries, expected_next, strict=True),
            start=1,
        ):
            await _tick(
                app,
                tick_id=f"contextual:backoff:{ordinal}",
                frm=current,
                to=boundary,
            )
            retry = app._ledger.project().contextual_life_retries[0]  # noqa: SLF001
            assert retry.retry_ordinal == ordinal
            assert retry.next_retry_at == next_retry
            current = boundary
        assert model.contextual_calls == 3
    finally:
        app.close()


@pytest.mark.asyncio
async def test_contextual_inspiration_considers_oldest_open_source_without_starvation(
    tmp_path: Path,
) -> None:
    seed = _seed(tmp_path / "contextual-order-seed.yaml", _CONTEXTUAL_FUTURE)
    model = _ContextualLifeModel()
    app = _build(tmp_path, seed, model, name="contextual-order")
    first = "event:observation:contextual-order:first"
    second = "event:observation:contextual-order:second"
    try:
        _record_user_observation(app, event_id=first, text="先说的一个地方。", at=NOW)
        _record_user_observation(
            app,
            event_id=second,
            text="紧接着又说了另一个地方。",
            at=NOW + timedelta(seconds=1),
        )
        await _tick(
            app,
            tick_id="contextual:order",
            frm=NOW,
            to=NOW + timedelta(minutes=5),
        )
        proposal = next(
            item.event
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
            if item.event.event_type == "ProposalRecorded"
            and item.event.payload().get("proposal_kind") == "contextual_life_inspiration"
        )
        assert proposal.payload()["source_event_refs"] == [first]
        assert model.last_contextual_payload is not None
        assert (
            model.last_contextual_payload["contextual_life_inspiration_sources"][0]["event_ref"]
            == first
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_recorded_contextual_plan_recovers_after_commit_conflict_and_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed = _seed(tmp_path / "contextual-recovery-seed.yaml", _CONTEXTUAL_FUTURE)
    model = _ContextualLifeModel()
    original = AspirationRuntime.commit_reviewed_crystallization
    attempts = 0

    def fail_first_commit(self, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConcurrencyConflict("simulated stale plan acceptance cursor")
        return original(self, **kwargs)

    monkeypatch.setattr(
        AspirationRuntime,
        "commit_reviewed_crystallization",
        fail_first_commit,
    )
    app = _build(tmp_path, seed, model, name="contextual-recovery")
    source_ref = "event:observation:contextual-plan-recovery"
    try:
        _record_user_observation(
            app,
            event_id=source_ref,
            text="深圳那个地方让我忽然有点想为以后做些准备。",
            at=NOW,
        )
        await _tick(
            app,
            tick_id="contextual:recovery:form",
            frm=NOW,
            to=NOW + timedelta(minutes=5),
        )
        await _tick(
            app,
            tick_id="contextual:recovery:record-select",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(days=1, minutes=5),
        )
        before = app._ledger.project()  # noqa: SLF001
        inspiration = next(
            item for item in before.aspirations if item.seed_id.startswith("contextual:")
        )
        assert inspiration.status == "active"
        checks = [
            item.event
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
            if item.event.event_type == "ProposalRecorded"
            and item.event.payload().get("proposal_kind") == "contextual_life_plan"
        ]
        assert len(checks) == 1
        assert checks[0].payload()["decision"] == "select"
        recorded_slot = checks[0].payload()["slot"]
        assert recorded_slot["activity_kind"] == "travel.destination_research"
    finally:
        app.close()

    restarted = _build(
        tmp_path,
        seed,
        model,
        name="contextual-recovery",
        now=NOW + timedelta(days=1, minutes=5),
    )
    try:
        await _tick(
            restarted,
            tick_id="contextual:recovery:restart",
            frm=NOW + timedelta(days=1, minutes=5),
            to=NOW + timedelta(days=1, minutes=15),
        )
        projection = restarted._ledger.project()  # noqa: SLF001
        recovered = next(
            item
            for item in projection.aspirations
            if item.aspiration_id == inspiration.aspiration_id
        )
        assert recovered.status == "crystallized"
        plan = next(
            item
            for item in projection.plans
            if item.plan_id == recovered.crystallized_plan_ref.removeprefix("plan:")
        )
        assert plan.activity_kind == recorded_slot["activity_kind"]
        assert plan.location_ref == recorded_slot["location_ref"]
        assert model.planning_calls == 1
        assert attempts == 2
    finally:
        restarted.close()


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
    tmp_path: Path,
    seed_path: Path,
    model: _LifeModel,
    *,
    name: str,
    now: datetime = NOW,
    **overrides,
):
    return build_sqlite_world_v2_turn_application(
        path=tmp_path / f"{name}.sqlite",
        config=_config(seed_path, **overrides),
        identities=_Identities(),
        router=_Router(),
        main_model=_MainModel(),
        quick_recovery=_QuickRecovery(),
        transport=_Transport(),
        activity_lifecycle_model=model,
        now=now,
    )


async def _tick(app, *, tick_id: str, frm: datetime, to: datetime) -> None:
    await app.tick(
        tick_id=tick_id,
        logical_time_from=frm,
        logical_time_to=to,
        observed_at=to,
        trace_id=f"trace:{tick_id}",
        causation_id="scheduler:aspiration",
        correlation_id="correlation:aspiration",
        reason="aspiration-test",
    )


def _draw_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event
        for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
        if item.event.event_type == "RandomDrawRecorded"
        and item.event.source == "world-v2:aspiration-random"
    ]


def _check_events(app):  # type: ignore[no-untyped-def]
    return [
        item.event
        for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
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
            app,
            tick_id="plant:b",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(hours=1),
        )
        await _tick(
            app,
            tick_id="plant:c",
            frm=NOW + timedelta(hours=1),
            to=NOW + timedelta(hours=6),
        )
        assert len(app._ledger.project().aspirations) == 1  # noqa: SLF001
        assert model.aspiration_calls == 1
        assert len(_draw_events(app)) == 1
        assert len(_check_events(app)) == 1

        # The next local day cannot replant an exhausted seed: no candidates
        # means no draw and no consumed check.
        await _tick(
            app,
            tick_id="plant:d",
            frm=NOW + timedelta(hours=6),
            to=NOW + timedelta(days=1),
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
            app,
            tick_id="noop:b",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(hours=2),
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
        tmp_path,
        _seed(tmp_path / "seed.yaml", seeds=_WITNESS_GATED_SEED),
        model,
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
        tmp_path,
        _seed(tmp_path / "seed.yaml"),
        model,
        name="fade",
        aspiration_fade_chance_bp=10_000,
    )
    try:
        await _tick(app, tick_id="fade:plant", frm=NOW, to=NOW + timedelta(minutes=5))
        assert len(app._ledger.project().aspirations) == 1  # noqa: SLF001

        # Day 10: idle for only ~10 days — not fade-eligible, so the day's
        # check records no fade draw and the wish stays active.
        await _tick(
            app,
            tick_id="fade:day10",
            frm=NOW + timedelta(minutes=5),
            to=NOW + timedelta(days=10),
        )
        projection = app._ledger.project()  # noqa: SLF001
        assert projection.aspirations[0].status == "active"
        assert not [
            event
            for event in _draw_events(app)
            if any(ref.startswith("fade:") for ref in event.payload()["candidate_refs"])
        ]

        # Day 15: past the 14-idle-day threshold, the certain roll fades it.
        await _tick(
            app,
            tick_id="fade:day15",
            frm=NOW + timedelta(days=10),
            to=NOW + timedelta(days=15),
        )
        faded = app._ledger.project().aspirations[0]  # noqa: SLF001
        assert faded.status == "faded"
        assert faded.faded_at is not None
        fade_draws = [
            event
            for event in _draw_events(app)
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
        tmp_path,
        _seed(tmp_path / "seed.yaml"),
        model,
        name="disabled",
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
        reinforce_aspiration(
            (_active(planted),),
            payload.model_copy(update={"expected_entity_revision": 2}),
            logical_time=later,
        )


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

    updated = crystallize_aspiration((_active(planted),), (_Plan(),), payload, logical_time=later)
    assert updated[0].status == "crystallized"
    assert updated[0].crystallized_plan_ref == "plan:japan-trip"
