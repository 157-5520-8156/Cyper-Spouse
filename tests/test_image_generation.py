from pathlib import Path
import json

import httpx
import pytest

from companion_daemon.image_generation import (
    CivitaiKrea2ImageGenerator,
    CivitaiTemplateWorkflowImageGenerator,
    CivitaiWorkflowImageGenerator,
    ImageGenerationProviderError,
    ImageQualityResult,
    OpenAIImageGenerator,
    VolcArkImageGenerator,
    render_character_image,
    life_image_prompt,
    visual_reference_paths,
)
from companion_daemon.visual_identity import load_visual_identity


def test_civitai_specialized_template_defaults_to_high_priority() -> None:
    template = json.loads(
        Path("configs/civitai-krea2-celia-realism-template.json").read_text(encoding="utf-8")
    )

    assert template["steps"][0]["priority"] == "high"


def test_openai_client_uses_an_explicit_proxy_without_inheriting_environment(monkeypatch) -> None:
    import companion_daemon.image_generation as image_generation

    observed: dict[str, object] = {}
    sentinel = object()

    def fake_async_client(**options):
        observed.update(options)
        return sentinel

    monkeypatch.setattr(image_generation.httpx, "AsyncClient", fake_async_client)

    client = image_generation._openai_client(
        timeout=1,
        proxy_url="http://127.0.0.1:7897",
        transport=None,
    )

    assert client is sentinel
    assert observed["proxy"] == "http://127.0.0.1:7897"
    assert observed["trust_env"] is False


def test_life_image_prompt_mentions_virtual_selfie() -> None:
    prompt = life_image_prompt("图书馆", kind="selfie")

    assert "selfie-style image" in prompt
    assert "图书馆" in prompt
    assert "Character identity anchor" in prompt
    assert "右侧脸颊有一颗浅色小痣" in prompt


def test_personal_media_prompt_uses_capture_mode_instead_of_forcing_selfie() -> None:
    prompt = life_image_prompt(
        "在展览门口留个到此一游的打卡",
        kind="character_media",
        capture_mode="check_in_timer",
    )

    assert "phone propped on a stable surface" in prompt
    assert "selfie-style" not in prompt
    assert "no arm reaches toward the camera" in prompt
    assert "Selfie style:" not in prompt
    assert "Camera style:" in prompt


def test_unfiltered_prompt_allows_natural_imperfection_but_not_degradation() -> None:
    prompt = life_image_prompt(
        "刚跑完步的随手照",
        kind="character_media",
        capture_mode="unfiltered",
    )

    assert "natural, harmless imperfection" in prompt
    assert "not humiliating" in prompt


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
    assert identity.reference_assets("everyday_selfie")[0].endswith("08-cafe-phone-canonical.png")
    assert any("02-bedtime-close-selfie" in path for path in identity.reference_assets("relationship_private"))


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
    assert b'name="quality"' in bytes(observed["body"])
    assert b"medium" in bytes(observed["body"])


@pytest.mark.asyncio
async def test_civitai_generator_freezes_one_canonical_reference_into_variant_workflow(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "artifact-redirect.test":
            return httpx.Response(
                302,
                headers={"Location": "https://artifact.test/result.png"},
            )
        if request.url.host == "artifact.test":
            return httpx.Response(200, content=b"civitai-image")
        observed["url"] = str(request.url)
        observed["headers"] = dict(request.headers)
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "workflow-1",
                "status": "succeeded",
                "steps": [
                    {"output": {"images": [{"url": "https://artifact-redirect.test/result.png"}]}}
                ],
            },
        )

    canonical = tmp_path / "canonical.png"
    secondary = tmp_path / "secondary.png"
    canonical.write_bytes(b"canonical-identity")
    secondary.write_bytes(b"secondary-angle")
    generated = await CivitaiWorkflowImageGenerator(
        "civitai-test-key",
        model="urn:air:sdxl:checkpoint:civitai:312530@2840768",
        transport=httpx.MockTransport(handler),
    ).generate(
        "same fictional adult character",
        output_path=tmp_path / "out.png",
        reference_images=(canonical, secondary),
    )

    assert generated.path.read_bytes() == b"civitai-image"
    assert "wait=180" in str(observed["url"])
    assert observed["headers"]["authorization"] == "Bearer civitai-test-key"  # type: ignore[index]
    payload = observed["payload"]
    assert payload["ephemeral"] is True
    assert payload["ephemeral"] is True  # type: ignore[index]
    assert payload["allowMatureContent"] is True  # type: ignore[index]
    image_input = payload["steps"][0]["input"]  # type: ignore[index]
    assert image_input["operation"] == "createVariant"
    assert image_input["model"] == "urn:air:sdxl:checkpoint:civitai:312530@2840768"
    assert image_input["image"] == "data:image/png;base64,Y2Fub25pY2FsLWlkZW50aXR5"
    assert "secondary-angle" not in image_input["image"]


@pytest.mark.asyncio
async def test_civitai_krea2_generator_uses_generic_imagegen_lora_stack(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "artifact.test":
            return httpx.Response(200, content=b"krea2-image")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "workflow-krea2",
                "status": "succeeded",
                "steps": [{"output": {"images": [{"url": "https://artifact.test/result.png"}]}}],
            },
        )

    reference = tmp_path / "identity.png"
    reference.write_bytes(b"ignored-by-krea2-generic")
    generated = await CivitaiKrea2ImageGenerator(
        "civitai-test-key",
        model="turbo",
        loras=(
            ("urn:air:krea2:lora:civitai:capability@1", 1.0),
            ("urn:air:krea2:lora:civitai:identity@2", 1.0),
            ("urn:air:krea2:lora:civitai:realism@3", 0.1),
        ),
        transport=httpx.MockTransport(handler),
    ).generate(
        "frozen media plan prompt",
        output_path=tmp_path / "out.png",
        reference_images=(reference,),
    )

    assert generated.path.read_bytes() == b"krea2-image"
    payload = observed["payload"]
    image_input = payload["steps"][0]["input"]  # type: ignore[index]
    assert image_input["ecosystem"] == "krea2"
    assert image_input["engine"] == "comfy"
    assert image_input["model"] == "turbo"
    assert image_input["operation"] == "createImage"
    assert image_input["sampler"] == "euler"
    assert image_input["scheduler"] == "simple"
    assert image_input["loras"] == [
        {"model": "urn:air:krea2:lora:civitai:capability@1", "strength": 1.0},
        {"model": "urn:air:krea2:lora:civitai:identity@2", "strength": 1.0},
        {"model": "urn:air:krea2:lora:civitai:realism@3", "strength": 0.1},
    ]
    assert "image" not in image_input


@pytest.mark.asyncio
async def test_civitai_template_generator_preserves_realism_recipe_and_only_fills_whitelisted_slots(
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "artifact.test":
            return httpx.Response(200, content=b"template-image")
        if request.url.path.endswith("/blobs/upload"):
            return httpx.Response(200, json={"uploadUrl": "https://upload.test/blob"})
        if request.url.host == "upload.test":
            assert request.content == b"identity-anchor"
            return httpx.Response(201, json={"id": "identity-blob-1", "available": True, "type": "image"})
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "workflow-template",
                "status": "succeeded",
                "steps": [{"output": {"images": [{"url": "https://artifact.test/result.png"}]}}],
            },
        )

    template = tmp_path / "realism-template.json"
    template.write_text(
        json.dumps(
            {
                "tags": ["project:krea2-celia-realism"],
                "allowMatureContent": True,
                "steps": [
                    {
                        "$type": "imageGen",
                        "name": "krea2-celia-realism",
                        "priority": "low",
                        "input": {
                            "engine": "comfy",
                            "ecosystem": "krea2",
                            "model": "turbo",
                            "operation": "createImage",
                            "images": ["{{identity_reference_data_url}}"],
                            "prompt": "{{render_prompt}}",
                            "negativePrompt": "keep-real-phone-photo-texture",
                            "width": 512,
                            "height": 768,
                            "steps": 8,
                            "cfgScale": 1,
                            "sampler": "euler",
                            "scheduler": "simple",
                            "seed": "{{seed}}",
                            "quantity": 1,
                            "loras": {
                                "urn:air:krea2:lora:civitai:identity@1": 1.0,
                                "urn:air:krea2:lora:civitai:capability@2": 1.0,
                                "urn:air:krea2:lora:civitai:realism@3": 0.1,
                            },
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reference = tmp_path / "identity.png"
    reference.write_bytes(b"identity-anchor")

    generated = await CivitaiTemplateWorkflowImageGenerator(
        "civitai-test-key",
        template_path=template,
        transport=httpx.MockTransport(handler),
    ).generate(
        "frozen matrix prompt",
        output_path=tmp_path / "out.png",
        size="1024x1536",
        reference_images=(reference,),
    )

    assert generated.path.read_bytes() == b"template-image"
    payload = observed["payload"]
    image_input = payload["steps"][0]["input"]  # type: ignore[index]
    assert image_input["prompt"] == "frozen matrix prompt"
    assert image_input["images"] == ["identity-blob-1"]
    assert image_input["width"] == 1024
    assert image_input["height"] == 1536
    assert isinstance(image_input["seed"], int)
    assert image_input["loras"] == {
        "urn:air:krea2:lora:civitai:identity@1": 1.0,
        "urn:air:krea2:lora:civitai:capability@2": 1.0,
        "urn:air:krea2:lora:civitai:realism@3": 0.1,
    }
    assert image_input["negativePrompt"] == "keep-real-phone-photo-texture"


@pytest.mark.asyncio
async def test_reference_free_civitai_template_submits_quickly_then_polls_to_completion(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.url.params.get("wait")))
        if request.url.host == "artifact.test":
            return httpx.Response(200, content=b"polled-template-image")
        if request.method == "POST":
            return httpx.Response(200, json={"id": "workflow-poll-1", "status": "scheduled"})
        if request.method == "GET" and request.url.path.endswith("/workflow-poll-1"):
            return httpx.Response(
                200,
                json={
                    "id": "workflow-poll-1",
                    "status": "succeeded",
                    "steps": [{"output": {"images": [{"url": "https://artifact.test/result.jpg"}]}}],
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    generated = await CivitaiTemplateWorkflowImageGenerator(
        "civitai-test-key",
        template_path=Path("configs/civitai-krea2-celia-realism-template.json"),
        transport=httpx.MockTransport(handler),
        require_reference_free=True,
    ).generate("frozen high-lane prompt", output_path=tmp_path / "out.jpg")

    assert generated.path.read_bytes() == b"polled-template-image"
    receipt = json.loads((tmp_path / "out.jpg.civitai.json").read_text(encoding="utf-8"))
    assert receipt == {
        "provider": "civitai_template",
        "status": "succeeded",
        "template": "civitai-krea2-celia-realism-template.json",
        "workflow_id": "workflow-poll-1",
    }
    assert calls[0] == ("POST", "/v2/consumer/workflows", "8")
    assert calls[1] == ("GET", "/v2/consumer/workflows/workflow-poll-1", None)


def test_civitai_template_generator_rejects_a_template_with_unbounded_prompt_slot(tmp_path: Path) -> None:
    template = tmp_path / "unsafe-template.json"
    template.write_text(
        json.dumps({"steps": [{"$type": "imageGen", "input": {"prompt": "free text"}}]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="render_prompt"):
        CivitaiTemplateWorkflowImageGenerator("civitai-test-key", template_path=template)


def test_civitai_template_generator_rejects_invalid_lora_and_recipe_controls(tmp_path: Path) -> None:
    template = tmp_path / "invalid-recipe.json"
    template.write_text(
        json.dumps(
            {
                "allowMatureContent": True,
                "steps": [
                    {
                        "$type": "imageGen",
                        "input": {
                            "engine": "comfy",
                            "ecosystem": "krea2",
                            "model": "turbo",
                            "operation": "createImage",
                            "images": ["{{identity_reference_data_url}}"],
                            "prompt": "{{render_prompt}}",
                            "negativePrompt": "fixed",
                            "width": 512,
                            "height": 768,
                            "steps": 8,
                            "cfgScale": 1,
                            "sampler": "euler",
                            "scheduler": "simple",
                            "quantity": 1,
                            "loras": {"not-an-air": float("nan")},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid Krea2 LoRA stack"):
        CivitaiTemplateWorkflowImageGenerator("civitai-test-key", template_path=template)


def test_repo_realism_template_is_a_valid_reviewed_krea2_recipe() -> None:
    template = Path("configs/civitai-krea2-celia-realism-template.json")

    generator = CivitaiTemplateWorkflowImageGenerator("civitai-test-key", template_path=template)

    input_payload = generator.template["steps"][0]["input"]  # type: ignore[index]
    assert "images" not in input_payload
    assert input_payload["loras"] == {
        "urn:air:krea2:lora:civitai:2750659@3094831": 1.0,
        "urn:air:krea2:lora:civitai:2787068@3140284": 1.0,
        "urn:air:krea2:lora:civitai:2781697@3132956": 0.1,
    }
    negative_prompt = str(input_payload["negativePrompt"])
    assert "visible genitals" not in negative_prompt
    assert "explicit sexual activity" not in negative_prompt
    assert "transparent clothing" not in negative_prompt


def test_reference_free_civitai_template_rejects_any_image_input(tmp_path: Path) -> None:
    template = tmp_path / "reference-input-template.json"
    template.write_text(
        json.dumps(
            {
                "allowMatureContent": True,
                "steps": [
                    {
                        "$type": "imageGen",
                        "input": {
                            "engine": "comfy",
                            "ecosystem": "krea2",
                            "model": "turbo",
                            "operation": "createImage",
                            "images": ["reviewed-face-only-anchor.jpg"],
                            "prompt": "{{render_prompt}}",
                            "negativePrompt": "fixed",
                            "width": 512,
                            "height": 768,
                            "steps": 8,
                            "cfgScale": 1,
                            "sampler": "euler",
                            "scheduler": "simple",
                            "seed": "{{seed}}",
                            "quantity": 1,
                            "loras": {"urn:air:krea2:lora:civitai:identity@1": 1.0},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="reference-free"):
        CivitaiTemplateWorkflowImageGenerator(
            "civitai-test-key",
            template_path=template,
            require_reference_free=True,
        )


@pytest.mark.asyncio
async def test_civitai_template_generator_keeps_a_reviewed_static_face_input(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert not request.url.path.endswith("/blobs/upload")
        if request.url.host == "artifact.test":
            return httpx.Response(200, content=b"static-template-image")
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "workflow-static-template",
                "status": "succeeded",
                "steps": [{"output": {"images": [{"url": "https://artifact.test/result.png"}]}}],
            },
        )

    template = tmp_path / "static-realism-template.json"
    template.write_text(
        json.dumps(
            {
                "allowMatureContent": True,
                "steps": [
                    {
                        "$type": "imageGen",
                        "input": {
                            "engine": "comfy",
                            "ecosystem": "krea2",
                            "model": "turbo",
                            "operation": "createImage",
                            "images": ["reviewed-face-only-anchor.jpg"],
                            "prompt": "{{render_prompt}}",
                            "negativePrompt": "fixed",
                            "width": 512,
                            "height": 768,
                            "steps": 8,
                            "cfgScale": 1,
                            "sampler": "euler",
                            "scheduler": "simple",
                            "seed": "{{seed}}",
                            "quantity": 1,
                            "loras": {"urn:air:krea2:lora:civitai:identity@1": 1.0},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = await CivitaiTemplateWorkflowImageGenerator(
        "civitai-test-key",
        template_path=template,
        transport=httpx.MockTransport(handler),
    ).generate(
        "frozen high lane prompt",
        output_path=tmp_path / "static-out.png",
        reference_images=(tmp_path / "unused-life-photo.png",),
    )

    assert result.path.read_bytes() == b"static-template-image"
    image_input = observed["payload"]["steps"][0]["input"]  # type: ignore[index]
    assert image_input["images"] == ["reviewed-face-only-anchor.jpg"]


@pytest.mark.asyncio
async def test_civitai_generator_classifies_insufficient_buzz_as_non_retryable_quota(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text='"insufficientBuzz"')

    generator = CivitaiWorkflowImageGenerator(
        "civitai-test-key",
        model="urn:air:sdxl:checkpoint:civitai:312530@2840768",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ImageGenerationProviderError) as raised:
        await generator.generate("test", output_path=tmp_path / "out.png")

    assert raised.value.kind == "quota"
    assert raised.value.detail == "insufficient_buzz"
    assert not raised.value.retryable


@pytest.mark.asyncio
async def test_openai_generator_classifies_rejected_request_without_exposing_raw_body(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": "invalid_image_edit", "message": "bad image edit"}},
        )

    generator = OpenAIImageGenerator("test-key", transport=httpx.MockTransport(handler))

    with pytest.raises(ImageGenerationProviderError) as raised:
        await generator.generate("same fictional character", output_path=tmp_path / "out.png")

    error = raised.value
    assert error.provider == "openai_image"
    assert error.kind == "invalid_request"
    assert error.status_code == 400
    assert not error.retryable


@pytest.mark.asyncio
async def test_ark_generator_sends_local_reference_images_as_base64(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["headers"] = dict(request.headers)
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": "c2VlZHJlYW0="}]})

    reference = tmp_path / "canonical.png"
    reference.write_bytes(b"reference-bytes")
    generator = VolcArkImageGenerator("ark-test-key", transport=httpx.MockTransport(handler))

    generated = await generator.generate(
        "same fictional character, real phone photo",
        output_path=tmp_path / "out.png",
        reference_images=(reference,),
    )

    assert generated.path.read_bytes() == b"seedream"
    assert str(observed["url"]).endswith("/images/generations")
    assert observed["headers"]["authorization"] == "Bearer ark-test-key"  # type: ignore[index]
    payload = observed["payload"]
    assert payload["response_format"] == "b64_json"  # type: ignore[index]
    assert payload["image"] == ["data:image/png;base64,cmVmZXJlbmNlLWJ5dGVz"]  # type: ignore[index]


@pytest.mark.asyncio
async def test_seedream_5_pro_omits_4_only_sequence_parameter(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": "c2VlZHJlYW0="}]})

    generated = await VolcArkImageGenerator(
        "ark-test-key",
        model="doubao-seedream-5-0-pro-260628",
        transport=httpx.MockTransport(handler),
    ).generate("same fictional character", output_path=tmp_path / "out.png")

    assert generated.path.read_bytes() == b"seedream"
    payload = observed["payload"]
    assert "sequential_image_generation" not in payload  # type: ignore[operator]


@pytest.mark.asyncio
async def test_ark_generator_explains_a_model_not_open_response(tmp_path: Path) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "ModelNotOpen", "message": "not activated"}},
        )

    with pytest.raises(ValueError, match="not enabled"):
        await VolcArkImageGenerator(
            "ark-test-key", transport=httpx.MockTransport(handler)
        ).generate("test", output_path=tmp_path / "out.png")


@pytest.mark.asyncio
async def test_quality_gate_retries_once_when_first_render_is_rejected(tmp_path: Path) -> None:
    class Generator:
        calls = 0
        prompts: list[str] = []

        async def generate(self, _prompt: str, *, output_path: Path, size: str = "1024x1024"):
            self.calls += 1
            self.prompts.append(_prompt)
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
    assert "Correct the prior rejected render" in generator.prompts[1]
    assert "first hand malformed" in generator.prompts[1]


@pytest.mark.asyncio
async def test_quality_gate_treats_frozen_shot_constraints_as_delivery_requirements(tmp_path: Path) -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"passed": true, "reason": "ok"}'}}]},
        )

    from companion_daemon.image_generation import OpenAIImageQualityGate

    image = tmp_path / "generated.png"
    image.write_bytes(b"png")
    gate = OpenAIImageQualityGate("test-key", transport=httpx.MockTransport(handler))

    result = await gate.assess(
        image,
        prompt=(
            "Frozen world media shot plan (must follow): no selfie arm.\n"
            "Motion requirement: transitional. Visible motion evidence: one foot is mid-step.\n"
            "Anti-static delivery constraints: do not make this a front-facing posed stance."
        ),
    )

    assert result.passed is True
    content = observed["payload"]["messages"][0]["content"][0]["text"]  # type: ignore[index]
    assert "non-negotiable camera" in content
    assert "front-facing posed stance" in content


@pytest.mark.asyncio
async def test_quality_gate_rejection_retries_with_anti_static_correction(tmp_path: Path) -> None:
    class Generator:
        prompts: list[str] = []

        async def generate(self, prompt: str, *, output_path: Path, size: str = "1024x1024"):
            self.prompts.append(prompt)
            output_path.write_bytes(b"render")
            from companion_daemon.image_generation import GeneratedImage

            return GeneratedImage(output_path, prompt)

    class Gate:
        calls = 0

        async def assess(self, _path: Path, *, prompt: str) -> ImageQualityResult:
            self.calls += 1
            assert "Motion requirement: transitional" in prompt
            return ImageQualityResult(
                passed=self.calls == 2,
                reason="front-facing posed stance with both arms hanging still",
            )

    generator = Generator()
    generated = await render_character_image(
        generator,
        "Frozen world media shot plan (must follow):\nMotion requirement: transitional.",
        output_path=tmp_path / "out.png",
        quality_gate=Gate(),
    )

    assert generated.attempts == 2
    assert len(generator.prompts) == 2
    assert "front-facing posed stance" in generator.prompts[1]
    assert "motion evidence" in generator.prompts[1]


def test_visual_reference_selection_keeps_anchor_and_limits_reference_cost() -> None:
    references = visual_reference_paths(
        Path("configs/visual_identity.yaml"),
        profile="everyday_selfie",
        scene_hint="傍晚在图书馆窗边的自拍",
    )

    assert len(references) == 2
    assert references[0].name == "08-cafe-phone-canonical.png"
    assert all(path.is_file() for path in references)
