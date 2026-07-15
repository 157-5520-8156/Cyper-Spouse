from __future__ import annotations

from datetime import timedelta
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.affect_acceptance_runtime import (
    AffectAcceptanceError,
    AffectAcceptanceRuntime,
    affect_mutation_event_id,
)
from companion_daemon.world_v2.affect_events import affect_mutation_hash
from companion_daemon.world_v2.appraisal_acceptance_runtime import (
    AppraisalAcceptanceRuntime,
    appraisal_mutation_event_id,
)
from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalMeaningRef,
    ProjectionCursor,
    affect_decay_config_digest,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger

from test_appraisal_authority import (
    NOW,
    WORLD_ID,
    accepted_payload,
    event,
    message_payload,
    prepare_claimed_interaction,
    record_proposal,
)


def _cursor(runtime: AffectAcceptanceRuntime) -> ProjectionCursor:
    head = runtime.ledger.project()
    return ProjectionCursor(
        world_revision=head.world_revision,
        deliberation_revision=head.deliberation_revision,
        ledger_sequence=head.ledger_sequence,
    )


def _accept_ready_appraisal(
    *, ledger: WorldLedger | SQLiteWorldLedger, issuer: AcceptedLedgerBatchIssuer
) -> None:
    _, trigger, evidence = prepare_claimed_interaction(ledger)
    payload = accepted_payload(ledger, trigger, evidence)
    appraisal = payload["appraisal"]
    assert isinstance(appraisal, dict)
    origin = appraisal["origin"]
    assert isinstance(origin, dict)
    origin["accepted_event_ref"] = appraisal_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id=str(payload["proposal_id"]),
        transition_id=str(payload["transition_id"]),
        event_type="AppraisalAccepted",
    )
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    record_proposal(ledger, trigger, evidence, payload)
    appraisal_runtime = AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    appraisal_runtime.accept_runtime_owned(
        handle=appraisal_runtime.pin_proposal(
            cursor=ProjectionCursor(
                world_revision=ledger.project().world_revision,
                deliberation_revision=ledger.project().deliberation_revision,
                ledger_sequence=ledger.project().ledger_sequence,
            ),
            proposal_id=str(payload["proposal_id"]),
        ),
        actor="worker:interaction-appraisal",
        source="test:appraisal-acceptance",
    )


def _record_ready_affect_proposal(
    ledger: WorldLedger | SQLiteWorldLedger,
) -> dict[str, object]:
    appraisal = ledger.project().appraisals[0]
    meaning = AppraisalMeaningRef(
        appraisal_id=appraisal.appraisal_id,
        hypothesis_id=appraisal.hypotheses[0].hypothesis_id,
        source_cluster_ref=appraisal.source_cluster_ref,
        accepted_change_id=appraisal.origin.change_id,
        accepted_transition_id=appraisal.origin.transition_id,
    )
    evidence = appraisal.evidence_refs[0]
    proposal_id = "proposal:affect:interaction:1"
    change_id = "change:affect:interaction:1"
    transition_id = "transition:affect:interaction:1"
    mutation_event_id = affect_mutation_event_id(
        world_id=WORLD_ID,
        proposal_id=proposal_id,
        transition_id=transition_id,
        event_type="AffectEpisodeOpened",
    )
    profile = AffectDecayProfileProjection(
        half_life_seconds=3_600,
        floor_bp=300,
        delay_seconds=120,
        config_version="affect-decay.1",
        config_digest=affect_decay_config_digest(
            kind="exponential_half_life",
            half_life_seconds=3_600,
            floor_bp=300,
            delay_seconds=120,
            config_version="affect-decay.1",
        ),
    )
    episode = AffectEpisodeProjection(
        episode_id="affect:interaction:1",
        entity_revision=1,
        origin=AffectOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=("policy:affect-v1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref=mutation_event_id,
        ),
        components=(
            AffectComponentProjection(
                component_id="component:hurt:interaction:1",
                dimension="hurt",
                source_cluster_ref=appraisal.source_cluster_ref,
                appraisal_refs=(meaning,),
                intensity_bp=4_200,
                decay_anchor_intensity_bp=4_200,
                opened_at=NOW,
                decay_anchor_at=NOW,
                decay_not_before=NOW + timedelta(seconds=120),
                last_stimulus_at=NOW,
                last_updated_at=NOW,
                decay_profile=profile,
                residue_bp=500,
            ),
        ),
        evidence_refs=(evidence,),
        opened_at=NOW,
        updated_at=NOW,
        status="active",
    )
    payload: dict[str, object] = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "appraisal_refs": [meaning.model_dump(mode="json")],
        "policy_refs": ["policy:affect-v1"],
        "acceptance_id": "acceptance:affect:interaction:1",
        "proposal_id": proposal_id,
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "episode": episode.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = affect_mutation_hash(payload)
    proposal = {
        "proposal_id": proposal_id,
        "proposal_kind": "affect_transition",
        "transition_kind": "open",
        "change_id": change_id,
        "transition_id": transition_id,
        "evaluated_world_revision": payload["evaluated_world_revision"],
        "expected_entity_revision": 0,
        "proposed_change_hash": payload["accepted_change_hash"],
        "evidence_refs": payload["evidence_refs"],
        "appraisal_refs": payload["appraisal_refs"],
        "policy_refs": payload["policy_refs"],
        "proposed_mutation": {
            "event_type": "AffectEpisodeOpened",
            "payload_json": json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        },
    }
    head = ledger.project()
    from test_appraisal_authority import event

    ledger.commit(
        [event("event:affect-proposed", "ProposalRecorded", proposal)],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    return payload


def _runtime(kind=WorldLedger.in_memory, *, path=None):
    issuer = AcceptedLedgerBatchIssuer()
    ledger = (
        kind(path=path, world_id=WORLD_ID, accepted_batch_issuer=issuer)
        if path is not None
        else kind(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    )
    ledger.commit(
        [event("event:world-started", "WorldStarted", {})],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    _accept_ready_appraisal(ledger=ledger, issuer=issuer)
    runtime = AffectAcceptanceRuntime(ledger=ledger, batch_issuer=issuer)
    payload = _record_ready_affect_proposal(ledger)
    return runtime, payload


def test_affect_runtime_commits_a_closed_accepted_batch() -> None:
    runtime, payload = _runtime()

    result = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"])),
        actor="worker:affect",
        source="test:affect-acceptance",
    )

    projection = runtime.ledger.project()
    assert result.world_revision == projection.world_revision
    assert projection.affect_proposals == ()
    assert projection.affect_episodes[0].components[0].dimension == "hurt"
    acceptance, mutation = (
        runtime.ledger.lookup_event_commit(event_id)[0] for event_id in result.event_ids
    )
    manifest = acceptance.payload()
    assert manifest["manifest_version"] == "affect-acceptance.1"
    assert manifest["mutation_event_id"] == mutation.event_id
    assert manifest["mutation_payload_hash"] == mutation.payload_hash

    with pytest.raises(AffectAcceptanceError, match="proposal_not_persisted"):
        runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"]))


def test_affect_runtime_replays_from_sqlite(tmp_path) -> None:
    runtime, payload = _runtime(SQLiteWorldLedger, path=tmp_path / "affect.sqlite3")
    runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"])),
        actor="worker:affect",
        source="test:affect-acceptance",
    )
    expected = runtime.ledger.project()
    assert runtime.ledger.rebuild() == expected
    runtime.close()

    reopened = SQLiteWorldLedger(path=tmp_path / "affect.sqlite3", world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


def test_accepted_affect_is_source_bound_into_the_next_context_capsule() -> None:
    runtime, payload = _runtime()
    result = runtime.accept_runtime_owned(
        handle=runtime.pin_proposal(cursor=_cursor(runtime), proposal_id=str(payload["proposal_id"])),
        actor="worker:affect",
        source="test:affect-acceptance",
    )
    projection = runtime.ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=runtime.ledger,
        relevance_scope=ContextRelevanceScope(
            actor_ref="actor:companion", related_subject_refs=("interaction:user:1",)
        ),
    ).compile(
        query_from_projection(
            projection, actor_ref="actor:companion", trigger_ref="event:next-turn"
        )
    )

    assert capsule.affect_episodes.availability == "available"
    assert len(capsule.affect_episodes.items) == 1
    assert '"dimension":"hurt"' in capsule.affect_episodes.items[0].payload_json
    assert result.event_ids[1] in {
        binding.ref for binding in capsule.affect_episodes.items[0].source_bindings
    }


@pytest.mark.asyncio
async def test_world_runtime_consumes_an_affect_proposal_idempotently() -> None:
    acceptance, payload = _runtime()
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=acceptance.ledger,
        affect_acceptance=acceptance,
        affect_acceptance_actor="worker:affect",
    )

    first = await runtime.accept_affect_proposal(str(payload["proposal_id"]))
    second = await runtime.accept_affect_proposal(str(payload["proposal_id"]))

    projection = acceptance.ledger.project()
    assert first == second
    assert first.status == "observed_only"
    assert first.observation_ref is None
    assert projection.affect_proposals == ()
    assert len(projection.affect_episodes) == 1
    assert len(projection.acceptance_decisions) == 2


@pytest.mark.asyncio
async def test_world_runtime_records_a_rejected_affect_proposal_idempotently() -> None:
    acceptance, payload = _runtime()
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=acceptance.ledger)

    first = await runtime.reject_affect_proposal(str(payload["proposal_id"]))
    second = await runtime.reject_affect_proposal(str(payload["proposal_id"]))

    projection = acceptance.ledger.project()
    decision = next(
        item for item in projection.acceptance_decisions if item.proposal_id == payload["proposal_id"]
    )
    assert first == second
    assert first.status == "observed_only"
    assert first.terminal_errors == ("affect.proposal_rejected",)
    assert decision.status == "rejected"
    assert projection.affect_proposals == ()
    assert projection.affect_episodes == ()


@pytest.mark.asyncio
async def test_world_runtime_records_an_outdated_affect_proposal_as_stale() -> None:
    acceptance, payload = _runtime()
    head = acceptance.ledger.project()
    acceptance.ledger.commit(
        [event("event:later-observation", "ObservationRecorded", message_payload("message:later"))],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=acceptance.ledger)

    outcome = await runtime.reject_affect_proposal(str(payload["proposal_id"]))

    projection = acceptance.ledger.project()
    decision = next(
        item for item in projection.acceptance_decisions if item.proposal_id == payload["proposal_id"]
    )
    assert outcome.terminal_errors == ("affect.proposal_stale",)
    assert decision.status == "stale"
    assert projection.affect_proposals == ()
