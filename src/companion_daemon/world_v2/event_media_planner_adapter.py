"""P0 one-way bridge from frozen World v2 evidence to ``event_media``.

This Module is intentionally *not* a World reader.  Its sole public seam is
the World v2 ``MediaPlanner`` interface: it opens an already immutable
opportunity sidecar, validates the embedded ``world-image-event-snapshot-v1``
contract, and asks the legacy image planner to interpret exactly those bytes.

It does not compile snapshots, choose candidates, create prompts, or read a
projection.  P0 admits only public/shareable ``life_share`` preview media.
The result-store seam is deliberately required for live use: without a
durable idempotency lookup the bridge reports unavailable rather than risking
another planner call after a crash.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Mapping, Protocol

from companion_daemon import event_media

from .media_v2 import (
    FrozenMediaEvidenceSnapshot,
    ImmutableMediaPayloadStore,
    MediaNotRenderable,
    MediaOpportunity,
    MediaPlan,
    MediaPlanningResult,
    StoredMediaPayload,
    canonical_media_json,
    media_digest,
    planning_request_id as expected_planning_request_id,
)


_OPPORTUNITY_CONTENT_TYPE = "application/vnd.world-v2.media-opportunity+json"
_PLAN_CONTENT_TYPE = "application/vnd.world-v2.media-plan+json"
_IMAGE_EVENT_SCHEMA = "world-image-event-snapshot-v1"
_PUBLIC_VISIBILITIES = frozenset({"public", "shareable"})
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
        if not self._p0_opportunity_is_authorized(opportunity):
            return self._not_renderable(opportunity, planning_request_id, "p0_opportunity_not_authorized")
        try:
            prior = await self._result_store.lookup(planning_request_id=planning_request_id)
        except Exception:
            return self._not_renderable(
                opportunity, planning_request_id, "planning_result_store_unavailable"
            )
        if prior is not None:
            return prior

        snapshot, error = self._load_image_event_snapshot(opportunity)
        if error is not None:
            return self._not_renderable(opportunity, planning_request_id, error)
        assert snapshot is not None
        legacy_opportunity = event_media.MediaOpportunity(
            opportunity_id=opportunity.opportunity_id,
            family="life_share",
            privacy_ceiling="ordinary",
            event_snapshot=snapshot,
            delivery_mode="preview",
            expression_requirements=(),
            audience_context=None,
            expression_charge_ceiling="none",
            private_expression_basis=None,
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

    def _p0_opportunity_is_authorized(self, opportunity: MediaOpportunity) -> bool:
        # ``privacy_ceiling`` is World visibility in the current persisted
        # model.  ``media_privacy_ceiling`` will be added by the snapshot
        # migration; accepting a future non-ordinary value here would silently
        # expand P0, so treat an absent field as the only compatible ordinary.
        return (
            opportunity.family == "life_share"
            and opportunity.delivery_mode == "preview"
            and opportunity.privacy_ceiling in _PUBLIC_VISIBILITIES
            and opportunity.media_lane == "ordinary_life"
            and opportunity.recipient_ref is None
            and opportunity.private_expression_basis_ref is None
            and getattr(opportunity, "media_privacy_ceiling", "ordinary") == "ordinary"
        )

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
        self, opportunity: MediaOpportunity
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
            # ``image_event_snapshot`` is an additive sidecar field owned by
            # the image-snapshot Module.  Older FrozenMediaEvidenceSnapshot
            # releases intentionally reject unknown fields, so validate the
            # established outer wire after taking this one embedded payload
            # out; we never reconstruct or mutate its bytes.
            if not isinstance(raw, dict):
                return None, "malformed_frozen_snapshot"
            outer_wire = dict(raw)
            outer_wire.pop("image_event_snapshot", None)
            # Canonical JSON represents the frozen tuple as an array while
            # the strict Pydantic wire model deliberately requires a tuple.
            # This is a wire-shape restoration, not a projection read.
            if isinstance(outer_wire.get("source_events"), list):
                outer_wire["source_events"] = tuple(outer_wire["source_events"])
            evidence = FrozenMediaEvidenceSnapshot.model_validate(outer_wire)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "malformed_frozen_snapshot"
        source_hashes = {item.event_ref: item.payload_hash for item in evidence.source_events}
        if tuple(sorted(source_hashes)) != opportunity.source_event_refs:
            return None, "frozen_snapshot_source_mismatch"
        snapshot = raw.get("image_event_snapshot")
        if not isinstance(snapshot, dict):
            return None, "missing_image_event_snapshot"
        error = self._validate_image_event_snapshot(snapshot=snapshot, source_hashes=source_hashes)
        return (snapshot, error) if error is not None else (snapshot, None)

    @staticmethod
    def _validate_image_event_snapshot(
        *, snapshot: Mapping[str, object], source_hashes: Mapping[str, str]
    ) -> str | None:
        required_mappings = (
            "event", "source", "location", "activity", "environment", "character",
            "visual_requirements", "evidence_index",
        )
        if snapshot.get("schema_version") != _IMAGE_EVENT_SCHEMA:
            return "unsupported_image_event_snapshot"
        if any(not isinstance(snapshot.get(name), dict) for name in required_mappings):
            return "malformed_image_event_snapshot"
        if not isinstance(snapshot.get("participants"), list) or not isinstance(snapshot.get("objects"), list):
            return "malformed_image_event_snapshot"
        if not isinstance(snapshot.get("existing_media"), list):
            return "malformed_image_event_snapshot"
        if snapshot.get("relationship_media_context") is not None:
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
            if (
                not isinstance(ref, str)
                or not isinstance(payload_hash, str)
                or visibility not in _PUBLIC_VISIBILITIES
                or source_hashes.get(ref) != payload_hash
            ):
                return "malformed_image_event_snapshot"
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
            or legacy_plan.family != "life_share"
            or legacy_plan.delivery_mode != "preview"
            or legacy_plan.snapshot_hash != inner_hash
        ):
            return self._not_renderable(opportunity, planning_request_id, "legacy_plan_binding_mismatch")
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
            family="life_share",
            planner_version=legacy_plan.version,
            schema_version=legacy_plan.version,
            media_machine_version=opportunity.media_machine_version,
            inspection_contract_version=opportunity.inspection_contract_version,
            media_lane="ordinary_life",
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


def _snapshot_leaves(value: object, pointer: str = "") -> set[str]:
    """Return RFC 6901 pointers for evidence-bearing leaves only."""
    if value is None:
        return set()
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


__all__ = ["EventMediaPlanningResultStore", "EventMediaPlannerAdapter"]
