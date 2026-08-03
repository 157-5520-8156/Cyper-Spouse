"""Open, source-bound drafts for model-authored life development.

The schema describes authority, evidence and executable shape.  It deliberately
contains no plot opening, event-kind catalogue, motive taxonomy or fixed
outcome prose.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import hashlib
import json
import re
from typing import Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, computed_field, field_validator, model_validator

from .schema_core import FrozenModel, PrivacyClass
from .schemas import (
    BiographicalCoordinateReplacement,
    DueWindow,
    ProjectionCursor,
)


_NARRATIVE_TAG = re.compile(r"^narrative:[a-z0-9][a-z0-9._-]{0,63}$")
_LOCAL_NPC_REF_PATTERN = r"^local:npc:[a-z0-9][a-z0-9._-]{0,63}$"
_LOCAL_NPC_REF = re.compile(_LOCAL_NPC_REF_PATTERN)
_LOCAL_PLACE_REF_PATTERN = r"^local:place:[a-z0-9][a-z0-9._-]{0,63}$"
_LOCAL_PLACE_REF = re.compile(_LOCAL_PLACE_REF_PATTERN)
_LOCAL_WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
LIFE_DEVELOPMENT_PRIVACY_ORDER = (
    "public",
    "shareable",
    "personal",
    "private",
    "withhold",
)
_PRIVACY_RANK = {
    value: rank for rank, value in enumerate(LIFE_DEVELOPMENT_PRIVACY_ORDER)
}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _canonicalize_string_set(value: object) -> object:
    """Canonicalize model-authored reference sets without changing authority."""

    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return tuple(sorted(set(value)))
    return value


class LifeDevelopmentLocationCapability(FrozenModel):
    """One source-bound way in which a reviewed location may be used.

    Availability is executable authority, not a suggestion about whether the
    character should go there.  Multiple capabilities may name the same place
    (for example its reviewed public hours and one already accepted plan).
    """

    location_ref: str = Field(min_length=1, max_length=512)
    privacy_class: PrivacyClass
    availability_kind: Literal[
        "reviewed_schedule",
        "current_presence",
        "accepted_plan",
        "settled_place",
    ]
    timezone_name: str = Field(min_length=1, max_length=128)
    local_windows: tuple[str, ...] = ()
    weekdays: tuple[int, ...] = ()
    available_from: datetime | None = None
    available_to: datetime | None = None
    now_allowed: bool = True
    authority_refs: tuple[str, ...] = Field(min_length=1, max_length=8)
    identity_content_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        exclude_if=lambda value: value is None,
    )
    identity_summary: str | None = Field(
        default=None,
        min_length=1,
        max_length=1_000,
        exclude_if=lambda value: value is None,
    )
    identity_payload_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    narrative_tags: tuple[str, ...] = Field(
        default=(), max_length=16, exclude_if=lambda value: not value
    )

    @model_validator(mode="after")
    def availability_shape_is_closed(self) -> "LifeDevelopmentLocationCapability":
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("location capability timezone is unknown") from exc
        if self.availability_kind == "reviewed_schedule":
            if not self.local_windows or not self.weekdays:
                raise ValueError("reviewed location capability requires a local schedule")
            if self.available_from is not None or self.available_to is not None:
                raise ValueError("reviewed schedule cannot also declare an absolute interval")
            if self.weekdays != tuple(sorted(set(self.weekdays))) or any(
                value < 0 or value > 6 for value in self.weekdays
            ):
                raise ValueError("location weekdays must be unique Monday=0 values")
            for value in self.local_windows:
                if _LOCAL_WINDOW.fullmatch(value) is None:
                    raise ValueError("location local window must use HH:MM-HH:MM")
        elif self.availability_kind == "current_presence":
            if (
                self.local_windows
                or self.weekdays
                or self.available_from is None
                or self.available_to is None
                or self.available_to <= self.available_from
            ):
                raise ValueError("current presence requires one ordered, finite authority interval")
            if not self.now_allowed:
                raise ValueError("current presence must authorize the present")
        elif self.availability_kind == "accepted_plan":
            if (
                self.local_windows
                or self.weekdays
                or self.available_from is None
                or self.available_to is None
                or self.available_to <= self.available_from
            ):
                raise ValueError("accepted plan requires one ordered absolute interval")
            if self.now_allowed:
                raise ValueError("accepted plan authority is limited to its bound interval")
        else:
            if (
                self.local_windows
                or self.weekdays
                or self.available_from is None
                or self.available_to is not None
                or not self.now_allowed
            ):
                raise ValueError(
                    "settled place requires one open-ended attempt authority"
                )
        identity_values = (
            self.identity_content_ref,
            self.identity_summary,
            self.identity_payload_hash,
        )
        if any(item is None for item in identity_values) and any(
            item is not None for item in identity_values
        ):
            raise ValueError("location identity content binding must be complete")
        if self.identity_summary is not None and self.availability_kind != "settled_place":
            raise ValueError("only a settled place may expose model-visible identity content")
        if self.narrative_tags != tuple(sorted(set(self.narrative_tags))):
            raise ValueError("location narrative tags must be sorted and unique")
        if self.authority_refs != tuple(sorted(set(self.authority_refs))):
            raise ValueError("location capability authority refs must be sorted and unique")
        for value in (self.available_from, self.available_to):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("location absolute interval must be timezone-aware")
        return self

    @computed_field
    @property
    def capability_ref(self) -> str:
        material: dict[str, object] = {
            "availability_kind": self.availability_kind,
            "available_from": (
                self.available_from.isoformat() if self.available_from is not None else None
            ),
            "available_to": (
                self.available_to.isoformat() if self.available_to is not None else None
            ),
            "authority_refs": self.authority_refs,
            "local_windows": self.local_windows,
            "location_ref": self.location_ref,
            "now_allowed": self.now_allowed,
            "privacy_class": self.privacy_class,
            "timezone_name": self.timezone_name,
            "weekdays": self.weekdays,
        }
        if self.identity_content_ref is not None:
            material["identity_content_ref"] = self.identity_content_ref
            material["identity_summary"] = self.identity_summary
            material["identity_payload_hash"] = self.identity_payload_hash
        if self.narrative_tags:
            material["narrative_tags"] = self.narrative_tags
        return "location-capability:" + _digest(material)

    def authorizes(self, *, timing_mode: str, window: DueWindow) -> bool:
        if self.availability_kind == "current_presence":
            return (
                timing_mode == "now"
                and self.available_from is not None
                and self.available_to is not None
                and window.opens_at >= self.available_from
                and window.closes_at <= self.available_to
            )
        if self.availability_kind == "accepted_plan":
            return (
                self.available_from is not None
                and self.available_to is not None
                and window.opens_at >= self.available_from
                and window.closes_at <= self.available_to
            )
        if self.availability_kind == "settled_place":
            return (
                self.available_from is not None
                and window.opens_at >= self.available_from
            )
        if timing_mode == "now" and not self.now_allowed:
            return False
        return _window_fits_local_schedule(
            window=window,
            timezone_name=self.timezone_name,
            local_windows=self.local_windows,
            weekdays=self.weekdays,
        )


class LifeDevelopmentBiographicalCoordinateCapability(FrozenModel):
    """One current coordinate identity exposed for conflict-safe replacement.

    It is not a menu of life directions.  It only lets an open model-authored
    consequence reuse the stable identity of state that already owns a tag
    namespace, so a later settlement can rebase instead of inventing a rival
    coordinate.
    """

    coordinate_ref: str = Field(pattern=r"^biography:[a-z][a-z0-9._-]{0,63}$")
    context_tags: tuple[str, ...] = Field(min_length=1, max_length=16)
    replaces_context_tag_prefixes: tuple[str, ...] = Field(min_length=1, max_length=8)
    privacy_class: PrivacyClass
    entity_revision: int = Field(ge=1)
    settlement_event_ref: str = Field(min_length=1)


class LifeDevelopmentNpcCapability(FrozenModel):
    """One stable, source-bound person available to the World Author.

    This carries identity facts, not a list of permitted plots or motives.
    """

    npc_ref: str = Field(pattern=r"^npc:")
    lifecycle_state: str = Field(min_length=1, max_length=64)
    identity_content_ref: str = Field(min_length=1, max_length=512)
    identity_summary: str = Field(min_length=1, max_length=4_000)
    identity_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authority_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    first_occurrence_ref: str | None = None
    shared_experience_refs: tuple[str, ...] = ()
    active_plan_refs: tuple[str, ...] = ()
    current_location_ref: str | None = None
    protagonist_closeness_bp: int | None = Field(default=None, ge=0, le=10_000)
    protagonist_friction_bp: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> "LifeDevelopmentNpcCapability":
        for values in (
            self.authority_refs,
            self.shared_experience_refs,
            self.active_plan_refs,
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError("NPC capability refs must be sorted and unique")
        return self


class LifeDevelopmentCapabilityManifest(FrozenModel):
    """Pinned-input capability facts, never a menu of story choices."""

    version: str = Field(min_length=1, max_length=128)
    owner_actor_ref: str = Field(
        default="legacy:unknown-owner",
        min_length=1,
        max_length=512,
    )
    pinned_cursor: ProjectionCursor
    anchor_refs: tuple[str, ...] = ()
    grounding_refs: tuple[str, ...] = ()
    location_capabilities: tuple[LifeDevelopmentLocationCapability, ...] = ()
    entity_refs: tuple[str, ...] = ()
    npc_capabilities: tuple[LifeDevelopmentNpcCapability, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    biographical_context_tags: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    biographical_coordinates: tuple[
        LifeDevelopmentBiographicalCoordinateCapability, ...
    ] = Field(default=(), exclude_if=lambda value: not value)
    active_aspiration_source_refs: tuple[str, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    allow_external_observation_outcomes: bool = False
    max_future_days: int = Field(ge=1, le=366)
    max_window_minutes: int = Field(ge=5, le=7 * 24 * 60)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> "LifeDevelopmentCapabilityManifest":
        for refs in (
            self.anchor_refs,
            self.grounding_refs,
            self.entity_refs,
            self.biographical_context_tags,
            self.active_aspiration_source_refs,
        ):
            if refs != tuple(sorted(set(refs))):
                raise ValueError("life development capability refs must be sorted and unique")
        if self.location_capabilities != tuple(
            sorted(
                set(self.location_capabilities),
                key=lambda item: (
                    item.location_ref,
                    item.availability_kind,
                    item.model_dump_json(),
                ),
            )
        ):
            raise ValueError("life development location capabilities must be sorted and unique")
        if self.biographical_coordinates != tuple(
            sorted(
                self.biographical_coordinates,
                key=lambda item: item.coordinate_ref,
            )
        ):
            raise ValueError("biographical coordinate capabilities must be sorted")
        refs = tuple(item.coordinate_ref for item in self.biographical_coordinates)
        if len(refs) != len(set(refs)):
            raise ValueError("biographical coordinate capability refs must be unique")
        if self.npc_capabilities != tuple(
            sorted(self.npc_capabilities, key=lambda item: item.npc_ref)
        ):
            raise ValueError("NPC capabilities must be sorted")
        npc_refs = tuple(item.npc_ref for item in self.npc_capabilities)
        if len(npc_refs) != len(set(npc_refs)):
            raise ValueError("NPC capability refs must be unique")
        if any(ref not in self.entity_refs for ref in npc_refs):
            raise ValueError("NPC capability must remain inside entity authority")
        return self

    @property
    def location_refs(self) -> tuple[str, ...]:
        return tuple(sorted({item.location_ref for item in self.location_capabilities}))

    @property
    def manifest_hash(self) -> str:
        material = self.model_dump(mode="json")
        if self.owner_actor_ref == "legacy:unknown-owner":
            # `.1` manifests were committed before subject authority became
            # explicit.  The decoded sentinel is not part of those immutable
            # bytes and therefore must not alter their historical identity.
            material.pop("owner_actor_ref", None)
        return _digest(material)


class LifeDevelopmentCapabilityManifestCompiler(Protocol):
    def compile(
        self,
        *,
        projection: object,
        wake: object,
        capsule: object,
    ) -> LifeDevelopmentCapabilityManifest: ...


class LifeDevelopmentClaimDeclaration(FrozenModel):
    claim_id: str = Field(pattern=r"^local:claim:[a-z0-9][a-z0-9._-]{0,63}$")
    summary: str = Field(min_length=1, max_length=2_000)
    scope: Literal["existing_world", "novel_world_generation"]
    subject_scope: Literal[
        "world_environment",
        "existing_entity",
        "provisional_entity",
        "user_or_shared_history",
        "character_completed_experience",
    ]
    source_refs: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("source_refs", mode="before")
    @classmethod
    def canonicalize_source_refs(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def source_shape_matches_scope(self) -> "LifeDevelopmentClaimDeclaration":
        if self.source_refs != tuple(sorted(set(self.source_refs))):
            raise ValueError("claim source refs must be sorted and unique")
        if self.scope == "existing_world" and not self.source_refs:
            raise ValueError("existing-world claim requires pinned source refs")
        if self.scope == "novel_world_generation":
            if self.source_refs:
                raise ValueError("novel-world claim cannot borrow existing source refs")
            if self.subject_scope not in {
                "world_environment",
                "provisional_entity",
            }:
                raise ValueError(
                    "novel-world claim cannot assert user/shared history or completed experience"
                )
        return self


class ProvisionalNpcDraft(FrozenModel):
    local_ref: str = Field(
        min_length=1,
        max_length=80,
        pattern=_LOCAL_NPC_REF_PATTERN,
    )
    summary: str = Field(min_length=1, max_length=1_000)
    narrative_tags: tuple[str, ...] = Field(default=(), max_length=16)
    privacy_class: PrivacyClass

    @field_validator("narrative_tags", mode="before")
    @classmethod
    def canonicalize_narrative_tags(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def is_local_and_open_ended(self) -> "ProvisionalNpcDraft":
        if _LOCAL_NPC_REF.fullmatch(self.local_ref) is None:
            raise ValueError("provisional NPC ref must use local:npc:<token>")
        if self.narrative_tags != tuple(sorted(set(self.narrative_tags))) or any(
            _NARRATIVE_TAG.fullmatch(item) is None for item in self.narrative_tags
        ):
            raise ValueError("provisional NPC tags must be canonical narrative:* refs")
        return self


class ProvisionalPlaceDraft(FrozenModel):
    """A model-authored place identity that becomes reusable only if settled.

    This is not a destination catalogue or proof that the character visited.
    The surrounding outcome supplies that meaning; settlement merely gives the
    described place a stable identity and attempt-only future capability.
    """

    local_ref: str = Field(
        min_length=1,
        max_length=88,
        pattern=_LOCAL_PLACE_REF_PATTERN,
    )
    summary: str = Field(min_length=1, max_length=1_000)
    narrative_tags: tuple[str, ...] = Field(default=(), max_length=16)
    timezone_name: str = Field(min_length=1, max_length=128)
    privacy_class: PrivacyClass

    @field_validator("narrative_tags", mode="before")
    @classmethod
    def canonicalize_narrative_tags(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def is_local_and_open_ended(self) -> "ProvisionalPlaceDraft":
        if _LOCAL_PLACE_REF.fullmatch(self.local_ref) is None:
            raise ValueError("provisional place ref must use local:place:<token>")
        if self.narrative_tags != tuple(sorted(set(self.narrative_tags))) or any(
            _NARRATIVE_TAG.fullmatch(item) is None for item in self.narrative_tags
        ):
            raise ValueError("provisional place tags must be canonical narrative:* refs")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("provisional place timezone is unknown") from exc
        return self



class LifeDevelopmentVisualLocationDraft(FrozenModel):
    """Claim-bound visible coordinates, not a request to create a picture."""

    location_ref: str = Field(min_length=1, max_length=512)
    kind: str | None = Field(default=None, min_length=1, max_length=128)
    country: str | None = Field(default=None, min_length=1, max_length=128)
    region: str | None = Field(default=None, min_length=1, max_length=128)
    city: str | None = Field(default=None, min_length=1, max_length=128)
    publicness: str | None = Field(default=None, min_length=1, max_length=64)


class LifeDevelopmentVisualEnvironmentDraft(FrozenModel):
    light: str | None = Field(default=None, min_length=1, max_length=480)
    weather: str | None = Field(default=None, min_length=1, max_length=480)
    structure: str | None = Field(default=None, min_length=1, max_length=480)
    region: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def has_visible_environment(self) -> "LifeDevelopmentVisualEnvironmentDraft":
        if not any((self.light, self.weather, self.structure, self.region)):
            raise ValueError("life-development visual environment cannot be empty")
        return self


class LifeDevelopmentVisualObjectDraft(FrozenModel):
    local_ref: str = Field(pattern=r"^local:object:[a-z0-9][a-z0-9._-]{0,63}$")
    kind: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=1_000)


class LifeDevelopmentVisualEvidenceDraft(FrozenModel):
    """Optional visual facts authored with one outcome in the same model call.

    ``claim_refs`` binds every field to claims already used by that outcome.
    Absence means the World Author supplied no safe visual interpretation;
    downstream code must not infer one from prose.
    """

    claim_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    activity_description: str | None = Field(default=None, min_length=1, max_length=1_000)
    location: LifeDevelopmentVisualLocationDraft | None = None
    environment: LifeDevelopmentVisualEnvironmentDraft | None = None
    objects: tuple[LifeDevelopmentVisualObjectDraft, ...] = Field(default=(), max_length=16)

    @field_validator("claim_refs", mode="before")
    @classmethod
    def canonicalize_claim_refs(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def is_claim_closed_and_concrete(self) -> "LifeDevelopmentVisualEvidenceDraft":
        if self.claim_refs != tuple(sorted(set(self.claim_refs))):
            raise ValueError("life-development visual claim refs must be sorted and unique")
        if not any((self.activity_description, self.location, self.environment, self.objects)):
            raise ValueError("life-development visual evidence cannot be empty")
        refs = tuple(item.local_ref for item in self.objects)
        if len(refs) != len(set(refs)):
            raise ValueError("life-development visual object refs must be unique")
        return self


class ObjectiveBiographicalTransitionDraft(FrozenModel):
    """An objective coordinate consequence belonging to one candidate result.

    The fields are open-ended. World Author may describe only a state made true
    by that exact candidate branch; it cannot use this slot for a motive, plan,
    or desired future.
    """

    coordinate_ref: str = Field(pattern=r"^biography:[a-z][a-z0-9._-]{0,63}$")
    summary: str = Field(min_length=1, max_length=12_000)
    context_tags: tuple[str, ...] = Field(min_length=1, max_length=16)
    replaces_context_tag_prefixes: tuple[str, ...] = Field(min_length=1, max_length=8)
    privacy_class: PrivacyClass = "personal"

    @field_validator(
        "context_tags", "replaces_context_tag_prefixes", mode="before"
    )
    @classmethod
    def canonicalize_coordinates(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def objective_coordinate_is_structurally_closed(
        self,
    ) -> "ObjectiveBiographicalTransitionDraft":
        if (
            self.coordinate_ref.startswith("biography:direction.")
            or any(item.startswith("direction.") for item in self.context_tags)
            or any(
                item.startswith("direction.")
                for item in self.replaces_context_tag_prefixes
            )
        ):
            raise ValueError(
                "objective transition cannot author the character direction namespace"
            )
        BiographicalCoordinateReplacement.create(
            coordinate_ref=self.coordinate_ref,
            summary=self.summary,
            context_tags=self.context_tags,
            replaces_context_tag_prefixes=self.replaces_context_tag_prefixes,
            privacy_class=self.privacy_class,
        )
        return self


class LifeDevelopmentOutcomeDraft(FrozenModel):
    experienced_by_ref: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=12_000)
    privacy_class: PrivacyClass
    relative_plausibility_weight: int = Field(ge=1, le=1_000_000)
    claim_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    provisional_npcs: tuple[ProvisionalNpcDraft, ...] = Field(default=(), max_length=4)
    provisional_places: tuple[ProvisionalPlaceDraft, ...] = Field(default=(), max_length=4)
    objective_biographical_transition: "ObjectiveBiographicalTransitionDraft | None" = None
    dynamic_life_direction: None = None
    visual_evidence: LifeDevelopmentVisualEvidenceDraft | None = None

    @field_validator("claim_refs", mode="before")
    @classmethod
    def canonicalize_claim_refs(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def local_refs_are_unique(self) -> "LifeDevelopmentOutcomeDraft":
        if self.claim_refs != tuple(sorted(set(self.claim_refs))):
            raise ValueError("outcome claim refs must be sorted and unique")
        refs = tuple(item.local_ref for item in self.provisional_npcs)
        if len(refs) != len(set(refs)):
            raise ValueError("outcome provisional NPC refs must be unique")
        place_refs = tuple(item.local_ref for item in self.provisional_places)
        if len(place_refs) != len(set(place_refs)):
            raise ValueError("outcome provisional place refs must be unique")
        if any(
            _PRIVACY_RANK[item.privacy_class] < _PRIVACY_RANK[self.privacy_class]
            for item in (*self.provisional_npcs, *self.provisional_places)
        ) or (
            self.objective_biographical_transition is not None
            and _PRIVACY_RANK[
                self.objective_biographical_transition.privacy_class
            ]
            < _PRIVACY_RANK[self.privacy_class]
        ):
            raise ValueError("outcome effect cannot weaken outcome privacy")
        if self.visual_evidence is not None:
            if not set(self.visual_evidence.claim_refs) <= set(self.claim_refs):
                raise ValueError("outcome visual evidence must close over outcome claim refs")
            if self.privacy_class not in {"public", "shareable"}:
                raise ValueError(
                    "recipient-unbound life-development visual evidence must be public or shareable"
                )
        return self


class LifeDevelopmentTimingDraft(FrozenModel):
    mode: Literal["now", "later"]
    duration_minutes: int | None = Field(default=None, ge=5)
    opens_at: datetime | None = None
    closes_at: datetime | None = None

    @model_validator(mode="after")
    def timing_shape_is_closed(self) -> "LifeDevelopmentTimingDraft":
        if self.mode == "now":
            if self.duration_minutes is None or any(
                value is not None for value in (self.opens_at, self.closes_at)
            ):
                raise ValueError("now timing requires only duration_minutes")
        elif (
            self.duration_minutes is not None
            or self.opens_at is None
            or self.closes_at is None
            or self.closes_at <= self.opens_at
        ):
            raise ValueError("later timing requires a valid opens_at/closes_at window")
        return self

    def resolve(
        self,
        *,
        logical_time: datetime,
        manifest: LifeDevelopmentCapabilityManifest,
    ) -> DueWindow:
        if self.mode == "now":
            assert self.duration_minutes is not None
            if self.duration_minutes > manifest.max_window_minutes:
                raise LifeDevelopmentDraftError(
                    "window_too_long",
                    "duration_minutes exceeds capability max_window_minutes",
                )
            return DueWindow(
                opens_at=logical_time,
                closes_at=logical_time + timedelta(minutes=self.duration_minutes),
            )
        assert self.opens_at is not None and self.closes_at is not None
        if self.opens_at < logical_time:
            raise LifeDevelopmentDraftError(
                "window_in_past",
                "later opens_at precedes pinned Logical Time",
                violations=(
                    {
                        "path": "timing.opens_at",
                        "message": (
                            "later opens_at must be at or after pinned logical time"
                        ),
                        "type": "window_in_past",
                    },
                ),
                failure_context={
                    "pinned_logical_time": logical_time.isoformat(),
                    "selected_opens_at": self.opens_at.isoformat(),
                    "selected_closes_at": self.closes_at.isoformat(),
                },
            )
        if self.closes_at - self.opens_at > timedelta(minutes=manifest.max_window_minutes):
            raise LifeDevelopmentDraftError(
                "window_too_long",
                "later window exceeds capability max_window_minutes",
            )
        if self.opens_at > logical_time + timedelta(days=manifest.max_future_days):
            raise LifeDevelopmentDraftError(
                "window_too_far", "later opens_at exceeds capability future horizon"
            )
        return DueWindow(opens_at=self.opens_at, closes_at=self.closes_at)


class LifeDevelopmentPossibilityDraft(FrozenModel):
    decision: Literal["propose"]
    authored_subject_ref: str = Field(min_length=1, max_length=512)
    causal_authority: Literal["world_contingency", "character_choice"]
    outcome_resolution_authority: Literal[
        "character_choice",
        "world_contingency",
        "external_observation",
    ]
    premise_scope: Literal["external_opportunity"]
    premise: str = Field(min_length=1, max_length=12_000)
    premise_claim_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    claim_declarations: tuple[LifeDevelopmentClaimDeclaration, ...] = Field(
        min_length=1,
        max_length=24,
    )
    timing: LifeDevelopmentTimingDraft
    anchor_refs: tuple[str, ...] = Field(min_length=1, max_length=16)
    location_ref: str | None = Field(default=None, min_length=1, max_length=512)
    location_capability_ref: str | None = Field(
        default=None,
        pattern=r"^location-capability:[0-9a-f]{64}$",
    )
    entity_refs: tuple[str, ...] = Field(default=(), max_length=8)
    privacy_class: PrivacyClass
    outcomes: tuple[LifeDevelopmentOutcomeDraft, ...] = Field(min_length=2, max_length=4)

    @field_validator(
        "anchor_refs",
        "entity_refs",
        "premise_claim_refs",
        mode="before",
    )
    @classmethod
    def canonicalize_reference_sets(cls, value: object) -> object:
        return _canonicalize_string_set(value)

    @model_validator(mode="after")
    def refs_are_canonical(self) -> "LifeDevelopmentPossibilityDraft":
        if any(
            outcome.experienced_by_ref != self.authored_subject_ref for outcome in self.outcomes
        ):
            raise ValueError("every life-development outcome must bind the authored subject")
        for refs in (
            self.anchor_refs,
            self.entity_refs,
            self.premise_claim_refs,
        ):
            if refs != tuple(sorted(set(refs))):
                raise ValueError("life development proposal refs must be sorted and unique")
        declaration_refs = tuple(item.claim_id for item in self.claim_declarations)
        if len(declaration_refs) != len(set(declaration_refs)):
            raise ValueError("claim declaration ids must be unique")
        used = {
            *self.premise_claim_refs,
            *(ref for outcome in self.outcomes for ref in outcome.claim_refs),
        }
        if used != set(declaration_refs):
            raise ValueError("premise and outcomes must exactly close over claim declarations")
        if (self.location_ref is None) != (self.location_capability_ref is None):
            raise ValueError("location_ref and location_capability_ref must be supplied together")
        for outcome in self.outcomes:
            visual = outcome.visual_evidence
            if (
                visual is not None
                and visual.location is not None
                and visual.location.location_ref != self.location_ref
            ):
                raise ValueError(
                    "outcome visual location must equal the authorized proposal location"
                )
        if any(
            _PRIVACY_RANK[item.privacy_class] < _PRIVACY_RANK[self.privacy_class]
            for item in self.outcomes
        ):
            raise ValueError("outcome privacy cannot weaken occurrence privacy")
        if (
            self.causal_authority == "world_contingency"
            and self.outcome_resolution_authority == "character_choice"
        ):
            raise ValueError("external contingency outcomes cannot be selected by the character")
        return self


class LifeDevelopmentNoOpDraft(FrozenModel):
    decision: Literal["no_op"]


LifeDevelopmentWorldDraft = LifeDevelopmentNoOpDraft | LifeDevelopmentPossibilityDraft


class CharacterChoiceNoOpDraft(FrozenModel):
    decision: Literal["no_op"]


class CharacterChoiceAcceptDraft(FrozenModel):
    decision: Literal["accept"]
    intention_summary: str = Field(min_length=1, max_length=4_000)
    importance_bp: int = Field(ge=0, le=10_000)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    participant_refs: tuple[str, ...] = Field(default=(), max_length=8)
    crystallized_aspiration_source_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Exact planted_event_ref of an active pinned aspiration when this "
            "accepted Plan concretizes it; null when it does not."
        ),
    )

    @model_validator(mode="after")
    def optional_window_is_complete(self) -> "CharacterChoiceAcceptDraft":
        if (self.opens_at is None) != (self.closes_at is None):
            raise ValueError("Character choice timing override must be complete")
        if (
            self.opens_at is not None
            and self.closes_at is not None
            and self.closes_at <= self.opens_at
        ):
            raise ValueError("Character choice timing override must be ordered")
        if self.participant_refs != tuple(sorted(set(self.participant_refs))):
            raise ValueError("Character choice participants must be sorted and unique")
        return self


CharacterChoiceDraft = CharacterChoiceNoOpDraft | CharacterChoiceAcceptDraft


class LifeDevelopmentDraftError(ValueError):
    def __init__(
        self,
        code: str,
        detail: str,
        *,
        violations: tuple[dict[str, str], ...] = (),
        failure_context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.violations = violations
        self.failure_context = dict(failure_context or {})


def parse_world_author_draft(
    *,
    raw: str,
    manifest: LifeDevelopmentCapabilityManifest,
    logical_time: datetime,
) -> LifeDevelopmentWorldDraft:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 64_000:
        raise LifeDevelopmentDraftError(
            "invalid_model_output", "World Author output must be bounded JSON text"
        )
    json_text = raw.strip()
    if json_text.startswith("```") and json_text.endswith("```"):
        first_newline = json_text.find("\n")
        opening = json_text[:first_newline].strip().casefold()
        if first_newline > 0 and opening in {"```", "```json"}:
            # Tolerate only a single pure transport envelope.  Nothing inside
            # is repaired or trusted: the extracted JSON still passes the
            # complete strict schema and authority validation below.
            json_text = json_text[first_newline + 1 : -3].strip()
    try:
        decoded = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise LifeDevelopmentDraftError(
            "invalid_json", "World Author output is not valid JSON"
        ) from exc
    if not isinstance(decoded, dict):
        raise LifeDevelopmentDraftError(
            "invalid_shape", "World Author output must be one JSON object"
        )
    if set(decoded) == {"replacement"}:
        # Strict OpenAI-compatible schemas cannot place the no_op/propose
        # union at the root.  The structured transport therefore moves that
        # exact union below one `replacement` property.  Remove only this
        # exact provider envelope and run the ordinary complete semantic and
        # authority validation below; the caller still retains `raw` unchanged
        # for the immutable external-result audit.
        replacement = decoded.get("replacement")
        if not isinstance(replacement, dict):
            raise LifeDevelopmentDraftError(
                "invalid_shape",
                "World Author replacement transport must contain one JSON object",
            )
        decoded = replacement
        json_text = json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if (
        decoded.get("decision") == "no_op"
        and set(decoded) == {"decision", "authored_subject_ref"}
        and decoded.get("authored_subject_ref") == manifest.owner_actor_ref
    ):
        # ``authored_subject_ref`` is required on the other branch of the
        # World Author union and models sometimes echo it onto an explicit
        # no-op.  The matching owner ref cannot authorize an effect or add a
        # fact, so discard only this exact harmless echo before constructing
        # the canonical no-op.  Every other extra field remains forbidden.
        decoded = {"decision": "no_op"}
        json_text = '{"decision":"no_op"}'
    if decoded.get("decision") != "no_op" and manifest.owner_actor_ref == "legacy:unknown-owner":
        # Historical immutable audits predate explicit subject authority.
        # Decode them only under an equally explicit legacy manifest identity;
        # every newly compiled production manifest names the real owner and
        # therefore cannot enter this compatibility path.
        decoded = dict(decoded)
        decoded.setdefault("authored_subject_ref", manifest.owner_actor_ref)
        outcomes = decoded.get("outcomes")
        if isinstance(outcomes, list):
            decoded["outcomes"] = [
                (
                    {
                        **outcome,
                        "experienced_by_ref": outcome.get(
                            "experienced_by_ref",
                            manifest.owner_actor_ref,
                        ),
                    }
                    if isinstance(outcome, dict)
                    else outcome
                )
                for outcome in outcomes
            ]
        json_text = json.dumps(
            decoded,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    try:
        if decoded.get("decision") == "no_op":
            draft: LifeDevelopmentWorldDraft = LifeDevelopmentNoOpDraft.model_validate_json(
                json_text
            )
        else:
            draft = LifeDevelopmentPossibilityDraft.model_validate_json(json_text)
    except ValueError as exc:
        detail = "World Author output violates the possibility schema"
        structured_violations: tuple[dict[str, str], ...] = ()
        if isinstance(exc, ValidationError):
            violations = []
            machine_violations = []
            for error in exc.errors(include_url=False, include_input=False):
                location = ".".join(str(part) for part in error["loc"]) or "<root>"
                violations.append(f"{location}: {error['msg']} [{error['type']}]")
                machine_violations.append(
                    {
                        "path": location,
                        "message": str(error["msg"]),
                        "type": str(error["type"]),
                    }
                )
            for violation in _visual_location_pairing_violations(decoded):
                if violation not in machine_violations:
                    machine_violations.append(violation)
                    violations.append(
                        f"{violation['path']}: {violation['message']} "
                        f"[{violation['type']}]"
                    )
            if violations:
                detail = f"{detail}: {'; '.join(violations)}"
                structured_violations = tuple(machine_violations)
        raise LifeDevelopmentDraftError(
            "invalid_shape",
            detail[:8_000],
            violations=structured_violations,
        ) from exc
    if isinstance(draft, LifeDevelopmentNoOpDraft):
        return draft
    if draft.authored_subject_ref != manifest.owner_actor_ref:
        raise LifeDevelopmentDraftError(
            "unauthorized_authored_subject",
            "authored_subject_ref must equal the pinned life owner_actor_ref",
        )
    if not set(draft.anchor_refs) <= set(manifest.anchor_refs):
        raise LifeDevelopmentDraftError(
            "unsupported_anchor_ref",
            "anchor_refs contains a ref absent from the pinned capability manifest",
        )
    resolved_window = draft.timing.resolve(
        logical_time=logical_time,
        manifest=manifest,
    )
    if draft.location_ref is not None:
        matching_location_capabilities = tuple(
            item
            for item in manifest.location_capabilities
            if item.location_ref == draft.location_ref
            and item.capability_ref == draft.location_capability_ref
        )
        location_capabilities = tuple(
            item
            for item in matching_location_capabilities
            if item.authorizes(
                timing_mode=draft.timing.mode,
                window=resolved_window,
            )
        )
        if not location_capabilities:
            if matching_location_capabilities:
                violations = (
                    {
                        "path": "timing",
                        "message": (
                            "the selected location capability does not authorize "
                            "the resolved proposal window"
                        ),
                        "type": "capability_window_unavailable",
                    },
                )
                detail = (
                    "the selected location capability exists but does not authorize "
                    "the proposed window"
                )
            else:
                violations = (
                    {
                        "path": "location_ref",
                        "message": (
                            "the selected location_ref and location_capability_ref "
                            "pair is absent from the pinned capability manifest"
                        ),
                        "type": "capability_not_available",
                    },
                    {
                        "path": "location_capability_ref",
                        "message": (
                            "the selected location_ref and location_capability_ref "
                            "pair is absent from the pinned capability manifest"
                        ),
                        "type": "capability_not_available",
                    },
                )
                detail = (
                    "the selected location_ref and location_capability_ref pair is "
                    "absent from the pinned capability manifest"
                )
            raise LifeDevelopmentDraftError(
                "unsupported_location_window",
                detail,
                violations=violations,
                failure_context={
                    "available_location_capability_count": len(
                        manifest.location_capabilities
                    ),
                    "matching_location_capability_count": len(
                        matching_location_capabilities
                    ),
                    "resolved_window": {
                        "opens_at": resolved_window.opens_at.isoformat(),
                        "closes_at": resolved_window.closes_at.isoformat(),
                    },
                    "selected_location_capability_ref": (
                        draft.location_capability_ref
                    ),
                    "selected_location_ref": draft.location_ref,
                    "timing_mode": draft.timing.mode,
                },
            )
        required_privacy = max(
            (_PRIVACY_RANK[item.privacy_class] for item in location_capabilities),
            default=0,
        )
        if _PRIVACY_RANK[draft.privacy_class] < required_privacy:
            raise LifeDevelopmentDraftError(
                "location_privacy_weakened",
                "proposal privacy is weaker than the applicable location capability",
            )
    if not set(draft.entity_refs) <= set(manifest.entity_refs):
        raise LifeDevelopmentDraftError(
            "unsupported_entity_ref",
            "entity_refs contains a ref absent from the pinned capability manifest",
        )
    if (
        draft.outcome_resolution_authority == "external_observation"
        and not manifest.allow_external_observation_outcomes
    ):
        raise LifeDevelopmentDraftError(
            "external_observation_not_authorized",
            "external observation outcome resolution is absent from the manifest",
        )
    for claim in draft.claim_declarations:
        if claim.scope == "existing_world" and not set(claim.source_refs) <= set(
            manifest.grounding_refs
        ):
            raise LifeDevelopmentDraftError(
                "unsupported_claim_source",
                "existing-world claim cites a ref absent from pinned grounding refs",
            )
    visible_prefixes = {
        item.split(":", 1)[0] + ":"
        for item in manifest.biographical_context_tags
        if ":" in item
    }
    for outcome in draft.outcomes:
        for direction in (
            outcome.dynamic_life_direction,
            outcome.objective_biographical_transition,
        ):
            if direction is None:
                continue
            introduced_prefixes = {
                item.split(":", 1)[0] + ":" for item in direction.context_tags
            }
            if not set(direction.replaces_context_tag_prefixes if isinstance(
                direction, ObjectiveBiographicalTransitionDraft
            ) else direction.supersedes_context_tag_prefixes) <= (
                visible_prefixes | introduced_prefixes
            ):
                raise LifeDevelopmentDraftError(
                    "unsupported_biographical_coordinate",
                    "a replaced coordinate must be currently visible or established by the same outcome",
                )
        transition = outcome.objective_biographical_transition
        if transition is None:
            continue
        overlapping = tuple(
            item
            for item in manifest.biographical_coordinates
            if set(item.replaces_context_tag_prefixes)
            & set(transition.replaces_context_tag_prefixes)
        )
        if any(item.coordinate_ref != transition.coordinate_ref for item in overlapping):
            expected = tuple(sorted(item.coordinate_ref for item in overlapping))
            raise LifeDevelopmentDraftError(
                "stale_biographical_coordinate_identity",
                "an objective transition overlaps current state and must reuse "
                f"one of these coordinate_ref values: {expected}",
            )
    return draft


def _visual_location_pairing_violations(
    decoded: dict[str, object],
) -> tuple[dict[str, str], ...]:
    """Make cross-field visual-location failures addressable by a model."""

    proposal_location = decoded.get("location_ref")
    outcomes = decoded.get("outcomes")
    if not isinstance(outcomes, list):
        return ()
    violations: list[dict[str, str]] = []
    proposal_coordinate_reported = False
    for index, outcome in enumerate(outcomes):
        if not isinstance(outcome, dict):
            continue
        visual = outcome.get("visual_evidence")
        if not isinstance(visual, dict):
            continue
        location = visual.get("location")
        if not isinstance(location, dict):
            continue
        visual_location = location.get("location_ref")
        if not isinstance(visual_location, str) or visual_location == proposal_location:
            continue
        if not proposal_coordinate_reported:
            violations.append(
                {
                    "path": "location_ref",
                    "message": (
                        "proposal location_ref must be present and equal every "
                        "visual location_ref, or every visual location must be null"
                    ),
                    "type": "visual_location_pairing",
                }
            )
            proposal_coordinate_reported = True
        violations.append(
            {
                "path": (
                    f"outcomes.{index}.visual_evidence.location.location_ref"
                ),
                "message": (
                    "visual location_ref must equal the proposal location_ref; "
                    "a background or origin place is not an execution coordinate"
                ),
                "type": "visual_location_pairing",
            }
        )
    return tuple(violations)


def _window_fits_local_schedule(
    *,
    window: DueWindow,
    timezone_name: str,
    local_windows: tuple[str, ...],
    weekdays: tuple[int, ...],
) -> bool:
    zone = ZoneInfo(timezone_name)
    local_open = window.opens_at.astimezone(zone)
    local_close = window.closes_at.astimezone(zone)
    for candidate_date in (
        local_open.date() - timedelta(days=1),
        local_open.date(),
        local_close.date(),
    ):
        if candidate_date.weekday() not in weekdays:
            continue
        for encoded in local_windows:
            start, end = _local_interval(
                candidate_date=candidate_date,
                encoded=encoded,
                zone=zone,
            )
            if local_open >= start and local_close <= end:
                return True
    return False


def _local_interval(
    *,
    candidate_date: date,
    encoded: str,
    zone: ZoneInfo,
) -> tuple[datetime, datetime]:
    match = _LOCAL_WINDOW.fullmatch(encoded)
    if match is None:
        raise ValueError("invalid local availability window")
    start_minute = int(match.group(1)) * 60 + int(match.group(2))
    end_minute = int(match.group(3)) * 60 + int(match.group(4))
    start = datetime.combine(
        candidate_date,
        time(hour=start_minute // 60, minute=start_minute % 60),
        tzinfo=zone,
    )
    end_date = candidate_date + timedelta(days=1) if end_minute <= start_minute else candidate_date
    end = datetime.combine(
        end_date,
        time(hour=end_minute // 60, minute=end_minute % 60),
        tzinfo=zone,
    )
    return start, end


def parse_character_choice(
    *,
    raw: str,
    offered: LifeDevelopmentPossibilityDraft,
    offered_window: DueWindow,
    active_aspiration_source_refs: tuple[str, ...] = (),
) -> CharacterChoiceDraft:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8_192:
        raise LifeDevelopmentDraftError(
            "invalid_character_output", "Character Model output must be bounded JSON"
        )
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LifeDevelopmentDraftError(
            "invalid_character_output",
            "Character Model output is not valid JSON",
        ) from exc
    if not isinstance(decoded, dict):
        raise LifeDevelopmentDraftError(
            "invalid_character_output",
            "Character Model output must be one JSON object",
        )
    try:
        if decoded.get("decision") == "no_op":
            draft: CharacterChoiceDraft = CharacterChoiceNoOpDraft.model_validate_json(raw)
        else:
            draft = CharacterChoiceAcceptDraft.model_validate_json(raw)
    except ValidationError as exc:
        violations = tuple(
            {
                "path": ".".join(str(part) for part in error["loc"]) or "<root>",
                "message": str(error["msg"]),
                "type": str(error["type"]),
            }
            for error in exc.errors(include_url=False, include_input=False)
        )
        detail = "; ".join(
            f"{item['path']}: {item['message']} [{item['type']}]"
            for item in violations
        )
        raise LifeDevelopmentDraftError(
            "invalid_character_output",
            ("Character Model output violates the choice schema: " + detail)[:8_000],
            violations=violations,
        ) from exc
    if isinstance(draft, CharacterChoiceNoOpDraft):
        return draft
    if not set(draft.participant_refs) <= set(offered.entity_refs):
        raise LifeDevelopmentDraftError(
            "unsupported_character_participant",
            "Character Model selected a participant outside the offered envelope",
        )
    if draft.opens_at is not None and (
        draft.opens_at < offered_window.opens_at
        or draft.closes_at is None
        or draft.closes_at > offered_window.closes_at
    ):
        raise LifeDevelopmentDraftError(
            "character_timing_outside_envelope",
            "Character Model timing must stay within the offered window",
        )
    if (
        draft.crystallized_aspiration_source_ref is not None
        and draft.crystallized_aspiration_source_ref
        not in active_aspiration_source_refs
    ):
        raise LifeDevelopmentDraftError(
            "unsupported_aspiration_source_ref",
            "Character Model may crystallize only an active aspiration source "
            "offered in the pinned capability manifest",
        )
    return draft


__all__ = [
    "CharacterChoiceAcceptDraft",
    "CharacterChoiceDraft",
    "CharacterChoiceNoOpDraft",
    "LifeDevelopmentCapabilityManifest",
    "LifeDevelopmentBiographicalCoordinateCapability",
    "LifeDevelopmentCapabilityManifestCompiler",
    "LifeDevelopmentLocationCapability",
    "LifeDevelopmentClaimDeclaration",
    "LifeDevelopmentDraftError",
    "LifeDevelopmentNoOpDraft",
    "ObjectiveBiographicalTransitionDraft",
    "LifeDevelopmentOutcomeDraft",
    "ProvisionalPlaceDraft",
    "LifeDevelopmentVisualEnvironmentDraft",
    "LifeDevelopmentVisualEvidenceDraft",
    "LifeDevelopmentVisualLocationDraft",
    "LifeDevelopmentVisualObjectDraft",
    "LifeDevelopmentPossibilityDraft",
    "LIFE_DEVELOPMENT_PRIVACY_ORDER",
    "LifeDevelopmentTimingDraft",
    "LifeDevelopmentWorldDraft",
    "ProvisionalNpcDraft",
    "parse_character_choice",
    "parse_world_author_draft",
]
