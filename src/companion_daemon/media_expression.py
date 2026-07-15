"""Complete, bounded visual-expression candidates for event media v5.

The planning model selects one ID.  It never assembles camera, address,
facial, pose, or embodied axes independently.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from companion_daemon.media_address import MediaAddressStrategy
from companion_daemon.media_camera import CameraGeometry
from companion_daemon.media_embodiment import (
    EmbodiedPresentation,
    upgrade_embodied_presentation_v3,
)
from companion_daemon.media_subject import (
    SubjectPresentationPlan,
    adapt_subject_for_media_address_v3,
    upgrade_subject_presentation_v3,
)


COMPLETE_CANDIDATE_VERSION = "complete-media-expression-candidate-v1"
IDENTITY_SELECTION_VERSION = "identity-reference-selection-v1"


@dataclass(frozen=True)
class IdentityReferenceSelection:
    asset_ids: tuple[str, ...]
    roles: tuple[str, ...]
    catalog_version: str
    contract_signature: str
    version: str = IDENTITY_SELECTION_VERSION

    @classmethod
    def create(
        cls, *, asset_ids: Sequence[str], roles: Sequence[str], catalog_version: str
    ) -> "IdentityReferenceSelection":
        if not asset_ids or len(asset_ids) != len(roles) or not catalog_version:
            raise ValueError("invalid identity reference selection")
        payload = (tuple(asset_ids), tuple(roles), catalog_version)
        return cls(*payload, contract_signature=_signature(payload))

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["asset_ids"] = list(self.asset_ids)
        value["roles"] = list(self.roles)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "IdentityReferenceSelection":
        if not isinstance(value, dict):
            raise ValueError("identity reference selection must be an object")
        result = cls(
            asset_ids=tuple(str(item) for item in value.get("asset_ids", [])),
            roles=tuple(str(item) for item in value.get("roles", [])),
            catalog_version=str(value.get("catalog_version") or ""),
            contract_signature=str(value.get("contract_signature") or ""),
            version=str(value.get("version") or ""),
        )
        if result.version != IDENTITY_SELECTION_VERSION:
            raise ValueError("unsupported identity reference selection version")
        if not result.asset_ids or len(result.asset_ids) != len(result.roles):
            raise ValueError("invalid identity reference selection")
        if result.contract_signature != _signature(
            (result.asset_ids, result.roles, result.catalog_version)
        ):
            raise ValueError("invalid identity reference selection contract")
        return result


@dataclass(frozen=True)
class CompleteMediaExpressionCandidate:
    candidate_id: str
    action_template_id: str
    action_cue: str
    media_address_strategy: MediaAddressStrategy
    camera_geometry: CameraGeometry
    legal_capture_modes: tuple[str, ...]
    legal_visual_forms: tuple[str, ...]
    legal_share_intents: tuple[str, ...]
    legal_interaction_bids: tuple[str, ...]
    legal_character_visibilities: tuple[str, ...]
    legal_routes: tuple[str, ...]
    subject_presentation: dict[str, object] | None = None
    embodied_presentation: dict[str, object] | None = None
    identity_reference_selection: IdentityReferenceSelection | None = None
    source_presentation_candidate_id: str | None = None
    version: str = COMPLETE_CANDIDATE_VERSION

    def planner_payload(self) -> dict[str, object]:
        return {
            "complete_candidate_id": self.candidate_id,
            "action_template_id": self.action_template_id,
            "action_cue": self.action_cue,
            "media_address_strategy": self.media_address_strategy.to_payload(),
            "camera_geometry": self.camera_geometry.to_payload(),
            "legal_capture_modes": list(self.legal_capture_modes),
            "legal_visual_forms": list(self.legal_visual_forms),
            "legal_share_intents": list(self.legal_share_intents),
            "legal_interaction_bids": list(self.legal_interaction_bids),
            "legal_character_visibilities": list(self.legal_character_visibilities),
            "legal_routes": list(self.legal_routes),
            "subject_presentation": self.subject_presentation,
            "embodied_presentation": self.embodied_presentation,
            "identity_reference_selection": (
                self.identity_reference_selection.to_payload()
                if self.identity_reference_selection
                else None
            ),
            "source_presentation_candidate_id": self.source_presentation_candidate_id,
        }


_BID_ADDRESS_RECIPES: tuple[tuple[tuple[str, ...], dict[str, str | None]], ...] = (
    (
        ("inform_status", "share_presence"),
        dict(
            address_mode="shared_attention",
            engagement_tactic="presence",
            disclosure_mode="open_context",
            staging_degree="camera_aware",
            temporal_beat="just_after",
            visual_priority="primary_evidence",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("coordinate_next_step",),
        dict(
            address_mode="consultative",
            engagement_tactic="coordination",
            disclosure_mode="evidence_first",
            staging_degree="lightly_arranged",
            temporal_beat="held_for_response",
            visual_priority="primary_evidence",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("share_discovery",),
        dict(
            address_mode="shared_attention",
            engagement_tactic="reveal",
            disclosure_mode="selective_focus",
            staging_degree="camera_aware",
            temporal_beat="reaction",
            visual_priority="primary_evidence",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("invite_opinion",),
        dict(
            address_mode="consultative",
            engagement_tactic="question",
            disclosure_mode="evidence_first",
            staging_degree="deliberately_posed",
            temporal_beat="held_for_response",
            visual_priority="primary_evidence",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("invite_appreciation", "celebrate_together"),
        dict(
            address_mode="direct_recipient",
            engagement_tactic="celebration",
            disclosure_mode="polished_display",
            staging_degree="deliberately_posed",
            temporal_beat="held_for_response",
            visual_priority="character",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("invite_playful_exchange", "seek_validation"),
        dict(
            address_mode="direct_recipient",
            engagement_tactic="comic_hook",
            disclosure_mode="unguarded_access",
            staging_degree="camera_aware",
            temporal_beat="reaction",
            visual_priority="character",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("seek_care",),
        dict(
            address_mode="direct_recipient",
            engagement_tactic="vulnerability",
            disclosure_mode="selective_focus",
            staging_degree="camera_aware",
            temporal_beat="aftermath",
            visual_priority="character",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("offer_reassurance",),
        dict(
            address_mode="evidence_mediated",
            engagement_tactic="reassurance",
            disclosure_mode="evidence_first",
            staging_degree="lightly_arranged",
            temporal_beat="just_after",
            visual_priority="primary_evidence",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("revisit_memory",),
        dict(
            address_mode="memory_recall",
            engagement_tactic="nostalgia",
            disclosure_mode="selective_focus",
            staging_degree="existing_artifact",
            temporal_beat="retrospective",
            visual_priority="relationship",
            expression_charge="none",
            attraction_mechanism=None,
        ),
    ),
    (
        ("invite_closeness",),
        dict(
            address_mode="direct_recipient",
            engagement_tactic="affection",
            disclosure_mode="unguarded_access",
            staging_degree="privately_composed",
            temporal_beat="held_for_response",
            visual_priority="relationship",
            expression_charge="subtle",
            attraction_mechanism=None,
        ),
    ),
)

_ATTRACTION_MECHANISMS = (
    "direct_invitation",
    "playful_tease",
    "withheld_attention",
    "sensory_immediacy",
    "private_trust",
    "confident_display",
    "interrupted_transition",
    "close_proximity",
    "atmospheric_suggestion",
)


def build_complete_candidates(
    *,
    opportunity_id: str,
    family: str,
    expression_charge_ceiling: str,
    presentation_candidates: Sequence[Mapping[str, object]] = (),
    recent_perceptual_signatures: Sequence[str] = (),
    identity_assets: Sequence[str] = (),
    reference_pose_metadata: Mapping[str, Mapping[str, str]] | None = None,
    identity_catalog_version: str = "",
    limit: int = 24,
) -> tuple[CompleteMediaExpressionCandidate, ...]:
    """Build a stable, stratified candidate set without consulting a model."""

    recipes = list(_BID_ADDRESS_RECIPES)
    if expression_charge_ceiling in {"charged", "veiled"}:
        allowed_charges = (
            ("charged", "veiled") if expression_charge_ceiling == "veiled" else ("charged",)
        )
        for charge in allowed_charges:
            for mechanism in _ATTRACTION_MECHANISMS:
                if family == "life_share" and mechanism != "atmospheric_suggestion":
                    continue
                recipes.append(
                    (
                        ("invite_desire",),
                        dict(
                            address_mode="direct_recipient",
                            engagement_tactic="attraction",
                            disclosure_mode=(
                                "partial_reveal" if charge == "veiled" else "selective_focus"
                            ),
                            staging_degree="privately_composed",
                            temporal_beat="held_for_response",
                            visual_priority=(
                                "environment"
                                if mechanism == "atmospheric_suggestion"
                                else "character"
                            ),
                            expression_charge=charge,
                            attraction_mechanism=mechanism,
                        ),
                    )
                )
    candidates: list[CompleteMediaExpressionCandidate] = []
    source = list(presentation_candidates) if family == "character_media" else [None]
    for index, (bids, address_values) in enumerate(recipes):
        for subject_index, presentation in enumerate(source):
            charge = str(address_values["expression_charge"])
            embodied = presentation.get("embodied_presentation") if presentation else None
            if embodied and str(embodied.get("sensual_charge")) != charge:
                continue
            modes = tuple(
                str(item)
                for item in (presentation or {}).get("legal_capture_modes", _life_modes(charge))
            )
            intents = tuple(
                str(item)
                for item in (presentation or {}).get("legal_share_intents", _life_intents(charge))
            )
            legal_bids = bids
            for capture_mode in modes:
                geometry, forms = _geometry_for(capture_mode, subject_index + index, family)
                if geometry is None:
                    continue
                forms = tuple(
                    form
                    for form in forms
                    if geometry.compatibility_error(capture_mode=capture_mode, visual_form=form)
                    is None
                )
                if not forms:
                    continue
                resolved_address_values = dict(address_values)
                if capture_mode == "existing_artifact":
                    resolved_address_values["staging_degree"] = "existing_artifact"
                address = MediaAddressStrategy.create(**resolved_address_values)
                subject_payload = None
                embodied_payload = None
                if presentation:
                    upgraded_subject = upgrade_subject_presentation_v3(
                        SubjectPresentationPlan.from_payload(presentation["subject_presentation"])
                    )
                    upgraded_subject = adapt_subject_for_media_address_v3(
                        upgraded_subject,
                        engagement_tactic=address.engagement_tactic,
                        attraction_mechanism=address.attraction_mechanism,
                    )
                    subject_payload = upgraded_subject.to_payload()
                    embodied_payload = upgrade_embodied_presentation_v3(
                        EmbodiedPresentation.from_payload(presentation["embodied_presentation"])
                    ).to_payload()
                for visual_form in forms:
                    candidate_id = (
                        f"expr:{index}:{subject_index}:{capture_mode}:{visual_form}:"
                        f"{charge}:{address.attraction_mechanism or 'none'}"
                    )
                    candidates.append(
                        CompleteMediaExpressionCandidate(
                            candidate_id=candidate_id,
                            action_template_id=_action_template(visual_form, address.temporal_beat),
                            action_cue=_action_cue(visual_form, address.temporal_beat),
                            media_address_strategy=address,
                            camera_geometry=geometry,
                            legal_capture_modes=(capture_mode,),
                            legal_visual_forms=(visual_form,),
                            legal_share_intents=intents,
                            legal_interaction_bids=legal_bids,
                            legal_character_visibilities=(
                                (str(presentation["character_visibility"]),)
                                if presentation
                                else ("none", "trace_only")
                            ),
                            legal_routes=(
                                ("reuse_existing",)
                                if capture_mode == "existing_artifact"
                                else ("generate",)
                            ),
                            subject_presentation=(
                                None if capture_mode == "existing_artifact" else subject_payload
                            ),
                            embodied_presentation=(
                                None if capture_mode == "existing_artifact" else embodied_payload
                            ),
                            identity_reference_selection=(
                                _identity_selection(
                                    geometry,
                                    assets=identity_assets,
                                    metadata=reference_pose_metadata or {},
                                    catalog_version=identity_catalog_version,
                                )
                                if family == "character_media"
                                and capture_mode != "existing_artifact"
                                else None
                            ),
                            source_presentation_candidate_id=(
                                str(presentation["presentation_candidate_id"])
                                if presentation and capture_mode != "existing_artifact"
                                else None
                            ),
                        )
                    )
    recent = {item for item in recent_perceptual_signatures[-12:] if item}
    recent_three = tuple(item for item in recent_perceptual_signatures[-3:] if item)
    candidates = [item for item in candidates if candidate_perceptual_signature(item) not in recent]
    candidates.sort(
        key=lambda item: (
            _perceptual_overlap(candidate_perceptual_signature(item), recent_three),
            _stable_rank(opportunity_id, item.candidate_id),
        )
    )
    selected: list[CompleteMediaExpressionCandidate] = []
    selected_ids: set[str] = set()

    def take(item: CompleteMediaExpressionCandidate) -> None:
        if item.candidate_id not in selected_ids and len(selected) < limit:
            selected.append(item)
            selected_ids.add(item.candidate_id)

    address_keys = sorted(
        {
            (
                item.media_address_strategy.engagement_tactic,
                item.media_address_strategy.attraction_mechanism or "none",
            )
            for item in candidates
        }
    )
    for tactic, mechanism in address_keys:
        matching = [
            item
            for item in candidates
            if item.media_address_strategy.engagement_tactic == tactic
            and (item.media_address_strategy.attraction_mechanism or "none") == mechanism
        ]
        preferred = _preferred_forms(tactic, family)
        take(
            min(
                matching,
                key=lambda item: (
                    0
                    if (
                        family == "character_media"
                        and "identifiable" in item.legal_character_visibilities
                    )
                    or family == "life_share"
                    else 1,
                    preferred.index(item.legal_visual_forms[0])
                    if item.legal_visual_forms[0] in preferred
                    else len(preferred),
                    _stable_rank(opportunity_id, item.candidate_id),
                ),
            )
        )
    forms = sorted({form for item in candidates for form in item.legal_visual_forms})
    for form in forms:
        take(next(item for item in candidates if form in item.legal_visual_forms))
    modes = sorted({mode for item in candidates for mode in item.legal_capture_modes})
    for mode in modes:
        take(next(item for item in candidates if mode in item.legal_capture_modes))
    selected.extend(item for item in candidates if item.candidate_id not in selected_ids)
    return tuple(selected[:limit])


def candidate_perceptual_signature(item: CompleteMediaExpressionCandidate) -> str:
    address = item.media_address_strategy
    camera = item.camera_geometry
    expression = "no_face"
    gaze = "no_face"
    pose = "none"
    if item.subject_presentation:
        face = item.subject_presentation.get("facial_performance") or {}
        performance = item.subject_presentation.get("performance") or {}
        expression = str(face.get("expression_family") or "unknown")
        gaze = str(face.get("gaze_sequence") or "unknown")
        pose = ":".join(
            str(performance.get(key) or "")
            for key in ("head_yaw", "shoulder_orientation", "posture", "gesture")
        )
    embodied = item.embodied_presentation or {}
    refs = (
        ",".join(item.identity_reference_selection.asset_ids)
        if item.identity_reference_selection
        else "none"
    )
    return "|".join(
        (
            address.engagement_tactic,
            camera.shot_distance,
            camera.camera_height,
            camera.view_axis,
            camera.subject_occupancy,
            camera.subject_placement,
            camera.orientation,
            expression,
            gaze,
            pose,
            str(embodied.get("body_strategy_id") or "none"),
            item.legal_visual_forms[0],
            refs,
        )
    )


def _preferred_forms(tactic: str, family: str) -> tuple[str, ...]:
    if family == "life_share":
        return {
            "attraction": ("contextual_still_life", "wide_scene", "subject_closeup"),
            "coordination": ("wide_scene", "process_pov", "subject_closeup"),
            "question": ("subject_closeup", "contextual_still_life"),
        }.get(tactic, ("contextual_still_life", "wide_scene", "process_pov"))
    return {
        "demonstration": ("body_detail", "portrait_context", "full_body"),
        "question": ("portrait_context", "full_body", "body_detail"),
        "comparison": ("body_detail", "portrait_context", "full_body"),
        "celebration": ("portrait_context", "full_body", "portrait_closeup"),
        "attraction": ("portrait_context", "portrait_closeup", "full_body"),
    }.get(tactic, ("portrait_context", "portrait_closeup", "full_body", "social_frame"))


def _perceptual_overlap(signature: str, recent: Sequence[str]) -> int:
    axes = signature.split("|")
    return sum(sum(axis == old for axis, old in zip(axes, item.split("|"))) for item in recent)


def _geometry_for(
    capture: str, variant: int, family: str
) -> tuple[CameraGeometry | None, tuple[str, ...]]:
    table: dict[str, tuple[dict[str, str], tuple[str, ...]]] = {
        "character_front_camera": (
            _geo(
                "close" if variant % 2 else "medium",
                "high" if variant % 3 == 0 else "eye",
                "left_three_quarter" if variant % 2 else "right_three_quarter",
                "portrait" if variant % 3 else "landscape",
                "dominant" if variant % 2 else "balanced",
                "left_third" if variant % 2 else "right_third",
                "supporting",
                "out_of_frame",
                "partial_crop" if variant % 3 == 0 else "casual_offset",
            ),
            ("portrait_closeup", "portrait_context"),
        ),
        "mirror": (
            _geo(
                "full_body" if variant % 2 else "medium",
                "chest",
                "reflection_oblique",
                "portrait",
                "balanced",
                "right_third",
                "supporting",
                "mirror_visible",
                "reflection_layer",
            ),
            ("full_body", "portrait_context"),
        ),
        "timer_fixed": (
            _geo(
                "full_body" if variant % 2 else "medium",
                "low" if variant % 3 == 0 else "chest",
                "front" if variant % 2 else "left_three_quarter",
                "landscape" if variant % 2 else "portrait",
                "balanced",
                "left_third",
                "balanced",
                "fixed_unseen",
                "foreground_interrupt" if variant % 3 == 0 else "clean_intentional",
            ),
            ("full_body", "portrait_context", "wide_scene"),
        ),
        "requested_helper": (
            _geo(
                "long",
                "eye",
                "right_three_quarter",
                "portrait",
                "balanced",
                "right_third",
                "balanced",
                "external_unseen",
                "clean_intentional",
            ),
            ("full_body", "wide_scene"),
        ),
        "known_companion": (
            _geo(
                "wide" if variant % 3 == 0 else "medium",
                "eye",
                "over_shoulder" if variant % 2 else "left_three_quarter",
                "landscape",
                "small" if variant % 3 == 0 else "balanced",
                "edge_right" if variant % 3 == 0 else "left_third",
                "dominant" if variant % 3 == 0 else "supporting",
                "external_unseen",
                "motion_trace" if variant % 3 == 0 else "foreground_interrupt",
            ),
            ("wide_scene", "social_frame")
            if variant % 3 == 0
            else ("portrait_context", "social_frame"),
        ),
        "external_sender": (
            _geo(
                "wide" if variant % 3 == 0 else "medium",
                "eye",
                "environment_pov" if variant % 3 == 0 else "right_three_quarter",
                "landscape",
                "small" if variant % 3 == 0 else "balanced",
                "edge_left" if variant % 3 == 0 else "right_third",
                "dominant" if variant % 3 == 0 else "supporting",
                "external_unseen",
                "motion_trace" if variant % 3 == 0 else "casual_offset",
            ),
            ("wide_scene",) if variant % 3 == 0 else ("portrait_context", "social_frame"),
        ),
        "character_rear_camera": (
            _geo(
                "wide" if variant % 3 == 0 else "close",
                "eye" if variant % 3 == 0 else "chest",
                "environment_pov",
                "landscape",
                ("trace" if family == "life_share" else "small")
                if variant % 3 == 0
                else ("trace" if family == "life_share" else "detail"),
                "not_applicable"
                if family == "life_share"
                else ("edge_left" if variant % 3 == 0 else "lower_frame"),
                "dominant" if variant % 3 == 0 else "supporting",
                "out_of_frame",
                "motion_trace" if variant % 3 == 0 else "foreground_interrupt",
            ),
            ("wide_scene",)
            if variant % 3 == 0
            else ("contextual_still_life", "process_pov", "subject_closeup"),
        ),
        "existing_artifact": (
            _geo(
                "medium",
                "eye",
                "environment_pov",
                "landscape",
                "absent" if family == "life_share" else "balanced",
                "not_applicable" if family == "life_share" else "center",
                "balanced",
                "artifact_inherited",
                "clean_intentional",
            ),
            ("wide_scene", "result_showcase", "portrait_context"),
        ),
    }
    return table.get(capture, (None, ()))


def _geo(
    distance: str,
    height: str,
    axis: str,
    orientation: str,
    occupancy: str,
    placement: str,
    environment: str,
    device: str,
    imperfection: str,
) -> CameraGeometry:
    return CameraGeometry.create(
        shot_distance=distance,
        camera_height=height,
        view_axis=axis,
        pitch="level",
        roll="slight_left" if imperfection in {"casual_offset", "partial_crop"} else "level",
        orientation=orientation,
        subject_occupancy=occupancy,
        subject_placement=placement,
        environment_share=environment,
        focus_behavior="subject_priority"
        if occupancy not in {"absent", "trace"}
        else "evidence_priority",
        imperfection_profile=imperfection,
        device_visibility=device,
    )


def _identity_selection(
    geometry: CameraGeometry,
    *,
    assets: Sequence[str],
    metadata: Mapping[str, Mapping[str, str]],
    catalog_version: str,
) -> IdentityReferenceSelection | None:
    available = tuple(dict.fromkeys(item for item in assets if item and Path(item).is_file()))
    if not available:
        return None
    canonical = available[0]
    target_yaw = {
        "front": "near_front",
        "left_three_quarter": "toward_frame_left",
        "left_profile": "toward_frame_left",
        "rear_three_quarter": "toward_frame_left",
        "right_three_quarter": "toward_frame_right",
        "right_profile": "toward_frame_right",
    }.get(geometry.view_axis, "near_front")
    angle = next(
        (item for item in available if metadata.get(item, {}).get("head_yaw") == target_yaw),
        canonical,
    )
    scale = (
        next((item for item in available if "fullbody" in Path(item).stem.lower()), canonical)
        if geometry.shot_distance in {"full_body", "long", "wide"}
        else canonical
    )
    selected_assets = (
        (canonical,)
        if geometry.subject_occupancy == "detail"
        else tuple(
            dict.fromkeys(
                (canonical, scale, angle)
                if geometry.shot_distance in {"full_body", "long", "wide"}
                else (canonical, angle)
            )
        )
    )
    return IdentityReferenceSelection.create(
        asset_ids=selected_assets,
        roles=tuple(
            "identity_anchor" if item == canonical else "geometry_anchor"
            for item in selected_assets
        ),
        catalog_version=catalog_version or "visual-identity-unknown",
    )


def _life_modes(charge: str) -> tuple[str, ...]:
    return ("character_rear_camera", "known_companion", "external_sender", "existing_artifact")


def _life_intents(charge: str) -> tuple[str, ...]:
    return (
        ("intimate_signal",)
        if charge != "none"
        else (
            "atmosphere",
            "record",
            "show_and_tell",
            "check_in",
            "seek_feedback",
            "progress_update",
            "complain",
            "care_update",
            "humor",
            "memory_keep",
        )
    )


def _action_template(form: str, beat: str) -> str:
    return f"{form}:{beat}:v1"


def _action_cue(form: str, beat: str) -> str:
    base = {
        "wide_scene": "Let the grounded place or transit setting carry the frame, with a human-scale trace rather than a postcard composition",
        "contextual_still_life": "Arrange nothing that was not already present; use nearby surfaces, light, and incidental clutter to make the selected evidence feel personally noticed",
        "process_pov": "Catch the selected activity while it is visibly underway, with believable hand or tool responsibility and a useful sense of sequence",
        "subject_closeup": "Bring the selected evidence close enough to inspect while keeping one contextual clue that explains why it matters now",
        "result_showcase": "Show the completed result together with one honest sign of the work or situation that produced it",
        "portrait_closeup": "Keep the face socially legible while a nearby event clue prevents the image from becoming an interchangeable beauty portrait",
        "portrait_context": "Let the character acknowledge the intended recipient while still visibly belonging to the current activity and place",
        "full_body": "Use the character's stance or ongoing transition to connect outfit, body action, and grounded setting without static mannequin posing",
        "body_detail": "Make the evidenced object or body state readable through a natural showing action, never an isolated fetish crop",
        "social_frame": "Make the known relationship to the photographer or companions legible through spacing, attention, and an ongoing shared beat",
    }.get(form, "Make the selected primary evidence visually central without inventing context")
    beat_cue = {
        "anticipation": "capture the small preparation just before the meaningful action",
        "mid_action": "preserve visible evidence that the action is currently happening",
        "just_after": "retain the physical or emotional residue immediately after the action",
        "reaction": "center the first believable reaction rather than a rehearsed generic smile",
        "held_for_response": "leave a deliberate visual pause that clearly awaits the recipient's response",
        "aftermath": "keep the honest disorder, fatigue, or result that remains afterward",
        "retrospective": "let the artifact or setting carry the sense of looking back",
    }[beat]
    return f"{base}; {beat_cue}."


def _stable_rank(seed: str, candidate_id: str) -> str:
    return sha256(f"{seed}|{candidate_id}".encode()).hexdigest()


def _signature(value: object) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, default=list).encode()
    ).hexdigest()
