from pathlib import Path

import httpx
import pytest

from companion_daemon.image_generation import (
    ImageQualityResult,
    OpenAIImageGenerator,
    render_character_image,
    life_image_prompt,
)
from companion_daemon.image_prompt_builder import ChatImageMessage, build_image_prompt
from companion_daemon.character import load_character
from companion_daemon.visual_identity import load_visual_identity


def test_life_image_prompt_mentions_virtual_selfie() -> None:
    prompt = life_image_prompt("图书馆", kind="selfie")

    assert "fictional character selfie" in prompt
    assert "图书馆" in prompt
    assert "Character identity anchor" in prompt
    assert "青绿色小发夹" in prompt


def test_relationship_tier_prompt_uses_its_own_constraints() -> None:
    prompt = life_image_prompt(
        "温柔的晚安自拍",
        kind="selfie",
        profile="relationship_private",
        relationship_tier="tender",
    )

    assert "Tender intimacy" in prompt
    assert "underwear-focused composition" in prompt


def test_visual_identity_loads_anchor() -> None:
    identity = load_visual_identity("configs/visual_identity.yaml")

    assert identity.name == "沈知栀 / Celia Shen"
    assert "不要复用真实公众人物" in identity.negative_prompt
    assert identity.reference_assets("everyday_selfie")[0].endswith("celia-v2-reference-01-canonical.png")
    assert any("relationship-private" in path for path in identity.reference_assets("relationship_private"))


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


@pytest.mark.asyncio
async def test_openai_generator_submits_reference_images_for_identity_render(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["content_type"] = request.headers.get("content-type")
        observed["body"] = request.content
        return httpx.Response(200, json={"data": [{"b64_json": "cG5n"}]})

    reference = tmp_path / "canonical.png"
    reference.write_bytes(b"reference")
    generator = OpenAIImageGenerator("test-key", transport=httpx.MockTransport(handler))

    generated = await generator.generate(
        "same fictional character",
        output_path=tmp_path / "out.png",
        reference_images=(reference,),
    )

    assert generated.path.read_bytes() == b"png"
    assert str(observed["url"]).endswith("/images/edits")
    assert "multipart/form-data" in str(observed["content_type"])
    assert b'name="image[]"' in bytes(observed["body"])


@pytest.mark.asyncio
async def test_quality_gate_retries_once_when_first_render_is_rejected(tmp_path: Path) -> None:
    class Generator:
        calls = 0

        async def generate(self, _prompt: str, *, output_path: Path, size: str = "1024x1024"):
            self.calls += 1
            output_path.write_bytes(f"render-{self.calls}".encode())
            from companion_daemon.image_generation import GeneratedImage
            return GeneratedImage(output_path, _prompt)

    class Gate:
        calls = 0

        async def assess(self, _path: Path, *, prompt: str) -> ImageQualityResult:
            self.calls += 1
            return ImageQualityResult(passed=self.calls == 2, reason="first hand malformed")

    generator = Generator()
    gate = Gate()
    generated = await render_character_image(
        generator, "selfie", output_path=tmp_path / "out.png", quality_gate=gate
    )

    assert generated.path.read_bytes() == b"render-2"
    assert generator.calls == 2
    assert gate.calls == 2
