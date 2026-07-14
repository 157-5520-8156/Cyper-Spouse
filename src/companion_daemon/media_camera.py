"""Concrete camera geometry and capture-physics validation for media v5."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


CAMERA_GEOMETRY_VERSION = "camera-geometry-v1"

SHOT_DISTANCES = {"detail", "intimate_close", "close", "medium", "full_body", "long", "wide"}
CAMERA_HEIGHTS = {"floor", "low", "chest", "eye", "high", "overhead"}
VIEW_AXES = {
    "front",
    "left_three_quarter",
    "right_three_quarter",
    "left_profile",
    "right_profile",
    "rear_three_quarter",
    "over_shoulder",
    "top_down_pov",
    "reflection_oblique",
    "environment_pov",
}
PITCHES = {"upward", "level", "slight_down", "steep_down"}
ROLLS = {"level", "slight_left", "slight_right"}
ORIENTATIONS = {"portrait", "landscape", "square"}
SUBJECT_OCCUPANCIES = {"absent", "trace", "small", "balanced", "dominant", "detail"}
SUBJECT_PLACEMENTS = {
    "not_applicable",
    "center",
    "left_third",
    "right_third",
    "edge_left",
    "edge_right",
    "lower_frame",
    "distributed",
}
ENVIRONMENT_SHARES = {"minimal", "supporting", "balanced", "dominant"}
FOCUS_BEHAVIORS = {"deep_context", "subject_priority", "evidence_priority", "layered_foreground"}
IMPERFECTION_PROFILES = {
    "clean_intentional",
    "casual_offset",
    "partial_crop",
    "motion_trace",
    "foreground_interrupt",
    "focus_breathing",
    "reflection_layer",
}
DEVICE_VISIBILITIES = {
    "out_of_frame",
    "visible_handheld",
    "mirror_visible",
    "fixed_unseen",
    "external_unseen",
    "artifact_inherited",
}

_CAPTURE_DISTANCES = {
    "character_front_camera": {"intimate_close", "close", "medium"},
    "character_rear_camera": SHOT_DISTANCES,
    "mirror": {"close", "medium", "full_body", "long"},
    "timer_fixed": {"close", "medium", "full_body", "long", "wide"},
    "requested_helper": {"close", "medium", "full_body", "long", "wide"},
    "known_companion": {"close", "medium", "full_body", "long", "wide"},
    "external_sender": {"close", "medium", "full_body", "long", "wide"},
    "existing_artifact": SHOT_DISTANCES,
}
_CAPTURE_DEVICES = {
    "character_front_camera": {"out_of_frame", "visible_handheld"},
    "character_rear_camera": {"out_of_frame", "visible_handheld"},
    "mirror": {"mirror_visible"},
    "timer_fixed": {"fixed_unseen"},
    "requested_helper": {"external_unseen"},
    "known_companion": {"external_unseen"},
    "external_sender": {"external_unseen"},
    "existing_artifact": {"artifact_inherited"},
}
_FORM_DISTANCES = {
    "wide_scene": {"medium", "long", "wide"},
    "contextual_still_life": {"detail", "close", "medium"},
    "process_pov": {"detail", "close", "medium"},
    "subject_closeup": {"detail", "close"},
    "result_showcase": {"close", "medium", "wide"},
    "portrait_closeup": {"intimate_close", "close"},
    "portrait_context": {"close", "medium"},
    "full_body": {"full_body", "long"},
    "body_detail": {"detail", "close"},
    "social_frame": {"medium", "long", "wide"},
}
_FORM_OCCUPANCIES = {
    "wide_scene": {"absent", "trace", "small", "balanced"},
    "contextual_still_life": {"absent", "trace"},
    "process_pov": {"absent", "trace", "detail"},
    "subject_closeup": {"dominant", "detail"},
    "result_showcase": {"absent", "trace", "small", "balanced"},
    "portrait_closeup": {"dominant"},
    "portrait_context": {"balanced", "dominant"},
    "full_body": {"balanced", "dominant"},
    "body_detail": {"detail"},
    # Multiple people are distributed by placement; occupancy remains one
    # frame-level amount from the global subject-occupancy vocabulary.
    "social_frame": {"balanced"},
}


@dataclass(frozen=True)
class CameraGeometry:
    shot_distance: str
    camera_height: str
    view_axis: str
    pitch: str
    roll: str
    orientation: str
    subject_occupancy: str
    subject_placement: str
    environment_share: str
    focus_behavior: str
    imperfection_profile: str
    device_visibility: str
    contract_signature: str
    version: str = CAMERA_GEOMETRY_VERSION

    @classmethod
    def create(cls, **values: str) -> "CameraGeometry":
        payload = {name: str(values.get(name) or "") for name in _FIELDS}
        _validate(payload)
        return cls(**payload, contract_signature=_signature(payload))

    def to_payload(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_payload(cls, value: object) -> "CameraGeometry":
        if not isinstance(value, dict):
            raise ValueError("camera geometry must be an object")
        payload = {name: str(value.get(name) or "") for name in _FIELDS}
        _validate(payload)
        if str(value.get("version") or "") != CAMERA_GEOMETRY_VERSION:
            raise ValueError("unsupported camera geometry version")
        if str(value.get("contract_signature") or "") != _signature(payload):
            raise ValueError("invalid camera geometry contract")
        return cls(**payload, contract_signature=str(value["contract_signature"]))

    def compatibility_error(self, *, capture_mode: str, visual_form: str) -> str | None:
        if capture_mode not in _CAPTURE_DISTANCES:
            return "unknown_capture_mode"
        if visual_form not in _FORM_DISTANCES:
            return "unknown_visual_form"
        if self.shot_distance not in _CAPTURE_DISTANCES[capture_mode]:
            return "capture_distance_conflict"
        if self.device_visibility not in _CAPTURE_DEVICES[capture_mode]:
            return "capture_device_conflict"
        if self.shot_distance not in _FORM_DISTANCES[visual_form]:
            return "visual_form_distance_conflict"
        if self.subject_occupancy not in _FORM_OCCUPANCIES[visual_form]:
            return "visual_form_occupancy_conflict"
        if (
            self.subject_occupancy in {"absent", "trace"}
            and self.subject_placement != "not_applicable"
        ):
            return "subject_placement_conflict"
        if (
            self.subject_occupancy not in {"absent", "trace"}
            and self.subject_placement == "not_applicable"
        ):
            return "subject_placement_conflict"
        if capture_mode == "mirror" and self.view_axis != "reflection_oblique":
            return "mirror_axis_conflict"
        if capture_mode == "existing_artifact" and self.imperfection_profile != "clean_intentional":
            return "artifact_geometry_reinterpretation"
        return None


_FIELDS = (
    "shot_distance",
    "camera_height",
    "view_axis",
    "pitch",
    "roll",
    "orientation",
    "subject_occupancy",
    "subject_placement",
    "environment_share",
    "focus_behavior",
    "imperfection_profile",
    "device_visibility",
)
_ENUMS = (
    SHOT_DISTANCES,
    CAMERA_HEIGHTS,
    VIEW_AXES,
    PITCHES,
    ROLLS,
    ORIENTATIONS,
    SUBJECT_OCCUPANCIES,
    SUBJECT_PLACEMENTS,
    ENVIRONMENT_SHARES,
    FOCUS_BEHAVIORS,
    IMPERFECTION_PROFILES,
    DEVICE_VISIBILITIES,
)


def _validate(value: dict[str, str]) -> None:
    for name, allowed in zip(_FIELDS, _ENUMS, strict=True):
        if value[name] not in allowed:
            raise ValueError(f"invalid camera geometry {name}")


def _signature(value: dict[str, str]) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
