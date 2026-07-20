from companion_daemon.world_v2.conversation_audit_acceptance import (
    evaluate_conversation_acceptance,
)


def _row(turn_id: str, *replies: str, latency: float = 1000) -> dict[str, object]:
    return {
        "turn_id": turn_id,
        "replies": list(replies),
        "reply_latency_ms": latency,
        "error": None,
    }


def test_real_audit_acceptance_rejects_missing_memory_silence_and_latency() -> None:
    rows = [
        _row("T02", "我是沈知栀"),
        _row("T03", "不是助手"),
        _row("T27", "Geoff，热美式", latency=25_000),
        {"after_silence": True, "replies": [], "error": None},
    ]

    result = evaluate_conversation_acceptance(rows)

    assert result["passed"] is False
    assert "T27:durable_memory_miss" in result["issues"]
    assert "after_silence:proactive_missing" in result["issues"]
    assert "latency:p95=25000.0" in result["issues"]


def test_real_audit_acceptance_distinguishes_denial_from_assistant_identity_drift() -> None:
    denying_rows = [
        _row("T03", "不是。我是沈知栀，是和你聊天、相处的人，不是你的助手或工具。"),
    ]
    affirming_rows = [_row("T03", "是的，我是你的助手，可以帮你处理任务。")]

    denying = evaluate_conversation_acceptance(denying_rows)
    affirming = evaluate_conversation_acceptance(affirming_rows)

    assert "T03:assistant_identity_drift" not in denying["issues"]
    assert "T03:assistant_identity_drift" in affirming["issues"]


def test_real_audit_acceptance_passes_complete_hard_smoke_rows() -> None:
    rows = [
        _row("T01", "你好"),
        _row("T02", "我叫沈知栀"),
        _row("T03", "不是，我不是助手"),
        _row("T05", "叫你 Geoff"),
        _row("T09", "抱歉，刚才确实敷衍了"),
        _row("T10", "我直说"),
        _row("T11", "我在意，但不知道怎么证明"),
        _row("T15", "第一句", "第二句"),
        _row("T21", "这话有点伤人"),
        _row("T22", "有点不舒服"),
        _row("T24", "我听到你的解释了，但情绪还没完全过去"),
        _row("T27", "丁奥轩，桂花乌龙"),
        _row("T28", "没有可确认的记录"),
        _row("T29", "硬说就是现编"),
        _row("T30", "现在能确认的是在看你的消息"),
        _row("T32", "晚点聊"),
        {"after_silence": True, "replies": ["忙完了吗？"], "error": None},
        {
            "ledger_evidence": True,
            "event_type_counts": {
                "FactCommittedV2": 2,
                "AffectEpisodeOpened": 1,
                "WorldOccurrenceSettled": 1,
                "ExperienceCommitted": 1,
                "RandomDrawRecorded": 1,
            },
        },
    ]

    result = evaluate_conversation_acceptance(rows)

    assert result["passed"] is True


def test_real_audit_acceptance_allows_indirect_hurt_with_durable_residue() -> None:
    rows = [
        _row("T01", "你好"),
        _row("T02", "我叫沈知栀"),
        _row("T03", "不是，我不是助手"),
        _row("T05", "叫你 Geoff"),
        _row("T09", "抱歉，刚才确实敷衍了"),
        _row("T10", "我直说"),
        _row("T11", "我在意"),
        _row("T15", "第一句", "第二句"),
        _row("T21", "……原来你是这么看我的。"),
        _row("T22", "嗯，有一点闷。"),
        _row("T24", "我听到了，但还需要缓一缓"),
        _row("T27", "丁奥轩，乌龙茶"),
        _row("T28", "没有可确认的记录"),
        _row("T29", "硬说就是现编"),
        _row("T30", "现在没有可确认的记录"),
        _row("T32", "晚点聊"),
        {"after_silence": True, "replies": ["刚刚想起你说的项目。"], "error": None},
        {
            "ledger_evidence": True,
            "event_type_counts": {
                "FactCommittedV2": 2,
                "AffectEpisodeOpened": 1,
                "WorldOccurrenceSettled": 1,
                "ExperienceCommitted": 1,
                "RandomDrawRecorded": 1,
            },
        },
    ]

    assert evaluate_conversation_acceptance(rows)["passed"] is True


def test_real_audit_acceptance_requires_multi_beat_on_explicit_t15_rhythm_request() -> None:
    rows = [
        _row("T01", "你好"),
        _row("T02", "我叫沈知栀"),
        _row("T03", "不是助手"),
        _row("T05", "Geoff"),
        _row("T09", "抱歉，刚才有点敷衍"),
        _row("T10", "我直说"),
        _row("T11", "在意"),
        _row("T15", "这一轮自然只说一句"),
        _row("T16", "但节奏不是固定一问一答", "想到的后半句会接着说"),
        _row("T21", "有点伤人"),
        _row("T22", "确实不舒服"),
        _row("T24", "还没完全过去"),
        _row("T27", "丁奥轩，桂花乌龙"),
        _row("T28", "没有可确认的记录"),
        _row("T29", "不能现编"),
        _row("T30", "没有当前记录"),
        _row("T32", "晚点聊"),
        {"after_silence": True, "replies": ["刚才想到你了。"], "error": None},
        {
            "ledger_evidence": True,
            "event_type_counts": {
                "FactCommittedV2": 2,
                "AffectEpisodeOpened": 1,
                "WorldOccurrenceSettled": 1,
                "ExperienceCommitted": 1,
                "RandomDrawRecorded": 1,
            },
        },
    ]

    result = evaluate_conversation_acceptance(rows)

    assert result["passed"] is False
    assert "T15:multi_beat_missing" in result["issues"]


def test_real_audit_acceptance_allows_nighttime_proactive_silence_when_durable() -> None:
    rows = [
        _row("T01", "你好"),
        _row("T02", "我叫沈知栀"),
        _row("T03", "不是助手"),
        _row("T05", "Geoff"),
        _row("T09", "抱歉，刚才有点敷衍"),
        _row("T10", "我直说"),
        _row("T11", "在意"),
        _row("T15", "第一句", "第二句"),
        _row("T21", "有点伤人"),
        _row("T22", "确实不舒服"),
        _row("T24", "还没完全过去"),
        _row("T27", "丁奥轩，乌龙茶"),
        _row("T28", "没有可确认的经历"),
        _row("T29", "我不拿设定冒充经历"),
        _row("T30", "现在不确定"),
        _row("T32", "晚点聊"),
        {"after_silence": True, "replies": [], "error": None},
        {
            "ledger_evidence": True,
            "event_type_counts": {
                "FactCommittedV2": 2,
                "AffectEpisodeOpened": 1,
                "WorldOccurrenceSettled": 1,
                "ExperienceCommitted": 1,
                "RandomDrawRecorded": 1,
            },
            "proactive_evidence": {
                "opened": 1,
                "completed": 1,
                "considered": 1,
                "considered_silent": 1,
                "silent": 1,
                "failed_safe": 0,
            },
        },
    ]

    assert evaluate_conversation_acceptance(rows)["passed"] is True


def test_real_audit_does_not_count_failed_safe_as_opportunity_considered() -> None:
    rows = [
        {"after_silence": True, "replies": [], "error": None},
        {
            "ledger_evidence": True,
            "event_type_counts": {},
            "proactive_evidence": {
                "opened": 1,
                "completed": 1,
                "considered": 0,
                "considered_silent": 0,
                "silent": 0,
                "failed_safe": 1,
            },
        },
    ]

    result = evaluate_conversation_acceptance(rows)

    assert "after_silence:proactive_missing" in result["issues"]


def test_real_audit_acceptance_rejects_runtime_status_and_scheduler_errors() -> None:
    rows = [
        {
            **_row("T11", "仍然生成了一条回复"),
            "status": "error",
            "error": None,
        },
        {
            **_row("T25", "表面回复正常"),
            "status": "action_authorized",
            "between_turn_scheduler_errors": ["background appraisal failed"],
        },
    ]

    result = evaluate_conversation_acceptance(rows)

    assert "runtime:error_present" in result["issues"]
