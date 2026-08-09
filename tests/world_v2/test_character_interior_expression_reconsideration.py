from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.character_interior.contracts import InnerDecision
from companion_daemon.world_v2.character_interior.expression_reconsideration import (
    CharacterInteriorExpressionReconsiderationReviewer,
)
from companion_daemon.world_v2.character_interior.run_result import (
    CausalOpportunityIdentity,
)
from companion_daemon.world_v2.private_turn_state import PrivateTurnState
from companion_daemon.world_v2.proposal_envelope import MinimalProposal
from companion_daemon.world_v2.schemas import (
    ClaimLease,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
)


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
CURSOR = ProjectionCursor(world_revision=5, deliberation_revision=4, ledger_sequence=12)
SNAPSHOT_HASH = "a" * 64
SNAPSHOT_ID = f"inner-life-snapshot:sha256:{SNAPSHOT_HASH}"
OLD_PRIVATE_STATE = PrivateTurnState(
    inner_state_summary="刚才本来想把自己的近况说完，但新消息改变了当下感受。",
    attended_source_refs=("event:beat:old",),
)
OLD_PROPOSAL = MinimalProposal(
    proposal_id="proposal:old",
    trigger_ref="event:observation:old",
    evaluated_world_revision=4,
    confidence=7_000,
    brief_rationale="保留上一个角色内心轮次的审计痕迹。",
    private_turn_state=OLD_PRIVATE_STATE,
    source_model_result="model-result:old",
    response_text="旧的半句话",
    stance="defer",
)


def _observation() -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:observation:interjection:1",
        world_id="world:test",
        event_type="ObservationRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="user:test",
        source="test",
        trace_id="trace:test",
        causation_id="platform:test",
        correlation_id="conversation:test",
        idempotency_key="observation:test:1",
        payload={"observation_id": "observation:test:1", "text": "等等"},
    )


def _process() -> TriggerProcess:
    return TriggerProcess(
        trigger_id="trigger:reconsideration:1",
        trigger_ref="expression-reconsideration:"
        + json.dumps(
            {
                "plan_id": "plan:old",
                "beat_id": "beat:old",
                "observation_id": "observation:test:1",
            },
            separators=(",", ":"),
        ),
        process_kind="expression_reconsideration",
        source_evidence_ref="event:observation:interjection:1",
        state="claimed",
        claim_lease=ClaimLease(
            owner_id="worker:test",
            attempt_id="attempt:reconsideration:1",
            acquired_at=NOW,
            expires_at=NOW + timedelta(minutes=2),
        ),
        attempt_ids=("attempt:reconsideration:1",),
    )


class _Ledger:
    blocks_event_loop = False

    def __init__(self, *, replacement: bool = False) -> None:
        old_audit = SimpleNamespace(
            trigger_ref=OLD_PROPOSAL.trigger_ref,
            proposal_id=OLD_PROPOSAL.proposal_id,
            event_ref="event:proposal:old",
            proposal_json=OLD_PROPOSAL.model_dump_json(),
        )
        replacement_audit = SimpleNamespace(
            trigger_ref="event:observation:interjection:1",
            proposal_id="proposal:new",
            event_ref="event:proposal:new",
        )
        self.projection = SimpleNamespace(
            logical_time=NOW,
            expression_beats=(
                SimpleNamespace(
                    plan_id="plan:old",
                    beat_id="beat:old",
                    proposal_id=OLD_PROPOSAL.proposal_id,
                    event_ref="event:beat:old",
                    payload_ref="payload:old",
                ),
            ),
            stored_message_payloads=(
                SimpleNamespace(payload_ref="payload:old", text="旧的半句话"),
            ),
            proposal_audits=(old_audit, replacement_audit) if replacement else (old_audit,),
            expression_plan_manifests=(SimpleNamespace(proposal_id="proposal:new"),)
            if replacement
            else (),
        )

    def project_at(self, cursor):  # type: ignore[no-untyped-def]
        assert cursor == CURSOR
        return self.projection


class _Interior:
    def __init__(self, disposition: str | None = "continue") -> None:
        self.disposition = disposition
        self.opportunities = []

    async def consider(self, opportunity):  # type: ignore[no-untyped-def]
        self.opportunities.append(opportunity)
        if self.disposition is None:
            return InnerDecision(
                inner_turn_id="character-inner-turn:test",
                opportunity_ref=opportunity.opportunity_ref,
                actor_ref=opportunity.actor_ref,
                cursor=opportunity.cursor,
                status="technical_failure",
                failure_code="provider_timeout",
            )
        manifest = opportunity.capability_manifest
        assert manifest is not None
        summary = "她重新看了这句和用户的打断。"
        author = {
            "model_id": "character-role:test",
            "model_version": "character-role:test.1",
            "model_call_id": "model-call:reconsideration-test",
            "request_hash": "sha256:" + "b" * 64,
            "response_hash": "sha256:" + "c" * 64,
            "attempt_ordinal": 0,
        }
        return InnerDecision(
            inner_turn_id="character-inner-turn:test",
            opportunity_ref=opportunity.opportunity_ref,
            actor_ref=opportunity.actor_ref,
            cursor=opportunity.cursor,
            snapshot_id=SNAPSHOT_ID,
            snapshot_hash=SNAPSHOT_HASH,
            status="decided",
            summary=summary,
            instant_private_self={"summary": summary},
            private_self_lineage={
                "relation": "single_pass",
                "initial_private_self": {"summary": summary},
                "initial_snapshot_id": SNAPSHOT_ID,
                "initial_snapshot_hash": SNAPSHOT_HASH,
                "initial_author_lineage": author,
                "final_private_self": {"summary": summary},
                "final_snapshot_id": SNAPSHOT_ID,
                "final_snapshot_hash": SNAPSHOT_HASH,
                "final_author_lineage": author,
            },
            author_lineage=author,
            decision={
                "contract": "character-interior-purpose-decision.1",
                "purpose": "expression_reconsideration",
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": ("character-interior-expression-reconsideration-decision.1"),
                    "disposition": self.disposition,
                },
            },
        )


@pytest.mark.asyncio
async def test_reconsideration_is_one_character_interior_opportunity() -> None:
    interior = _Interior("continue")
    reviewer = CharacterInteriorExpressionReconsiderationReviewer(
        character_interior=interior,  # type: ignore[arg-type]
        ledger=_Ledger(),
        actor_ref="character:test",
    )

    observation_event = _observation()
    decision = await reviewer.review(
        process=_process(), observation_event=observation_event, cursor=CURSOR
    )

    assert decision.disposition == "continue"
    assert decision.rationale_ref == "character-inner-turn:test"
    assert decision.character_interior_lineage is not None
    assert decision.character_interior_lineage.snapshot_hash == SNAPSHOT_HASH
    assert len(interior.opportunities) == 1
    opportunity = interior.opportunities[0]
    assert opportunity.purpose == "expression_reconsideration"
    assert decision.character_interior_lineage.opportunity_ref == CausalOpportunityIdentity(
        world_id="world:test",
        actor_ref="character:test",
        purpose="expression_reconsideration",
        source_refs=tuple(sorted(opportunity.source_refs)),
        epoch=observation_event.event_id,
    ).opportunity_ref
    assert decision.character_interior_lineage.causal_source_refs == tuple(
        sorted(opportunity.source_refs)
    )
    assert decision.character_interior_lineage.causal_actor_ref == "character:test"
    assert opportunity.source_refs == (
        "event:observation:interjection:1",
        "event:beat:old",
        "event:proposal:old",
    )
    assert opportunity.capability_manifest.payload["old_pending_expression"]["text"] == (
        "旧的半句话"
    )
    assert opportunity.capability_manifest.payload["old_private_turn_state"] == {
        "value": OLD_PRIVATE_STATE.model_dump(mode="json"),
        "proposal_id": OLD_PROPOSAL.proposal_id,
        "proposal_ref": "event:proposal:old",
        "authority": "turn_local_audit_only",
        "fact_authority": False,
    }


@pytest.mark.asyncio
async def test_replacement_needs_existing_accepted_plan_and_failure_is_not_continue() -> None:
    without_plan = CharacterInteriorExpressionReconsiderationReviewer(
        character_interior=_Interior("supersede"),  # type: ignore[arg-type]
        ledger=_Ledger(replacement=False),
        actor_ref="character:test",
    )
    cancelled = await without_plan.review(
        process=_process(), observation_event=_observation(), cursor=CURSOR
    )
    assert cancelled.disposition == "cancel"
    assert cancelled.replacement_plan_ref is None

    with_plan = CharacterInteriorExpressionReconsiderationReviewer(
        character_interior=_Interior("supersede"),  # type: ignore[arg-type]
        ledger=_Ledger(replacement=True),
        actor_ref="character:test",
    )
    replaced = await with_plan.review(
        process=_process(), observation_event=_observation(), cursor=CURSOR
    )
    assert replaced.disposition == "supersede"
    assert replaced.replacement_plan_ref == "event:proposal:new"

    technical = CharacterInteriorExpressionReconsiderationReviewer(
        character_interior=_Interior(None),  # type: ignore[arg-type]
        ledger=_Ledger(),
        actor_ref="character:test",
    )
    with pytest.raises(RuntimeError, match="provider_timeout"):
        await technical.review(process=_process(), observation_event=_observation(), cursor=CURSOR)
