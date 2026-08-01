from companion_daemon.character import load_character


def test_character_profile_exposes_only_production_identity_sources() -> None:
    character = load_character("configs/character.yaml")
    identity_material = "\n".join(
        (
            *character.canonical_facts,
            *character.shared_history_facts,
            *character.counterpart_history_facts,
        )
    )

    assert character.name == "沈知栀"
    assert "华东师范大学" in identity_material
    assert "城市漫游兴趣群" in identity_material
    assert "没有可验证的宠物饲养经历" in identity_material
    assert "成都" not in identity_material
    assert character.counterpart_history_facts == []
    assert "age" not in character.identity
    assert "year" not in character.identity
    assert not hasattr(character, "origin_story")
    assert not hasattr(character, "example_messages")
    assert not hasattr(character, "system_prompt")
    assert character.relationship is None


def test_character_profile_does_not_freeze_the_current_relationship_stage() -> None:
    character = load_character("configs/character.yaml")
    per_turn_identity_material = "\n".join(
        (
            character.base_prompt,
            character.personality or "",
            character.speech or "",
            *character.style_rules,
            *character.boundaries,
        )
    )

    assert "刚认识" not in per_turn_identity_material
    assert "当前阶段" not in per_turn_identity_material


def test_character_persona_has_her_own_attention_instead_of_a_question_seeking_role() -> None:
    character = load_character("configs/character.yaml")

    assert character.personality is not None
    assert "始终有自己的思路、生活和情绪" in character.personality
    assert "不会为了维持对话把注意力固定在对方身上" in character.personality
    assert character.speech is not None
    assert "取决于当下的注意、情绪和关系情境" in character.speech
    assert "刚认识时语气礼貌、轻松、带一点好奇" not in character.speech
    assert "对用户有好奇" not in character.base_prompt
    assert all("想知道的时候才问" not in rule for rule in character.style_rules)
    assert all("虚拟" not in boundary for boundary in character.boundaries)
    assert any("World context" in boundary for boundary in character.boundaries)
