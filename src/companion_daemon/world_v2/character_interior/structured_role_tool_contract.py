"""Versioned provider transport for structured background character purposes.

The contract compiler derives provider JSON Schema from the canonical typed
role wire and purpose payload.  It owns transport shape and request identity
only; it never supplies a character choice or repairs semantic output.
"""

from __future__ import annotations

from collections.abc import Mapping as ABCMapping
from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
from typing import Mapping

from ..schema_core import canonicalize_json_value

_CONTRACT_VERSION = "1"
_MEDIA_SELECTION_TOOL_NAME = "character_role_media_selection_v1"
_QQ_ATTACHMENT_PERCEPTION_TOOL_NAME = "character_role_qq_attachment_perception_v1"
_PROACTIVE_TOOL_NAME = "character_role_proactive_contact_v1"
_WORLD_STIMULUS_TOOL_NAME = "character_role_world_stimulus_appraisal_v1"
_PRIVATE_IMPRESSION_TOOL_NAME = "character_role_private_impression_reflection_v1"
_OUTCOME_SELECTION_TOOL_NAME = "character_role_outcome_selection_v1"
_ACTIVITY_LIFECYCLE_TOOL_NAME = "character_role_activity_lifecycle_choice_v1"
_LIFE_DEVELOPMENT_TOOL_NAME = "character_role_life_development_choice_v1"
_EXPRESSION_RECONSIDERATION_TOOL_NAME = "character_role_expression_reconsideration_v1"
_FACT_MEMORY_RETENTION_TOOL_NAME = "character_role_fact_memory_retention_v1"
_EXPERIENCE_MEMORY_RETENTION_TOOL_NAME = "character_role_experience_memory_retention_v1"
_MEMORY_WITHDRAWAL_REVIEW_TOOL_NAME = "character_role_memory_withdrawal_review_v1"


def _canonical_json(value: object) -> str:
    value = _canonical_json_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_json_value(value: object) -> object:
    """Turn frozen capability mappings into stable JSON without changing meaning."""

    if isinstance(value, ABCMapping):
        return {
            key: _canonical_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_canonical_json_value(item) for item in value]
    return canonicalize_json_value(value)


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


# The faculty precompiles every built-in purpose (currently more than eight
# canonical wires).  A smaller cache evicted ExpressionDraft/_WireRoleResult
# before the first capability-specialized proactive contract and put Pydantic
# schema generation back on the provider-entry path.
@lru_cache(maxsize=32)
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


def _compile_generic_decision_contract(
    *,
    purpose: str,
    tool_name: str,
    payload_schema: dict[str, object],
    capability_identity: object,
    source_refs: tuple[str, ...],
    recall_allowed: bool,
    description: str,
) -> StructuredRoleToolContract:
    """Build the shared role-result transport envelope for choice purposes.

    The payload remains purpose-specific and is always derived by the caller
    from its canonical typed model.  This helper owns only the repeated
    decision/recall transport, source binding, and request identity plumbing.
    """

    if (
        any(not isinstance(item, str) or not item for item in source_refs)
        or len(source_refs) != len(set(source_refs))
        or len(source_refs) > 64
    ):
        raise ValueError(f"{purpose} source refs are malformed")

    from .structured_role import _WireRoleResult

    role_schema = _provider_schema(_WireRoleResult)
    role_properties = _required_object_properties(role_schema)
    decision_schema = role_properties.get("decision")
    if not isinstance(decision_schema, dict):
        raise ValueError(f"canonical {purpose} role schema has no decision")
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
        raise ValueError(f"canonical {purpose} role decision schema is incomplete")
    decision_properties = _required_object_properties(decision_object)
    if source_refs:
        source_ref_schema = decision_properties.get("source_refs")
        if not isinstance(source_ref_schema, dict):
            raise ValueError(f"{purpose} source-ref schema is incomplete")
        source_ref_items = source_ref_schema.get("items")
        if not isinstance(source_ref_items, dict):
            raise ValueError(f"{purpose} source-ref items are incomplete")
        decision_properties["source_refs"] = {
            **deepcopy(source_ref_schema),
            "minItems": len(source_refs),
            "maxItems": len(source_refs),
            "items": {**deepcopy(source_ref_items), "enum": list(source_refs)},
            "prefixItems": [{"const": item} for item in source_refs],
        }
    decision_properties["payload"] = deepcopy(payload_schema)
    decision_object["required"] = ["source_refs", "payload"]

    common = {
        key: deepcopy(role_properties[key])
        for key in ("summary", "attended_source_refs", "recall_query", "proposals")
    }
    required = [
        "status",
        "summary",
        "attended_source_refs",
        "decision",
        "recall_query",
        "proposals",
    ]
    decision_branch = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["decision"]},
            **common,
            "decision": decision_object,
        },
        "required": required,
        "additionalProperties": False,
    }
    decision_branch["properties"]["recall_query"] = {"type": "null"}
    decision_branch["properties"]["proposals"] = {
        **deepcopy(common["proposals"]),
        "maxItems": 0,
    }
    branches: list[dict[str, object]] = [decision_branch]
    if recall_allowed:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["recall_request"]},
                    "summary": deepcopy(common["summary"]),
                    "attended_source_refs": deepcopy(common["attended_source_refs"]),
                    "decision": {"type": "null"},
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
                "required": required,
                "additionalProperties": False,
            }
        )
    parameters = {"type": "object", "anyOf": branches}
    function = {
        "name": tool_name,
        "description": description,
        "parameters": parameters,
    }
    provider_tools = ({"type": "function", "function": function},)
    schema_digest = "sha256:" + sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()
    capabilities_digest = "sha256:" + sha256(
        _canonical_json(
            {"payload": capability_identity, "source_refs": source_refs}
        ).encode("utf-8")
    ).hexdigest()
    contract_digest = "sha256:" + sha256(
        _canonical_json(
            {
                "purpose": purpose,
                "tool_name": tool_name,
                "version": _CONTRACT_VERSION,
                "schema_sha256": schema_digest,
                "capabilities_sha256": capabilities_digest,
                "recall_allowed": recall_allowed,
            }
        ).encode("utf-8")
    ).hexdigest()
    identity = StructuredRoleToolContractIdentity(
        contract_id="character-role-forced-tool",
        purpose=purpose,
        tool_name=tool_name,
        version=_CONTRACT_VERSION,
        schema_sha256=schema_digest,
        capabilities_sha256=capabilities_digest,
        contract_sha256=contract_digest,
        recall_allowed=str(recall_allowed).lower(),
    )
    return StructuredRoleToolContract(
        purpose=purpose,
        provider_tools=provider_tools,
        provider_tool_choice={
            "type": "function",
            "function": {"name": tool_name},
        },
        identity=identity,
    )


class StructuredRoleToolContracts:
    """Compile one purpose-scoped forced function from canonical typed wires."""

    @staticmethod
    def precompile() -> None:
        """Compile canonical Pydantic wires outside provider-entry budgets."""

        from ..expression_draft import ExpressionDraft
        from .structured_role import (
            _ActivityLifecyclePayload,
            _ExpressionReconsiderationPayload,
            _MediaSelectionPayload,
            _MemoryRetentionPayload,
            _MemoryWithdrawalReviewPayload,
            _QQAttachmentPerceptionPayload,
            _PrivateImpressionProposal,
            _OutcomeSelectionPayload,
            _WorldStimulusAppraisalResult,
            _WireRoleResult,
        )
        from ..life_development_draft import (
            CharacterChoiceAcceptDraft,
            CharacterChoiceNoOpDraft,
        )

        _compiled_provider_schema(ExpressionDraft)
        _compiled_provider_schema(_ActivityLifecyclePayload)
        _compiled_provider_schema(_ExpressionReconsiderationPayload)
        _compiled_provider_schema(_MediaSelectionPayload)
        _compiled_provider_schema(_QQAttachmentPerceptionPayload)
        _compiled_provider_schema(_MemoryRetentionPayload)
        _compiled_provider_schema(_MemoryWithdrawalReviewPayload)
        _compiled_provider_schema(_WorldStimulusAppraisalResult)
        _compiled_provider_schema(_PrivateImpressionProposal)
        _compiled_provider_schema(_OutcomeSelectionPayload)
        _compiled_provider_schema(_WireRoleResult)
        _compiled_provider_schema(CharacterChoiceAcceptDraft)
        _compiled_provider_schema(CharacterChoiceNoOpDraft)

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

    def media_selection(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the role's select/no-op choice over media candidates."""

        return self._cached_media_selection(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def qq_attachment_perception(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the role's select/no-op choice over QQ attachments."""

        return self._cached_qq_attachment_perception(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def world_stimulus_appraisal(
        self,
        *,
        capability_payload: Mapping[str, object],
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the typed appraisal proposal envelope for one pinned wake.

        The payload schema is derived from ``_WorldStimulusAppraisalResult``;
        capability-dependent affect/relationship/source checks remain in the
        CharacterInterior materializer.  The provider contract therefore
        closes transport and typed field shape without choosing whether the
        character activates an appraisal, changes Affect, or does nothing.
        """

        return self._cached_world_stimulus_appraisal(
            _canonical_json(capability_payload),
            recall_allowed,
        )

    def private_impression_reflection(
        self,
        *,
        capability_payload: Mapping[str, object],
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the source-bound private-impression proposal wire.

        The capability exposes position-stable short tokens rather than long
        source refs.  The tool constrains those tokens and the offered expiry
        conditions, while the CharacterInterior materializer still owns the
        anchor/source closure and the decision to form no proposal.
        """

        return self._cached_private_impression_reflection(
            _canonical_json(capability_payload),
            recall_allowed,
        )

    def outcome_selection(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile one source-bound external outcome choice.

        The provider sees only the exact candidates and optional direction
        capability already admitted by the world runtime.  It cannot invent a
        candidate, settle an occurrence, or grant a biographical transition.
        Those remain downstream authority decisions.
        """

        return self._cached_outcome_selection(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def activity_lifecycle_choice(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the role's select/no-op activity opening choice."""

        return self._cached_activity_lifecycle_choice(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def life_development_choice(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the role's accept/no-op choice over one offered life opportunity."""

        return self._cached_life_development_choice(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def expression_reconsideration(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the source-bound disposition for an interrupted beat."""

        return self._cached_expression_reconsideration(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def memory_retention(
        self,
        *,
        purpose: str,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the shared fact/experience retain-or-no-change wire."""

        if purpose not in {"fact_memory_retention", "experience_memory_retention"}:
            raise ValueError("memory retention compiler received an unsupported purpose")
        return self._cached_memory_retention(
            purpose,
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    def memory_withdrawal_review(
        self,
        *,
        capability_payload: Mapping[str, object],
        source_refs: tuple[str, ...] = (),
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        """Compile the offered retain/forget/revise review disposition."""

        return self._cached_memory_withdrawal_review(
            _canonical_json(capability_payload),
            _canonical_json(source_refs),
            recall_allowed,
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_media_selection(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _MediaSelectionPayload

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("media selection capability must be one object")
        def _tokens(raw_items: object) -> list[str]:
            if not isinstance(raw_items, list):
                return []
            return [
                token
                for item in raw_items
                if isinstance(
                    token := (
                        item
                        if isinstance(item, str)
                        else item.get("token") if isinstance(item, dict) else None
                    ),
                    str,
                )
                and token
            ]

        offered_tokens = _tokens(capability_payload.get("offered_tokens"))
        candidate_tokens = _tokens(capability_payload.get("candidates"))
        if offered_tokens and candidate_tokens and set(offered_tokens) != set(candidate_tokens):
            raise ValueError("media selection token views disagree")
        offered = [*offered_tokens, *candidate_tokens]
        if (
            not offered
            or any(not isinstance(item, str) or not item for item in offered)
        ):
            raise ValueError("media selection offered tokens are malformed")
        offered = list(dict.fromkeys(offered))
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("media selection source refs are malformed")
        payload_schema = _provider_schema(_MediaSelectionPayload)
        properties = _required_object_properties(payload_schema)
        selected = properties.get("selected_token")
        if not isinstance(selected, dict):
            raise ValueError("media selection selected-token schema is incomplete")
        non_null_selected = _non_null_schema(selected, field_name="selected_token")
        # Keep no_op minimal, matching the local materializer.  A provider
        # must not send an explicit JSON null and rely on the host to decide
        # what that means.
        payload_schema["required"] = ["decision"]
        payload_schema["anyOf"] = [
            {
                "properties": {"decision": {"enum": ["no_op"]}},
                "required": ["decision"],
                "not": {"required": ["selected_token"]},
            },
            {
                "properties": {
                    "decision": {"enum": ["select"]},
                    "selected_token": {
                        **deepcopy(non_null_selected),
                        "enum": list(offered),
                    },
                },
                "required": ["decision", "selected_token"],
            },
        ]
        return _compile_generic_decision_contract(
            purpose="media_selection",
            tool_name=_MEDIA_SELECTION_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound media selection choice. The "
                "character may select one offered media candidate or explicitly "
                "choose no_op; the function constrains capability and transport "
                "shape only and does not choose for the character."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_qq_attachment_perception(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _QQAttachmentPerceptionPayload

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("QQ attachment perception capability must be one object")
        offered = capability_payload.get("offered_tokens")
        if (
            not isinstance(offered, list)
            or not offered
            or any(not isinstance(item, str) or not item for item in offered)
            or len(offered) != len(set(offered))
        ):
            raise ValueError("QQ attachment perception offered tokens are malformed")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("QQ attachment perception source refs are malformed")
        payload_schema = _provider_schema(_QQAttachmentPerceptionPayload)
        properties = _required_object_properties(payload_schema)
        selected = properties.get("selected_token")
        if not isinstance(selected, dict):
            raise ValueError("QQ attachment perception selected-token schema is incomplete")
        non_null_selected = _non_null_schema(selected, field_name="selected_token")
        payload_schema["required"] = ["decision"]
        payload_schema["anyOf"] = [
            {
                "properties": {"decision": {"enum": ["no_op"]}},
                "required": ["decision"],
                "not": {"required": ["selected_token"]},
            },
            {
                "properties": {
                    "decision": {"enum": ["select"]},
                    "selected_token": {
                        **deepcopy(non_null_selected),
                        "enum": list(offered),
                    },
                },
                "required": ["decision", "selected_token"],
            },
        ]
        return _compile_generic_decision_contract(
            purpose="qq_attachment_perception",
            tool_name=_QQ_ATTACHMENT_PERCEPTION_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound QQ attachment perception choice. "
                "The character may select one offered attachment or explicitly "
                "choose no_op; the function constrains capability and transport "
                "shape only and does not choose for the character."
            ),
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

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_world_stimulus_appraisal(
        capability_payload_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        # These imports are intentionally local: structured_role imports this
        # compiler during module initialization, while the canonical payload
        # model lives in structured_role itself.
        from .structured_role import _WorldStimulusAppraisalResult, _WireRoleResult

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("world stimulus capability must be one object")
        role_schema = _provider_schema(_WireRoleResult)
        role_properties = _required_object_properties(role_schema)
        common = {
            key: deepcopy(role_properties[key])
            for key in (
                "summary",
                "attended_source_refs",
            )
        }
        proposal_schema = _provider_schema(_WorldStimulusAppraisalResult)
        proposal_branch_properties = {
            **common,
            "decision": {"type": "null"},
            "recall_query": {"type": "null"},
            "proposals": {
                "type": "array",
                "minItems": 1,
                "maxItems": 1,
                "items": proposal_schema,
            },
        }

        branches: list[dict[str, object]] = []
        for status in ("no_change", "transition"):
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": [status]},
                        **deepcopy(proposal_branch_properties),
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
        if recall_allowed:
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["recall_request"]},
                        "summary": deepcopy(common["summary"]),
                        "attended_source_refs": deepcopy(common["attended_source_refs"]),
                        "decision": {"type": "null"},
                        "recall_query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1024,
                        },
                        "proposals": {"type": "array", "maxItems": 0},
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

        parameters = {"type": "object", "anyOf": branches}
        function = {
            "name": _WORLD_STIMULUS_TOOL_NAME,
            "description": (
                "Return the complete source-bound world_stimulus_appraisal role result. "
                "The character may choose no_change or a transition, including any legal "
                "Affect, relationship, aspiration, or experience proposal. The function "
                "constrains transport and typed shape only; it does not choose the appraisal "
                "or supply semantic values."
            ),
            "parameters": parameters,
        }
        provider_tools = ({"type": "function", "function": function},)
        schema_digest = "sha256:" + sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()
        capabilities_digest = (
            "sha256:" + sha256(_canonical_json(capability_payload).encode("utf-8")).hexdigest()
        )
        contract_digest = (
            "sha256:"
            + sha256(
                _canonical_json(
                    {
                        "purpose": "world_stimulus_appraisal",
                        "tool_name": _WORLD_STIMULUS_TOOL_NAME,
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
            purpose="world_stimulus_appraisal",
            tool_name=_WORLD_STIMULUS_TOOL_NAME,
            version=_CONTRACT_VERSION,
            schema_sha256=schema_digest,
            capabilities_sha256=capabilities_digest,
            contract_sha256=contract_digest,
            recall_allowed=str(recall_allowed).lower(),
        )
        return StructuredRoleToolContract(
            purpose="world_stimulus_appraisal",
            provider_tools=provider_tools,
            provider_tool_choice={
                "type": "function",
                "function": {"name": _WORLD_STIMULUS_TOOL_NAME},
            },
            identity=identity,
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_private_impression_reflection(
        capability_payload_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _PrivateImpressionProposal, _WireRoleResult

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("private impression capability must be one object")
        short_tokens = capability_payload.get("short_tokens")
        anchor_short_tokens = capability_payload.get("anchor_short_tokens")
        existing_impression_short_tokens = capability_payload.get(
            "existing_impression_short_tokens"
        )
        expiry_conditions = capability_payload.get("expiry_conditions")
        if (
            not isinstance(short_tokens, list)
            or not short_tokens
            or any(not isinstance(item, str) or not item for item in short_tokens)
            or len(short_tokens) != len(set(short_tokens))
            or not isinstance(anchor_short_tokens, list)
            or any(
                not isinstance(item, str) or item not in short_tokens
                for item in anchor_short_tokens
            )
            or not isinstance(existing_impression_short_tokens, list)
            or any(
                not isinstance(item, str) or item not in short_tokens
                for item in existing_impression_short_tokens
            )
            or len(existing_impression_short_tokens)
            != len(set(existing_impression_short_tokens))
            or not isinstance(expiry_conditions, list)
            or not expiry_conditions
            or any(not isinstance(item, str) or not item for item in expiry_conditions)
            or len(expiry_conditions) != len(set(expiry_conditions))
        ):
            raise ValueError("private impression capability token/expiry manifest is malformed")

        role_schema = _provider_schema(_WireRoleResult)
        role_properties = _required_object_properties(role_schema)
        proposal_schema = _provider_schema(_PrivateImpressionProposal)
        proposal_properties = _required_object_properties(proposal_schema)
        proposal_properties["proposal_type"] = {
            "type": "string",
            "enum": ["private_impression_transition"],
        }
        for field_name in ("source_refs", "predecessor_refs"):
            field = proposal_properties.get(field_name)
            if not isinstance(field, dict) or not isinstance(field.get("items"), dict):
                raise ValueError(f"private impression {field_name} schema is incomplete")
            allowed_tokens = (
                existing_impression_short_tokens
                if field_name == "predecessor_refs"
                else short_tokens
            )
            field["items"] = {**deepcopy(field["items"]), "enum": list(allowed_tokens)}
        expiry_field = proposal_properties.get("expiry_condition")
        if not isinstance(expiry_field, dict):
            raise ValueError("private impression expiry schema is incomplete")
        proposal_properties["expiry_condition"] = {
            **deepcopy(expiry_field),
            "enum": list(expiry_conditions),
        }
        proposal_schema["required"] = [
            "proposal_type",
            "decision",
            "predecessor_refs",
            "source_refs",
            "reflection_summary",
            "confidence_bp",
            "expiry_condition",
        ]

        common = {
            key: deepcopy(role_properties[key])
            for key in ("summary", "attended_source_refs")
        }
        required = [
            "status",
            "summary",
            "attended_source_refs",
            "decision",
            "recall_query",
            "proposals",
        ]
        transition = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["transition"]},
                **common,
                "decision": {"type": "null"},
                "recall_query": {"type": "null"},
                "proposals": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": proposal_schema,
                },
            },
            "required": required,
            "additionalProperties": False,
        }
        no_change = {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["no_change"]},
                **common,
                "decision": {"type": "null"},
                "recall_query": {"type": "null"},
                "proposals": {"type": "array", "maxItems": 0},
            },
            "required": required,
            "additionalProperties": False,
        }
        branches: list[dict[str, object]] = [no_change, transition]
        if recall_allowed:
            branches.append(
                {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["recall_request"]},
                        **common,
                        "decision": {"type": "null"},
                        "recall_query": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 1024,
                        },
                        "proposals": {"type": "array", "maxItems": 0},
                    },
                    "required": required,
                    "additionalProperties": False,
                }
            )
        parameters = {"type": "object", "anyOf": branches}
        function = {
            "name": _PRIVATE_IMPRESSION_TOOL_NAME,
            "description": (
                "Return the complete source-bound private impression reflection. "
                "The character may form one tentative retain/consolidate/supersede "
                "proposal, make no change, or request one bounded recall. The function "
                "constrains tokens and transport shape only; the character owns the "
                "interpretation and whether to form it."
            ),
            "parameters": parameters,
        }
        provider_tools = ({"type": "function", "function": function},)
        schema_digest = "sha256:" + sha256(_canonical_json(parameters).encode("utf-8")).hexdigest()
        capabilities_digest = (
            "sha256:" + sha256(_canonical_json(capability_payload).encode("utf-8")).hexdigest()
        )
        contract_digest = (
            "sha256:"
            + sha256(
                _canonical_json(
                    {
                        "purpose": "private_impression_reflection",
                        "tool_name": _PRIVATE_IMPRESSION_TOOL_NAME,
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
            purpose="private_impression_reflection",
            tool_name=_PRIVATE_IMPRESSION_TOOL_NAME,
            version=_CONTRACT_VERSION,
            schema_sha256=schema_digest,
            capabilities_sha256=capabilities_digest,
            contract_sha256=contract_digest,
            recall_allowed=str(recall_allowed).lower(),
        )
        return StructuredRoleToolContract(
            purpose="private_impression_reflection",
            provider_tools=provider_tools,
            provider_tool_choice={
                "type": "function",
                "function": {"name": _PRIVATE_IMPRESSION_TOOL_NAME},
            },
            identity=identity,
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_outcome_selection(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _OutcomeSelectionPayload

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("outcome selection capability must be one object")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("outcome selection source refs are malformed")
        offered_tokens = capability_payload.get("offered_tokens")
        if (
            not isinstance(offered_tokens, list)
            or not offered_tokens
            or any(not isinstance(item, str) or not item for item in offered_tokens)
            or len(offered_tokens) != len(set(offered_tokens))
        ):
            raise ValueError("outcome selection offered tokens are malformed")
        allow_direction = capability_payload.get("allow_character_life_direction", False)
        if not isinstance(allow_direction, bool):
            raise ValueError("outcome selection direction capability is malformed")
        payload_schema = _provider_schema(_OutcomeSelectionPayload)
        payload_properties = _required_object_properties(payload_schema)
        selected = payload_properties.get("selected_token")
        if not isinstance(selected, dict):
            raise ValueError("outcome selection token schema is incomplete")
        payload_properties["selected_token"] = {
            **deepcopy(selected),
            "enum": list(offered_tokens),
        }
        if not allow_direction:
            payload_properties["character_life_direction"] = {"type": "null"}
        payload_schema["required"] = ["selected_token", "character_life_direction"]
        return _compile_generic_decision_contract(
            purpose="outcome_selection",
            tool_name=_OUTCOME_SELECTION_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound outcome_selection role result. "
                "Select one offered candidate or request one bounded recall. The "
                "function constrains transport and capability shape only; the "
                "character owns which candidate, if any, matters."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_activity_lifecycle_choice(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _ActivityLifecyclePayload

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("activity lifecycle capability must be one object")
        offered_tokens = capability_payload.get("offered_tokens")
        if (
            not isinstance(offered_tokens, list)
            or not offered_tokens
            or any(not isinstance(item, str) or not item for item in offered_tokens)
            or len(offered_tokens) != len(set(offered_tokens))
        ):
            raise ValueError("activity lifecycle offered tokens are malformed")
        payload_schema = _provider_schema(_ActivityLifecyclePayload)
        properties = _required_object_properties(payload_schema)
        selected = properties.get("selected_token")
        if not isinstance(selected, dict):
            raise ValueError("activity lifecycle selected-token schema is incomplete")
        non_null_selected = _non_null_schema(selected, field_name="selected_token")
        # The installed downstream wire uses the minimal ``{"decision":
        # "no_op"}`` shape.  Keep that exact shape in the provider contract;
        # a JSON null selected_token would be semantically different from the
        # canonical no-op and would be rejected by the materializer.
        payload_schema["required"] = ["decision"]
        payload_schema["anyOf"] = [
            {
                "properties": {
                    "decision": {"enum": ["no_op"]},
                },
                "required": ["decision"],
                "not": {"required": ["selected_token"]},
            },
            {
                "properties": {
                    "decision": {"enum": ["select"]},
                    "selected_token": {
                        **deepcopy(non_null_selected),
                        "enum": list(offered_tokens),
                    },
                },
                "required": ["decision", "selected_token"],
            },
        ]
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("activity lifecycle source refs are malformed")
        return _compile_generic_decision_contract(
            purpose="activity_lifecycle_choice",
            tool_name=_ACTIVITY_LIFECYCLE_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound activity lifecycle choice. The "
                "character may select one offered opening or explicitly choose no_op; "
                "the function constrains capability and transport shape only."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_expression_reconsideration(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import (
            _EXPRESSION_RECONSIDERATION_DISPOSITIONS,
            _ExpressionReconsiderationPayload,
        )

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("expression reconsideration capability must be one object")
        allowed = capability_payload.get("allowed_dispositions")
        if (
            not isinstance(allowed, list)
            or not allowed
            or any(not isinstance(item, str) or not item for item in allowed)
            or len(allowed) != len(set(allowed))
            or not set(allowed) <= _EXPRESSION_RECONSIDERATION_DISPOSITIONS
        ):
            raise ValueError("expression reconsideration dispositions are malformed")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("expression reconsideration source refs are malformed")
        payload_schema = _provider_schema(_ExpressionReconsiderationPayload)
        properties = _required_object_properties(payload_schema)
        disposition = properties.get("disposition")
        if not isinstance(disposition, dict):
            raise ValueError("expression reconsideration disposition schema is incomplete")
        properties["disposition"] = {
            **deepcopy(disposition),
            "enum": list(allowed),
        }
        return _compile_generic_decision_contract(
            purpose="expression_reconsideration",
            tool_name=_EXPRESSION_RECONSIDERATION_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound expression reconsideration choice. "
                "Choose one disposition from the supplied capability; the function "
                "constrains transport shape only and does not choose for the character."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_memory_retention(
        purpose: str,
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _MemoryRetentionPayload

        tool_names = {
            "fact_memory_retention": _FACT_MEMORY_RETENTION_TOOL_NAME,
            "experience_memory_retention": _EXPERIENCE_MEMORY_RETENTION_TOOL_NAME,
        }
        tool_name = tool_names.get(purpose)
        if tool_name is None:
            raise ValueError("memory retention compiler received an unsupported purpose")
        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("memory retention capability must be one object")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("memory retention source refs are malformed")
        payload_schema = _provider_schema(_MemoryRetentionPayload)
        return _compile_generic_decision_contract(
            purpose=purpose,
            tool_name=tool_name,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound memory retention choice. The "
                "character may retain this exact source with personally authored "
                "cue, reasons, and salience, or explicitly choose no_change. The "
                "function constrains transport and typed shape only; it does not "
                "choose for the character."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_memory_withdrawal_review(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from .structured_role import _MemoryWithdrawalReviewPayload

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("memory withdrawal capability must be one object")
        offered = capability_payload.get("offered_tokens")
        if (
            not isinstance(offered, list)
            or not offered
            or any(not isinstance(item, str) or not item for item in offered)
            or len(offered) != len(set(offered))
            or not set(offered) <= {"retain", "forget", "revise"}
        ):
            raise ValueError("memory withdrawal offered dispositions are malformed")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("memory withdrawal source refs are malformed")
        payload_schema = _provider_schema(_MemoryWithdrawalReviewPayload)
        properties = _required_object_properties(payload_schema)
        selected_token = properties.get("selected_token")
        if not isinstance(selected_token, dict):
            raise ValueError("memory withdrawal selected-token schema is incomplete")
        properties["selected_token"] = {
            **deepcopy(selected_token),
            "enum": list(offered),
        }
        return _compile_generic_decision_contract(
            purpose="memory_withdrawal_review",
            tool_name=_MEMORY_WITHDRAWAL_REVIEW_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound memory withdrawal review. Choose "
                "one offered disposition for this exact candidate; the function "
                "constrains transport and candidate shape only and does not choose "
                "for the character."
            ),
        )

    @staticmethod
    @lru_cache(maxsize=32)
    def _cached_life_development_choice(
        capability_payload_json: str,
        source_refs_json: str,
        recall_allowed: bool,
    ) -> StructuredRoleToolContract:
        from ..life_development_draft import (
            CharacterChoiceAcceptDraft,
            CharacterChoiceNoOpDraft,
        )

        capability_payload = json.loads(capability_payload_json)
        if not isinstance(capability_payload, dict):
            raise ValueError("life development choice capability must be one object")
        external_opportunity = capability_payload.get("external_opportunity")
        if not isinstance(external_opportunity, dict):
            raise ValueError("life development choice capability lacks opportunity")
        entity_refs = external_opportunity.get("entity_refs")
        if (
            not isinstance(entity_refs, list)
            or any(not isinstance(item, str) or not item for item in entity_refs)
            or len(entity_refs) != len(set(entity_refs))
        ):
            raise ValueError("life development choice participant capability is malformed")
        active_aspiration_refs = capability_payload.get("active_aspiration_source_refs")
        if (
            not isinstance(active_aspiration_refs, list)
            or any(not isinstance(item, str) or not item for item in active_aspiration_refs)
            or len(active_aspiration_refs) != len(set(active_aspiration_refs))
        ):
            raise ValueError("life development choice aspiration capability is malformed")
        source_refs = json.loads(source_refs_json)
        if not isinstance(source_refs, list):
            raise ValueError("life development choice source refs are malformed")

        no_op_schema = _provider_schema(CharacterChoiceNoOpDraft)
        accept_schema = _provider_schema(CharacterChoiceAcceptDraft)
        accept_properties = _required_object_properties(accept_schema)

        participant_schema = accept_properties.get("participant_refs")
        if not isinstance(participant_schema, dict):
            raise ValueError("life development choice participant schema is incomplete")
        participant_items = participant_schema.get("items")
        if not isinstance(participant_items, dict):
            raise ValueError("life development choice participant items are incomplete")
        accept_properties["participant_refs"] = {
            **deepcopy(participant_schema),
            "maxItems": len(entity_refs) if entity_refs else 0,
            "uniqueItems": True,
            "items": (
                {**deepcopy(participant_items), "enum": list(entity_refs)}
                if entity_refs
                else deepcopy(participant_items)
            ),
        }

        aspiration_schema = accept_properties.get("crystallized_aspiration_source_ref")
        if not isinstance(aspiration_schema, dict):
            raise ValueError("life development choice aspiration schema is incomplete")
        if active_aspiration_refs:
            aspiration_variants = aspiration_schema.get("anyOf")
            if not isinstance(aspiration_variants, list):
                raise ValueError("life development choice aspiration variants are incomplete")
            accept_properties["crystallized_aspiration_source_ref"] = {
                "anyOf": [
                    {
                        **deepcopy(item),
                        "enum": list(active_aspiration_refs),
                    }
                    if isinstance(item, dict) and item.get("type") == "string"
                    else deepcopy(item)
                    for item in aspiration_variants
                ]
            }
        else:
            accept_properties["crystallized_aspiration_source_ref"] = {"type": "null"}

        opens_at = accept_properties.get("opens_at")
        closes_at = accept_properties.get("closes_at")
        if not isinstance(opens_at, dict) or not isinstance(closes_at, dict):
            raise ValueError("life development choice timing schema is incomplete")
        accept_schema["allOf"] = [
            {
                "anyOf": [
                    {
                        "not": {
                            "anyOf": [
                                {"required": ["opens_at"]},
                                {"required": ["closes_at"]},
                            ]
                        }
                    },
                    {
                        "properties": {
                            "opens_at": {"type": "null"},
                            "closes_at": {"type": "null"},
                        },
                        "required": ["opens_at", "closes_at"],
                    },
                    {
                        "properties": {
                            "opens_at": _non_null_schema(opens_at, field_name="opens_at"),
                            "closes_at": _non_null_schema(closes_at, field_name="closes_at"),
                        },
                        "required": ["opens_at", "closes_at"],
                    },
                ]
            }
        ]

        payload_schema = {
            "type": "object",
            "properties": {
                "completion": {
                    "anyOf": [no_op_schema, accept_schema],
                }
            },
            "required": ["completion"],
            "additionalProperties": False,
        }
        return _compile_generic_decision_contract(
            purpose="life_development_choice",
            tool_name=_LIFE_DEVELOPMENT_TOOL_NAME,
            payload_schema=payload_schema,
            capability_identity=capability_payload,
            source_refs=tuple(source_refs),
            recall_allowed=recall_allowed,
            description=(
                "Return the complete source-bound life-development choice. The "
                "character may accept this offered opportunity with a personally "
                "authored intention, timing within the supplied window, and offered "
                "participants, or choose no_op. The function constrains transport and "
                "capability shape only; it does not choose for the character."
            ),
        )


__all__ = [
    "StructuredRoleToolContract",
    "StructuredRoleToolContractIdentity",
    "StructuredRoleToolContracts",
]
