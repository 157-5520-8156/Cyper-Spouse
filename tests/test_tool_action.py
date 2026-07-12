from datetime import UTC, datetime
from pathlib import Path

import pytest

from companion_daemon.db import CompanionStore
from companion_daemon.tool_action import (
    FakeToolAdapter,
    FakeToolOutcome,
    ToolExecutionRequest,
)
from companion_daemon.world import WorldError, WorldKernel


NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def world_seed() -> dict[str, object]:
    return {
        "world_id": "zhizhi-tools-v1",
        "logical_at": NOW.isoformat(),
        "protagonist": {
            "id": "zhizhi",
            "name": "沈知栀",
            "kind": "companion",
            "templates": [],
        },
    }


def started_kernel(tmp_path: Path) -> tuple[WorldKernel, int]:
    kernel = WorldKernel(CompanionStore(tmp_path / "world.sqlite"))
    started = kernel.submit(
        {"type": "start_world", "seed": world_seed()}, expected_revision=0
    )
    user = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )
    return kernel, user.revision


def observe_confirmation(
    kernel: WorldKernel,
    *,
    revision: int,
    message_id: str,
    text: str = "确认执行",
) -> int:
    return kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": "zhizhi-tools-v1",
            "message_id": message_id,
            "user_id": "user:geoff",
            "text": text,
            "sent_at": NOW.isoformat(),
        },
        expected_revision=revision,
    ).revision


def test_fake_tool_adapter_returns_recordable_result_without_real_side_effects() -> None:
    adapter = FakeToolAdapter(
        outcomes={
            "calendar.create": FakeToolOutcome(
                status="delivered",
                detail="模拟日程已创建",
                output={"event_id": "fake-event-1"},
            )
        }
    )

    result = adapter.execute(
        ToolExecutionRequest(
            action_id="tool:proposal-1",
            proposal_id="proposal-1",
            tool_name="calendar.create",
            arguments={"title": "复习"},
        )
    )

    assert result.status == "delivered"
    assert result.execution_mode == "fake"
    assert result.effect_scope == "none"
    assert result.output == {"event_id": "fake-event-1"}
    assert result.to_world_result() == {
        "kind": "tool_execution",
        "status": "delivered",
        "execution_mode": "fake",
        "effect_scope": "none",
        "detail": "模拟日程已创建",
        "output": {"event_id": "fake-event-1"},
    }


def test_tool_proposal_requires_confirmation_before_an_action_exists(tmp_path: Path) -> None:
    kernel, revision = started_kernel(tmp_path)

    proposed = kernel.propose_tool_action(
        world_id="zhizhi-tools-v1",
        proposal_id="proposal-1",
        user_id="user:geoff",
        tool_name="calendar.create",
        arguments={"title": "复习"},
        summary="在日历中创建复习日程",
        risk="confirmation_required",
        expected_revision=revision,
    )

    assert [event.event_type for event in proposed.events] == [
        "ToolProposed",
        "UserConfirmationRequired",
    ]
    tool = kernel.snapshot(proposed.world_id)["tool_actions"]["proposal-1"]
    assert tool["status"] == "awaiting_confirmation"
    assert tool["confirmation_required"] is True
    assert tool["action_id"] is None
    assert "tool:proposal-1" not in kernel.snapshot(proposed.world_id)["actions"]

    with pytest.raises(WorldError, match="observed explicit confirmation"):
        kernel.authorize_tool_action(
            world_id=proposed.world_id,
            proposal_id="proposal-1",
            confirmation_message_id="not-observed",
            expected_revision=proposed.revision,
        )
    with pytest.raises(WorldError, match="unsettled tool operation"):
        kernel.validate_reply_candidate(
            proposed.world_id,
            {"reply_text": "日程已经创建好了。"},
            user_id="user:geoff",
        )


def test_authorized_fake_tool_execution_records_result_settlement_and_safe_summary(
    tmp_path: Path,
) -> None:
    kernel, revision = started_kernel(tmp_path)
    proposed = kernel.propose_tool_action(
        world_id="zhizhi-tools-v1",
        proposal_id="proposal-1",
        user_id="user:geoff",
        tool_name="calendar.create",
        arguments={"title": "复习"},
        summary="在日历中创建复习日程",
        risk="confirmation_required",
        expected_revision=revision,
    )
    authorized = kernel.authorize_tool_action(
        world_id=proposed.world_id,
        proposal_id="proposal-1",
        confirmation_message_id="m-confirm-1",
        expected_revision=observe_confirmation(
            kernel,
            revision=proposed.revision,
            message_id="m-confirm-1",
        ),
    )

    assert [event.event_type for event in authorized.events] == [
        "ToolAuthorized",
        "CostReservationDecided",
        "ActionScheduled",
    ]
    action = kernel.snapshot(proposed.world_id)["actions"]["tool:proposal-1"]
    assert action["status"] == "scheduled"
    assert action["kind"] == "tool_execution"

    settled = kernel.execute_fake_tool_action(
        world_id=proposed.world_id,
        proposal_id="proposal-1",
        adapter=FakeToolAdapter(
            outcomes={
                "calendar.create": FakeToolOutcome(
                    status="delivered",
                    detail="模拟日程已创建",
                    output={"event_id": "fake-event-1"},
                )
            }
        ),
        expected_revision=authorized.revision,
    )

    assert [event.event_type for event in settled.events] == [
        "ExternalResultRecorded",
        "ActionSettled",
        "CostReservationSettled",
        "NecessaryResultSummarized",
    ]
    tool = kernel.snapshot(proposed.world_id)["tool_actions"]["proposal-1"]
    assert tool["status"] == "simulated"
    assert tool["completed_in_reality"] is False
    assert tool["result_summary"] == "模拟完成（未执行真实操作）：模拟日程已创建"
    assert tool["result"]["effect_scope"] == "none"
    assert kernel.snapshot(proposed.world_id)["actions"]["tool:proposal-1"]["status"] == "delivered"
    assert kernel.validate_reply_candidate(
        proposed.world_id,
        {"reply_text": tool["result_summary"]},
        user_id="user:geoff",
    )["reply_text"].startswith("模拟完成（未执行真实操作）")
    assert [event.event_type for event in kernel.events(proposed.world_id)[-12:]] == [
        "ToolProposed",
        "UserConfirmationRequired",
        "UserMessageObserved",
        "ToolAuthorized",
        "CostReservationDecided",
        "ActionScheduled",
        "ActionAttempted",
        "ActionDispatchClaimed",
        "ExternalResultRecorded",
        "ActionSettled",
        "CostReservationSettled",
        "NecessaryResultSummarized",
    ]
    assert kernel.rebuild_projection(
        proposed.world_id, "world_current_state"
    ).matches_live is True


def test_rejected_or_blocked_tool_never_schedules_or_claims_completion(
    tmp_path: Path,
) -> None:
    kernel, revision = started_kernel(tmp_path)
    proposed = kernel.propose_tool_action(
        world_id="zhizhi-tools-v1",
        proposal_id="proposal-blocked",
        user_id="user:geoff",
        tool_name="bank.transfer",
        arguments={"amount": 1000},
        summary="转账 1000 元",
        risk="blocked",
        expected_revision=revision,
    )

    rejected = kernel.authorize_tool_action(
        world_id=proposed.world_id,
        proposal_id="proposal-blocked",
        confirmation_message_id="m-confirm-blocked",
        expected_revision=observe_confirmation(
            kernel,
            revision=proposed.revision,
            message_id="m-confirm-blocked",
        ),
    )

    assert [event.event_type for event in rejected.events] == ["ToolRejected"]
    tool = kernel.snapshot(proposed.world_id)["tool_actions"]["proposal-blocked"]
    assert tool["status"] == "rejected"
    assert tool["completed_in_reality"] is False
    assert tool["result_summary"].startswith("未执行：")
    assert "tool:proposal-blocked" not in kernel.snapshot(proposed.world_id)["actions"]
    with pytest.raises(WorldError, match="authorized"):
        kernel.execute_fake_tool_action(
            world_id=proposed.world_id,
            proposal_id="proposal-blocked",
            adapter=FakeToolAdapter(),
            expected_revision=rejected.revision,
        )


def test_failed_fake_tool_is_settled_but_never_summarized_as_completed(
    tmp_path: Path,
) -> None:
    kernel, revision = started_kernel(tmp_path)
    proposed = kernel.propose_tool_action(
        world_id="zhizhi-tools-v1",
        proposal_id="proposal-failed",
        user_id="user:geoff",
        tool_name="calendar.create",
        arguments={"title": "复习"},
        summary="在日历中创建复习日程",
        risk="confirmation_required",
        expected_revision=revision,
    )
    authorized = kernel.authorize_tool_action(
        world_id=proposed.world_id,
        proposal_id="proposal-failed",
        confirmation_message_id="m-confirm-failed",
        expected_revision=observe_confirmation(
            kernel,
            revision=proposed.revision,
            message_id="m-confirm-failed",
        ),
    )
    kernel.execute_fake_tool_action(
        world_id=proposed.world_id,
        proposal_id="proposal-failed",
        adapter=FakeToolAdapter(
            outcomes={
                "calendar.create": FakeToolOutcome(
                    status="failed",
                    detail="模拟适配器拒绝了请求",
                )
            }
        ),
        expected_revision=authorized.revision,
    )

    tool = kernel.snapshot(proposed.world_id)["tool_actions"]["proposal-failed"]
    assert tool["status"] == "failed"
    assert tool["completed_in_reality"] is False
    assert tool["result_summary"] == "未完成：模拟适配器拒绝了请求"
    with pytest.raises(WorldError, match="unsettled tool operation"):
        kernel.validate_reply_candidate(
            proposed.world_id,
            {"reply_text": "日程已经创建好了。"},
            user_id="user:geoff",
        )
