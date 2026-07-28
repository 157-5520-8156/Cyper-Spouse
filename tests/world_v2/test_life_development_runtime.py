from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from companion_daemon.world_v2.errors import ConcurrencyConflict
from companion_daemon.world_v2.batch_invariants import validate_commit_batch
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.life_content_store import (
    InMemoryImmutableLifeContentStore,
    life_content_payload_hash,
)
from companion_daemon.world_v2.life_development_draft import (
    LifeDevelopmentCapabilityManifest,
    LifeDevelopmentDraftError,
    LifeDevelopmentLocationCapability,
    parse_world_author_draft,
)
from companion_daemon.world_v2.life_development_runtime import (
    LifeDevelopmentProposalReader,
    LifeDevelopmentRuntime,
)
from companion_daemon.world_v2.life_events import WorldOccurrenceCommittedPayload
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.proposal_audit_schemas import (
    RecordedModelResultAudit,
)
from companion_daemon.world_v2.schemas import (
    ClockObservation,
    DueWindow,
    EvidenceRef,
    ProjectionCursor,
    WorldEvent,
    WorldOccurrenceProjection,
)


WORLD_ID = "world:life-development"
OWNER = "actor:companion"
NOW = datetime(2026, 7, 29, 2, 0, tzinfo=UTC)


class _SequenceModel:
    def __init__(self, *, model: str, outputs: tuple[object, ...]) -> None:
        self.model = model
        self._outputs = list(outputs)
        self.calls = 0
        self.messages: list[list[dict[str, str]]] = []

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
    ) -> str:
        del temperature
        self.calls += 1
        self.messages.append(messages)
        output = self._outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        assert isinstance(output, str)
        return output


class _PinnedCapsuleCompiler:
    def __init__(self, *, ledger: WorldLedger) -> None:
        self._ledger = ledger

    def compile_for_deliberation(self, _query):  # type: ignore[no-untyped-def]
        projection = self._ledger.project()
        context = {
            "current_self_state": {
                "character_core": {"values": ["autonomy"]},
                "personality_state": {"availability": "present"},
            },
            "active_affect": [{"dimension": "irritation", "intensity_bp": 3200}],
            "recent_world_life": [],
        }
        capsule = SimpleNamespace(
            capsule_id="1" * 64,
            snapshot_hash=projection.semantic_hash,
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
            logical_time=projection.logical_time,
            model_content_json=json.dumps(context, ensure_ascii=False),
        )
        return SimpleNamespace(capsule=capsule)


def _commit_at_head(ledger: WorldLedger, event: WorldEvent) -> None:
    projection = ledger.project()
    ledger.commit_at_cursor(
        (event,),
        expected_cursor=ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        ),
    )


def _replace_event_payload(
    event: WorldEvent,
    *,
    payload: dict[str, object],
) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version=event.schema_version,
        event_id=event.event_id,
        world_id=event.world_id,
        event_type=event.event_type,
        logical_time=event.logical_time,
        created_at=event.created_at,
        actor=event.actor,
        source=event.source,
        trace_id=event.trace_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
        idempotency_key=(
            domain_idempotency_key(
                event_type=event.event_type,
                world_id=event.world_id,
                payload=payload,
            )
            or event.idempotency_key
        ),
        payload=payload,
    )


def _seed_clock(
    ledger: WorldLedger,
    *,
    event_id: str = "event:clock:life-development",
    logical_time: datetime = NOW,
    logical_time_from: datetime | None = None,
) -> WorldEvent:
    logical_time_from = logical_time_from or logical_time - timedelta(minutes=10)
    clock = ClockObservation(
        schema_version="world-v2.1",
        tick_id="life-development",
        world_id=ledger.world_id,
        logical_time=logical_time,
        created_at=logical_time,
        trace_id="trace:life-development",
        causation_id="scheduler:life-development",
        correlation_id="correlation:life-development",
        logical_time_from=logical_time_from,
        logical_time_to=logical_time,
        reason="test",
    )
    event = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=ledger.world_id,
        event_type="ClockAdvanced",
        logical_time=logical_time,
        created_at=logical_time,
        actor="system:clock",
        source="test",
        trace_id=clock.trace_id,
        causation_id=clock.causation_id,
        correlation_id=clock.correlation_id,
        idempotency_key="clock:life-development:" + event_id,
        payload=clock.model_dump(mode="json"),
    )
    _commit_at_head(ledger, event)
    return event


def _projection_cursor(ledger: WorldLedger) -> ProjectionCursor:
    projection = ledger.project()
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


def _hash_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _location_capability(
    *,
    authority_refs: tuple[str, ...] = ("policy:test-location",),
) -> LifeDevelopmentLocationCapability:
    return LifeDevelopmentLocationCapability(
        location_ref="location:campus-courtyard",
        privacy_class="shareable",
        availability_kind="reviewed_schedule",
        timezone_name="Asia/Shanghai",
        local_windows=("00:00-00:00",),
        weekdays=(0, 1, 2, 3, 4, 5, 6),
        authority_refs=authority_refs,
    )


def _location_bound_world_draft(
    *,
    wake: WorldEvent,
    capability: LifeDevelopmentLocationCapability,
    timing: dict[str, object],
    privacy_class: str,
    dynamic_direction: dict[str, object] | None = None,
    causal_authority: str = "world_contingency",
    outcome_resolution_authority: str = "world_contingency",
) -> str:
    return json.dumps(
        {
            "decision": "propose",
            "causal_authority": causal_authority,
            "outcome_resolution_authority": outcome_resolution_authority,
            "premise_scope": "external_opportunity",
            "premise": "这个地点出现了一次没有预先写进剧本的变化。",
            "premise_claim_refs": ["local:claim:location-change"],
            "claim_declarations": [
                {
                    "claim_id": "local:claim:location-change",
                    "summary": "地点中发生一次开放生成的环境变化。",
                    "scope": "novel_world_generation",
                    "subject_scope": "world_environment",
                    "source_refs": [],
                }
            ],
            "timing": timing,
            "anchor_refs": [wake.event_id],
            "location_ref": capability.location_ref,
            "location_capability_ref": capability.capability_ref,
            "entity_refs": [],
            "privacy_class": privacy_class,
            "outcomes": [
                {
                    "text": "变化留下了一些影响。",
                    "privacy_class": privacy_class,
                    "relative_plausibility_weight": 1,
                    "claim_refs": ["local:claim:location-change"],
                    "provisional_npcs": [],
                    "dynamic_life_direction": dynamic_direction,
                },
                {
                    "text": "变化很快过去了。",
                    "privacy_class": privacy_class,
                    "relative_plausibility_weight": 1,
                    "claim_refs": ["local:claim:location-change"],
                    "provisional_npcs": [],
                    "dynamic_life_direction": None,
                },
            ],
        },
        ensure_ascii=False,
    )


def _manifest(
    wake: WorldEvent,
    *,
    pinned_cursor: ProjectionCursor,
    location_capability: LifeDevelopmentLocationCapability | None = None,
) -> LifeDevelopmentCapabilityManifest:
    return LifeDevelopmentCapabilityManifest(
        version="life-development-capability.test.1",
        pinned_cursor=pinned_cursor,
        anchor_refs=(wake.event_id,),
        grounding_refs=(wake.event_id,),
        location_capabilities=(location_capability or _location_capability(),),
        entity_refs=(),
        max_future_days=30,
        max_window_minutes=12 * 60,
    )


class _StaticManifestCompiler:
    def __init__(
        self,
        *,
        wake: WorldEvent,
        location_capability: LifeDevelopmentLocationCapability | None = None,
    ) -> None:
        self._wake = wake
        self._location_capability = location_capability

    def compile(self, *, projection, wake, capsule):  # type: ignore[no-untyped-def]
        del wake, capsule
        return _manifest(
            self._wake,
            pinned_cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
            location_capability=self._location_capability,
        )


def test_location_capability_enforces_privacy_and_complete_local_window() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    with pytest.raises(
        ValueError,
        match="current presence requires one ordered, finite authority interval",
    ):
        LifeDevelopmentLocationCapability(
            location_ref="location:unbounded-presence",
            privacy_class="personal",
            availability_kind="current_presence",
            timezone_name="Asia/Shanghai",
            available_from=NOW,
            authority_refs=("event:location:unbounded-presence",),
        )

    private_presence = LifeDevelopmentLocationCapability(
        location_ref="location:private-room",
        privacy_class="private",
        availability_kind="current_presence",
        timezone_name="Asia/Shanghai",
        available_from=NOW - timedelta(hours=1),
        available_to=NOW + timedelta(minutes=5),
        authority_refs=("event:location:private-room",),
    )
    private_manifest = LifeDevelopmentCapabilityManifest(
        version="life-development-capability.test.location",
        pinned_cursor=_projection_cursor(ledger),
        anchor_refs=(wake.event_id,),
        grounding_refs=(wake.event_id,),
        location_capabilities=(private_presence,),
        max_future_days=30,
        max_window_minutes=12 * 60,
    )

    with pytest.raises(
        LifeDevelopmentDraftError,
        match="location_privacy_weakened",
    ):
        parse_world_author_draft(
            raw=_location_bound_world_draft(
                wake=wake,
                capability=private_presence,
                timing={"mode": "now", "duration_minutes": 5},
                privacy_class="public",
            ),
            manifest=private_manifest,
            logical_time=NOW,
        )

    with pytest.raises(
        LifeDevelopmentDraftError,
        match="unsupported_location_window",
    ):
        parse_world_author_draft(
            raw=_location_bound_world_draft(
                wake=wake,
                capability=private_presence,
                timing={"mode": "now", "duration_minutes": 10},
                privacy_class="private",
            ),
            manifest=private_manifest,
            logical_time=NOW,
        )

    scheduled = LifeDevelopmentLocationCapability(
        location_ref="location:library",
        privacy_class="shareable",
        availability_kind="reviewed_schedule",
        timezone_name="Asia/Shanghai",
        local_windows=("08:00-21:30",),
        weekdays=(2,),
        authority_refs=("policy:reviewed-library",),
    )
    scheduled_manifest = private_manifest.model_copy(update={"location_capabilities": (scheduled,)})
    with pytest.raises(
        LifeDevelopmentDraftError,
        match="unsupported_location_window",
    ):
        parse_world_author_draft(
            raw=_location_bound_world_draft(
                wake=wake,
                capability=scheduled,
                timing={
                    "mode": "later",
                    "opens_at": (NOW + timedelta(hours=11)).isoformat(),
                    "closes_at": (NOW + timedelta(hours=12)).isoformat(),
                },
                privacy_class="shareable",
            ),
            manifest=scheduled_manifest,
            logical_time=NOW,
        )


def test_world_contingency_cannot_install_a_character_life_direction() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    capability = _location_capability()
    manifest = _manifest(wake, pinned_cursor=_projection_cursor(ledger))

    with pytest.raises(LifeDevelopmentDraftError, match="invalid_shape"):
        parse_world_author_draft(
            raw=_location_bound_world_draft(
                wake=wake,
                capability=capability,
                timing={"mode": "now", "duration_minutes": 20},
                privacy_class="personal",
                dynamic_direction={
                    "summary": "她决定今后都围绕这件事生活。",
                    "narrative_tags": ["narrative:imposed-direction"],
                    "duration_days": 30,
                    "privacy_class": "personal",
                },
            ),
            manifest=manifest,
            logical_time=NOW,
        )


def test_world_contingency_cannot_delegate_its_outcome_to_the_character() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    capability = _location_capability()
    manifest = _manifest(wake, pinned_cursor=_projection_cursor(ledger))

    with pytest.raises(LifeDevelopmentDraftError, match="invalid_shape"):
        parse_world_author_draft(
            raw=_location_bound_world_draft(
                wake=wake,
                capability=capability,
                timing={"mode": "now", "duration_minutes": 20},
                privacy_class="personal",
                outcome_resolution_authority="character_choice",
            ),
            manifest=manifest,
            logical_time=NOW,
        )


def _runtime(
    *,
    ledger: WorldLedger,
    wake: WorldEvent,
    world_author: _SequenceModel,
    character_model: _SequenceModel,
    store: InMemoryImmutableLifeContentStore | None = None,
    location_capability: LifeDevelopmentLocationCapability | None = None,
) -> tuple[LifeDevelopmentRuntime, InMemoryImmutableLifeContentStore]:
    content_store = store or InMemoryImmutableLifeContentStore()
    return (
        LifeDevelopmentRuntime(
            ledger=ledger,
            content_store=content_store,
            world_author=world_author,
            character_model=character_model,
            capsule_compiler=_PinnedCapsuleCompiler(ledger=ledger),
            capability_manifest_compiler=_StaticManifestCompiler(
                wake=wake,
                location_capability=location_capability,
            ),
            owner_actor_ref=OWNER,
        ),
        content_store,
    )


def _location_bound_occurrence_batch(
    *,
    capability: LifeDevelopmentLocationCapability,
    effect_window: DueWindow,
    effect_privacy: str = "personal",
    policy_refs: tuple[str, ...] = ("policy:life-development-v1",),
    proposal_capability_ref: str | None = None,
    omit_proposal_location: bool = False,
    manifest_capability: LifeDevelopmentLocationCapability | None = None,
) -> tuple[WorldEvent, WorldEvent]:
    proposal_id = "proposal:life-development:location-bound-test"
    proposal_event_id = "event:life-development:proposal:location-bound-test"
    occurrence_id = "occurrence:life-development:location-bound-test"
    possibility = {
        "timing": {
            "mode": "later",
            "opens_at": (NOW + timedelta(hours=2)).isoformat(),
            "closes_at": (NOW + timedelta(hours=4)).isoformat(),
        },
        "location_ref": (None if omit_proposal_location else capability.location_ref),
        "location_capability_ref": (
            None if omit_proposal_location else proposal_capability_ref or capability.capability_ref
        ),
        "location_capability": (
            None
            if omit_proposal_location
            else capability.model_dump(
                mode="json",
                exclude={"capability_ref"},
            )
        ),
        "privacy_class": "personal",
    }
    manifest = LifeDevelopmentCapabilityManifest(
        version="life-development-capability.batch-test.1",
        pinned_cursor=ProjectionCursor(
            world_revision=1,
            deliberation_revision=0,
            ledger_sequence=1,
        ),
        location_capabilities=(manifest_capability or capability,),
        max_future_days=30,
        max_window_minutes=12 * 60,
    )
    proposal = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=proposal_event_id,
        world_id=WORLD_ID,
        event_type="ProposalRecorded",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:world-v2:life-development",
        source="world-v2:life-development",
        trace_id="trace:location-bound-batch",
        causation_id="event:clock:location-bound-batch",
        correlation_id="correlation:location-bound-batch",
        idempotency_key="proposal:location-bound-batch",
        payload={
            "proposal_id": proposal_id,
            "proposal_kind": "life_development",
            "possibility_authority_version": "life-development-possibility.2",
            "possibility_authority": possibility,
            "possibility_authority_hash": _hash_json(possibility),
            "capability_manifest_version": manifest.version,
            "capability_manifest_hash": manifest.manifest_hash,
            "world_author_deliberation": {
                "capability_manifest": manifest.model_dump(
                    mode="json",
                    exclude_computed_fields=True,
                ),
            },
            "effect_kind": "world_occurrence",
            "effect_ref": occurrence_id,
        },
    )
    evidence = (
        EvidenceRef(
            ref_id="event:clock:location-bound-batch",
            evidence_type="committed_world_event",
            claim_purpose="future_plan",
            source_world_revision=1,
            immutable_hash="a" * 64,
        ),
    )
    occurrence = WorldOccurrenceProjection(
        occurrence_id=occurrence_id,
        entity_revision=1,
        trigger_ref=proposal.event_id,
        participant_refs=(OWNER,),
        location_ref=capability.location_ref,
        time_window=effect_window,
        candidate_outcome_refs=("candidate:location-bound-batch",),
        visibility=effect_privacy,
        status="committed",
    )
    payload = WorldOccurrenceCommittedPayload(
        change_id="change:location-bound-batch",
        transition_id="transition:location-bound-batch",
        expected_entity_revision=0,
        evidence_refs=evidence,
        policy_refs=policy_refs,
        occurrence=occurrence,
    ).model_dump(mode="json")
    effect = WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id="event:life-development:occurrence:location-bound-test",
        world_id=WORLD_ID,
        event_type="WorldOccurrenceCommitted",
        logical_time=NOW,
        created_at=NOW,
        actor="worker:world-v2:life-development",
        source="world-v2:life-development",
        trace_id=proposal.trace_id,
        causation_id=proposal.event_id,
        correlation_id=proposal.correlation_id,
        idempotency_key="occurrence:location-bound-batch",
        payload=payload,
    )
    return proposal, effect


def test_batch_rejects_location_effect_without_its_capability_authority() -> None:
    capability = _location_capability()
    events = _location_bound_occurrence_batch(
        capability=capability,
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
    )

    with pytest.raises(ValueError, match="location capability authority"):
        validate_commit_batch(events, expected_world_revision=1)


def test_batch_rejects_a_bare_location_effect_without_frozen_capability() -> None:
    capability = _location_capability()
    events = _location_bound_occurrence_batch(
        capability=capability,
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
        omit_proposal_location=True,
    )

    with pytest.raises(ValueError, match="bare location effect"):
        validate_commit_batch(events, expected_world_revision=1)


def test_batch_rejects_location_capability_snapshot_with_a_different_ref() -> None:
    capability = _location_capability()
    events = _location_bound_occurrence_batch(
        capability=capability,
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
        policy_refs=(
            "policy:life-development-v1",
            "policy:test-location",
        ),
        proposal_capability_ref="location-capability:" + "f" * 64,
    )

    with pytest.raises(ValueError, match="snapshot does not match its ref"):
        validate_commit_batch(events, expected_world_revision=1)


def test_batch_rejects_location_capability_absent_from_pinned_manifest() -> None:
    capability = _location_capability()
    events = _location_bound_occurrence_batch(
        capability=capability,
        manifest_capability=_location_capability(
            authority_refs=("policy:different-location-authority",),
        ),
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
        policy_refs=(
            "policy:life-development-v1",
            "policy:test-location",
        ),
    )

    with pytest.raises(ValueError, match="absent from its pinned manifest"):
        validate_commit_batch(events, expected_world_revision=1)


def test_batch_rejects_location_proposal_outside_its_capability_window() -> None:
    capability = LifeDevelopmentLocationCapability(
        location_ref="location:campus-courtyard",
        privacy_class="shareable",
        availability_kind="accepted_plan",
        timezone_name="Asia/Shanghai",
        available_from=NOW + timedelta(hours=2),
        available_to=NOW + timedelta(hours=3),
        now_allowed=False,
        authority_refs=("policy:test-location",),
    )
    events = _location_bound_occurrence_batch(
        capability=capability,
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
        policy_refs=(
            "policy:life-development-v1",
            "policy:test-location",
        ),
    )

    with pytest.raises(ValueError, match="does not authorize the proposed window"):
        validate_commit_batch(events, expected_world_revision=1)


def test_batch_rejects_location_effect_that_weakens_privacy() -> None:
    capability = _location_capability()
    events = _location_bound_occurrence_batch(
        capability=capability,
        effect_window=DueWindow(
            opens_at=NOW + timedelta(hours=2),
            closes_at=NOW + timedelta(hours=4),
        ),
        effect_privacy="public",
        policy_refs=(
            "policy:life-development-v1",
            "policy:test-location",
        ),
    )

    with pytest.raises(ValueError, match="weakened its authorized privacy"):
        validate_commit_batch(events, expected_world_revision=1)


@pytest.mark.asyncio
async def test_no_op_is_pinned_audited_and_effect_once() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=('{"decision":"no_op"}',),
    )
    character_model = _SequenceModel(
        model="test-character-model",
        outputs=(AssertionError("no_op must not call the Character Model"),),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=character_model,
    )
    first = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:first",
        correlation_id="correlation:life-development",
    )
    second = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:replay",
        correlation_id="correlation:life-development",
    )

    assert first.status == "no_op"
    assert second.status == "no_op"
    assert first.proposal_event_ref == second.proposal_event_ref
    assert world_author.calls == 1
    assert character_model.calls == 0
    projection = ledger.project()
    assert projection.plans == ()
    assert projection.world_occurrences == ()
    assert len(projection.model_result_audits) == 1
    proposal_event = ledger.lookup_event_commit(first.proposal_event_ref or "")[0]
    proposal = proposal_event.payload()
    assert proposal["proposal_kind"] == "life_development"
    assert proposal["decision"] == "no_op"
    assert proposal["model_role"] == "world_author"
    assert proposal["world_author_model"] == "test-world-author"
    assert proposal["context_capsule_id"] == "1" * 64
    assert proposal["context_cursor"]["world_revision"] == 1
    assert (
        proposal["capability_manifest_hash"]
        == _manifest(
            wake,
            pinned_cursor=ProjectionCursor.model_validate(proposal["context_cursor"]),
        ).manifest_hash
    )


@pytest.mark.asyncio
async def test_stale_model_result_cas_does_not_poison_a_different_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=(
            '{"decision":"no_op"}',
            '{ "decision": "no_op" }',
        ),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=_SequenceModel(
            model="test-character",
            outputs=(),
        ),
    )
    original_commit = ledger.commit_at_cursor
    failed_once = False

    def fail_first_model_result_commit(events, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal failed_once
        if not failed_once and any(event.event_type == "ModelResultRecorded" for event in events):
            failed_once = True
            raise ConcurrencyConflict("simulated stale model-result prefix")
        return original_commit(events, **kwargs)

    monkeypatch.setattr(ledger, "commit_at_cursor", fail_first_model_result_commit)

    first = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:model-result-stale",
        correlation_id="correlation:life-development",
    )
    second = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:model-result-retry",
        correlation_id="correlation:life-development",
    )

    assert first.status == "stale_prefix"
    assert second.status == "no_op"
    assert world_author.calls == 2
    # Two distinct provider byte sequences plus one shared, immutable
    # capability-manifest sidecar.
    assert len(store._records) == 3  # noqa: SLF001


@pytest.mark.asyncio
async def test_world_author_can_commit_a_free_adverse_world_contingency() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    adverse = {
        "decision": "propose",
        "causal_authority": "world_contingency",
        "outcome_resolution_authority": "world_contingency",
        "premise_scope": "external_opportunity",
        "premise": "院子上方忽然落下一阵很急的冰雹，晾着的手账被打湿了。",
        "premise_claim_refs": ["local:claim:hail"],
        "claim_declarations": [
            {
                "claim_id": "local:claim:hail",
                "summary": "此刻院子里突发冰雹并打湿了手账。",
                "scope": "novel_world_generation",
                "subject_scope": "world_environment",
                "source_refs": [],
            }
        ],
        "timing": {"mode": "now", "duration_minutes": 20},
        "anchor_refs": [wake.event_id],
        "location_ref": "location:campus-courtyard",
        "location_capability_ref": _location_capability().capability_ref,
        "entity_refs": [],
        "privacy_class": "personal",
        "outcomes": [
            {
                "text": "她赶到时还是有几页洇开了，字迹糊成一团。",
                "privacy_class": "personal",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:hail"],
                "provisional_npcs": [
                    {
                        "local_ref": "local:npc:umbrella-student",
                        "summary": "躲雨时遇见一位帮忙捡起散页的陌生学生。",
                        "narrative_tags": ["narrative:chance_meeting"],
                        "privacy_class": "personal",
                    }
                ],
                "dynamic_life_direction": None,
            },
            {
                "text": "她及时收回了手账，只湿了封面和边角。",
                "privacy_class": "personal",
                "relative_plausibility_weight": 3,
                "claim_refs": ["local:claim:hail"],
                "provisional_npcs": [],
                "dynamic_life_direction": None,
            },
        ],
    }
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=(json.dumps(adverse, ensure_ascii=False),),
    )
    character_model = _SequenceModel(
        model="test-character-model",
        outputs=(
            AssertionError(
                "world contingency occurrence must not ask the Character Model to happen"
            ),
        ),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=character_model,
    )

    first = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:adverse",
        correlation_id="correlation:life-development",
    )
    second = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:adverse-replay",
        correlation_id="correlation:life-development",
    )

    assert first.status == "occurrence_committed"
    assert second.status == "occurrence_committed"
    assert second.proposal_event_ref == first.proposal_event_ref
    assert second.occurrence_id == first.occurrence_id
    assert world_author.calls == 1
    assert character_model.calls == 0
    occurrence = ledger.project().world_occurrences[0]
    assert occurrence.occurrence_id == first.occurrence_id
    assert occurrence.status == "active"
    assert occurrence.activated_at == NOW
    assert occurrence.location_ref == "location:campus-courtyard"
    assert occurrence.time_window.opens_at == NOW
    assert occurrence.time_window.closes_at == NOW + timedelta(minutes=20)
    assert [item.causal_authority for item in occurrence.candidate_outcomes] == [
        "world_contingency",
        "world_contingency",
    ]
    assert [item.relative_plausibility_weight for item in occurrence.candidate_outcomes] == [1, 3]
    introduced = occurrence.candidate_outcomes[0].provisional_npc_introductions[0]
    assert introduced.provisional_entity_ref.startswith("provisional:npc:")
    introduced_content = store.read_exact(content_ref=introduced.summary_content_ref)
    assert introduced_content is not None
    assert introduced_content.content_kind == "provisional_npc_introduction"
    assert all(item.dynamic_life_arc_context is None for item in occurrence.candidate_outcomes)
    expected_texts = [item["text"] for item in adverse["outcomes"]]
    for descriptor, text in zip(
        occurrence.candidate_outcomes,
        expected_texts,
        strict=True,
    ):
        assert descriptor.content_ref is not None
        stored = store.read_exact(content_ref=descriptor.content_ref)
        assert stored is not None
        assert stored.text == text
        assert stored.content_payload_hash == life_content_payload_hash(text)

    proposal_event, proposal_commit = ledger.lookup_event_commit(first.proposal_event_ref or "")
    occurrence_event, occurrence_commit = next(
        ledger.lookup_event_commit(item.event_id)
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "WorldOccurrenceCommitted"
    )
    proposal_payload = proposal_event.payload()
    possibility_authority = proposal_payload["possibility_authority"]
    location_capability = _location_capability()
    assert proposal_payload["causal_authority"] == "world_contingency"
    assert proposal_payload["model_role"] == "world_author"
    assert proposal_payload["possibility_authority_version"] == "life-development-possibility.2"
    assert possibility_authority["location_capability_ref"] == location_capability.capability_ref
    assert possibility_authority["location_capability"] == (
        location_capability.model_dump(
            mode="json",
            exclude={"capability_ref"},
        )
    )
    assert set(occurrence_event.payload()["policy_refs"]) == {
        "policy:life-development-v1",
        "policy:test-location",
    }
    assert occurrence_event.causation_id == proposal_event.event_id
    assert proposal_commit == occurrence_commit
    assert proposal_commit.event_ids == (
        proposal_event.event_id,
        occurrence_event.event_id,
        next(
            item.event_id
            for item in ledger.project().committed_world_event_refs
            if item.event_type == "WorldOccurrenceActivated"
        ),
    )


@pytest.mark.asyncio
async def test_ledger_rejects_a_self_authorized_manifest_not_bound_to_model_audit() -> None:
    source = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(source)
    capability = _location_capability()
    runtime, _ = _runtime(
        ledger=source,
        wake=wake,
        world_author=_SequenceModel(
            model="test-world-author",
            outputs=(
                _location_bound_world_draft(
                    wake=wake,
                    capability=capability,
                    timing={"mode": "now", "duration_minutes": 20},
                    privacy_class="shareable",
                ),
            ),
        ),
        character_model=_SequenceModel(
            model="test-character-model",
            outputs=(),
        ),
    )
    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:valid-source",
        correlation_id="correlation:life-development",
    )
    proposal_event, domain_commit = source.lookup_event_commit(
        result.proposal_event_ref or ""
    )
    proposal_payload = proposal_event.payload()
    deliberation = proposal_payload["world_author_deliberation"]
    assert isinstance(deliberation, dict)
    audit_event_ref = deliberation["model_result_event_refs"][0]
    assert isinstance(audit_event_ref, str)
    _, audit_commit = source.lookup_event_commit(audit_event_ref)
    audit_events = tuple(
        source.lookup_event_commit(event_id)[0]
        for event_id in audit_commit.event_ids
    )
    domain_events = tuple(
        source.lookup_event_commit(event_id)[0]
        for event_id in domain_commit.event_ids
    )

    replay = WorldLedger.in_memory(world_id=WORLD_ID)
    _seed_clock(replay)
    replay.commit_at_cursor(
        audit_events,
        expected_cursor=_projection_cursor(replay),
        commit_id="commit:test:replay-life-development-audit",
    )

    forged = json.loads(json.dumps(proposal_payload, ensure_ascii=False))
    forged_deliberation = forged["world_author_deliberation"]
    forged_manifest = forged_deliberation["capability_manifest"]
    forged_manifest["entity_refs"] = ["entity:forged-self-authority"]
    parsed_manifest = LifeDevelopmentCapabilityManifest.model_validate_json(
        json.dumps(forged_manifest, ensure_ascii=False)
    )
    forged["capability_manifest_hash"] = parsed_manifest.manifest_hash
    forged["world_author_deliberation_hash"] = _hash_json(
        forged_deliberation
    )
    tampered_proposal = _replace_event_payload(
        proposal_event,
        payload=forged,
    )

    with pytest.raises(
        ValueError,
        match="manifest bytes changed",
    ):
        replay.commit_at_cursor(
            (tampered_proposal, *domain_events[1:]),
            expected_cursor=_projection_cursor(replay),
            commit_id="commit:test:forged-life-development-authority",
        )


@pytest.mark.asyncio
async def test_location_capability_authority_is_carried_into_the_world_effect() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    location_authority = _seed_clock(
        ledger,
        event_id="event:clock:location-authority",
    )
    wake = _seed_clock(
        ledger,
        event_id="event:clock:life-development-authority-test",
        logical_time=NOW + timedelta(minutes=10),
        logical_time_from=NOW,
    )
    capability = _location_capability(
        authority_refs=(
            location_authority.event_id,
            "policy:test-location",
        ),
    )
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=(
            _location_bound_world_draft(
                wake=wake,
                capability=capability,
                timing={"mode": "now", "duration_minutes": 20},
                privacy_class="personal",
            ),
        ),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=_SequenceModel(
            model="test-character-model",
            outputs=(
                AssertionError("world contingency occurrence must not call the Character Model"),
            ),
        ),
        location_capability=capability,
    )

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:location-authority",
        correlation_id="correlation:life-development",
    )

    assert result.status == "occurrence_committed"
    occurrence_event = next(
        ledger.lookup_event_commit(item.event_id)[0]
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "WorldOccurrenceCommitted"
    )
    occurrence_payload = occurrence_event.payload()
    assert {item["ref_id"] for item in occurrence_payload["evidence_refs"]} == {
        location_authority.event_id,
        wake.event_id,
    }
    assert set(occurrence_payload["policy_refs"]) == {
        "policy:life-development-v1",
        "policy:test-location",
    }


@pytest.mark.asyncio
async def test_character_model_freely_accepts_an_external_opportunity_into_a_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    offered_opens = NOW + timedelta(hours=2)
    offered_closes = NOW + timedelta(hours=4)
    chosen_opens = NOW + timedelta(hours=2, minutes=30)
    chosen_closes = NOW + timedelta(hours=3, minutes=30)
    opportunity = {
        "decision": "propose",
        "causal_authority": "character_choice",
        "outcome_resolution_authority": "character_choice",
        "premise_scope": "external_opportunity",
        "premise": "校园院子今晚临时开放一小段露天电影。",
        "premise_claim_refs": ["local:claim:screening"],
        "claim_declarations": [
            {
                "claim_id": "local:claim:screening",
                "summary": "校园院子存在一场可自由参加的临时露天电影。",
                "scope": "novel_world_generation",
                "subject_scope": "world_environment",
                "source_refs": [],
            }
        ],
        "timing": {
            "mode": "later",
            "opens_at": offered_opens.isoformat(),
            "closes_at": offered_closes.isoformat(),
        },
        "anchor_refs": [wake.event_id],
        "location_ref": "location:campus-courtyard",
        "location_capability_ref": _location_capability().capability_ref,
        "entity_refs": [],
        "privacy_class": "shareable",
        "outcomes": [
            {
                "text": "电影放完时风有点凉，院子里的人慢慢散了。",
                "privacy_class": "shareable",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:screening"],
                "provisional_npcs": [],
                "dynamic_life_direction": {
                    "summary": "她开始留意这座城市偶尔出现的小型露天放映。",
                    "narrative_tags": ["narrative:outdoor-film"],
                    "duration_days": 14,
                    "privacy_class": "personal",
                },
            },
            {
                "text": "中途下了一点小雨，放映比预计早结束。",
                "privacy_class": "shareable",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:screening"],
                "provisional_npcs": [],
                "dynamic_life_direction": {
                    "summary": "她想继续找一些不太正式的小型放映。",
                    "narrative_tags": ["narrative:small-screenings"],
                    "duration_days": 10,
                    "privacy_class": "personal",
                },
            },
        ],
    }
    intention = "我想带杯热饮去坐后排，看一小时；不想把今晚全交给活动。"
    world_author = _SequenceModel(
        model="shared-provider/world-author",
        outputs=(json.dumps(opportunity, ensure_ascii=False),),
    )
    character_model = _SequenceModel(
        model="shared-provider/character-model",
        outputs=(
            json.dumps(
                {
                    "decision": "accept",
                    "intention_summary": intention,
                    "importance_bp": 4300,
                    "opens_at": chosen_opens.isoformat(),
                    "closes_at": chosen_closes.isoformat(),
                    "participant_refs": [],
                },
                ensure_ascii=False,
            ),
        ),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=character_model,
    )
    commit_character_plan = runtime._commit_character_plan  # noqa: SLF001
    commit_attempts = 0

    def crash_once_after_model_results(**kwargs):  # type: ignore[no-untyped-def]
        nonlocal commit_attempts
        commit_attempts += 1
        if commit_attempts == 1:
            raise RuntimeError("simulated crash after durable model results")
        return commit_character_plan(**kwargs)

    monkeypatch.setattr(
        runtime,
        "_commit_character_plan",
        crash_once_after_model_results,
    )

    with pytest.raises(RuntimeError, match="after durable model results"):
        await runtime.advance_once(
            wake_event_ref=wake.event_id,
            trace_id="trace:character-choice-crash",
            correlation_id="correlation:life-development",
        )
    assert ledger.project().plans == ()
    assert world_author.calls == 1
    assert character_model.calls == 1
    assert len(ledger.project().model_result_audits) == 2

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:character-choice-replay",
        correlation_id="correlation:life-development",
    )

    assert result.status == "plan_committed"
    assert world_author.calls == 1
    assert character_model.calls == 1
    plan = ledger.project().plans[0]
    assert plan.plan_id == result.plan_id
    assert plan.scheduled_window is not None
    assert plan.scheduled_window.opens_at == chosen_opens
    assert plan.scheduled_window.closes_at == chosen_closes
    assert plan.importance_bp == 4300
    assert not plan.activity_kind.startswith("social.")

    proposal_event, proposal_commit = ledger.lookup_event_commit(result.proposal_event_ref or "")
    proposal = proposal_event.payload()
    assert proposal["causal_authority"] == "character_choice"
    assert proposal["world_author_model"] == "shared-provider/world-author"
    assert proposal["character_model_role"] == "character_model"
    assert proposal["character_model"] == "shared-provider/character-model"
    assert len(proposal["possibility_authority_hash"]) == 64
    assert len(proposal["character_choice_hash"]) == 64
    intention_binding = next(
        item for item in proposal["content_bindings"] if item["role"] == "character_intention"
    )
    stored_intention = store.read_exact(content_ref=intention_binding["content_ref"])
    assert stored_intention is not None
    assert stored_intention.text == intention
    material = LifeDevelopmentProposalReader(
        ledger=ledger,
        content_store=store,
    ).read_for_plan(plan_id=plan.plan_id)
    assert material is not None
    assert material.premise == opportunity["premise"]
    assert material.character_intention == intention
    assert [item.text for item in material.outcomes] == [
        item["text"] for item in opportunity["outcomes"]
    ]
    assert {item.descriptor.causal_authority for item in material.outcomes} == {"character_choice"}
    assert all(item.descriptor.dynamic_life_arc_context is not None for item in material.outcomes)

    plan_event, plan_commit = next(
        ledger.lookup_event_commit(item.event_id)
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "ActivityPlanned"
    )
    possibility_authority = proposal["possibility_authority"]
    location_capability = _location_capability()
    assert possibility_authority["location_capability_ref"] == location_capability.capability_ref
    assert possibility_authority["location_capability"] == (
        location_capability.model_dump(
            mode="json",
            exclude={"capability_ref"},
        )
    )
    assert set(plan_event.payload()["policy_refs"]) == {
        "policy:life-development-v1",
        "policy:test-location",
    }
    assert plan_event.causation_id == proposal_event.event_id
    assert proposal_commit == plan_commit
    assert proposal_commit.event_ids == (
        proposal_event.event_id,
        plan_event.event_id,
    )


@pytest.mark.asyncio
async def test_invalid_world_draft_gets_one_source_bound_reselection() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    invalid = {
        "decision": "propose",
        "causal_authority": "world_contingency",
        "outcome_resolution_authority": "world_contingency",
        "premise_scope": "external_opportunity",
        "premise": "一个没有授权来源的地点发生了临时变化。",
        "premise_claim_refs": ["local:claim:place"],
        "claim_declarations": [
            {
                "claim_id": "local:claim:place",
                "summary": "一个新环境变化。",
                "scope": "novel_world_generation",
                "subject_scope": "world_environment",
                "source_refs": [],
            }
        ],
        "timing": {"mode": "now", "duration_minutes": 15},
        "anchor_refs": [wake.event_id],
        "location_ref": "location:not-in-capability-manifest",
        "location_capability_ref": _location_capability().capability_ref,
        "entity_refs": [],
        "privacy_class": "personal",
        "outcomes": [
            {
                "text": "变化持续了一会儿。",
                "privacy_class": "personal",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:place"],
                "provisional_npcs": [],
                "dynamic_life_direction": None,
            },
            {
                "text": "变化很快结束。",
                "privacy_class": "personal",
                "relative_plausibility_weight": 1,
                "claim_refs": ["local:claim:place"],
                "provisional_npcs": [],
                "dynamic_life_direction": None,
            },
        ],
    }
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=(
            json.dumps(invalid, ensure_ascii=False),
            '{"decision":"no_op"}',
        ),
    )
    character_model = _SequenceModel(
        model="test-character-model",
        outputs=(AssertionError("repaired no_op must not call character model"),),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=character_model,
    )

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:repair",
        correlation_id="correlation:life-development",
    )

    assert result.status == "no_op"
    assert world_author.calls == 2
    repair_request = json.loads(world_author.messages[1][-1]["content"])
    assert repair_request["validation_failure"]["code"] == "unsupported_location_window"
    assert "same pinned Context and capability manifest" in repair_request["instruction"]
    proposal = ledger.lookup_event_commit(result.proposal_event_ref or "")[0].payload()
    assert proposal["repair_ordinal"] == 1
    audits = [
        RecordedModelResultAudit.model_validate_json(item.audit_json)
        for item in ledger.project().model_result_audits
    ]
    assert [item.status for item in audits] == [
        "main_invalid",
        "main_invalid_recovered",
    ]
    assert [item.request_hash for item in audits] == [
        hashlib.sha256(
            json.dumps(
                messages,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for messages in world_author.messages
    ]
    for audit, expected_raw in zip(
        audits,
        (json.dumps(invalid, ensure_ascii=False), '{"decision":"no_op"}'),
        strict=True,
    ):
        assert audit.response_hash == life_content_payload_hash(expected_raw)
        assert any(
            record.text == expected_raw and record.content_payload_hash == audit.response_hash
            for record in store._records.values()  # noqa: SLF001
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("corrective", "expected_code", "has_second_output"),
    (
        ("{}", "corrective_invalid", True),
        (TimeoutError("corrective timeout"), "corrective_timeout", False),
    ),
)
async def test_failed_correction_retains_both_exact_attempts_without_world_effect(
    corrective: object,
    expected_code: str,
    has_second_output: bool,
) -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=("{}", corrective),
    )
    runtime, store = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=_SequenceModel(
            model="test-character-model",
            outputs=(AssertionError("failed World Author must not reach Character"),),
        ),
    )

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:failed-correction",
        correlation_id="correlation:life-development",
    )

    assert result.status == "technical_failure"
    assert result.reason_code == "life_development.world_author_unavailable"
    audits = [
        RecordedModelResultAudit.model_validate_json(item.audit_json)
        for item in ledger.project().model_result_audits
    ]
    assert [item.status for item in audits] == [
        "main_invalid",
        "recovery_failed",
    ]
    assert audits[1].failure_code == expected_code
    assert (audits[1].response_hash is not None) is has_second_output
    assert len(store._records) == (2 if has_second_output else 1)  # noqa: SLF001
    assert ledger.project().plans == ()
    assert ledger.project().world_occurrences == ()
    assert ledger.project().proposal_audits == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        TimeoutError("provider unavailable"),
        httpx.ReadTimeout("provider unavailable"),
        ValueError("malformed provider response"),
    ),
)
async def test_provider_failure_is_technical_and_writes_no_world_effect(
    failure: BaseException,
) -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    world_author = _SequenceModel(
        model="test-world-author",
        outputs=(failure,),
    )
    character_model = _SequenceModel(
        model="test-character-model",
        outputs=(AssertionError("provider failure must not call character model"),),
    )
    runtime, _ = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=world_author,
        character_model=character_model,
    )

    result = await runtime.advance_once(
        wake_event_ref=wake.event_id,
        trace_id="trace:provider-failure",
        correlation_id="correlation:life-development",
    )

    assert result.status == "technical_failure"
    assert result.reason_code == "life_development.world_author_unavailable"
    assert ledger.project().plans == ()
    assert ledger.project().world_occurrences == ()
    audits = ledger.project().model_result_audits
    assert len(audits) == 1
    audit = RecordedModelResultAudit.model_validate_json(audits[0].audit_json)
    assert audit.response_hash is None
    assert audit.attempted_model_id == "test-world-author"
    expected_status = (
        "main_timeout"
        if isinstance(failure, (TimeoutError, httpx.TimeoutException))
        else "main_exception"
    )
    assert audit.status == expected_status
    assert (
        audit.request_hash
        == hashlib.sha256(
            json.dumps(
                world_author.messages[0],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    assert [
        item
        for item in ledger.project().committed_world_event_refs
        if item.event_type == "ProposalRecorded"
    ] == []


@pytest.mark.asyncio
async def test_programming_error_is_not_disguised_as_provider_failure() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    runtime, _ = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=_SequenceModel(
            model="test-world-author",
            outputs=(RuntimeError("adapter invariant broke"),),
        ),
        character_model=_SequenceModel(
            model="test-character-model",
            outputs=(),
        ),
    )

    with pytest.raises(RuntimeError, match="adapter invariant broke"):
        await runtime.advance_once(
            wake_event_ref=wake.event_id,
            trace_id="trace:programming-error",
            correlation_id="correlation:life-development",
        )

    assert ledger.project().model_result_audits == ()
    assert ledger.project().plans == ()
    assert ledger.project().world_occurrences == ()


@pytest.mark.asyncio
async def test_character_programming_error_also_propagates() -> None:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    wake = _seed_clock(ledger)
    capability = _location_capability()
    runtime, _ = _runtime(
        ledger=ledger,
        wake=wake,
        world_author=_SequenceModel(
            model="test-world-author",
            outputs=(
                _location_bound_world_draft(
                    wake=wake,
                    capability=capability,
                    timing={
                        "mode": "later",
                        "opens_at": (NOW + timedelta(hours=2)).isoformat(),
                        "closes_at": (NOW + timedelta(hours=3)).isoformat(),
                    },
                    privacy_class="shareable",
                    causal_authority="character_choice",
                    outcome_resolution_authority="character_choice",
                ),
            ),
        ),
        character_model=_SequenceModel(
            model="test-character-model",
            outputs=(RuntimeError("character adapter invariant broke"),),
        ),
    )

    with pytest.raises(RuntimeError, match="character adapter invariant broke"):
        await runtime.advance_once(
            wake_event_ref=wake.event_id,
            trace_id="trace:character-programming-error",
            correlation_id="correlation:life-development",
        )

    assert len(ledger.project().model_result_audits) == 1
    assert ledger.project().plans == ()
    assert ledger.project().world_occurrences == ()
