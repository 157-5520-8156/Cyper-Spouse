"""Source-bound categorical compute hints derived from a trusted Context Capsule."""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_capsule import ContextCapsule, TrustedContextCapsuleHandle


class RouteHints(BaseModel):
    """No content, secret, reply instruction, or behaviour preference crosses this seam."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    source: Literal["none", "trusted_capsule"] = "none"
    source_capsule_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    ambiguity: Literal["ordinary", "significant"] = "ordinary"
    severity: Literal["ordinary", "moderate", "high", "acute"] = "ordinary"
    conflict_complexity: Literal["ordinary", "complex"] = "ordinary"
    continuity: Literal["transient", "persistent"] = "transient"
    derivation_version: Literal["route-hints.1"] = "route-hints.1"

    @model_validator(mode="after")
    def source_binding_is_complete(self) -> Self:
        if (self.source == "trusted_capsule") != (self.source_capsule_id is not None):
            raise ValueError("route hints require a complete capsule source binding")
        return self


_AMBIGUOUS = frozenset({"uncertainty", "misunderstanding", "user_confused"})
_AMBIGUOUS_USER_AFFECT = frozenset({"confused", "uncertain"})
_NEGATIVE = frozenset(
    {
        "disappointment",
        "dismissal",
        "boundary_violation",
        "dehumanization",
        "coercion",
        "control_pressure",
        "betrayal",
        "loss",
        "reliability_broken",
        "npc_conflict",
    }
)
_SEVERITY_ORDER = {"ordinary": 0, "moderate": 1, "high": 2, "acute": 3}


def derive_route_hints(capsule_handle: TrustedContextCapsuleHandle) -> RouteHints:
    """Compress trusted typed slices to compute-only categories.

    The caller passes the compiler-issued capsule, not arbitrary message text.  Only exact
    typed appraisal and advisory coordinates are inspected; all prose and private values are
    discarded before the router sees the result.
    """

    if type(capsule_handle) is not TrustedContextCapsuleHandle:
        raise ValueError("route hints require a trusted compiler-issued Context Capsule handle")
    capsule = ContextCapsule.model_validate(
        capsule_handle.capsule.model_dump(mode="python", warnings="error")
    )
    meanings: set[str] = set()
    severity = "ordinary"
    negative_appraisals = 0

    for item in capsule.appraisals.items:
        value = json.loads(item.payload_json)
        hypotheses = value.get("hypotheses", ())
        appraisal_is_negative = False
        if isinstance(hypotheses, list):
            for hypothesis in hypotheses:
                if not isinstance(hypothesis, dict):
                    continue
                meaning = hypothesis.get("meaning")
                if isinstance(meaning, str):
                    meanings.add(meaning)
                    appraisal_is_negative = appraisal_is_negative or meaning in _NEGATIVE
                candidate_severity = hypothesis.get("severity")
                if (
                    isinstance(candidate_severity, str)
                    and candidate_severity in _SEVERITY_ORDER
                    and _SEVERITY_ORDER[candidate_severity] > _SEVERITY_ORDER[severity]
                ):
                    severity = candidate_severity
        negative_appraisals += appraisal_is_negative

    for item in capsule.advisories.items:
        value = json.loads(item.payload_json)
        kind = value.get("kind")
        candidates = value.get("candidates", ())
        if not isinstance(kind, str) or not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            weight = candidate.get("weight_bp")
            confidence = candidate.get("confidence_bp")
            label = candidate.get("value")
            if (
                isinstance(weight, int)
                and isinstance(confidence, int)
                and weight >= 2_000
                and confidence >= 5_000
                and isinstance(label, str)
            ):
                if kind in {"appraisal.base", "appraisal.negative", "appraisal.relationship"}:
                    meanings.add(label)
                if kind == "user_affect.signal" and label in _AMBIGUOUS_USER_AFFECT:
                    meanings.add("user_confused")
                if kind == "appraisal.severity" and label in _SEVERITY_ORDER:
                    if _SEVERITY_ORDER[label] > _SEVERITY_ORDER[severity]:
                        severity = label

    persistent = bool(capsule.open_threads.items) or bool(capsule.appraisals.items)
    negative_meanings = meanings & _NEGATIVE
    complex_conflict = negative_appraisals >= 2 or len(negative_meanings) >= 2
    return RouteHints(
        source="trusted_capsule",
        source_capsule_id=capsule.capsule_id,
        ambiguity="significant" if meanings & _AMBIGUOUS else "ordinary",
        severity=severity,
        conflict_complexity="complex" if complex_conflict else "ordinary",
        continuity="persistent" if persistent else "transient",
    )


__all__ = ["RouteHints", "derive_route_hints"]
