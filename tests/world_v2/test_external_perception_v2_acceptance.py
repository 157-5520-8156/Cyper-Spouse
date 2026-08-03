from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.external_perception_acceptance import (
    ExternalPerceptionAcceptanceError,
    ExternalPerceptionAcceptanceRuntime,
    ExternalPerceptionDeliveryProducer,
)
from companion_daemon.world_v2.external_perception_events import (
    ExternalPerceptionChannelProof,
    ExternalPerceptionLiveDelivery,
    ExternalPerceptionSelection,
    FrozenExternalSignalSnapshot,
    compile_external_perception_life_influences,
)
from companion_daemon.world_v2.proposal_audit_schemas import (
    ModelResultRecordedPayload,
    RecordedModelResultAudit,
    RecordedModelRoute,
    canonical_json,
    model_audit_json,
    sha256,
)
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
WORLD_ID = "world-external-perception-v2"


def _model_result(*, cursor: ProjectionCursor) -> ModelResultRecordedPayload:
    call_id = "model-call:external-perception:1"
    response_hash = "b" * 64
    result_ref = "model-result:" + sha256(
        canonical_json({"model_call_id": call_id, "response_hash": response_hash})
    )
    audit = RecordedModelResultAudit(
        model_call_id=call_id,
        model_result_ref=result_ref,
        attempt_id="attention-attempt:1",
        route=RecordedModelRoute(
            tier="flash",
            reason_code="external_perception_attention",
            router_version="external-perception-attention-router.1",
        ),
        model_id="fixture-character-model",
        model_version="1",
        request_hash="a" * 64,
        response_hash=response_hash,
        status="proposal_validated",
    )
    audit_json = model_audit_json(audit)
    proposal_hash = "sha256:" + "9" * 64
    deliberation_result_id = "deliberation:" + sha256(
        canonical_json(
            {
                "capsule_id": "c" * 64,
                "proposal_hash": proposal_hash,
                "attempt_audits": [json.loads(audit_json)],
            }
        )
    )
    return ModelResultRecordedPayload(
        audit_contract="model-result-audit.1",
        model_result_ref=result_ref,
        deliberation_result_id=deliberation_result_id,
        proposal_hash=proposal_hash,
        model_call_id=call_id,
        attempt_id="attention-attempt:1",
        capsule_id="c" * 64,
        trigger_ref="attention-attempt:1",
        evaluated_world_revision=cursor.world_revision,
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )


def _snapshot() -> FrozenExternalSignalSnapshot:
    visible = canonical_json(
        {
            "headline": "深圳发布暴雨预警",
            "licensed_summary": "气象部门提示部分区域可能出现强降雨。",
            "publisher": "publisher:weather-authority",
        }
    )
    return FrozenExternalSignalSnapshot(
        snapshot_ref="external-snapshot:signal:rain:revision:3",
        signal_revision_ref="signal:rain:revision:3",
        source_id="source:weather-authority",
        upstream_publisher_ref="publisher:weather-authority",
        upstream_item_id="rain-alert-20260803",
        source_policy_revision="source-policy:weather:2",
        source_payload_hash="d" * 64,
        normalized_hash="e" * 64,
        headline="深圳发布暴雨预警",
        licensed_summary="气象部门提示部分区域可能出现强降雨。",
        canonical_url="https://weather.example.test/alerts/rain-20260803",
        published_at=NOW - timedelta(minutes=10),
        observed_at=NOW - timedelta(minutes=8),
        expires_at=NOW + timedelta(hours=2),
        correction_lineage_refs=("signal:rain:revision:2",),
        model_visible_material_json=visible,
        model_visible_material_hash=hashlib.sha256(visible.encode()).hexdigest(),
        may_expose_to_character_model=True,
        may_quote=False,
        may_freeze_durable_snapshot=True,
    )


def _delivery(*, cursor: ProjectionCursor | None = None) -> ExternalPerceptionLiveDelivery:
    pinned = cursor or ProjectionCursor(
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    snapshot = _snapshot()
    return ExternalPerceptionLiveDelivery(
        world_id=WORLD_ID,
        deployment_mode_revision="live:external-perception.1",
        attention_attempt_id="attention-attempt:1",
        window_id="window:external-perception:1",
        candidate_snapshot_hash="c" * 64,
        pinned_cursor=pinned,
        actor_ref="agent:companion",
        encountered_world_time=NOW,
        observed_wall_time=NOW,
        attention_model_result=_model_result(cursor=pinned),
        selections=(
            ExternalPerceptionSelection(
                perception_id="external-perception:rain:1",
                candidate_ref="candidate:rain:1",
                snapshot=snapshot,
                channel=ExternalPerceptionChannelProof(
                    channel_ref="channel:public-alerts:weather",
                    channel_kind="public_alert",
                    proof_refs=("source-policy:weather:2",),
                    proof_hash="f" * 64,
                    access_summary="当前设备可接收该公开预警源。",
                ),
                subjective_summary="我注意到深圳这会儿可能有一阵很大的雨。",
                epistemic_notes="这是刚发布的预警，但具体影响范围仍要看后续更新。",
                attended_context_refs=("life-arc:current-city",),
                privacy_class="public",
            ),
        ),
    )


def test_live_delivery_commits_exact_audit_snapshot_and_perception_atomically() -> None:
    producer = ExternalPerceptionDeliveryProducer()
    runtime = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID,
        delivery_producer=producer,
    )

    committed = runtime.accept(producer.prepare(_delivery()))
    projection = runtime.ledger.project()

    assert committed.world_revision == 3
    assert committed.deliberation_revision == 1
    assert committed.perceptions[0].perception_id == "external-perception:rain:1"
    assert tuple(item.event_type for item in projection.committed_world_event_refs) == (
        "AcceptanceRecorded",
        "ExternalSignalSnapshotAdopted",
        "ExternalPerceptionRecorded",
    )
    assert len(projection.model_result_audits) == 1
    assert projection.external_signal_snapshots[0].signal_revision_ref == ("signal:rain:revision:3")
    perception = projection.external_perceptions[0]
    assert perception.snapshot_ref == "external-snapshot:signal:rain:revision:3"
    assert perception.subjective_summary.startswith("我注意到")
    assert not projection.trigger_processes
    assert not projection.affect_episodes
    assert not projection.memory_candidates
    assert not projection.plans


def test_live_delivery_rejects_shadow_mode_and_stale_cursor_before_writing() -> None:
    producer = ExternalPerceptionDeliveryProducer()
    runtime = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID,
        delivery_producer=producer,
    )
    delivery = _delivery()
    with pytest.raises(ExternalPerceptionAcceptanceError, match="live_mode_required"):
        producer.prepare(delivery.model_copy(update={"deployment_mode_revision": "shadow:test.1"}))

    handle = producer.prepare(delivery)
    runtime.ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:unrelated-deliberation",
                world_id=WORLD_ID,
                event_type="ProposalRecorded",
                logical_time=NOW,
                created_at=NOW,
                actor="test",
                source="test",
                trace_id="trace:stale",
                causation_id="cause:stale",
                correlation_id="correlation:stale",
                idempotency_key="proposal:stale",
                payload={"proposal_id": "proposal:stale"},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    with pytest.raises(ConcurrencyConflict):
        runtime.accept(handle)
    assert not runtime.ledger.project().external_perceptions


def test_cold_replay_and_context_consumer_need_no_sidecar_or_network() -> None:
    producer = ExternalPerceptionDeliveryProducer()
    runtime = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID,
        delivery_producer=producer,
    )
    runtime.accept(producer.prepare(_delivery()))

    live = runtime.ledger.project()
    rebuilt = runtime.ledger.rebuild()
    influences = compile_external_perception_life_influences(rebuilt)

    assert rebuilt == live
    assert len(influences) == 1
    assert influences[0].source_event_ref == rebuilt.external_perceptions[0].accepted_event_ref
    assert influences[0].signal_revision_ref == "signal:rain:revision:3"
    assert influences[0].headline == ""
    assert influences[0].subjective_summary == "我注意到深圳这会儿可能有一阵很大的雨。"
    assert influences[0].may_quote is False
    assert influences[0].licensed_summary == ""
    assert influences[0].behavior_suggestion is None


def test_delivery_cannot_freeze_material_the_source_policy_forbids() -> None:
    producer = ExternalPerceptionDeliveryProducer()
    delivery = _delivery()
    forbidden = delivery.selections[0].snapshot.model_copy(
        update={"may_freeze_durable_snapshot": False}
    )
    selection = delivery.selections[0].model_copy(update={"snapshot": forbidden})

    with pytest.raises(ExternalPerceptionAcceptanceError, match="snapshot_not_licensed"):
        producer.prepare(delivery.model_copy(update={"selections": (selection,)}))


def test_sqlite_restart_reconciles_the_same_delivery_effect_once(tmp_path: Path) -> None:
    path = tmp_path / "external-perception.sqlite3"
    first_producer = ExternalPerceptionDeliveryProducer()
    first_runtime = ExternalPerceptionAcceptanceRuntime.open(
        path=path,
        world_id=WORLD_ID,
        delivery_producer=first_producer,
    )
    first = first_runtime.accept(first_producer.prepare(_delivery()))
    first_runtime.close()

    retry_producer = ExternalPerceptionDeliveryProducer()
    reopened = ExternalPerceptionAcceptanceRuntime.open(
        path=path,
        world_id=WORLD_ID,
        delivery_producer=retry_producer,
    )
    retry = reopened.accept(retry_producer.prepare(_delivery()))
    projection = reopened.ledger.project()
    rebuilt = reopened.ledger.rebuild()
    reopened.close()

    assert retry == first
    assert len(projection.external_signal_snapshots) == 1
    assert len(projection.external_perceptions) == 1
    assert rebuilt == projection
