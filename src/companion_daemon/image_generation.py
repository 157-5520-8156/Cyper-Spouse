import base64
from dataclasses import dataclass
from pathlib import Path

import httpx

from companion_daemon.visual_identity import load_visual_identity


@dataclass(frozen=True)
class GeneratedImage:
    path: Path
    prompt: str


class OpenAIImageGenerator:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-image-2",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def generate(
        self,
        prompt: str,
        *,
        output_path: Path,
        size: str = "1024x1024",
    ) -> GeneratedImage:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(timeout=180) as client:
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


def life_image_prompt(
    topic: str,
    *,
    kind: str = "life",
    visual_identity_path: Path | None = Path("configs/visual_identity.yaml"),
) -> str:
    if kind == "selfie":
        identity_block = ""
        if visual_identity_path and visual_identity_path.exists():
            identity_block = "\n" + load_visual_identity(str(visual_identity_path)).prompt_block()
        return (
            "Create an original virtual-life selfie-style image of沈知栀 / Celia Shen, "
            "a gentle Chinese college student with shoulder-length dark hair and a subtle teal hairpin. "
            "It should feel like a tasteful fictional character selfie, not a real person's photo. "
            f"Moment/topic: {topic}. No text, no watermark."
            f"{identity_block}"
        )
    if kind == "food":
        return (
            "Create a cozy phone-photo style image of a small meal or snack a Chinese college student might share. "
            f"Moment/topic: {topic}. Natural lighting, realistic but clearly AI-generated, no text, no watermark."
        )
    return (
        "Create a cozy phone-photo style fictional life snapshot from a Chinese college student's day. "
        f"Moment/topic: {topic}. No text, no watermark."
    )
