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


def test_settled_npc_occurrence_is_not_companion_life_when_only_bound_by_plan() -> None:
    ledger = _settled_life_ledger()

    assert WorldLifeContextCompiler().compile(
        projection=ledger.project(), actor_ref="actor:companion"
    ) == ()


def test_plan_owned_npc_occurrence_does_not_enter_companion_context() -> None:
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
    assert capsule.world_life.source_refs == ()
    model_slice = json.loads(capsule.model_content_json)["slices"]["world_life"]
    assert model_slice["items"] == []


def test_npc_occurrence_does_not_leak_to_an_unrelated_actor() -> None:
    ledger = _settled_life_ledger()

    assert WorldLifeContextCompiler().compile(
        projection=ledger.project(), actor_ref="actor:unrelated"
    ) == ()
