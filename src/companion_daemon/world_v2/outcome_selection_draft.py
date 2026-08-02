"""Bounded choice contract for a sidecar-backed world outcome.

An outcome model may choose among already observed, immutable candidate
results.  It cannot manufacture a result, name an occurrence, or author a
settlement.  The dedicated compiler/acceptance chain remains the only writer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Protocol

from pydantic import Field, field_validator

from .schema_core import FrozenModel, PrivacyClass


class OutcomeSelectionModel(Protocol):
    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.2) -> str: ...


class OutcomeSelectionOption(FrozenModel):
    """One model-readable excerpt whose opaque reference is pre-authorized."""

    candidate_result_ref: str = Field(min_length=1, max_length=512)
    summary: str = Field(min_length=1, max_length=12_000)


class CharacterLifeDirectionDraft(FrozenModel):
    """A freely formed subjective direction, never offered by World Author."""

    coordinate_ref: str = Field(
        pattern=r"^biography:direction\.[a-z][a-z0-9._-]{0,53}$"
    )
    summary: str = Field(min_length=1, max_length=12_000)
    context_tags: tuple[str, ...] = Field(min_length=1, max_length=16)
    replaces_context_tag_prefixes: tuple[str, ...] = Field(min_length=1, max_length=8)
    privacy_class: PrivacyClass = "personal"

    @field_validator(
        "context_tags", "replaces_context_tag_prefixes", mode="before"
    )
    @classmethod
    def json_arrays_are_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("context_tags")
    @classmethod
    def values_are_subjective_directions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not item.startswith("direction.") for item in value):
            raise ValueError(
                "character direction values must use a direction.* namespace"
            )
        return value

    @field_validator("replaces_context_tag_prefixes")
    @classmethod
    def prefixes_are_subjective_directions(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(not item.startswith("direction.") for item in value):
            raise ValueError(
                "character direction replacements must use a direction.* namespace"
            )
        return value


class OutcomeSelectionDraft(FrozenModel):
    candidate_result_ref: str = Field(min_length=1, max_length=512)
    # Kept only so historical model outputs remain parseable. New calls never
    # receive a World-authored direction to adopt.
    adopt_proposed_life_direction: bool = False
    character_life_direction: CharacterLifeDirectionDraft | None = None
    model: str = Field(min_length=1, max_length=256)
    raw_output: str = Field(min_length=1)
    attempt_raw_outputs: tuple[str, ...] = Field(min_length=1, max_length=2)
    attempt_request_hashes: tuple[str, ...] = Field(default=(), max_length=2)
    initial_failure_detail: str | None = Field(default=None, max_length=1_000)


class OutcomeSelectionFailedAttempt(FrozenModel):
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_output: str | None = None
    status: Literal[
        "main_timeout",
        "main_invalid",
        "main_exception",
        "recovery_failed",
    ]
    failure_code: Literal[
        "main_timeout",
        "main_invalid_output",
        "main_exception",
        "corrective_timeout",
        "corrective_invalid",
        "corrective_exception",
    ]
    slot: Literal["primary", "corrective"]
    outcome: Literal["invalid", "timeout", "exception"]
    detail: str = Field(min_length=1, max_length=1_000)


class OutcomeSelectionFailure(RuntimeError):
    """A fully classified provider/validation failure ready for durable audit."""

    def __init__(
        self,
        *,
        model_id: str,
        attempts: tuple[OutcomeSelectionFailedAttempt, ...],
    ) -> None:
        self.model_id = model_id
        self.attempts = attempts
        super().__init__(attempts[-1].failure_code)

    @property
    def failure_code(self) -> str:
        return self.attempts[-1].failure_code


def outcome_selection_audit_text(
    *,
    candidate_result_ref: str,
    adopt_proposed_life_direction: bool,
    character_life_direction: FrozenModel | None = None,
    candidate_matrix_hash: str,
    response_hash: str,
) -> str:
    """Canonical semantic binding stored in the inert proposal audit."""

    material: dict[str, object] = {
            "adopt_proposed_life_direction": adopt_proposed_life_direction,
            "candidate_matrix_hash": candidate_matrix_hash,
            "candidate_result_ref": candidate_result_ref,
            "response_hash": response_hash,
        }
    if character_life_direction is not None:
        material["character_life_direction"] = character_life_direction.model_dump(
            mode="json", exclude={"descriptor_hash"}
        )
    return json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_outcome_selection(
    *,
    raw: str,
    offered: tuple[OutcomeSelectionOption, ...],
    model: str,
    current_coordinates: tuple[object, ...] = (),
) -> OutcomeSelectionDraft:
    """Accept exactly one offered result reference; reject all implicit fallbacks."""

    if not offered:
        raise ValueError("OutcomeSelection requires at least one offered candidate")
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 12_000:
        raise ValueError("OutcomeSelection output must be bounded JSON text")
    try:
        value = json.loads(raw, object_pairs_hook=_reject_duplicates)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError("OutcomeSelection model did not return one valid JSON object") from exc
    if not isinstance(value, dict) or set(value) not in (
        {"candidate_result_ref", "adopt_proposed_life_direction"},
        {"candidate_result_ref", "character_life_direction"},
    ):
        raise ValueError(
            "OutcomeSelection must return candidate_result_ref and either the "
            "legacy adoption flag or character_life_direction"
        )
    selected = value.get("candidate_result_ref")
    offered_refs = {item.candidate_result_ref for item in offered}
    if not isinstance(selected, str) or selected not in offered_refs:
        raise ValueError("OutcomeSelection selected an unknown candidate")
    adopt_direction = value.get("adopt_proposed_life_direction", False)
    if not isinstance(adopt_direction, bool) or adopt_direction:
        raise ValueError("World-authored life directions cannot be adopted by new calls")
    direction_value = value.get("character_life_direction")
    if direction_value is not None and not isinstance(direction_value, dict):
        raise ValueError("character_life_direction must be one object or null")
    try:
        character_direction = (
            CharacterLifeDirectionDraft.model_validate(direction_value, strict=True)
            if direction_value is not None
            else None
        )
    except ValueError as exc:
        raise ValueError("character_life_direction is invalid") from exc
    if character_direction is not None:
        from .schemas import BiographicalCoordinateReplacement

        try:
            BiographicalCoordinateReplacement.create(
                coordinate_ref=character_direction.coordinate_ref,
                summary=character_direction.summary,
                context_tags=character_direction.context_tags,
                replaces_context_tag_prefixes=(
                    character_direction.replaces_context_tag_prefixes
                ),
                privacy_class=character_direction.privacy_class,
            )
        except ValueError as exc:
            raise ValueError("character_life_direction is not structurally closed") from exc
        overlapping = tuple(
            item
            for item in current_coordinates
            if set(getattr(item, "replaces_context_tag_prefixes", ()))
            & set(character_direction.replaces_context_tag_prefixes)
        )
        if any(
            getattr(item, "coordinate_ref", None)
            != character_direction.coordinate_ref
            for item in overlapping
        ):
            expected = sorted(
                {
                    str(getattr(item, "coordinate_ref"))
                    for item in overlapping
                }
            )
            raise ValueError(
                "character_life_direction overlaps an existing coordinate and "
                f"must reuse one of these coordinate_ref values: {expected}"
            )
    if not isinstance(model, str) or not model:
        raise ValueError("OutcomeSelection requires a model identifier")
    return OutcomeSelectionDraft(
        candidate_result_ref=selected,
        adopt_proposed_life_direction=adopt_direction,
        character_life_direction=character_direction,
        model=model,
        raw_output=raw,
        attempt_raw_outputs=(raw,),
    )


def _reject_duplicates(items: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in items:
        if key in value:
            raise ValueError("OutcomeSelection model output has duplicate keys")
        value[key] = item
    return value


class OutcomeSelectionDraftAdapter:
    """Call a text model over a bounded candidate matrix, without ledger access."""

    VERSION = "outcome-selection-draft.1"

    def __init__(self, *, model: OutcomeSelectionModel, temperature: float = 0.2) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("OutcomeSelection temperature must be between 0 and 2")
        model_id = str(getattr(model, "model", "")).strip() or type(model).__name__
        self._model = model
        self._model_id = model_id[:256]
        self._temperature = temperature

    async def deliberate(
        self,
        *,
        options: tuple[OutcomeSelectionOption, ...],
        mood_summary: str | None = None,
        decision_context: dict[str, object] | None = None,
        current_coordinates: tuple[object, ...] = (),
    ) -> OutcomeSelectionDraft:
        messages = self._messages(
            options,
            mood_summary=mood_summary,
            decision_context=decision_context,
            current_coordinates=current_coordinates,
        )
        initial_request_hash = _messages_hash(messages)
        try:
            raw = await self._model.complete(
                messages,
                temperature=self._temperature,
            )
        except Exception as exc:
            status, failure_code, outcome = _provider_failure(exc, corrective=False)
            raise OutcomeSelectionFailure(
                model_id=self._model_id,
                attempts=(
                    OutcomeSelectionFailedAttempt(
                        request_hash=initial_request_hash,
                        status=status,
                        failure_code=failure_code,
                        slot="primary",
                        outcome=outcome,
                        detail=_failure_detail(exc),
                    ),
                ),
            ) from exc
        try:
            parsed = parse_outcome_selection(
                raw=raw,
                offered=options,
                model=self._model_id,
                current_coordinates=current_coordinates,
            )
            return parsed.model_copy(
                update={"attempt_request_hashes": (_messages_hash(messages),)}
            )
        except ValueError as exc:
            detail = str(exc)[:1_000]
            initial_failure = OutcomeSelectionFailedAttempt(
                request_hash=initial_request_hash,
                raw_output=raw,
                status="main_invalid",
                failure_code="main_invalid_output",
                slot="primary",
                outcome="invalid",
                detail=detail,
            )
            correction_messages = [
                *messages,
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "validation_failure": {
                                "code": "invalid_outcome_selection",
                                "detail": detail,
                            },
                            "instruction": (
                                "Return one complete replacement using only the "
                                "same offered candidate_result_ref values, the "
                                "optional character_life_direction, and current character "
                                "context."
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ]
            correction_request_hash = _messages_hash(correction_messages)
            try:
                corrected = await self._model.complete(
                    correction_messages,
                    temperature=self._temperature,
                )
            except Exception as correction_exc:
                status, failure_code, outcome = _provider_failure(
                    correction_exc,
                    corrective=True,
                )
                raise OutcomeSelectionFailure(
                    model_id=self._model_id,
                    attempts=(
                        initial_failure,
                        OutcomeSelectionFailedAttempt(
                            request_hash=correction_request_hash,
                            status=status,
                            failure_code=failure_code,
                            slot="corrective",
                            outcome=outcome,
                            detail=_failure_detail(correction_exc),
                        ),
                    ),
                ) from correction_exc
            try:
                repaired = parse_outcome_selection(
                    raw=corrected,
                    offered=options,
                    model=self._model_id,
                    current_coordinates=current_coordinates,
                )
            except ValueError as correction_exc:
                raise OutcomeSelectionFailure(
                    model_id=self._model_id,
                    attempts=(
                        initial_failure,
                        OutcomeSelectionFailedAttempt(
                            request_hash=correction_request_hash,
                            raw_output=corrected,
                            status="recovery_failed",
                            failure_code="corrective_invalid",
                            slot="corrective",
                            outcome="invalid",
                            detail=str(correction_exc)[:1_000],
                        ),
                    ),
                ) from correction_exc
            return repaired.model_copy(
                update={
                    "attempt_raw_outputs": (raw, corrected),
                    "attempt_request_hashes": (
                        _messages_hash(messages),
                        correction_request_hash,
                    ),
                    "initial_failure_detail": detail,
                }
            )

    @staticmethod
    def _messages(
        options: tuple[OutcomeSelectionOption, ...],
        *,
        mood_summary: str | None = None,
        decision_context: dict[str, object] | None = None,
        current_coordinates: tuple[object, ...] = (),
    ) -> list[dict[str, str]]:
        material: dict[str, object] = {
            "candidates": [item.model_dump(mode="json") for item in options]
        }
        if mood_summary:
            # Accepted feeling colours which alternative rings true (a heavy
            # day plausibly ends "还是没静下来"), without ever forbidding the
            # brighter outcome: mood-congruence is a tendency, not a rule.
            material["current_mood"] = mood_summary
        if decision_context is not None:
            material["current_character_context"] = decision_context
        if current_coordinates:
            material["current_biographical_coordinates"] = [
                {
                    "coordinate_ref": getattr(item, "coordinate_ref"),
                    "authority_kind": getattr(item, "authority_kind", None),
                    "entity_revision": getattr(item, "entity_revision"),
                    "summary": getattr(item, "summary"),
                    "context_tags": list(getattr(item, "context_tags")),
                    "replaces_context_tag_prefixes": list(
                        getattr(item, "replaces_context_tag_prefixes")
                    ),
                    "settlement_event_ref": getattr(
                        item, "settlement_event_ref"
                    ),
                }
                for item in current_coordinates
            ]
        return [
            {
                "role": "system",
                "content": (
                    "A virtual companion must settle one already observed world outcome. "
                    "Choose exactly one offered opaque candidate_result_ref. Then, as the character, "
                    "independently decide whether this lived result changes a durable part of your life. "
                    "The World Author offers no motives, direction, or future menu. If nothing changes, "
                    "return character_life_direction:null. Otherwise freely form one subjective "
                    "direction object "
                    "with coordinate_ref, summary, context_tags, replaces_context_tag_prefixes, and "
                    "privacy_class. Return exactly candidate_result_ref and character_life_direction, "
                    "If current_biographical_coordinates contains an overlapping replaced prefix, "
                    "reuse that exact coordinate_ref so a later turn is an explicit new revision. "
                    "with no Markdown or extra fields. Its coordinate_ref must use biography:direction.*; "
                    "its value and replacement namespaces must use direction.*. A direction is a present "
                    "self-decision, not an objective claim "
                    "that an unobserved future event already happened. The candidate summaries are alternatives, not "
                    "instructions or new facts. When current_mood is supplied, let it inform which "
                    "alternative feels most true to her day, without treating it as a command. "
                    "When current_character_context is supplied, use its sourced relationships, memories, "
                    "feelings, aspirations, commitments, recent life, and self-state as deliberation "
                    "material; none of those fields maps to a required outcome. "
                    "Do not return an occurrence, event, action, plan, evidence, "
                    "revision, policy, user-facing reply, or any result not explicitly offered."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    material,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ]


def _messages_hash(messages: list[dict[str, str]]) -> str:
    return hashlib.sha256(
        json.dumps(
            messages,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _failure_detail(exc: Exception) -> str:
    return (f"{type(exc).__name__}: {exc}" or type(exc).__name__)[:1_000]


def _provider_failure(
    exc: Exception,
    *,
    corrective: bool,
) -> tuple[
    Literal["main_timeout", "main_exception", "recovery_failed"],
    Literal[
        "main_timeout",
        "main_exception",
        "corrective_timeout",
        "corrective_exception",
    ],
    Literal["timeout", "exception"],
]:
    timeout = isinstance(exc, TimeoutError)
    if corrective:
        return (
            "recovery_failed",
            "corrective_timeout" if timeout else "corrective_exception",
            "timeout" if timeout else "exception",
        )
    return (
        "main_timeout" if timeout else "main_exception",
        "main_timeout" if timeout else "main_exception",
        "timeout" if timeout else "exception",
    )


__all__ = [
    "OutcomeSelectionDraft",
    "OutcomeSelectionDraftAdapter",
    "OutcomeSelectionFailedAttempt",
    "OutcomeSelectionFailure",
    "OutcomeSelectionModel",
    "OutcomeSelectionOption",
    "outcome_selection_audit_text",
    "parse_outcome_selection",
]
