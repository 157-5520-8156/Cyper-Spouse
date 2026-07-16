"""P3 compiler for recipient-scoped character-media evidence.

This is deliberately separate from ``MediaEvidenceSnapshotCompiler``.  The
P0/P2 compiler remains public/shareable-only; this seam reads personal/private
facts only after a pinned relationship context has bound one recipient.
"""

from __future__ import annotations

from dataclasses import dataclass

from .appearance_state import AppearanceStateRecordedPayload, appearance_state_at
from .image_evidence_contract import CharacterMediaEvidenceV1
from .media_evidence_snapshot import (
    CompiledMediaEvidence,
    MediaEvidenceNotRenderable,
    MediaEvidenceSnapshotCompiler,
    _RECIPIENT_SCOPED_VISIBILITIES,
)
from .media_v2 import (
    FrozenMediaEvidenceSnapshot,
    ImageEventSnapshotV3,
    MediaEvidenceSource,
    PhotoCandidate,
    PrivateMediaSnapshotAuthorization,
    canonical_media_json,
    media_digest,
    media_payload_hash,
)
from .private_image_evidence_contract import RecipientScopedImageEvidenceDeclaredPayload
from .relationship_media_context import RelationshipMediaContextV1
from .schemas import ProjectionCursor, WorldEvent
from .visible_physical_state import (
    VisiblePhysicalStateRecordedPayload,
    visible_physical_state_at,
)


@dataclass(frozen=True, slots=True)
class PrivateMediaEvidenceCompileRequest:
    candidate: PhotoCandidate
    category: str
    cursor: ProjectionCursor
    relationship_context: RelationshipMediaContextV1
    media_lane: str
    expression_charge_ceiling: str


class PrivateMediaEvidenceSnapshotCompiler:
    """Freeze the narrow P3 read capability at one exact projection cursor."""

    def __init__(self, *, ledger) -> None:  # ledger has the existing LedgerPort shape.
        self._ledger = ledger
        self._public_helpers = MediaEvidenceSnapshotCompiler(ledger=ledger)

    def compile(self, request: PrivateMediaEvidenceCompileRequest) -> CompiledMediaEvidence:
        candidate, context = request.candidate, request.relationship_context
        contract = candidate.character_media_contract
        if (
            candidate.family != "character_media"
            or candidate.privacy_ceiling not in _RECIPIENT_SCOPED_VISIBILITIES
            or contract is None
            or contract.kind not in {"selfie", "mirror"}
            or not set(contract.allowed_capture_modes) <= {"character_front_camera", "mirror"}
        ):
            raise MediaEvidenceNotRenderable("p3_candidate_not_private_self_authored")
        if request.media_lane not in {"alluring_life", "exclusive_private"}:
            raise MediaEvidenceNotRenderable("p3_media_lane_unsupported")
        if context.audience.character_ref != contract.subject_ref:
            raise MediaEvidenceNotRenderable("p3_context_character_mismatch")
        projection = self._ledger.project_at(request.cursor)
        self._public_helpers._require_exact_cursor(projection, request.cursor)
        if projection.logical_time is None:
            raise MediaEvidenceNotRenderable("p3_logical_time_missing")
        if projection.logical_time >= context.expires_at:
            raise MediaEvidenceNotRenderable("p3_relationship_context_expired")
        events = self._load_candidate_sources(candidate=candidate, projection=projection, cursor=request.cursor)
        declaration_event, declaration, source_event = self._recipient_declaration(
            events=events, candidate=candidate, recipient_ref=context.audience.recipient_ref
        )
        relation_event = self._relationship_origin(
            projection=projection, context=context, recipient_ref=context.audience.recipient_ref
        )

        body: dict[str, object] = {
            "schema_version": "world-image-event-snapshot-v3",
            "event": {
                "event_id": source_event.event_id,
                "type": source_event.event_type,
                "status": "committed",
                "logical_at": source_event.logical_time.isoformat(),
            },
            "source": {"channel": "direct_experience", "person": "character"},
            "location": {}, "activity": {}, "participants": (), "objects": (), "environment": {},
            "character": {"subject_ref": contract.subject_ref, "presence": {"present": True}},
            "existing_media": (),
            "visual_requirements": {"requires_readable_text": False},
            "relationship_media_context": context.model_dump(mode="json"),
        }
        origins: dict[str, tuple[WorldEvent, str]] = {
            "/event": (source_event, declaration.image_evidence.visibility),
            "/source": (source_event, declaration.image_evidence.visibility),
            "/visual_requirements": (source_event, declaration.image_evidence.visibility),
            "/character": (declaration_event, declaration.image_evidence.visibility),
            "/relationship_media_context": (relation_event, "private"),
            "/relationship_media_context/private_expression_basis": (declaration_event, "private"),
        }
        self._public_helpers._merge_explicit_evidence(
            target=body,
            origins=origins,
            event=declaration_event,
            fallback_visibility=declaration.image_evidence.visibility,
            raw=declaration.image_evidence.planner_payload(),
            allowed_visibilities=_RECIPIENT_SCOPED_VISIBILITIES,
            visibility_reason="p3_evidence_not_recipient_scoped",
        )
        extras = self._freeze_historical_character_state(
            projection=projection,
            candidate=candidate,
            context=context,
            at_logical_time=source_event.logical_time,
            body=body,
            origins=origins,
        )
        if not any(body[name] for name in ("location", "activity", "participants", "objects", "environment", "existing_media")):
            raise MediaEvidenceNotRenderable("p3_no_visual_evidence")

        evidence_index = self._public_helpers._build_index(body=body, origins=origins)
        snapshot = ImageEventSnapshotV3(
            event=body["event"], source=body["source"], location=body["location"],
            activity=body["activity"], participants=body["participants"], objects=body["objects"],
            environment=body["environment"], character=body["character"],
            existing_media=body["existing_media"], visual_requirements=body["visual_requirements"],
            relationship_media_context=context, evidence_index=evidence_index,
        )
        all_events = (*events, relation_event, *extras)
        source_events = tuple(
            MediaEvidenceSource(event_ref=item.event_id, payload_hash=item.payload_hash)
            for item in sorted({item.event_id: item for item in all_events}.values(), key=lambda item: item.event_id)
        )
        authorization_body = {
            "candidate_id": candidate.candidate_id,
            "candidate_revision": candidate.entity_revision,
            "recipient_ref": context.audience.recipient_ref,
            "media_lane": request.media_lane,
            "media_privacy_ceiling": "intimate",
            "expression_charge_ceiling": request.expression_charge_ceiling,
            "allowed_capture_modes": contract.allowed_capture_modes,
            "candidate_contract_digest": contract.authority_digest,
            "relationship_context_digest": context.authority_digest,
            "private_basis_digest": context.private_expression_basis.basis_digest,
            "source_event_refs": tuple(item.event_ref for item in source_events),
        }
        authorization = PrivateMediaSnapshotAuthorization(
            **authorization_body,
            authorization_digest=media_digest(authorization_body),
        )
        frozen = FrozenMediaEvidenceSnapshot(
            source_events=source_events,
            complete_candidate=candidate.model_dump(mode="json"),
            image_event_snapshot=snapshot,
            private_media_authorization=authorization,
        )
        image_body = canonical_media_json(snapshot.model_dump(mode="json"))
        frozen_body = canonical_media_json(frozen.model_dump(mode="json"))
        snapshot_ref = "sidecar:world-image-event-snapshot:" + media_digest({
            "contract": "world-image-event-snapshot-v3", "candidate_id": candidate.candidate_id,
            "category": request.category, "cursor": request.cursor.model_dump(mode="json"),
            "relationship_context": context.authority_digest,
            "sources": [(item.event_ref, item.payload_hash) for item in source_events],
        })
        return CompiledMediaEvidence(
            snapshot=frozen, snapshot_body=frozen_body, snapshot_ref=snapshot_ref,
            snapshot_hash=media_payload_hash(frozen_body), image_event_snapshot_body=image_body,
            image_event_snapshot_hash=media_payload_hash(image_body),
            evidence_index_digest=media_digest({
                pointer: entry.model_dump(mode="json") for pointer, entry in snapshot.evidence_index.items()
            }),
        )

    def _load_candidate_sources(self, *, candidate: PhotoCandidate, projection, cursor: ProjectionCursor) -> tuple[WorldEvent, ...]:
        refs = {item.event_id: item for item in projection.committed_world_event_refs}
        events: list[WorldEvent] = []
        for source_ref in candidate.source_event_refs:
            committed = refs.get(source_ref)
            located = self._ledger.lookup_event_commit(source_ref)
            if (
                committed is None or located is None or located[0].event_id != source_ref
                or located[0].payload_hash != committed.payload_hash
                or located[0].logical_time > projection.logical_time
            ):
                raise MediaEvidenceNotRenderable("p3_candidate_source_unavailable")
            events.append(located[0])
        if not events:
            raise MediaEvidenceNotRenderable("p3_candidate_has_no_sources")
        return tuple(events)

    @staticmethod
    def _recipient_declaration(*, events: tuple[WorldEvent, ...], candidate: PhotoCandidate, recipient_ref: str):
        matches: list[tuple[WorldEvent, RecipientScopedImageEvidenceDeclaredPayload, WorldEvent]] = []
        by_ref = {item.event_id: item for item in events}
        for event in events:
            if event.event_type != "RecipientScopedImageEvidenceDeclared":
                continue
            try:
                declaration = RecipientScopedImageEvidenceDeclaredPayload.model_validate_json(event.payload_json)
            except ValueError as exc:
                raise MediaEvidenceNotRenderable("p3_malformed_recipient_evidence") from exc
            source = by_ref.get(declaration.source_event_ref)
            character: CharacterMediaEvidenceV1 | None = declaration.image_evidence.character_media
            if (
                source is None or declaration.recipient_ref != recipient_ref
                or declaration.source_privacy_ceiling != candidate.privacy_ceiling
                or character is None or candidate.character_media_contract is None
                or character.character_ref != candidate.character_media_contract.subject_ref
                or not set(candidate.character_media_contract.allowed_capture_modes) <= set(character.capture_capabilities)
            ):
                continue
            matches.append((event, declaration, source))
        if len(matches) != 1:
            raise MediaEvidenceNotRenderable("p3_recipient_evidence_not_exactly_one")
        return matches[0]

    def _relationship_origin(self, *, projection, context: RelationshipMediaContextV1, recipient_ref: str) -> WorldEvent:
        relationship = next(
            (item for item in projection.relationship_states if item.relationship_id == context.audience.relationship_id),
            None,
        )
        origin_ref = context.audience.relationship_origin_event_ref
        if (
            relationship is None or relationship.subject_ref != recipient_ref
            or relationship.entity_revision != context.audience.relationship_revision
            or relationship.stage != context.audience.relationship_stage
            or origin_ref is None
        ):
            raise MediaEvidenceNotRenderable("p3_relationship_context_not_current")
        committed = next((item for item in projection.committed_world_event_refs if item.event_id == origin_ref), None)
        located = self._ledger.lookup_event_commit(origin_ref)
        if committed is None or located is None or located[0].payload_hash != committed.payload_hash:
            raise MediaEvidenceNotRenderable("p3_relationship_origin_unavailable")
        return located[0]

    def _freeze_historical_character_state(self, *, projection, candidate: PhotoCandidate,
                                           context: RelationshipMediaContextV1, at_logical_time,
                                           body: dict[str, object], origins: dict[str, tuple[WorldEvent, str]]) -> tuple[WorldEvent, ...]:
        contract = candidate.character_media_contract
        assert contract is not None
        physical = visible_physical_state_at(
            tuple(projection.visible_physical_states), subject_ref=contract.subject_ref,
            at_logical_time=at_logical_time,
        )
        basis = context.private_expression_basis
        if (
            physical is None or not physical.has_positive_cues
            or physical.physical_state_id != basis.physical_state_id
            or physical.entity_revision != basis.physical_state_revision
            or physical.source_event_ref != basis.source_event_ref
            or physical.source_event_payload_hash != basis.source_event_payload_hash
            or physical.visibility not in _RECIPIENT_SCOPED_VISIBILITIES
        ):
            raise MediaEvidenceNotRenderable("p3_embodied_basis_not_current")
        record, anchor = self._public_helpers._state_events(
            projection=projection, state=physical, event_type="VisiblePhysicalStateRecorded",
            payload_model=VisiblePhysicalStateRecordedPayload,
        )
        character = body["character"]
        assert isinstance(character, dict)
        character["visible_physical_state"] = physical.model_dump(mode="json")
        origins["/character/visible_physical_state"] = (record, physical.visibility)
        origins["/relationship_media_context/private_expression_basis"] = (record, physical.visibility)
        extras: list[WorldEvent] = [record, anchor]
        appearance = appearance_state_at(
            tuple(projection.appearance_states), subject_ref=contract.subject_ref,
            at_logical_time=at_logical_time,
        )
        if appearance is not None and appearance.visibility in _RECIPIENT_SCOPED_VISIBILITIES:
            appearance_record, appearance_anchor = self._public_helpers._state_events(
                projection=projection, state=appearance, event_type="AppearanceStateRecorded",
                payload_model=AppearanceStateRecordedPayload,
            )
            character["appearance_state"] = appearance.model_dump(mode="json")
            origins["/character/appearance_state"] = (appearance_record, appearance.visibility)
            extras.extend((appearance_record, appearance_anchor))
        return tuple(extras)


__all__ = ["PrivateMediaEvidenceCompileRequest", "PrivateMediaEvidenceSnapshotCompiler"]
