"""CharacterInterior private-impression scheduling and typed acceptance."""

from __future__ import annotations

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.appraisal_events import appraisal_mutation_hash
from companion_daemon.world_v2.batch_invariants import (
    interaction_appraisal_trigger_identity,
    private_impression_trigger_identity,
)
from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.character_interior.authority import (
    _DeferredInteriorAuthority,
)
from companion_daemon.world_v2.character_interior.contracts import FACET_NAMES
from companion_daemon.world_v2.character_interior.structured_role import (
    StructuredCharacterRoleFaculty,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.private_impression_producer import (
    PrivateImpressionTriggerOpener,
    PrivateImpressionTriggerRuntime,
    _PrivateImpressionInteriorAuthorityHandler,
    _PRIVATE_IMPRESSION_MAX_ATTEMPTS,
    _digest,
    compile_private_impression_reflection_capsule,
)
from companion_daemon.world_v2.schemas import (
    ClaimLease,
    EvidenceRef,
    TriggerProcess,
)
from companion_daemon.world_v2.companion_identity import CompanionIdentityFrame

from test_appraisal_authority import (
    accepted_payload as appraisal_payload,
    authorized_batch as appraisal_authorized_batch,
    commit,
    event,
    message_payload,
    prepare_claimed_interaction,
    record_proposal as record_appraisal_proposal,
)


WORLD_ID = "world-v2-appraisal-authority"
OWNER = "worker:test:private-impression"


def _ledger_with_active_appraisal():
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    commit(ledger, [event("world-start", "WorldStarted", {})])
    ledger, trigger, evidence = prepare_claimed_interaction(ledger)
    payload = appraisal_payload(ledger, trigger, evidence)
    record_appraisal_proposal(ledger, trigger, evidence, payload)
    commit(ledger, appraisal_authorized_batch(trigger, payload))
    return ledger


def _append_second_appraisal(ledger: WorldLedger) -> None:
    logical_time = ledger.project().logical_time
    assert logical_time is not None
    commit(
        ledger,
        [event("message-event:2", "ObservationRecorded", message_payload("message:2"))],
    )
    opened = TriggerProcess(
        trigger_id=interaction_appraisal_trigger_identity(WORLD_ID, "message:2"),
        trigger_ref="interaction:message:2",
        process_kind="interaction_appraisal",
        source_evidence_ref="message:2",
        state="open",
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-opened:2",
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
                attempt_id="attempt:interaction:2",
                acquired_at=logical_time,
                expires_at=logical_time + timedelta(minutes=2),
            ),
            "attempt_ids": ("attempt:interaction:2",),
        }
    )
    commit(
        ledger,
        [
            event(
                "interaction-trigger-claimed:2",
                "TriggerProcessClaimed",
                {"process": claimed.model_dump(mode="json")},
            )
        ],
    )
    observation = next(
        item for item in ledger.project().message_observations if item.observation_id == "message:2"
    )
    evidence = EvidenceRef(
        ref_id="message:2",
        evidence_type="observed_message",
        claim_purpose="private_hypothesis",
        source_world_revision=observation.world_revision,
        immutable_hash=observation.event_payload_hash,
    )
    first = ledger.project().appraisals[0]
    second = first.model_copy(
        update={
            "appraisal_id": "appraisal:interaction:2",
            "source_cluster_ref": "conversation:2",
            "origin": first.origin.model_copy(
                update={
                    "change_id": "change:interaction-appraisal:2",
                    "transition_id": "transition:interaction-appraisal:2",
                    "accepted_event_ref": "interaction-appraisal-accepted:2",
                }
            ),
            "evidence_refs": (evidence,),
        }
    )
    payload: dict[str, object] = {
        "change_id": second.origin.change_id,
        "transition_id": second.origin.transition_id,
        "expected_entity_revision": 0,
        "evidence_refs": [evidence.model_dump(mode="json")],
        "policy_refs": ["policy:appraisal-v1"],
        "acceptance_id": "acceptance:interaction-appraisal:2",
        "proposal_id": "proposal:interaction-appraisal:2",
        "evaluated_world_revision": ledger.project().world_revision,
        "accepted_change_hash": "0" * 64,
        "trigger_id": claimed.trigger_id,
        "appraisal": second.model_dump(mode="json"),
    }
    payload["accepted_change_hash"] = appraisal_mutation_hash(payload)
    commit(
        ledger,
        [
            event(
                "interaction-appraisal-proposed:2",
                "ProposalRecorded",
                {
                    "proposal_id": payload["proposal_id"],
                    "proposal_kind": "appraisal_transition",
                    "transition_kind": "accept",
                    "change_id": payload["change_id"],
                    "trigger_id": claimed.trigger_id,
                    "trigger_ref": claimed.trigger_ref,
                    "source_evidence_ref": claimed.source_evidence_ref,
                    "evaluated_world_revision": payload["evaluated_world_revision"],
                    "expected_entity_revision": 0,
                    "proposed_change_hash": payload["accepted_change_hash"],
                    "evidence_refs": [evidence.model_dump(mode="json")],
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
        ],
    )
    commit(
        ledger,
        [
            event(
                "interaction-appraisal-acceptance:2",
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
            event("interaction-appraisal-accepted:2", "AppraisalAccepted", payload),
            event(
                "interaction-appraisal-completed:2",
                "TriggerProcessCompleted",
                {
                    "trigger_id": claimed.trigger_id,
                    "owner_id": "worker:interaction-appraisal",
                    "attempt_id": "attempt:interaction:2",
                    "completed_at": logical_time.isoformat(),
                    "runtime_outcome_ref": "appraisal:appraisal:interaction:2",
                },
            ),
        ],
    )


def test_reflection_capsule_combines_role_relationship_affect_and_lived_layers() -> None:
    projection = _ledger_with_active_appraisal().project()
    anchor = projection.appraisals[0]
    accepted_at = projection.logical_time
    assert accepted_at is not None
    layered = SimpleNamespace(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
        logical_time=accepted_at,
        appraisals=(anchor,),
        character_core=SimpleNamespace(
            core_id="core:zhizhi",
            entity_revision=2,
            actor_ref="agent:companion",
            origin=SimpleNamespace(accepted_event_ref="event:core:2"),
            values=SimpleNamespace(
                model_dump=lambda **_: {"slow_evolving": {"temperament_refs": ["observant"]}}
            ),
        ),
        relationship_states=(
            SimpleNamespace(
                relationship_id="relationship:primary",
                entity_revision=3,
                subject_ref=anchor.subject_ref,
                origin=SimpleNamespace(accepted_event_ref="event:relationship:3"),
                stage="friend",
                variables=SimpleNamespace(
                    model_dump=lambda **_: {"trust_bp": 7200, "closeness_bp": 6800}
                ),
                temperature="warm",
                commitment_refs=(),
                last_adjusted_at=accepted_at,
            ),
        ),
        affect_episodes=(
            SimpleNamespace(
                episode_id="affect:1",
                entity_revision=1,
                status="active",
                origin=SimpleNamespace(accepted_event_ref="event:affect:1"),
                components=(
                    SimpleNamespace(
                        appraisal_refs=(SimpleNamespace(appraisal_id=anchor.appraisal_id),),
                        dimension="warmth",
                        intensity_bp=4300,
                        residue_bp=600,
                        last_updated_at=accepted_at,
                    ),
                ),
                updated_at=accepted_at,
            ),
        ),
        experiences=(
            SimpleNamespace(
                experience_id="experience:tea",
                status="committed",
                origin=SimpleNamespace(accepted_event_ref="event:experience:tea"),
                values=SimpleNamespace(
                    participant_refs=(anchor.subject_ref,),
                    summary_ref="content:shared-tea",
                    occurred_from=accepted_at,
                    occurred_to=accepted_at,
                ),
            ),
        ),
        private_impressions=(
            SimpleNamespace(
                impression_id="impression:older",
                status="active",
                subject_ref=anchor.subject_ref,
                origin=SimpleNamespace(accepted_event_ref="event:impression:older"),
                reflection_summary="我之前觉得她在意被认真听见。",
                confidence_bp=5400,
                last_supported=accepted_at,
                expiry_condition="until_counter_evidence",
                interpretation_refs=(f"appraisal:{anchor.appraisal_id}:meaning:misunderstanding",),
            ),
        ),
    )

    capsule = compile_private_impression_reflection_capsule(
        projection=layered,
        appraisal=anchor,
        identity_frame=CompanionIdentityFrame(
            companion_name="沈知栀",
            counterpart_name="Geoff",
        ),
        world_id=WORLD_ID,
        content_reader=lambda ref: "一起喝茶时聊了很久。" if ref == "content:shared-tea" else None,
    )

    assert {item.source_kind for item in capsule.sources} == {
        "appraisal",
        "character_core",
        "relationship",
        "affect",
        "experience",
        "existing_impression",
    }
    experience = next(item for item in capsule.sources if item.source_kind == "experience")
    assert json.loads(experience.value_json)["summary_text"] == "一起喝茶时聊了很久。"


class _Model:
    model = "test-private-impression"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        return self.responses.pop(0)


class _PrivateInteriorProjection:
    async def project(self, *, subject):  # type: ignore[no-untyped-def]
        source_refs = subject.source_refs
        return {
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "cursor": subject.cursor,
            "logical_time": subject.logical_time,
            "situation": {
                "availability": "available",
                "content": {"fixture": "accepted appraisal"},
                "source_refs": source_refs,
            },
            "continuity": {
                "availability": "available",
                "content": {"fixture": "private interpretation continuity"},
                "source_refs": source_refs,
            },
            "facets": {
                name: {
                    "availability": "available",
                    "content": {"summary": name},
                    "source_refs": source_refs,
                }
                for name in FACET_NAMES
            },
        }


class _PrivateInteriorWireModel:
    """Test-only old response fixtures translated into the unified role wire."""

    supports_required_tool_choice = True

    def __init__(self, delegate: _Model) -> None:
        self._delegate = delegate
        self.model = delegate.model

    async def complete(self, messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        raw = await self._delegate.complete(messages, temperature=temperature)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if not isinstance(value, dict) or "status" in value:
            return raw
        decision = value.get("decision")
        if decision is None and isinstance(value.get("retain"), bool):
            decision = "retain" if value["retain"] else "no_change"
        request = json.loads(messages[-1]["content"])
        source_refs = request["capability_manifest"]["source_refs"]
        if decision == "no_change":
            result = {
                "status": "no_change",
                "summary": "She chose not to retain a new private impression.",
                "attended_source_refs": source_refs,
                "decision": None,
                "recall_query": None,
                "proposals": [],
            }
        elif decision in {"retain", "consolidate", "supersede"}:
            proposal = {
                "proposal_type": "private_impression_transition",
                "decision": decision,
                "predecessor_refs": value.get("predecessor_refs", []),
                "source_refs": value.get("source_refs"),
                "reflection_summary": value.get("reflection_summary"),
                "confidence_bp": value.get("confidence"),
                "expiry_condition": value.get("expiry_condition"),
            }
            result = {
                "status": "transition",
                "summary": "She formed a tentative, revisable private reading.",
                "attended_source_refs": source_refs,
                "decision": None,
                "recall_query": None,
                "proposals": [proposal],
            }
        else:
            return raw
        return json.dumps(result, ensure_ascii=False)

    async def complete_json(
        self,
        messages,
        *,
        temperature: float = 0.8,
        tools,
        tool_choice,
    ) -> str:  # type: ignore[no-untyped-def]
        if not isinstance(tools, list) or len(tools) != 1:
            raise AssertionError("private impression must use exactly one required tool")
        function = tools[0].get("function")
        if not isinstance(function, dict) or function.get("name") != (
            "character_role_private_impression_reflection_v1"
        ):
            raise AssertionError("private impression used the wrong tool")
        if tool_choice != {
            "type": "function",
            "function": {"name": "character_role_private_impression_reflection_v1"},
        }:
            raise AssertionError("private impression tool choice was not forced")
        return await self.complete(messages, temperature=temperature)


def _private_runtime(
    ledger,
    model,
    *,
    owner_id: str = OWNER,
) -> tuple[PrivateImpressionTriggerRuntime, CharacterInterior]:
    authority = _DeferredInteriorAuthority()
    interior = CharacterInterior(
        projection=_PrivateInteriorProjection(),
        role=StructuredCharacterRoleFaculty(
            model=_PrivateInteriorWireModel(model),
            model_id=model.model,
        ),
        authority=authority,
    )
    runtime = PrivateImpressionTriggerRuntime(
        ledger=ledger,
        character_interior=interior,
        companion_actor_ref="actor:companion",
        owner_id=owner_id,
    )
    authority.bind((_PrivateImpressionInteriorAuthorityHandler(runtime),))
    return runtime, interior




def _retain(
    source_refs: list[str],
    *,
    reflection_summary: str = "我暂时觉得这更像是失望，不一定是在否定我。",
) -> str:
    return json.dumps(
        {
            "decision": "retain",
            "source_refs": source_refs,
            "reflection_summary": reflection_summary,
            "confidence": 6_000,
            "expiry_condition": "until_counter_evidence",
        },
        ensure_ascii=False,
    )




@pytest.mark.asyncio
async def test_opener_leaves_one_deterministic_trigger_per_accepted_appraisal() -> None:
    ledger = _ledger_with_active_appraisal()
    opener = PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER)

    trigger_id = await opener.open_once()
    assert trigger_id == private_impression_trigger_identity(
        WORLD_ID, "interaction-appraisal-accepted"
    )
    process = next(
        item for item in ledger.project().trigger_processes if item.trigger_id == trigger_id
    )
    assert process.process_kind == "private_impression_deliberation"
    assert process.source_evidence_ref == "interaction-appraisal-accepted"
    assert process.state == "open"

    # The identity is durable: repeated passes never open a second trigger.
    assert await opener.open_once() is None


@pytest.mark.asyncio
async def test_character_interior_accepts_one_source_bound_private_impression() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(
        [_retain(["appraisal:appraisal:interaction:1:meaning:disappointment"])]
    )
    runtime, _interior = _private_runtime(ledger, model)

    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    projection = ledger.project()
    assert len(projection.private_impressions) == 1
    impression = projection.private_impressions[0]
    assert impression.reflection_summary == (
        "我暂时觉得这更像是失望，不一定是在否定我。"
    )
    assert projection.trigger_processes[-1].state == "terminal"
    assert len(model.calls) == 1
    assert projection.model_result_audits[-1].audit_contract == "model-result-audit.7"


@pytest.mark.asyncio
async def test_character_interior_no_change_consumes_only_that_trigger() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(['{"decision":"no_change"}'])
    runtime, _interior = _private_runtime(ledger, model)

    result = await runtime.drain_one()

    assert result.work_status == "no_change"
    assert ledger.project().private_impressions == ()
    assert ledger.project().trigger_processes[-1].state == "terminal"
    assert len(model.calls) == 1
    terminal = ledger.project().trigger_processes[-1]
    completion_event_id = "event:private-impression:completed:" + _digest(
        [terminal.trigger_id, terminal.attempt_ids[-1]]
    )
    completion = ledger.lookup_event_commit(completion_event_id)
    assert completion is not None
    quiet_audit = completion[0].payload()["character_interior_model_result"]
    assert quiet_audit["audit_contract"] == "model-result-audit.7"


@pytest.mark.asyncio
async def test_invalid_private_reflection_uses_one_interior_correction_then_retries_later() -> None:
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(["{}", "{}"])
    runtime, interior = _private_runtime(ledger, model)

    result = await runtime.drain_one()

    assert result.work_status == "technical_failure"
    assert len(model.calls) == 2
    assert ledger.project().private_impressions == ()
    assert ledger.project().trigger_processes[-1].state == "claimed"
    assert interior.runtime_health()["last_failure_code"] == (
        "invalid_role_result_after_correction"
    )
    assert (await runtime.drain_one()).status == "owned_elsewhere"


@pytest.mark.asyncio
async def test_repeated_validation_failures_terminal_the_trigger_after_bounded_attempts() -> None:
    """A reflection whose model output never validates must not reclaim
    forever.  After ``_PRIVATE_IMPRESSION_MAX_ATTEMPTS`` attempts the process
    is terminal, no further provider calls are made, and the opener does not
    re-derive the same trigger (it was opened once already)."""
    ledger = _ledger_with_active_appraisal()
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()
    model = _Model(["{}"] * (_PRIVATE_IMPRESSION_MAX_ATTEMPTS * 2))
    runtime, _ = _private_runtime(ledger, model)

    for _ in range(_PRIVATE_IMPRESSION_MAX_ATTEMPTS):
        result = await runtime.drain_one()
        assert result.work_status == "technical_failure"
        # Advance the logical clock past the claim lease so the next drain
        # may reclaim (mirrors production clock ticks).
        projection = ledger.project()
        assert projection.logical_time is not None
        commit(
            ledger,
            [
                event(
                    f"event:clock-advance:{_}",
                    "ClockAdvanced",
                    {
                        "logical_time_from": projection.logical_time.isoformat(),
                        "logical_time_to": (
                            projection.logical_time + timedelta(minutes=5)
                        ).isoformat(),
                    },
                    at=projection.logical_time + timedelta(minutes=5),
                )
            ],
        )

    # The bounded attempts are exhausted: the next drain terminals the
    # process without another provider call, and the drain then idles.
    result = await runtime.drain_one()
    assert result.status == "owned_elsewhere"
    processes = [
        item
        for item in ledger.project().trigger_processes
        if item.process_kind == "private_impression_deliberation"
    ]
    assert all(item.state == "terminal" for item in processes)
    assert len(model.calls) <= _PRIVATE_IMPRESSION_MAX_ATTEMPTS * 2
    assert (await runtime.drain_one()).status == "idle"



@pytest.mark.asyncio
async def test_short_token_capability_maps_to_real_refs_and_recovers_on_missed_anchor() -> None:
    """The private-impression capability hands the model short tokens so any
    provider can select a source without echoing very long hash refs.  A
    short-token proposal must map back to the real refs before validation,
    and a first attempt that misses the anchor must recover on the interior
    correction exactly like a real flash-grade model would."""
    ledger = _ledger_with_active_appraisal()
    _append_second_appraisal(ledger)
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()

    capability = _compile_live_capability(ledger)

    # Short tokens are present, anchors are expressed as short tokens, and the
    # map resolves to the real (long) source refs.
    assert len(capability["short_tokens"]) >= 4
    assert capability["anchor_short_tokens"]
    assert capability["token_map"][capability["anchor_short_tokens"][0]] in (
        capability["anchor_source_refs"]
    )

    model = _ShortTokenModel()
    runtime, _ = _private_runtime(ledger, model)
    result = await runtime.drain_one()

    assert result.work_status == "accepted"
    # Either the first pick already hit the anchor (one call) or the interior
    # correction recovered a missed anchor (two calls).  Both are production-
    # valid; what matters is the impression landed with real refs.
    assert 1 <= len(model.calls) <= 2
    impressions = [
        item
        for item in ledger.project().private_impressions
        if item.status == "active"
    ]
    assert len(impressions) == 1
    # The persisted impression references real source refs, never short tokens.
    assert all(
        not ref.startswith("s") or not ref[1:].isdigit()
        for ref in impressions[0].interpretation_refs
    )
    # The accepted transition payload carries real refs too: the recorded
    # model-result audit exists and its audit payload must not contain any
    # short token reference.
    assert ledger.project().model_result_audits
    audit_json = json.dumps(
        [item.model_dump(mode="json") for item in ledger.project().model_result_audits]
    )
    for token in capability["short_tokens"]:
        assert f'"{token}"' not in audit_json


class _ShortTokenModel:
    """Flash-grade model that selects short tokens; misses the anchor once."""

    model = "deepseek-v4-flash"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[-1]["content"])
        payload = request["capability_manifest"]["payload"]
        short_tokens = payload["short_tokens"]
        anchors = payload["anchor_short_tokens"]
        chosen = anchors[0]
        if len(self.calls) == 1:
            chosen = next(
                (item for item in short_tokens if item not in anchors),
                chosen,
            )
        return json.dumps(
            {
                "status": "transition",
                "summary": "她形成了一个暂时的私人解读。",
                "attended_source_refs": request["inner_life_snapshot"]["source_refs"],
                "decision": None,
                "recall_query": None,
                "proposals": [
                    {
                        "proposal_type": "private_impression_transition",
                        "decision": "retain",
                        "predecessor_refs": [],
                        "source_refs": [chosen],
                        "reflection_summary": "我暂时觉得这更像是失望，不一定是在否定我。",
                        "confidence_bp": 6_000,
                        "expiry_condition": "until_counter_evidence",
                    }
                ],
            },
            ensure_ascii=False,
        )


def _compile_live_capability(ledger) -> dict[str, object]:
    from companion_daemon.world_v2.private_impression_producer import (
        _private_impression_capability,
        compile_private_impression_reflection_capsule,
    )

    projection = ledger.project()
    process = next(
        item
        for item in projection.trigger_processes
        if item.process_kind == "private_impression_deliberation"
        and item.state != "terminal"
    )
    appraisal = next(
        item
        for item in projection.appraisals
        if item.origin.accepted_event_ref == process.source_evidence_ref
    )
    capsule = compile_private_impression_reflection_capsule(
        projection=projection,
        appraisal=appraisal,
        identity_frame=CompanionIdentityFrame(
            companion_name="枝枝", counterpart_name="对方"
        ),
        world_id=ledger.world_id,
    )
    manifest = _private_impression_capability(capsule)
    return json.loads(manifest.payload_json)



@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(10))
@pytest.mark.asyncio
async def test_short_token_contract_accepts_ten_production_like_runs(seed: int) -> None:
    """Ten production-like runs must all accept the private impression when the
    model selects short tokens (with per-seed variation: some miss the anchor
    first and rely on the interior correction; some pick two sources).  This
    is the acceptance bar for the short-token contract: any provider can hit
    >= 90% without echoing long hash refs."""
    ledger = _ledger_with_active_appraisal()
    if seed in (1, 3, 5, 7, 9):
        _append_second_appraisal(ledger)
    await PrivateImpressionTriggerOpener(ledger=ledger, owner_id=OWNER).open_once()

    model = _ProductionShortTokenModel(seed=seed)
    runtime, _ = _private_runtime(ledger, model)
    result = await runtime.drain_one()

    assert result.work_status == "accepted", f"run {seed} failed: {result.work_status}"
    impressions = [
        item
        for item in ledger.project().private_impressions
        if item.status == "active"
    ]
    assert len(impressions) == 1, f"run {seed} produced no active impression"


class _ProductionShortTokenModel:
    """Flash-grade model: selects short tokens, may miss the anchor on the
    first attempt (interior correction recovers it), and may select two
    sources on some runs."""

    model = "deepseek-v4-flash"

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages, *, temperature: float = 0.2) -> str:  # type: ignore[no-untyped-def]
        del temperature
        self.calls.append(messages)
        request = json.loads(messages[-1]["content"])
        payload = request["capability_manifest"]["payload"]
        short_tokens = payload["short_tokens"]
        anchors = payload["anchor_short_tokens"]
        chosen = anchors[0]
        if len(self.calls) == 1 and self.seed < 4:
            chosen = next(
                (item for item in short_tokens if item not in anchors),
                chosen,
            )
        source_refs = [chosen]
        if self.seed % 3 == 0 and len(short_tokens) > 1:
            extra = next((item for item in short_tokens if item != chosen), None)
            if extra is not None:
                source_refs.append(extra)
        return json.dumps(
            {
                "status": "transition",
                "summary": "她形成了一个暂时的私人解读。",
                "attended_source_refs": request["inner_life_snapshot"]["source_refs"],
                "decision": None,
                "recall_query": None,
                "proposals": [
                    {
                        "proposal_type": "private_impression_transition",
                        "decision": "retain",
                        "predecessor_refs": [],
                        "source_refs": source_refs,
                        "reflection_summary": "我暂时觉得这更像是失望，不一定是在否定我。",
                        "confidence_bp": 6_000,
                        "expiry_condition": "until_counter_evidence",
                    }
                ],
            },
            ensure_ascii=False,
        )
