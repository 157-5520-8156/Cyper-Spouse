from companion_daemon.self_core import SelfCore, parse_self_core, build_self_core_prompt


def test_self_core_to_prompt_block_has_sections() -> None:
    core = SelfCore(
        identity="我叫沈知栀，华东师范大学大二学生。",
        user_profile="他在成都，读过《我与地坛》。",
        relationship="刚认识，聊过书和天气。",
        knowledge_boundary="不知道他的真名和学校。",
        active_threads=["他上次说明天考试"],
    )
    block = core.to_prompt_block()
    assert "自我认知" in block
    assert "我是谁" in block
    assert "我了解的用户" in block
    assert "我们的关系" in block
    assert "我不确定的" in block
    assert "还在想的" in block
    assert "沈知栀" in block
    assert "成都" in block


def test_self_core_roundtrip_storage() -> None:
    core = SelfCore(
        identity="我叫沈知栀。",
        user_profile="他在成都。",
        relationship="刚认识。",
        knowledge_boundary="不知道真名。",
        active_threads=["考试", "天气"],
    )
    text = core.to_storage_text()
    restored = SelfCore.from_storage_text(text)
    assert restored.identity == "我叫沈知栀。"
    assert restored.user_profile == "他在成都。"
    assert restored.relationship == "刚认识。"
    assert restored.knowledge_boundary == "不知道真名。"
    assert restored.active_threads == ["考试", "天气"]


def test_self_core_initial() -> None:
    core = SelfCore.initial()
    assert "沈知栀" in core.identity
    assert "角色档案" in core.identity
    assert core.active_threads == []


def test_parse_self_core_valid() -> None:
    raw = """我叫沈知栀，通过读书群认识了用户。
---
他在成都，好像在读大学。
---
刚认识，聊过书和天气。
---
不知道他的真名和学校。
---
他说明天考试||天气很冷"""
    core = parse_self_core(raw)
    assert core is not None
    assert "沈知栀" in core.identity
    assert "成都" in core.user_profile
    assert "刚认识" in core.relationship
    assert "真名" in core.knowledge_boundary
    assert core.active_threads == ["他说明天考试", "天气很冷"]


def test_parse_self_core_too_few_parts() -> None:
    assert parse_self_core("只有一部分") is None
    assert parse_self_core("a---b---c") is None
    assert parse_self_core("a---b---c---d") is not None


def test_build_self_core_prompt_includes_memories() -> None:
    memories = [
        {"kind": "life_fact", "content": "用户在成都"},
        {"kind": "favorite_thing", "content": "用户读过《我与地坛》"},
    ]
    prompt = build_self_core_prompt(memories, "stranger", "calm")
    assert "用户在成都" in prompt
    assert "我与地坛" in prompt
    assert "stranger" in prompt
    assert "calm" in prompt
    assert "静态角色档案会在别处提供" in prompt
