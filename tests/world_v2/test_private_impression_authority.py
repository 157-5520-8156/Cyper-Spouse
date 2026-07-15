from __future__ import annotations

import json

import pytest

from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.private_impression_events import private_impression_mutation_hash
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
    payload = _private_payload(ledger)
    commit(ledger, [_proposal_event(payload)])
    commit(
        ledger,
        [
            _acceptance_event(payload),
            event("private-impression-accepted", "PrivateImpressionAccepted", payload),
        ],
    )

    projection = ledger.project()
    assert projection.private_impressions[0].impression_id == "impression:response-frustration"
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
    assert capsule.private_impressions.items[0].item_ref == "impression:response-frustration"
    assert capsule.private_impressions.items[0].privacy_class == "withhold"


def test_private_impression_cannot_persist_free_text_or_bypass_acceptance() -> None:
    ledger = _ledger_with_active_appraisal()
    payload = _private_payload(ledger)
    payload["impression"] = dict(payload["impression"])
    payload["impression"]["interpretation_refs"] = ["the user is difficult"]
    payload["accepted_change_hash"] = private_impression_mutation_hash(payload)

    with pytest.raises(ValueError, match="interpretations must be appraisal references"):
        commit(ledger, [_proposal_event(payload)])

    valid = _private_payload(ledger)
    with pytest.raises(ValueError, match="AcceptanceRecorded"):
        commit(
            ledger,
            [event("private-impression-without-proposal", "PrivateImpressionAccepted", valid)],
        )
