from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from companion_daemon.world_v2.external_world_perception import (
    ExternalSignalPlace,
    ExternalSignalSourceItem,
    ExternalSignalSourceFailure,
    ExternalSignalSourcePage,
    RecordedSignalSourceAdapter,
    SourceCursor,
    SourcePolicyRevision,
    SourceProfile,
    SQLiteWorldPerceptionHub,
)


NOW = datetime(2026, 8, 3, 1, 30, tzinfo=UTC)
TEST_POLICY = SourcePolicyRevision(
    policy_revision="policy:fixture:1",
    may_fetch=True,
    may_cache_raw=True,
    may_store_normalized_summary=True,
    may_embed=True,
    may_expose_to_character_model=False,
    may_quote=False,
    may_freeze_durable_snapshot=False,
    maximum_raw_retention_seconds=86_400,
    maximum_signal_retention_seconds=86_400,
    maximum_normalized_retention_seconds=2_592_000,
)


def test_source_profile_cannot_exceed_its_audited_retention_policy() -> None:
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:licensed",
        pages=(),
    )
    policy = SourcePolicyRevision(
        policy_revision="policy:fixture:1",
        may_fetch=True,
        may_cache_raw=True,
        may_store_normalized_summary=True,
        may_embed=True,
        may_expose_to_character_model=False,
        may_quote=False,
        may_freeze_durable_snapshot=False,
        maximum_raw_retention_seconds=3_600,
        maximum_signal_retention_seconds=86_400,
        maximum_normalized_retention_seconds=86_400,
    )

    with pytest.raises(ValueError, match="raw retention exceeds"):
        SourceProfile(
            adapter=source,
            policy=policy,
            poll_interval_seconds=600,
            signal_ttl_seconds=3_600,
            raw_retention_seconds=3_601,
        )


@pytest.mark.asyncio
async def test_hub_ingests_one_source_page_into_durable_ttl_health(tmp_path) -> None:
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:local-news",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed><entry>fixture</entry></feed>",
                next_cursor=SourceCursor(opaque_value="page:1"),
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="item:summer-book-fair",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        place_scope=ExternalSignalPlace(
                            geometry_kind="region_ref",
                            source_place_ref="region:fixture:city",
                            label="测试城市",
                        ),
                        signal_kind="local_culture_report",
                        headline="周末旧书市集开放",
                        licensed_summary="主办方公布了本周末旧书市集的开放时间。",
                        canonical_url="https://example.test/events/book-fair",
                        published_at=NOW - timedelta(minutes=5),
                    ),
                ),
            ),
        ),
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: NOW,
    )
    try:
        result = await hub.advance_once(observed_at=NOW)
        health = hub.health_snapshot()

        assert result.status == "progressed"
        assert result.progressed_units == 1
        assert result.more_due is False
        assert result.next_wake_at == NOW + timedelta(minutes=10)
        assert health.state == "healthy"
        assert health.signal_revision_count == 1
        assert health.active_signal_count == 1
        assert health.expired_signal_count == 0
        assert health.raw_evidence_count == 1
        assert health.raw_evidence_bytes == 35
        assert health.signal_revisions_last_24h == 1
        assert health.sidecar_main_bytes > 0
        assert health.sidecar_wal_bytes >= 0
        assert health.sidecar_growth_24h_bytes >= 0
        assert health.duplicate_suppressed_count == 0
        assert health.source_states[0].source_id == "source:fixture:local-news"
        assert health.source_states[0].policy_revision == "policy:fixture:1"
        assert health.source_states[0].state == "healthy"
        assert health.source_states[0].last_result == "new_revisions"
        assert health.source_states[0].last_cursor == "page:1"
        assert health.source_states[0].accepted_revision_count == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_hub_appends_a_revision_when_the_same_source_item_changes(tmp_path) -> None:
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:weather",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>rain expected</feed>",
                next_cursor=SourceCursor(opaque_value="page:1"),
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="alert:rain",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="weather_report",
                        headline="明天下雨",
                        licensed_summary="来源预计明天有雨。",
                        published_at=NOW,
                    ),
                ),
            ),
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>rain less likely</feed>",
                next_cursor=SourceCursor(opaque_value="page:2"),
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="alert:rain",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="weather_report",
                        headline="明天降雨概率下降",
                        licensed_summary="同一来源更新了明天的降雨预测。",
                        published_at=NOW,
                        updated_at=NOW + timedelta(minutes=10),
                    ),
                ),
            ),
        ),
    )
    clock = [NOW]
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-revision.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        assert (await hub.advance_once(observed_at=NOW)).status == "progressed"
        clock[0] = NOW + timedelta(minutes=10)
        assert (await hub.advance_once(observed_at=clock[0])).status == "progressed"

        health = hub.health_snapshot()

        assert health.signal_revision_count == 2
        assert health.active_signal_count == 1
        assert health.superseded_revision_count == 1
        assert health.source_states[0].last_cursor == "page:2"
        assert health.source_states[0].accepted_revision_count == 2
        assert health.duplicate_suppressed_count == 0
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_restart_resumes_cursor_and_suppresses_an_identical_revision(tmp_path) -> None:
    database = tmp_path / "external-perception-restart.sqlite3"
    item = ExternalSignalSourceItem(
        upstream_item_id="topic:one",
        gateway_ref="gateway:fixture",
        upstream_publisher_ref="publisher:fixture",
        signal_kind="public_post",
        headline="同一条来源内容",
        licensed_summary="重启后的来源窗口再次包含了它。",
        published_at=NOW,
    )
    first = RecordedSignalSourceAdapter(
        source_id="source:fixture:restart",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>same item</feed>",
                next_cursor=SourceCursor(opaque_value="cursor:one"),
                items=(item,),
            ),
        ),
    )

    def profile(adapter: RecordedSignalSourceAdapter) -> SourceProfile:
        return SourceProfile(
            adapter=adapter,
            policy=TEST_POLICY,
            poll_interval_seconds=600,
            signal_ttl_seconds=3_600,
            raw_retention_seconds=86_400,
        )

    hub = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(first),),
        wall_clock=lambda: NOW,
    )
    await hub.advance_once(observed_at=NOW)
    await hub.aclose()

    replay = RecordedSignalSourceAdapter(
        source_id="source:fixture:restart",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/atom+xml",
                evidence_bytes=b"<feed>same item</feed>",
                next_cursor=SourceCursor(opaque_value="cursor:two"),
                items=(item,),
            ),
        ),
    )
    restarted = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(replay),),
        wall_clock=lambda: NOW + timedelta(minutes=10),
    )
    try:
        await restarted.advance_once(observed_at=NOW + timedelta(minutes=10))
        health = restarted.health_snapshot()

        assert replay.observed_cursors == ("cursor:one",)
        assert health.signal_revision_count == 1
        assert health.active_signal_count == 1
        assert health.raw_evidence_count == 1
        assert health.source_states[0].accepted_revision_count == 1
        assert health.source_states[0].duplicate_suppressed_count == 1
        assert health.source_states[0].last_cursor == "cursor:two"
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_signal_ttl_and_raw_retention_expire_without_rewriting_history(tmp_path) -> None:
    clock = [NOW]
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:ttl",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>brief</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="brief:one",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="local_brief",
                        headline="短时有效消息",
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-ttl.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=60,
                signal_ttl_seconds=60,
                raw_retention_seconds=60,
                normalized_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=61)

        expired_before_maintenance = hub.health_snapshot()
        assert expired_before_maintenance.signal_revision_count == 1
        assert expired_before_maintenance.active_signal_count == 0
        assert expired_before_maintenance.expired_signal_count == 1
        assert expired_before_maintenance.raw_evidence_count == 1

        await hub.advance_once(observed_at=clock[0])
        after_maintenance = hub.health_snapshot()
        assert after_maintenance.signal_revision_count == 1
        assert after_maintenance.raw_evidence_count == 0
        assert after_maintenance.search_indexed_revision_count == 0
        assert after_maintenance.source_states[0].last_result == "not_modified"
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_normalized_retention_removes_stale_revisions_and_clusters(tmp_path) -> None:
    clock = [NOW]
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:normalized-retention",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>temporary item</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="temporary:one",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="temporary_report",
                        headline="最终会滑出来源窗口的临时内容",
                        published_at=NOW,
                    ),
                ),
            ),
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>current window empty</rss>",
            ),
        ),
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-normalized-retention.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=120,
                signal_ttl_seconds=60,
                raw_retention_seconds=86_400,
                normalized_retention_seconds=120,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=121)
        await hub.advance_once(observed_at=clock[0])
        health = hub.health_snapshot()

        assert health.signal_revision_count == 0
        assert health.active_signal_count == 0
        assert health.expired_signal_count == 0
        assert health.cluster_count == 0
        assert health.search_indexed_revision_count == 0
        assert health.source_states[0].last_cursor is None
        assert health.source_states[0].last_result == "no_new_signal"
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_page_isolates_a_bad_correction_and_keeps_a_valid_correction_edge(
    tmp_path,
) -> None:
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:corrections",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>original</rss>",
                next_cursor=SourceCursor(opaque_value="correction:one"),
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="report:original",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="public_report",
                        headline="来源最初的说法",
                        published_at=NOW,
                    ),
                ),
            ),
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>correction page</rss>",
                next_cursor=SourceCursor(opaque_value="correction:two"),
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="report:bad-correction",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="public_report_correction",
                        headline="找不到原文的纠错",
                        published_at=NOW + timedelta(minutes=10),
                        correction_of_upstream_item_id="report:missing",
                    ),
                    ExternalSignalSourceItem(
                        upstream_item_id="report:valid-correction",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="public_report_correction",
                        headline="来源明确更正了原说法",
                        published_at=NOW + timedelta(minutes=10),
                        correction_of_upstream_item_id="report:original",
                    ),
                ),
            ),
        ),
    )
    clock = [NOW]
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-corrections.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(minutes=10)
        result = await hub.advance_once(observed_at=clock[0])
        health = hub.health_snapshot()

        assert result.status == "progressed"
        assert health.signal_revision_count == 2
        assert health.correction_edge_count == 1
        assert health.rejected_item_count == 1
        assert health.source_states[0].rejected_item_count == 1
        assert health.source_states[0].last_cursor == "correction:two"
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_exact_canonical_url_clusters_sources_without_merging_their_claims(
    tmp_path,
) -> None:
    def source(source_id: str, headline: str) -> RecordedSignalSourceAdapter:
        return RecordedSignalSourceAdapter(
            source_id=source_id,
            pages=(
                ExternalSignalSourcePage(
                    evidence_media_type="application/rss+xml",
                    evidence_bytes=b"<rss>shared event evidence</rss>",
                    items=(
                        ExternalSignalSourceItem(
                            upstream_item_id="post:local-festival",
                            gateway_ref="gateway:fixture",
                            upstream_publisher_ref="publisher:fixture",
                            signal_kind="local_report",
                            headline=headline,
                            canonical_url=("https://example.test/events/festival#source-fragment"),
                            published_at=NOW,
                        ),
                    ),
                ),
            ),
        )

    first = source("source:fixture:a", "来源 A 的原文标题")
    second = source("source:fixture:b", "来源 B 的不同标题")
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-cluster.sqlite3",
        sources=tuple(
            SourceProfile(
                adapter=adapter,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            )
            for adapter in (first, second)
        ),
        wall_clock=lambda: NOW,
    )
    try:
        first_result = await hub.advance_once(observed_at=NOW)
        second_result = await hub.advance_once(observed_at=NOW)
        health = hub.health_snapshot()

        assert first_result.more_due is True
        assert second_result.more_due is False
        assert health.signal_revision_count == 2
        assert health.active_signal_count == 2
        assert health.cluster_count == 1
        assert health.raw_evidence_count == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_source_failures_back_off_independently_and_reset_after_success(
    tmp_path,
) -> None:
    recovering = RecordedSignalSourceAdapter(
        source_id="source:fixture:a-recovering",
        pages=(
            ExternalSignalSourceFailure("source_timeout"),
            ExternalSignalSourceFailure("source_timeout"),
            ExternalSignalSourceFailure("source_timeout"),
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>recovered</rss>",
            ),
        ),
    )
    healthy = RecordedSignalSourceAdapter(
        source_id="source:fixture:b-healthy",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>healthy</rss>",
            ),
        ),
    )
    clock = [NOW]
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-failures.sqlite3",
        sources=(
            SourceProfile(
                adapter=recovering,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
            SourceProfile(
                adapter=healthy,
                policy=TEST_POLICY,
                poll_interval_seconds=999_999,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        first = await hub.advance_once(observed_at=clock[0])
        assert first.status == "retry_wait"
        assert first.more_due is True
        assert (await hub.advance_once(observed_at=clock[0])).status == "progressed"

        recovering_health = hub.health_snapshot().source_states[0]
        assert recovering_health.next_refresh_at == NOW + timedelta(minutes=10)

        clock[0] = NOW + timedelta(minutes=10)
        await hub.advance_once(observed_at=clock[0])
        assert hub.health_snapshot().source_states[0].next_refresh_at == (
            NOW + timedelta(minutes=40)
        )

        clock[0] = NOW + timedelta(minutes=40)
        await hub.advance_once(observed_at=clock[0])
        assert hub.health_snapshot().source_states[0].next_refresh_at == (
            NOW + timedelta(minutes=160)
        )

        clock[0] = NOW + timedelta(minutes=160)
        assert (await hub.advance_once(observed_at=clock[0])).status == "progressed"
        recovered = hub.health_snapshot().source_states[0]
        assert recovered.state == "healthy"
        assert recovered.consecutive_failures == 0
        assert recovered.last_failure_code is None
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_search_index_and_embedding_are_built_without_world_authority(
    tmp_path,
) -> None:
    class Embedding:
        version = "embedding:fixture:1"
        dimensions = 3

        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            self.calls += 1
            return tuple((1.0, 0.0, 0.5) for _ in texts)

    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:indexed",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>indexed</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="indexed:one",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="interest_report",
                        headline="独立书店举行夜读活动",
                        licensed_summary="活动包括旧书交换。",
                        entities=("独立书店", "旧书"),
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )
    embedding = Embedding()
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-index.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: NOW,
        embedding=embedding,
    )
    try:
        acquisition = await hub.advance_once(observed_at=NOW)
        assert acquisition.more_due is True
        assert embedding.calls == 0

        result = await hub.advance_once(observed_at=NOW)
        health = hub.health_snapshot()

        assert result.committed_perception_count == 0
        assert embedding.calls == 1
        assert health.search_indexed_revision_count == 1
        assert health.fts_state == "healthy"
        assert health.embedding_state == "healthy"
        assert health.embedding_version == "embedding:fixture:1"
        assert health.embedding_indexed_revision_count == 1
        assert health.last_embedding_failure_code is None
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_parser_rejections_are_visible_without_blocking_acquisition(tmp_path) -> None:
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:partly-malformed",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>one malformed item</rss>",
                parser_rejected_item_count=1,
                parser_failure_codes=("feed_item_malformed",),
            ),
        ),
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-malformed.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: NOW,
    )
    try:
        assert (await hub.advance_once(observed_at=NOW)).status == "progressed"
        health = hub.health_snapshot()
        assert health.state == "warning"
        assert health.rejected_item_count == 1
        assert health.source_states[0].state == "malformed"
        assert health.source_states[0].last_page_rejected_item_count == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_embedding_failure_degrades_index_health_but_keeps_the_signal(tmp_path) -> None:
    class BrokenEmbedding:
        version = "embedding:broken:1"
        dimensions = 3

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            del texts
            raise RuntimeError("provider unavailable")

    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:embedding-failure",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>still durable</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="durable:one",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="publisher_report",
                        headline="即使 embedding 失败也保留",
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )
    hub = SQLiteWorldPerceptionHub(
        path=tmp_path / "external-perception-embedding-failure.sqlite3",
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=600,
                signal_ttl_seconds=3_600,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: NOW,
        embedding=BrokenEmbedding(),
    )
    try:
        acquisition = await hub.advance_once(observed_at=NOW)
        assert acquisition.more_due is True
        assert (await hub.advance_once(observed_at=NOW)).status == "retry_wait"
        health = hub.health_snapshot()
        assert health.signal_revision_count == 1
        assert health.search_indexed_revision_count == 1
        assert health.embedding_indexed_revision_count == 0
        assert health.embedding_state == "degraded"
        assert health.last_embedding_failure_code is not None
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_restart_preserves_embedding_retry_backoff(tmp_path) -> None:
    class BrokenEmbedding:
        version = "embedding:broken:stable"
        dimensions = 2

        def __init__(self) -> None:
            self.calls = 0

        def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
            del texts
            self.calls += 1
            raise RuntimeError("still unavailable")

    database = tmp_path / "external-perception-embedding-restart.sqlite3"
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:embedding-restart",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>retry survives restart</rss>",
                items=(
                    ExternalSignalSourceItem(
                        upstream_item_id="retry:one",
                        gateway_ref="gateway:fixture",
                        upstream_publisher_ref="publisher:fixture",
                        signal_kind="publisher_report",
                        headline="等待独立 embedding 重试",
                        published_at=NOW,
                    ),
                ),
            ),
        ),
    )

    def profile(adapter: RecordedSignalSourceAdapter) -> SourceProfile:
        return SourceProfile(
            adapter=adapter,
            policy=TEST_POLICY,
            poll_interval_seconds=600,
            signal_ttl_seconds=3_600,
            raw_retention_seconds=86_400,
        )

    first_embedding = BrokenEmbedding()
    hub = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(source),),
        wall_clock=lambda: NOW,
        embedding=first_embedding,
    )
    await hub.advance_once(observed_at=NOW)
    assert (await hub.advance_once(observed_at=NOW)).status == "retry_wait"
    assert first_embedding.calls == 1
    await hub.aclose()

    restarted_embedding = BrokenEmbedding()
    restarted_source = RecordedSignalSourceAdapter(
        source_id="source:fixture:embedding-restart",
        pages=(),
    )
    restarted_at = NOW + timedelta(minutes=1)
    restarted = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(restarted_source),),
        wall_clock=lambda: restarted_at,
        embedding=restarted_embedding,
    )
    try:
        result = await restarted.advance_once(observed_at=restarted_at)
        assert result.status == "idle"
        assert result.next_wake_at == NOW + timedelta(minutes=10)
        assert restarted_embedding.calls == 0
        assert restarted.health_snapshot().embedding_pending_count == 1
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_retired_source_state_is_preserved_but_not_scheduled_after_restart(
    tmp_path,
) -> None:
    database = tmp_path / "external-perception-retired-source.sqlite3"

    def source(source_id: str) -> RecordedSignalSourceAdapter:
        return RecordedSignalSourceAdapter(
            source_id=source_id,
            pages=(
                ExternalSignalSourcePage(
                    evidence_media_type="application/rss+xml",
                    evidence_bytes=f"<rss>{source_id}</rss>".encode(),
                ),
            ),
        )

    def profile(adapter: RecordedSignalSourceAdapter) -> SourceProfile:
        return SourceProfile(
            adapter=adapter,
            policy=TEST_POLICY,
            poll_interval_seconds=600,
            signal_ttl_seconds=3_600,
            raw_retention_seconds=86_400,
        )

    first = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(source("source:fixture:a-retired")), profile(source("source:fixture:z"))),
        wall_clock=lambda: NOW,
    )
    await first.aclose()

    retained = source("source:fixture:z")
    restarted = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(retained),),
        wall_clock=lambda: NOW,
    )
    try:
        assert (await restarted.advance_once(observed_at=NOW)).status == "progressed"
        health = restarted.health_snapshot()
        assert tuple(item.source_id for item in health.source_states) == ("source:fixture:z",)
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_source_expiry_is_clamped_and_duplicate_observation_does_not_mutate_revision(
    tmp_path,
) -> None:
    database = tmp_path / "external-perception-immutable-revision.sqlite3"
    item = ExternalSignalSourceItem(
        upstream_item_id="ttl:bounded",
        gateway_ref="gateway:fixture",
        upstream_publisher_ref="publisher:fixture",
        signal_kind="short_lived_report",
        headline="来源声称很久有效但本地只保留一分钟",
        published_at=NOW,
        expires_at=NOW + timedelta(days=1),
    )
    source = RecordedSignalSourceAdapter(
        source_id="source:fixture:immutable-revision",
        pages=(
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>first</rss>",
                items=(item,),
            ),
            ExternalSignalSourcePage(
                evidence_media_type="application/rss+xml",
                evidence_bytes=b"<rss>second observation</rss>",
                items=(item,),
            ),
        ),
    )
    clock = [NOW]
    hub = SQLiteWorldPerceptionHub(
        path=database,
        sources=(
            SourceProfile(
                adapter=source,
                policy=TEST_POLICY,
                poll_interval_seconds=30,
                signal_ttl_seconds=60,
                raw_retention_seconds=86_400,
            ),
        ),
        wall_clock=lambda: clock[0],
    )
    try:
        await hub.advance_once(observed_at=NOW)
        clock[0] = NOW + timedelta(seconds=30)
        await hub.advance_once(observed_at=clock[0])

        with sqlite3.connect(database) as connection:
            revision = connection.execute(
                """
                SELECT observed_at, effective_expires_at
                FROM external_signal_revisions
                """
            ).fetchone()
        assert revision == (
            NOW.isoformat(),
            (NOW + timedelta(seconds=60)).isoformat(),
        )

        clock[0] = NOW + timedelta(seconds=61)
        health = hub.health_snapshot()
        assert health.signal_revision_count == 1
        assert health.active_signal_count == 1

        clock[0] = NOW + timedelta(seconds=91)
        expired = hub.health_snapshot()
        assert expired.active_signal_count == 0
        assert expired.expired_signal_count == 1
    finally:
        await hub.aclose()


@pytest.mark.asyncio
async def test_policy_revision_change_creates_a_new_signal_revision_and_revokes_old_index(
    tmp_path,
) -> None:
    database = tmp_path / "external-perception-policy-revision.sqlite3"
    item = ExternalSignalSourceItem(
        upstream_item_id="policy:one",
        gateway_ref="gateway:fixture",
        upstream_publisher_ref="publisher:fixture",
        signal_kind="publisher_report",
        headline="相同内容但许可发生变化",
        published_at=NOW,
    )

    def adapter() -> RecordedSignalSourceAdapter:
        return RecordedSignalSourceAdapter(
            source_id="source:fixture:policy-change",
            pages=(
                ExternalSignalSourcePage(
                    evidence_media_type="application/rss+xml",
                    evidence_bytes=b"<rss>same content</rss>",
                    items=(item,),
                ),
            ),
        )

    def profile(
        source: RecordedSignalSourceAdapter,
        policy: SourcePolicyRevision,
    ) -> SourceProfile:
        return SourceProfile(
            adapter=source,
            policy=policy,
            poll_interval_seconds=600,
            signal_ttl_seconds=3_600,
            raw_retention_seconds=86_400,
        )

    first = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(adapter(), TEST_POLICY),),
        wall_clock=lambda: NOW,
    )
    await first.advance_once(observed_at=NOW)
    await first.aclose()

    restrictive = TEST_POLICY.model_copy(
        update={
            "policy_revision": "policy:fixture:2-restrictive",
            "may_embed": False,
            "may_expose_to_character_model": False,
        }
    )
    later = NOW + timedelta(minutes=10)
    restarted = SQLiteWorldPerceptionHub(
        path=database,
        sources=(profile(adapter(), restrictive),),
        wall_clock=lambda: later,
    )
    try:
        await restarted.advance_once(observed_at=later)
        health = restarted.health_snapshot()
        with sqlite3.connect(database) as connection:
            policy_revisions = tuple(
                row[0]
                for row in connection.execute(
                    """
                    SELECT source_policy_revision
                    FROM external_signal_revisions ORDER BY revision
                    """
                )
            )

        assert policy_revisions == (
            "policy:fixture:1",
            "policy:fixture:2-restrictive",
        )
        assert health.signal_revision_count == 2
        assert health.superseded_revision_count == 1
        assert health.search_indexed_revision_count == 1
        assert health.embedding_pending_count == 0
    finally:
        await restarted.aclose()
