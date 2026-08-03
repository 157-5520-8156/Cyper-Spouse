from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.external_world_perception.contracts import (
    CharacterAttentionContext,
    CharacterAttentionRequest,
    CharacterAttentionResult,
    PerceptionWindow,
    LiveCharacterAttentionContext,
    LiveCharacterAttentionRequest,
    LiveCharacterAttentionResult,
    LivePerceptionWindow,
    PerceptionChannelProof,
)
from companion_daemon.world_v2.external_world_perception.production_attention import (
    CapsuleBackedLiveAttentionContextPort,
    CapsuleBackedShadowAttentionContextPort,
    ChatCompletionLiveAttentionModel,
    ChatCompletionShadowAttentionModel,
    StaticLiveAttentionChannelPort,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.schemas import WorldEvent
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=UTC)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _world() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id="world:attention-production")
    ledger.commit(
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:world-started:attention-production",
                world_id=ledger.world_id,
                event_type="WorldStarted",
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:attention-production",
                causation_id="cause:attention-production",
                correlation_id="correlation:attention-production",
                idempotency_key="identity:attention-production",
                payload={},
            )
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


class _ForbiddenUserSlicesCapsule:
    capsule_id = "c" * 64

    def __init__(self) -> None:
        source = SimpleNamespace(ref="event:self:1")
        item = SimpleNamespace(
            item_ref="self:1",
            payload_json='{"activity":"在窗边整理东西"}',
            source_bindings=(source,),
        )
        self.character_core = SimpleNamespace(items=(item,))
        self.affect_episodes = SimpleNamespace(items=())
        self.current_situation = SimpleNamespace(items=())
        self.world_life = SimpleNamespace(items=())
        self.relationship_slice = SimpleNamespace(items=())
        self.appraisals = SimpleNamespace(items=())
        self.open_threads = SimpleNamespace(items=())
        self.recent_experiences = SimpleNamespace(items=())

    @property
    def recent_dialogue(self) -> object:
        raise AssertionError("external attention must not read user dialogue")

    @property
    def relevant_facts(self) -> object:
        raise AssertionError("external attention must not read user location facts")

    @property
    def private_impressions(self) -> object:
        raise AssertionError("external attention must not read private user impressions")


class _Compiler:
    def __init__(self) -> None:
        self.queries = []

    def compile(self, query):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        return _ForbiddenUserSlicesCapsule()


@pytest.mark.asyncio
async def test_context_port_freezes_complete_cursor_and_only_role_safe_capsule_slices() -> None:
    ledger = _world()
    compiler = _Compiler()
    channel = PerceptionChannelProof(
        channel_ref="channel:public-feed",
        channel_kind="public_online_feed",
        evidence_refs=("event:world-started:attention-production",),
        accessible_source_ids=("source:public-feed",),
        valid_until=NOW + timedelta(hours=1),
    )
    port = CapsuleBackedLiveAttentionContextPort(
        ledger=ledger,
        capsule_compiler=compiler,
        channel_port=StaticLiveAttentionChannelPort((channel,)),
    )

    context = await port.freeze_attention_context(
        world_id=ledger.world_id,
        actor_ref="character:zhizhi",
        observed_at=NOW,
    )

    assert isinstance(context, LiveCharacterAttentionContext)
    projected = ledger.project()
    assert context.pinned_world_cursor == ProjectionCursor(
        world_revision=projected.world_revision,
        deliberation_revision=projected.deliberation_revision,
        ledger_sequence=projected.ledger_sequence,
    )
    assert context.world_logical_time == NOW
    assert context.current_self_state[0].text == '{"activity":"在窗边整理东西"}'
    assert context.current_self_state[0].source_refs == ("event:self:1",)
    assert context.available_channels == (channel,)
    assert compiler.queries[0].cursor == context.pinned_world_cursor


@pytest.mark.asyncio
async def test_context_port_rejects_channel_evidence_missing_from_pinned_world() -> None:
    ledger = _world()
    port = CapsuleBackedLiveAttentionContextPort(
        ledger=ledger,
        capsule_compiler=_Compiler(),
        channel_port=StaticLiveAttentionChannelPort(
            (
                PerceptionChannelProof(
                    channel_ref="channel:invented",
                    channel_kind="public_online_feed",
                    evidence_refs=("event:not-committed",),
                    accessible_source_ids=("source:public-feed",),
                    valid_until=NOW + timedelta(hours=1),
                ),
            )
        ),
    )

    with pytest.raises(ValueError, match="absent from the pinned World cursor"):
        await port.freeze_attention_context(
            world_id=ledger.world_id,
            actor_ref="character:zhizhi",
            observed_at=NOW,
        )


class _ChatModel:
    model = "fixture-background-model"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        self.calls.append((messages, temperature))
        return self.response


def _request(*, ordinal: int = 0) -> LiveCharacterAttentionRequest:
    projection = _world().project()
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )
    context = LiveCharacterAttentionContext(
        world_id="world:attention-production",
        actor_ref="character:zhizhi",
        pinned_world_cursor=cursor,
        world_logical_time=NOW,
        current_self_state=(),
        situation=(),
        relevant_context=(),
        available_channels=(),
    )
    window = LivePerceptionWindow.model_construct(
        window_id="window:1",
        attention_attempt_id="attempt:1",
        opportunity_id="opportunity:1",
        world_id=context.world_id,
        actor_ref=context.actor_ref,
        pinned_world_cursor=cursor,
        attention_policy_revision="attention-policy:1",
        deployment_mode="live",
        deployment_mode_revision="live:test:1",
        generated_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        candidates=(),
        durable_snapshots=(),
        candidate_set_hash="a" * 64,
        exposure_draw_ref="draw:1",
    )
    return LiveCharacterAttentionRequest(
        attention_attempt_id="attempt:1",
        retry_ordinal=0,
        selection_ordinal=ordinal,
        window=window,
        current_context=context,
        validation_failure_codes=("result_shape_invalid:selections",) if ordinal else (),
        rejected_result_json='{"wrong":true}' if ordinal else None,
    )


@pytest.mark.asyncio
async def test_model_adapter_preserves_exact_audit_and_leaves_zero_or_many_to_character() -> None:
    raw = '{ "selections" : [] }'
    model = _ChatModel(raw)
    adapter = ChatCompletionLiveAttentionModel(
        model=model,
        model_id=model.model,
        adapter_revision="external-attention-chat.1",
    )

    result = await adapter.consider_attention(_request())

    assert result.decision == LiveCharacterAttentionResult(selections=())
    assert result.model_result.proposal_hash == "sha256:" + _hash_text('{"selections":[]}')
    audit = json.loads(result.model_result.audit_json)
    assert audit["request_hash"] == _hash_text(
        json.dumps(model.calls[0][0], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    assert audit["response_hash"] == _hash_text(raw)
    trace = adapter.trace_for(
        attention_attempt_id="attempt:1", retry_ordinal=0, selection_ordinal=0
    )
    assert trace.request_json == json.dumps(
        model.calls[0][0], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert trace.response_text == raw
    system_prompt = model.calls[0][0][0]["content"]
    assert "固定动机" not in system_prompt
    assert "必须联系" not in system_prompt
    assert "是否注意以及注意多少条都由你决定" in system_prompt


@pytest.mark.asyncio
async def test_model_adapter_exposes_exact_reselection_failures_without_local_fallback() -> None:
    model = _ChatModel('{"selections":[]}')
    adapter = ChatCompletionLiveAttentionModel(model=model, model_id=model.model)

    await adapter.consider_attention(_request(ordinal=1))

    prompt = model.calls[0][0][1]["content"]
    assert "result_shape_invalid:selections" in prompt
    assert json.loads(prompt)["reselection"]["rejected_result_json"] == '{"wrong":true}'
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_invalid_model_shape_is_returned_to_coordinator_without_local_repair() -> None:
    model = _ChatModel("not json at all")
    adapter = ChatCompletionLiveAttentionModel(model=model, model_id=model.model)

    rejected = await adapter.consider_attention(_request())

    assert rejected["invalid_model_output"] == "not json at all"
    assert rejected["response_hash"] == _hash_text("not json at all")
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_shadow_context_uses_same_capsule_but_only_an_opaque_read_cursor() -> None:
    ledger = _world()
    compiler = _Compiler()
    channel = PerceptionChannelProof(
        channel_ref="channel:public-feed",
        channel_kind="public_online_feed",
        evidence_refs=("event:world-started:attention-production",),
        accessible_source_ids=("source:public-feed",),
        valid_until=NOW + timedelta(hours=1),
    )
    port = CapsuleBackedShadowAttentionContextPort(
        ledger=ledger,
        capsule_compiler=compiler,
        channel_port=StaticLiveAttentionChannelPort((channel,)),
    )

    context = await port.freeze_attention_context(
        world_id=ledger.world_id,
        actor_ref="character:zhizhi",
        observed_at=NOW,
    )

    assert isinstance(context, CharacterAttentionContext)
    cursor = json.loads(context.pinned_world_cursor.removeprefix("projection-cursor:"))
    assert cursor == {
        "deliberation_revision": 0,
        "ledger_sequence": 1,
        "world_revision": 1,
    }
    assert context.current_self_state[0].source_refs == ("event:self:1",)


@pytest.mark.asyncio
async def test_shadow_model_uses_real_character_choice_without_v2_audit_authority() -> None:
    live_request = _request()
    shadow_context = CharacterAttentionContext(
        world_id=live_request.current_context.world_id,
        actor_ref=live_request.current_context.actor_ref,
        pinned_world_cursor="projection-cursor:{}",
        current_self_state=(),
        situation=(),
        relevant_context=(),
        available_channels=(),
    )
    shadow_window = PerceptionWindow.model_construct(
        **live_request.window.model_dump(
            mode="python",
            exclude={
                "durable_snapshots",
                "pinned_world_cursor",
                "deployment_mode",
                "deployment_mode_revision",
            },
        ),
        deployment_mode="shadow",
        deployment_mode_revision="shadow:test:1",
        pinned_world_cursor=shadow_context.pinned_world_cursor,
    )
    request = CharacterAttentionRequest(
        attention_attempt_id=shadow_window.attention_attempt_id,
        retry_ordinal=0,
        selection_ordinal=0,
        window=shadow_window,
        current_context=shadow_context,
    )
    model = _ChatModel('{"selections":[]}')
    adapter = ChatCompletionShadowAttentionModel(model=model, model_id=model.model)

    result = await adapter.consider_attention(request)

    assert result == CharacterAttentionResult(selections=())
    assert not hasattr(result, "model_result")
    assert "是否注意以及注意多少条都由你决定" in model.calls[0][0][0]["content"]
