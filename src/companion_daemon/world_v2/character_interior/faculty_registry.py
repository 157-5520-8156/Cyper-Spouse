"""Frozen internal registry for Character Interior faculties."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from .author_identity import supplied_semantic_author_id


def _fallback_faculty_author_id(faculty: object) -> str:
    """Stable diagnostic identity for non-production fixture Faculties."""

    material = {
        "name": str(getattr(faculty, "name", type(faculty).__name__)),
        "version": str(
            getattr(faculty, "VERSION", getattr(faculty, "version", "unversioned"))
        ),
        "implementation": f"{type(faculty).__module__}.{type(faculty).__qualname__}",
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"unverified-character-semantic-author:sha256:{digest}"


def _faculty_author_identity(faculty: object) -> tuple[str, bool]:
    supplied = getattr(faculty, "author_identity", None)
    if callable(supplied):
        supplied = supplied()
    if isinstance(supplied, Mapping):
        semantic_author_id = supplied_semantic_author_id(supplied)
        if semantic_author_id is not None:
            return semantic_author_id, True
    return _fallback_faculty_author_id(faculty), False


class _FacultyRegistry:
    def __init__(self, *, primary: object, additional: tuple[object, ...] = ()) -> None:
        faculties: dict[str, object] = {}
        purpose_faculties: dict[str, object] = {}
        purpose_owner_counts: Counter[str] = Counter()
        purpose_semantic_author_ids: dict[str, str] = {}
        faculty_semantic_author_ids: dict[str, str] = {}
        unverified_author_faculty_names: list[str] = []
        legacy_compatibility_route_names: list[str] = []
        for faculty in (primary, *additional):
            name = str(getattr(faculty, "name", "")).strip()
            if not name:
                raise ValueError("character interior faculty requires a stable name")
            if name in faculties:
                raise ValueError(f"duplicate character interior faculty: {name}")
            faculties[name] = faculty
            semantic_author_id, is_verified = _faculty_author_identity(faculty)
            faculty_semantic_author_ids[name] = semantic_author_id
            if not is_verified:
                unverified_author_faculty_names.append(name)
            if bool(getattr(faculty, "legacy_compatibility_route", False)):
                legacy_compatibility_route_names.append(name)
            for purpose in tuple(getattr(faculty, "purposes", ())):
                normalized = str(purpose).strip()
                if not normalized:
                    raise ValueError("character interior faculty purpose is empty")
                purpose_owner_counts[normalized] += 1
                if normalized in purpose_faculties:
                    raise ValueError(
                        f"duplicate character interior purpose faculty: {normalized}"
                    )
                purpose_faculties[normalized] = faculty
                purpose_semantic_author_ids[normalized] = semantic_author_id
        self._faculties: Mapping[str, object] = MappingProxyType(faculties)
        self._purpose_faculties: Mapping[str, object] = MappingProxyType(
            purpose_faculties
        )
        self._purpose_owner_counts: Mapping[str, int] = MappingProxyType(
            dict(sorted(purpose_owner_counts.items()))
        )
        self._purpose_semantic_author_ids: Mapping[str, str] = MappingProxyType(
            dict(sorted(purpose_semantic_author_ids.items()))
        )
        self._faculty_semantic_author_ids: Mapping[str, str] = MappingProxyType(
            dict(sorted(faculty_semantic_author_ids.items()))
        )
        self._semantic_author_ids = tuple(
            sorted(set(purpose_semantic_author_ids.values()))
        )
        self._unverified_author_faculty_names = tuple(
            sorted(unverified_author_faculty_names)
        )
        self._legacy_compatibility_route_names = tuple(
            sorted(legacy_compatibility_route_names)
        )
        self._primary_name = str(getattr(primary, "name"))

    @property
    def primary(self) -> object:
        return self._faculties[self._primary_name]

    @property
    def primary_name(self) -> str:
        return self._primary_name

    @property
    def purpose_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._purpose_faculties))

    @property
    def faculty_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._faculties))

    @property
    def purpose_owner_counts(self) -> Mapping[str, int]:
        return self._purpose_owner_counts

    @property
    def duplicate_purpose_owner_count(self) -> int:
        return sum(max(0, count - 1) for count in self._purpose_owner_counts.values())

    @property
    def legacy_compatibility_route_names(self) -> tuple[str, ...]:
        return self._legacy_compatibility_route_names

    @property
    def semantic_author_ids(self) -> tuple[str, ...]:
        return self._semantic_author_ids

    @property
    def semantic_author_count(self) -> int:
        return len(self._semantic_author_ids)

    @property
    def purpose_semantic_author_ids(self) -> Mapping[str, str]:
        return self._purpose_semantic_author_ids

    @property
    def faculty_semantic_author_ids(self) -> Mapping[str, str]:
        return self._faculty_semantic_author_ids

    @property
    def unverified_author_faculty_names(self) -> tuple[str, ...]:
        return self._unverified_author_faculty_names

    def get(self, name: str) -> object | None:
        return self._faculties.get(name)

    def for_purpose(self, purpose: str) -> object:
        faculty = self._purpose_faculties.get(purpose)
        if faculty is None:
            raise ValueError(f"unregistered character interior purpose: {purpose}")
        return faculty


__all__: list[str] = []
