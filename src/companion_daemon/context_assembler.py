"""Bounded, provenance-preserving context selection for world conversations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Iterable, Mapping, Sequence


LAYER_NAMES: Final[tuple[str, ...]] = (
    "character_core",
    "user_profile",
    "current_scene",
    "retrieved_experiences",
    "expression_guidance",
)
_CURRENT_STATUSES: Final[frozenset[str]] = frozenset(
    {"", "active", "committed", "confirmed", "current", "delivered", "observed"}
)


@dataclass(frozen=True, slots=True)
class LayerBudget:
    max_chars: int
    max_items: int

    def __post_init__(self) -> None:
        if self.max_chars <= 0 or self.max_items <= 0:
            raise ValueError("context layer budgets must be positive")


@dataclass(frozen=True, slots=True)
class ContextBudgets:
    character_core: LayerBudget = LayerBudget(max_chars=3600, max_items=40)
    user_profile: LayerBudget = LayerBudget(max_chars=1600, max_items=8)
    current_scene: LayerBudget = LayerBudget(max_chars=1200, max_items=8)
    retrieved_experiences: LayerBudget = LayerBudget(max_chars=2400, max_items=8)
    expression_guidance: LayerBudget = LayerBudget(max_chars=1200, max_items=8)


@dataclass(frozen=True, slots=True)
class ContextEntry:
    content: str
    source_id: str
    source: str
    source_type: str
    subject: str
    logical_at: str
    purpose: str
    pinned: bool = False
    importance: int = 0
    status: str = "current"
    conflict_key: str = ""

    def __post_init__(self) -> None:
        required = {
            "content": self.content,
            "source_id": self.source_id,
            "source": self.source,
            "source_type": self.source_type,
            "subject": self.subject,
            "purpose": self.purpose,
        }
        if any(not value.strip() for value in required.values()):
            raise ValueError("context entries require content and provenance")


class ContextAssembler:
    """Select five bounded context layers without losing provenance."""

    def __init__(self, budgets: ContextBudgets | None = None) -> None:
        self.budgets = budgets or ContextBudgets()

    def assemble(
        self,
        *,
        character_core: Iterable[ContextEntry],
        user_profile: Iterable[ContextEntry],
        current_scene: Iterable[ContextEntry],
        retrieved_experiences: Iterable[ContextEntry],
        expression_guidance: Iterable[ContextEntry],
        rotation_key: str = "",
    ) -> dict[str, dict[str, object]]:
        candidates = {
            "character_core": character_core,
            "user_profile": user_profile,
            "current_scene": current_scene,
            "retrieved_experiences": retrieved_experiences,
            "expression_guidance": expression_guidance,
        }
        return {
            name: self._select(
                candidates[name],
                getattr(self.budgets, name),
                rotation_key=f"{rotation_key}:{name}",
            )
            for name in LAYER_NAMES
        }

    def assemble_world_context(
        self,
        context: Mapping[str, object],
        *,
        user_id: str,
        retrieved_experiences: Sequence[Mapping[str, object]],
        expression_guidance: Mapping[str, object],
        rotation_key: str,
    ) -> dict[str, dict[str, object]]:
        """Adapt the world read model into the sole prompt-facing five layers."""
        scene = _mapping(context.get("current_scene"))
        logical_at = _text(scene.get("logical_at"))
        self_core = _mapping(context.get("self_core"))
        character_source_id = _text(self_core.get("source_id")) or (
            f"character-core:{_text(self_core.get('entity_id')) or 'zhizhi'}"
        )
        character_source = _text(self_core.get("source")) or "world_projection"
        character_time = _text(self_core.get("logical_at")) or logical_at
        character_subject = _text(self_core.get("entity_id")) or "zhizhi"
        character_core: list[ContextEntry] = []

        def add_character(content: str, *, purpose: str, pinned: bool) -> None:
            if not content.strip():
                return
            character_core.append(
                ContextEntry(
                    content=content.strip(),
                    source_id=f"{character_source_id}:{len(character_core)}",
                    source=character_source,
                    source_type="character_core",
                    subject=character_subject,
                    logical_at=character_time,
                    purpose=purpose,
                    pinned=pinned,
                    importance=100 if pinned else 50,
                )
            )

        name = _text(self_core.get("name"))
        if name:
            add_character(f"name: {name}", purpose="identity", pinned=True)
        for field, purpose in (
            ("stable_traits", "identity"),
            ("values", "values"),
            ("preferences", "preferences"),
            ("relationship_principles", "relationship_boundary"),
            ("speech_anchors", "style"),
            ("boundaries", "boundary"),
        ):
            for value in _sequence(self_core.get(field)):
                add_character(_text(value), purpose=purpose, pinned=True)
        continuity = _mapping(self_core.get("continuity"))
        for field in ("active_goals", "completed_goals"):
            for value in _sequence(continuity.get(field)):
                add_character(_text(value), purpose="continuity", pinned=False)
        relationship = _mapping(continuity.get("user_relationship"))
        if relationship:
            add_character(
                "relationship: "
                + json.dumps(relationship, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                purpose="relationship_continuity",
                pinned=False,
            )

        user_profile = [
            ContextEntry(
                content=_text(item.get("value")),
                source_id=_required_text(item, "source_id"),
                source=_text(item.get("source")) or _required_text(item, "source_id"),
                source_type=_text(item.get("source_type")) or "fact",
                subject=_text(item.get("subject")) or user_id,
                logical_at=_text(item.get("logical_at")),
                purpose="personalize",
                pinned=bool(item.get("pinned", False)),
                importance=int(item.get("importance") or 50),
                status=_text(item.get("reference_state")) or _text(item.get("status")),
                conflict_key=_text(item.get("conflict_key")),
            )
            for raw in _sequence(context.get("user_profile"))
            if (item := _mapping(raw)) and _text(item.get("value"))
        ]

        scene_source = _mapping(context.get("current_scene_source"))
        current_scene = [
            ContextEntry(
                content=_required_text(scene_source, "content"),
                source_id=_required_text(scene_source, "source_id"),
                source=_text(scene_source.get("source")) or "world_projection",
                source_type=_text(scene_source.get("source_type")) or "current_scene",
                subject=_text(scene_source.get("subject")) or character_subject,
                logical_at=_text(scene_source.get("logical_at")) or logical_at,
                purpose="current_state",
                pinned=True,
                importance=100,
                status=_text(scene_source.get("reference_state")) or "current",
            )
        ]

        experiences = [
            ContextEntry(
                content=_required_text(item, "content"),
                source_id=_required_text(item, "source_id"),
                source=_text(item.get("source")) or _required_text(item, "source_id"),
                source_type=_text(item.get("source_type")) or "experience",
                subject=_text(item.get("subject")) or character_subject,
                logical_at=_text(item.get("occurred_at")) or _text(item.get("logical_at")),
                purpose=_text(item.get("purpose")) or "continuity",
                pinned=bool(item.get("pinned", False)),
                importance=int(item.get("importance") or 50),
                status=_text(item.get("reference_state")) or _text(item.get("status")),
                conflict_key=_text(item.get("conflict_key")),
            )
            for item in retrieved_experiences
            if _text(item.get("content"))
        ]

        label = _text(expression_guidance.get("label")) or "neutral"
        prompt_line = _required_text(expression_guidance, "prompt_line")
        rule_version = _text(expression_guidance.get("rule_version")) or "unversioned"
        guidance = [
            ContextEntry(
                content=f"{label}：{prompt_line}",
                source_id=f"expression-guidance:{rule_version}:{label}",
                source=f"world_behavior_policy:{rule_version}",
                source_type="expression_guidance",
                subject=character_subject,
                logical_at=logical_at,
                purpose="expression",
                pinned=True,
                importance=100,
            )
        ]
        return self.assemble(
            character_core=character_core,
            user_profile=user_profile,
            current_scene=current_scene,
            retrieved_experiences=experiences,
            expression_guidance=guidance,
            rotation_key=rotation_key,
        )

    @staticmethod
    def _select(
        candidates: Iterable[ContextEntry],
        budget: LayerBudget,
        *,
        rotation_key: str,
    ) -> dict[str, object]:
        entries: list[dict[str, object]] = []
        used_chars = 0
        curated = ContextAssembler._curate(candidates)
        pinned = sorted(
            (item for item in curated if item.pinned),
            key=lambda item: (item.importance, item.logical_at, item.source_id),
            reverse=True,
        )
        rotating: list[ContextEntry] = []
        unpinned = [item for item in curated if not item.pinned]
        for importance in sorted({item.importance for item in unpinned}, reverse=True):
            priority_group = sorted(
                (item for item in unpinned if item.importance == importance),
                key=lambda item: (item.logical_at, item.source_id),
                reverse=True,
            )
            digest = sha256(rotation_key.encode("utf-8")).digest()
            offset = int.from_bytes(digest[:8], "big") % len(priority_group)
            rotating.extend(priority_group[offset:] + priority_group[:offset])
        for candidate in [*pinned, *rotating]:
            if len(entries) >= budget.max_items:
                break
            content_chars = len(candidate.content)
            if used_chars + content_chars > budget.max_chars:
                continue
            entries.append(
                {
                    "source_id": candidate.source_id,
                    "source": candidate.source,
                    "source_type": candidate.source_type,
                    "subject": candidate.subject,
                    "logical_at": candidate.logical_at,
                    "purpose": candidate.purpose,
                    "selection": "pinned" if candidate.pinned else "rotating",
                    "content": candidate.content,
                }
            )
            used_chars += content_chars
        return {
            "max_chars": budget.max_chars,
            "max_items": budget.max_items,
            "used_chars": used_chars,
            "entries": entries,
        }

    @staticmethod
    def _curate(candidates: Iterable[ContextEntry]) -> list[ContextEntry]:
        current = [item for item in candidates if item.status in _CURRENT_STATUSES]
        latest_for_conflict: dict[tuple[str, str], ContextEntry] = {}
        for item in current:
            if not item.conflict_key:
                continue
            key = (item.subject, item.conflict_key)
            prior = latest_for_conflict.get(key)
            if prior is None or (item.logical_at, item.source_id) > (
                prior.logical_at,
                prior.source_id,
            ):
                latest_for_conflict[key] = item
        selected_conflicts = {item.source_id for item in latest_for_conflict.values()}
        seen: set[str] = set()
        curated: list[ContextEntry] = []
        for item in current:
            if item.source_id in seen:
                continue
            if item.conflict_key and item.source_id not in selected_conflicts:
                continue
            seen.add(item.source_id)
            curated.append(item)
        return curated


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else ()


def _text(value: object) -> str:
    return str(value or "").strip()


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = _text(value.get(key))
    if not result:
        raise ValueError(f"world context entry requires {key}")
    return result
