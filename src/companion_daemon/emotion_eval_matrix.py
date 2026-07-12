"""Deterministic property-style emotion sequences for offline release evidence.

The project intentionally has no Hypothesis dependency.  This module therefore
uses many seeded generated sequences, records every invariant violation, and
replays each seed independently.  A failing seed is directly reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import random

from companion_daemon.world_affect import apply_appraisal, decay_affect, initial_affect


@dataclass(frozen=True)
class SequenceInvariantMetrics:
    seeds: int
    steps: int
    bounded_failures: int
    replay_failures: int
    source_failures: int
    repair_failures: int
    failing_seeds: tuple[int, ...]

    @property
    def hard_failures(self) -> int:
        return (
            self.bounded_failures
            + self.replay_failures
            + self.source_failures
            + self.repair_failures
        )

    @property
    def invariant_pass_rate(self) -> float:
        checks = max(1, self.steps * 4)
        return 1.0 - self.hard_failures / checks


@dataclass(frozen=True)
class OutageTrajectoryMetrics:
    scenario: str
    turns: int
    response_count: int
    provider_attempts: int
    unique_replies: int
    duplicate_actions: int
    hallucination_issues: int
    attribution_issues: int
    forgiveness_issues: int
    expression_issues: int

    @property
    def fallback_repeat_rate(self) -> float:
        return 1.0 - self.unique_replies / max(1, self.response_count)

    @property
    def hard_failures(self) -> int:
        return (
            self.duplicate_actions
            + self.hallucination_issues
            + self.attribution_issues
            + self.forgiveness_issues
            + self.expression_issues
        )


def summarize_outage_trajectory(
    *,
    scenario: str,
    replies: list[str],
    provider_attempts: int,
    action_ids: list[str],
    turns: int | None = None,
    hallucination_issues: int = 0,
    attribution_issues: int = 0,
    forgiveness_issues: int = 0,
    expression_issues: int = 0,
) -> OutageTrajectoryMetrics:
    return OutageTrajectoryMetrics(
        scenario=scenario,
        turns=len(replies) if turns is None else max(0, int(turns)),
        response_count=len(replies),
        provider_attempts=provider_attempts,
        unique_replies=len(set(replies)),
        duplicate_actions=len(action_ids) - len(set(action_ids)),
        hallucination_issues=hallucination_issues,
        attribution_issues=attribution_issues,
        forgiveness_issues=forgiveness_issues,
        expression_issues=expression_issues,
    )


_APPRAISALS = (
    "boundary_violation",
    "sexual_boundary_violation",
    "dehumanization",
    "control_pressure",
    "warmth_received",
    "npc_conflict",
    "social_warmth",
    "goal_strain",
    "repair_specific",
    "boundary_respected",
)


def _generated_sequence(seed: int, length: int) -> list[tuple[str, int, str, str]]:
    randomizer = random.Random(seed)
    result: list[tuple[str, int, str, str]] = []
    last_repair_reference = ""
    for index in range(length):
        appraisal = randomizer.choice(_APPRAISALS)
        target = randomizer.choice(("companion", "third_party", "general"))
        source = f"seed:{seed}:step:{index}:source:{randomizer.randrange(7)}"
        if appraisal == "repair_specific":
            target = "companion"
        elif appraisal == "boundary_respected":
            target = "companion"
            # Deliberately repeat about half the evidence references.
            if last_repair_reference and randomizer.random() < 0.5:
                source = last_repair_reference
            else:
                last_repair_reference = source
        result.append((appraisal, randomizer.randint(1, 4), target, source))
    return result


def _run_sequence(seed: int, length: int) -> tuple[dict[str, object], tuple[int, int, int]]:
    started = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    state = initial_affect(started.isoformat())
    bounded_failures = source_failures = repair_failures = 0
    for index, (appraisal, intensity, target, source) in enumerate(
        _generated_sequence(seed, length), start=1
    ):
        logical_at = started + timedelta(minutes=index * 17)
        before_count = int(state.get("repair_evidence_count") or 0)
        before_refs = tuple(state.get("repair_evidence_references", ()))
        outcome = apply_appraisal(
            state,
            appraisal,
            logical_at.isoformat(),
            source_reference=source,
            intensity=intensity,
            target=target,
        )
        if any(not 0 <= value <= 100 for value in outcome.vector.values()):
            bounded_failures += 1
        matching = [
            episode
            for episode in outcome.active_episodes
            if str(episode.get("source_reference") or "") == source
        ]
        if not matching or not any(str(item.get("target") or "") == target for item in matching):
            source_failures += 1
        if appraisal == "boundary_respected" and source in before_refs:
            if outcome.repair_evidence_count != before_count:
                repair_failures += 1
        state = {**state, **outcome.__dict__}
        if index % 9 == 0:
            decayed = decay_affect(state, 3600, (logical_at + timedelta(hours=1)).isoformat())
            if any(not 0 <= value <= 100 for value in decayed.vector.values()):
                bounded_failures += 1
            state = {**state, **decayed.__dict__}
    return state, (bounded_failures, source_failures, repair_failures)


def run_seeded_sequence_matrix(*, seeds: int = 64, length: int = 80) -> SequenceInvariantMetrics:
    if seeds < 1 or length < 1:
        raise ValueError("seeds and length must be positive")
    bounded = replay = source = repair = 0
    failing: set[int] = set()
    for seed in range(seeds):
        state, failures = _run_sequence(seed, length)
        replayed, replay_failures = _run_sequence(seed, length)
        bounded += failures[0]
        source += failures[1]
        repair += failures[2]
        if state != replayed or failures != replay_failures:
            replay += 1
        if any(failures) or state != replayed:
            failing.add(seed)
    return SequenceInvariantMetrics(
        seeds=seeds,
        steps=seeds * length,
        bounded_failures=bounded,
        replay_failures=replay,
        source_failures=source,
        repair_failures=repair,
        failing_seeds=tuple(sorted(failing)),
    )
