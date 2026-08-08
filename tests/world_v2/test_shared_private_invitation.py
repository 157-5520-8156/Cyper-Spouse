"""shared_private invitations: gate, plan, advisory, and expiry abandonment."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology

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
            return json.dumps(
                {
                    "decision": "select",
                    "candidate_token": capsule["shared_private_candidate"]["token"],
                }
            )
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


def test_ordinary_future_author_never_sees_shared_private_openings(
    tmp_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=_seed(tmp_path / "seed.yaml"),
        chronology=LocalChronology("Asia/Shanghai"),
    )
    default_shapes = catalog.future_candidates_at(
        instant=NOW,
        plans=(),
    )
    assert default_shapes == ()
    invitation_only = catalog.future_candidates_at(
        instant=NOW,
        plans=(),
        social_shapes=frozenset({"shared_private"}),
    )
    assert invitation_only
    assert all(item.opening.social_shape == "shared_private" for item in invitation_only)


def test_catalog_rejects_unreviewed_shared_private_shapes(tmp_path: Path) -> None:
    # Present-moment openings must not claim shared_private.
    bad_present = _SEED.replace(
        "      social_shape: alone\n", "      social_shape: shared_private\n", 1
    )
    path = tmp_path / "bad-present.yaml"
    path.write_text(bad_present.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match="not reviewed for this catalog section"):
        ReviewedLifeSeedCatalog.from_yaml(path=path, chronology=LocalChronology("Asia/Shanghai"))

    # A shared_private future opening without a closeness floor fails closed.
    bad_floor = _SEED.replace("      requires_relationship_closeness_bp: 0\n", "")
    path = tmp_path / "bad-floor.yaml"
    path.write_text(bad_floor.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match="closeness floor"):
        ReviewedLifeSeedCatalog.from_yaml(path=path, chronology=LocalChronology("Asia/Shanghai"))
