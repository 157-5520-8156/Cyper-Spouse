#!/usr/bin/env python
"""One real vision round-trip through the production perception transport.

Reads provider credentials from ``Settings`` (``.env``), archives one local
image into a scratch attachment archive, and drives the exact
``describe → analyze → lookup → read_exact`` path an accepted perception
Action would take.  Nothing touches the production database or ledger; the
transport writes to a scratch SQLite file under ``output/``.

    .venv/bin/python scripts/run_world_v2_perception_vision_probe.py \
        --image assets/reference/08-cafe-phone-canonical.png

Cost: one vision-model chat completion (negligible).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from companion_daemon.config import get_settings  # noqa: E402
from companion_daemon.world_v2.perception_vision_transport import (  # noqa: E402
    SQLiteDurableVisionPerceptionTransport,
)
from companion_daemon.world_v2.qq_attachment_archive import QQAttachmentArchive  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", default="assets/reference/08-cafe-phone-canonical.png"
    )
    parser.add_argument("--model", default=None, help="override Settings.vision_model")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.openai_api_key:
        print("OPENAI_API_KEY is required", file=sys.stderr)
        return 2
    image_path = Path(args.image)
    data = image_path.read_bytes()
    ref = "qq-attachment:image:sha256:" + hashlib.sha256(data).hexdigest()

    with tempfile.TemporaryDirectory(prefix="perception-probe-") as scratch:
        archive = QQAttachmentArchive(Path(scratch) / "attachments")
        archive.store(ref, data)
        descriptor = archive.describe(attachment_ref=ref, analysis_kind="vision")
        model = args.model or settings.vision_model
        transport = SQLiteDurableVisionPerceptionTransport(
            Path(scratch) / "perception-probe.sqlite",
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=model,
            proxy_url=settings.openai_proxy_url,
        )
        try:
            class _Action:
                payload_ref = ref
                payload_hash = descriptor.content_hash

            input_ref, input_hash, body = await archive.resolve(_Action())
            key = "perception:probe:" + hashlib.sha256(
                (model + descriptor.content_hash).encode()
            ).hexdigest()
            started = time.monotonic()
            result_ref, result_hash, provider_ref, cost, received_at = (
                await transport.analyze(
                    analysis_kind="vision",
                    input_ref=input_ref,
                    input_hash=input_hash,
                    body=body,
                    idempotency_key=key,
                )
            )
            latency_ms = round((time.monotonic() - started) * 1000)
            recovered = await transport.lookup(idempotency_key=key)
            content = transport.read_exact(result_ref=result_ref)
            assert recovered is not None and content is not None
            assert content.result_hash == result_hash
            print(
                json.dumps(
                    {
                        "image": str(image_path),
                        "image_bytes": len(data),
                        "model": model,
                        "attachment_ref": ref,
                        "input_hash": input_hash,
                        "result_ref": result_ref,
                        "result_hash": result_hash,
                        "provider_ref": provider_ref,
                        "cost": cost,
                        "received_at": received_at.isoformat(),
                        "latency_ms": latency_ms,
                        "lookup_recovers_same_result": recovered == (
                            result_ref, result_hash, provider_ref, cost, received_at
                        ),
                        "text": content.text,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            transport.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
