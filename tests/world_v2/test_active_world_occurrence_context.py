from __future__ import annotations

from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.context_resolver import query_from_projection
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.expression_draft import (
    world_claim_source_refs_by_scope,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.life_aftermath_runtime import LifeAftermathRuntime
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    StoredLifeContent,
    life_content_payload_hash,
)
from companion_daemon.world_v2.life_events import NpcRegisteredPayload
from companion_daemon.world_v2.life_development_runtime import (
    LifeDevelopmentProposalReader,
)
from companion_daemon.world_v2.ledger_context_resolver import (
    ContextRelevanceScope,
    context_capsule_compiler_from_ledger,
)
from companion_daemon.world_v2.model_facing_context import (
    compact_chat_model_facing_context,
)
from companion_daemon.world_v2.occurrence_content_coordinator import (
    OccurrenceContentCoordinator,
)
from companion_daemon.world_v2.recall_corpus import RecallCorpusCompiler
from companion_daemon.world_v2.recall_index import RecallCursor
from companion_daemon.world_v2.schemas import (
    EvidenceRef,
    NpcProjection,
    ProjectionCursor,
    WorldEvent,
)
from test_life_development_runtime import (
    NOW,
    OWNER,
    WORLD_ID,
    _commit_at_head,
    _location_bound_world_draft,
    _location_capability,
    _hash_json,
    _runtime,
    _seed_clock,
    _StaticManifestCompiler,
    _SequenceModel,
    _source_closure_review,
)

NPC_REF = "npc:lin"


class _EntityManifestCompiler:
    def __init__(self, *, wake: WorldEvent, entity_refs: tuple[str, ...]) -> None:
        self._base = _StaticManifestCompiler(
            wake=wake,
            location_capability=_location_capability(),
        )
        self._entity_refs = tuple(sorted(set(entity_refs)))

    def compile(self, *, projection, wake, capsule):  # type: ignore[no-untyped-def]
        return self._base.compile(
            projection=projection,
            wake=wake,
            capsule=capsule,
        ).model_copy(update={"entity_refs": self._entity_refs})


def _register_npc(*, ledger: WorldLedger, wake: WorldEvent) -> None:
    wake_ref = next(
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_id == wake.event_id
    )
    payload = NpcRegisteredPayload(
        change_id="change:npc:lin",
        transition_id="transition:npc:lin",
        expected_entity_revision=0,
        evidence_refs=(
            EvidenceRef(
                ref_id=wake_ref.event_id,
                evidence_type="committed_world_event",
                claim_purpose="current_fact",
                source_world_revision=wake_ref.world_revision,
                immutable_hash=wake_ref.payload_hash,
            ),
        ),
        policy_refs=("policy:npc-test.1",),
        npc=NpcProjection(
            npc_id="lin",
            entity_revision=1,
            stable_identity_ref="identity:npc:lin",
            privacy_class="personal",
        ),
    )
    payload_json = payload.model_dump(mode="json")
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:npc:lin:registered",
        world_id=ledger.world_id,
        event_type="NpcRegistered",
        logical_time=wake.logical_time,
        created_at=wake.created_at,
        actor="worker:test",
        source="test",
        trace_id=wake.trace_id,
        causation_id=wake.event_id,
        correlation_id=wake.correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type="NpcRegistered",
                world_id=ledger.world_id,
                payload=payload_json,
            )
            or "npc:lin:registered"
        ),
        payload=payload_json,
    )
    _commit_at_head(ledger, event)


async def _source_closed_active_occurrence(
    *,
    privacy_class: str = "personal",
    source_closed: bool = True,
    entity_refs: tuple[str, ...] = (),
) -> tuple[WorldLedger, object]:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    if NPC_REF in entity_refs:
        _register_npc(ledger=ledger, wake=wake)
    capability = _location_capability()
    premise = "暑假午后，校园院子里突然落下一阵很急的冰雹。"
    draft = json.loads(
        _location_bound_world_draft(
            wake=wake,
            capability=capability,
            timing={"mode": "now", "duration_minutes": 20},
            privacy_class=privacy_class,
        )
    )
    draft["premise"] = premise
    draft["entity_refs"] = list(entity_refs)
    source_reviewer = _SequenceModel(
        model="test-source-reviewer",
        outputs=(_source_closure_review(decision="supported"),),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=_SequenceModel(
            model="test-world-author",
            outputs=(json.dumps(draft, ensure_ascii=False),),
        ),
        source_closure_reviewer=source_reviewer,
        character_model=_SequenceModel(model="test-character-model", outputs=()),
        location_capability=capability,
    )
    if entity_refs:
        runtime._manifest_compiler = _EntityManifestCompiler(  # noqa: SLF001
            wake=wake,
            entity_refs=entity_refs,
        )

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:active-world-context",
        correlation_id="correlation:active-world-context",
    )

    assert result.status == "occurrence_committed"
    assert ledger.project().world_occurrences[0].status == "active"
    if not source_closed:
        ledger = _replay_as_legacy_without_source_closure(ledger)
    return ledger, store


def _replay_as_legacy_without_source_closure(ledger: WorldLedger) -> WorldLedger:
    """Rebuild the exact occurrence as the historical pre-review `.3` format."""

    evidence = ledger.export_replay_evidence()
    legacy = WorldLedger.in_memory(world_id=ledger.world_id)
    for commit in evidence.commits:
        events: list[WorldEvent] = []
        for item in evidence.events:
            if item.commit_id != commit.commit_id:
                continue
            event = item.event
            payload = event.payload()
            if (
                event.event_type == "ProposalRecorded"
                and payload.get("possibility_authority_version")
                == "life-development-possibility.7"
            ):
                payload["possibility_authority_version"] = (
                    "life-development-possibility.3"
                )
                for key in tuple(payload):
                    if key.startswith("world_author_source_closure_") or key.startswith(
                        "world_author_novel_origin_"
                    ):
                        payload.pop(key)
                event = WorldEvent.from_payload(
                    payload=payload,
                    **event.model_dump(
                        mode="python",
                        exclude={"payload_json", "payload_hash"},
                    ),
                )
            events.append(event)
        projection = legacy.project()
        legacy.commit_at_cursor(
            events,
            expected_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
            commit_id=commit.commit_id,
        )
    return legacy


@pytest.mark.asyncio
async def test_source_closed_active_occurrence_reaches_current_self_without_future_outcomes() -> None:
    ledger, store = await _source_closed_active_occurrence()
    projection = ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(actor_ref=OWNER),
        life_content_store=store,
    ).compile(
        query_from_projection(
            projection,
            actor_ref=OWNER,
            trigger_ref="event:current-conversation",
        )
    )

    context = json.loads(capsule.model_content_json)
    active = next(
        item
        for item in context["slices"]["world_life"]["items"]
        if item["value"].get("context_kind") == "active_world_occurrence"
    )
    value = active["value"]
    assert value == {
        "activated_at": "2026-07-29T02:00:00Z",
        "context_kind": "active_world_occurrence",
        "location_ref": "location:campus-courtyard",
        "occurrence_entity_revision": 2,
        "occurrence_id": value["occurrence_id"],
        "participant_refs": [OWNER],
        "premise": {
            "content_payload_hash": value["premise"]["content_payload_hash"],
            "content_ref": value["premise"]["content_ref"],
            "text": "暑假午后，校园院子里突然落下一阵很急的冰雹。",
            "truncated": False,
        },
        "privacy_class": "personal",
        "proposal_source": value["proposal_source"],
        "source_bindings": value["source_bindings"],
        "status": "active",
        "time_window": {
            "closes_at": "2026-07-29T02:20:00Z",
            "opens_at": "2026-07-29T02:00:00Z",
        },
    }
    assert (
        value["proposal_source"]["authority_event_ref"]
        == projection.world_occurrences[0].trigger_ref
    )
    assert {
        binding["authority_event_ref"] for binding in value["source_bindings"]
    } == {
        next(
            item.event_id
            for item in projection.committed_world_event_refs
            if item.event_type == "WorldOccurrenceCommitted"
        ),
        next(
            item.event_id
            for item in projection.committed_world_event_refs
            if item.event_type == "WorldOccurrenceActivated"
        ),
    }
    encoded = json.dumps(value, ensure_ascii=False)
    assert "candidate_outcome" not in encoded
    assert "result_payload" not in encoded
    assert "变化留下了一些影响" not in encoded

    compact = json.loads(compact_chat_model_facing_context(capsule.model_content_json))
    current_life = compact["current_self_state"]["recent_self_experiences"]["items"][0]
    assert current_life["source_ref"] == value["occurrence_id"]
    assert current_life["status"] == "active"
    assert current_life["premise"]["text"] == value["premise"]["text"]
    assert "candidate_outcomes" not in current_life
    compact_active = next(
        item["value"]
        for item in compact["slices"]["world_life"]["items"]
        if item["value"].get("context_kind") == "active_world_occurrence"
    )
    assert "proposal_source" not in compact_active
    assert "source_bindings" not in compact_active

    allowed = world_claim_source_refs_by_scope(context=compact)
    assert value["occurrence_id"] in allowed["current_world"]
    assert value["occurrence_id"] not in allowed["past_world"]


@pytest.mark.asyncio
async def test_active_reader_retains_historical_v5_review_identities() -> None:
    ledger, _store = await _source_closed_active_occurrence()
    occurrence = ledger.project().world_occurrences[0]
    proposal_event = ledger.lookup_event_commit(occurrence.trigger_ref)[0]
    proposal = proposal_event.payload()
    proposal["possibility_authority_version"] = "life-development-possibility.5"
    raw_hash = proposal["world_author_raw_output_hash"]
    manifest_hash = proposal["capability_manifest_hash"]
    source_deliberation = proposal["world_author_source_closure_deliberation"]
    source_deliberation["decision_subject_hash"] = _hash_json(
        {
            "capability_manifest_hash": manifest_hash,
            "world_author_raw_output_hash": raw_hash,
        }
    )
    proposal["world_author_source_closure_deliberation_hash"] = _hash_json(
        source_deliberation
    )
    novel_deliberation = proposal["world_author_novel_origin_deliberation"]
    novel_deliberation["decision_subject_hash"] = _hash_json(
        {
            "contract": "life-development-novel-origin-review.2",
            "capability_manifest_hash": manifest_hash,
            "world_author_raw_output_hash": raw_hash,
        }
    )
    proposal["world_author_novel_origin_deliberation_hash"] = _hash_json(
        novel_deliberation
    )

    LifeDevelopmentProposalReader._validate_active_source_closure(  # noqa: SLF001
        proposal=proposal,
        possibility_version="life-development-possibility.5",
        proposal_event=proposal_event,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ("legacy_without_source_closure", "sidecar_hash_mismatch", "withhold"),
)
async def test_active_occurrence_authority_or_privacy_failure_is_unavailable_not_fatal(
    failure: str,
) -> None:
    ledger, store = await _source_closed_active_occurrence(
        privacy_class="withhold" if failure == "withhold" else "personal",
        source_closed=failure != "legacy_without_source_closure",
    )
    if failure == "sidecar_hash_mismatch":
        occurrence = ledger.project().world_occurrences[0]
        proposal = ledger.lookup_event_commit(occurrence.trigger_ref)[0].payload()
        premise = proposal["possibility_authority"]["premise"]
        replacement = "这不是提案里冻结的前提正文。"
        mismatched = InMemoryImmutableLifeContentStore()
        mismatched.put_if_absent(
            StoredLifeContent(
                content_ref=premise["content_ref"],
                content_kind="outcome_candidate",
                content_payload_hash=life_content_payload_hash(replacement),
                text=replacement,
            )
        )
        store = mismatched

    projection = ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(actor_ref=OWNER),
        life_content_store=store,
    ).compile(
        query_from_projection(
            projection,
            actor_ref=OWNER,
            trigger_ref=f"event:failure:{failure}",
        )
    )

    context = json.loads(capsule.model_content_json)
    world_life = context["slices"]["world_life"]
    items = (
        world_life["items"]
        if world_life.get("availability") == "available"
        else []
    )
    assert not any(
        item["value"].get("context_kind") == "active_world_occurrence"
        for item in items
    )


@pytest.mark.asyncio
async def test_active_occurrence_stays_out_of_long_term_recall_corpus() -> None:
    class RecordingRecall:
        def __init__(self) -> None:
            self.sources = None

        def refresh(self, **kwargs) -> None:
            self.sources = kwargs["sources"]

        def discard(self, *_args, **_kwargs) -> None:
            raise AssertionError("valid active Context must not degrade Recall")

    ledger, store = await _source_closed_active_occurrence()
    recall = RecordingRecall()
    projection = ledger.project()
    context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(actor_ref=OWNER),
        life_content_store=store,
        recall_coordinator=recall,
    ).compile(
        query_from_projection(
            projection,
            actor_ref=OWNER,
            trigger_ref="event:active-not-memory",
        )
    )

    assert recall.sources is not None
    assert recall.sources.world_life == ()
    assert recall.sources.recent_experiences == ()


@pytest.mark.asyncio
async def test_companion_and_npc_lived_experience_enters_context_and_recall() -> None:
    class RecordingRecall:
        def __init__(self) -> None:
            self.sources = None

        def refresh(self, **kwargs) -> None:
            self.sources = kwargs["sources"]

        def discard(self, *_args, **_kwargs) -> None:
            raise AssertionError("valid shared experience must not degrade Recall")

    ledger, store = await _source_closed_active_occurrence(
        entity_refs=(NPC_REF,),
    )
    settle_wake = _seed_clock(
        ledger,
        event_id="event:clock:settle-shared-experience",
        logical_time=NOW + timedelta(minutes=10),
        logical_time_from=NOW,
    )
    aftermath = LifeAftermathRuntime(
        ledger=ledger,
        catalog=SimpleNamespace(),
        occurrence_content=OccurrenceContentCoordinator(
            ledger=ledger,
            store=store,
        ),
        content_store=store,
        owner_actor_ref=OWNER,
        capsule_compiler=SimpleNamespace(),
    )
    settled = await aftermath.advance_once(
        wake_event_ref=settle_wake.event_id,
        trace_id="trace:settle-shared-experience",
        correlation_id="correlation:settle-shared-experience",
    )
    assert settled.status == "settled"
    experience = ledger.project().experiences[0]
    assert experience.values.participant_refs == (OWNER, NPC_REF)

    recall = RecordingRecall()
    projection = ledger.project()
    capsule = context_capsule_compiler_from_ledger(
        ledger=ledger,
        relevance_scope=ContextRelevanceScope(actor_ref=OWNER),
        life_content_store=store,
        recall_coordinator=recall,
    ).compile(
        query_from_projection(
            projection,
            actor_ref=OWNER,
            trigger_ref="event:shared-experience-context",
        )
    )
    context = json.loads(capsule.model_content_json)
    recent = context["slices"]["recent_experiences"]["items"]
    assert len(recent) == 1
    assert recent[0]["value"]["values"]["participant_refs"] == [OWNER, NPC_REF]
    assert recall.sources is not None
    assert len(recall.sources.recent_experiences) == 1

    documents = RecallCorpusCompiler().compile(
        cursor=RecallCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
        actor_ref=OWNER,
        subject_refs=(OWNER,),
        sources=recall.sources,
    )
    recalled = next(
        item
        for item in documents
        if item.source_item_ref == experience.experience_id
    )
    assert recalled.subject_refs == (OWNER, NPC_REF)
