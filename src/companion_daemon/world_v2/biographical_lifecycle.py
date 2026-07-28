"""Biographical time and long-lived life context for World V2.

The module has one small read interface: given authoritative Logical Time and
the currently accepted Life Arcs, return the character's source-bound
biographical context.  Callers do not reimplement age, academic calendar, or
arc activation rules.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator
import yaml

from .life_events import DomainMutationPayload
from .schema_core import FrozenModel
from .schemas import LifeArcProjection


AcademicPhase = Literal[
    "before_enrollment",
    "term",
    "winter_break",
    "summer_break",
    "graduated",
]


class AnnualDateWindow(FrozenModel):
    opens_on: str = Field(pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
    closes_on: str = Field(pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")

    def contains(self, value: date) -> bool:
        token = value.strftime("%m-%d")
        if self.opens_on <= self.closes_on:
            return self.opens_on <= token <= self.closes_on
        return token >= self.opens_on or token <= self.closes_on


class AcademicTimeline(FrozenModel):
    enrolled_on: date
    expected_graduation_on: date
    term_windows: tuple[AnnualDateWindow, ...] = Field(min_length=1, max_length=4)
    winter_break_windows: tuple[AnnualDateWindow, ...] = Field(
        default=(), max_length=2
    )
    summer_break_windows: tuple[AnnualDateWindow, ...] = Field(
        default=(), max_length=2
    )
    enrollment_context_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def dates_are_ordered(self) -> "AcademicTimeline":
        if self.expected_graduation_on <= self.enrolled_on:
            raise ValueError("academic graduation must follow enrollment")
        if self.enrollment_context_tags != tuple(
            sorted(set(self.enrollment_context_tags))
        ):
            raise ValueError("academic enrollment context tags must be sorted and unique")
        if any(
            item.startswith("residence:") for item in self.enrollment_context_tags
        ):
            raise ValueError(
                "academic enrollment context cannot own current residence"
            )
        return self


class BiographicalResidenceTimeline(FrozenModel):
    """Reviewed default residence at each calendar phase.

    These values are operator-owned facts, not activity choices.  An active
    Life Arc carrying one residence tag may temporarily override the applicable
    baseline; when it ends, the phase baseline becomes current again.
    """

    before_enrollment: str = Field(pattern=r"^residence:[a-z0-9._-]+$")
    term: str = Field(pattern=r"^residence:[a-z0-9._-]+$")
    winter_break: str = Field(pattern=r"^residence:[a-z0-9._-]+$")
    summer_break: str = Field(pattern=r"^residence:[a-z0-9._-]+$")
    graduated: str = Field(pattern=r"^residence:[a-z0-9._-]+$")


class BiographicalLifecycleDocument(FrozenModel):
    version: str = Field(min_length=1, max_length=128)
    birth_date: date
    academic: AcademicTimeline
    residence: BiographicalResidenceTimeline
    baseline_context_tags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def context_tags_are_canonical(self) -> "BiographicalLifecycleDocument":
        if self.baseline_context_tags != tuple(sorted(set(self.baseline_context_tags))):
            raise ValueError("baseline biographical context tags must be sorted and unique")
        if any(item.startswith("residence:") for item in self.baseline_context_tags):
            raise ValueError("baseline context cannot own current residence")
        if self.birth_date >= self.academic.enrolled_on:
            raise ValueError("birth date must precede academic enrollment")
        return self


class BiographicalContext(FrozenModel):
    logical_at: datetime
    age: int | None = Field(default=None, ge=0, le=150)
    academic_phase: AcademicPhase | None = None
    academic_year: int | None = Field(default=None, ge=1, le=12)
    context_tags: tuple[str, ...]
    active_life_arc_ids: tuple[str, ...] = ()


class LifeArcChangedPayload(DomainMutationPayload):
    operation: Literal["start", "complete", "abandon"]
    arc_before: LifeArcProjection | None
    arc_after: LifeArcProjection

    @model_validator(mode="after")
    def transition_is_complete(self) -> "LifeArcChangedPayload":
        after = self.arc_after
        if after.source_event_ref not in {item.ref_id for item in self.evidence_refs}:
            raise ValueError("Life Arc source event must be bound as evidence")
        if self.operation == "start":
            if (
                self.arc_before is not None
                or self.expected_entity_revision != 0
                or after.entity_revision != 1
                or after.status != "active"
            ):
                raise ValueError("Life Arc start must create active revision one")
        else:
            before = self.arc_before
            expected_status = "completed" if self.operation == "complete" else "abandoned"
            if (
                before is None
                or before.status != "active"
                or self.expected_entity_revision != before.entity_revision
                or after.entity_revision != before.entity_revision + 1
                or after.arc_id != before.arc_id
                or after.status != expected_status
            ):
                raise ValueError("Life Arc terminal transition is inconsistent")
            immutable = (
                "owner_actor_ref",
                "arc_kind",
                "context_pack_ref",
                "context_tags",
                "effect_descriptor_hash",
                "started_at",
                "ends_at",
                "source_event_ref",
                "privacy_class",
            )
            if any(getattr(after, field) != getattr(before, field) for field in immutable):
                raise ValueError("Life Arc terminal transition changed immutable context")
        return self


BIOGRAPHICAL_LIFECYCLE_PAYLOAD_MODELS = {
    "LifeArcChanged": LifeArcChangedPayload,
}


def reduce_life_arc(
    arcs: tuple[LifeArcProjection, ...],
    payload: LifeArcChangedPayload,
    *,
    logical_time: datetime,
) -> tuple[LifeArcProjection, ...]:
    after = payload.arc_after
    if payload.operation == "start":
        if any(item.arc_id == after.arc_id for item in arcs):
            raise ValueError("Life Arc already exists")
        if after.started_at > logical_time:
            raise ValueError("Life Arc cannot start after authoritative logical time")
        return (*arcs, after)
    before = next((item for item in arcs if item.arc_id == after.arc_id), None)
    if before is None or before != payload.arc_before:
        raise ValueError("Life Arc before image does not match current projection")
    if after.closed_at is None or after.closed_at > logical_time:
        raise ValueError("Life Arc cannot close after authoritative logical time")
    if (
        payload.operation == "complete"
        and before.ends_at is not None
        and after.closed_at != before.ends_at
    ):
        raise ValueError("timed Life Arc completion must preserve its effective end")
    return tuple(after if item.arc_id == after.arc_id else item for item in arcs)


class BiographicalLifecycleCatalog:
    """Compile all date and accepted-arc context behind one interface."""

    def __init__(
        self,
        *,
        document: BiographicalLifecycleDocument | None,
        timezone_name: str,
    ) -> None:
        try:
            timezone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown biographical timezone: {timezone_name}") from exc
        self._document = document
        self._timezone = timezone
        self.version = document.version if document is not None else "biography.unconfigured"
        self.document_hash = hashlib.sha256(
            json.dumps(
                (
                    document.model_dump(mode="json")
                    if document is not None
                    else {"version": self.version}
                ),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    @classmethod
    def from_yaml(
        cls,
        *,
        path: Path,
        timezone_name: str,
    ) -> "BiographicalLifecycleCatalog":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("world seed must be an object")
        document = raw.get("biographical_lifecycle")
        if not isinstance(document, dict):
            # Small test/deployment-specific catalogs predating biography.1 remain
            # valid without inventing an age, school phase, or residence fact.
            return cls(document=None, timezone_name=timezone_name)
        canonical = {
            **document,
            "baseline_context_tags": tuple(document.get("baseline_context_tags", ())),
            "academic": {
                **document["academic"],
                "term_windows": tuple(document["academic"].get("term_windows", ())),
                "winter_break_windows": tuple(
                    document["academic"].get("winter_break_windows", ())
                ),
                "summer_break_windows": tuple(
                    document["academic"].get("summer_break_windows", ())
                ),
                "enrollment_context_tags": tuple(
                    document["academic"].get("enrollment_context_tags", ())
                ),
            },
        }
        return cls(
            document=BiographicalLifecycleDocument.model_validate(canonical),
            timezone_name=timezone_name,
        )

    def context_at(
        self,
        logical_at: datetime,
        *,
        life_arcs: tuple[object, ...],
    ) -> BiographicalContext:
        if logical_at.tzinfo is None or logical_at.utcoffset() is None:
            raise ValueError("biographical context requires timezone-aware logical time")
        local = logical_at.astimezone(self._timezone)
        local_date = local.date()
        if self._document is None:
            return BiographicalContext(
                logical_at=logical_at,
                context_tags=(),
            )
        age = (
            local_date.year
            - self._document.birth_date.year
            - (
                (local_date.month, local_date.day)
                < (self._document.birth_date.month, self._document.birth_date.day)
            )
        )
        phase, academic_year = self._academic_reading(local_date)
        tags = set(self._document.baseline_context_tags)
        residence_tag = getattr(self._document.residence, phase)
        tags.add(
            "season:"
            + (
                "spring"
                if 3 <= local_date.month <= 5
                else "summer"
                if 6 <= local_date.month <= 8
                else "autumn"
                if 9 <= local_date.month <= 11
                else "winter"
            )
        )
        if phase == "before_enrollment":
            tags.add("academic:before_enrollment")
        elif phase == "graduated":
            tags.add("academic:graduated")
        else:
            tags.add("academic:enrolled")
            tags.update(self._document.academic.enrollment_context_tags)
            tags.add(f"calendar:{phase}")
            if phase == "term":
                tags.add("calendar:classes_open")
        active_arc_ids: list[str] = []
        active_residences: list[tuple[datetime, str, str]] = []
        for arc in life_arcs:
            if getattr(arc, "status", None) != "active":
                continue
            starts_at = getattr(arc, "started_at", None)
            ends_at = getattr(arc, "ends_at", None)
            if isinstance(starts_at, datetime) and logical_at < starts_at:
                continue
            if isinstance(ends_at, datetime) and logical_at >= ends_at:
                continue
            arc_id = str(getattr(arc, "arc_id", ""))
            if not arc_id:
                continue
            active_arc_ids.append(arc_id)
            arc_tags = tuple(
                str(item) for item in getattr(arc, "context_tags", ()) if item
            )
            residence_tags = tuple(
                item for item in arc_tags if item.startswith("residence:")
            )
            if len(residence_tags) > 1:
                raise ValueError("one active Life Arc cannot assert two residences")
            tags.update(
                item for item in arc_tags if not item.startswith("residence:")
            )
            if residence_tags:
                assert isinstance(starts_at, datetime)
                active_residences.append((starts_at, arc_id, residence_tags[0]))
        if active_residences:
            # The newest still-active residence-bearing Arc is the current
            # override. Older residence arcs remain active history and resume
            # automatically if a shorter newer stay ends.
            residence_tag = max(active_residences, key=lambda item: (item[0], item[1]))[
                2
            ]
        tags.add(residence_tag)
        return BiographicalContext(
            logical_at=logical_at,
            age=age,
            academic_phase=phase,
            academic_year=academic_year,
            context_tags=tuple(sorted(tags)),
            active_life_arc_ids=tuple(sorted(active_arc_ids)),
        )

    def _academic_reading(self, value: date) -> tuple[AcademicPhase, int | None]:
        academic = self._document.academic
        if value < academic.enrolled_on:
            return "before_enrollment", None
        if value >= academic.expected_graduation_on:
            return "graduated", None
        academic_year = value.year - academic.enrolled_on.year + (
            1 if (value.month, value.day) >= (
                academic.enrolled_on.month,
                academic.enrolled_on.day,
            ) else 0
        )
        if any(item.contains(value) for item in academic.term_windows):
            return "term", academic_year
        if any(item.contains(value) for item in academic.winter_break_windows):
            return "winter_break", academic_year
        if any(item.contains(value) for item in academic.summer_break_windows):
            return "summer_break", academic_year
        # Calendar gaps are treated as breaks, never silently as teaching time.
        return "summer_break", academic_year


__all__ = [
    "AcademicPhase",
    "BIOGRAPHICAL_LIFECYCLE_PAYLOAD_MODELS",
    "BiographicalContext",
    "BiographicalLifecycleCatalog",
    "BiographicalResidenceTimeline",
    "LifeArcChangedPayload",
    "reduce_life_arc",
]
