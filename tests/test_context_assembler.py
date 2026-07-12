from __future__ import annotations

from companion_daemon.context_assembler import (
    ContextAssembler,
    ContextBudgets,
    ContextEntry,
    LayerBudget,
)


def _entry(
    content: str,
    *,
    source_id: str,
    purpose: str,
    pinned: bool = False,
) -> ContextEntry:
    return ContextEntry(
        content=content,
        source_id=source_id,
        source="world-ledger",
        source_type="fact",
        subject="user:geoff",
        logical_at="2026-07-12T10:00:00+08:00",
        purpose=purpose,
        pinned=pinned,
    )


def test_assembler_exposes_exact_five_layers_with_character_and_item_budgets() -> None:
    budgets = ContextBudgets(
        character_core=LayerBudget(max_chars=6, max_items=1),
        user_profile=LayerBudget(max_chars=6, max_items=1),
        current_scene=LayerBudget(max_chars=6, max_items=1),
        retrieved_experiences=LayerBudget(max_chars=6, max_items=1),
        expression_guidance=LayerBudget(max_chars=6, max_items=1),
    )
    assembler = ContextAssembler(budgets)

    assembled = assembler.assemble(
        character_core=[_entry("真诚", source_id="core:values", purpose="identity")],
        user_profile=[_entry("住上海", source_id="fact:city", purpose="personalize")],
        current_scene=[_entry("在宿舍", source_id="scene:now", purpose="current_state")],
        retrieved_experiences=[
            _entry("散过步", source_id="experience:walk", purpose="continuity")
        ],
        expression_guidance=[
            _entry("温和但不附和", source_id="guidance:stance", purpose="expression")
        ],
    )

    assert list(assembled) == [
        "character_core",
        "user_profile",
        "current_scene",
        "retrieved_experiences",
        "expression_guidance",
    ]
    for layer in assembled.values():
        assert layer["max_chars"] == 6
        assert layer["max_items"] == 1
        assert layer["used_chars"] <= 6
        assert len(layer["entries"]) <= 1
        entry = layer["entries"][0]
        assert set(
            (
                "source_id",
                "source",
                "source_type",
                "subject",
                "logical_at",
                "purpose",
                "selection",
                "content",
            )
        ).issubset(entry)


def test_user_profile_excludes_superseded_and_disputed_facts_and_keeps_latest_conflict() -> None:
    assembler = ContextAssembler()
    base = {
        "source": "world-ledger",
        "source_type": "fact",
        "subject": "user:geoff",
        "purpose": "personalize",
        "conflict_key": "location:current",
    }
    user_profile = [
        ContextEntry(
            content="用户住在成都。",
            source_id="fact:chengdu",
            logical_at="2026-07-10T10:00:00+08:00",
            status="superseded",
            **base,
        ),
        ContextEntry(
            content="用户住在杭州。",
            source_id="fact:hangzhou",
            logical_at="2026-07-11T10:00:00+08:00",
            status="disputed",
            **base,
        ),
        ContextEntry(
            content="用户住在上海。",
            source_id="fact:shanghai",
            logical_at="2026-07-12T10:00:00+08:00",
            status="current",
            **base,
        ),
    ]

    assembled = assembler.assemble(
        character_core=[],
        user_profile=user_profile,
        current_scene=[],
        retrieved_experiences=[],
        expression_guidance=[],
    )

    assert [
        entry["source_id"] for entry in assembled["user_profile"]["entries"]
    ] == ["fact:shanghai"]


def test_pinned_entries_stay_while_rotating_entries_change_deterministically() -> None:
    assembler = ContextAssembler(
        ContextBudgets(user_profile=LayerBudget(max_chars=100, max_items=2))
    )
    entries = [
        _entry("名字是 Geoff", source_id="fact:name", purpose="identity", pinned=True),
        _entry("喜欢茶", source_id="fact:tea", purpose="personalize"),
        _entry("喜欢咖啡", source_id="fact:coffee", purpose="personalize"),
        _entry("喜欢散步", source_id="fact:walk", purpose="personalize"),
    ]

    def selected(rotation_key: str) -> list[str]:
        assembled = assembler.assemble(
            character_core=[],
            user_profile=entries,
            current_scene=[],
            retrieved_experiences=[],
            expression_guidance=[],
            rotation_key=rotation_key,
        )
        return [
            str(item["source_id"])
            for item in assembled["user_profile"]["entries"]
        ]

    assert selected("turn-a") == selected("turn-a")
    assert selected("turn-a")[0] == "fact:name"
    assert selected("turn-b")[0] == "fact:name"
    assert selected("turn-a") != selected("turn-b")


def test_world_context_adapter_builds_prompt_ready_layers_with_provenance() -> None:
    context = {
        "self_core": {
            "entity_id": "zhizhi",
            "name": "沈知栀",
            "stable_traits": ["慢热，有自己的判断"],
            "values": ["真诚比漂亮话重要"],
            "preferences": [],
            "relationship_principles": [],
            "speech_anchors": [],
            "boundaries": [],
            "continuity": {"active_goals": ["整理课程笔记"]},
            "source_id": "world-seed:zhizhi",
            "source": "configs/world_seed.yaml",
            "logical_at": "2026-07-11T09:00:00+08:00",
        },
        "user_profile": [
            {
                "source_id": "fact:tea",
                "source": "message:42",
                "source_type": "fact",
                "subject": "user:geoff",
                "logical_at": "2026-07-12T09:00:00+08:00",
                "value": "用户喜欢桂花乌龙。",
                "reference_state": "confirmed",
                "pinned": False,
            }
        ],
        "current_scene": {
            "logical_at": "2026-07-12T10:00:00+08:00",
            "location": "宿舍",
            "activity": "整理笔记",
            "activity_status": "active",
        },
        "current_scene_source": {
            "source_id": "current-scene:now",
            "source": "world_projection",
            "source_type": "current_scene",
            "subject": "zhizhi",
            "logical_at": "2026-07-12T10:00:00+08:00",
            "reference_state": "current",
            "content": "现在在宿舍，正在整理笔记。",
        },
    }
    retrieved = [
        {
            "source_id": "experience:walk",
            "source": "event:ExperienceCommitted:walk",
            "source_type": "experience",
            "subject": "zhizhi",
            "occurred_at": "2026-07-11T20:00:00+08:00",
            "reference_state": "committed",
            "content": "在校园散了会儿步。",
        }
    ]

    assembled = ContextAssembler().assemble_world_context(
        context,
        user_id="user:geoff",
        retrieved_experiences=retrieved,
        expression_guidance={
            "label": "guarded",
            "prompt_line": "可以关心，但不要无条件附和。",
            "rule_version": "behavior-v1",
        },
        rotation_key="message:43",
    )

    assert assembled["character_core"]["entries"][0]["selection"] == "pinned"
    assert assembled["user_profile"]["entries"] == [
        {
            "source_id": "fact:tea",
            "source": "message:42",
            "source_type": "fact",
            "subject": "user:geoff",
            "logical_at": "2026-07-12T09:00:00+08:00",
            "purpose": "personalize",
            "selection": "rotating",
            "content": "用户喜欢桂花乌龙。",
        }
    ]
    assert assembled["current_scene"]["entries"][0]["source_id"] == "current-scene:now"
    assert assembled["retrieved_experiences"]["entries"][0]["source_id"] == "experience:walk"
    assert assembled["expression_guidance"]["entries"][0]["content"] == (
        "guarded：可以关心，但不要无条件附和。"
    )
