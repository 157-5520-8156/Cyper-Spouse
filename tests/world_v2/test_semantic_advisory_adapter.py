from __future__ import annotations

import asyncio
import json

import pytest

from companion_daemon.world_v2.advisory_compiler import AdvisoryCompiler
from companion_daemon.world_v2.matrix_catalog import default_matrix_catalog
from companion_daemon.world_v2.semantic_advisory_adapter import SemanticAdvisoryAdapter
from test_advisory_compiler import AUTHORITY_KEY, _request


class _Model:
    def __init__(self, response: str, *, delay: float = 0) -> None:
        self.response = response
        self.delay = delay
        self.calls: list[tuple[list[dict[str, str]], float]] = []

    async def complete(
        self, messages: list[dict[str, str]], *, temperature: float = 0.2
    ) -> str:
        self.calls.append((messages, temperature))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


def _response() -> str:
    return json.dumps(
        {
            "classifications": [
                {
                    "field_id": "appraisal.negative",
                    "alternatives": [
                        {
                            "value": "disappointment",
                            "weight_bp": 6500,
                            "confidence_bp": 8100,
                            "source_refs": ["event:message:1"],
                            "basis": "trigger_implicit",
                        },
                        {
                            "value": "dismissal",
                            "weight_bp": 3500,
                            "confidence_bp": 5700,
                            "source_refs": ["event:message:1", "thread:life-share"],
                            "basis": "recent_context",
                        },
                    ],
                },
                {
                    "field_id": "user_affect.signal",
                    "alternatives": [
                        {
                            "value": "disappointed",
                            "weight_bp": 7200,
                            "confidence_bp": 7600,
                            "source_refs": ["event:message:1"],
                            "basis": "trigger_implicit",
                        },
                        {
                            "value": "uncertain",
                            "weight_bp": 2800,
                            "confidence_bp": 5100,
                            "source_refs": ["event:message:1"],
                            "basis": "uncertain_alternative",
                        },
                    ],
                },
                {
                    "field_id": "continuity.thread_signal",
                    "alternatives": [
                        {
                            "value": "possible_unfinished_share",
                            "weight_bp": 10_000,
                            "confidence_bp": 6900,
                            "source_refs": ["event:message:1"],
                            "basis": "recent_context",
                        }
                    ],
                },
                {
                    "field_id": "interruption.cost",
                    "alternatives": [
                        {
                            "value": "high",
                            "weight_bp": 7000,
                            "confidence_bp": 7000,
                            "source_refs": ["event:message:1"],
                            "basis": "trigger_literal",
                        }
                    ],
                },
            ]
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_semantic_adapter_returns_only_source_bound_catalog_alternatives() -> None:
    model = _Model(_response())
    catalog = default_matrix_catalog()
    adapter = SemanticAdvisoryAdapter(model=model, catalog=catalog)
    compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(adapter,),
        authority_key=AUTHORITY_KEY,
        timeout_seconds=0.1,
    )

    result = await compiler.compile(_request())

    assert result.trace[0].status == "success"
    assert [item.field_id for item in result.advisories] == [
        "appraisal.negative",
        "continuity.thread_signal",
        "interruption.cost",
        "user_affect.signal",
    ]
    alternatives = result.advisories[0].candidates
    assert [(item.value, item.weight, item.confidence) for item in alternatives] == [
        ("disappointment", 6500, 8100),
        ("dismissal", 3500, 5700),
    ]
    assert all(item.expires_at == _request().expires_at for item in alternatives)
    assert result.advisories[0].catalog_version == catalog.catalog_version
    assert model.calls[0][1] == 0.15
    system = model.calls[0][0][0]["content"]
    remote_input = model.calls[0][0][1]["content"]
    assert "Never return prose, reply text, advice, actions" in system
    assert "Preserve genuine ambiguity" in system
    assert "authentication_tag" not in remote_input
    assert "authority_hash" not in remote_input


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        "not-json",
        json.dumps(
            {
                "classifications": [
                    {
                        "field_id": "relationship.action",
                        "alternatives": [
                            {
                                "value": "repair",
                                "weight_bp": 10_000,
                                "confidence_bp": 9000,
                                "source_refs": ["event:message:1"],
                                "basis": "trigger_implicit",
                            }
                        ],
                    }
                ]
            }
        ),
        json.dumps(
            {
                "classifications": [
                    {
                        "field_id": "appraisal.base",
                        "alternatives": [
                            {
                                "value": "ordinary",
                                "weight_bp": 10_000,
                                "confidence_bp": 9000,
                                "source_refs": ["secret:unbound"],
                                "basis": "trigger_literal",
                                "reply_instruction": "apologize",
                            }
                        ],
                    }
                ]
            }
        ),
    ],
)
async def test_invalid_or_behavioural_model_json_fails_open_in_compiler(response: str) -> None:
    catalog = default_matrix_catalog()
    compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(SemanticAdvisoryAdapter(model=_Model(response), catalog=catalog),),
        authority_key=AUTHORITY_KEY,
        timeout_seconds=0.1,
    )

    result = await compiler.compile(_request())

    assert result.advisories == ()
    assert result.trace[0].status == "invalid_output"
    assert result.trace[0].error_code == "invalid_structure"


@pytest.mark.asyncio
async def test_semantic_model_timeout_is_compiler_owned_fail_open() -> None:
    catalog = default_matrix_catalog()
    compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(
            SemanticAdvisoryAdapter(model=_Model(_response(), delay=0.1), catalog=catalog),
        ),
        authority_key=AUTHORITY_KEY,
        timeout_seconds=0.01,
    )

    result = await compiler.compile(_request())

    assert result.advisories == ()
    assert result.trace[0].status == "timeout"
    assert result.trace[0].error_code == "adapter_timeout"


@pytest.mark.asyncio
async def test_relative_model_weights_are_normalized_inside_the_adapter() -> None:
    response = json.dumps(
        {
            "classifications": [
                {
                    "field_id": "appraisal.base",
                    "alternatives": [
                        {
                            "value": "uncertainty",
                            "weight_bp": 7,
                            "confidence_bp": 7000,
                            "source_refs": ["event:message:1"],
                            "basis": "trigger_implicit",
                        },
                        {
                            "value": "misunderstanding",
                            "weight_bp": 3,
                            "confidence_bp": 6000,
                            "source_refs": ["event:message:1"],
                            "basis": "uncertain_alternative",
                        },
                    ],
                }
            ]
        }
    )
    catalog = default_matrix_catalog()
    compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(SemanticAdvisoryAdapter(model=_Model(response), catalog=catalog),),
        authority_key=AUTHORITY_KEY,
        timeout_seconds=0.1,
    )

    result = await compiler.compile(_request())

    assert [item.weight for item in result.advisories[0].candidates] == [7000, 3000]


@pytest.mark.asyncio
async def test_oversized_semantic_output_fails_open_without_leaking_provider_content() -> None:
    catalog = default_matrix_catalog()
    response = json.dumps({"classifications": [], "padding": "secret" * 10_000})
    compiler = AdvisoryCompiler(
        catalog=catalog,
        adapters=(SemanticAdvisoryAdapter(model=_Model(response), catalog=catalog),),
        authority_key=AUTHORITY_KEY,
        timeout_seconds=0.1,
    )

    result = await compiler.compile(_request())

    assert result.trace[0].status == "invalid_output"
    assert "secret" not in result.model_dump_json()
