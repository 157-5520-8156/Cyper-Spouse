"""Shot-local character presentation for event media.

The World may provide durable visible appearance facts.  Everything else in
this module is a frozen photographic performance, not a mutation of World
state.  Candidate bundles keep pose axes coherent while leaving the planning
model a bounded, replayable choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from companion_daemon.visual_identity import load_visual_identity


DEFAULT_SUBJECT_CONFIG = Path("configs/media_subject_templates.yaml")


@dataclass(frozen=True)
class SubjectAppearance:
    source: str
    hair_arrangement: str
    outfit_role: str
    grooming: str
    accessories: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["accessories"] = list(self.accessories)
        value["evidence_refs"] = list(self.evidence_refs)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "SubjectAppearance":
        if not isinstance(value, dict):
            raise ValueError("subject appearance must be an object")
        source = str(value.get("source") or "")
        if source not in {"world_fact", "media_local"}:
            raise ValueError("invalid subject appearance source")
        return cls(
            source=source,
            hair_arrangement=_required_text(value, "hair_arrangement"),
            outfit_role=_required_text(value, "outfit_role"),
            grooming=_required_text(value, "grooming"),
            accessories=tuple(str(item) for item in value.get("accessories", [])),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs", [])),
        )


@dataclass(frozen=True)
class SubjectPerformance:
    head_yaw: str
    head_pitch: str
    head_roll: str
    gaze_target: str
    expression: str
    shoulder_orientation: str
    posture: str
    gesture: str
    photo_awareness: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "SubjectPerformance":
        if not isinstance(value, dict):
            raise ValueError("subject performance must be an object")
        return cls(**{name: _required_text(value, name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SubjectPresentationPlan:
    variant_id: str
    appearance: SubjectAppearance
    performance: SubjectPerformance
    subject_signature: str

    def to_payload(self) -> dict[str, object]:
        return {
            "variant_id": self.variant_id,
            "appearance": self.appearance.to_payload(),
            "performance": self.performance.to_payload(),
            "subject_signature": self.subject_signature,
        }

    @classmethod
    def from_payload(cls, value: object) -> "SubjectPresentationPlan":
        if not isinstance(value, dict):
            raise ValueError("subject presentation must be an object")
        presentation = cls(
            variant_id=_required_text(value, "variant_id"),
            appearance=SubjectAppearance.from_payload(value.get("appearance")),
            performance=SubjectPerformance.from_payload(value.get("performance")),
            subject_signature=_required_text(value, "subject_signature"),
        )
        if presentation.subject_signature != _subject_signature(
            presentation.appearance, presentation.performance
        ):
            raise ValueError("invalid subject signature")
        return presentation


@dataclass(frozen=True)
class SubjectCandidate:
    variant_id: str
    presentation: SubjectPresentationPlan

    def planner_payload(self) -> dict[str, object]:
        return {
            "subject_variant_id": self.variant_id,
            "appearance": self.presentation.appearance.to_payload(),
            "performance": self.presentation.performance.to_payload(),
            "subject_signature": self.presentation.subject_signature,
        }


@dataclass(frozen=True)
class SubjectCatalog:
    variants: tuple[dict[str, object], ...]
    reference_pose_metadata: dict[str, dict[str, str]]


@lru_cache(maxsize=8)
def load_subject_catalog(path: Path = DEFAULT_SUBJECT_CONFIG) -> SubjectCatalog:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    variants = raw.get("variants")
    metadata = raw.get("reference_pose_metadata", {})
    if not isinstance(variants, list) or not isinstance(metadata, dict):
        raise ValueError("invalid media subject catalog")
    return SubjectCatalog(
        variants=tuple(dict(item) for item in variants if isinstance(item, dict)),
        reference_pose_metadata={
            str(key): {str(k): str(v) for k, v in value.items()}
            for key, value in metadata.items()
            if isinstance(value, dict)
        },
    )


def build_subject_candidates(
    *,
    snapshot: Mapping[str, object],
    opportunity_id: str,
    capture_mode: str,
    character_visibility: str,
    recent_subject_signatures: Sequence[str] = (),
    config_path: Path = DEFAULT_SUBJECT_CONFIG,
    limit: int = 6,
) -> tuple[SubjectCandidate, ...]:
    """Return deterministic legal bundles for the planning model to choose."""
    catalog = load_subject_catalog(config_path)
    appearance_state = _mapping(_mapping(snapshot.get("character")).get("appearance_state"))
    world_appearance = _world_appearance(appearance_state) if appearance_state else None
    recent = {item for item in recent_subject_signatures[-12:] if item}
    recent_three = tuple(recent_subject_signatures[-3:])
    candidates: list[SubjectCandidate] = []
    for raw in catalog.variants:
        variant_id = str(raw.get("id") or "")
        allowed_capture = {str(item) for item in raw.get("capture_modes", [])}
        allowed_visibility = {str(item) for item in raw.get("character_visibilities", [])}
        if not variant_id or capture_mode not in allowed_capture or character_visibility not in allowed_visibility:
            continue
        performance = SubjectPerformance.from_payload(raw.get("performance"))
        appearance = world_appearance or _media_local_appearance(
            raw, snapshot=snapshot, stable_seed=f"{opportunity_id}:{variant_id}"
        )
        signature = _subject_signature(appearance, performance)
        if signature in recent:
            continue
        presentation = SubjectPresentationPlan(variant_id, appearance, performance, signature)
        candidates.append(SubjectCandidate(variant_id, presentation))

    def score(item: SubjectCandidate) -> tuple[int, str]:
        signature = item.presentation.subject_signature
        axes = signature.split("|")
        soft = sum(sum(axis in recent_item.split("|") for axis in axes) for recent_item in recent_three)
        stable = sha256(f"{opportunity_id}:{item.variant_id}".encode()).hexdigest()
        return soft, stable

    return tuple(sorted(candidates, key=score)[:limit])


def select_identity_references(
    *,
    identity_path: Path,
    presentation: SubjectPresentationPlan,
    subject_config_path: Path = DEFAULT_SUBJECT_CONFIG,
    profile: str = "everyday_selfie",
    relationship_tier: str | None = None,
    limit: int = 2,
) -> tuple[Path, ...]:
    """Choose identity anchors whose nuisance pose least resembles the plan."""
    identity = load_visual_identity(str(identity_path))
    catalog = load_subject_catalog(subject_config_path)
    performance = presentation.performance
    axes = {
        "head_yaw": performance.head_yaw,
        "head_pitch": performance.head_pitch,
        "head_roll": performance.head_roll,
        "gaze_target": performance.gaze_target,
        "expression": performance.expression,
    }
    scored: list[tuple[int, int, Path]] = []
    assets = (
        identity.relationship_reference_assets(relationship_tier)
        if relationship_tier
        else identity.reference_assets(profile)
    )
    for index, raw_path in enumerate(assets):
        path = Path(raw_path)
        if not path.is_file():
            continue
        metadata = catalog.reference_pose_metadata.get(raw_path, {})
        if not metadata:
            metadata = catalog.reference_pose_metadata.get(str(path), {})
        copy_score = sum(metadata.get(key) == value for key, value in axes.items())
        scored.append((copy_score, index, path))
    return tuple(item[2] for item in sorted(scored)[:limit])


def presentation_prompt_block(presentation: SubjectPresentationPlan) -> str:
    appearance = presentation.appearance
    performance = presentation.performance
    accessories = ", ".join(appearance.accessories) or "none"
    return (
        "Frozen subject presentation (camera-frame directions, not the character's left/right):\n"
        f"- appearance source: {appearance.source}; hair: {appearance.hair_arrangement}; "
        f"outfit role: {appearance.outfit_role}; grooming: {appearance.grooming}; "
        f"accessories: {accessories}\n"
        f"- head: yaw={performance.head_yaw}, pitch={performance.head_pitch}, "
        f"roll={performance.head_roll}; gaze={performance.gaze_target}; "
        f"expression={performance.expression}\n"
        f"- shoulders={performance.shoulder_orientation}; posture={performance.posture}; "
        f"gesture={performance.gesture}; photo awareness={performance.photo_awareness}\n"
        "The accessory list is exhaustive for this shot: do not add an unlisted signature hair clip "
        "or inherit accessories from an identity reference.\n"
        "Identity references define facial identity only. Do not copy their head angle, gaze, "
        "expression, hairstyle, gesture, or framing. Follow this frozen presentation instead."
    )


def _world_appearance(value: Mapping[str, object]) -> SubjectAppearance | None:
    required = ("hair_arrangement", "outfit_role", "grooming")
    if any(not isinstance(value.get(key), str) or not str(value[key]).strip() for key in required):
        return None
    return SubjectAppearance(
        source="world_fact",
        hair_arrangement=str(value["hair_arrangement"]).strip(),
        outfit_role=str(value["outfit_role"]).strip(),
        grooming=str(value["grooming"]).strip(),
        accessories=tuple(str(item) for item in value.get("accessories", []) or []),
        evidence_refs=("/character/appearance_state",),
    )


def _media_local_appearance(
    variant: Mapping[str, object], *, snapshot: Mapping[str, object], stable_seed: str
) -> SubjectAppearance:
    choices = _mapping(variant.get("appearance"))
    hair = _stable_choice(choices.get("hair_arrangements"), stable_seed + ":hair", "natural_down")
    grooming = _stable_choice(choices.get("grooming"), stable_seed + ":grooming", "natural")
    accessory_options = choices.get("accessory_options", [[]])
    accessories = _stable_choice_raw(accessory_options, stable_seed + ":accessories", [])
    return SubjectAppearance(
        source="media_local",
        hair_arrangement=str(hair),
        outfit_role=_infer_outfit_role(snapshot),
        grooming=str(grooming),
        accessories=tuple(str(item) for item in accessories if item),
    )


def _infer_outfit_role(snapshot: Mapping[str, object]) -> str:
    kind = str(_mapping(snapshot.get("activity")).get("kind") or "").lower()
    return {
        "cooking": "home_cooking",
        "eating": "event_appropriate_casual",
        "study": "campus_casual",
        "studying": "campus_casual",
        "exercise": "athletic",
        "sleep": "home_rest",
        "travel": "travel_casual",
        "walking": "outdoor_casual",
    }.get(kind, "event_appropriate_casual")


def _subject_signature(appearance: SubjectAppearance, performance: SubjectPerformance) -> str:
    return "|".join(
        (
            appearance.hair_arrangement,
            performance.head_yaw,
            performance.head_pitch,
            performance.head_roll,
            performance.gaze_target,
            performance.expression,
            performance.shoulder_orientation,
            performance.gesture,
        )
    )


def _stable_choice(value: object, seed: str, fallback: str) -> str:
    selected = _stable_choice_raw(value, seed, fallback)
    return str(selected)


def _stable_choice_raw(value: object, seed: str, fallback: Any) -> Any:
    if not isinstance(value, list) or not value:
        return fallback
    index = int(sha256(seed.encode()).hexdigest()[:8], 16) % len(value)
    return value[index]


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ValueError(f"missing {key}")
    return result.strip()


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}
