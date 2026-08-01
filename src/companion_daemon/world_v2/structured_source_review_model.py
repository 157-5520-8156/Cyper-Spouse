"""Strict provider wire schemas for World V2 source-authority review calls.

This adapter only translates an explicit ``output_contract.contract`` into a
provider-side JSON Schema.  It never infers a contract from prose and does not
make a source-authority or character-behaviour decision.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Literal
from urllib.parse import urlsplit

import httpx

from companion_daemon.llm import (
    ModelCallUsage,
    OpenAICompatibleChatModel,
    ProviderCapacityGate,
    ProviderCircuitBreaker,
)

from .life_development_draft import (
    LifeDevelopmentNoOpDraft,
    LifeDevelopmentPossibilityDraft,
)
from .source_review_authority import (
    InventoryAvailabilityExhausted,
    SOURCE_REVIEW_CALL_TIMEOUT_SECONDS,
    SourceReviewAuthority,
)
from .life_development_source_closure import (
    LifeDevelopmentNovelOriginReview,
    LifeDevelopmentSourceClosureReview,
)


_SOURCE_FAILURE_CATEGORIES = [
    "undeclared_external_assertion",
    "subject_authority_mismatch",
    "temporal_authority_mismatch",
    "occurrence_or_status_authority_mismatch",
]

_SOURCE_RELATIONS = [
    "unclosed",
    "exact_current_report_discourse_coverage",
    "declared_world_claim_source_mismatch",
]

_SOURCE_FAILURE_DIMENSIONS = [
    "participant_role",
    "logical_modality",
    "polarity",
    "temporal_relation",
    "agent_patient_relation",
    "added_external_premise",
    "habitual_or_generic_scope",
]

_REPORT_RELATIVE_DECISIONS_V2 = [
    "covered_by_exact_current_report",
    "covered_by_first_person_immediate_private_continuity",
    "not_external_proposition",
    "retain_unclosed",
]

_REPORT_RELATIVE_DECISIONS_V3 = [
    "covered_by_exact_current_report",
    "covered_by_exact_dialogue_record",
    "covered_by_first_person_immediate_private_continuity",
    "not_external_proposition",
    "retain_unclosed",
]

_REPORT_RELATIVE_DECISIONS_V1 = [
    "covered_by_exact_current_report",
    "retain_unclosed",
]

_CANDIDATE_COVERAGE_DECISIONS = [
    "closed",
    "unclosed",
    "not_external_proposition",
]

_CANDIDATE_COVERAGE_SOURCE_RELATIONS = [
    "unclosed",
    "not_external_proposition",
    "exact_current_report_discourse_coverage",
    "exact_dialogue_record_coverage",
    "first_person_immediate_private_continuity",
    "declared_world_claim_source_coverage",
]


@dataclass(frozen=True, slots=True)
class StrictOutputCapabilityEvidence:
    """Deployment evidence that one endpoint can enforce named wire contracts.

    Installing a JSON Schema in ``response_format`` proves only that this
    adapter knows how to construct the request.  Routed providers may have no
    endpoint that accepts that parameter for a particular model.  This value
    keeps transport capability separate from adapter capability so production
    composition can fail closed without making a model call.
    """

    status: Literal["verified", "unverified", "disabled"]
    evidence_source: str
    reason_code: str
    provider: str
    model: str
    contracts: tuple[str, ...] = ()
    observed_at: str | None = None
    evidence_revision: str | None = None
    audit_sample_count: int | None = None
    audit_success_count: int | None = None
    contract_schema_digests: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_source.strip():
            raise ValueError("strict output evidence source must be non-empty")
        if not self.reason_code.strip():
            raise ValueError("strict output evidence reason must be non-empty")
        if not self.provider.strip():
            raise ValueError("strict output evidence provider must be non-empty")
        if not self.model.strip():
            raise ValueError("strict output evidence model must be non-empty")
        normalized_contracts = tuple(
            dict.fromkeys(str(contract).strip() for contract in self.contracts if str(contract).strip())
        )
        if self.status == "verified" and not normalized_contracts:
            raise ValueError("verified strict output evidence requires contracts")
        if self.status != "verified" and normalized_contracts:
            raise ValueError("unverified strict output evidence cannot grant contracts")
        if self.audit_sample_count is not None and self.audit_sample_count < 1:
            raise ValueError("strict output audit sample count must be positive")
        if self.audit_success_count is not None and (
            self.audit_sample_count is None
            or self.audit_success_count < 0
            or self.audit_success_count > self.audit_sample_count
        ):
            raise ValueError("strict output audit success count is invalid")
        digest_contracts = tuple(contract for contract, _digest in self.contract_schema_digests)
        if self.status == "verified" and digest_contracts != normalized_contracts:
            raise ValueError("verified strict output evidence requires every schema digest")
        if self.status != "verified" and self.contract_schema_digests:
            raise ValueError("unverified strict output evidence cannot carry schema digests")
        object.__setattr__(self, "contracts", normalized_contracts)

    @classmethod
    def verified(
        cls,
        *,
        evidence_source: str,
        provider: str,
        model: str,
        contracts: tuple[str, ...],
        observed_at: str | None,
        evidence_revision: str | None = None,
        audit_sample_count: int | None = None,
        audit_success_count: int | None = None,
        contract_schema_digests: tuple[tuple[str, str], ...] | None = None,
    ) -> "StrictOutputCapabilityEvidence":
        digests = contract_schema_digests or tuple(
            (contract, _strict_contract_schema_digest(contract))
            for contract in contracts
        )
        return cls(
            status="verified",
            evidence_source=evidence_source,
            reason_code="strict_output.endpoint_capability_verified",
            provider=provider,
            model=model,
            contracts=contracts,
            observed_at=observed_at,
            evidence_revision=evidence_revision,
            audit_sample_count=audit_sample_count,
            audit_success_count=audit_success_count,
            contract_schema_digests=digests,
        )

    @classmethod
    def unverified(
        cls,
        *,
        evidence_source: str,
        reason_code: str,
        provider: str,
        model: str,
        observed_at: str | None = None,
    ) -> "StrictOutputCapabilityEvidence":
        return cls(
            status="unverified",
            evidence_source=evidence_source,
            reason_code=reason_code,
            provider=provider,
            model=model,
            observed_at=observed_at,
        )

    def supports(self, contract: str) -> bool:
        return self.status == "verified" and contract in self.contracts

    def health_snapshot(self) -> dict[str, object]:
        return {
            "status": self.status,
            "evidence_source": self.evidence_source,
            "reason_code": self.reason_code,
            "provider": self.provider,
            "model": self.model,
            "contracts": self.contracts,
            "observed_at": self.observed_at,
            "qualified_at": (
                self.observed_at if self.status == "verified" else None
            ),
            "evidence_revision": self.evidence_revision,
            "audit_sample_count": self.audit_sample_count,
            "audit_success_count": self.audit_success_count,
            "contract_schema_digests": dict(self.contract_schema_digests),
        }


def openrouter_inventory_capability_evidence(
    *,
    enabled: bool,
    base_url: str,
    model: str,
) -> StrictOutputCapabilityEvidence:
    """Resolve release-pinned evidence for one OpenRouter Inventory route.

    OpenRouter structured-output support is endpoint-specific.  Unknown routes
    deliberately remain unverified; adding a model here requires both provider
    capability evidence and a multi-turn production-contract audit.  A single
    syntactically valid response is not sufficient.
    """

    if not enabled:
        return StrictOutputCapabilityEvidence(
            status="disabled",
            evidence_source="configuration",
            reason_code="source_inventory.disabled_by_configuration",
            provider="openrouter",
            model=model,
        )
    parsed = urlsplit(base_url.rstrip("/"))
    official_endpoint = (
        parsed.scheme.casefold() == "https"
        and parsed.netloc.casefold() == "openrouter.ai"
        and parsed.path.rstrip("/") == "/api/v1"
    )
    if not official_endpoint:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="configuration",
            reason_code="source_inventory.endpoint_capability_unverified",
            provider="openrouter",
            model=model,
        )
    normalized_model = model.strip().casefold()
    if normalized_model == "nousresearch/hermes-4-70b":
        # OpenRouter's model metadata did not advertise ``structured_outputs``
        # for this checkpoint on 2026-08-01, and require_parameters consequently
        # returned no compatible endpoint in a production-contract probe.
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="openrouter_model_metadata",
            reason_code="source_inventory.structured_outputs_not_advertised",
            provider="openrouter",
            model=model,
            observed_at="2026-08-01",
        )
    if normalized_model == "inclusionai/ling-2.6-flash":
        # The route accepts JSON Schema, but a six-turn exact-contract audit
        # yielded zero accepted turns (invalid locators followed by technical
        # failures). Transport syntax alone is insufficient authority evidence.
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="production_contract_audit",
            reason_code="source_inventory.contract_reliability_unverified",
            provider="openrouter",
            model=model,
            observed_at="2026-08-01",
        )
    if normalized_model == "openai/gpt-5.4-nano":
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="production_contract_audit",
            provider="openrouter",
            model=model,
            contracts=("candidate-external-proposition-inventory.5",),
            observed_at="2026-08-01",
            evidence_revision="inventory-v5-openrouter-gpt54nano-20260801.1",
            audit_sample_count=14,
            audit_success_count=13,
            contract_schema_digests=(
                (
                    "candidate-external-proposition-inventory.5",
                    "cd55ce09687b5b4e68b1a6805244f76e9c43d4e286b3bee5bb183715a38519fb",
                ),
            ),
        )
    if normalized_model == "openai/gpt-5.4-mini":
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="production_contract_audit",
            provider="openrouter",
            model=model,
            contracts=("candidate-external-proposition-inventory.5",),
            observed_at="2026-08-01",
            evidence_revision="inventory-v5-openrouter-gpt54mini-20260801.1",
            audit_sample_count=9,
            audit_success_count=9,
            contract_schema_digests=(
                (
                    "candidate-external-proposition-inventory.5",
                    "cd55ce09687b5b4e68b1a6805244f76e9c43d4e286b3bee5bb183715a38519fb",
                ),
            ),
        )
    return StrictOutputCapabilityEvidence.unverified(
        evidence_source="release_registry",
        reason_code="source_inventory.strict_output_capability_unverified",
        provider="openrouter",
        model=model,
    )


def direct_openai_model_id(model: str) -> str:
    """Translate OpenRouter's official-OpenAI name to the direct API model id.

    The deployment setting predates the cross-provider reserve lane and may
    therefore contain ``openai/<checkpoint>``.  Sending that routed spelling to
    ``api.openai.com`` produces a provider-side unknown-model failure.  Only
    the explicit OpenAI namespace is stripped; other provider namespaces stay
    visible and consequently remain unqualified below.
    """

    normalized = model.strip()
    if normalized.casefold().startswith("openai/"):
        return normalized.split("/", 1)[1]
    return normalized


def openai_inventory_capability_evidence(
    *,
    enabled: bool,
    base_url: str,
    model: str,
) -> StrictOutputCapabilityEvidence:
    """Resolve exact Inventory V5 capability for the official OpenAI route.

    An exact request reaching a real endpoint is not response evidence.  Only
    release-pinned routes with returned strict wires from the production
    prompt/schema/parser audit are qualified here.
    """

    if not enabled:
        return StrictOutputCapabilityEvidence(
            status="disabled",
            evidence_source="configuration",
            reason_code="source_inventory.disabled_by_configuration",
            provider="openai",
            model=model,
        )
    parsed = urlsplit(base_url.rstrip("/"))
    official_endpoint = (
        parsed.scheme.casefold() == "https"
        and parsed.netloc.casefold() == "api.openai.com"
        and parsed.path.rstrip("/") == "/v1"
    )
    if not official_endpoint:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="configuration",
            reason_code="source_inventory.endpoint_capability_unverified",
            provider="openai",
            model=model,
        )
    if model.strip().casefold() == "gpt-5.4-mini":
        # Exact production prompt/schema/parser audit: 10/10 semantic boundary
        # cases passed (recent first-person life, user facts, immediate private
        # state, generalization, and current-report uptake).  One earlier
        # deployed-parameter probe returned an invalid strict wire, so the
        # release record deliberately retains the honest 11/12 route count.
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="production_contract_audit",
            provider="openai",
            model=model,
            contracts=("candidate-external-proposition-inventory.5",),
            observed_at="2026-08-01",
            evidence_revision="inventory-v5-openai-gpt54mini-20260801.2",
            audit_sample_count=12,
            audit_success_count=11,
            contract_schema_digests=(
                (
                    "candidate-external-proposition-inventory.5",
                    "cd55ce09687b5b4e68b1a6805244f76e9c43d4e286b3bee5bb183715a38519fb",
                ),
            ),
        )
    return StrictOutputCapabilityEvidence.unverified(
        evidence_source="release_registry",
        reason_code="source_inventory.strict_output_capability_unverified",
        provider="openai",
        model=model,
    )


_ACTIVE_SOURCE_REVIEW_STRICT_CONTRACTS = (
    "source-closure-review.7",
    "report-relative-entailment-adjudication.3",
)


def audited_source_review_capability_evidence(
    *,
    base_url: str,
    model: str,
    provider: Literal["openai", "openrouter"],
) -> StrictOutputCapabilityEvidence:
    """Return audited strict-wire evidence for an exact active review route.

    An OpenAI-compatible client is only a request encoder.  It does not prove
    that an arbitrary endpoint/checkpoint enforces any installed schema.  This
    registry therefore binds capability to the host, exact model id, contract
    names, schema digests, and a retained multi-scenario production-contract
    audit. Only the currently active full-review and report-relative contracts
    are qualified; dormant V8/Coverage remain unavailable until their own
    audits pass. Unknown routes stay explicitly unverified.
    """

    parsed = urlsplit(base_url.rstrip("/"))
    normalized_model = model.strip().casefold()
    if provider == "openai":
        endpoint_matches = (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == "api.openai.com"
            and parsed.path.rstrip("/") == "/v1"
        )
        qualified_model = "gpt-4.1-mini"
        evidence_revision = (
            "source-review-openai-gpt-4.1-mini-20260801.active-v7-rra3.1"
        )
        audit_sample_count = 16
        audit_success_count = 13
    else:
        endpoint_matches = (
            parsed.scheme.casefold() == "https"
            and parsed.netloc.casefold() == "openrouter.ai"
            and parsed.path.rstrip("/") == "/api/v1"
        )
        qualified_model = "qwen/qwen-plus"
        evidence_revision = (
            "source-review-openrouter-qwen-qwen-plus-20260801.active-v7-rra3.2"
        )
        audit_sample_count = 13
        audit_success_count = 13
    if not endpoint_matches:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="configuration",
            reason_code="source_review.endpoint_capability_unverified",
            provider=provider,
            model=model,
        )
    if normalized_model != qualified_model:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="release_registry",
            reason_code="source_review.strict_output_capability_unverified",
            provider=provider,
            model=model,
        )
    return StrictOutputCapabilityEvidence.verified(
        evidence_source="production_contract_audit",
        provider=provider,
        model=model,
        contracts=_ACTIVE_SOURCE_REVIEW_STRICT_CONTRACTS,
        observed_at="2026-08-01",
        evidence_revision=evidence_revision,
        audit_sample_count=audit_sample_count,
        audit_success_count=audit_success_count,
        contract_schema_digests=tuple(
            (contract, _strict_contract_schema_digest(contract))
            for contract in _ACTIVE_SOURCE_REVIEW_STRICT_CONTRACTS
        ),
    )


def _failure_category_array_schema(*, maximum: int) -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "string",
            "enum": _SOURCE_FAILURE_CATEGORIES,
        },
        "maxItems": maximum,
    }


_SOURCE_CLOSURE_REVIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ci": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": 8,
        },
        "v": _failure_category_array_schema(maximum=4),
        # Reserved legacy field: the current review contract requires it to
        # be returned explicitly but never populated.
        "p": _failure_category_array_schema(maximum=0),
        "visible_findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": _SOURCE_FAILURE_CATEGORIES,
                    },
                    "visible_span": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1_024,
                    },
                    "claim_index": {
                        "anyOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "null"},
                        ]
                    },
                    "source_relation": {
                        "type": "string",
                        "enum": _SOURCE_RELATIONS,
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "maxItems": 8,
                    },
                },
                "required": [
                    "category",
                    "visible_span",
                    "claim_index",
                    "source_relation",
                    "source_refs",
                ],
                "additionalProperties": False,
            },
            "maxItems": 16,
        },
        "r": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
        },
    },
    "required": ["ci", "v", "p", "visible_findings", "r"],
    "additionalProperties": False,
}

_DECLARED_WORLD_CLAIM_REVIEW_SCHEMA = deepcopy(_SOURCE_CLOSURE_REVIEW_SCHEMA)
_DECLARED_WORLD_CLAIM_REVIEW_SCHEMA["properties"]["v"]["maxItems"] = 0
_DECLARED_WORLD_CLAIM_REVIEW_SCHEMA["properties"]["visible_findings"]["maxItems"] = 0


def _report_relative_schema(
    *,
    contract: str,
    decisions: list[str],
    include_failure_dimensions: bool,
    include_source_refs: bool,
) -> dict[str, object]:
    finding_properties: dict[str, object] = {
        "finding_index": {"type": "integer", "minimum": 0},
        "decision": {
            "type": "string",
            "enum": decisions,
        },
    }
    if include_failure_dimensions:
        finding_properties["failure_dimensions"] = {
            "type": "array",
            "items": {
                "type": "string",
                "enum": _SOURCE_FAILURE_DIMENSIONS,
            },
            "maxItems": 7,
        }
    if include_source_refs:
        finding_properties["source_refs"] = {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "maxItems": 8,
        }
    return {
        "type": "object",
        "properties": {
            "contract": {
                "type": "string",
                "enum": [contract],
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": finding_properties,
                    "required": list(finding_properties),
                    "additionalProperties": False,
                },
                "minItems": 1,
                "maxItems": 16,
            },
            "r": {
                "type": "string",
                "minLength": 1,
                "maxLength": 240,
            },
        },
        "required": ["contract", "findings", "r"],
        "additionalProperties": False,
    }


_REPORT_RELATIVE_SCHEMA_V2 = _report_relative_schema(
    contract="report-relative-entailment-adjudication.2",
    decisions=_REPORT_RELATIVE_DECISIONS_V2,
    include_failure_dimensions=True,
    include_source_refs=False,
)

_REPORT_RELATIVE_SCHEMA_V1 = _report_relative_schema(
    contract="report-relative-entailment-adjudication.1",
    decisions=_REPORT_RELATIVE_DECISIONS_V1,
    include_failure_dimensions=False,
    include_source_refs=False,
)

_REPORT_RELATIVE_SCHEMA_V3 = _report_relative_schema(
    contract="report-relative-entailment-adjudication.3",
    decisions=_REPORT_RELATIVE_DECISIONS_V3,
    include_failure_dimensions=True,
    include_source_refs=True,
)

_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "string",
            "enum": ["candidate-external-proposition-inventory.3"],
        },
        "propositions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "locator": {
                        "type": "object",
                        "properties": {
                            "beat_index": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 15,
                            },
                            # Offset exactness remains a deterministic parser
                            # boundary. Deliberately mirror the untrusted wire
                            # here so unique-substring mechanical recovery
                            # remains possible without semantic guessing.
                            "char_start": {"type": "integer"},
                            "char_end": {"type": "integer"},
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1_024,
                            },
                        },
                        "required": [
                            "beat_index",
                            "char_start",
                            "char_end",
                            "text",
                        ],
                        "additionalProperties": False,
                    },
                    "semantic_role": {
                        "type": "string",
                        "enum": [
                            "outer_private_state",
                            "embedded_external_proposition",
                            "standalone_external_proposition",
                            "nonassertive_content",
                        ],
                    },
                    "parent_index": {
                        "anyOf": [
                            {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 31,
                            },
                            {"type": "null"},
                        ]
                    },
                },
                "required": ["locator", "semantic_role", "parent_index"],
                "additionalProperties": False,
            },
            "maxItems": 32,
        },
    },
    "required": ["contract", "propositions"],
    "additionalProperties": False,
}

_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V4_SCHEMA = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_SCHEMA
)
_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V4_SCHEMA["properties"]["contract"]["enum"] = [
    "candidate-external-proposition-inventory.4"
]
_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V4_SCHEMA["properties"]["propositions"]["items"][
    "properties"
]["semantic_role"]["enum"] = [
    "immediate_private_state",
    "source_bearing_private_episode",
    "embedded_external_proposition",
    "standalone_external_proposition",
    "nonassertive_content",
]

_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V5_SCHEMA = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V4_SCHEMA
)
_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V5_SCHEMA["properties"]["contract"]["enum"] = [
    "candidate-external-proposition-inventory.5"
]
_CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V5_SCHEMA["properties"]["propositions"]["items"][
    "properties"
]["semantic_role"]["enum"] = [
    "immediate_private_state",
    "source_bearing_private_episode",
    "embedded_external_proposition",
    "standalone_external_proposition",
    "world_unbound_generalization",
    "nonassertive_content",
]
_candidate_inventory_v5_item = _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V5_SCHEMA["properties"][
    "propositions"
]["items"]
_candidate_inventory_v5_item["properties"].pop("parent_index")
_candidate_inventory_v5_item["required"] = ["locator", "semantic_role"]

_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "string",
            "enum": ["candidate-external-proposition-coverage.1"],
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "locator": {
                        "type": "object",
                        "properties": {
                            "beat_index": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 15,
                            },
                            "char_start": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 4_096,
                            },
                            "char_end": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 4_096,
                            },
                            "text": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 1_024,
                            },
                        },
                        "required": [
                            "beat_index",
                            "char_start",
                            "char_end",
                            "text",
                        ],
                        "additionalProperties": False,
                    },
                    "decision": {
                        "type": "string",
                        "enum": _CANDIDATE_COVERAGE_DECISIONS,
                    },
                    "source_relation": {
                        "type": "string",
                        "enum": _CANDIDATE_COVERAGE_SOURCE_RELATIONS,
                    },
                    "source_refs": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "minLength": 1,
                        },
                        "maxItems": 8,
                    },
                },
                "required": [
                    "locator",
                    "decision",
                    "source_relation",
                    "source_refs",
                ],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 16,
        },
    },
    "required": ["contract", "findings"],
    "additionalProperties": False,
}

_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V2_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "string",
            "enum": ["candidate-external-proposition-coverage.2"],
        },
        "inventory_complete": {"type": "boolean"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "locator_index": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 31,
                    },
                    "decision": {
                        "type": "string",
                        "enum": _CANDIDATE_COVERAGE_DECISIONS,
                    },
                    "source_relation": {
                        "type": "string",
                        "enum": _CANDIDATE_COVERAGE_SOURCE_RELATIONS,
                    },
                    "source_ref_indexes": {
                        "type": "array",
                        "items": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 255,
                        },
                        "maxItems": 8,
                    },
                },
                "required": [
                    "locator_index",
                    "decision",
                    "source_relation",
                    "source_ref_indexes",
                ],
                "additionalProperties": False,
            },
            "maxItems": 32,
        },
    },
    "required": ["contract", "inventory_complete", "findings"],
    "additionalProperties": False,
}

_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V2_SCHEMA
)
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA["properties"]["contract"]["enum"] = [
    "candidate-external-proposition-coverage.3"
]
_candidate_coverage_v3_base_item = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA["properties"]["findings"]["items"]
)


def _candidate_coverage_v3_finding_variant(
    *,
    decision: str,
    source_relation: str,
    source_ref_indexes_minimum: int | None = None,
    source_ref_indexes_maximum: int = 8,
) -> dict[str, object]:
    """Build one provider-enforced, mechanically coherent verdict branch."""

    variant = deepcopy(_candidate_coverage_v3_base_item)
    properties = variant["properties"]
    properties["decision"]["enum"] = [decision]
    properties["source_relation"]["enum"] = [source_relation]
    source_ref_indexes = properties["source_ref_indexes"]
    source_ref_indexes["maxItems"] = source_ref_indexes_maximum
    if source_ref_indexes_minimum is not None:
        source_ref_indexes["minItems"] = source_ref_indexes_minimum
    return variant


_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA["properties"]["findings"]["items"] = {
    "anyOf": [
        _candidate_coverage_v3_finding_variant(
            decision="not_external_proposition",
            source_relation="not_external_proposition",
            source_ref_indexes_maximum=0,
        ),
        _candidate_coverage_v3_finding_variant(
            decision="unclosed",
            source_relation="unclosed",
            source_ref_indexes_maximum=0,
        ),
        _candidate_coverage_v3_finding_variant(
            decision="closed",
            source_relation="first_person_immediate_private_continuity",
            source_ref_indexes_maximum=0,
        ),
        *[
            _candidate_coverage_v3_finding_variant(
                decision="closed",
                source_relation=source_relation,
                source_ref_indexes_minimum=1,
            )
            for source_relation in (
                "exact_current_report_discourse_coverage",
                "exact_dialogue_record_coverage",
                "declared_world_claim_source_coverage",
                "pinned_context_authority_coverage",
            )
        ],
    ]
}

_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V4_SCHEMA = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA
)
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V4_SCHEMA["properties"]["contract"]["enum"] = [
    "candidate-external-proposition-coverage.4"
]
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V4_SCHEMA["properties"]["missing_findings"] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "locator": deepcopy(_candidate_inventory_v5_item["properties"]["locator"]),
            "semantic_role": {
                "type": "string",
                "enum": [
                    "source_bearing_private_episode",
                    "embedded_external_proposition",
                    "standalone_external_proposition",
                ],
            },
        },
        "required": ["locator", "semantic_role"],
        "additionalProperties": False,
    },
    "maxItems": 16,
}
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V4_SCHEMA["required"] = [
    "contract",
    "inventory_complete",
    "findings",
    "missing_findings",
]

# Inventory V5 is the sole authority for exhaustive proposition discovery.
# Coverage V5 receives those host-bound locator indexes and returns verdicts
# only; it cannot claim inventory completeness or discover new spans.
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V5_SCHEMA = deepcopy(
    _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA
)
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V5_SCHEMA["properties"]["contract"]["enum"] = [
    "candidate-external-proposition-coverage.5"
]
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V5_SCHEMA["properties"].pop("inventory_complete")
_CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V5_SCHEMA["required"] = [
    "contract",
    "findings",
]

_CANDIDATE_EPISTEMIC_ROLE_CONFLICT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "contract": {
            "type": "string",
            "enum": ["candidate-epistemic-role-conflict.1"],
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "locator_index": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 31,
                    },
                    "decision": {
                        "type": "string",
                        "enum": [
                            "reclassify_immediate",
                            "reclassify_nonassertive",
                            "requires_source",
                            "uncertain",
                        ],
                    },
                },
                "required": ["locator_index", "decision"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 32,
        },
    },
    "required": ["contract", "findings"],
    "additionalProperties": False,
}

_SOURCE_CLOSURE_APPEAL_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "ci": {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "maxItems": 8,
        },
        "v": _failure_category_array_schema(maximum=4),
        "p": _failure_category_array_schema(maximum=4),
        "r": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
        },
    },
    "required": ["ci", "v", "p", "r"],
    "additionalProperties": False,
}


def _strict_provider_schema(model_type: object) -> dict[str, object]:
    """Translate one authoritative Pydantic wire into provider strict form.

    Provider strict schemas require every object property to be explicit.
    Supplying nulls and empty arrays for Pydantic-defaulted fields is already
    accepted by the parser and changes no semantic authority.
    """

    schema_builder = getattr(model_type, "model_json_schema")
    schema = schema_builder(mode="validation")

    def convert(value: object) -> object:
        if isinstance(value, list):
            return [convert(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted: dict[str, object] = {}
        for key, child in value.items():
            if key in {"properties", "$defs"} and isinstance(child, dict):
                # These are name maps, not schema-annotation objects. A real
                # wire field may itself be named `description` or `title`.
                converted[key] = {
                    name: convert(field_schema)
                    for name, field_schema in child.items()
                }
                continue
            if key in {"default", "description", "title"}:
                continue
            if key == "const":
                converted["enum"] = [convert(child)]
                continue
            converted[key] = convert(child)
        if converted.get("type") == "object":
            properties = converted.get("properties")
            if isinstance(properties, dict):
                converted["required"] = list(properties)
                converted["additionalProperties"] = False
        return converted

    converted = convert(schema)
    if not isinstance(converted, dict):
        raise TypeError("Pydantic wire schema must be an object")
    return converted


def _strict_decision_coordinate_envelope_schema(
    model_type: object,
    *,
    coordinate_fields: tuple[str, ...],
) -> dict[str, object]:
    """Make a model validator's verdict/coordinate invariant transport-visible.

    Pydantic's generated schema cannot express an ``after`` model validator.
    Keeping the flat schema here would therefore let a strict provider return
    ``supported`` together with rejection coordinates, only for the runtime
    parser to reject those provider-valid bytes.  The provider dialect forbids
    a root ``anyOf``, so carry the semantic union one level below a transport-
    only ``review`` envelope.  Life's parser accepts that envelope while still
    decoding historical flat bytes.
    """

    flat = _strict_provider_schema(model_type)
    definitions = flat.pop("$defs", None)

    supported = deepcopy(flat)
    supported_properties = supported["properties"]
    supported_properties["decision"]["enum"] = ["supported"]
    for field in coordinate_fields:
        supported_properties[field]["maxItems"] = 0

    unsupported_variants: list[dict[str, object]] = []
    for required_coordinate in coordinate_fields:
        variant = deepcopy(flat)
        variant_properties = variant["properties"]
        variant_properties["decision"]["enum"] = ["unsupported"]
        variant_properties[required_coordinate]["minItems"] = 1
        unsupported_variants.append(variant)

    envelope: dict[str, object] = {
        "type": "object",
        "properties": {
            "review": {
                "anyOf": [supported, *unsupported_variants],
            }
        },
        "required": ["review"],
        "additionalProperties": False,
    }
    if isinstance(definitions, dict):
        envelope["$defs"] = definitions
    return envelope


_LIFE_SOURCE_CLOSURE_REVIEW_SCHEMA = _strict_decision_coordinate_envelope_schema(
    LifeDevelopmentSourceClosureReview,
    coordinate_fields=(
        "unsupported_claim_ids",
        "undeclared_fact_fragments",
        "undeclared_fact_paths",
        "typed_location_conflicts",
    ),
)
_LIFE_NOVEL_ORIGIN_REVIEW_SCHEMA = _strict_decision_coordinate_envelope_schema(
    LifeDevelopmentNovelOriginReview,
    coordinate_fields=(
        "unsupported_claims",
        "unsupported_provisional_npcs",
        "unsupported_outcome_prerequisites",
    ),
)
_WORLD_AUTHOR_NO_OP_SCHEMA = _strict_provider_schema(LifeDevelopmentNoOpDraft)
_WORLD_AUTHOR_PROPOSAL_SCHEMA = _strict_provider_schema(LifeDevelopmentPossibilityDraft)
_WORLD_AUTHOR_PROPOSAL_DEFS = _WORLD_AUTHOR_PROPOSAL_SCHEMA.pop("$defs", {})
_WORLD_AUTHOR_SOURCE_REWRITE_SCHEMA: dict[str, object] = {
    "type": "object",
    "$defs": _WORLD_AUTHOR_PROPOSAL_DEFS,
    # OpenAI's strict structured-output dialect requires the root itself to
    # be a plain object (no root-level anyOf).  Keep the semantic union intact
    # one level below it; the Life parser removes only this transport envelope
    # while retaining the provider's exact bytes in the immutable audit.
    "properties": {
        "replacement": {
            "anyOf": [
                _WORLD_AUTHOR_NO_OP_SCHEMA,
                _WORLD_AUTHOR_PROPOSAL_SCHEMA,
            ],
        },
    },
    "required": ["replacement"],
    "additionalProperties": False,
}
_WORLD_AUTHOR_SOURCE_REWRITE_PROPOSE_REPAIR_SCHEMA: dict[str, object] = {
    "type": "object",
    "$defs": deepcopy(_WORLD_AUTHOR_PROPOSAL_DEFS),
    "properties": {
        "replacement": deepcopy(_WORLD_AUTHOR_PROPOSAL_SCHEMA),
    },
    "required": ["replacement"],
    "additionalProperties": False,
}

_STRICT_SCHEMAS: dict[str, tuple[str, dict[str, object]]] = {
    "source-closure-review.7": (
        "source_closure_review_v7",
        _SOURCE_CLOSURE_REVIEW_SCHEMA,
    ),
    "source-closure-review.8": (
        "source_closure_review_v8",
        _DECLARED_WORLD_CLAIM_REVIEW_SCHEMA,
    ),
    "report-relative-entailment-adjudication.3": (
        "report_relative_entailment_adjudication_v3",
        _REPORT_RELATIVE_SCHEMA_V3,
    ),
    "report-relative-entailment-adjudication.2": (
        "report_relative_entailment_adjudication_v2",
        _REPORT_RELATIVE_SCHEMA_V2,
    ),
    "report-relative-entailment-adjudication.1": (
        "report_relative_entailment_adjudication_v1",
        _REPORT_RELATIVE_SCHEMA_V1,
    ),
    "candidate-external-proposition-coverage.1": (
        "candidate_external_proposition_coverage_v1",
        _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_SCHEMA,
    ),
    "candidate-external-proposition-coverage.2": (
        "candidate_external_proposition_coverage_v2",
        _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V2_SCHEMA,
    ),
    "candidate-external-proposition-coverage.3": (
        "candidate_external_proposition_coverage_v3",
        _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V3_SCHEMA,
    ),
    "candidate-external-proposition-coverage.4": (
        "candidate_external_proposition_coverage_v4",
        _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V4_SCHEMA,
    ),
    "candidate-external-proposition-coverage.5": (
        "candidate_external_proposition_coverage_v5",
        _CANDIDATE_EXTERNAL_PROPOSITION_COVERAGE_V5_SCHEMA,
    ),
    "candidate-epistemic-role-conflict.1": (
        "candidate_epistemic_role_conflict_v1",
        _CANDIDATE_EPISTEMIC_ROLE_CONFLICT_SCHEMA,
    ),
    "candidate-external-proposition-inventory.3": (
        "candidate_external_proposition_inventory_v3",
        _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_SCHEMA,
    ),
    "candidate-external-proposition-inventory.4": (
        "candidate_external_proposition_inventory_v4",
        _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V4_SCHEMA,
    ),
    "candidate-external-proposition-inventory.5": (
        "candidate_external_proposition_inventory_v5",
        _CANDIDATE_EXTERNAL_PROPOSITION_INVENTORY_V5_SCHEMA,
    ),
    "source-closure-appeal.4": (
        "source_closure_appeal_v4",
        _SOURCE_CLOSURE_APPEAL_SCHEMA,
    ),
    "life-development-source-closure-review.1": (
        "life_development_source_closure_review_v1",
        _LIFE_SOURCE_CLOSURE_REVIEW_SCHEMA,
    ),
    "life-development-novel-origin-review.1": (
        "life_development_novel_origin_review_v1",
        _LIFE_NOVEL_ORIGIN_REVIEW_SCHEMA,
    ),
    "life-development-novel-origin-review.2": (
        "life_development_novel_origin_review_v2",
        _LIFE_NOVEL_ORIGIN_REVIEW_SCHEMA,
    ),
    "world-author-source-rewrite.1": (
        "world_author_source_rewrite_v1",
        _WORLD_AUTHOR_SOURCE_REWRITE_SCHEMA,
    ),
    "world-author-source-rewrite-propose-repair.1": (
        "world_author_source_rewrite_propose_repair_v1",
        _WORLD_AUTHOR_SOURCE_REWRITE_PROPOSE_REPAIR_SCHEMA,
    ),
}


def _strict_contract_schema_digest(contract: str) -> str:
    selected = _STRICT_SCHEMAS.get(contract)
    if selected is None:
        raise ValueError(f"unknown strict output contract: {contract}")
    _name, schema = selected
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _explicit_output_contract(messages: list[dict[str, str]]) -> str | None:
    """Return only a contract explicitly carried by one JSON request message."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        output_contract = value.get("output_contract")
        if isinstance(output_contract, dict):
            contract = output_contract.get("contract")
            if isinstance(contract, str):
                return contract
        review_contract = value.get("review_contract")
        if isinstance(review_contract, str):
            return review_contract
    return None


class StructuredSourceReviewModel(OpenAICompatibleChatModel):
    """OpenAI-compatible reviewer with strict schemas for known source wires."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        require_provider_parameters: bool = False,
        reasoning_effort: str = "none",
        max_completion_tokens: int = 900,
        proxy_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        usage_observer: Callable[[ModelCallUsage], None] | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        capacity_gate: ProviderCapacityGate | None = None,
        client: httpx.AsyncClient | None = None,
        inventory_call_timeout_seconds: float | None = None,
        strict_output_capability_evidence: StrictOutputCapabilityEvidence | None = None,
    ) -> None:
        if inventory_call_timeout_seconds is not None and (
            not math.isfinite(inventory_call_timeout_seconds)
            or inventory_call_timeout_seconds <= 0
        ):
            raise ValueError("inventory call timeout must be finite and positive")
        super().__init__(
            api_key,
            base_url,
            model,
            reasoning_effort=reasoning_effort,
            max_completion_tokens=max_completion_tokens,
            proxy_url=proxy_url,
            transport=transport,
            usage_observer=usage_observer,
            circuit_breaker=circuit_breaker,
            capacity_gate=capacity_gate,
            client=client,
        )
        self.require_provider_parameters = require_provider_parameters
        self.inventory_call_timeout_seconds = inventory_call_timeout_seconds
        if require_provider_parameters:
            self.provider = "openrouter"
        if strict_output_capability_evidence is None:
            strict_output_capability_evidence = StrictOutputCapabilityEvidence.unverified(
                evidence_source="none",
                reason_code="strict_output.endpoint_capability_unverified",
                provider=self.provider,
                model=self.model,
            )
        if (
            strict_output_capability_evidence.provider.casefold()
            != self.provider.casefold()
        ):
            raise ValueError("strict output evidence provider must match model provider")
        if strict_output_capability_evidence.model.casefold() != self.model.casefold():
            raise ValueError("strict output evidence model must match configured model")
        for contract, recorded_digest in (
            strict_output_capability_evidence.contract_schema_digests
        ):
            if _strict_contract_schema_digest(contract) != recorded_digest:
                raise ValueError(
                    "strict output evidence schema digest does not match the installed contract"
                )
        self.strict_output_capability_evidence = strict_output_capability_evidence
        self._strict_runtime_status = (
            "qualified_unprobed"
            if strict_output_capability_evidence.status == "verified"
            else "not_qualified"
        )
        self._strict_runtime_successes = 0
        self._strict_runtime_failures = 0
        self._strict_runtime_last_checked_at: str | None = None
        self._strict_runtime_last_failure_code: str | None = None

    def supports_strict_output_contract(self, contract: str) -> bool:
        """Declare whether this adapter can enforce ``contract`` at transport.

        Composition must not infer this capability merely because an object can
        perform some other source review.  The declaration is deliberately
        bounded to the schemas this adapter actually installs in
        ``response_format``.
        """

        return contract in _STRICT_SCHEMAS and self.strict_output_capability_evidence.supports(
            contract
        )

    def installs_strict_output_contract(self, contract: str) -> bool:
        """Report local schema installation separately from release evidence."""

        return contract in _STRICT_SCHEMAS

    def fork_isolated_runtime(self) -> "StructuredSourceReviewModel":
        """Reuse immutable route configuration with fresh failure state.

        Background Life review and visible conversation may use the same
        audited provider endpoints, but they must not share a circuit breaker,
        strict-runtime counters or an in-flight authority task.  The HTTP
        connection pool is intentionally reusable and carries no semantic
        health decision; its original composition owner closes it only after
        every authority task has quiesced.
        """

        if self.capacity_gate is not None:
            # A capacity gate may carry a process marker and physical-worker
            # lease.  Blindly copying or sharing it would falsely claim an
            # isolated runtime. Production remote source-review leaves do not
            # install one, so fail explicitly for unsupported custom routes.
            raise ValueError(
                "source-review runtime isolation does not support a capacity gate"
            )
        circuit = self.circuit_breaker
        isolated_circuit = (
            ProviderCircuitBreaker(
                failure_threshold=circuit.failure_threshold,
                cooldown_seconds=circuit.cooldown_seconds,
                clock=circuit.clock,
            )
            if circuit is not None
            else None
        )
        return StructuredSourceReviewModel(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            require_provider_parameters=self.require_provider_parameters,
            reasoning_effort=self.reasoning_effort,
            max_completion_tokens=self.max_completion_tokens,
            proxy_url=self.proxy_url,
            transport=self.transport,
            usage_observer=self.usage_observer,
            circuit_breaker=isolated_circuit,
            client=self.client,
            inventory_call_timeout_seconds=self.inventory_call_timeout_seconds,
            strict_output_capability_evidence=self.strict_output_capability_evidence,
        )

    def strict_output_capability_snapshot(self) -> dict[str, object]:
        """Return immutable transport evidence without invoking the provider."""

        return self.strict_output_capability_evidence.health_snapshot()

    def strict_output_runtime_snapshot(self) -> dict[str, object]:
        """Describe observed endpoint liveness separately from qualification."""

        return {
            "status": self._strict_runtime_status,
            "successful_calls": self._strict_runtime_successes,
            "failed_calls": self._strict_runtime_failures,
            "last_checked_at": self._strict_runtime_last_checked_at,
            "last_failure_code": self._strict_runtime_last_failure_code,
        }

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        contract = _explicit_output_contract(messages)
        try:
            result = await super().complete_json(messages, temperature=temperature)
        except BaseException as exc:
            self._record_strict_runtime_failure(contract=contract, exc=exc)
            raise
        self._record_strict_runtime_success(contract=contract)
        return result

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, dict[str, object]]:
        contract = _explicit_output_contract(messages)
        try:
            result = await super().complete_json_with_usage(
                messages,
                temperature=temperature,
            )
        except BaseException as exc:
            self._record_strict_runtime_failure(contract=contract, exc=exc)
            raise
        self._record_strict_runtime_success(contract=contract)
        return result

    def _record_strict_runtime_success(self, *, contract: str | None) -> None:
        if not self.strict_output_capability_evidence.supports(contract or ""):
            return
        self._strict_runtime_status = "runtime_succeeded"
        self._strict_runtime_successes += 1
        self._strict_runtime_last_checked_at = datetime.now(UTC).isoformat()
        self._strict_runtime_last_failure_code = None

    def _record_strict_runtime_failure(
        self,
        *,
        contract: str | None,
        exc: BaseException,
    ) -> None:
        if not self.strict_output_capability_evidence.supports(contract or ""):
            return
        self._strict_runtime_status = "runtime_failed"
        self._strict_runtime_failures += 1
        self._strict_runtime_last_checked_at = datetime.now(UTC).isoformat()
        if isinstance(exc, asyncio.CancelledError):
            reason = str(exc.args[0]) if exc.args else "caller_cancelled"
            code = f"cancelled:{reason}"
        elif isinstance(exc, TimeoutError):
            code = "provider_timeout"
        elif isinstance(exc, httpx.HTTPStatusError):
            code = f"http_status:{exc.response.status_code}"
        else:
            code = f"exception:{type(exc).__name__}"
        self._strict_runtime_last_failure_code = code[:160]

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
        # ``reasoning_effort`` is not part of Chat Completions for every
        # schema-capable checkpoint (notably the direct GPT-4o/4.1 families).
        # An empty value is an explicit route capability choice: omit the
        # argument entirely instead of sending an invalid empty string.
        if not self.reasoning_effort:
            payload.pop("reasoning_effort", None)
        if self.require_provider_parameters:
            # OpenRouter's provider routing advertises the portable
            # ``max_tokens`` parameter.  ``max_completion_tokens`` is the
            # official OpenAI spelling and would make require_parameters
            # reject every otherwise compatible OpenRouter endpoint.
            payload["max_tokens"] = payload.pop("max_completion_tokens")
            # Provider endpoints disagree on whether reasoning can be disabled.
            # OpenAI models explicitly support ``reasoning_effort=none`` and
            # become several seconds faster for this bounded classifier.
            # Other routed families may require reasoning, so omit the
            # optional knob and let OpenRouter select a schema-capable endpoint.
            if not self.model.startswith("openai/"):
                payload.pop("reasoning_effort", None)
            payload["provider"] = {"require_parameters": True}
        if not json_object:
            return payload
        contract = _explicit_output_contract(messages)
        selected = _STRICT_SCHEMAS.get(contract) if contract is not None else None
        if selected is None:
            return payload
        name, schema = selected
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": schema,
            },
        }
        return payload


class InventoryAvailabilityAuthority(SourceReviewAuthority):
    """Serial availability failover inside the single Inventory semantic role.

    Production may retain this object as dormant health/configuration state
    while a lane is unverified; ``supports_strict_output_contract`` remains
    false and composition cannot install it. Once every lane is independently
    qualified for the exact Inventory V5 wire, the reserve is created only
    after the primary ends and is an availability winner, never a second vote.
    """

    def __init__(
        self,
        *,
        primary: StructuredSourceReviewModel,
        secondary: StructuredSourceReviewModel,
        attempt_timeout_seconds: float,
        secondary_attempt_timeout_seconds: float | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        route_rejection_cooldown_seconds: float = 600.0,
        provider_timeout_cooldown_seconds: float = 600.0,
    ) -> None:
        if not math.isfinite(attempt_timeout_seconds) or not (
            0 < attempt_timeout_seconds <= 10
        ):
            raise ValueError("inventory attempt timeout must be in (0, 10] seconds")
        secondary_timeout = (
            attempt_timeout_seconds
            if secondary_attempt_timeout_seconds is None
            else float(secondary_attempt_timeout_seconds)
        )
        if not math.isfinite(secondary_timeout) or secondary_timeout <= 0:
            raise ValueError("inventory secondary timeout must be finite and positive")
        if attempt_timeout_seconds + secondary_timeout > (
            SOURCE_REVIEW_CALL_TIMEOUT_SECONDS - 1.0
        ):
            raise ValueError("inventory route timeouts exceed the caller boundary")
        if not callable(monotonic_clock):
            raise TypeError("inventory monotonic clock must be callable")
        if not math.isfinite(route_rejection_cooldown_seconds) or route_rejection_cooldown_seconds <= 0:
            raise ValueError("route rejection cooldown must be finite and positive")
        if not math.isfinite(provider_timeout_cooldown_seconds) or provider_timeout_cooldown_seconds <= 0:
            raise ValueError("provider timeout cooldown must be finite and positive")
        # Keep both routes inside the existing caller-owned 22 second bound.
        # Production may allocate a short primary probe and a longer reserve;
        # this does not expand the enclosing validation budget.
        # The enclosing adapter receives a slightly larger bound so this
        # authority, not caller cancellation, publishes terminal exhaustion.
        internal_deadline = attempt_timeout_seconds + secondary_timeout + 0.5
        super().__init__(
            primary=primary,
            secondary=secondary,
            hedge_after_seconds=attempt_timeout_seconds,
            deadline_seconds=internal_deadline,
            caller_timeout_seconds=SOURCE_REVIEW_CALL_TIMEOUT_SECONDS,
        )
        self.model = (
            "inventory-availability-authority:"
            f"{primary.model}|{secondary.model}"
        )
        self.provider = "inventory-availability-authority"
        self.inventory_attempt_timeout_seconds = float(attempt_timeout_seconds)
        self.inventory_secondary_reserved_seconds = secondary_timeout
        self.inventory_call_timeout_seconds = min(
            SOURCE_REVIEW_CALL_TIMEOUT_SECONDS - 0.5,
            self.deadline_seconds + 0.25,
        )
        self.strict_output_capability_evidences = (
            primary.strict_output_capability_evidence,
            secondary.strict_output_capability_evidence,
        )
        self._monotonic_clock = monotonic_clock
        self._route_rejection_cooldown_seconds = float(route_rejection_cooldown_seconds)
        self._provider_timeout_cooldown_seconds = float(provider_timeout_cooldown_seconds)
        self._route_suppressed_until = {"primary": 0.0, "secondary": 0.0}
        self._route_suppression_reason: dict[str, str | None] = {
            "primary": None,
            "secondary": None,
        }
        self._route_skipped_calls = {"primary": 0, "secondary": 0}
        self._last_winner_protocol: str | None = None
        self._full_review_fallback_started = 0
        self._full_review_fallback_succeeded = 0
        self._full_review_fallback_failed = 0
        self._last_full_review_fallback_outcome: str | None = None

    def _lane_attempt_timeout_seconds(
        self,
        *,
        lane: Literal["primary", "secondary"],
        ordinal: int,
        remaining_seconds: float,
        allow_hedge: bool,
    ) -> float:
        del ordinal, allow_hedge
        configured = (
            self.inventory_attempt_timeout_seconds
            if lane == "primary"
            else self.inventory_secondary_reserved_seconds
        )
        return min(configured, remaining_seconds)

    def _lane_preflight_failure(
        self,
        lane: Literal["primary", "secondary"],
    ) -> str | None:
        now = self._monotonic_clock()
        with self._health_lock:
            retry_at = self._route_suppressed_until[lane]
            reason = self._route_suppression_reason[lane]
            if retry_at <= now:
                return None
            self._route_skipped_calls[lane] += 1
        return f"route_suppressed:{reason or 'technical_failure'}"

    def _after_lane_failure(
        self,
        lane: Literal["primary", "secondary"],
        reason: str,
    ) -> None:
        if reason.endswith(":http_403"):
            suppression_reason = "http_403"
            cooldown = self._route_rejection_cooldown_seconds
        elif reason == "provider_timeout":
            suppression_reason = "provider_timeout"
            cooldown = self._provider_timeout_cooldown_seconds
        else:
            return
        with self._health_lock:
            self._route_suppression_reason[lane] = suppression_reason
            self._route_suppressed_until[lane] = self._monotonic_clock() + cooldown

    def _after_lane_success(
        self,
        lane: Literal["primary", "secondary"],
    ) -> None:
        with self._health_lock:
            self._route_suppressed_until[lane] = 0.0
            self._route_suppression_reason[lane] = None
            self._last_winner_protocol = "inventory_v5"

    @staticmethod
    def _exhausted_exception(
        lane_failures: dict[Literal["primary", "secondary"], str],
        **kwargs: object,
    ) -> InventoryAvailabilityExhausted:
        return InventoryAvailabilityExhausted(lane_failures, **kwargs)

    def record_full_source_closure_fallback(self, outcome: str) -> None:
        """Publish strict fallback health without changing its semantic result."""

        if outcome not in {"started", "succeeded", "failed"}:
            raise ValueError("unknown full source-closure fallback outcome")
        with self._health_lock:
            if outcome == "started":
                self._full_review_fallback_started += 1
            elif outcome == "succeeded":
                self._full_review_fallback_succeeded += 1
                self._last_winner_protocol = "full_source_closure_review.7"
            else:
                self._full_review_fallback_failed += 1
            self._last_full_review_fallback_outcome = outcome

    def health_snapshot(self) -> dict[str, object]:
        snapshot = super().health_snapshot()
        now = self._monotonic_clock()
        with self._health_lock:
            snapshot.update(
                {
                    "primary_attempt_timeout_seconds": (
                        self.inventory_attempt_timeout_seconds
                    ),
                    "secondary_attempt_timeout_seconds": (
                        self.inventory_secondary_reserved_seconds
                    ),
                    "route_rejection_cooldown_seconds": (
                        self._route_rejection_cooldown_seconds
                    ),
                    "provider_timeout_cooldown_seconds": (
                        self._provider_timeout_cooldown_seconds
                    ),
                    "route_suppression": {
                        lane: {
                            "active": self._route_suppressed_until[lane] > now,
                            "reason": self._route_suppression_reason[lane],
                            "retry_after_seconds": max(
                                0.0,
                                self._route_suppressed_until[lane] - now,
                            ),
                            "skipped_calls": self._route_skipped_calls[lane],
                        }
                        for lane in ("primary", "secondary")
                    },
                    "last_winner_protocol": self._last_winner_protocol,
                    "full_source_closure_fallback": {
                        "started": self._full_review_fallback_started,
                        "succeeded": self._full_review_fallback_succeeded,
                        "failed": self._full_review_fallback_failed,
                        "last_outcome": self._last_full_review_fallback_outcome,
                    },
                }
            )
        return snapshot

    def strict_output_runtime_snapshot(self) -> dict[str, object]:
        """Expose lane liveness and the single winning route without probing."""

        lane_runtime = {
            "primary": self.primary.strict_output_runtime_snapshot(),
            "secondary": self.secondary.strict_output_runtime_snapshot(),
        }
        authority_health = self.health_snapshot()
        last_winner = authority_health["last_winner_lane"]
        fallback_health = authority_health["full_source_closure_fallback"]
        assert isinstance(fallback_health, dict)
        if authority_health["last_winner_protocol"] == "full_source_closure_review.7":
            status = "degraded"
        elif fallback_health["last_outcome"] == "failed":
            status = "runtime_failed"
        elif last_winner in {"primary", "secondary"}:
            status = "runtime_succeeded"
        elif any(
            snapshot["status"] == "runtime_failed"
            for snapshot in lane_runtime.values()
        ):
            status = "runtime_failed"
        elif all(
            snapshot["status"] == "qualified_unprobed"
            for snapshot in lane_runtime.values()
        ):
            status = "qualified_unprobed"
        else:
            status = "unavailable"
        checked = sorted(
            str(value)
            for snapshot in lane_runtime.values()
            if (value := snapshot["last_checked_at"]) is not None
        )
        failed_codes = [
            str(value)
            for snapshot in lane_runtime.values()
            if (value := snapshot["last_failure_code"]) is not None
        ]
        return {
            "status": status,
            "successful_calls": sum(
                int(snapshot["successful_calls"])
                for snapshot in lane_runtime.values()
            ),
            "failed_calls": sum(
                int(snapshot["failed_calls"])
                for snapshot in lane_runtime.values()
            ),
            "last_checked_at": checked[-1] if checked else None,
            "last_failure_code": (
                failed_codes[-1] if status == "runtime_failed" and failed_codes else None
            ),
            "last_winner_lane": last_winner,
            "last_winner_protocol": authority_health["last_winner_protocol"],
            "lane_models": authority_health["lane_models"],
            "lane_providers": authority_health["lane_providers"],
            "lanes": lane_runtime,
            "route_suppression": authority_health["route_suppression"],
            "route_rejection_cooldown_seconds": authority_health[
                "route_rejection_cooldown_seconds"
            ],
            "provider_timeout_cooldown_seconds": authority_health[
                "provider_timeout_cooldown_seconds"
            ],
            "full_source_closure_fallback": fallback_health,
        }


__all__ = [
    "direct_openai_model_id",
    "InventoryAvailabilityAuthority",
    "openai_inventory_capability_evidence",
    "audited_source_review_capability_evidence",
    "StrictOutputCapabilityEvidence",
    "StructuredSourceReviewModel",
    "openrouter_inventory_capability_evidence",
]
