"""Durable transport contract for the production image-machine adapter."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon import event_media
from companion_daemon.world_v2.media_provider_results import (
    MediaProviderArtifactResult,
    MediaProviderInspectionResult,
    media_provider_result_hash,
)
from companion_daemon.world_v2.media_provider_transport import (
    SQLiteDurableMediaProviderTransport,
)
from companion_daemon.world_v2.platform_action_executor import (
    MediaProviderDispatchRequest,
)


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
_PLAN_BODY = json.dumps({"plan_id": "plan:transport", "version": "event-media-plan-v5"})


def _request(
    *, kind: str, action_id: str, idempotency_key: str, body: str, content_type: str
) -> MediaProviderDispatchRequest:
    return MediaProviderDispatchRequest(
        action_id=action_id,
        kind=kind,  # type: ignore[arg-type]
        provider_ref="provider:media-renderer",
        payload_ref=f"sidecar:{idempotency_key}",
        payload_hash="sha256:" + hashlib.sha256(body.encode()).hexdigest(),
        content_type=content_type,
        body=body,
        idempotency_key=idempotency_key,
        grant_id="grant:world-v2:media-render",
        grant_revision=1,
    )


class _Renderer:
    def __init__(self, *, image: Path, fail: bool = False) -> None:
        self.image = image
        self.fail = fail
        self.calls = 0

    async def render(self, plan):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail:
            return event_media.MediaRenderFailure(
                plan_id="plan:transport", reason="image_provider_quota", attempts=1
            )
        return event_media.RenderedMedia(
            plan_id="plan:transport",
            path=self.image,
            artifact_hash=hashlib.sha256(self.image.read_bytes()).hexdigest(),
            prompt="frozen prompt",
            attempts=1,
            inspection=event_media.MediaInspection(
                passed=True,
                reason="accepted",
                observed_summary="一张桌上的咖啡照片",
                observed_facts=("coffee",),
                deviations=(),
                inspector_model="test-inspector",
            ),
        )


def _parse_plan_stub(payload):  # type: ignore[no-untyped-def]
    assert payload == json.loads(_PLAN_BODY)
    return object()


@pytest.fixture()
def image(tmp_path: Path) -> Path:
    path = tmp_path / "render.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")
    return path


@pytest.mark.asyncio
async def test_render_then_inspection_replay_survive_restart(
    tmp_path: Path, image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        event_media.MediaPlan, "from_payload", staticmethod(_parse_plan_stub)
    )
    db = tmp_path / "transport.sqlite"
    renderer = _Renderer(image=image)
    transport = SQLiteDurableMediaProviderTransport(
        path=str(db), world_id="world:transport", renderer=renderer, now=lambda: NOW
    )
    render_request = _request(
        kind="media_render",
        action_id="action:media-render:1",
        idempotency_key="media-render:plan:transport",
        body=_PLAN_BODY,
        content_type="application/vnd.world-v2.media-plan+json",
    )
    receipt = await transport.send(render_request)
    assert receipt.status == "delivered"
    assert renderer.calls == 1
    # Effect-once: the same key never re-renders and returns identical bytes.
    again = await transport.send(render_request)
    assert again == receipt
    assert renderer.calls == 1
    looked_up = await transport.lookup(
        idempotency_key=render_request.idempotency_key,
        request_fingerprint=render_request.fingerprint,
    )
    assert looked_up == receipt

    result = await transport.lookup_execution_result(
        action_id=render_request.action_id,
        idempotency_key=render_request.idempotency_key,
        request_fingerprint=render_request.fingerprint,
    )
    assert isinstance(result, MediaProviderArtifactResult)
    assert media_provider_result_hash(result) == receipt.raw_payload_hash
    artifact_body = json.loads(result.artifact_body)
    assert artifact_body["encoding"] == "base64"

    transport.close()
    # A fresh process must recover both the receipt and the paired inspection
    # without another renderer call.
    restarted = SQLiteDurableMediaProviderTransport(
        path=str(db),
        world_id="world:transport",
        renderer=_Renderer(image=image, fail=True),
        now=lambda: NOW,
    )
    recovered = await restarted.lookup(
        idempotency_key=render_request.idempotency_key,
        request_fingerprint=render_request.fingerprint,
    )
    assert recovered == receipt

    inspection_request = _request(
        kind="media_inspection",
        action_id="action:media-inspection:1",
        idempotency_key="media-inspection:artifact:1",
        body=result.artifact_body,
        content_type="application/vnd.world-v2.media-artifact+json",
    ).model_copy(update={"payload_ref": result.artifact_payload_ref})
    inspection_receipt = await restarted.send(inspection_request)
    assert inspection_receipt.status == "delivered"
    inspection = await restarted.lookup_execution_result(
        action_id=inspection_request.action_id,
        idempotency_key=inspection_request.idempotency_key,
        request_fingerprint=inspection_request.fingerprint,
    )
    assert isinstance(inspection, MediaProviderInspectionResult)
    assert inspection.passed is True
    assert inspection.observed_summary == "一张桌上的咖啡照片"
    assert inspection.repairable is False
    assert media_provider_result_hash(inspection) == inspection_receipt.raw_payload_hash
    restarted.close()


@pytest.mark.asyncio
async def test_render_failure_is_a_persisted_terminal_receipt(
    tmp_path: Path, image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        event_media.MediaPlan, "from_payload", staticmethod(_parse_plan_stub)
    )
    renderer = _Renderer(image=image, fail=True)
    transport = SQLiteDurableMediaProviderTransport(
        path=str(tmp_path / "transport-failed.sqlite"),
        world_id="world:transport",
        renderer=renderer,
        now=lambda: NOW,
    )
    request = _request(
        kind="media_render",
        action_id="action:media-render:failed",
        idempotency_key="media-render:plan:failed",
        body=_PLAN_BODY,
        content_type="application/vnd.world-v2.media-plan+json",
    )
    receipt = await transport.send(request)
    assert receipt.status == "failed"
    assert receipt.error_class == "image_provider_quota"
    assert await transport.send(request) == receipt
    assert renderer.calls == 1
    assert (
        await transport.lookup_execution_result(
            action_id=request.action_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        is None
    )
    transport.close()


@pytest.mark.asyncio
async def test_inspection_without_paired_render_record_fails_closed(
    tmp_path: Path, image: Path
) -> None:
    transport = SQLiteDurableMediaProviderTransport(
        path=str(tmp_path / "transport-orphan.sqlite"),
        world_id="world:transport",
        renderer=_Renderer(image=image),
        now=lambda: NOW,
    )
    body = json.dumps({"encoding": "base64", "artifact_hash": "x", "bytes": "aGk="})
    request = _request(
        kind="media_inspection",
        action_id="action:media-inspection:orphan",
        idempotency_key="media-inspection:orphan",
        body=body,
        content_type="application/vnd.world-v2.media-artifact+json",
    )
    receipt = await transport.send(request)
    assert receipt.status == "failed"
    assert receipt.error_class == "inspection_record_unavailable"
    transport.close()


@pytest.mark.asyncio
async def test_idempotency_key_cannot_be_rebound_to_different_bytes(
    tmp_path: Path, image: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        event_media.MediaPlan, "from_payload", staticmethod(_parse_plan_stub)
    )
    transport = SQLiteDurableMediaProviderTransport(
        path=str(tmp_path / "transport-conflict.sqlite"),
        world_id="world:transport",
        renderer=_Renderer(image=image),
        now=lambda: NOW,
    )
    request = _request(
        kind="media_render",
        action_id="action:media-render:conflict",
        idempotency_key="media-render:plan:conflict",
        body=_PLAN_BODY,
        content_type="application/vnd.world-v2.media-plan+json",
    )
    await transport.send(request)
    with pytest.raises(ValueError, match="different request"):
        await transport.lookup(
            idempotency_key=request.idempotency_key,
            request_fingerprint="sha256:" + "f" * 64,
        )
    transport.close()
