from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from companion_daemon.world_v2.accepted_ledger_batch import AcceptedLedgerBatchIssuer
from companion_daemon.world_v2.event_identity import domain_idempotency_key
from companion_daemon.world_v2.event_catalog import event_contract
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.media_selection import MediaSelection
from companion_daemon.world_v2.media_selection_acceptance_manifest import (
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION,
    MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION,
    MediaSelectionAcceptanceManifest,
    MediaSelectionAcceptanceManifestV2,
    build_media_selection_acceptance_manifest,
    build_media_selection_acceptance_manifest_v2,
    parse_media_selection_acceptance_manifest,
)
from companion_daemon.world_v2.media_selection_acceptance_runtime import (
    MediaSelectionAcceptanceError,
    MediaSelectionAcceptanceRuntime,
    MediaSelectionProposalRecorder,
)
from companion_daemon.world_v2.media_selection_proposal import MediaSelectionProposalCompiler
from companion_daemon.world_v2.media_v2 import (
    CharacterMediaCandidateContract,
    InMemoryImmutableMediaPayloadStore,
    MediaEvidenceSource,
    MediaOpportunity,
    PhotoCandidate,
    PhotoCandidateOpenedPayload,
    canonical_media_json,
    media_payload_hash,
    character_media_contract_digest,
)
from companion_daemon.world_v2.schemas import (
    BudgetAccount,
    ProjectionCursor,
    ProviderMediaGrantBinding,
    WorldEvent,
)


def _manifest():
    return build_media_selection_acceptance_manifest(
        acceptance_id="acceptance:media-selection:1",
        acceptance_event_ref="event:media-selection-acceptance:1",
        proposal_id="proposal:media-selection:1",
        proposal_event_ref="event:media-selection-proposal:1",
        proposal_event_payload_hash="a" * 64,
        evaluated_world_revision=7,
        accepted_change_id="change:media-selection:1",
        accepted_change_hash="b" * 64,
        candidate_id="candidate:1",
        expected_candidate_revision=1,
        candidate_authority_hash="c" * 64,
        selection_hash="d" * 64,
        opportunity_event_id="event:media-opportunity:1",
        opportunity_payload_hash="e" * 64,
        opportunity_id="opportunity:1",
        snapshot_ref="sidecar:snapshot:1",
        snapshot_hash="sha256:" + "f" * 64,
        reservation_event_id="event:reservation:1",
        reservation_payload_hash="1" * 64,
        action_event_id="event:action:1",
        action_payload_hash="2" * 64,
        policy_digest="3" * 64,
    )


def test_manifest_hash_binds_every_accepted_media_effect_without_a_sha_fixed_point() -> None:
    manifest = _manifest()
    assert manifest.manifest_hash
    with pytest.raises(ValueError, match="manifest hash"):
        manifest.model_copy(update={"opportunity_id": "opportunity:forged"}).model_validate(
            {**manifest.model_dump(mode="json"), "opportunity_id": "opportunity:forged"}
        )


def test_p1_manifest_wire_and_hash_remain_byte_compatible_after_p3_is_installed() -> None:
    manifest = _manifest()

    assert manifest.manifest_version == MEDIA_SELECTION_ACCEPTANCE_MANIFEST_VERSION
    assert manifest.manifest_hash == "4616dc74bb29b1490766e3c1504bbbd6ce70e4feddd2a65b3e2a17af8606acac"
    parsed = parse_media_selection_acceptance_manifest(manifest.model_dump(mode="json"))
    assert type(parsed) is MediaSelectionAcceptanceManifest
    assert parsed == manifest


def test_p3_manifest_requires_and_hash_binds_all_private_authority_digests() -> None:
    p1_material = _manifest().model_dump(
        mode="json", exclude={"manifest_version", "status", "manifest_hash"}
    )
    manifest = build_media_selection_acceptance_manifest_v2(
        **p1_material,
        p3_authorization_digest="4" * 64,
        relationship_context_digest="5" * 64,
        private_basis_digest="6" * 64,
        snapshot_schema_version="world-image-event-snapshot-v3",
    )

    assert manifest.manifest_version == MEDIA_SELECTION_ACCEPTANCE_MANIFEST_V2_VERSION
    parsed = parse_media_selection_acceptance_manifest(manifest.model_dump(mode="json"))
    assert type(parsed) is MediaSelectionAcceptanceManifestV2
    assert parsed == manifest

    missing_basis = manifest.model_dump(mode="json")
    missing_basis.pop("private_basis_digest")
    with pytest.raises(ValueError, match="private_basis_digest"):
        parse_media_selection_acceptance_manifest(missing_basis)

    forged_context = manifest.model_dump(mode="json")
    forged_context["relationship_context_digest"] = "7" * 64
    with pytest.raises(ValueError, match="manifest hash"):
        parse_media_selection_acceptance_manifest(forged_context)

    wrong_schema = manifest.model_dump(mode="json")
    wrong_schema["snapshot_schema_version"] = "world-image-event-snapshot-v2"
    with pytest.raises(ValueError, match="snapshot_schema_version"):
        parse_media_selection_acceptance_manifest(wrong_schema)


def test_catalog_accepts_only_the_complete_p3_manifest_wire_shape() -> None:
    p1_material = _manifest().model_dump(
        mode="json", exclude={"manifest_version", "status", "manifest_hash"}
    )
    payload = build_media_selection_acceptance_manifest_v2(
        **p1_material,
        p3_authorization_digest="4" * 64,
        relationship_context_digest="5" * 64,
        private_basis_digest="6" * 64,
        snapshot_schema_version="world-image-event-snapshot-v3",
    ).model_dump(mode="json")

    event_contract("AcceptanceRecorded").validate_payload(payload)
    payload.pop("p3_authorization_digest")
    with pytest.raises(ValueError, match="p3_authorization_digest"):
        event_contract("AcceptanceRecorded").validate_payload(payload)


NOW = datetime(2026, 7, 16, 19, tzinfo=UTC)


def _event(*, world_id: str, event_id: str, event_type: str, payload: dict[str, object], causation_id: str) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1", event_id=event_id, event_type=event_type, world_id=world_id,
        logical_time=NOW, created_at=NOW, actor="test:media-selection", source="test:media-selection",
        trace_id="trace:media-selection", causation_id=causation_id,
        correlation_id="correlation:media-selection",
        idempotency_key=(
            domain_idempotency_key(event_type=event_type, world_id=world_id, payload=payload)
            or "identity:" + event_id
        ),
        payload=payload,
    )


def _cursor(projection) -> ProjectionCursor:  # type: ignore[no-untyped-def]
    return ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )


class _Authorizer:
    """Keep this ledger test focused on P1 acceptance, not snapshot mapping."""

    def authorize(self, *, cursor, selection, category, observed_at, expires_at):  # type: ignore[no-untyped-def]
        del cursor, observed_at
        body = canonical_media_json({"schema_version": "test-frozen-image-evidence.1"})
        compiled = SimpleNamespace(
            snapshot_ref="sidecar:test:media-selection",
            snapshot_hash=media_payload_hash(body),
            snapshot_body=body,
        )
        return (
            MediaOpportunity(
                opportunity_id="opportunity:test:media-selection", candidate_id=selection.candidate_id,
                family="life_share", delivery_mode="preview", privacy_ceiling="shareable",
                media_privacy_ceiling="ordinary", event_snapshot_ref=compiled.snapshot_ref,
                event_snapshot_hash=compiled.snapshot_hash,
                source_event_refs=("event:world-started:media-selection",),
                catalog_version="test-media-selection.1", ecology_category=category,
                ecology_observed_at=NOW, expires_at=expires_at,
            ),
            compiled,
        )


def test_real_ledger_acceptance_commits_one_selected_candidate_and_planning_action(monkeypatch) -> None:
    """Exercise recorder, accepted-batch guard, reducers, and replay projection."""

    world_id = "world:media-selection-acceptance-real"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer)
    clock = _event(
        world_id=world_id, event_id="event:clock:media-selection", event_type="ClockAdvanced",
        payload={
            "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        },
        causation_id="cause:clock",
    )
    ledger.commit((clock,), expected_world_revision=0, expected_deliberation_revision=0)
    started = _event(
        world_id=world_id, event_id="event:world-started:media-selection",
        event_type="WorldStarted", payload={}, causation_id="cause:world-started",
    )
    account = BudgetAccount(
        account_id="account:media-selection", category="image", window_id="window:media-selection", limit=5
    )
    account_event = _event(
        world_id=world_id, event_id="event:account:media-selection", event_type="BudgetAccountConfigured",
        payload={"account": account.model_dump(mode="json")}, causation_id=started.event_id,
    )
    ledger.commit((started, account_event), expected_world_revision=1, expected_deliberation_revision=0)
    candidate = PhotoCandidate(
        candidate_id="candidate:media-selection", source_event_refs=(started.event_id,), family="life_share",
        privacy_ceiling="shareable", opened_at=NOW, expires_at=NOW + timedelta(hours=1),
        ecology_category="activity_result", ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref=started.event_id, payload_hash=started.payload_hash),),
    )
    candidate_event = _event(
        world_id=world_id, event_id="event:candidate:media-selection", event_type="PhotoCandidateOpened",
        payload=PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"), causation_id=account_event.event_id,
    )
    ledger.commit_at_cursor((candidate_event,), expected_cursor=_cursor(ledger.project()))
    projection = ledger.project()
    proposal = MediaSelectionProposalCompiler(catalog_version="test-media-selection.1").compile(
        projection=projection,
        selection=MediaSelection(candidate_id=candidate.candidate_id, family="life_share"),
        model="test-flash", raw_output_hash="sha256:" + "a" * 64,
        normalized_output_hash="sha256:" + "b" * 64,
    )
    recorder = MediaSelectionProposalRecorder(ledger=ledger)
    with pytest.raises(ValueError, match="proposal_candidate_not_current"):
        recorder.record(
            cursor=_cursor(projection),
            proposal=proposal.model_copy(update={"expected_candidate_revision": 2}),
            actor="worker:media-selection", source="test:media-selection", created_at=NOW,
            trace_id="trace:media-selection", correlation_id="correlation:media-selection",
        )
    record = recorder.record(
        cursor=_cursor(projection), proposal=proposal, actor="worker:media-selection",
        source="test:media-selection", created_at=NOW, trace_id="trace:media-selection",
        correlation_id="correlation:media-selection",
    )
    proposal_event, _proposal_commit = ledger.lookup_event_commit(record.proposal_event_ref)  # type: ignore[misc]
    assert proposal_event.causation_id == candidate_event.event_id
    # Provider-grant semantics are covered elsewhere; this isolates the
    # accepted-batch wiring while keeping both runtime and reducer consistent.
    monkeypatch.setattr(
        "companion_daemon.world_v2.reducers.require_provider_media_grant", lambda **_kwargs: object()
    )
    runtime = MediaSelectionAcceptanceRuntime(
        ledger=ledger, authorizer=_Authorizer(), sidecar=InMemoryImmutableMediaPayloadStore(), batch_issuer=issuer,
    )
    committed = runtime.accept(
        handle=runtime.pin_proposal(
            cursor=_cursor(ledger.project()), proposal_event_ref=record.proposal_event_ref
        ),
        actor="worker:media-selection", source="test:media-selection", logical_time=NOW, created_at=NOW,
        trace_id="trace:media-selection", correlation_id="correlation:media-selection",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1), account_id=account.account_id,
        amount_limit=1,
    )

    replayed = ledger.project()
    assert len(committed.event_ids) == 4
    assert [item.status for item in replayed.photo_candidates] == ["selected"]
    assert replayed.media_opportunities[0].selection_proposal_id == proposal.proposal_id
    assert replayed.media_opportunities[0].selected_candidate_revision == candidate.entity_revision
    assert replayed.actions[0].kind == "media_planning"
    assert replayed.actions[0].budget_reservation_id == replayed.budget_reservations[0].reservation_id


def test_real_ledger_acceptance_preserves_p2_candidate_and_snapshot_lineages(monkeypatch) -> None:
    """P2 may add frozen snapshot facts without replacing selected lineage."""

    world_id = "world:media-selection-acceptance-p2"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer)
    clock = _event(
        world_id=world_id, event_id="event:clock:media-selection-p2", event_type="ClockAdvanced",
        payload={
            "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        }, causation_id="cause:clock",
    )
    ledger.commit((clock,), expected_world_revision=0, expected_deliberation_revision=0)
    started = _event(
        world_id=world_id, event_id="event:world-started:media-selection-p2",
        event_type="WorldStarted", payload={}, causation_id="cause:world-started",
    )
    account = BudgetAccount(
        account_id="account:media-selection-p2", category="image", window_id="window:media-selection-p2", limit=5
    )
    account_event = _event(
        world_id=world_id, event_id="event:account:media-selection-p2", event_type="BudgetAccountConfigured",
        payload={"account": account.model_dump(mode="json")}, causation_id=started.event_id,
    )
    ledger.commit((started, account_event), expected_world_revision=1, expected_deliberation_revision=0)
    source = MediaEvidenceSource(event_ref=started.event_id, payload_hash=started.payload_hash)
    contract = CharacterMediaCandidateContract(
        subject_ref="agent:companion", kind="selfie",
        allowed_capture_modes=("character_front_camera",),
        allowed_character_visibility=("identifiable",),
        authority_digest=character_media_contract_digest(
            subject_ref="agent:companion", kind="selfie", source_events=(source,),
            allowed_capture_modes=("character_front_camera",),
            allowed_character_visibility=("identifiable",),
        ),
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:media-selection-p2", source_event_refs=(started.event_id,),
        family="character_media", privacy_ceiling="shareable", opened_at=NOW,
        expires_at=NOW + timedelta(hours=1), ecology_category="character_media:selfie",
        ecology_observed_at=NOW, source_events=(source,), character_media_contract=contract,
    )
    candidate_event = _event(
        world_id=world_id, event_id="event:candidate:media-selection-p2", event_type="PhotoCandidateOpened",
        payload=PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"),
        causation_id=account_event.event_id,
    )
    ledger.commit_at_cursor((candidate_event,), expected_cursor=_cursor(ledger.project()))
    projection = ledger.project()
    proposal = MediaSelectionProposalCompiler(catalog_version="test-media-selection-p2.1").compile(
        projection=projection,
        selection=MediaSelection(candidate_id=candidate.candidate_id, family="character_media"),
        model="test-flash", raw_output_hash="sha256:" + "a" * 64,
        normalized_output_hash="sha256:" + "b" * 64,
    )
    recorded = MediaSelectionProposalRecorder(ledger=ledger).record(
        cursor=_cursor(projection), proposal=proposal, actor="worker:media-selection",
        source="test:media-selection", created_at=NOW, trace_id="trace:media-selection",
        correlation_id="correlation:media-selection",
    )

    class _P2Authorizer:
        def authorize(self, *, cursor, selection, category, observed_at, expires_at):  # type: ignore[no-untyped-def]
            del cursor, observed_at
            body = canonical_media_json({"schema_version": "test-frozen-image-evidence.p2"})
            compiled = SimpleNamespace(
                snapshot_ref="sidecar:test:media-selection-p2", snapshot_hash=media_payload_hash(body),
                snapshot_body=body,
            )
            return MediaOpportunity(
                opportunity_id="opportunity:test:media-selection-p2", candidate_id=selection.candidate_id,
                family="character_media", delivery_mode="preview", privacy_ceiling="shareable",
                media_privacy_ceiling="ordinary", event_snapshot_ref=compiled.snapshot_ref,
                event_snapshot_hash=compiled.snapshot_hash, source_event_refs=(started.event_id,),
                candidate_source_event_refs=(started.event_id,), snapshot_source_events=(source,),
                catalog_version="test-media-selection-p2.1", ecology_category=category,
                ecology_observed_at=NOW, expires_at=expires_at,
            ), compiled

    monkeypatch.setattr(
        "companion_daemon.world_v2.reducers.require_provider_media_grant", lambda **_kwargs: object()
    )
    runtime = MediaSelectionAcceptanceRuntime(
        ledger=ledger, authorizer=_P2Authorizer(), sidecar=InMemoryImmutableMediaPayloadStore(),
        batch_issuer=issuer,
    )
    runtime.accept(
        handle=runtime.pin_proposal(cursor=_cursor(ledger.project()), proposal_event_ref=recorded.proposal_event_ref),
        actor="worker:media-selection", source="test:media-selection", logical_time=NOW, created_at=NOW,
        trace_id="trace:media-selection", correlation_id="correlation:media-selection",
        grant=ProviderMediaGrantBinding(grant_id="grant:test", grant_revision=1), account_id=account.account_id,
        amount_limit=1,
    )

    replayed = ledger.project()
    opportunity = replayed.media_opportunities[0]
    assert replayed.photo_candidates[0].status == "selected"
    assert opportunity.family == "character_media"
    assert opportunity.candidate_source_event_refs == candidate.source_event_refs
    assert opportunity.snapshot_source_events == candidate.source_events


def test_pin_rejects_selection_proposal_not_caused_by_its_candidate_opening() -> None:
    """A structurally valid direct ledger write cannot skip candidate lineage."""

    world_id = "world:media-selection-forged-lineage"
    issuer = AcceptedLedgerBatchIssuer()
    ledger = WorldLedger.in_memory(world_id=world_id, accepted_batch_issuer=issuer)
    clock = _event(
        world_id=world_id,
        event_id="event:clock:forged-lineage",
        event_type="ClockAdvanced",
        payload={
            "logical_time_from": (NOW - timedelta(seconds=1)).isoformat(),
            "logical_time_to": NOW.isoformat(),
        },
        causation_id="cause:clock",
    )
    ledger.commit((clock,), expected_world_revision=0, expected_deliberation_revision=0)
    started = _event(
        world_id=world_id,
        event_id="event:world-started:forged-lineage",
        event_type="WorldStarted",
        payload={},
        causation_id="cause:world-started",
    )
    candidate = PhotoCandidate(
        candidate_id="candidate:forged-lineage",
        source_event_refs=(started.event_id,),
        family="life_share",
        privacy_ceiling="shareable",
        opened_at=NOW,
        expires_at=NOW + timedelta(hours=1),
        ecology_category="activity_result",
        ecology_observed_at=NOW,
        source_events=(MediaEvidenceSource(event_ref=started.event_id, payload_hash=started.payload_hash),),
    )
    candidate_event = _event(
        world_id=world_id,
        event_id="event:candidate:forged-lineage",
        event_type="PhotoCandidateOpened",
        payload=PhotoCandidateOpenedPayload(candidate=candidate).model_dump(mode="json"),
        causation_id=started.event_id,
    )
    ledger.commit((started, candidate_event), expected_world_revision=1, expected_deliberation_revision=0)
    projection = ledger.project()
    proposal = MediaSelectionProposalCompiler(catalog_version="test-media-selection.1").compile(
        projection=projection,
        selection=MediaSelection(candidate_id=candidate.candidate_id, family="life_share"),
        model="test-flash",
        raw_output_hash="sha256:" + "a" * 64,
        normalized_output_hash="sha256:" + "b" * 64,
    )
    forged = _event(
        world_id=world_id,
        event_id="event:proposal:forged-lineage",
        event_type="MediaSelectionProposalRecorded",
        payload=proposal.model_dump(mode="json"),
        causation_id=started.event_id,
    )
    ledger.commit_at_cursor((forged,), expected_cursor=_cursor(projection))

    runtime = MediaSelectionAcceptanceRuntime(
        ledger=ledger,
        authorizer=_Authorizer(),
        sidecar=InMemoryImmutableMediaPayloadStore(),
        batch_issuer=issuer,
    )

    with pytest.raises(MediaSelectionAcceptanceError, match="proposal_lineage_invalid"):
        runtime.pin_proposal(cursor=_cursor(ledger.project()), proposal_event_ref=forged.event_id)
