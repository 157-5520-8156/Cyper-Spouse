from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from companion_daemon.world_v2.production_turn_application import (
    LifeEcologyComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return f"user:{platform_user_id}", f"user:{platform_user_id}"


class _Router:
    async def route(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("seven-day life ecology scenario does not run chat")


class _MainModel:
    async def propose(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("seven-day life ecology scenario does not run chat")


class _QuickRecovery:
    async def recover(self, _request, _failure):  # type: ignore[no-untyped-def]
        raise AssertionError("seven-day life ecology scenario does not run chat")


class _Transport:
    provider = "platform:test"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("seven-day life ecology scenario does not dispatch")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _SelectingLifeModel:
    model = "test-seven-day-life-model"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        del temperature
        capsule = json.loads(messages[-1]["content"])
        if "candidate" in capsule:
            return json.dumps({
                "decision": "select",
                "candidate_token": capsule["candidate"]["token"],
            })
        openings = capsule.get("openings", [])
        if not openings:
            return '{"decision":"no_op"}'
        # planned => (start, abandon); active => (pause, complete, abandon).
        # Complete on the third tick so every slot closes before the next slot.
        selected = openings[1] if len(openings) >= 3 else openings[0]
        return json.dumps({"decision": "select", "opening_token": selected["opening_token"]})


def _config(*, world_id: str, seed_path: Path) -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id=world_id,
        companion_actor_ref="agent:companion",
        reply_target="user:user.1",
        action_pump_owner=f"pump:{world_id}",
        local_timezone="Asia/Shanghai",
        life_ecology=LifeEcologyComposition.production_v1(seed_catalog_path=seed_path),
    )


def _utc(local: datetime) -> datetime:
    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


@pytest.mark.asyncio
async def test_seven_day_multi_period_multi_seed_production_event_richness_and_media_gate(
    tmp_path: Path,
) -> None:
    seed_path = Path("configs/world_seed.yaml")
    raw = yaml.safe_load(seed_path.read_text(encoding="utf-8"))["life_author_catalog"]
    openings = raw["openings"]
    opening_by_activity = {item["activity_kind"]: item for item in openings}
    expected_activities = set(opening_by_activity)
    all_activities: set[str] = set()
    all_outcomes: set[tuple[str, str]] = set()
    all_taxonomy: set[str] = set()
    private_taxonomy_count = 0
    all_declarations = 0

    local_start = datetime(2026, 7, 20, 0, 0)
    # Nine information-bearing windows spread across seven local days.  Each
    # opening gets a realistic opportunity to enter the production catalog;
    # we do not burn five identical scheduler wakes on every day merely to
    # inflate the tick count.
    # reviewed-life.4 widened the low-intensity filler openings, so evenings
    # are contested by rest/browsing; two extra evening slots keep the sole
    # environmental-opportunity walk observable in this random sample.
    slots = (
        (0, 7, 0),
        (0, 9, 30),
        (1, 10, 30),
        (1, 17, 30),
        (2, 13, 0),
        (3, 14, 30),
        (3, 19, 45),
        (4, 16, 45),
        (5, 18, 15),
        (6, 20, 15),
        (6, 21, 45),
    )
    for seed_index in range(3):
        world_id = f"world:life-richness:seed-{seed_index + 1}"
        bootstrap = _utc(local_start)
        app = build_sqlite_world_v2_turn_application(
            path=tmp_path / f"life-richness-{seed_index + 1}.sqlite",
            config=_config(world_id=world_id, seed_path=seed_path),
            identities=_Identities(), router=_Router(), main_model=_MainModel(),
            quick_recovery=_QuickRecovery(), transport=_Transport(),
            activity_lifecycle_model=_SelectingLifeModel(), now=bootstrap,
        )
        previous = bootstrap
        try:
            for day, hour, minute in slots:
                slot = _utc(local_start + timedelta(days=day, hours=hour, minutes=minute))
                started_at = slot + timedelta(minutes=1)
                # Ordinary completion tracks the accepted schedule window, so
                # each slot settles only after its own plan's window closes.
                phases = [("plan", slot), ("start", started_at)]
                for phase, at in phases:
                    await app.tick(
                        tick_id=f"seed-{seed_index + 1}:day-{day + 1}:{hour:02d}{minute:02d}:{phase}",
                        logical_time_from=previous, logical_time_to=at, observed_at=at,
                        trace_id=f"trace:richness:{seed_index}:{day}:{hour}:{phase}",
                        causation_id="scheduler:seven-day-richness",
                        correlation_id=f"correlation:richness:{seed_index}",
                        reason="seven-day-production-richness",
                    )
                    previous = at
                window = app._ledger.project().plans[-1].scheduled_window  # noqa: SLF001
                assert window is not None
                settle_at = window.closes_at + timedelta(seconds=30)
                await app.tick(
                    tick_id=f"seed-{seed_index + 1}:day-{day + 1}:{hour:02d}{minute:02d}:settle",
                    logical_time_from=previous, logical_time_to=settle_at, observed_at=settle_at,
                    trace_id=f"trace:richness:{seed_index}:{day}:{hour}:settle",
                    causation_id="scheduler:seven-day-richness",
                    correlation_id=f"correlation:richness:{seed_index}",
                    reason="seven-day-production-richness",
                )
                previous = settle_at

            projection = app._ledger.project()  # noqa: SLF001 - public composition evidence
            assert len(projection.plans) == len(slots)
            assert len(projection.world_occurrences) == len(projection.plans)
            assert len(projection.experiences) == len(projection.plans)
            assert len(projection.life_content_descriptors) == 2 * len(projection.plans)
            assert all(item.status == "completed" for item in projection.plans)
            assert all(item.status == "settled" for item in projection.world_occurrences)

            activities = {item.activity_kind for item in projection.plans}
            all_activities.update(activities)
            result_by_id = {
                outcome["id"]: activity["activity_kind"]
                for activity in openings for outcome in activity.get("outcomes", [])
            }
            for occurrence in projection.world_occurrences:
                assert occurrence.result_id is not None
                outcome_id = occurrence.result_id.rsplit(":", 1)[-1]
                all_outcomes.add((result_by_id[outcome_id], outcome_id))

            taxonomy = app.event_ecology_source_taxonomy()
            all_taxonomy.update(item.category for item in taxonomy)
            private_taxonomy_count += sum(
                item.privacy_class in {"personal", "private", "withhold"}
                for item in taxonomy
            )
            # A real SQLite ledger still requires an independently accepted
            # visual declaration; since reviewed-life.12 the life ecology owns
            # a reviewed visual-evidence author as that writer.  Candidates
            # may therefore exist, but every one must bind an accepted
            # declaration event and stay public/shareable.  Personal and
            # private lived sources still never become candidates merely
            # because their taxonomy exists, and without a configured
            # recipient no recipient-scoped declaration may appear at all.
            declaration_refs = {
                ref.event_id
                for ref in projection.committed_world_event_refs
                if ref.event_type == "ImageEvidenceDeclared"
            }
            assert not any(
                ref.event_type == "RecipientScopedImageEvidenceDeclared"
                for ref in projection.committed_world_event_refs
            )
            for candidate in projection.photo_candidates:
                assert candidate.privacy_ceiling in {"public", "shareable"}
                assert declaration_refs.intersection(candidate.source_event_refs)
            all_declarations += len(declaration_refs)
            # This fixture injects no media selection deployment, so declared
            # candidates must stay candidates: no opportunity, no planning.
            assert projection.media_opportunities == ()
        finally:
            app.close()

    # The production draw is genuinely random-authority backed; coverage is an
    # observed property, not a fixture that may force a preferred opening.
    # The daytime-oriented sample does not require overnight-only openings;
    # those have a separate post-midnight production vertical.  Until
    # reviewed-life.10 the catalog had only 12 present openings, so the test
    # could enumerate the tolerated misses of a 33-plan sample.
    # reviewed-life.11 nearly tripled the possibility space (34 openings), so
    # exhaustive coverage of one bounded sample is no longer the intent:
    # richness is now asserted as breadth (many distinct activities, sources,
    # domains, textures) while every observed activity must still be a
    # reviewed one — the anti-fabrication half of the original assertion.
    assert all_activities <= expected_activities
    assert len(all_activities) >= 10
    assert len(all_outcomes) >= 14
    assert "activity_result" in all_taxonomy
    assert "shared_experience" in all_taxonomy
    assert private_taxonomy_count > 0

    reached = [opening_by_activity[item] for item in sorted(all_activities)]
    assert len({item["source"] for item in reached}) >= 3
    assert len({item["domain"] for item in reached}) >= 6
    assert {item["social_shape"] for item in reached} >= {"alone"}
    assert len({item["deviation"] for item in reached}) >= 3
    assert len({item["visual_potential"] for item in reached}) >= 5
    assert len({item["privacy"] for item in reached}) >= 3
    # The image-supply lane is an observed property of the same sample: the
    # recorded chance draw is world-seeded and deterministic, so across three
    # seeds at least one settled shareable moment becomes declared evidence.
    assert all_declarations >= 1


@pytest.mark.asyncio
async def test_bounded_deterministic_seed_search_reaches_real_npc_social_aftermath(
    tmp_path: Path,
) -> None:
    """Prove the NPC branch exists in the real random space without forcing it."""

    seed_path = Path("configs/world_seed.yaml")
    local = datetime(2026, 7, 20, 10, 30)
    planned = _utc(local)
    found = False

    # reviewed-life.11 widened the Monday-morning candidate set from ~4 to ~9
    # openings, so one draw hits the NPC opening less often; the bounded
    # deterministic search needs a proportionally larger seed budget to keep
    # proving the branch exists without forcing it.
    for seed_index in range(1, 25):
        world_id = f"world:social-reach:{seed_index}"
        tick_prefix = f"social-search-{seed_index}"
        app = build_sqlite_world_v2_turn_application(
            path=tmp_path / f"social-reach-{seed_index}.sqlite",
            config=_config(world_id=world_id, seed_path=seed_path),
            identities=_Identities(), router=_Router(), main_model=_MainModel(),
            quick_recovery=_QuickRecovery(), transport=_Transport(),
            activity_lifecycle_model=_SelectingLifeModel(), now=_utc(datetime(2026, 7, 20)),
        )
        try:
            await app.tick(
                tick_id=f"{tick_prefix}:plan",
                logical_time_from=_utc(datetime(2026, 7, 20)),
                logical_time_to=planned, observed_at=planned,
                trace_id=f"trace:social-reach:{seed_index}:plan",
                causation_id="scheduler:social-reach",
                correlation_id=f"correlation:social-reach:{seed_index}",
                reason="bounded-social-reachability",
            )
            projection = app._ledger.project()  # noqa: SLF001 - production evidence
            assert len(projection.plans) == 1
            if projection.plans[0].activity_kind != "social.literature_reading_list":
                continue

            found = True
            started_at = planned + timedelta(minutes=1)
            window = projection.plans[0].scheduled_window
            assert window is not None
            # Ordinary completion tracks the accepted schedule window.
            settle_at = window.closes_at + timedelta(seconds=30)
            for phase, wake_from, at in (
                ("start", planned, started_at),
                ("settle", started_at, settle_at),
            ):
                await app.tick(
                    tick_id=f"{tick_prefix}:{phase}",
                    logical_time_from=wake_from,
                    logical_time_to=at, observed_at=at,
                    trace_id=f"trace:social-reach:{seed_index}:{phase}",
                    causation_id="scheduler:social-reach",
                    correlation_id=f"correlation:social-reach:{seed_index}",
                    reason="bounded-social-reachability",
                )

            projection = app._ledger.project()  # noqa: SLF001 - production evidence
            plan = projection.plans[0]
            occurrence = projection.world_occurrences[0]
            assert plan.status == "completed"
            assert plan.participant_refs == ("npc:literature-fan",)
            assert occurrence.status == "settled"
            assert "npc:literature-fan" in occurrence.participant_refs
            assert len(projection.experiences) == 1
            settlement_event_ref = occurrence.settlement_event_ref
            assert settlement_event_ref is not None
            assert any(
                item.process_kind == "npc_world_appraisal"
                and item.source_evidence_ref == settlement_event_ref
                for item in projection.trigger_processes
            )
            taxonomy = app.event_ecology_source_taxonomy()
            assert "npc_shared_outcome" in {item.category for item in taxonomy}
            assert projection.photo_candidates == ()
            break
        finally:
            app.close()

    assert found, "bounded deterministic seed search did not reach the reviewed NPC opening"
