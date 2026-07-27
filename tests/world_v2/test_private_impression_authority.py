from __future__ import annotations

import asyncio
import json

import pytest

from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.private_impression_events import private_impression_mutation_hash
from companion_daemon.world_v2.private_impression_producer import (
    PrivateImpressionDraftAdapter,
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
)
from companion_daemon.world_v2.schemas import (
    AppraisalMeaningRef,
    PrivateImpressionOrigin,
    PrivateImpressionProjection,
)

from test_appraisal_authority import (
    NOW,
    accepted_payload as appraisal_payload,
    authorized_batch as appraisal_authorized_batch,
    commit,
    event,
    prepare_claimed_interaction,
    record_proposal as record_appraisal_proposal,
)


def _private_payload(ledger) -> dict[str, object]:
    appraisal = ledger.project().appraisals[0]
    evidence = appraisal.evidence_refs[0]
    appraisal_ref = AppraisalMeaningRef(
        appraisal_id=appraisal.appraisal_id,
        hypothesis_id="meaning:disappointment",
        source_cluster_ref=appraisal.source_cluster_ref,
        accepted_change_id=appraisal.origin.change_id,
        accepted_transition_id=appraisal.origin.transition_id,
    )
    impression = PrivateImpressionProjection(
        impression_id="impression:response-frustration",
        entity_revision=1,
        subject_ref=appraisal.subject_ref,
        interpretation_refs=(
            f"appraisal:{appraisal_ref.appraisal_id}:{appraisal_ref.hypothesis_id}",
        ),
        source_refs=("message-event:1",),
        confidence_bp=6_500,
        first_seen=NOW,
        last_supported=NOW,
        expiry_condition="until_appraisal_contradicted",
        status="active",
        origin=PrivateImpressionOrigin(
            change_id="change:private-impression:1",
            transition_id="transition:private-impression:1",
            policy_refs=("policy:private-impression.1",),
            accepted_event_ref="private-impression-accepted",
        ),
    )
    payload: dict[str, object] = {
        "change_id": "change:private-impression:1",
        "transition_id": "transition:private-impression:1",
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "appraisal_refs": [appraisal_ref.model_dump(mode="json")],
        "policy_refs": ["policy:private-impression.1"],
        "acceptance_id": "acceptance:private-impression:1",
        "proposal_id": "proposal:private-impression:1",
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "impression": impression.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = private_impression_mutation_hash(payload)
    return payload


def _proposal_event(payload: dict[str, object]):
    return event(
        "private-impression-proposed",
        "ProposalRecorded",
        {
            "proposal_id": payload["proposal_id"],
            "proposal_kind": "private_impression_transition",
            "proposal_encoding": "typed-authority-v1",
            "authority_contract_ref": "proposal-contract:private-impression.1",
            "transition_kind": "open",
            "change_id": payload["change_id"],
            "transition_id": payload["transition_id"],
            "evaluated_world_revision": payload["evaluated_world_revision"],
            "expected_entity_revision": payload["expected_entity_revision"],
            "proposed_change_hash": payload["accepted_change_hash"],
            "evidence_refs": payload["evidence_refs"],
            "appraisal_refs": payload["appraisal_refs"],
            "policy_refs": payload["policy_refs"],
            "proposed_mutation": {
                "event_type": "PrivateImpressionAccepted",
                "payload_json": json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            },
        },
    )


def _acceptance_event(payload: dict[str, object]):
    return event(
        "private-impression-acceptance",
        "AcceptanceRecorded",
        {
            "status": "accepted",
            "acceptance_id": payload["acceptance_id"],
            "proposal_id": payload["proposal_id"],
            "evaluated_world_revision": payload["evaluated_world_revision"],
            "accepted_change_id": payload["change_id"],
            "accepted_change_hash": payload["accepted_change_hash"],
        },
    )


def _ledger_with_active_appraisal():
    ledger = WorldLedger.in_memory(world_id="world-v2-appraisal-authority")
    commit(ledger, [event("world-start", "WorldStarted", {})])
    ledger, trigger, evidence = prepare_claimed_interaction(ledger)
    payload = appraisal_payload(ledger, trigger, evidence)
    record_appraisal_proposal(ledger, trigger, evidence, payload)
    commit(ledger, appraisal_authorized_batch(trigger, payload))
    return ledger


def test_private_impression_is_appraisal_bound_and_visible_only_to_internal_context() -> None:
    ledger = _ledger_with_active_appraisal()

    class Model:
        model = "test-role-reflection"

        async def complete(self, messages, *, temperature=0.1):  # type: ignore[no-untyped-def]
            del messages, temperature
            return json.dumps(
                {
                    "retain": True,
                    "source_refs": ["appraisal:appraisal:interaction:1:meaning:disappointment"],
                    "reflection_summary": "我暂时觉得这更像是失望，但仍可能有别的解释。",
                    "confidence": 6500,
                    "expiry_condition": "until_appraisal_contradicted",
                },
                ensure_ascii=False,
            )

    async def produce() -> None:
        await PrivateImpressionTriggerOpener(
            ledger=ledger,
            owner_id="worker:test:private-impression",
        ).open_once()
        result = await PrivateImpressionTriggerRuntime(
            ledger=ledger,
            adapter=PrivateImpressionDraftAdapter(model=Model()),
            owner_id="worker:test:private-impression",
        ).drain_one()
        assert result.work_status == "accepted"

    asyncio.run(produce())

    projection = ledger.project()
    assert projection.private_impressions[0].reflection_summary is not None
    assert projection.private_impression_proposals == ()

    compiler = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(
            actor_ref="actor:companion", related_subject_refs=("interaction:user:1",)
        ),
    )
    capsule = compiler.compile(
        query_from_projection(
            projection, actor_ref="actor:companion", trigger_ref="message-event:1"
        )
    )
    assert capsule.private_impressions.availability == "available"
    assert capsule.private_impressions.items[0].item_ref.startswith("impression:")
    assert capsule.private_impressions.items[0].privacy_class == "withhold"


def test_private_impression_cannot_replace_source_refs_or_bypass_acceptance() -> None:
    ledger = _ledger_with_active_appraisal()
    payload = _private_payload(ledger)
    payload["impression"] = dict(payload["impression"])
    payload["impression"]["interpretation_refs"] = ["the user is difficult"]
    payload["accepted_change_hash"] = private_impression_mutation_hash(payload)

    with pytest.raises(ValueError, match="interpretations must be appraisal references"):
        commit(ledger, [_proposal_event(payload)])

    valid = _private_payload(ledger)
    with pytest.raises(ValueError, match="new_write_requires_role_reflection"):
        commit(
            ledger,
            [event("private-impression-without-proposal", "PrivateImpressionAccepted", valid)],
        )
