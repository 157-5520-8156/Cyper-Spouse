import asyncio
import json

import httpx
import pytest

from companion_daemon.world_v2.structured_source_review_model import (
    direct_openai_model_id,
    InventoryAvailabilityAuthority,
    openai_inventory_capability_evidence,
    audited_source_review_capability_evidence,
    StrictOutputCapabilityEvidence,
    StructuredSourceReviewModel,
)
from companion_daemon.world_v2.source_review_authority import (
    InventoryAvailabilityExhausted,
)


def _contract_message(contract: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": "Return JSON only."},
        {
            "role": "user",
            "content": json.dumps(
                {"output_contract": {"contract": contract}},
                separators=(",", ":"),
            ),
        },
    ]


def _strict_schema(payload: dict[str, object]) -> dict[str, object]:
    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["type"] == "json_schema"
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["strict"] is True
    schema = envelope["schema"]
    assert isinstance(schema, dict)
    return schema


def _assert_every_object_is_closed_and_fully_required(schema: dict[str, object]) -> None:
    if schema.get("type") == "object":
        properties = schema.get("properties")
        if properties is not None:
            assert isinstance(properties, dict)
            assert schema.get("additionalProperties") is False
            assert set(schema.get("required", ())) == set(properties)
            for child in properties.values():
                assert isinstance(child, dict)
                _assert_every_object_is_closed_and_fully_required(child)
    items = schema.get("items")
    if isinstance(items, dict):
        _assert_every_object_is_closed_and_fully_required(items)
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for child in alternatives:
            assert isinstance(child, dict)
            _assert_every_object_is_closed_and_fully_required(child)


@pytest.mark.asyncio
async def test_source_closure_completion_uses_its_strict_wire_schema() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": ('{"ci":[],"v":[],"p":[],"visible_findings":[],"r":"ok"}')
                        }
                    }
                ]
            },
        )

    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
        transport=httpx.MockTransport(handler),
    )

    result = await model.complete_json(_contract_message("source-closure-review.7"))

    assert result == '{"ci":[],"v":[],"p":[],"visible_findings":[],"r":"ok"}'
    response_format = captured_payload["response_format"]
    assert response_format == {
        "type": "json_schema",
        "json_schema": {
            "name": "source_closure_review_v7",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "ci": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0},
                        "maxItems": 8,
                    },
                    "v": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "undeclared_external_assertion",
                                "subject_authority_mismatch",
                                "temporal_authority_mismatch",
                                "occurrence_or_status_authority_mismatch",
                            ],
                        },
                        "maxItems": 4,
                    },
                    "p": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "undeclared_external_assertion",
                                "subject_authority_mismatch",
                                "temporal_authority_mismatch",
                                "occurrence_or_status_authority_mismatch",
                            ],
                        },
                        "maxItems": 0,
                    },
                    "visible_findings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "undeclared_external_assertion",
                                        "subject_authority_mismatch",
                                        "temporal_authority_mismatch",
                                        "occurrence_or_status_authority_mismatch",
                                    ],
                                },
                                "visible_span": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 1024,
                                },
                                "claim_index": {
                                    "anyOf": [
                                        {"type": "integer", "minimum": 0},
                                        {"type": "null"},
                                    ]
                                },
                                "source_relation": {
                                    "type": "string",
                                    "enum": [
                                        "unclosed",
                                        "exact_current_report_discourse_coverage",
                                        "declared_world_claim_source_mismatch",
                                    ],
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
            },
        },
    }
    assert "provider" not in captured_payload
    await model.aclose()


@pytest.mark.parametrize(
    ("contract", "schema_name"),
    [
        (
            "report-relative-entailment-adjudication.3",
            "report_relative_entailment_adjudication_v3",
        ),
        (
            "report-relative-entailment-adjudication.2",
            "report_relative_entailment_adjudication_v2",
        ),
        (
            "report-relative-entailment-adjudication.1",
            "report_relative_entailment_adjudication_v1",
        ),
        (
            "candidate-external-proposition-coverage.1",
            "candidate_external_proposition_coverage_v1",
        ),
        (
            "candidate-external-proposition-coverage.2",
            "candidate_external_proposition_coverage_v2",
        ),
        (
            "candidate-external-proposition-coverage.3",
            "candidate_external_proposition_coverage_v3",
        ),
        (
            "candidate-external-proposition-coverage.5",
            "candidate_external_proposition_coverage_v5",
        ),
        (
            "candidate-epistemic-role-conflict.1",
            "candidate_epistemic_role_conflict_v1",
        ),
        (
            "candidate-external-proposition-inventory.3",
            "candidate_external_proposition_inventory_v3",
        ),
        (
            "candidate-external-proposition-inventory.4",
            "candidate_external_proposition_inventory_v4",
        ),
        (
            "candidate-external-proposition-inventory.5",
            "candidate_external_proposition_inventory_v5",
        ),
        (
            "source-closure-review.8",
            "source_closure_review_v8",
        ),
        ("source-closure-appeal.4", "source_closure_appeal_v4"),
        (
            "life-development-source-closure-review.1",
            "life_development_source_closure_review_v1",
        ),
        (
            "life-development-novel-origin-review.1",
            "life_development_novel_origin_review_v1",
        ),
        (
            "life-development-novel-origin-review.2",
            "life_development_novel_origin_review_v2",
        ),
        (
            "world-author-source-rewrite.1",
            "world_author_source_rewrite_v1",
        ),
        (
            "world-author-source-rewrite-propose-repair.1",
            "world_author_source_rewrite_propose_repair_v1",
        ),
    ],
)
def test_each_known_contract_has_a_closed_strict_schema(
    contract: str,
    schema_name: str,
) -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    payload = model.request_payload(
        _contract_message(contract),
        temperature=0.0,
        json_object=True,
    )

    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == schema_name
    _assert_every_object_is_closed_and_fully_required(_strict_schema(payload))


def test_unknown_compatible_endpoint_does_not_auto_qualify_strict_contracts() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://openai-compatible.invalid/v1",
        "reviewer",
    )

    assert (
        model.supports_strict_output_contract("candidate-external-proposition-inventory.3") is False
    )
    assert (
        model.supports_strict_output_contract("candidate-external-proposition-inventory.5") is False
    )
    assert (
        model.supports_strict_output_contract("candidate-external-proposition-coverage.3") is False
    )
    assert (
        model.supports_strict_output_contract("candidate-external-proposition-coverage.5") is False
    )
    assert model.supports_strict_output_contract("candidate-epistemic-role-conflict.1") is False
    assert model.supports_strict_output_contract("unknown-review-contract.1") is False


def test_audited_source_review_evidence_is_endpoint_model_and_schema_exact() -> None:
    evidence = audited_source_review_capability_evidence(
        base_url="https://api.openai.com/v1",
        model="gpt-4.1-mini",
        provider="openai",
    )
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "gpt-4.1-mini",
        strict_output_capability_evidence=evidence,
    )

    assert model.supports_strict_output_contract("source-closure-review.7") is True
    assert (
        model.supports_strict_output_contract(
            "report-relative-entailment-adjudication.3"
        )
        is True
    )
    assert model.supports_strict_output_contract("source-closure-review.8") is False
    assert (
        model.supports_strict_output_contract(
            "candidate-external-proposition-coverage.5"
        )
        is False
    )
    assert evidence.evidence_source == "production_contract_audit"
    assert evidence.audit_sample_count == 16
    assert evidence.audit_success_count == 13
    assert model.supports_strict_output_contract("candidate-external-proposition-inventory.5") is False
    assert dict(evidence.contract_schema_digests)["source-closure-review.7"] == (
        "99e95d9e68eb7648f8aa282d675ce0fbbf293078f1d6640031d693d23ee48beb"
    )

    wrong_endpoint = audited_source_review_capability_evidence(
        base_url="https://openai-compatible.invalid/v1",
        model="gpt-4.1-mini",
        provider="openai",
    )
    wrong_model = audited_source_review_capability_evidence(
        base_url="https://api.openai.com/v1",
        model="gpt-5.6-terra",
        provider="openai",
    )
    assert wrong_endpoint.status == "unverified"
    assert wrong_model.status == "unverified"

    openrouter = audited_source_review_capability_evidence(
        base_url="https://openrouter.ai/api/v1",
        model="qwen/qwen-plus",
        provider="openrouter",
    )
    assert openrouter.contracts == (
        "source-closure-review.7",
        "report-relative-entailment-adjudication.3",
    )
    assert openrouter.audit_sample_count == 13
    assert openrouter.audit_success_count == 13


def test_openrouter_strict_contract_requires_endpoint_capability_evidence() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "nousresearch/hermes-4-70b",
        require_provider_parameters=True,
    )

    assert (
        model.supports_strict_output_contract(
            "candidate-external-proposition-inventory.5"
        )
        is False
    )
    assert model.strict_output_capability_snapshot() == {
        "status": "unverified",
        "evidence_source": "none",
        "reason_code": "strict_output.endpoint_capability_unverified",
        "provider": "openrouter",
        "model": "nousresearch/hermes-4-70b",
        "contracts": (),
        "observed_at": None,
        "qualified_at": None,
        "evidence_revision": None,
        "audit_sample_count": None,
        "audit_success_count": None,
        "contract_schema_digests": {},
    }


def test_direct_openai_inventory_evidence_uses_returned_v5_contract_audit() -> None:
    model = direct_openai_model_id("openai/gpt-5.4-mini")

    evidence = openai_inventory_capability_evidence(
        enabled=True,
        base_url="https://api.openai.com/v1",
        model=model,
    )

    assert model == "gpt-5.4-mini"
    assert evidence.health_snapshot() == {
        "status": "verified",
        "evidence_source": "production_contract_audit",
        "reason_code": "strict_output.endpoint_capability_verified",
        "provider": "openai",
        "model": "gpt-5.4-mini",
        "contracts": ("candidate-external-proposition-inventory.5",),
        "observed_at": "2026-08-01",
        "qualified_at": "2026-08-01",
        "evidence_revision": "inventory-v5-openai-gpt54mini-20260801.2",
        "audit_sample_count": 12,
        "audit_success_count": 11,
        "contract_schema_digests": {
            "candidate-external-proposition-inventory.5": (
                "cd55ce09687b5b4e68b1a6805244f76e9c43d4e286b3bee5bb183715a38519fb"
            )
        },
    }


def test_direct_openai_inventory_evidence_fails_closed_for_proxy_endpoint() -> None:
    evidence = openai_inventory_capability_evidence(
        enabled=True,
        base_url="https://openai-compatible.invalid/v1",
        model="gpt-5.4-mini",
    )

    assert evidence.status == "unverified"
    assert evidence.reason_code == "source_inventory.endpoint_capability_unverified"


def test_openrouter_strict_contract_is_bounded_by_matching_verified_evidence() -> None:
    evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="release_probe",
        provider="openrouter",
        model="inventory-model",
        contracts=("candidate-external-proposition-inventory.5",),
        observed_at="2026-08-01",
    )
    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "inventory-model",
        require_provider_parameters=True,
        strict_output_capability_evidence=evidence,
    )

    assert (
        model.supports_strict_output_contract(
            "candidate-external-proposition-inventory.5"
        )
        is True
    )
    assert (
        model.supports_strict_output_contract(
            "candidate-external-proposition-coverage.5"
        )
        is False
    )
    assert model.strict_output_capability_snapshot()["status"] == "verified"
    assert model.strict_output_runtime_snapshot()["status"] == "qualified_unprobed"


def test_openrouter_strict_contract_refuses_stale_schema_evidence() -> None:
    evidence = StrictOutputCapabilityEvidence(
        status="verified",
        evidence_source="stale_release_probe",
        reason_code="strict_output.endpoint_capability_verified",
        provider="openrouter",
        model="inventory-model",
        contracts=("candidate-external-proposition-inventory.5",),
        observed_at="2026-07-01",
        contract_schema_digests=(
            ("candidate-external-proposition-inventory.5", "0" * 64),
        ),
    )

    with pytest.raises(ValueError, match="schema digest"):
        StructuredSourceReviewModel(
            "key",
            "https://openrouter.ai/api/v1",
            "inventory-model",
            require_provider_parameters=True,
            strict_output_capability_evidence=evidence,
        )


@pytest.mark.asyncio
async def test_openrouter_strict_runtime_health_separates_success_from_qualification() -> None:
    evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="test_contract_audit",
        provider="openrouter",
        model="inventory-model",
        contracts=("candidate-external-proposition-inventory.5",),
        observed_at="2026-08-01",
    )
    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "inventory-model",
        require_provider_parameters=True,
        strict_output_capability_evidence=evidence,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"contract":"candidate-external-proposition-inventory.5",'
                                    '"propositions":[]}'
                                )
                            }
                        }
                    ]
                },
            )
        ),
    )

    await model.complete_json(
        _contract_message("candidate-external-proposition-inventory.5")
    )

    runtime = model.strict_output_runtime_snapshot()
    assert runtime["status"] == "runtime_succeeded"
    assert runtime["successful_calls"] == 1
    assert runtime["failed_calls"] == 0
    assert runtime["last_checked_at"] is not None
    assert runtime["last_failure_code"] is None
    await model.aclose()


@pytest.mark.asyncio
async def test_openrouter_strict_runtime_health_records_typed_provider_rejection() -> None:
    evidence = StrictOutputCapabilityEvidence.verified(
        evidence_source="test_contract_audit",
        provider="openrouter",
        model="inventory-model",
        contracts=("candidate-external-proposition-inventory.5",),
        observed_at="2026-08-01",
    )
    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "inventory-model",
        require_provider_parameters=True,
        strict_output_capability_evidence=evidence,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, json={"error": "no endpoint"})
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await model.complete_json(
            _contract_message("candidate-external-proposition-inventory.5")
        )

    runtime = model.strict_output_runtime_snapshot()
    assert runtime["status"] == "runtime_failed"
    assert runtime["successful_calls"] == 0
    assert runtime["failed_calls"] == 1
    assert runtime["last_checked_at"] is not None
    assert runtime["last_failure_code"] == "http_status:404"
    await model.aclose()


@pytest.mark.asyncio
async def test_inventory_availability_authority_uses_one_serial_winner_and_records_routes() -> None:
    contract = "candidate-external-proposition-inventory.5"

    def evidence(model: str, *, provider: str) -> StrictOutputCapabilityEvidence:
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="test_contract_audit",
            provider=provider,
            model=model,
            contracts=(contract,),
            observed_at="2026-08-01",
        )

    primary = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "inventory-primary",
        require_provider_parameters=True,
        strict_output_capability_evidence=evidence(
            "inventory-primary",
            provider="openrouter",
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(403, json={"error": "content rejected"})
        ),
    )
    secondary = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "inventory-secondary",
        strict_output_capability_evidence=evidence(
            "inventory-secondary",
            provider="openai",
        ),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"contract":"candidate-external-proposition-inventory.5",'
                                    '"propositions":[]}'
                                )
                            }
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "total_tokens": 13,
                    },
                },
            )
        ),
    )
    authority = InventoryAvailabilityAuthority(
        primary=primary,
        secondary=secondary,
        attempt_timeout_seconds=0.1,
    )
    assert authority.hedge_after_seconds == 0.1
    assert authority.deadline_seconds >= 0.2
    assert authority.inventory_secondary_reserved_seconds == 0.1
    assert authority.inventory_call_timeout_seconds > authority.deadline_seconds

    raw, usage = await authority.complete_json_with_usage(
        _contract_message(contract),
        temperature=0.0,
    )

    assert json.loads(raw) == {"contract": contract, "propositions": []}
    assert usage["provider"] == "openai"
    attempts = raw.source_review_attempts
    assert [
        (attempt.model_id, attempt.outcome, attempt.failure_code)
        for attempt in attempts
    ] == [
        ("inventory-primary", "exception", "HTTPStatusError:http_403"),
        ("inventory-secondary", "winner", None),
    ]
    runtime = authority.strict_output_runtime_snapshot()
    assert runtime["status"] == "runtime_succeeded"
    assert runtime["last_winner_lane"] == "secondary"
    assert runtime["lanes"]["primary"]["status"] == "runtime_failed"
    assert runtime["lanes"]["primary"]["last_failure_code"] == "http_status:403"
    assert runtime["lanes"]["secondary"]["status"] == "runtime_succeeded"
    authority_health = authority.health_snapshot()
    assert authority_health["lane_providers"] == {
        "primary": "openrouter",
        "secondary": "openai",
    }
    assert authority_health["lane_failures"]["primary"] == 1
    assert authority_health["last_lane_failure_reasons"]["primary"] == (
        "HTTPStatusError:http_403"
    )
    await primary.aclose()
    await secondary.aclose()


@pytest.mark.asyncio
async def test_inventory_suppresses_known_403_route_across_calls_without_faking_attempt() -> None:
    contract = "candidate-external-proposition-inventory.5"
    now = [100.0]
    primary_calls = 0
    secondary_calls = 0

    def evidence(model: str, *, provider: str) -> StrictOutputCapabilityEvidence:
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="test_contract_audit",
            provider=provider,
            model=model,
            contracts=(contract,),
            observed_at="2026-08-01",
        )

    def primary_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        return httpx.Response(403, json={"error": "route rejected"})

    def secondary_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal secondary_calls
        secondary_calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"contract":"candidate-external-proposition-inventory.5",'
                                '"propositions":[]}'
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    primary = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "inventory-primary",
        require_provider_parameters=True,
        strict_output_capability_evidence=evidence(
            "inventory-primary", provider="openrouter"
        ),
        transport=httpx.MockTransport(primary_handler),
    )
    secondary = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "inventory-secondary",
        strict_output_capability_evidence=evidence(
            "inventory-secondary", provider="openai"
        ),
        transport=httpx.MockTransport(secondary_handler),
    )
    authority = InventoryAvailabilityAuthority(
        primary=primary,
        secondary=secondary,
        attempt_timeout_seconds=0.05,
        secondary_attempt_timeout_seconds=0.1,
        monotonic_clock=lambda: now[0],
    )

    first, _ = await authority.complete_json_with_usage(
        _contract_message(contract), temperature=0.0
    )
    second, _ = await authority.complete_json_with_usage(
        _contract_message(contract), temperature=0.0
    )

    assert primary_calls == 1
    assert secondary_calls == 2
    assert len(first.source_review_attempts) == 2
    assert [attempt.lane for attempt in second.source_review_attempts] == ["secondary"]
    health = authority.health_snapshot()
    assert health["route_suppression"]["primary"]["active"] is True
    assert health["route_suppression"]["primary"]["reason"] == "http_403"
    assert health["route_suppression"]["primary"]["skipped_calls"] == 1
    assert health["primary_attempt_timeout_seconds"] == 0.05
    assert health["secondary_attempt_timeout_seconds"] == 0.1
    await primary.aclose()
    await secondary.aclose()


@pytest.mark.asyncio
async def test_inventory_timeout_uses_short_cooldown_then_half_open_probe() -> None:
    contract = "candidate-external-proposition-inventory.5"
    now = [100.0]

    def evidence(model: str, *, provider: str) -> StrictOutputCapabilityEvidence:
        return StrictOutputCapabilityEvidence.verified(
            evidence_source="test_contract_audit",
            provider=provider,
            model=model,
            contracts=(contract,),
            observed_at="2026-08-01",
        )

    class _RejectedPrimary:
        model = "inventory-primary"
        provider = "openrouter"
        strict_output_capability_evidence = evidence(model, provider=provider)

        def __init__(self) -> None:
            self.calls = 0

        def strict_output_runtime_snapshot(self) -> dict[str, object]:
            return {
                "status": "runtime_failed",
                "successful_calls": 0,
                "failed_calls": self.calls,
                "last_checked_at": None,
                "last_failure_code": "http_status:403",
            }

        async def complete_json_with_usage(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.calls += 1
            request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
            response = httpx.Response(403, request=request)
            raise httpx.HTTPStatusError("rejected", request=request, response=response)

    class _BlockingSecondary:
        model = "inventory-secondary"
        provider = "openai"
        strict_output_capability_evidence = evidence(model, provider=provider)

        def __init__(self) -> None:
            self.calls = 0

        def strict_output_runtime_snapshot(self) -> dict[str, object]:
            return {
                "status": "runtime_failed",
                "successful_calls": 0,
                "failed_calls": self.calls,
                "last_checked_at": None,
                "last_failure_code": "provider_timeout",
            }

        async def complete_json_with_usage(self, messages, *, temperature=0.0):  # type: ignore[no-untyped-def]
            del messages, temperature
            self.calls += 1
            await asyncio.Future()

    primary = _RejectedPrimary()
    secondary = _BlockingSecondary()
    authority = InventoryAvailabilityAuthority(
        primary=primary,  # type: ignore[arg-type]
        secondary=secondary,  # type: ignore[arg-type]
        attempt_timeout_seconds=0.01,
        secondary_attempt_timeout_seconds=0.01,
        monotonic_clock=lambda: now[0],
    )

    with pytest.raises(InventoryAvailabilityExhausted) as first:
        await authority.complete_json_with_usage(
            _contract_message(contract), temperature=0.0
        )
    with pytest.raises(InventoryAvailabilityExhausted) as suppressed:
        await authority.complete_json_with_usage(
            _contract_message(contract), temperature=0.0
        )

    assert primary.calls == 1
    assert secondary.calls == 1
    assert first.value.lane_failures["secondary"] == "provider_timeout"
    assert suppressed.value.source_review_attempts == ()
    assert suppressed.value.lane_failures == {
        "primary": "route_suppressed:http_403",
        "secondary": "route_suppressed:provider_timeout",
    }

    # Inventory-only transient suppression prevents every ordinary user turn
    # from repaying an 8-second dead-route probe.  One bounded half-open
    # probe is allowed after ten minutes.
    now[0] += 601.0
    with pytest.raises(InventoryAvailabilityExhausted) as reprobe:
        await authority.complete_json_with_usage(
            _contract_message(contract), temperature=0.0
        )
    assert primary.calls == 2
    assert secondary.calls == 2
    assert [attempt.failure_code for attempt in reprobe.value.source_review_attempts] == [
        "HTTPStatusError:http_403",
        "provider_timeout",
    ]
    authority.record_full_source_closure_fallback("started")
    authority.record_full_source_closure_fallback("succeeded")
    runtime = authority.strict_output_runtime_snapshot()
    assert runtime["status"] == "degraded"
    assert runtime["last_winner_protocol"] == "full_source_closure_review.7"
    assert runtime["full_source_closure_fallback"] == {
        "started": 1,
        "succeeded": 1,
        "failed": 0,
        "last_outcome": "succeeded",
    }


def test_candidate_coverage_v2_schema_uses_only_host_bound_indexes() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.2"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["contract", "inventory_complete", "findings"]
    findings = properties["findings"]
    assert isinstance(findings, dict)
    item = findings["items"]
    assert isinstance(item, dict)
    item_properties = item["properties"]
    assert isinstance(item_properties, dict)
    assert list(item_properties) == [
        "locator_index",
        "decision",
        "source_relation",
        "source_ref_indexes",
    ]
    assert "pinned_context_authority_coverage" not in item_properties["source_relation"]["enum"]
    assert "locator" not in item_properties
    assert "source_refs" not in item_properties


def test_candidate_coverage_v3_schema_uses_only_host_bound_indexes() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.3"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["contract", "inventory_complete", "findings"]
    assert properties["contract"]["enum"] == ["candidate-external-proposition-coverage.3"]
    findings = properties["findings"]
    assert isinstance(findings, dict)
    item = findings["items"]
    assert isinstance(item, dict)
    assert list(item) == ["anyOf"]
    assert any(
        variant["properties"]["source_relation"]["enum"] == ["pinned_context_authority_coverage"]
        for variant in item["anyOf"]
    )
    for variant in item["anyOf"]:
        item_properties = variant["properties"]
        assert list(item_properties) == [
            "locator_index",
            "decision",
            "source_relation",
            "source_ref_indexes",
        ]
        assert "locator" not in item_properties
        assert "source_refs" not in item_properties


def test_candidate_coverage_v3_schema_mechanically_couples_decision_and_relation() -> None:
    """The provider cannot emit a structurally contradictory terminal verdict."""

    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.3"),
            temperature=0.0,
            json_object=True,
        )
    )
    item = schema["properties"]["findings"]["items"]
    variants = item["anyOf"]
    decision_relation_pairs = {
        (
            tuple(variant["properties"]["decision"]["enum"]),
            tuple(variant["properties"]["source_relation"]["enum"]),
            variant["properties"]["source_ref_indexes"].get("minItems"),
            variant["properties"]["source_ref_indexes"].get("maxItems"),
        )
        for variant in variants
    }

    assert (
        ("not_external_proposition",),
        ("not_external_proposition",),
        None,
        0,
    ) in decision_relation_pairs
    assert (("unclosed",), ("unclosed",), None, 0) in decision_relation_pairs
    assert (
        ("closed",),
        ("first_person_immediate_private_continuity",),
        None,
        0,
    ) in decision_relation_pairs
    assert any(
        decision == ("closed",)
        and relation == ("exact_current_report_discourse_coverage",)
        and minimum == 1
        for decision, relation, minimum, _maximum in decision_relation_pairs
    )


def test_candidate_coverage_v5_schema_is_indexed_verdict_only() -> None:
    """Inventory V5 alone owns exhaustive decomposition and locator discovery."""

    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.5"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert list(properties) == ["contract", "findings"]
    assert properties["contract"]["enum"] == ["candidate-external-proposition-coverage.5"]
    assert schema["required"] == ["contract", "findings"]
    assert "inventory_complete" not in properties
    assert "missing_findings" not in properties

    finding_variants = properties["findings"]["items"]["anyOf"]
    assert finding_variants == _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.4"),
            temperature=0.0,
            json_object=True,
        )
    )["properties"]["findings"]["items"]["anyOf"]
    for variant in finding_variants:
        assert list(variant["properties"]) == [
            "locator_index",
            "decision",
            "source_relation",
            "source_ref_indexes",
        ]


def test_candidate_inventory_v4_schema_separates_private_temporal_authority() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-inventory.4"),
            temperature=0.0,
            json_object=True,
        )
    )
    propositions = schema["properties"]["propositions"]
    roles = propositions["items"]["properties"]["semantic_role"]["enum"]
    assert "immediate_private_state" in roles
    assert "source_bearing_private_episode" in roles
    assert "outer_private_state" not in roles


def test_candidate_inventory_v5_schema_removes_parent_linkage() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-inventory.5"),
            temperature=0.0,
            json_object=True,
        )
    )
    proposition = schema["properties"]["propositions"]["items"]
    assert list(proposition["properties"]) == ["locator", "semantic_role"]
    assert proposition["required"] == ["locator", "semantic_role"]
    assert proposition["properties"]["semantic_role"]["enum"] == [
        "immediate_private_state",
        "source_bearing_private_episode",
        "embedded_external_proposition",
        "standalone_external_proposition",
        "world_unbound_generalization",
        "nonassertive_content",
    ]


def test_declared_claim_review_schema_cannot_rejudge_visible_text() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("source-closure-review.8"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert properties["v"]["maxItems"] == 0
    assert properties["p"]["maxItems"] == 0
    assert properties["visible_findings"]["maxItems"] == 0


def test_report_relative_v2_schema_matches_the_current_wire_enums() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("report-relative-entailment-adjudication.2"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["contract"] == {
        "type": "string",
        "enum": ["report-relative-entailment-adjudication.2"],
    }
    findings = properties["findings"]
    assert isinstance(findings, dict)
    item = findings["items"]
    assert isinstance(item, dict)
    item_properties = item["properties"]
    assert isinstance(item_properties, dict)
    assert item_properties["decision"] == {
        "type": "string",
        "enum": [
            "covered_by_exact_current_report",
            "covered_by_first_person_immediate_private_continuity",
            "not_external_proposition",
            "retain_unclosed",
        ],
    }
    assert item_properties["failure_dimensions"] == {
        "type": "array",
        "items": {
            "type": "string",
            "enum": [
                "participant_role",
                "logical_modality",
                "polarity",
                "temporal_relation",
                "agent_patient_relation",
                "added_external_premise",
                "habitual_or_generic_scope",
            ],
        },
        "maxItems": 7,
    }


def test_report_relative_v1_schema_preserves_the_legacy_wire() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("report-relative-entailment-adjudication.1"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    findings = properties["findings"]
    assert isinstance(findings, dict)
    item = findings["items"]
    assert isinstance(item, dict)
    item_properties = item["properties"]
    assert isinstance(item_properties, dict)
    assert item_properties["decision"] == {
        "type": "string",
        "enum": [
            "covered_by_exact_current_report",
            "retain_unclosed",
        ],
    }
    assert "failure_dimensions" not in item_properties


def test_candidate_coverage_schema_includes_non_external_propositions() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-coverage.1"),
            temperature=0.0,
            json_object=True,
        )
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    findings = properties["findings"]
    assert isinstance(findings, dict)
    finding = findings["items"]
    assert isinstance(finding, dict)
    finding_properties = finding["properties"]
    assert isinstance(finding_properties, dict)
    assert finding_properties["decision"] == {
        "type": "string",
        "enum": ["closed", "unclosed", "not_external_proposition"],
    }
    assert finding_properties["source_relation"] == {
        "type": "string",
        "enum": [
            "unclosed",
            "not_external_proposition",
            "exact_current_report_discourse_coverage",
            "exact_dialogue_record_coverage",
            "first_person_immediate_private_continuity",
            "declared_world_claim_source_coverage",
        ],
    }


def test_candidate_inventory_v3_schema_matches_the_exact_decomposition_wire() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("candidate-external-proposition-inventory.3"),
            temperature=0.0,
            json_object=True,
        )
    )

    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert properties["contract"] == {
        "type": "string",
        "enum": ["candidate-external-proposition-inventory.3"],
    }
    propositions = properties["propositions"]
    assert isinstance(propositions, dict)
    assert propositions["maxItems"] == 32
    item = propositions["items"]
    assert isinstance(item, dict)
    item_properties = item["properties"]
    assert isinstance(item_properties, dict)
    assert item_properties["semantic_role"] == {
        "type": "string",
        "enum": [
            "outer_private_state",
            "embedded_external_proposition",
            "standalone_external_proposition",
            "nonassertive_content",
        ],
    }
    assert item_properties["parent_index"] == {
        "anyOf": [
            {"type": "integer", "minimum": 0, "maximum": 31},
            {"type": "null"},
        ]
    }
    locator = item_properties["locator"]
    assert isinstance(locator, dict)
    locator_properties = locator["properties"]
    assert isinstance(locator_properties, dict)
    assert locator_properties["beat_index"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 15,
    }
    assert set(locator["required"]) == {
        "beat_index",
        "char_start",
        "char_end",
        "text",
    }


def test_source_closure_appeal_schema_cannot_add_review_fields() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("source-closure-appeal.4"),
            temperature=0.0,
            json_object=True,
        )
    )

    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["ci", "v", "p", "r"]


def test_life_source_closure_schema_matches_the_parser_wire() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("life-development-source-closure-review.1"),
            temperature=0.0,
            json_object=True,
        )
    )

    assert schema["type"] == "object"
    assert "anyOf" not in schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["review"]
    review = properties["review"]
    assert isinstance(review, dict)
    alternatives = review["anyOf"]
    assert isinstance(alternatives, list)
    assert len(alternatives) == 5

    supported = alternatives[0]
    assert isinstance(supported, dict)
    supported_properties = supported["properties"]
    assert supported_properties["decision"] == {
        "enum": ["supported"],
        "type": "string",
    }
    for field in (
        "unsupported_claim_ids",
        "undeclared_fact_fragments",
        "undeclared_fact_paths",
        "typed_location_conflicts",
    ):
        assert supported_properties[field]["maxItems"] == 0

    rejection_fields = (
        "unsupported_claim_ids",
        "undeclared_fact_fragments",
        "undeclared_fact_paths",
        "typed_location_conflicts",
    )
    for variant, required_coordinate in zip(
        alternatives[1:], rejection_fields, strict=True
    ):
        assert isinstance(variant, dict)
        variant_properties = variant["properties"]
        assert variant_properties["decision"] == {
            "enum": ["unsupported"],
            "type": "string",
        }
        assert variant_properties[required_coordinate]["minItems"] == 1


def test_life_novel_origin_schema_matches_the_parser_wire() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("life-development-novel-origin-review.2"),
            temperature=0.0,
            json_object=True,
        )
    )

    assert schema["type"] == "object"
    assert "anyOf" not in schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["review"]
    review = properties["review"]
    assert isinstance(review, dict)
    alternatives = review["anyOf"]
    assert isinstance(alternatives, list)
    assert len(alternatives) == 4

    supported = alternatives[0]
    assert isinstance(supported, dict)
    supported_properties = supported["properties"]
    assert supported_properties["decision"] == {
        "enum": ["supported"],
        "type": "string",
    }
    for field in (
        "unsupported_claims",
        "unsupported_provisional_npcs",
        "unsupported_outcome_prerequisites",
    ):
        assert supported_properties[field]["maxItems"] == 0

    rejection_fields = (
        "unsupported_claims",
        "unsupported_provisional_npcs",
        "unsupported_outcome_prerequisites",
    )
    for variant, required_coordinate in zip(
        alternatives[1:], rejection_fields, strict=True
    ):
        assert isinstance(variant, dict)
        variant_properties = variant["properties"]
        assert variant_properties["decision"] == {
            "enum": ["unsupported"],
            "type": "string",
        }
        assert variant_properties[required_coordinate]["minItems"] == 1
        assert variant_properties["undeclared_premise_fragments"]["maxItems"] == 0


def test_world_author_source_rewrite_schema_preserves_no_op_and_full_proposal() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("world-author-source-rewrite.1"),
            temperature=0.0,
            json_object=True,
        )
    )

    assert schema["type"] == "object"
    assert "anyOf" not in schema
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert list(properties) == ["replacement"]
    replacement = properties["replacement"]
    assert isinstance(replacement, dict)
    alternatives = replacement["anyOf"]
    assert isinstance(alternatives, list)
    assert len(alternatives) == 2
    no_op, proposal = alternatives
    assert isinstance(no_op, dict)
    assert isinstance(proposal, dict)
    assert no_op == {
        "additionalProperties": False,
        "properties": {
            "decision": {
                "enum": ["no_op"],
                "type": "string",
            }
        },
        "required": ["decision"],
        "type": "object",
    }
    proposal_properties = proposal["properties"]
    assert isinstance(proposal_properties, dict)
    assert proposal_properties["decision"] == {
        "enum": ["propose"],
        "type": "string",
    }
    outcomes = proposal_properties["outcomes"]
    assert isinstance(outcomes, dict)
    assert outcomes["minItems"] == 2
    assert outcomes["maxItems"] == 4
    claims = proposal_properties["claim_declarations"]
    assert isinstance(claims, dict)
    assert claims["minItems"] == 1
    assert claims["maxItems"] == 24
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    visual_object = definitions["LifeDevelopmentVisualObjectDraft"]
    assert isinstance(visual_object, dict)
    visual_properties = visual_object["properties"]
    assert isinstance(visual_properties, dict)
    assert visual_properties["description"]["type"] == "string"
    assert "description" in visual_object["required"]


def test_world_author_source_rewrite_propose_repair_schema_forbids_no_op() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )

    schema = _strict_schema(
        model.request_payload(
            _contract_message("world-author-source-rewrite-propose-repair.1"),
            temperature=0.0,
            json_object=True,
        )
    )

    assert schema["type"] == "object"
    properties = schema["properties"]
    assert isinstance(properties, dict)
    replacement = properties["replacement"]
    assert isinstance(replacement, dict)
    assert "anyOf" not in replacement
    replacement_properties = replacement["properties"]
    assert isinstance(replacement_properties, dict)
    assert replacement_properties["decision"] == {
        "enum": ["propose"],
        "type": "string",
    }


@pytest.mark.asyncio
async def test_world_author_source_rewrite_preserves_the_provider_root_envelope() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"replacement":{"decision":"no_op"}}',
                        }
                    }
                ]
            },
        )

    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
        transport=httpx.MockTransport(handler),
    )

    result = await model.complete_json(
        _contract_message("world-author-source-rewrite.1"),
    )

    # External bytes stay exact for the immutable ModelResult audit.  The
    # Life parser, not the provider adapter, removes this transport-only root.
    assert result == '{"replacement":{"decision":"no_op"}}'
    schema = _strict_schema(captured_payload)
    assert "anyOf" not in schema
    assert schema["required"] == ["replacement"]
    await model.aclose()


def test_explicit_life_review_contract_selects_schema_without_prose_inference() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    messages = [
        {"role": "system", "content": "Return the supplied contract."},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "review_contract": "life-development-source-closure-review.1",
                    "output_contract": {"type": "object"},
                },
                separators=(",", ":"),
            ),
        },
    ]

    payload = model.request_payload(
        messages,
        temperature=0.0,
        json_object=True,
    )

    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == "life_development_source_closure_review_v1"


def test_unknown_contract_falls_back_to_json_object_without_prose_inference() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    messages = [
        {
            "role": "system",
            "content": ("This prose mentions source-closure-review.7 but is not a wire contract."),
        },
        {
            "role": "user",
            "content": '{"output_contract":{"contract":"future-review.1"}}',
        },
    ]

    payload = model.request_payload(
        messages,
        temperature=0.0,
        json_object=True,
    )

    assert payload["response_format"] == {"type": "json_object"}
    assert "provider" not in payload


def test_openrouter_openai_reviewer_keeps_explicit_no_reasoning() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "openai/gpt-5.6-luna",
        require_provider_parameters=True,
        reasoning_effort="none",
    )

    payload = model.request_payload(
        _contract_message("source-closure-review.7"),
        temperature=0.0,
        json_object=True,
    )

    assert payload["reasoning_effort"] == "none"
    assert payload["provider"] == {"require_parameters": True}


def test_direct_non_reasoning_reviewer_omits_unsupported_reasoning_argument() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        reasoning_effort="",
    )

    payload = model.request_payload(
        _contract_message("source-closure-review.7"),
        temperature=0.0,
        json_object=True,
    )

    assert "reasoning_effort" not in payload
    assert payload["max_completion_tokens"] == 900
    assert _strict_schema(payload)["additionalProperties"] is False


def test_direct_gpt5_reviewer_keeps_qualified_minimal_reasoning_argument() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "gpt-5-mini",
        reasoning_effort="minimal",
    )

    payload = model.request_payload(
        _contract_message("source-closure-review.7"),
        temperature=0.0,
        json_object=True,
    )

    assert payload["reasoning_effort"] == "minimal"
    assert payload["max_completion_tokens"] == 900


@pytest.mark.parametrize(
    ("contract", "schema_name"),
    [
        ("source-closure-review.7", "source_closure_review_v7"),
        (
            "candidate-external-proposition-inventory.3",
            "candidate_external_proposition_inventory_v3",
        ),
    ],
)
def test_wire_reselection_uses_the_original_explicit_contract(
    contract: str,
    schema_name: str,
) -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    messages = [
        *_contract_message(contract),
        {"role": "assistant", "content": '{"bad":"wire"}'},
        {
            "role": "user",
            "content": '{"repair":"wire_only","stable_error":{"code":"invalid"}}',
        },
    ]

    payload = model.request_payload(
        messages,
        temperature=0.0,
        json_object=True,
    )

    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == schema_name


def test_assistant_output_cannot_replace_the_requested_wire_contract() -> None:
    model = StructuredSourceReviewModel(
        "key",
        "https://api.openai.com/v1",
        "reviewer",
    )
    messages = [
        *_contract_message("source-closure-review.7"),
        {
            "role": "assistant",
            "content": (
                '{"output_contract":{"contract":"candidate-external-proposition-coverage.1"}}'
            ),
        },
        {"role": "user", "content": "Repair the same wire."},
    ]

    payload = model.request_payload(
        messages,
        temperature=0.0,
        json_object=True,
    )

    response_format = payload["response_format"]
    assert isinstance(response_format, dict)
    envelope = response_format["json_schema"]
    assert isinstance(envelope, dict)
    assert envelope["name"] == "source_closure_review_v7"


@pytest.mark.asyncio
async def test_metered_openrouter_completion_requires_structured_parameter_support() -> None:
    captured_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "review-1",
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"contract":"candidate-external-proposition-coverage.1",'
                                '"findings":[]}'
                            )
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        )

    model = StructuredSourceReviewModel(
        "key",
        "https://openrouter.ai/api/v1",
        "reviewer",
        require_provider_parameters=True,
        transport=httpx.MockTransport(handler),
    )

    text, usage = await model.complete_json_with_usage(
        _contract_message("candidate-external-proposition-coverage.1")
    )

    assert text.startswith('{"contract":"candidate-external-proposition-coverage.1"')
    assert captured_payload["provider"] == {"require_parameters": True}
    assert captured_payload["max_tokens"] == 900
    assert "max_completion_tokens" not in captured_payload
    assert "reasoning_effort" not in captured_payload
    assert _strict_schema(captured_payload)["properties"]["contract"] == {
        "type": "string",
        "enum": ["candidate-external-proposition-coverage.1"],
    }
    assert isinstance(usage, dict)
    assert usage["provider"] == "openrouter"
    await model.aclose()
