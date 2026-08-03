from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.external_world_perception import (
    CharacterAttentionContext,
    CharacterAttentionRequest,
    CharacterAttentionResult,
    CharacterAttentionTechnicalFailure,
    ExternalSignalSourceItem,
    ExternalSignalSourcePage,
    PerceptionChannelProof,
    RecordedSignalSourceAdapter,
    ShadowAttentionRuntime,
    SourceBoundAttentionContextItem,
    SourcePolicyRevision,
    SourceProfile,
    SQLiteWorldPerceptionHub,
)


NOW = datetime(2026, 8, 3, 2, 0, tzinfo=UTC)
EXPOSABLE_POLICY = SourcePolicyRevision(
    policy_revision="policy:shadow-fixture:1",
    may_fetch=True,
    may_cache_raw=True,
    may_store_normalized_summary=True,
    may_embed=False,
    may_expose_to_character_model=True,
    may_quote=False,
    may_freeze_durable_snapshot=False,
    maximum_raw_retention_seconds=86_400,
    maximum_signal_retention_seconds=86_400,
    maximum_normalized_retention_seconds=2_592_000,
)


class _ContextPort:
    def __init__(self, context: CharacterAttentionContext) -> None:
        self.context = context

    async def freeze_attention_context(
        self, *, world_id: str, actor_ref: str, observed_at: datetime
    ) -> CharacterAttentionContext:
        assert world_id == self.context.world_id
        assert actor_ref == self.context.actor_ref
        assert observed_at.tzinfo is not None
        return self.context


class _SequenceAttentionModel:
    model_id = "character-attention:fixture:1"

    def __init__(self, outputs: tuple[object, ...]) -> None:
        self._outputs = list(outputs)
        self.requests: list[CharacterAttentionRequest] = []

    async def consider_attention(self, request: CharacterAttentionRequest) -> object:
        self.requests.append(request)
        output = self._outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output(request) if callable(output) else output


class _BlockingAttentionModel:
    model_id = "character-attention:fixture:1"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests: list[CharacterAttentionRequest] = []

    async def consider_attention(self, request: CharacterAttentionRequest) -> object:
        self.requests.append(request)
        self.started.set()
        await self.release.wait()
        return {"selections": []}


def _context(
    *,
    cursor: str = "cursor:world:41",
    source_ids: tuple[str, ...] = ("source:fixture:shadow",),
) -> CharacterAttentionContext:
    return CharacterAttentionContext(
        world_id="zhizhi-world-v2",
        actor_ref="character:zhizhi",
        pinned_world_cursor=cursor,
        current_self_state=(
            SourceBoundAttentionContextItem(
                context_ref="context:self:current",
                context_kind="current_self_state",
                text="刚从外面回来，正在休息。",
                source_refs=("world-event:activity:41",),
            ),
        ),
        situation=(),
        relevant_context=(),
        available_channels=(
            PerceptionChannelProof(
                channel_ref="channel:public-feed",
                channel_kind="public_online_feed",
                evidence_refs=("capability:public-feed:1",),
                accessible_source_ids=source_ids,
                valid_until=NOW + timedelta(hours=1),
            ),
        ),
    )


def _source() -> RecordedSignalSourceAdapter:
    return RecordedSignalSourceAdapter(
        source_id="source:fixture:shadow",
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


def _profile(source: RecordedSignalSourceAdapter) -> SourceProfile:
    return SourceProfile(
        adapter=source,
        policy=EXPOSABLE_POLICY,
        poll_interval_seconds=600,
        signal_ttl_seconds=3_600,
        raw_retention_seconds=86_400,
    )


def _slow_profile(source: RecordedSignalSourceAdapter) -> SourceProfile:
    return SourceProfile(
        adapter=source,
        policy=EXPOSABLE_POLICY,
        poll_interval_seconds=86_400,
        signal_ttl_seconds=86_400,
        raw_retention_seconds=86_400,
    )


@pytest.mark.asyncio
async def test_new_attention_opportunity_is_not_starved_by_a_large_due_source_set(
    tmp_path,
) -> None:
    source_ids = tuple(f"source:fixture:busy:{index:02d}" for index in range(11))
    sources = tuple(
        RecordedSignalSourceAdapter(
            source_id=source_id,
            pages=(
                ExternalSignalSourcePage(
                    evidence_media_type="application/atom+xml",
                    evidence_bytes=b"<feed>first observed trend</feed>",
                    items=(
                        ExternalSignalSourceItem(
                            upstream_item_id="item:first-trend",
                            gateway_ref="gateway:fixture",
                            upstream_publisher_ref="publisher:fixture",
                            signal_kind="platform_trend_observation",
                            headline="一个刚出现的趋势",
                            published_at=NOW,
                        ),
                    ),
                ),
            )
            if index == 0
            else (),
        )
        for index, source_id in enumerate(source_ids)
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-shadow-fairness.sqlite3",
        sources=tuple(_profile(source) for source in sources),
        wall_clock=lambda: NOW,
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:fairness",
            deployment_mode_revision="shadow:fairness",
            worker_id="worker:test:fairness",
            context_port=_ContextPort(_context(source_ids=source_ids)),
            model=_SequenceAttentionModel((CharacterAttentionResult(selections=()),)),
            merge_wait_seconds=120,
        ),
    )
    try:
        assert (await hub.advance_once(observed_at=NOW)).status == "progressed"

        waiting = await hub.advance_once(observed_at=NOW)

        assert waiting.status == "window_wait"
        assert waiting.next_wake_at == NOW + timedelta(minutes=2)
        assert sources[0].observed_cursors == (None,)
        assert all(source.observed_cursors == () for source in sources[1:])
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_shadow_attention_freezes_a_sourced_window_and_records_model_none_once(
    tmp_path,
) -> None:
    clock = [NOW]
    model = _SequenceAttentionModel((CharacterAttentionResult(selections=()),))
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-shadow-none.sqlite3",
        sources=(_profile(_source()),),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:none",
            context_port=_ContextPort(_context()),
            model=model,
            merge_wait_seconds=120,
        ),
    )
    try:
        assert (await hub.advance_once(observed_at=NOW)).status == "progressed"

        waiting = await hub.advance_once(observed_at=NOW)
        assert waiting.status == "window_wait"
        assert waiting.next_wake_at == NOW + timedelta(minutes=2)

        clock[0] = NOW + timedelta(minutes=2)
        considered = await hub.advance_once(observed_at=clock[0])
        assert considered.status == "attention_no_selection"

        assert len(model.requests) == 1
        request = model.requests[0]
        assert request.window.deployment_mode == "shadow"
        assert request.window.pinned_world_cursor == "cursor:world:41"
        assert request.window.attention_attempt_id == request.attention_attempt_id
        assert request.selection_ordinal == 0
        assert request.validation_failure_codes == ()
        assert len(request.window.candidates) == 1
        dossier = request.window.candidates[0]
        assert dossier.model_visible_material[0].headline == "周末音乐节临时增加夜场"
        assert dossier.model_visible_material[0].licensed_summary == "主办方发布了新增夜场的安排。"
        assert dossier.accessible_channels[0].channel_ref == "channel:public-feed"

        health = hub.health_snapshot()
        assert health.shadow_attention.state == "attention_no_selection"
        assert health.shadow_attention.model_no_selection_count == 1
        assert health.shadow_attention.shadow_selected_count == 0
        assert health.shadow_attention.model_call_count_24h == 1

        clock[0] = NOW + timedelta(minutes=3)
        assert (await hub.advance_once(observed_at=clock[0])).status == "idle"
        assert len(model.requests) == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_invalid_shadow_selection_gets_one_exact_model_owned_reselection(
    tmp_path,
) -> None:
    clock = [NOW]

    def invalid(_: CharacterAttentionRequest) -> object:
        return {
            "selections": [
                {
                    "candidate_ref": "candidate:not-in-window",
                    "exact_signal_revision_refs": ["revision:not-in-window"],
                    "selected_channel_ref": "channel:invented",
                    "subjective_summary": "我注意到了它。",
                    "epistemic_notes": "",
                    "attended_context_refs": [],
                }
            ]
        }

    def corrected(request: CharacterAttentionRequest) -> object:
        dossier = request.window.candidates[0]
        return {
            "selections": [
                {
                    "candidate_ref": dossier.candidate_ref,
                    "exact_signal_revision_refs": [dossier.exact_signal_revisions[0]],
                    "selected_channel_ref": dossier.accessible_channels[0].channel_ref,
                    "subjective_summary": "我刷到附近周末的音乐节加了夜场。",
                    "epistemic_notes": "目前只有主办方这一个来源。",
                    "attended_context_refs": ["context:self:current"],
                }
            ]
        }

    model = _SequenceAttentionModel((invalid, corrected))
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-shadow-reselection.sqlite3",
        sources=(_profile(_source()),),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:reselection",
            context_port=_ContextPort(_context()),
            model=model,
            merge_wait_seconds=1,
        ),
    )
    try:
        await hub.advance_once(observed_at=NOW)
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=1)

        result = await hub.advance_once(observed_at=clock[0])

        assert result.status == "shadow_selected"
        assert [item.selection_ordinal for item in model.requests] == [0, 1]
        assert model.requests[1].attention_attempt_id == model.requests[0].attention_attempt_id
        assert model.requests[1].retry_ordinal == 0
        assert model.requests[1].validation_failure_codes == (
            "unknown_candidate:candidate:not-in-window",
        )
        assert model.requests[1].rejected_result_json is not None
        health = hub.health_snapshot()
        assert health.shadow_attention.state == "shadow_selected"
        assert health.shadow_attention.shadow_selected_count == 1
        assert health.shadow_attention.invalid_result_count_24h == 1
        assert health.shadow_attention.model_call_count_24h == 2
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_invalid_reselection_is_technical_failure_with_stable_10_30_120_retry(
    tmp_path,
) -> None:
    clock = [NOW]

    def invalid(_: CharacterAttentionRequest) -> object:
        return {
            "selections": [
                {
                    "candidate_ref": "candidate:invented",
                    "exact_signal_revision_refs": ["revision:invented"],
                    "selected_channel_ref": "channel:invented",
                    "subjective_summary": "这不是合法候选。",
                }
            ]
        }

    def valid(request: CharacterAttentionRequest) -> object:
        dossier = request.window.candidates[0]
        return {
            "selections": [
                {
                    "candidate_ref": dossier.candidate_ref,
                    "exact_signal_revision_refs": [dossier.exact_signal_revisions[0]],
                    "selected_channel_ref": dossier.accessible_channels[0].channel_ref,
                    "subjective_summary": "这次我确实注意到了。",
                }
            ]
        }

    model = _SequenceAttentionModel((invalid,) * 6 + (valid,))
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-shadow-backoff.sqlite3",
        sources=(_slow_profile(_source()),),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:backoff",
            context_port=_ContextPort(_context()),
            model=model,
            merge_wait_seconds=1,
        ),
    )
    try:
        await hub.advance_once(observed_at=NOW)
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=1)

        first = await hub.advance_once(observed_at=clock[0])
        assert first.status == "retry_wait"
        assert first.next_wake_at == NOW + timedelta(minutes=10, seconds=1)

        clock[0] = first.next_wake_at
        second = await hub.advance_once(observed_at=clock[0])
        assert second.status == "retry_wait"
        assert second.next_wake_at == NOW + timedelta(minutes=40, seconds=1)

        clock[0] = second.next_wake_at
        third = await hub.advance_once(observed_at=clock[0])
        assert third.status == "retry_wait"
        assert third.next_wake_at == NOW + timedelta(hours=2, minutes=40, seconds=1)

        clock[0] = third.next_wake_at
        recovered = await hub.advance_once(observed_at=clock[0])
        assert recovered.status == "shadow_selected"

        attempt_ids = {item.attention_attempt_id for item in model.requests}
        assert len(attempt_ids) == 1
        assert [item.retry_ordinal for item in model.requests] == [0, 0, 1, 1, 2, 2, 3]
        assert [item.selection_ordinal for item in model.requests] == [0, 1, 0, 1, 0, 1, 0]
        health = hub.health_snapshot()
        assert health.shadow_attention.technical_failure_count_24h == 3
        assert health.shadow_attention.invalid_result_count_24h == 6
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_restart_reuses_the_frozen_window_and_does_not_repeat_a_terminal_shadow_attempt(
    tmp_path,
) -> None:
    database = tmp_path / "external-perception-shadow-restart.sqlite3"
    clock = [NOW]
    first_model = _SequenceAttentionModel(
        (CharacterAttentionTechnicalFailure("provider_unavailable"),)
    )
    first = SQLiteWorldPerceptionHub(
        path=database,
        sources=(_slow_profile(_source()),),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:before-restart",
            context_port=_ContextPort(_context(cursor="cursor:world:before")),
            model=first_model,
            merge_wait_seconds=1,
        ),
    )
    await first.advance_once(observed_at=NOW)
    await first.advance_once(observed_at=NOW)
    clock[0] = NOW + timedelta(seconds=1)
    failed = await first.advance_once(observed_at=clock[0])
    assert failed.status == "retry_wait"
    frozen_attempt_id = first_model.requests[0].attention_attempt_id
    await first.aclose()

    def valid(request: CharacterAttentionRequest) -> object:
        dossier = request.window.candidates[0]
        return {
            "selections": [
                {
                    "candidate_ref": dossier.candidate_ref,
                    "exact_signal_revision_refs": [dossier.exact_signal_revisions[0]],
                    "selected_channel_ref": dossier.accessible_channels[0].channel_ref,
                    "subjective_summary": "重启后仍只处理原来冻结的这一窗。",
                }
            ]
        }

    replay_source = RecordedSignalSourceAdapter(
        source_id="source:fixture:shadow",
        pages=(),
    )
    recovered_model = _SequenceAttentionModel((valid,))
    clock[0] = failed.next_wake_at
    restarted = SQLiteWorldPerceptionHub(
        path=database,
        sources=(_slow_profile(replay_source),),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:after-restart",
            context_port=_ContextPort(_context(cursor="cursor:world:after")),
            model=recovered_model,
            merge_wait_seconds=1,
        ),
    )
    try:
        recovered = await restarted.advance_once(observed_at=clock[0])
        assert recovered.status == "shadow_selected"
        assert recovered_model.requests[0].attention_attempt_id == frozen_attempt_id
        assert recovered_model.requests[0].retry_ordinal == 1
        assert recovered_model.requests[0].window.pinned_world_cursor == "cursor:world:before"

        clock[0] += timedelta(seconds=1)
        assert (await restarted.advance_once(observed_at=clock[0])).status == "idle"
        assert len(recovered_model.requests) == 1
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_concurrent_worker_joins_the_live_shadow_attention_lease(tmp_path) -> None:
    database = tmp_path / "external-perception-shadow-lease.sqlite3"
    clock = [NOW]
    blocking = _BlockingAttentionModel()

    def runtime(*, worker_id: str, model: object) -> ShadowAttentionRuntime:
        return ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id=worker_id,
            context_port=_ContextPort(_context()),
            model=model,  # type: ignore[arg-type]
            merge_wait_seconds=1,
            lease_seconds=60,
        )

    first = SQLiteWorldPerceptionHub(
        path=database,
        sources=(_slow_profile(_source()),),
        wall_clock=lambda: clock[0],
        shadow_attention=runtime(worker_id="worker:test:lease:first", model=blocking),
    )
    await first.advance_once(observed_at=NOW)
    await first.advance_once(observed_at=NOW)

    second_model = _SequenceAttentionModel((CharacterAttentionResult(selections=()),))
    second = SQLiteWorldPerceptionHub(
        path=database,
        sources=(
            _slow_profile(
                RecordedSignalSourceAdapter(
                    source_id="source:fixture:shadow",
                    pages=(),
                )
            ),
        ),
        wall_clock=lambda: clock[0],
        shadow_attention=runtime(worker_id="worker:test:lease:second", model=second_model),
    )
    try:
        clock[0] = NOW + timedelta(seconds=1)
        first_task = asyncio.create_task(first.advance_once(observed_at=clock[0]))
        await asyncio.wait_for(blocking.started.wait(), timeout=2)

        joined = await second.advance_once(observed_at=clock[0])
        assert joined.status == "joined_existing"
        assert joined.next_wake_at == clock[0] + timedelta(seconds=60)
        assert second_model.requests == []

        clock[0] += timedelta(seconds=61)
        reclaimed = await second.advance_once(observed_at=clock[0])
        assert reclaimed.status == "attention_no_selection"
        assert second_model.requests[0].retry_ordinal == 1

        blocking.release.set()
        assert (await first_task).status == "joined_existing"
        assert len(blocking.requests) == 1
        assert second.health_snapshot().shadow_attention.model_no_selection_count == 1
    finally:
        blocking.release.set()
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_shadow_window_preserves_exact_corrections_and_cross_source_disagreement(
    tmp_path,
) -> None:
    clock = [NOW]
    canonical_url = "https://events.example.test/night-market"
    first_source = RecordedSignalSourceAdapter(
        source_id="source:fixture:a",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>original and correction</feed>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="market:original",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:a",
                        signal_kind="local_event_report",
                        headline="夜市周六开放",
                        licensed_summary="最初公告称周六开放。",
                        canonical_url=canonical_url,
                        published_at=NOW,
                    ),
                    ExternalSignalSourceItem(
                        upstream_item_id="market:correction",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:a",
                        signal_kind="local_event_correction",
                        headline="更正：夜市改到周日",
                        licensed_summary="发布者明确更正了日期。",
                        canonical_url=canonical_url,
                        published_at=NOW + timedelta(seconds=1),
                        correction_of_upstream_item_id="market:original",
                    ),
                ),
            ),
        ),
    )
    second_source = RecordedSignalSourceAdapter(
        source_id="source:fixture:b",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>second source</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="night-market:b",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:b",
                        signal_kind="local_event_report",
                        headline="周末夜市活动安排",
                        licensed_summary="另一来源仍写的是周六。",
                        canonical_url=canonical_url,
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )
    model = _SequenceAttentionModel((CharacterAttentionResult(selections=()),))
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-shadow-evidence.sqlite3",
        sources=(_slow_profile(first_source), _slow_profile(second_source)),
        wall_clock=lambda: clock[0],
        shadow_attention=ShadowAttentionRuntime(
            world_id="zhizhi-world-v2",
            actor_ref="character:zhizhi",
            attention_policy_revision="attention-policy:shadow:1",
            deployment_mode_revision="shadow:1",
            worker_id="worker:test:evidence",
            context_port=_ContextPort(
                _context(source_ids=("source:fixture:a", "source:fixture:b"))
            ),
            model=model,
            merge_wait_seconds=1,
        ),
    )
    try:
        await hub.advance_once(observed_at=NOW)
        await hub.advance_once(observed_at=NOW)
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=1)
        assert (await hub.advance_once(observed_at=clock[0])).status == "attention_no_selection"

        dossier = model.requests[0].window.candidates[0]
        assert len(dossier.exact_signal_revisions) == 3
        assert len(dossier.model_visible_material) == 3
        assert len(dossier.corrections) == 1
        assert dossier.corrections[0].corrected_revision_ref in dossier.exact_signal_revisions
        assert dossier.corrections[0].correction_revision_ref in dossier.exact_signal_revisions
        assert dossier.source_disagreements[0].differing_fields == (
            "headline",
            "licensed_summary",
        )
    finally:
        await hub.aclose()
