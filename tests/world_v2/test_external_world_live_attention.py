from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json

import pytest

from companion_daemon.world_v2.external_perception_acceptance import (
    ExternalPerceptionAcceptanceRuntime,
    ExternalPerceptionDeliveryProducer,
)
from companion_daemon.world_v2.external_world_perception import (
    AuditedLiveCharacterAttentionResult,
    CharacterAttentionTechnicalFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    LiveAttentionRuntime,
    LiveCharacterAttentionContext,
    LiveCharacterAttentionRequest,
    LiveCharacterAttentionResult,
    LiveCharacterAttentionSelection,
    PerceptionChannelProof,
    RecordedSignalSourceAdapter,
    SourceBoundAttentionContextItem,
    SourcePolicyRevision,
    SourceProfile,
    SQLiteWorldPerceptionHub,
)
from companion_daemon.world_v2.external_world_perception.live_acceptance import (
    LifeWakingExternalPerceptionAcceptance,
    ProducerBackedExternalPerceptionAcceptance,
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


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)
WORLD_ID = "zhizhi-world-v2"
POLICY = SourcePolicyRevision(
    policy_revision="policy:live-fixture:1",
    may_fetch=True,
    may_cache_raw=True,
    may_store_normalized_summary=True,
    may_embed=False,
    may_expose_to_character_model=True,
    may_quote=False,
    may_freeze_durable_snapshot=True,
    maximum_raw_retention_seconds=86_400,
    maximum_signal_retention_seconds=86_400,
    maximum_normalized_retention_seconds=2_592_000,
)


class _CrashAfterCommit(BaseException):
    pass


class _AcceptancePort:
    def __init__(self, runtime: ExternalPerceptionAcceptanceRuntime, *, fail_after=None) -> None:
        self.runtime = runtime
        self.producer = ExternalPerceptionDeliveryProducer()
        self.fail_after = fail_after
        self.calls = []

    async def accept_external_perception(self, delivery):
        self.calls.append(delivery)
        receipt = self.runtime.accept(self.producer.prepare(delivery))
        if self.fail_after is not None:
            failure = self.fail_after
            self.fail_after = None
            raise failure
        return receipt


class _Life:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def advance_life_ecology_once(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        return object()


class _FailOnceLife(_Life):
    async def advance_life_ecology_once(self, **kwargs: str) -> object:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("fixture_life_crash_after_world_commit")
        return object()


class _LedgerContextPort:
    def __init__(
        self,
        acceptance: ExternalPerceptionAcceptanceRuntime,
        *,
        stale_once: bool = False,
    ) -> None:
        self.acceptance = acceptance
        self.calls = 0
        self.stale_once = stale_once

    async def freeze_attention_context(self, *, world_id, actor_ref, observed_at):
        self.calls += 1
        projection = self.acceptance.ledger.project()
        cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        if self.stale_once and self.calls == 1:
            cursor = ProjectionCursor(
                world_revision=0,
                deliberation_revision=0,
                ledger_sequence=0,
            )
        return LiveCharacterAttentionContext(
            world_id=world_id,
            actor_ref=actor_ref,
            pinned_world_cursor=cursor,
            world_logical_time=observed_at,
            current_self_state=(
                SourceBoundAttentionContextItem(
                    context_ref="context:self:current",
                    context_kind="current_self_state",
                    text="刚从外面回来，正在休息。",
                    source_refs=("event:activity:current",),
                ),
            ),
            situation=(),
            relevant_context=(),
            available_channels=(
                PerceptionChannelProof(
                    channel_ref="channel:public-feed",
                    channel_kind="public_online_feed",
                    evidence_refs=("capability:public-feed:1",),
                    accessible_source_ids=("source:fixture:live",),
                    valid_until=observed_at + timedelta(hours=1),
                ),
            ),
        )


class _LiveModel:
    model_id = "character-attention:live-fixture:1"

    def __init__(
        self,
        *,
        invalid_first: bool = False,
        empty_notes_first: bool = False,
        quote_source_first: bool = False,
        quote_headline_first: bool = False,
        select_all_revisions: bool = False,
    ) -> None:
        self.invalid_first = invalid_first
        self.empty_notes_first = empty_notes_first
        self.quote_source_first = quote_source_first
        self.quote_headline_first = quote_headline_first
        self.select_all_revisions = select_all_revisions
        self.requests: list[LiveCharacterAttentionRequest] = []

    async def consider_attention(self, request: LiveCharacterAttentionRequest):
        self.requests.append(request)
        dossier = request.window.candidates[0]
        if self.invalid_first and len(self.requests) == 1:
            decision = LiveCharacterAttentionResult(
                selections=(
                    LiveCharacterAttentionSelection(
                        candidate_ref="candidate:not-offered",
                        exact_signal_revision_refs=("signal:not-offered",),
                        selected_channel_ref="channel:not-offered",
                        subjective_summary="我注意到了。",
                        epistemic_notes="我不能确认这个候选。",
                        privacy_class="public",
                    ),
                )
            )
        else:
            revision_refs = (
                dossier.exact_signal_revisions
                if self.select_all_revisions
                else (dossier.exact_signal_revisions[-1],)
            )
            decision = LiveCharacterAttentionResult(
                selections=(
                    LiveCharacterAttentionSelection(
                        candidate_ref=dossier.candidate_ref,
                        exact_signal_revision_refs=revision_refs,
                        selected_channel_ref=dossier.accessible_channels[0].channel_ref,
                        subjective_summary=(
                            "周末音乐节临时增加夜场"
                            if self.quote_headline_first and len(self.requests) == 1
                            else "主办方发布了新增夜场的安排。"
                            if self.quote_source_first and len(self.requests) == 1
                            else "我刷到附近周末的音乐节加了夜场。"
                        ),
                        epistemic_notes=(
                            ""
                            if self.empty_notes_first and len(self.requests) == 1
                            else "目前来源范围已经写在证据窗里。"
                        ),
                        attended_context_refs=("context:self:current",),
                        privacy_class="public",
                    ),
                )
            )
        return _audited(request=request, decision=decision)


def _audited(*, request, decision):
    decision_json = canonical_json(decision.model_dump(mode="json"))
    proposal_hash = "sha256:" + sha256(decision_json)
    call_id = f"model-call:{request.attention_attempt_id}:{request.selection_ordinal}"
    # Provider response auditing binds exact transport bytes; it need not equal
    # the canonical semantic decision bytes used by proposal_hash.
    response_hash = sha256("```json\n" + decision_json + "\n```")
    result_ref = "model-result:" + sha256(
        canonical_json({"model_call_id": call_id, "response_hash": response_hash})
    )
    audit = RecordedModelResultAudit(
        model_call_id=call_id,
        model_result_ref=result_ref,
        attempt_id=request.attention_attempt_id,
        route=RecordedModelRoute(
            tier="flash",
            reason_code="external_perception_attention",
            router_version="external-perception-attention-router.1",
        ),
        model_id="character-attention:live-fixture:1",
        model_version="1",
        request_hash=sha256(request.model_dump_json()),
        response_hash=response_hash,
        status="proposal_validated",
    )
    audit_json = model_audit_json(audit)
    result = ModelResultRecordedPayload(
        model_result_ref=result_ref,
        deliberation_result_id="deliberation:"
        + sha256(
            canonical_json(
                {
                    "capsule_id": request.window.candidate_set_hash,
                    "proposal_hash": proposal_hash,
                    "attempt_audits": [json.loads(audit_json)],
                }
            )
        ),
        proposal_hash=proposal_hash,
        model_call_id=call_id,
        attempt_id=request.attention_attempt_id,
        capsule_id=request.window.candidate_set_hash,
        trigger_ref=request.attention_attempt_id,
        evaluated_world_revision=request.window.pinned_world_cursor.world_revision,
        attempt_index=0,
        attempt_count=1,
        audit_json=audit_json,
        audit_hash=sha256(audit_json),
    )
    return AuditedLiveCharacterAttentionResult(decision=decision, model_result=result)


def _source():
    return RecordedSignalSourceAdapter(
        source_id="source:fixture:live",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>city music festival</feed>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="item:music-festival",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="local_culture_report",
                        headline="周末音乐节临时增加夜场",
                        licensed_summary="主办方发布了新增夜场的安排。",
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )


def _undated_trend_source():
    return RecordedSignalSourceAdapter(
        source_id="source:fixture:live",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>observed platform ranking</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="rank:music-festival",
                        gateway_ref="rsshub:http://127.0.0.1:1200",
                        upstream_publisher_ref="aggregator:trend",
                        signal_kind="platform_trend_observation",
                        headline="周末音乐节临时增加夜场",
                        licensed_summary="",
                        canonical_url="https://example.test/trends/music-festival",
                        published_at=None,
                    ),
                ),
            ),
        ),
    )


def _multi_revision_source():
    canonical_url = "https://events.example.test/music-festival"
    return RecordedSignalSourceAdapter(
        source_id="source:fixture:live",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>two publisher reports</feed>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="item:music-festival:a",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture:a",
                        signal_kind="local_culture_report",
                        headline="周末音乐节临时增加夜场",
                        licensed_summary="主办方发布了新增夜场的安排。",
                        canonical_url=canonical_url,
                        published_at=NOW,
                    ),
                    ExternalSignalSourceItem(
                        upstream_item_id="item:music-festival:b",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture:b",
                        signal_kind="local_culture_report",
                        headline="音乐节夜场安排更新",
                        licensed_summary="另一发布者也列出了夜场时间。",
                        canonical_url=canonical_url,
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )


def _profile(source, *, raw_retention_seconds=86_400):
    return SourceProfile(
        adapter=source,
        policy=POLICY,
        poll_interval_seconds=86_400,
        signal_ttl_seconds=86_400,
        raw_retention_seconds=raw_retention_seconds,
    )


def _hub(
    tmp_path,
    *,
    model,
    acceptance_port,
    context_port,
    clock,
    source=None,
    raw_retention_seconds=86_400,
    merge_wait_seconds=1,
):
    return SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-live.sqlite3",
        sources=(
            _profile(
                source or _source(),
                raw_retention_seconds=raw_retention_seconds,
            ),
        ),
        wall_clock=lambda: clock[0],
        live_attention=LiveAttentionRuntime(
            world_id=WORLD_ID,
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:live:1",
            deployment_mode_revision="live:1",
            worker_id="worker:test:live",
            context_port=context_port,
            model=model,
            acceptance_port=acceptance_port,
            merge_wait_seconds=merge_wait_seconds,
        ),
    )


async def _reach_attention(hub, clock):
    assert (await hub.advance_once(observed_at=clock[0])).status == "progressed"
    assert (await hub.advance_once(observed_at=clock[0])).status == "window_wait"
    clock[0] += timedelta(seconds=1)
    return await hub.advance_once(observed_at=clock[0])


@pytest.mark.asyncio
async def test_live_attention_commits_selected_evidence_with_audited_character_choice(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel(invalid_first=True)
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        result = await _reach_attention(hub, clock)
        projection = acceptance.ledger.project()

        assert result.status == "perception_committed"
        assert result.committed_perception_count == 1
        assert [item.selection_ordinal for item in model.requests] == [0, 1]
        assert model.requests[1].validation_failure_codes == (
            "unknown_candidate:candidate:not-offered",
        )
        assert len(projection.external_perceptions) == 1
        assert projection.external_perceptions[0].subjective_summary.startswith("我刷到")
        assert port.calls[0].pinned_cursor == ProjectionCursor(
            world_revision=0, deliberation_revision=0, ledger_sequence=0
        )
        assert port.calls[0].selections[0].snapshot.model_visible_material_hash
        health = hub.health_snapshot().shadow_attention
        assert health.state == "perception_committed"
        assert health.live_committed_count == 1
        assert health.outbox_backlog_count == 0
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_uses_observation_time_without_forging_publication_time(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel()
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
        source=_undated_trend_source(),
    )
    try:
        result = await _reach_attention(hub, clock)
        visible = model.requests[0].window.candidates[0].model_visible_material[0]
        snapshot = port.calls[0].selections[0].snapshot

        assert result.status == "perception_committed"
        assert visible.published_at is None
        assert visible.observed_at == NOW
        assert snapshot.published_at is None
        assert snapshot.observed_at == NOW
        assert acceptance.ledger.export_replay_evidence().projection == (
            acceptance.ledger.export_replay_evidence().replay
        )
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_retains_raw_through_merge_and_scheduler_handoff(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    hub = _hub(
        tmp_path,
        model=_LiveModel(),
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
        raw_retention_seconds=631,
        merge_wait_seconds=600,
    )
    try:
        assert (await hub.advance_once(observed_at=clock[0])).status == "progressed"
        clock[0] += timedelta(seconds=30)
        assert (await hub.advance_once(observed_at=clock[0])).status == "window_wait"
        clock[0] += timedelta(seconds=600)

        result = await hub.advance_once(observed_at=clock[0])

        assert result.status == "perception_committed"
        replay = acceptance.ledger.export_replay_evidence()
        assert replay.projection == replay.replay
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_end_to_end_commits_then_opens_one_life_opportunity(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    life = _Life()
    port = LifeWakingExternalPerceptionAcceptance(
        acceptance=ProducerBackedExternalPerceptionAcceptance(
            producer=producer,
            runtime=acceptance,
        ),
        life=life,
    )
    model = _LiveModel()
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        result = await _reach_attention(hub, clock)
        projection = acceptance.ledger.rebuild()

        assert result.status == "perception_committed"
        assert len(projection.external_perceptions) == 1
        assert life.calls == [
            {
                "wake_event_ref": projection.external_perceptions[0].accepted_event_ref,
                "trace_id": f"trace:external-perception:{model.requests[0].attention_attempt_id}",
                "correlation_id": (f"external-perception:{model.requests[0].attention_attempt_id}"),
            }
        ]
        assert (await hub.advance_once(observed_at=clock[0])).status in {
            "idle",
            "joined_existing",
        }
        assert len(life.calls) == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_returns_empty_epistemic_notes_for_one_model_reselection(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel(empty_notes_first=True)
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        result = await _reach_attention(hub, clock)

        assert result.status == "perception_committed"
        assert [item.selection_ordinal for item in model.requests] == [0, 1]
        assert model.requests[1].validation_failure_codes == (
            f"epistemic_notes_required:{model.requests[0].window.candidates[0].candidate_ref}",
        )
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_reselects_when_role_copies_non_quotable_source_prose(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel(quote_source_first=True)
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        result = await _reach_attention(hub, clock)
        revision_ref = model.requests[0].window.candidates[0].exact_signal_revisions[0]

        assert result.status == "perception_committed"
        assert [item.selection_ordinal for item in model.requests] == [0, 1]
        assert model.requests[1].validation_failure_codes == (
            f"non_quotable_source_reproduced:{revision_ref}",
        )
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_attention_reselects_when_role_copies_non_quotable_headline(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel(quote_headline_first=True)
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        result = await _reach_attention(hub, clock)
        revision_ref = model.requests[0].window.candidates[0].exact_signal_revisions[0]

        assert result.status == "perception_committed"
        assert model.requests[1].validation_failure_codes == (
            f"non_quotable_source_reproduced:{revision_ref}",
        )
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_one_character_selection_can_adopt_multiple_exact_source_revisions(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel(select_all_revisions=True)
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
        source=_multi_revision_source(),
    )
    try:
        result = await _reach_attention(hub, clock)

        assert result.status == "perception_committed"
        assert result.committed_perception_count == 2
        assert len(model.requests) == 1
        assert len(model.requests[0].window.candidates[0].exact_signal_revisions) == 2
        assert len(acceptance.ledger.project().external_perceptions) == 2
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_stale_live_attention_is_superseded_and_same_signal_gets_fresh_cursor(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance)
    port.producer = producer
    model = _LiveModel()
    context = _LedgerContextPort(acceptance, stale_once=True)
    hub = _hub(tmp_path, model=model, acceptance_port=port, context_port=context, clock=clock)
    try:
        await hub.advance_once(observed_at=clock[0])
        await hub.advance_once(observed_at=clock[0])
        acceptance.ledger.commit(
            [
                WorldEvent.from_payload(
                    schema_version="world-v2.1",
                    event_id="event:concurrent",
                    world_id=WORLD_ID,
                    event_type="ProposalRecorded",
                    logical_time=clock[0],
                    created_at=clock[0],
                    actor="test",
                    source="test",
                    trace_id="trace:concurrent",
                    causation_id="cause:concurrent",
                    correlation_id="correlation:concurrent",
                    idempotency_key="proposal:concurrent",
                    payload={"proposal_id": "proposal:concurrent"},
                )
            ],
            expected_world_revision=0,
            expected_deliberation_revision=0,
        )
        clock[0] += timedelta(seconds=1)
        assert (await hub.advance_once(observed_at=clock[0])).status == "superseded"
        assert not acceptance.ledger.project().external_perceptions
        stale_health = hub.health_snapshot().shadow_attention
        assert stale_health.state == "superseded"
        assert stale_health.live_superseded_count == 1

        assert (await hub.advance_once(observed_at=clock[0])).status == "window_wait"
        clock[0] += timedelta(seconds=1)
        committed = await hub.advance_once(observed_at=clock[0])
        assert committed.status == "perception_committed"
        assert len({item.attention_attempt_id for item in model.requests}) == 2
        assert model.requests[-1].window.pinned_world_cursor.ledger_sequence == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_live_acceptance_retry_after_post_commit_crash_does_not_repeat_model_or_event(
    tmp_path,
):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(
        acceptance,
        fail_after=CharacterAttentionTechnicalFailure("post_commit_crash"),
    )
    port.producer = producer
    model = _LiveModel()
    hub = _hub(
        tmp_path,
        model=model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        first = await _reach_attention(hub, clock)
        assert first.status == "retry_wait"
        assert first.next_wake_at == clock[0] + timedelta(minutes=10)
        assert len(acceptance.ledger.project().external_perceptions) == 1
        waiting_health = hub.health_snapshot().shadow_attention
        assert waiting_health.state == "delivery_pending"
        assert waiting_health.outbox_backlog_count == 1
        assert waiting_health.acceptance_failure_count_24h == 1

        await hub.aclose()
        recovered_model = _LiveModel()
        hub = _hub(
            tmp_path,
            model=recovered_model,
            acceptance_port=port,
            context_port=_LedgerContextPort(acceptance),
            clock=clock,
        )
        clock[0] = first.next_wake_at
        recovered = await hub.advance_once(observed_at=clock[0])
        assert recovered.status == "perception_committed"
        assert len(model.requests) == 1
        assert recovered_model.requests == []
        assert len(port.calls) == 2
        assert port.calls[0] == port.calls[1]
        assert len(acceptance.ledger.project().external_perceptions) == 1
        recovered_health = hub.health_snapshot().shadow_attention
        assert recovered_health.state == "perception_committed"
        assert recovered_health.outbox_backlog_count == 0
        assert recovered_health.live_committed_count == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_sqlite_retry_reconciles_committed_delivery_then_retries_the_same_life_wake(
    tmp_path,
):
    clock = [NOW]
    world_path = tmp_path / "world.sqlite3"
    first_producer = ExternalPerceptionDeliveryProducer()
    first_acceptance = ExternalPerceptionAcceptanceRuntime.open(
        path=world_path,
        world_id=WORLD_ID,
        delivery_producer=first_producer,
    )
    life = _FailOnceLife()
    first_model = _LiveModel()
    first_hub = _hub(
        tmp_path,
        model=first_model,
        acceptance_port=LifeWakingExternalPerceptionAcceptance(
            acceptance=ProducerBackedExternalPerceptionAcceptance(
                producer=first_producer,
                runtime=first_acceptance,
            ),
            life=life,
        ),
        context_port=_LedgerContextPort(first_acceptance),
        clock=clock,
    )
    first = await _reach_attention(first_hub, clock)
    assert first.status == "retry_wait"
    assert len(first_acceptance.ledger.project().external_perceptions) == 1
    await first_hub.aclose()
    first_acceptance.close()

    retry_producer = ExternalPerceptionDeliveryProducer()
    retry_acceptance = ExternalPerceptionAcceptanceRuntime.open(
        path=world_path,
        world_id=WORLD_ID,
        delivery_producer=retry_producer,
    )
    retry_model = _LiveModel()
    retry_hub = _hub(
        tmp_path,
        model=retry_model,
        acceptance_port=LifeWakingExternalPerceptionAcceptance(
            acceptance=ProducerBackedExternalPerceptionAcceptance(
                producer=retry_producer,
                runtime=retry_acceptance,
            ),
            life=life,
        ),
        context_port=_LedgerContextPort(retry_acceptance),
        clock=clock,
    )
    try:
        clock[0] = first.next_wake_at
        recovered = await retry_hub.advance_once(observed_at=clock[0])
        projection = retry_acceptance.ledger.rebuild()

        assert recovered.status == "perception_committed"
        assert retry_model.requests == []
        assert len(projection.external_perceptions) == 1
        assert len(life.calls) == 2
        assert life.calls[0] == life.calls[1]
    finally:
        await retry_hub.aclose()
        retry_acceptance.close()


@pytest.mark.asyncio
async def test_hard_crash_after_world_commit_reconciles_outbox_after_lease_expiry(tmp_path):
    clock = [NOW]
    producer = ExternalPerceptionDeliveryProducer()
    acceptance = ExternalPerceptionAcceptanceRuntime.in_memory(
        world_id=WORLD_ID, delivery_producer=producer
    )
    port = _AcceptancePort(acceptance, fail_after=_CrashAfterCommit())
    port.producer = producer
    first_model = _LiveModel()
    hub = _hub(
        tmp_path,
        model=first_model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    with pytest.raises(_CrashAfterCommit):
        await _reach_attention(hub, clock)
    assert len(acceptance.ledger.project().external_perceptions) == 1
    await hub.aclose()

    recovered_model = _LiveModel()
    hub = _hub(
        tmp_path,
        model=recovered_model,
        acceptance_port=port,
        context_port=_LedgerContextPort(acceptance),
        clock=clock,
    )
    try:
        clock[0] += timedelta(minutes=5, seconds=1)
        recovered = await hub.advance_once(observed_at=clock[0])

        assert recovered.status == "perception_committed"
        assert len(first_model.requests) == 1
        assert recovered_model.requests == []
        assert len(port.calls) == 2
        assert len(acceptance.ledger.project().external_perceptions) == 1
    finally:
        await hub.aclose()


def test_live_runtime_cannot_reuse_a_shadow_deployment_identity():
    with pytest.raises(ValueError, match="live attention cannot use a shadow"):
        LiveAttentionRuntime(
            world_id=WORLD_ID,
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:live:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:live",
            context_port=object(),
            model=object(),
            acceptance_port=object(),
        )
