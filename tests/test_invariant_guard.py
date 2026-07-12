from pathlib import Path

from companion_daemon.db import CompanionStore
from companion_daemon.invariant_guard import InvariantGuard
from companion_daemon.world import WorldKernel

from test_world_kernel import world_seed


def _world(tmp_path: Path) -> tuple[WorldKernel, str]:
    kernel = WorldKernel(CompanionStore(tmp_path / "guard.sqlite"))
    started = kernel.submit({"type": "start_world", "seed": world_seed()}, expected_revision=0)
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": started.world_id,
            "user_id": "user:geoff",
            "name": "geoff",
        },
        expected_revision=started.revision,
    )
    kernel.submit(
        {
            "type": "observe_user_message",
            "world_id": started.world_id,
            "message_id": "m:1",
            "user_id": "user:geoff",
            "text": "我有点失望。",
            "sent_at": "2026-07-11T09:00:00+00:00",
        },
        expected_revision=registered.revision,
    )
    return kernel, started.world_id


def test_guard_accepts_natural_fallible_expression_without_style_gate(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "我感觉你可能有点失望，是不是我刚才没接住？",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "accept"
    assert result.candidate is not None


def test_guard_hard_rejects_uncommitted_fact_reference(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "我昨天去看展了。",
            "mentioned_event_ids": ["experience:invented"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "experience:invented",
                    "text": "昨天去看展",
                }
            ],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "hard_reject"
    assert result.candidate is None
    assert result.reason
