"""Reviewed seed catalog for clock-bound life-plan openings.

The catalog is configuration, not mutable world history.  It exposes matrix
coordinates and eligibility while keeping plan/event identity out of YAML.
The catalog may also describe a bounded set of reviewed places and people.
Those values are configuration candidates only: production installs NPC
identity in the ledger and records an exact wake-bound availability snapshot
before a selected plan may reference either one.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import Field, model_validator
import yaml

from .local_chronology import LocalChronology
from .schema_core import FrozenModel, PrivacyClass


LifeOpeningSource = Literal[
    "routine", "intentional_goal", "social", "environmental_opportunity",
    "interruption", "aftermath", "user_influence",
]
# Reviewed self-capture facts an opening's settled occurrence may declare.
# The vocabulary intentionally mirrors the image-evidence capture contract;
# it never includes third parties the catalog cannot prove present.
ReviewedCaptureCapability = Literal[
    "character_front_camera", "mirror", "timer_fixed", "requested_helper",
]
LifeDomain = Literal[
    "sleep_wake", "hygiene_private", "meal_drink", "study_class",
    "creative_photo_writing", "commute_walk", "errand_household",
    "rest_recovery", "family_roommate_friend", "digital_leisure",
]
SocialShape = Literal["alone", "npc", "user_relayed", "shared_private", "public"]
Deviation = Literal["delay", "change_plan", "avoid", "impulse", "persist", "repair"]
VisualPotential = Literal[
    "none", "ambient", "place", "object", "food", "activity", "social",
    "character", "private_transition",
]
_PRIVACY_RANK = {"public": 0, "shareable": 1, "personal": 2, "private": 3, "withhold": 4}


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ReviewedLifeSeedOpening(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    activity_kind: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    source: LifeOpeningSource
    domain: LifeDomain
    social_shape: SocialShape = "alone"
    deviation: Deviation = "persist"
    visual_potential: VisualPotential = "none"
    privacy: PrivacyClass = "personal"
    local_windows: tuple[str, ...] = Field(min_length=1, max_length=8)
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)
    duration_minutes: int = Field(ge=5, le=12 * 60)
    importance_bp: int = Field(ge=0, le=10_000)
    max_per_local_day: int = Field(default=1, ge=1, le=8)
    location_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    npc_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    # Only meaningful for shared_private future openings: the user
    # relationship's slow closeness variable must have reached this floor
    # before the opening may even be offered to the invitation lane.
    requires_relationship_closeness_bp: int | None = Field(default=None, ge=0, le=10_000)
    outcomes: tuple["ReviewedLifeOutcome", ...] = ()
    # Optional reviewed visual annex.  Presence never creates a photo; it only
    # registers which always-true visible facts a settled occurrence of this
    # opening may later declare through the source-bound declaration seam.
    visual_evidence: "ReviewedOpeningVisualEvidence | None" = None

    # Present-moment openings stay alone/NPC; the future subclass additionally
    # reviews user-participating shared_private invitations.
    _REVIEWED_SOCIAL_SHAPES: ClassVar[frozenset[str]] = frozenset({"alone", "npc"})

    @model_validator(mode="after")
    def reviewed_coordinates_are_canonical(self) -> "ReviewedLifeSeedOpening":
        if self.weekdays != tuple(sorted(set(self.weekdays))) or any(
            value < 0 or value > 6 for value in self.weekdays
        ):
            raise ValueError("life seed weekdays must be unique Monday=0 values")
        for value in self.local_windows:
            _parse_window(value)
        if (self.social_shape == "npc") != (self.npc_id is not None):
            raise ValueError("NPC-shaped life opening must bind exactly one reviewed NPC")
        if self.social_shape not in type(self)._REVIEWED_SOCIAL_SHAPES:
            raise ValueError("life opening social shape is not reviewed for this catalog section")
        if self.social_shape == "alone" and self.npc_id is not None:
            raise ValueError("alone life opening cannot bind an NPC")
        if self.social_shape == "shared_private":
            if self.requires_relationship_closeness_bp is None:
                raise ValueError("shared_private opening requires a relationship closeness floor")
            if self.privacy not in {"private", "withhold"}:
                raise ValueError("shared_private opening must stay private")
        elif self.requires_relationship_closeness_bp is not None:
            raise ValueError("only shared_private openings may gate on relationship closeness")
        if self.visual_evidence is not None:
            if self.visual_potential == "none":
                raise ValueError("a visually silent opening cannot register visual evidence")
            if self.privacy == "withhold":
                raise ValueError("a withheld opening cannot register visual evidence")
            if self.visual_potential == "private_transition":
                if self.privacy != "private":
                    raise ValueError("private transition visual evidence requires a private opening")
                if set(self.visual_evidence.self_capture) - {"character_front_camera", "mirror"}:
                    raise ValueError(
                        "private transition visual evidence permits only self-authored capture"
                    )
            elif self.privacy not in {"public", "shareable"}:
                # Personal/private ordinary openings have no reviewed public
                # declaration path yet; registering an annex would leave a
                # permanently undeclarable dead entry.
                raise ValueError(
                    "ordinary visual evidence requires a public or shareable opening"
                )
        return self

    def eligible_at(self, local_time: datetime) -> bool:
        minute = local_time.hour * 60 + local_time.minute
        return local_time.weekday() in self.weekdays and any(
            _contains_minute(window, minute) for window in self.local_windows
        )

    def policy_refs(self, *, catalog_version: str) -> tuple[str, ...]:
        return tuple(sorted({
            f"policy:life-author-catalog:{catalog_version}",
            f"matrix:source:{self.source}",
            f"matrix:domain:{self.domain}",
            f"matrix:social:{self.social_shape}",
            f"matrix:deviation:{self.deviation}",
            f"matrix:visual:{self.visual_potential}",
            f"matrix:privacy:{self.privacy}",
        }))


class ReviewedLifeOutcome(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    text: str = Field(min_length=1, max_length=800)
    privacy: PrivacyClass


class ReviewedVisualLocationFacts(FrozenModel):
    """Reviewed, always-true visible facts of an opening's bound place."""

    id: str = Field(min_length=1, max_length=128)
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    city: str | None = Field(default=None, min_length=1, max_length=64)
    publicness: Literal["public", "semi_public", "private"] | None = None
    mirror_available: bool | None = None


class ReviewedVisualEnvironmentFacts(FrozenModel):
    light: str | None = Field(default=None, min_length=1, max_length=120)
    structure: str | None = Field(default=None, min_length=1, max_length=160)


class ReviewedVisualObjectFacts(FrozenModel):
    id: str = Field(min_length=1, max_length=128)
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=240)


class ReviewedOpeningVisualEvidence(FrozenModel):
    """The reviewed visual slice a settled occurrence of this opening supports.

    This is configuration, not history: nothing here asserts that a photo
    exists or may be sent.  A declaration author may copy these values
    verbatim into a source-bound visual declaration only after the occurrence
    actually settled; the settled outcome text remains the event-specific
    evidence.  Free-form prompt material stays forbidden.
    """

    activity_description: str = Field(min_length=1, max_length=240)
    location: ReviewedVisualLocationFacts | None = None
    environment: ReviewedVisualEnvironmentFacts | None = None
    objects: tuple[ReviewedVisualObjectFacts, ...] = Field(default=(), max_length=4)
    self_capture: tuple[ReviewedCaptureCapability, ...] = Field(default=(), max_length=4)
    share_chance_bp: int | None = Field(default=None, ge=1, le=9_000)

    @model_validator(mode="after")
    def capture_facts_are_canonical(self) -> "ReviewedOpeningVisualEvidence":
        if len(set(self.self_capture)) != len(self.self_capture):
            raise ValueError("reviewed self capture capabilities must be unique")
        if "mirror" in self.self_capture and (
            self.location is None or self.location.mirror_available is not True
        ):
            raise ValueError("reviewed mirror capture requires a reviewed mirror location fact")
        if {"timer_fixed", "requested_helper"}.intersection(self.self_capture) and (
            self.location is None or self.location.publicness != "public"
        ):
            raise ValueError("reviewed check-in capture requires a reviewed public location fact")
        object_ids = tuple(item.id for item in self.objects)
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("reviewed visual object ids must be unique")
        return self


class ReviewedLifeSeedFutureOpening(ReviewedLifeSeedOpening):
    """A reviewed opening the companion may commit to days ahead of time.

    A future opening never competes with the present-moment lane: it is only
    offered to the Future Life Author, which schedules a concrete slot inside
    one of its ``local_windows`` on a day ``advance_days_min..advance_days_max``
    ahead.  The lifecycle machinery then honors the resulting plan exactly like
    any other clock-bound plan when its day arrives.
    """

    advance_days_min: int = Field(default=1, ge=1, le=7)
    advance_days_max: int = Field(default=7, ge=1, le=7)

    # Future openings may additionally review one user-participating
    # shared_private shape: the invitation lane alone consumes it, gated by
    # relationship closeness, and only a real user answer can start it.
    _REVIEWED_SOCIAL_SHAPES: ClassVar[frozenset[str]] = frozenset(
        {"alone", "npc", "shared_private"}
    )

    @model_validator(mode="after")
    def future_slot_is_schedulable(self) -> "ReviewedLifeSeedFutureOpening":
        if self.advance_days_min > self.advance_days_max:
            raise ValueError("future opening advance day range is inverted")
        for window in self.local_windows:
            start, end = _parse_window(window)
            # A future slot is scheduled at the window start with the reviewed
            # duration, so the window must not wrap midnight and must be able
            # to contain the whole slot within one civil day.
            if start >= end:
                raise ValueError("future opening window cannot wrap midnight")
            if start + self.duration_minutes > end:
                raise ValueError("future opening duration does not fit its window")
        return self



NpcInitiativeKind = Literal["small_favor", "shared_time", "friction"]


class ReviewedNpcInitiatedEvent(FrozenModel):
    """A reviewed possibility of one NPC entering her day uninvited.

    These are the only moments in which an NPC may act first (borrow a book,
    pull her aside, disagree about the reading list).  Whether one actually
    happens is decided by the NPC-initiative runtime: a recorded weighted draw
    whose "nothing happened" candidate is always legal, then a bounded model
    confirmation.  ``base_chance_bp`` is the reviewed per-check baseline mass
    inside a 10_000 probability space shared with that nothing candidate, so
    small values keep NPC initiative a rare texture rather than a schedule.
    """

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    initiative_kind: NpcInitiativeKind
    npc_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    location_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    summary: str = Field(min_length=1, max_length=240)
    privacy: PrivacyClass
    local_windows: tuple[str, ...] = Field(min_length=1, max_length=8)
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)
    duration_minutes: int = Field(ge=5, le=180)
    base_chance_bp: int = Field(ge=1, le=10_000)
    outcomes: tuple["ReviewedLifeOutcome", ...] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def reviewed_initiative_is_canonical(self) -> "ReviewedNpcInitiatedEvent":
        _validate_availability(self.local_windows, self.weekdays)
        for window in self.local_windows:
            start, end = _parse_window(window)
            # An initiated occurrence opens at the wake instant and runs its
            # full reviewed duration, so like future openings the window must
            # not wrap midnight and must be able to host the whole slot.
            if start >= end:
                raise ValueError("npc initiated event window cannot wrap midnight")
            if start + self.duration_minutes > end:
                raise ValueError("npc initiated event duration does not fit its window")
        outcome_ids = tuple(item.id for item in self.outcomes)
        if len(outcome_ids) != len(set(outcome_ids)):
            raise ValueError("npc initiated event outcome ids must be unique")
        if any(
            _PRIVACY_RANK[item.privacy] < _PRIVACY_RANK[self.privacy]
            for item in self.outcomes
        ):
            raise ValueError("npc initiated event outcome cannot weaken its event privacy")
        return self

    def eligible_at(self, local_time: datetime) -> bool:
        """The full reviewed duration must fit a window starting right now."""

        minute = local_time.hour * 60 + local_time.minute
        if local_time.weekday() not in self.weekdays:
            return False
        for window in self.local_windows:
            start, end = _parse_window(window)
            if start <= minute and minute + self.duration_minutes <= end:
                return True
        return False


class ReviewedAspirationSeed(FrozenModel):
    """One reviewed low-stakes wish the companion may quietly come to hold.

    Unlike openings, a seed carries no windows or durations: an aspiration has
    no due window and never enters the lifecycle pipeline.  ``text`` is the
    wish exactly as she would hold it (anti-fabrication: the model confirms or
    declines, it never writes the wish).  ``base_chance_bp`` is the per-check
    planting mass inside a 10_000 space shared with an always-legal "nothing"
    candidate, so wishes stay rare (production values sit around 500-1000).
    ``requires_recent_activity_kinds`` is the eligibility witness: the wish may
    only sprout while at least one plan of a listed kind was accepted within
    the runtime's recent-material window (empty = no witness required).
    """

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    text: str = Field(min_length=1, max_length=240)
    privacy: PrivacyClass
    base_chance_bp: int = Field(ge=1, le=10_000)
    requires_recent_activity_kinds: tuple[str, ...] = Field(default=(), max_length=8)
    # The reviewed crystallization seam: when set, an active wish grown from
    # this seed may — rarely, via a recorded draw plus a bounded model
    # confirmation — turn into one concrete future plan of exactly this
    # reviewed future opening.  ``None`` keeps the wish forever ambient.
    crystallizes_into: str | None = Field(
        default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$"
    )

    @model_validator(mode="after")
    def witness_kinds_are_canonical(self) -> "ReviewedAspirationSeed":
        if self.requires_recent_activity_kinds != tuple(
            sorted(set(self.requires_recent_activity_kinds))
        ):
            raise ValueError("aspiration witness kinds must be unique and sorted")
        return self


class ReviewedLifeSeedLocation(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    location_ref: str = Field(min_length=1, max_length=256)
    privacy: PrivacyClass
    local_windows: tuple[str, ...] = Field(min_length=1, max_length=8)
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def availability_is_canonical(self) -> "ReviewedLifeSeedLocation":
        _validate_availability(self.local_windows, self.weekdays)
        return self

    def available_at(self, local_time: datetime) -> bool:
        return _available_at(self.local_windows, self.weekdays, local_time)


class ReviewedLifeSeedNpc(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    npc_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    stable_identity_ref: str = Field(min_length=1, max_length=256)
    known_trait_refs: tuple[str, ...] = ()
    privacy: PrivacyClass
    location_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    local_windows: tuple[str, ...] = Field(min_length=1, max_length=8)
    weekdays: tuple[int, ...] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def availability_is_canonical(self) -> "ReviewedLifeSeedNpc":
        _validate_availability(self.local_windows, self.weekdays)
        if len(self.known_trait_refs) != len(set(self.known_trait_refs)):
            raise ValueError("reviewed NPC trait refs must be unique")
        return self

    def available_at(self, local_time: datetime) -> bool:
        return _available_at(self.local_windows, self.weekdays, local_time)


class _CatalogDocument(FrozenModel):
    version: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    openings: tuple[ReviewedLifeSeedOpening, ...] = Field(min_length=1, max_length=128)
    future_openings: tuple[ReviewedLifeSeedFutureOpening, ...] = Field(
        default=(), max_length=32
    )
    npc_initiated_events: tuple[ReviewedNpcInitiatedEvent, ...] = Field(
        default=(), max_length=32
    )
    aspiration_seeds: tuple[ReviewedAspirationSeed, ...] = Field(
        default=(), max_length=16
    )
    locations: tuple[ReviewedLifeSeedLocation, ...] = ()
    npcs: tuple[ReviewedLifeSeedNpc, ...] = ()

    @model_validator(mode="after")
    def opening_ids_are_unique(self) -> "_CatalogDocument":
        ids = tuple(
            item.id
            for item in (
                *self.openings,
                *self.future_openings,
                *self.npc_initiated_events,
                *self.aspiration_seeds,
            )
        )
        if len(ids) != len(set(ids)):
            raise ValueError("life author seed opening ids must be unique")
        known_activity_kinds = {
            item.activity_kind for item in (*self.openings, *self.future_openings)
        }
        known_future_opening_ids = {item.id for item in self.future_openings}
        for seed in self.aspiration_seeds:
            unknown = set(seed.requires_recent_activity_kinds) - known_activity_kinds
            if unknown:
                raise ValueError(
                    "aspiration seed witness references an unknown activity kind"
                )
            if (
                seed.crystallizes_into is not None
                and seed.crystallizes_into not in known_future_opening_ids
            ):
                raise ValueError(
                    "aspiration seed crystallizes into an unknown future opening"
                )
        # Aftermath resolves outcomes by activity kind, so a kind may not be
        # claimed by both a present-moment opening and a future opening.
        kinds = tuple(
            item.activity_kind for item in (*self.openings, *self.future_openings)
        )
        if len(kinds) != len(set(kinds)):
            raise ValueError("life author seed activity kinds must be unique")
        location_ids = tuple(item.id for item in self.locations)
        location_refs = tuple(item.location_ref for item in self.locations)
        npc_ids = tuple(item.id for item in self.npcs)
        stable_npc_ids = tuple(item.npc_id for item in self.npcs)
        stable_refs = tuple(item.stable_identity_ref for item in self.npcs)
        if len(location_ids) != len(set(location_ids)) or len(location_refs) != len(set(location_refs)):
            raise ValueError("reviewed locations must have unique ids and refs")
        if (
            len(npc_ids) != len(set(npc_ids))
            or len(stable_npc_ids) != len(set(stable_npc_ids))
            or len(stable_refs) != len(set(stable_refs))
        ):
            raise ValueError("reviewed NPC identities must be unique")
        known_locations = set(location_ids)
        known_npcs = set(npc_ids)
        every_opening = (*self.openings, *self.future_openings)
        if any(item.location_id not in known_locations for item in self.npcs if item.location_id):
            raise ValueError("reviewed NPC references an unknown location")
        if any(item.location_id not in known_locations for item in every_opening if item.location_id):
            raise ValueError("life opening references an unknown location")
        if any(item.npc_id not in known_npcs for item in every_opening if item.npc_id):
            raise ValueError("life opening references an unknown NPC")
        locations = {item.id: item for item in self.locations}
        npcs = {item.id: item for item in self.npcs}
        for opening in every_opening:
            location = locations.get(opening.location_id or "")
            npc = npcs.get(opening.npc_id or "")
            if location is not None and _PRIVACY_RANK[opening.privacy] < _PRIVACY_RANK[location.privacy]:
                raise ValueError("life opening cannot weaken its reviewed location privacy")
            if npc is not None and _PRIVACY_RANK[opening.privacy] < _PRIVACY_RANK[npc.privacy]:
                raise ValueError("life opening cannot weaken its reviewed NPC privacy")
            outcome_ids = tuple(item.id for item in opening.outcomes)
            if len(outcome_ids) != len(set(outcome_ids)):
                raise ValueError("life opening outcome ids must be unique")
            if any(
                _PRIVACY_RANK[item.privacy] < _PRIVACY_RANK[opening.privacy]
                for item in opening.outcomes
            ):
                raise ValueError("life outcome cannot weaken its opening privacy")
        for event in self.npc_initiated_events:
            npc = npcs.get(event.npc_id)
            location = locations.get(event.location_id)
            if npc is None:
                raise ValueError("npc initiated event references an unknown NPC")
            if location is None:
                raise ValueError("npc initiated event references an unknown location")
            if _PRIVACY_RANK[event.privacy] < _PRIVACY_RANK[npc.privacy]:
                raise ValueError("npc initiated event cannot weaken its reviewed NPC privacy")
            if _PRIVACY_RANK[event.privacy] < _PRIVACY_RANK[location.privacy]:
                raise ValueError("npc initiated event cannot weaken its reviewed location privacy")
            # The event may only happen while the NPC is present: every legal
            # start minute plus the full duration must lie inside the NPC's
            # and the location's reviewed weekly availability.
            for weekday in event.weekdays:
                for window in event.local_windows:
                    start, end = _parse_window(window)
                    for schedule in (npc, location):
                        if not _weekly_covers_slot(
                            schedule.local_windows, schedule.weekdays,
                            weekday=weekday, start_minute=start,
                            duration_minutes=end - start,
                        ):
                            raise ValueError(
                                "npc initiated event window is outside reviewed availability"
                            )
        return self


class ReviewedLifeSeedCandidate(FrozenModel):
    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening: ReviewedLifeSeedOpening
    location_ref: str | None = None
    participant_ref: str | None = None
    availability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    daypart_fit_bp: int = Field(default=10_000, ge=1, le=10_000)


class ReviewedLifeSeedFutureCandidate(FrozenModel):
    """One concrete future slot: a reviewed opening on a specific local day.

    The token deliberately excludes the wake event: the same local day must
    compile the same candidate identity on every wake, so the daily recorded
    draw and its bounded model decision replay instead of re-rolling.
    """

    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    opening: ReviewedLifeSeedFutureOpening
    target_local_date: date
    day_offset: int = Field(ge=1, le=7)
    local_window: str = Field(min_length=1)
    opens_at: datetime
    closes_at: datetime
    location_ref: str | None = None
    participant_ref: str | None = None
    availability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class NpcInitiativeCandidate(FrozenModel):
    """One reviewed NPC-initiated event that could legally start right now.

    The token deliberately excludes the wake event and the minute: one local
    day compiles one stable identity per reviewed event, so the daily check
    budget and the at-most-one-occurrence-per-day rule replay deterministically.
    """

    token: str = Field(pattern=r"^[0-9a-f]{64}$")
    event: ReviewedNpcInitiatedEvent
    npc_ref: str = Field(min_length=1)
    location_ref: str = Field(min_length=1)
    availability_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewedLifeSeedReachability(FrozenModel):
    """Catalog-level proof that all reviewed authorities can overlap."""

    opening_id: str = Field(min_length=1)
    activity_kind: str = Field(min_length=1)
    reachable: bool
    witness_weekday: int | None = Field(default=None, ge=0, le=6)
    witness_minute: int | None = Field(default=None, ge=0, le=1_439)
    reason_code: Literal["reachable", "no_joint_reviewed_availability"]

    @model_validator(mode="after")
    def witness_matches_status(self) -> "ReviewedLifeSeedReachability":
        has_witness = self.witness_weekday is not None and self.witness_minute is not None
        if self.reachable != has_witness:
            raise ValueError("reachable life opening must have exactly one civil-time witness")
        if self.reachable != (self.reason_code == "reachable"):
            raise ValueError("life opening reachability reason does not match status")
        return self


class ReviewedLifeSeedCatalog:
    """Load once, then compile eligible candidates from local chronology."""

    def __init__(self, *, document: _CatalogDocument, chronology: LocalChronology) -> None:
        self._document = document
        self._chronology = chronology
        self.version = document.version
        self.catalog_hash = _digest(document.model_dump(mode="json"))

    @classmethod
    def from_yaml(cls, *, path: Path, chronology: LocalChronology) -> "ReviewedLifeSeedCatalog":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("life_author_catalog"), dict):
            raise ValueError("world seed has no reviewed life_author_catalog")
        catalog = dict(raw["life_author_catalog"])
        openings = catalog.get("openings")
        if not isinstance(openings, list):
            raise ValueError("world seed life_author_catalog.openings must be a list")
        future_openings = catalog.get("future_openings", ())
        if not isinstance(future_openings, (list, tuple)):
            raise ValueError("world seed life_author_catalog.future_openings must be a list")
        npc_initiated_events = catalog.get("npc_initiated_events", ())
        if not isinstance(npc_initiated_events, (list, tuple)):
            raise ValueError("world seed life_author_catalog.npc_initiated_events must be a list")
        def _canonical_visual_evidence(item: dict) -> dict:
            annex = item.get("visual_evidence")
            if not isinstance(annex, dict):
                return item
            return {
                **item,
                "visual_evidence": {
                    **annex,
                    "objects": tuple(annex.get("objects", ())),
                    "self_capture": tuple(annex.get("self_capture", ())),
                },
            }

        for field, values in (
            ("openings", openings),
            ("future_openings", future_openings),
            ("npc_initiated_events", npc_initiated_events),
        ):
            catalog[field] = tuple(
                _canonical_visual_evidence({
                    **item,
                    "local_windows": tuple(item.get("local_windows", ())),
                    "weekdays": tuple(item.get("weekdays", ())),
                    "outcomes": tuple(item.get("outcomes", ())),
                })
                if isinstance(item, dict)
                else item
                for item in values
            )
        aspiration_seeds = catalog.get("aspiration_seeds", ())
        if not isinstance(aspiration_seeds, (list, tuple)):
            raise ValueError("world seed life_author_catalog.aspiration_seeds must be a list")
        catalog["aspiration_seeds"] = tuple(
            {
                **item,
                "requires_recent_activity_kinds": tuple(
                    item.get("requires_recent_activity_kinds", ())
                ),
            }
            if isinstance(item, dict)
            else item
            for item in aspiration_seeds
        )
        for field in ("locations", "npcs"):
            values = catalog.get(field, ())
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"world seed life_author_catalog.{field} must be a list")
            catalog[field] = tuple(
                {
                    **item,
                    "local_windows": tuple(item.get("local_windows", ())),
                    "weekdays": tuple(item.get("weekdays", ())),
                    **(
                        {"known_trait_refs": tuple(item.get("known_trait_refs", ()))}
                        if field == "npcs" else {}
                    ),
                }
                if isinstance(item, dict) else item
                for item in values
            )
        document = _CatalogDocument.model_validate(catalog)
        return cls(document=document, chronology=chronology)

    def candidates_at(
        self, *, instant: datetime, wake_event_ref: str, plans: tuple[object, ...],
        npcs: tuple[object, ...] = (),
    ) -> tuple[ReviewedLifeSeedCandidate, ...]:
        local = self._chronology.localize(instant)
        assert local is not None
        open_plans = tuple(
            plan for plan in plans
            if getattr(plan, "status", None) in {"planned", "active", "paused"}
        )
        accepted_locals: dict[str, list[datetime]] = {}
        for plan in plans:
            accepted_at = getattr(getattr(plan, "authority_origin", None), "accepted_at", None)
            if not isinstance(accepted_at, datetime):
                continue
            accepted_local = self._chronology.localize(accepted_at)
            if accepted_local is not None:
                kind = str(getattr(plan, "activity_kind", ""))
                accepted_locals.setdefault(kind, []).append(accepted_local)
        locations = {item.id: item for item in self._document.locations}
        reviewed_npcs = {item.id: item for item in self._document.npcs}
        active_npc_ids = {
            str(getattr(item, "npc_id", "")) for item in npcs
            if getattr(item, "status", None) == "active"
        }
        candidates: list[ReviewedLifeSeedCandidate] = []
        for opening in self._document.openings:
            location = locations.get(opening.location_id or "")
            npc = reviewed_npcs.get(opening.npc_id or "")
            used_today = sum(
                _attributed_local_date(accepted_local, opening=opening) == local.date()
                for accepted_local in accepted_locals.get(opening.activity_kind, ())
            )
            if not opening.eligible_at(local) or used_today >= opening.max_per_local_day:
                continue
            if self._present_lane_suppressed(
                open_plans=open_plans, instant=instant, opening=opening
            ):
                continue
            if location is not None and not location.available_at(local):
                continue
            if npc is not None and (
                npc.npc_id not in active_npc_ids
                or not npc.available_at(local)
                or (opening.location_id is not None and npc.location_id != opening.location_id)
            ):
                continue
            availability_material = {
                "catalog_version": self.version,
                "catalog_hash": self.catalog_hash,
                "wake_event_ref": wake_event_ref,
                "local_instant": local.isoformat(),
                "opening_id": opening.id,
                "location_id": opening.location_id,
                "location_ref": location.location_ref if location else None,
                "npc_id": npc.npc_id if npc else None,
                "daypart_fit_bp": _daypart_fit_bp(opening.local_windows, local),
            }
            candidates.append(ReviewedLifeSeedCandidate(
                token=_digest({
                    "catalog_version": self.version,
                    "catalog_hash": self.catalog_hash,
                    "wake_event_ref": wake_event_ref,
                    "opening_id": opening.id,
                }),
                opening=opening,
                location_ref=location.location_ref if location else None,
                participant_ref=f"npc:{npc.npc_id}" if npc else None,
                availability_hash=_digest(availability_material),
                daypart_fit_bp=availability_material["daypart_fit_bp"],
            ))
        return tuple(candidates)

    @staticmethod
    def _present_lane_suppressed(
        *, open_plans: tuple[object, ...], instant: datetime,
        opening: ReviewedLifeSeedOpening,
    ) -> bool:
        """Decide whether existing plans occupy the present-moment slot.

        Only a life that is actually in motion suppresses a new present
        opening: an active/paused plan, a planned window that would overlap
        the would-be activity ``[instant, instant + duration]``, or an
        overdue planned window still waiting on lifecycle resolution.  A plan
        whose window opens strictly beyond the current opening ("周三去湖边"
        made on Monday) leaves today's life free instead of freezing it.
        """

        horizon = instant + timedelta(minutes=opening.duration_minutes)
        for plan in open_plans:
            if getattr(plan, "status", None) in {"active", "paused"}:
                return True
            window = getattr(plan, "scheduled_window", None)
            opens_at = getattr(window, "opens_at", None)
            if not isinstance(opens_at, datetime) or opens_at <= horizon:
                return True
        return False

    def future_candidates_at(
        self, *, instant: datetime, plans: tuple[object, ...],
        npcs: tuple[object, ...] = (), horizon_days: int = 7,
        max_candidates: int = 16,
        social_shapes: frozenset[str] = frozenset({"alone", "npc"}),
    ) -> tuple[ReviewedLifeSeedFutureCandidate, ...]:
        """Compile concrete future slots from the reviewed future catalog.

        Each candidate is one (future opening, local day, window) triple whose
        reviewed weekly location/NPC availability covers the whole slot and
        whose slot does not collide with an already-committed plan window.
        Identity is wake-independent so one local day compiles one stable
        candidate set (see ``ReviewedLifeSeedFutureCandidate``).

        ``social_shapes`` keeps the lanes honest: the ordinary future life
        author never schedules a ``shared_private`` invitation on its own,
        and the invitation lane compiles only that shape.
        """

        local = self._chronology.localize(instant)
        assert local is not None
        occupied = tuple(
            window
            for plan in plans
            if getattr(plan, "status", None) in {"planned", "active", "paused"}
            and (window := getattr(plan, "scheduled_window", None)) is not None
        )
        locations = {item.id: item for item in self._document.locations}
        reviewed_npcs = {item.id: item for item in self._document.npcs}
        active_npc_ids = {
            str(getattr(item, "npc_id", "")) for item in npcs
            if getattr(item, "status", None) == "active"
        }
        candidates: list[ReviewedLifeSeedFutureCandidate] = []
        for opening in self._document.future_openings:
            if opening.social_shape not in social_shapes:
                continue
            location = locations.get(opening.location_id or "")
            npc = reviewed_npcs.get(opening.npc_id or "")
            if npc is not None and (
                npc.npc_id not in active_npc_ids
                or (opening.location_id is not None and npc.location_id != opening.location_id)
            ):
                continue
            first_offset = max(1, opening.advance_days_min)
            last_offset = min(horizon_days, opening.advance_days_max)
            for offset in range(first_offset, last_offset + 1):
                target_date = local.date() + timedelta(days=offset)
                if target_date.weekday() not in opening.weekdays:
                    continue
                for window in opening.local_windows:
                    start_minute, _ = _parse_window(window)
                    if location is not None and not _weekly_covers_slot(
                        location.local_windows, location.weekdays,
                        weekday=target_date.weekday(), start_minute=start_minute,
                        duration_minutes=opening.duration_minutes,
                    ):
                        continue
                    if npc is not None and not _weekly_covers_slot(
                        npc.local_windows, npc.weekdays,
                        weekday=target_date.weekday(), start_minute=start_minute,
                        duration_minutes=opening.duration_minutes,
                    ):
                        continue
                    opens_at = datetime.combine(
                        target_date,
                        time(start_minute // 60, start_minute % 60),
                        tzinfo=local.tzinfo,
                    ).astimezone(timezone.utc)
                    closes_at = opens_at + timedelta(minutes=opening.duration_minutes)
                    if any(
                        item.opens_at < closes_at and opens_at < item.closes_at
                        for item in occupied
                    ):
                        continue
                    availability_material = {
                        "catalog_version": self.version,
                        "catalog_hash": self.catalog_hash,
                        "opening_id": opening.id,
                        "target_local_date": target_date.isoformat(),
                        "local_window": window,
                        "location_id": opening.location_id,
                        "location_ref": location.location_ref if location else None,
                        "npc_id": npc.npc_id if npc else None,
                    }
                    candidates.append(ReviewedLifeSeedFutureCandidate(
                        token=_digest({
                            "catalog_version": self.version,
                            "catalog_hash": self.catalog_hash,
                            "opening_id": opening.id,
                            "target_local_date": target_date.isoformat(),
                            "local_window": window,
                        }),
                        opening=opening,
                        target_local_date=target_date,
                        day_offset=offset,
                        local_window=window,
                        opens_at=opens_at,
                        closes_at=closes_at,
                        location_ref=location.location_ref if location else None,
                        participant_ref=f"npc:{npc.npc_id}" if npc else None,
                        availability_hash=_digest(availability_material),
                    ))
        candidates.sort(key=lambda item: (item.day_offset, item.opening.id, item.local_window))
        return tuple(candidates[:max_candidates])

    def npc_initiative_candidates_at(
        self, *, instant: datetime, npcs: tuple[object, ...] = (),
    ) -> tuple[NpcInitiativeCandidate, ...]:
        """Compile the NPC-initiated events that may legally begin right now.

        Presence is absolute: an event is offered only while its reviewed NPC
        (and location) can cover the whole duration, and only for an NPC that
        is registered active in the ledger.  Everything past this point is
        probability and semantics, never eligibility.
        """

        local = self._chronology.localize(instant)
        assert local is not None
        minute = local.hour * 60 + local.minute
        locations = {item.id: item for item in self._document.locations}
        reviewed_npcs = {item.id: item for item in self._document.npcs}
        active_npc_ids = {
            str(getattr(item, "npc_id", "")) for item in npcs
            if getattr(item, "status", None) == "active"
        }
        candidates: list[NpcInitiativeCandidate] = []
        for event in self._document.npc_initiated_events:
            npc = reviewed_npcs[event.npc_id]
            location = locations[event.location_id]
            if npc.npc_id not in active_npc_ids or not event.eligible_at(local):
                continue
            # Load-time validation already proved the reviewed windows lie
            # inside NPC/location availability; re-checking here keeps "never
            # while the NPC is absent" true even if that invariant loosens.
            if not all(
                _weekly_covers_slot(
                    schedule.local_windows, schedule.weekdays,
                    weekday=local.weekday(), start_minute=minute,
                    duration_minutes=event.duration_minutes,
                )
                for schedule in (npc, location)
            ):
                continue
            availability_material = {
                "catalog_version": self.version,
                "catalog_hash": self.catalog_hash,
                "event_id": event.id,
                "local_date": local.date().isoformat(),
                "npc_id": npc.npc_id,
                "location_ref": location.location_ref,
            }
            candidates.append(NpcInitiativeCandidate(
                token=_digest({
                    "catalog_version": self.version,
                    "catalog_hash": self.catalog_hash,
                    "event_id": event.id,
                    "local_date": local.date().isoformat(),
                }),
                event=event,
                npc_ref=f"npc:{npc.npc_id}",
                location_ref=location.location_ref,
                availability_hash=_digest(availability_material),
            ))
        candidates.sort(key=lambda item: item.event.id)
        return tuple(candidates)

    @property
    def reviewed_locations(self) -> tuple[ReviewedLifeSeedLocation, ...]:
        return self._document.locations

    @property
    def reviewed_npcs(self) -> tuple[ReviewedLifeSeedNpc, ...]:
        return self._document.npcs

    @property
    def reviewed_future_openings(self) -> tuple[ReviewedLifeSeedFutureOpening, ...]:
        return self._document.future_openings

    @property
    def reviewed_npc_initiated_events(self) -> tuple[ReviewedNpcInitiatedEvent, ...]:
        return self._document.npc_initiated_events

    @property
    def reviewed_aspiration_seeds(self) -> tuple[ReviewedAspirationSeed, ...]:
        return self._document.aspiration_seeds

    def localize(self, instant: datetime) -> datetime:
        """Expose the deployment-owned civil time this catalog was reviewed in."""

        local = self._chronology.localize(instant)
        assert local is not None
        return local

    @property
    def activity_domains(self) -> dict[str, str]:
        """Reviewed activity-to-domain coordinates used by generic rhythm policy."""

        return {
            item.activity_kind: item.domain
            for item in (*self._document.openings, *self._document.future_openings)
        }

    def reachability_report(self) -> tuple[ReviewedLifeSeedReachability, ...]:
        """Prove every opening against reviewed weekly location/NPC schedules.

        This does not assert that an activity happened or that a dynamic NPC
        will be active.  It only exposes whether the reviewed possibility
        catalog contains at least one civil minute at which all of its static
        authorities can legally overlap.
        """

        locations = {item.id: item for item in self._document.locations}
        npcs = {item.id: item for item in self._document.npcs}
        report: list[ReviewedLifeSeedReachability] = []
        for opening in (*self._document.openings, *self._document.future_openings):
            location = locations.get(opening.location_id or "")
            npc = npcs.get(opening.npc_id or "")
            witness: tuple[int, int] | None = None
            for weekday in range(7):
                if weekday not in opening.weekdays:
                    continue
                for minute in range(1_440):
                    if not any(
                        _contains_minute(window, minute)
                        for window in opening.local_windows
                    ):
                        continue
                    if location is not None and (
                        weekday not in location.weekdays
                        or not any(
                            _contains_minute(window, minute)
                            for window in location.local_windows
                        )
                    ):
                        continue
                    if npc is not None and (
                        weekday not in npc.weekdays
                        or not any(
                            _contains_minute(window, minute)
                            for window in npc.local_windows
                        )
                    ):
                        continue
                    witness = (weekday, minute)
                    break
                if witness is not None:
                    break
            report.append(ReviewedLifeSeedReachability(
                opening_id=opening.id,
                activity_kind=opening.activity_kind,
                reachable=witness is not None,
                witness_weekday=witness[0] if witness is not None else None,
                witness_minute=witness[1] if witness is not None else None,
                reason_code=(
                    "reachable" if witness is not None
                    else "no_joint_reviewed_availability"
                ),
            ))
        # NPC-initiated events always bind one NPC and one location, and a
        # witness must be a legal *start* minute (the whole duration fits).
        for event in self._document.npc_initiated_events:
            npc = npcs[event.npc_id]
            location = locations[event.location_id]
            witness = None
            for weekday in range(7):
                if weekday not in event.weekdays:
                    continue
                for window in event.local_windows:
                    start, end = _parse_window(window)
                    for minute in range(start, end - event.duration_minutes + 1):
                        if all(
                            _weekly_covers_slot(
                                schedule.local_windows, schedule.weekdays,
                                weekday=weekday, start_minute=minute,
                                duration_minutes=event.duration_minutes,
                            )
                            for schedule in (npc, location)
                        ):
                            witness = (weekday, minute)
                            break
                    if witness is not None:
                        break
                if witness is not None:
                    break
            report.append(ReviewedLifeSeedReachability(
                opening_id=event.id,
                activity_kind=f"npc_initiative.{event.initiative_kind}",
                reachable=witness is not None,
                witness_weekday=witness[0] if witness is not None else None,
                witness_minute=witness[1] if witness is not None else None,
                reason_code=(
                    "reachable" if witness is not None
                    else "no_joint_reviewed_availability"
                ),
            ))
        return tuple(report)

    def outcomes_for_activity(self, activity_kind: str) -> tuple[ReviewedLifeOutcome, ...]:
        matches = tuple(
            item.outcomes
            for item in (*self._document.openings, *self._document.future_openings)
            if item.activity_kind == activity_kind and item.outcomes
        )
        if len(matches) > 1:
            raise ValueError("reviewed aftermath is ambiguous for activity kind")
        return matches[0] if matches else ()

    def opening_for_activity(self, activity_kind: str) -> ReviewedLifeSeedOpening | None:
        """Return the unique reviewed opening owning one activity kind."""

        return next(
            (
                item
                for item in (*self._document.openings, *self._document.future_openings)
                if item.activity_kind == activity_kind
            ),
            None,
        )


def _parse_window(value: str) -> tuple[int, int]:
    try:
        start, end = value.split("-", 1)
        start_hour, start_minute = (int(item) for item in start.split(":"))
        end_hour, end_minute = (int(item) for item in end.split(":"))
    except (ValueError, AttributeError) as exc:
        raise ValueError("life seed local window must be HH:MM-HH:MM") from exc
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        raise ValueError("life seed local window is outside civil time")
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    if start_total == end_total:
        raise ValueError("life seed local window cannot be empty")
    return start_total, end_total


def _contains_minute(window: str, minute: int) -> bool:
    start, end = _parse_window(window)
    return start <= minute < end if start < end else minute >= start or minute < end


def _attributed_local_date(accepted_local: datetime, *, opening: ReviewedLifeSeedOpening):
    """Attribute one accepted plan to the local day its life-night belongs to.

    A midnight-wrapping window such as bedtime ``22:30-00:30`` regularly
    accepts its plan a few minutes after midnight.  Charging that acceptance
    to the *new* civil day used to consume the whole next day's quota — at
    23:50 tonight she "already prepared for bed today" because of last
    night's 00:01 acceptance — freezing the life author for the entire
    evening.  An acceptance inside the after-midnight tail of a wrapping
    window therefore belongs to the previous local day.
    """

    minute = accepted_local.hour * 60 + accepted_local.minute
    for window in opening.local_windows:
        start, end = _parse_window(window)
        if start > end and minute < end:
            return accepted_local.date() - timedelta(days=1)
    return accepted_local.date()


def _daypart_fit_bp(windows: tuple[str, ...], local: datetime) -> int:
    """Softly prefer the middle of an eligible window without forbidding edges."""

    minute = local.hour * 60 + local.minute
    fits: list[int] = []
    for window in windows:
        if not _contains_minute(window, minute):
            continue
        start, end = _parse_window(window)
        duration = end - start if start < end else 1_440 - start + end
        elapsed = (minute - start) % 1_440
        distance_from_center = abs(2 * elapsed - duration)
        fits.append(
            6_000 + 4_000 * (duration - distance_from_center) // duration
        )
    return max(fits, default=1)


def _validate_availability(windows: tuple[str, ...], weekdays: tuple[int, ...]) -> None:
    if weekdays != tuple(sorted(set(weekdays))) or any(value < 0 or value > 6 for value in weekdays):
        raise ValueError("availability weekdays must be unique Monday=0 values")
    for value in windows:
        _parse_window(value)


def _available_at(windows: tuple[str, ...], weekdays: tuple[int, ...], local: datetime) -> bool:
    minute = local.hour * 60 + local.minute
    return local.weekday() in weekdays and any(_contains_minute(window, minute) for window in windows)


def _weekly_covers_slot(
    windows: tuple[str, ...], weekdays: tuple[int, ...], *,
    weekday: int, start_minute: int, duration_minutes: int,
) -> bool:
    """Check one reviewed weekly schedule covers a whole non-wrapping slot."""

    if weekday not in weekdays:
        return False
    slot_end = start_minute + duration_minutes
    for window in windows:
        start, end = _parse_window(window)
        if start < end:
            if start <= start_minute and slot_end <= end:
                return True
        # A wrapping window ("22:00-02:00") covers the slot if it fits either
        # the late-evening arm or the small-hours arm of the same civil day.
        elif start_minute >= start and slot_end <= end + 1_440:
            return True
        elif slot_end <= end:
            return True
    return False


__all__ = [
    "NpcInitiativeCandidate",
    "NpcInitiativeKind",
    "ReviewedAspirationSeed",
    "ReviewedCaptureCapability",
    "ReviewedNpcInitiatedEvent",
    "ReviewedLifeSeedCandidate",
    "ReviewedLifeSeedCatalog",
    "ReviewedLifeSeedFutureCandidate",
    "ReviewedLifeSeedFutureOpening",
    "ReviewedLifeSeedOpening",
    "ReviewedLifeSeedLocation",
    "ReviewedLifeSeedNpc",
    "ReviewedLifeSeedReachability",
    "ReviewedLifeOutcome",
    "ReviewedOpeningVisualEvidence",
    "ReviewedVisualEnvironmentFacts",
    "ReviewedVisualLocationFacts",
    "ReviewedVisualObjectFacts",
]
