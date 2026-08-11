from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.interaction_act_context_builder import (
    InteractionActContextBuilder,
    install_interaction_act_context,
)
from companion_daemon.world_v2.interaction_act_events import (
    InteractionActProposalRecordedPayload,
    build_interaction_act_accepted_payload,
    canonical_interaction_act_mutation_hash,
)
from companion_daemon.world_v2.interaction_act_reducers import reduce_interaction_act
from companion_daemon.world_v2.interaction_act_runtime import (
    InteractionActRoleOutput,
    ObservedInteractionActSource,
    materialize_interaction_act_mutation,
)
from companion_daemon.world_v2.schemas import (
    CommitResult,
    CommittedWorldEventRef,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)
WORLD = "world:interaction-act-context"
COMPANION = "actor:companion"
USER = "user:primary"
SOURCE_TEXT = "我有一本旧书，下次见面带给你。"


class _Ledger:
    blocks_event_loop = False

    def __init__(self, events: dict[str, tuple[WorldEvent, CommitResult]]) -> None:
        self._events = events

    def lookup_event_commit(self, event_ref: str):
        return self._events.get(event_ref)


def test_snapshot_preserves_frame_and_participant_status_marks_without_aggregation() -> None:
    value = {
        "frame": {
            "subject_ref": USER,
            "counterparty_refs": [COMPANION],
            "act_kind": "角色自由命名的互动动作",
            "object": None,
        },
        "participant_statuses": [
            {
                "actor_ref": USER,
                "status_code": "等待后续",
                "source_ref": {
                    "authority_kind": "observed_message",
                    "source_event_ref": "event:observation:status-mark",
                    "source_actor_ref": USER,
                },
                "source_text_span": "以后再接着说",
                "updated_at": NOW.isoformat(),
            }
        ],
        "external_outcome": "not_established",
    }
    snapshot = compile_inner_life_snapshot(
        {
            "world_id": WORLD,
            "actor_ref": COMPANION,
            "trigger_ref": "event:stimulus:status-mark",
            "world_revision": 8,
            "deliberation_revision": 3,
            "ledger_sequence": 12,
            "logical_time": NOW.isoformat(),
            "consumer_scope": "deliberation_internal",
            "viewer_privacy_ceiling": "private",
            "slices": {
                "interaction_acts": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": "interior:interaction-act:status-mark",
                            "privacy_class": "private",
                            "value": value,
                        }
                    ],
                }
            },
        }
    )

    material = snapshot.materials["interaction_acts"][0]
    assert material == {
        **value,
        "source_ref": "interior:interaction-act:status-mark",
    }
    assert "status" not in material


def _accepted_projection():
    mutation = materialize_interaction_act_mutation(
        authored=InteractionActRoleOutput(
            contract="interaction-act-role-output.2",
            source_text_span="下次见面带给你",
            operation="declare",
            status_code="等待后续",
            interaction_act_ref=None,
            act_kind="角色自定义的后续交接动作",
            subject_ref=USER,
            counterparty_refs=(COMPANION,),
            object_ref=None,
            object_label="一本旧书",
        ),
        source=ObservedInteractionActSource(
            world_id=WORLD,
            conversation_ref="conversation:interaction-act:context",
            source_event_ref="event:observation:interaction-act-context",
            source_world_revision=7,
            source_payload_hash="a" * 64,
            source_actor_ref=USER,
            source_text=SOURCE_TEXT,
        ),
        current=(),
        logical_time=NOW,
    )
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:context",
        proposal_hash="sha256:" + "b" * 64,
        change_id="change:interaction-act:context",
        accepted_change_hash="c" * 64,
        evaluated_world_revision=7,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text=SOURCE_TEXT,
    )
    proposal_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-act-proposal:context",
        world_id=WORLD,
        event_type="InteractionActProposalRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:interaction-act-proposal-compiler",
        source="test",
        trace_id="trace:interaction-act-context",
        causation_id="event:proposal-audit:context",
        correlation_id="message:context",
        idempotency_key="idempotency:interaction-act-proposal:context",
        payload=proposal.model_dump(mode="json"),
    )
    accepted = build_interaction_act_accepted_payload(
        acceptance_id="acceptance:interaction-act:context",
        source_proposal_event=proposal_event,
    )
    accepted_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=accepted.accepted_event_ref,
        world_id=WORLD,
        event_type="InteractionActTransitionAccepted",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:interaction-act",
        source="test",
        trace_id="trace:interaction-act-context",
        causation_id="event:acceptance:interaction-act-context",
        correlation_id="message:context",
        idempotency_key="idempotency:interaction-act-accepted:context",
        payload=accepted.model_dump(mode="json"),
    )
    current, history = reduce_interaction_act(
        (),
        (),
        mutation,
        logical_time=NOW,
        accepted_event_ref=accepted_event.event_id,
    )
    cursor = ProjectionCursor(
        world_revision=8,
        deliberation_revision=3,
        ledger_sequence=12,
    )
    committed = CommittedWorldEventRef(
        event_id=accepted_event.event_id,
        event_type=accepted_event.event_type,
        world_revision=8,
        payload_hash=accepted_event.payload_hash,
        logical_time=accepted_event.logical_time,
    )
    projection = SimpleNamespace(
        world_id=WORLD,
        interaction_acts=current,
        interaction_act_transitions=history,
        committed_world_event_refs=(committed,),
    )
    commit = CommitResult(
        world_revision=8,
        deliberation_revision=3,
        ledger_sequence=12,
        event_ids=(accepted_event.event_id,),
    )
    return projection, cursor, accepted_event, commit


def _revised_projection():
    projection, _, _, _ = _accepted_projection()
    current = projection.interaction_acts[0]
    revised_at = NOW + timedelta(minutes=1)
    mutation = materialize_interaction_act_mutation(
        authored=InteractionActRoleOutput(
            contract="interaction-act-role-output.2",
            source_text_span="等我想好再回你",
            operation="revise",
            status_code="暂时保留判断",
            interaction_act_ref=current.interaction_act_id,
            act_kind=current.act_kind,
            subject_ref=current.subject_ref,
            counterparty_refs=current.counterparty_refs,
            object_ref=current.object_descriptor.object_ref,
            object_label=None,
        ),
        source=ObservedInteractionActSource(
            world_id=WORLD,
            conversation_ref=current.conversation_ref,
            source_event_ref="event:observation:interaction-act-counterparty-status",
            source_world_revision=8,
            source_payload_hash="d" * 64,
            source_actor_ref=COMPANION,
            source_text="我先记着，等我想好再回你。",
        ),
        current=projection.interaction_acts,
        logical_time=revised_at,
    )
    proposal = InteractionActProposalRecordedPayload(
        contract="interaction-act-proposal.1",
        proposal_id="proposal:interaction-act:counterparty-status",
        proposal_hash="sha256:" + "e" * 64,
        change_id="change:interaction-act:counterparty-status",
        accepted_change_hash="f" * 64,
        evaluated_world_revision=8,
        mutation_payload_hash=canonical_interaction_act_mutation_hash(mutation),
        mutation=mutation,
        observed_source_text="我先记着，等我想好再回你。",
    )
    proposal_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:interaction-act-proposal:counterparty-status",
        world_id=WORLD,
        event_type="InteractionActProposalRecorded",
        logical_time=revised_at,
        created_at=revised_at,
        actor="worker:interaction-act-proposal-compiler",
        source="test",
        trace_id="trace:interaction-act-counterparty-status",
        causation_id="event:proposal-audit:counterparty-status",
        correlation_id="message:counterparty-status",
        idempotency_key="idempotency:interaction-act-proposal:counterparty-status",
        payload=proposal.model_dump(mode="json"),
    )
    accepted = build_interaction_act_accepted_payload(
        acceptance_id="acceptance:interaction-act:counterparty-status",
        source_proposal_event=proposal_event,
    )
    accepted_event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=accepted.accepted_event_ref,
        world_id=WORLD,
        event_type="InteractionActTransitionAccepted",
        logical_time=revised_at,
        created_at=revised_at,
        actor="worker:interaction-act",
        source="test",
        trace_id="trace:interaction-act-counterparty-status",
        causation_id="event:acceptance:interaction-act-counterparty-status",
        correlation_id="message:counterparty-status",
        idempotency_key="idempotency:interaction-act-accepted:counterparty-status",
        payload=accepted.model_dump(mode="json"),
    )
    current, history = reduce_interaction_act(
        projection.interaction_acts,
        projection.interaction_act_transitions,
        mutation,
        logical_time=revised_at,
        accepted_event_ref=accepted_event.event_id,
    )
    cursor = ProjectionCursor(
        world_revision=9,
        deliberation_revision=3,
        ledger_sequence=13,
    )
    committed = CommittedWorldEventRef(
        event_id=accepted_event.event_id,
        event_type=accepted_event.event_type,
        world_revision=9,
        payload_hash=accepted_event.payload_hash,
        logical_time=accepted_event.logical_time,
    )
    projection = SimpleNamespace(
        world_id=WORLD,
        interaction_acts=current,
        interaction_act_transitions=history,
        committed_world_event_refs=(committed,),
    )
    commit = CommitResult(
        world_revision=9,
        deliberation_revision=3,
        ledger_sequence=13,
        event_ids=(accepted_event.event_id,),
    )
    return projection, cursor, accepted_event, commit


@pytest.mark.asyncio
async def test_context_pins_latest_accepted_event_without_interpreting_act_kind() -> None:
    projection, cursor, accepted_event, commit = _accepted_projection()
    join = await InteractionActContextBuilder(
        ledger=_Ledger({accepted_event.event_id: (accepted_event, commit)})
    ).build(
        projection=projection,
        actor_ref=COMPANION,
        cursor=cursor,
    )

    assert len(join.items) == 1
    item = join.items[0]
    assert item["value"] == {
        "frame": {
            "subject_ref": USER,
            "counterparty_refs": [COMPANION],
            "act_kind": "角色自定义的后续交接动作",
            "object": {
                "object_ref": projection.interaction_acts[0].object_descriptor.object_ref,
                "object_label": "一本旧书",
                "epistemic_scope": "report_only",
            },
        },
        "participant_statuses": [
            {
                "actor_ref": USER,
                "status_code": "等待后续",
                "source_ref": {
                    "authority_kind": "observed_message",
                    "source_event_ref": "event:observation:interaction-act-context",
                    "source_actor_ref": USER,
                },
                "source_text_span": "下次见面带给你",
                "updated_at": NOW.isoformat(),
            }
        ],
        "external_outcome": "not_established",
    }
    assert "status" not in item["value"]
    envelope = join.source_envelopes[item["item_ref"]]
    assert envelope["source_bindings"] == [
        {
            "source_kind": "committed_event",
            "authority_type": "InteractionActTransitionAccepted",
            "ref": accepted_event.event_id,
            "source_world_revision": 8,
            "immutable_hash": accepted_event.payload_hash,
        }
    ]


@pytest.mark.asyncio
async def test_context_preserves_each_participant_status_without_globalizing_latest() -> None:
    projection, cursor, accepted_event, commit = _revised_projection()
    join = await InteractionActContextBuilder(
        ledger=_Ledger({accepted_event.event_id: (accepted_event, commit)})
    ).build(
        projection=projection,
        actor_ref=COMPANION,
        cursor=cursor,
    )

    value = join.items[0]["value"]
    assert value["participant_statuses"] == [
        {
            "actor_ref": USER,
            "status_code": "等待后续",
            "source_ref": {
                "authority_kind": "observed_message",
                "source_event_ref": "event:observation:interaction-act-context",
                "source_actor_ref": USER,
            },
            "source_text_span": "下次见面带给你",
            "updated_at": NOW.isoformat(),
        },
        {
            "actor_ref": COMPANION,
            "status_code": "暂时保留判断",
            "source_ref": {
                "authority_kind": "observed_message",
                "source_event_ref": (
                    "event:observation:interaction-act-counterparty-status"
                ),
                "source_actor_ref": COMPANION,
            },
            "source_text_span": "等我想好再回你",
            "updated_at": (NOW + timedelta(minutes=1)).isoformat(),
        },
    ]
    assert "status" not in value
    assert value["external_outcome"] == "not_established"
    snapshot = compile_inner_life_snapshot(
        install_interaction_act_context(
            {
                "world_id": WORLD,
                "actor_ref": COMPANION,
                "trigger_ref": "event:stimulus:counterparty-status",
                "world_revision": cursor.world_revision,
                "deliberation_revision": cursor.deliberation_revision,
                "ledger_sequence": cursor.ledger_sequence,
                "logical_time": (NOW + timedelta(minutes=1)).isoformat(),
                "consumer_scope": "deliberation_internal",
                "viewer_privacy_ceiling": "private",
                "slices": {},
            },
            join,
        ),
        source_envelopes=join.source_envelopes,
    )
    assert snapshot.materials["interaction_acts"][0] == {
        **value,
        "source_ref": join.items[0]["item_ref"],
    }
    inventory = next(
        item
        for item in snapshot.source_inventory
        if item.scope == "interaction_acts"
    )
    assert inventory.direct_source_refs == (
        accepted_event.event_id,
        "event:observation:interaction-act-context",
        "event:observation:interaction-act-counterparty-status",
    )


@pytest.mark.asyncio
async def test_context_fails_closed_when_latest_accepted_event_is_not_readable() -> None:
    projection, cursor, _, _ = _accepted_projection()

    with pytest.raises(ValueError, match="authority is not committed"):
        await InteractionActContextBuilder(ledger=_Ledger({})).build(
            projection=projection,
            actor_ref=COMPANION,
            cursor=cursor,
        )


@pytest.mark.asyncio
async def test_snapshot_exposes_exact_interaction_act_in_three_role_facets() -> None:
    projection, cursor, accepted_event, commit = _accepted_projection()
    join = await InteractionActContextBuilder(
        ledger=_Ledger({accepted_event.event_id: (accepted_event, commit)})
    ).build(
        projection=projection,
        actor_ref=COMPANION,
        cursor=cursor,
    )
    context = install_interaction_act_context(
        {
            "world_id": WORLD,
            "actor_ref": COMPANION,
            "trigger_ref": "event:stimulus:context",
            "world_revision": cursor.world_revision,
            "deliberation_revision": cursor.deliberation_revision,
            "ledger_sequence": cursor.ledger_sequence,
            "logical_time": NOW.isoformat(),
            "consumer_scope": "deliberation_internal",
            "viewer_privacy_ceiling": "private",
            "slices": {},
        },
        join,
    )
    snapshot = compile_inner_life_snapshot(
        context,
        source_envelopes=join.source_envelopes,
    )

    material = snapshot.materials["interaction_acts"][0]
    assert material == {**join.items[0]["value"], "source_ref": join.items[0]["item_ref"]}
    for facet_name in (
        "subjective_relationship",
        "autonomous_impulses",
        "expression_stance",
    ):
        facet = next(item for item in snapshot.facet_views if item.name == facet_name)
        assert "interaction_acts" in facet.content["material_keys"]
        assert facet.source_refs == (join.items[0]["item_ref"],)
    inventory = next(
        item
        for item in snapshot.source_inventory
        if item.scope == "interaction_acts"
    )
    assert inventory.authority_bindings[0].ref == accepted_event.event_id
    assert inventory.direct_source_refs == (
        accepted_event.event_id,
        "event:observation:interaction-act-context",
    )
