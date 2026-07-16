from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.affect_events import (
    AffectBaselineAdjustedPayload,
    AffectEpisodeResolvedPayload,
    affect_mutation_hash,
)
from companion_daemon.world_v2.batch_invariants import (
    interaction_appraisal_trigger_identity,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.errors import IdempotencyConflict
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.runtime import WorldRuntime
from companion_daemon.world_v2.relationship_trigger import (
    relationship_deliberation_trigger_events,
    relationship_deliberation_trigger_id,
)
from companion_daemon.world_v2.relationship_trigger_runtime import RelationshipTriggerRuntime
from companion_daemon.world_v2.schemas import (
    AffectComponentProjection,
    AffectCalibrationEpisodeRef,
    AffectDecayProfileProjection,
    AffectEpisodeProjection,
    AffectOrigin,
    AppraisalHypothesis,
    AppraisalOrigin,
    AppraisalProjection,
    AppraisalMeaningRef,
    ClaimLease,
    ClockObservation,
    EvidenceRef,
    ProjectionCursor,
    TriggerProcess,
    WorldEvent,
    affect_decay_config_digest,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


WORLD_ID = "world-v2-appraisal-authority"
NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    at: datetime = NOW,
) -> WorldEvent:
    identity = domain_idempotency_key(event_type=event_type, world_id=WORLD_ID, payload=payload)
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=at,
        created_at=at,
        actor=str(payload.get("actor", "system:test")),
        source=str(payload.get("source", "test")),
        trace_id=str(payload.get("trace_id", "trace:appraisal-authority")),
        causation_id=str(payload.get("causation_id", f"cause:{event_id}")),
        correlation_id=str(payload.get("correlation_id", "correlation:appraisal-authority")),
        idempotency_key=identity or f"identity:{event_id}",
        payload=payload,
    )


Ledger = WorldLedger | SQLiteWorldLedger


def commit(ledger: Ledger, events: list[WorldEvent]) -> None:
    head = ledger.project()
    ledger.commit(
        events,
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )


def message_payload(observation_id: str) -> dict[str, object]:
    return {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD_ID,
        "logical_time": NOW.isoformat(),
        "created_at": NOW.isoformat(),
        "trace_id": "trace:message",
        "causation_id": f"cause:{observation_id}",
        "correlation_id": "correlation:message",
        "source": "test-platform",
        "source_event_id": f"source:{observation_id}",
        "actor": "user:test",
        "channel": "direct_message",
        "payload_ref": f"payload:{observation_id}",
        "payload_hash": "a" * 64,
        "received_at": NOW.isoformat(),
    }


def prepare_claimed_interaction(
    ledger: Ledger | None = None,
) -> tuple[Ledger, TriggerProcess, EvidenceRef]:
    ledger = ledger or WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [event("message-event:1", "ObservationRecorded", message_payload("message:1"))],
    )
    opened = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "message:1"),
        trigger_ref="interaction:message:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:1",
        state="open",
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-opened",
                "TriggerProcessOpened",
                {"process": opened.model_dump(mode="json")},
            )
        ],
    )
    claimed = opened.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:interaction-appraisal",
                attempt_id="attempt:interaction:1",
                acquired_at=NOW,
                expires_at=NOW + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:interaction:1",),
        }
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-claimed",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    message = ledger.project().message_observations[0]
    evidence = EvidenceRef(
        ref_id="message:1",
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        source_world_revision=message.world_revision,
        immutable_hash=message.event_payload_hash,
    )
    return ledger, claimed, evidence


def accepted_payload(
    ledger: Ledger, trigger: TriggerProcess, evidence: EvidenceRef
) -> dict[str, object]:
    appraisal = AppraisalProjection(
        appraisal_id="appraisal:interaction:1",
        entity_revision=1,
        subject_ref="interaction:user:1",
        source_cluster_ref="conversation:1",
        origin=AppraisalOrigin(
            change_id="change:interaction-appraisal:1",
            transition_id="transition:interaction-appraisal:1",
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref="interaction-appraisal-accepted",
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id="meaning:disappointment",
                meaning="disappointment",
                attribution="user",
                controllability="partly_controllable",
                severity="moderate",
                weight_bp=6_500,
            ),
            AppraisalHypothesis(
                hypothesis_id="meaning:misunderstanding",
                meaning="misunderstanding",
                attribution="unknown",
                controllability="controllable",
                severity="low",
                weight_bp=3_500,
            ),
        ),
        evidence_refs=(evidence,),
        confidence_bp=7_200,
        accepted_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    payload: dict[str, object] = {
        "change_id": "change:interaction-appraisal:1",
        "transition_id": "transition:interaction-appraisal:1",
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "policy_refs": ["policy:appraisal-v1"],
        "acceptance_id": "acceptance:interaction-appraisal:1",
        "proposal_id": "proposal:interaction-appraisal:1",
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "trigger_id": trigger.trigger_id,
        "appraisal": appraisal.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    return payload


def record_proposal(
    ledger: Ledger,
    trigger: TriggerProcess,
    evidence: EvidenceRef,
    payload: dict[str, object],
    additional_evidence: tuple[EvidenceRef, ...] = (),
) -> None:
    commit(ledger, [proposal_event(trigger, evidence, payload, additional_evidence)])


def proposal_event(
    trigger: TriggerProcess,
    evidence: EvidenceRef,
    payload: dict[str, object],
    additional_evidence: tuple[EvidenceRef, ...] = (),
) -> WorldEvent:
    return event(
        "interaction-appraisal-proposed",
        "ProposalRecorded",
        {
            "proposal_id": payload["proposal_id"],
            "proposal_kind": "appraisal_transition",
            "transition_kind": "accept",
            "change_id": payload["change_id"],
            "trigger_id": trigger.trigger_id,
            "trigger_ref": trigger.trigger_ref,
            "source_evidence_ref": trigger.source_evidence_ref,
            "evaluated_world_revision": payload["evaluated_world_revision"],
            "expected_entity_revision": payload["expected_entity_revision"],
            "proposed_change_hash": payload["accepted_change_hash"],
            "evidence_refs": [
                evidence.model_dump(mode="json"),
                *(item.model_dump(mode="json") for item in additional_evidence),
            ],
            "policy_refs": payload["policy_refs"],
            "proposed_mutation": {
                "event_type": "AppraisalAccepted",
                "payload_json": json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        },
    )


def authorized_batch(trigger: TriggerProcess, payload: dict[str, object]) -> list[WorldEvent]:
    return [
        event(
            "interaction-appraisal-acceptance",
            "AcceptanceRecorded",
            {
                "status": "accepted",
                "acceptance_id": payload["acceptance_id"],
                "proposal_id": payload["proposal_id"],
                "evaluated_world_revision": payload["evaluated_world_revision"],
                "accepted_change_id": payload["change_id"],
                "accepted_change_hash": payload["accepted_change_hash"],
            },
        ),
        event("interaction-appraisal-accepted", "AppraisalAccepted", payload),
        event(
            "interaction-appraisal-completed",
            "TriggerProcessCompleted",
            {
                "trigger_id": trigger.trigger_id,
                "owner_id": "worker:interaction-appraisal",
                "attempt_id": "attempt:interaction:1",
                "completed_at": NOW.isoformat(),
                "runtime_outcome_ref": "appraisal:appraisal:interaction:1",
            },
        ),
    ]


def test_observed_message_can_follow_the_shared_appraisal_authority_path() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)

    commit(ledger, authorized_batch(trigger, payload))

    assert ledger.project().appraisals[0].hypotheses[0].meaning == "disappointment"
    assert ledger.project().trigger_processes[0].state == "terminal"
    assert ledger.project().appraisal_proposals == ()
    assert ledger.project().appraisal_proposal_ids == ("proposal:interaction-appraisal:1",)


def test_sqlite_replays_the_complete_interaction_appraisal_authority_path(
    tmp_path,
) -> None:
    path = tmp_path / "appraisal-authority.sqlite3"
    ledger, trigger, evidence = prepare_claimed_interaction(
        SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    )
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))
    expected = ledger.project()
    assert ledger.rebuild() == expected
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.asyncio
async def test_accepted_appraisal_can_open_affect_through_an_independent_authority_path(
    tmp_path, persistent: bool
) -> None:
    path = tmp_path / "affect-authority.sqlite3"
    authority_ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID) if persistent else None
    ledger, trigger, evidence = prepare_claimed_interaction(authority_ledger)
    appraisal_payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, appraisal_payload)
    commit(ledger, authorized_batch(trigger, appraisal_payload))

    accepted = ledger.project().appraisals[0]
    meaning = AppraisalMeaningRef(
        appraisal_id=accepted.appraisal_id,
        hypothesis_id=accepted.hypotheses[0].hypothesis_id,
        source_cluster_ref=accepted.source_cluster_ref,
        accepted_change_id=accepted.origin.change_id,
        accepted_transition_id=accepted.origin.transition_id,
    )
    proposal_id = "proposal:affect:1"
    change_id = "change:affect:1"
    transition_id = "transition:affect:1"
    acceptance_id = "acceptance:affect:1"
    opened_event_id = "affect-opened"
    revision = ledger.project().world_revision
    episode = AffectEpisodeProjection(
        episode_id="affect:interaction:1",
        entity_revision=1,
        origin=AffectOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=("policy:affect-v1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref=opened_event_id,
        ),
        components=(
            AffectComponentProjection(
                component_id="component:hurt:interaction:1",
                dimension="hurt",
                source_cluster_ref=accepted.source_cluster_ref,
                appraisal_refs=(meaning,),
                intensity_bp=4_200,
                decay_anchor_intensity_bp=4_200,
                opened_at=NOW,
                decay_anchor_at=NOW,
                decay_not_before=NOW + timedelta(seconds=120),
                last_stimulus_at=NOW,
                last_updated_at=NOW,
                decay_profile=AffectDecayProfileProjection(
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
                ),
                residue_bp=500,
            ),
        ),
        evidence_refs=(evidence,),
        opened_at=NOW,
        updated_at=NOW,
        status="active",
    )
    affect_payload: dict[str, object] = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "appraisal_refs": [meaning.model_dump(mode="json")],
        "policy_refs": ["policy:affect-v1"],
        "acceptance_id": acceptance_id,
        "proposal_id": proposal_id,
        "evaluated_world_revision": revision,
        "accepted_change_hash": "0" * 64,
        "episode": episode.model_dump(mode="json"),
    }
    affect_payload["accepted_change_hash"] = affect_mutation_hash(affect_payload)
    proposed_json = json.dumps(
        affect_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    commit(
        ledger,
        [
            event(
                "affect-proposed",
                "ProposalRecorded",
                {
                    "proposal_id": proposal_id,
                    "proposal_kind": "affect_transition",
                    "transition_kind": "open",
                    "change_id": change_id,
                    "transition_id": transition_id,
                    "evaluated_world_revision": revision,
                    "expected_entity_revision": 0,
                    "proposed_change_hash": affect_payload["accepted_change_hash"],
                    "evidence_refs": [evidence.model_dump(mode="json")],
                    "appraisal_refs": [meaning.model_dump(mode="json")],
                    "policy_refs": ["policy:affect-v1"],
                    "proposed_mutation": {
                        "event_type": "AffectEpisodeOpened",
                        "payload_json": proposed_json,
                    },
                },
            )
        ],
    )
    assert ledger.project().affect_episodes == ()
    with pytest.raises(ValueError, match="adjacent revision-pinned AcceptanceRecorded"):
        commit(
            ledger,
            [event("affect-opened-without-acceptance", "AffectEpisodeOpened", affect_payload)],
        )
    commit(
        ledger,
        [
            event(
                "affect-accepted",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": acceptance_id,
                    "proposal_id": proposal_id,
                    "evaluated_world_revision": revision,
                    "accepted_change_id": change_id,
                    "accepted_change_hash": affect_payload["accepted_change_hash"],
                },
            ),
            event(opened_event_id, "AffectEpisodeOpened", affect_payload),
        ],
    )

    projection = ledger.project()
    assert projection.affect_episodes == (episode,)
    assert projection.affect_proposals == ()
    assert ledger.rebuild() == projection
    runtime = WorldRuntime(world_id=WORLD_ID, ledger=ledger)
    clock = ClockObservation(
        schema_version="world-v2.1",
        tick_id="affect-decay:1",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace:affect-decay",
        causation_id="scheduler:affect-decay",
        correlation_id="correlation:affect-decay",
        logical_time_from=NOW,
        logical_time_to=NOW + timedelta(hours=1),
        reason="scheduled_tick",
    )
    first = await runtime.advance(clock)
    duplicate = await runtime.advance(clock)
    assert duplicate == first
    decayed = ledger.project()
    assert decayed.affect_episodes[0].entity_revision == 2
    assert decayed.affect_episodes[0].components[0].intensity_bp < 4_200
    assert ledger.rebuild() == decayed
    projection = decayed
    if persistent:
        ledger.close()
        reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
        assert reopened.project() == projection
        assert reopened.rebuild() == projection
        assert await WorldRuntime(world_id=WORLD_ID, ledger=reopened).advance(clock) == first
        assert reopened.project() == projection
        reopened.close()


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.asyncio
async def test_accepted_appraisal_opens_a_replayable_relationship_trigger(
    tmp_path, persistent: bool
) -> None:
    path = tmp_path / "relationship-trigger.sqlite3"
    authority_ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID) if persistent else None
    ledger, trigger, evidence = prepare_claimed_interaction(authority_ledger)
    appraisal_payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, appraisal_payload)
    commit(ledger, authorized_batch(trigger, appraisal_payload))
    located = ledger.lookup_event_commit("interaction-appraisal-accepted")
    assert located is not None
    appraisal_event = located[0]
    trigger_events = relationship_deliberation_trigger_events(
        appraisal_event=appraisal_event, owner_id="worker:relationship"
    )
    commit(ledger, list(trigger_events))

    class _Worker:
        calls: list[str] = []

        async def process(self, *, world_id, cursor, appraisal_event):  # type: ignore[no-untyped-def]
            assert world_id == WORLD_ID
            assert appraisal_event.event_id == "interaction-appraisal-accepted"
            assert cursor == ProjectionCursor(
                world_revision=ledger.project().world_revision,
                deliberation_revision=ledger.project().deliberation_revision,
                ledger_sequence=ledger.project().ledger_sequence,
            )
            self.calls.append(appraisal_event.event_id)
            return SimpleNamespace(status="no_change")

    worker = _Worker()
    runtime = RelationshipTriggerRuntime(
        ledger=ledger, worker=worker, owner_id="worker:relationship"
    )
    result = await runtime.drain_one()
    projection = ledger.project()

    trigger_id = relationship_deliberation_trigger_id(
        world_id=WORLD_ID, appraisal_event_id="interaction-appraisal-accepted"
    )
    assert result.trigger_id == trigger_id
    assert result.status == "processed"
    assert result.work_status == "no_change"
    assert worker.calls == ["interaction-appraisal-accepted"]
    assert projection.trigger_processes[-1].state == "terminal"
    assert projection.trigger_processes[-1].runtime_outcome_ref.endswith(":no_change")
    assert ledger.rebuild() == projection
    assert (await runtime.drain_one()).status == "idle"
    if persistent:
        ledger.close()
        reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
        assert reopened.project() == projection
        assert reopened.rebuild() == projection
        reopened.close()


def test_appraisal_acceptance_cannot_be_synthesized_without_persisted_proposal() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)

    with pytest.raises(ValueError, match="unknown proposal"):
        commit(ledger, authorized_batch(trigger, payload))


def test_typed_accepted_decision_cannot_commit_without_its_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)

    with pytest.raises(ValueError, match="domain mutation immediately"):
        commit(ledger, [authorized_batch(trigger, payload)[0]])


def test_world_change_cannot_intervene_between_acceptance_and_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    batch = authorized_batch(trigger, payload)
    batch.insert(
        1,
        event(
            "intervening-observation",
            "ObservationRecorded",
            {"observation_id": "observation:intervening"},
        ),
    )

    with pytest.raises(ValueError, match="domain mutation immediately"):
        commit(ledger, batch)


def test_rejected_decision_matches_and_terminalizes_its_proposal() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    rejection = event(
        "interaction-appraisal-rejected",
        "AcceptanceRecorded",
        {
            "status": "rejected",
            "acceptance_id": "acceptance:interaction-appraisal:rejected",
            "proposal_id": payload["proposal_id"],
            "evaluated_world_revision": payload["evaluated_world_revision"],
        },
    )
    commit(ledger, [rejection])

    assert ledger.project().appraisal_proposals == ()
    assert ledger.project().acceptance_decisions[0].status == "rejected"


def test_appraisal_proposal_rejects_nonexistent_evidence() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    missing = evidence.model_copy(update={"ref_id": "message:missing"})
    missing_dump = missing.model_dump(mode="json")
    payload["evidence_refs"] = [*payload["evidence_refs"], missing_dump]
    payload["appraisal"]["evidence_refs"] = [
        *payload["appraisal"]["evidence_refs"],
        missing_dump,
    ]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="observed-message evidence"):
        record_proposal(
            ledger,
            trigger,
            evidence,
            payload,
            additional_evidence=(missing,),
        )


def test_interaction_trigger_cannot_open_for_an_unobserved_message() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    invalid = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "message:missing"),
        trigger_ref="interaction:message:missing",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:missing",
        state="open",
    )

    with pytest.raises(ValueError, match="observed message"):
        commit(
            ledger,
            [
                event(
                    "invalid-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": invalid.model_dump(mode="json")},
                )
            ],
        )


def test_proposal_and_acceptance_require_separate_atomic_commits() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)

    with pytest.raises(ValueError, match="separate deliberation commit"):
        commit(
            ledger,
            [proposal_event(trigger, evidence, payload), *authorized_batch(trigger, payload)],
        )


def test_proposal_reducer_rejects_a_forged_stale_caller_revision() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    payload["evaluated_world_revision"] = 0
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    proposed = proposal_event(trigger, evidence, payload)
    head = ledger.project()

    with pytest.raises(ValueError, match="current world revision"):
        ledger.commit(
            [proposed],
            expected_world_revision=0,
            expected_deliberation_revision=head.deliberation_revision,
        )


def test_acceptance_must_precede_the_authorized_mutation() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    batch = authorized_batch(trigger, payload)

    with pytest.raises(ValueError, match="AcceptanceRecorded"):
        commit(ledger, [batch[1], batch[0], batch[2]])


def test_interaction_trigger_identity_is_deterministic() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [event("message-event:1", "ObservationRecorded", message_payload("message:1"))],
    )
    invalid = TriggerProcess(
        trigger_id="appraisal:interaction:arbitrary",
        trigger_ref="interaction:message:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:1",
        state="open",
    )

    with pytest.raises(ValueError, match="identity is not deterministic"):
        commit(
            ledger,
            [
                event(
                    "nondeterministic-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": invalid.model_dump(mode="json")},
                )
            ],
        )


def test_observed_message_rejects_forged_provenance_metadata() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    forged = evidence.model_copy(update={"immutable_hash": "a" * 64})
    forged_dump = forged.model_dump(mode="json")
    payload["evidence_refs"] = [forged_dump]
    payload["appraisal"]["evidence_refs"] = [forged_dump]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="provenance"):
        record_proposal(ledger, trigger, forged, payload)


def test_acceptance_identity_rejects_a_conflicting_second_decision() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))

    with pytest.raises(IdempotencyConflict):
        commit(
            ledger,
            [
                event(
                    "conflicting-interaction-appraisal-acceptance",
                    "AcceptanceRecorded",
                    {
                        "status": "stale",
                        "acceptance_id": payload["acceptance_id"],
                        "proposal_id": payload["proposal_id"],
                        "evaluated_world_revision": payload["evaluated_world_revision"],
                    },
                )
            ],
        )


def test_uninstalled_appraisal_policy_cannot_authorize_a_transition() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    payload["policy_refs"] = ["policy:does-not-exist"]
    payload["appraisal"]["origin"]["policy_refs"] = ["policy:does-not-exist"]
    payload["accepted_change_hash"] = "0" * 64
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)

    with pytest.raises(ValueError, match="uninstalled policy"):
        record_proposal(ledger, trigger, evidence, payload)


def test_consumed_proposal_identity_cannot_be_reused() -> None:
    ledger, trigger, evidence = prepare_claimed_interaction()
    payload = accepted_payload(ledger, trigger, evidence)
    record_proposal(ledger, trigger, evidence, payload)
    commit(ledger, authorized_batch(trigger, payload))
    reused = proposal_event(trigger, evidence, payload).payload()
    reused["change_id"] = "change:reused-proposal-id"
    reused["evaluated_world_revision"] = ledger.project().world_revision

    with pytest.raises(ValueError, match="identity is already registered"):
        commit(
            ledger,
            [event("reused-appraisal-proposal", "ProposalRecorded", reused)],
        )


def test_generic_observation_cannot_masquerade_as_a_message() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [
            event(
                "tool-shaped-observation",
                "ObservationRecorded",
                {"observation_id": "tool-result:1"},
            )
        ],
    )
    trigger = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "tool-result:1"),
        trigger_ref="interaction:tool-result:1",
        process_kind="interaction_appraisal",
        source_evidence_ref="tool-result:1",
        state="open",
    )

    with pytest.raises(ValueError, match="observed message"):
        commit(
            ledger,
            [
                event(
                    "tool-result-interaction-trigger",
                    "TriggerProcessOpened",
                    {"process": trigger.model_dump(mode="json")},
                )
            ],
        )


def test_partial_observation_shape_cannot_become_message_authority() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    partial = message_payload("partial:1")
    for field in (
        "schema_version",
        "logical_time",
        "created_at",
        "trace_id",
        "causation_id",
        "correlation_id",
        "received_at",
    ):
        partial.pop(field)
    with pytest.raises(ValueError, match="Field required"):
        commit(
            ledger,
            [
                event(
                    "partial-observation",
                    "ObservationRecorded",
                    partial,
                )
            ],
        )
    commit(
        ledger,
        [
            event(
                "corrected-partial-observation",
                "ObservationRecorded",
                message_payload("partial:1"),
            )
        ],
    )
    assert ledger.project().message_observations[0].observation_id == "partial:1"


def test_observation_id_cannot_alias_different_message_bytes() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    first = message_payload("message:alias")
    second = {
        **first,
        "source_event_id": "source:message:alias:second",
        "payload_hash": "d" * 64,
    }
    commit(ledger, [event("message-alias-first", "ObservationRecorded", first)])

    with pytest.raises(ValueError, match="observation identity"):
        commit(
            ledger,
            [event("message-alias-second", "ObservationRecorded", second)],
        )


def test_acceptance_without_a_proposal_is_rejected_at_ingress() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)

    with pytest.raises(ValueError, match="proposal_id"):
        commit(
            ledger,
            [
                event(
                    "orphan-acceptance",
                    "AcceptanceRecorded",
                    {"status": "accepted"},
                )
            ],
        )


def test_baseline_calibration_rejects_single_round_evidence_without_episode_history() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [event("baseline-time", "ObservationRecorded", {"observation_id": "time:1"})],
    )
    commit(
        ledger,
        [
            event(
                "baseline-evidence:1",
                "OperatorObservationRecorded",
                {"observation_id": "calibration:1", "observation_hash": "1" * 64},
            ),
            event(
                "baseline-evidence:2",
                "OperatorObservationRecorded",
                {"observation_id": "calibration:2", "observation_hash": "2" * 64},
            ),
        ],
    )
    evidence_refs = [
        {
            "ref_id": "calibration:1",
            "evidence_type": "operator_observation",
            "claim_purpose": "private_hypothesis",
            "source_world_revision": None,
            "immutable_hash": "1" * 64,
        },
        {
            "ref_id": "calibration:2",
            "evidence_type": "operator_observation",
            "claim_purpose": "private_hypothesis",
            "source_world_revision": None,
            "immutable_hash": "2" * 64,
        },
    ]
    revision = ledger.project().world_revision
    mutation: dict[str, object] = {
        "change_id": "change:baseline:hurt:1",
        "transition_id": "transition:baseline:hurt:1",
        "expected_entity_revision": 0,
        "evidence_refs": evidence_refs,
        "appraisal_refs": [],
        "policy_refs": ["policy:affect-baseline-v1"],
        "acceptance_id": "acceptance:baseline:hurt:1",
        "proposal_id": "proposal:baseline:hurt:1",
        "evaluated_world_revision": revision,
        "accepted_change_hash": "0" * 64,
        "dimension": "hurt",
        "baseline_before_bp": 0,
        "proposed_delta_bp": 300,
        "accepted_delta_bp": 200,
        "baseline_after_bp": 200,
        "calibration_policy_version": "affect-baseline-calibration.1",
        "calibration_window_from": (NOW - timedelta(days=10)).isoformat(),
        "calibration_window_to": NOW.isoformat(),
        "basis_episode_refs": [
            {
                "episode_id": "affect:missing-history",
                "terminal_entity_revision": 2,
                "component_id": "component:missing-history",
            }
        ],
    }
    mutation["accepted_change_hash"] = affect_mutation_hash(mutation)
    mutation_json = json.dumps(mutation, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with pytest.raises(ValueError, match="accepted change hash|basis does not resolve"):
        commit(
            ledger,
            [
                event(
                    "baseline-proposed",
                    "ProposalRecorded",
                    {
                        "proposal_id": mutation["proposal_id"],
                        "proposal_kind": "affect_transition",
                        "transition_kind": "baseline_adjust",
                        "change_id": mutation["change_id"],
                        "transition_id": mutation["transition_id"],
                        "evaluated_world_revision": revision,
                        "expected_entity_revision": 0,
                        "proposed_change_hash": mutation["accepted_change_hash"],
                        "evidence_refs": evidence_refs,
                        "appraisal_refs": [],
                        "policy_refs": ["policy:affect-baseline-v1"],
                        "proposed_mutation": {
                            "event_type": "AffectBaselineAdjusted",
                            "payload_json": mutation_json,
                        },
                    },
                )
            ],
        )


def test_acceptance_identity_is_globally_unique_across_proposals() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(
        ledger,
        [
            event(
                "generic-proposal:1",
                "ProposalRecorded",
                {"proposal_id": "generic:1", "evaluated_world_revision": 0},
            ),
            event(
                "generic-proposal:2",
                "ProposalRecorded",
                {"proposal_id": "generic:2", "evaluated_world_revision": 0},
            ),
        ],
    )
    commit(
        ledger,
        [
            event(
                "generic-rejection:1",
                "AcceptanceRecorded",
                {
                    "status": "rejected",
                    "acceptance_id": "acceptance:global",
                    "proposal_id": "generic:1",
                    "evaluated_world_revision": 0,
                },
            )
        ],
    )

    with pytest.raises(ValueError, match="acceptance identity"):
        commit(
            ledger,
            [
                event(
                    "generic-rejection:2",
                    "AcceptanceRecorded",
                    {
                        "status": "rejected",
                        "acceptance_id": "acceptance:global",
                        "proposal_id": "generic:2",
                        "evaluated_world_revision": 0,
                    },
                )
            ],
        )


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_resolved_affect_episode(
    ledger: SQLiteWorldLedger,
    *,
    index: int,
    at: datetime,
    source_cluster_ref: str,
) -> AffectEpisodeProjection:
    observation_id = f"baseline-message:{index}"
    observation_payload = {
        "schema_version": "world-v2.1",
        "observation_kind": "message",
        "observation_id": observation_id,
        "world_id": WORLD_ID,
        "logical_time": at.isoformat(),
        "created_at": at.isoformat(),
        "trace_id": f"trace:baseline-message:{index}",
        "causation_id": f"cause:baseline-message:{index}",
        "correlation_id": f"correlation:baseline:{index}",
        "source": "test-platform",
        "source_event_id": f"source:baseline-message:{index}",
        "actor": "user:test",
        "channel": "direct_message",
        "payload_ref": f"payload:baseline-message:{index}",
        "payload_hash": f"{index}" * 64,
        "received_at": at.isoformat(),
    }
    commit(
        ledger,
        [
            event(
                f"event:baseline-message:{index}",
                "ObservationRecorded",
                observation_payload,
                at=at,
            )
        ],
    )

    trigger_id = interaction_appraisal_trigger_identity(WORLD_ID, observation_id)
    trigger = TriggerProcess(
        trigger_id=trigger_id,
        trigger_ref=f"interaction:{observation_id}",
        process_kind="interaction_appraisal",
        source_evidence_ref=observation_id,
        state="open",
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-trigger-opened:{index}",
                "TriggerProcessOpened",
                {"process": trigger.model_dump(mode="json")},
                at=at,
            )
        ],
    )
    claimed = trigger.model_copy(
        update={
            "state": "claimed",
            "claim_lease": ClaimLease(
                owner_id="worker:baseline-appraisal",
                attempt_id=f"attempt:baseline:{index}",
                acquired_at=at,
                expires_at=at + timedelta(minutes=2),
            ),
            "attempt_ids": (f"attempt:baseline:{index}",),
        }
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-trigger-claimed:{index}",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
                at=at,
            )
        ],
    )
    observation = next(
        item
        for item in ledger.project().message_observations
        if item.observation_id == observation_id
    )
    evidence = EvidenceRef(
        ref_id=observation_id,
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        source_world_revision=observation.world_revision,
        immutable_hash=observation.event_payload_hash,
    )

    appraisal_id = f"appraisal:baseline:{index}"
    appraisal_change_id = f"change:baseline-appraisal:{index}"
    appraisal_transition_id = f"transition:baseline-appraisal:{index}"
    appraisal_proposal_id = f"proposal:baseline-appraisal:{index}"
    appraisal_acceptance_id = f"acceptance:baseline-appraisal:{index}"
    appraisal_event_id = f"event:baseline-appraisal-accepted:{index}"
    appraisal = AppraisalProjection(
        appraisal_id=appraisal_id,
        entity_revision=1,
        subject_ref="interaction:user:test",
        source_cluster_ref=source_cluster_ref,
        origin=AppraisalOrigin(
            change_id=appraisal_change_id,
            transition_id=appraisal_transition_id,
            policy_refs=("policy:appraisal-v1",),
            matrix_catalog_version="appraisal-matrix.1",
            clustering_policy_version="source-clustering.1",
            accepted_event_ref=appraisal_event_id,
        ),
        hypotheses=(
            AppraisalHypothesis(
                hypothesis_id=f"meaning:baseline-hurt:{index}",
                meaning="boundary_violation",
                attribution="user",
                controllability="controllable",
                severity="moderate",
                weight_bp=10_000,
            ),
        ),
        evidence_refs=(evidence,),
        confidence_bp=8_000,
        accepted_at=at,
        expires_at=at + timedelta(days=30),
    )
    appraisal_revision = ledger.project().world_revision
    appraisal_payload: dict[str, object] = {
        "change_id": appraisal_change_id,
        "transition_id": appraisal_transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "policy_refs": ["policy:appraisal-v1"],
        "acceptance_id": appraisal_acceptance_id,
        "proposal_id": appraisal_proposal_id,
        "evaluated_world_revision": appraisal_revision,
        "accepted_change_hash": "0" * 64,
        "trigger_id": trigger_id,
        "appraisal": appraisal.model_dump(mode="json"),
    }
    appraisal_payload["accepted_change_hash"] = appraisal_mutation_hash(appraisal_payload)
    commit(
        ledger,
        [
            event(
                f"event:baseline-appraisal-proposed:{index}",
                "ProposalRecorded",
                {
                    "proposal_id": appraisal_proposal_id,
                    "proposal_kind": "appraisal_transition",
                    "transition_kind": "accept",
                    "change_id": appraisal_change_id,
                    "trigger_id": trigger_id,
                    "trigger_ref": trigger.trigger_ref,
                    "source_evidence_ref": observation_id,
                    "evaluated_world_revision": appraisal_revision,
                    "expected_entity_revision": 0,
                    "proposed_change_hash": appraisal_payload["accepted_change_hash"],
                    "evidence_refs": [evidence.model_dump(mode="json")],
                    "policy_refs": ["policy:appraisal-v1"],
                    "proposed_mutation": {
                        "event_type": "AppraisalAccepted",
                        "payload_json": _canonical_json(appraisal_payload),
                    },
                },
                at=at,
            )
        ],
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-appraisal-acceptance:{index}",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": appraisal_acceptance_id,
                    "proposal_id": appraisal_proposal_id,
                    "evaluated_world_revision": appraisal_revision,
                    "accepted_change_id": appraisal_change_id,
                    "accepted_change_hash": appraisal_payload["accepted_change_hash"],
                },
                at=at,
            ),
            event(appraisal_event_id, "AppraisalAccepted", appraisal_payload, at=at),
            event(
                f"event:baseline-appraisal-completed:{index}",
                "TriggerProcessCompleted",
                {
                    "trigger_id": trigger_id,
                    "owner_id": "worker:baseline-appraisal",
                    "attempt_id": f"attempt:baseline:{index}",
                    "completed_at": at.isoformat(),
                    "runtime_outcome_ref": f"appraisal:{appraisal_id}",
                },
                at=at,
            ),
        ],
    )

    meaning = AppraisalMeaningRef(
        appraisal_id=appraisal_id,
        hypothesis_id=appraisal.hypotheses[0].hypothesis_id,
        source_cluster_ref=source_cluster_ref,
        accepted_change_id=appraisal_change_id,
        accepted_transition_id=appraisal_transition_id,
    )
    affect_proposal_id = f"proposal:baseline-affect-open:{index}"
    affect_change_id = f"change:baseline-affect-open:{index}"
    affect_transition_id = f"transition:baseline-affect-open:{index}"
    affect_acceptance_id = f"acceptance:baseline-affect-open:{index}"
    affect_event_id = f"event:baseline-affect-opened:{index}"
    episode = AffectEpisodeProjection(
        episode_id=f"affect:baseline:{index}",
        entity_revision=1,
        origin=AffectOrigin(
            change_id=affect_change_id,
            transition_id=affect_transition_id,
            policy_refs=("policy:affect-v1",),
            matrix_catalog_version="affect-matrix.1",
            accepted_event_ref=affect_event_id,
        ),
        components=(
            AffectComponentProjection(
                component_id=f"component:baseline-hurt:{index}",
                dimension="hurt",
                source_cluster_ref=source_cluster_ref,
                appraisal_refs=(meaning,),
                intensity_bp=3_000,
                decay_anchor_intensity_bp=3_000,
                opened_at=at,
                decay_anchor_at=at,
                decay_not_before=at + timedelta(minutes=2),
                last_stimulus_at=at,
                last_updated_at=at,
                decay_profile=AffectDecayProfileProjection(
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
                ),
                residue_bp=500,
            ),
        ),
        evidence_refs=(evidence,),
        opened_at=at,
        updated_at=at,
        status="active",
    )
    affect_revision = ledger.project().world_revision
    affect_payload: dict[str, object] = {
        "change_id": affect_change_id,
        "transition_id": affect_transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "appraisal_refs": [meaning.model_dump(mode="json")],
        "policy_refs": ["policy:affect-v1"],
        "acceptance_id": affect_acceptance_id,
        "proposal_id": affect_proposal_id,
        "evaluated_world_revision": affect_revision,
        "accepted_change_hash": "0" * 64,
        "episode": episode.model_dump(mode="json"),
    }
    affect_payload["accepted_change_hash"] = affect_mutation_hash(affect_payload)
    commit(
        ledger,
        [
            event(
                f"event:baseline-affect-proposed:{index}",
                "ProposalRecorded",
                {
                    "proposal_id": affect_proposal_id,
                    "proposal_kind": "affect_transition",
                    "transition_kind": "open",
                    "change_id": affect_change_id,
                    "transition_id": affect_transition_id,
                    "evaluated_world_revision": affect_revision,
                    "expected_entity_revision": 0,
                    "proposed_change_hash": affect_payload["accepted_change_hash"],
                    "evidence_refs": [evidence.model_dump(mode="json")],
                    "appraisal_refs": [meaning.model_dump(mode="json")],
                    "policy_refs": ["policy:affect-v1"],
                    "proposed_mutation": {
                        "event_type": "AffectEpisodeOpened",
                        "payload_json": _canonical_json(affect_payload),
                    },
                },
                at=at,
            )
        ],
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-affect-acceptance:{index}",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": affect_acceptance_id,
                    "proposal_id": affect_proposal_id,
                    "evaluated_world_revision": affect_revision,
                    "accepted_change_id": affect_change_id,
                    "accepted_change_hash": affect_payload["accepted_change_hash"],
                },
                at=at,
            ),
            event(affect_event_id, "AffectEpisodeOpened", affect_payload, at=at),
        ],
    )

    resolve_proposal_id = f"proposal:baseline-affect-resolve:{index}"
    resolve_change_id = f"change:baseline-affect-resolve:{index}"
    resolve_transition_id = f"transition:baseline-affect-resolve:{index}"
    resolve_acceptance_id = f"acceptance:baseline-affect-resolve:{index}"
    resolve_revision = ledger.project().world_revision
    resolve_payload: dict[str, object] = {
        "change_id": resolve_change_id,
        "transition_id": resolve_transition_id,
        "expected_entity_revision": 1,
        "evidence_refs": (evidence,),
        "appraisal_refs": (),
        "policy_refs": ("policy:affect-v1",),
        "acceptance_id": resolve_acceptance_id,
        "proposal_id": resolve_proposal_id,
        "evaluated_world_revision": resolve_revision,
        "accepted_change_hash": "0" * 64,
        "episode_id": episode.episode_id,
        "resolved_at": at,
        "resolution_refs": (evidence,),
        "reason_code": "calibration_history_resolved",
    }
    resolve_payload["accepted_change_hash"] = affect_mutation_hash(resolve_payload)
    resolve_payload = AffectEpisodeResolvedPayload.model_validate(resolve_payload).model_dump(
        mode="json"
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-resolve-proposed:{index}",
                "ProposalRecorded",
                {
                    "proposal_id": resolve_proposal_id,
                    "proposal_kind": "affect_transition",
                    "transition_kind": "resolve",
                    "change_id": resolve_change_id,
                    "transition_id": resolve_transition_id,
                    "evaluated_world_revision": resolve_revision,
                    "expected_entity_revision": 1,
                    "proposed_change_hash": resolve_payload["accepted_change_hash"],
                    "evidence_refs": [evidence.model_dump(mode="json")],
                    "appraisal_refs": [],
                    "policy_refs": ["policy:affect-v1"],
                    "proposed_mutation": {
                        "event_type": "AffectEpisodeResolved",
                        "payload_json": _canonical_json(resolve_payload),
                    },
                },
                at=at,
            )
        ],
    )
    commit(
        ledger,
        [
            event(
                f"event:baseline-resolve-acceptance:{index}",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": resolve_acceptance_id,
                    "proposal_id": resolve_proposal_id,
                    "evaluated_world_revision": resolve_revision,
                    "accepted_change_id": resolve_change_id,
                    "accepted_change_hash": resolve_payload["accepted_change_hash"],
                },
                at=at,
            ),
            event(
                f"event:baseline-affect-resolved:{index}",
                "AffectEpisodeResolved",
                resolve_payload,
                at=at,
            ),
        ],
    )
    return next(
        item for item in ledger.project().affect_episodes if item.episode_id == episode.episode_id
    )


def _baseline_payload(
    ledger: SQLiteWorldLedger,
    episodes: tuple[AffectEpisodeProjection, ...],
    *,
    proposal_suffix: str,
    expected_entity_revision: int = 0,
    baseline_before_bp: int = 0,
) -> dict[str, object]:
    evidence_refs: list[EvidenceRef] = []
    for episode in episodes:
        for evidence in (*episode.evidence_refs, *episode.resolution_refs):
            if evidence not in evidence_refs:
                evidence_refs.append(evidence)
    proposal_id = f"proposal:baseline-adjust:{proposal_suffix}"
    payload: dict[str, object] = {
        "change_id": f"change:baseline-adjust:{proposal_suffix}",
        "transition_id": f"transition:baseline-adjust:{proposal_suffix}",
        "expected_entity_revision": expected_entity_revision,
        "evidence_refs": tuple(evidence_refs),
        "appraisal_refs": (),
        "policy_refs": ("policy:affect-baseline-v1",),
        "acceptance_id": f"acceptance:baseline-adjust:{proposal_suffix}",
        "proposal_id": proposal_id,
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "dimension": "hurt",
        "baseline_before_bp": baseline_before_bp,
        "proposed_delta_bp": 250,
        "accepted_delta_bp": 200,
        "baseline_after_bp": baseline_before_bp + 200,
        "calibration_policy_version": "affect-baseline-calibration.1",
        "calibration_window_from": episodes[0].opened_at,
        "calibration_window_to": episodes[-1].closed_at,
        "basis_episode_refs": tuple(
            AffectCalibrationEpisodeRef(
                episode_id=episode.episode_id,
                terminal_entity_revision=episode.entity_revision,
                component_id=episode.components[0].component_id,
            )
            for episode in episodes
        ),
    }
    payload["accepted_change_hash"] = affect_mutation_hash(payload)
    return AffectBaselineAdjustedPayload.model_validate(payload).model_dump(mode="json")


def _baseline_proposal_event(payload: dict[str, object]) -> WorldEvent:
    return event(
        f"event:{payload['proposal_id']}",
        "ProposalRecorded",
        {
            "proposal_id": payload["proposal_id"],
            "proposal_kind": "affect_transition",
            "transition_kind": "baseline_adjust",
            "change_id": payload["change_id"],
            "transition_id": payload["transition_id"],
            "evaluated_world_revision": payload["evaluated_world_revision"],
            "expected_entity_revision": payload["expected_entity_revision"],
            "proposed_change_hash": payload["accepted_change_hash"],
            "evidence_refs": payload["evidence_refs"],
            "appraisal_refs": [],
            "policy_refs": ["policy:affect-baseline-v1"],
            "proposed_mutation": {
                "event_type": "AffectBaselineAdjusted",
                "payload_json": _canonical_json(payload),
            },
        },
    )


def test_sqlite_replays_complete_affect_baseline_authority_chain(tmp_path) -> None:
    path = tmp_path / "affect-baseline-authority.sqlite3"
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    episodes = tuple(
        _record_resolved_affect_episode(
            ledger,
            index=index,
            at=at,
            source_cluster_ref=f"conversation:baseline:{index}",
        )
        for index, at in enumerate((NOW - timedelta(days=8), NOW - timedelta(days=4), NOW), start=1)
    )
    payload = _baseline_payload(ledger, episodes, proposal_suffix="success")
    commit(ledger, [_baseline_proposal_event(payload)])
    commit(
        ledger,
        [
            event(
                "event:baseline-adjust-acceptance:success",
                "AcceptanceRecorded",
                {
                    "status": "accepted",
                    "acceptance_id": payload["acceptance_id"],
                    "proposal_id": payload["proposal_id"],
                    "evaluated_world_revision": payload["evaluated_world_revision"],
                    "accepted_change_id": payload["change_id"],
                    "accepted_change_hash": payload["accepted_change_hash"],
                },
            ),
            event(
                "event:baseline-adjusted:success",
                "AffectBaselineAdjusted",
                payload,
            ),
        ],
    )

    expected = ledger.project()
    assert expected.affect_baselines[0].baseline_bp == 200
    assert expected.affect_baselines[0].calibration_revision == 1
    assert expected.affect_proposals == ()
    assert ledger.rebuild() == expected

    stale_payload = _baseline_payload(
        ledger,
        episodes,
        proposal_suffix="stale",
        expected_entity_revision=0,
        baseline_before_bp=0,
    )
    with pytest.raises(ValueError, match="stale affect baseline calibration"):
        commit(ledger, [_baseline_proposal_event(stale_payload)])
    assert ledger.project() == expected
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    assert reopened.project() == expected
    assert reopened.rebuild() == expected
    reopened.close()
