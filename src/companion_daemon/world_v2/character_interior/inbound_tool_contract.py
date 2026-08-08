"""Canonical forced-tool transport for the inbound character call.

This module owns provider-visible structure only.  It neither makes a role
choice nor materializes proposals: callers receive the pre-existing wire after
the transport-only ``result_kind`` envelope has been removed.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Literal

from ..expression_draft import (
    ExpressionDraft,
    ExpressionDraftCapabilities,
    required_authored_expression_fields,
)
from ..private_turn_state import PrivateTurnState
from ..recall_audit import CharacterRecallRequest
from .inbound_appraisal_wire import AppraisalDraftWire


InboundToolPhase = Literal["initial", "after_recall", "final"]
InboundToolTransport = Literal["atomic", "stream"]
_CONTRACT_VERSION = "1"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inline_refs(schema: object, definitions: dict[str, object]) -> object:
    """Remove local Pydantic refs without copying field inventories by hand."""

    if isinstance(schema, list):
        return [_inline_refs(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        target = definitions.get(ref.removeprefix("#/$defs/"))
        if target is None:
            raise ValueError(f"unresolved canonical schema ref: {ref}")
        return _inline_refs(target, definitions)
    return {
        key: _inline_refs(value, definitions)
        for key, value in schema.items()
        if key not in {"$defs", "title", "default"}
    }


def _provider_schema(model_type: object) -> dict[str, object]:
    model_json_schema = getattr(model_type, "model_json_schema")
    generated = model_json_schema(mode="validation")
    if not isinstance(generated, dict):
        raise TypeError("canonical wire schema must be an object")
    definitions = generated.get("$defs")
    if not isinstance(definitions, dict):
        definitions = {}
    converted = _inline_refs(generated, definitions)
    if not isinstance(converted, dict):
        raise TypeError("provider wire schema must be an object")
    return converted


def _capability_expression_schema(
    capabilities: ExpressionDraftCapabilities,
    *,
    require_turn_posture: bool,
) -> dict[str, object]:
    """Specialize the authoritative ExpressionDraft schema to deployment facts."""

    schema = _provider_schema(ExpressionDraft)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("ExpressionDraft canonical schema has no properties")
    beats = properties.get("beats")
    if not isinstance(beats, dict) or not isinstance(beats.get("items"), dict):
        raise ValueError("ExpressionDraft canonical schema has no beats items")
    beats["maxItems"] = capabilities.max_beats
    beat_properties = beats["items"].get("properties")
    if not isinstance(beat_properties, dict):
        raise ValueError("ExpressionDraft beat schema has no properties")
    modality = beat_properties.get("modality")
    if not isinstance(modality, dict):
        raise ValueError("ExpressionDraft beat schema has no modality")
    modality["enum"] = list(capabilities.modalities)
    def constrain_option_ids(field: object, option_ids: list[str]) -> None:
        if not isinstance(field, dict) or not option_ids:
            return
        variants = field.get("anyOf")
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict) and variant.get("type") == "string":
                    variant["enum"] = option_ids
                    return
        if field.get("type") == "string":
            field["enum"] = option_ids

    constrain_option_ids(
        beat_properties.get("reaction_id"),
        [item.option_id for item in capabilities.reaction_options],
    )
    constrain_option_ids(
        beat_properties.get("sticker_id"),
        [item.option_id for item in capabilities.sticker_options],
    )
    later_beats = deepcopy(beats)
    later_beats["maxItems"] = capabilities.max_later_beats
    later_properties = later_beats["items"].get("properties")
    if not isinstance(later_properties, dict) or not isinstance(
        later_properties.get("modality"), dict
    ):
        raise ValueError("ExpressionDraft later beat schema has no modality")
    later_properties["modality"]["enum"] = ["text"]
    required = required_authored_expression_fields(
        capabilities=capabilities,
        require_turn_posture=require_turn_posture,
    )
    if capabilities.private_turn_state_mode == "required":
        required = required | {"private_turn_state"}
    schema["required"] = sorted(required)
    # Provider JSON-schema support has a dependable ``anyOf`` subset.  Keep
    # the timing union transport-only while making the deployment's deferred
    # beat budget visible before a provider can emit an unexecutable plan.
    schema["anyOf"] = [
        {"properties": {"timing_choice": {"enum": ["now", "silent"]}}},
        {
            "properties": {
                "timing_choice": {"enum": ["later"]},
                "beats": later_beats,
            }
        },
    ]
    return schema


@dataclass(frozen=True)
class InboundToolContractIdentity:
    contract_id: str
    phase: InboundToolPhase
    transport: InboundToolTransport
    tool_name: str
    version: str
    schema_sha256: str
    capabilities_sha256: str
    contract_sha256: str

    def request_identity_material(self) -> dict[str, str]:
        """Local-only contract coordinates bound into the provider audit hash."""

        return {
            "contract_id": self.contract_id,
            "phase": self.phase,
            "transport": self.transport,
            "tool_name": self.tool_name,
            "version": self.version,
            "schema_sha256": self.schema_sha256,
            "capabilities_sha256": self.capabilities_sha256,
            "contract_sha256": self.contract_sha256,
        }


@dataclass(frozen=True)
class InboundToolContract:
    """One provider-standard function and lossless decoder for one phase."""

    phase: InboundToolPhase
    transport: InboundToolTransport
    capabilities: ExpressionDraftCapabilities
    recall_allowed: bool
    require_turn_posture: bool
    provider_tools: tuple[dict[str, object], ...]
    provider_tool_choice: dict[str, object]
    identity: InboundToolContractIdentity

    def unwrap(self, raw_arguments: str) -> str:
        """Validate and remove only the exact forced-tool transport wrapper."""

        try:
            value = json.loads(raw_arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("forced transport must be one JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("forced transport must be one JSON object")
        kind = value.get("result_kind")
        if kind == "decision":
            expected = (
                {"result_kind", "appraisal_draft", "expression_draft"}
                if self.transport == "atomic"
                else {"result_kind", "protocol", "appraisal_draft", "events"}
            )
            if set(value) != expected:
                raise ValueError("forced decision transport envelope is ambiguous")
        elif kind == "recall":
            if not self.recall_allowed:
                raise ValueError("forced recall transport is unavailable")
            allowed = {
                frozenset({"result_kind", "recall_request"}),
                frozenset({"result_kind", "private_turn_state", "recall_request"}),
            }
            if self.capabilities.private_turn_state_mode == "required":
                allowed = {
                    frozenset({"result_kind", "private_turn_state", "recall_request"})
                }
            if frozenset(value) not in allowed:
                raise ValueError("forced recall transport envelope is ambiguous")
        else:
            raise ValueError("forced transport result_kind is missing or invalid")
        return json.dumps(
            {key: item for key, item in value.items() if key != "result_kind"},
            ensure_ascii=False,
            separators=(",", ":"),
        )


class InboundToolContracts:
    """Deep module: all inbound forced-tool schema/version knowledge in one seam."""

    def contract_for(
        self,
        *,
        phase: InboundToolPhase,
        transport: InboundToolTransport = "atomic",
        capabilities: ExpressionDraftCapabilities,
        recall_allowed: bool,
        require_turn_posture: bool = False,
    ) -> InboundToolContract:
        if phase not in {"initial", "after_recall", "final"}:
            raise ValueError("unsupported inbound tool phase")
        if transport not in {"atomic", "stream"}:
            raise ValueError("unsupported inbound tool transport")
        recall_allowed = phase == "initial" and recall_allowed
        tool_name = (
            f"character_inbound_{phase}_v{_CONTRACT_VERSION}"
            if transport == "atomic" and phase in {"initial", "after_recall"}
            else f"character_inbound_{phase}_{transport}_v{_CONTRACT_VERSION}"
        )
        appraisal_schema = _provider_schema(AppraisalDraftWire)
        appraisal_required = appraisal_schema.get("required")
        if not isinstance(appraisal_required, list):
            raise ValueError("AppraisalDraft canonical schema has no required fields")
        appraisal_schema["required"] = sorted({*appraisal_required, "affect"})
        appraisal_schema["anyOf"] = AppraisalDraftWire.provider_lifecycle_branches()
        expression_schema = _capability_expression_schema(
            capabilities,
            require_turn_posture=require_turn_posture,
        )
        decision_properties: dict[str, object] = {
            "result_kind": {"type": "string", "enum": ["decision"]},
            "appraisal_draft": appraisal_schema,
        }
        decision_required = ["result_kind", "appraisal_draft", "expression_draft"]
        if transport == "atomic":
            decision_properties["expression_draft"] = expression_schema
        else:
            expression_properties = expression_schema.get("properties")
            expression_required = expression_schema.get("required")
            if not isinstance(expression_properties, dict) or not isinstance(
                expression_required, list
            ):
                raise ValueError("ExpressionDraft stream schema is incomplete")
            beat_array = expression_properties.get("beats")
            if not isinstance(beat_array, dict) or not isinstance(
                beat_array.get("items"), dict
            ):
                raise ValueError("ExpressionDraft stream beat schema is incomplete")
            deferred_beats = deepcopy(beat_array)
            deferred_beats["maxItems"] = capabilities.max_later_beats
            deferred_modality = deferred_beats["items"].get("properties", {}).get(
                "modality"
            )
            if not isinstance(deferred_modality, dict):
                raise ValueError("ExpressionDraft deferred beat modality is incomplete")
            deferred_modality["enum"] = ["text"]
            head_properties = {
                key: deepcopy(value)
                for key, value in expression_properties.items()
                if key not in {"beats", "episode_disposition"}
            }
            head_properties.update(
                {
                    "type": {"type": "string", "enum": ["head"]},
                    "beat": deepcopy(beat_array["items"]),
                    "beats": deferred_beats,
                    "leading_typing_beat": deepcopy(beat_array["items"]),
                }
            )
            beat_modality = head_properties["beat"].get("properties", {}).get(
                "modality"
            )
            if isinstance(beat_modality, dict):
                beat_modality["enum"] = [
                    modality for modality in capabilities.modalities if modality != "typing"
                ]
            typing_modality = head_properties["leading_typing_beat"].get(
                "properties", {}
            ).get("modality")
            if isinstance(typing_modality, dict):
                typing_modality["enum"] = ["typing"]
            head_required = [
                "type",
                *[
                    field
                    for field in expression_required
                    if field not in {"beats", "episode_disposition"}
                ],
            ]
            continuation = {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["beat"]},
                    "beat": deepcopy(beat_array["items"]),
                    "world_claims": deepcopy(expression_properties["world_claims"]),
                },
                "required": ["type", "beat", "world_claims"],
                "additionalProperties": False,
            }
            end = {
                "type": "object",
                "properties": {"type": {"type": "string", "enum": ["end"]}},
                "required": ["type"],
                "additionalProperties": False,
            }
            decision_properties.update(
                {
                    "protocol": {
                        "type": "string",
                        "enum": ["character-interior-events.1"],
                    },
                    "events": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": capabilities.max_beats + 2,
                        "items": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": head_properties,
                                    "required": head_required,
                                    "additionalProperties": False,
                                    "anyOf": [
                                        {
                                            "properties": {
                                                "timing_choice": {"enum": ["now"]}
                                            },
                                            "required": ["beat"],
                                        },
                                        {
                                            "properties": {
                                                "timing_choice": {"enum": ["later"]}
                                            },
                                            "required": ["beats"],
                                        },
                                        {
                                            "properties": {
                                                "timing_choice": {"enum": ["silent"]}
                                            }
                                        },
                                    ],
                                },
                                continuation,
                                end,
                            ]
                        },
                    },
                }
            )
            decision_required = [
                "result_kind",
                "protocol",
                "appraisal_draft",
                "events",
            ]
        decision_branch: dict[str, object] = {
            "type": "object",
            "properties": decision_properties,
            "required": decision_required,
            "additionalProperties": False,
        }
        branches: list[dict[str, object]] = [decision_branch]
        if recall_allowed:
            recall_required = ["result_kind", "recall_request"]
            if capabilities.private_turn_state_mode == "required":
                recall_required.append("private_turn_state")
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "result_kind": {"type": "string", "enum": ["recall"]},
                        "recall_request": _provider_schema(CharacterRecallRequest),
                        "private_turn_state": _provider_schema(PrivateTurnState),
                    },
                    "required": recall_required,
                    "additionalProperties": False,
                }
            )
        parameters = {
            "anyOf": branches,
        }
        function = {
            "name": tool_name,
            "description": (
                "Return one inbound character result. "
                + (
                    "result_kind is transport-only: choose recall only when requesting the "
                    "available recall-first path; otherwise return the complete appraisal_draft "
                    + (
                        "and append-only expression events you chose."
                        if transport == "stream"
                        else "and expression_draft you chose."
                    )
                    if recall_allowed
                    else "Return result_kind=decision with the complete appraisal_draft and "
                    + (
                        "append-only expression events you chose; recall is not available "
                        "on this call."
                        if transport == "stream"
                        else "expression_draft you chose; recall is not available on this call."
                    )
                )
                + " Deployment capability profile="
                + capabilities.profile_id
                + f"; max_beats={capabilities.max_beats}; "
                + f"max_later_beats={capabilities.max_later_beats}."
            ),
            "parameters": parameters,
        }
        provider_tools = ({"type": "function", "function": function},)
        digest = "sha256:" + sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()
        capabilities_digest = "sha256:" + sha256(
            _canonical_json(capabilities.model_dump(mode="json")).encode("utf-8")
        ).hexdigest()
        contract_digest = "sha256:" + sha256(
            _canonical_json(
                {
                    "phase": phase,
                    "transport": transport,
                    "recall_allowed": recall_allowed,
                    "require_turn_posture": require_turn_posture,
                    "schema_sha256": digest,
                    "capabilities_sha256": capabilities_digest,
                    "tool_name": tool_name,
                }
            ).encode("utf-8")
        ).hexdigest()
        identity = InboundToolContractIdentity(
            contract_id="character-inbound-forced-tool",
            phase=phase,
            transport=transport,
            tool_name=tool_name,
            version=_CONTRACT_VERSION,
            schema_sha256=digest,
            capabilities_sha256=capabilities_digest,
            contract_sha256=contract_digest,
        )
        return InboundToolContract(
            phase=phase,
            transport=transport,
            capabilities=capabilities,
            recall_allowed=recall_allowed,
            require_turn_posture=require_turn_posture,
            provider_tools=provider_tools,
            provider_tool_choice={"type": "function", "function": {"name": tool_name}},
            identity=identity,
        )


__all__ = [
    "InboundToolContract",
    "InboundToolContractIdentity",
    "InboundToolContracts",
    "InboundToolPhase",
    "InboundToolTransport",
]
