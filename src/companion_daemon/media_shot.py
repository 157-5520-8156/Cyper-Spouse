"""Deterministic, world-grounded plans for personal-media renders."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import yaml

from companion_daemon.llm import ChatModel
from companion_daemon.world_media import WorldMediaDecision


DEFAULT_TEMPLATE_PATH = Path("configs/media_shot_templates.yaml")
_CAPTURE_MODES = {
    "handheld_selfie", "check_in_timer", "check_in_helper", "mirror", "candid_life", "unfiltered",
}
_MOTION_CLASSES = {"transitional", "interaction", "observational", "candid"}
_MAX_CUES = 4


@dataclass(frozen=True)
class MediaShotPlan:
    version: str
    plan_id: str
    media_kind: str
    relationship_tier: str | None
    capture_mode: str
    source_activity_id: str | None
    source_template_id: str | None
    logical_at: str
    location: str | None
    companions: tuple[str, ...]
    scene_category: str
    template_id: str
    action: str
    gaze: str
    expression: str
    framing: str
    camera_angle: str
    environment_cues: tuple[str, ...]
    constraints: tuple[str, ...]
    diversity_fingerprint: str
    motion_class: str | None = None
    motion_cue: str | None = None
    anti_static_constraints: tuple[str, ...] = ()
    camera_authorship: str | None = None
    sharing_motive: str | None = None
    creative_variant_id: str | None = None
    render_direction: str | None = None

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["companions"] = list(self.companions)
        payload["environment_cues"] = list(self.environment_cues)
        payload["constraints"] = list(self.constraints)
        payload["anti_static_constraints"] = list(self.anti_static_constraints)
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> "MediaShotPlan":
        if not is_valid_media_shot_plan(payload):
            raise ValueError("invalid media shot plan")
        value = payload if isinstance(payload, dict) else {}
        return cls(
            version=str(value["version"]),
            plan_id=str(value["plan_id"]),
            media_kind=str(value["media_kind"]),
            relationship_tier=(str(value["relationship_tier"]) if value.get("relationship_tier") else None),
            capture_mode=str(value["capture_mode"]),
            source_activity_id=(str(value["source_activity_id"]) if value.get("source_activity_id") else None),
            source_template_id=(str(value["source_template_id"]) if value.get("source_template_id") else None),
            logical_at=str(value["logical_at"]),
            location=str(value["location"]) if value.get("location") else None,
            companions=tuple(str(item) for item in value.get("companions", [])),
            scene_category=str(value["scene_category"]),
            template_id=str(value["template_id"]),
            action=str(value["action"]),
            gaze=str(value["gaze"]),
            expression=str(value["expression"]),
            framing=str(value["framing"]),
            camera_angle=str(value["camera_angle"]),
            environment_cues=tuple(str(item) for item in value.get("environment_cues", [])),
            constraints=tuple(str(item) for item in value.get("constraints", [])),
            diversity_fingerprint=str(value["diversity_fingerprint"]),
            motion_class=(str(value["motion_class"]) if value.get("motion_class") else None),
            motion_cue=(str(value["motion_cue"]) if value.get("motion_cue") else None),
            anti_static_constraints=tuple(
                str(item) for item in value.get("anti_static_constraints", [])
            ),
            camera_authorship=(str(value["camera_authorship"]) if value.get("camera_authorship") else None),
            sharing_motive=(str(value["sharing_motive"]) if value.get("sharing_motive") else None),
            creative_variant_id=(
                str(value["creative_variant_id"]) if value.get("creative_variant_id") else None
            ),
            render_direction=(str(value["render_direction"]) if value.get("render_direction") else None),
        )

    def prompt_block(self) -> str:
        source = (
            f"active world activity={self.source_activity_id}, template={self.source_template_id}, "
            f"logical time={self.logical_at}"
            if self.source_activity_id
            else "no active world activity supports a specific location or trip"
        )
        location = self.location or "no asserted location"
        companions = ", ".join(self.companions) or "none"
        cues = "; ".join(self.environment_cues) or "ordinary non-specific daily surroundings"
        constraints = "; ".join(self.constraints)
        motion = ""
        if self.motion_class and self.motion_cue:
            anti_static = "; ".join(self.anti_static_constraints)
            motion = (
                f"\n- Motion requirement: {self.motion_class}. Visible motion evidence: {self.motion_cue}."
                f"\n- Anti-static delivery constraints: {anti_static}."
            )
        direction = (
            f"\n- Creative rendering direction ({self.creative_variant_id}): {self.render_direction}."
            if self.creative_variant_id and self.render_direction
            else ""
        )
        authorship = (
            f"\n- Camera authorship: {self.camera_authorship}."
            if self.camera_authorship
            else ""
        )
        motive = f"\n- Sharing impulse: {self.sharing_motive}." if self.sharing_motive else ""
        return (
            "\n\nFrozen world media shot plan (must follow):\n"
            f"- Evidence: {source}.\n"
            f"- Location: {location}. Registered companions: {companions}.\n"
            f"- Scene category: {self.scene_category}.\n"
            f"- Subject action: {self.action}. Gaze: {self.gaze}. Expression: {self.expression}.\n"
            f"- Framing: {self.framing}. Camera angle: {self.camera_angle}.\n"
            f"- Visible scene cues: {cues}.\n"
            f"- Non-negotiable constraints: {constraints}.{authorship}{motive}{motion}{direction}"
        )


@dataclass(frozen=True)
class CreativeDirectionVariant:
    """A safe, reusable photographic expression rather than a world fact."""

    variant_id: str
    intent: str
    baseline_direction: str


class MediaShotDirector:
    """Use one bounded LLM call to make a frozen shot feel personally chosen.

    The model can select an allowed expression and describe only pose, gaze,
    timing and camera feel.  It never sees authority to alter activity, place,
    companions, capture mode or the anti-static constraints already frozen in
    ``MediaShotPlan``.  A deterministic catalog fallback preserves rendering
    when the model is unavailable or returns an invalid envelope.
    """

    def __init__(self, model: ChatModel | None, template_path: Path = DEFAULT_TEMPLATE_PATH):
        self.model = model
        self.template_path = template_path

    async def direct(self, plan: MediaShotPlan) -> MediaShotPlan:
        variants = _creative_variants(_load_templates(str(self.template_path.resolve())), plan.capture_mode)
        if not variants:
            return plan
        fallback = variants[_stable_index(plan.plan_id, len(variants))]
        chosen = fallback
        direction = fallback.baseline_direction
        if self.model is not None:
            try:
                raw = await _complete_json(self.model, self._messages(plan, variants))
                response = _json_object(raw)
                candidate = next(
                    (item for item in variants if item.variant_id == response.get("variant_id")),
                    None,
                )
                proposed = str(response.get("render_direction") or "").strip()
                if candidate and _safe_render_direction(proposed, plan):
                    chosen = candidate
                    direction = proposed
            except Exception:
                # Visual direction is optional embellishment, never a reason to
                # fail a legitimate, already-authorized media request.
                pass
        return replace(
            plan,
            version="media-shot-v3",
            creative_variant_id=chosen.variant_id,
            render_direction=direction,
        )

    @staticmethod
    def _messages(
        plan: MediaShotPlan, variants: tuple[CreativeDirectionVariant, ...]
    ) -> list[dict[str, str]]:
        allowed = "\n".join(
            f"- {item.variant_id}: {item.intent}. Baseline: {item.baseline_direction}"
            for item in variants
        )
        return [
            {
                "role": "system",
                "content": (
                    "You are a constrained personal-photo art director. Return one JSON object only. "
                    "You cannot decide whether a photo is sent and cannot add, remove, or reinterpret "
                    "world facts. Select one allowed variant_id and write render_direction in one short "
                    "sentence (max 180 characters) about only pose, gaze, expression, timing, or camera feel. "
                    "Do not mention a place, event, companion, object, outfit, time, backstory, text, or any new fact. "
                    "Keep it like a believable photo someone would choose to share, not a fashion editorial or paparazzi image."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Frozen camera facts (do not alter):\n"
                    f"capture_mode={plan.capture_mode}; action={plan.action}; gaze={plan.gaze}; "
                    f"expression={plan.expression}; framing={plan.framing}; camera={plan.camera_angle}; "
                    f"camera_authorship={plan.camera_authorship or 'unspecified'}; "
                    f"sharing_motive={plan.sharing_motive or 'ordinary update'}; "
                    f"motion={plan.motion_cue or 'none'}; anti_static={'; '.join(plan.anti_static_constraints)}\n"
                    f"Allowed expressive variants:\n{allowed}\n"
                    'Return {"variant_id":"...","render_direction":"..."}. '
                ),
            },
        ]


class MediaShotPlanner:
    """Compile one replayable shot plan from accepted world facts and a stable seed."""

    def __init__(self, template_path: Path = DEFAULT_TEMPLATE_PATH):
        self.template_path = template_path

    def plan(
        self,
        snapshot: dict[str, object],
        decision: WorldMediaDecision,
        request_id: str,
    ) -> MediaShotPlan:
        capture_mode = str(decision.capture_mode or "handheld_selfie")
        if capture_mode not in _CAPTURE_MODES:
            raise ValueError(f"unsupported capture mode: {capture_mode}")
        activity = _active_activity(snapshot)
        companions = _companions(snapshot, activity)
        if capture_mode == "candid_life" and not companions:
            raise ValueError("candid life media requires registered companion evidence")
        template_id = str(activity.get("template_id") or "") if activity else ""
        category = _scene_category(self._templates(), template_id, activity)
        candidates = _templates_for(self._templates(), category, capture_mode)
        recent = _recent_fingerprints(snapshot)
        selected = _select_candidate(candidates, request_id, recent)
        location = str(activity.get("location") or "") if activity else ""
        constraints = [str(item) for item in selected.get("constraints", [])]
        motion_class = str(selected.get("motion_class") or "")
        motion_cue = str(selected.get("motion_cue") or "")
        anti_static_constraints = tuple(
            str(item) for item in selected.get("anti_static_constraints", [])
        )
        camera_authorship = str(selected.get("camera_authorship") or _default_camera_authorship(capture_mode))
        sharing_motive = str(selected.get("sharing_motive") or _default_sharing_motive(capture_mode))
        if not activity:
            constraints.append("Do not portray a trip, landmark, completed activity, or companion as established fact.")
        if capture_mode == "check_in_timer":
            constraints.extend((
                "The phone is not held in hand.",
                "No arm reaches toward the camera and both hands are naturally visible.",
            ))
        if capture_mode == "check_in_helper":
            constraints.extend((
                "A passerby or venue staff member holds the rear camera at a normal requested-photo distance.",
                "The helper is not visible and is not an established companion.",
                "The subject is not holding a phone and no arm reaches toward the camera.",
            ))
        if capture_mode == "candid_life":
            constraints.append("Use a third-person viewpoint only; do not add unregistered people.")
        fingerprint = "|".join((
            capture_mode,
            str(selected["action"]),
            str(selected["gaze"]),
            str(selected["framing"]),
            str(selected["camera_angle"]),
            motion_class,
        ))
        return MediaShotPlan(
            version=str(self._templates().get("version") or "media-shot-v1"),
            plan_id=f"shot:{request_id}",
            media_kind=decision.kind,
            relationship_tier=decision.intimacy_tier,
            capture_mode=capture_mode,
            source_activity_id=str(activity.get("activity_id") or "") or None,
            source_template_id=template_id or None,
            logical_at=str(_mapping(snapshot.get("clock")).get("logical_at") or ""),
            location=location or None,
            companions=companions,
            scene_category=category,
            template_id=str(selected["id"]),
            action=str(selected["action"]),
            gaze=str(selected["gaze"]),
            expression=str(selected["expression"]),
            framing=str(selected["framing"]),
            camera_angle=str(selected["camera_angle"]),
            environment_cues=tuple(str(item) for item in selected.get("environment_cues", [])[:_MAX_CUES]),
            constraints=tuple(dict.fromkeys(constraints)),
            diversity_fingerprint=fingerprint,
            motion_class=motion_class,
            motion_cue=motion_cue,
            anti_static_constraints=anti_static_constraints,
            camera_authorship=camera_authorship or None,
            sharing_motive=sharing_motive or None,
        )

    def _templates(self) -> dict[str, object]:
        return _load_templates(str(self.template_path.resolve()))


def is_valid_media_shot_plan(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    required_strings = (
        "version", "plan_id", "media_kind", "capture_mode", "logical_at", "scene_category",
        "template_id", "action", "gaze", "expression", "framing", "camera_angle",
        "diversity_fingerprint",
    )
    if any(not isinstance(payload.get(name), str) or not payload[name].strip() for name in required_strings):
        return False
    version = payload.get("version")
    if version not in {"media-shot-v1", "media-shot-v2", "media-shot-v3"}:
        return False
    if len(_stable_json(payload)) > 4_000 or payload.get("capture_mode") not in _CAPTURE_MODES:
        return False
    for name in ("companions", "environment_cues", "constraints"):
        value = payload.get(name)
        if not isinstance(value, list) or len(value) > 6 or any(
            not isinstance(item, str) or len(item) > 240 for item in value
        ):
            return False
    if version == "media-shot-v2":
        if payload.get("motion_class") not in _MOTION_CLASSES:
            return False
        if (
            not isinstance(payload.get("motion_cue"), str)
            or not payload["motion_cue"].strip()
            or len(payload["motion_cue"]) > 240
        ):
            return False
        anti_static = payload.get("anti_static_constraints")
        if not isinstance(anti_static, list) or not anti_static or len(anti_static) > 6:
            return False
        if any(not isinstance(item, str) or not item.strip() or len(item) > 240 for item in anti_static):
            return False
    if version == "media-shot-v3":
        if payload.get("motion_class") not in _MOTION_CLASSES:
            return False
        if not isinstance(payload.get("motion_cue"), str) or not payload["motion_cue"].strip():
            return False
        anti_static = payload.get("anti_static_constraints")
        if not isinstance(anti_static, list) or not anti_static:
            return False
        variant = payload.get("creative_variant_id")
        direction = payload.get("render_direction")
        if (
            not isinstance(variant, str)
            or not variant.strip()
            or len(variant) > 80
            or not isinstance(direction, str)
            or not direction.strip()
            or len(direction) > 240
        ):
            return False
        for name in ("camera_authorship", "sharing_motive"):
            value = payload.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > 240:
                return False
    return all(
        payload.get(name) in (None, "") or isinstance(payload.get(name), str)
        for name in ("relationship_tier", "source_activity_id", "source_template_id", "location")
    )


def is_world_grounded_media_shot_plan(
    payload: object, snapshot: dict[str, object], request_id: str
) -> bool:
    """Verify a serialized plan was frozen from this exact current world scene."""
    if not is_valid_media_shot_plan(payload) or not isinstance(payload, dict):
        return False
    if payload.get("plan_id") != f"shot:{request_id}":
        return False
    clock = _mapping(snapshot.get("clock"))
    if payload.get("logical_at") != str(clock.get("logical_at") or ""):
        return False
    activity = _active_activity(snapshot)
    if not activity:
        return not any(
            payload.get(name) for name in ("source_activity_id", "source_template_id", "location")
        ) and not payload.get("companions")
    expected_activity_id = str(activity.get("activity_id") or "")
    expected_template_id = str(activity.get("template_id") or "")
    expected_location = str(activity.get("location") or "")
    return (
        payload.get("source_activity_id") == expected_activity_id
        and (payload.get("source_template_id") or "") == expected_template_id
        and (payload.get("location") or "") == expected_location
        and tuple(payload.get("companions") or ()) == _companions(snapshot, activity)
    )


@lru_cache
def _load_templates(path: str) -> dict[str, object]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("templates"), dict):
        raise ValueError(f"media shot templates are invalid: {path}")
    return raw


def _active_activity(snapshot: dict[str, object]) -> dict[str, object]:
    agenda = _mapping(snapshot.get("agenda"))
    active = [item for item in agenda.values() if isinstance(item, dict) and item.get("status") == "active"]
    return dict(sorted(active, key=lambda item: str(item.get("activity_id") or ""))[0]) if active else {}


def _companions(snapshot: dict[str, object], activity: dict[str, object]) -> tuple[str, ...]:
    raw = activity.get("companions") or snapshot.get("current_companions") or ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())[:4]


def _scene_category(templates: dict[str, object], template_id: str, activity: dict[str, object]) -> str:
    categories = _mapping(templates.get("activity_categories"))
    if template_id and isinstance(categories.get(template_id), str):
        return str(categories[template_id])
    title = f"{activity.get('title') or ''} {activity.get('location') or ''}".lower()
    if any(token in title for token in ("展", "摄影", "作品集")):
        return "exploring"
    if any(token in title for token in ("散步", "走")):
        return "walking"
    return "daily"


def _templates_for(templates: dict[str, object], category: str, capture_mode: str) -> list[dict[str, object]]:
    catalog = _mapping(templates.get("templates"))
    source = _mapping(catalog.get(category))
    choices = source.get(capture_mode)
    if not isinstance(choices, list):
        choices = _mapping(catalog.get("daily")).get(capture_mode)
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"no media shot templates for {category}/{capture_mode}")
    required = {"id", "action", "gaze", "expression", "framing", "camera_angle"}
    if templates.get("version") == "media-shot-v2":
        required |= {"motion_class", "motion_cue", "anti_static_constraints"}
    if any(not isinstance(item, dict) or not required.issubset(item) for item in choices):
        raise ValueError(f"invalid media shot template for {category}/{capture_mode}")
    return [dict(item) for item in choices]


def _default_camera_authorship(capture_mode: str) -> str:
    return {
        "handheld_selfie": "the character holding her own front-facing phone",
        "check_in_timer": "a phone propped by the character on a timer",
        "check_in_helper": "a helpful passerby or venue staff member holding the rear camera",
        "mirror": "the character holding her own phone within a believable reflection",
        "candid_life": "a registered companion holding the camera",
        "unfiltered": "the character holding her own front-facing phone",
    }[capture_mode]


def _default_sharing_motive(capture_mode: str) -> str:
    return {
        "handheld_selfie": "a small personal update to someone familiar",
        "check_in_timer": "marking that she was there",
        "check_in_helper": "a lightly posed proof-of-visit she asked someone nearby to take",
        "mirror": "sharing a casual outfit or reflection check",
        "candid_life": "forwarding a moment a companion caught during the activity",
        "unfiltered": "sending a harmless, unpolished update rather than a polished portrait",
    }[capture_mode]


def _creative_variants(
    templates: dict[str, object], capture_mode: str
) -> tuple[CreativeDirectionVariant, ...]:
    catalog = _mapping(templates.get("creative_directions"))
    source = catalog.get(capture_mode) or catalog.get("default") or []
    if not isinstance(source, list):
        return ()
    variants: list[CreativeDirectionVariant] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        variant_id = str(item.get("id") or "").strip()
        intent = str(item.get("intent") or "").strip()
        direction = str(item.get("baseline_direction") or "").strip()
        if variant_id and intent and direction and len(direction) <= 240:
            variants.append(CreativeDirectionVariant(variant_id, intent, direction))
    return tuple(variants)


def _select_candidate(
    candidates: list[dict[str, object]], request_id: str, recent: set[str]
) -> dict[str, object]:
    start = int.from_bytes(sha256(request_id.encode("utf-8")).digest()[:4], "big") % len(candidates)
    for offset in range(len(candidates)):
        candidate = candidates[(start + offset) % len(candidates)]
        fingerprint = "|".join(
            str(candidate.get(name) or "")
            for name in ("action", "gaze", "framing", "camera_angle", "motion_class")
        )
        if not any(fingerprint in previous for previous in recent):
            return candidate
    return candidates[start]


def _recent_fingerprints(snapshot: dict[str, object]) -> set[str]:
    media = _mapping(snapshot.get("media"))
    recent: set[str] = set()
    for item in list(media.values())[-4:]:
        if not isinstance(item, dict) or item.get("status") not in {"generated", "shared"}:
            continue
        plan = _mapping(item.get("shot_plan"))
        fingerprint = plan.get("diversity_fingerprint")
        if isinstance(fingerprint, str):
            recent.add(fingerprint)
    return recent


async def _complete_json(model: ChatModel, messages: list[dict[str, str]]) -> str:
    complete_json = getattr(model, "complete_json", None)
    if callable(complete_json):
        return await complete_json(messages, temperature=0.85)
    return await model.complete(messages, temperature=0.85)


def _json_object(raw: str) -> dict[str, object]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _safe_render_direction(direction: str, plan: MediaShotPlan) -> bool:
    if not direction or len(direction) > 240 or "\n" in direction or "\r" in direction:
        return False
    forbidden = (plan.location, *plan.companions, plan.source_activity_id, plan.source_template_id)
    lowered = direction.casefold()
    if any(token and str(token).casefold() in lowered for token in forbidden):
        return False
    # Direction may refine photographic delivery but must not be a second scene
    # description that smuggles in new people, places, objects, or a story.
    unsafe_markers = (
        "location", "friend", "companion", "gallery", "地点", "场馆", "展览", "旅行",
        "同伴", "朋友", "穿着", "衣服", "背景",
    )
    return not any(marker.casefold() in lowered for marker in unsafe_markers)


def _stable_index(seed: str, size: int) -> int:
    return int.from_bytes(sha256(seed.encode("utf-8")).digest()[:4], "big") % size


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stable_json(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
