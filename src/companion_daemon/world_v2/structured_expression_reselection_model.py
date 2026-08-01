"""Strict transport shape for one role-authored expression reselection.

The role still owns the semantic choice: it may speak now, defer, or remain
silent, and it authors every visible beat and private turn state.  This module
only binds that fresh choice to the expression wire the current deployment can
actually materialize.  It deliberately contains no motive, tone, question, or
social-behaviour policy.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
from typing import Final

from .expression_draft import (
    EXPRESSION_DELAY_MAX_SECONDS,
    RESPONSE_EXPECTATION_WAIT_MAX_SECONDS,
    ExpressionDraftCapabilities,
)
from .structured_source_review_model import StructuredSourceReviewModel


EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT: Final[str] = "expression-source-reselection-direct.1"

_CADENCE_PROFILES: Final[tuple[str, ...]] = (
    "rapid",
    "conversational",
    "hesitant",
    "escalating",
)
_EPISODE_DISPOSITIONS: Final[tuple[str, ...]] = (
    "complete_without_more",
    "append",
    "cancel_pending",
    "supersede_pending",
)
_EXPRESSION_MODALITIES: Final[tuple[str, ...]] = (
    "text",
    "reaction",
    "sticker",
    "typing",
)
_AUTHORABLE_WORLD_CLAIM_SCOPES: Final[tuple[str, ...]] = (
    "current_world",
    "past_world",
    "counterpart_history",
    "shared_history",
    "stable_identity",
)
_CONTRACT_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "contract",
        "profile_id",
        "modalities",
        "reaction_ids",
        "sticker_ids",
        "max_beats",
        "max_later_beats",
        "private_turn_state_required",
        "cadence_required",
        "allowed_source_ref_aliases",
        "world_claim_source_ref_aliases_by_scope",
        "response_expectation_assessment_required",
        "provider_message_bound",
        "schema_sha256",
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _schema_digest(schema: dict[str, object]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(schema).encode('utf-8')).hexdigest()}"


def _bounded_unique_strings(
    values: Iterable[str],
    *,
    maximum: int,
    label: str,
) -> list[str]:
    result = list(values)
    if (
        len(result) > maximum
        or any(not isinstance(value, str) or not value or len(value) > 512 for value in result)
        or len(result) != len(set(result))
    ):
        raise ValueError(f"invalid {label}")
    return result


def expression_reselection_output_contract(
    *,
    capabilities: ExpressionDraftCapabilities,
    allowed_source_ref_aliases: Iterable[str],
    world_claim_source_ref_aliases_by_scope: dict[str, Iterable[str]],
    response_expectation_assessment_required: bool,
    combined: bool,
    provider_message_bound: bool = True,
) -> dict[str, object]:
    """Bind one direct reselection to executable transport facts.

    A combined cognition response has additional host-owned fields and needs a
    distinct root contract.  Reusing the direct contract for that wider wire
    would make a provider schema appear stricter than it actually is, so this
    builder fails closed instead.
    """

    if combined:
        raise ValueError("combined cognition requires its own reselection contract")
    aliases = sorted(
        _bounded_unique_strings(
            set(allowed_source_ref_aliases),
            maximum=256,
            label="source ref aliases",
        )
    )
    if set(world_claim_source_ref_aliases_by_scope) != set(_AUTHORABLE_WORLD_CLAIM_SCOPES):
        raise ValueError("invalid world claim source scope map")
    claim_aliases_by_scope = {
        scope: sorted(
            _bounded_unique_strings(
                set(world_claim_source_ref_aliases_by_scope[scope]),
                maximum=256,
                label=f"{scope} source ref aliases",
            )
        )
        for scope in _AUTHORABLE_WORLD_CLAIM_SCOPES
    }
    if any(
        not set(scope_aliases).issubset(aliases)
        for scope_aliases in claim_aliases_by_scope.values()
    ):
        raise ValueError("world claim scope aliases exceed the visible source aliases")
    contract: dict[str, object] = {
        "contract": EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT,
        "profile_id": capabilities.profile_id,
        "modalities": list(capabilities.modalities),
        "reaction_ids": [item.option_id for item in capabilities.reaction_options],
        "sticker_ids": [item.option_id for item in capabilities.sticker_options],
        "max_beats": capabilities.max_beats,
        "max_later_beats": capabilities.max_later_beats,
        "private_turn_state_required": capabilities.private_turn_state_mode == "required",
        # A provider strict object must make every field explicit.  This never
        # selects the cadence value; it only prevents a local default from
        # silently making that character-owned choice.
        "cadence_required": True,
        "allowed_source_ref_aliases": aliases,
        "world_claim_source_ref_aliases_by_scope": claim_aliases_by_scope,
        "response_expectation_assessment_required": (response_expectation_assessment_required),
        "provider_message_bound": provider_message_bound,
    }
    contract["schema_sha256"] = _schema_digest(_expression_schema(contract))
    return contract


def _nullable(schema: dict[str, object]) -> dict[str, object]:
    return {"anyOf": [schema, {"type": "null"}]}


def _closed_object(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _ref_or_null(name: str) -> dict[str, object]:
    return _nullable({"$ref": f"#/$defs/{name}"})


def _source_ref_array(
    aliases: list[str],
    *,
    maximum: int,
    minimum: int = 0,
) -> dict[str, object]:
    item_schema: dict[str, object]
    if aliases:
        item_schema = {"type": "string", "enum": aliases}
    else:
        # Provider strict schemas reject an empty enum.  With maxItems=0 this
        # branch remains closed without inventing a source token.
        item_schema = {"type": "string", "minLength": 1, "maxLength": 512}
        maximum = 0
        minimum = 0
    result: dict[str, object] = {
        "type": "array",
        "items": item_schema,
        "minItems": minimum,
        "maxItems": maximum,
    }
    return result


def _validate_realtime_source_ref_uniqueness(expression: dict[str, object]) -> None:
    """Enforce ref-set semantics omitted from the provider's schema dialect.

    OpenAI strict structured output rejects JSON Schema's ``uniqueItems``.
    The transport schema therefore constrains membership and cardinality while
    this local hard boundary retains uniqueness before any corrected draft can
    reach source review or materialization.
    """

    private_state = expression.get("private_turn_state")
    if isinstance(private_state, dict):
        attended_refs = private_state.get("attended_source_refs")
        if (
            isinstance(attended_refs, list)
            and all(isinstance(item, str) for item in attended_refs)
            and len(attended_refs) != len(set(attended_refs))
        ):
            raise ValueError("duplicate source refs in private turn state")
    claims = expression.get("world_claims")
    if not isinstance(claims, list):
        return
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        source_refs = claim.get("source_refs")
        if (
            isinstance(source_refs, list)
            and all(isinstance(item, str) for item in source_refs)
            and len(source_refs) != len(set(source_refs))
        ):
            raise ValueError("duplicate source refs in world claim")


def _beat_schema(
    modality: str,
    *,
    reaction_ids: list[str],
    sticker_ids: list[str],
) -> dict[str, object]:
    text: dict[str, object] = {"type": "null"}
    reaction_id: dict[str, object] = {"type": "null"}
    sticker_id: dict[str, object] = {"type": "null"}
    if modality == "text":
        text = {"type": "string", "minLength": 1, "maxLength": 4_096}
    elif modality == "reaction":
        reaction_id = {
            "type": "string",
            "enum": reaction_ids,
        }
    elif modality == "sticker":
        sticker_id = {
            "type": "string",
            "enum": sticker_ids,
        }
    return _closed_object(
        {
            "modality": {"type": "string", "enum": [modality]},
            "text": text,
            "reaction_id": reaction_id,
            "sticker_id": sticker_id,
        }
    )


def _expression_definitions(contract: dict[str, object]) -> dict[str, object]:
    aliases = contract["allowed_source_ref_aliases"]
    claim_aliases_by_scope = contract["world_claim_source_ref_aliases_by_scope"]
    reaction_ids = contract["reaction_ids"]
    sticker_ids = contract["sticker_ids"]
    modalities = contract["modalities"]
    if not all(
        isinstance(value, list) for value in (aliases, reaction_ids, sticker_ids, modalities)
    ) or not isinstance(claim_aliases_by_scope, dict):
        raise ValueError("invalid expression reselection contract lists")
    alias_values = list(aliases)
    reaction_values = list(reaction_ids)
    sticker_values = list(sticker_ids)
    modality_values = list(modalities)
    if contract["provider_message_bound"] is False:
        modality_values = [value for value in modality_values if value != "reaction"]

    visible_modalities = [value for value in modality_values if value != "typing"]
    visible_beat_branches = [
        _beat_schema(
            modality,
            reaction_ids=reaction_values,
            sticker_ids=sticker_values,
        )
        for modality in visible_modalities
    ]
    beat_branches = [
        _beat_schema(
            modality,
            reaction_ids=reaction_values,
            sticker_ids=sticker_values,
        )
        for modality in modality_values
    ]
    text_beat = _beat_schema(
        "text",
        reaction_ids=reaction_values,
        sticker_ids=sticker_values,
    )
    claim_branches = [
        _closed_object(
            {
                "claim_text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                },
                "scope": {"type": "string", "enum": [scope]},
                "source_refs": _source_ref_array(
                    list(scope_aliases),
                    minimum=1,
                    maximum=min(8, len(scope_aliases)),
                ),
            }
        )
        for scope in _AUTHORABLE_WORLD_CLAIM_SCOPES
        for scope_aliases in (claim_aliases_by_scope.get(scope),)
        if isinstance(scope_aliases, list) and scope_aliases
    ]
    return {
        "PrivateTurnState": _closed_object(
            {
                "contract": {
                    "type": "string",
                    "enum": ["private-turn-state.1"],
                },
                "inner_state_summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 480,
                    "pattern": "\\S",
                },
                "attended_source_refs": _source_ref_array(
                    alias_values,
                    maximum=8,
                ),
            }
        ),
        "ExpressionBeatDraftChoice": {"anyOf": beat_branches},
        "VisibleExpressionBeatDraftChoice": {"anyOf": visible_beat_branches},
        "TextExpressionBeatDraftChoice": text_beat,
        "ResponseExpectationDraft": _closed_object(
            {
                "hoped_response": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "pressure_bp": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "importance_bp": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "wait_position_bp": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "expires_after_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 172_800,
                },
            }
        ),
        "ResponseExpectationAssessmentDraft": _closed_object(
            {
                "status": {
                    "type": "string",
                    "enum": [
                        "fulfilled",
                        "superseded",
                        "still_pending",
                        "uncertain",
                    ],
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 240,
                },
            }
        ),
        "VariationProfile": _closed_object(
            {
                "deviation_kind": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "deviation_intensity": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10_000,
                },
                "change_phase": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "sampling_mode": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
                "recovery_posture": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                },
            }
        ),
        "WorldClaimDraft": ({"anyOf": claim_branches} if claim_branches else _closed_object({})),
    }


def _expression_properties(
    *,
    timing_choice: str,
    contract: dict[str, object],
) -> dict[str, object]:
    private_state: dict[str, object] = {"$ref": "#/$defs/PrivateTurnState"}
    if contract["private_turn_state_required"] is False:
        private_state = _nullable(private_state)
    assessment: dict[str, object] = {"$ref": "#/$defs/ResponseExpectationAssessmentDraft"}
    if contract["response_expectation_assessment_required"] is False:
        assessment = _nullable(assessment)

    max_beats = contract["max_beats"]
    max_later_beats = contract["max_later_beats"]
    if not isinstance(max_beats, int) or not isinstance(max_later_beats, int):
        raise ValueError("invalid expression beat bounds")
    if timing_choice == "now":
        beats: dict[str, object] = {
            "type": "array",
            "items": {"$ref": "#/$defs/ExpressionBeatDraftChoice"},
            "minItems": 1,
            "maxItems": max_beats,
        }
        delay_position: dict[str, object] = {"type": "null"}
        expires: dict[str, object] = {"type": "null"}
        expectation = _ref_or_null("ResponseExpectationDraft")
    elif timing_choice == "later":
        beats = {
            "type": "array",
            "items": {"$ref": "#/$defs/TextExpressionBeatDraftChoice"},
            "minItems": 1,
            "maxItems": max_later_beats,
        }
        delay_position = {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        }
        expires = {
            "type": "integer",
            "minimum": 2,
            "maximum": 172_800,
        }
        expectation = _ref_or_null("ResponseExpectationDraft")
    else:
        beats = {
            "type": "array",
            # ``maxItems=0`` makes the item shape unreachable at runtime, but
            # strict providers still resolve every local ref while admitting
            # the schema.  Reuse an installed definition rather than leaving
            # a phantom empty-beat type in the contract.
            "items": {"$ref": "#/$defs/VisibleExpressionBeatDraftChoice"},
            "maxItems": 0,
        }
        delay_position = {"type": "null"}
        expires = {"type": "null"}
        expectation = {"type": "null"}

    # Keep a stable schema serialization for provider caches and audits. JSON
    # object member order is not a causal or validity boundary.
    return {
        "private_turn_state": private_state,
        "timing_choice": {
            "type": "string",
            "enum": [timing_choice],
        },
        "cadence": {
            "type": "string",
            "enum": list(_CADENCE_PROFILES),
        },
        "beats": beats,
        "delay_position_bp": delay_position,
        "expires_after_seconds": expires,
        "stance": {
            "type": "string",
            "minLength": 1,
            "maxLength": 128,
        },
        "brief_rationale": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
        },
        "impulse_summary": _nullable(
            {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
            }
        ),
        "confidence": {
            "type": "integer",
            "minimum": 0,
            "maximum": 10_000,
        },
        "variation_profile": _ref_or_null("VariationProfile"),
        "response_expectation": expectation,
        "response_expectation_assessment": assessment,
        "world_claims": {
            "type": "array",
            "items": {"$ref": "#/$defs/WorldClaimDraft"},
            "maxItems": (
                8
                if any(
                    contract["world_claim_source_ref_aliases_by_scope"].get(scope)
                    for scope in _AUTHORABLE_WORLD_CLAIM_SCOPES
                )
                else 0
            ),
        },
    }


def _expression_schema(contract: dict[str, object]) -> dict[str, object]:
    max_beats = contract["max_beats"]
    if not isinstance(max_beats, int) or isinstance(max_beats, bool):
        raise ValueError("invalid expression beat bounds")
    return {
        "type": "object",
        "$defs": _expression_definitions(contract),
        "properties": {
            "expression_draft": {
                "anyOf": [
                    _closed_object(
                        _expression_properties(
                            timing_choice="now",
                            contract=contract,
                        )
                    ),
                    _closed_object(
                        _expression_properties(
                            timing_choice="later",
                            contract=contract,
                        )
                    ),
                    _closed_object(
                        _expression_properties(
                            timing_choice="silent",
                            contract=contract,
                        )
                    ),
                ]
            },
            "episode_disposition": _nullable(
                {
                    "type": "string",
                    "enum": list(_EPISODE_DISPOSITIONS),
                }
            ),
        },
        "required": ["expression_draft", "episode_disposition"],
        "additionalProperties": False,
    }


def _validated_contract_from_messages(
    messages: list[dict[str, str]],
) -> dict[str, object] | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            envelope = json.loads(content)
        except (TypeError, ValueError):
            continue
        if not isinstance(envelope, dict):
            continue
        contract = envelope.get("output_contract")
        if not isinstance(contract, dict):
            continue
        if contract.get("contract") != EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT:
            continue
        _validate_contract(contract)
        return contract
    return None


def _validate_contract(contract: dict[str, object]) -> None:
    if set(contract) != _CONTRACT_FIELDS:
        raise ValueError("invalid expression reselection contract fields")
    profile_id = contract["profile_id"]
    if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 128:
        raise ValueError("invalid expression capability profile")
    modalities = contract["modalities"]
    reaction_ids = contract["reaction_ids"]
    sticker_ids = contract["sticker_ids"]
    aliases = contract["allowed_source_ref_aliases"]
    claim_aliases_by_scope = contract["world_claim_source_ref_aliases_by_scope"]
    if not isinstance(modalities, list):
        raise ValueError("invalid expression modalities")
    modality_values = _bounded_unique_strings(
        modalities,
        maximum=4,
        label="expression modalities",
    )
    if "text" not in modality_values or any(
        value not in _EXPRESSION_MODALITIES for value in modality_values
    ):
        raise ValueError("invalid expression modalities")
    if not isinstance(reaction_ids, list) or not isinstance(sticker_ids, list):
        raise ValueError("invalid expression option lists")
    reaction_values = _bounded_unique_strings(
        reaction_ids,
        maximum=256,
        label="reaction ids",
    )
    sticker_values = _bounded_unique_strings(
        sticker_ids,
        maximum=256,
        label="sticker ids",
    )
    if bool(reaction_values) != ("reaction" in modality_values):
        raise ValueError("reaction modality and options do not match")
    if bool(sticker_values) != ("sticker" in modality_values):
        raise ValueError("sticker modality and options do not match")
    if not isinstance(aliases, list):
        raise ValueError("invalid source ref aliases")
    alias_values = _bounded_unique_strings(
        aliases,
        maximum=256,
        label="source ref aliases",
    )
    if alias_values != sorted(alias_values):
        raise ValueError("source ref aliases must be canonical")
    if not isinstance(claim_aliases_by_scope, dict) or set(claim_aliases_by_scope) != set(
        _AUTHORABLE_WORLD_CLAIM_SCOPES
    ):
        raise ValueError("invalid world claim source scope map")
    for scope in _AUTHORABLE_WORLD_CLAIM_SCOPES:
        scope_aliases = claim_aliases_by_scope[scope]
        if not isinstance(scope_aliases, list):
            raise ValueError("invalid world claim source scope aliases")
        normalized_scope_aliases = _bounded_unique_strings(
            scope_aliases,
            maximum=256,
            label=f"{scope} source ref aliases",
        )
        if normalized_scope_aliases != sorted(normalized_scope_aliases) or not set(
            normalized_scope_aliases
        ).issubset(alias_values):
            raise ValueError("invalid world claim source scope aliases")
    max_beats = contract["max_beats"]
    max_later_beats = contract["max_later_beats"]
    if (
        isinstance(max_beats, bool)
        or not isinstance(max_beats, int)
        or not 1 <= max_beats <= 16
        or isinstance(max_later_beats, bool)
        or not isinstance(max_later_beats, int)
        or not 1 <= max_later_beats <= max_beats
    ):
        raise ValueError("invalid expression beat limits")
    for field in (
        "private_turn_state_required",
        "cadence_required",
        "response_expectation_assessment_required",
        "provider_message_bound",
    ):
        if not isinstance(contract[field], bool):
            raise ValueError(f"invalid {field}")
    if contract["cadence_required"] is not True:
        raise ValueError("strict reselection requires an explicit cadence")
    expected_digest = _schema_digest(_expression_schema(contract))
    if contract["schema_sha256"] != expected_digest:
        raise ValueError("expression reselection schema digest mismatch")


def _position_within_closed_interval(
    *,
    minimum: int,
    maximum: int,
    position_bp: int,
) -> int:
    if (
        isinstance(position_bp, bool)
        or not isinstance(position_bp, int)
        or not 0 <= position_bp <= 10_000
        or maximum < minimum
    ):
        raise ValueError("invalid authored timing position")
    return minimum + ((maximum - minimum) * position_bp // 10_000)


def _normalize_expression_reselection_output(
    raw: str,
    *,
    allow_historical_canonical_wire: bool,
) -> str:
    """Turn one validated author wire into canonical ExpressionDraft JSON."""

    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid expression reselection envelope")
    wrapped = set(value) == {"expression_draft", "episode_disposition"}
    expression = value.get("expression_draft") if wrapped else value
    if not isinstance(expression, dict):
        raise ValueError("invalid expression reselection draft")
    if not allow_historical_canonical_wire:
        _validate_realtime_source_ref_uniqueness(expression)
    transport_fields = {"delay_position_bp"}
    legacy_typing_prefix = "typing_prefix_count" in expression
    if (
        allow_historical_canonical_wire
        and not legacy_typing_prefix
        and not (transport_fields & set(expression))
    ):
        return raw
    if not wrapped:
        raise ValueError("strict expression reselection requires its envelope")
    if not transport_fields.issubset(expression):
        raise ValueError("strict expression reselection requires its transport fields")
    normalized = dict(expression)
    timing_choice = normalized.get("timing_choice")
    beats = normalized.get("beats")
    if not isinstance(beats, list):
        raise ValueError("invalid expression beat list")
    if legacy_typing_prefix:
        if not allow_historical_canonical_wire:
            raise ValueError("realtime strict reselection cannot use a typing prefix")
        typing_prefix_count = normalized.pop("typing_prefix_count", None)
        if (
            isinstance(typing_prefix_count, bool)
            or not isinstance(typing_prefix_count, int)
            or typing_prefix_count < 0
        ):
            raise ValueError("invalid typing prefix count")
        if timing_choice != "now" and typing_prefix_count != 0:
            raise ValueError("typing prefix is available only for immediate expression")
        normalized["beats"] = [
            *(
                {
                    "modality": "typing",
                    "text": None,
                    "reaction_id": None,
                    "sticker_id": None,
                }
                for _ in range(typing_prefix_count)
            ),
            *beats,
        ]
    else:
        # New strict wire carries the role-authored beats directly. Do not
        # partition or reconstruct them: array order is execution order.
        normalized["beats"] = beats

    delay_position_bp = normalized.pop("delay_position_bp", None)
    expires_after_seconds = normalized.get("expires_after_seconds")
    if timing_choice == "later":
        if (
            isinstance(expires_after_seconds, bool)
            or not isinstance(expires_after_seconds, int)
            or not 2 <= expires_after_seconds <= 172_800
        ):
            raise ValueError("invalid later expiry")
        normalized["delay_seconds"] = _position_within_closed_interval(
            minimum=1,
            maximum=min(
                EXPRESSION_DELAY_MAX_SECONDS,
                expires_after_seconds - 1,
            ),
            position_bp=delay_position_bp,
        )
    else:
        if delay_position_bp is not None:
            raise ValueError("non-later expression cannot select a delay position")
        normalized["delay_seconds"] = None

    expectation = normalized.get("response_expectation")
    if isinstance(expectation, dict):
        normalized_expectation = dict(expectation)
        wait_position_bp = normalized_expectation.pop("wait_position_bp", None)
        expectation_expiry = normalized_expectation.get("expires_after_seconds")
        if (
            isinstance(expectation_expiry, bool)
            or not isinstance(expectation_expiry, int)
            or not 60 <= expectation_expiry <= 172_800
        ):
            raise ValueError("invalid response expectation expiry")
        normalized_expectation["wait_seconds"] = _position_within_closed_interval(
            minimum=30,
            maximum=min(
                RESPONSE_EXPECTATION_WAIT_MAX_SECONDS,
                expectation_expiry - 1,
            ),
            position_bp=wait_position_bp,
        )
        normalized["response_expectation"] = normalized_expectation
    elif expectation is not None:
        raise ValueError("invalid response expectation")

    value["expression_draft"] = normalized
    return _canonical_json(value)


def normalize_expression_reselection_output(raw: str) -> str:
    """Normalize a strict wire while preserving historical canonical replay bytes.

    Historical committed candidates can predate the strict provider transport
    envelope.  This compatibility entry point keeps those bytes unchanged; it
    must not be used to admit a new provider response.
    """

    return _normalize_expression_reselection_output(
        raw,
        allow_historical_canonical_wire=True,
    )


def normalize_realtime_expression_reselection_output(raw: str) -> str:
    """Normalize a newly authored correction and require the negotiated strict wire.

    The role selects every visible or typing beat in its exact execution order
    and the relative position inside each valid time interval. Local code
    converts only bounded timing positions; it never moves a typing beat or
    reconstructs a prefix.
    """

    return _normalize_expression_reselection_output(
        raw,
        allow_historical_canonical_wire=False,
    )


class StructuredExpressionReselectionModel(StructuredSourceReviewModel):
    """Source reviewer plus one strict, capability-bound expression wire."""

    def supports_strict_output_contract(self, contract: str) -> bool:
        return (
            contract == EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT
            or super().supports_strict_output_contract(contract)
        )

    def request_payload(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        json_object: bool = False,
    ) -> dict[str, object]:
        payload = super().request_payload(
            messages,
            temperature=temperature,
            json_object=json_object,
        )
        if not json_object:
            return payload
        contract = _validated_contract_from_messages(messages)
        if contract is None:
            return payload
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "expression_source_reselection_direct_v1",
                "strict": True,
                "schema": _expression_schema(contract),
            },
        }
        return payload


__all__ = [
    "EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT",
    "StructuredExpressionReselectionModel",
    "expression_reselection_output_contract",
    "normalize_expression_reselection_output",
    "normalize_realtime_expression_reselection_output",
]
