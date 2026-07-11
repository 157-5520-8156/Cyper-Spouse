"""Drive logical world time from recorded wall-clock observations."""
from __future__ import annotations

from datetime import datetime, timedelta

from companion_daemon.world import WorldDecision, WorldKernel


class WorldClockDriver:
    """The sole seam that converts wall time and a clock rate into world time."""

    def __init__(self, kernel: WorldKernel):
        self.kernel = kernel

    def tick(
        self,
        world_id: str,
        *,
        observed_now: datetime,
        expected_revision: int,
    ) -> WorldDecision | None:
        if observed_now.tzinfo is None:
            raise ValueError("observed_now must be timezone-aware")
        snapshot = self.kernel.snapshot(world_id)
        clock = snapshot["clock"]
        rate = int(clock.get("rate") or 0)
        if str(clock.get("mode") or "paused") == "paused" or rate == 0:
            return None

        raw_anchor = snapshot.get("clock_observed_at")
        if not raw_anchor:
            # Epochs created before the driver anchor was projected need one
            # bounded migration lookup.  The first successful tick records the
            # anchor, so steady-state ticks remain O(1) in ledger length.
            anchor = next(
                (
                    event
                    for event in reversed(self.kernel.events(world_id))
                    if event.event_type in {"ClockAdvanced", "ClockModeChanged"}
                ),
                None,
            )
            if anchor is None:
                return None
            raw_anchor = anchor.payload.get("observed_at") or anchor.observed_at
        observed_anchor = datetime.fromisoformat(str(raw_anchor))
        elapsed = observed_now - observed_anchor
        if elapsed <= timedelta(0):
            return None

        logical_now = datetime.fromisoformat(str(clock["logical_at"]))
        target = logical_now + timedelta(seconds=elapsed.total_seconds() * rate)
        return self.kernel.submit(
            {
                "type": "advance_clock",
                "world_id": world_id,
                "target_logical_at": target.isoformat(),
                "observed_at": observed_now.isoformat(),
                "idempotency_key": f"clock-tick:{world_id}:{observed_now.isoformat()}:{rate}",
            },
            expected_revision=expected_revision,
        )
