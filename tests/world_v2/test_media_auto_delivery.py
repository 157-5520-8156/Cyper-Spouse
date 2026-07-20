"""World-owned delivery policy: guardrails, recovery, and fail-closed skips."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.media_auto_delivery import (
    MediaAutoDeliveryComposition,
    MediaAutoDeliveryWorker,
)
from companion_daemon.world_v2.media_v2 import media_delivery_action_id


NOW = datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
WORLD_ID = "world:auto-delivery"
POLICY = "system:world-v2:media-delivery-policy"


def _composition(**overrides: object) -> MediaAutoDeliveryComposition:
    values: dict[str, object] = {
        "delivery_target_ref": "conversation:qq:c2c:10001",
        "recipient_ref": "user:10001",
        "account_id": "account:media",
        "amount_limit": 0,
        "max_deliveries_per_day": 1,
        "min_gap": timedelta(hours=2),
    }
    values.update(overrides)
    return MediaAutoDeliveryComposition(**values)  # type: ignore[arg-type]


def _preview_bundle(index: int) -> dict[str, object]:
    return {
        "preview": SimpleNamespace(
            preview_id=f"preview:media:{index}",
            plan_id=f"plan:{index}",
            artifact_id=f"artifact:{index}",
            inspection_id=f"inspection:{index}",
            recipient_ref=None,
        ),
        "plan": SimpleNamespace(
            plan_id=f"plan:{index}", family="life_share", media_lane="ordinary_life",
            frozen_at=NOW,
        ),
        "inspection": SimpleNamespace(
            inspection_id=f"inspection:{index}", plan_id=f"plan:{index}",
            artifact_id=f"artifact:{index}", passed=True,
            observed_summary="ok", reason_code="accepted",
        ),
        "artifact": SimpleNamespace(
            artifact_id=f"artifact:{index}", plan_id=f"plan:{index}",
            artifact_ref=f"sidecar:{index}",
            artifact_hash="sha256:" + f"{index:064x}"[-64:],
        ),
    }


class _Ledger:
    world_id = WORLD_ID

    def __init__(self, projection: SimpleNamespace) -> None:
        self.projection = projection

    def project(self) -> SimpleNamespace:
        return self.projection


class _Application:
    def __init__(self) -> None:
        self.approvals: list[object] = []
        self.deliveries: list[dict[str, object]] = []

    async def approve_media_automatic_delivery(self, *, approval, **_kwargs):  # type: ignore[no-untyped-def]
        self.approvals.append(approval)
        return approval

    async def deliver_approved_media_once(self, **kwargs):  # type: ignore[no-untyped-def]
        self.deliveries.append(kwargs)
        return SimpleNamespace(status="processed", action_id="action:media-delivery:test")


def _projection(
    *,
    bundles: tuple[dict[str, object], ...],
    approvals: tuple[object, ...] = (),
    actions: tuple[object, ...] = (),
    deliveries: tuple[object, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        logical_time=NOW,
        media_previews=tuple(item["preview"] for item in bundles),
        media_plans=tuple(item["plan"] for item in bundles),
        media_inspections=tuple(item["inspection"] for item in bundles),
        media_artifacts=tuple(item["artifact"] for item in bundles),
        media_delivery_approvals=approvals,
        media_deliveries=deliveries,
        actions=actions,
    )


def _approval(index: int, *, approved_at: datetime, revision: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        approval_id=f"approval:media:preview:media:{index}",
        entity_revision=revision,
        operator_ref=POLICY,
        approved_at=approved_at,
        expires_at=approved_at + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_policy_approves_and_delivers_one_pending_preview() -> None:
    application = _Application()
    worker = MediaAutoDeliveryWorker(
        application=application,
        ledger=_Ledger(_projection(bundles=(_preview_bundle(1),))),
        composition=_composition(),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "delivered_attempted"
    assert len(application.approvals) == 1
    approval = application.approvals[0]
    assert approval.operator_ref == POLICY
    assert approval.delivery_target_ref == "conversation:qq:c2c:10001"
    assert application.deliveries[0]["actor"] == POLICY


@pytest.mark.asyncio
async def test_daily_cap_blocks_a_second_new_send_decision() -> None:
    application = _Application()
    consumed = _approval(1, approved_at=NOW - timedelta(hours=3))
    first = _preview_bundle(1)
    projection = _projection(
        bundles=(first, _preview_bundle(2)),
        approvals=(consumed,),
        deliveries=(SimpleNamespace(plan_id="plan:1"),),
    )
    worker = MediaAutoDeliveryWorker(
        application=application, ledger=_Ledger(projection), composition=_composition(),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "budget_exhausted"
    assert application.approvals == [] and application.deliveries == []


@pytest.mark.asyncio
async def test_min_gap_defers_a_new_send_decision() -> None:
    application = _Application()
    recent = _approval(1, approved_at=NOW - timedelta(minutes=10))
    projection = _projection(
        bundles=(_preview_bundle(1), _preview_bundle(2)),
        approvals=(recent,),
        deliveries=(SimpleNamespace(plan_id="plan:1"),),
    )
    worker = MediaAutoDeliveryWorker(
        application=application,
        ledger=_Ledger(projection),
        composition=_composition(max_deliveries_per_day=5),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "min_gap"
    assert application.approvals == []


@pytest.mark.asyncio
async def test_existing_current_approval_is_redriven_without_a_new_decision() -> None:
    application = _Application()
    approval = _approval(1, approved_at=NOW - timedelta(minutes=5))
    projection = _projection(bundles=(_preview_bundle(1),), approvals=(approval,))
    worker = MediaAutoDeliveryWorker(
        application=application, ledger=_Ledger(projection), composition=_composition(),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "delivered_attempted"
    assert application.approvals == []
    assert application.deliveries[0]["approval_revision"] == 1


@pytest.mark.asyncio
async def test_terminal_failed_delivery_is_never_resent_by_the_policy() -> None:
    application = _Application()
    approval = _approval(1, approved_at=NOW - timedelta(hours=3))
    failed_action = SimpleNamespace(
        action_id=media_delivery_action_id(
            world_id=WORLD_ID,
            approval_id=approval.approval_id,
            approval_revision=1,
        ),
        state="failed",
    )
    projection = _projection(
        bundles=(_preview_bundle(1),), approvals=(approval,), actions=(failed_action,),
    )
    worker = MediaAutoDeliveryWorker(
        application=application,
        ledger=_Ledger(projection),
        composition=_composition(max_deliveries_per_day=5),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "idle"
    assert application.approvals == [] and application.deliveries == []


@pytest.mark.asyncio
async def test_failed_inspection_previews_are_ignored() -> None:
    application = _Application()
    bundle = _preview_bundle(1)
    bundle["inspection"] = SimpleNamespace(
        inspection_id="inspection:1", plan_id="plan:1", artifact_id="artifact:1",
        passed=False, observed_summary=None, reason_code="rejected",
    )
    worker = MediaAutoDeliveryWorker(
        application=application,
        ledger=_Ledger(_projection(bundles=(bundle,))),
        composition=_composition(),
    )
    result = await worker.drain_once(trace_id="trace:t", correlation_id="corr:t")
    assert result.status == "idle"
    assert application.approvals == []
