"""Historical catalog validation for retired aspiration crystallization data.

The catalog-to-plan author lane is intentionally absent from production.  The
remaining test protects replay/config validation without constructing the
retired character-model route.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology


_SEED = """
world_id: crystallization-test
life_author_catalog:
  version: reviewed-life-test.9
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
    - id: future-lakeside-walk
      activity_kind: commute.lakeside_walk
      source: intentional_goal
      domain: commute_walk
      social_shape: alone
      deviation: persist
      visual_potential: place
      privacy: private
      location_id: dorm-room
      local_windows: ["16:30-18:00"]
      weekdays: [0, 1, 2, 3, 4, 5, 6]
      duration_minutes: 50
      importance_bp: 4800
      advance_days_min: 1
      advance_days_max: 5
  aspiration_seeds:
    - id: aspire-liwa-seasons
      text: 想把丽娃河的四季拍全，凑成一组自己的小相册。
      privacy: shareable
      base_chance_bp: 10000
      crystallizes_into: future-lakeside-walk
"""


def test_catalog_rejects_an_unknown_crystallization_target(tmp_path: Path) -> None:
    bad = _SEED.replace(
        "crystallizes_into: future-lakeside-walk",
        "crystallizes_into: nowhere",
    )
    path = tmp_path / "bad-seed.yaml"
    path.write_text(bad.strip(), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown future opening"):
        ReviewedLifeSeedCatalog.from_yaml(
            path=path,
            chronology=LocalChronology("Asia/Shanghai"),
        )
