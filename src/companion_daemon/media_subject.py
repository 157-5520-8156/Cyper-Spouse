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
import json
from math import exp, log
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from companion_daemon.media_domain import PRIVACY_RANK
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
SUBJECT_PRESENTATION_V2 = "subject-presentation-v2"
SUBJECT_PRESENTATION_V3 = "subject-presentation-v3"

EXPRESSION_FAMILIES = {
    "neutral_present", "warm", "amused", "playfully_performed", "proud",
    "frustrated", "embarrassed", "tired", "vulnerable", "tender",
    "desire_direct", "desire_withheld",
}
MOUTH_BEHAVIORS = {
    "relaxed_closed", "relaxed_parted", "small_smile", "asymmetric_half_smile",
    "suppressed_laugh", "open_laugh", "subtle_pout", "lightly_pressed",
    "mid_speech", "not_visible",
}
EYE_BEHAVIORS = {
    "steady_lens", "soft_lens", "screen_check", "evidence_focus", "look_away",
    "glance_back", "companion_focus", "relaxed_heavy_lidded", "not_visible",
}
BROW_BEHAVIORS = {
    "neutral", "slight_lift", "single_brow_energy", "faint_inward_draw",
    "relaxed_lowered", "not_visible",
}
GAZE_SEQUENCES = {
    "continuous_lens", "evidence_then_lens", "lens_then_away", "away_then_back",
    "screen_then_lens", "companion_then_camera", "no_face",
}
FACIAL_ENERGIES = {"low", "contained", "lively", "breathless", "held", "recovering"}


@dataclass(frozen=True)
class FacialPerformance:
    expression_family: str
    mouth_behavior: str
    eye_behavior: str
    brow_behavior: str
    gaze_sequence: str
    facial_energy: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "FacialPerformance":
        if not isinstance(value, dict):
            raise ValueError("facial performance must be an object")
        result = cls(**{name: _required_text(value, name) for name in (
            "expression_family", "mouth_behavior", "eye_behavior", "brow_behavior",
            "gaze_sequence", "facial_energy",
        )})
        enums = (
            (result.expression_family, EXPRESSION_FAMILIES),
            (result.mouth_behavior, MOUTH_BEHAVIORS),
            (result.eye_behavior, EYE_BEHAVIORS),
            (result.brow_behavior, BROW_BEHAVIORS),
            (result.gaze_sequence, GAZE_SEQUENCES),
            (result.facial_energy, FACIAL_ENERGIES),
        )
        if any(item not in allowed for item, allowed in enums):
            raise ValueError("invalid facial performance enum")
        return result


@dataclass(frozen=True)
class PhotoDisplayStrategy:
    """A shot-local social performance, expanded so replay never rereads recipes."""

    strategy_id: str
    communicative_goals: tuple[str, ...]
    intentionality: str
    intensity: str
    holistic_cue: str
    mouth: str
    eyes: str
    brows: str
    gaze_quality: str
    facial_tension: str
    temporal_beat: str
    forbidden_cues: tuple[str, ...] = ()
    tone_affinities: tuple[str, ...] = ()
    minimum_privacy: str = "ordinary"
    requires_relationship: bool = False
    exclude_when_severe_affect: bool = False

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["communicative_goals"] = list(self.communicative_goals)
        value["forbidden_cues"] = list(self.forbidden_cues)
        value["tone_affinities"] = list(self.tone_affinities)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "PhotoDisplayStrategy":
        if not isinstance(value, dict):
            raise ValueError("photo display strategy must be an object")
        return cls(
            strategy_id=_required_text(value, "strategy_id"),
            communicative_goals=tuple(
                str(item) for item in value.get("communicative_goals", []) if str(item)
            ),
            intentionality=_required_text(value, "intentionality"),
            intensity=_required_text(value, "intensity"),
            holistic_cue=_required_text(value, "holistic_cue"),
            mouth=_required_text(value, "mouth"),
            eyes=_required_text(value, "eyes"),
            brows=_required_text(value, "brows"),
            gaze_quality=_required_text(value, "gaze_quality"),
            facial_tension=_required_text(value, "facial_tension"),
            temporal_beat=_required_text(value, "temporal_beat"),
            forbidden_cues=tuple(str(item) for item in value.get("forbidden_cues", [])),
            tone_affinities=tuple(str(item) for item in value.get("tone_affinities", [])),
            minimum_privacy=_optional_text(value, "minimum_privacy", "ordinary"),
            requires_relationship=bool(value.get("requires_relationship", False)),
            exclude_when_severe_affect=bool(value.get("exclude_when_severe_affect", False)),
        )


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
    version: str = "subject-presentation-v1"
    display_strategy: PhotoDisplayStrategy | None = None
    facial_performance: FacialPerformance | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "variant_id": self.variant_id,
            "appearance": self.appearance.to_payload(),
            "performance": self.performance.to_payload(),
            "subject_signature": self.subject_signature,
        }
        if self.version == SUBJECT_PRESENTATION_V2:
            payload["version"] = self.version
            payload["display_strategy"] = (
                self.display_strategy.to_payload() if self.display_strategy else None
            )
        elif self.version == SUBJECT_PRESENTATION_V3:
            payload["version"] = self.version
            payload["performance"].pop("expression", None)  # pose does not own the face in v3
            payload["performance"].pop("gaze_target", None)
            payload["display_strategy"] = (
                self.display_strategy.to_payload() if self.display_strategy else None
            )
            payload["facial_performance"] = (
                self.facial_performance.to_payload() if self.facial_performance else None
            )
        return payload

    @classmethod
    def create_v2(
        cls,
        *,
        variant_id: str,
        appearance: SubjectAppearance,
        performance: SubjectPerformance,
        display_strategy: PhotoDisplayStrategy,
    ) -> "SubjectPresentationPlan":
        return cls(
            variant_id=variant_id,
            appearance=appearance,
            performance=performance,
            subject_signature=_subject_signature(appearance, performance, display_strategy),
            version=SUBJECT_PRESENTATION_V2,
            display_strategy=display_strategy,
        )

    @classmethod
    def from_payload(cls, value: object) -> "SubjectPresentationPlan":
        if not isinstance(value, dict):
            raise ValueError("subject presentation must be an object")
        version = str(value.get("version") or "subject-presentation-v1")
        display_strategy = (
            PhotoDisplayStrategy.from_payload(value.get("display_strategy"))
            if version in {SUBJECT_PRESENTATION_V2, SUBJECT_PRESENTATION_V3}
            else None
        )
        performance_value = value.get("performance")
        if version == SUBJECT_PRESENTATION_V3 and isinstance(performance_value, dict):
            performance_value = {
                **performance_value,
                "expression": "facial_performance_v3",
                "gaze_target": "facial_performance_v3",
            }
        facial_performance = (
            FacialPerformance.from_payload(value.get("facial_performance"))
            if version == SUBJECT_PRESENTATION_V3
            else None
        )
        presentation = cls(
            variant_id=_required_text(value, "variant_id"),
            appearance=SubjectAppearance.from_payload(value.get("appearance")),
            performance=SubjectPerformance.from_payload(performance_value),
            subject_signature=_required_text(value, "subject_signature"),
            version=version,
            display_strategy=display_strategy,
            facial_performance=facial_performance,
        )
        if version not in {"subject-presentation-v1", SUBJECT_PRESENTATION_V2, SUBJECT_PRESENTATION_V3}:
            raise ValueError("unsupported subject presentation version")
        if version in {SUBJECT_PRESENTATION_V2, SUBJECT_PRESENTATION_V3} and display_strategy is None:
            raise ValueError("missing photo display strategy")
        if version == SUBJECT_PRESENTATION_V3 and facial_performance is None:
            raise ValueError("missing facial performance")
        if presentation.subject_signature != _subject_signature(
            presentation.appearance, presentation.performance, presentation.display_strategy,
            presentation.facial_performance,
        ):
            raise ValueError("invalid subject signature")
        return presentation


def upgrade_subject_presentation_v3(
    presentation: SubjectPresentationPlan,
) -> SubjectPresentationPlan:
    """Split a coherent v2 display recipe into pose and facial contracts."""

    if presentation.version == SUBJECT_PRESENTATION_V3:
        return presentation
    if presentation.version != SUBJECT_PRESENTATION_V2 or presentation.display_strategy is None:
        raise ValueError("subject v3 requires a v2 display strategy")
    facial = _facial_performance(presentation.display_strategy)
    performance = replace(
        presentation.performance,
        expression="facial_performance_v3",
        gaze_target="facial_performance_v3",
    )
    return SubjectPresentationPlan(
        variant_id=presentation.variant_id,
        appearance=presentation.appearance,
        performance=performance,
        subject_signature=_subject_signature(
            presentation.appearance, performance, presentation.display_strategy, facial
        ),
        version=SUBJECT_PRESENTATION_V3,
        display_strategy=presentation.display_strategy,
        facial_performance=facial,
    )


def adapt_subject_for_attraction_mechanism_v3(
    presentation: SubjectPresentationPlan,
    mechanism: str,
) -> SubjectPresentationPlan:
    """Bind attraction intent to a complete face performance, not loose facial axes."""

    if presentation.version != SUBJECT_PRESENTATION_V3 or presentation.facial_performance is None:
        raise ValueError("attraction adaptation requires subject presentation v3")
    recipes: dict[str, FacialPerformance] = {
        "direct_invitation": FacialPerformance("desire_direct", "relaxed_parted", "steady_lens", "neutral", "continuous_lens", "held"),
        "playful_tease": FacialPerformance("playfully_performed", "subtle_pout", "steady_lens", "single_brow_energy", "away_then_back", "lively"),
        "withheld_attention": FacialPerformance("desire_withheld", "relaxed_closed", "glance_back", "relaxed_lowered", "away_then_back", "held"),
        "sensory_immediacy": FacialPerformance("desire_direct", "relaxed_parted", "soft_lens", "neutral", "lens_then_away", "breathless"),
        "private_trust": FacialPerformance("tender", "relaxed_parted", "relaxed_heavy_lidded", "relaxed_lowered", "continuous_lens", "low"),
        "confident_display": FacialPerformance("desire_direct", "asymmetric_half_smile", "steady_lens", "single_brow_energy", "continuous_lens", "contained"),
        "interrupted_transition": FacialPerformance("desire_withheld", "relaxed_parted", "evidence_focus", "slight_lift", "evidence_then_lens", "recovering"),
        "close_proximity": FacialPerformance("tender", "relaxed_parted", "soft_lens", "neutral", "continuous_lens", "held"),
        "atmospheric_suggestion": FacialPerformance("desire_withheld", "relaxed_closed", "look_away", "relaxed_lowered", "lens_then_away", "low"),
    }
    facial = recipes.get(mechanism)
    if facial is None:
        raise ValueError("unknown attraction mechanism")
    return replace(
        presentation,
        facial_performance=facial,
        subject_signature=_subject_signature(
            presentation.appearance,
            presentation.performance,
            presentation.display_strategy,
            facial,
        ),
    )


def adapt_subject_for_media_address_v3(
    presentation: SubjectPresentationPlan,
    *,
    engagement_tactic: str,
    attraction_mechanism: str | None = None,
) -> SubjectPresentationPlan:
    """Give every whole-image tactic a coherent face without reusing v2 goal bindings."""

    if attraction_mechanism:
        return adapt_subject_for_attraction_mechanism_v3(presentation, attraction_mechanism)
    recipes = {
        "presence": FacialPerformance("warm", "relaxed_closed", "soft_lens", "neutral", "continuous_lens", "contained"),
        "reveal": FacialPerformance("proud", "small_smile", "evidence_focus", "slight_lift", "evidence_then_lens", "contained"),
        "demonstration": FacialPerformance("neutral_present", "relaxed_closed", "evidence_focus", "neutral", "evidence_then_lens", "contained"),
        "question": FacialPerformance("neutral_present", "relaxed_parted", "steady_lens", "slight_lift", "continuous_lens", "held"),
        "comparison": FacialPerformance("neutral_present", "lightly_pressed", "evidence_focus", "slight_lift", "evidence_then_lens", "contained"),
        "contrast": FacialPerformance("amused", "asymmetric_half_smile", "glance_back", "single_brow_energy", "away_then_back", "lively"),
        "comic_hook": FacialPerformance("playfully_performed", "subtle_pout", "steady_lens", "slight_lift", "continuous_lens", "lively"),
        "celebration": FacialPerformance("proud", "open_laugh", "steady_lens", "slight_lift", "continuous_lens", "lively"),
        "vulnerability": FacialPerformance("vulnerable", "relaxed_parted", "soft_lens", "faint_inward_draw", "lens_then_away", "low"),
        "reassurance": FacialPerformance("tender", "small_smile", "soft_lens", "neutral", "continuous_lens", "contained"),
        "coordination": FacialPerformance("neutral_present", "mid_speech", "evidence_focus", "slight_lift", "evidence_then_lens", "contained"),
        "affection": FacialPerformance("tender", "relaxed_parted", "soft_lens", "relaxed_lowered", "continuous_lens", "held"),
        "nostalgia": FacialPerformance("tender", "small_smile", "look_away", "relaxed_lowered", "lens_then_away", "low"),
    }
    facial = recipes.get(engagement_tactic)
    if presentation.version != SUBJECT_PRESENTATION_V3 or facial is None:
        raise ValueError("unsupported media address facial performance")
    return replace(
        presentation,
        facial_performance=facial,
        subject_signature=_subject_signature(
            presentation.appearance,
            presentation.performance,
            presentation.display_strategy,
            facial,
        ),
    )


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
            "display_strategy": (
                self.presentation.display_strategy.to_payload()
                if self.presentation.display_strategy
                else None
            ),
        }


@dataclass(frozen=True)
class SubjectCatalog:
    variants: tuple[dict[str, object], ...]
    reference_pose_metadata: dict[str, dict[str, str]]
    render_lexicon: dict[str, dict[str, str]]
    display_strategies: dict[str, PhotoDisplayStrategy]
    bindings: dict[str, tuple[str, ...]]


@lru_cache(maxsize=8)
def load_subject_catalog(path: Path = DEFAULT_SUBJECT_CONFIG) -> SubjectCatalog:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    variants = raw.get("variants")
    metadata = raw.get("reference_pose_metadata", {})
    lexicon = raw.get("render_lexicon", {})
    strategies = raw.get("display_strategies", {})
    bindings = raw.get("display_strategy_bindings", {})
    if not all(
        (
            isinstance(variants, list),
            isinstance(metadata, dict),
            isinstance(lexicon, dict),
            isinstance(strategies, dict),
            isinstance(bindings, dict),
        )
    ):
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
        display_strategies={
            str(key): PhotoDisplayStrategy.from_payload({"strategy_id": str(key), **value})
            for key, value in strategies.items()
            if isinstance(value, dict)
        },
        bindings={
            str(key): tuple(str(item) for item in value)
            for key, value in bindings.items()
            if isinstance(value, list)
        },
    )


def build_subject_candidates(
    *,
    snapshot: Mapping[str, object],
    opportunity_id: str,
    capture_mode: str,
    character_visibility: str,
    recent_subject_signatures: Sequence[str] = (),
    privacy_ceiling: str = "personal",
    relationship_stage: str = "",
    public_affect: Mapping[str, object] | None = None,
    display_bounds: Sequence[str] = (),
    config_path: Path = DEFAULT_SUBJECT_CONFIG,
    limit: int = 6,
) -> tuple[SubjectCandidate, ...]:
    """Return deterministic legal bundles for the planning model to choose."""
    catalog = load_subject_catalog(config_path)
    appearance_state = _mapping(_mapping(snapshot.get("character")).get("appearance_state"))
    world_appearance = _world_appearance(appearance_state) if appearance_state else None
    recent = {item for item in recent_subject_signatures[-12:] if item}
    recent_three = tuple(recent_subject_signatures[-3:])
    if privacy_ceiling not in PRIVACY_RANK:
        raise ValueError("invalid subject privacy ceiling")
    bounded_strategies = {str(item) for item in display_bounds if str(item)}
    severe_affect = _is_severe_public_affect(public_affect)
    affect_labels = _public_affect_labels(snapshot, public_affect)
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
        strategy_ids = catalog.bindings.get(variant_id, ())
        if not strategy_ids:
            signature = _subject_signature(appearance, performance)
            if signature not in recent:
                presentation = SubjectPresentationPlan(
                    variant_id, appearance, performance, signature
                )
                candidates.append(SubjectCandidate(variant_id, presentation))
            continue
        for index, strategy_id in enumerate(strategy_ids):
            strategy = catalog.display_strategies.get(strategy_id)
            if strategy is None:
                raise ValueError(f"unknown display strategy binding: {strategy_id}")
            if strategy.minimum_privacy not in PRIVACY_RANK:
                raise ValueError(f"invalid display strategy privacy: {strategy_id}")
            if PRIVACY_RANK[strategy.minimum_privacy] > PRIVACY_RANK[privacy_ceiling]:
                continue
            if strategy.requires_relationship and not relationship_stage:
                continue
            if severe_affect and strategy.exclude_when_severe_affect:
                continue
            if bounded_strategies and strategy_id not in bounded_strategies:
                continue
            if character_visibility == "body_detail":
                strategy = replace(
                    strategy,
                    mouth="not_applicable",
                    eyes="not_applicable",
                    brows="not_applicable",
                    gaze_quality="not_applicable",
                    facial_tension="not_applicable",
                    temporal_beat="detail_is_made_legible_through_framing_and_gesture",
                    forbidden_cues=(),
                )
            strategy_performance = replace(performance, expression=strategy.strategy_id)
            candidate_id = variant_id if index == 0 else f"{variant_id}__{strategy_id}"
            presentation = SubjectPresentationPlan.create_v2(
                variant_id=candidate_id,
                appearance=appearance,
                performance=strategy_performance,
                display_strategy=strategy,
            )
            if presentation.subject_signature not in recent:
                candidates.append(SubjectCandidate(candidate_id, presentation))

    def weighted_key(item: SubjectCandidate) -> tuple[float, int, str]:
        signature = item.presentation.subject_signature
        axes = signature.split("|")
        soft = sum(sum(axis in recent_item.split("|") for axis in axes) for recent_item in recent_three)
        stable = sha256(f"{opportunity_id}:{item.variant_id}".encode()).hexdigest()
        risk = {"low": 0, "medium": 2, "high": 6}.get(
            item.presentation.performance.occlusion_complexity, 1
        )
        strategy = item.presentation.display_strategy
        affinity = 0
        if strategy and affect_labels.intersection(strategy.tone_affinities):
            affinity += 2
        if strategy and relationship_stage and "invite_closeness" in strategy.communicative_goals:
            affinity += 1
        penalty = soft + risk - affinity
        composite = 1 if "__" in item.variant_id else 0
        weight = exp(-0.7 * penalty) * (1.15 if composite == 0 else 1.0)
        uniform = (int(stable[:16], 16) + 1) / ((1 << 64) + 1)
        return -log(uniform) / weight, composite, stable

    ranked = sorted(candidates, key=weighted_key)
    # Stable weighted sampling without replacement across social-strategy
    # strata keeps the shortlist semantically varied before the LLM chooses.
    representatives: list[SubjectCandidate] = []
    seen_strategies: set[str] = set()
    for item in ranked:
        strategy_id = (
            item.presentation.display_strategy.strategy_id
            if item.presentation.display_strategy
            else f"legacy:{item.variant_id}"
        )
        if strategy_id not in seen_strategies:
            representatives.append(item)
            seen_strategies.add(strategy_id)
    selected = representatives[:limit]
    if len(selected) < limit:
        selected_ids = {item.variant_id for item in selected}
        selected.extend(item for item in ranked if item.variant_id not in selected_ids)
    return tuple(selected[:limit])


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
    social = ""
    if presentation.display_strategy and presentation.version != SUBJECT_PRESENTATION_V3:
        strategy = presentation.display_strategy
        forbidden = "; ".join(_forbidden_cue_text(item) for item in strategy.forbidden_cues)
        social = (
            "\nFrozen photo display strategy (the social performance for this recipient):\n"
            f"- overall behavior: {strategy.holistic_cue}\n"
            f"- visible expression cues: {strategy.mouth.replace('_', ' ')}; "
            f"{strategy.eyes.replace('_', ' ')}; {strategy.brows.replace('_', ' ')}; "
            f"{strategy.gaze_quality.replace('_', ' ')}; "
            f"{strategy.facial_tension.replace('_', ' ')}\n"
            f"- captured beat: {strategy.temporal_beat.replace('_', ' ')}\n"
            f"- forbidden expression cues: {forbidden or 'none'}\n"
        )
    facial = presentation.facial_performance
    facial_line = (
        f"- facial performance: family={facial.expression_family}; mouth={facial.mouth_behavior}; "
        f"eyes={facial.eye_behavior}; brows={facial.brow_behavior}; "
        f"gaze sequence={facial.gaze_sequence}; energy={facial.facial_energy}\n"
        if facial
        else f"- expression and body: {render('expression', performance.expression)}; "
    )
    body_prefix = "- body: " if facial else ""
    return (
        "Frozen subject presentation (camera-frame directions, not the character's left/right):\n"
        f"- appearance source: {appearance.source}; hair: "
        f"{render('hair_arrangement', appearance.hair_arrangement)}; "
        f"outfit: {render('outfit_role', appearance.outfit_role)}; "
        f"grooming: {render('grooming', appearance.grooming)}; "
        f"accessories: {accessories}\n"
        f"- head geometry: {render('head_yaw', performance.head_yaw)}; "
        f"{render('head_pitch', performance.head_pitch)}; "
        f"{render('head_roll', performance.head_roll)}"
        f"{'' if facial else '; ' + render('gaze_target', performance.gaze_target)}\n"
        f"{facial_line}{body_prefix}{render('shoulder_orientation', performance.shoulder_orientation)}; "
        f"{render('posture', performance.posture)}\n"
        f"- action and camera awareness: {render('gesture', performance.gesture)}; "
        f"{render('photo_awareness', performance.photo_awareness)}\n"
        f"- hand feasibility: {render('hand_occupancy', performance.hand_occupancy)}; "
        f"occlusion risk={performance.occlusion_complexity}. Keep the wrist, hand, sleeve opening, "
        "and any displayed object anatomically readable and visually distinct, with the correct "
        "contact or attachment to the described surface.\n"
        "The accessory list is exhaustive for this shot: do not add an unlisted signature hair clip "
        "or inherit accessories from an identity reference.\n"
        f"{social}"
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


def _subject_signature(
    appearance: SubjectAppearance,
    performance: SubjectPerformance,
    display_strategy: PhotoDisplayStrategy | None = None,
    facial_performance: FacialPerformance | None = None,
) -> str:
    axes = [
            appearance.hair_arrangement,
            performance.head_yaw,
            performance.head_pitch,
            performance.head_roll,
            performance.gaze_target,
            performance.expression,
            performance.shoulder_orientation,
            performance.gesture,
    ]
    if display_strategy:
        axes.extend(
            (
                display_strategy.strategy_id,
                display_strategy.mouth,
                display_strategy.gaze_quality,
                sha256(
                    json.dumps(
                        display_strategy.to_payload(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            )
        )
    if facial_performance:
        axes.extend(facial_performance.to_payload().values())
    return "|".join(axes)


def _facial_performance(strategy: PhotoDisplayStrategy) -> FacialPerformance:
    family = {
        "pretend_innocent": "playfully_performed",
        "mock_wronged": "playfully_performed",
        "deadpan_reveal": "neutral_present",
        "suppressed_laugh": "amused",
        "self_deprecating_grin": "embarrassed",
        "small_proud_reveal": "proud",
        "soft_bid_for_care": "vulnerable",
        "tired_unfiltered": "tired",
        "composed_attraction": "desire_direct",
        "playful_challenge": "desire_direct",
        "warm_include_you": "tender",
        "candid_enjoyment": "warm",
    }.get(strategy.strategy_id, "neutral_present")
    mouth = {
        "pretend_innocent": "subtle_pout",
        "suppressed_laugh": "suppressed_laugh",
        "self_deprecating_grin": "asymmetric_half_smile",
        "composed_attraction": "relaxed_parted",
        "playful_challenge": "asymmetric_half_smile",
        "tired_unfiltered": "relaxed_parted",
    }.get(strategy.strategy_id, "small_smile" if family in {"warm", "proud", "tender"} else "relaxed_closed")
    eye = {
        "composed_attraction": "steady_lens",
        "playful_challenge": "steady_lens",
        "tired_unfiltered": "relaxed_heavy_lidded",
        "curious_check": "evidence_focus",
    }.get(strategy.strategy_id, "soft_lens")
    gaze = {
        "curious_check": "evidence_then_lens",
        "suppressed_laugh": "lens_then_away",
        "playful_challenge": "away_then_back",
    }.get(strategy.strategy_id, "continuous_lens")
    return FacialPerformance(
        expression_family=family,
        mouth_behavior=mouth,
        eye_behavior=eye,
        brow_behavior=("single_brow_energy" if strategy.strategy_id == "playful_challenge" else "slight_lift" if family == "playfully_performed" else "neutral"),
        gaze_sequence=gaze,
        facial_energy=("held" if family.startswith("desire") else "low" if family in {"tired", "vulnerable"} else "contained"),
    )


def _forbidden_cue_text(value: str) -> str:
    articles = {
        "exaggerated_duck_face": "not an exaggerated duck face",
        "broad_smile": "no broad smile",
        "kiss_gesture": "no kiss gesture",
        "distressed_expression": "no genuinely distressed expression",
    }
    return articles.get(value, f"no {value.replace('_', ' ')}")


def _is_severe_public_affect(value: Mapping[str, object] | None) -> bool:
    if not value:
        return False
    if str(value.get("severity") or "").lower() in {"severe", "acute"}:
        return True
    return any(value.get(key) is True for key in ("acute_pain", "severe_distress"))


def _public_affect_labels(
    snapshot: Mapping[str, object],
    value: Mapping[str, object] | None,
) -> set[str]:
    labels: set[str] = set()
    emotion = _mapping(snapshot.get("character")).get("emotion")
    if isinstance(emotion, str) and emotion:
        labels.add(emotion.lower())
    if value:
        for key in ("emotion", "tone", "dominant"):
            item = value.get(key)
            if isinstance(item, str) and item:
                labels.add(item.lower())
        labels.update(
            str(key).lower()
            for key, item in value.items()
            if isinstance(item, (int, float)) and item > 0
        )
    return labels


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
