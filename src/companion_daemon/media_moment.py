"""Frozen, recipient-aware life-moment contracts for character media.

This is deliberately separate from pose, facial display and camera geometry.
It answers the small but important question those contracts cannot answer alone:
why does this frame feel like it was taken *during a life moment*, rather than
as a finished generic portrait?
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

import yaml

from companion_daemon.media_address import TEMPORAL_BEATS
from companion_daemon.media_camera import CAPTURE_MODES


DEFAULT_MOMENT_CONFIG = Path("configs/media_moment_templates.yaml")
MOMENT_CAPTURE_VERSION_V1 = "moment-capture-v1"
MOMENT_CAPTURE_VERSION = "moment-capture-v2"

MOMENT_MODES = {
    "uninterrupted_activity",
    "interrupted_transition",
    "brief_pause",
    "responsive_reaction",
    "settled_aftermath",
    "recipient_pause",
    "composed_recall",
}
CAMERA_RELATIONS = {
    "self_interruption",
    "reflection_pause",
    "fixed_observation",
    "helper_check_in",
    "companion_catch",
    "external_shared_frame",
    "artifact_inherited",
}
SCENE_ANCHORS = {
    "event_object",
    "task_surface",
    "transient_environment",
    "body_transition",
    "social_context",
    "memory_context",
    "primary_evidence",
}
@dataclass(frozen=True)
class MomentCapture:
    """A signed, shot-local continuity contract.

    It never creates facts. ``scene_anchor`` only decides how already selected
    event evidence should participate in the frame.
    """

    strategy_id: str
    moment_mode: str
    camera_relation: str
    scene_anchor: str
    continuity_cue: str
    anti_static_direction: str
    evidence_refs: tuple[str, ...]
    contract_signature: str
    version: str = MOMENT_CAPTURE_VERSION

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        moment_mode: str,
        camera_relation: str,
        scene_anchor: str,
        continuity_cue: str,
        anti_static_direction: str,
        evidence_refs: Sequence[str] = (),
    ) -> "MomentCapture":
        payload = {
            "strategy_id": strategy_id,
            "moment_mode": moment_mode,
            "camera_relation": camera_relation,
            "scene_anchor": scene_anchor,
            "continuity_cue": continuity_cue,
            "anti_static_direction": anti_static_direction,
            "evidence_refs": tuple(evidence_refs),
        }
        _validate(payload)
        return cls(**payload, contract_signature=_signature(payload))

    def to_payload(self) -> dict[str, object]:
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    def bind_evidence(
        self, *, primary_evidence_ref: str, supporting_evidence_refs: Sequence[str]
    ) -> "MomentCapture":
        """Bind the already selected event evidence without reinterpreting the moment."""

        return self.create(
            strategy_id=self.strategy_id,
            moment_mode=self.moment_mode,
            camera_relation=self.camera_relation,
            scene_anchor=_scene_anchor_from_evidence(primary_evidence_ref),
            continuity_cue=self.continuity_cue,
            anti_static_direction=self.anti_static_direction,
            evidence_refs=(primary_evidence_ref, *supporting_evidence_refs),
        )

    @classmethod
    def from_payload(cls, value: object) -> "MomentCapture":
        if not isinstance(value, dict):
            raise ValueError("moment capture must be an object")
        payload = {
            name: str(value.get(name) or "")
            for name in (
                "strategy_id",
                "moment_mode",
                "camera_relation",
                "scene_anchor",
                "continuity_cue",
                "anti_static_direction",
            )
        }
        payload["evidence_refs"] = tuple(
            str(item) for item in value.get("evidence_refs", []) if str(item)
        )
        _validate(payload)
        version = str(value.get("version") or "")
        if version not in {MOMENT_CAPTURE_VERSION_V1, MOMENT_CAPTURE_VERSION}:
            raise ValueError("unsupported moment capture version")
        expected_signature = (
            _signature_v1(payload) if version == MOMENT_CAPTURE_VERSION_V1 else _signature(payload)
        )
        if str(value.get("contract_signature") or "") != expected_signature:
            raise ValueError("invalid moment capture contract")
        return cls(
            **payload,
            contract_signature=str(value["contract_signature"]),
            version=version,
        )


def choose_moment_capture(
    *,
    temporal_beat: str,
    capture_mode: str,
    visual_form: str,
    stable_seed: str,
    config_path: Path = DEFAULT_MOMENT_CONFIG,
) -> MomentCapture:
    """Return the bounded life-moment interpretation for one candidate."""

    catalog = load_moment_catalog(config_path)
    base = catalog["temporal_beats"].get(temporal_beat)
    if not isinstance(base, dict):
        raise ValueError("unknown moment temporal beat")
    variants = [base, *(item for item in base.get("alternatives", []) if isinstance(item, dict))]
    strategy = {**base, **variants[_stable_index(stable_seed, len(variants))]}
    relations = strategy.get("camera_relations")
    if not isinstance(relations, dict):
        raise ValueError("invalid moment camera relation catalog")
    camera_relation = relations.get(capture_mode)
    if not isinstance(camera_relation, str):
        raise ValueError("unsupported moment capture mode")
    return MomentCapture.create(
        strategy_id=str(strategy["strategy_id"]),
        moment_mode=str(strategy["moment_mode"]),
        camera_relation=camera_relation,
        scene_anchor=_scene_anchor(visual_form=visual_form),
        continuity_cue=str(strategy["continuity_cue"]),
        anti_static_direction=str(strategy["anti_static_direction"]),
    )


@lru_cache(maxsize=8)
def load_moment_catalog(config_path: Path = DEFAULT_MOMENT_CONFIG) -> dict[str, object]:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("version") != MOMENT_CAPTURE_VERSION:
        raise ValueError("invalid moment capture catalog")
    beats = raw.get("temporal_beats")
    if not isinstance(beats, dict):
        raise ValueError("missing moment temporal beats")
    for name, value in beats.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise ValueError("invalid moment beat")
        _validate(
            {
                "strategy_id": str(value.get("strategy_id") or ""),
                "moment_mode": str(value.get("moment_mode") or ""),
                "camera_relation": "self_interruption",
                "scene_anchor": "event_object",
                "continuity_cue": str(value.get("continuity_cue") or ""),
                "anti_static_direction": str(value.get("anti_static_direction") or ""),
                "evidence_refs": (),
            }
        )
        relations = value.get("camera_relations")
        if not isinstance(relations, dict) or set(relations) != set(CAPTURE_MODES):
            raise ValueError("incomplete moment capture relations")
        if any(item not in CAMERA_RELATIONS for item in relations.values()):
            raise ValueError("invalid moment capture relation")
        alternatives = value.get("alternatives", [])
        if not isinstance(alternatives, list):
            raise ValueError("invalid moment alternatives")
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                raise ValueError("invalid moment alternative")
            merged = {**value, **alternative}
            _validate(
                {
                    "strategy_id": str(merged.get("strategy_id") or ""),
                    "moment_mode": str(merged.get("moment_mode") or ""),
                    "camera_relation": "self_interruption",
                    "scene_anchor": "event_object",
                    "continuity_cue": str(merged.get("continuity_cue") or ""),
                    "anti_static_direction": str(merged.get("anti_static_direction") or ""),
                    "evidence_refs": (),
                }
            )
    if set(beats) != TEMPORAL_BEATS:
        raise ValueError("incomplete moment temporal beat catalog")
    return raw


def _scene_anchor(*, visual_form: str) -> str:
    if visual_form == "social_frame":
        return "social_context"
    if visual_form in {"process_pov", "body_detail"}:
        return "body_transition" if visual_form == "body_detail" else "task_surface"
    if visual_form in {"portrait_context", "wide_scene", "full_body"}:
        return "transient_environment"
    return "memory_context"


def _scene_anchor_from_evidence(primary_ref: str) -> str:
    if primary_ref.startswith("/participants/"):
        return "social_context"
    if primary_ref.startswith("/character/visible_physical_state"):
        return "body_transition"
    if primary_ref.startswith("/objects/"):
        return "event_object"
    if primary_ref.startswith("/activity/"):
        return "task_surface"
    if primary_ref.startswith(("/location/", "/environment/")):
        return "transient_environment"
    return "primary_evidence"


def _validate(value: Mapping[str, object]) -> None:
    if not value["strategy_id"]:
        raise ValueError("missing moment strategy id")
    if value["moment_mode"] not in MOMENT_MODES:
        raise ValueError("invalid moment mode")
    if value["camera_relation"] not in CAMERA_RELATIONS:
        raise ValueError("invalid moment camera relation")
    if value["scene_anchor"] not in SCENE_ANCHORS:
        raise ValueError("invalid moment scene anchor")
    if not value["continuity_cue"] or not value["anti_static_direction"]:
        raise ValueError("missing moment direction")
    refs = value.get("evidence_refs", ())
    if not isinstance(refs, tuple) or any(not item.startswith("/") for item in refs):
        raise ValueError("invalid moment evidence references")


def _signature(value: Mapping[str, object]) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _signature_v1(value: Mapping[str, object]) -> str:
    legacy = {
        name: value[name]
        for name in (
            "strategy_id",
            "moment_mode",
            "camera_relation",
            "scene_anchor",
            "continuity_cue",
            "anti_static_direction",
        )
    }
    return _signature(legacy)


def _stable_index(seed: str, length: int) -> int:
    if length < 1:
        raise ValueError("empty moment alternatives")
    return int(sha256(seed.encode()).hexdigest()[:16], 16) % length
