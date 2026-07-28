from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from companion_daemon.world_v2.life_author_seed import ReviewedLifeSeedCatalog
from companion_daemon.world_v2.local_chronology import LocalChronology


def test_future_candidates_recompute_biography_for_each_target_date(
    legacy_story_seed_path: Path,
) -> None:
    catalog = ReviewedLifeSeedCatalog.from_yaml(
        path=legacy_story_seed_path,
        chronology=LocalChronology("Asia/Shanghai"),
    )
    before_graduation = datetime(
        2028, 6, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    candidates = catalog.future_candidates_at(
        instant=before_graduation,
        plans=(),
        npcs=(),
        life_arcs=(),
        max_candidates=100,
    )
    after_graduation = tuple(
        item
        for item in candidates
        if item.target_local_date >= date(2028, 6, 30)
    )

    assert after_graduation
    assert all(
        item.biographical_context is not None
        and item.biographical_context.academic_phase == "graduated"
        for item in after_graduation
    )
    assert "future-photo-batch-sort" not in {
        item.opening.id for item in after_graduation
    }
