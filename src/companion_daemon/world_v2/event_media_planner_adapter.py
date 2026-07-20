"""Frozen preview bridge from World v2 evidence to ``event_media``.

This Module is intentionally *not* a World reader.  Its sole public seam is
the World v2 ``MediaPlanner`` interface: it opens an already immutable
opportunity sidecar, validates the embedded ``world-image-event-snapshot-v1``
contract, and asks the legacy image planner to interpret exactly those bytes.

It does not compile snapshots, choose candidates, create prompts, or read a
projection.  It admits public/shareable P0 ``life_share``, fact-bound P2
``character_media``, and the narrow recipient-scoped P3 preview contract.
P2/P3 authorization remains outer-sidecar data and never reaches the legacy
planner's snapshot as free-form authority.
The result-store seam is deliberately required for live use: without a
durable idempotency lookup the bridge reports unavailable rather than risking
another planner call after a crash.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from threading import RLock
from typing import Mapping, Protocol

from companion_daemon import event_media
from companion_daemon.media_eligibility import PrivateExpressionBasis

from .media_v2 import (
    CharacterMediaSnapshotAuthorization,
    PrivateMediaSnapshotAuthorization,
    FrozenMediaEvidenceSnapshot,
    ImmutableMediaPayloadStore,
    MediaNotRenderable,
    MediaOpportunity,
    MediaPlan,
    MediaPlanningResult,
    StoredMediaPayload,
    PhotoCandidate,
    canonical_media_json,
    media_digest,
    media_payload_hash,
    planning_request_id as expected_planning_request_id,
)
from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


_OPPORTUNITY_CONTENT_TYPE = "application/vnd.world-v2.media-opportunity+json"
_PLAN_CONTENT_TYPE = "application/vnd.world-v2.media-plan+json"
_P0_IMAGE_EVENT_SCHEMA = "world-image-event-snapshot-v1"
_P2_IMAGE_EVENT_SCHEMA = "world-image-event-snapshot-v2"
_P3_IMAGE_EVENT_SCHEMA = "world-image-event-snapshot-v3"
_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})
_RECIPIENT_SCOPED_VISIBILITIES = frozenset({"personal", "private"})
_STRUCTURAL_SNAPSHOT_KEYS = frozenset({"schema_version", "evidence_index"})


class EventMediaPlanningResultStore(Protocol):
    """Durable terminal-result lookup owned by the composed provider adapter.

    The World v2 action worker performs ``lookup`` before ``plan``; the bridge
    repeats that check because the legacy planner accepts no idempotency key.
    A production adapter must implement this using a provider receipt store or
    equivalent durable database.  This Module deliberately provides no
    in-memory default.
    """

    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None: ...

    async def put_if_absent(
        self, *, planning_request_id: str, result: MediaPlanningResult
    ) -> None: ...


_RESULT_STORE_SCHEMA_VERSION = "world-v2.event-media-planning-result.v1"


class SQLiteEventMediaPlanningResultStore:
    """Durable, world-scoped terminal receipts for the legacy planner bridge.

    This adapter persists the *complete* World v2 terminal value, including an
    optional opaque plan sidecar.  It owns no planner policy and deliberately
    does not resolve the payload reference: a request id can only ever be
    rebound to byte-for-byte identical canonical result bytes.
    """

    def __init__(self, *, path: str, world_id: str) -> None:
        if not path or not world_id:
            raise ValueError("event-media planning result store needs path and world id")
        self._world_id = world_id
        self._lock = RLock()
        self._database_write_lock = sqlite_write_lock(path)
        # Autocommit: the default isolation level opens an implicit
        # transaction on any DML and keeps it (and its WAL read snapshot)
        # open until an explicit commit.  The idempotent-replay path below
        # (INSERT OR IGNORE with rowcount 0) used to return without one,
        # permanently pinning the shared WAL's checkpoint/reset point.
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._database_write_lock:
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_v2_event_media_planning_result (
                    world_id TEXT NOT NULL,
                    planning_request_id TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY (world_id, planning_request_id)
                )
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None:
        if not planning_request_id:
            raise ValueError("planning request id is required")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT result_hash, result_json
                FROM world_v2_event_media_planning_result
                WHERE world_id = ? AND planning_request_id = ?
                """,
                (self._world_id, planning_request_id),
            ).fetchone()
        if row is None:
            return None
        return _decode_result(
            planning_request_id=planning_request_id,
            result_hash=str(row[0]),
            result_json=str(row[1]),
        )

    async def put_if_absent(
        self, *, planning_request_id: str, result: MediaPlanningResult
    ) -> None:
        if not planning_request_id:
            raise ValueError("planning request id is required")
        encoded = _encode_result(planning_request_id=planning_request_id, result=result)
        result_hash = media_payload_hash(encoded)
        with self._database_write_lock, self._lock:
            inserted = self._connection.execute(
                """
                INSERT OR IGNORE INTO world_v2_event_media_planning_result
                    (world_id, planning_request_id, result_hash, result_json)
                VALUES (?, ?, ?, ?)
                """,
                (self._world_id, planning_request_id, result_hash, encoded),
            ).rowcount
            if inserted == 1:
                self._connection.commit()
                return
            row = self._connection.execute(
                """
                SELECT result_hash, result_json
                FROM world_v2_event_media_planning_result
                WHERE world_id = ? AND planning_request_id = ?
                """,
                (self._world_id, planning_request_id),
            ).fetchone()
        if row is None:
            raise RuntimeError("event-media planning result disappeared during immutable insert")
        stored = _decode_result(
            planning_request_id=planning_request_id,
            result_hash=str(row[0]),
            result_json=str(row[1]),
        )
        if stored != result:
            raise ValueError("planning request id is already bound to a different immutable terminal result")


def _encode_result(*, planning_request_id: str, result: MediaPlanningResult) -> str:
    """Encode the closed ``MediaPlanningResult`` union with explicit wire version."""

    terminal_request_id = (
        result.plan.planning_request_id if result.plan is not None
        else result.not_renderable.planning_request_id if result.not_renderable is not None
        else None
    )
    if terminal_request_id != planning_request_id:
        raise ValueError("planning result terminal value does not match its request id")
    plan_payload = result.plan_payload
    wire: dict[str, object] = {
        "schema_version": _RESULT_STORE_SCHEMA_VERSION,
        "planning_request_id": planning_request_id,
        "plan": result.plan.model_dump(mode="json") if result.plan is not None else None,
        "not_renderable": (
            result.not_renderable.model_dump(mode="json")
            if result.not_renderable is not None else None
        ),
        "plan_payload": (
            {
                "payload_ref": plan_payload.payload_ref,
                "payload_hash": plan_payload.payload_hash,
                "content_type": plan_payload.content_type,
                "body": plan_payload.body,
            }
            if plan_payload is not None else None
        ),
    }
    return canonical_media_json(wire)


def _decode_result(
    *, planning_request_id: str, result_hash: str, result_json: str
) -> MediaPlanningResult:
    """Restore and validate a result without trusting SQLite's stored text."""

    if result_hash != media_payload_hash(result_json):
        raise ValueError("event-media planning result hash does not bind stored bytes")
    try:
        value = json.loads(result_json)
        if not isinstance(value, dict) or set(value) != {
            "schema_version", "planning_request_id", "plan", "not_renderable", "plan_payload",
        }:
            raise ValueError("invalid result wire shape")
        if (
            value["schema_version"] != _RESULT_STORE_SCHEMA_VERSION
            or value["planning_request_id"] != planning_request_id
        ):
            raise ValueError("result wire is bound to a different planning request")
        plan_value = value["plan"]
        not_renderable_value = value["not_renderable"]
        payload_value = value["plan_payload"]
        plan = MediaPlan.model_validate_json(canonical_media_json(plan_value)) if plan_value is not None else None
        not_renderable = (
            MediaNotRenderable.model_validate_json(canonical_media_json(not_renderable_value))
            if not_renderable_value is not None else None
        )
        if payload_value is not None and not isinstance(payload_value, dict):
            raise ValueError("plan payload wire must be an object or null")
        plan_payload = (
            StoredMediaPayload(
                payload_ref=str(payload_value["payload_ref"]),
                payload_hash=str(payload_value["payload_hash"]),
                content_type=str(payload_value["content_type"]),
                body=str(payload_value["body"]),
            )
            if payload_value is not None else None
        )
        return MediaPlanningResult(
            plan=plan,
            not_renderable=not_renderable,
            plan_payload=plan_payload,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("stored event-media planning result is malformed") from exc


class EventMediaPlannerAdapter:
    """Validate one frozen P0 opportunity and delegate it once to image v5."""

    def __init__(
        self,
        *,
        sidecar: ImmutableMediaPayloadStore,
        legacy_planner: event_media.MediaPlanner,
        result_store: EventMediaPlanningResultStore | None = None,
    ) -> None:
        self._sidecar = sidecar
        self._legacy_planner = legacy_planner
        self._result_store = result_store

    async def lookup(self, *, planning_request_id: str) -> MediaPlanningResult | None:
        if self._result_store is None:
            return None
        return await self._result_store.lookup(planning_request_id=planning_request_id)

    async def plan(
        self, *, opportunity: MediaOpportunity, planning_request_id: str
    ) -> MediaPlanningResult:
        if self._result_store is None:
            return self._not_renderable(
                opportunity, planning_request_id, "planning_result_store_unavailable"
            )
        if planning_request_id != expected_planning_request_id(opportunity.opportunity_id):
            return self._not_renderable(
                opportunity, planning_request_id, "planning_request_id_mismatch"
            )
        lane = self._authorized_lane(opportunity)
        if lane is None:
            # Preserve P0's public error contract for legacy/invalid
            # opportunities.  A valid P2 opportunity reaches its distinct
            # authorization checks below.
            return self._not_renderable(opportunity, planning_request_id, "p0_opportunity_not_authorized")
        try:
            prior = await self._result_store.lookup(planning_request_id=planning_request_id)
        except Exception:
            return self._not_renderable(
                opportunity, planning_request_id, "planning_result_store_unavailable"
            )
        if prior is not None:
            return prior

        snapshot, error = self._load_image_event_snapshot(opportunity, lane=lane)
        if error is not None:
            return self._not_renderable(opportunity, planning_request_id, error)
        assert snapshot is not None
        p3_authorization = self._p3_authorization_from_sidecar(opportunity) if lane == "p3" else None
        if lane == "p3" and p3_authorization is None:
            return self._not_renderable(opportunity, planning_request_id, "p3_private_authorization_missing")
        legacy_opportunity = event_media.MediaOpportunity(
            opportunity_id=opportunity.opportunity_id,
            family=opportunity.family,
            privacy_ceiling=("intimate" if lane == "p3" else "ordinary"),
            event_snapshot=snapshot,
            delivery_mode="preview",
            expression_requirements=(),
            audience_context=(self._p3_audience(snapshot) if lane == "p3" else None),
            expression_charge_ceiling=(p3_authorization.expression_charge_ceiling if p3_authorization is not None else "none"),
            private_expression_basis=(self._p3_basis(snapshot) if lane == "p3" else None),
            allowed_evidence_refs=tuple(sorted(_snapshot_leaves(snapshot))),
        )
        try:
            legacy_result = await self._legacy_planner.plan(legacy_opportunity, recent_media=())
        except Exception:
            return await self._store_terminal(
                planning_request_id=planning_request_id,
                result=self._not_renderable(opportunity, planning_request_id, "legacy_planner_failed"),
            )
        result = self._translate_legacy_result(
            opportunity=opportunity,
            planning_request_id=planning_request_id,
            image_event_snapshot=snapshot,
            legacy_result=legacy_result,
        )
        return await self._store_terminal(planning_request_id=planning_request_id, result=result)

    def _authorized_lane(self, opportunity: MediaOpportunity) -> str | None:
        # ``privacy_ceiling`` is World visibility in the current persisted
        # model.  ``media_privacy_ceiling`` will be added by the snapshot
        # migration; accepting a future non-ordinary value here would silently
        # expand P0, so treat an absent field as the only compatible ordinary.
        if (
            opportunity.family == "life_share"
            and opportunity.delivery_mode == "preview"
            and opportunity.privacy_ceiling in _PUBLIC_VISIBILITIES
            and opportunity.media_lane == "ordinary_life"
            and opportunity.recipient_ref is None
            and opportunity.private_expression_basis_ref is None
            and getattr(opportunity, "media_privacy_ceiling", "ordinary") == "ordinary"
        ):
            return "p0"
        if (
            opportunity.family == "character_media"
            and opportunity.delivery_mode == "preview"
            and opportunity.privacy_ceiling in _PUBLIC_VISIBILITIES
            and opportunity.media_lane == "ordinary_life"
            and opportunity.recipient_ref is None
            and opportunity.private_expression_basis_ref is None
            and opportunity.media_privacy_ceiling == "ordinary"
            and bool(opportunity.candidate_source_event_refs)
            and bool(opportunity.snapshot_source_events)
        ):
            return "p2"
        if (
            opportunity.family == "character_media"
            and opportunity.delivery_mode == "preview"
            and opportunity.privacy_ceiling == "private"
            and opportunity.media_privacy_ceiling == "intimate"
            and opportunity.media_lane in {"alluring_life", "exclusive_private"}
            and opportunity.recipient_ref is not None
            and opportunity.private_expression_basis_ref is not None
            and opportunity.p3_authorization_digest is not None
            and bool(opportunity.candidate_source_event_refs)
            and bool(opportunity.snapshot_source_events)
        ):
            return "p3"
        return None

    async def _store_terminal(
        self, *, planning_request_id: str, result: MediaPlanningResult
    ) -> MediaPlanningResult:
        assert self._result_store is not None
        try:
            await self._result_store.put_if_absent(
                planning_request_id=planning_request_id, result=result
            )
            stored = await self._result_store.lookup(planning_request_id=planning_request_id)
        except Exception:
            # The legacy planner result must not escape as replay-safe unless
            # it reached the durable store.  Returning a terminal unavailable
            # value makes the worker record the absence rather than retrying
            # an untracked model call.
            return self._not_renderable_from_result(result, "planning_result_store_unavailable")
        return stored if stored is not None else self._not_renderable_from_result(
            result, "planning_result_store_unavailable"
        )

    def _load_image_event_snapshot(
        self, opportunity: MediaOpportunity, *, lane: str
    ) -> tuple[dict[str, object] | None, str | None]:
        record = self._sidecar.read_exact(payload_ref=opportunity.event_snapshot_ref)
        if (
            record is None
            or record.payload_hash != opportunity.event_snapshot_hash
            or record.content_type != _OPPORTUNITY_CONTENT_TYPE
        ):
            return None, "frozen_snapshot_unavailable"
        try:
            raw = json.loads(record.body)
            if not isinstance(raw, dict):
                return None, "malformed_frozen_snapshot"
            # Validate the whole immutable wire.  In particular, P2's outer
            # authorization is only valid when it is paired with its V2 inner
            # snapshot; stripping that inner value would weaken the contract.
            evidence = FrozenMediaEvidenceSnapshot.model_validate_json(record.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Preserve the legacy diagnostic boundary: once the outer record
            # exists and carries an inner image snapshot object, malformed
            # inner provenance is reported as an image-snapshot failure.
            return None, (
                "malformed_image_event_snapshot"
                if isinstance(raw.get("image_event_snapshot"), dict)
                else "malformed_frozen_snapshot"
            )
        source_hashes = {item.event_ref: item.payload_hash for item in evidence.source_events}
        if tuple(sorted(source_hashes)) != opportunity.source_event_refs:
            return None, "frozen_snapshot_source_mismatch"
        if opportunity.snapshot_source_events and evidence.source_events != opportunity.snapshot_source_events:
            return None, "frozen_snapshot_hash_lineage_mismatch"
        if lane == "p2":
            error = self._validate_p2_outer_authorization(
                opportunity=opportunity, evidence=evidence,
            )
            if error is not None:
                return None, error
        if lane == "p3":
            error = self._validate_p3_outer_authorization(opportunity=opportunity, evidence=evidence)
            if error is not None:
                return None, error
        snapshot = raw.get("image_event_snapshot")
        if not isinstance(snapshot, dict):
            return None, "missing_image_event_snapshot"
        error = self._validate_image_event_snapshot(
            snapshot=snapshot, source_hashes=source_hashes, lane=lane,
        )
        return (snapshot, error) if error is not None else (snapshot, None)

    @staticmethod
    def _validate_p3_outer_authorization(
        *, opportunity: MediaOpportunity, evidence: FrozenMediaEvidenceSnapshot,
    ) -> str | None:
        authorization = evidence.private_media_authorization
        if authorization is None or evidence.complete_candidate is None:
            return "p3_private_authorization_missing"
        try:
            candidate = PhotoCandidate.model_validate_json(canonical_media_json(evidence.complete_candidate))
        except ValueError:
            return "p3_complete_candidate_malformed"
        snapshot = evidence.image_event_snapshot
        context = getattr(snapshot, "relationship_media_context", None)
        contract = candidate.character_media_contract
        if (
            snapshot is None or snapshot.schema_version != _P3_IMAGE_EVENT_SCHEMA
            or context is None or contract is None
            or candidate.candidate_id != opportunity.candidate_id
            or candidate.entity_revision != authorization.candidate_revision
            or candidate.source_event_refs != opportunity.candidate_source_event_refs
            or authorization.candidate_id != candidate.candidate_id
            or authorization.candidate_contract_digest != contract.authority_digest
            or authorization.recipient_ref != opportunity.recipient_ref
            or authorization.media_lane != opportunity.media_lane
            or authorization.authorization_digest != opportunity.p3_authorization_digest
            or authorization.relationship_context_digest != context.authority_digest
            or authorization.private_basis_digest != context.private_expression_basis.basis_digest
            or context.audience.recipient_ref != opportunity.recipient_ref
            or context.private_expression_basis.basis_id != opportunity.private_expression_basis_ref
            or authorization.source_event_refs != opportunity.source_event_refs
            or not set(authorization.allowed_capture_modes) <= {"character_front_camera", "mirror"}
        ):
            return "p3_private_authorization_mismatch"
        return None

    @staticmethod
    def _validate_p2_outer_authorization(
        *, opportunity: MediaOpportunity, evidence: FrozenMediaEvidenceSnapshot,
    ) -> str | None:
        authorization = evidence.character_media_authorization
        if authorization is None or evidence.complete_candidate is None:
            return "p2_character_authorization_missing"
        try:
            candidate = PhotoCandidate.model_validate_json(
                canonical_media_json(evidence.complete_candidate)
            )
        except ValueError:
            return "p2_complete_candidate_malformed"
        contract = candidate.character_media_contract
        if (
            candidate.family != "character_media"
            or contract is None
            or candidate.candidate_id != opportunity.candidate_id
            or candidate.source_event_refs != opportunity.candidate_source_event_refs
            or authorization != CharacterMediaSnapshotAuthorization(
                candidate_id=candidate.candidate_id,
                candidate_revision=candidate.entity_revision,
                subject_ref=contract.subject_ref,
                kind=contract.kind,
                allowed_capture_modes=contract.allowed_capture_modes,
                allowed_character_visibility=contract.allowed_character_visibility,
                authority_digest=contract.authority_digest,
                source_event_refs=candidate.source_event_refs,
            )
            or not set(candidate.source_event_refs).issubset(opportunity.source_event_refs)
        ):
            return "p2_character_authorization_mismatch"
        return None

    @staticmethod
    def _validate_image_event_snapshot(
        *, snapshot: Mapping[str, object], source_hashes: Mapping[str, str], lane: str
    ) -> str | None:
        required_mappings = (
            "event", "source", "location", "activity", "environment", "character",
            "visual_requirements", "evidence_index",
        )
        expected_schema = (
            _P0_IMAGE_EVENT_SCHEMA if lane == "p0" else _P2_IMAGE_EVENT_SCHEMA
            if lane == "p2" else _P3_IMAGE_EVENT_SCHEMA
        )
        if snapshot.get("schema_version") != expected_schema:
            return "unsupported_image_event_snapshot"
        if lane == "p2" and (
            "character_media_authorization" in snapshot
            or {"capture_authorization", "candidate_contract"}.intersection(
                snapshot.get("character", {}) if isinstance(snapshot.get("character"), dict) else {}
            )
        ):
            return "p2_authorization_leaked_into_planner_snapshot"
        if any(not isinstance(snapshot.get(name), dict) for name in required_mappings):
            return "malformed_image_event_snapshot"
        if not isinstance(snapshot.get("participants"), list) or not isinstance(snapshot.get("objects"), list):
            return "malformed_image_event_snapshot"
        if not isinstance(snapshot.get("existing_media"), list):
            return "malformed_image_event_snapshot"
        if lane != "p3" and snapshot.get("relationship_media_context") is not None:
            # P0 must not allow relationship/audience information into the
            # legacy planner, even if an upstream writer accidentally froze it.
            return "p0_private_media_context_not_authorized"
        event = snapshot["event"]
        assert isinstance(event, dict)
        if event.get("status") != "committed" or not isinstance(event.get("event_id"), str):
            return "malformed_image_event_snapshot"
        visual_requirements = snapshot["visual_requirements"]
        assert isinstance(visual_requirements, dict)
        if visual_requirements.get("requires_readable_text") is True:
            # Artifact reuse verification is a later P0 compiler lane.  Until
            # then a bridge cannot turn a textual description into an image.
            return "readable_text_requires_artifact"
        existing_media = snapshot["existing_media"]
        assert isinstance(existing_media, list)
        if existing_media:
            # The World snapshot deliberately carries an artifact ref/hash,
            # never a mutable local path.  P0 has no provider-backed lookup
            # port yet, so exposing this to the legacy planner would let it
            # treat metadata as a reusable file.  Reject rather than invent a
            # path or silently switch to a generated image.
            return "existing_media_lookup_unavailable"
        evidence_index = snapshot["evidence_index"]
        assert isinstance(evidence_index, dict)
        leaves = _snapshot_leaves(snapshot)
        if not leaves or set(leaves) != set(evidence_index):
            return "malformed_image_event_snapshot"
        for pointer, entry in evidence_index.items():
            if not isinstance(pointer, str) or not isinstance(entry, dict):
                return "malformed_image_event_snapshot"
            ref, payload_hash, visibility = (
                entry.get("source_event_ref"),
                entry.get("source_payload_hash"),
                entry.get("visibility"),
            )
            allowed_visibilities = (
                _RECIPIENT_SCOPED_VISIBILITIES if lane == "p3" else _PUBLIC_VISIBILITIES
            )
            if (
                not isinstance(ref, str)
                or not isinstance(payload_hash, str)
                or visibility not in allowed_visibilities
                or source_hashes.get(ref) != payload_hash
            ):
                return "malformed_image_event_snapshot"
        if lane == "p3" and not isinstance(snapshot.get("relationship_media_context"), dict):
            return "p3_relationship_media_context_missing"
        return None

    def _translate_legacy_result(
        self,
        *,
        opportunity: MediaOpportunity,
        planning_request_id: str,
        image_event_snapshot: dict[str, object],
        legacy_result: event_media.PlanningResult,
    ) -> MediaPlanningResult:
        if isinstance(legacy_result, event_media.NotRenderable):
            return self._not_renderable(
                opportunity, planning_request_id, _reason_code(legacy_result.reason)
            )
        if not isinstance(legacy_result, event_media.PlannedMedia):
            return self._not_renderable(opportunity, planning_request_id, "invalid_legacy_planner_result")
        legacy_plan = legacy_result.plan
        inner_hash = hashlib.sha256(canonical_media_json(image_event_snapshot).encode("utf-8")).hexdigest()
        if (
            legacy_plan.opportunity_id != opportunity.opportunity_id
            or legacy_plan.family != opportunity.family
            or legacy_plan.delivery_mode != "preview"
            or legacy_plan.snapshot_hash != inner_hash
        ):
            return self._not_renderable(opportunity, planning_request_id, "legacy_plan_binding_mismatch")
        if opportunity.family == "character_media" and opportunity.media_lane == "ordinary_life":
            authorization = self._p2_authorization_from_sidecar(opportunity)
            if authorization is None:
                return self._not_renderable(opportunity, planning_request_id, "p2_character_authorization_missing")
            lane = getattr(legacy_plan.media_lane, "lane", "ordinary_life")
            if (
                legacy_plan.privacy != "ordinary"
                or legacy_plan.capture_mode not in authorization.allowed_capture_modes
                or legacy_plan.character_visibility not in authorization.allowed_character_visibility
                or lane != "ordinary_life"
                or legacy_plan.private_expression_basis is not None
            ):
                return self._not_renderable(opportunity, planning_request_id, "p2_legacy_plan_exceeds_authorization")
        if opportunity.family == "character_media" and opportunity.media_lane in {"alluring_life", "exclusive_private"}:
            authorization = self._p3_authorization_from_sidecar(opportunity)
            context = image_event_snapshot.get("relationship_media_context")
            lane = getattr(legacy_plan.media_lane, "lane", "")
            expected_basis = (
                context.get("private_expression_basis")
                if isinstance(context, dict) else None
            )
            if (
                authorization is None
                or not isinstance(context, dict)
                or not isinstance(expected_basis, dict)
                or legacy_plan.privacy != "intimate"
                or legacy_plan.capture_mode not in authorization.allowed_capture_modes
                or lane != authorization.media_lane
                or legacy_plan.private_expression_basis is None
                or legacy_plan.private_expression_basis.kind != expected_basis.get("kind")
                or legacy_plan.private_expression_basis.evidence_ref != expected_basis.get("evidence_ref")
                or legacy_plan.private_expression_basis.recipient_ref != opportunity.recipient_ref
            ):
                return self._not_renderable(opportunity, planning_request_id, "p3_legacy_plan_exceeds_authorization")
        body = canonical_media_json(legacy_plan.to_payload())
        payload_ref = "sidecar:media-plan:" + media_digest({
            "opportunity_id": opportunity.opportunity_id,
            "planning_request_id": planning_request_id,
            "image_event_snapshot_hash": inner_hash,
        })
        payload = StoredMediaPayload(
            payload_ref=payload_ref,
            payload_hash="sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest(),
            content_type=_PLAN_CONTENT_TYPE,
            body=body,
        )
        try:
            frozen_at = _snapshot_logical_time(image_event_snapshot)
        except ValueError:
            return self._not_renderable(opportunity, planning_request_id, "malformed_image_event_snapshot")
        return MediaPlanningResult(plan=MediaPlan(
            plan_id=legacy_plan.plan_id,
            planning_request_id=planning_request_id,
            opportunity_id=opportunity.opportunity_id,
            event_snapshot_hash=opportunity.event_snapshot_hash,
            family=opportunity.family,
            planner_version=legacy_plan.version,
            schema_version=legacy_plan.version,
            media_machine_version=opportunity.media_machine_version,
            inspection_contract_version=opportunity.inspection_contract_version,
            media_lane=(opportunity.media_lane if opportunity.media_lane != "ordinary_life" else "ordinary_life"),
            plan_payload_ref=payload.payload_ref,
            plan_payload_hash=payload.payload_hash,
            frozen_at=frozen_at,
        ), plan_payload=payload)

    @staticmethod
    def _not_renderable(
        opportunity: MediaOpportunity, planning_request_id: str, reason_code: str
    ) -> MediaPlanningResult:
        return MediaPlanningResult(not_renderable=MediaNotRenderable(
            opportunity_id=opportunity.opportunity_id,
            planning_request_id=planning_request_id,
            event_snapshot_hash=opportunity.event_snapshot_hash,
            reason_code=reason_code,
            planner_version="event-media-planner-adapter.p0",
        ))

    def _not_renderable_from_result(
        self, result: MediaPlanningResult, reason_code: str
    ) -> MediaPlanningResult:
        if result.plan is not None:
            return MediaPlanningResult(not_renderable=MediaNotRenderable(
                opportunity_id=result.plan.opportunity_id,
                planning_request_id=result.plan.planning_request_id,
                event_snapshot_hash=result.plan.event_snapshot_hash,
                reason_code=reason_code,
                planner_version="event-media-planner-adapter.p0",
            ))
        assert result.not_renderable is not None
        return MediaPlanningResult(not_renderable=result.not_renderable.model_copy(update={"reason_code": reason_code}))

    def _p2_authorization_from_sidecar(
        self, opportunity: MediaOpportunity,
    ) -> CharacterMediaSnapshotAuthorization | None:
        record = self._sidecar.read_exact(payload_ref=opportunity.event_snapshot_ref)
        if record is None or record.payload_hash != opportunity.event_snapshot_hash:
            return None
        try:
            raw = json.loads(record.body)
            if not isinstance(raw, dict):
                return None
            return FrozenMediaEvidenceSnapshot.model_validate_json(
                record.body
            ).character_media_authorization
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _p3_authorization_from_sidecar(
        self, opportunity: MediaOpportunity,
    ) -> PrivateMediaSnapshotAuthorization | None:
        record = self._sidecar.read_exact(payload_ref=opportunity.event_snapshot_ref)
        if record is None or record.payload_hash != opportunity.event_snapshot_hash:
            return None
        try:
            return FrozenMediaEvidenceSnapshot.model_validate_json(record.body).private_media_authorization
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _p3_audience(snapshot: Mapping[str, object]) -> event_media.AudienceContext:
        context = snapshot.get("relationship_media_context")
        if not isinstance(context, Mapping) or not isinstance(context.get("audience"), Mapping):
            raise ValueError("p3 relationship context is missing")
        audience = context["audience"]
        return event_media.AudienceContext(
            recipient_ref=str(audience.get("recipient_ref") or ""),
            relationship_stage=str(audience.get("relationship_stage") or ""),
        )

    @staticmethod
    def _p3_basis(snapshot: Mapping[str, object]) -> PrivateExpressionBasis:
        context = snapshot.get("relationship_media_context")
        if not isinstance(context, Mapping) or not isinstance(context.get("private_expression_basis"), Mapping):
            raise ValueError("p3 private basis is missing")
        basis = context["private_expression_basis"]
        return PrivateExpressionBasis(
            kind=str(basis.get("kind") or ""),
            evidence_refs=(str(basis.get("evidence_ref") or ""),),
            required_charge=str(basis.get("required_charge") or ""),
        )


def _snapshot_leaves(value: object, pointer: str = "") -> set[str]:
    """Return RFC 6901 pointers for evidence-bearing leaves only."""
    if value is None:
        # The top-level absent relationship slot is wire structure in P0/P2.
        # Nested nulls, however, are explicit values in a V3 typed context and
        # must match the compiler's closed evidence index exactly.
        return set() if pointer == "/relationship_media_context" else {pointer}
    if isinstance(value, dict):
        leaves: set[str] = set()
        for key, item in value.items():
            if pointer == "" and key in _STRUCTURAL_SNAPSHOT_KEYS:
                continue
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            leaves |= _snapshot_leaves(item, pointer + "/" + escaped)
        return leaves
    if isinstance(value, list):
        leaves: set[str] = set()
        for index, item in enumerate(value):
            leaves |= _snapshot_leaves(item, pointer + "/" + str(index))
        return leaves
    return {pointer}


def _snapshot_logical_time(snapshot: Mapping[str, object]) -> datetime:
    event = snapshot.get("event")
    if not isinstance(event, Mapping) or not isinstance(event.get("logical_at"), str):
        raise ValueError("image snapshot has no event logical time")
    value = datetime.fromisoformat(event["logical_at"].replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("image snapshot logical time must be timezone-aware")
    return value


def _reason_code(value: object) -> str:
    """Map legacy free-form reasons into the bounded World result field."""
    if not isinstance(value, str):
        return "legacy_not_renderable"
    compact = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return compact[:128] or "legacy_not_renderable"


__all__ = [
    "EventMediaPlanningResultStore",
    "EventMediaPlannerAdapter",
    "SQLiteEventMediaPlanningResultStore",
]
