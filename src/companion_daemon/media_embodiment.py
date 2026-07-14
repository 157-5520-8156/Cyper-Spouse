"""Shot-local embodied presentation for event-grounded personal media."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
from hashlib import sha256
import json
from math import exp, log
from pathlib import Path
from typing import Mapping, Sequence

import yaml


DEFAULT_EMBODIMENT_CONFIG = Path("configs/media_embodiment_templates.yaml")
VISIBLE_STATE_SCHEMA = "visible-physical-state-v1"
VISIBLE_STATE_POLICY = "visible-physical-state-resolver-v1"

PHYSICAL_SALIENCE_LEVELS = frozenset({"none", "contextual", "foregrounded"})
SENSUAL_CHARGE_LEVELS = frozenset({"none", "subtle", "charged", "veiled"})
SENSUAL_CHARGE_RANK = {"none": 0, "subtle": 1, "charged": 2, "veiled": 3}
COVERAGE_MODES = frozenset(
    {"fully_dressed", "functional_bodywear", "private_apparel", "strategic_cover"}
)

VISIBLE_CUE_IDS = frozenset(
    {
        "perspiration",
        "flush",
        "recovering_breath",
        "damp_hair",
        "wet_skin",
        "rain_damp_fabric",
        "sleepy_face",
        "posture_fatigue",
        "muscle_tension",
    }
)
VISIBLE_CUE_INTENSITIES = frozenset({"light", "moderate", "marked"})
VISIBLE_BODY_REGIONS = frozenset(
    {
        "face",
        "hair",
        "neck",
        "shoulders",
        "arms",
        "hands",
        "torso",
        "waist",
        "legs",
        "clothing",
    }
)


@dataclass(frozen=True)
class VisiblePhysicalCue:
    cue_id: str
    intensity: str
    regions: tuple[str, ...]
    source: str
    evidence_refs: tuple[str, ...]
    logical_at: str
    source_event_id: str
    derivation_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["regions"] = list(self.regions)
        value["evidence_refs"] = list(self.evidence_refs)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "VisiblePhysicalCue":
        if not isinstance(value, dict):
            raise ValueError("visible physical cue must be an object")
        cue = cls(
            cue_id=str(value.get("cue_id") or ""),
            intensity=str(value.get("intensity") or ""),
            regions=tuple(str(item) for item in value.get("regions", [])),
            source=str(value.get("source") or ""),
            evidence_refs=tuple(str(item) for item in value.get("evidence_refs", [])),
            logical_at=str(value.get("logical_at") or ""),
            source_event_id=str(value.get("source_event_id") or ""),
            derivation_id=(
                str(value["derivation_id"]) if value.get("derivation_id") else None
            ),
        )
        if cue.cue_id not in VISIBLE_CUE_IDS:
            raise ValueError("invalid visible physical cue")
        if cue.intensity not in VISIBLE_CUE_INTENSITIES:
            raise ValueError("invalid visible physical cue intensity")
        if any(region not in VISIBLE_BODY_REGIONS for region in cue.regions):
            raise ValueError("invalid visible body region")
        if cue.source not in {"world_fact", "derived"}:
            raise ValueError("invalid visible physical cue source")
        if not cue.logical_at or not cue.source_event_id:
            raise ValueError("visible physical cue requires logical time and source event")
        if not cue.evidence_refs or any(not item.startswith("/") for item in cue.evidence_refs):
            raise ValueError("invalid visible physical cue evidence")
        if cue.source == "derived" and not cue.derivation_id:
            raise ValueError("derived physical cue requires derivation id")
        if cue.source == "world_fact" and cue.derivation_id is not None:
            raise ValueError("world physical cue cannot have derivation id")
        return cue


@dataclass(frozen=True)
class VisiblePhysicalState:
    cues: tuple[VisiblePhysicalCue, ...]
    policy_version: str = VISIBLE_STATE_POLICY

    def to_payload(self) -> dict[str, object]:
        return {
            "policy_version": self.policy_version,
            "cues": [cue.to_payload() for cue in self.cues],
        }


class VisiblePhysicalStateResolver:
    """Resolve visible bodily facts without writing them back to the World."""

    def resolve(self, snapshot: Mapping[str, object]) -> VisiblePhysicalState:
        character = _mapping(snapshot.get("character"))
        frozen = character.get("visible_physical_state")
        if isinstance(frozen, dict):
            return VisiblePhysicalState(self._world_cues(frozen))
        return VisiblePhysicalState(self._derived_cues(snapshot))

    def _world_cues(self, state: Mapping[str, object]) -> tuple[VisiblePhysicalCue, ...]:
        if str(state.get("schema_version") or "") != VISIBLE_STATE_SCHEMA:
            raise ValueError("unsupported visible physical state schema")
        raw_cues = state.get("cues", [])
        if not isinstance(raw_cues, list):
            raise ValueError("visible physical state cues must be a list")
        cues: list[VisiblePhysicalCue] = []
        logical_at = str(state.get("observed_at") or "")
        source_event_ids = state.get("source_event_ids", [])
        source_event_id = (
            str(source_event_ids[0])
            if isinstance(source_event_ids, list) and source_event_ids
            else ""
        )
        if not logical_at or not source_event_id:
            raise ValueError("visible physical state requires provenance")
        for index, raw in enumerate(raw_cues):
            if not isinstance(raw, dict):
                raise ValueError("visible physical state cue must be an object")
            cues.append(
                VisiblePhysicalCue.from_payload(
                    {
                        **raw,
                        "source": "world_fact",
                        "evidence_refs": [
                            f"/character/visible_physical_state/cues/{index}"
                        ],
                        "derivation_id": None,
                        "logical_at": logical_at,
                        "source_event_id": source_event_id,
                    }
                )
            )
        return tuple(cues)

    def _derived_cues(
        self, snapshot: Mapping[str, object]
    ) -> tuple[VisiblePhysicalCue, ...]:
        activity = _mapping(snapshot.get("activity"))
        event = _mapping(snapshot.get("event"))
        kind = str(activity.get("kind") or "").lower()
        intensity = str(activity.get("intensity") or "").lower()
        if kind not in {"exercise", "workout", "running", "dance", "swimming"}:
            return ()
        level = "moderate" if intensity in {"high", "vigorous", "intense"} else "light"
        derivation_id = "exercise-high-v1" if level == "moderate" else "exercise-light-v1"
        logical_at = str(event.get("logical_at") or "")
        source_event_id = str(event.get("event_id") or "")
        if not logical_at or not source_event_id:
            raise ValueError("derived visible physical state requires event provenance")
        base_refs = (
            "/activity/kind",
            *(("/activity/intensity",) if "intensity" in activity else ()),
        )
        values = (
            ("perspiration", ("face", "neck", "arms")),
            ("flush", ("face", "neck")),
            ("recovering_breath", ("torso",)),
        )
        return tuple(
            VisiblePhysicalCue(
                cue_id=cue_id,
                intensity=level,
                regions=regions,
                source="derived",
                evidence_refs=base_refs,
                logical_at=logical_at,
                source_event_id=source_event_id,
                derivation_id=derivation_id,
            )
            for cue_id, regions in values
        )


EMBODIED_PRESENTATION_V1 = "embodied-presentation-v1"


@dataclass(frozen=True)
class EmbodiedPresentation:
    physical_salience: str
    sensual_charge: str
    coverage_mode: str
    body_strategy_id: str
    physical_cues: tuple[VisiblePhysicalCue, ...]
    holistic_cue: str
    framing_cue: str
    action_cue: str
    sensory_cues: tuple[str, ...]
    allowed_regions: tuple[str, ...]
    forbidden_cues: tuple[str, ...]
    relationship_stage_basis: str
    sensual_charge_ceiling: str
    wardrobe_evidence_refs: tuple[str, ...] = ()
    version: str = EMBODIED_PRESENTATION_V1
    contract_signature: str = ""

    @classmethod
    def create(cls, **values: object) -> "EmbodiedPresentation":
        presentation = cls(**values)  # type: ignore[arg-type]
        return cls(
            **{
                **presentation.__dict__,
                "contract_signature": _embodied_signature(presentation),
            }
        )

    def to_payload(self) -> dict[str, object]:
        value = asdict(self)
        value["physical_cues"] = [cue.to_payload() for cue in self.physical_cues]
        value["sensory_cues"] = list(self.sensory_cues)
        value["allowed_regions"] = list(self.allowed_regions)
        value["forbidden_cues"] = list(self.forbidden_cues)
        value["wardrobe_evidence_refs"] = list(self.wardrobe_evidence_refs)
        return value

    @classmethod
    def from_payload(cls, value: object) -> "EmbodiedPresentation":
        if not isinstance(value, dict):
            raise ValueError("embodied presentation must be an object")
        presentation = cls(
            physical_salience=str(value.get("physical_salience") or ""),
            sensual_charge=str(value.get("sensual_charge") or ""),
            coverage_mode=str(value.get("coverage_mode") or ""),
            body_strategy_id=str(value.get("body_strategy_id") or ""),
            physical_cues=tuple(
                VisiblePhysicalCue.from_payload(item)
                for item in value.get("physical_cues", [])
            ),
            holistic_cue=str(value.get("holistic_cue") or ""),
            framing_cue=str(value.get("framing_cue") or ""),
            action_cue=str(value.get("action_cue") or ""),
            sensory_cues=tuple(str(item) for item in value.get("sensory_cues", [])),
            allowed_regions=tuple(str(item) for item in value.get("allowed_regions", [])),
            forbidden_cues=tuple(str(item) for item in value.get("forbidden_cues", [])),
            relationship_stage_basis=str(value.get("relationship_stage_basis") or ""),
            sensual_charge_ceiling=str(value.get("sensual_charge_ceiling") or "none"),
            wardrobe_evidence_refs=tuple(
                str(item) for item in value.get("wardrobe_evidence_refs", [])
            ),
            version=str(value.get("version") or ""),
            contract_signature=str(value.get("contract_signature") or ""),
        )
        _validate_embodied_presentation(presentation)
        if presentation.contract_signature != _embodied_signature(presentation):
            raise ValueError("invalid embodied presentation contract")
        return presentation


@dataclass(frozen=True)
class EmbodiedCandidate:
    candidate_id: str
    presentation: EmbodiedPresentation
    legal_capture_modes: tuple[str, ...]
    legal_share_intents: tuple[str, ...]

    def planner_payload(self) -> dict[str, object]:
        return {
            "embodied_variant_id": self.candidate_id,
            "presentation": self.presentation.to_payload(),
            "legal_capture_modes": list(self.legal_capture_modes),
            "legal_share_intents": list(self.legal_share_intents),
        }


@lru_cache(maxsize=8)
def load_embodiment_catalog(
    path: Path = DEFAULT_EMBODIMENT_CONFIG,
) -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if int(raw.get("version") or 0) != 1 or not isinstance(raw.get("strategies"), dict):
        raise ValueError("invalid embodiment catalog")
    return {
        str(key): dict(value)
        for key, value in raw["strategies"].items()
        if isinstance(value, dict)
    }


def build_embodied_candidates(
    *,
    snapshot: Mapping[str, object],
    opportunity_id: str,
    relationship_stage: str = "",
    sensual_charge_ceiling: str = "none",
    recent_signatures: Sequence[str] = (),
    config_path: Path = DEFAULT_EMBODIMENT_CONFIG,
    limit: int = 8,
) -> tuple[EmbodiedCandidate, ...]:
    """Return deterministic legal body-and-sensuality bundles for one opportunity."""
    if sensual_charge_ceiling not in SENSUAL_CHARGE_RANK:
        raise ValueError("invalid sensual charge ceiling")
    state = VisiblePhysicalStateResolver().resolve(snapshot)
    wardrobe_refs = _private_wardrobe_evidence_refs(snapshot)
    catalog = load_embodiment_catalog(config_path)
    recent = {item.split("|", 1)[0] for item in recent_signatures[-12:] if item}
    recent_three = tuple(recent_signatures[-3:])
    candidates: list[EmbodiedCandidate] = []
    for strategy_id, raw in catalog.items():
        salience = str(raw.get("physical_salience") or "")
        charges = tuple(str(item) for item in raw.get("sensual_charges", []))
        coverage_modes = tuple(str(item) for item in raw.get("coverage_modes", []))
        capture_modes = tuple(str(item) for item in raw.get("capture_modes", []))
        share_intents = tuple(str(item) for item in raw.get("share_intents", []))
        required_cues = {str(item) for item in raw.get("required_physical_cues", [])}
        matched_cues = tuple(cue for cue in state.cues if cue.cue_id in required_cues)
        if required_cues and not matched_cues:
            continue
        selected_cues = matched_cues if required_cues else ()
        for charge in charges:
            if not _relationship_allows_charge(relationship_stage, charge):
                continue
            if SENSUAL_CHARGE_RANK[charge] > SENSUAL_CHARGE_RANK[sensual_charge_ceiling]:
                continue
            for coverage_mode in coverage_modes:
                if (
                    coverage_mode in {"private_apparel", "strategic_cover"}
                    and not wardrobe_refs
                ):
                    continue
                if charge == "veiled" and (
                    coverage_mode not in {"private_apparel", "strategic_cover"}
                    or not wardrobe_refs
                ):
                    continue
                candidate_id = f"{strategy_id}:{charge}:{coverage_mode}"
                presentation = EmbodiedPresentation.create(
                    physical_salience=salience,
                    sensual_charge=charge,
                    coverage_mode=coverage_mode,
                    body_strategy_id=strategy_id,
                    physical_cues=selected_cues,
                    holistic_cue=str(raw.get("holistic_cue") or ""),
                    framing_cue=str(raw.get("framing_cue") or ""),
                    action_cue=str(raw.get("action_cue") or ""),
                    sensory_cues=tuple(str(item) for item in raw.get("sensory_cues", [])),
                    allowed_regions=tuple(str(item) for item in raw.get("allowed_regions", [])),
                    forbidden_cues=tuple(str(item) for item in raw.get("forbidden_cues", [])),
                    relationship_stage_basis=relationship_stage,
                    sensual_charge_ceiling=sensual_charge_ceiling,
                    wardrobe_evidence_refs=(
                        wardrobe_refs
                        if coverage_mode in {"private_apparel", "strategic_cover"}
                        else ()
                    ),
                )
                if presentation.contract_signature in recent:
                    continue
                legal_share_intents = tuple(
                    intent
                    for intent in share_intents
                    if (
                        (charge == "none" and intent != "intimate_signal")
                        or (charge != "none" and intent == "intimate_signal")
                    )
                )
                if not legal_share_intents:
                    continue
                candidates.append(
                    EmbodiedCandidate(
                        candidate_id,
                        presentation,
                        capture_modes,
                        legal_share_intents,
                    )
                )

    def weighted_key(item: EmbodiedCandidate) -> tuple[float, str]:
        soft = sum(
            _embodied_axis_overlap(item.presentation, recent_item)
            for recent_item in recent_three
        )
        stable = sha256(f"{opportunity_id}:{item.candidate_id}".encode()).hexdigest()
        weight = exp(-0.55 * soft)
        uniform = (int(stable[:16], 16) + 1) / ((1 << 64) + 1)
        return -log(uniform) / weight, stable

    return tuple(sorted(candidates, key=weighted_key)[:limit])


def embodiment_prompt_block(presentation: EmbodiedPresentation) -> str:
    cue_text = "; ".join(
        f"{cue.cue_id}={cue.intensity} on {','.join(cue.regions)} "
        f"(source={cue.source}, event={cue.source_event_id}, at={cue.logical_at}, "
        f"evidence={','.join(cue.evidence_refs)})"
        for cue in presentation.physical_cues
    ) or "no additional visible physical-state cue"
    return (
        "Frozen embodied presentation:\n"
        f"- dimensions: physical_salience={presentation.physical_salience}; "
        f"sensual_charge={presentation.sensual_charge}; "
        f"coverage_mode={presentation.coverage_mode}\n"
        f"- whole-body behavior: {presentation.holistic_cue}\n"
        f"- framing: {presentation.framing_cue}\n"
        f"- action: {presentation.action_cue}\n"
        f"- evidenced physical cues: {cue_text}\n"
        f"- sensory treatment: {'; '.join(presentation.sensory_cues) or 'ordinary'}\n"
        f"- regions may appear naturally: {', '.join(presentation.allowed_regions) or 'none emphasized'}\n"
        f"- forbidden: {'; '.join(presentation.forbidden_cues)}. Never isolate one body part "
        "as a fetish subject; keep every key area opaquely covered and the anatomy physically coherent."
    )


def _validate_embodied_presentation(presentation: EmbodiedPresentation) -> None:
    if presentation.version != EMBODIED_PRESENTATION_V1:
        raise ValueError("unsupported embodied presentation version")
    if presentation.physical_salience not in PHYSICAL_SALIENCE_LEVELS:
        raise ValueError("invalid physical salience")
    if presentation.sensual_charge not in SENSUAL_CHARGE_LEVELS:
        raise ValueError("invalid sensual charge")
    if presentation.coverage_mode not in COVERAGE_MODES:
        raise ValueError("invalid coverage mode")
    if presentation.sensual_charge_ceiling not in SENSUAL_CHARGE_RANK:
        raise ValueError("invalid sensual charge ceiling")
    if (
        SENSUAL_CHARGE_RANK[presentation.sensual_charge]
        > SENSUAL_CHARGE_RANK[presentation.sensual_charge_ceiling]
    ):
        raise ValueError("sensual charge exceeds ceiling")
    if not _relationship_allows_charge(
        presentation.relationship_stage_basis, presentation.sensual_charge
    ):
        raise ValueError("relationship stage does not allow sensual charge")
    if presentation.sensual_charge == "veiled" and (
        presentation.coverage_mode not in {"private_apparel", "strategic_cover"}
        or not presentation.wardrobe_evidence_refs
    ):
        raise ValueError("veiled presentation requires wardrobe evidence")
    if (
        presentation.coverage_mode in {"private_apparel", "strategic_cover"}
        and not presentation.wardrobe_evidence_refs
    ):
        raise ValueError("private coverage requires wardrobe evidence")
    if not all(
        (
            presentation.body_strategy_id,
            presentation.holistic_cue,
            presentation.framing_cue,
            presentation.action_cue,
        )
    ):
        raise ValueError("incomplete embodied presentation")
    if any(region not in VISIBLE_BODY_REGIONS for region in presentation.allowed_regions):
        raise ValueError("invalid embodied presentation region")


def _relationship_allows_charge(stage: str, charge: str) -> bool:
    if charge == "none":
        return True
    if charge in {"subtle", "charged"}:
        return stage in {"ambiguous", "lover"}
    return charge == "veiled" and stage == "lover"


def _private_wardrobe_evidence_refs(snapshot: Mapping[str, object]) -> tuple[str, ...]:
    refs: list[str] = []
    appearance = _mapping(_mapping(snapshot.get("character")).get("appearance_state"))
    for field in ("coverage_mode", "outfit_role"):
        value = str(appearance.get(field) or "").lower()
        if _private_wardrobe_text(value):
            refs.append(f"/character/appearance_state/{field}")
    event = _mapping(snapshot.get("event"))
    for field in ("summary", "outcome"):
        if _private_wardrobe_text(str(event.get(field) or "").lower()):
            refs.append(f"/event/{field}")
    objects = snapshot.get("objects", [])
    if isinstance(objects, list):
        for index, item in enumerate(objects):
            if not isinstance(item, dict):
                continue
            for field in ("kind", "description"):
                if _private_wardrobe_text(str(item.get(field) or "").lower()):
                    refs.append(f"/objects/{index}/{field}")
    return tuple(dict.fromkeys(refs))


def _private_wardrobe_text(value: str) -> bool:
    return any(
        token in value
        for token in (
            "private_apparel",
            "strategic_cover",
            "lingerie",
            "underwear",
            "bathrobe",
            "bath towel",
            "bedsheet",
            "oversized shirt",
            "内衣",
            "浴袍",
            "浴巾",
            "床单",
            "宽大衬衫",
        )
    )


def _embodied_signature(presentation: EmbodiedPresentation) -> str:
    values = (
        presentation.version,
        presentation.physical_salience,
        presentation.sensual_charge,
        presentation.coverage_mode,
        presentation.body_strategy_id,
        tuple(tuple(sorted(cue.to_payload().items())) for cue in presentation.physical_cues),
        presentation.holistic_cue,
        presentation.framing_cue,
        presentation.action_cue,
        presentation.sensory_cues,
        presentation.allowed_regions,
        presentation.forbidden_cues,
        presentation.relationship_stage_basis,
        presentation.sensual_charge_ceiling,
        presentation.wardrobe_evidence_refs,
    )
    return _contract_signature(values)


def _embodied_axis_overlap(presentation: EmbodiedPresentation, recent: str) -> int:
    return sum(
        axis in recent
        for axis in (
            presentation.physical_salience,
            presentation.sensual_charge,
            presentation.coverage_mode,
            presentation.body_strategy_id,
        )
    )


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _contract_signature(value: Sequence[object]) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
