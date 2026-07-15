from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.media_execution_runtime import (
    MediaExecutionError,
    MediaExecutionWorker,
)
from companion_daemon.world_v2.media_provider_results import (
    MediaProviderArtifactResult,
    media_provider_result_hash,
)
from companion_daemon.world_v2.media_v2 import media_payload_hash
from companion_daemon.world_v2.schemas import Action, ExecutionReceipt


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
FINGERPRINT = "sha256:" + "a" * 64


def _action() -> Action:
    return Action.model_construct(
        schema_version="world-v2.1",
        action_id="action:media-render:provider-result",
        world_id="world:media-provider-result",
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:media-provider-result",
        causation_id="cause:media-provider-result",
        correlation_id="correlation:media-provider-result",
        kind="media_render",
        layer="media_action",
        intent_ref="plan:media-provider-result",
        actor="agent:companion",
        target="provider:media-renderer",
        payload_ref="sidecar:plan:media-provider-result",
        payload_hash="sha256:" + "b" * 64,
        provider_media_grant=None,
        idempotency_key="media-render:provider-result",
        budget_reservation_id="reservation:media-provider-result",
        state="delivered",
        recovery_policy="effect_once",
    )


def _result() -> MediaProviderArtifactResult:
    body = '{"image":"opaque-provider-bytes"}'
    return MediaProviderArtifactResult(
        action_id="action:media-render:provider-result",
        idempotency_key="media-render:provider-result",
        request_fingerprint=FINGERPRINT,
        artifact_payload_ref="sidecar:artifact:provider-result",
        artifact_payload_hash=media_payload_hash(body),
        artifact_content_type="application/vnd.world-v2.media-artifact+json",
        artifact_body=body,
    )


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def record_rendered_artifact(self, **kwargs):
        self.calls.append(("artifact", kwargs))

    def record_render_failure(self, **kwargs):
        self.calls.append(("failed", kwargs))


class _Ledger:
    def __init__(self, projection) -> None:
        self._projection = projection

    def project(self):
        return self._projection


class _Transport:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str]] = []

    async def lookup_execution_result(self, *, action_id, idempotency_key, request_fingerprint):
        self.calls.append((action_id, idempotency_key, request_fingerprint))
        return self.result


@pytest.mark.asyncio
async def test_worker_only_materializes_provider_bytes_bound_to_terminal_receipt() -> None:
    action = _action()
    result = _result()
    receipt = ExecutionReceipt(
        receipt_id="receipt:media-provider-result",
        result_id="result:media-provider-result",
        action_id=action.action_id,
        provider="provider:media",
        provider_ref="provider-ref:media-provider-result",
        source_event_id="source:media-provider-result",
        receipt_kind="terminal",
        observed_state="delivered",
        is_terminal=True,
        artifact_refs=("request:" + FINGERPRINT,),
        cost_actual=0,
        received_at=NOW,
        raw_payload_hash=media_provider_result_hash(result),
    )
    projection = SimpleNamespace(
        actions=(action,),
        media_artifacts=(),
        media_inspections=(),
        execution_receipts=(receipt,),
    )
    runtime = _Runtime()
    transport = _Transport(result)
    worker = MediaExecutionWorker(runtime=runtime, ledger=_Ledger(projection), transport=transport)

    assert await worker.drain_once(logical_time=NOW) == "artifact_recorded"
    assert transport.calls == [(action.action_id, action.idempotency_key, FINGERPRINT)]
    assert runtime.calls[0][0] == "artifact"
    assert runtime.calls[0][1]["artifact_payload"].body == result.artifact_body


@pytest.mark.asyncio
async def test_worker_rejects_result_not_bound_to_terminal_receipt_hash() -> None:
    action = _action()
    result = _result()
    receipt = ExecutionReceipt(
        receipt_id="receipt:media-provider-result",
        result_id="result:media-provider-result",
        action_id=action.action_id,
        provider="provider:media",
        provider_ref="provider-ref:media-provider-result",
        source_event_id="source:media-provider-result",
        receipt_kind="terminal",
        observed_state="delivered",
        is_terminal=True,
        artifact_refs=("request:" + FINGERPRINT,),
        cost_actual=0,
        received_at=NOW,
        raw_payload_hash="sha256:" + "f" * 64,
    )
    projection = SimpleNamespace(
        actions=(action,), media_artifacts=(), media_inspections=(), execution_receipts=(receipt,)
    )
    worker = MediaExecutionWorker(runtime=_Runtime(), ledger=_Ledger(projection), transport=_Transport(result))

    with pytest.raises(MediaExecutionError, match="terminal receipt hash"):
        await worker.drain_once(logical_time=NOW)


@pytest.mark.asyncio
async def test_worker_closes_a_terminal_repair_failure_without_a_second_provider_call() -> None:
    action = _action().model_copy(update={"kind": "media_repair", "state": "failed"})
    projection = SimpleNamespace(
        actions=(action,), media_artifacts=(), media_inspections=(), execution_receipts=()
    )
    runtime = _Runtime()
    transport = _Transport(_result())
    worker = MediaExecutionWorker(runtime=runtime, ledger=_Ledger(projection), transport=transport)

    assert await worker.drain_once(logical_time=NOW) == "render_failed"
    assert transport.calls == []
    assert runtime.calls == [
        ("failed", {"action_id": action.action_id, "reason_code": "provider_failed", "logical_time": NOW})
    ]
