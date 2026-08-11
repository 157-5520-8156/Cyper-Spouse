from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from companion_daemon.world_v2.interaction_act_reducers import reduce_interaction_act
from companion_daemon.world_v2.interaction_act_runtime import (
    DeliveredExpressionInteractionActSource,
    InteractionActRoleOutput,
    ObservedInteractionActSource,
    interaction_act_context_items,
    interaction_act_conversation_ref,
    materialize_interaction_act_mutation,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
WORLD = "world:interaction-act"
CONVERSATION = "conversation:qq:geoff"
COUNTERPART = "user:geoff"
COMPANION = "actor:companion"
FRIEND = "npc:friend"
OUTSIDER = "npc:outsider"


def _observed(
    *,
    actor: str,
    text: str,
    suffix: str,
    revision: int = 7,
    payload_hash: str = "a" * 64,
) -> ObservedInteractionActSource:
    return ObservedInteractionActSource(
        world_id=WORLD,
        conversation_ref=CONVERSATION,
        source_event_ref=f"event:observation:{suffix}",
        source_world_revision=revision,
        source_payload_hash=payload_hash,
        source_actor_ref=actor,
        source_text=text,
    )


def _output(
    *,
    operation: str,
    status_code: str,
    source_text_span: str,
    interaction_act_ref: str | None = None,
    act_kind: str = "多方协调",
    subject_ref: str = COUNTERPART,
    counterparty_refs: tuple[str, ...] = (COMPANION, FRIEND),
    object_ref: str | None = None,
    object_label: str | None = None,
) -> InteractionActRoleOutput:
    return InteractionActRoleOutput(
        contract="interaction-act-role-output.2",
        source_text_span=source_text_span,
        operation=operation,
        status_code=status_code,
        interaction_act_ref=interaction_act_ref,
        act_kind=act_kind,
        subject_ref=subject_ref,
        counterparty_refs=counterparty_refs,
        object_ref=object_ref,
        object_label=object_label,
    )


def _declare_group():
    source = _observed(
        actor=COUNTERPART,
        text="我已经把你和小林都约到周六一起讨论了。",
        suffix="declare-group",
    )
    mutation = materialize_interaction_act_mutation(
        authored=_output(
            operation="declare",
            status_code="  已发起协调  ",
            source_text_span=source.source_text,
            act_kind="  多方周末协调  ",
        ),
        source=source,
        current=(),
        logical_time=NOW,
    )
    current, history = reduce_interaction_act(
        (),
        (),
        mutation,
        logical_time=NOW,
        accepted_event_ref="event:interaction-act-transition-accepted:declare-group",
    )
    return source, mutation, current, history


def _revise(*, current, actor: str, text: str, status_code: str, minute: int):
    act = current[0]
    source = _observed(
        actor=actor,
        text=text,
        suffix=f"revise-{actor}-{minute}",
        revision=7 + minute,
        payload_hash=f"{minute % 10}" * 64,
    )
    object_ref = (
        act.object_descriptor.object_ref
        if act.object_descriptor is not None
        else None
    )
    mutation = materialize_interaction_act_mutation(
        authored=_output(
            operation="revise",
            status_code=status_code,
            source_text_span=text,
            interaction_act_ref=act.interaction_act_id,
            act_kind=act.act_kind,
            subject_ref=act.subject_ref,
            counterparty_refs=act.counterparty_refs,
            object_ref=object_ref,
        ),
        source=source,
        current=current,
        logical_time=NOW + timedelta(minutes=minute),
    )
    return source, mutation


def test_declare_projects_free_subject_status_for_a_multi_party_act() -> None:
    source, mutation, current, history = _declare_group()

    projected = current[0]
    assert projected.act_kind == "多方周末协调"
    assert projected.counterparty_refs == (COMPANION, FRIEND)
    assert projected.external_outcome == "not_established"
    assert len(projected.participant_statuses) == 1
    status = projected.participant_statuses[0]
    assert status.actor_ref == COUNTERPART
    assert status.status_code == "已发起协调"
    assert status.source_ref == source.as_source_ref()
    assert status.source_text_span == mutation.source_text_span
    assert status.updated_at == NOW
    assert history[0].operation == "declare"
    assert history[0].status_before is None
    assert history[0].status_after == "已发起协调"


def test_revise_appends_and_replaces_only_each_participants_own_free_status() -> None:
    _, _, current, history = _declare_group()
    subject_status = current[0].participant_statuses[0]

    accepted_source, accepted = _revise(
        current=current,
        actor=COMPANION,
        text="我先答应参加。",
        status_code="accepted",
        minute=1,
    )
    current, history = reduce_interaction_act(
        current,
        history,
        accepted,
        logical_time=NOW + timedelta(minutes=1),
        accepted_event_ref="event:interaction-act-transition-accepted:accepted",
    )
    revised_source, revised = _revise(
        current=current,
        actor=COMPANION,
        text="不过我现在改成待确认。",
        status_code="  待确认  ",
        minute=2,
    )
    current, history = reduce_interaction_act(
        current,
        history,
        revised,
        logical_time=NOW + timedelta(minutes=2),
        accepted_event_ref="event:interaction-act-transition-accepted:revised",
    )
    friend_source, friend_revision = _revise(
        current=current,
        actor=FRIEND,
        text="我还要看看时间。",
        status_code="看看时间再说",
        minute=3,
    )
    updated, updated_history = reduce_interaction_act(
        current,
        history,
        friend_revision,
        logical_time=NOW + timedelta(minutes=3),
        accepted_event_ref="event:interaction-act-transition-accepted:friend",
    )

    statuses = {item.actor_ref: item for item in updated[0].participant_statuses}
    assert statuses[COUNTERPART] == subject_status
    assert statuses[COMPANION].status_code == "待确认"
    assert statuses[COMPANION].source_ref == revised_source.as_source_ref()
    assert statuses[FRIEND].source_ref == friend_source.as_source_ref()
    assert accepted_source.as_source_ref() in updated[0].source_refs
    assert updated[0].external_outcome == "not_established"
    assert updated_history[-3].status_before is None
    assert updated_history[-3].status_after == "accepted"
    assert updated_history[-2].status_before == "accepted"
    assert updated_history[-2].status_after == "待确认"
    assert updated_history[-1].status_before is None

    context = interaction_act_context_items(
        updated,
        history=updated_history,
        conversation_ref=CONVERSATION,
        participant_ref=FRIEND,
        limit=4,
    )
    assert context[0].participant_statuses == updated[0].participant_statuses
    assert context[0].source_text_span == "我还要看看时间。"
    forged_history = updated_history[:-1] + (
        updated_history[-1].model_copy(update={"status_before": "伪造旧状态"}),
    )
    with pytest.raises(ValueError, match="transition chain is not exact"):
        interaction_act_context_items(
            updated,
            history=forged_history,
            conversation_ref=CONVERSATION,
            participant_ref=FRIEND,
        )


def test_optional_report_only_object_never_establishes_external_outcome() -> None:
    source = _observed(
        actor=COUNTERPART,
        text="这本旧书我已经装箱了，之后继续跟进。",
        suffix="declare-book",
    )
    mutation = materialize_interaction_act_mutation(
        authored=_output(
            operation="declare",
            status_code="已说明",
            source_text_span="之后继续跟进。",
            object_label="这本旧书",
            counterparty_refs=(COMPANION,),
        ),
        source=source,
        current=(),
        logical_time=NOW,
    )
    current, _ = reduce_interaction_act(
        (),
        (),
        mutation,
        logical_time=NOW,
        accepted_event_ref="event:interaction-act-transition-accepted:book",
    )
    descriptor = current[0].object_descriptor
    assert descriptor is not None
    assert descriptor.object_label == "这本旧书"
    assert descriptor.epistemic_scope == "report_only"

    forged = mutation.model_copy(
        update={
            "act_after": mutation.act_after.model_copy(
                update={"external_outcome": "completed"}
            )
        }
    )
    with pytest.raises(ValueError, match="cannot establish an external outcome"):
        reduce_interaction_act(
            (),
            (),
            forged,
            logical_time=NOW,
            accepted_event_ref="event:interaction-act-transition-accepted:forged",
        )


@pytest.mark.parametrize(
    ("source_text", "span", "object_label", "error"),
    (
        ("好呀。好呀。", "好呀。", None, "source text span is not exact-once"),
        ("这本书和那本书都可以。", "都可以。", "书", "object label is not exact-once"),
        ("这本书可以。", "可以。", "一辆车", "object label is not bound"),
    ),
)
def test_declare_requires_exact_source_spans(
    source_text: str,
    span: str,
    object_label: str | None,
    error: str,
) -> None:
    source = _observed(
        actor=COUNTERPART,
        text=source_text,
        suffix="invalid-span",
    )
    with pytest.raises(ValueError, match=error):
        materialize_interaction_act_mutation(
            authored=_output(
                operation="declare",
                status_code="已表达",
                source_text_span=span,
                object_label=object_label,
                counterparty_refs=(COMPANION,),
            ),
            source=source,
            current=(),
            logical_time=NOW,
        )


def test_role_output_rejects_invisible_span_and_bounds_open_text() -> None:
    with pytest.raises(ValueError, match="source text span must contain visible text"):
        _output(
            operation="declare",
            status_code="已表达",
            source_text_span=" ",
        )
    with pytest.raises(ValueError, match="at most 128 characters"):
        _output(
            operation="declare",
            status_code="x" * 129,
            source_text_span="有效来源",
        )


def test_revise_requires_exact_immutable_frame_and_a_bound_participant() -> None:
    _, _, current, history = _declare_group()
    act = current[0]
    outsider_source = _observed(
        actor=OUTSIDER,
        text="我也改一下。",
        suffix="outsider",
    )
    with pytest.raises(ValueError, match="requires a bound participant"):
        materialize_interaction_act_mutation(
            authored=_output(
                operation="revise",
                status_code="插入状态",
                source_text_span=outsider_source.source_text,
                interaction_act_ref=act.interaction_act_id,
                act_kind=act.act_kind,
                subject_ref=act.subject_ref,
                counterparty_refs=act.counterparty_refs,
            ),
            source=outsider_source,
            current=current,
            logical_time=NOW + timedelta(minutes=1),
        )
    participant_source = _observed(
        actor=COMPANION,
        text="我来更新。",
        suffix="participant",
    )
    with pytest.raises(ValueError, match="changed semantic coordinates"):
        materialize_interaction_act_mutation(
            authored=_output(
                operation="revise",
                status_code="更新",
                source_text_span=participant_source.source_text,
                interaction_act_ref=act.interaction_act_id,
                act_kind="偷偷换了动作类型",
                subject_ref=act.subject_ref,
                counterparty_refs=act.counterparty_refs,
            ),
            source=participant_source,
            current=current,
            logical_time=NOW + timedelta(minutes=1),
        )

    _, valid = _revise(
        current=current,
        actor=COMPANION,
        text="我来更新。",
        status_code="更新",
        minute=1,
    )
    forged_subject = act.participant_statuses[0].model_copy(
        update={"status_code": "伪造主体状态"}
    )
    forged_after = valid.act_after.model_copy(
        update={
            "participant_statuses": (
                forged_subject,
                *valid.act_after.participant_statuses[1:],
            )
        }
    )
    forged = valid.model_copy(update={"act_after": forged_after})
    with pytest.raises(ValueError, match="only change its source actor"):
        reduce_interaction_act(
            current,
            history,
            forged,
            logical_time=NOW + timedelta(minutes=1),
            accepted_event_ref="event:interaction-act-transition-accepted:forged-status",
        )


def test_declare_source_actor_must_be_subject() -> None:
    source = _observed(
        actor=COMPANION,
        text="我声明一件事。",
        suffix="wrong-subject",
    )
    with pytest.raises(ValueError, match="subject does not match source actor"):
        materialize_interaction_act_mutation(
            authored=_output(
                operation="declare",
                status_code="已声明",
                source_text_span=source.source_text,
                subject_ref=COUNTERPART,
                counterparty_refs=(COMPANION,),
            ),
            source=source,
            current=(),
            logical_time=NOW,
        )


def test_revision_cas_and_effect_once_remain_mechanical() -> None:
    _, _, current, history = _declare_group()
    _, first = _revise(
        current=current,
        actor=COMPANION,
        text="第一次更新。",
        status_code="第一次",
        minute=1,
    )
    _, concurrent = _revise(
        current=current,
        actor=FRIEND,
        text="并发更新。",
        status_code="并发",
        minute=1,
    )
    updated, updated_history = reduce_interaction_act(
        current,
        history,
        first,
        logical_time=NOW + timedelta(minutes=1),
        accepted_event_ref="event:interaction-act-transition-accepted:first",
    )
    with pytest.raises(ValueError, match="compare-and-swap failed"):
        reduce_interaction_act(
            updated,
            updated_history,
            concurrent,
            logical_time=NOW + timedelta(minutes=1),
            accepted_event_ref="event:interaction-act-transition-accepted:concurrent",
        )
    with pytest.raises(ValueError, match="transition identity already exists"):
        reduce_interaction_act(
            updated,
            updated_history,
            first,
            logical_time=NOW + timedelta(minutes=1),
            accepted_event_ref="event:interaction-act-transition-accepted:first",
        )


def test_participant_and_conversation_shapes_are_generic_but_bounded() -> None:
    eight = tuple(f"actor:counterparty:{index}" for index in range(8))
    authored = _output(
        operation="declare",
        status_code="自由状态",
        source_text_span="有效来源",
        counterparty_refs=eight,
    )
    assert authored.counterparty_refs == eight
    with pytest.raises(ValueError, match="at most 8 items"):
        _output(
            operation="declare",
            status_code="自由状态",
            source_text_span="有效来源",
            counterparty_refs=(*eight, "actor:counterparty:8"),
        )
    with pytest.raises(ValueError, match="counterparties must be unique"):
        _output(
            operation="declare",
            status_code="自由状态",
            source_text_span="有效来源",
            counterparty_refs=(COMPANION, COMPANION),
        )
    first = interaction_act_conversation_ref(
        world_id=WORLD,
        channel="qq",
        participant_refs=(COUNTERPART, COMPANION, FRIEND),
    )
    second = interaction_act_conversation_ref(
        world_id=WORLD,
        channel="qq",
        participant_refs=(FRIEND, COUNTERPART, COMPANION),
    )
    assert first == second
    with pytest.raises(ValueError, match="coordinates are incomplete"):
        interaction_act_conversation_ref(
            world_id=WORLD,
            channel="qq",
            participant_refs=(COUNTERPART, *eight, OUTSIDER),
        )


def test_delivered_source_status_retains_exact_receipt_without_claiming_fulfillment() -> None:
    _, _, current, history = _declare_group()
    act = current[0]
    source = DeliveredExpressionInteractionActSource(
        world_id=WORLD,
        conversation_ref=CONVERSATION,
        source_event_ref="event:expression:status",
        source_world_revision=8,
        source_payload_hash="8" * 64,
        source_actor_ref=COMPANION,
        source_text="我已经看到，先记为收到。",
        expression_plan_id="expression-plan:status",
        expression_plan_event_ref="event:plan:status",
        expression_plan_event_payload_hash="1" * 64,
        expression_beat_id="expression-beat:status",
        expression_beat_event_ref="event:beat:status",
        expression_beat_event_payload_hash="2" * 64,
        stored_payload_event_ref="event:expression:status",
        stored_payload_event_payload_hash="8" * 64,
        action_id="action:status",
        action_payload_hash="sha256:" + "3" * 64,
        action_target_ref=COUNTERPART,
        action_event_ref="event:action:status",
        action_event_payload_hash="4" * 64,
        receipt_id="receipt:status",
        receipt_event_ref="event:receipt:status",
        receipt_world_revision=9,
        receipt_payload_hash="5" * 64,
        receipt_status="delivered",
    )
    mutation = materialize_interaction_act_mutation(
        authored=_output(
            operation="revise",
            status_code="收到但尚未履行",
            source_text_span=source.source_text,
            interaction_act_ref=act.interaction_act_id,
            act_kind=act.act_kind,
            subject_ref=act.subject_ref,
            counterparty_refs=act.counterparty_refs,
        ),
        source=source,
        current=current,
        logical_time=NOW + timedelta(minutes=1),
    )
    updated, _ = reduce_interaction_act(
        current,
        history,
        mutation,
        logical_time=NOW + timedelta(minutes=1),
        accepted_event_ref="event:interaction-act-transition-accepted:delivered",
    )
    companion_status = next(
        item
        for item in updated[0].participant_statuses
        if item.actor_ref == COMPANION
    )
    assert companion_status.source_ref.delivery_proof is not None
    assert companion_status.source_ref.delivery_proof.receipt_status == "delivered"
    assert updated[0].external_outcome == "not_established"
