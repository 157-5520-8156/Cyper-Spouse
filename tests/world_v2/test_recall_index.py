from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.recall_audit import (
    CharacterRecallRequest,
    MAX_RECALL_AUDIT_BYTES,
)
from companion_daemon.world_v2.recall_corpus import RecallCorpusSources
from companion_daemon.world_v2.recall_index import (
    FeatureHashRecallEmbedding,
    InMemoryRecallIndex,
    RecallCursor,
    RecallDocument,
    RecallQuery,
    RecallSourceBinding,
    SQLiteRecallIndex,
)
from companion_daemon.world_v2.recall_runtime import (
    RecallCoordinator,
    verify_trusted_recall_trace,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
CURSOR = RecallCursor(
    world_revision=12,
    deliberation_revision=4,
    ledger_sequence=81,
)


class _SemanticFixtureEmbedding:
    version = "fixture-semantic.1"
    dimensions = 3

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors = {
            "泡茶工具": (1.0, 0.0, 0.0),
            "乌龙茶": (0.9, 0.1, 0.0),
            "她上周买了一个白色盖碗。": (0.99, 0.01, 0.0),
            "用户最近开始喜欢喝乌龙茶。": (0.8, 0.2, 0.0),
            "雨夜回家时觉得很放松。": (0.0, 1.0, 0.0),
            "她暂时觉得用户可能很在意被认真倾听。": (0.0, 0.0, 1.0),
            "不应进入当前查看者范围。": (0.0, 0.0, 1.0),
            "旧的、已被替代的茶偏好。": (1.0, 0.0, 0.0),
        }
        return tuple(vectors[text] for text in texts)


class _CountingSemanticFixtureEmbedding(_SemanticFixtureEmbedding):
    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.embedded_texts.extend(texts)
        return super().embed(texts)


class _CalibratedSemanticFixtureEmbedding:
    version = "calibrated-semantic-fixture.1"
    dimensions = 2
    dense_match_threshold_bp = 4_200

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        vectors = {
            "间接地问起旧事": (1.0, 0.0),
            "一条语义相关但没有共同字词的记忆": (0.45, 0.893),
        }
        return tuple(vectors[text] for text in texts)


class _RetrievalTextFixtureEmbedding:
    version = "retrieval-text-fixture.1"
    dimensions = 2
    dense_match_threshold_bp = 4_200

    def __init__(self) -> None:
        self.embedded_texts: list[str] = []

    def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.embedded_texts.extend(texts)
        vectors = {
            "你还记得我是做什么的吗": (1.0, 0.0),
            "我是软件工程师。": (0.0, 1.0),
            "用户工作、职业、做什么；原话：我是软件工程师。": (0.99, 0.01),
        }
        return tuple(vectors[text] for text in texts)


def _bindings(*refs: str, revision: int) -> tuple[RecallSourceBinding, ...]:
    return tuple(
        RecallSourceBinding(
            source_kind="committed_event",
            authority_type="FixtureEvent",
            ref=ref,
            source_world_revision=revision,
            immutable_hash=f"{index + 1:x}" * 64,
        )
        for index, ref in enumerate(sorted(refs))
    )


def _documents() -> tuple[RecallDocument, ...]:
    return (
        RecallDocument(
            document_id="recall:fact:oolong",
            memory_kind="semantic",
            source_item_ref="fact:oolong",
            source_slice="relevant_facts",
            source_refs=("event:fact:oolong", "event:observation:oolong"),
            source_bindings=_bindings(
                "event:fact:oolong", "event:observation:oolong", revision=7
            ),
            source_world_revision=7,
            text="用户最近开始喜欢喝乌龙茶。",
            actor_ref="agent:companion",
            subject_refs=("user:primary",),
            link_refs=("thread:tea", "topic:tea"),
            occurred_from=NOW.replace(day=25),
            valid_from=NOW.replace(day=25),
            privacy_class="personal",
        ),
        RecallDocument(
            document_id="recall:experience:gaiwan",
            memory_kind="episodic",
            source_item_ref="experience:gaiwan",
            source_slice="recent_experiences",
            source_refs=("event:experience:gaiwan",),
            source_bindings=_bindings("event:experience:gaiwan", revision=8),
            source_world_revision=8,
            text="她上周买了一个白色盖碗。",
            actor_ref="agent:companion",
            subject_refs=("agent:companion",),
            link_refs=("topic:tea",),
            occurred_from=NOW.replace(day=20),
            occurred_to=NOW.replace(day=20, hour=12, minute=10),
            privacy_class="private",
        ),
        RecallDocument(
            document_id="recall:experience:rain",
            memory_kind="episodic",
            source_item_ref="experience:rain",
            source_slice="recent_experiences",
            source_refs=("event:experience:rain",),
            source_bindings=_bindings("event:experience:rain", revision=9),
            source_world_revision=9,
            text="雨夜回家时觉得很放松。",
            actor_ref="agent:companion",
            subject_refs=("agent:companion",),
            link_refs=("topic:weather",),
            occurred_from=NOW.replace(day=26),
            privacy_class="private",
        ),
        RecallDocument(
            document_id="recall:reflection:listening",
            memory_kind="reflective",
            source_item_ref="impression:listening",
            source_slice="private_impressions",
            source_refs=("event:appraisal:listening", "event:impression:listening"),
            source_bindings=_bindings(
                "event:appraisal:listening",
                "event:impression:listening",
                revision=10,
            ),
            source_world_revision=10,
            text="她暂时觉得用户可能很在意被认真倾听。",
            actor_ref="agent:companion",
            subject_refs=("user:primary",),
            link_refs=("relationship:user:primary",),
            occurred_from=NOW.replace(day=26),
            privacy_class="withhold",
            authority="defeasible_interpretation",
        ),
        RecallDocument(
            document_id="recall:private:other",
            memory_kind="semantic",
            source_item_ref="fact:other",
            source_slice="relevant_facts",
            source_refs=("event:fact:other",),
            source_bindings=_bindings("event:fact:other", revision=11),
            source_world_revision=11,
            text="不应进入当前查看者范围。",
            actor_ref="agent:companion",
            subject_refs=("user:other",),
            occurred_from=NOW,
            privacy_class="withhold",
        ),
        RecallDocument(
            document_id="recall:superseded:tea",
            memory_kind="semantic",
            source_item_ref="fact:old-tea",
            source_slice="relevant_facts",
            source_refs=("event:fact:old-tea",),
            source_bindings=_bindings("event:fact:old-tea", revision=3),
            source_world_revision=3,
            text="旧的、已被替代的茶偏好。",
            actor_ref="agent:companion",
            subject_refs=("user:primary",),
            occurred_from=NOW.replace(year=2025),
            valid_from=NOW.replace(year=2025),
            valid_to=NOW.replace(day=1),
            status="superseded",
            privacy_class="personal",
        ),
    )


def _query(**updates: object) -> RecallQuery:
    values: dict[str, object] = {
        "query_text": "泡茶工具",
        "cursor": CURSOR,
        "actor_ref": "agent:companion",
        "subject_refs": ("agent:companion", "user:primary"),
        "viewer_privacy_ceiling": "withhold",
        "at": NOW,
        "limit": 4,
        "accessibility_seed": "draw:recall:tea:1",
    }
    values.update(updates)
    return RecallQuery(**values)


def test_hybrid_recall_fuses_dense_lexical_temporal_and_structured_evidence() -> None:
    index = InMemoryRecallIndex(embedding=_SemanticFixtureEmbedding())
    index.rebuild(cursor=CURSOR, documents=_documents())

    result = index.search(
        _query(
            query_text="乌龙茶",
            link_refs=("thread:tea",),
            occurred_from=NOW.replace(day=19),
        )
    )

    assert result.index_cursor == CURSOR
    assert result.embedding_version == "fixture-semantic.1"
    assert result.hits[0].document.source_item_ref == "fact:oolong"
    assert {"lexical", "structured", "temporal"} <= set(result.hits[0].match_channels)
    assert any(
        hit.document.source_item_ref == "experience:gaiwan"
        and "dense" in hit.match_channels
        for hit in result.hits
    )
    assert all(hit.document.status == "active" for hit in result.hits)
    assert result.query_hash and result.result_hash


def test_dense_recall_can_surface_a_source_bound_association_without_word_overlap() -> None:
    index = InMemoryRecallIndex(embedding=_SemanticFixtureEmbedding())
    index.rebuild(cursor=CURSOR, documents=_documents())

    result = index.search(_query())

    assert result.hits[0].document.source_item_ref == "experience:gaiwan"
    assert "dense" in result.hits[0].match_channels
    assert all(
        hit.document.source_item_ref not in {"fact:other", "fact:old-tea"}
        for hit in result.hits
    )


def test_superseded_fact_requires_explicit_historical_recall() -> None:
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=CURSOR, documents=_documents())

    current = index.search(_query(query_text="旧的茶偏好", limit=4))
    historical = index.search(
        _query(query_text="旧的茶偏好", include_historical=True, limit=4)
    )

    assert all(
        hit.document.source_item_ref != "fact:old-tea" for hit in current.hits
    )
    old = next(
        hit for hit in historical.hits if hit.document.source_item_ref == "fact:old-tea"
    )
    assert old.document.status == "superseded"
    assert old.document.valid_to == NOW.replace(day=1)


def test_semantic_adapter_can_calibrate_dense_candidate_threshold() -> None:
    document = RecallDocument(
        document_id="recall:calibrated",
        memory_kind="semantic",
        source_item_ref="fact:calibrated",
        source_slice="relevant_facts",
        source_refs=("event:fact:calibrated",),
        source_bindings=_bindings("event:fact:calibrated", revision=7),
        source_world_revision=7,
        text="一条语义相关但没有共同字词的记忆",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=NOW,
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=_CalibratedSemanticFixtureEmbedding())
    index.rebuild(cursor=CURSOR, documents=(document,))

    result = index.search(_query(query_text="间接地问起旧事"))

    assert len(result.hits) == 1
    assert result.hits[0].dense_score_bp == 4_500
    assert result.hits[0].match_channels == ("dense",)


def test_recall_embeds_search_metadata_but_returns_exact_source_text() -> None:
    embedding = _RetrievalTextFixtureEmbedding()
    document = RecallDocument(
        document_id="recall:occupation",
        memory_kind="semantic",
        source_item_ref="fact:occupation",
        source_slice="relevant_facts",
        source_refs=("event:fact:occupation",),
        source_bindings=_bindings("event:fact:occupation", revision=7),
        source_world_revision=7,
        text="我是软件工程师。",
        retrieval_text="用户工作、职业、做什么；原话：我是软件工程师。",
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=NOW,
        privacy_class="personal",
    )
    index = InMemoryRecallIndex(embedding=embedding)
    index.rebuild(cursor=CURSOR, documents=(document,))

    result = index.search(_query(query_text="你还记得我是做什么的吗", limit=1))

    assert result.hits[0].document.text == "我是软件工程师。"
    assert result.hits[0].document.retrieval_text == document.retrieval_text
    assert "dense" in result.hits[0].match_channels
    assert document.retrieval_text in embedding.embedded_texts


def test_lexical_recall_keeps_a_short_entity_cue_inside_a_long_message() -> None:
    document = RecallDocument(
        document_id="recall:coffee-reaction",
        memory_kind="semantic",
        source_item_ref="fact:coffee-reaction",
        source_slice="relevant_facts",
        source_refs=("event:fact:coffee-reaction",),
        source_bindings=_bindings("event:fact:coffee-reaction", revision=7),
        source_world_revision=7,
        text="咖啡是真不行，我一喝就心悸。",
        retrieval_text=(
            "Exact source statement: 咖啡是真不行，我一喝就心悸。 "
            "Semantic fact slot: 用户健康状况、过敏或受伤"
        ),
        actor_ref="agent:companion",
        subject_refs=("user:primary",),
        occurred_from=NOW,
        privacy_class="private",
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=CURSOR, documents=(document,))

    result = index.search(
        _query(
            query_text="公司楼下新开了家咖啡馆，装修还挺好看的",
            limit=1,
        )
    )

    assert result.hits
    assert result.hits[0].document.source_item_ref == "fact:coffee-reaction"
    assert "lexical" in result.hits[0].match_channels


def test_pinned_snapshot_keeps_exact_rows_after_newer_index_rebuild() -> None:
    index = InMemoryRecallIndex(embedding=_SemanticFixtureEmbedding())
    index.rebuild(cursor=CURSOR, documents=_documents())
    pinned = index.snapshot()
    expected = pinned.search(_query())
    newer = RecallCursor(
        world_revision=13,
        deliberation_revision=5,
        ledger_sequence=82,
    )

    index.rebuild(cursor=newer, documents=())

    assert index.snapshot().cursor == newer
    assert pinned.search(_query()) == expected


def test_sqlite_recall_sidecar_reopens_and_rebuilds_without_becoming_truth(
    tmp_path,
) -> None:
    path = tmp_path / "recall.sqlite"
    first = SQLiteRecallIndex(
        path=path,
        world_id="world:recall-test",
        embedding=_SemanticFixtureEmbedding(),
    )
    first.rebuild(cursor=CURSOR, documents=_documents())
    expected = first.search(_query())
    first.close()

    reopened = SQLiteRecallIndex(
        path=path,
        world_id="world:recall-test",
        embedding=_SemanticFixtureEmbedding(),
    )
    replayed = reopened.search(_query())
    no_write = reopened.rebuild(cursor=CURSOR, documents=tuple(reversed(_documents())))
    rebuilt = reopened.search(_query())
    reopened.close()

    assert replayed == expected
    assert rebuilt == expected
    assert no_write.mode == "noop"
    assert no_write.sqlite_changes == 0
    assert all(
        hit.document.source_refs
        and hit.document.authority in {"world_fact", "defeasible_interpretation"}
        for hit in rebuilt.hits
    )


def test_sqlite_rebuild_reuses_embeddings_for_unchanged_documents(tmp_path) -> None:
    embedding = _CountingSemanticFixtureEmbedding()
    index = SQLiteRecallIndex(
        path=tmp_path / "recall-cache.sqlite",
        world_id="world:recall-cache",
        embedding=embedding,
    )
    index.rebuild(cursor=CURSOR, documents=_documents())
    first_count = len(embedding.embedded_texts)

    report = index.rebuild(
        cursor=RecallCursor(
            world_revision=13,
            deliberation_revision=5,
            ledger_sequence=82,
        ),
        documents=tuple(reversed(_documents())),
    )
    index.close()

    assert first_count == len(_documents())
    assert len(embedding.embedded_texts) == first_count
    assert report.mode == "cursor_only"


def test_recall_result_is_bounded_before_durable_audit() -> None:
    base = _documents()[0]
    documents = tuple(
        base.model_copy(
            update={
                "document_id": f"recall:large:{index}",
                "source_item_ref": f"fact:large:{index}",
                "text": "凤凰单丛" * 256,
            }
        )
        for index in range(6)
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=CURSOR, documents=documents)
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
    )

    trace = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(query_text="凤凰单丛", limit=6),
            accessibility_seed="draw:bounded-audit",
            expected_cursor=CURSOR,
            trigger_ref="trigger:bounded-audit",
        )
    )
    encoded = json.dumps(
        trace.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert trace.hits
    assert len(trace.hits) < len(documents)
    assert len(encoded) <= MAX_RECALL_AUDIT_BYTES


def test_semantic_embedding_runs_only_after_character_chooses_recall() -> None:
    semantic = _CountingSemanticFixtureEmbedding()
    primary = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    primary.rebuild(cursor=CURSOR, documents=_documents())
    coordinator = RecallCoordinator.from_built_index(
        index=primary,
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        semantic_embedding=semantic,
    )

    coordinator.prefetch(
        query_text="乌龙茶",
        accessibility_seed="draw:local-prefetch",
        trigger_ref="trigger:semantic-on-pull",
    )
    assert semantic.embedded_texts == []

    coordinator.recall(
        request=CharacterRecallRequest(query_text="泡茶工具"),
        accessibility_seed="draw:semantic-pull",
        expected_cursor=CURSOR,
        trigger_ref="trigger:semantic-on-pull",
    )

    assert "泡茶工具" in semantic.embedded_texts
    assert "她上周买了一个白色盖碗。" in semantic.embedded_texts


def test_paired_carry_accepts_only_the_target_context_exact_predecessor() -> None:
    trigger_ref = "trigger:paired-exact"
    first = CURSOR
    second = RecallCursor(
        world_revision=13,
        deliberation_revision=5,
        ledger_sequence=82,
    )
    third = RecallCursor(
        world_revision=14,
        deliberation_revision=6,
        ledger_sequence=83,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=first, documents=_documents())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=first,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref=trigger_ref,
    )
    source = coordinator.recall(
        request=CharacterRecallRequest(query_text="乌龙茶"),
        accessibility_seed="draw:paired-exact",
        expected_cursor=first,
        trigger_ref=trigger_ref,
    )
    coordinator.refresh(
        cursor=second,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref=trigger_ref,
    )
    coordinator.refresh(
        cursor=third,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref=trigger_ref,
    )

    carried = verify_trusted_recall_trace(
        coordinator.carry_forward(
            source,
            evaluated_cursor=second,
            trigger_ref=trigger_ref,
        )
    )
    assert carried.index_cursor == first
    assert carried.evaluated_cursor == second
    assert carried.paired_transition_hash is not None
    with pytest.raises(ValueError, match="exact predecessor"):
        coordinator.carry_forward(
            source,
            evaluated_cursor=third,
            trigger_ref=trigger_ref,
        )
    with pytest.raises(ValueError, match="exactly once"):
        coordinator.carry_forward(
            coordinator.carry_forward(
                source,
                evaluated_cursor=second,
                trigger_ref=trigger_ref,
            ),
            evaluated_cursor=third,
            trigger_ref=trigger_ref,
        )


def test_same_cursor_contexts_remain_isolated_by_trigger_identity() -> None:
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=CURSOR, documents=_documents())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="trigger:first",
    )
    coordinator.refresh(
        cursor=CURSOR,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref="trigger:second",
    )

    first = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(query_text="乌龙茶"),
            accessibility_seed="draw:first-trigger",
            expected_cursor=CURSOR,
            trigger_ref="trigger:first",
        )
    )
    second = verify_trusted_recall_trace(
        coordinator.recall(
            request=CharacterRecallRequest(query_text="乌龙茶"),
            accessibility_seed="draw:second-trigger",
            expected_cursor=CURSOR,
            trigger_ref="trigger:second",
        )
    )

    assert first.hits
    assert second.hits == ()
    assert first.trigger_ref == "trigger:first"
    assert second.trigger_ref == "trigger:second"


def test_paired_carry_cannot_skip_an_intervening_different_trigger_context() -> None:
    first = CURSOR
    intervening = RecallCursor(
        world_revision=13,
        deliberation_revision=5,
        ledger_sequence=82,
    )
    target = RecallCursor(
        world_revision=14,
        deliberation_revision=6,
        ledger_sequence=83,
    )
    index = InMemoryRecallIndex(embedding=FeatureHashRecallEmbedding())
    index.rebuild(cursor=first, documents=_documents())
    coordinator = RecallCoordinator.from_built_index(
        index=index,
        cursor=first,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        trigger_ref="trigger:paired",
    )
    source = coordinator.recall(
        request=CharacterRecallRequest(query_text="乌龙茶"),
        accessibility_seed="draw:paired-intervening",
        expected_cursor=first,
        trigger_ref="trigger:paired",
    )
    coordinator.refresh(
        cursor=intervening,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref="trigger:other",
    )
    coordinator.refresh(
        cursor=target,
        actor_ref="agent:companion",
        subject_refs=("agent:companion", "user:primary"),
        logical_time=NOW,
        sources=RecallCorpusSources(),
        trigger_ref="trigger:paired",
    )

    with pytest.raises(ValueError, match="exact predecessor"):
        coordinator.carry_forward(
            source,
            evaluated_cursor=target,
            trigger_ref="trigger:paired",
        )
