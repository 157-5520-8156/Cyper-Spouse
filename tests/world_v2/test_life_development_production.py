from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from world_v2_application import (
    build_sqlite_world_v2_test_application,
    compose_fixture_character_interior,
    compose_fixture_character_purpose,
)

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.life_development_capability import (
    ProjectionLifeCapabilityManifestCompiler,
)
from companion_daemon.world_v2.local_chronology import LocalChronology
from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
)
from companion_daemon.world_v2.schemas import (
    CommittedWorldEventRef,
    DueWindow,
    ProjectionCursor,
    WorldEvent,
    WorldPlaceProjection,
)

NOW = datetime(2026, 7, 29, 10, 0, tzinfo=UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("life development ecology does not deliberate a reply")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("life development ecology does not deliberate a reply")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("life development ecology does not send a chat Action")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _NoOpWorldAuthor:
    model = "test-production-world-author"
    semantic_authority_id = "semantic-authority:test:production-world-author"

    def __init__(self, *, forbidden: bool = False) -> None:
        self.calls = 0
        self._forbidden = forbidden

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        if self._forbidden:
            raise AssertionError("cold recovery must not call the World Author")
        return '{"decision":"no_op"}'


class _NeverCharacterModel:
    model = "test-production-character-model"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        raise AssertionError("a World Author no_op must not call the Character Model")


class _UnavailableWorldAuthor:
    model = "test-production-unavailable-world-author"
    semantic_authority_id = "semantic-authority:test:production-unavailable-world-author"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        raise TimeoutError("test World Author outage")


class _PlanWorldAuthor:
    model = "test-production-plan-world-author"
    semantic_authority_id = "semantic-authority:test:production-plan-world-author"

    def __init__(self, *, wake_event_ref: str) -> None:
        self.calls = 0
        self._wake_event_ref = wake_event_ref

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        return json.dumps(
            {
                "decision": "propose",
                "authored_subject_ref": "actor:companion",
                "causal_authority": "character_choice",
                "outcome_resolution_authority": "world_contingency",
                "premise_scope": "external_opportunity",
                "premise": "公园今天临时有一小段露天电影。",
                "premise_claim_refs": ["local:claim:screening"],
                "claim_declarations": [
                    {
                        "claim_id": "local:claim:screening",
                        "summary": "公园存在一场可以自由参加的临时露天电影。",
                        "scope": "novel_world_generation",
                        "subject_scope": "world_environment",
                        "source_refs": [],
                    }
                ],
                "timing": {"mode": "now", "duration_minutes": 60},
                "anchor_refs": [self._wake_event_ref],
                "location_ref": None,
                "entity_refs": [],
                "privacy_class": "shareable",
                "outcomes": [
                    {
                        "experienced_by_ref": "actor:companion",
                        "text": "电影放完时风有点凉，人群慢慢散了。",
                        "privacy_class": "shareable",
                        "relative_plausibility_weight": 1,
                        "claim_refs": ["local:claim:screening"],
                        "provisional_npcs": [
                            {
                                "local_ref": "local:npc:screening-organizer",
                                "summary": "负责收放映设备的活动组织者。",
                                "narrative_tags": ["narrative:outdoor-film"],
                                "privacy_class": "personal",
                            }
                        ],
                        "dynamic_life_direction": None,
                    },
                    {
                        "experienced_by_ref": "actor:companion",
                        "text": "中途下了一点小雨，放映比预计早结束。",
                        "privacy_class": "shareable",
                        "relative_plausibility_weight": 2,
                        "claim_refs": ["local:claim:screening"],
                        "provisional_npcs": [
                            {
                                "local_ref": "local:npc:screening-organizer",
                                "summary": "负责收放映设备的活动组织者。",
                                "narrative_tags": ["narrative:outdoor-film"],
                                "privacy_class": "personal",
                            }
                        ],
                        "dynamic_life_direction": None,
                    },
                ],
            },
            ensure_ascii=False,
        )


class _AcceptCharacterModel:
    model = "test-production-accept-character"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(
        self,
        _messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        return json.dumps(
            {
                "decision": "accept",
                "intention_summary": "我想带杯水去后排坐一会儿。",
                "importance_bp": 4300,
                "participant_refs": [],
            },
            ensure_ascii=False,
        )


class _SupportingSourceReviewer:
    model = "test-production-independent-life-source-reviewer"
    semantic_authority_id = (
        "semantic-authority:test:production-independent-life-source-reviewer"
    )

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        del temperature
        if "focused novel-origin critic" in messages[0]["content"]:
            return json.dumps(
                {
                    "decision": "supported",
                    "unsupported_claims": [],
                    "unsupported_provisional_npcs": [],
                    "unsupported_outcome_prerequisites": [],
                    "undeclared_premise_fragments": [],
                    "reason": "No prior history or imported prerequisite is present.",
                }
            )
        return json.dumps(
            {
                "decision": "supported",
                "unsupported_claim_ids": [],
                "undeclared_fact_fragments": [],
                "undeclared_fact_paths": [],
                "typed_location_conflicts": [],
                "reason": "Proposal-scoped novel facts are declared and source-closed.",
            }
        )


class _SelectFirstLifecycle:
    model = "test-production-select-lifecycle"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        material = json.loads(messages[1]["content"])
        capability = material["capability_manifest"]
        token = capability["payload"]["openings"][0]["opening_token"]
        return json.dumps(
            {
                "status": "decision",
                "summary": "我现在想开始这项已经安排好的活动。",
                "attended_source_refs": [],
                "decision": {
                    "source_refs": capability["source_refs"],
                    "payload": {
                        "decision": "select",
                        "selected_token": token,
                    },
                },
                "recall_query": None,
                "proposals": [],
            },
            ensure_ascii=False,
        )


def _open_life_seed(path: Path, *, role: bool = True) -> Path:
    path.write_text(
        f"""
world_id: open-life-production
life_author_catalog:
  version: open-life.1
  {"story_candidate_role: legacy_replay_and_fixture" if role else ""}
  locations:
    - id: public-park
      location_ref: location:public-park
      privacy: shareable
      local_windows: ["06:00-23:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
  npcs: []
  openings: []
  future_openings: []
  npc_initiated_events: []
  aspiration_seeds: []
""".strip(),
        encoding="utf-8",
    )
    return path


def _wake() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:clock:open-life",
        event_type="ClockAdvanced",
        world_id="world:open-life-production",
        logical_time=NOW,
        created_at=NOW,
        actor="system:clock",
        source="test",
        trace_id="trace:open-life",
        causation_id="scheduler:open-life",
        correlation_id="correlation:open-life",
        idempotency_key="clock:open-life",
        payload={"tick_id": "open-life"},
    )


def test_projection_manifest_compiler_exposes_facts_and_affordances_without_story_candidates(
    tmp_path: Path,
) -> None:
    wake = _wake()
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_open_life_seed(tmp_path / "open-life.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    place_summary = "她在一次已结算经历里发现的临河旧书摊。"
    place_summary_hash = life_content_payload_hash(place_summary)
    content_store = InMemoryImmutableLifeContentStore()
    content_store.put_if_absent(
        StoredLifeContent(
            content_ref="content:place:user-mentioned-shop",
            content_kind="provisional_place_introduction",
            content_payload_hash=place_summary_hash,
            text=place_summary,
        )
    )
    projection = SimpleNamespace(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=11,
        logical_time=NOW,
        committed_world_event_refs=(
            CommittedWorldEventRef(
                event_id=wake.event_id,
                event_type=wake.event_type,
                world_revision=7,
                payload_hash=wake.payload_hash,
                logical_time=wake.logical_time,
            ),
        ),
        life_arcs=(),
        world_places=(
            WorldPlaceProjection(
                location_ref="location:open-life:" + "a" * 64,
                stable_identity_ref="content:place:user-mentioned-shop",
                summary_payload_hash=place_summary_hash,
                narrative_tags=("narrative:user_influence",),
                timezone_name="Asia/Shanghai",
                privacy_class="personal",
                access_assurance="attempt_only",
                source_event_ref=wake.event_id,
                effect_descriptor_hash="b" * 64,
                accepted_at=NOW,
            ),
        ),
        npcs=(SimpleNamespace(npc_id="friend", status="active"),),
        plans=(
            SimpleNamespace(
                owner_actor_ref="actor:companion",
                status="active",
                location_ref="location:current-cafe",
                evidence_refs=(),
                scheduled_window=None,
            ),
        ),
        locations=(
            SimpleNamespace(
                actor_ref="actor:companion",
                values=SimpleNamespace(
                    location_ref="location:current-cafe",
                    privacy_class="personal",
                    since=NOW - timedelta(hours=1),
                ),
                origin=SimpleNamespace(
                    accepted_event_ref="event:location:current-cafe",
                ),
            ),
        ),
    )
    capsule = SimpleNamespace(
        model_content_json=json.dumps(
            {"sources": [{"source_event_ref": wake.event_id}]},
            separators=(",", ":"),
        ),
        current_situation=SimpleNamespace(
            availability="available",
            source_refs=(wake.event_id,),
        ),
    )
    compiler = ProjectionLifeCapabilityManifestCompiler(
        owner_actor_ref="actor:companion",
        catalog=catalog,
        content_store=content_store,
    )

    manifest = compiler.compile(
        projection=projection,
        wake=wake,
        capsule=capsule,
    )

    assert manifest.pinned_cursor == ProjectionCursor(
        world_revision=7,
        deliberation_revision=3,
        ledger_sequence=11,
    )
    assert manifest.anchor_refs == (wake.event_id,)
    assert manifest.grounding_refs == (wake.event_id,)
    assert manifest.location_refs == (
        "location:current-cafe",
        "location:open-life:" + "a" * 64,
        "location:public-park",
    )
    current_presence = next(
        item
        for item in manifest.location_capabilities
        if item.location_ref == "location:current-cafe"
        and item.availability_kind == "current_presence"
    )
    assert current_presence.available_from == NOW - timedelta(hours=1)
    assert current_presence.available_to == NOW + timedelta(minutes=5)
    assert current_presence.authority_refs == (
        "event:clock:open-life",
        "event:location:current-cafe",
    )
    dynamic_place = next(
        item
        for item in manifest.location_capabilities
        if item.location_ref == "location:open-life:" + "a" * 64
    )
    assert dynamic_place.availability_kind == "settled_place"
    assert dynamic_place.identity_content_ref == "content:place:user-mentioned-shop"
    assert dynamic_place.identity_summary == place_summary
    assert dynamic_place.identity_payload_hash == place_summary_hash
    assert dynamic_place.authorizes(
        timing_mode="later",
        window=DueWindow(
            opens_at=NOW + timedelta(days=2),
            closes_at=NOW + timedelta(days=2, hours=1),
        ),
    )
    assert manifest.entity_refs == ("npc:friend",)

    without_identity = ProjectionLifeCapabilityManifestCompiler(
        owner_actor_ref="actor:companion",
        catalog=catalog,
        content_store=InMemoryImmutableLifeContentStore(),
    ).compile(
        projection=projection,
        wake=wake,
        capsule=capsule,
    )
    assert "location:open-life:" + "a" * 64 not in without_identity.location_refs


@pytest.mark.asyncio
async def test_production_open_life_no_op_is_effect_once_across_cold_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "open-life-production.sqlite"
    seed = _open_life_seed(tmp_path / "production-seed.yaml")
    config = WorldV2TurnApplicationConfig(
        world_id="world:open-life-production",
        companion_actor_ref="actor:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:open-life-production",
        character_memory_enabled=False,
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed),
    )
    world_author = _NoOpWorldAuthor()
    character_model = _NeverCharacterModel()
    app = build_sqlite_world_v2_test_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_MainModel(),
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="life_development_choice",
                    provider=character_model,
                ),
            ),
        ),
        transport=_Transport(),
        life_world_author_model=world_author,
        now=NOW,
    )
    wake_event_ref = "event:trigger:clock:open-life-production"
    try:
        await app.tick(
            tick_id="open-life-production",
            logical_time_from=NOW,
            logical_time_to=NOW.replace(minute=10),
            observed_at=NOW.replace(minute=10),
            trace_id="trace:open-life-production",
            causation_id="scheduler:open-life-production",
            correlation_id="correlation:open-life-production",
            reason="open-life-production",
            run_life_ecology=False,
        )
        first = await app.advance_life_ecology_once(
            wake_event_ref=wake_event_ref,
            trace_id="trace:open-life-production",
            correlation_id="correlation:open-life-production",
        )
        assert first.status == "idle"
        assert first.life_development_followup_status == "no_op"
        assert world_author.calls == 1
        assert character_model.calls == 0
    finally:
        app.close()

    recovered_world_author = _NoOpWorldAuthor(forbidden=True)
    recovered_character_model = _NeverCharacterModel()
    reopened = build_sqlite_world_v2_test_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_MainModel(),
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="life_development_choice",
                    provider=recovered_character_model,
                ),
            ),
        ),
        transport=_Transport(),
        life_world_author_model=recovered_world_author,
        now=NOW.replace(minute=10),
    )
    try:
        recovered = await reopened.advance_life_ecology_once(
            wake_event_ref=wake_event_ref,
            trace_id="trace:open-life-production-recovery",
            correlation_id="correlation:open-life-production",
        )
        assert recovered.status == "joined_existing"
        assert recovered.reason_code == "life_ecology.run_completed"
        assert recovered.life_development_followup_status is None
        assert recovered_world_author.calls == 0
        assert recovered_character_model.calls == 0
    finally:
        reopened.close()


def test_production_open_life_refuses_an_unmarked_legacy_story_catalog(
    tmp_path: Path,
) -> None:
    seed = _open_life_seed(tmp_path / "unmarked-seed.yaml", role=False)
    config = WorldV2TurnApplicationConfig(
        world_id="world:open-life-production",
        companion_actor_ref="actor:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:open-life-production",
        character_memory_enabled=False,
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed),
    )

    with pytest.raises(ValueError, match="legacy_replay_and_fixture"):
        build_sqlite_world_v2_test_application(
            path=tmp_path / "unmarked.sqlite",
            config=config,
            identities=_Identities(),
            router=_Router(),
            character_interior=compose_fixture_character_interior(
                inbound_author=_MainModel(),
                purpose_faculties=(
                    compose_fixture_character_purpose(
                        purpose="life_development_choice",
                        provider=_NeverCharacterModel(),
                    ),
                ),
            ),
            transport=_Transport(),
            life_world_author_model=_NoOpWorldAuthor(),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_production_open_life_failure_retries_at_ten_minutes_without_early_model_call(
    tmp_path: Path,
) -> None:
    database = tmp_path / "open-life-retry.sqlite"
    seed = _open_life_seed(tmp_path / "retry-seed.yaml")
    config = WorldV2TurnApplicationConfig(
        world_id="world:open-life-production",
        companion_actor_ref="actor:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:open-life-production",
        character_memory_enabled=False,
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed),
    )
    world_author = _UnavailableWorldAuthor()
    app = build_sqlite_world_v2_test_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_MainModel(),
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="life_development_choice",
                    provider=_NeverCharacterModel(),
                ),
            ),
        ),
        transport=_Transport(),
        life_world_author_model=world_author,
        now=NOW,
    )
    try:
        await app.tick(
            tick_id="open-life-retry-first",
            logical_time_from=NOW,
            logical_time_to=NOW.replace(minute=10),
            observed_at=NOW.replace(minute=10),
            trace_id="trace:open-life-retry-first",
            causation_id="scheduler:open-life-retry",
            correlation_id="correlation:open-life-retry",
            reason="open-life-retry",
            run_life_ecology=False,
        )
        first = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:open-life-retry-first",
            trace_id="trace:open-life-retry-first",
            correlation_id="correlation:open-life-retry",
        )
        schedule = app._ledger.project().life_ecology_schedule  # noqa: SLF001
        assert first.status == "deferred"
        assert world_author.calls == 1
        assert schedule is not None
        assert schedule.consecutive_failures == 1
        assert schedule.next_consideration_at == NOW.replace(minute=20)

        await app.tick(
            tick_id="open-life-retry-early",
            logical_time_from=NOW.replace(minute=10),
            logical_time_to=NOW.replace(minute=19),
            observed_at=NOW.replace(minute=19),
            trace_id="trace:open-life-retry-early",
            causation_id="scheduler:open-life-retry",
            correlation_id="correlation:open-life-retry",
            reason="open-life-retry",
            run_life_ecology=False,
        )
        early = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:open-life-retry-early",
            trace_id="trace:open-life-retry-early",
            correlation_id="correlation:open-life-retry",
        )
        assert early.life_development_followup_status is None
        assert world_author.calls == 1

        await app.tick(
            tick_id="open-life-retry-due",
            logical_time_from=NOW.replace(minute=19),
            logical_time_to=NOW.replace(minute=20),
            observed_at=NOW.replace(minute=20),
            trace_id="trace:open-life-retry-due",
            causation_id="scheduler:open-life-retry",
            correlation_id="correlation:open-life-retry",
            reason="open-life-retry",
            run_life_ecology=False,
        )
        due = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:open-life-retry-due",
            trace_id="trace:open-life-retry-due",
            correlation_id="correlation:open-life-retry",
        )
        schedule = app._ledger.project().life_ecology_schedule  # noqa: SLF001
        assert due.status == "deferred"
        assert world_author.calls == 2
        assert schedule is not None
        assert schedule.consecutive_failures == 2
        assert schedule.next_consideration_at == NOW.replace(minute=50)

        await app.tick(
            tick_id="open-life-retry-third",
            logical_time_from=NOW.replace(minute=20),
            logical_time_to=NOW.replace(minute=50),
            observed_at=NOW.replace(minute=50),
            trace_id="trace:open-life-retry-third",
            causation_id="scheduler:open-life-retry",
            correlation_id="correlation:open-life-retry",
            reason="open-life-retry",
            run_life_ecology=False,
        )
        third = await app.advance_life_ecology_once(
            wake_event_ref="event:trigger:clock:open-life-retry-third",
            trace_id="trace:open-life-retry-third",
            correlation_id="correlation:open-life-retry",
        )
        schedule = app._ledger.project().life_ecology_schedule  # noqa: SLF001
        assert third.status == "deferred"
        assert world_author.calls == 3
        assert schedule is not None
        assert schedule.consecutive_failures == 3
        assert schedule.next_consideration_at == NOW.replace(
            hour=12,
            minute=50,
        )
    finally:
        app.close()


@pytest.mark.asyncio
async def test_production_dynamic_character_plan_opens_aftermath_from_frozen_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "open-life-dynamic-aftermath.sqlite"
    seed = _open_life_seed(tmp_path / "dynamic-aftermath-seed.yaml")
    config = WorldV2TurnApplicationConfig(
        world_id="world:open-life-production",
        companion_actor_ref="actor:companion",
        reply_target="user:user.1",
        action_pump_owner="pump:open-life-production",
        character_memory_enabled=False,
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed),
    )
    first_wake = "event:trigger:clock:open-life-plan"
    world_author = _PlanWorldAuthor(wake_event_ref=first_wake)
    character_model = _AcceptCharacterModel()
    app = build_sqlite_world_v2_test_application(
        path=database,
        config=config,
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(
            inbound_author=_MainModel(),
            purpose_faculties=(
                compose_fixture_character_purpose(
                    purpose="life_development_choice",
                    provider=character_model,
                ),
                compose_fixture_character_purpose(
                    purpose="activity_lifecycle_choice",
                    provider=_SelectFirstLifecycle(),
                ),
            ),
        ),
        transport=_Transport(),
        life_world_author_model=world_author,
        life_source_closure_reviewer=_SupportingSourceReviewer(),
        now=NOW,
    )
    try:
        await app.tick(
            tick_id="open-life-plan",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=10),
            observed_at=NOW + timedelta(minutes=10),
            trace_id="trace:open-life-plan",
            causation_id="scheduler:open-life-plan",
            correlation_id="correlation:open-life-plan",
            reason="open-life-plan",
            run_life_ecology=False,
        )
        planned = await app.advance_life_ecology_once(
            wake_event_ref=first_wake,
            trace_id="trace:open-life-plan",
            correlation_id="correlation:open-life-plan",
        )
        assert planned.life_development_followup_status == "plan_committed"
        plan = app._ledger.project().plans[0]  # noqa: SLF001
        assert plan.activity_kind.startswith("open_life.")

        second_wake = "event:trigger:clock:open-life-plan-start"
        await app.tick(
            tick_id="open-life-plan-start",
            logical_time_from=NOW + timedelta(minutes=10),
            logical_time_to=NOW + timedelta(minutes=11),
            observed_at=NOW + timedelta(minutes=11),
            trace_id="trace:open-life-plan-start",
            causation_id="scheduler:open-life-plan",
            correlation_id="correlation:open-life-plan",
            reason="open-life-plan",
            run_life_ecology=False,
        )
        opened = await app.advance_life_ecology_once(
            wake_event_ref=second_wake,
            trace_id="trace:open-life-plan-start",
            correlation_id="correlation:open-life-plan",
        )

        projection = app._ledger.project()  # noqa: SLF001
        assert opened.activity_followup_status == "transitioned"
        assert opened.aftermath_followup_status == "occurrence_opened"
        assert opened.life_development_followup_status is None
        assert world_author.calls == 1
        assert character_model.calls == 1
        occurrence = projection.world_occurrences[0]
        assert occurrence.status == "active"
        assert occurrence.trigger_ref == plan.plan_id
        assert [item.causal_authority for item in occurrence.candidate_outcomes] == [
            "world_contingency",
            "world_contingency",
        ]
        assert [
            item.relative_plausibility_weight
            for item in occurrence.candidate_outcomes
        ] == [1, 2]
        assert all(
            item.candidate_result_ref.startswith("candidate:life-development:")
            for item in occurrence.candidate_outcomes
        )

        aftermath = app._life_ecology._aftermath_followup  # noqa: SLF001
        original_commit = aftermath._commit  # noqa: SLF001
        failed_once = False

        def fail_once_after_recorded_draw(events, *, commit_id):  # type: ignore[no-untyped-def]
            nonlocal failed_once
            if (
                not failed_once
                and commit_id.startswith("commit:life-aftermath:proposal:")
            ):
                failed_once = True
                raise RuntimeError("simulated crash after outcome draw")
            return original_commit(events, commit_id=commit_id)

        monkeypatch.setattr(
            aftermath,
            "_commit",
            fail_once_after_recorded_draw,
        )
        third_wake = "event:trigger:clock:open-life-plan-settle"
        await app.tick(
            tick_id="open-life-plan-settle",
            logical_time_from=NOW + timedelta(minutes=11),
            logical_time_to=NOW + timedelta(minutes=71),
            observed_at=NOW + timedelta(minutes=71),
            trace_id="trace:open-life-plan-settle",
            causation_id="scheduler:open-life-plan",
            correlation_id="correlation:open-life-plan",
            reason="open-life-plan",
            run_life_ecology=False,
        )
        interrupted = await app.advance_life_ecology_once(
            wake_event_ref=third_wake,
            trace_id="trace:open-life-plan-settle",
            correlation_id="correlation:open-life-plan",
        )
        assert interrupted.reason_code == "life_ecology.aftermath_followup_failed"
        recorded_draws = [
            item.event
            for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
            if item.event.event_type == "RandomDrawRecorded"
            and item.event.source == "world-v2:life-aftermath-random"
        ]
        assert len(recorded_draws) == 1
        selected_candidate_ref = recorded_draws[0].payload()[
            "selected_candidate_ref"
        ]
        monkeypatch.setattr(aftermath, "_commit", original_commit)

        fourth_wake = "event:trigger:clock:open-life-plan-recover-settlement"
        await app.tick(
            tick_id="open-life-plan-recover-settlement",
            logical_time_from=NOW + timedelta(minutes=71),
            logical_time_to=NOW + timedelta(minutes=72),
            observed_at=NOW + timedelta(minutes=72),
            trace_id="trace:open-life-plan-recover-settlement",
            causation_id="scheduler:open-life-plan",
            correlation_id="correlation:open-life-plan",
            reason="open-life-plan",
            run_life_ecology=False,
        )
        settled = await app.advance_life_ecology_once(
            wake_event_ref=fourth_wake,
            trace_id="trace:open-life-plan-recover-settlement",
            correlation_id="correlation:open-life-plan",
        )

        projection = app._ledger.project()  # noqa: SLF001
        assert settled.aftermath_followup_status == "settled"
        assert settled.biographical_followup_status == "transitioned"
        assert projection.world_occurrences[0].settled_outcome_ref == (
            selected_candidate_ref
        )
        assert (
            sum(
                item.event.event_type == "RandomDrawRecorded"
                and item.event.source == "world-v2:life-aftermath-random"
                for item in app._ledger.export_replay_evidence().events  # noqa: SLF001
            )
            == 1
        )
        assert len(
            [
                npc
                for npc in projection.npcs
                if npc.source_event_ref is not None
                and npc.effect_descriptor_hash is not None
            ]
        ) == 1
        assert all(arc.arc_kind != "dynamic" for arc in projection.life_arcs)
        assert projection.pending_biographical_settlements == ()
    finally:
        app.close()
