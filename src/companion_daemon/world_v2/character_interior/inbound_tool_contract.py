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
InboundToolSchemaDialect = Literal["standard", "deepseek-strict"]
_CONTRACT_VERSION = "1"

# DeepSeek's strict tool dialect intentionally has a smaller JSON-Schema
# vocabulary than the canonical Pydantic wire.  The provider also requires
# every property of every object to be present; optional semantic fields are
# therefore transported as explicit ``null`` and retain their meaning in the
# existing canonical materializer.  This projection is kept here, rather than
# at call sites, so the standard provider path and the strict provider path
# cannot drift apart.
_DEEPSEEK_STRICT_UNSUPPORTED_KEYS = frozenset(
    {
        "default",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "title",
        "uniqueItems",
    }
)
_DEEPSEEK_STRICT_FORMATS = frozenset(
    {"email", "hostname", "ipv4", "ipv6", "uuid"}
)


def _nullable_strict_schema(schema: object) -> object:
    if isinstance(schema, dict):
        if schema.get("type") == "null":
            return schema
        variants = schema.get("anyOf")
        if isinstance(variants, list) and any(
            isinstance(item, dict) and item.get("type") == "null" for item in variants
        ):
            return schema
    return {"anyOf": [schema, {"type": "null"}]}


def _enum_json_type(value: object) -> str | None:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return None


def _deepseek_strict_schema(value: object) -> object:
    """Project one canonical schema into DeepSeek's strict tool dialect.

    The function is deliberately a pure schema adapter.  It never changes
    the canonical decoder or adds a semantic default; omitted optional values
    become explicit JSON nulls and are still validated by the host afterward.
    """

    if isinstance(value, list):
        return [_deepseek_strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected: dict[str, object] = {}
    for key, item in value.items():
        if key in _DEEPSEEK_STRICT_UNSUPPORTED_KEYS:
            continue
        if key == "format" and item not in _DEEPSEEK_STRICT_FORMATS:
            continue
        if key == "const":
            projected["enum"] = [item]
            continue
        projected[key] = _deepseek_strict_schema(item)

    properties = projected.get("properties")
    if not isinstance(properties, dict):
        return projected

    original_required = set(projected.get("required", ()))
    properties = {
        key: _deepseek_strict_schema(item) for key, item in properties.items()
    }
    projected["properties"] = properties

    # Branches such as affect lifecycle, timing, and the outer recall/decision
    # union are object schemas without a full property inventory.  Expand each
    # branch to the same required-null envelope, preserving non-null fields
    # that the canonical object already required.
    branches = projected.get("anyOf")
    if isinstance(branches, list):
        projected_branches: list[object] = []
        for branch in branches:
            if not isinstance(branch, dict):
                projected_branches.append(branch)
                continue
            branch = dict(branch)
            branch.setdefault("type", "object")
            branch_properties = branch.get("properties")
            if not isinstance(branch_properties, dict):
                branch_properties = {}
            branch_properties = {
                key: _deepseek_strict_schema(item)
                for key, item in branch_properties.items()
            }
            for key, schema in properties.items():
                if key not in branch_properties:
                    branch_properties[key] = (
                        schema if key in original_required else _nullable_strict_schema(schema)
                    )
            branch["properties"] = branch_properties
            branch["required"] = list(properties)
            branch["additionalProperties"] = False
            projected_branches.append(branch)
        projected["anyOf"] = projected_branches

    projected["required"] = list(properties)
    projected["additionalProperties"] = False
    return projected


def _deepseek_documented_schema_subset(value: object) -> object:
    """Keep only the strict JSON-Schema subset documented by DeepSeek."""

    if isinstance(value, list):
        return [_deepseek_documented_schema_subset(item) for item in value]
    if not isinstance(value, dict):
        return value
    projected = {
        key: _deepseek_documented_schema_subset(item)
        for key, item in value.items()
        if key not in {"allOf", "not", "oneOf", "prefixItems"}
    }
    enum = projected.get("enum")
    if "type" not in projected and isinstance(enum, list) and enum:
        typed_values: dict[str, list[object]] = {}
        for item in enum:
            item_type = _enum_json_type(item)
            if item_type is None:
                break
            typed_values.setdefault(item_type, []).append(item)
        else:
            if len(typed_values) == 1:
                projected["type"] = next(iter(typed_values))
            else:
                projected.pop("enum", None)
                projected["anyOf"] = [
                    {"type": item_type, "enum": items}
                    for item_type, items in typed_values.items()
                ]
    return projected


def deepseek_strict_tool_schema(value: object) -> object:
    """Project a background role tool into DeepSeek's documented strict subset.

    The interactive inbound contract retains its already qualified request
    identity. New background strict tools additionally remove unsupported
    composition keywords and type every ``anyOf`` branch before provider use.
    """

    return _deepseek_documented_schema_subset(_deepseek_strict_schema(value))


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
    media_request = properties.get("media_request")
    if not isinstance(media_request, dict):
        raise ValueError("ExpressionDraft canonical schema has no media_request")
    media_request["enum"] = (
        ["none", "consider_available_candidate"]
        if capabilities.media_request_mode == "candidate_only"
        else ["none"]
    )
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
    # Canonical replay may inherit the safe empty default, but a strict-tool
    # provider cannot omit object properties.  Requiring the live wire to say
    # ``world_claims=[]`` keeps each timing branch non-null and prevents a
    # schema-valid JSON null from consuming the role's one correction.
    required = required | {"world_claims"}
    if capabilities.private_turn_state_mode == "required":
        required = required | {"private_turn_state"}
    schema["required"] = sorted(required)
    # Provider JSON-schema support has a dependable ``anyOf`` subset.  Compile
    # every timing/posture invariant that the provider dialect can express so
    # an ordinary ``now + yield`` or ``later + interject`` choice is rejected
    # before it spends the one bounded host correction.  The canonical model
    # remains authoritative for relative comparisons such as expiry > delay.
    no_due_window = {
        "delay_seconds": {"type": "null"},
        "expires_after_seconds": {"type": "null"},
    }
    schema["anyOf"] = [
        {
            "properties": {
                "timing_choice": {"enum": ["now"]},
                "beats": beats,
                "turn_posture": {
                    "enum": [None, "continue", "interject", "supersede"]
                },
                **no_due_window,
            }
        },
        {
            "properties": {
                "timing_choice": {"enum": ["later"]},
                "beats": later_beats,
                "turn_posture": {
                    "enum": [None, "yield", "continue", "supersede"]
                },
            }
        },
        {
            "properties": {
                "timing_choice": {"enum": ["silent"]},
                "beats": {**deepcopy(beats), "maxItems": 0},
                "turn_posture": {
                    "enum": [None, "yield", "continue", "supersede"]
                },
                "response_expectation": {"type": "null"},
                **no_due_window,
            }
        },
    ]
    return schema


@dataclass(frozen=True)
class InboundToolContractIdentity:
    contract_id: str
    phase: InboundToolPhase
    transport: InboundToolTransport
    schema_dialect: InboundToolSchemaDialect
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
            "schema_dialect": self.schema_dialect,
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
        if self.identity.schema_dialect == "deepseek-strict":
            # DeepSeek strict mode requires every property of the outer
            # object to be present.  The branch that was not selected is
            # represented by explicit nulls; remove only those transport-null
            # siblings before applying the ordinary exact-envelope rules.
            parameters = self.provider_tools[0]["function"].get("parameters")
            properties = parameters.get("properties") if isinstance(parameters, dict) else None
            if not isinstance(properties, dict) or set(value) != set(properties):
                raise ValueError("DeepSeek strict transport envelope is incomplete")
            value = {
                key: item
                for key, item in value.items()
                if key == "result_kind" or item is not None
            }
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
        schema_dialect: InboundToolSchemaDialect = "standard",
    ) -> InboundToolContract:
        if phase not in {"initial", "after_recall", "final"}:
            raise ValueError("unsupported inbound tool phase")
        if transport not in {"atomic", "stream"}:
            raise ValueError("unsupported inbound tool transport")
        if schema_dialect not in {"standard", "deepseek-strict"}:
            raise ValueError("unsupported inbound tool schema dialect")
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
            # The stream head has three mutually exclusive beat transports:
            # an immediate visible beat (optionally preceded by typing), a
            # deferred beat array, or no beat for silence.  Required-tool
            # providers require every property to be present, so merely
            # making the sibling fields nullable is not enough: models may
            # otherwise emit an empty array/object for both transports.  Put
            # the transport exclusion directly in each timing branch.  This
            # mirrors ``_expression_event_head`` and prevents a provider from
            # returning a schema-valid but locally ambiguous head.
            null_transport = {"type": "null"}
            now_head_branch = {
                "properties": {
                    "timing_choice": {"enum": ["now"]},
                    "turn_posture": {
                        "enum": [None, "continue", "interject", "supersede"]
                    },
                    "beat": deepcopy(beat_array["items"]),
                    "beats": null_transport,
                },
                "required": ["beat"],
            }
            later_head_branch = {
                "properties": {
                    "timing_choice": {"enum": ["later"]},
                    "turn_posture": {
                        "enum": [None, "yield", "continue", "supersede"]
                    },
                    "beat": null_transport,
                    "beats": deferred_beats,
                    "leading_typing_beat": null_transport,
                },
                "required": ["beats"],
            }
            silent_head_branch = {
                "properties": {
                    "timing_choice": {"enum": ["silent"]},
                    "turn_posture": {
                        "enum": [None, "yield", "continue", "supersede"]
                    },
                    "beat": null_transport,
                    "beats": null_transport,
                    "leading_typing_beat": null_transport,
                }
            }
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
                                        now_head_branch,
                                        later_head_branch,
                                        silent_head_branch,
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
        # Make the union's branch-exclusive fields explicit.  The ordinary
        # dialect may omit these siblings, while DeepSeek strict requires
        # every property to be present; using ``type: null`` here makes the
        # decision branch unable to fill recall fields (and vice versa)
        # instead of relying on a post-hoc host rejection.
        all_branch_properties: set[str] = set()
        for branch in branches:
            properties = branch.get("properties")
            if not isinstance(properties, dict):
                raise ValueError("inbound tool branch has no object properties")
            all_branch_properties.update(properties)
        for branch in branches:
            properties = branch["properties"]
            assert isinstance(properties, dict)
            for property_name in all_branch_properties - set(properties):
                properties[property_name] = {"type": "null"}
        # DeepSeek's function-calling dialect requires every function's root
        # parameters schema to declare ``type: object``.  Keep the semantic
        # decision/recall union below that provider-compatible root; the root
        # properties are only the lossless union of branch properties, while
        # the branch schemas and local ``unwrap`` remain authoritative for
        # exact result-kind validation.
        root_properties: dict[str, object] = {}
        for branch in branches:
            branch_properties = branch.get("properties")
            if not isinstance(branch_properties, dict):
                raise ValueError("inbound tool branch has no object properties")
            for property_name, property_schema in branch_properties.items():
                if property_name != "result_kind":
                    current = root_properties.get(property_name)
                    if current is None:
                        root_properties[property_name] = deepcopy(property_schema)
                        continue
                    if not isinstance(current, dict) or not isinstance(property_schema, dict):
                        raise ValueError("inbound root property schema is not an object")
                    current_is_null = current.get("type") == "null"
                    branch_is_null = property_schema.get("type") == "null"
                    if current_is_null and not branch_is_null:
                        root_properties[property_name] = {
                            "anyOf": [deepcopy(property_schema), {"type": "null"}]
                        }
                    elif not current_is_null and branch_is_null:
                        root_properties[property_name] = {
                            "anyOf": [deepcopy(current), {"type": "null"}]
                        }
                    elif current != property_schema:
                        root_properties[property_name] = {
                            "anyOf": [deepcopy(current), deepcopy(property_schema)]
                        }
                    continue
                # ``result_kind`` is shared by both branches.  The provider
                # facing envelope must admit every branch discriminator; the
                # branch-level ``anyOf`` schemas still enforce the exact
                # discriminator/field pairing and ``unwrap`` remains the
                # semantic authority after transport decoding.
                current = root_properties.get(property_name)
                if current is None:
                    root_properties[property_name] = deepcopy(property_schema)
                    continue
                if not isinstance(current, dict) or not isinstance(property_schema, dict):
                    raise ValueError("inbound result_kind schema is not an object")
                current_enum = current.get("enum")
                branch_enum = property_schema.get("enum")
                if not isinstance(current_enum, list) or not isinstance(branch_enum, list):
                    raise ValueError("inbound result_kind schema has no enum")
                current["enum"] = list(dict.fromkeys([*current_enum, *branch_enum]))
        parameters = {
            "type": "object",
            "properties": root_properties,
            "required": ["result_kind"],
            "additionalProperties": False,
            "anyOf": branches,
        }
        if schema_dialect == "deepseek-strict":
            strict_parameters = _deepseek_strict_schema(parameters)
            if not isinstance(strict_parameters, dict):
                raise ValueError("DeepSeek strict tool parameters must be an object")
            parameters = strict_parameters
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
                + " For timing_choice=now return at least one visible beat and never choose "
                "turn_posture=yield. For timing_choice=later return at least one text beat "
                "and never choose interject. For timing_choice=silent return no beats and "
                "never choose interject."
            ),
            "parameters": parameters,
        }
        if schema_dialect == "deepseek-strict":
            function["strict"] = True
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
                    "schema_dialect": schema_dialect,
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
            schema_dialect=schema_dialect,
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
    "InboundToolSchemaDialect",
    "InboundToolPhase",
    "InboundToolTransport",
]
