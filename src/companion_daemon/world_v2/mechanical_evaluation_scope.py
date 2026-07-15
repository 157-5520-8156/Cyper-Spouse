"""Frozen boundary for one replay/performance evaluation fixture.

The scope is deliberately independent of ledger and platform adapters.  It
prevents a mechanical evaluator from silently widening an assertion to every
background Action or AffectEpisode in a shared test world.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json


class MechanicalEvaluationScopeError(ValueError):
    """A fixture does not define a reproducible mechanical assertion boundary."""


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AffectRetentionAssertion:
    """An episode that must not be cleared before this fixture's end cursor."""

    episode_id: str
    required_status: str = "active"

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or self.required_status not in {"active", "resolved", "superseded"}:
            raise MechanicalEvaluationScopeError("affect retention assertion is invalid")


@dataclass(frozen=True, slots=True)
class RandomDrawExpectation:
    status: str
    draw_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in {"installed", "not_applicable", "missing_required"}:
            raise MechanicalEvaluationScopeError("random draw expectation status is invalid")
        if any(not item.strip() for item in self.draw_ids) or len(set(self.draw_ids)) != len(self.draw_ids):
            raise MechanicalEvaluationScopeError("random draw ids must be unique and non-empty")
        if self.status == "installed" and not self.draw_ids:
            raise MechanicalEvaluationScopeError("installed random authority requires expected draw ids")
        if self.status != "installed" and self.draw_ids:
            raise MechanicalEvaluationScopeError("only installed random authority may require draw ids")


@dataclass(frozen=True, slots=True)
class PerformanceSampleExpectation:
    sample_id: str
    startup: str

    def __post_init__(self) -> None:
        if not self.sample_id.strip() or self.startup not in {"hot", "cold"}:
            raise MechanicalEvaluationScopeError("performance sample expectation is invalid")


@dataclass(frozen=True, slots=True)
class MechanicalEvaluationScope:
    fixture_id: str
    fixture_version: str
    world_id: str
    start_ledger_sequence: int
    end_ledger_sequence: int
    action_ids_expected_to_settle: tuple[str, ...]
    affect_assertions: tuple[AffectRetentionAssertion, ...]
    random_draw_expectation: RandomDrawExpectation
    performance_samples: tuple[PerformanceSampleExpectation, ...]

    def __post_init__(self) -> None:
        if not all(item.strip() for item in (self.fixture_id, self.fixture_version, self.world_id)):
            raise MechanicalEvaluationScopeError("fixture and world identities are required")
        if self.start_ledger_sequence < 0 or self.end_ledger_sequence < self.start_ledger_sequence:
            raise MechanicalEvaluationScopeError("fixture cursor range is invalid")
        for values, label in (
            (self.action_ids_expected_to_settle, "action ids"),
            (tuple(item.episode_id for item in self.affect_assertions), "affect episode ids"),
            (tuple(item.sample_id for item in self.performance_samples), "performance sample ids"),
        ):
            if any(not item.strip() for item in values) or len(set(values)) != len(values):
                raise MechanicalEvaluationScopeError(f"fixture {label} must be unique and non-empty")
        if not any(item.startup == "hot" for item in self.performance_samples):
            raise MechanicalEvaluationScopeError("fixture requires at least one hot performance sample")

    @property
    def fixture_manifest_hash(self) -> str:
        return _digest(
            {
                "fixture_id": self.fixture_id,
                "fixture_version": self.fixture_version,
                "world_id": self.world_id,
                "start_ledger_sequence": self.start_ledger_sequence,
                "end_ledger_sequence": self.end_ledger_sequence,
                "action_ids_expected_to_settle": self.action_ids_expected_to_settle,
                "affect_assertions": [
                    (item.episode_id, item.required_status) for item in self.affect_assertions
                ],
                "random_draw_expectation": (
                    self.random_draw_expectation.status,
                    self.random_draw_expectation.draw_ids,
                ),
                "performance_samples": [
                    (item.sample_id, item.startup) for item in self.performance_samples
                ],
            }
        )


__all__ = [
    "AffectRetentionAssertion",
    "MechanicalEvaluationScope",
    "MechanicalEvaluationScopeError",
    "PerformanceSampleExpectation",
    "RandomDrawExpectation",
]
