from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fact_authority import committed_fact_evidence
from test_memory_candidate_authority import (
    NOW,
    WORLD,
    candidate,
    event,
    initialized_ledger_with_fact,
    record_memory_accept_mutate,
)
from test_memory_candidate_authority import (
    mutation as memory_mutation,
)
from test_production_turn_application import (
    _Identities,
    _Router,
    _Transport,
)

from companion_daemon.world_v2.character_interior.production import (
    compose_fixture_character_interior,
)
from companion_daemon.world_v2.fact_events import FactChangedPayload, fact_mutation_hash
from companion_daemon.world_v2.memory_withdrawal_review import (
    MemoryWithdrawalReviewRuntime,
    _digest,
    materialize_memory_withdrawal_review_draft,
)
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    FactOrigin,
    FactProjection,
    FactProposalProjection,
    FactProposedMutation,
    TriggerProcess,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from character_interior import canonical_inner_decision


class _Model:
    model = "test-memory-review"

    def __init__(self, disposition: str) -> None:
        self.disposition = disposition
        self.calls = 0

    async def complete(self, messages, *, temperature=0.2) -> str:
        self.calls += 1
        assert messages[1]["role"] == "user"
        return '{"disposition":"' + self.disposition + '"}'


class _StructuredInteriorWithdrawalModel(_Model):
    supports_required_tool_choice = True

    async def complete_json(
        self,
        messages,
        *,
        temperature=0.2,
        tools=None,
        tool_choice=None,
    ):  # type: ignore[no-untyped-def]
        if tools is not None:
            assert len(tools) == 1
            assert tools[0]["function"]["name"] == (
                "character_role_memory_withdrawal_review_v1"
            )
            assert tool_choice == {
                "type": "function",
                "function": {"name": "character_role_memory_withdrawal_review_v1"},
            }
        return await self.complete(messages, temperature=temperature)

    async def complete(self, messages, *, temperature=0.2) -> str:
        del temperature
        self.calls += 1
        request = json.loads(messages[1]["content"])
        capability = request["capability_manifest"]
        return json.dumps(
            {
                "status": "decision",
                "summary": "I want this cue forgotten after its source was withdrawn.",
                "attended_source_refs": [],
                "decision": {
                    "source_refs": capability["source_refs"],
                    "payload": {"selected_token": self.disposition},
                },
                "recall_query": None,
                "proposals": [],
            }
        )


class _MalformedModel(_Model):
    async def complete(self, messages, *, temperature=0.2) -> str:
        self.calls += 1
        return '{"disposition":"forget","candidate_id":"forged"}'


class _NonTextModel(_Model):
    def __init__(self, disposition: str) -> None:
        super().__init__(disposition)
        self.correction_content_was_text = False

    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        del temperature
        self.calls += 1
        if self.calls == 2:
            self.correction_content_was_text = isinstance(messages[2]["content"], str)
        return {"disposition": self.disposition}


class _BlockedReviewModel:
    model = "blocked-memory-review"

    async def complete(self, messages, *, temperature=0.2):  # type: ignore[no-untyped-def]
        del messages, temperature
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _WithdrawalInterior:
    """Exercise the runtime through CharacterInterior.consider only."""

    def __init__(self, model, *, timeout_seconds: float = 8.0) -> None:  # type: ignore[no-untyped-def]
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.opportunities = []

    async def consider(self, opportunity):  # type: ignore[no-untyped-def]
        self.opportunities.append(opportunity)
        manifest = opportunity.capability_manifest
        messages = [
            {"role": "system", "content": "memory withdrawal review"},
            {
                "role": "user",
                "content": json.dumps(
                    {"capability_manifest": manifest.binding_value()},
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            async with asyncio.timeout(self.timeout_seconds):
                raw = await self.model.complete(messages, temperature=0.15)
                try:
                    disposition = materialize_memory_withdrawal_review_draft(
                        raw
                    ).disposition
                except (TypeError, ValueError):
                    rejected = raw if isinstance(raw, str) else json.dumps(raw)
                    raw = await self.model.complete(
                        [
                            *messages,
                            {"role": "assistant", "content": rejected},
                            {"role": "user", "content": "correct the JSON contract"},
                        ],
                        temperature=0.15,
                    )
                    disposition = materialize_memory_withdrawal_review_draft(
                        raw
                    ).disposition
        except (TimeoutError, TypeError, ValueError):
            return SimpleNamespace(
                status="technical_failure",
                failure_code="invalid_output",
                decision=None,
                author_lineage=None,
                inner_turn_id="inner-turn:test:technical-failure",
            )
        return canonical_inner_decision(
            opportunity,
            decision={
                "contract": "character-interior-purpose-decision.1",
                "purpose": opportunity.purpose,
                "source_refs": list(opportunity.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": "character-interior-memory-withdrawal-review.1",
                    "selected_token": disposition,
                },
            },
            identity=f"memory-withdrawal:{len(self.opportunities)}:{self.model.model}",
        )


def _record_fact_withdrawal(ledger: SQLiteWorldLedger) -> None:
    projection = ledger.project()
    ledger.commit(
        [
            event(
                "operator:memory-review-withdraw",
                "OperatorObservationRecorded",
                {
                    "observation_id": "operator:memory-review-withdraw",
                    "observation_hash": "8" * 64,
                },
            )
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    before = ledger.project().facts[0]
    prior = committed_fact_evidence(ledger, before)
    withdrawal = EvidenceRef(
        ref_id="operator:memory-review-withdraw",
        evidence_type="operator_observation",
        claim_purpose="current_fact",
        immutable_hash="8" * 64,
    )
    values = before.values.model_copy(
        update={
            "source_evidence_refs": (*before.values.source_evidence_refs, prior, withdrawal),
            "status": "withdrawn",
            "withdrawal_reason_code": "user_request",
            "withdrawal_evidence_ref": withdrawal.ref_id,
        }
    )
    origin = FactOrigin(
        change_id="change:fact:memory-review-withdraw",
        transition_id="transition:fact:memory-review-withdraw",
        policy_refs=before.origin.policy_refs,
        accepted_event_ref="event:fact:memory-review-withdrawn",
    )
    after = FactProjection(
        fact_id=before.fact_id,
        entity_revision=before.entity_revision + 1,
        semantic_fingerprint=before.semantic_fingerprint,
        values=values,
        origin=origin,
        committed_at=before.committed_at,
        updated_at=NOW,
    )
    raw = {
        "change_id": origin.change_id,
        "transition_id": origin.transition_id,
        "expected_entity_revision": before.entity_revision,
        "evidence_refs": values.source_evidence_refs,
        "policy_refs": origin.policy_refs,
        "acceptance_id": "acceptance:fact:memory-review-withdraw",
        "proposal_id": "proposal:fact:memory-review-withdraw",
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "operation": "withdraw",
        "fact_before": before,
        "fact_after": after,
        "compensates_transition_id": None,
    }
    raw["accepted_change_hash"] = fact_mutation_hash(raw)
    mutation = FactChangedPayload.model_validate(raw)
    proposal = FactProposalProjection(
        proposal_id=mutation.proposal_id,
        proposal_encoding="typed-authority-v1",
        authority_contract_ref="proposal-contract:fact.1",
        transition_kind="withdraw",
        change_id=mutation.change_id,
        transition_id=mutation.transition_id,
        evaluated_world_revision=mutation.evaluated_world_revision,
        expected_entity_revision=mutation.expected_entity_revision,
        proposed_change_hash=mutation.accepted_change_hash,
        evidence_refs=mutation.evidence_refs,
        policy_refs=mutation.policy_refs,
        proposed_mutation=FactProposedMutation(
            event_type="FactWithdrawn",
            payload_json=json.dumps(
                mutation.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
    )
    projection = ledger.project()
    ledger.commit(
        [
            event(
                "event:proposal:fact:memory-review-withdraw",
                "ProposalRecorded",
                proposal.model_dump(mode="json"),
            )
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    projection = ledger.project()
    ledger.commit(
        [
            event(
                "event:acceptance:fact:memory-review-withdraw",
                "AcceptanceRecorded",
                {
                    "acceptance_id": mutation.acceptance_id,
                    "status": "accepted",
                    "proposal_id": mutation.proposal_id,
                    "evaluated_world_revision": mutation.evaluated_world_revision,
                    "accepted_change_id": mutation.change_id,
                    "accepted_change_hash": mutation.accepted_change_hash,
                },
            ),
            event(
                after.origin.accepted_event_ref, "FactWithdrawn", mutation.model_dump(mode="json")
            ),
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )


def _prepared(path: Path) -> tuple[SQLiteWorldLedger, str]:
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD)
    _, source = initialized_ledger_with_fact(ledger)
    opened = candidate(source, accepted_event_ref="event:memory:review-open")
    record_memory_accept_mutate(
        ledger,
        memory_mutation(
            opened, operation="open", evaluated_world_revision=ledger.project().world_revision
        ),
    )
    active = candidate(
        source,
        revision=2,
        status="active",
        opened_at=opened.opened_at,
        reviewed_at=NOW,
        accepted_event_ref="event:memory:review-active",
    )
    record_memory_accept_mutate(
        ledger,
        memory_mutation(
            active,
            operation="accept",
            before=opened,
            evaluated_world_revision=ledger.project().world_revision,
        ),
    )
    _record_fact_withdrawal(ledger)
    return ledger, active.candidate_id


def test_review_draft_is_a_closed_semantic_choice() -> None:
    assert (
        materialize_memory_withdrawal_review_draft('{"disposition":"retain"}').disposition
        == "retain"
    )
    assert (
        materialize_memory_withdrawal_review_draft('{"disposition":"forget"}').disposition
        == "forget"
    )
    assert (
        materialize_memory_withdrawal_review_draft('{"disposition":"revise"}').disposition
        == "revise"
    )
    with pytest.raises(ValueError, match="unsupported"):
        materialize_memory_withdrawal_review_draft(
            '{"disposition":"forget","candidate_id":"memory:forged"}'
        )


@pytest.mark.asyncio
async def test_fact_reducer_does_not_cascade_but_review_forgets_exactly_once(tmp_path) -> None:
    path = tmp_path / "memory-review.sqlite3"
    ledger, candidate_id = _prepared(path)
    assert ledger.project().memory_candidates[0].values.status == "active"
    assert not any(
        item.process_kind == "memory_candidate_review"
        for item in ledger.project().trigger_processes
    )
    model = _Model("forget")
    interior = _WithdrawalInterior(model)
    runtime = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=interior,
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )
    result = await runtime.drain_one()
    assert (result.status, result.work_status) == ("processed", "forget")
    projection = ledger.project()
    head = next(item for item in projection.memory_candidates if item.candidate_id == candidate_id)
    assert head.values.status == "forgotten"
    assert head.entity_revision == 3
    assert projection.memory_candidate_transitions[-1].operation == "forget"
    assert (
        next(
            item
            for item in projection.trigger_processes
            if item.process_kind == "memory_candidate_review"
        ).state
        == "terminal"
    )
    assert await runtime.drain_one() == type(result)(trigger_id="", status="idle")
    assert model.calls == 1
    assert len(interior.opportunities) == 1
    assert interior.opportunities[0].purpose == "memory_withdrawal_review"
    assert interior.opportunities[0].capability_manifest.payload[
        "offered_tokens"
    ] == ["retain", "forget"]
    forgotten_event = ledger.lookup_event_commit(head.origin.accepted_event_ref)
    assert forgotten_event is not None
    forgotten_payload = json.loads(forgotten_event[0].payload_json)
    assert forgotten_payload["character_interior_model_result"][
        "audit_contract"
    ] == "model-result-audit.7"
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    restarted_model = _Model("retain")
    restarted = MemoryWithdrawalReviewRuntime(
        ledger=reopened,
        character_interior=_WithdrawalInterior(restarted_model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )
    assert (await restarted.drain_one()).status == "idle"
    assert restarted_model.calls == 0
    reopened.close()


@pytest.mark.asyncio
async def test_retain_is_a_durable_review_without_hidden_candidate_mutation(tmp_path) -> None:
    ledger, _ = _prepared(tmp_path / "retain.sqlite3")
    before = ledger.project().memory_candidates[0]
    model = _Model("retain")
    result = await MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    ).drain_one()
    assert (result.status, result.work_status) == ("processed", "retain")
    after = ledger.project().memory_candidates[0]
    assert after == before
    assert ledger.project().trigger_processes[-1].state == "terminal"
    terminal = ledger.project().trigger_processes[-1]
    completion_event_id = "event:memory-review:completed:" + _digest(
        [terminal.trigger_id, terminal.attempt_ids[-1]]
    )
    completion = ledger.lookup_event_commit(completion_event_id)
    assert completion is not None
    assert completion[0].payload()["character_interior_model_result"][
        "audit_contract"
    ] == "model-result-audit.7"
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model_type", [_MalformedModel, _NonTextModel])
@pytest.mark.asyncio
async def test_malformed_semantic_review_is_corrected_once_then_remains_technical(
    tmp_path, model_type
) -> None:
    ledger, _ = _prepared(tmp_path / "malformed.sqlite3")
    before = ledger.project().memory_candidates[0]
    model = model_type("forget")
    runtime = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )
    result = await runtime.drain_one()
    assert (result.status, result.work_status) == ("processed", "technical_failure")
    assert ledger.project().memory_candidates[0] == before
    assert ledger.project().trigger_processes[-1].state == "claimed"
    assert model.calls == 2
    if isinstance(model, _NonTextModel):
        assert model.correction_content_was_text is True
    ledger.close()


@pytest.mark.asyncio
async def test_memory_review_provider_has_one_total_deadline(tmp_path) -> None:
    ledger, _ = _prepared(tmp_path / "timeout.sqlite3")
    runtime = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(
            _BlockedReviewModel(), timeout_seconds=0.1
        ),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )

    result = await runtime.drain_one()

    assert (result.status, result.work_status) == ("processed", "technical_failure")
    assert ledger.project().trigger_processes[-1].state == "claimed"
    ledger.close()


@pytest.mark.asyncio
async def test_restart_after_proposal_reuses_audit_and_finishes_without_second_model_call(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "proposal-crash.sqlite3"
    ledger, _ = _prepared(path)
    model = _Model("forget")
    runtime = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )

    async def crash(**kwargs):
        raise RuntimeError("simulated crash after durable proposal")

    monkeypatch.setattr(runtime, "_accept_mutation_and_complete", crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await runtime.drain_one()
    assert ledger.project().memory_candidates[0].values.status == "active"
    assert len(ledger.project().memory_candidate_proposals) == 1
    ledger.close()

    reopened = SQLiteWorldLedger(path=path, world_id=WORLD)
    resumed_model = _Model("retain")
    resumed = MemoryWithdrawalReviewRuntime(
        ledger=reopened,
        character_interior=_WithdrawalInterior(resumed_model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )
    result = await resumed.drain_one()
    assert result.status == "joined"
    assert reopened.project().memory_candidates[0].values.status == "forgotten"
    assert resumed_model.calls == 0
    reopened.close()


@pytest.mark.asyncio
async def test_concurrent_workers_join_one_claim(tmp_path) -> None:
    ledger, _ = _prepared(tmp_path / "race.sqlite3")
    first_model, second_model = _Model("forget"), _Model("forget")
    first = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(first_model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review:a",
    )
    second = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(second_model),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review:b",
    )
    results = await asyncio.gather(first.drain_one(), second.drain_one())
    assert {item.status for item in results} <= {"processed", "owned_elsewhere", "idle", "joined"}
    assert sum(model.calls for model in (first_model, second_model)) == 1
    assert ledger.project().memory_candidates[0].values.status == "forgotten"
    assert (
        len(
            [
                item
                for item in ledger.project().memory_candidate_transitions
                if item.operation == "forget"
            ]
        )
        == 1
    )
    ledger.close()


@pytest.mark.asyncio
async def test_forged_trigger_source_binding_fails_closed(tmp_path) -> None:
    ledger, _ = _prepared(tmp_path / "forged.sqlite3")
    projection = ledger.project()
    withdrawal_ref = next(
        item.event_id
        for item in projection.committed_world_event_refs
        if item.event_type == "FactWithdrawn"
    )
    forged = TriggerProcess(
        trigger_id="trigger:memory-review:forged",
        trigger_ref="memory-review:forged",
        process_kind="memory_candidate_review",
        source_evidence_ref=withdrawal_ref,
        state="open",
    )
    ledger.commit(
        [
            event(
                "event:memory-review:forged",
                "TriggerProcessOpened",
                {"process": forged.model_dump(mode="json")},
            )
        ],
        expected_world_revision=projection.world_revision,
        expected_deliberation_revision=projection.deliberation_revision,
    )
    runtime = MemoryWithdrawalReviewRuntime(
        ledger=ledger,
        character_interior=_WithdrawalInterior(_Model("forget")),
        actor_ref="character:zhizhi",
        owner_id="worker:memory-review",
    )
    with pytest.raises(ValueError, match="does not bind"):
        await runtime.drain_one()
    assert ledger.project().memory_candidates[0].values.status == "active"
    ledger.close()


def test_review_trigger_cannot_name_a_non_withdrawal_source(tmp_path) -> None:
    ledger, _ = _prepared(tmp_path / "wrong-source.sqlite3")
    projection = ledger.project()
    forged = TriggerProcess(
        trigger_id="trigger:memory-review:wrong-source",
        trigger_ref="memory-review:wrong-source",
        process_kind="memory_candidate_review",
        source_evidence_ref="world:start",
        state="open",
    )
    with pytest.raises(ValueError, match="exact Fact withdrawal"):
        ledger.commit(
            [
                event(
                    "event:memory-review:wrong-source",
                    "TriggerProcessOpened",
                    {"process": forged.model_dump(mode="json")},
                )
            ],
            expected_world_revision=projection.world_revision,
            expected_deliberation_revision=projection.deliberation_revision,
        )
    ledger.close()


@pytest.mark.asyncio
async def test_production_builder_drains_character_interior_memory_withdrawal_review(
    tmp_path,
) -> None:
    path = tmp_path / "production-memory-review.sqlite3"
    prepared, _ = _prepared(path)
    prepared.close()
    model = _StructuredInteriorWithdrawalModel("forget")
    app = build_sqlite_world_v2_turn_application(
        path=path,
        config=WorldV2TurnApplicationConfig(
            world_id=WORLD,
            companion_actor_ref="actor:companion",
            reply_target="conversation:test",
            action_pump_owner="worker:action-pump",
        ),
        identities=_Identities(),
        router=_Router(),
        character_interior=compose_fixture_character_interior(model=model),
        transport=_Transport(),
        now=NOW,
    )
    result = await app.drain_background_once()
    assert result is not None
    assert (result.status, result.work_status) == ("processed", "forget")
    assert model.calls == 1
    app.close()
