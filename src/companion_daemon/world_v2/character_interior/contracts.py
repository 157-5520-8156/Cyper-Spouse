"""Public, source-bound contracts for the unified Character Interior.

The snapshot is a read-only interpretation of an already committed World head.
It is not another authority store.  Character-authored effects remain sparse
typed proposals and must pass their existing domain authorities.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from types import MappingProxyType
from typing import Annotated, Any, Literal, Mapping

from pydantic import Field, field_validator, model_validator

from ..schema_core import FrozenModel, PrivacyClass, canonicalize_json_value
from ..schemas import ProjectionCursor
from ..recall_audit import PrefetchPresentationAudit


FACET_NAMES = (
    "private_self",
    "selective_memory",
    "appraisal_affect",
    "emotional_continuity",
    "subjective_relationship",
    "aspirations_conflicts",
    "autonomous_impulses",
    "expression_stance",
)


_AffectDimension = Literal[
    "hurt",
    "anger",
    "sadness",
    "loneliness",
    "anxiety",
    "resentment",
    "warmth",
    "joy",
]


class InteriorAffectNewComponentTarget(FrozenModel):
    """A role-authored target for a newly opened Affect component."""

    dimension: _AffectDimension
    target_intensity_bp: int = Field(ge=1, le=10_000)


class InteriorAffectExistingComponentTarget(FrozenModel):
    """A role-authored revision of one exact component offered at this cursor."""

    component_id: str = Field(min_length=1, max_length=512)
    dimension: _AffectDimension
    target_intensity_bp: int = Field(ge=1, le=10_000)


class InteriorAffectOpenTransition(FrozenModel):
    operation: Literal["open"]
    component_targets: list[InteriorAffectNewComponentTarget] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dimensions_are_unique(self) -> "InteriorAffectOpenTransition":
        dimensions = tuple(item.dimension for item in self.component_targets)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("new Affect component dimensions must be unique")
        return self


class InteriorAffectUpdateTransition(FrozenModel):
    operation: Literal["update"]
    episode_id: str = Field(min_length=1, max_length=512)
    component_targets: list[InteriorAffectExistingComponentTarget] = Field(
        min_length=1, max_length=8
    )

    @model_validator(mode="after")
    def component_ids_are_unique(self) -> "InteriorAffectUpdateTransition":
        component_ids = tuple(item.component_id for item in self.component_targets)
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("updated Affect component identities must be unique")
        return self


class InteriorAffectResolveTransition(FrozenModel):
    operation: Literal["resolve"]
    episode_id: str = Field(min_length=1, max_length=512)
    resolution_summary: str = Field(min_length=1, max_length=1_200)


class InteriorAffectSupersedeTransition(FrozenModel):
    operation: Literal["supersede"]
    episode_id: str = Field(min_length=1, max_length=512)
    component_targets: list[InteriorAffectNewComponentTarget] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def dimensions_are_unique(self) -> "InteriorAffectSupersedeTransition":
        dimensions = tuple(item.dimension for item in self.component_targets)
        if len(dimensions) != len(set(dimensions)):
            raise ValueError("successor Affect component dimensions must be unique")
        return self


InteriorAffectTransition = Annotated[
    InteriorAffectOpenTransition
    | InteriorAffectUpdateTransition
    | InteriorAffectResolveTransition
    | InteriorAffectSupersedeTransition,
    Field(discriminator="operation"),
]


def _canonical_json(value: object) -> str:
    def json_value(item: object) -> object:
        if isinstance(item, dict):
            return {key: json_value(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [json_value(child) for child in item]
        if isinstance(item, datetime):
            return canonicalize_json_value(item)
        return item

    try:
        return json.dumps(
            json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("character interior material must be canonical JSON data") from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _decoded_object(payload_json: str) -> Mapping[str, object]:
    decoded = json.loads(payload_json)
    if not isinstance(decoded, dict):
        raise ValueError("character interior view content must be an object")
    # Return a fresh, shallow read-only copy. Nested mutation cannot alter the
    # authoritative payload_json or its bound hash.
    return MappingProxyType(decoded)


def _redact_materials(
    materials: dict[str, object], visible_source_refs: set[str]
) -> dict[str, object]:
    """Delete non-visible source items while preserving explicit absence."""

    redacted: dict[str, object] = {}
    for key, value in materials.items():
        if isinstance(value, list):
            retained = [
                item
                for item in value
                if not isinstance(item, dict)
                or not isinstance(item.get("source_ref"), str)
                or item["source_ref"] in visible_source_refs
            ]
            if retained:
                redacted[key] = retained
            continue
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            retained = [
                item
                for item in value["items"]
                if not isinstance(item, dict)
                or not isinstance(item.get("source_ref"), str)
                or item["source_ref"] in visible_source_refs
            ]
            if retained:
                redacted[key] = {**value, "items": retained}
            elif value.get("availability") == "unavailable":
                redacted[key] = value
            continue
        redacted[key] = value
    return redacted


class _InteriorContextView(FrozenModel):
    availability: Literal["available", "unavailable"]
    payload_json: str
    source_refs: tuple[str, ...] = ()
    content_hash: str = Field(min_length=64, max_length=64)

    @property
    def content(self) -> Mapping[str, object]:
        return _decoded_object(self.payload_json)

    @model_validator(mode="after")
    def source_and_payload_are_consistent(self) -> "_InteriorContextView":
        decoded = json.loads(self.payload_json)
        if not isinstance(decoded, dict) or self.payload_json != _canonical_json(decoded):
            raise ValueError("character interior view payload must be a canonical object")
        if self.content_hash != _digest(decoded):
            raise ValueError("character interior view content hash is invalid")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("character interior view source refs must be unique")
        if self.availability == "available" and not self.source_refs:
            raise ValueError("available character interior view must be source-bound")
        if self.availability == "unavailable" and (decoded or self.source_refs):
            raise ValueError("unavailable character interior view cannot claim material")
        return self

    @classmethod
    def from_material(
        cls,
        *,
        availability: str,
        content: Mapping[str, Any],
        source_refs: tuple[str, ...],
    ) -> "_InteriorContextView":
        payload_json = _canonical_json(dict(content))
        return cls(
            availability=availability,
            payload_json=payload_json,
            source_refs=source_refs,
            content_hash=_digest(dict(content)),
        )


class _InteriorFacet(_InteriorContextView):
    name: str

    @field_validator("name")
    @classmethod
    def name_is_one_of_the_contract_facets(cls, value: str) -> str:
        if value not in FACET_NAMES:
            raise ValueError("unknown character interior facet")
        return value


class _InteriorSourceInventoryItem(FrozenModel):
    """One semantic item plus its exact upstream authority envelope.

    ``authority_bindings`` retain the cryptographic Capsule proof for snapshot
    identity/replay.  ``model_view`` deliberately exposes only compact refs
    and lifecycle coordinates so proof bookkeeping does not crowd semantic
    character context.
    """

    source_ref: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    privacy_class: str | None = Field(default=None, min_length=1, max_length=64)
    authority_scope: str | None = Field(default=None, min_length=1, max_length=128)
    authority_bindings: tuple["_InteriorSourceAuthorityBinding", ...] = ()
    direct_source_refs: tuple[str, ...] = ()
    entity_revision: int | None = Field(default=None, ge=1)
    valid_from: str | None = Field(default=None, min_length=1, max_length=64)
    valid_to: str | None = Field(default=None, min_length=1, max_length=64)
    expires_at: str | None = Field(default=None, min_length=1, max_length=64)
    predecessor_refs: tuple[str, ...] = ()
    conflict_refs: tuple[str, ...] = ()
    revision_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def temporal_bounds_are_explicit_and_ordered(self) -> "_InteriorSourceInventoryItem":
        binding_identities = tuple(
            (
                item.source_kind,
                item.authority_type,
                item.ref,
                item.source_world_revision,
                item.immutable_hash,
            )
            for item in self.authority_bindings
        )
        if binding_identities != tuple(sorted(set(binding_identities))):
            raise ValueError("character interior authority bindings must be canonical")
        for label, refs in (
            ("direct source", self.direct_source_refs),
            ("predecessor", self.predecessor_refs),
            ("conflict", self.conflict_refs),
            ("revision", self.revision_refs),
        ):
            if refs != tuple(dict.fromkeys(refs)) or any(not ref for ref in refs):
                raise ValueError(f"character interior {label} refs must be unique and non-empty")
        authority_refs = {item.ref for item in self.authority_bindings}
        if authority_refs and not authority_refs.issubset(set(self.direct_source_refs)):
            raise ValueError("character interior direct refs omit an authority binding")
        parsed: dict[str, datetime] = {}
        for field_name in ("valid_from", "valid_to", "expires_at"):
            raw = getattr(self, field_name)
            if raw is None:
                continue
            try:
                value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(
                    f"character interior source {field_name} must be ISO datetime"
                ) from exc
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"character interior source {field_name} must be timezone-aware")
            parsed[field_name] = value
        if (
            "valid_from" in parsed
            and "valid_to" in parsed
            and parsed["valid_to"] < parsed["valid_from"]
        ):
            raise ValueError("character interior source validity window is inverted")
        return self

    def model_view(self) -> dict[str, object]:
        """Compact semantic provenance; immutable hashes stay snapshot-private."""

        value: dict[str, object] = {
            "source_ref": self.source_ref,
            "scope": self.scope,
            "content_hash": self.content_hash,
        }
        optional = {
            "privacy_class": self.privacy_class,
            "authority_scope": self.authority_scope,
            "entity_revision": self.entity_revision,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "expires_at": self.expires_at,
        }
        value.update({key: item for key, item in optional.items() if item is not None})
        if self.authority_bindings:
            value["authority_refs"] = list(
                dict.fromkeys(item.ref for item in self.authority_bindings)
            )
        for key, refs in (
            ("direct_source_refs", self.direct_source_refs),
            ("predecessor_refs", self.predecessor_refs),
            ("conflict_refs", self.conflict_refs),
            ("revision_refs", self.revision_refs),
        ):
            if refs:
                value[key] = list(refs)
        return value


class _InteriorSourceAuthorityBinding(FrozenModel):
    source_kind: Literal[
        "committed_event",
        "execution_receipt",
        "projection_snapshot",
        "immutable_payload",
    ]
    authority_type: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=1_024)
    source_world_revision: int = Field(ge=0)
    immutable_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class _InteriorBinding(FrozenModel):
    """One identity-bound Capsule coordinate, including explicit absence."""

    availability: Literal["available", "unavailable"]
    payload_json: str = "null"
    content_hash: str | None = Field(default=None, min_length=64, max_length=64)
    reason: str | None = Field(default=None, min_length=1, max_length=128)

    @property
    def value(self) -> object:
        return json.loads(self.payload_json)

    @model_validator(mode="after")
    def availability_matches_payload(self) -> "_InteriorBinding":
        value = json.loads(self.payload_json)
        if self.payload_json != _canonical_json(value):
            raise ValueError("character interior binding payload must be canonical")
        if self.availability == "available":
            if self.reason is not None or self.content_hash != _digest(value):
                raise ValueError("available character interior binding is incomplete")
        elif value is not None or self.content_hash is not None or self.reason is None:
            raise ValueError("unavailable character interior binding is incomplete")
        return self

    @classmethod
    def available(cls, value: object) -> "_InteriorBinding":
        return cls(
            availability="available",
            payload_json=_canonical_json(value),
            content_hash=_digest(value),
        )

    @classmethod
    def unavailable(cls, reason: str) -> "_InteriorBinding":
        return cls(availability="unavailable", reason=reason)

    def model_view(self) -> dict[str, object]:
        if self.availability == "unavailable":
            return {"availability": "unavailable"}
        return {
            "availability": "available",
            "value": self.value,
        }


class _InteriorCapabilityManifest(FrozenModel):
    """One source-bound ability/opportunity offered to the character.

    The manifest describes what can be inspected or chosen.  It does not
    prescribe whether the character should use the ability or which candidate
    she should select.
    """

    contract: Literal["character-interior-capability-manifest.1"] = (
        "character-interior-capability-manifest.1"
    )
    capability_ref: str = Field(min_length=1, max_length=512)
    capability_kind: str = Field(min_length=1, max_length=128)
    payload_json: str = Field(min_length=2, max_length=262_144)
    payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=64)

    @property
    def payload(self) -> Mapping[str, object]:
        return _decoded_object(self.payload_json)

    @model_validator(mode="after")
    def payload_and_sources_are_bound(self) -> "_InteriorCapabilityManifest":
        value = json.loads(self.payload_json)
        if not isinstance(value, dict) or self.payload_json != _canonical_json(value):
            raise ValueError("capability manifest payload must be a canonical object")
        expected_hash = "sha256:" + hashlib.sha256(self.payload_json.encode()).hexdigest()
        if self.payload_hash != expected_hash:
            raise ValueError("capability manifest payload hash is invalid")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("capability manifest source refs must be unique")
        if any(not item for item in self.source_refs):
            raise ValueError("capability manifest source refs cannot be empty")
        return self

    def binding_value(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "capability_ref": self.capability_ref,
            "capability_kind": self.capability_kind,
            "payload_hash": self.payload_hash,
            "source_refs": list(self.source_refs),
        }


class _InteriorSubject(FrozenModel):
    inner_turn_ref: str = Field(min_length=1, max_length=512)
    world_id: str = Field(min_length=1, max_length=256)
    actor_ref: str = Field(min_length=1, max_length=256)
    trigger_ref: str = Field(min_length=1, max_length=512)
    cursor: ProjectionCursor
    logical_time: datetime
    purpose: str = Field(min_length=1, max_length=128)
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=32)
    viewer_scope: Literal["deliberation_internal"] = "deliberation_internal"
    privacy_ceiling: PrivacyClass = "private"
    budget_policy_ref: str = Field(
        default="context-capsule-budget:production",
        min_length=1,
        max_length=256,
    )
    capability_manifest: _InteriorCapabilityManifest | None = None
    context_note: str | None = Field(default=None, min_length=1, max_length=1_024)

    @field_validator("source_refs")
    @classmethod
    def subject_sources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("character interior subject source refs must be unique")
        if any(not item for item in value):
            raise ValueError("character interior subject source refs cannot be empty")
        return value

    @field_validator("context_note")
    @classmethod
    def optional_note_has_content(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("character interior context note must contain text")
        return value


class InteriorStimulus(_InteriorSubject):
    """One committed change the character may privately experience."""

    contract: Literal["character-interior-stimulus.1"] = "character-interior-stimulus.1"
    stimulus_ref: str = Field(min_length=1, max_length=512)


class InteriorOpportunity(_InteriorSubject):
    """One authorized chance for the character to decide what she wants to do."""

    contract: Literal["character-interior-opportunity.1"] = "character-interior-opportunity.1"
    opportunity_ref: str = Field(min_length=1, max_length=512)


class _InteriorAuthorLineage(FrozenModel):
    """Auditable identity of the exact role-author response, without its text."""

    contract: Literal["character-interior-author-lineage.1"] = "character-interior-author-lineage.1"
    model_id: str = Field(min_length=1, max_length=256)
    model_version: str = Field(min_length=1, max_length=256)
    model_call_id: str = Field(min_length=1, max_length=512)
    request_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    response_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    attempt_ordinal: int = Field(ge=0, le=1)
    parent_model_call_id: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def correction_parent_matches_attempt(self) -> "_InteriorAuthorLineage":
        if (self.attempt_ordinal == 1) != (self.parent_model_call_id is not None):
            raise ValueError("character author correction lineage is incomplete")
        return self


class InnerLifeSnapshot(FrozenModel):
    """Immutable projection of one actor at one complete ledger cursor."""

    contract: Literal["inner-life-snapshot.1"] = "inner-life-snapshot.1"
    snapshot_id: str = Field(min_length=1, max_length=128)
    snapshot_hash: str = Field(min_length=64, max_length=64)
    availability: Literal["available", "unavailable"]
    world_id: str | None = Field(default=None, min_length=1, max_length=256)
    actor_ref: str | None = Field(default=None, min_length=1, max_length=256)
    cursor: ProjectionCursor | None = None
    logical_time: datetime | None = None
    situation: _InteriorContextView
    continuity: _InteriorContextView
    facet_views: tuple[_InteriorFacet, ...] = Field(min_length=8, max_length=8)
    materials_json: str
    materials_hash: str = Field(min_length=64, max_length=64)
    # Live-only trusted retrieval capability carried between the core and its
    # private Faculty. It is identity-bound but deliberately excluded from the
    # provider view; the Faculty expands its verified audit into typed Context
    # slices before the same role is called again.
    recall_trace_json: str | None = Field(default=None, exclude=True)
    # Automatic retrieval is a candidate environment, not the character's
    # selected Recall.  Keep its trusted live trace separate so provider and
    # durable audit code cannot confuse seeing a candidate with choosing it.
    prefetch_trace_json: str | None = Field(default=None, exclude=True)
    source_refs: tuple[str, ...] = ()
    source_inventory: tuple[_InteriorSourceInventoryItem, ...] = ()
    viewer_scope: _InteriorBinding
    privacy_scope: _InteriorBinding
    capability_scope: _InteriorBinding
    context_compiler: _InteriorBinding
    snapshot_compiler: _InteriorBinding
    truncation: _InteriorBinding

    @property
    def facets(self) -> Mapping[str, _InteriorFacet]:
        return MappingProxyType({item.name: item for item in self.facet_views})

    @property
    def materials(self) -> Mapping[str, object]:
        return _decoded_object(self.materials_json)

    def _identity_material(self) -> dict[str, object]:
        return {
            "contract": self.contract,
            "availability": self.availability,
            "world_id": self.world_id,
            "actor_ref": self.actor_ref,
            "cursor": self.cursor.model_dump(mode="json") if self.cursor else None,
            "logical_time": (
                canonicalize_json_value(self.logical_time)
                if self.logical_time is not None
                else None
            ),
            "situation": self.situation.model_dump(mode="json"),
            "continuity": self.continuity.model_dump(mode="json"),
            "facets": tuple(item.model_dump(mode="json") for item in self.facet_views),
            "materials_json": self.materials_json,
            "materials_hash": self.materials_hash,
            "recall_trace_json": self.recall_trace_json,
            "prefetch_trace_json": self.prefetch_trace_json,
            "source_refs": self.source_refs,
            "source_inventory": tuple(
                item.model_dump(mode="json") for item in self.source_inventory
            ),
            "viewer_scope": self.viewer_scope.model_dump(mode="json"),
            "privacy_scope": self.privacy_scope.model_dump(mode="json"),
            "capability_scope": self.capability_scope.model_dump(mode="json"),
            "context_compiler": self.context_compiler.model_dump(mode="json"),
            "snapshot_compiler": self.snapshot_compiler.model_dump(mode="json"),
            "truncation": self.truncation.model_dump(mode="json"),
        }

    @model_validator(mode="after")
    def identity_and_inventory_are_complete(self) -> "InnerLifeSnapshot":
        names = tuple(item.name for item in self.facet_views)
        if names != FACET_NAMES:
            raise ValueError("character interior snapshot must contain all eight ordered facets")
        decoded_materials = json.loads(self.materials_json)
        if (
            not isinstance(decoded_materials, dict)
            or self.materials_json != _canonical_json(decoded_materials)
            or self.materials_hash != _digest(decoded_materials)
        ):
            raise ValueError("character interior materials are not content-bound")
        if self.recall_trace_json is not None:
            try:
                trace_value = json.loads(self.recall_trace_json)
            except json.JSONDecodeError as exc:
                raise ValueError("character interior recall trace is not JSON") from exc
            if not isinstance(trace_value, dict) or self.recall_trace_json != _canonical_json(
                trace_value
            ):
                raise ValueError("character interior recall trace is not canonical")
        if self.prefetch_trace_json is not None:
            try:
                trace_value = json.loads(self.prefetch_trace_json)
            except json.JSONDecodeError as exc:
                raise ValueError("character interior prefetch trace is not JSON") from exc
            if not isinstance(trace_value, dict) or self.prefetch_trace_json != _canonical_json(
                trace_value
            ):
                raise ValueError("character interior prefetch trace is not canonical")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("character interior snapshot source refs must be unique")
        inventory_refs = tuple(dict.fromkeys(item.source_ref for item in self.source_inventory))
        if inventory_refs != self.source_refs:
            raise ValueError("character interior source inventory is incomplete or unordered")
        inventory_coordinates = tuple(
            (item.source_ref, item.scope) for item in self.source_inventory
        )
        if len(inventory_coordinates) != len(set(inventory_coordinates)):
            raise ValueError("character interior source inventory coordinates are duplicated")
        material_coordinates: set[tuple[str, str]] = set()
        for scope, value in decoded_materials.items():
            candidates = value.get("items") if isinstance(value, dict) else value
            if not isinstance(candidates, list):
                continue
            material_coordinates.update(
                (source_ref, scope)
                for item in candidates
                if isinstance(item, dict)
                and isinstance((source_ref := item.get("source_ref")), str)
            )
        if not material_coordinates.issubset(set(inventory_coordinates)):
            raise ValueError("character interior material has no source envelope")
        inventory_ref_set = set(self.source_refs)
        if any(
            not set(facet.source_refs).issubset(inventory_ref_set) for facet in self.facet_views
        ):
            raise ValueError("character interior facet escapes the source inventory")
        if self.availability == "available" and not self.source_refs:
            raise ValueError("available character interior snapshot needs sources")
        expected_hash = _digest(self._identity_material())
        if self.snapshot_hash != expected_hash:
            raise ValueError("character interior snapshot hash is invalid")
        if self.snapshot_id != f"inner-life-snapshot:sha256:{expected_hash}":
            raise ValueError("character interior snapshot identity is invalid")
        return self

    @classmethod
    def create(
        cls,
        *,
        availability: Literal["available", "unavailable"],
        world_id: str | None,
        actor_ref: str | None,
        cursor: ProjectionCursor | None,
        logical_time: datetime | None,
        situation: _InteriorContextView,
        continuity: _InteriorContextView,
        facet_views: tuple[_InteriorFacet, ...],
        materials: Mapping[str, object],
        source_refs: tuple[str, ...],
        source_inventory: tuple[_InteriorSourceInventoryItem, ...],
        viewer_scope: _InteriorBinding,
        privacy_scope: _InteriorBinding,
        capability_scope: _InteriorBinding,
        context_compiler: _InteriorBinding,
        snapshot_compiler: _InteriorBinding,
        truncation: _InteriorBinding,
        recall_trace_json: str | None = None,
        prefetch_trace_json: str | None = None,
    ) -> "InnerLifeSnapshot":
        materials_json = _canonical_json(dict(materials))
        common: dict[str, object] = {
            "contract": "inner-life-snapshot.1",
            "availability": availability,
            "world_id": world_id,
            "actor_ref": actor_ref,
            "cursor": cursor,
            "logical_time": logical_time,
            "situation": situation,
            "continuity": continuity,
            "facet_views": facet_views,
            "materials_json": materials_json,
            "materials_hash": _digest(dict(materials)),
            "recall_trace_json": recall_trace_json,
            "prefetch_trace_json": prefetch_trace_json,
            "source_refs": source_refs,
            "source_inventory": source_inventory,
            "viewer_scope": viewer_scope,
            "privacy_scope": privacy_scope,
            "capability_scope": capability_scope,
            "context_compiler": context_compiler,
            "snapshot_compiler": snapshot_compiler,
            "truncation": truncation,
        }
        provisional = cls.model_construct(
            snapshot_id="pending",
            snapshot_hash="0" * 64,
            **common,
        )
        snapshot_hash = _digest(provisional._identity_material())
        return cls(
            snapshot_id=f"inner-life-snapshot:sha256:{snapshot_hash}",
            snapshot_hash=snapshot_hash,
            **common,
        )

    def model_view(
        self,
        *,
        visible_source_refs: frozenset[str] | None = None,
    ) -> dict[str, object]:
        """Return a deterministic provider view without minting another identity."""

        visible = (
            set(self.source_refs)
            if visible_source_refs is None
            else set(self.source_refs) & set(visible_source_refs)
        )
        materials = _redact_materials(dict(self.materials), visible)
        faculties: dict[str, object] = {}
        for facet in self.facet_views:
            raw_keys = facet.content.get("material_keys")
            keys = (
                [key for key in raw_keys if isinstance(key, str) and key in materials]
                if isinstance(raw_keys, list)
                else []
            )
            faculties[facet.name] = {
                "availability": (
                    "available"
                    if facet.availability == "available" and bool(keys)
                    else "unavailable"
                ),
                "material_keys": keys,
            }
        cursor = self.cursor.model_dump(mode="json") if self.cursor else {}
        if self.logical_time is not None:
            cursor["logical_time"] = self.logical_time.isoformat()
        return {
            "contract": self.contract,
            "authority": "derived_from_verified_context",
            "availability": self.availability,
            "snapshot_id": self.snapshot_id,
            "snapshot_hash": self.snapshot_hash,
            "world_id": self.world_id,
            "actor_ref": self.actor_ref,
            "cursor": cursor,
            "materials": materials,
            "faculties": faculties,
            "source_refs": [ref for ref in self.source_refs if ref in visible],
            "source_inventory": [
                item.model_view() for item in self.source_inventory if item.source_ref in visible
            ],
            "viewer_scope": self.viewer_scope.model_view(),
            "privacy_scope": self.privacy_scope.model_view(),
            "capability_scope": self.capability_scope.model_view(),
            "context_compiler": self.context_compiler.model_view(),
            "snapshot_compiler": self.snapshot_compiler.model_view(),
            "truncation": self.truncation.model_view(),
        }


class _InstantPrivateSelf(FrozenModel):
    """Short model-authored stance for one Inner Turn, never a durable fact."""

    contract: Literal["instant-private-self.1"] = "instant-private-self.1"
    summary: str = Field(min_length=1, max_length=1_024)
    attended_source_refs: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("summary")
    @classmethod
    def summary_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instant private self summary cannot be blank")
        return value

    @field_validator("attended_source_refs")
    @classmethod
    def attention_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("instant private self attention refs must be unique")
        return value


class _PrivateSelfLineage(FrozenModel):
    """The valid private-self states on one Inner Turn.

    A one-pass turn has one authored state which is both initial and final.  A
    selective-Recall turn preserves the valid pre-Recall state and explicitly
    relates the post-Recall state to that author call.  This is proposal audit,
    not hidden reasoning and not a durable World fact.
    """

    contract: Literal["private-self-lineage.1"] = "private-self-lineage.1"
    relation: Literal["single_pass", "selective_recall"]
    initial_private_self: _InstantPrivateSelf
    initial_snapshot_id: str = Field(min_length=1, max_length=128)
    initial_snapshot_hash: str = Field(min_length=64, max_length=64)
    initial_author_lineage: _InteriorAuthorLineage | None = None
    recall_query: str | None = Field(default=None, min_length=1, max_length=1_024)
    final_private_self: _InstantPrivateSelf
    final_snapshot_id: str = Field(min_length=1, max_length=128)
    final_snapshot_hash: str = Field(min_length=64, max_length=64)
    final_author_lineage: _InteriorAuthorLineage | None = None
    final_parent_model_call_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
    )

    @model_validator(mode="after")
    def states_and_parent_match_relation(self) -> "_PrivateSelfLineage":
        if self.relation == "single_pass":
            if any(
                (
                    self.recall_query is not None,
                    self.final_parent_model_call_id is not None,
                    self.initial_private_self != self.final_private_self,
                    self.initial_snapshot_id != self.final_snapshot_id,
                    self.initial_snapshot_hash != self.final_snapshot_hash,
                    self.initial_author_lineage != self.final_author_lineage,
                )
            ):
                raise ValueError("single-pass private self has an invalid lineage")
            return self

        if self.recall_query is None:
            raise ValueError("selective-Recall private self requires its query")
        initial_author = self.initial_author_lineage
        final_author = self.final_author_lineage
        expected_parent = initial_author.model_call_id if initial_author is not None else None
        if self.final_parent_model_call_id != expected_parent:
            raise ValueError("post-Recall private self has an invalid parent")
        if initial_author is not None and final_author is not None:
            if (
                initial_author.model_id != final_author.model_id
                or initial_author.model_version != final_author.model_version
                or initial_author.model_call_id == final_author.model_call_id
            ):
                raise ValueError("selective Recall changed or reused its character author")
        return self


class InnerTransition(FrozenModel):
    """Sparse private effects proposed after experiencing one stimulus."""

    contract: Literal["character-inner-transition.1"] = "character-inner-transition.1"
    inner_turn_id: str = Field(min_length=1, max_length=128)
    stimulus_ref: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=256)
    cursor: ProjectionCursor
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    snapshot_hash: str | None = Field(default=None, min_length=64, max_length=64)
    status: Literal["transitioned", "model_no_change", "technical_failure"]
    summary: str | None = Field(default=None, min_length=1, max_length=1_024)
    attended_source_refs: tuple[str, ...] = Field(default=(), max_length=32)
    instant_private_self: _InstantPrivateSelf | None = None
    private_self_lineage: _PrivateSelfLineage | None = None
    proposal_refs: tuple[str, ...] = Field(default=(), max_length=32)
    author_lineage: _InteriorAuthorLineage | None = None
    presented_prefetch_traces: tuple[PrefetchPresentationAudit, ...] = Field(
        default=(), max_length=4
    )
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def terminal_shape_matches_status(self) -> "InnerTransition":
        if self.status == "technical_failure":
            if (
                self.failure_code is None
                or self.summary is not None
                or self.instant_private_self is not None
                or self.private_self_lineage is not None
                or self.proposal_refs
                or self.author_lineage is not None
                or self.presented_prefetch_traces
            ):
                raise ValueError("technical inner transition has an invalid terminal shape")
        elif any(
            (
                self.failure_code is not None,
                self.summary is None,
                self.snapshot_id is None,
                self.snapshot_hash is None,
                self.instant_private_self is None,
                self.private_self_lineage is None,
                self.author_lineage is None,
            )
        ):
            raise ValueError("model inner transition lacks its authored lineage")
        elif (
            self.instant_private_self.summary != self.summary
            or self.instant_private_self.attended_source_refs != self.attended_source_refs
        ):
            raise ValueError("instant private self is not bound to the transition")
        if self.private_self_lineage is not None and (
            self.instant_private_self != self.private_self_lineage.final_private_self
            or self.author_lineage != self.private_self_lineage.final_author_lineage
            or self.snapshot_id != self.private_self_lineage.final_snapshot_id
            or self.snapshot_hash != self.private_self_lineage.final_snapshot_hash
        ):
            raise ValueError("private-self lineage is not bound to the transition")
        if len(self.attended_source_refs) != len(set(self.attended_source_refs)):
            raise ValueError("inner transition attention refs must be unique")
        return self


class InnerDecision(FrozenModel):
    """The character's decision, explicit silence, or a technical failure."""

    contract: Literal["character-inner-decision.1"] = "character-inner-decision.1"
    inner_turn_id: str = Field(min_length=1, max_length=128)
    opportunity_ref: str = Field(min_length=1, max_length=512)
    actor_ref: str = Field(min_length=1, max_length=256)
    cursor: ProjectionCursor
    snapshot_id: str | None = Field(default=None, min_length=1, max_length=128)
    snapshot_hash: str | None = Field(default=None, min_length=64, max_length=64)
    status: Literal["decided", "model_silent", "technical_failure"]
    summary: str | None = Field(default=None, min_length=1, max_length=1_024)
    attended_source_refs: tuple[str, ...] = Field(default=(), max_length=32)
    instant_private_self: _InstantPrivateSelf | None = None
    private_self_lineage: _PrivateSelfLineage | None = None
    decision: dict[str, Any] | None = None
    proposal_refs: tuple[str, ...] = Field(default=(), max_length=32)
    author_lineage: _InteriorAuthorLineage | None = None
    presented_prefetch_traces: tuple[PrefetchPresentationAudit, ...] = Field(
        default=(), max_length=4
    )
    failure_code: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def terminal_shape_matches_status(self) -> "InnerDecision":
        if self.status == "technical_failure":
            if any(
                (
                    self.failure_code is None,
                    self.summary is not None,
                    self.instant_private_self is not None,
                    self.private_self_lineage is not None,
                    self.decision is not None,
                    bool(self.proposal_refs),
                    self.author_lineage is not None,
                    bool(self.presented_prefetch_traces),
                )
            ):
                raise ValueError("technical inner decision has an invalid terminal shape")
        elif self.status == "model_silent":
            if self.failure_code is not None or self.summary is None or self.decision is not None:
                raise ValueError("model silence has an invalid terminal shape")
        elif self.failure_code is not None or self.summary is None or self.decision is None:
            raise ValueError("character decision has an invalid terminal shape")
        if self.status != "technical_failure":
            if any(
                (
                    self.snapshot_id is None,
                    self.snapshot_hash is None,
                    self.instant_private_self is None,
                    self.private_self_lineage is None,
                    self.author_lineage is None,
                )
            ):
                raise ValueError("model inner decision lacks its authored lineage")
            if (
                self.instant_private_self.summary != self.summary
                or self.instant_private_self.attended_source_refs != self.attended_source_refs
            ):
                raise ValueError("instant private self is not bound to the decision")
        if self.private_self_lineage is not None and (
            self.instant_private_self != self.private_self_lineage.final_private_self
            or self.author_lineage != self.private_self_lineage.final_author_lineage
            or self.snapshot_id != self.private_self_lineage.final_snapshot_id
            or self.snapshot_hash != self.private_self_lineage.final_snapshot_hash
        ):
            raise ValueError("private-self lineage is not bound to the decision")
        if len(self.attended_source_refs) != len(set(self.attended_source_refs)):
            raise ValueError("inner decision attention refs must be unique")
        return self


__all__ = [
    "InteriorAffectTransition",
    "InteriorAffectNewComponentTarget",
    "InteriorAffectExistingComponentTarget",
    "InteriorAffectOpenTransition",
    "InteriorAffectUpdateTransition",
    "InteriorAffectResolveTransition",
    "InteriorAffectSupersedeTransition",
    "InteriorStimulus",
    "InteriorOpportunity",
    "InnerLifeSnapshot",
    "InnerTransition",
    "InnerDecision",
]
