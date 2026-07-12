import base64
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

import httpx

from companion_daemon.image_requests import detect_style_tags
from companion_daemon.visual_identity import load_visual_identity


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    prompt: str


@dataclass(frozen=True)
class ImageQualityResult:
    passed: bool
    reason: str


class ImageGenerator(Protocol):
    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
        reference_images: Iterable[Path] = (),
    ) -> GeneratedImage: ...


class ImageQualityGate(Protocol):
    async def assess(self, image_path: Path, *, prompt: str) -> ImageQualityResult: ...


class OpenAIImageGenerator:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-image-2",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
        reference_images: Iterable[Path] = (),
    ) -> GeneratedImage:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        references = tuple(reference_images)
        async with httpx.AsyncClient(timeout=180, trust_env=False, transport=self.transport) as client:
            if references:
                files = [
                    (
                        "image[]",
                        (
                            path.name,
                            path.read_bytes(),
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        ),
                    )
                    for path in references
                ]
                response = await client.post(
                    f"{self.base_url}/images/edits",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    data={"model": self.model, "prompt": prompt, "size": size, "output_format": "png"},
                    files=files,
                )
            else:
                response = await client.post(
                    f"{self.base_url}/images/generations",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "size": size,
                        "output_format": "png",
                    },
                )
            response.raise_for_status()
        data = response.json()["data"][0]
        image_b64 = data.get("b64_json") or data.get("image_base64")
        if not image_b64:
            raise ValueError("Image API response did not include base64 image data")
        output_path.write_bytes(base64.b64decode(image_b64))
        return GeneratedImage(path=output_path, prompt=prompt)


class ComfyUIImageGenerator:
    """Submit a user-provided ComfyUI API workflow with small, explicit substitutions.

    The workflow stays outside Python so a local LoRA, IP-Adapter, or FaceID graph
    remains editable in ComfyUI.  Use $PROMPT, $NEGATIVE_PROMPT, $LORA_PATH, and
    $REFERENCE_IMAGE_N placeholders in its string values.
    """

    def __init__(
        self,
        *,
        base_url: str,
        workflow_path: Path,
        lora_path: str | None = None,
        negative_prompt: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.workflow_path = workflow_path
        self.lora_path = lora_path or ""
        self.negative_prompt = negative_prompt
        self.transport = transport

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
        reference_images: Iterable[Path] = (),
    ) -> GeneratedImage:
        if not self.workflow_path.is_file():
            raise FileNotFoundError(f"ComfyUI workflow was not found: {self.workflow_path}")
        try:
            workflow = json.loads(self.workflow_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"ComfyUI workflow is not valid JSON: {self.workflow_path}") from exc
        references = tuple(reference_images)
        substitutions = {
            "$PROMPT": prompt,
            "$NEGATIVE_PROMPT": self.negative_prompt,
            "$LORA_PATH": self.lora_path,
            "$SIZE": size,
            **{f"$REFERENCE_IMAGE_{index}": str(path) for index, path in enumerate(references, start=1)},
        }
        workflow = _replace_workflow_tokens(workflow, substitutions)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=30, trust_env=False, transport=self.transport) as client:
            queued = await client.post(f"{self.base_url}/prompt", json={"prompt": workflow})
            queued.raise_for_status()
            prompt_id = str(queued.json().get("prompt_id") or "")
            if not prompt_id:
                raise ValueError("ComfyUI did not return a prompt_id")
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                history = await client.get(f"{self.base_url}/history/{prompt_id}")
                history.raise_for_status()
                image = _comfy_output_image(history.json(), prompt_id)
                if image:
                    response = await client.get(
                        f"{self.base_url}/view",
                        params={"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")},
                    )
                    response.raise_for_status()
                    output_path.write_bytes(response.content)
                    return GeneratedImage(path=output_path, prompt=prompt)
                await _sleep_briefly()
        raise TimeoutError(f"ComfyUI generation did not finish within 240 seconds: {prompt_id}")


class FallbackImageGenerator:
    """Prefer one backend but make a failed local render non-fatal when allowed."""

    def __init__(self, primary: ImageGenerator, fallback: ImageGenerator | None = None):
        self.primary = primary
        self.fallback = fallback

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
        reference_images: Iterable[Path] = (),
    ) -> GeneratedImage:
        references = tuple(reference_images)
        try:
            return await self.primary.generate(
                prompt, output_path=output_path, size=size, reference_images=references
            )
        except Exception:
            if self.fallback is None:
                raise
            return await self.fallback.generate(
                prompt, output_path=output_path, size=size, reference_images=references
            )


class OpenAIImageQualityGate:
    """A compact visual check for face/hand failures before an image is delivered."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport

    async def assess(self, image_path: Path, *, prompt: str) -> ImageQualityResult:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        request = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Review this fictional-character image for delivery. Return JSON only: "
                            '{"passed": boolean, "reason": string}. Pass only if the face is coherent, '
                            "hands are not visibly malformed, it contains no text/watermark, and it fits the request. "
                            f"Request: {prompt[:600]}"
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
                ],
            }],
        }
        async with httpx.AsyncClient(timeout=45, trust_env=False, transport=self.transport) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=request,
            )
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        decision = json.loads(content)
        return ImageQualityResult(bool(decision.get("passed")), str(decision.get("reason") or "unspecified"))


async def render_character_image(
    generator: Any,
    prompt: str,
    *,
    output_path: Path,
    reference_images: Iterable[Path] = (),
    size: str = "1024x1024",
    quality_gate: ImageQualityGate | None = None,
) -> GeneratedImage:
    """Call new renderers with references while keeping existing test doubles valid."""
    references = tuple(reference_images)
    for attempt in range(2 if quality_gate else 1):
        try:
            generated = await generator.generate(
                prompt,
                output_path=output_path,
                size=size,
                reference_images=references,
            )
        except TypeError as exc:
            if "reference_images" not in str(exc):
                raise
            generated = await generator.generate(prompt, output_path=output_path, size=size)
        if quality_gate is None:
            return generated
        try:
            assessment = await quality_gate.assess(generated.path, prompt=prompt)
        except Exception:
            # A delivery quality check is an enhancement, not a reason to hide
            # a successful image when the evaluator is temporarily unavailable.
            return generated
        if assessment.passed or attempt == 1:
            return generated
    raise AssertionError("unreachable")


def visual_reference_paths(
    visual_identity_path: Path | None,
    *,
    profile: str = "everyday_selfie",
    relationship_tier: str | None = None,
) -> tuple[Path, ...]:
    if visual_identity_path is None or not visual_identity_path.is_file():
        return ()
    identity = load_visual_identity(str(visual_identity_path))
    assets = (
        identity.relationship_reference_assets(relationship_tier)
        if relationship_tier
        else identity.reference_assets(profile)
    )
    return tuple(path for asset in assets if (path := Path(asset)).is_file())


def life_image_prompt(
    topic: str,
    *,
    kind: str = "life",
    profile: str = "everyday_selfie",
    relationship_tier: str | None = None,
    visual_identity_path: Path | None = Path("configs/visual_identity.yaml"),
) -> str:
    style_tags = detect_style_tags(topic)
    if kind == "selfie":
        identity_block = ""
        if visual_identity_path and visual_identity_path.exists():
            identity_block = "\n" + load_visual_identity(str(visual_identity_path)).prompt_block(
                relationship_tier=relationship_tier if profile == "relationship_private" else None
            )
        privacy_line = (
            " It is a private, tender moment between established fictional partners; "
            "still fully clothed, non-explicit, and never pornographic."
            if profile == "relationship_private" and not relationship_tier
            else ""
        )
        return (
            "Create an original virtual-life selfie-style image of沈知栀 / Celia Shen, "
            "a gentle Chinese college student with shoulder-length dark hair and a subtle teal hairpin. "
            "It should feel like a tasteful fictional character selfie, not a real person's photo. "
            f"Moment/topic: {topic}. Style: {style_tags}. No text, no watermark.{privacy_line}"
            f"{identity_block}"
        )
    if kind == "food":
        return (
            "Create a cozy phone-photo style image of a small meal or snack a Chinese college student might share. "
            f"Moment/topic: {topic}. Style: {style_tags}. Natural lighting, realistic but clearly AI-generated, no text, no watermark."
        )
    return (
        "Create a cozy phone-photo style fictional life snapshot from a Chinese college student's day. "
        f"Moment/topic: {topic}. Style: {style_tags}. No text, no watermark."
    )


def _replace_workflow_tokens(value: object, substitutions: dict[str, str]) -> object:
    if isinstance(value, dict):
        return {key: _replace_workflow_tokens(item, substitutions) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_workflow_tokens(item, substitutions) for item in value]
    if isinstance(value, str):
        for token, replacement in substitutions.items():
            value = value.replace(token, replacement)
    return value


def _comfy_output_image(history: object, prompt_id: str) -> dict[str, str] | None:
    record = history.get(prompt_id) if isinstance(history, dict) else None
    outputs = record.get("outputs") if isinstance(record, dict) else None
    if not isinstance(outputs, dict):
        return None
    for node in outputs.values():
        images = node.get("images") if isinstance(node, dict) else None
        if isinstance(images, list) and images and isinstance(images[0], dict):
            first = images[0]
            if isinstance(first.get("filename"), str):
                return {key: str(value) for key, value in first.items() if key in {"filename", "subfolder", "type"}}
    return None


async def _sleep_briefly() -> None:
    import asyncio
    await asyncio.sleep(0.75)
