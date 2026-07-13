"""Shot-local character presentation for event media.

The World may provide durable visible appearance facts.  Everything else in
this module is a frozen photographic performance, not a mutation of World
state.  Candidate bundles keep pose axes coherent while leaving the planning
model a bounded, replayable choice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from companion_daemon.visual_identity import load_visual_identity


DEFAULT_SUBJECT_CONFIG = Path("configs/media_subject_templates.yaml")
_PERFORMANCE_REQUIRED_FIELDS = (
    "head_yaw",
    "head_pitch",
    "head_roll",
    "gaze_target",
    "expression",
    "shoulder_orientation",
    "posture",
    "gesture",
    "photo_awareness",
)
_OCCLUSION_RANK = {"low": 0, "medium": 1, "high": 2}


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
    hand_occupancy: str = "unspecified"
    occlusion_complexity: str = "unknown"

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "SubjectPerformance":
        if not isinstance(value, dict):
            raise ValueError("subject performance must be an object")
        return cls(
            **{name: _required_text(value, name) for name in _PERFORMANCE_REQUIRED_FIELDS},
            hand_occupancy=_optional_text(value, "hand_occupancy", "unspecified"),
            occlusion_complexity=_optional_text(value, "occlusion_complexity", "unknown"),
        )


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
    render_lexicon: dict[str, dict[str, str]]


@lru_cache(maxsize=8)
def load_subject_catalog(path: Path = DEFAULT_SUBJECT_CONFIG) -> SubjectCatalog:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    variants = raw.get("variants")
    metadata = raw.get("reference_pose_metadata", {})
    lexicon = raw.get("render_lexicon", {})
    if not isinstance(variants, list) or not isinstance(metadata, dict) or not isinstance(lexicon, dict):
        raise ValueError("invalid media subject catalog")
    return SubjectCatalog(
        variants=tuple(dict(item) for item in variants if isinstance(item, dict)),
        reference_pose_metadata={
            str(key): {str(k): str(v) for k, v in value.items()}
            for key, value in metadata.items()
            if isinstance(value, dict)
        },
        render_lexicon={
            str(field): {str(key): str(text) for key, text in values.items()}
            for field, values in lexicon.items()
            if isinstance(values, dict)
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
        derived_occlusion = _derive_occlusion_complexity(
            capture_mode, character_visibility, performance.gesture
        )
        performance = replace(
            performance,
            hand_occupancy=_derive_hand_occupancy(capture_mode, performance.gesture),
            occlusion_complexity=_max_occlusion_complexity(
                derived_occlusion, performance.occlusion_complexity
            ),
        )
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
        risk = {"low": 0, "medium": 2, "high": 6}.get(
            item.presentation.performance.occlusion_complexity, 1
        )
        return soft + risk, stable

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


def presentation_prompt_block(
    presentation: SubjectPresentationPlan,
    *,
    config_path: Path = DEFAULT_SUBJECT_CONFIG,
) -> str:
    appearance = presentation.appearance
    performance = presentation.performance
    try:
        lexicon = load_subject_catalog(config_path).render_lexicon
    except (OSError, TypeError, ValueError):
        lexicon = {}

    def render(field: str, value: str) -> str:
        return lexicon.get(field, {}).get(value, value.replace("_", " "))

    accessories = "; ".join(render("accessories", item) for item in appearance.accessories)
    accessories = accessories or "no accessory"
    return (
        "Frozen subject presentation (camera-frame directions, not the character's left/right):\n"
        f"- appearance source: {appearance.source}; hair: "
        f"{render('hair_arrangement', appearance.hair_arrangement)}; "
        f"outfit: {render('outfit_role', appearance.outfit_role)}; "
        f"grooming: {render('grooming', appearance.grooming)}; "
        f"accessories: {accessories}\n"
        f"- head and gaze: {render('head_yaw', performance.head_yaw)}; "
        f"{render('head_pitch', performance.head_pitch)}; "
        f"{render('head_roll', performance.head_roll)}; "
        f"{render('gaze_target', performance.gaze_target)}\n"
        f"- expression and body: {render('expression', performance.expression)}; "
        f"{render('shoulder_orientation', performance.shoulder_orientation)}; "
        f"{render('posture', performance.posture)}\n"
        f"- action and camera awareness: {render('gesture', performance.gesture)}; "
        f"{render('photo_awareness', performance.photo_awareness)}\n"
        f"- hand feasibility: {render('hand_occupancy', performance.hand_occupancy)}; "
        f"occlusion risk={performance.occlusion_complexity}. Keep the wrist, hand, sleeve opening, "
        "and any displayed object anatomically readable and visually distinct, with the correct "
        "contact or attachment to the described surface.\n"
        "The accessory list is exhaustive for this shot: do not add an unlisted signature hair clip "
        "or inherit accessories from an identity reference.\n"
        "Identity references and the general identity anchor define identity only. This shot-specific "
        "appearance overrides their default hairstyle tendencies. Do not copy their head angle, gaze, "
        "expression, hairstyle, gesture, or framing. Follow this frozen presentation instead."
    )


def capture_hand_feasibility_error(
    presentation: SubjectPresentationPlan,
    *,
    capture_mode: str,
    character_visibility: str,
) -> str | None:
    """Validate frozen hand responsibilities while allowing pre-extension v2 payloads."""
    performance = presentation.performance
    expected_hands = _derive_hand_occupancy(capture_mode, performance.gesture)
    if performance.hand_occupancy not in {"unspecified", expected_hands}:
        return "capture_hand_occupancy_conflict"
    expected_occlusion = _derive_occlusion_complexity(
        capture_mode, character_visibility, performance.gesture
    )
    if performance.occlusion_complexity == "unknown":
        return None
    actual_rank = _OCCLUSION_RANK.get(performance.occlusion_complexity)
    expected_rank = _OCCLUSION_RANK[expected_occlusion]
    if actual_rank is None or actual_rank < expected_rank:
        return "capture_occlusion_complexity_conflict"
    return None


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


def _derive_hand_occupancy(capture_mode: str, gesture: str) -> str:
    evidence_gestures = {
        "show_primary_evidence",
        "hold_or_point_at_primary_evidence",
        "interact_with_primary_evidence",
        "present_specific_body_detail",
        "adjust_clothing_or_accessory",
    }
    if capture_mode == "character_front_camera":
        return (
            "one_hand_operates_phone_other_presents_evidence"
            if gesture in evidence_gestures
            else "one_hand_operates_phone_other_remains_natural"
        )
    if capture_mode == "mirror":
        return "one_hand_holds_phone_other_performs_gesture"
    return "both_hands_available"


def _derive_occlusion_complexity(
    capture_mode: str, character_visibility: str, gesture: str
) -> str:
    evidence_gestures = {
        "show_primary_evidence",
        "hold_or_point_at_primary_evidence",
        "present_specific_body_detail",
        "adjust_clothing_or_accessory",
    }
    if capture_mode in {"character_front_camera", "mirror"} and (
        gesture in evidence_gestures or character_visibility == "body_detail"
    ):
        return "medium"
    return "low"


def _max_occlusion_complexity(derived: str, configured: str) -> str:
    if configured not in _OCCLUSION_RANK:
        return derived
    return max((derived, configured), key=_OCCLUSION_RANK.__getitem__)


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


def _optional_text(value: Mapping[str, object], key: str, fallback: str) -> str:
    result = value.get(key)
    return result.strip() if isinstance(result, str) and result.strip() else fallback


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}
