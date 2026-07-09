from companion_daemon.image_generation import life_image_prompt
from companion_daemon.image_prompt_builder import ChatImageMessage, build_image_prompt
from companion_daemon.character import load_character
from companion_daemon.visual_identity import load_visual_identity


def test_life_image_prompt_mentions_virtual_selfie() -> None:
    prompt = life_image_prompt("图书馆", kind="selfie")

    assert "fictional character selfie" in prompt
    assert "图书馆" in prompt
    assert "Character identity anchor" in prompt
    assert "青绿色小发夹" in prompt


def test_visual_identity_loads_anchor() -> None:
    identity = load_visual_identity("configs/visual_identity.yaml")

    assert identity.name == "沈知栀 / Celia Shen"
    assert "不要复用真实公众人物" in identity.negative_prompt


def test_build_image_prompt_keeps_character_identity_for_selfie() -> None:
    payload = build_image_prompt(
        "给我发一张水彩风格自拍看看",
        character=load_character("configs/character.yaml"),
    )

    assert payload.mode == "character"
    assert "Character identity anchor" in payload.prompt
    assert "watercolor" in payload.prompt
    assert "青绿色小发夹" in payload.prompt


def test_build_image_prompt_resolves_recent_visual_context() -> None:
    payload = build_image_prompt(
        "那张发我看看",
        character=load_character("configs/character.yaml"),
        recent_messages=[
            ChatImageMessage("我刚在图书馆靠窗的位置拍了一张梧桐叶的照片。", is_user=False),
        ],
    )

    assert payload.used_context
    assert "梧桐叶" in payload.directive
