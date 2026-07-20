"""Bounded model deliberation for a frozen activity-lifecycle opening set.

This adapter is deliberately not an activity authority.  It receives only
safe prose and opaque tokens compiled by the activity catalog, calls an
injected text model, and returns either ``no_op`` or one token that was
already offered.  It cannot inspect a plan, manufacture an operation, or
write a ledger.  The later proposal-audit/acceptance vertical can persist the
raw and canonical bytes exposed by :class:`ActivityLifecycleModelDraft`.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .schema_core import FrozenModel


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_json_object(raw: str) -> dict[str, object]:
    """Parse exactly one JSON object, rejecting duplicate keys as ambiguous."""

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("ActivityLifecycleDraft model output has duplicate keys")
            value[key] = item
        return value

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("ActivityLifecycleDraft model did not return one valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("ActivityLifecycleDraft model did not return one valid JSON object")
    return value


class ActivityLifecycleDraftModel(Protocol):
    """Minimal injected seam for Flash, Thinking, or deterministic test models.

    The protocol intentionally has no tool, action, ledger, or callback
    capability.  Composition may choose any text model that meets it.
    """

    model: str

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class ActivityLifecycleOpening(FrozenModel):
    """One catalog-issued opaque choice plus the model-safe description."""

    opening_token: str = Field(min_length=1, max_length=512)
    safe_summary: str = Field(min_length=1, max_length=800)


class ActivityLifecycleDraftCapsule(FrozenModel):
    """The only world-derived input accepted by the model adapter.

    There are intentionally no plan IDs, evidence refs, world revisions,
    operation names, actor IDs, or mutable handles in this capsule.
    """

    situation_summary: str = Field(min_length=1, max_length=2_000)
    openings: tuple[ActivityLifecycleOpening, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def opening_tokens_are_unique(self) -> "ActivityLifecycleDraftCapsule":
        tokens = tuple(item.opening_token for item in self.openings)
        if len(set(tokens)) != len(tokens):
            raise ValueError("ActivityLifecycleDraft opening tokens must be unique")
        return self


class ActivityLifecycleModelDraft(FrozenModel):
    """A parsed choice and immutable model-output audit material.

    ``raw_output`` is absent only when an empty catalog deterministically
    yields no-op without a model call.  Such a no-op is not a scripted life
    transition; it simply records that the catalog provided no choice.
    """

    decision: Literal["no_op", "opening_token"]
    opening_token: str | None = Field(default=None, min_length=1, max_length=512)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    raw_output: str | None = None
    raw_output_hash: str | None = None
    normalized_json: str | None = None
    normalized_output_hash: str | None = None

    @model_validator(mode="after")
    def choice_and_audit_shape_are_closed(self) -> "ActivityLifecycleModelDraft":
        has_audit = self.raw_output is not None
        if self.decision == "no_op" and self.opening_token is not None:
            raise ValueError("ActivityLifecycleDraft no_op cannot contain an opening token")
        if self.decision == "opening_token" and self.opening_token is None:
            raise ValueError("ActivityLifecycleDraft opening_token requires a token")
        audit_fields = (
            self.model,
            self.raw_output_hash,
            self.normalized_json,
            self.normalized_output_hash,
        )
        if has_audit != all(value is not None for value in audit_fields):
            raise ValueError("ActivityLifecycleDraft audit fields must be all present or absent")
        if not has_audit and any(value is not None for value in audit_fields):
            raise ValueError("ActivityLifecycleDraft empty-catalog no_op has no model audit")
        return self


def materialize_activity_lifecycle_draft(
    *, raw: str, capsule: ActivityLifecycleDraftCapsule, model: str
) -> ActivityLifecycleModelDraft:
    """Strictly parse a model response against this exact frozen token set."""

    if not model:
        raise ValueError("ActivityLifecycleDraft requires a model identifier")
    payload = _parse_json_object(raw)
    decision = payload.get("decision")
    offered = {opening.opening_token for opening in capsule.openings}
    if decision == "no_op":
        if set(payload) != {"decision"}:
            raise ValueError("ActivityLifecycleDraft no_op may contain only decision")
        normalized = json.dumps({"decision": "no_op"}, ensure_ascii=False, separators=(",", ":"))
        return ActivityLifecycleModelDraft(
            decision="no_op",
            model=model,
            raw_output=raw,
            raw_output_hash=_sha256(raw),
            normalized_json=normalized,
            normalized_output_hash=_sha256(normalized),
        )
    if decision != "select" or set(payload) != {"decision", "opening_token"}:
        raise ValueError("ActivityLifecycleDraft must select one opening token or no_op")
    token = payload["opening_token"]
    if not isinstance(token, str) or token not in offered:
        raise ValueError("ActivityLifecycleDraft selected an unknown opening token")
    normalized = json.dumps(
        {"decision": "select", "opening_token": token},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return ActivityLifecycleModelDraft(
        decision="opening_token",
        opening_token=token,
        model=model,
        raw_output=raw,
        raw_output_hash=_sha256(raw),
        normalized_json=normalized,
        normalized_output_hash=_sha256(normalized),
    )


class ActivityLifecycleDraftAdapter:
    """Call one injected text model without granting any world-write capability."""

    VERSION = "activity-lifecycle-draft.1"

    def __init__(self, *, model: ActivityLifecycleDraftModel, temperature: float = 0.2) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("ActivityLifecycleDraft temperature must be between 0 and 2")
        self._model = model
        self._model_id = (str(getattr(model, "model", "")).strip() or type(model).__name__)[:256]
        self._temperature = temperature

    async def deliberate(self, *, capsule: ActivityLifecycleDraftCapsule) -> ActivityLifecycleModelDraft:
        if not capsule.openings:
            return ActivityLifecycleModelDraft(decision="no_op")
        raw = await self._model.complete(self._messages(capsule), temperature=self._temperature)
        return materialize_activity_lifecycle_draft(raw=raw, capsule=capsule, model=self._model_id)

    @staticmethod
    def _messages(capsule: ActivityLifecycleDraftCapsule) -> list[dict[str, str]]:
        return [
            {
                "role": "system",
                "content": (
                    "Choose at most one offered opaque opening token, or decline. Every opening has already "
                    "passed plan-state, time-relation, capability, and authority checks. Prefer one coherent "
                    "transition so an accepted life plan can actually progress; use no_op only when none of "
                    "the supplied summaries fits the current situation, not merely because details are abstract. "
                    "Return exactly one JSON "
                    'object: {"decision":"no_op"} or '
                    '{"decision":"select","opening_token":"one offered token"}. '
                    "Do not return operations, plan ids, world ids, evidence, revisions, event ids, hashes, "
                    "actions, or extra fields. The token is only a choice label; it is not permission to act."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "situation_summary": capsule.situation_summary,
                        "openings": [item.model_dump(mode="json") for item in capsule.openings],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


__all__ = [
    "ActivityLifecycleDraftAdapter",
    "ActivityLifecycleDraftCapsule",
    "ActivityLifecycleDraftModel",
    "ActivityLifecycleModelDraft",
    "ActivityLifecycleOpening",
    "materialize_activity_lifecycle_draft",
]
