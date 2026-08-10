from datetime import UTC, datetime
import importlib.util
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parents[2] / "scripts" / "run_world_v2_conversation_audit.py"
_SPEC = importlib.util.spec_from_file_location("world_v2_conversation_audit", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_journey_start = _MODULE._journey_start


def test_journey_start_can_anchor_relative_fixture_at_current_wall_clock() -> None:
    current = datetime(2026, 8, 10, 4, 30, 17, 123456, tzinfo=UTC)

    assert _journey_start(
        {"started_at_local": "2026-07-17T00:05:00+08:00"},
        anchor_now=True,
        now=current,
    ) == datetime(2026, 8, 10, 4, 30, 17, tzinfo=UTC)


def test_journey_start_rejects_naive_current_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        _journey_start(
            {"started_at_local": "2026-07-17T00:05:00+08:00"},
            anchor_now=True,
            now=datetime(2026, 8, 10, 4, 30, 17),
        )
