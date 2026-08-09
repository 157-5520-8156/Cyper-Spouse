from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.affect_acceptance_runtime import AffectAcceptanceRuntime
from companion_daemon.world_v2.affect_proposal_compiler import AffectProposalCompiler
from companion_daemon.world_v2.appraisal_acceptance_runtime import AppraisalAcceptanceRuntime
from companion_daemon.world_v2.appraisal_proposal_compiler import AppraisalProposalCompiler
from companion_daemon.world_v2.appraisal_proposal_worker import AppraisalProposalWorker
from companion_daemon.world_v2.aspiration_events import AspirationPlantedPayload
from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.character_interior.authority import (
    _DeferredInteriorAuthority,
)
from companion_daemon.world_v2.character_interior.contracts import FACET_NAMES
from companion_daemon.world_v2.character_interior.structured_role import (
    StructuredCharacterRoleFaculty,
)
from companion_daemon.world_v2.character_interior.world_stimulus import (
    CharacterInteriorWorldStimulusRuntime,
    _WorldStimulusInteriorAuthorityHandler,
    _WorldStimulusRelationshipSignalSettlement,
)
from companion_daemon.world_v2.character_interior.run_result import (
    CausalOpportunityIdentity,
)
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.immediate_emotion_proposal_worker import (
    ImmediateEmotionProposalWorker,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.relationship_acceptance_runtime import (
    RelationshipAcceptanceRuntime,
    relationship_mutation_event_id,
)
from companion_daemon.world_v2.relationship_adjustment_acceptance_runtime import (
    RelationshipAdjustmentAcceptanceRuntime,
)
from companion_daemon.world_v2.relationship_adjustment_compiler import (
    RelationshipAdjustmentCompiler,
)
from companion_daemon.world_v2.relationship_adjustment_trigger_runtime import (
    RelationshipAdjustmentTriggerRuntime,
)
from companion_daemon.world_v2.relationship_adjustment_worker import (
    RelationshipAdjustmentWorker,
)
from companion_daemon.world_v2.relationship_events import (
    RelationshipSignalAcceptedPayload,
    relationship_mutation_hash,
)
from companion_daemon.world_v2.relationship_proposal_compiler import (
    RelationshipProposalCompiler,
)
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    validate_proposal_envelope,
)
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    AspirationProjection,
    ProjectionCursor,
    RelationshipProposalProjection,
    RelationshipProposedMutation,
    RelationshipSignalOrigin,
    RelationshipSignalProjection,
    RelationshipVariableDeltas,
    WorldEvent,
    relationship_signal_fingerprint,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from test_life_projection import WORLD_ID, commit, seed_through_proposal, settlement_batch


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
SOURCE_REF = "occurrence-settled"


def test_causal_opportunity_identity_is_canonical_and_actor_bound() -> None:
    first = CausalOpportunityIdentity(
        world_id=WORLD_ID,
        actor_ref="actor:companion",
        purpose="world_stimulus_appraisal",
        source_refs=("source:a", "source:b"),
        epoch="epoch:1",
    )
    replay = first.model_copy()
    other_actor = first.model_copy(update={"actor_ref": "actor:other"})

    assert first.opportunity_ref == replay.opportunity_ref
    assert first.opportunity_ref != other_actor.opportunity_ref
    with pytest.raises(ValueError, match="canonicalized"):
        CausalOpportunityIdentity(
            world_id=WORLD_ID,
            actor_ref="actor:companion",
            purpose="world_stimulus_appraisal",
            source_refs=("source:b", "source:a"),
            epoch="epoch:1",
        )


class _Projection:
    def __init__(self, *, source_ref: str = SOURCE_REF) -> None:
        self.source_ref = source_ref
        self.subjects = []

    async def project(self, *, subject):  # type: ignore[no-untyped-def]
        self.subjects.append(subject)
        return {
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "cursor": subject.cursor,
            "logical_time": subject.logical_time,
            "situation": {
                "availability": "available",
                "content": {"current_activity": "living her own evening"},
                "source_refs": (self.source_ref,),
            },
            "continuity": {
                "availability": "available",
                "content": {"emotional_continuity": "open to change"},
                "source_refs": (self.source_ref,),
            },
            "facets": {
                name: {
                    "availability": "available",
                    "content": {"summary": name},
                    "source_refs": (self.source_ref,),
                }
                for name in FACET_NAMES
            },
        }


class _RoleModel:
    model = "fixture-character-author"
    supports_required_tool_choice = True

    def __init__(
        self,
        *,
        decision: str = "activate",
        failure: Exception | None = None,
        source_ref: str = SOURCE_REF,
        include_affect: bool = False,
        relationship_subject_ref: str | None = None,
        invalid_affect_once: bool = False,
        aspiration_transition: dict[str, object] | None = None,
        experience_transition: dict[str, object] | None = None,
    ) -> None:
        self.decision = decision
        self.failure = failure
        self.source_ref = source_ref
        self.include_affect = include_affect
        self.relationship_subject_ref = relationship_subject_ref
        self.invalid_affect_once = invalid_affect_once
        self.aspiration_transition = aspiration_transition
        self.experience_transition = experience_transition
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature=0.8):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        self.messages.append(messages)
        if self.failure is not None:
            raise self.failure
        common = {
            "proposal_type": "world_stimulus_appraisal_result",
            "decision": self.decision,
            "brief_rationale": "这件事在她心里留下了自己的分量。",
            "behavior_tendency": "先放在心里感受",
            "stance": "按自己的理解看待",
            "display_strategy": "withhold",
            "confidence": 7200,
        }
        if self.decision == "activate":
            common.update(
                {
                    "meaning_candidates": [
                        {
                            "meaning": "她感觉这次经历让自己的方向往前走了一小步",
                            "confidence": 7000,
                        },
                        {
                            "meaning": "但下一步会怎样仍然没有把握",
                            "confidence": 3000,
                        },
                    ],
                    "attribution": "situation",
                    "severity": 4200,
                    "expiry": None,
                }
            )
            if self.include_affect:
                common["affect_transition"] = {
                    "operation": "open",
                    "component_targets": [
                        {
                            "dimension": "joy",
                            "target_intensity_bp": (
                                100
                                if self.invalid_affect_once and self.calls == 1
                                else 4_200
                            ),
                        }
                    ],
                }
            if self.relationship_subject_ref is not None:
                common["relationship_signal"] = {
                    "subject_ref": self.relationship_subject_ref,
                    "signal_code": "她觉得这件事改变了自己对这段关系的感受",
                    "confidence_bp": 7600,
                    "persistence": "durable",
                    "rationale_code": "当前真实经历牵动了她对这段关系的理解",
                    "suggested_deltas": {
                        "trust_bp": 90,
                        "closeness_bp": 140,
                        "respect_bp": 30,
                        "reliability_bp": 20,
                        "mutuality_bp": 110,
                        "repair_confidence_bp": 0,
                    },
                }
        if self.aspiration_transition is not None:
            common["aspiration_transition"] = self.aspiration_transition
        if self.experience_transition is not None:
            common["experience_transition"] = self.experience_transition
        status = (
            "transition"
            if self.decision == "activate"
            or self.aspiration_transition is not None
            or self.experience_transition is not None
            else "no_change"
        )
        return json.dumps(
            {
                "status": status,
                "summary": "她先按自己的感受消化这件事。",
                "attended_source_refs": [self.source_ref],
                "decision": None,
                "recall_query": None,
                "proposals": [common],
            },
            ensure_ascii=False,
        )

    async def complete_json(
        self,
        messages,
        *,
        temperature=0.8,
        tools,
        tool_choice,
    ):  # type: ignore[no-untyped-def]
        assert tools
        assert tool_choice == {
            "type": "function",
            "function": {"name": "character_role_world_stimulus_appraisal_v1"},
        }
        return await self.complete(messages, temperature=temperature)


def _runtime_for_ledger(
    *,
    ledger,
    issuer,
    model: _RoleModel,
    source_ref: str,
    companion_actor_ref: str,
    worker_wrapper=None,
    settle_relationship: bool = False,
    relationship_acceptance: RelationshipAcceptanceRuntime | None = None,
):  # type: ignore[no-untyped-def]
    appraisal_worker = AppraisalProposalWorker(
        compiler=AppraisalProposalCompiler(
            ledger=ledger,
            world_appraisal_subject_ref=companion_actor_ref,
        ),
        acceptance=AppraisalAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
        actor="worker:appraisal",
    )
    emotion_worker = ImmediateEmotionProposalWorker(
        appraisal_worker=appraisal_worker,
        affect_compiler=AffectProposalCompiler(ledger=ledger),
        affect_acceptance=AffectAcceptanceRuntime(
            ledger=ledger,
            batch_issuer=issuer,
        ),
        actor="worker:affect",
    )
    authority = _DeferredInteriorAuthority()
    projection = _Projection(source_ref=source_ref)
    interior = CharacterInterior(
        projection=projection,
        role=StructuredCharacterRoleFaculty(model=model, model_id=model.model),
        authority=authority,
    )
    handler = _WorldStimulusInteriorAuthorityHandler(
        ledger=ledger,
        owner_id="worker:appraisal",
    )
    authority.bind((handler,))
    runtime = CharacterInteriorWorldStimulusRuntime(
        ledger=ledger,
        character_interior=interior,
        emotion_worker=(
            emotion_worker
            if worker_wrapper is None
            else worker_wrapper(emotion_worker)
        ),
        owner_id="worker:appraisal",
        companion_actor_ref=companion_actor_ref,
        relationship_settlement=(
            _WorldStimulusRelationshipSignalSettlement(
                ledger=ledger,
                compiler=RelationshipProposalCompiler(ledger=ledger),
                acceptance=relationship_acceptance
                or RelationshipAcceptanceRuntime(ledger=ledger, batch_issuer=issuer),
                owner_id="worker:appraisal",
            )
            if settle_relationship
            else None
        ),
    )
    return runtime, ledger, projection


def _runtime(*, model: _RoleModel, worker_wrapper=None):  # type: ignore[no-untyped-def]
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    return _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        worker_wrapper=worker_wrapper,
    )


def _seed_active_aspiration(ledger) -> AspirationProjection:  # type: ignore[no-untyped-def]
    projection = ledger.project()
    logical_time = projection.logical_time
    assert logical_time is not None
    source = ledger.lookup_event_commit(SOURCE_REF)
    assert source is not None
    source_authority = next(
        item
        for item in projection.committed_world_event_refs
        if item.event_id == SOURCE_REF
    )
    planted_event_ref = "event:test:aspiration:planted"
    aspiration = AspirationProjection(
        aspiration_id="aspiration:test:weekend-pottery",
        entity_revision=1,
        owner_actor_ref="actor:companion",
        seed_id="character-interior:test-seed",
        origin_kind="reviewed_seed",
        text="想找个周末试一次拉坯。",
        privacy_class="private",
        planted_at=logical_time,
        planted_event_ref=planted_event_ref,
        source_event_ref=SOURCE_REF,
    )
    evidence = EvidenceRef(
        ref_id=SOURCE_REF,
        evidence_type="settled_world_event",
        claim_purpose="private_hypothesis",
        source_world_revision=source_authority.world_revision,
        immutable_hash=source_authority.payload_hash,
    )
    payload = AspirationPlantedPayload(
        change_id="change:test:aspiration:plant",
        transition_id="transition:test:aspiration:plant",
        expected_entity_revision=0,
        evidence_refs=(evidence,),
        policy_refs=("policy:aspiration.1",),
        aspiration=aspiration,
    )
    raw = payload.model_dump(mode="json")
    identity = domain_idempotency_key(
        event_type="AspirationPlanted",
        world_id=ledger.world_id,
        payload=raw,
    )
    assert identity is not None
    commit(
        ledger,
        [
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id=planted_event_ref,
                world_id=ledger.world_id,
                event_type="AspirationPlanted",
                logical_time=logical_time,
                created_at=source[0].created_at,
                actor="test:aspiration-seed",
                source="test:aspiration-seed",
                trace_id=source[0].trace_id,
                causation_id=SOURCE_REF,
                correlation_id=source[0].correlation_id,
                idempotency_key=identity,
                payload=raw,
            )
        ],
    )
    return aspiration


class _CrashOnceWorker:
    def __init__(self, delegate: ImmediateEmotionProposalWorker) -> None:
        self._delegate = delegate
        self.ledger = delegate.ledger
        self.calls = 0

    def process(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated crash after durable author audit")
        return self._delegate.process(**kwargs)


class _CrashAfterAppraisalWorker:
    def __init__(
        self,
        delegate: ImmediateEmotionProposalWorker,
        *,
        crash_calls: tuple[int, ...] = (1,),
    ) -> None:
        self._delegate = delegate
        self.ledger = delegate.ledger
        self._crash_calls = crash_calls
        self.calls = 0

    def process(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls in self._crash_calls:
            audit_cursor = kwargs["audit_cursor"]
            current_cursor = kwargs.get("current_cursor")
            appraisal = self._delegate._appraisal  # noqa: SLF001 - crash seam fixture
            if current_cursor is not None and current_cursor != audit_cursor:
                appraisal.process_rebased(
                    world_id=kwargs["world_id"],
                    audit_cursor=audit_cursor,
                    current_cursor=current_cursor,
                    proposal_id=kwargs["proposal_id"],
                )
            else:
                appraisal.process(
                    world_id=kwargs["world_id"],
                    cursor=audit_cursor,
                    proposal_id=kwargs["proposal_id"],
                )
            raise RuntimeError("simulated crash after Appraisal acceptance")
        return self._delegate.process(**kwargs)


class _CrashOnceRelationshipAcceptance(RelationshipAcceptanceRuntime):
    def __init__(self, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(**kwargs)
        self.calls = 0

    def accept_runtime_owned(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated crash before relationship acceptance")
        return super().accept_runtime_owned(**kwargs)


def _cursor_for(ledger) -> ProjectionCursor:  # type: ignore[no-untyped-def]
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _relationship_event(
    ledger,
    *,
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    source_event: WorldEvent,
) -> WorldEvent:  # type: ignore[no-untyped-def]
    identity = domain_idempotency_key(
        event_type=event_type,
        world_id=ledger.world_id,
        payload=payload,
    )
    assert identity is not None
    logical_time = ledger.project().logical_time
    assert logical_time is not None
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=ledger.world_id,
        event_type=event_type,
        logical_time=logical_time,
        created_at=source_event.created_at,
        actor="test:relationship-seed",
        source="test:relationship-seed",
        trace_id=source_event.trace_id,
        causation_id=source_event.event_id,
        correlation_id=source_event.correlation_id,
        idempotency_key=identity,
        payload=payload,
    )


async def _seed_relationship_state(
    *,
    ledger,
    issuer,
    source_ref: str,
    subject_ref: str,
) -> None:  # type: ignore[no-untyped-def]
    located = ledger.lookup_event_commit(source_ref)
    assert located is not None
    source_event, _source_commit = located
    logical_time = ledger.project().logical_time
    assert logical_time is not None
    source_authority = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == source_ref
    )
    evidence_type = (
        "settled_world_event"
        if source_event.event_type == "WorldOccurrenceSettled"
        else "committed_world_event"
    )
    evidence = EvidenceRef(
        ref_id=source_ref,
        evidence_type=evidence_type,
        claim_purpose="private_hypothesis",
        source_world_revision=source_authority.world_revision,
        immutable_hash=source_event.payload_hash,
    )
    proposal_id = "proposal:test:relationship-seed"
    change_id = "change:test:relationship-seed"
    transition_id = "transition:test:relationship-seed"
    policy_refs = ("policy:relationship-signal-v1",)
    mutation_event_id = relationship_mutation_event_id(
        world_id=ledger.world_id,
        proposal_id=proposal_id,
        transition_id=transition_id,
        event_type="RelationshipSignalAccepted",
    )
    signal = RelationshipSignalProjection(
        signal_id="signal:test:relationship-seed",
        semantic_fingerprint=relationship_signal_fingerprint(
            subject_ref=subject_ref,
            signal_code="existing_relationship_context",
            evidence_refs=(evidence,),
            policy_refs=policy_refs,
        ),
        entity_revision=1,
        subject_ref=subject_ref,
        signal_code="existing_relationship_context",
        confidence_bp=8000,
        persistence="durable",
        rationale_code="fixture_existing_relationship",
        suggested_deltas=RelationshipVariableDeltas(
            trust_bp=100,
            closeness_bp=100,
        ),
        evidence_refs=(evidence,),
        origin=RelationshipSignalOrigin(
            change_id=change_id,
            transition_id=transition_id,
            policy_refs=policy_refs,
            accepted_event_ref=mutation_event_id,
        ),
        accepted_at=logical_time,
    )
    mutation: dict[str, object] = {
        "change_id": change_id,
        "transition_id": transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "policy_refs": list(policy_refs),
        "acceptance_id": "acceptance:test:relationship-seed",
        "proposal_id": proposal_id,
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "signal": signal.model_dump(mode="json"),
    }
    mutation["accepted_change_hash"] = relationship_mutation_hash(mutation)
    RelationshipSignalAcceptedPayload.model_validate_json(
        json.dumps(
            mutation,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    typed = RelationshipProposalProjection(
        proposal_id=proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:relationship.1",
        transition_kind="signal",
        change_id=change_id,
        transition_id=transition_id,
        evaluated_world_revision=ledger.project().world_revision,
        expected_entity_revision=0,
        proposed_change_hash=str(mutation["accepted_change_hash"]),
        evidence_refs=(evidence,),
        policy_refs=policy_refs,
        proposed_mutation=RelationshipProposedMutation(
            event_type="RelationshipSignalAccepted",
            payload_json=json.dumps(
                mutation,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    cursor = _cursor_for(ledger)
    proposal_event = _relationship_event(
        ledger,
        event_id="event:test:relationship-seed:proposal",
        event_type="ProposalRecorded",
        payload=typed.model_dump(mode="json"),
        source_event=source_event,
    )
    ledger.commit_at_cursor(
        [proposal_event],
        expected_cursor=cursor,
        commit_id="commit:test:relationship-seed:proposal",
    )
    acceptance = RelationshipAcceptanceRuntime(
        ledger=ledger,
        batch_issuer=issuer,
    )
    acceptance.accept_runtime_owned(
        handle=acceptance.pin_proposal(
            cursor=_cursor_for(ledger),
            proposal_id=proposal_id,
        ),
        actor="test:relationship-seed",
        source="test:relationship-seed",
    )
    adjustment = await RelationshipAdjustmentTriggerRuntime(
        ledger=ledger,
        worker=RelationshipAdjustmentWorker(
            ledger=ledger,
            compiler=RelationshipAdjustmentCompiler(ledger=ledger),
            acceptance=RelationshipAdjustmentAcceptanceRuntime(
                ledger=ledger,
                batch_issuer=issuer,
            ),
            actor="test:relationship-seed",
        ),
        owner_id="test:relationship-seed",
    ).drain_one()
    assert adjustment.work_status == "accepted"
    assert ledger.project().relationship_states[0].subject_ref == subject_ref


@pytest.mark.asyncio
async def test_settled_world_change_crosses_one_eight_facet_character_interior_turn() -> None:
    model = _RoleModel(decision="activate", include_affect=True)
    runtime, ledger, projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    assert projection.subjects[0].purpose == "world_stimulus_appraisal"
    assert projection.subjects[0].source_refs == (SOURCE_REF,)
    supplied = json.loads(model.messages[0][1]["content"])
    assert supplied["eight_facets"] == list(FACET_NAMES)
    assert supplied["capability_manifest"]["payload"]["source_event"]["event_id"] == (
        SOURCE_REF
    )
    assert supplied["capability_manifest"]["payload"]["active_affect_heads"] == []
    appraisal = ledger.project().appraisals[0]
    assert appraisal.evidence_refs[0].ref_id == SOURCE_REF
    assert [item.dimension for item in ledger.project().affect_episodes[0].components] == [
        "joy"
    ]
    assert not any(
        item.process_kind in {"affect_deliberation", "relationship_deliberation"}
        and item.state != "terminal"
        for item in ledger.project().trigger_processes
    )


@pytest.mark.asyncio
async def test_source_bound_opportunity_advance_and_health_are_one_causal_view() -> None:
    model = _RoleModel(decision="no_change")
    runtime, ledger, _projection = _runtime(model=model)

    unrelated = await runtime.advance_once("event:unrelated-accepted-change")
    assert unrelated.status == "idle"
    assert model.calls == 0

    result = await runtime.advance_once(SOURCE_REF)
    assert result.work_status == "no_change"
    assert result.opportunity_ref is not None
    assert result.source_refs == (SOURCE_REF,)
    assert result.epoch == SOURCE_REF
    assert result.contract_version == "causal-opportunity.1"

    health = runtime.health_snapshot(WORLD_ID)
    assert health.world_id == WORLD_ID
    assert health.actor_ref == "actor:companion"
    assert health.purpose == "world_stimulus_appraisal"
    assert health.terminal_count == 1
    assert health.last_source_ref == SOURCE_REF
    assert health.last_opportunity_ref == result.opportunity_ref
    assert health.open_count == 0
    assert health.claimed_count == 0
    assert ledger.project().trigger_processes

    model_audit = next(
        item
        for item in ledger.project().model_result_audits
        if json.loads(item.audit_json)["character_interior_lineage"]["purpose"]
        == "world_stimulus_appraisal"
    )
    audit_json = json.loads(model_audit.audit_json)
    assert audit_json["character_interior_lineage"]["opportunity_ref"] == (
        result.opportunity_ref
    )

    replay = await runtime.advance_once(SOURCE_REF)
    assert replay.status == "idle"
    assert model.calls == 1
    assert runtime.health_snapshot(WORLD_ID).opportunity_count == 1


@pytest.mark.asyncio
async def test_model_no_change_is_durable_and_completes_the_exact_trigger() -> None:
    model = _RoleModel(decision="no_change")
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "no_change"
    assert model.calls == 1
    assert not ledger.project().appraisals
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    assert process.state == "terminal"
    assert any(
        item.trigger_ref == SOURCE_REF
        for item in ledger.project().proposal_audits
    )


@pytest.mark.asyncio
async def test_same_world_stimulus_inner_turn_can_plant_a_free_source_bound_aspiration() -> None:
    model = _RoleModel(
        decision="no_change",
        aspiration_transition={
            "operation": "plant",
            "aspiration_id": None,
            "text": "想找个周末学一次拉坯，看看自己会不会真的喜欢。",
            "privacy_class": "private",
            "tension_summary": "有兴趣，但不确定最近的精力够不够。",
            "tension_source_refs": [SOURCE_REF],
            "source_refs": [SOURCE_REF],
            "reason_summary": "这次经历让她第一次认真把它当成自己的可能方向。",
        },
    )
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    assert ledger.project().appraisals == ()
    assert len(ledger.project().aspirations) == 1
    aspiration = ledger.project().aspirations[0]
    assert aspiration.origin_kind == "character_authored"
    assert aspiration.text == "想找个周末学一次拉坯，看看自己会不会真的喜欢。"
    assert aspiration.tension_summary == "有兴趣，但不确定最近的精力够不够。"
    assert aspiration.tension_source_refs == (SOURCE_REF,)
    assert aspiration.source_event_ref == SOURCE_REF
    assert (await runtime.drain_one()).status == "idle"
    assert ledger.rebuild() == ledger.project()


@pytest.mark.asyncio
async def test_aspiration_transition_cannot_cite_material_outside_the_pinned_capability() -> None:
    model = _RoleModel(
        decision="no_change",
        aspiration_transition={
            "operation": "plant",
            "aspiration_id": None,
            "text": "想自己去一个没有依据的地方。",
            "privacy_class": "private",
            "tension_summary": None,
            "tension_source_refs": [],
            "source_refs": ["event:not-offered"],
            "reason_summary": "无依据。",
        },
    )
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "technical_failure"
    assert model.calls == 2
    assert ledger.project().aspirations == ()


@pytest.mark.asyncio
async def test_same_world_stimulus_inner_turn_can_revise_direction_and_inner_conflict() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    aspiration = _seed_active_aspiration(ledger)
    model = _RoleModel(
        decision="no_change",
        aspiration_transition={
            "operation": "revise",
            "aspiration_id": aspiration.aspiration_id,
            "text": "还是想学拉坯，但先只约一次体验，不逼自己长期坚持。",
            "privacy_class": "private",
            "tension_summary": "兴趣没有消失，只是不想把兴趣变成新的负担。",
            "tension_source_refs": [SOURCE_REF, aspiration.planted_event_ref],
            "source_refs": [SOURCE_REF, aspiration.planted_event_ref],
            "reason_summary": "她重新理解了自己真正想要的尺度。",
        },
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    revised = ledger.project().aspirations[0]
    assert revised.entity_revision == 2
    assert revised.text == "还是想学拉坯，但先只约一次体验，不逼自己长期坚持。"
    assert revised.tension_summary == "兴趣没有消失，只是不想把兴趣变成新的负担。"
    assert revised.revision_event_ref is not None
    assert ledger.rebuild() == ledger.project()


@pytest.mark.asyncio
async def test_same_world_stimulus_inner_turn_can_reinforce_an_existing_direction() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    aspiration = _seed_active_aspiration(ledger)
    model = _RoleModel(
        decision="no_change",
        aspiration_transition={
            "operation": "reinforce",
            "aspiration_id": aspiration.aspiration_id,
            "text": None,
            "privacy_class": None,
            "tension_summary": None,
            "tension_source_refs": [],
            "source_refs": [SOURCE_REF, aspiration.planted_event_ref],
            "reason_summary": "新的经历让这个方向在她心里更明确了一点。",
        },
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    reinforced = ledger.project().aspirations[0]
    assert reinforced.entity_revision == 2
    assert reinforced.reinforcement_count == 1
    assert reinforced.last_reinforced_at == ledger.project().logical_time
    assert ledger.rebuild() == ledger.project()


@pytest.mark.asyncio
async def test_same_world_stimulus_inner_turn_can_explicitly_abandon_its_direction() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=WORLD_ID, accepted_batch_issuer=issuer)
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    aspiration = _seed_active_aspiration(ledger)
    model = _RoleModel(
        decision="no_change",
        aspiration_transition={
            "operation": "abandon",
            "aspiration_id": aspiration.aspiration_id,
            "text": None,
            "privacy_class": None,
            "tension_summary": None,
            "tension_source_refs": [],
            "source_refs": [SOURCE_REF, aspiration.planted_event_ref],
            "reason_summary": "她发现这件事已经不再是自己想走的方向。",
        },
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    abandoned = ledger.project().aspirations[0]
    assert abandoned.entity_revision == 2
    assert abandoned.status == "abandoned"
    assert abandoned.abandonment_summary == "她发现这件事已经不再是自己想走的方向。"
    assert abandoned.abandonment_event_ref is not None
    assert ledger.rebuild() == ledger.project()


@pytest.mark.asyncio
async def test_appraisal_without_affect_settles_without_orphan_downstream_work() -> None:
    model = _RoleModel(decision="activate", include_affect=False)
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    projection = ledger.project()
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    assert process.state == "terminal"
    assert projection.affect_episodes == ()
    assert not any(
        item.process_kind in {"affect_deliberation", "relationship_deliberation"}
        for item in projection.trigger_processes
    )
    assert (await runtime.drain_one()).status == "idle"


@pytest.mark.asyncio
async def test_out_of_bounds_affect_gets_one_role_owned_reselection() -> None:
    model = _RoleModel(
        decision="activate",
        include_affect=True,
        invalid_affect_once=True,
    )
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 2
    assert ledger.project().affect_episodes[0].components[0].intensity_bp == 4_200


@pytest.mark.asyncio
async def test_provider_failure_stays_technical_and_leaves_trigger_retryable() -> None:
    model = _RoleModel(failure=ConnectionError("provider unavailable"))
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "technical_failure"
    assert model.calls == 1
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    assert process.state == "claimed"
    assert not ledger.project().proposal_audits


@pytest.mark.asyncio
async def test_provider_failure_defers_same_trigger_so_background_budget_can_move_on() -> None:
    model = _RoleModel(failure=ConnectionError("provider unavailable"))
    runtime, ledger, _projection = _runtime(model=model)

    first = await runtime.drain_one()
    second = await runtime.drain_one()

    assert first.work_status == "technical_failure"
    assert second.status == "idle"
    assert model.calls == 1
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    assert process.state == "claimed"


@pytest.mark.asyncio
async def test_restart_reuses_durable_character_result_without_calling_provider_twice() -> None:
    model = _RoleModel(decision="activate")
    runtime, ledger, _projection = _runtime(
        model=model,
        worker_wrapper=_CrashOnceWorker,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await runtime.drain_one()
    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    recovery_model = _RoleModel(
        failure=AssertionError("recovery must reuse the durable character result")
    )
    restarted, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )
    recovered = await restarted.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert recovery_model.calls == 0
    assert len(ledger.project().appraisals) == 1


@pytest.mark.asyncio
async def test_crash_before_typed_failure_also_defers_nonterminal_trigger(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    model = _RoleModel(decision="activate")
    runtime, ledger, _projection = _runtime(
        model=model,
    )

    async def crash_before_source_event(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated source crash")

    monkeypatch.setattr(runtime, "_source_event", crash_before_source_event)

    with pytest.raises(RuntimeError, match="simulated source crash"):
        await runtime.drain_one()

    assert (await runtime.drain_one()).status == "idle"
    monkeypatch.undo()
    runtime._technical_failure_deferred_until.clear()  # noqa: SLF001 - clock seam fixture
    recovered = await runtime.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert any(item.state == "terminal" for item in ledger.project().trigger_processes)


@pytest.mark.asyncio
async def test_restart_does_not_duplicate_an_aspiration_settled_before_later_state_work() -> None:
    model = _RoleModel(
        decision="activate",
        aspiration_transition={
            "operation": "plant",
            "aspiration_id": None,
            "text": "想找一天去学拉坯。",
            "privacy_class": "private",
            "tension_summary": None,
            "tension_source_refs": [],
            "source_refs": [SOURCE_REF],
            "reason_summary": "这件事让她真的起了这个念头。",
        },
    )
    runtime, ledger, _projection = _runtime(
        model=model,
        worker_wrapper=_CrashOnceWorker,
    )

    with pytest.raises(RuntimeError, match="simulated crash"):
        await runtime.drain_one()
    interrupted = ledger.project()
    assert len(interrupted.aspirations) == 1

    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    recovery_model = _RoleModel(
        failure=AssertionError("recovery must not duplicate the settled aspiration")
    )
    restarted, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )
    recovered = await restarted.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert recovery_model.calls == 0
    assert len(ledger.project().aspirations) == 1
    assert ledger.rebuild() == ledger.project()


@pytest.mark.asyncio
async def test_sqlite_cold_replay_resumes_one_character_authored_aspiration(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "character-interior-aspiration-replay.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    model = _RoleModel(
        decision="activate",
        aspiration_transition={
            "operation": "plant",
            "aspiration_id": None,
            "text": "想找一天去学拉坯。",
            "privacy_class": "private",
            "tension_summary": None,
            "tension_source_refs": [],
            "source_refs": [SOURCE_REF],
            "reason_summary": "这件事让她真的起了这个念头。",
        },
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        worker_wrapper=_CrashOnceWorker,
    )
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runtime.drain_one()
    assert len(ledger.project().aspirations) == 1
    ledger.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    recovery_model = _RoleModel(
        failure=AssertionError("cold replay must not invoke the character author")
    )
    recovered_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=reopened,
        issuer=reopened_issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )

    recovered = await recovered_runtime.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert recovery_model.calls == 0
    assert len(reopened.project().aspirations) == 1
    assert reopened.rebuild() == reopened.project()
    reopened.close()


@pytest.mark.asyncio
async def test_restart_finishes_authored_affect_after_appraisal_terminalized_source() -> None:
    model = _RoleModel(decision="activate", include_affect=True)
    runtime, ledger, _projection = _runtime(
        model=model,
        worker_wrapper=_CrashAfterAppraisalWorker,
    )

    with pytest.raises(RuntimeError, match="after Appraisal acceptance"):
        await runtime.drain_one()
    source_process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    assert source_process.state == "terminal"
    assert ledger.project().affect_episodes == ()

    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    recovery_model = _RoleModel(
        failure=AssertionError("terminal recovery must not re-author the Affect transition")
    )
    restarted, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )
    recovered = await restarted.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert recovery_model.calls == 0
    assert len(ledger.project().affect_episodes) == 1
    assert (await runtime.drain_one()).status == "idle"


@pytest.mark.asyncio
async def test_cold_restart_finishes_authored_affect_without_reauthoring(tmp_path) -> None:
    path = tmp_path / "world-stimulus-affect-recovery.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    model = _RoleModel(decision="activate", include_affect=True)
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        worker_wrapper=_CrashAfterAppraisalWorker,
    )

    with pytest.raises(RuntimeError, match="after Appraisal acceptance"):
        await runtime.drain_one()
    assert model.calls == 1
    assert ledger.project().affect_episodes == ()
    ledger.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    recovery_model = _RoleModel(
        failure=AssertionError("cold recovery must not re-author the Affect transition")
    )
    recovered_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=reopened,
        issuer=reopened_issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )

    recovered = await recovered_runtime.drain_one()

    assert recovered.work_status == "accepted"
    assert recovery_model.calls == 0
    assert len(reopened.project().affect_episodes) == 1
    assert reopened.rebuild() == reopened.project()
    reopened.close()


@pytest.mark.asyncio
async def test_terminal_recovery_failure_defers_only_this_runtime_and_allows_another_recovery() -> None:
    """One failed terminal aftermath cannot monopolize later recovery work.

    The two triggers are both already terminal because their immutable
    CharacterInterior audits crossed Appraisal acceptance before their Affect
    aftermath crashed.  A fresh runtime then crashes while resuming the first
    one: its next public ``drain_one`` must advance the independent terminal
    recovery, while another fresh runtime can still resume the deferred first
    trigger without re-authoring either decision.
    """
    model = _RoleModel(decision="activate", include_affect=True)
    setup_runtime, ledger, setup_projection = _runtime(
        model=model,
        worker_wrapper=lambda delegate: _CrashAfterAppraisalWorker(
            delegate,
            crash_calls=(1, 2),
        ),
    )

    with pytest.raises(RuntimeError, match="after Appraisal acceptance"):
        await setup_runtime.drain_one()
    first_trigger_id = next(
        item.trigger_id
        for item in ledger.project().trigger_processes
        if item.process_kind == "npc_world_appraisal"
    )
    accepted_ref = ledger.project().appraisals[0].origin.accepted_event_ref

    from companion_daemon.world_v2.reflection_scheduler import ReflectionScheduler

    assert ReflectionScheduler(ledger=ledger, actor="worker:reflection").open_once(
        trace_id="trace:terminal-recovery-isolation",
        correlation_id="correlation:terminal-recovery-isolation",
    ).opened == 1
    model.source_ref = accepted_ref
    setup_projection.source_ref = accepted_ref
    with pytest.raises(RuntimeError, match="after Appraisal acceptance"):
        await setup_runtime.drain_one()
    reflection_trigger_id = next(
        item.trigger_id
        for item in ledger.project().trigger_processes
        if item.process_kind == "life_reflection"
    )
    assert all(
        item.state == "terminal"
        for item in ledger.project().trigger_processes
        if item.trigger_id in {first_trigger_id, reflection_trigger_id}
    )

    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    recovery_model = _RoleModel(
        failure=AssertionError("terminal aftermath recovery must not re-author")
    )
    failing_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        worker_wrapper=_CrashOnceWorker,
    )

    with pytest.raises(RuntimeError, match="simulated crash after durable author audit"):
        await failing_runtime.drain_one()
    advanced = await failing_runtime.drain_one()

    assert advanced.trigger_id == reflection_trigger_id
    assert advanced.work_status == "accepted"
    assert recovery_model.calls == 0

    restarted_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )
    resumed = await restarted_runtime.drain_one()

    assert resumed.trigger_id == first_trigger_id
    assert resumed.work_status == "accepted"
    assert recovery_model.calls == 0
    assert (await restarted_runtime.drain_one()).status == "idle"


@pytest.mark.asyncio
async def test_settled_world_occurrence_relationship_signal_is_settled_once_and_adjustable() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref=SOURCE_REF,
        subject_ref="user:geoff",
    )
    model = _RoleModel(
        decision="activate",
        relationship_subject_ref="user:geoff",
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        settle_relationship=True,
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    signals = ledger.project().relationship_signals
    assert len(signals) == 2
    authored = next(
        item
        for item in signals
        if item.signal_code == "她觉得这件事改变了自己对这段关系的感受"
    )
    assert authored.subject_ref == "user:geoff"
    assert authored.evidence_refs[0].ref_id == SOURCE_REF
    assert not any(
        item.process_kind == "relationship_deliberation"
        for item in ledger.project().trigger_processes
    )

    before_replay = ledger.project()
    assert (await runtime.drain_one()).status == "idle"
    assert ledger.project() == before_replay

    adjustment = await RelationshipAdjustmentTriggerRuntime(
        ledger=ledger,
        worker=RelationshipAdjustmentWorker(
            ledger=ledger,
            compiler=RelationshipAdjustmentCompiler(ledger=ledger),
            acceptance=RelationshipAdjustmentAcceptanceRuntime(
                ledger=ledger,
                batch_issuer=issuer,
            ),
            actor="worker:relationship-adjustment",
        ),
        owner_id="worker:relationship-adjustment",
    ).drain_one()
    assert adjustment.work_status == "accepted"
    assert authored.signal_id in ledger.project().relationship_adjustments[-1].signal_refs


@pytest.mark.asyncio
async def test_world_stimulus_cannot_target_a_subject_outside_the_pinned_relationship_manifest() -> None:
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref=SOURCE_REF,
        subject_ref="user:geoff",
    )
    model = _RoleModel(
        decision="activate",
        relationship_subject_ref="npc:not-in-current-relationship",
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        settle_relationship=True,
    )

    result = await runtime.drain_one()

    assert result.work_status == "technical_failure"
    assert model.calls == 2
    assert len(ledger.project().relationship_signals) == 1
    assert not any(
        item.proposal_id.startswith(
            "proposal:character-interior-world-stimulus:"
        )
        for item in ledger.project().proposal_audits
    )


@pytest.mark.asyncio
async def test_claimed_source_recovers_pending_world_relationship_acceptance_after_restart(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "world-stimulus-relationship-recovery.sqlite3"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=issuer,
    )
    seed_through_proposal(ledger)
    commit(ledger, settlement_batch())
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref=SOURCE_REF,
        subject_ref="user:geoff",
    )
    acceptance = _CrashOnceRelationshipAcceptance(
        ledger=ledger,
        batch_issuer=issuer,
    )
    model = _RoleModel(
        decision="activate",
        relationship_subject_ref="user:geoff",
    )
    runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        settle_relationship=True,
        relationship_acceptance=acceptance,
    )

    with pytest.raises(RuntimeError, match="before relationship acceptance"):
        await runtime.drain_one()
    interrupted = ledger.project()
    assert next(
        item
        for item in interrupted.trigger_processes
        if item.process_kind == "npc_world_appraisal"
    ).state == "claimed"
    assert len(interrupted.relationship_proposals) == 1
    assert model.calls == 1
    ledger.close()

    reopened_issuer = AcceptedLedgerBatchIssuer()
    reopened = SQLiteWorldLedger(
        path=path,
        world_id=WORLD_ID,
        accepted_batch_issuer=reopened_issuer,
    )
    recovery_model = _RoleModel(
        failure=AssertionError("recovery must not call the character model")
    )
    recovered_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=reopened,
        issuer=reopened_issuer,
        model=recovery_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
        settle_relationship=True,
    )

    recovered = await recovered_runtime.drain_one()

    assert recovered.work_status == "accepted"
    assert model.calls == 1
    assert recovery_model.calls == 0
    assert len(reopened.project().relationship_signals) == 2
    assert reopened.project().relationship_proposals == ()
    assert (await recovered_runtime.drain_one()).status == "idle"
    assert reopened.rebuild() == reopened.project()
    reopened.close()


@pytest.mark.asyncio
async def test_unanswered_expression_uses_the_same_character_interior_stimulus_lane() -> None:
    from companion_daemon.world_v2.silence_appraisal_trigger import (
        SilenceAppraisalTriggerOpener,
    )
    from test_silence_appraisal import (
        IDLE_THRESHOLD,
        _advance,
        _deliver_reply,
        _delivered_reply_world,
        _receipt_event_id,
    )

    visible_runtime, ledger, _worker, _turn = _delivered_reply_world()
    await _deliver_reply(visible_runtime)
    await _advance(
        visible_runtime,
        ledger,
        seconds=IDLE_THRESHOLD * 2,
        tick_id="tick:unified-silence",
    )
    source_ref = _receipt_event_id(ledger)
    opener = SilenceAppraisalTriggerOpener(
        ledger=ledger,
        owner_id="worker:appraisal",
        idle_seconds_threshold=IDLE_THRESHOLD,
    )
    assert await opener.open_once() is not None
    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref=source_ref,
        subject_ref="user:geoff",
    )
    model = _RoleModel(
        decision="activate",
        source_ref=source_ref,
        relationship_subject_ref="user:geoff",
    )
    runtime, _ledger, projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=source_ref,
        companion_actor_ref="agent:companion",
        settle_relationship=True,
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    assert projection.subjects[0].source_refs == (source_ref,)
    assert ledger.project().appraisals[0].evidence_refs[0].ref_id == source_ref
    assert any(
        item.signal_code == "她觉得这件事改变了自己对这段关系的感受"
        and item.evidence_refs[0].ref_id == source_ref
        for item in ledger.project().relationship_signals
    )


@pytest.mark.asyncio
async def test_abandoned_plan_uses_the_same_character_interior_stimulus_lane() -> None:
    from companion_daemon.world_v2.plan_disruption_appraisal_trigger import (
        PlanDisruptionAppraisalTriggerOpener,
    )
    from test_plan_disruption_appraisal import (
        _abandoned_plan_world,
        _plan_and_abandon,
    )

    _base_runtime, ledger, _worker, _turn = await _abandoned_plan_world()
    source_ref = _plan_and_abandon(ledger, future_window=True)
    opener = PlanDisruptionAppraisalTriggerOpener(
        ledger=ledger,
        owner_id="worker:appraisal",
    )
    assert await opener.open_once() is not None
    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    await _seed_relationship_state(
        ledger=ledger,
        issuer=issuer,
        source_ref=source_ref,
        subject_ref="user:geoff",
    )
    model = _RoleModel(
        decision="activate",
        source_ref=source_ref,
        relationship_subject_ref="user:geoff",
    )
    runtime, _ledger, projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=model,
        source_ref=source_ref,
        companion_actor_ref="agent:companion",
        settle_relationship=True,
    )

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    supplied = json.loads(model.messages[0][1]["content"])
    assert supplied["capability_manifest"]["payload"]["stimulus_kind"] == (
        "abandoned_activity_plan"
    )
    assert projection.subjects[0].source_refs == (source_ref,)
    assert any(
        item.signal_code == "她觉得这件事改变了自己对这段关系的感受"
        and item.evidence_refs[0].ref_id == source_ref
        for item in ledger.project().relationship_signals
    )


@pytest.mark.asyncio
async def test_character_can_open_one_source_bound_thread_in_the_same_experience_turn() -> None:
    model = _RoleModel(
        decision="no_change",
        experience_transition={
            "domain": "thread",
            "operation": "open",
            "target_id": None,
            "expected_entity_revision": 0,
            "thread_kind": "topic_open",
            "importance_bp": 6100,
            "due_at": "2026-08-05T18:00:00Z",
            "expires_at": "2026-08-06T18:00:00Z",
            "resolution_kind": None,
            "cancellation_reason_code": None,
            "source_refs": [SOURCE_REF],
            "reason_summary": "她觉得这件事值得以后再回来看。",
        },
    )
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    assert model.calls == 1
    audits = ledger.project().proposal_audits
    assert len(audits) == 1
    proposal = validate_proposal_envelope(json.loads(audits[0].proposal_json))
    assert isinstance(proposal, DecisionProposal)
    change = next(
        item for item in proposal.proposed_changes if item.kind == "thread_transition"
    )
    assert change.transition == "open"
    assert change.expected_entity_revision == 0
    assert change.evidence_refs == (SOURCE_REF,)
    settled = ledger.project()
    assert any(
        item.status == "accepted"
        and item.accepted_change_id == change.change_id
        for item in settled.acceptance_decisions
    )
    assert len(settled.threads) == 1
    assert settled.threads[0].thread_id == change.target_id
    assert settled.threads[0].values.kind == "topic_open"
    assert settled.thread_transitions[-1].change_id == change.change_id

    # Restart/replay sees the terminal source and never re-authors the choice.
    replay_model = _RoleModel(decision="no_change")
    issuer = ledger._accepted_batch_issuer  # noqa: SLF001 - fixture authority
    assert issuer is not None
    replay, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=issuer,
        model=replay_model,
        source_ref=SOURCE_REF,
        companion_actor_ref="actor:companion",
    )
    replayed = await replay.drain_one()
    assert replayed.status == "idle"
    assert replay_model.calls == 0
    assert len(ledger.project().proposal_audits) == 1


@pytest.mark.asyncio
async def test_unavailable_goal_creation_is_rejected_before_proposal_write() -> None:
    model = _RoleModel(
        decision="no_change",
        experience_transition={
            "domain": "goal",
            "operation": "open",
            "target_id": "goal:invented",
            "expected_entity_revision": 0,
            "reason_kind": "renewed_intent",
            "source_refs": [SOURCE_REF],
            "reason_summary": "不能把自由文字冒充成 Goal 内容权威。",
        },
    )
    runtime, ledger, _projection = _runtime(model=model)

    result = await runtime.drain_one()

    assert result.work_status == "technical_failure"
    assert model.calls == 2  # the one allowed same-role constrained correction
    assert ledger.project().proposal_audits == ()
    assert not any(
        item.event_type in {"ModelResultRecorded", "ProposalRecordedV2"}
        for item in ledger.project().committed_world_event_refs
    )


# --- Reflection scheduler unit tests ---


class _StubAppraisal:
    def __init__(self, appraisal_id: str, confidence_bp: int, event_ref: str) -> None:
        self.appraisal_id = appraisal_id
        self.confidence_bp = confidence_bp
        self.status = "active"
        self.origin = type("O", (), {"accepted_event_ref": event_ref})()


class _StubProcess:
    def __init__(self, kind: str, source_evidence_ref: str) -> None:
        self.process_kind = kind
        self.source_evidence_ref = source_evidence_ref


class _StubProjection:
    def __init__(self, appraisals, processes) -> None:
        self.appraisals = appraisals
        self.trigger_processes = processes
        self.logical_time = NOW
        self.world_revision = 7
        self.deliberation_revision = 3
        self.ledger_sequence = 11
        self.committed_world_event_refs = ()


class _StubLedger:
    world_id = "world:life-ecology"

    def __init__(self, appraisals, processes) -> None:
        self._projection = _StubProjection(appraisals, processes)
        self.commits = []

    def project(self):
        return self._projection

    def commit_at_cursor(self, events, *, expected_cursor, commit_id):
        self.commits.append((events, commit_id))

    def lookup_event_commit(self, event_id):
        return None


def test_reflection_scheduler_opens_trigger_for_strong_appraisal() -> None:
    from companion_daemon.world_v2.reflection_scheduler import ReflectionScheduler

    appraisal = _StubAppraisal(
        "appraisal:strong", 7_500, "event:appraisal-accepted:strong"
    )
    ledger = _StubLedger([appraisal], [])
    scheduler = ReflectionScheduler(ledger=ledger, actor="worker:reflection")
    result = scheduler.open_once(trace_id="t", correlation_id="c")
    assert result.opened == 1
    assert len(ledger.commits) == 1
    event = ledger.commits[0][0][0]
    assert event.event_type == "TriggerProcessOpened"
    import json

    process = json.loads(event.payload_json)["process"]
    assert process["process_kind"] == "life_reflection"
    assert process["source_evidence_ref"] == "event:appraisal-accepted:strong"


def test_reflection_scheduler_skips_weak_appraisal() -> None:
    from companion_daemon.world_v2.reflection_scheduler import ReflectionScheduler

    appraisal = _StubAppraisal("appraisal:weak", 3_000, "event:appraisal-accepted:weak")
    ledger = _StubLedger([appraisal], [])
    scheduler = ReflectionScheduler(ledger=ledger, actor="worker:reflection")
    result = scheduler.open_once(trace_id="t", correlation_id="c")
    assert result.opened == 0
    assert ledger.commits == []


def test_reflection_scheduler_skips_already_reflected_appraisal() -> None:
    from companion_daemon.world_v2.reflection_scheduler import ReflectionScheduler

    appraisal = _StubAppraisal(
        "appraisal:done", 8_000, "event:appraisal-accepted:done"
    )
    process = _StubProcess("life_reflection", "event:appraisal-accepted:done")
    ledger = _StubLedger([appraisal], [process])
    scheduler = ReflectionScheduler(ledger=ledger, actor="worker:reflection")
    result = scheduler.open_once(trace_id="t", correlation_id="c")
    assert result.opened == 0
    assert ledger.commits == []


@pytest.mark.asyncio
async def test_life_reflection_reuses_accepted_appraisal_as_source_bound_stimulus() -> None:
    # The original world occurrence has already crossed the appraisal lane.
    # A later reflection must be allowed to form a fresh interpretation from
    # that immutable AppraisalAccepted event instead of being sent through a
    # compiler that only understands raw observations/world occurrences.
    first_model = _RoleModel(decision="activate")
    runtime, ledger, _projection = _runtime(model=first_model)
    first = await runtime.drain_one()
    assert first.work_status == "accepted"
    accepted_ref = ledger.project().appraisals[0].origin.accepted_event_ref

    from companion_daemon.world_v2.reflection_scheduler import ReflectionScheduler

    opened = ReflectionScheduler(ledger=ledger, actor="worker:reflection").open_once(
        trace_id="trace:reflection-test",
        correlation_id="correlation:reflection-test",
    )
    assert opened.opened == 1

    reflection_model = _RoleModel(
        decision="activate",
        include_affect=True,
        source_ref=accepted_ref,
    )
    reflection_runtime, _ledger, _projection = _runtime_for_ledger(
        ledger=ledger,
        issuer=ledger._accepted_batch_issuer,  # noqa: SLF001 - fixture authority
        model=reflection_model,
        source_ref=accepted_ref,
        companion_actor_ref="actor:companion",
    )
    reflected = await reflection_runtime.drain_one()

    assert reflected.work_status == "accepted"
    assert reflection_model.calls == 1
    process = next(
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "life_reflection"
    )
    assert process.state == "terminal"
    assert len(ledger.project().affect_episodes) == 1
