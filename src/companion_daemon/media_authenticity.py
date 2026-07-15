"""Frozen whole-image phone-photography behavior for event media.

This contract describes how a plausible device would render a grounded scene.
It deliberately does not invent scene facts and does not equate authenticity
with indiscriminate blur, grain, or low quality.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping

import yaml


PHOTOGRAPHIC_AUTHENTICITY_VERSION = "photographic-authenticity-v1"
AUTHENTICITY_CATALOG_VERSION = "media-authenticity-catalog-v1"
DEFAULT_AUTHENTICITY_CATALOG = Path("configs/media_photographic_authenticity_templates.yaml")

DEVICE_RENDERINGS = {
    "front_wide", "rear_standard", "rear_ultrawide", "tele_crop",
    "unknown_phone", "artifact_inherited",
}
EXPOSURE_BEHAVIORS = {
    "stable", "highlight_protected", "shadow_lifted", "backlit_compromise",
    "mixed_light_compromise", "low_light_stack", "flash_falloff", "artifact_inherited",
}
COLOR_BEHAVIORS = {
    "neutral_phone", "warm_cast", "cool_cast", "mixed_white_balance",
    "moderately_vivid", "muted", "artifact_inherited",
}
PROCESSING_LEVELS = {
    "light", "typical_phone", "social_edit", "strong_filter", "artifact_inherited",
}
SCENE_ORDERLINESS = {"lived_in", "ordinary", "lightly_arranged", "display_ready", "commercial"}
CAPTURE_IMPERFECTIONS = {
    "clean", "off_center", "partial_crop", "minor_motion", "focus_transition",
    "reflection_layer", "foreground_interrupt", "lens_smudge_or_flare", "artifact_inherited",
}
ENVIRONMENT_ENTROPIES = {"sparse", "normal", "busy", "transient", "artifact_inherited"}
REGIONAL_GROUNDINGS = {"explicit", "weak", "none", "artifact_inherited"}
AESTHETIC_INTENTS = {
    "documentary", "pleasant_share", "atmospheric", "editorial", "commercial",
    "artifact_inherited",
}


@dataclass(frozen=True)
class PhotographicAuthenticityProfile:
    profile_id: str
    device_rendering: str
    exposure_behavior: str
    color_behavior: str
    processing_level: str
    scene_orderliness: str
    capture_imperfection: str
    environment_entropy: str
    regional_grounding: str
    aesthetic_intent: str
    catalog_version: str
    contract_signature: str
    version: str = PHOTOGRAPHIC_AUTHENTICITY_VERSION

    @classmethod
    def create(cls, **values: str) -> "PhotographicAuthenticityProfile":
        catalog_version = str(values.get("catalog_version") or AUTHENTICITY_CATALOG_VERSION)
        payload = (
            values["profile_id"], values["device_rendering"], values["exposure_behavior"],
            values["color_behavior"], values["processing_level"], values["scene_orderliness"],
            values["capture_imperfection"], values["environment_entropy"],
            values["regional_grounding"], values["aesthetic_intent"], catalog_version,
        )
        result = cls(*payload, contract_signature=_signature(payload))
        result._validate()
        return result

    def _validate(self) -> None:
        enums = (
            (self.device_rendering, DEVICE_RENDERINGS),
            (self.exposure_behavior, EXPOSURE_BEHAVIORS),
            (self.color_behavior, COLOR_BEHAVIORS),
            (self.processing_level, PROCESSING_LEVELS),
            (self.scene_orderliness, SCENE_ORDERLINESS),
            (self.capture_imperfection, CAPTURE_IMPERFECTIONS),
            (self.environment_entropy, ENVIRONMENT_ENTROPIES),
            (self.regional_grounding, REGIONAL_GROUNDINGS),
            (self.aesthetic_intent, AESTHETIC_INTENTS),
        )
        if (
            not self.profile_id
            or self.version != PHOTOGRAPHIC_AUTHENTICITY_VERSION
            or self.catalog_version != AUTHENTICITY_CATALOG_VERSION
        ):
            raise ValueError("invalid photographic authenticity profile")
        if any(value not in allowed for value, allowed in enums):
            raise ValueError("invalid photographic authenticity enum")
        expected = _signature((
            self.profile_id, self.device_rendering, self.exposure_behavior,
            self.color_behavior, self.processing_level, self.scene_orderliness,
            self.capture_imperfection, self.environment_entropy,
            self.regional_grounding, self.aesthetic_intent, self.catalog_version,
        ))
        if self.contract_signature != expected:
            raise ValueError("invalid photographic authenticity contract")

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "PhotographicAuthenticityProfile":
        if not isinstance(value, dict):
            raise ValueError("photographic authenticity must be an object")
        names = (
            "profile_id", "device_rendering", "exposure_behavior", "color_behavior",
            "processing_level", "scene_orderliness", "capture_imperfection",
            "environment_entropy", "regional_grounding", "aesthetic_intent", "catalog_version",
            "contract_signature", "version",
        )
        result = cls(**{name: str(value.get(name) or "") for name in names})
        result._validate()
        return result


def choose_authenticity_profile(
    *,
    stable_seed: str,
    capture_mode: str,
    family: str,
    staging_degree: str,
    visual_form: str,
    event_snapshot: Mapping[str, object] | None = None,
    catalog_path: Path = DEFAULT_AUTHENTICITY_CATALOG,
) -> PhotographicAuthenticityProfile:
    """Choose one coherent profile with stable variation and conservative grounding."""

    _validate_catalog(catalog_path)

    if capture_mode == "existing_artifact":
        return PhotographicAuthenticityProfile.create(
            profile_id="artifact_inherited", device_rendering="artifact_inherited",
            exposure_behavior="artifact_inherited", color_behavior="artifact_inherited",
            processing_level="artifact_inherited", scene_orderliness="ordinary",
            capture_imperfection="artifact_inherited", environment_entropy="artifact_inherited",
            regional_grounding="artifact_inherited", aesthetic_intent="artifact_inherited",
        )
    device_options = {
        "character_front_camera": ("front_wide",),
        "character_rear_camera": ("rear_standard", "rear_ultrawide"),
        "mirror": ("rear_standard",),
        "timer_fixed": ("rear_standard", "rear_ultrawide"),
        "requested_helper": ("rear_standard", "tele_crop"),
        "known_companion": ("rear_standard", "rear_ultrawide", "tele_crop"),
        "external_sender": ("unknown_phone", "rear_standard"),
    }.get(capture_mode, ("unknown_phone",))
    snapshot = event_snapshot if isinstance(event_snapshot, Mapping) else {}
    environment = snapshot.get("environment")
    activity = snapshot.get("activity")
    location = snapshot.get("location")
    requirements = snapshot.get("visual_requirements")
    environment = environment if isinstance(environment, Mapping) else {}
    activity = activity if isinstance(activity, Mapping) else {}
    location = location if isinstance(location, Mapping) else {}
    requirements = requirements if isinstance(requirements, Mapping) else {}
    lighting = " ".join(str(value).lower() for value in environment.values())
    activity_text = " ".join(str(value).lower() for value in activity.values())
    explicit_intent = str(requirements.get("aesthetic_intent") or "")
    intent_options = {
        "unposed": ("documentary", "pleasant_share"),
        "camera_aware": ("pleasant_share", "documentary", "atmospheric"),
        "lightly_arranged": ("pleasant_share", "atmospheric", "documentary"),
        "deliberately_posed": ("pleasant_share", "editorial", "atmospheric"),
        "privately_composed": ("atmospheric", "pleasant_share", "documentary"),
    }.get(staging_degree, ("pleasant_share", "documentary"))
    if explicit_intent in AESTHETIC_INTENTS - {"artifact_inherited"}:
        intent_options = (explicit_intent,)
    if family == "life_share":
        intent_options = tuple(
            item for item in intent_options
            if item not in {"editorial", "commercial"} or explicit_intent == item
        )
    intent = _pick(stable_seed + ":intent", intent_options)
    device = _pick(stable_seed + ":device", device_options)
    if any(token in lighting for token in ("night", "dark", "dim", "low light", "夜", "昏暗")):
        exposure_options = ("low_light_stack", "shadow_lifted")
    elif any(token in lighting for token in ("backlight", "backlit", "逆光", "sun behind")):
        exposure_options = ("backlit_compromise", "highlight_protected")
    elif any(token in lighting for token in ("mixed", "neon", "混合", "室内外")):
        exposure_options = ("mixed_light_compromise", "highlight_protected")
    elif any(token in lighting for token in ("flash", "闪光")):
        exposure_options = ("flash_falloff",)
    else:
        exposure_options = ("stable", "highlight_protected")
    if any(token in lighting for token in ("warm", "golden", "暖")):
        color_options = ("warm_cast", "neutral_phone")
    elif any(token in lighting for token in ("cool", "blue", "冷", "蓝")):
        color_options = ("cool_cast", "neutral_phone")
    elif any(token in lighting for token in ("mixed", "neon", "混合")):
        color_options = ("mixed_white_balance", "neutral_phone")
    else:
        color_options = ("neutral_phone", "moderately_vivid", "muted")
    explicit_processing = str(requirements.get("processing_level") or "")
    processing_options = (
        (explicit_processing,)
        if explicit_processing in PROCESSING_LEVELS - {"artifact_inherited"}
        else {
            "documentary": ("light", "typical_phone"),
            "pleasant_share": ("typical_phone", "light", "social_edit"),
            "atmospheric": ("typical_phone", "social_edit"),
            "editorial": ("social_edit", "typical_phone"),
            "commercial": ("social_edit", "strong_filter"),
        }[intent]
    )
    scene_state = str(environment.get("scene_orderliness") or "")
    if scene_state in SCENE_ORDERLINESS:
        orderliness_options = (scene_state,)
    elif any(token in activity_text for token in ("mess", "spill", "failed", "翻车", "弄乱")):
        orderliness_options = ("lived_in", "ordinary")
    else:
        orderliness_options = {
            "unposed": ("ordinary", "lived_in"),
            "camera_aware": ("ordinary", "lightly_arranged"),
            "lightly_arranged": ("lightly_arranged", "ordinary"),
            "deliberately_posed": ("display_ready", "lightly_arranged"),
            "privately_composed": ("ordinary", "lightly_arranged"),
        }.get(staging_degree, ("ordinary",))
    if intent == "commercial":
        orderliness_options = ("commercial", "display_ready")
    entropy_value = str(environment.get("entropy") or "")
    if entropy_value in ENVIRONMENT_ENTROPIES:
        entropy_options = (entropy_value,)
    elif any(token in activity_text for token in ("travel", "walking", "running", "transit", "旅行", "步行", "跑", "候车")):
        entropy_options = ("transient", "normal")
    elif any(token in lighting + activity_text for token in ("crowd", "busy", "拥挤", "人多")):
        entropy_options = ("busy", "normal")
    else:
        entropy_options = ("normal", "sparse")
    imperfection_options = ["clean", "off_center", "partial_crop"]
    if any(token in activity_text for token in ("walking", "running", "dance", "步行", "跑", "舞")):
        imperfection_options.extend(("minor_motion", "focus_transition"))
    if capture_mode in {"known_companion", "external_sender", "timer_fixed"}:
        imperfection_options.append("foreground_interrupt")
    if capture_mode == "mirror":
        imperfection_options.append("reflection_layer")
    if any(token in lighting + activity_text for token in ("rain", "雨", "steam", "雾")):
        imperfection_options.append("lens_smudge_or_flare")
    regional = "none"
    if any(location.get(key) for key in ("country", "region", "city")):
        regional = "explicit"
    return PhotographicAuthenticityProfile.create(
        profile_id=f"{intent}:{capture_mode}:{visual_form}",
        device_rendering=device,
        exposure_behavior=_pick(stable_seed + ":exposure", exposure_options),
        color_behavior=_pick(stable_seed + ":color", color_options),
        processing_level=_pick(stable_seed + ":processing", processing_options),
        scene_orderliness=_pick(stable_seed + ":orderliness", orderliness_options),
        capture_imperfection=_pick(
            stable_seed + ":imperfection", tuple(dict.fromkeys(imperfection_options))
        ),
        environment_entropy=_pick(stable_seed + ":entropy", entropy_options),
        regional_grounding=regional,
        aesthetic_intent=intent,
    )


def authenticity_prompt_block(profile: PhotographicAuthenticityProfile) -> str:
    return (
        "Photographic Authenticity (one coherent phone-image behavior, not a generic quality filter):\n"
        f"- device rendering: {profile.device_rendering}; exposure: {profile.exposure_behavior}; "
        f"color: {profile.color_behavior}; processing: {profile.processing_level}\n"
        f"- scene orderliness: {profile.scene_orderliness}; environment entropy: "
        f"{profile.environment_entropy}; single credible imperfection: {profile.capture_imperfection}\n"
        f"- aesthetic intent: {profile.aesthetic_intent}; regional grounding: "
        f"{profile.regional_grounding}. Never invent a country, city, sign, architecture, or transit system "
        "that is not present in selected event evidence. Authenticity does not require blanket grain, blur, "
        "desaturation, clutter, or poor image quality."
    )


def _pick(seed: str, values: tuple[str, ...]) -> str:
    if not values:
        raise ValueError("empty authenticity choice")
    return values[int(sha256(seed.encode()).hexdigest()[:16], 16) % len(values)]


@lru_cache(maxsize=8)
def _validate_catalog(path: Path) -> None:
    if not path.is_file():
        return
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("schema_version") != AUTHENTICITY_CATALOG_VERSION:
        raise ValueError("unsupported photographic authenticity catalog")
    axes = raw.get("axes")
    expected = {
        "device_rendering": DEVICE_RENDERINGS,
        "exposure_behavior": EXPOSURE_BEHAVIORS,
        "color_behavior": COLOR_BEHAVIORS,
        "processing_level": PROCESSING_LEVELS,
        "scene_orderliness": SCENE_ORDERLINESS,
        "capture_imperfection": CAPTURE_IMPERFECTIONS,
        "environment_entropy": ENVIRONMENT_ENTROPIES,
        "regional_grounding": REGIONAL_GROUNDINGS,
        "aesthetic_intent": AESTHETIC_INTENTS,
    }
    if not isinstance(axes, dict) or any(
        set(str(item) for item in axes.get(name, [])) != allowed
        for name, allowed in expected.items()
    ):
        raise ValueError("authenticity catalog axes do not match runtime contract")


def _signature(value: object) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:24]
