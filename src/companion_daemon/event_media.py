"""Event-grounded planning and rendering for personal media.

This module is deliberately independent from the World write model.  Its two
public seams accept frozen values and return values that the World can persist
as External Results.  Neither planning nor rendering writes World state.
"""

from __future__ import annotations

import base64
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import httpx

from companion_daemon.budget import BudgetGate, image_render_estimate
from companion_daemon.image_generation import GeneratedImage, ImageGenerator, visual_reference_paths
from companion_daemon.llm import ChatModel, model_call_scope
from companion_daemon.media_domain import PRIVACY_LEVELS, PRIVACY_RANK as _PRIVACY_RANK
from companion_daemon.media_embodiment import (
    DEFAULT_EMBODIMENT_CONFIG,
    EMBODIED_PRESENTATION_V2,
    EMBODIED_PRESENTATION_V3,
    SENSUAL_CHARGE_LEVELS,
    SENSUAL_CHARGE_RANK,
    EmbodiedPresentation,
    build_embodied_candidates,
    embodied_capture_feasibility_error,
    embodiment_prompt_block,
)
from companion_daemon.media_address import MediaAddressStrategy
from companion_daemon.media_authenticity import (
    PhotographicAuthenticityProfile,
    authenticity_prompt_block,
)
from companion_daemon.media_camera import CameraGeometry
from companion_daemon.media_expression import (
    IdentityReferenceSelection,
    PERCEPTUAL_SIGNATURE_VERSION,
    build_perceptual_signature,
    build_complete_candidates,
)
from companion_daemon.media_interaction import (
    DEFAULT_INTERACTION_CONFIG,
    MediaInteractionBid,
    load_interaction_catalog,
)
from companion_daemon.media_eligibility import (
    CHARGE_RANK as _ELIGIBILITY_CHARGE_RANK,
    FrozenPrivateExpressionBasis,
    MediaEligibilityRouter,
    PrivateExpressionBasis,
)
from companion_daemon.media_moment import MomentCapture
from companion_daemon.media_shot import MediaShotPlan
from companion_daemon.media_subject import (
    DEFAULT_SUBJECT_CONFIG,
    PhotoDisplayStrategy,
    SubjectPresentationPlan,
    SUBJECT_PRESENTATION_V3,
    SUBJECT_PRESENTATION_V4,
    build_subject_candidates,
    capture_hand_feasibility_error,
    load_subject_catalog,
    presentation_prompt_block,
    select_identity_references,
)
from companion_daemon.visual_identity import load_visual_identity


PLAN_VERSION_V1 = "event-media-plan-v1"
PLAN_VERSION_V2 = "event-media-plan-v2"
PLAN_VERSION_V3 = "event-media-plan-v3"
PLAN_VERSION = "event-media-plan-v4"
PLAN_VERSION_V5 = "event-media-plan-v5"
SUPPORTED_PLAN_VERSIONS = {
    PLAN_VERSION_V1,
    PLAN_VERSION_V2,
    PLAN_VERSION_V3,
    PLAN_VERSION,
    PLAN_VERSION_V5,
}
QUALITY_PLAN_VERSIONS = {PLAN_VERSION_V2, PLAN_VERSION_V3, PLAN_VERSION, PLAN_VERSION_V5}
INSPECTION_VERSION = "media-inspection-v6"
INSPECTION_VERSION_V7 = "media-inspection-v7"

FAMILIES = {"life_share", "character_media"}
CONTENT_DOMAINS = {
    "place_environment",
    "food_drink",
    "object_possession",
    "activity_process",
    "outcome_progress",
    "appearance_style",
    "body_health",
    "social_interaction",
    "nature_animal",
    "information_screen",
    "travel_transit",
    "other_grounded",
}
VISUAL_FORMS = {
    "wide_scene",
    "contextual_still_life",
    "process_pov",
    "subject_closeup",
    "result_showcase",
    "portrait_closeup",
    "portrait_context",
    "full_body",
    "body_detail",
    "social_frame",
}
SHARE_INTENTS = {
    "atmosphere",
    "record",
    "show_and_tell",
    "check_in",
    "seek_feedback",
    "progress_update",
    "complain",
    "care_update",
    "humor",
    "intimate_signal",
    "memory_keep",
}
CAPTURE_MODES = {
    "character_front_camera",
    "character_rear_camera",
    "mirror",
    "timer_fixed",
    "requested_helper",
    "known_companion",
    "external_sender",
    "existing_artifact",
}
CHARACTER_VISIBILITIES = {"none", "trace_only", "identifiable", "body_detail"}
OTHER_PEOPLE_VISIBILITIES = {
    "none",
    "anonymous_incidental",
    "known_anonymized",
    "identity_referenced",
}
POLISH_LEVELS = {"raw", "casual", "curated"}
TONES = {
    "neutral",
    "calm",
    "warm",
    "bright",
    "amused",
    "playful",
    "proud",
    "tired",
    "frustrated",
    "embarrassed",
    "tender",
    "vulnerable",
}
INTIMATE_INTENSITIES = {"soft", "tender", "bold"}
ROUTES = {"generate", "reuse_existing"}
DELIVERY_MODES = {"preview", "automatic"}

_LIFE_CAPTURE_MODES = {
    "character_rear_camera",
    "known_companion",
    "external_sender",
    "existing_artifact",
}
_CHARACTER_CAPTURE_MODES = set(CAPTURE_MODES)
_MAX_PLAN_BYTES = 24_000

_LIFE_MATRIX: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "place_environment": (
        frozenset({"wide_scene", "contextual_still_life"}),
        frozenset({"atmosphere", "record", "memory_keep"}),
    ),
    "food_drink": (
        frozenset({"contextual_still_life", "subject_closeup", "process_pov"}),
        frozenset({"show_and_tell", "atmosphere", "record", "complain"}),
    ),
    "object_possession": (
        frozenset({"subject_closeup", "contextual_still_life"}),
        frozenset({"show_and_tell", "seek_feedback", "memory_keep"}),
    ),
    "activity_process": (
        frozenset({"process_pov", "contextual_still_life", "wide_scene"}),
        frozenset({"record", "progress_update", "complain"}),
    ),
    "outcome_progress": (
        frozenset({"result_showcase", "subject_closeup", "wide_scene"}),
        frozenset({"progress_update", "show_and_tell", "memory_keep"}),
    ),
    "travel_transit": (
        frozenset({"wide_scene", "process_pov", "contextual_still_life"}),
        frozenset({"check_in", "atmosphere", "record"}),
    ),
    "nature_animal": (
        frozenset({"wide_scene", "subject_closeup", "process_pov"}),
        frozenset({"atmosphere", "show_and_tell", "humor"}),
    ),
    "information_screen": (
        frozenset({"subject_closeup", "contextual_still_life"}),
        frozenset({"show_and_tell", "progress_update", "care_update"}),
    ),
    "social_interaction": (
        frozenset({"social_frame", "wide_scene", "contextual_still_life"}),
        frozenset({"record", "humor", "memory_keep"}),
    ),
    "other_grounded": (
        frozenset(
            VISUAL_FORMS - {"portrait_closeup", "portrait_context", "full_body", "body_detail"}
        ),
        frozenset(SHARE_INTENTS - {"intimate_signal"}),
    ),
}

_CHARACTER_MATRIX: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "place_environment": (
        frozenset({"portrait_context", "full_body", "wide_scene"}),
        frozenset({"check_in", "record", "memory_keep", "atmosphere"}),
    ),
    "food_drink": (
        frozenset({"portrait_closeup", "portrait_context"}),
        frozenset({"record", "show_and_tell", "complain", "humor"}),
    ),
    "object_possession": (
        frozenset({"body_detail", "subject_closeup", "portrait_context"}),
        frozenset({"show_and_tell", "seek_feedback", "memory_keep"}),
    ),
    "activity_process": (
        frozenset({"portrait_closeup", "portrait_context", "full_body", "social_frame"}),
        frozenset(
            {
                "record",
                "show_and_tell",
                "complain",
                "humor",
                "care_update",
                "memory_keep",
                "intimate_signal",
            }
        ),
    ),
    "outcome_progress": (
        frozenset({"portrait_context", "full_body"}),
        frozenset({"progress_update", "show_and_tell", "memory_keep"}),
    ),
    "appearance_style": (
        frozenset({"portrait_closeup", "portrait_context", "full_body", "body_detail"}),
        frozenset({"show_and_tell", "seek_feedback", "memory_keep", "intimate_signal"}),
    ),
    "body_health": (
        frozenset({"body_detail", "portrait_closeup"}),
        frozenset({"care_update", "complain", "record"}),
    ),
    "social_interaction": (
        frozenset({"social_frame", "portrait_context"}),
        frozenset({"record", "humor", "memory_keep"}),
    ),
    "nature_animal": (
        frozenset({"portrait_closeup", "portrait_context", "full_body"}),
        frozenset({"atmosphere", "show_and_tell", "humor", "memory_keep"}),
    ),
    "information_screen": (
        frozenset({"portrait_context", "body_detail"}),
        frozenset({"show_and_tell", "progress_update", "care_update"}),
    ),
    "travel_transit": (
        frozenset({"portrait_closeup", "portrait_context", "full_body", "wide_scene"}),
        frozenset({"check_in", "record", "atmosphere", "memory_keep"}),
    ),
    "other_grounded": (
        frozenset({"portrait_closeup", "portrait_context", "full_body", "body_detail"}),
        frozenset(SHARE_INTENTS),
    ),
}

_COMPOSITION_DIRECTIONS = frozenset(
    {
        "主体与事件环境同时可辨的自然中近景",
        "让事件环境占主要面积的宽景",
        "突出主证据细节的近景",
        "带少量环境线索的自然人像",
        "轻微偏心构图的生活瞬间",
        "适合朋友观看的轻度摆拍构图",
        "不刻意完美的手机随手构图",
        "完整展示人物与环境关系的全身构图",
    }
)
_ACTION_DIRECTIONS = frozenset(
    {
        "自然地把{primary}带进画面",
        "正在观察{primary}",
        "正在使用{primary}",
        "正在展示{primary}",
        "刚与{primary}互动后的瞬间",
        "一边进行当前活动一边自然看向镜头",
        "围绕{primary}做一个轻松的小姿势",
        "用自然手势指向{primary}",
    }
)
_CAMERA_DIRECTIONS = frozenset(
    {
        "略高于视线的轻微倾斜手机机位",
        "接近视线高度的自然手机机位",
        "稍低机位但不夸张",
        "后摄正常透视且没有自拍臂",
        "固定设备的稳定第三人称视角",
        "镜面反射成立且手机位置自然",
        "同伴手持相机的友好观看距离",
        "他人代拍的自然观看距离",
        "保持原始媒体已有的相机视角",
        "带轻微手机抓拍感但不是偷拍视角",
    }
)
_MOTIVE_DIRECTIONS = frozenset(
    {
        "把这个生活瞬间分享给熟悉的人",
        "记录当时的环境与感受",
        "让对方看看刚发现或获得的东西",
        "说明当前进度或状态",
        "用轻松方式吐槽这个瞬间",
        "征求对方的看法",
        "留下值得记住的画面",
        "传递克制且非露骨的亲密信号",
    }
)
_MODEL_CONSTRAINTS = frozenset(
    {
        "不生成可读文字",
        "手部结构自然",
        "不增加未登记人物",
        "无关人物保持匿名",
        "不把随手照拍成时尚大片",
        "不使用偷拍或狗仔视角",
        "身体与镜面反射符合物理关系",
    }
)
_CAPTURE_DERIVED_CONSTRAINTS: dict[str, tuple[str, ...]] = {
    "character_rear_camera": ("不出现自拍臂",),
    "timer_fixed": ("不出现自拍臂",),
    "requested_helper": ("不出现自拍臂",),
    "known_companion": ("不出现自拍臂",),
    "external_sender": ("不出现自拍臂",),
}
_WORLD_EXPRESSION_CONSTRAINTS = frozenset(
    {
        "整体表现更活泼但不过度表演",
        "保留朋友间会分享的轻度摆拍感",
        "氛围感优先但主体仍清楚",
        "允许轻微搞怪但不丑化角色",
        "保留不刻意完美的生活质感",
        "动作丰富但符合当前活动",
    }
)
_INTERNAL_GROUNDING_CONSTRAINT = (
    "Creative photographic directions cannot add facts beyond the selected evidence."
)
_FROZEN_CONSTRAINTS = (
    _MODEL_CONSTRAINTS
    | _WORLD_EXPRESSION_CONSTRAINTS
    | {item for values in _CAPTURE_DERIVED_CONSTRAINTS.values() for item in values}
    | {_INTERNAL_GROUNDING_CONSTRAINT}
)
_CAPTURE_CAMERA_DIRECTIONS: dict[str, frozenset[str]] = {
    "character_front_camera": frozenset(
        {
            "略高于视线的轻微倾斜手机机位",
            "接近视线高度的自然手机机位",
            "稍低机位但不夸张",
        }
    ),
    "character_rear_camera": frozenset(
        {
            "后摄正常透视且没有自拍臂",
            "接近视线高度的自然手机机位",
            "稍低机位但不夸张",
            "带轻微手机抓拍感但不是偷拍视角",
        }
    ),
    "mirror": frozenset({"镜面反射成立且手机位置自然"}),
    "timer_fixed": frozenset({"固定设备的稳定第三人称视角"}),
    "requested_helper": frozenset({"他人代拍的自然观看距离"}),
    "known_companion": frozenset({"同伴手持相机的友好观看距离", "带轻微手机抓拍感但不是偷拍视角"}),
    "external_sender": frozenset({"他人代拍的自然观看距离", "带轻微手机抓拍感但不是偷拍视角"}),
    "existing_artifact": frozenset({"保持原始媒体已有的相机视角"}),
}
_DEFAULT_CAMERA_DIRECTION = {
    "character_front_camera": "接近视线高度的自然手机机位",
    "character_rear_camera": "后摄正常透视且没有自拍臂",
    "mirror": "镜面反射成立且手机位置自然",
    "timer_fixed": "固定设备的稳定第三人称视角",
    "requested_helper": "他人代拍的自然观看距离",
    "known_companion": "同伴手持相机的友好观看距离",
    "external_sender": "他人代拍的自然观看距离",
    "existing_artifact": "保持原始媒体已有的相机视角",
}


@dataclass(frozen=True)
class MediaOpportunity:
    """One World-selected chance to make media from one committed event."""

    opportunity_id: str
    family: str
    privacy_ceiling: str
    event_snapshot: dict[str, object]
    delivery_mode: str = "preview"
    expression_requirements: tuple[str, ...] = ()
    audience_context: "AudienceContext | None" = None
    sensual_charge_ceiling: str = "none"
    expression_charge_ceiling: str | None = None
    private_expression_basis: PrivateExpressionBasis | None = None


@dataclass(frozen=True)
class AudienceContext:
    """World-frozen audience facts; absence permits only low-intensity ordinary bids."""

    recipient_ref: str = ""
    relationship_stage: str = ""
    public_affect: dict[str, object] | None = None
    display_bounds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MediaPlan:
    """The single replayable photographic interpretation of an opportunity."""

    version: str
    plan_id: str
    opportunity_id: str
    event_id: str
    snapshot_hash: str
    delivery_mode: str
    family: str
    content_domain: str
    visual_form: str
    share_intent: str
    capture_mode: str
    character_visibility: str
    other_people_visibility: str
    polish: str
    tone: str
    privacy: str
    primary_evidence_ref: str
    supporting_evidence_refs: tuple[str, ...]
    evidence_values: dict[str, object]
    composition: str
    action: str
    camera_direction: str
    sharing_motive: str
    constraints: tuple[str, ...]
    route: str
    diversity_fingerprint: str
    planned_summary: str
    intimate_intensity: str | None = None
    existing_artifact_path: str | None = None
    subject_presentation: SubjectPresentationPlan | None = None
    interaction_bid: MediaInteractionBid | None = None
    embodied_presentation: EmbodiedPresentation | None = None
    action_template_id: str | None = None
    action_cue: str | None = None
    media_address_strategy: MediaAddressStrategy | None = None
    camera_geometry: CameraGeometry | None = None
    identity_reference_selection: IdentityReferenceSelection | None = None
    expression_charge_ceiling: str | None = None
    relationship_stage_basis: str | None = None
    photographic_authenticity: PhotographicAuthenticityProfile | None = None
    moment_capture: MomentCapture | None = None
    private_expression_basis: FrozenPrivateExpressionBasis | None = None

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["supporting_evidence_refs"] = list(self.supporting_evidence_refs)
        payload["constraints"] = list(self.constraints)
        payload["subject_presentation"] = (
            self.subject_presentation.to_payload() if self.subject_presentation else None
        )
        payload["interaction_bid"] = (
            self.interaction_bid.to_payload() if self.interaction_bid else None
        )
        payload["embodied_presentation"] = (
            self.embodied_presentation.to_payload() if self.embodied_presentation else None
        )
        if self.version == PLAN_VERSION_V5:
            payload["media_address_strategy"] = (
                self.media_address_strategy.to_payload() if self.media_address_strategy else None
            )
            payload["camera_geometry"] = (
                self.camera_geometry.to_payload() if self.camera_geometry else None
            )
            payload["identity_reference_selection"] = (
                self.identity_reference_selection.to_payload()
                if self.identity_reference_selection
                else None
            )
            if self.photographic_authenticity:
                payload["photographic_authenticity"] = self.photographic_authenticity.to_payload()
            else:
                payload.pop("photographic_authenticity", None)
            if self.moment_capture:
                payload["moment_capture"] = self.moment_capture.to_payload()
            else:
                payload.pop("moment_capture", None)
            payload["private_expression_basis"] = (
                self.private_expression_basis.to_payload()
                if self.private_expression_basis
                else None
            )
            for key in ("composition", "action", "camera_direction", "sharing_motive"):
                payload.pop(key, None)
        else:
            for key in (
                "action_template_id",
                "action_cue",
                "media_address_strategy",
                "camera_geometry",
                "identity_reference_selection",
                "expression_charge_ceiling",
                "relationship_stage_basis",
                "photographic_authenticity",
                "moment_capture",
                "private_expression_basis",
            ):
                payload.pop(key, None)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "MediaPlan":
        if not isinstance(payload, dict):
            raise ValueError("media plan payload must be an object")
        if len(_stable_json(payload).encode("utf-8")) > _MAX_PLAN_BYTES:
            raise ValueError("media plan payload is too large")
        try:
            version = str(payload["version"])
            v5_geometry = (
                CameraGeometry.from_payload(payload["camera_geometry"])
                if version == PLAN_VERSION_V5
                else None
            )
            plan = cls(
                version=version,
                plan_id=str(payload["plan_id"]),
                opportunity_id=str(payload["opportunity_id"]),
                event_id=str(payload["event_id"]),
                snapshot_hash=str(payload["snapshot_hash"]),
                delivery_mode=str(payload["delivery_mode"]),
                family=str(payload["family"]),
                content_domain=str(payload["content_domain"]),
                visual_form=str(payload["visual_form"]),
                share_intent=str(payload["share_intent"]),
                capture_mode=str(payload["capture_mode"]),
                character_visibility=str(payload["character_visibility"]),
                other_people_visibility=str(payload["other_people_visibility"]),
                polish=str(payload["polish"]),
                tone=str(payload["tone"]),
                privacy=str(payload["privacy"]),
                primary_evidence_ref=str(payload["primary_evidence_ref"]),
                supporting_evidence_refs=tuple(
                    str(item) for item in payload.get("supporting_evidence_refs", [])
                ),
                evidence_values=dict(payload.get("evidence_values", {})),
                composition=(
                    _v5_composition(v5_geometry)
                    if v5_geometry is not None
                    else str(payload["composition"])
                ),
                action=(
                    _v5_action_direction(str(payload.get("action_cue") or ""))
                    if version == PLAN_VERSION_V5
                    else str(payload["action"])
                ),
                camera_direction=(
                    _DEFAULT_CAMERA_DIRECTION[str(payload["capture_mode"])]
                    if version == PLAN_VERSION_V5
                    else str(payload["camera_direction"])
                ),
                sharing_motive=(
                    _v5_sharing_motive(str(payload["share_intent"]))
                    if version == PLAN_VERSION_V5
                    else str(payload["sharing_motive"])
                ),
                constraints=tuple(str(item) for item in payload.get("constraints", [])),
                route=str(payload["route"]),
                diversity_fingerprint=str(payload["diversity_fingerprint"]),
                planned_summary=str(payload["planned_summary"]),
                intimate_intensity=(
                    str(payload["intimate_intensity"])
                    if payload.get("intimate_intensity") is not None
                    else None
                ),
                existing_artifact_path=(
                    str(payload["existing_artifact_path"])
                    if payload.get("existing_artifact_path")
                    else None
                ),
                subject_presentation=(
                    SubjectPresentationPlan.from_payload(payload["subject_presentation"])
                    if payload.get("subject_presentation") is not None
                    else None
                ),
                interaction_bid=(
                    MediaInteractionBid.from_payload(payload["interaction_bid"])
                    if payload.get("interaction_bid") is not None
                    else None
                ),
                embodied_presentation=(
                    EmbodiedPresentation.from_payload(payload["embodied_presentation"])
                    if payload.get("embodied_presentation") is not None
                    else None
                ),
                action_template_id=(
                    str(payload["action_template_id"])
                    if payload.get("action_template_id") is not None
                    else None
                ),
                action_cue=(
                    str(payload["action_cue"]) if payload.get("action_cue") is not None else None
                ),
                media_address_strategy=(
                    MediaAddressStrategy.from_payload(payload["media_address_strategy"])
                    if payload.get("media_address_strategy") is not None
                    else None
                ),
                camera_geometry=(
                    v5_geometry if payload.get("camera_geometry") is not None else None
                ),
                identity_reference_selection=(
                    IdentityReferenceSelection.from_payload(payload["identity_reference_selection"])
                    if payload.get("identity_reference_selection") is not None
                    else None
                ),
                expression_charge_ceiling=(
                    str(payload["expression_charge_ceiling"])
                    if payload.get("expression_charge_ceiling") is not None
                    else None
                ),
                relationship_stage_basis=(
                    str(payload["relationship_stage_basis"])
                    if payload.get("relationship_stage_basis") is not None
                    else None
                ),
                photographic_authenticity=(
                    PhotographicAuthenticityProfile.from_payload(
                        payload["photographic_authenticity"]
                    )
                    if payload.get("photographic_authenticity") is not None
                    else None
                ),
                moment_capture=(
                    MomentCapture.from_payload(payload["moment_capture"])
                    if payload.get("moment_capture") is not None
                    else None
                ),
                private_expression_basis=(
                    FrozenPrivateExpressionBasis.from_payload(payload["private_expression_basis"])
                    if payload.get("private_expression_basis") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid media plan payload") from exc
        reason = _validate_frozen_plan(plan)
        if reason:
            raise ValueError(f"invalid media plan payload: {reason}")
        return plan


@dataclass(frozen=True)
class PlannedMedia:
    plan: MediaPlan


@dataclass(frozen=True)
class NotRenderable:
    opportunity_id: str
    reason: str
    details: str = ""


PlanningResult = PlannedMedia | NotRenderable


@dataclass(frozen=True)
class MediaInspection:
    passed: bool
    reason: str
    observed_summary: str
    observed_facts: tuple[str, ...]
    deviations: tuple[str, ...]
    inspector_model: str
    rule_version: str = INSPECTION_VERSION
    observed_subject_presentation: dict[str, object] | None = None
    reference_pose_copy: bool = False
    garment_topology_ok: bool | None = None
    hand_sleeve_occlusion_ok: bool | None = None
    evidence_attachment_ok: bool | None = None
    observed_photo_display_strategy: str | None = None
    display_strategy_broadly_matches: bool | None = None
    expression_artifact_free: bool | None = None
    salient_expression_cues: tuple[str, ...] = ()
    forbidden_expression_cues: tuple[str, ...] = ()
    physical_salience_matches: bool | None = None
    sensual_charge_broadly_matches: bool | None = None
    coverage_mode_matches: bool | None = None
    observed_physical_cues: tuple[str, ...] = ()
    unsupported_physical_cues: tuple[str, ...] = ()
    non_explicit_boundary_ok: bool | None = None
    body_framing_non_fetishizing: bool | None = None
    capture_authorship_matches: bool | None = None
    hand_action_contract_matches: bool | None = None
    social_bid_broadly_legible: bool | None = None
    observed_camera_geometry: dict[str, object] | None = None
    camera_geometry_broadly_matches: bool | None = None
    observed_address_strategy: dict[str, object] | None = None
    address_strategy_broadly_matches: bool | None = None
    interaction_bid_legible: bool | None = None
    capture_relationship_legible: bool | None = None
    generic_portrait_dilution: bool | None = None
    photographic_authenticity_ok: bool | None = None
    identity_consistency_ok: bool | None = None
    observed_expression_family: str | None = None
    perceptual_signature: str | None = None
    observed_facial_display_strategy: str | None = None
    facial_display_strategy_matches: bool | None = None
    observed_facial_actions: dict[str, object] | None = None
    facial_micro_performance_matches: bool | None = None
    generic_smile_fallback: bool | None = None
    reference_expression_copy_detected: bool | None = None
    authenticity_profile_matches: bool | None = None
    commercial_render_dilution: bool | None = None
    regional_grounding_matches: bool | None = None
    observed_authenticity: dict[str, object] | None = None
    moment_capture_matches: bool | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            **asdict(self),
            "observed_facts": list(self.observed_facts),
            "deviations": list(self.deviations),
            "salient_expression_cues": list(self.salient_expression_cues),
            "forbidden_expression_cues": list(self.forbidden_expression_cues),
            "observed_physical_cues": list(self.observed_physical_cues),
            "unsupported_physical_cues": list(self.unsupported_physical_cues),
        }


@dataclass(frozen=True)
class RenderedMedia:
    plan_id: str
    path: Path
    artifact_hash: str
    prompt: str
    attempts: int
    inspection: MediaInspection
    reused_existing: bool = False


@dataclass(frozen=True)
class MediaRenderFailure:
    plan_id: str
    reason: str
    attempts: int
    last_inspection: MediaInspection | None = None


RenderResult = RenderedMedia | MediaRenderFailure


class MediaInspector(Protocol):
    async def inspect(
        self,
        image_path: Path,
        *,
        plan: MediaPlan,
        prompt: str,
        reference_images: Iterable[Path] = (),
    ) -> MediaInspection: ...


class MediaPlanner:
    """Classify one frozen opportunity with one bounded LLM call."""

    def __init__(
        self,
        model: ChatModel,
        *,
        enabled: bool | None = None,
        subject_config_path: Path = DEFAULT_SUBJECT_CONFIG,
        interaction_config_path: Path = DEFAULT_INTERACTION_CONFIG,
        embodiment_config_path: Path = DEFAULT_EMBODIMENT_CONFIG,
        v5_enabled: bool | None = None,
        visual_identity_path: Path = Path("configs/visual_identity.yaml"),
    ):
        self.model = model
        self.enabled = _env_flag("COMPANION_EVENT_MEDIA_ENABLED") if enabled is None else enabled
        self.subject_config_path = subject_config_path
        self.interaction_config_path = interaction_config_path
        self.embodiment_config_path = embodiment_config_path
        self.v5_enabled = (
            _env_flag("COMPANION_EVENT_MEDIA_V5_ENABLED") if v5_enabled is None else v5_enabled
        )
        self.visual_identity_path = visual_identity_path

    async def plan(
        self,
        opportunity: MediaOpportunity,
        recent_media: Sequence[str | MediaPlan | dict[str, object]] = (),
    ) -> PlanningResult:
        if not self.enabled:
            return NotRenderable(opportunity.opportunity_id, "event_media_feature_disabled")
        preflight = _validate_opportunity(opportunity)
        if preflight:
            return NotRenderable(opportunity.opportunity_id, preflight)
        lane = MediaEligibilityRouter().classify(
            family=opportunity.family,
            privacy_ceiling=opportunity.privacy_ceiling,
            expression_charge_ceiling=_expression_charge_ceiling(opportunity),
            event_snapshot=opportunity.event_snapshot,
            private_expression_basis=opportunity.private_expression_basis,
            recipient_ref=(
                opportunity.audience_context.recipient_ref if opportunity.audience_context else ""
            ),
        )
        # Historical v1-v4 plans retain their original replay semantics.  The
        # eligibility lane is a v5 entry rule, not a reinterpretation of them.
        if self.v5_enabled and not lane.allowed:
            return NotRenderable(opportunity.opportunity_id, lane.reason, lane.details)
        recent = tuple(_history_fingerprint(item) for item in recent_media[-12:])
        recent_subjects = tuple(_history_subject_signature(item) for item in recent_media[-12:])
        recent_embodiments = tuple(
            _history_embodiment_signature(item) for item in recent_media[-12:]
        )
        recent_perceptual = tuple(
            signature
            for item in recent_media[-12:]
            if (signature := _history_perceptual_signature(item))
        )
        candidate_opportunity = (
            replace(
                opportunity,
                sensual_charge_ceiling=_expression_charge_ceiling(opportunity),
            )
            if self.v5_enabled
            else opportunity
        )
        try:
            presentation_candidates = _planner_character_candidates(
                candidate_opportunity,
                recent_subjects=recent_subjects,
                recent_embodiments=recent_embodiments,
                subject_config_path=self.subject_config_path,
                embodiment_config_path=self.embodiment_config_path,
                limit=24 if self.v5_enabled else 8,
            )
        except (OSError, ValueError, TypeError) as exc:
            return NotRenderable(
                opportunity.opportunity_id, "presentation_catalog_unavailable", str(exc)[:240]
            )
        try:
            interaction_bids = _planner_interaction_bids(
                candidate_opportunity, config_path=self.interaction_config_path
            )
        except (OSError, ValueError, TypeError) as exc:
            return NotRenderable(
                opportunity.opportunity_id, "interaction_catalog_unavailable", str(exc)[:240]
            )
        complete_candidates = ()
        if self.v5_enabled:
            identity_assets: tuple[str, ...] = ()
            reference_pose_metadata: dict[str, dict[str, str]] = {}
            identity_catalog_version = ""
            if opportunity.family == "character_media":
                try:
                    identity = load_visual_identity(str(self.visual_identity_path))
                    identity_assets = tuple(
                        dict.fromkeys(
                            (
                                *((identity.reference_asset,) if identity.reference_asset else ()),
                                *(
                                    asset
                                    for values in identity.reference_sets.values()
                                    for asset in values
                                ),
                            )
                        )
                    )
                    reference_pose_metadata = load_subject_catalog(
                        self.subject_config_path
                    ).reference_pose_metadata
                    identity_catalog_version = sha256(
                        self.visual_identity_path.read_bytes()
                        + self.subject_config_path.read_bytes()
                    ).hexdigest()[:16]
                except (OSError, TypeError, ValueError) as exc:
                    return NotRenderable(
                        opportunity.opportunity_id,
                        "identity_catalog_unavailable",
                        str(exc)[:240],
                    )
            complete_candidates = tuple(
                payload
                for item in build_complete_candidates(
                    opportunity_id=opportunity.opportunity_id,
                    family=opportunity.family,
                    expression_charge_ceiling=_expression_charge_ceiling(opportunity),
                    presentation_candidates=presentation_candidates,
                    recent_perceptual_signatures=recent_perceptual,
                    identity_assets=identity_assets,
                    reference_pose_metadata=reference_pose_metadata,
                    identity_catalog_version=identity_catalog_version,
                    event_snapshot=opportunity.event_snapshot,
                )
                if _complete_candidate_world_legal((payload := item.planner_payload()), opportunity)
                and (
                    not self.v5_enabled
                    or lane.lane != "private_expression"
                    or _ELIGIBILITY_CHARGE_RANK[
                        str(payload["media_address_strategy"]["expression_charge"])
                    ]
                    >= _ELIGIBILITY_CHARGE_RANK[lane.required_charge]
                )
                and (
                    lane.lane != "private_expression"
                    or payload["legal_capture_modes"] in (["character_front_camera"], ["mirror"])
                )
            )
            if not complete_candidates:
                return NotRenderable(opportunity.opportunity_id, "no_complete_expression_candidate")
        try:
            with model_call_scope(
                "media_planning", action_id=f"media-planning:{opportunity.opportunity_id}"
            ):
                raw = await self.model.complete(
                    (
                        _planning_messages_v5(
                            opportunity, recent, complete_candidates, interaction_bids
                        )
                        if self.v5_enabled
                        else _planning_messages(
                            opportunity, recent, presentation_candidates, interaction_bids
                        )
                    ),
                    temperature=0.65,
                )
            proposal = json.loads(raw)
        except Exception as exc:
            return NotRenderable(opportunity.opportunity_id, "invalid_model_output", str(exc)[:240])
        if not isinstance(proposal, dict):
            return NotRenderable(opportunity.opportunity_id, "invalid_model_output")
        if self.v5_enabled:
            return _freeze_proposal_v5(
                opportunity,
                proposal,
                recent,
                complete_candidates=complete_candidates,
                presentation_candidates=presentation_candidates,
                recent_subjects=recent_subjects,
                recent_embodiments=recent_embodiments,
                subject_config_path=self.subject_config_path,
                interaction_config_path=self.interaction_config_path,
                embodiment_config_path=self.embodiment_config_path,
            )
        return _freeze_proposal(
            opportunity,
            proposal,
            recent,
            recent_subjects=recent_subjects,
            recent_embodiments=recent_embodiments,
            subject_config_path=self.subject_config_path,
            interaction_config_path=self.interaction_config_path,
            embodiment_config_path=self.embodiment_config_path,
        )


class MediaRenderer:
    """Compile, render/reuse, inspect and at most once repair a frozen plan."""

    def __init__(
        self,
        *,
        generator: ImageGenerator | Any | None,
        inspector: MediaInspector,
        output_dir: Path,
        visual_identity_path: Path | None = Path("configs/visual_identity.yaml"),
        subject_config_path: Path = DEFAULT_SUBJECT_CONFIG,
        budget_gate: BudgetGate | None = None,
        size: str = "1024x1536",
        quality: str = "medium",
    ):
        self.generator = generator
        self.inspector = inspector
        self.output_dir = output_dir
        self.visual_identity_path = visual_identity_path
        self.subject_config_path = subject_config_path
        self.budget_gate = budget_gate
        self.size = size
        self.quality = quality

    async def render(self, plan: MediaPlan) -> RenderResult:
        invalid = _validate_frozen_plan(plan)
        if invalid:
            return MediaRenderFailure(plan.plan_id, f"invalid_frozen_plan:{invalid}", 0)
        references = self._references(plan)
        prompt = compile_media_prompt(
            plan,
            self.visual_identity_path,
            subject_config_path=self.subject_config_path,
        )
        if plan.route == "reuse_existing":
            path = Path(plan.existing_artifact_path or "")
            if not path.is_file():
                return MediaRenderFailure(plan.plan_id, "existing_artifact_unavailable", 0)
            return await self._inspect_existing(plan, path, prompt, references)
        if self.generator is None:
            return MediaRenderFailure(plan.plan_id, "image_generator_unavailable", 0)
        estimate = image_render_estimate(
            reference_count=len(references), size=self.size, quality=self.quality, attempts=2
        )
        if (
            self.budget_gate
            and not self.budget_gate.check(
                estimate, automatic=plan.delivery_mode == "automatic"
            ).allowed
        ):
            return MediaRenderFailure(plan.plan_id, "budget_gate_blocked", 0)

        output_path = self.output_dir / f"{_safe_filename(plan.plan_id)}.png"
        active_prompt = prompt
        last_inspection: MediaInspection | None = None
        for attempt in (1, 2):
            try:
                generated = await self._generate(
                    active_prompt, output_path=output_path, references=references
                )
                inspection = await self.inspector.inspect(
                    generated.path,
                    plan=plan,
                    prompt=active_prompt,
                    reference_images=references[:1],
                )
            except Exception as exc:
                return MediaRenderFailure(
                    plan.plan_id, f"render_or_inspection_failed:{exc}", attempt
                )
            inspection = _enforce_inspection_contract(
                inspection,
                automatic=plan.delivery_mode == "automatic",
                subject_required=plan.subject_presentation is not None,
                facial_contract_required=(
                    plan.character_visibility == "identifiable"
                    and plan.subject_presentation is not None
                    and plan.subject_presentation.version == SUBJECT_PRESENTATION_V4
                ),
                quality_required=plan.version in QUALITY_PLAN_VERSIONS,
                social_required=(
                    plan.version in {PLAN_VERSION_V3, PLAN_VERSION, PLAN_VERSION_V5}
                    and plan.subject_presentation is not None
                    and plan.subject_presentation.display_strategy is not None
                ),
                embodied_required=(
                    plan.version in {PLAN_VERSION, PLAN_VERSION_V5}
                    and plan.embodied_presentation is not None
                ),
                capture_contract_required=(
                    plan.embodied_presentation is not None
                    and plan.embodied_presentation.version
                    in {
                        EMBODIED_PRESENTATION_V2,
                        EMBODIED_PRESENTATION_V3,
                    }
                ),
                v5_required=plan.version == PLAN_VERSION_V5,
                enhanced_v5_required=(
                    plan.photographic_authenticity is not None
                    or (
                        plan.subject_presentation is not None
                        and plan.subject_presentation.version == SUBJECT_PRESENTATION_V4
                    )
                ),
                moment_capture_required=plan.moment_capture is not None,
                self_authored_capture_required=plan.private_expression_basis is not None,
            )
            last_inspection = inspection
            if inspection.passed:
                if self.budget_gate:
                    self.budget_gate.record(
                        image_render_estimate(
                            reference_count=len(references),
                            size=self.size,
                            quality=self.quality,
                            attempts=attempt,
                        ),
                        note=f"event_media:{plan.family}:attempts={attempt}",
                    )
                return RenderedMedia(
                    plan_id=plan.plan_id,
                    path=generated.path,
                    artifact_hash=sha256(generated.path.read_bytes()).hexdigest(),
                    prompt=prompt,
                    attempts=attempt,
                    inspection=inspection,
                )
            if attempt == 1:
                active_prompt = _repair_prompt(prompt, inspection)
        return MediaRenderFailure(
            plan.plan_id,
            last_inspection.reason if last_inspection else "inspection_failed",
            2,
            last_inspection,
        )

    async def _inspect_existing(
        self,
        plan: MediaPlan,
        path: Path,
        prompt: str,
        references: tuple[Path, ...],
    ) -> RenderResult:
        try:
            inspection = await self.inspector.inspect(
                path, plan=plan, prompt=prompt, reference_images=references[:1]
            )
        except Exception as exc:
            return MediaRenderFailure(plan.plan_id, f"inspection_failed:{exc}", 0)
        inspection = _enforce_inspection_contract(
            inspection,
            automatic=plan.delivery_mode == "automatic",
            subject_required=plan.subject_presentation is not None,
            facial_contract_required=(
                plan.character_visibility == "identifiable"
                and plan.subject_presentation is not None
                and plan.subject_presentation.version == SUBJECT_PRESENTATION_V4
            ),
            quality_required=plan.version in QUALITY_PLAN_VERSIONS,
            social_required=(
                plan.version in {PLAN_VERSION_V3, PLAN_VERSION, PLAN_VERSION_V5}
                and plan.subject_presentation is not None
                and plan.subject_presentation.display_strategy is not None
            ),
            embodied_required=(
                plan.version in {PLAN_VERSION, PLAN_VERSION_V5}
                and plan.embodied_presentation is not None
            ),
            capture_contract_required=(
                plan.embodied_presentation is not None
                and plan.embodied_presentation.version
                in {
                    EMBODIED_PRESENTATION_V2,
                    EMBODIED_PRESENTATION_V3,
                }
            ),
            v5_required=plan.version == PLAN_VERSION_V5,
            enhanced_v5_required=(
                plan.photographic_authenticity is not None
                or (
                    plan.subject_presentation is not None
                    and plan.subject_presentation.version == SUBJECT_PRESENTATION_V4
                )
            ),
            moment_capture_required=plan.moment_capture is not None,
            self_authored_capture_required=plan.private_expression_basis is not None,
        )
        if not inspection.passed:
            return MediaRenderFailure(plan.plan_id, inspection.reason, 0, inspection)
        return RenderedMedia(
            plan_id=plan.plan_id,
            path=path,
            artifact_hash=sha256(path.read_bytes()).hexdigest(),
            prompt=prompt,
            attempts=0,
            inspection=inspection,
            reused_existing=True,
        )

    def _references(self, plan: MediaPlan) -> tuple[Path, ...]:
        if plan.character_visibility not in {"identifiable", "body_detail"}:
            return ()
        if plan.version == PLAN_VERSION_V5 and plan.identity_reference_selection:
            # v5 freezes geometry-matched assets; expression charge never selects a bedroom pose.
            return tuple(
                path
                for path in (
                    Path(asset_id) for asset_id in plan.identity_reference_selection.asset_ids
                )
                if path.is_file()
            )[:2]
        if (
            plan.version in QUALITY_PLAN_VERSIONS
            and plan.subject_presentation
            and self.visual_identity_path
            and self.visual_identity_path.is_file()
            and self.subject_config_path.is_file()
        ):
            profile = "everyday_selfie"
            relationship_tier = None
            if plan.version == PLAN_VERSION and plan.embodied_presentation:
                profile = {
                    "none": "everyday_selfie",
                    "subtle": "relationship_private",
                    "charged": "relationship_private_bold",
                    "veiled": "relationship_private_bold",
                }[plan.embodied_presentation.sensual_charge]
            elif plan.privacy == "intimate":
                profile = "relationship_private"
                relationship_tier = plan.intimate_intensity
            return select_identity_references(
                identity_path=self.visual_identity_path,
                presentation=plan.subject_presentation,
                subject_config_path=self.subject_config_path,
                profile=profile,
                relationship_tier=relationship_tier,
            )
        fallback_profile = (
            {
                "none": "everyday_selfie",
                "subtle": "relationship_private",
                "charged": "relationship_private_bold",
                "veiled": "relationship_private_bold",
            }[plan.embodied_presentation.sensual_charge]
            if plan.version == PLAN_VERSION and plan.embodied_presentation
            else "relationship_private"
            if plan.privacy == "intimate"
            else "everyday_selfie"
        )
        return visual_reference_paths(
            self.visual_identity_path,
            profile=fallback_profile,
            relationship_tier=(None if plan.version == PLAN_VERSION else plan.intimate_intensity),
            scene_hint=plan.diversity_fingerprint,
        )

    async def _generate(
        self, prompt: str, *, output_path: Path, references: tuple[Path, ...]
    ) -> GeneratedImage:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            return await self.generator.generate(
                prompt,
                output_path=output_path,
                size=self.size,
                quality=self.quality,
                reference_images=references,
            )
        except TypeError as exc:
            if "quality" not in str(exc):
                raise
        try:
            return await self.generator.generate(
                prompt,
                output_path=output_path,
                size=self.size,
                reference_images=references,
            )
        except TypeError as exc:
            if "reference_images" not in str(exc):
                raise
            return await self.generator.generate(prompt, output_path=output_path, size=self.size)


class OpenAIMediaInspector:
    """One visual call that both gates delivery and describes the actual image."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini",
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.proxy_url = proxy_url
        self.transport = transport

    async def inspect(
        self,
        image_path: Path,
        *,
        plan: MediaPlan,
        prompt: str,
        reference_images: Iterable[Path] = (),
    ) -> MediaInspection:
        content: list[dict[str, object]] = [{"type": "text", "text": _inspection_prompt(plan)}]
        content.append(_image_content(image_path, "Generated or reused media"))
        reference = next(iter(reference_images), None)
        if reference and reference.is_file():
            content.append({"type": "text", "text": "Fictional character identity reference:"})
            content.append(_image_content(reference, "Identity reference"))
        request = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": content}],
        }
        options: dict[str, object] = {
            "timeout": 45,
            "trust_env": False,
            "transport": self.transport,
        }
        if self.proxy_url:
            options["proxy"] = self.proxy_url
        async with httpx.AsyncClient(**options) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=request,
            )
            response.raise_for_status()
        payload = json.loads(response.json()["choices"][0]["message"]["content"])
        if not isinstance(payload, dict):
            raise ValueError("inspector returned a non-object")
        return MediaInspection(
            passed=bool(payload.get("passed")),
            reason=str(payload.get("reason") or "unspecified"),
            observed_summary=str(payload.get("observed_summary") or "").strip(),
            observed_facts=tuple(str(item) for item in payload.get("observed_facts", [])[:12]),
            deviations=tuple(str(item) for item in payload.get("deviations", [])[:12]),
            inspector_model=self.model,
            rule_version=(
                INSPECTION_VERSION_V7 if plan.version == PLAN_VERSION_V5 else INSPECTION_VERSION
            ),
            observed_subject_presentation=(
                dict(payload["observed_subject_presentation"])
                if isinstance(payload.get("observed_subject_presentation"), dict)
                else None
            ),
            reference_pose_copy=_optional_bool(payload, "reference_pose_copy") is True,
            garment_topology_ok=_optional_bool(payload, "garment_topology_ok"),
            hand_sleeve_occlusion_ok=_optional_bool(payload, "hand_sleeve_occlusion_ok"),
            evidence_attachment_ok=_optional_bool(payload, "evidence_attachment_ok"),
            observed_photo_display_strategy=(
                str(payload.get("observed_photo_display_strategy") or "").strip() or None
            ),
            display_strategy_broadly_matches=_optional_bool(
                payload, "display_strategy_broadly_matches"
            ),
            expression_artifact_free=_optional_bool(payload, "expression_artifact_free"),
            salient_expression_cues=tuple(
                str(item) for item in payload.get("salient_expression_cues", [])[:12]
            ),
            forbidden_expression_cues=tuple(
                str(item) for item in payload.get("forbidden_expression_cues", [])[:12]
            ),
            physical_salience_matches=_optional_bool(payload, "physical_salience_matches"),
            sensual_charge_broadly_matches=_optional_bool(
                payload, "sensual_charge_broadly_matches"
            ),
            coverage_mode_matches=_optional_bool(payload, "coverage_mode_matches"),
            observed_physical_cues=tuple(
                str(item) for item in payload.get("observed_physical_cues", [])[:12]
            ),
            unsupported_physical_cues=tuple(
                str(item) for item in payload.get("unsupported_physical_cues", [])[:12]
            ),
            non_explicit_boundary_ok=_optional_bool(payload, "non_explicit_boundary_ok"),
            body_framing_non_fetishizing=_optional_bool(payload, "body_framing_non_fetishizing"),
            capture_authorship_matches=_optional_bool(payload, "capture_authorship_matches"),
            hand_action_contract_matches=_optional_bool(payload, "hand_action_contract_matches"),
            social_bid_broadly_legible=_optional_bool(payload, "social_bid_broadly_legible"),
            observed_camera_geometry=(
                dict(payload["observed_camera_geometry"])
                if isinstance(payload.get("observed_camera_geometry"), dict)
                else None
            ),
            camera_geometry_broadly_matches=_optional_bool(
                payload, "camera_geometry_broadly_matches"
            ),
            observed_address_strategy=(
                dict(payload["observed_address_strategy"])
                if isinstance(payload.get("observed_address_strategy"), dict)
                else None
            ),
            address_strategy_broadly_matches=_optional_bool(
                payload, "address_strategy_broadly_matches"
            ),
            interaction_bid_legible=_optional_bool(payload, "interaction_bid_legible"),
            capture_relationship_legible=_optional_bool(payload, "capture_relationship_legible"),
            generic_portrait_dilution=_optional_bool(payload, "generic_portrait_dilution"),
            photographic_authenticity_ok=_optional_bool(payload, "photographic_authenticity_ok"),
            identity_consistency_ok=_optional_bool(payload, "identity_consistency_ok"),
            observed_expression_family=(
                str(payload.get("observed_expression_family") or "").strip() or None
            ),
            perceptual_signature=(str(payload.get("perceptual_signature") or "").strip() or None),
            observed_facial_display_strategy=(
                str(payload.get("observed_facial_display_strategy") or "").strip() or None
            ),
            facial_display_strategy_matches=_optional_bool(
                payload, "facial_display_strategy_matches"
            ),
            observed_facial_actions=(
                dict(payload["observed_facial_actions"])
                if isinstance(payload.get("observed_facial_actions"), dict)
                else None
            ),
            facial_micro_performance_matches=_optional_bool(
                payload, "facial_micro_performance_matches"
            ),
            generic_smile_fallback=_optional_bool(payload, "generic_smile_fallback"),
            reference_expression_copy_detected=_optional_bool(
                payload, "reference_expression_copy_detected"
            ),
            authenticity_profile_matches=_optional_bool(payload, "authenticity_profile_matches"),
            commercial_render_dilution=_optional_bool(payload, "commercial_render_dilution"),
            regional_grounding_matches=_optional_bool(payload, "regional_grounding_matches"),
            observed_authenticity=(
                dict(payload["observed_authenticity"])
                if isinstance(payload.get("observed_authenticity"), dict)
                else None
            ),
            moment_capture_matches=_optional_bool(payload, "moment_capture_matches"),
        )


class LegacyMediaShotAdapter:
    """Map recoverable MediaShotPlan v1-v3 payloads into the new renderer seam."""

    @staticmethod
    def adapt(
        shot_plan: MediaShotPlan,
        *,
        opportunity_id: str,
        event_id: str,
        delivery_mode: str = "preview",
    ) -> MediaPlan:
        capture = {
            "handheld_selfie": "character_front_camera",
            "check_in_timer": "timer_fixed",
            "check_in_helper": "requested_helper",
            "mirror": "mirror",
            "candid_life": "known_companion",
            "unfiltered": "character_front_camera",
        }.get(shot_plan.capture_mode, "character_front_camera")
        intent = "intimate_signal" if shot_plan.media_kind == "relationship_private" else "record"
        privacy = "intimate" if intent == "intimate_signal" else "personal"
        content_domain = "appearance_style" if privacy == "intimate" else "activity_process"
        evidence = {
            "/legacy/location": shot_plan.location or "未断言具体地点",
            "/legacy/action": shot_plan.action,
            "/legacy/environment": list(shot_plan.environment_cues),
        }
        if shot_plan.companions:
            evidence["/participants/0/id"] = shot_plan.companions[0]
        fingerprint = "|".join(
            (
                "character_media",
                content_domain,
                "portrait_context",
                intent,
                capture,
                "identifiable",
                "casual",
                "tender" if privacy == "intimate" else "warm",
            )
        )
        return MediaPlan(
            version=PLAN_VERSION_V1,
            plan_id=f"event-plan:{opportunity_id}",
            opportunity_id=opportunity_id,
            event_id=event_id,
            snapshot_hash=sha256(_stable_json(shot_plan.to_payload()).encode()).hexdigest(),
            delivery_mode=delivery_mode,
            family="character_media",
            content_domain=content_domain,
            visual_form="portrait_context",
            share_intent=intent,
            capture_mode=capture,
            character_visibility="identifiable",
            other_people_visibility="none",
            polish="raw" if shot_plan.capture_mode == "unfiltered" else "casual",
            tone="tender" if privacy == "intimate" else "warm",
            privacy=privacy,
            primary_evidence_ref="/legacy/action",
            supporting_evidence_refs=tuple(item for item in evidence if item != "/legacy/action"),
            evidence_values=evidence,
            composition="带少量环境线索的自然人像",
            action="自然地把{primary}带进画面",
            camera_direction=_DEFAULT_CAMERA_DIRECTION[capture],
            sharing_motive=(
                "传递克制且非露骨的亲密信号"
                if privacy == "intimate"
                else "把这个生活瞬间分享给熟悉的人"
            ),
            constraints=tuple(
                dict.fromkeys(
                    (
                        "不生成可读文字",
                        "手部结构自然",
                        *(() if capture == "character_front_camera" else ("不出现自拍臂",)),
                        _INTERNAL_GROUNDING_CONSTRAINT,
                    )
                )
            ),
            route="generate",
            diversity_fingerprint=fingerprint,
            planned_summary=f"{shot_plan.scene_category}中的{shot_plan.action}",
            intimate_intensity=shot_plan.relationship_tier if privacy == "intimate" else None,
        )


def compile_media_prompt(
    plan: MediaPlan,
    visual_identity_path: Path | None,
    *,
    subject_config_path: Path = DEFAULT_SUBJECT_CONFIG,
) -> str:
    """Compile only frozen, selected evidence; never reopen the World snapshot."""
    if plan.version == PLAN_VERSION_V5:
        return _compile_media_prompt_v5(
            plan,
            visual_identity_path,
            subject_config_path=subject_config_path,
        )
    evidence = "\n".join(
        f"- {pointer}: {_compact_value(value)}" for pointer, value in plan.evidence_values.items()
    )
    resolved_action = plan.action.replace(
        "{primary}", _compact_value(plan.evidence_values[plan.primary_evidence_ref])
    )
    identity = ""
    if (
        plan.character_visibility in {"identifiable", "body_detail"}
        and visual_identity_path
        and visual_identity_path.is_file()
    ):
        profile = "relationship_private" if plan.privacy == "intimate" else "everyday_selfie"
        identity = "\n" + load_visual_identity(str(visual_identity_path)).prompt_block(
            relationship_tier=(
                plan.intimate_intensity
                if plan.version != PLAN_VERSION and profile == "relationship_private"
                else None
            )
        )
    people = {
        "none": "No other person is visible.",
        "anonymous_incidental": "Incidental people may appear only generic, obscured or out of focus.",
        "known_anonymized": "Known companions may appear naturally but without a stable identifiable face.",
        "identity_referenced": "Render only people supported by explicit identity references.",
    }[plan.other_people_visibility]
    privacy = (
        "Adult fictional character only; flirtatious but non-explicit, key areas covered, no sexual act."
        if plan.privacy == "intimate"
        else "No sexualized or intimate escalation."
    )
    subject = (
        "\n"
        + presentation_prompt_block(
            plan.subject_presentation,
            config_path=subject_config_path,
        )
        if plan.subject_presentation
        else ""
    )
    interaction = (
        "\nSocial invitation: "
        f"communicative goal={plan.interaction_bid.communicative_goal}; "
        f"hoped response={plan.interaction_bid.hoped_response}; "
        f"response pressure={plan.interaction_bid.response_pressure}. "
        "Let this shape the photograph subtly; do not add text or imply that the response already occurred."
        if plan.interaction_bid
        else ""
    )
    embodiment = (
        "\n" + embodiment_prompt_block(plan.embodied_presentation)
        if plan.embodied_presentation
        else ""
    )
    complete_contract = ""
    if (
        plan.subject_presentation
        and plan.embodied_presentation
        and plan.embodied_presentation.version == EMBODIED_PRESENTATION_V2
    ):
        performance = plan.subject_presentation.performance
        bid = plan.interaction_bid.communicative_goal if plan.interaction_bid else "none"
        display = (
            plan.subject_presentation.display_strategy.strategy_id
            if plan.subject_presentation.display_strategy
            else "none"
        )
        complete_contract = (
            "\nComplete character-photo contract (these dimensions must coexist in one image): "
            f"capture_mode={plan.capture_mode}; camera_authorship={_camera_authorship(plan.capture_mode)}; "
            f"hand_occupancy={performance.hand_occupancy}; "
            f"body_action_variant={plan.embodied_presentation.action_variant_id}; "
            f"required_free_hands={plan.embodied_presentation.required_free_hands}; "
            f"photo_display_strategy={display}; interaction_bid={bid}. "
            "Never render an unseen extra camera operator, an impossible free hand, or an action that "
            "erases the recipient-directed social purpose."
        )
    return (
        "Create one believable fictional personal-media photograph. No text or watermark.\n"
        f"Frozen plan={plan.plan_id}; event={plan.event_id}; family={plan.family}.\n"
        f"Classification: domain={plan.content_domain}; form={plan.visual_form}; "
        f"intent={plan.share_intent}; capture={plan.capture_mode}; polish={plan.polish}; "
        f"tone={plan.tone}; privacy={plan.privacy}.\n"
        f"Composition: {plan.composition}. Action: {resolved_action}. Camera: {plan.camera_direction}.\n"
        f"Reason this feels shareable: {plan.sharing_motive}.\n"
        f"Selected event evidence only:\n{evidence}\n"
        f"People rule: {people}\nPrivacy rule: {privacy}\n"
        f"Non-negotiable constraints: {'; '.join(plan.constraints) or 'stay faithful to selected evidence'}."
        f"{identity}"
        f"{interaction}"
        f"{subject}"
        f"{embodiment}"
        f"{complete_contract}"
    )


def _compile_media_prompt_v5(
    plan: MediaPlan,
    visual_identity_path: Path | None,
    *,
    subject_config_path: Path,
) -> str:
    assert plan.media_address_strategy is not None
    assert plan.camera_geometry is not None
    evidence = "\n".join(
        f"- {pointer}: {_compact_value(value)}" for pointer, value in plan.evidence_values.items()
    )
    address = plan.media_address_strategy
    if plan.interaction_bid is None:
        return "missing_interaction_bid"
    bid_address_error = address.bid_compatibility_error(plan.interaction_bid.communicative_goal)
    if bid_address_error:
        return bid_address_error
    camera = plan.camera_geometry
    identity = "Identity Reference Responsibilities: no character identity reference is used."
    if plan.identity_reference_selection:
        roles = "; ".join(plan.identity_reference_selection.roles)
        identity = (
            f"Identity Reference Responsibilities: {roles}. References define identity or broad geometry "
            "only; never copy their hairstyle, smile, head tilt, pose, wardrobe, or framing."
        )
    identity_anchor = ""
    if (
        plan.character_visibility in {"identifiable", "body_detail"}
        and visual_identity_path
        and visual_identity_path.is_file()
    ):
        identity_anchor = "\n" + load_visual_identity(str(visual_identity_path)).prompt_block()
    subject = (
        presentation_prompt_block(plan.subject_presentation, config_path=subject_config_path)
        if plan.subject_presentation
        else "No identifiable character is the visual subject; only grounded non-identifying traces may appear."
    )
    embodiment = (
        embodiment_prompt_block(plan.embodied_presentation)
        if plan.embodied_presentation
        else "No invented bodily state, private apparel, or character action."
    )
    authenticity = (
        authenticity_prompt_block(plan.photographic_authenticity)
        if plan.photographic_authenticity
        else ""
    )
    portrait_authenticity = (
        "Character-photo realism: preserve ordinary, shot-specific skin and facial asymmetry; keep "
        "the selected expression beat and moment readable. Do not apply global beauty-retouching, "
        "studio key-light symmetry, immaculate background cleanup, mannequin smoothness, or a fashion-"
        "editorial pose unless selected event evidence explicitly requires it. Any face/eye correction is "
        "local and must preserve the frozen identity, camera geometry and facial contract."
        if plan.character_visibility in {"identifiable", "body_detail"}
        else ""
    )
    people = {
        "none": "No other person is visible.",
        "anonymous_incidental": "Incidental people remain generic, obscured, or out of focus.",
        "known_anonymized": "Known companions may appear without a stable identifiable face.",
        "identity_referenced": "Only explicitly referenced identities may be recognizable.",
    }[plan.other_people_visibility]
    privacy = (
        "Adult fictional character only; strong attraction may be legible, but coverage stays opaque and complete, with no sexual act or fetish crop."
        if plan.privacy == "intimate"
        else "Do not escalate the scene into intimate or sexualized content."
    )
    attraction = (
        f"; attraction_mechanism={address.attraction_mechanism}"
        if address.attraction_mechanism
        else ""
    )
    moment = (
        "Moment Capture: "
        f"mode={plan.moment_capture.moment_mode}; "
        f"camera_relation={plan.moment_capture.camera_relation}; "
        f"scene_anchor={plan.moment_capture.scene_anchor}; "
        f"continuity={plan.moment_capture.continuity_cue}; "
        f"anti_static={plan.moment_capture.anti_static_direction}.\n"
        if plan.moment_capture
        else ""
    )
    capture_physics = {
        "character_front_camera": (
            "Self-authorship invariant: the character is operating the front-facing phone herself. "
            "Make that physically legible with a credible foreground hand or forearm operating the phone, or a "
            "partial device edge. It may be naturally cropped, but this evidence cannot disappear completely. "
            "No third photographer, tripod, or authorless portrait viewpoint."
        ),
        "mirror": (
            "Self-authorship invariant: the phone is visibly reflected and held by the character in the mirror; "
            "the reflected hand, device, pose and camera angle must agree. No third photographer or impossible "
            "extra device."
        ),
    }.get(plan.capture_mode, "Capture authorship must remain physically visible and coherent.")
    private_basis = (
        "Private-expression proof: "
        f"kind={plan.private_expression_basis.kind}; "
        f"evidence={plan.private_expression_basis.evidence_ref}: "
        f"{_compact_value(plan.private_expression_basis.evidence_value)}; "
        f"minimum_charge={plan.private_expression_basis.required_charge}. "
        "This recipient-directed private moment must remain grounded in that proof; do not replace it "
        "with a generic glamour portrait or invent a different private situation.\n"
        if plan.private_expression_basis
        else ""
    )
    return (
        "Create one believable fictional personal-media photograph. No text or watermark.\n"
        f"Frozen MediaPlan v5={plan.plan_id}; event={plan.event_id}; family={plan.family}.\n"
        f"Classification and Action: domain={plan.content_domain}; form={plan.visual_form}; "
        f"intent={plan.share_intent}; polish={plan.polish}; tone={plan.tone}; "
        f"action_template={plan.action_template_id}; action={plan.action_cue}.\n"
        f"Selected event evidence:\n{evidence}\n"
        f"{private_basis}"
        f"Interaction Bid: goal={plan.interaction_bid.communicative_goal if plan.interaction_bid else 'none'}; "
        f"hoped_response={plan.interaction_bid.hoped_response if plan.interaction_bid else 'none'}; "
        f"pressure={plan.interaction_bid.response_pressure if plan.interaction_bid else 'none'}.\n"
        f"Media Address Strategy: address={address.address_mode}; tactic={address.engagement_tactic}; "
        f"disclosure={address.disclosure_mode}; staging={address.staging_degree}; "
        f"temporal_beat={address.temporal_beat}; visual_priority={address.visual_priority}; "
        f"expression_charge={address.expression_charge}{attraction}.\n"
        f"Camera Geometry: capture_author={_camera_authorship(plan.capture_mode)}; "
        f"distance={camera.shot_distance}; height={camera.camera_height}; view_axis={camera.view_axis}; "
        f"camera_face_distance={camera.camera_face_distance}; "
        f"face_radial_position={camera.face_radial_position}; "
        f"pitch={camera.pitch}; roll={camera.roll}; orientation={camera.orientation}; "
        f"subject_occupancy={camera.subject_occupancy}; placement={camera.subject_placement}; "
        f"environment_share={camera.environment_share}; focus={camera.focus_behavior}; "
        f"imperfection={camera.imperfection_profile}; device={camera.device_visibility}.\n"
        f"Capture Physics: {capture_physics}\n"
        f"{moment}"
        f"{authenticity + chr(10) if authenticity else ''}"
        f"{portrait_authenticity + chr(10) if portrait_authenticity else ''}"
        f"{identity}{identity_anchor}\n"
        f"{subject}\n{embodiment}\n"
        f"People rule: {people}\nPrivacy rule: {privacy}\n"
        f"Constraints: {'; '.join(plan.constraints) or 'use selected evidence only'}."
    )


def _freeze_proposal(
    opportunity: MediaOpportunity,
    proposal: dict[str, object],
    recent: tuple[str, ...],
    *,
    recent_subjects: tuple[str, ...] = (),
    recent_embodiments: tuple[str, ...] = (),
    subject_config_path: Path = DEFAULT_SUBJECT_CONFIG,
    interaction_config_path: Path = DEFAULT_INTERACTION_CONFIG,
    embodiment_config_path: Path = DEFAULT_EMBODIMENT_CONFIG,
    presentation_candidate_limit: int = 8,
    frozen_presentation_candidates: tuple[dict[str, object], ...] | None = None,
) -> PlanningResult:
    fields = {
        "content_domain": CONTENT_DOMAINS,
        "visual_form": VISUAL_FORMS,
        "share_intent": SHARE_INTENTS,
        "capture_mode": CAPTURE_MODES,
        "character_visibility": CHARACTER_VISIBILITIES,
        "other_people_visibility": OTHER_PEOPLE_VISIBILITIES,
        "polish": POLISH_LEVELS,
        "tone": TONES,
        "privacy": PRIVACY_LEVELS,
        "route": ROUTES,
    }
    values: dict[str, str] = {}
    for name, allowed in fields.items():
        value = proposal.get(name)
        if not isinstance(value, str) or value not in allowed:
            return NotRenderable(opportunity.opportunity_id, "invalid_classification", name)
        values[name] = value
    primary = proposal.get("primary_evidence_ref")
    supporting = proposal.get("supporting_evidence_refs", [])
    if not isinstance(primary, str) or not primary.startswith("/"):
        return NotRenderable(opportunity.opportunity_id, "invalid_primary_evidence_ref")
    if not isinstance(supporting, list) or any(not isinstance(item, str) for item in supporting):
        return NotRenderable(opportunity.opportunity_id, "invalid_supporting_evidence_refs")
    if len(supporting) > 8:
        return NotRenderable(opportunity.opportunity_id, "too_many_supporting_evidence_refs")
    pointers = [primary, *supporting]
    if len(set(pointers)) != len(pointers):
        return NotRenderable(opportunity.opportunity_id, "duplicate_evidence_ref")
    evidence: dict[str, object] = {}
    try:
        for pointer in pointers:
            evidence[pointer] = _resolve_pointer(opportunity.event_snapshot, pointer)
    except (KeyError, IndexError, TypeError, ValueError):
        return NotRenderable(opportunity.opportunity_id, "unknown_evidence_ref")
    for text_field in (
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
    ):
        if not isinstance(proposal.get(text_field), str) or not str(proposal[text_field]).strip():
            return NotRenderable(opportunity.opportunity_id, "invalid_model_output", text_field)
    direction_texts = tuple(
        str(proposal[name]).strip()
        for name in ("composition", "action", "camera_direction", "sharing_motive")
    )
    ungrounded = _unselected_fact_mentioned(opportunity.event_snapshot, pointers, direction_texts)
    if ungrounded:
        return NotRenderable(opportunity.opportunity_id, "unselected_fact_in_direction", ungrounded)
    constraints = proposal.get("constraints", [])
    if not isinstance(constraints, list) or any(not isinstance(item, str) for item in constraints):
        return NotRenderable(opportunity.opportunity_id, "invalid_model_output", "constraints")
    if len(constraints) + len(opportunity.expression_requirements) > 12:
        return NotRenderable(opportunity.opportunity_id, "too_many_constraints")
    if proposal.get("intimate_intensity") is not None:
        return NotRenderable(opportunity.opportunity_id, "legacy_intimate_intensity_in_v4")

    bid_id = proposal.get("interaction_bid_id")
    if not isinstance(bid_id, str) or not bid_id:
        return NotRenderable(opportunity.opportunity_id, "missing_interaction_bid")
    legal_bids = _interaction_bid_values(opportunity, config_path=interaction_config_path)
    bid_values = legal_bids.get(bid_id)
    if bid_values is None:
        return NotRenderable(opportunity.opportunity_id, "illegal_interaction_bid")
    interaction_bid = MediaInteractionBid.create(
        bid_id=f"media-bid:{opportunity.opportunity_id}",
        communicative_goal=bid_id,
        hoped_response=str(bid_values["hoped_response"]),
        response_pressure=str(bid_values["response_pressure"]),
        audience_ref=(
            opportunity.audience_context.recipient_ref
            if opportunity.audience_context is not None
            else ""
        ),
        minimum_privacy=str(bid_values.get("minimum_privacy") or "ordinary"),
    )
    if _PRIVACY_RANK[interaction_bid.minimum_privacy] > _PRIVACY_RANK[values["privacy"]]:
        return NotRenderable(opportunity.opportunity_id, "interaction_bid_privacy_conflict")

    reason = _validate_combination(opportunity, values, primary, pointers)
    if reason:
        return NotRenderable(opportunity.opportunity_id, reason)
    direction_error = _validate_direction_catalog(proposal, values)
    if direction_error:
        return NotRenderable(opportunity.opportunity_id, direction_error)
    existing_path = _selected_existing_path(opportunity.event_snapshot, evidence)
    subject_presentation: SubjectPresentationPlan | None = None
    embodied_presentation: EmbodiedPresentation | None = None
    if opportunity.family == "character_media" and values["route"] == "generate":
        candidate_id = proposal.get("presentation_candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            return NotRenderable(opportunity.opportunity_id, "missing_presentation_candidate")
        legal_candidates = frozen_presentation_candidates or _planner_character_candidates(
            opportunity,
            recent_subjects=recent_subjects,
            recent_embodiments=recent_embodiments,
            subject_config_path=subject_config_path,
            embodiment_config_path=embodiment_config_path,
            limit=presentation_candidate_limit,
        )
        selected = next(
            (
                item
                for item in legal_candidates
                if item["presentation_candidate_id"] == candidate_id
            ),
            None,
        )
        if selected is None:
            return NotRenderable(opportunity.opportunity_id, "illegal_presentation_candidate")
        modes = selected.get("legal_capture_modes", [])
        intents = selected.get("legal_share_intents", [])
        if values["capture_mode"] not in modes:
            return NotRenderable(opportunity.opportunity_id, "presentation_capture_conflict")
        if values["share_intent"] not in intents:
            return NotRenderable(opportunity.opportunity_id, "presentation_intent_conflict")
        if selected.get("character_visibility") != values["character_visibility"]:
            return NotRenderable(opportunity.opportunity_id, "presentation_visibility_conflict")
        subject_presentation = SubjectPresentationPlan.from_payload(
            selected["subject_presentation"]
        )
        embodied_presentation = EmbodiedPresentation.from_payload(selected["embodied_presentation"])
        physical_evidence_refs = {
            pointer for cue in embodied_presentation.physical_cues for pointer in cue.evidence_refs
        }
        if any(pointer not in evidence for pointer in physical_evidence_refs):
            return NotRenderable(opportunity.opportunity_id, "unselected_physical_state_evidence")
        if any(pointer not in evidence for pointer in embodied_presentation.wardrobe_evidence_refs):
            return NotRenderable(opportunity.opportunity_id, "unselected_wardrobe_evidence")
        strategy = subject_presentation.display_strategy
        if strategy and interaction_bid.communicative_goal not in strategy.communicative_goals:
            return NotRenderable(opportunity.opportunity_id, "subject_interaction_bid_conflict")
        bid_error = _embodiment_bid_error(embodied_presentation, interaction_bid)
        if bid_error:
            return NotRenderable(opportunity.opportunity_id, bid_error)
    fingerprint_parts = (
        opportunity.family,
        values["content_domain"],
        values["visual_form"],
        values["share_intent"],
        values["capture_mode"],
        values["character_visibility"],
        values["polish"],
        values["tone"],
    )
    if embodied_presentation:
        fingerprint_parts += (
            embodied_presentation.physical_salience,
            embodied_presentation.sensual_charge,
            embodied_presentation.coverage_mode,
            embodied_presentation.body_strategy_id,
        )
        if embodied_presentation.version == EMBODIED_PRESENTATION_V2:
            fingerprint_parts += (embodied_presentation.action_variant_id,)
    fingerprint = "|".join(fingerprint_parts)
    if fingerprint in recent[-12:]:
        return NotRenderable(opportunity.opportunity_id, "duplicate_recent_fingerprint")
    event = _mapping(opportunity.event_snapshot.get("event"))
    plan = MediaPlan(
        version=PLAN_VERSION,
        plan_id=f"event-plan:{opportunity.opportunity_id}",
        opportunity_id=opportunity.opportunity_id,
        event_id=str(event.get("event_id")),
        snapshot_hash=sha256(_stable_json(opportunity.event_snapshot).encode()).hexdigest(),
        delivery_mode=opportunity.delivery_mode,
        family=opportunity.family,
        content_domain=values["content_domain"],
        visual_form=values["visual_form"],
        share_intent=values["share_intent"],
        capture_mode=values["capture_mode"],
        character_visibility=values["character_visibility"],
        other_people_visibility=values["other_people_visibility"],
        polish=values["polish"],
        tone=values["tone"],
        privacy=values["privacy"],
        primary_evidence_ref=primary,
        supporting_evidence_refs=tuple(supporting),
        evidence_values=evidence,
        composition=str(proposal["composition"]).strip()[:600],
        action=str(proposal["action"]).strip()[:400],
        camera_direction=str(proposal["camera_direction"]).strip()[:400],
        sharing_motive=str(proposal["sharing_motive"]).strip()[:400],
        constraints=tuple(
            dict.fromkeys(
                (
                    *(str(item).strip()[:300] for item in constraints if item.strip()),
                    *(
                        item.strip()[:300]
                        for item in opportunity.expression_requirements
                        if item.strip()
                    ),
                    *_CAPTURE_DERIVED_CONSTRAINTS.get(values["capture_mode"], ()),
                    _INTERNAL_GROUNDING_CONSTRAINT,
                )
            )
        ),
        route=values["route"],
        diversity_fingerprint=fingerprint,
        planned_summary=(
            f"{values['share_intent']}：{_compact_value(evidence[primary])}；"
            f"{str(proposal['action']).strip().replace('{primary}', _compact_value(evidence[primary]))}"
        )[:600],
        intimate_intensity=None,
        existing_artifact_path=existing_path if values["route"] == "reuse_existing" else None,
        subject_presentation=subject_presentation,
        interaction_bid=interaction_bid,
        embodied_presentation=embodied_presentation,
    )
    frozen_error = _validate_frozen_plan(plan)
    if frozen_error:
        return NotRenderable(opportunity.opportunity_id, frozen_error)
    if len(_stable_json(plan.to_payload()).encode("utf-8")) > _MAX_PLAN_BYTES:
        return NotRenderable(opportunity.opportunity_id, "media_plan_too_large")
    return PlannedMedia(plan)


def _freeze_proposal_v5(
    opportunity: MediaOpportunity,
    proposal: dict[str, object],
    recent: tuple[str, ...],
    *,
    complete_candidates: tuple[dict[str, object], ...],
    presentation_candidates: tuple[dict[str, object], ...],
    recent_subjects: tuple[str, ...],
    recent_embodiments: tuple[str, ...],
    subject_config_path: Path,
    interaction_config_path: Path,
    embodiment_config_path: Path,
) -> PlanningResult:
    """Freeze a model-selected complete candidate without accepting free visual directions."""

    forbidden_free_fields = {
        "composition",
        "action",
        "camera_direction",
        "sharing_motive",
        "presentation_candidate_id",
    }
    if forbidden_free_fields.intersection(proposal):
        return NotRenderable(opportunity.opportunity_id, "free_visual_direction_in_v5")
    required_enums = {
        "content_domain": CONTENT_DOMAINS,
        "visual_form": VISUAL_FORMS,
        "share_intent": SHARE_INTENTS,
        "capture_mode": CAPTURE_MODES,
        "character_visibility": CHARACTER_VISIBILITIES,
        "other_people_visibility": OTHER_PEOPLE_VISIBILITIES,
        "polish": POLISH_LEVELS,
        "tone": TONES,
        "privacy": PRIVACY_LEVELS,
        "route": ROUTES,
    }
    for field, allowed in required_enums.items():
        if proposal.get(field) not in allowed:
            return NotRenderable(opportunity.opportunity_id, "invalid_classification", field)

    candidate_id = proposal.get("complete_candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        return NotRenderable(opportunity.opportunity_id, "missing_complete_expression_candidate")
    selected = next(
        (item for item in complete_candidates if item["complete_candidate_id"] == candidate_id),
        None,
    )
    if selected is None:
        return NotRenderable(opportunity.opportunity_id, "illegal_complete_expression_candidate")
    for proposal_field, candidate_field in (
        ("capture_mode", "legal_capture_modes"),
        ("visual_form", "legal_visual_forms"),
        ("share_intent", "legal_share_intents"),
        ("interaction_bid_id", "legal_interaction_bids"),
        ("character_visibility", "legal_character_visibilities"),
        ("route", "legal_routes"),
    ):
        if proposal.get(proposal_field) not in selected.get(candidate_field, []):
            return NotRenderable(
                opportunity.opportunity_id, "complete_expression_candidate_conflict", proposal_field
            )
    try:
        address = MediaAddressStrategy.from_payload(selected["media_address_strategy"])
        geometry = CameraGeometry.from_payload(selected["camera_geometry"])
    except (KeyError, TypeError, ValueError) as exc:
        return NotRenderable(
            opportunity.opportunity_id, "invalid_complete_expression_candidate", str(exc)[:240]
        )
    geometry_error = geometry.compatibility_error(
        capture_mode=str(proposal["capture_mode"]), visual_form=str(proposal["visual_form"])
    )
    if geometry_error:
        return NotRenderable(opportunity.opportunity_id, geometry_error)
    if address.expression_charge != "none":
        if (
            proposal.get("share_intent") != "intimate_signal"
            or proposal.get("privacy") != "intimate"
        ):
            return NotRenderable(
                opportunity.opportunity_id, "expression_charge_requires_intimate_signal"
            )
        if (
            SENSUAL_CHARGE_RANK[address.expression_charge]
            > SENSUAL_CHARGE_RANK[_expression_charge_ceiling(opportunity)]
        ):
            return NotRenderable(opportunity.opportunity_id, "expression_charge_ceiling_exceeded")

    legacy = dict(proposal)
    legacy.update(
        {
            "composition": _v5_composition(geometry),
            "action": _v5_action_direction(str(selected["action_cue"])),
            "camera_direction": _DEFAULT_CAMERA_DIRECTION[str(proposal["capture_mode"])],
            "sharing_motive": _v5_sharing_motive(str(proposal["share_intent"])),
            "constraints": list(proposal.get("constraints", [])),
        }
    )
    intimate_life_share = (
        opportunity.family == "life_share"
        and proposal.get("share_intent") == "intimate_signal"
        and proposal.get("privacy") == "intimate"
    )
    original_bid_id = str(proposal.get("interaction_bid_id") or "")
    compatible_opportunity = replace(
        opportunity,
        sensual_charge_ceiling=_expression_charge_ceiling(opportunity),
    )
    if intimate_life_share:
        legal_intents = _LIFE_MATRIX[str(proposal["content_domain"])][1]
        legacy["share_intent"] = "record" if "record" in legal_intents else sorted(legal_intents)[0]
        legacy["privacy"] = "ordinary"
        legacy["interaction_bid_id"] = "share_presence"
        legacy["sharing_motive"] = _v5_sharing_motive(str(legacy["share_intent"]))
    subject_payload = selected.get("subject_presentation")
    if subject_payload is not None:
        source_id = selected.get("source_presentation_candidate_id")
        source = next(
            (
                item
                for item in presentation_candidates
                if item.get("presentation_candidate_id") == source_id
            ),
            None,
        )
        if source is None:
            return NotRenderable(
                opportunity.opportunity_id, "orphaned_complete_expression_candidate"
            )
        legacy["presentation_candidate_id"] = source["presentation_candidate_id"]
        source_strategy = source["subject_presentation"].get("display_strategy") or {}
        source_goals = tuple(str(item) for item in source_strategy.get("communicative_goals", []))
        if legacy["interaction_bid_id"] not in source_goals:
            bid_catalog = _interaction_bid_values(
                compatible_opportunity, config_path=interaction_config_path
            )
            source_embodiment = EmbodiedPresentation.from_payload(source["embodied_presentation"])
            compatible_goal = next(
                (
                    goal
                    for goal in source_goals
                    if goal in bid_catalog
                    and _embodiment_bid_error(
                        source_embodiment,
                        MediaInteractionBid.create(
                            bid_id=f"media-bid:{opportunity.opportunity_id}",
                            communicative_goal=goal,
                            hoped_response=str(bid_catalog[goal]["hoped_response"]),
                            response_pressure=str(bid_catalog[goal]["response_pressure"]),
                            audience_ref=(
                                opportunity.audience_context.recipient_ref
                                if opportunity.audience_context
                                else ""
                            ),
                            minimum_privacy=str(
                                bid_catalog[goal].get("minimum_privacy") or "ordinary"
                            ),
                        ),
                    )
                    is None
                ),
                None,
            )
            if compatible_goal is None:
                return NotRenderable(
                    opportunity.opportunity_id, "orphaned_complete_expression_candidate"
                )
            legacy["interaction_bid_id"] = compatible_goal
    frozen = _freeze_proposal(
        compatible_opportunity,
        legacy,
        recent,
        recent_subjects=recent_subjects,
        recent_embodiments=recent_embodiments,
        subject_config_path=subject_config_path,
        interaction_config_path=interaction_config_path,
        embodiment_config_path=embodiment_config_path,
        presentation_candidate_limit=24,
        frozen_presentation_candidates=presentation_candidates,
    )
    if isinstance(frozen, NotRenderable):
        return frozen
    private_basis: FrozenPrivateExpressionBasis | None = None
    if opportunity.private_expression_basis is not None:
        try:
            private_basis = opportunity.private_expression_basis.freeze(
                opportunity.event_snapshot,
                recipient_ref=(
                    opportunity.audience_context.recipient_ref
                    if opportunity.audience_context
                    else ""
                ),
            )
        except ValueError as exc:
            return NotRenderable(opportunity.opportunity_id, str(exc))
        if private_basis.evidence_ref not in frozen.plan.evidence_values:
            return NotRenderable(
                opportunity.opportunity_id,
                "unselected_private_expression_basis_evidence",
            )
        if str(proposal["capture_mode"]) not in {"character_front_camera", "mirror"}:
            return NotRenderable(
                opportunity.opportunity_id,
                "private_expression_requires_self_authored_capture",
            )
    identity = (
        IdentityReferenceSelection.from_payload(selected["identity_reference_selection"])
        if selected.get("identity_reference_selection") is not None
        else None
    )
    authenticity = (
        PhotographicAuthenticityProfile.from_payload(selected["photographic_authenticity"])
        if selected.get("photographic_authenticity") is not None
        else None
    )
    moment_capture = (
        MomentCapture.from_payload(selected["moment_capture"])
        if selected.get("moment_capture") is not None
        else None
    )
    if moment_capture is not None:
        moment_capture = moment_capture.bind_evidence(
            primary_evidence_ref=frozen.plan.primary_evidence_ref,
            supporting_evidence_refs=frozen.plan.supporting_evidence_refs,
        )
    interaction_bid = frozen.plan.interaction_bid
    if intimate_life_share or legacy.get("interaction_bid_id") != original_bid_id:
        bid_values = _interaction_bid_values(
            compatible_opportunity, config_path=interaction_config_path
        ).get(original_bid_id)
        if bid_values is None:
            return NotRenderable(opportunity.opportunity_id, "illegal_interaction_bid")
        interaction_bid = MediaInteractionBid.create(
            bid_id=f"media-bid:{opportunity.opportunity_id}",
            communicative_goal=original_bid_id,
            hoped_response=str(bid_values["hoped_response"]),
            response_pressure=str(bid_values["response_pressure"]),
            audience_ref=(
                opportunity.audience_context.recipient_ref if opportunity.audience_context else ""
            ),
            minimum_privacy=str(bid_values.get("minimum_privacy") or "ordinary"),
        )
    plan = replace(
        frozen.plan,
        version=PLAN_VERSION_V5,
        share_intent=str(proposal["share_intent"]),
        privacy=str(proposal["privacy"]),
        action_template_id=str(selected["action_template_id"]),
        action_cue=str(selected["action_cue"]),
        media_address_strategy=address,
        camera_geometry=geometry,
        identity_reference_selection=identity,
        subject_presentation=(
            SubjectPresentationPlan.from_payload(subject_payload)
            if subject_payload is not None
            else None
        ),
        embodied_presentation=(
            EmbodiedPresentation.from_payload(selected["embodied_presentation"])
            if selected.get("embodied_presentation") is not None
            else None
        ),
        interaction_bid=interaction_bid,
        expression_charge_ceiling=_expression_charge_ceiling(opportunity),
        relationship_stage_basis=(
            opportunity.audience_context.relationship_stage if opportunity.audience_context else ""
        ),
        photographic_authenticity=authenticity,
        moment_capture=moment_capture,
        private_expression_basis=private_basis,
    )
    plan = replace(plan, diversity_fingerprint=_v5_fingerprint(plan))
    if plan.diversity_fingerprint in recent[-12:]:
        return NotRenderable(opportunity.opportunity_id, "duplicate_recent_fingerprint")
    error = _validate_frozen_plan(plan)
    if error:
        return NotRenderable(opportunity.opportunity_id, error)
    return PlannedMedia(plan)


def _v5_composition(geometry: CameraGeometry) -> str:
    if geometry.shot_distance in {"long", "wide"}:
        return "让事件环境占主要面积的宽景"
    if geometry.subject_occupancy in {"detail", "dominant"}:
        return "突出主证据细节的近景"
    if geometry.shot_distance == "full_body":
        return "完整展示人物与环境关系的全身构图"
    return "主体与事件环境同时可辨的自然中近景"


def _v5_action_direction(action_cue: str) -> str:
    return "自然地把{primary}带进画面"


def _v5_sharing_motive(share_intent: str) -> str:
    return {
        "complain": "用轻松方式吐槽这个瞬间",
        "seek_feedback": "征求对方的看法",
        "memory_keep": "留下值得记住的画面",
        "intimate_signal": "传递克制且非露骨的亲密信号",
        "progress_update": "说明当前进度或状态",
    }.get(share_intent, "把这个生活瞬间分享给熟悉的人")


def _v5_fingerprint(plan: MediaPlan) -> str:
    assert plan.media_address_strategy is not None and plan.camera_geometry is not None
    parts = [
        plan.family,
        plan.content_domain,
        plan.visual_form,
        plan.share_intent,
        plan.capture_mode,
        plan.character_visibility,
        plan.polish,
        plan.tone,
        plan.media_address_strategy.engagement_tactic,
        plan.media_address_strategy.expression_charge,
        plan.media_address_strategy.attraction_mechanism or "none",
        plan.camera_geometry.shot_distance,
        plan.camera_geometry.camera_height,
        plan.camera_geometry.view_axis,
        plan.camera_geometry.subject_occupancy,
        plan.camera_geometry.subject_placement,
        plan.camera_geometry.orientation,
        plan.camera_geometry.imperfection_profile,
    ]
    if plan.camera_geometry.version == "camera-geometry-v2":
        parts.extend(
            (
                plan.camera_geometry.camera_face_distance,
                plan.camera_geometry.face_radial_position,
            )
        )
    if plan.subject_presentation:
        parts.append(plan.subject_presentation.subject_signature)
    if plan.embodied_presentation:
        parts.extend(
            (
                plan.embodied_presentation.body_strategy_id,
                plan.embodied_presentation.action_variant_id,
            )
        )
    if plan.identity_reference_selection:
        parts.extend(plan.identity_reference_selection.asset_ids)
    if plan.photographic_authenticity:
        parts.extend(
            (
                plan.photographic_authenticity.aesthetic_intent,
                plan.photographic_authenticity.device_rendering,
                plan.photographic_authenticity.scene_orderliness,
                plan.photographic_authenticity.capture_imperfection,
            )
        )
    if plan.moment_capture:
        parts.extend(
            (
                plan.moment_capture.moment_mode,
                plan.moment_capture.camera_relation,
                plan.moment_capture.scene_anchor,
            )
        )
    return "|".join(parts)


def _validate_opportunity(opportunity: MediaOpportunity) -> str | None:
    if not opportunity.opportunity_id.strip():
        return "missing_opportunity_id"
    if opportunity.family not in FAMILIES:
        return "invalid_family"
    if opportunity.privacy_ceiling not in PRIVACY_LEVELS:
        return "invalid_privacy_ceiling"
    if opportunity.sensual_charge_ceiling not in SENSUAL_CHARGE_LEVELS:
        return "invalid_sensual_charge_ceiling"
    if (
        opportunity.expression_charge_ceiling is not None
        and opportunity.expression_charge_ceiling not in SENSUAL_CHARGE_LEVELS
    ):
        return "invalid_expression_charge_ceiling"
    if (
        opportunity.expression_charge_ceiling is not None
        and opportunity.sensual_charge_ceiling != "none"
        and opportunity.expression_charge_ceiling != opportunity.sensual_charge_ceiling
    ):
        return "conflicting_expression_charge_ceilings"
    charge_ceiling = _expression_charge_ceiling(opportunity)
    if charge_ceiling != "none" and opportunity.privacy_ceiling != "intimate":
        return "sensual_charge_ceiling_requires_intimate_privacy"
    stage = opportunity.audience_context.relationship_stage if opportunity.audience_context else ""
    if charge_ceiling in {"subtle", "charged"} and stage not in {
        "ambiguous",
        "lover",
    }:
        return "sensual_charge_ceiling_relationship_conflict"
    if charge_ceiling == "veiled" and stage != "lover":
        return "sensual_charge_ceiling_relationship_conflict"
    if opportunity.delivery_mode not in DELIVERY_MODES:
        return "invalid_delivery_mode"
    if any(
        item not in _WORLD_EXPRESSION_CONSTRAINTS for item in opportunity.expression_requirements
    ):
        return "unsupported_expression_requirement"
    event = _mapping(opportunity.event_snapshot.get("event"))
    if not event.get("event_id"):
        return "missing_committed_event"
    if str(event.get("status")) not in {"committed", "settled", "completed"}:
        return "event_not_committed"
    return None


def _expression_charge_ceiling(opportunity: MediaOpportunity) -> str:
    """Return the v5 name while preserving the v4 input contract."""

    return opportunity.expression_charge_ceiling or opportunity.sensual_charge_ceiling


def _complete_candidate_world_legal(
    candidate: dict[str, object], opportunity: MediaOpportunity
) -> bool:
    modes = candidate.get("legal_capture_modes", [])
    if not isinstance(modes, list) or len(modes) != 1:
        return False
    mode = str(modes[0])
    snapshot = opportunity.event_snapshot
    if mode == "known_companion":
        return bool(_known_companions(snapshot))
    if mode == "external_sender":
        return _has_external_sender(snapshot)
    if mode == "existing_artifact":
        return bool(_accessible_existing_media(snapshot))
    if mode == "mirror":
        return bool(_mapping(snapshot.get("location")).get("mirror_available"))
    if mode == "requested_helper":
        return str(_mapping(snapshot.get("location")).get("kind")) == "public"
    return True


def _embodiment_bid_error(
    embodiment: EmbodiedPresentation,
    bid: MediaInteractionBid,
) -> str | None:
    charge = embodiment.sensual_charge
    goal = bid.communicative_goal
    if charge == "none" and goal in {"invite_closeness", "invite_desire"}:
        return "embodiment_interaction_bid_conflict"
    if charge == "subtle" and goal not in {
        "invite_closeness",
        "invite_appreciation",
    }:
        return "embodiment_interaction_bid_conflict"
    if charge in {"charged", "veiled"} and goal not in {
        "invite_desire",
        "invite_closeness",
        "invite_playful_exchange",
        "invite_appreciation",
    }:
        return "embodiment_interaction_bid_conflict"
    return None


def _validate_combination(
    opportunity: MediaOpportunity,
    values: dict[str, str],
    primary: str,
    pointers: Sequence[str],
) -> str | None:
    snapshot = opportunity.event_snapshot
    family = opportunity.family
    visibility = values["character_visibility"]
    capture = values["capture_mode"]
    matrix = _LIFE_MATRIX if family == "life_share" else _CHARACTER_MATRIX
    rule = matrix.get(values["content_domain"])
    if rule is None:
        return "family_domain_conflict"
    allowed_forms, allowed_intents = rule
    if values["visual_form"] not in allowed_forms:
        return "matrix_visual_form_conflict"
    if values["share_intent"] not in allowed_intents:
        return "matrix_share_intent_conflict"
    if values["visual_form"] == "body_detail" and visibility != "body_detail":
        return "body_detail_visibility_conflict"
    if visibility == "body_detail" and values["visual_form"] not in {
        "body_detail",
        "subject_closeup",
    }:
        return "body_detail_visibility_conflict"
    if family == "life_share":
        if visibility not in {"none", "trace_only"}:
            return "family_visibility_conflict"
        if capture not in _LIFE_CAPTURE_MODES:
            return "family_capture_conflict"
        if values["privacy"] == "intimate" or values["share_intent"] == "intimate_signal":
            return "life_share_cannot_be_intimate"
    else:
        if visibility not in {"identifiable", "body_detail"}:
            return "family_visibility_conflict"
        if capture not in _CHARACTER_CAPTURE_MODES:
            return "family_capture_conflict"
    if _PRIVACY_RANK[values["privacy"]] > _PRIVACY_RANK[opportunity.privacy_ceiling]:
        return "privacy_ceiling_exceeded"
    if values["share_intent"] == "intimate_signal" and values["privacy"] != "intimate":
        return "intimate_signal_requires_intimate_privacy"
    if values["privacy"] == "intimate" and values["share_intent"] != "intimate_signal":
        return "intimate_privacy_requires_signal"
    if capture == "known_companion" and not _known_companions(snapshot):
        return "missing_companion_evidence"
    if capture == "known_companion" and not any(
        item.startswith("/participants/") for item in pointers
    ):
        return "unselected_companion_evidence"
    if capture == "external_sender" and not _has_external_sender(snapshot):
        return "missing_external_sender_evidence"
    if capture == "external_sender" and not any(item.startswith("/source/") for item in pointers):
        return "unselected_external_sender_evidence"
    if capture == "existing_artifact" or values["route"] == "reuse_existing":
        if not _accessible_existing_media(snapshot):
            return "missing_existing_artifact"
        if capture != "existing_artifact" or values["route"] != "reuse_existing":
            return "artifact_route_conflict"
        if not any(
            item.startswith("/existing_media/") and item.endswith("/path") for item in pointers
        ):
            return "unselected_existing_artifact"
    elif values["route"] != "generate":
        return "artifact_route_conflict"
    if capture == "mirror" and not bool(_mapping(snapshot.get("location")).get("mirror_available")):
        return "missing_mirror_evidence"
    if (
        capture == "requested_helper"
        and str(_mapping(snapshot.get("location")).get("kind")) != "public"
    ):
        return "helper_requires_public_place"
    if values["content_domain"] == "body_health":
        if "body_health" not in primary or not _mapping(
            _mapping(snapshot.get("character")).get("body_health")
        ):
            return "missing_body_health_evidence"
    if values["visual_form"] == "social_frame":
        if values["other_people_visibility"] == "none":
            return "social_frame_requires_people"
        if not (_known_companions(snapshot) or _has_external_sender(snapshot)):
            return "social_frame_requires_people"
    elif values["other_people_visibility"] in {"known_anonymized", "identity_referenced"}:
        return "people_visibility_without_social_form"
    if values["other_people_visibility"] == "identity_referenced" and not _has_identity_reference(
        snapshot
    ):
        return "missing_identity_reference"
    readable = bool(_mapping(snapshot.get("visual_requirements")).get("requires_readable_text"))
    if values["content_domain"] == "information_screen" and readable:
        if not _accessible_existing_media(snapshot) or values["route"] != "reuse_existing":
            return "readable_text_requires_artifact"
    if values["content_domain"] == "other_grounded" and values["share_intent"] == "intimate_signal":
        return "other_grounded_cannot_be_intimate"
    return None


def _validate_frozen_plan(plan: MediaPlan) -> str | None:
    if plan.version not in SUPPORTED_PLAN_VERSIONS:
        return "unsupported_version"
    if plan.version == PLAN_VERSION_V5:
        return _validate_frozen_plan_v5(plan)
    if (
        plan.expression_charge_ceiling is not None
        or plan.relationship_stage_basis is not None
        or plan.photographic_authenticity is not None
        or plan.private_expression_basis is not None
    ):
        return "v5_contract_in_legacy_plan"
    enums = (
        (plan.family, FAMILIES),
        (plan.content_domain, CONTENT_DOMAINS),
        (plan.visual_form, VISUAL_FORMS),
        (plan.share_intent, SHARE_INTENTS),
        (plan.capture_mode, CAPTURE_MODES),
        (plan.character_visibility, CHARACTER_VISIBILITIES),
        (plan.other_people_visibility, OTHER_PEOPLE_VISIBILITIES),
        (plan.polish, POLISH_LEVELS),
        (plan.tone, TONES),
        (plan.privacy, PRIVACY_LEVELS),
        (plan.route, ROUTES),
        (plan.delivery_mode, DELIVERY_MODES),
    )
    if any(value not in allowed for value, allowed in enums):
        return "invalid_enum"
    direction_error = _validate_direction_catalog(
        plan.to_payload(),
        {"share_intent": plan.share_intent, "capture_mode": plan.capture_mode},
        check_constraints=False,
    )
    if direction_error:
        return direction_error
    if any(item not in _FROZEN_CONSTRAINTS for item in plan.constraints):
        return "unsupported_frozen_constraint"
    pointers = (plan.primary_evidence_ref, *plan.supporting_evidence_refs)
    if len(pointers) != len(set(pointers)) or set(plan.evidence_values) != set(pointers):
        return "invalid_evidence"
    expected_parts = (
        plan.family,
        plan.content_domain,
        plan.visual_form,
        plan.share_intent,
        plan.capture_mode,
        plan.character_visibility,
        plan.polish,
        plan.tone,
    )
    if plan.version == PLAN_VERSION and plan.embodied_presentation:
        expected_parts += (
            plan.embodied_presentation.physical_salience,
            plan.embodied_presentation.sensual_charge,
            plan.embodied_presentation.coverage_mode,
            plan.embodied_presentation.body_strategy_id,
        )
        if plan.embodied_presentation.version == EMBODIED_PRESENTATION_V2:
            expected_parts += (plan.embodied_presentation.action_variant_id,)
    expected = "|".join(expected_parts)
    if plan.diversity_fingerprint != expected:
        return "invalid_fingerprint"
    if plan.family == "life_share" and plan.character_visibility not in {"none", "trace_only"}:
        return "family_visibility_conflict"
    if plan.family == "character_media" and plan.character_visibility not in {
        "identifiable",
        "body_detail",
    }:
        return "family_visibility_conflict"
    if plan.visual_form == "body_detail" and plan.character_visibility != "body_detail":
        return "body_detail_visibility_conflict"
    if plan.character_visibility == "body_detail" and plan.visual_form not in {
        "body_detail",
        "subject_closeup",
    }:
        return "body_detail_visibility_conflict"
    matrix = _LIFE_MATRIX if plan.family == "life_share" else _CHARACTER_MATRIX
    rule = matrix.get(plan.content_domain)
    if not rule or plan.visual_form not in rule[0] or plan.share_intent not in rule[1]:
        return "matrix_conflict"
    if plan.family == "life_share" and plan.capture_mode not in _LIFE_CAPTURE_MODES:
        return "family_capture_conflict"
    if plan.family == "life_share" and (
        plan.privacy == "intimate" or plan.share_intent == "intimate_signal"
    ):
        return "life_share_cannot_be_intimate"
    if plan.share_intent == "intimate_signal" and plan.privacy != "intimate":
        return "intimate_signal_requires_intimate_privacy"
    if plan.privacy == "intimate" and plan.share_intent != "intimate_signal":
        return "intimate_privacy_requires_signal"
    if (plan.capture_mode == "existing_artifact") != (plan.route == "reuse_existing"):
        return "artifact_route_conflict"
    if plan.route == "reuse_existing" and not plan.existing_artifact_path:
        return "missing_existing_artifact"
    if plan.route == "reuse_existing":
        selected_paths = {
            str(value)
            for pointer, value in plan.evidence_values.items()
            if pointer.startswith("/existing_media/")
            and pointer.endswith("/path")
            and isinstance(value, str)
        }
        if plan.existing_artifact_path not in selected_paths:
            return "existing_artifact_path_mismatch"
    if plan.visual_form == "social_frame" and plan.other_people_visibility == "none":
        return "social_frame_requires_people"
    if plan.visual_form != "social_frame" and plan.other_people_visibility in {
        "known_anonymized",
        "identity_referenced",
    }:
        return "people_visibility_without_social_form"
    if plan.content_domain == "body_health" and "body_health" not in plan.primary_evidence_ref:
        return "missing_body_health_evidence"
    evidence_refs = tuple(plan.evidence_values)
    if plan.capture_mode == "known_companion" and not any(
        item.startswith("/participants/") for item in evidence_refs
    ):
        return "unselected_companion_evidence"
    if plan.capture_mode == "external_sender" and not any(
        item.startswith("/source/") for item in evidence_refs
    ):
        return "unselected_external_sender_evidence"
    if plan.capture_mode == "existing_artifact" and not any(
        item.startswith("/existing_media/") and item.endswith("/path") for item in evidence_refs
    ):
        return "unselected_existing_artifact"
    if plan.version == PLAN_VERSION:
        if plan.intimate_intensity is not None:
            return "legacy_intimate_intensity_in_v4"
    elif plan.embodied_presentation is not None:
        return "legacy_embodied_presentation_conflict"
    elif plan.intimate_intensity and (
        plan.share_intent != "intimate_signal"
        or plan.intimate_intensity not in INTIMATE_INTENSITIES
    ):
        return "invalid_intimate_intensity"
    if plan.version in {PLAN_VERSION_V1, PLAN_VERSION_V2}:
        if plan.interaction_bid is not None:
            return "legacy_interaction_bid_conflict"
    elif plan.interaction_bid is None:
        return "missing_interaction_bid"
    else:
        try:
            MediaInteractionBid.from_payload(plan.interaction_bid.to_payload())
        except ValueError:
            return "invalid_interaction_bid"
        if plan.interaction_bid.bid_id != f"media-bid:{plan.opportunity_id}":
            return "invalid_interaction_bid_id"
        if (
            plan.interaction_bid.communicative_goal in {"invite_closeness", "invite_desire"}
            and plan.privacy != "intimate"
        ):
            return "interaction_bid_privacy_conflict"
        if _PRIVACY_RANK[plan.interaction_bid.minimum_privacy] > _PRIVACY_RANK[plan.privacy]:
            return "interaction_bid_privacy_conflict"
    if plan.version == PLAN_VERSION_V1:
        if plan.subject_presentation is not None:
            return "v1_subject_presentation_conflict"
    elif plan.version == PLAN_VERSION_V2:
        if plan.family == "life_share" and plan.subject_presentation is not None:
            return "life_share_subject_presentation_conflict"
        if (
            plan.family == "character_media"
            and plan.route == "generate"
            and plan.subject_presentation is None
        ):
            return "missing_subject_presentation"
    elif plan.family == "life_share" and (
        plan.subject_presentation is not None or plan.embodied_presentation is not None
    ):
        return "life_share_presentation_conflict"
    elif (
        plan.family == "character_media"
        and plan.route == "generate"
        and plan.subject_presentation is None
    ):
        return "missing_subject_presentation"
    elif plan.subject_presentation is not None:
        try:
            SubjectPresentationPlan.from_payload(plan.subject_presentation.to_payload())
        except ValueError:
            return "invalid_subject_presentation"
        if (
            plan.version in {PLAN_VERSION_V3, PLAN_VERSION}
            and plan.route == "generate"
            and plan.subject_presentation.version != "subject-presentation-v2"
        ):
            return "legacy_subject_presentation_in_modern_plan"
        strategy = plan.subject_presentation.display_strategy
        if (
            plan.version in {PLAN_VERSION_V3, PLAN_VERSION}
            and strategy is not None
            and plan.interaction_bid is not None
            and plan.interaction_bid.communicative_goal not in strategy.communicative_goals
        ):
            return "subject_interaction_bid_conflict"
        if strategy is not None:
            if strategy.minimum_privacy not in _PRIVACY_RANK:
                return "invalid_subject_display_privacy"
            if _PRIVACY_RANK[strategy.minimum_privacy] > _PRIVACY_RANK[plan.privacy]:
                return "subject_display_privacy_conflict"
        feasibility_error = capture_hand_feasibility_error(
            plan.subject_presentation,
            capture_mode=plan.capture_mode,
            character_visibility=plan.character_visibility,
        )
        if feasibility_error:
            return feasibility_error
        if plan.version == PLAN_VERSION and plan.embodied_presentation is not None:
            feasibility_error = embodied_capture_feasibility_error(
                plan.embodied_presentation,
                capture_mode=plan.capture_mode,
                hand_occupancy=plan.subject_presentation.performance.hand_occupancy,
            )
            if feasibility_error:
                return feasibility_error
    if plan.version == PLAN_VERSION:
        if (
            plan.family == "character_media"
            and plan.route == "generate"
            and plan.embodied_presentation is None
        ):
            return "missing_embodied_presentation"
        if plan.embodied_presentation is not None:
            try:
                EmbodiedPresentation.from_payload(plan.embodied_presentation.to_payload())
            except ValueError:
                return "invalid_embodied_presentation"
            embodiment = plan.embodied_presentation
            physical_evidence_refs = {
                pointer for cue in embodiment.physical_cues for pointer in cue.evidence_refs
            }
            if any(pointer not in plan.evidence_values for pointer in physical_evidence_refs):
                return "unselected_physical_state_evidence"
            if any(
                pointer not in plan.evidence_values for pointer in embodiment.wardrobe_evidence_refs
            ):
                return "unselected_wardrobe_evidence"
            if embodiment.sensual_charge == "none":
                if plan.share_intent == "intimate_signal" or plan.privacy == "intimate":
                    return "sensual_charge_intent_conflict"
            elif plan.share_intent != "intimate_signal" or plan.privacy != "intimate":
                return "sensual_charge_requires_intimate_signal"
            if plan.interaction_bid:
                bid_error = _embodiment_bid_error(embodiment, plan.interaction_bid)
                if bid_error:
                    return bid_error
    return None


def _validate_frozen_plan_v5(plan: MediaPlan) -> str | None:
    enums = (
        (plan.family, FAMILIES),
        (plan.content_domain, CONTENT_DOMAINS),
        (plan.visual_form, VISUAL_FORMS),
        (plan.share_intent, SHARE_INTENTS),
        (plan.capture_mode, CAPTURE_MODES),
        (plan.character_visibility, CHARACTER_VISIBILITIES),
        (plan.other_people_visibility, OTHER_PEOPLE_VISIBILITIES),
        (plan.polish, POLISH_LEVELS),
        (plan.tone, TONES),
        (plan.privacy, PRIVACY_LEVELS),
        (plan.route, ROUTES),
        (plan.delivery_mode, DELIVERY_MODES),
    )
    if any(value not in allowed for value, allowed in enums):
        return "invalid_enum"
    if not plan.action_template_id or not plan.action_cue:
        return "missing_action_contract"
    if plan.media_address_strategy is None or plan.camera_geometry is None:
        return "missing_complete_expression_contract"
    try:
        MediaAddressStrategy.from_payload(plan.media_address_strategy.to_payload())
        CameraGeometry.from_payload(plan.camera_geometry.to_payload())
    except ValueError:
        return "invalid_complete_expression_contract"
    if plan.photographic_authenticity is not None:
        try:
            PhotographicAuthenticityProfile.from_payload(
                plan.photographic_authenticity.to_payload()
            )
        except ValueError:
            return "invalid_photographic_authenticity"
    if plan.moment_capture is not None:
        try:
            MomentCapture.from_payload(plan.moment_capture.to_payload())
        except ValueError:
            return "invalid_moment_capture"
        if plan.moment_capture.version == "moment-capture-v2" and set(
            plan.moment_capture.evidence_refs
        ) != set(plan.evidence_values):
            return "moment_capture_evidence_conflict"
    geometry_error = plan.camera_geometry.compatibility_error(
        capture_mode=plan.capture_mode, visual_form=plan.visual_form
    )
    if geometry_error:
        return geometry_error
    if plan.family == "character_media" and plan.route == "generate":
        if plan.identity_reference_selection is None:
            return "missing_identity_reference_selection"
        try:
            IdentityReferenceSelection.from_payload(plan.identity_reference_selection.to_payload())
        except ValueError:
            return "invalid_identity_reference_selection"
    elif plan.identity_reference_selection is not None:
        return "unexpected_identity_reference_selection"
    address = plan.media_address_strategy
    if plan.expression_charge_ceiling not in SENSUAL_CHARGE_LEVELS:
        return "invalid_expression_charge_ceiling"
    if (
        SENSUAL_CHARGE_RANK[address.expression_charge]
        > SENSUAL_CHARGE_RANK[plan.expression_charge_ceiling]
    ):
        return "expression_charge_ceiling_exceeded"
    stage = plan.relationship_stage_basis or ""
    if address.expression_charge in {"subtle", "charged"} and stage not in {
        "ambiguous",
        "lover",
    }:
        return "expression_charge_relationship_conflict"
    if address.expression_charge == "veiled" and stage != "lover":
        return "expression_charge_relationship_conflict"
    if address.expression_charge == "none":
        if plan.share_intent == "intimate_signal" or plan.privacy == "intimate":
            return "expression_charge_intent_conflict"
    elif plan.share_intent != "intimate_signal" or plan.privacy != "intimate":
        return "expression_charge_requires_intimate_signal"
    private_expression = plan.family == "character_media" and (
        plan.privacy == "intimate" or address.expression_charge != "none"
    )
    if private_expression:
        basis = plan.private_expression_basis
        if basis is None:
            return "missing_private_expression_basis"
        if basis.validate_payload():
            return "invalid_private_expression_basis"
        if basis.evidence_ref not in plan.evidence_values:
            return "unselected_private_expression_basis_evidence"
        if plan.evidence_values[basis.evidence_ref] != basis.evidence_value:
            return "private_expression_basis_value_mismatch"
        if not plan.interaction_bid or plan.interaction_bid.audience_ref != basis.recipient_ref:
            return "private_expression_recipient_mismatch"
        if (
            SENSUAL_CHARGE_RANK[basis.required_charge]
            > SENSUAL_CHARGE_RANK[plan.expression_charge_ceiling]
        ):
            return "private_expression_charge_ceiling_too_low"
        if (
            SENSUAL_CHARGE_RANK[address.expression_charge]
            < SENSUAL_CHARGE_RANK[basis.required_charge]
        ):
            return "private_expression_charge_below_basis_floor"
        if plan.capture_mode not in {"character_front_camera", "mirror"}:
            return "private_expression_requires_self_authored_capture"
    elif plan.private_expression_basis is not None:
        return "unexpected_private_expression_basis"
    if address.engagement_tactic == "attraction":
        if not plan.interaction_bid or plan.interaction_bid.communicative_goal != "invite_desire":
            return "attraction_interaction_bid_conflict"
    if plan.family == "life_share":
        if plan.character_visibility not in {"none", "trace_only"}:
            return "family_visibility_conflict"
        if plan.subject_presentation is not None or plan.embodied_presentation is not None:
            return "life_share_presentation_conflict"
        if (
            address.expression_charge != "none"
            and address.attraction_mechanism != "atmospheric_suggestion"
        ):
            return "life_share_attraction_mechanism_conflict"
        if address.expression_charge != "none" and not plan.primary_evidence_ref.startswith(
            (
                "/location/",
                "/objects/",
                "/environment/",
                "/character/appearance_state/",
                "/existing_media/",
            )
        ):
            return "ungrounded_intimate_life_share"
        if address.expression_charge == "veiled" and not plan.primary_evidence_ref.startswith(
            ("/objects/", "/character/appearance_state/", "/existing_media/")
        ):
            return "veiled_life_share_requires_private_evidence"
    elif plan.route == "generate":
        if plan.subject_presentation is None or plan.embodied_presentation is None:
            return "missing_character_presentation"
        if plan.subject_presentation.version not in {
            SUBJECT_PRESENTATION_V3,
            SUBJECT_PRESENTATION_V4,
        }:
            return "legacy_subject_presentation_in_v5"
        if (
            plan.subject_presentation.version == SUBJECT_PRESENTATION_V4
            and plan.photographic_authenticity is None
        ):
            return "missing_photographic_authenticity"
        if plan.embodied_presentation.version != EMBODIED_PRESENTATION_V3:
            return "legacy_embodied_presentation_in_v5"
        if plan.embodied_presentation.sensual_charge != address.expression_charge:
            return "expression_embodiment_charge_conflict"
    elif plan.subject_presentation is not None or plan.embodied_presentation is not None:
        return "artifact_presentation_reinterpretation"
    if plan.family != "character_media" and plan.moment_capture is not None:
        return "unexpected_moment_capture"
    if plan.photographic_authenticity is not None:
        if plan.photographic_authenticity.regional_grounding == "explicit" and not any(
            pointer in {"/location/country", "/location/region", "/location/city"}
            for pointer in plan.evidence_values
        ):
            return "unselected_regional_grounding"
    if plan.diversity_fingerprint != _v5_fingerprint(plan):
        return "invalid_fingerprint"

    # Reuse the complete v4 evidence, matrix, capture and presentation validator.
    legacy_parts = [
        plan.family,
        plan.content_domain,
        plan.visual_form,
        plan.share_intent,
        plan.capture_mode,
        plan.character_visibility,
        plan.polish,
        plan.tone,
    ]
    legacy_share_intent = plan.share_intent
    legacy_privacy = plan.privacy
    legacy_interaction_bid = plan.interaction_bid
    if plan.family == "life_share" and plan.share_intent == "intimate_signal":
        legal_intents = _LIFE_MATRIX[plan.content_domain][1]
        legacy_share_intent = "record" if "record" in legal_intents else sorted(legal_intents)[0]
        legacy_privacy = "ordinary"
        legacy_parts[3] = legacy_share_intent
        legacy_interaction_bid = MediaInteractionBid.create(
            bid_id=f"media-bid:{plan.opportunity_id}",
            communicative_goal="share_presence",
            hoped_response="acknowledge_or_light_reaction",
            response_pressure="low",
            audience_ref=(plan.interaction_bid.audience_ref if plan.interaction_bid else ""),
            minimum_privacy="ordinary",
        )
    if plan.embodied_presentation:
        legacy_parts.extend(
            (
                plan.embodied_presentation.physical_salience,
                plan.embodied_presentation.sensual_charge,
                plan.embodied_presentation.coverage_mode,
                plan.embodied_presentation.body_strategy_id,
            )
        )
        if plan.embodied_presentation.version in {
            EMBODIED_PRESENTATION_V2,
            EMBODIED_PRESENTATION_V3,
        }:
            legacy_parts.append(plan.embodied_presentation.action_variant_id)
    legacy_subject = plan.subject_presentation
    legacy_embodiment = plan.embodied_presentation
    if legacy_subject and legacy_subject.version == SUBJECT_PRESENTATION_V3:
        if legacy_subject.display_strategy is None:
            return "missing_photo_display_strategy"
        legacy_strategy = replace(
            legacy_subject.display_strategy,
            communicative_goals=(legacy_interaction_bid.communicative_goal,),
        )
        legacy_subject = SubjectPresentationPlan.create_v2(
            variant_id=legacy_subject.variant_id,
            appearance=legacy_subject.appearance,
            performance=replace(
                legacy_subject.performance,
                expression=legacy_subject.display_strategy.strategy_id,
            ),
            display_strategy=legacy_strategy,
        )
    elif legacy_subject and legacy_subject.version == SUBJECT_PRESENTATION_V4:
        strategy = PhotoDisplayStrategy(
            strategy_id=legacy_subject.facial_display_strategy.strategy_family,
            communicative_goals=(legacy_interaction_bid.communicative_goal,),
            intentionality="recipient_aware",
            intensity=legacy_subject.facial_micro_performance.display_intensity,
            holistic_cue=legacy_subject.facial_display_strategy.performance_intent,
            mouth=legacy_subject.facial_micro_performance.mouth_action,
            eyes=legacy_subject.facial_micro_performance.eye_aperture,
            brows=legacy_subject.facial_micro_performance.brow_action,
            gaze_quality=legacy_subject.facial_micro_performance.gaze_target,
            facial_tension=legacy_subject.facial_micro_performance.facial_energy,
            temporal_beat=legacy_subject.facial_micro_performance.temporal_phase,
        )
        legacy_subject = SubjectPresentationPlan.create_v2(
            variant_id=legacy_subject.variant_id,
            appearance=legacy_subject.appearance,
            performance=replace(
                legacy_subject.performance,
                expression=strategy.strategy_id,
                gaze_target=strategy.gaze_quality,
            ),
            display_strategy=strategy,
        )
    if legacy_embodiment and legacy_embodiment.version == EMBODIED_PRESENTATION_V3:
        legacy_embodiment = EmbodiedPresentation.create(
            **{
                **legacy_embodiment.__dict__,
                "version": EMBODIED_PRESENTATION_V2,
                "contract_signature": "",
            }
        )
    legacy = replace(
        plan,
        version=PLAN_VERSION,
        diversity_fingerprint="|".join(legacy_parts),
        action_template_id=None,
        action_cue=None,
        media_address_strategy=None,
        camera_geometry=None,
        identity_reference_selection=None,
        expression_charge_ceiling=None,
        relationship_stage_basis=None,
        photographic_authenticity=None,
        private_expression_basis=None,
        subject_presentation=legacy_subject,
        embodied_presentation=legacy_embodiment,
        share_intent=legacy_share_intent,
        privacy=legacy_privacy,
        interaction_bid=legacy_interaction_bid,
        sharing_motive=_v5_sharing_motive(legacy_share_intent),
    )
    legacy_error = _validate_frozen_plan(legacy)
    return legacy_error


def _planning_messages(
    opportunity: MediaOpportunity,
    recent: tuple[str, ...],
    presentation_candidates: tuple[dict[str, object], ...] = (),
    interaction_bids: tuple[dict[str, object], ...] = (),
) -> list[dict[str, str]]:
    recent_three = recent[-3:]
    return [
        {
            "role": "system",
            "content": (
                "You are MediaPlanner. Interpret one committed fictional-world event as one plausible "
                "photo a human might actually take and share. Return one JSON object only. Never invent "
                "a place, participant, possession, body condition, readable text, or completed event. "
                "The World already froze family and privacy ceiling; do not return family. Pick exactly "
                "one value for every requested dimension and exactly one primary evidence JSON Pointer. "
                "Every JSON Pointer must use RFC 6901 plain form beginning with '/', for example "
                "'/objects/0/description'; never use URI-fragment form beginning with '#/'. "
                "Supporting evidence is optional. Prefer controlled variety, but facts and capture-source "
                "legality always win. A character photo may be posed, atmospheric, funny, polished or raw; "
                "avoid both lifeless standing and paparazzi-like framing unless evidence specifically supports it. "
                "For generated character media, choose exactly one supplied presentation_candidate_id. Each "
                "candidate is one coherent frozen subject-and-body performance: never rewrite or independently "
                "combine its appearance, gaze, pose, expression, gesture, bodily state, wardrobe coverage, "
                "sensual charge, or photo awareness."
            ),
        },
        {
            "role": "user",
            "content": (
                f"opportunity_id={opportunity.opportunity_id}\nfamily={opportunity.family}\n"
                f"privacy_ceiling={opportunity.privacy_ceiling}\n"
                f"sensual_charge_ceiling={opportunity.sensual_charge_ceiling}\n"
                f"delivery_mode={opportunity.delivery_mode}\n"
                f"audience_context={_stable_json(asdict(opportunity.audience_context) if opportunity.audience_context else {})}\n"
                f"event_snapshot={_stable_json(opportunity.event_snapshot)}\n"
                f"hard_banned_fingerprints_last_12={_stable_json(recent)}\n"
                f"soft_penalty_last_3={_stable_json(recent_three)}\n"
                f"legal_character_presentation_candidates={_stable_json(presentation_candidates)}\n"
                f"legal_interaction_bid_candidates={_stable_json(interaction_bids)}\n"
                "Enums:\n"
                f"content_domain={sorted(CONTENT_DOMAINS)}\nvisual_form={sorted(VISUAL_FORMS)}\n"
                f"share_intent={sorted(SHARE_INTENTS)}\ncapture_mode={sorted(CAPTURE_MODES)}\n"
                f"character_visibility={sorted(CHARACTER_VISIBILITIES)}\n"
                f"other_people_visibility={sorted(OTHER_PEOPLE_VISIBILITIES)}\n"
                f"polish={sorted(POLISH_LEVELS)}\ntone={sorted(TONES)}\n"
                f"privacy={sorted(PRIVACY_LEVELS)}\nroute={sorted(ROUTES)}\n"
                f"{_matrix_guidance()}\n"
                f"{_direction_guidance()}\n"
                "Return fields: content_domain, visual_form, share_intent, capture_mode, "
                "character_visibility, other_people_visibility, polish, tone, privacy, "
                "primary_evidence_ref, supporting_evidence_refs, composition, action, "
                "camera_direction, sharing_motive, constraints, and route. Never return intimate_intensity."
                " Return exactly one interaction_bid_id from the supplied candidates. Also return "
                "presentation_candidate_id for generated character_media; omit it otherwise. Interaction bids "
                "are invitations, never claims that the recipient will respond."
            ),
        },
    ]


def _planning_messages_v5(
    opportunity: MediaOpportunity,
    recent: tuple[str, ...],
    complete_candidates: tuple[dict[str, object], ...],
    interaction_bids: tuple[dict[str, object], ...],
) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are MediaPlanner v5. Return one JSON object only. Select event-grounded content "
                "classification, RFC 6901 evidence pointers, one interaction bid, and one supplied "
                "complete_candidate_id. The complete candidate is indivisible: do not return or rewrite "
                "composition, camera geometry, action, expression, pose, embodied strategy, attraction "
                "mechanism, or identity references. Never invent facts, people, readable text, body state, "
                "private apparel, or a completed future event."
            ),
        },
        {
            "role": "user",
            "content": (
                f"opportunity_id={opportunity.opportunity_id}\nfamily={opportunity.family}\n"
                f"privacy_ceiling={opportunity.privacy_ceiling}\n"
                f"expression_charge_ceiling={_expression_charge_ceiling(opportunity)}\n"
                f"delivery_mode={opportunity.delivery_mode}\n"
                f"audience_context={_stable_json(asdict(opportunity.audience_context) if opportunity.audience_context else {})}\n"
                f"event_snapshot={_stable_json(opportunity.event_snapshot)}\n"
                f"private_expression_basis={_stable_json(opportunity.private_expression_basis.__dict__ if opportunity.private_expression_basis else {})}\n"
                f"hard_banned_fingerprints_last_12={_stable_json(recent)}\n"
                f"legal_complete_media_expression_candidates={_stable_json(complete_candidates)}\n"
                f"legal_interaction_bid_candidates={_stable_json(interaction_bids)}\n"
                "Return fields: content_domain, visual_form, share_intent, capture_mode, "
                "character_visibility, other_people_visibility, polish, tone, privacy, "
                "primary_evidence_ref, supporting_evidence_refs, constraints, route, "
                "interaction_bid_id, complete_candidate_id. When private_expression_basis is non-empty, "
                "include its frozen basis evidence pointer among primary or "
                "supporting evidence. Never return free visual directions or "
                "intimate_intensity.\n"
                f"content_domain={sorted(CONTENT_DOMAINS)}\nvisual_form={sorted(VISUAL_FORMS)}\n"
                f"share_intent={sorted(SHARE_INTENTS)}\ncapture_mode={sorted(CAPTURE_MODES)}\n"
                f"character_visibility={sorted(CHARACTER_VISIBILITIES)}\n"
                f"other_people_visibility={sorted(OTHER_PEOPLE_VISIBILITIES)}\n"
                f"polish={sorted(POLISH_LEVELS)}\ntone={sorted(TONES)}\n"
                f"privacy={sorted(PRIVACY_LEVELS)}\nroute={sorted(ROUTES)}\n{_matrix_guidance()}"
            ),
        },
    ]


def _interaction_bid_values(
    opportunity: MediaOpportunity,
    *,
    config_path: Path,
) -> dict[str, dict[str, object]]:
    catalog = load_interaction_catalog(config_path)
    available: dict[str, dict[str, object]] = {}
    for bid_id, raw in catalog.items():
        minimum_privacy = str(raw.get("minimum_privacy") or "ordinary")
        if minimum_privacy not in _PRIVACY_RANK:
            raise ValueError(f"invalid interaction bid privacy: {bid_id}")
        if _PRIVACY_RANK[minimum_privacy] > _PRIVACY_RANK[opportunity.privacy_ceiling]:
            continue
        if bid_id in {"invite_closeness", "invite_desire"}:
            stage = (
                opportunity.audience_context.relationship_stage
                if opportunity.audience_context
                else ""
            )
            if stage not in {"ambiguous", "lover"}:
                continue
            minimum_charge = "charged" if bid_id == "invite_desire" else "subtle"
            if (
                SENSUAL_CHARGE_RANK[opportunity.sensual_charge_ceiling]
                < SENSUAL_CHARGE_RANK[minimum_charge]
            ):
                continue
        pressure = str(raw.get("response_pressure") or "")
        hoped_response = str(raw.get("hoped_response") or "")
        if pressure not in {"none", "low", "medium"} or not hoped_response:
            raise ValueError(f"invalid interaction bid: {bid_id}")
        available[bid_id] = raw
    return available


def _planner_interaction_bids(
    opportunity: MediaOpportunity,
    *,
    config_path: Path,
) -> tuple[dict[str, object], ...]:
    values = _interaction_bid_values(opportunity, config_path=config_path)
    return tuple(
        {
            "interaction_bid_id": bid_id,
            "communicative_goal": bid_id,
            "hoped_response": raw["hoped_response"],
            "response_pressure": raw["response_pressure"],
            "share_intent_affinities": list(raw.get("share_intent_affinities", [])),
        }
        for bid_id, raw in sorted(values.items())
    )


def _inspection_prompt(plan: MediaPlan) -> str:
    if plan.version == PLAN_VERSION_V5:
        return (
            "Inspect this fictional personal-media image against MediaPlan v5. Return JSON only with "
            "passed, reason, observed_summary, observed_facts, deviations, observed_camera_geometry, "
            "camera_geometry_broadly_matches, observed_address_strategy, "
            "address_strategy_broadly_matches, interaction_bid_legible, "
            "capture_relationship_legible, generic_portrait_dilution, "
            "photographic_authenticity_ok, identity_consistency_ok, observed_expression_family, and "
            "perceptual_signature. For plans carrying the newer facial/authenticity contracts, also return "
            "observed_facial_display_strategy, facial_display_strategy_matches, observed_facial_actions "
            "(brow, eye_aperture, gaze, nose_cheek, mouth, asymmetry, temporal_phase), "
            "facial_micro_performance_matches, generic_smile_fallback, "
            "reference_expression_copy_detected, authenticity_profile_matches, "
            "commercial_render_dilution, regional_grounding_matches, and observed_authenticity. "
            "For a frozen Moment Capture contract, also return moment_capture_matches. "
            "Also return every v6 quality, subject, social, embodied and capture "
            "field applicable to the frozen plan. Reject a third-party image that reads as paparazzi or "
            "an authorless AI editorial; a front-camera image lacking a credible visible self-authorship "
            "relationship (operator hand/forearm operating the phone or a partial device) or that contradicts its frozen distance, "
            "occupancy or device physics; a mirror image lacking the character visibly holding the reflected "
            "phone with a physically consistent hand, reflection and camera angle; invite_desire diluted into a polite generic portrait; camera, "
            "hands, action or authorship conflicts; copied reference head tilt/smile/hair/framing; identity "
            "drift; structural defects; invented private facts; non-explicit boundary violations; a frozen "
            "social performance collapsed into the same polite small smile; a reference image's exact face "
            "performance copied into the output; ordinary personal media inflated into a commercial render; "
            "or regional visual claims unsupported by selected evidence. Treat the frozen facial contract as "
            "one coherent visible still-frame beat, never as a diagnosis or a requirement to show multiple times. "
            f"perceptual_signature must use exactly this ordered schema: {PERCEPTUAL_SIGNATURE_VERSION}|"
            "engagement_tactic|attraction_mechanism|shot_distance|camera_height|view_axis|"
            "camera_face_distance|face_radial_position|subject_occupancy|subject_placement|orientation|"
            "display_family|gaze_sequence|nose_cheek_action|mouth_action|performance_authorship|"
            "temporal_phase|expression_beat|pose|embodied_strategy|aesthetic_intent|scene_orderliness|"
            "capture_imperfection|visual_form|identity_references. Describe observed values, not merely "
            "copying the planned values. Frozen inspection contract: "
            f"{_stable_json(_inspection_contract_payload(plan))}"
        )
    subject_fields = (
        " Also return observed_subject_presentation as an object with visible hair_arrangement, "
        "head_yaw, head_pitch, head_roll, gaze_target, expression, shoulder_orientation, posture, "
        "gesture and photo_awareness; return reference_pose_copy as a boolean. Reject when the output "
        "visibly contradicts the frozen subject presentation or copies the identity reference's pose, "
        "gaze, expression, hairstyle, gesture and framing instead of following the plan. "
        f"Planned subject presentation: {_stable_json(plan.subject_presentation.to_payload())}."
        if plan.subject_presentation
        else ""
    )
    quality_fields = (
        " Return garment_topology_ok, hand_sleeve_occlusion_ok, and evidence_attachment_ok as "
        "booleans. Reject fused or impossible cuffs/sleeves/wrists, hands hidden by implausible "
        "garment topology, or selected evidence that floats, merges, or attaches to the wrong "
        "surface. Use true when a check is visibly sound or genuinely not applicable."
    )
    social_fields = (
        " Return observed_photo_display_strategy (short string), "
        "display_strategy_broadly_matches (boolean), expression_artifact_free (boolean), "
        "salient_expression_cues (string array), and forbidden_expression_cues (string array). "
        "Judge the broad social meaning and salient cues, not exact facial-muscle geometry. Reject a "
        "reversed meaning, malformed expression, or a planned forbidden cue; minor auxiliary cue "
        "differences should be deviations rather than rejection."
        if plan.subject_presentation and plan.subject_presentation.display_strategy
        else ""
    )
    capture_contract_fields = (
        " Also return capture_authorship_matches, hand_action_contract_matches, and "
        "social_bid_broadly_legible as booleans. Compare the camera operator, visible device hand, "
        "required free hands, complete action variant, display strategy, and interaction bid as one "
        "contract."
        if plan.embodied_presentation
        and plan.embodied_presentation.version == EMBODIED_PRESENTATION_V2
        else ""
    )
    embodiment_fields = (
        " Return physical_salience_matches, sensual_charge_broadly_matches, coverage_mode_matches, "
        "non_explicit_boundary_ok, and body_framing_non_fetishizing as booleans; also return "
        f"observed_physical_cues and unsupported_physical_cues as string arrays.{capture_contract_fields} "
        "Reject missing planned "
        "bodily salience, ordinary-portrait dilution of charged/veiled intent, unsupported sweat/wet hair/"
        "wardrobe, more exposure than planned, transparent coverage, key-area visibility, sexual acts, "
        "fetishized isolated body-part framing, or impossible straps/sleeves/towels/sheets/mirror anatomy. "
        f"Planned embodied presentation: {_stable_json(plan.embodied_presentation.to_payload())}."
        if plan.embodied_presentation
        else ""
    )
    return (
        "Inspect this fictional personal-media image. Return JSON only with passed (boolean), reason "
        "(string), observed_summary (one factual Chinese sentence), observed_facts (string array), "
        "and deviations (string array). Reject malformed face/hands/body, unwanted text/watermark, "
        "identity mismatch when a reference is supplied, privacy escalation, or a visible contradiction "
        "of capture source, character visibility, people visibility, composition, action, or selected "
        f"evidence.{subject_fields}{quality_fields}{social_fields}{embodiment_fields} Frozen plan: "
        f"{_stable_json(plan.to_payload())[:5000]}"
    )


def _inspection_contract_payload(plan: MediaPlan) -> dict[str, object]:
    """Keep every visual contract while bounding large event evidence values.

    Inspection must not depend on field order or an arbitrary string slice: v5 plans can
    legitimately grow beyond the old prompt cutoff, which used to remove the facial and
    authenticity contracts near the end of the serialized payload.
    """

    payload: dict[str, object] = {
        "version": plan.version,
        "plan_id": plan.plan_id,
        "event_id": plan.event_id,
        "family": plan.family,
        "classification": {
            "content_domain": plan.content_domain,
            "visual_form": plan.visual_form,
            "share_intent": plan.share_intent,
            "capture_mode": plan.capture_mode,
            "character_visibility": plan.character_visibility,
            "other_people_visibility": plan.other_people_visibility,
            "polish": plan.polish,
            "tone": plan.tone,
            "privacy": plan.privacy,
        },
        "selected_evidence": {
            pointer: _compact_value(value) for pointer, value in plan.evidence_values.items()
        },
        "action": {
            "template_id": plan.action_template_id,
            "cue": plan.action_cue,
        },
        "constraints": list(plan.constraints),
        "interaction_bid": (plan.interaction_bid.to_payload() if plan.interaction_bid else None),
        "media_address_strategy": (
            plan.media_address_strategy.to_payload() if plan.media_address_strategy else None
        ),
        "camera_geometry": (plan.camera_geometry.to_payload() if plan.camera_geometry else None),
        "photographic_authenticity": (
            plan.photographic_authenticity.to_payload() if plan.photographic_authenticity else None
        ),
        "moment_capture": (plan.moment_capture.to_payload() if plan.moment_capture else None),
        "identity_reference_selection": (
            plan.identity_reference_selection.to_payload()
            if plan.identity_reference_selection
            else None
        ),
        "subject_presentation": (
            plan.subject_presentation.to_payload() if plan.subject_presentation else None
        ),
        "embodied_presentation": (
            plan.embodied_presentation.to_payload() if plan.embodied_presentation else None
        ),
        "private_expression_basis": (
            plan.private_expression_basis.to_payload() if plan.private_expression_basis else None
        ),
    }
    return payload


def _repair_prompt(prompt: str, inspection: MediaInspection) -> str:
    if inspection.rule_version == INSPECTION_VERSION_V7:
        return (
            f"{prompt}\n\nThe previous v5 image was rejected: {inspection.reason}. "
            f"Visible deviations: {'; '.join(inspection.deviations) or inspection.reason}. "
            "Correct only the observed camera geometry, recipient address, capture relationship, lived "
            "moment continuity, facial legibility, identity, anatomy, or photographic-authenticity defect "
            "named above. Keep the "
            "same event evidence, complete candidate, camera author, geometry, interaction bid, address "
            "strategy, same frozen Moment Capture contract and selected anchor evidence, same frozen facial "
            "display and visible-action contract, embodied contract, wardrobe "
            "facts, coverage and charge; do not replace the face with a generic smile or turn personal media "
            "into a commercial render."
        )
    return (
        f"{prompt}\n\nThe previous image was rejected: {inspection.reason}. "
        f"Visible deviations: {'; '.join(inspection.deviations) or inspection.reason}. "
        "Repair only those visible defects. Keep the same event evidence, classification, subject, "
        "capture authorship, composition intent, privacy, scene, interaction bid, and the same social "
        "performance and embodied presentation; do not select a new photo concept, expression strategy, "
        "sensual-charge level, clothing fact, or body strategy."
    )


def _enforce_inspection_contract(
    inspection: MediaInspection,
    *,
    automatic: bool,
    subject_required: bool = False,
    quality_required: bool = False,
    social_required: bool = False,
    embodied_required: bool = False,
    capture_contract_required: bool = False,
    v5_required: bool = False,
    enhanced_v5_required: bool = False,
    facial_contract_required: bool = False,
    moment_capture_required: bool = False,
    self_authored_capture_required: bool = False,
) -> MediaInspection:
    quality_defects = tuple(
        name
        for name, value in (
            ("garment_topology_failed", inspection.garment_topology_ok),
            ("hand_sleeve_occlusion_failed", inspection.hand_sleeve_occlusion_ok),
            ("evidence_attachment_failed", inspection.evidence_attachment_ok),
        )
        if value is False
    )
    if inspection.passed and quality_defects:
        return replace(
            inspection,
            passed=False,
            reason=quality_defects[0],
            deviations=(*inspection.deviations, *quality_defects),
        )
    social_defects: tuple[str, ...] = ()
    if inspection.display_strategy_broadly_matches is False:
        social_defects += ("display_strategy_contradiction",)
    if inspection.expression_artifact_free is False:
        social_defects += ("malformed_expression",)
    if inspection.forbidden_expression_cues:
        social_defects += ("forbidden_expression_cue",)
    if inspection.passed and social_defects:
        return replace(
            inspection,
            passed=False,
            reason=social_defects[0],
            deviations=(*inspection.deviations, *social_defects),
        )
    embodiment_checks = [
        ("physical_salience_mismatch", inspection.physical_salience_matches),
        ("sensual_charge_mismatch", inspection.sensual_charge_broadly_matches),
        ("coverage_mode_mismatch", inspection.coverage_mode_matches),
        ("explicit_boundary_violation", inspection.non_explicit_boundary_ok),
        ("fetishizing_body_framing", inspection.body_framing_non_fetishizing),
    ]
    if capture_contract_required:
        embodiment_checks.extend(
            [
                ("capture_authorship_mismatch", inspection.capture_authorship_matches),
                ("hand_action_contract_mismatch", inspection.hand_action_contract_matches),
                ("social_bid_not_legible", inspection.social_bid_broadly_legible),
            ]
        )
    embodiment_defects = tuple(name for name, value in embodiment_checks if value is False)
    if inspection.unsupported_physical_cues:
        embodiment_defects += ("unsupported_physical_cue",)
    if inspection.passed and embodiment_defects:
        return replace(
            inspection,
            passed=False,
            reason=embodiment_defects[0],
            deviations=(*inspection.deviations, *embodiment_defects),
        )
    if inspection.passed and inspection.reference_pose_copy:
        return replace(
            inspection,
            passed=False,
            reason="reference_pose_copy",
            deviations=(*inspection.deviations, "copied nuisance pose from identity reference"),
        )
    v5_defects = tuple(
        name
        for name, failed in (
            ("camera_geometry_mismatch", inspection.camera_geometry_broadly_matches is False),
            ("address_strategy_mismatch", inspection.address_strategy_broadly_matches is False),
            ("interaction_bid_not_legible", inspection.interaction_bid_legible is False),
            ("capture_relationship_not_legible", inspection.capture_relationship_legible is False),
            ("generic_portrait_dilution", inspection.generic_portrait_dilution is True),
            ("photographic_authenticity_failed", inspection.photographic_authenticity_ok is False),
            ("identity_consistency_failed", inspection.identity_consistency_ok is False),
            (
                "moment_capture_mismatch",
                moment_capture_required and inspection.moment_capture_matches is False,
            ),
        )
        if failed
    )
    if inspection.passed and v5_defects:
        return replace(
            inspection,
            passed=False,
            reason=v5_defects[0],
            deviations=(*inspection.deviations, *v5_defects),
        )
    facial_defects = tuple(
        name
        for name, failed in (
            (
                "facial_display_strategy_mismatch",
                inspection.facial_display_strategy_matches is False,
            ),
            (
                "facial_micro_performance_mismatch",
                inspection.facial_micro_performance_matches is False,
            ),
            ("generic_smile_fallback", inspection.generic_smile_fallback is True),
            (
                "reference_expression_copy_detected",
                inspection.reference_expression_copy_detected is True,
            ),
        )
        if failed
    )
    authenticity_defects = tuple(
        name
        for name, failed in (
            ("authenticity_profile_mismatch", inspection.authenticity_profile_matches is False),
            ("commercial_render_dilution", inspection.commercial_render_dilution is True),
            ("regional_grounding_mismatch", inspection.regional_grounding_matches is False),
        )
        if failed
    )
    enhanced_defects = authenticity_defects + (facial_defects if facial_contract_required else ())
    if inspection.passed and enhanced_v5_required and enhanced_defects:
        return replace(
            inspection,
            passed=False,
            reason=enhanced_defects[0],
            deviations=(*inspection.deviations, *enhanced_defects),
        )
    missing = ""
    if automatic and not inspection.observed_summary.strip():
        missing = "inspection_summary_missing"
    elif automatic and subject_required and not inspection.observed_subject_presentation:
        missing = "observed_subject_presentation_missing"
    elif (
        automatic
        and quality_required
        and any(
            value is None
            for value in (
                inspection.garment_topology_ok,
                inspection.hand_sleeve_occlusion_ok,
                inspection.evidence_attachment_ok,
            )
        )
    ):
        missing = "inspection_quality_fields_missing"
    elif (
        automatic
        and social_required
        and any(
            value is None
            for value in (
                inspection.display_strategy_broadly_matches,
                inspection.expression_artifact_free,
            )
        )
    ):
        missing = "inspection_social_performance_fields_missing"
    elif (
        automatic
        and embodied_required
        and any(
            value is None
            for value in (
                inspection.physical_salience_matches,
                inspection.sensual_charge_broadly_matches,
                inspection.coverage_mode_matches,
                inspection.non_explicit_boundary_ok,
                inspection.body_framing_non_fetishizing,
            )
        )
    ):
        missing = "inspection_embodiment_fields_missing"
    elif (
        automatic
        and capture_contract_required
        and any(
            value is None
            for value in (
                inspection.capture_authorship_matches,
                inspection.hand_action_contract_matches,
                inspection.social_bid_broadly_legible,
            )
        )
    ):
        missing = "inspection_capture_contract_fields_missing"
    elif (
        automatic
        and v5_required
        and (
            not inspection.observed_camera_geometry
            or not inspection.observed_address_strategy
            or not inspection.perceptual_signature
            or (facial_contract_required and not inspection.observed_expression_family)
            or any(
                value is None
                for value in (
                    inspection.camera_geometry_broadly_matches,
                    inspection.address_strategy_broadly_matches,
                    inspection.interaction_bid_legible,
                    inspection.capture_relationship_legible,
                    inspection.generic_portrait_dilution,
                    inspection.photographic_authenticity_ok,
                    inspection.identity_consistency_ok,
                    inspection.moment_capture_matches if moment_capture_required else True,
                )
            )
        )
    ):
        missing = "inspection_v7_fields_missing"
    elif (
        automatic
        and enhanced_v5_required
        and (
            not inspection.observed_authenticity
            or any(
                value is None
                for value in (
                    inspection.authenticity_profile_matches,
                    inspection.commercial_render_dilution,
                    inspection.regional_grounding_matches,
                )
            )
            or (
                facial_contract_required
                and (
                    not inspection.observed_facial_display_strategy
                    or not inspection.observed_facial_actions
                    or any(
                        value is None
                        for value in (
                            inspection.facial_display_strategy_matches,
                            inspection.facial_micro_performance_matches,
                            inspection.generic_smile_fallback,
                            inspection.reference_expression_copy_detected,
                        )
                    )
                )
            )
        )
    ):
        missing = "inspection_expression_authenticity_fields_missing"
    if (
        not missing
        and self_authored_capture_required
        and inspection.capture_relationship_legible is None
    ):
        missing = "inspection_self_authored_capture_relationship_missing"
    if inspection.passed and missing:
        return replace(
            inspection,
            passed=False,
            reason=missing,
            deviations=(*inspection.deviations, missing.replace("_", " ")),
        )
    return inspection


def _history_fingerprint(item: str | MediaPlan | dict[str, object]) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, MediaPlan):
        return item.diversity_fingerprint
    return str(item.get("diversity_fingerprint") or "")


def _history_subject_signature(item: str | MediaPlan | dict[str, object]) -> str:
    if isinstance(item, str):
        return ""
    if isinstance(item, MediaPlan):
        return item.subject_presentation.subject_signature if item.subject_presentation else ""
    subject = item.get("subject_presentation")
    return str(subject.get("subject_signature") or "") if isinstance(subject, dict) else ""


def _history_embodiment_signature(item: str | MediaPlan | dict[str, object]) -> str:
    if isinstance(item, str):
        return ""
    if isinstance(item, MediaPlan):
        if not item.embodied_presentation:
            return ""
        body = item.embodied_presentation
        return "|".join(
            (
                body.contract_signature,
                body.physical_salience,
                body.sensual_charge,
                body.coverage_mode,
                body.body_strategy_id,
                body.action_variant_id,
            )
        )
    embodiment = item.get("embodied_presentation")
    if not isinstance(embodiment, dict):
        return ""
    return "|".join(
        str(embodiment.get(key) or "")
        for key in (
            "contract_signature",
            "physical_salience",
            "sensual_charge",
            "coverage_mode",
            "body_strategy_id",
            "action_variant_id",
        )
    )


_LEGACY_PERCEPTUAL_SIGNATURE_VERSION = "media-perceptual-v2"
_LEGACY_PERCEPTUAL_SIGNATURE_PARTS = 24


def _upcast_perceptual_signature(signature: str) -> str:
    """Align an observed v2 signature with v3 before positional comparison.

    v3 inserted ``expression_beat`` before the pose axis.  Returning a v2
    value unchanged would make every later comparison interpret pose as an
    expression beat, and so on.  The historic observation has no beat data,
    therefore it gets one explicit legacy sentinel rather than invented data.
    """

    parts = signature.split("|")
    if (
        len(parts) == _LEGACY_PERCEPTUAL_SIGNATURE_PARTS
        and parts[0] == _LEGACY_PERCEPTUAL_SIGNATURE_VERSION
    ):
        return "|".join(
            (
                PERCEPTUAL_SIGNATURE_VERSION,
                *parts[1:17],
                "legacy_expression_beat",
                *parts[17:],
            )
        )
    return signature


def _history_perceptual_signature(item: str | MediaPlan | dict[str, object]) -> str:
    if isinstance(item, dict):
        inspection = item.get("inspection")
        observed = (
            str(inspection.get("perceptual_signature") or "")
            if isinstance(inspection, dict)
            else str(item.get("perceptual_signature") or "")
        )
        observed = _upcast_perceptual_signature(observed)
        if observed.startswith(PERCEPTUAL_SIGNATURE_VERSION + "|"):
            return observed
        nested = item.get("plan")
        if isinstance(nested, dict):
            item = nested
        try:
            item = MediaPlan.from_payload(item)
        except ValueError:
            return ""
    if not isinstance(item, MediaPlan) or item.version != PLAN_VERSION_V5:
        return ""
    if item.media_address_strategy is None or item.camera_geometry is None:
        return ""
    facial = item.subject_presentation.facial_performance if item.subject_presentation else None
    facial_display = (
        item.subject_presentation.facial_display_strategy if item.subject_presentation else None
    )
    facial_micro = (
        item.subject_presentation.facial_micro_performance if item.subject_presentation else None
    )
    performance = item.subject_presentation.performance if item.subject_presentation else None
    pose = (
        ":".join(
            (
                performance.head_yaw,
                performance.shoulder_orientation,
                performance.posture,
                performance.gesture,
            )
        )
        if performance
        else "none"
    )
    refs = (
        ",".join(item.identity_reference_selection.asset_ids)
        if item.identity_reference_selection
        else "none"
    )
    return build_perceptual_signature(
        engagement_tactic=item.media_address_strategy.engagement_tactic,
        attraction_mechanism=item.media_address_strategy.attraction_mechanism or "none",
        shot_distance=item.camera_geometry.shot_distance,
        camera_height=item.camera_geometry.camera_height,
        view_axis=item.camera_geometry.view_axis,
        camera_face_distance=item.camera_geometry.camera_face_distance,
        face_radial_position=item.camera_geometry.face_radial_position,
        subject_occupancy=item.camera_geometry.subject_occupancy,
        subject_placement=item.camera_geometry.subject_placement,
        orientation=item.camera_geometry.orientation,
        display_family=(
            facial_display.strategy_family
            if facial_display
            else facial.expression_family
            if facial
            else "no_face"
        ),
        gaze_sequence=(
            facial_micro.gaze_sequence
            if facial_micro
            else facial.gaze_sequence
            if facial
            else "no_face"
        ),
        nose_cheek_action=(
            facial_micro.nose_cheek_action if facial_micro else "legacy_face_action"
        ),
        mouth_action=(facial_micro.mouth_action if facial_micro else "legacy_mouth_action"),
        performance_authorship=(
            facial_micro.performance_authorship if facial_micro else "legacy_authorship"
        ),
        temporal_phase=(facial_micro.temporal_phase if facial_micro else "legacy_temporal"),
        expression_beat=(
            facial_micro.expression_beat_id if facial_micro else "legacy_expression_beat"
        ),
        pose=pose,
        embodied_strategy=(
            item.embodied_presentation.body_strategy_id if item.embodied_presentation else "none"
        ),
        aesthetic_intent=(
            item.photographic_authenticity.aesthetic_intent
            if item.photographic_authenticity
            else "legacy_authenticity"
        ),
        scene_orderliness=(
            item.photographic_authenticity.scene_orderliness
            if item.photographic_authenticity
            else "legacy_orderliness"
        ),
        capture_imperfection=(
            item.photographic_authenticity.capture_imperfection
            if item.photographic_authenticity
            else "legacy_imperfection"
        ),
        visual_form=item.visual_form,
        identity_references=refs,
    )


def _planner_character_candidates(
    opportunity: MediaOpportunity,
    *,
    recent_subjects: tuple[str, ...],
    recent_embodiments: tuple[str, ...],
    subject_config_path: Path,
    embodiment_config_path: Path,
    limit: int = 8,
) -> tuple[dict[str, object], ...]:
    """Compose complete legal subject/body candidates before the LLM chooses one."""
    if opportunity.family != "character_media":
        return ()
    subjects = _planner_subject_candidates(
        opportunity,
        recent_subjects=recent_subjects,
        config_path=subject_config_path,
    )
    relationship_stage = (
        opportunity.audience_context.relationship_stage if opportunity.audience_context else ""
    )
    embodiments = build_embodied_candidates(
        snapshot=opportunity.event_snapshot,
        opportunity_id=opportunity.opportunity_id,
        relationship_stage=relationship_stage,
        sensual_charge_ceiling=opportunity.sensual_charge_ceiling,
        recent_signatures=recent_embodiments,
        config_path=embodiment_config_path,
        limit=256,
    )
    combined: list[dict[str, object]] = []
    for subject in subjects:
        subject_modes = {str(item) for item in subject.get("legal_capture_modes", [])}
        for body in embodiments:
            legal_modes = []
            performance = subject.get("performance", {})
            hand_occupancy = str(
                performance.get("hand_occupancy") if isinstance(performance, dict) else ""
            )
            for mode in sorted(subject_modes & set(body.legal_capture_modes)):
                if not embodied_capture_feasibility_error(
                    body.presentation,
                    capture_mode=mode,
                    hand_occupancy=hand_occupancy,
                ):
                    legal_modes.append(mode)
            if not legal_modes:
                continue
            subject_payload = {
                "variant_id": subject["subject_variant_id"],
                "appearance": subject["appearance"],
                "performance": subject["performance"],
                "subject_signature": subject["subject_signature"],
                "version": "subject-presentation-v2",
                "display_strategy": subject["display_strategy"],
            }
            subject_contract_id = sha256(_stable_json(subject_payload).encode("utf-8")).hexdigest()[
                :12
            ]
            combined.append(
                {
                    "presentation_candidate_id": (
                        f"{subject['subject_variant_id']}~"
                        f"{subject_contract_id}@@{body.candidate_id}"
                    ),
                    "subject_presentation": subject_payload,
                    "embodied_presentation": body.presentation.to_payload(),
                    "character_visibility": subject["character_visibility"],
                    "minimum_privacy": subject["display_strategy"]["minimum_privacy"],
                    "legal_capture_modes": legal_modes,
                    "legal_share_intents": list(body.legal_share_intents),
                    "capture_physics_contracts": {
                        mode: {
                            "camera_authorship": _camera_authorship(mode),
                            "hand_occupancy": hand_occupancy,
                            "required_free_hands": body.presentation.required_free_hands,
                            "camera_support": body.presentation.camera_support,
                        }
                        for mode in legal_modes
                    },
                }
            )

    def stable_key(item: dict[str, object]) -> tuple[str, str]:
        candidate_id = str(item["presentation_candidate_id"])
        return (
            sha256(f"{opportunity.opportunity_id}:{candidate_id}".encode()).hexdigest(),
            candidate_id,
        )

    # A pure random top-eight can accidentally erase a capture source or body-detail
    # option. Greedily cover the legal capture/visibility surface, with stable seeded
    # tie-breaking, then spend remaining slots on variety.
    universe: set[tuple[str, str, str]] = set().union(
        *(_candidate_coverage_axes(item) for item in combined)
    )
    social_universe = {
        (str(item["character_visibility"]), goal)
        for item in combined
        for goal in item["subject_presentation"]["display_strategy"]["communicative_goals"]
    }
    uncovered = set(universe)
    social_uncovered = set(social_universe)
    remaining = sorted(combined, key=stable_key)
    selected: list[dict[str, object]] = []
    available_charges = sorted(
        {str(item["embodied_presentation"]["sensual_charge"]) for item in remaining},
        key=SENSUAL_CHARGE_RANK.__getitem__,
    )
    for charge in available_charges:
        charge_candidates = [
            item for item in remaining if item["embodied_presentation"]["sensual_charge"] == charge
        ]
        if not charge_candidates or len(selected) >= limit:
            continue
        preferred_goal = "invite_desire" if charge in {"charged", "veiled"} else None
        if preferred_goal:
            compatible = [
                item
                for item in charge_candidates
                if preferred_goal
                in item["subject_presentation"]["display_strategy"]["communicative_goals"]
            ]
            if compatible:
                charge_candidates = compatible
        charge_modes: set[str] = set()
        quota = 1 if charge == "none" else 2
        for _ in range(quota):
            available = [item for item in charge_candidates if item in remaining]
            if not available or len(selected) >= limit:
                break
            choice = min(
                available,
                key=lambda item: (
                    -sum(mode not in charge_modes for mode in item["legal_capture_modes"]),
                    not bool(item["embodied_presentation"]["physical_cues"])
                    if charge in {"charged", "veiled"}
                    else False,
                    min(
                        (
                            {
                                "character_front_camera": 0,
                                "mirror": 1,
                                "timer_fixed": 2,
                                "known_companion": 3,
                                "requested_helper": 4,
                                "character_rear_camera": 5,
                                "external_sender": 6,
                            }.get(str(mode), 99)
                            for mode in item["legal_capture_modes"]
                        ),
                        default=99,
                    )
                    if charge in {"charged", "veiled"}
                    else 0,
                    stable_key(item),
                ),
            )
            selected.append(choice)
            remaining.remove(choice)
            charge_modes.update(str(mode) for mode in choice["legal_capture_modes"])
            uncovered -= _candidate_coverage_axes(choice)
            social_uncovered -= {
                (str(choice["character_visibility"]), goal)
                for goal in choice["subject_presentation"]["display_strategy"][
                    "communicative_goals"
                ]
            }
    while remaining and len(selected) < limit:
        best = min(
            remaining,
            key=lambda item: (
                -len(_candidate_coverage_axes(item) & uncovered),
                -(
                    _candidate_social_affinity(opportunity, item)
                    if any(
                        (str(item["character_visibility"]), goal) in social_uncovered
                        for goal in item["subject_presentation"]["display_strategy"][
                            "communicative_goals"
                        ]
                    )
                    else 0
                ),
                -sum(
                    (str(item["character_visibility"]), goal) in social_uncovered
                    for goal in item["subject_presentation"]["display_strategy"][
                        "communicative_goals"
                    ]
                ),
                stable_key(item),
            ),
        )
        selected.append(best)
        remaining.remove(best)
        uncovered -= _candidate_coverage_axes(best)
        social_uncovered -= {
            (str(best["character_visibility"]), goal)
            for goal in best["subject_presentation"]["display_strategy"]["communicative_goals"]
        }
        if not uncovered:
            break
    for item in remaining:
        if len(selected) >= limit:
            break
        selected.append(item)
    return tuple(selected)


def _camera_authorship(capture_mode: str) -> str:
    return {
        "character_front_camera": "character_holds_capture_device",
        "character_rear_camera": "character_holds_capture_device",
        "mirror": "character_holds_capture_device",
        "timer_fixed": "fixed_device",
        "requested_helper": "requested_helper_operates_camera",
        "known_companion": "known_companion_operates_camera",
        "external_sender": "external_sender_operates_camera",
        "existing_artifact": "frozen_existing_artifact",
    }.get(capture_mode, "unknown")


def _candidate_coverage_axes(candidate: dict[str, object]) -> set[tuple[str, str, str]]:
    intents = {str(item) for item in candidate.get("legal_share_intents", [])}
    visibility = str(candidate.get("character_visibility") or "")
    axes = {("character_visibility", visibility, intent) for intent in intents}
    for mode_value in candidate.get("legal_capture_modes", []):
        mode = str(mode_value)
        canonical_visibility = "body_detail" if mode == "character_rear_camera" else "identifiable"
        if visibility == canonical_visibility:
            axes.update(("capture_mode", mode, intent) for intent in intents)
    return axes


def _candidate_social_affinity(opportunity: MediaOpportunity, candidate: dict[str, object]) -> int:
    """Softly preserve event-relevant social purposes without making them rules."""
    subject = candidate.get("subject_presentation")
    if not isinstance(subject, dict):
        return 0
    display = subject.get("display_strategy")
    if not isinstance(display, dict):
        return 0
    goals = {str(item) for item in display.get("communicative_goals", [])}
    snapshot = opportunity.event_snapshot
    score = 0
    character = snapshot.get("character")
    if (
        isinstance(character, dict)
        and isinstance(character.get("body_health"), dict)
        and candidate.get("character_visibility") == "body_detail"
    ):
        score += 6 * bool(goals & {"seek_care", "seek_validation"})
        score += 2 * (
            "character_rear_camera" in candidate.get("legal_capture_modes", [])
            and bool(goals & {"seek_care", "seek_validation"})
        )
    if isinstance(snapshot.get("objects"), list):
        score += 2 * bool(goals & {"share_discovery", "invite_opinion"})
    if isinstance(snapshot.get("participants"), list) and snapshot.get("participants"):
        score += 1 * bool(goals & {"share_presence", "invite_playful_exchange"})
    return score


def _planner_subject_candidates(
    opportunity: MediaOpportunity,
    *,
    recent_subjects: tuple[str, ...],
    config_path: Path,
) -> tuple[dict[str, object], ...]:
    if opportunity.family != "character_media":
        return ()
    combined: dict[tuple[str, str], dict[str, object]] = {}
    for visibility in ("identifiable", "body_detail"):
        for capture_mode in sorted(_CHARACTER_CAPTURE_MODES - {"existing_artifact"}):
            for candidate in build_subject_candidates(
                snapshot=opportunity.event_snapshot,
                opportunity_id=opportunity.opportunity_id,
                capture_mode=capture_mode,
                character_visibility=visibility,
                recent_subject_signatures=recent_subjects,
                **_subject_context_kwargs(opportunity),
                config_path=config_path,
            ):
                # The diversity signature intentionally omits hand/occlusion bookkeeping,
                # but capture legality cannot. Merge only byte-identical presentations.
                key = (_stable_json(candidate.presentation.to_payload()), visibility)
                if key not in combined:
                    payload = candidate.planner_payload()
                    payload["character_visibility"] = visibility
                    payload["legal_capture_modes"] = []
                    combined[key] = payload
                modes = combined[key]["legal_capture_modes"]
                if isinstance(modes, list) and capture_mode not in modes:
                    modes.append(capture_mode)
    return tuple(combined[key] for key in sorted(combined))


def _subject_context_kwargs(
    opportunity: MediaOpportunity,
    *,
    privacy: str | None = None,
) -> dict[str, object]:
    audience = opportunity.audience_context
    return {
        "privacy_ceiling": privacy or opportunity.privacy_ceiling,
        "relationship_stage": audience.relationship_stage if audience else "",
        "public_affect": audience.public_affect if audience else None,
        "display_bounds": audience.display_bounds if audience else (),
    }


def _resolve_pointer(document: object, pointer: str) -> object:
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON pointer")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            current = current[int(token)]
        else:
            raise TypeError("cannot traverse scalar")
    return current


def _unselected_fact_mentioned(
    snapshot: dict[str, object],
    selected_pointers: Sequence[str],
    directions: Sequence[str],
) -> str | None:
    """Reject directions that smuggle another known snapshot fact into the plan.

    Novel prose cannot be proven true by string matching, so the model contract
    also forbids factual nouns outside selected evidence.  This deterministic
    check closes the common failure mode where the model notices a real but
    unselected place, participant, object, or body fact elsewhere in the input.
    """
    joined = "\n".join(directions)
    for pointer, value in _scalar_leaves(snapshot):
        selected = any(
            pointer == root or pointer.startswith(f"{root}/") for root in selected_pointers
        )
        if selected or not isinstance(value, str):
            continue
        candidate = value.strip()
        if len(candidate) >= 2 and candidate in joined:
            return pointer
    return None


def _scalar_leaves(value: object, pointer: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            token = str(key).replace("~", "~0").replace("/", "~1")
            yield from _scalar_leaves(item, f"{pointer}/{token}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _scalar_leaves(item, f"{pointer}/{index}")
        return
    yield pointer, value


def _selected_existing_path(snapshot: dict[str, object], evidence: dict[str, object]) -> str | None:
    known = tuple(
        str(item.get("path"))
        for item in _accessible_existing_media(snapshot)
        if isinstance(item.get("path"), str)
    )
    for value in evidence.values():
        if isinstance(value, str) and value in known:
            return value
    return next(iter(known), None)


def _existing_media(snapshot: dict[str, object]) -> list[dict[str, object]]:
    value = snapshot.get("existing_media")
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _accessible_existing_media(snapshot: dict[str, object]) -> list[dict[str, object]]:
    return [
        item
        for item in _existing_media(snapshot)
        if item.get("accessible") is True
        or (isinstance(item.get("path"), str) and Path(str(item["path"])).is_file())
    ]


def _known_companions(snapshot: dict[str, object]) -> list[dict[str, object]]:
    value = snapshot.get("participants")
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, dict)
        and str(item.get("role")) in {"known_companion", "friend", "family"}
    ]


def _has_external_sender(snapshot: dict[str, object]) -> bool:
    source = _mapping(snapshot.get("source"))
    person = str(source.get("person") or source.get("sender") or "")
    return bool(person and person not in {"character", "self"})


def _has_identity_reference(snapshot: dict[str, object]) -> bool:
    return any(item.get("identity_reference") for item in _known_companions(snapshot))


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _compact_value(value: object) -> str:
    if isinstance(value, str):
        return value[:500]
    return _stable_json(value)[:500]


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _matrix_guidance() -> str:
    lines = ["Legal combinations (domain: forms; intents):"]
    for family, matrix in (("life_share", _LIFE_MATRIX), ("character_media", _CHARACTER_MATRIX)):
        lines.append(f"{family}:")
        for domain, (forms, intents) in matrix.items():
            lines.append(f"- {domain}: {','.join(sorted(forms))}; {','.join(sorted(intents))}")
    return "\n".join(lines)


def _direction_guidance() -> str:
    return (
        "Choose the four direction strings verbatim from these catalogs. "
        "Only {primary} may carry a world fact and it is resolved after validation.\n"
        f"composition={sorted(_COMPOSITION_DIRECTIONS)}\n"
        f"action={sorted(_ACTION_DIRECTIONS)}\n"
        f"camera_direction={sorted(_CAMERA_DIRECTIONS)}\n"
        f"sharing_motive={sorted(_MOTIVE_DIRECTIONS)}\n"
        f"constraints may contain only={sorted(_MODEL_CONSTRAINTS)}. Capture-source invariants such "
        "as no selfie arm are added by the compiler; never return them yourself."
    )


def _validate_direction_catalog(
    proposal: dict[str, object], values: dict[str, str], *, check_constraints: bool = True
) -> str | None:
    if proposal.get("composition") not in _COMPOSITION_DIRECTIONS:
        return "unsupported_composition_direction"
    if proposal.get("action") not in _ACTION_DIRECTIONS:
        return "unsupported_action_direction"
    if proposal.get("camera_direction") not in _CAMERA_DIRECTIONS:
        return "unsupported_camera_direction"
    capture_mode = values.get("capture_mode")
    if (
        capture_mode not in _CAPTURE_CAMERA_DIRECTIONS
        or proposal.get("camera_direction") not in _CAPTURE_CAMERA_DIRECTIONS[capture_mode]
    ):
        return "capture_camera_direction_conflict"
    motive = proposal.get("sharing_motive")
    if motive not in _MOTIVE_DIRECTIONS:
        return "unsupported_sharing_motive"
    intimate_motive = "传递克制且非露骨的亲密信号"
    if values["share_intent"] == "intimate_signal" and motive != intimate_motive:
        return "intimate_motive_conflict"
    if values["share_intent"] != "intimate_signal" and motive == intimate_motive:
        return "intimate_motive_conflict"
    constraints = proposal.get("constraints", [])
    if check_constraints:
        if isinstance(constraints, list) and any(
            item not in _MODEL_CONSTRAINTS for item in constraints
        ):
            return "unsupported_model_constraint"
    return None


def _safe_filename(value: str) -> str:
    safe = "".join(
        character if character.isalnum() or character in "-_" else "-" for character in value
    )
    return safe[:120] or "media"


def _image_content(path: Path, _label: str) -> dict[str, object]:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}", "detail": "high"},
    }


def _optional_bool(value: dict[str, object], key: str) -> bool | None:
    item = value.get(key)
    return item if isinstance(item, bool) else None


__all__ = [
    "LegacyMediaShotAdapter",
    "MediaInspection",
    "MediaInspector",
    "MediaOpportunity",
    "MediaPlan",
    "MediaPlanner",
    "MediaRenderFailure",
    "MediaRenderer",
    "NotRenderable",
    "OpenAIMediaInspector",
    "PlannedMedia",
    "RenderedMedia",
    "compile_media_prompt",
]
