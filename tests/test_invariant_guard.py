from datetime import UTC, datetime, timedelta
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


def test_guard_locally_redacts_one_unsettleable_sentence(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "我在西湖边喝咖啡。你今天还好吗？",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "accept_with_local_redaction"
    assert result.candidate is not None
    assert result.candidate["reply_text"] == "你今天还好吗？"


def test_guard_does_not_redact_candidates_with_provenance_or_actions(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "我在西湖边喝咖啡。你今天还好吗？",
            "mentioned_event_ids": ["message:m:1"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "message:m:1",
                    "text": "我有点失望。",
                    "assertion": "你有点失望。",
                }
            ],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "hard_reject"


def test_guard_rejects_ungrounded_identity_and_external_capability_claims(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "关心不是程序，是我想回应你。要不要我帮你远程点杯咖啡？",
            "mentioned_event_ids": [],
            "proposed_action_ids": [],
            "claims": [],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "hard_reject"
    assert result.reason == "absolute_meta_agency_guarantee"


def test_guard_does_not_let_an_unrelated_claim_cover_a_local_world_detail(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "你说你有点失望。图书馆门口新开了一家花店。",
            "mentioned_event_ids": ["message:m:1"],
            "proposed_action_ids": [],
            "claims": [
                {
                    "source_id": "message:m:1",
                    "text": "我有点失望。",
                    "assertion": "你说你有点失望。",
                }
            ],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "hard_reject"
    assert result.reason == "reply states a local world detail without a committed source id"


def test_guard_binds_a_scheduled_same_user_media_action(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    requested = kernel.submit(
        {
            "type": "request_media",
            "world_id": world_id,
            "request_id": "media:guard-test",
            "user_id": "user:geoff",
            "media_kind": "creative_image",
            "topic": "一张小图",
            "reason": "用户请求",
        },
        expected_revision=kernel.revision(world_id),
    )
    action_id = "media-generation:media:guard-test"

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "结果回来之前，不把这张图当成已经发出。",
            "mentioned_event_ids": [],
            "proposed_action_ids": [action_id],
            "claims": [],
        },
        user_id="user:geoff",
    )

    assert requested.revision == kernel.revision(world_id)
    assert result.disposition == "requires_action_settlement"
    assert result.action_ids == (action_id,)


def test_referenced_action_is_bound_to_reply_without_being_settled(
    tmp_path: Path,
) -> None:
    kernel, world_id = _world(tmp_path)
    action_id = "media-generation:media:dependency-test"
    kernel.submit(
        {
            "type": "request_media",
            "world_id": world_id,
            "request_id": "media:dependency-test",
            "user_id": "user:geoff",
            "media_kind": "creative_image",
            "topic": "一张小图",
            "reason": "用户请求",
        },
        expected_revision=kernel.revision(world_id),
    )

    delivery_id, _, outgoing_action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="simulator",
        text="我先把这张图排进去了，结果回来前不说它已经发出。",
        kind="reply",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        trace={
            "world_id": world_id,
            "user_id": "user:geoff",
            "appraisal": "user_request",
            "expression_policy": "自然说明待处理状态。",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "引用待结算媒体动作。",
            "action_settlement": {
                "action_ids": [action_id],
                "status": "pending_guard_settlement",
            },
        },
    )
    outgoing = kernel.snapshot(world_id)["actions"][outgoing_action_id]

    assert outgoing["action_dependencies"] == {
        "referenced_action_ids": [action_id],
        "semantics": "pending_external_action_reference",
    }
    assert kernel.snapshot(world_id)["actions"][action_id]["status"] == "scheduled"

    kernel.settle_outgoing_action(delivery_id, delivered=True)

    assert kernel.snapshot(world_id)["actions"][action_id]["status"] == "scheduled"


def test_terminal_or_other_user_action_cannot_be_referenced(tmp_path: Path) -> None:
    kernel, world_id = _world(tmp_path)
    registered = kernel.submit(
        {
            "type": "register_user",
            "world_id": world_id,
            "user_id": "user:other",
            "name": "other",
        },
        expected_revision=kernel.revision(world_id),
    )
    assert registered.revision == kernel.revision(world_id)
    kernel.submit(
        {
            "type": "request_media",
            "world_id": world_id,
            "request_id": "media:other-user",
            "user_id": "user:other",
            "media_kind": "creative_image",
            "topic": "一张小图",
            "reason": "用户请求",
        },
        expected_revision=kernel.revision(world_id),
    )

    result = InvariantGuard().resolve(
        kernel,
        world_id,
        {
            "reply_text": "结果回来之前，不把这件事当成已经完成。",
            "mentioned_event_ids": [],
            "proposed_action_ids": ["media-generation:media:other-user"],
            "claims": [],
        },
        user_id="user:geoff",
    )

    assert result.disposition == "hard_reject"
    assert result.reason == "reply action reference belongs to another user"
