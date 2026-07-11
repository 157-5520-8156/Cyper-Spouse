from datetime import datetime, timedelta
from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.world import WorldKernel
from companion_daemon.world_clock import WorldClockDriver


def test_accelerated_clock_tick_advances_from_recorded_wall_time(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    mode = kernel.submit(
        {
            "type": "set_clock_mode",
            "world_id": started.world_id,
            "mode": "accelerated",
            "rate": 8,
            "idempotency_key": "clock-mode-8x",
        },
        expected_revision=started.revision,
    )
    anchor = datetime.fromisoformat(kernel.events(started.world_id)[-1].observed_at)
    logical_before = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))

    decision = WorldClockDriver(kernel).tick(
        started.world_id,
        observed_now=anchor + timedelta(seconds=5),
        expected_revision=mode.revision,
    )

    assert decision is not None
    logical_after = datetime.fromisoformat(str(kernel.snapshot(started.world_id)["clock"]["logical_at"]))
    assert logical_after - logical_before == timedelta(seconds=40)
    assert decision.events[0].payload["observed_at"] == (anchor + timedelta(seconds=5)).isoformat()


def test_paused_clock_tick_is_a_noop(tmp_path: Path) -> None:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    anchor = datetime.fromisoformat(kernel.events(started.world_id)[-1].observed_at)

    decision = WorldClockDriver(kernel).tick(
        started.world_id,
        observed_now=anchor + timedelta(hours=1),
        expected_revision=started.revision,
    )

    assert decision is None
    assert kernel.revision(started.world_id) == started.revision
