from companion_daemon.character import load_character


def test_character_profile_includes_rich_persona() -> None:
    character = load_character("configs/character.yaml")
    prompt = character.system_prompt()

    assert "沈知栀" in prompt
    assert "华东师范大学" in prompt
    assert "刚认识" in prompt
    assert "成都" in prompt
    assert "城市漫游兴趣群" in prompt
    assert "纯粹的私聊文字" in prompt
