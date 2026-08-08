"""Versioned provider transport for structured background character purposes.

The contract compiler derives provider JSON Schema from the canonical typed
role wire and purpose payload.  It owns transport shape and request identity
only; it never supplies a character choice or repairs semantic output.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from typing import Mapping

_CONTRACT_VERSION = "1"
_PROACTIVE_TOOL_NAME = "character_role_proactive_contact_v1"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inline_refs(schema: object, definitions: dict[str, object]) -> object:
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


@lru_cache(maxsize=8)
def _compiled_provider_schema(model_type: object) -> dict[str, object]:
    generated = getattr(model_type, "model_json_schema")(mode="validation")
    if not isinstance(generated, dict):
        raise TypeError("canonical role schema must be one object")
    definitions = generated.get("$defs")
    converted = _inline_refs(
        generated,
        definitions if isinstance(definitions, dict) else {},
    )
    if not isinstance(converted, dict):
        raise TypeError("provider role schema must be one object")
    return converted


def _provider_schema(model_type: object) -> dict[str, object]:
    # Callers specialize a private copy.  The expensive Pydantic generation
    # and recursive ref closure happen once per canonical wire type.
    return deepcopy(_compiled_provider_schema(model_type))


def _required_object_properties(schema: dict[str, object]) -> dict[str, object]:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("canonical role schema has no properties")
    return properties


def _constrain_option(field: object, option_ids: list[str]) -> None:
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


def _non_null_schema(field: object, *, field_name: str) -> dict[str, object]:
    if not isinstance(field, dict):
        raise ValueError(f"canonical proactive {field_name} schema is incomplete")
    variants = field.get("anyOf")
    if isinstance(variants, list):
        non_null = [
            item for item in variants if isinstance(item, dict) and item.get("type") != "null"
        ]
        if len(non_null) == 1:
            return deepcopy(non_null[0])
    if field.get("type") != "null":
        return deepcopy(field)
    raise ValueError(f"canonical proactive {field_name} has no non-null schema")


def _close_beat_value_choice(beat_schema: dict[str, object]) -> None:
    properties = _required_object_properties(beat_schema)
    branches: list[dict[str, object]] = []
    for modality, value_field in (
        ("text", "text"),
        ("reaction", "reaction_id"),
        ("sticker", "sticker_id"),
    ):
        branch_properties: dict[str, object] = {
            "modality": {"enum": [modality]},
            value_field: _non_null_schema(
                properties.get(value_field),
                field_name=value_field,
            ),
        }
        branch_properties.update(
            {
                other: {"type": "null"}
                for other in ("text", "reaction_id", "sticker_id")
                if other != value_field
            }
        )
        branches.append(
            {
                "properties": branch_properties,
                "required": [value_field],
            }
        )
    branches.append(
        {
            "properties": {
                "modality": {"enum": ["typing"]},
                "text": {"type": "null"},
                "reaction_id": {"type": "null"},
                "sticker_id": {"type": "null"},
            }
        }
    )
    beat_schema["anyOf"] = branches


def _close_world_claim_sources(world_claims: object) -> None:
    if not isinstance(world_claims, dict) or not isinstance(world_claims.get("items"), dict):
        raise ValueError("canonical proactive world claim schema is incomplete")
    claim = world_claims["items"]
    source_refs = _required_object_properties(claim).get("source_refs")
    if not isinstance(source_refs, dict):
        raise ValueError("canonical proactive world claim sources are incomplete")
    claim["anyOf"] = [
        {
            "properties": {
                "scope": {
                    "enum": [
                        "current_world",
                        "past_world",
                        "counterpart_history",
                        "shared_history",
                    ]
                },
                "source_refs": {**deepcopy(source_refs), "minItems": 1},
            },
            "required": ["source_refs"],
        },
        {"properties": {"scope": {"enum": ["stable_identity"]}}},
        {
            "properties": {
                "scope": {"enum": ["subjective_or_hypothetical"]},
                "source_refs": {**deepcopy(source_refs), "maxItems": 0},
            }
        },
    ]


def _proactive_payload_schema(
    expression_capabilities: Mapping[str, object],
) -> dict[str, object]:
    from ..expression_draft import ExpressionDraft

    # ProactiveDraft only tightens ExpressionDraft.impulse_summary from
    # optional to required.  Deriving the common wire here avoids importing
    # the proactive runtime back through the CharacterInterior package graph.
    schema = _provider_schema(ExpressionDraft)
    properties = _required_object_properties(schema)
    # The same role authors the private self through the outer canonical
    # summary/attention fields.  The trusted boundary materializes that exact
    # value into ProactiveDraft.private_turn_state after validation.
    properties.pop("private_turn_state", None)
    required = {
        "timing_choice",
        "cadence",
        "beats",
        "stance",
        "brief_rationale",
        "impulse_summary",
        "confidence",
        "world_claims",
    }
    schema["required"] = sorted(required)
    properties["impulse_summary"] = _non_null_schema(
        properties.get("impulse_summary"),
        field_name="impulse_summary",
    )
    _close_world_claim_sources(properties.get("world_claims"))

    max_beats = expression_capabilities.get("max_beats")
    max_later_beats = expression_capabilities.get("max_later_beats")
    modalities = expression_capabilities.get("modalities")
    if (
        not isinstance(max_beats, int)
        or not 1 <= max_beats <= 16
        or not isinstance(max_later_beats, int)
        or not 1 <= max_later_beats <= max_beats
        or not isinstance(modalities, list | tuple)
        or not modalities
        or any(not isinstance(item, str) or not item for item in modalities)
    ):
        raise ValueError("proactive expression capability profile is malformed")
    beats = properties.get("beats")
    if not isinstance(beats, dict) or not isinstance(beats.get("items"), dict):
        raise ValueError("canonical proactive schema has no beat items")
    beats["maxItems"] = max_beats
    beat_properties = beats["items"].get("properties")
    if not isinstance(beat_properties, dict):
        raise ValueError("canonical proactive beat schema has no properties")
    modality = beat_properties.get("modality")
    if not isinstance(modality, dict):
        raise ValueError("canonical proactive beat schema has no modality")
    modality["enum"] = list(modalities)
    _close_beat_value_choice(beats["items"])

    reaction_options = expression_capabilities.get("reaction_options")
    sticker_options = expression_capabilities.get("sticker_options")
    _constrain_option(
        beat_properties.get("reaction_id"),
        [
            item["option_id"]
            for item in reaction_options
            if isinstance(item, dict) and isinstance(item.get("option_id"), str)
        ]
        if isinstance(reaction_options, list | tuple)
        else [],
    )
    _constrain_option(
        beat_properties.get("sticker_id"),
        [
            item["option_id"]
            for item in sticker_options
            if isinstance(item, dict) and isinstance(item.get("option_id"), str)
        ]
        if isinstance(sticker_options, list | tuple)
        else [],
    )

    later_beats = deepcopy(beats)
    later_beats["maxItems"] = max_later_beats
    later_modality = later_beats["items"].get("properties", {}).get("modality")
    if not isinstance(later_modality, dict):
        raise ValueError("canonical proactive later beat schema has no modality")
    later_modality["enum"] = ["text"]
    later_beats["minItems"] = 1

    immediate_beats = deepcopy(beats)
    immediate_beats["minItems"] = 1
    item_schema = immediate_beats.get("items")
    if not isinstance(item_schema, dict):
        raise ValueError("canonical proactive beat item schema is incomplete")
    visible_item = deepcopy(item_schema)
    visible_properties = visible_item.get("properties")
    if not isinstance(visible_properties, dict):
        raise ValueError("canonical proactive visible beat schema is incomplete")
    visible_modality = visible_properties.get("modality")
    if not isinstance(visible_modality, dict):
        raise ValueError("canonical proactive visible modality is incomplete")
    visible_modality["enum"] = [item for item in modalities if item != "typing"]
    # JSON Schema has no direct "last item" keyword.  The bounded canonical
    # max lets the standard Draft 2020 prefixItems vocabulary express the
    # terminal-visible invariant without removing legal leading/interstitial
    # typing beats from the character's choice.
    immediate_beats["allOf"] = [
        {
            "anyOf": [
                {
                    "minItems": length,
                    "maxItems": length,
                    "prefixItems": [
                        *[deepcopy(item_schema) for _ in range(length - 1)],
                        deepcopy(visible_item),
                    ],
                }
                for length in range(1, max_beats + 1)
            ]
        }
    ]

    no_due_window = {
        "delay_seconds": {"type": "null"},
        "expires_after_seconds": {"type": "null"},
    }
    schema["anyOf"] = [
        {
            "properties": {
                "timing_choice": {"enum": ["now"]},
                "beats": immediate_beats,
                "turn_posture": {"enum": [None, "continue", "interject", "supersede"]},
                **no_due_window,
            }
        },
        {
            "properties": {
                "timing_choice": {"enum": ["later"]},
                "beats": later_beats,
                "delay_seconds": _non_null_schema(
                    properties.get("delay_seconds"),
                    field_name="delay_seconds",
                ),
                "expires_after_seconds": _non_null_schema(
                    properties.get("expires_after_seconds"),
                    field_name="expires_after_seconds",
                ),
                "turn_posture": {"enum": [None, "yield", "continue", "supersede"]},
            },
            "required": ["delay_seconds", "expires_after_seconds"],
        },
        {
            "properties": {
                "timing_choice": {"enum": ["silent"]},
                "beats": {**deepcopy(beats), "maxItems": 0},
                "turn_posture": {"enum": [None, "yield", "continue", "supersede"]},
                "response_expectation": {"type": "null"},
                **no_due_window,
            }
        },
    ]
    return schema


@dataclass(frozen=True, slots=True)
class StructuredRoleToolContractIdentity:
    contract_id: str
    purpose: str
    tool_name: str
    version: str
    schema_sha256: str
    capabilities_sha256: str
    contract_sha256: str
    recall_allowed: str

    def request_identity_material(self) -> dict[str, str]:
        return {
            "contract_id": self.contract_id,
            "purpose": self.purpose,
            "tool_name": self.tool_name,
            "version": self.version,
            "schema_sha256": self.schema_sha256,
            "capabilities_sha256": self.capabilities_sha256,
            "contract_sha256": self.contract_sha256,
            "recall_allowed": self.recall_allowed,
        }


@dataclass(frozen=True, slots=True)
class StructuredRoleToolContract:
    purpose: str
    provider_tools: tuple[dict[str, object], ...]
    provider_tool_choice: dict[str, object]
    identity: StructuredRoleToolContractIdentity


class StructuredRoleToolContracts:
    """Compile one purpose-scoped forced function from canonical typed wires."""

    @staticmethod
    def precompile() -> None:
        """Compile canonical Pydantic wires outside provider-entry budgets."""

        from ..expression_draft import ExpressionDraft
        from .structured_role import _WireRoleResult

        _compiled_provider_schema(ExpressionDraft)
        _compiled_provider_schema(_WireRoleResult)

    @classmethod
    def precompile_proactive(
        cls,
        *,
        expression_capabilities: Mapping[str, object],
    ) -> None:
        """Warm both proactive recall phases before a worker can claim work.

        The capability-specialized schema is immutable for one production
        composition, but its first construction still performs a deepcopy,
        branch specialization, and digest calculation.  Doing that lazily
        inside ``CharacterInterior.consider`` makes a tiny test/production
        author budget race the schema compiler before the provider is entered.
        Warming both legal phases here changes no role choice and preserves the
        same LRU key used by the live call.
        """

        payload = _canonical_json(expression_capabilities)
        cls._cached_proactive_contact(payload, True)
        cls._cached_proactive_contact(payload, False)

    def proactive_contact(
        self,
        *,
        capability_payload: Mapping[str, object],
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        expression_capabilities = capability_payload.get("expression_capabilities")
        if not isinstance(expression_capabilities, dict):
            raise ValueError("proactive capability lacks expression capabilities")
        return self._cached_proactive_contact(
            _canonical_json(expression_capabilities),
            recall_allowed,
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_proactive_contact(
        expression_capabilities_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        # Runtime invocation happens only after structured_role is fully
        # imported, so this local reference keeps one canonical wire without
        # creating an import cycle at module initialization.
        from .structured_role import _WireRoleResult

        expression_capabilities = json.loads(expression_capabilities_json)
        if not isinstance(expression_capabilities, dict):
            raise ValueError("proactive expression capabilities must be one object")
        role_schema = _provider_schema(_WireRoleResult)
        role_properties = _required_object_properties(role_schema)
        decision_schema = role_properties.get("decision")
        if not isinstance(decision_schema, dict):
            raise ValueError("canonical role schema has no decision")
        variants = decision_schema.get("anyOf")
        decision_object = (
            next(
                (
                    item
                    for item in variants
                    if isinstance(item, dict) and item.get("type") == "object"
                ),
                None,
            )
            if isinstance(variants, list)
            else decision_schema
        )
        if not isinstance(decision_object, dict):
            raise ValueError("canonical role decision schema is incomplete")
        decision_properties = _required_object_properties(decision_object)
        decision_properties["payload"] = _proactive_payload_schema(expression_capabilities)
        decision_object["required"] = ["source_refs", "payload"]

        common = {
            key: deepcopy(role_properties[key])
            for key in (
                "summary",
                "attended_source_refs",
                "recall_query",
                "proposals",
            )
        }
        decision_branch = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["decision"]},
                **common,
                "decision": decision_object,
            },
            "required": [
                "status",
                "summary",
                "attended_source_refs",
                "decision",
                "recall_query",
                "proposals",
            ],
            "additionalProperties": False,
        }
        decision_branch["properties"]["recall_query"] = {"enum": [None]}
        decision_branch["properties"]["proposals"] = {
            **deepcopy(common["proposals"]),
            "maxItems": 0,
        }
        branches = [decision_branch]
        if recall_allowed:
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["recall_request"]},
                        "summary": deepcopy(common["summary"]),
                        "attended_source_refs": deepcopy(common["attended_source_refs"]),
                        "decision": {"enum": [None]},
                        "recall_query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1024,
                        },
                        "proposals": {
                            **deepcopy(common["proposals"]),
                            "maxItems": 0,
                        },
                    },
                    "required": [
                        "status",
                        "summary",
                        "attended_source_refs",
                        "decision",
                        "recall_query",
                        "proposals",
                    ],
                    "additionalProperties": False,
                }
            )
        parameters = {"anyOf": branches}
        function = {
            "name": _PROACTIVE_TOOL_NAME,
            "description": (
                "Return the complete source-bound proactive_contact role result. "
                "For timing_choice=later, expires_after_seconds MUST be greater "
                "than delay_seconds. "
                "The function constrains transport shape only; whether to act now, "
                "act later, stay silent, request recall, and every private or visible "
                "semantic field remain the character's choice."
            ),
            "parameters": parameters,
        }
        provider_tools = ({"type": "function", "function": function},)
        schema_digest = "sha256:" + sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()
        capabilities_digest = (
            "sha256:" + sha256(_canonical_json(expression_capabilities).encode("utf-8")).hexdigest()
        )
        contract_digest = (
            "sha256:"
            + sha256(
                _canonical_json(
                    {
                        "purpose": "proactive_contact",
                        "tool_name": _PROACTIVE_TOOL_NAME,
                        "version": _CONTRACT_VERSION,
                        "schema_sha256": schema_digest,
                        "capabilities_sha256": capabilities_digest,
                        "recall_allowed": recall_allowed,
                    }
                ).encode("utf-8")
            ).hexdigest()
        )
        identity = StructuredRoleToolContractIdentity(
            contract_id="character-role-forced-tool",
            purpose="proactive_contact",
            tool_name=_PROACTIVE_TOOL_NAME,
            version=_CONTRACT_VERSION,
            schema_sha256=schema_digest,
            capabilities_sha256=capabilities_digest,
            contract_sha256=contract_digest,
            recall_allowed=str(recall_allowed).lower(),
        )
        return StructuredRoleToolContract(
            purpose="proactive_contact",
            provider_tools=provider_tools,
            provider_tool_choice={
                "type": "function",
                "function": {"name": _PROACTIVE_TOOL_NAME},
            },
            identity=identity,
        )


__all__ = [
    "StructuredRoleToolContract",
    "StructuredRoleToolContractIdentity",
    "StructuredRoleToolContracts",
]
