from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.expression_episode import (
    AuthorizationResult,
    EpisodeCandidate,
    EpisodeExternalResult,
    EpisodeObservation,
    EpisodeOutcome,
    EpisodePolicy,
    EpisodeReplaySnapshot,
    ExpressionEpisode,
    FullCognitionResult,
    InnerSeed,
)
from companion_daemon.world_v2.interactive_turn_budget import (
    InteractiveTurnBudgetPolicy,
)
from companion_daemon.world_v2.schemas import Observation, ProjectionCursor


NOW = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + seconds, future))
        await future

    def advance(self, seconds: float) -> None:
        self.now += seconds
        ready = [item for item in self.sleepers if item[0] <= self.now]
        self.sleepers = [item for item in self.sleepers if item[0] > self.now]
        for _, future in ready:
            if not future.done():
                future.set_result(None)


def observation() -> EpisodeObservation:
    source = Observation(
        schema_version="world-v2.1",
        observation_id="observation:episode:1",
        world_id="world:episode",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:episode:1",
        causation_id="qq:1",
        correlation_id="conversation:1",
        source="platform:qq",
        source_event_id="qq:1",
        actor="user:primary",
        channel="qq",
        payload_ref="ingress:qq:1",
        payload_hash="sha256:" + "1" * 64,
        text="今天过得怎么样？",
        received_at=NOW,
        reply_context={"target": "user:primary", "platform_message_id": "qq:1"},
    )
    cursor = ProjectionCursor(
        world_revision=7, deliberation_revision=3, ledger_sequence=12
    )
    return EpisodeObservation(
        observation=source,
        observation_event_ref="event:trigger:observation:qq:1",
        observation_event_hash="a" * 64,
        cursor=cursor,
        inner_seed=InnerSeed(
            seed_id="inner-seed:1",
            capsule_id="b" * 64,
            observation_ref=source.observation_id,
            observation_event_ref="event:trigger:observation:qq:1",
            cursor=cursor,
            accepted_source_bindings=(
                "character-core:active",
                "affect:accepted",
                "relationship:primary",
                "memory:recent",
                "thread:open",
                "activity:current",
            ),
            advisory_source_bindings=("advisory:current-message:qq:1",),
        ),
    )


def candidate(
    phase: str,
    *,
    text: str = "我今天还挺充实的，刚忙完一点自己的事。你呢？",
    seed: InnerSeed | None = None,
    grounded: bool = True,
    advisory_as_fact: bool = False,
) -> EpisodeCandidate:
    seed = seed or observation().inner_seed
    bindings = (
        *seed.accepted_source_bindings,
        *seed.advisory_source_bindings,
        seed.observation_ref,
        seed.observation_event_ref,
    )
    return EpisodeCandidate(
        phase=phase,
        seed_id=seed.seed_id,
        observation_ref=seed.observation_ref,
        observation_event_ref=seed.observation_event_ref,
        cursor=seed.cursor,
        text=text,
        proposal_ref=f"proposal:{phase}:1",
        audit_ref=f"audit:{phase}:1",
        source_bindings=bindings if grounded else (),
        current_turn_advisory_claimed_as_fact=advisory_as_fact,
    )


class Authorizer:
    def __init__(self) -> None:
        self.calls: list[EpisodeCandidate] = []

    async def authorize(self, item, *, budget):  # type: ignore[no-untyped-def]
        self.calls.append(item)
        return AuthorizationResult(
            plan_id=f"plan:{item.phase}:1",
            action_ids=(f"action:{item.phase}:1",),
        )


@pytest.mark.asyncio
async def test_provisional_authorizes_while_full_cognition_is_still_running() -> None:
    clock = FakeClock()
    full_gate = asyncio.Event()
    seen_seeds: list[InnerSeed] = []
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        seen_seeds.append(seed)
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        seen_seeds.append(seed)
        await full_gate.wait()
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    result = await episode.respond(
        observation(),
        InteractiveTurnBudgetPolicy(clock=clock, sleep=clock.sleep).start(),
    )

    assert result.winner == "provisional"
    assert result.full_pending is True
    assert result.authorized_action_ids == ("action:provisional:1",)
    assert seen_seeds[0] is seen_seeds[1]
    assert authorizer.calls[0].source_bindings
    full_gate.set()
    await episode.aclose()


@pytest.mark.asyncio
async def test_full_wins_before_provisional_authorization_and_only_full_is_sent() -> None:
    provisional_gate = asyncio.Event()
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await provisional_gate.wait()
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    result = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )

    assert result.winner == "full"
    assert result.authorized_action_ids == ("action:full:1",)
    assert [item.phase for item in authorizer.calls] == ["full"]
    await episode.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["稍等，我在想。", "让我看看再说。", "我在想一下。"])
async def test_placeholder_provisional_is_rejected(text: str) -> None:
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed, text=text)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    result = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )

    assert result.winner == "full"
    assert [item.phase for item in authorizer.calls] == ["full"]
    assert "provisional.placeholder" in result.rejections


@pytest.mark.asyncio
async def test_ungrounded_provisional_is_rejected() -> None:
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed, grounded=False)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    result = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )

    assert result.winner == "full"
    assert "provisional.source_bindings_incomplete" in result.rejections


@pytest.mark.asyncio
async def test_delivered_provisional_cannot_be_superseded_but_full_can_append() -> None:
    full_gate = asyncio.Event()
    authorizer = Authorizer()
    cancellations: list[str] = []

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await full_gate.wait()
        return FullCognitionResult(
            disposition="supersede_pending",
            candidate=candidate("full", seed=seed),
            replacement_plan_ref="audit:replacement-plan:1",
        )

    async def cancel(action_id: str, *, reason: str) -> bool:
        cancellations.append(f"{action_id}:{reason}")
        return True

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        cancel_pending=cancel,
        policy=EpisodePolicy(mode="on"),
    )
    first = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )
    full_gate.set()
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id=first.authorized_action_ids[0], observed_state="delivered"
        )
    )

    assert settled.disposition == "complete_without_more"
    assert settled.fail_closed_reason == "supersede.delivered_immutable"
    assert cancellations == []
    assert [item.phase for item in authorizer.calls] == ["provisional"]


@pytest.mark.asyncio
async def test_undispatched_provisional_can_be_cancelled_atomically() -> None:
    full_gate = asyncio.Event()
    authorizer = Authorizer()
    cancelled: list[str] = []

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await full_gate.wait()
        return FullCognitionResult(disposition="cancel_pending")

    async def cancel(action_id: str, *, reason: str) -> bool:
        cancelled.append(action_id)
        return True

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        cancel_pending=cancel,
        policy=EpisodePolicy(mode="on"),
    )
    first = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )
    full_gate.set()
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id=first.authorized_action_ids[0], observed_state="authorized"
        )
    )

    assert settled.disposition == "cancel_pending"
    assert settled.cancelled_action_ids == first.authorized_action_ids
    assert cancelled == list(first.authorized_action_ids)


@pytest.mark.asyncio
async def test_dispatch_started_is_the_non_cancellable_boundary() -> None:
    full_gate = asyncio.Event()
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await full_gate.wait()
        return FullCognitionResult(disposition="cancel_pending")

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    first = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )
    full_gate.set()
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id=first.authorized_action_ids[0],
            observed_state="dispatch_started",
        )
    )

    assert settled.disposition == "complete_without_more"
    assert settled.fail_closed_reason == "cancel.dispatch_started"


@pytest.mark.asyncio
async def test_shadow_mode_records_candidate_but_authorizes_only_full() -> None:
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="shadow"),
    )
    result = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )

    assert result.winner == "full"
    assert result.shadow_provisional_ref == "proposal:provisional:1"
    assert [item.phase for item in authorizer.calls] == ["full"]


@pytest.mark.asyncio
async def test_duplicate_respond_reuses_episode_without_more_provider_calls() -> None:
    calls = {"provisional": 0, "full": 0}
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        calls["provisional"] += 1
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        calls["full"] += 1
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    source = observation()
    first, duplicate = await asyncio.gather(
        episode.respond(source, InteractiveTurnBudgetPolicy().start()),
        episode.respond(source, InteractiveTurnBudgetPolicy().start()),
    )

    assert duplicate == first
    assert calls == {"provisional": 1, "full": 1}


@pytest.mark.asyncio
async def test_restart_replay_does_not_call_either_provider_again() -> None:
    source = observation()
    replayed = EpisodeReplaySnapshot(
        outcome=EpisodeOutcome(
            episode_id=source.episode_id,
            status="provisional_authorized",
            winner="provisional",
            authorized_action_ids=("action:provisional:1",),
            plan_id="plan:provisional:1",
            full_pending=True,
            provider_slots_started=2,
        ),
        authorization=AuthorizationResult(
            plan_id="plan:provisional:1",
            action_ids=("action:provisional:1",),
        ),
        full_result=FullCognitionResult(disposition="complete_without_more"),
    )

    async def forbidden(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("replay must not invoke a model")

    async def lookup(episode_id: str) -> EpisodeReplaySnapshot | None:
        return replayed if episode_id == source.episode_id else None

    episode = ExpressionEpisode(
        provisional_author=forbidden,
        full_cognition=forbidden,
        authorizer=Authorizer(),
        replay_lookup=lookup,
        policy=EpisodePolicy(mode="on"),
    )

    result = await episode.respond(source, InteractiveTurnBudgetPolicy().start())
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id="action:provisional:1", observed_state="delivered"
        )
    )

    assert result == replayed.outcome
    assert settled.disposition == "complete_without_more"


@pytest.mark.asyncio
async def test_delivered_provisional_can_append_one_audited_full_plan() -> None:
    gate = asyncio.Event()
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await gate.wait()
        return FullCognitionResult(
            disposition="append", candidate=candidate("full", seed=seed)
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    first = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )
    gate.set()
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id=first.authorized_action_ids[0],
            observed_state="delivered",
            receipt_ref="receipt:provisional:1",
        )
    )

    assert settled.disposition == "append"
    assert settled.authorized_action_ids == ("action:full:1",)
    assert settled.delivered_refs == ("receipt:provisional:1",)
    assert [item.phase for item in authorizer.calls] == ["provisional", "full"]


@pytest.mark.asyncio
async def test_current_turn_advisory_cannot_be_claimed_as_accepted_affect() -> None:
    authorizer = Authorizer()

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed, advisory_as_fact=True)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    result = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )

    assert result.winner == "full"
    assert "provisional.advisory_claimed_as_fact" in result.rejections


@pytest.mark.asyncio
async def test_candidate_deadline_preserves_dispatch_reserve_and_two_slot_limit() -> None:
    clock = FakeClock()
    gate = asyncio.Event()
    calls: list[str] = []

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        calls.append("provisional")
        await gate.wait()
        return candidate("provisional", seed=seed)

    async def full(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        calls.append("full")
        await gate.wait()
        return FullCognitionResult(
            disposition="complete_without_more",
            candidate=candidate("full", seed=seed),
        )

    authorizer = Authorizer()
    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=authorizer,
        policy=EpisodePolicy(mode="on"),
    )
    # The deadline semantics under test are independent of the production
    # default numbers, so this policy pins its own explicit budget.
    responding = asyncio.create_task(
        episode.respond(
            observation(),
            InteractiveTurnBudgetPolicy(
                total_seconds=5.5,
                hedge_after_seconds=1.5,
                acceptance_dispatch_reserve_seconds=1.2,
                clock=clock,
                sleep=clock.sleep,
            ).start(),
        )
    )
    await asyncio.sleep(0)
    clock.advance(4.31)
    gate.set()
    result = await responding

    assert result.status == "failed_safe"
    assert result.provider_slots_started == 2
    assert sorted(calls) == ["full", "provisional"]
    assert authorizer.calls == []
    assert any(item.endswith("candidate_deadline") for item in result.rejections)


@pytest.mark.asyncio
async def test_supersede_without_replacement_acceptance_safely_cancels_pending() -> None:
    gate = asyncio.Event()
    cancelled: list[str] = []

    async def provisional(seed, _observation, _budget):  # type: ignore[no-untyped-def]
        return candidate("provisional", seed=seed)

    async def full(_seed, _observation, _budget):  # type: ignore[no-untyped-def]
        await gate.wait()
        return FullCognitionResult(disposition="supersede_pending")

    async def cancel(action_id: str, *, reason: str) -> bool:
        del reason
        cancelled.append(action_id)
        return True

    episode = ExpressionEpisode(
        provisional_author=provisional,
        full_cognition=full,
        authorizer=Authorizer(),
        cancel_pending=cancel,
        policy=EpisodePolicy(mode="on"),
    )
    first = await episode.respond(
        observation(), InteractiveTurnBudgetPolicy().start()
    )
    gate.set()
    settled = await episode.settle(
        EpisodeExternalResult(
            action_id=first.authorized_action_ids[0], observed_state="authorized"
        )
    )

    assert settled.disposition == "cancel_pending"
    assert settled.cancelled_action_ids == first.authorized_action_ids
    assert settled.fail_closed_reason == "supersede.replacement_plan_ref_unavailable"
    assert cancelled == list(first.authorized_action_ids)
