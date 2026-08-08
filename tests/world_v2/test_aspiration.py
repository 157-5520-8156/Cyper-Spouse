from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.aspiration_events import (
    AspirationAbandonedPayload,
    AspirationCrystallizedPayload,
    AspirationReinforcedPayload,
    AspirationRevisedPayload,
)
from companion_daemon.world_v2.aspiration_reducers import (
    abandon_aspiration,
    crystallize_aspiration,
    reinforce_aspiration,
    revise_aspiration,
)
from companion_daemon.world_v2.aspiration_seed_policy import (
    NOTHING_CANDIDATE_REF,
    AspirationSeedCandidate,
    AspirationWeightPolicy,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology
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


def test_weight_policy_keeps_planting_a_rare_recorded_gate(tmp_path: Path) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml", seeds=_THREE_SEEDS),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    seeds = {item.id: item for item in catalog.reviewed_aspiration_seeds}
    assert set(seeds) == {"aspire-japan-trip", "aspire-liwa-seasons", "aspire-finish-reading"}

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
        evidence_refs=(
            _evidence("event:plan-accept"),
            _evidence("event:aspiration:planted:test"),
        ),
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

    mismatched = _active(
        planted,
        aspiration_id="aspiration:other",
        planted_event_ref="event:aspiration:planted:other",
    )
    with pytest.raises(ValueError, match="exact planting event"):
        crystallize_aspiration(
            (mismatched,),
            (_Plan(),),
            payload.model_copy(update={"aspiration_id": "aspiration:other"}),
            logical_time=later,
        )


def test_character_authored_aspiration_can_be_revised_with_source_bound_tension() -> None:
    planted = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    revised_at = planted + timedelta(days=5)
    before = _active(
        planted,
        seed_id="character-interior:initial",
        origin_kind="character_authored",
    )
    event_ref = "event:aspiration:revised:test"
    after = before.model_copy(
        update={
            "entity_revision": 2,
            "text": "想学拉坯，但又担心现在没精力长期坚持。",
            "tension_summary": "兴趣很真切，现实精力也确实有限。",
            "tension_source_refs": (
                "event:witness-2",
                before.planted_event_ref,
            ),
            "last_revised_at": revised_at,
            "revision_event_ref": event_ref,
        }
    )
    payload = AspirationRevisedPayload(
        change_id="change:revise",
        transition_id="transition:revise",
        expected_entity_revision=1,
        evidence_refs=(
            _evidence("event:witness-2"),
            _evidence(before.planted_event_ref),
        ),
        aspiration_before=before,
        aspiration_after=after,
    )

    updated = revise_aspiration(
        (before,),
        payload,
        event_ref=event_ref,
        logical_time=revised_at,
    )

    assert updated == (after,)
    assert updated[0].tension_source_refs == (
        "event:witness-2",
        before.planted_event_ref,
    )


def test_character_authored_aspiration_abandonment_is_an_explicit_terminal_choice() -> None:
    planted = datetime(2026, 7, 1, 4, 0, tzinfo=UTC)
    abandoned_at = planted + timedelta(days=8)
    before = _active(
        planted,
        seed_id="character-interior:initial",
        origin_kind="character_authored",
    )
    event_ref = "event:aspiration:abandoned:test"
    after = before.model_copy(
        update={
            "entity_revision": 2,
            "status": "abandoned",
            "abandoned_at": abandoned_at,
            "abandonment_summary": "她现在不想再把这件事当成自己的方向。",
            "abandonment_source_refs": (
                "event:witness-2",
                before.planted_event_ref,
            ),
            "abandonment_event_ref": event_ref,
        }
    )
    payload = AspirationAbandonedPayload(
        change_id="change:abandon",
        transition_id="transition:abandon",
        expected_entity_revision=1,
        evidence_refs=(
            _evidence("event:witness-2"),
            _evidence(before.planted_event_ref),
        ),
        aspiration_before=before,
        aspiration_after=after,
    )

    updated = abandon_aspiration(
        (before,),
        payload,
        event_ref=event_ref,
        logical_time=abandoned_at,
    )

    assert updated == (after,)
    with pytest.raises(ValueError, match="only an active aspiration"):
        reinforce_aspiration(
            updated,
            AspirationReinforcedPayload(
                change_id="change:late-reinforce",
                transition_id="transition:late-reinforce",
                expected_entity_revision=2,
                evidence_refs=(_evidence("event:witness-3"),),
                aspiration_id=before.aspiration_id,
                reinforced_at=abandoned_at + timedelta(days=1),
                reinforcement_evidence_ref="event:witness-3",
            ),
            logical_time=abandoned_at + timedelta(days=1),
        )
