from companion_daemon.image_generation import life_image_prompt
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
