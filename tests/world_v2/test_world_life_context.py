from __future__ import annotations

import json

from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.world_life_context import WorldLifeContextCompiler
from test_life_projection import WORLD_ID, commit, seed_through_proposal, settlement_batch


def _settled_life_ledger() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    return ledger


def test_settled_npc_occurrence_is_visible_when_bound_to_a_companion_plan() -> None:
    ledger = _settled_life_ledger()

    items = WorldLifeContextCompiler().compile(
        projection=ledger.project(), actor_ref="actor:companion"
    )

    assert len(items) == 1
    item = items[0]
    assert item.occurrence_id == "occurrence-tea"
    assert item.participant_refs == ("npc:lin",)
    assert item.location_ref == "room:kitchen"
    assert item.source.authority_event_ref == "occurrence-settled"
    assert item.source.authority_world_revision == 9


def test_world_life_slice_reaches_the_next_deliberation_context_with_exact_settlement_source() -> None:
    ledger = _settled_life_ledger()
    projection = ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(actor_ref="actor:companion"),
    ).compile(
        query_from_projection(
            projection, actor_ref="actor:companion", trigger_ref="event:next-turn"
        )
    )

    assert capsule.world_life.availability == "available"
    assert capsule.world_life.source_refs == ("occurrence-settled",)
    model_slice = json.loads(capsule.model_content_json)["slices"]["world_life"]
    assert model_slice["items"][0]["value"]["occurrence_id"] == "occurrence-tea"
    assert model_slice["items"][0]["value"]["source"]["authority_payload_hash"]


def test_npc_occurrence_does_not_leak_to_an_unrelated_actor() -> None:
    ledger = _settled_life_ledger()

    assert WorldLifeContextCompiler().compile(
        projection=ledger.project(), actor_ref="actor:unrelated"
    ) == ()
