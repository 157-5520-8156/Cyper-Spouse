"""Canonical CharacterInterior fixtures shared by World V2 boundary tests."""

from __future__ import annotations

from datetime import datetime
import hashlib

from companion_daemon.world_v2.character_interior.contracts import (
    InnerDecision,
    _InstantPrivateSelf,
    _InteriorAuthorLineage,
    _PrivateSelfLineage,
)
from companion_daemon.world_v2.character_interior.snapshot_compiler import (
    compile_inner_life_snapshot,
)
from companion_daemon.world_v2.schemas import ProjectionCursor


def canonical_inner_life_model_view(
    *,
    world_id: str,
    actor_ref: str,
    cursor: ProjectionCursor,
    logical_time: datetime,
    source_ref: str = "character-core:fixture:1",
) -> dict[str, object]:
    """Return the provider view of the real typed snapshot compiler."""

    return compile_inner_life_snapshot(
        {
            "world_id": world_id,
            "actor_ref": actor_ref,
            "world_revision": cursor.world_revision,
            "deliberation_revision": cursor.deliberation_revision,
            "ledger_sequence": cursor.ledger_sequence,
            "logical_time": logical_time.isoformat(),
            "consumer_scope": "deliberation_internal",
            "slices": {
                "character_core": {
                    "availability": "available",
                    "items": [
                        {
                            "item_ref": source_ref,
                            "value": {
                                "values": {
                                    "slow_evolving": {"self_description": "有自己的生活和判断"}
                                }
                            },
                        }
                    ],
                }
            },
        }
    ).model_view()


def canonical_inner_decision(
    opportunity,
    *,
    decision: dict[str, object] | None,
    summary: str = "她按自己的当下判断做了选择。",
    identity: str = "fixture",
    status: str = "decided",
) -> InnerDecision:
    """Return a complete successful CharacterInterior decision fixture."""

    seed = hashlib.sha256(identity.encode()).hexdigest()
    snapshot_hash = hashlib.sha256((identity + ":snapshot").encode()).hexdigest()
    author = _InteriorAuthorLineage(
        model_id="character-interior-fixture",
        model_version="fixture.1",
        model_call_id=f"model-call:character-interior:{seed[:32]}",
        request_hash="sha256:"
        + hashlib.sha256((identity + ":request").encode()).hexdigest(),
        response_hash="sha256:"
        + hashlib.sha256((identity + ":response").encode()).hexdigest(),
        attempt_ordinal=0,
    )
    private_self = _InstantPrivateSelf(
        summary=summary,
        attended_source_refs=opportunity.source_refs,
    )
    private_lineage = _PrivateSelfLineage(
        relation="single_pass",
        initial_private_self=private_self,
        initial_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        initial_snapshot_hash=snapshot_hash,
        initial_author_lineage=author,
        final_private_self=private_self,
        final_snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        final_snapshot_hash=snapshot_hash,
        final_author_lineage=author,
    )
    return InnerDecision(
        inner_turn_id=f"character-inner-turn:sha256:{seed}",
        opportunity_ref=opportunity.opportunity_ref,
        actor_ref=opportunity.actor_ref,
        cursor=opportunity.cursor,
        snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
        snapshot_hash=snapshot_hash,
        status=status,
        summary=summary,
        attended_source_refs=opportunity.source_refs,
        instant_private_self=private_self,
        private_self_lineage=private_lineage,
        decision=decision,
        author_lineage=author,
    )


__all__ = ["canonical_inner_decision", "canonical_inner_life_model_view"]
