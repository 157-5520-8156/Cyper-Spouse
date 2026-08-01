"""Exact claim capabilities derived from one pinned biographical reading.

The model-facing ``biographical_context`` item is intentionally rich: it lets
the character notice age, academic phase, season, residence and active Life
Arcs together.  That whole item must not also be a bearer token for an
arbitrary current or past occurrence.  This module derives narrow,
content-addressed claim capabilities for the coordinates that the item
actually contains.

These capabilities do not decide what the character says.  They only make the
epistemic boundary executable: a visible or durable biographical assertion can
cite an exact coordinate, while the parent item remains attention context.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, NamedTuple


BiographicalClaimScope = Literal["current_world"]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class BiographicalCoordinateAuthority(NamedTuple):
    """One exact coordinate and the only WorldClaim lane it can enter."""

    source_ref: str
    parent_item_ref: str
    scope: BiographicalClaimScope
    field_path: str
    logical_at: str
    value: object

    def evidence_material(self) -> dict[str, object]:
        material: dict[str, object] = {
            "contract": "biographical-coordinate-authority.1",
            "parent_item_ref": self.parent_item_ref,
            "claim_scope": self.scope,
            "field_path": self.field_path,
            "value": self.value,
        }
        material["logical_at"] = self.logical_at
        return material


_PINNED_COORDINATES = (
    "age",
    "academic_phase",
    "academic_year",
    "season",
    "calendar_context_tags",
    "current_residence_context_tags",
)
_ACTIVE_ARC_SEMANTIC_FIELDS = (
    "arc_id",
    "arc_kind",
    "context_pack_ref",
    "context_tags",
    "started_at",
    "ends_at",
    "context_summary",
)


def _coordinate(
    *,
    parent_item_ref: str,
    scope: BiographicalClaimScope,
    field_path: str,
    logical_at: str,
    value: object,
) -> BiographicalCoordinateAuthority:
    identity = {
        "contract": "biographical-coordinate-authority.1",
        "parent_item_ref": parent_item_ref,
        "claim_scope": scope,
        "field_path": field_path,
        "logical_at": logical_at,
        "value": value,
    }
    return BiographicalCoordinateAuthority(
        source_ref=f"biography-coordinate:sha256:{_digest(identity)}",
        parent_item_ref=parent_item_ref,
        scope=scope,
        field_path=field_path,
        logical_at=logical_at,
        value=value,
    )


def _biographical_items(context: dict[str, object]) -> tuple[dict[str, object], ...]:
    slices = context.get("slices")
    world_life = slices.get("world_life") if isinstance(slices, dict) else None
    if (
        not isinstance(world_life, dict)
        or world_life.get("availability") != "available"
    ):
        return ()
    items = world_life.get("items")
    if not isinstance(items, list):
        return ()
    return tuple(
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("value"), dict)
        and item["value"].get("context_kind") == "biographical_context"
    )


def biographical_parent_attention_refs(context: dict[str, object]) -> frozenset[str]:
    """Return broad biography item identities that may be noticed, never cited."""

    refs: set[str] = set()
    for item in _biographical_items(context):
        for field in ("item_ref", "source_ref"):
            value = item.get(field)
            if isinstance(value, str) and value:
                refs.add(value)
    return frozenset(refs)


def biographical_coordinate_authorities(
    context: dict[str, object],
) -> tuple[BiographicalCoordinateAuthority, ...]:
    """Derive deterministic field-level claim capabilities from visible Context."""

    authorities: list[BiographicalCoordinateAuthority] = []
    context_logical_at = context.get("logical_time")
    for item in _biographical_items(context):
        value = item["value"]
        if not isinstance(value, dict):  # narrowed by ``_biographical_items``
            continue
        parent_item_ref = item.get("item_ref") or item.get("source_ref")
        if not isinstance(parent_item_ref, str) or not parent_item_ref:
            continue
        logical_at_value = value.get("logical_at", context_logical_at)
        logical_at = logical_at_value if isinstance(logical_at_value, str) else None
        if logical_at is None:
            # Every coordinate is a reading at pinned Logical Time.  Without
            # that anchor there is no current-world capability to grant.
            continue
        # Every field here is a reading *at the pinned logical time*.  Age and
        # academic phase evolve just as season and residence do; immutable
        # identity belongs to character_core, not to this clock-derived view.
        for field in _PINNED_COORDINATES:
            coordinate_value = value.get(field)
            if coordinate_value is None or coordinate_value in ([], ()):
                continue
            authorities.append(
                _coordinate(
                    parent_item_ref=parent_item_ref,
                    scope="current_world",
                    field_path=f"/{field}",
                    logical_at=logical_at,
                    value=coordinate_value,
                )
            )
        active_life_arcs = value.get("active_life_arcs")
        if not isinstance(active_life_arcs, list):
            continue
        for arc in active_life_arcs:
            if not isinstance(arc, dict):
                continue
            arc_id = arc.get("arc_id")
            if not isinstance(arc_id, str) or not arc_id:
                continue
            semantic_arc = {
                field: arc[field]
                for field in _ACTIVE_ARC_SEMANTIC_FIELDS
                if field in arc and arc[field] is not None
            }
            authorities.append(
                _coordinate(
                    parent_item_ref=parent_item_ref,
                    scope="current_world",
                    field_path=f"/active_life_arcs/{arc_id}",
                    logical_at=logical_at,
                    value=semantic_arc,
                )
            )
    return tuple(
        sorted(
            authorities,
            key=lambda item: (
                item.parent_item_ref,
                item.scope,
                item.field_path,
                item.source_ref,
            ),
        )
    )


__all__ = [
    "BiographicalClaimScope",
    "BiographicalCoordinateAuthority",
    "biographical_coordinate_authorities",
    "biographical_parent_attention_refs",
]
