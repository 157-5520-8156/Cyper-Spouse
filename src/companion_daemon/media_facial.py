"""Recipient-aware facial display and visible still-frame performance contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

import yaml


FACIAL_DISPLAY_VERSION = "facial-display-strategy-v1"
FACIAL_MICRO_VERSION = "facial-micro-performance-v1"
FACIAL_CATALOG_VERSION = "media-facial-catalog-v1"
DEFAULT_FACIAL_CATALOG = Path("configs/media_facial_performance_templates.yaml")

DISPLAY_FAMILIES = {
    "present_and_available", "warm_connection", "amusement_leaking",
    "deliberate_cuteness", "mock_defiance", "comic_self_exposure", "proud_display",
    "consultative_check", "frustrated_complaint", "embarrassed_repair", "tired_access",
    "vulnerable_disclosure", "tender_private", "desire_direct", "desire_withheld",
    "neutral_evidence",
}
BROW_ACTIONS = {"neutral", "settled", "soft_lift", "bilateral_lift", "one_brow_lift", "brief_question", "slight_inward_draw", "slight_knit", "relaxed_lowered", "not_visible"}
EYE_APERTURES = {"natural", "slightly_widened", "wide_playful", "smile_narrowed", "heavy_lidded", "relaxed_heavy", "brief_squint", "brief_squeeze", "single_wink", "not_visible"}
GAZE_TARGETS = {"lens", "screen", "screen_preview", "primary_evidence", "off_frame", "environment", "companion", "reflection", "not_visible"}
GAZE_SEQUENCES = {"held_lens", "just_returned_to_lens", "about_to_look_away", "caught_off_frame", "evidence_then_recipient", "screen_then_lens", "companion_then_camera", "lens_then_away", "no_face"}
NOSE_CHEEK_ACTIONS = {"relaxed", "cheek_lift", "small_nose_scrunch", "cheek_puff", "one_cheek_tension", "asymmetric_cheek", "flushed_relaxation", "not_visible"}
MOUTH_ACTIONS = {"relaxed_closed", "soft_parted", "small_smile", "suppressed_smile", "asymmetric_half_smile", "subtle_pout", "lightly_pressed", "mid_speech", "open_laugh", "breath_recovery", "not_visible"}
FACIAL_ASYMMETRIES = {"balanced", "subtle_left", "subtle_right", "dynamic_natural", "momentary_irregular", "not_visible"}
DISPLAY_INTENSITIES = {"trace", "low", "subtle", "medium", "clear", "high", "heightened"}
PERFORMANCE_AUTHORSHIPS = {"spontaneous", "recipient_aware", "deliberately_performed", "photographer_prompted", "unperformed_capture", "responsive_candid", "selfie_micro_pose", "playfully_performed", "polished_portrait_performance", "private_recipient_performance", "not_visible"}
TEMPORAL_PHASES = {"preparation", "onset", "held_beat", "leaking", "apex", "release", "after_reaction", "not_visible"}
FACIAL_ENERGIES = {"low", "contained", "lively", "breathless", "held", "recovering", "not_visible"}


@dataclass(frozen=True)
class FacialDisplayStrategy:
    strategy_family: str
    recipient_effect: str
    performance_intent: str
    catalog_version: str
    contract_signature: str
    version: str = FACIAL_DISPLAY_VERSION

    @classmethod
    def create(
        cls,
        *,
        strategy_family: str,
        recipient_effect: str,
        performance_intent: str,
        catalog_version: str = FACIAL_CATALOG_VERSION,
    ) -> "FacialDisplayStrategy":
        payload = (strategy_family, recipient_effect, performance_intent, catalog_version)
        result = cls(*payload, contract_signature=_signature(payload))
        result._validate()
        return result

    def _validate(self) -> None:
        if (
            self.version != FACIAL_DISPLAY_VERSION
            or self.catalog_version != FACIAL_CATALOG_VERSION
            or self.strategy_family not in DISPLAY_FAMILIES
        ):
            raise ValueError("invalid facial display strategy")
        if not self.recipient_effect or not self.performance_intent:
            raise ValueError("incomplete facial display strategy")
        if self.contract_signature != _signature((
            self.strategy_family,
            self.recipient_effect,
            self.performance_intent,
            self.catalog_version,
        )):
            raise ValueError("invalid facial display contract")

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "FacialDisplayStrategy":
        if not isinstance(value, dict):
            raise ValueError("facial display strategy must be an object")
        result = cls(**{name: str(value.get(name) or "") for name in (
            "strategy_family", "recipient_effect", "performance_intent", "catalog_version",
            "contract_signature", "version"
        )})
        result._validate()
        return result


@dataclass(frozen=True)
class FacialMicroPerformance:
    brow_action: str
    eye_aperture: str
    gaze_target: str
    gaze_sequence: str
    nose_cheek_action: str
    mouth_action: str
    facial_asymmetry: str
    display_intensity: str
    performance_authorship: str
    temporal_phase: str
    facial_energy: str
    recipe_id: str
    catalog_version: str
    contract_signature: str
    version: str = FACIAL_MICRO_VERSION

    @classmethod
    def create(cls, **values: str) -> "FacialMicroPerformance":
        names = (
            "brow_action", "eye_aperture", "gaze_target", "gaze_sequence",
            "nose_cheek_action", "mouth_action", "facial_asymmetry", "display_intensity",
            "performance_authorship", "temporal_phase", "facial_energy",
        )
        recipe_id = str(values.get("recipe_id") or "custom")
        catalog_version = str(values.get("catalog_version") or FACIAL_CATALOG_VERSION)
        payload = (*tuple(values[name] for name in names), recipe_id, catalog_version)
        result = cls(*payload, contract_signature=_signature(payload))
        result._validate()
        return result

    def _validate(self) -> None:
        enums = (
            (self.brow_action, BROW_ACTIONS), (self.eye_aperture, EYE_APERTURES),
            (self.gaze_target, GAZE_TARGETS), (self.gaze_sequence, GAZE_SEQUENCES),
            (self.nose_cheek_action, NOSE_CHEEK_ACTIONS), (self.mouth_action, MOUTH_ACTIONS),
            (self.facial_asymmetry, FACIAL_ASYMMETRIES), (self.display_intensity, DISPLAY_INTENSITIES),
            (self.performance_authorship, PERFORMANCE_AUTHORSHIPS),
            (self.temporal_phase, TEMPORAL_PHASES), (self.facial_energy, FACIAL_ENERGIES),
        )
        if (
            self.version != FACIAL_MICRO_VERSION
            or self.catalog_version != FACIAL_CATALOG_VERSION
            or not self.recipe_id
            or any(value not in allowed for value, allowed in enums)
        ):
            raise ValueError("invalid facial micro-performance")
        payload = (
            self.brow_action, self.eye_aperture, self.gaze_target, self.gaze_sequence,
            self.nose_cheek_action, self.mouth_action, self.facial_asymmetry,
            self.display_intensity, self.performance_authorship, self.temporal_phase,
            self.facial_energy, self.recipe_id, self.catalog_version,
        )
        if self.contract_signature != _signature(payload):
            raise ValueError("invalid facial micro-performance contract")

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "FacialMicroPerformance":
        if not isinstance(value, dict):
            raise ValueError("facial micro-performance must be an object")
        names = (
            "brow_action", "eye_aperture", "gaze_target", "gaze_sequence", "nose_cheek_action",
            "mouth_action", "facial_asymmetry", "display_intensity", "performance_authorship",
            "temporal_phase", "facial_energy", "recipe_id", "catalog_version",
            "contract_signature", "version",
        )
        result = cls(**{name: str(value.get(name) or "") for name in names})
        result._validate()
        return result


_FALLBACK_STRATEGY_AFFINITIES = {
    "presence": ("present_and_available", "warm_connection", "amusement_leaking"),
    "reveal": ("proud_display", "amusement_leaking", "warm_connection"),
    "demonstration": ("neutral_evidence", "present_and_available", "proud_display"),
    "question": ("consultative_check", "deliberate_cuteness", "present_and_available"),
    "comparison": ("consultative_check", "mock_defiance", "neutral_evidence"),
    "contrast": ("mock_defiance", "amusement_leaking", "comic_self_exposure", "frustrated_complaint"),
    "comic_hook": ("deliberate_cuteness", "mock_defiance", "amusement_leaking", "comic_self_exposure", "embarrassed_repair"),
    "celebration": ("proud_display", "amusement_leaking", "warm_connection"),
    "vulnerability": ("vulnerable_disclosure", "tired_access", "embarrassed_repair"),
    "reassurance": ("warm_connection", "tender_private", "present_and_available"),
    "coordination": ("neutral_evidence", "consultative_check", "present_and_available"),
    "affection": ("tender_private", "warm_connection", "desire_withheld"),
    "nostalgia": ("tender_private", "warm_connection", "desire_withheld"),
    "attraction": ("desire_direct", "desire_withheld", "deliberate_cuteness", "mock_defiance", "tender_private"),
}

_FALLBACK_ATTRACTION_AFFINITIES = {
    "direct_invitation": ("desire_direct", "mock_defiance", "deliberate_cuteness"),
    "confident_display": ("desire_direct", "mock_defiance", "deliberate_cuteness"),
    "sensory_immediacy": ("desire_direct", "mock_defiance", "deliberate_cuteness"),
    "withheld_attention": ("desire_withheld", "tender_private", "amusement_leaking"),
    "interrupted_transition": ("desire_withheld", "tender_private", "amusement_leaking"),
    "atmospheric_suggestion": ("desire_withheld", "tender_private", "amusement_leaking"),
    "playful_tease": ("deliberate_cuteness", "mock_defiance", "amusement_leaking"),
    "private_trust": ("tender_private", "desire_withheld", "desire_direct"),
    "close_proximity": ("tender_private", "desire_withheld", "desire_direct"),
}


def _micro(brow_action: str, eye_aperture: str, gaze_target: str, gaze_sequence: str, nose_cheek_action: str, mouth_action: str, facial_asymmetry: str, display_intensity: str, performance_authorship: str, temporal_phase: str, facial_energy: str) -> dict[str, str]:
    return locals()


_MICRO_RECIPES: dict[str, tuple[dict[str, str], ...]] = {
    "present_and_available": (_micro("neutral", "natural", "lens", "held_lens", "relaxed", "relaxed_closed", "balanced", "subtle", "recipient_aware", "held_beat", "contained"),),
    "warm_connection": (_micro("neutral", "natural", "lens", "held_lens", "cheek_lift", "small_smile", "dynamic_natural", "subtle", "recipient_aware", "held_beat", "contained"),),
    "amusement_leaking": (
        _micro("soft_lift", "smile_narrowed", "lens", "just_returned_to_lens", "small_nose_scrunch", "suppressed_smile", "dynamic_natural", "clear", "spontaneous", "leaking", "lively"),
        _micro("neutral", "brief_squint", "off_frame", "caught_off_frame", "cheek_lift", "asymmetric_half_smile", "subtle_left", "clear", "spontaneous", "after_reaction", "lively"),
    ),
    "deliberate_cuteness": (
        _micro("soft_lift", "slightly_widened", "lens", "held_lens", "relaxed", "subtle_pout", "balanced", "clear", "deliberately_performed", "held_beat", "held"),
        _micro("soft_lift", "natural", "lens", "just_returned_to_lens", "one_cheek_tension", "suppressed_smile", "subtle_right", "clear", "deliberately_performed", "onset", "lively"),
    ),
    "mock_defiance": (
        _micro("one_brow_lift", "natural", "off_frame", "just_returned_to_lens", "one_cheek_tension", "lightly_pressed", "subtle_right", "clear", "deliberately_performed", "held_beat", "contained"),
        _micro("one_brow_lift", "brief_squint", "lens", "about_to_look_away", "relaxed", "asymmetric_half_smile", "subtle_left", "clear", "recipient_aware", "held_beat", "lively"),
    ),
    "comic_self_exposure": (_micro("slight_inward_draw", "natural", "lens", "evidence_then_recipient", "small_nose_scrunch", "lightly_pressed", "dynamic_natural", "clear", "recipient_aware", "after_reaction", "recovering"),),
    "proud_display": (_micro("soft_lift", "natural", "lens", "evidence_then_recipient", "cheek_lift", "asymmetric_half_smile", "subtle_left", "clear", "recipient_aware", "held_beat", "contained"),),
    "consultative_check": (_micro("soft_lift", "natural", "lens", "evidence_then_recipient", "relaxed", "soft_parted", "balanced", "clear", "recipient_aware", "held_beat", "held"),),
    "frustrated_complaint": (_micro("slight_inward_draw", "brief_squint", "lens", "held_lens", "one_cheek_tension", "lightly_pressed", "dynamic_natural", "clear", "recipient_aware", "after_reaction", "contained"),),
    "embarrassed_repair": (_micro("slight_inward_draw", "smile_narrowed", "off_frame", "just_returned_to_lens", "small_nose_scrunch", "suppressed_smile", "subtle_right", "clear", "recipient_aware", "release", "recovering"),),
    "tired_access": (_micro("relaxed_lowered", "heavy_lidded", "lens", "held_lens", "relaxed", "soft_parted", "dynamic_natural", "subtle", "spontaneous", "after_reaction", "low"),),
    "vulnerable_disclosure": (_micro("slight_inward_draw", "natural", "lens", "about_to_look_away", "relaxed", "soft_parted", "balanced", "subtle", "recipient_aware", "held_beat", "low"),),
    "tender_private": (_micro("relaxed_lowered", "heavy_lidded", "lens", "held_lens", "cheek_lift", "soft_parted", "dynamic_natural", "subtle", "recipient_aware", "held_beat", "held"),),
    "desire_direct": (_micro("neutral", "heavy_lidded", "lens", "held_lens", "flushed_relaxation", "soft_parted", "subtle_left", "clear", "deliberately_performed", "held_beat", "held"),),
    "desire_withheld": (_micro("relaxed_lowered", "natural", "off_frame", "just_returned_to_lens", "flushed_relaxation", "relaxed_closed", "dynamic_natural", "clear", "recipient_aware", "onset", "held"),),
    "neutral_evidence": (_micro("neutral", "natural", "primary_evidence", "evidence_then_recipient", "relaxed", "relaxed_closed", "balanced", "trace", "recipient_aware", "held_beat", "contained"),),
}

_EXTRA_MICRO_RECIPES: dict[str, tuple[dict[str, str], ...]] = {
    "present_and_available": (
        _micro("settled", "natural", "screen_preview", "screen_then_lens", "relaxed", "soft_parted", "dynamic_natural", "low", "selfie_micro_pose", "onset", "contained"),
        _micro("settled", "natural", "companion", "companion_then_camera", "relaxed", "mid_speech", "dynamic_natural", "medium", "responsive_candid", "after_reaction", "lively"),
    ),
    "warm_connection": (
        _micro("soft_lift", "smile_narrowed", "lens", "held_lens", "cheek_lift", "suppressed_smile", "subtle_left", "medium", "selfie_micro_pose", "leaking", "contained"),
        _micro("settled", "natural", "companion", "companion_then_camera", "cheek_lift", "small_smile", "dynamic_natural", "low", "responsive_candid", "after_reaction", "lively"),
    ),
    "amusement_leaking": (
        _micro("bilateral_lift", "brief_squeeze", "lens", "just_returned_to_lens", "small_nose_scrunch", "open_laugh", "momentary_irregular", "high", "responsive_candid", "apex", "lively"),
        _micro("settled", "smile_narrowed", "screen_preview", "screen_then_lens", "asymmetric_cheek", "suppressed_smile", "subtle_right", "medium", "selfie_micro_pose", "leaking", "contained"),
    ),
    "deliberate_cuteness": (
        _micro("bilateral_lift", "wide_playful", "lens", "held_lens", "cheek_puff", "relaxed_closed", "balanced", "medium", "playfully_performed", "held_beat", "held"),
        _micro("soft_lift", "single_wink", "lens", "held_lens", "asymmetric_cheek", "asymmetric_half_smile", "subtle_left", "medium", "playfully_performed", "apex", "lively"),
        _micro("brief_question", "natural", "lens", "about_to_look_away", "small_nose_scrunch", "subtle_pout", "dynamic_natural", "low", "selfie_micro_pose", "onset", "contained"),
    ),
    "mock_defiance": (
        _micro("one_brow_lift", "natural", "lens", "lens_then_away", "asymmetric_cheek", "asymmetric_half_smile", "subtle_left", "medium", "playfully_performed", "release", "contained"),
        _micro("slight_knit", "brief_squint", "off_frame", "just_returned_to_lens", "one_cheek_tension", "subtle_pout", "subtle_right", "medium", "selfie_micro_pose", "held_beat", "held"),
    ),
    "comic_self_exposure": (
        _micro("settled", "relaxed_heavy", "lens", "held_lens", "relaxed", "lightly_pressed", "balanced", "medium", "playfully_performed", "held_beat", "low"),
        _micro("bilateral_lift", "wide_playful", "primary_evidence", "evidence_then_recipient", "small_nose_scrunch", "mid_speech", "momentary_irregular", "high", "responsive_candid", "after_reaction", "lively"),
    ),
    "proud_display": (
        _micro("settled", "natural", "lens", "held_lens", "relaxed", "relaxed_closed", "subtle_right", "low", "polished_portrait_performance", "held_beat", "contained"),
        _micro("soft_lift", "smile_narrowed", "primary_evidence", "evidence_then_recipient", "cheek_lift", "small_smile", "dynamic_natural", "medium", "selfie_micro_pose", "release", "lively"),
    ),
    "consultative_check": (
        _micro("brief_question", "natural", "screen_preview", "screen_then_lens", "relaxed", "lightly_pressed", "balanced", "medium", "selfie_micro_pose", "held_beat", "held"),
        _micro("bilateral_lift", "slightly_widened", "primary_evidence", "evidence_then_recipient", "relaxed", "mid_speech", "dynamic_natural", "medium", "recipient_aware", "onset", "contained"),
    ),
    "frustrated_complaint": (
        _micro("slight_knit", "relaxed_heavy", "lens", "held_lens", "one_cheek_tension", "mid_speech", "dynamic_natural", "medium", "selfie_micro_pose", "apex", "low"),
        _micro("slight_inward_draw", "brief_squeeze", "off_frame", "lens_then_away", "small_nose_scrunch", "lightly_pressed", "momentary_irregular", "medium", "responsive_candid", "after_reaction", "recovering"),
    ),
    "embarrassed_repair": (
        _micro("brief_question", "natural", "off_frame", "just_returned_to_lens", "cheek_lift", "subtle_pout", "subtle_left", "low", "selfie_micro_pose", "onset", "held"),
        _micro("slight_inward_draw", "smile_narrowed", "lens", "about_to_look_away", "small_nose_scrunch", "asymmetric_half_smile", "momentary_irregular", "medium", "responsive_candid", "release", "recovering"),
    ),
    "tired_access": (
        _micro("relaxed_lowered", "relaxed_heavy", "screen_preview", "screen_then_lens", "relaxed", "relaxed_closed", "dynamic_natural", "trace", "unperformed_capture", "after_reaction", "low"),
        _micro("settled", "relaxed_heavy", "lens", "held_lens", "flushed_relaxation", "breath_recovery", "momentary_irregular", "medium", "private_recipient_performance", "release", "breathless"),
    ),
    "vulnerable_disclosure": (
        _micro("slight_inward_draw", "natural", "off_frame", "just_returned_to_lens", "relaxed", "lightly_pressed", "subtle_left", "low", "private_recipient_performance", "onset", "held"),
        _micro("settled", "relaxed_heavy", "lens", "about_to_look_away", "relaxed", "soft_parted", "dynamic_natural", "medium", "private_recipient_performance", "held_beat", "low"),
    ),
    "tender_private": (
        _micro("settled", "natural", "lens", "held_lens", "cheek_lift", "relaxed_closed", "subtle_left", "low", "private_recipient_performance", "held_beat", "contained"),
        _micro("relaxed_lowered", "relaxed_heavy", "screen_preview", "screen_then_lens", "relaxed", "soft_parted", "dynamic_natural", "medium", "private_recipient_performance", "onset", "held"),
    ),
    "desire_direct": (
        _micro("settled", "natural", "lens", "held_lens", "flushed_relaxation", "relaxed_closed", "subtle_right", "medium", "private_recipient_performance", "held_beat", "held"),
        _micro("one_brow_lift", "relaxed_heavy", "lens", "held_lens", "asymmetric_cheek", "asymmetric_half_smile", "subtle_left", "medium", "polished_portrait_performance", "held_beat", "contained"),
    ),
    "desire_withheld": (
        _micro("settled", "natural", "off_frame", "lens_then_away", "relaxed", "suppressed_smile", "subtle_left", "low", "private_recipient_performance", "release", "contained"),
        _micro("relaxed_lowered", "relaxed_heavy", "lens", "about_to_look_away", "flushed_relaxation", "soft_parted", "dynamic_natural", "medium", "private_recipient_performance", "onset", "held"),
    ),
    "neutral_evidence": (
        _micro("settled", "natural", "screen_preview", "screen_then_lens", "relaxed", "mid_speech", "dynamic_natural", "low", "unperformed_capture", "onset", "contained"),
        _micro("brief_question", "natural", "primary_evidence", "evidence_then_recipient", "relaxed", "relaxed_closed", "balanced", "low", "selfie_micro_pose", "held_beat", "contained"),
    ),
}

_MICRO_RECIPES = {
    family: (*recipes, *_EXTRA_MICRO_RECIPES.get(family, ()))
    for family, recipes in _MICRO_RECIPES.items()
}


def choose_facial_contract(
    *,
    stable_seed: str,
    engagement_tactic: str,
    attraction_mechanism: str | None,
    capture_mode: str = "character_front_camera",
    face_visible: bool = True,
    catalog_path: Path = DEFAULT_FACIAL_CATALOG,
) -> tuple[FacialDisplayStrategy, FacialMicroPerformance]:
    tactic_affinities, attraction_affinities = _load_affinity_catalog(catalog_path)
    if not face_visible:
        family = "neutral_evidence"
        micro = FacialMicroPerformance.create(
            **_micro("not_visible", "not_visible", "not_visible", "no_face", "not_visible", "not_visible", "not_visible", "trace", "not_visible", "not_visible", "not_visible"),
            recipe_id="neutral_evidence:no_face",
        )
        return FacialDisplayStrategy.create(strategy_family=family, recipient_effect="keep attention on grounded evidence", performance_intent="face is not visible"), micro
    families = list(tactic_affinities.get(engagement_tactic, ("present_and_available",)))
    if attraction_mechanism in attraction_affinities:
        families = list(attraction_affinities[attraction_mechanism])
    family = _pick(stable_seed + ":family", tuple(families))
    compatible = tuple(
        item for item in _MICRO_RECIPES[family]
        if _capture_compatible(item, capture_mode=capture_mode)
    )
    if not compatible:
        compatible = _MICRO_RECIPES[family]
    recipe = _pick_object(stable_seed + ":micro", compatible)
    recipe_id = f"{family}:{_MICRO_RECIPES[family].index(recipe)}"
    effect = {
        "desire_direct": "make attraction unmistakably recipient-directed",
        "desire_withheld": "leave the recipient aware that attention is being held back",
        "deliberate_cuteness": "invite a playful affectionate response",
        "mock_defiance": "invite teasing without reversing the underlying warmth",
        "amusement_leaking": "let a real reaction escape before composure returns",
    }.get(family, "make the intended social bid legible without overacting")
    return (
        FacialDisplayStrategy.create(
            strategy_family=family,
            recipient_effect=effect,
            performance_intent="a coherent still-frame social performance, not an inferred inner emotion",
        ),
        FacialMicroPerformance.create(**recipe, recipe_id=recipe_id),
    )


def _capture_compatible(recipe: dict[str, str], *, capture_mode: str) -> bool:
    authorship = recipe["performance_authorship"]
    gaze = recipe["gaze_target"]
    sequence = recipe["gaze_sequence"]
    if capture_mode == "character_front_camera":
        return authorship not in {
            "responsive_candid", "photographer_prompted", "unperformed_capture"
        } and gaze != "companion" and sequence != "companion_then_camera"
    if capture_mode == "mirror":
        return gaze not in {"companion", "screen_preview"} and sequence not in {
            "companion_then_camera", "screen_then_lens"
        }
    if capture_mode == "timer_fixed":
        return gaze not in {"screen", "screen_preview", "companion"} and sequence not in {
            "screen_then_lens", "companion_then_camera"
        }
    if capture_mode in {"known_companion", "external_sender", "requested_helper"}:
        return authorship not in {"selfie_micro_pose"} and gaze not in {
            "screen", "screen_preview"
        } and sequence != "screen_then_lens"
    return True


@lru_cache(maxsize=8)
def _load_affinity_catalog(
    path: Path,
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Load the extensible semantic matrix while keeping visible recipes fully frozen."""

    if not path.is_file():
        return _FALLBACK_STRATEGY_AFFINITIES, _FALLBACK_ATTRACTION_AFFINITIES
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != FACIAL_CATALOG_VERSION:
        raise ValueError("unsupported facial performance catalog")

    def matrix(name: str) -> dict[str, tuple[str, ...]]:
        values = raw.get(name)
        if not isinstance(values, dict):
            raise ValueError(f"missing facial catalog matrix: {name}")
        result: dict[str, tuple[str, ...]] = {}
        for key, families in values.items():
            if not isinstance(key, str) or not isinstance(families, list) or not families:
                raise ValueError(f"invalid facial catalog matrix row: {name}")
            frozen = tuple(str(item) for item in families)
            if any(item not in DISPLAY_FAMILIES for item in frozen):
                raise ValueError(f"unknown facial family in catalog: {name}")
            result[key] = frozen
        return result

    axes = raw.get("visible_action_axes")
    expected_axes = {
        "brow_action": BROW_ACTIONS,
        "eye_aperture": EYE_APERTURES,
        "gaze_target": GAZE_TARGETS,
        "gaze_sequence": GAZE_SEQUENCES,
        "nose_cheek_action": NOSE_CHEEK_ACTIONS,
        "mouth_action": MOUTH_ACTIONS,
        "facial_asymmetry": FACIAL_ASYMMETRIES,
        "display_intensity": DISPLAY_INTENSITIES,
        "performance_authorship": PERFORMANCE_AUTHORSHIPS,
        "temporal_phase": TEMPORAL_PHASES,
        "facial_energy": FACIAL_ENERGIES,
    }
    if not isinstance(axes, dict) or any(
        set(str(item) for item in axes.get(name, [])) != allowed
        for name, allowed in expected_axes.items()
    ):
        raise ValueError("facial catalog visible-action axes do not match runtime contract")
    return matrix("tactic_affinities"), matrix("attraction_affinities")


def facial_prompt_block(strategy: FacialDisplayStrategy, micro: FacialMicroPerformance) -> str:
    return (
        "Facial Display Strategy (social meaning for this recipient):\n"
        f"- family: {strategy.strategy_family}; intended recipient effect: {strategy.recipient_effect}; "
        f"performance intent: {strategy.performance_intent}\n"
        "Facial Micro-Performance (visible actions in this single captured beat, not a diagnosis or a multi-frame claim):\n"
        f"- brow: {micro.brow_action}; eye aperture: {micro.eye_aperture}; current gaze target: {micro.gaze_target}; "
        f"captured gaze beat: {micro.gaze_sequence}\n"
        f"- nose/cheek: {micro.nose_cheek_action}; mouth: {micro.mouth_action}; asymmetry: {micro.facial_asymmetry}\n"
        f"- intensity: {micro.display_intensity}; authorship: {micro.performance_authorship}; "
        f"temporal phase: {micro.temporal_phase}; energy: {micro.facial_energy}. Render these as one coherent face, "
        "not as independent sliders; avoid defaulting to a polite small smile."
    )


def _pick(seed: str, values: tuple[str, ...]) -> str:
    return values[int(sha256(seed.encode()).hexdigest()[:16], 16) % len(values)]


def _pick_object(seed: str, values: tuple[dict[str, str], ...]) -> dict[str, str]:
    return values[int(sha256(seed.encode()).hexdigest()[:16], 16) % len(values)]


def _signature(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]
