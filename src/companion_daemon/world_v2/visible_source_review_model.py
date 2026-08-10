"""Strict-tool transport for the compact visible-Beat source verdict."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

from .structured_source_review_model import StrictOutputCapabilityEvidence
from .visible_source_closure_protocol import (
    VISIBLE_SOURCE_CLOSURE_CONTRACT,
    visible_source_closure_schema,
)


_TOOL_NAME = "visible_beat_source_verdict_v1"
_DEEPSEEK_ENDPOINTS = frozenset(
    {
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
    }
)
_QUALIFIED_MODEL = "deepseek-v4-flash"


def visible_source_verdict_schema_digest() -> str:
    encoded = json.dumps(
        visible_source_closure_schema(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def audited_visible_source_verdict_capability_evidence(
    *,
    enabled: bool,
    base_url: str,
    model: str,
) -> StrictOutputCapabilityEvidence:
    """Return evidence only for the retained exact DeepSeek Flash audit.

    This proves the strict transport and compact verdict contract.  It does
    not claim independence from a Character author using the same checkpoint.
    """

    if not enabled:
        return StrictOutputCapabilityEvidence(
            status="disabled",
            evidence_source="configuration",
            reason_code="visible_source_verdict.disabled_by_configuration",
            provider="deepseek",
            model=model,
        )
    parsed = urlsplit(base_url.rstrip("/"))
    normalized_endpoint = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"
    endpoint_matches = normalized_endpoint in _DEEPSEEK_ENDPOINTS
    if not endpoint_matches:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="configuration",
            reason_code="visible_source_verdict.endpoint_capability_unverified",
            provider="deepseek",
            model=model,
        )
    if model.strip().casefold() != _QUALIFIED_MODEL:
        return StrictOutputCapabilityEvidence.unverified(
            evidence_source="release_registry",
            reason_code="visible_source_verdict.contract_reliability_unverified",
            provider="deepseek",
            model=model,
        )
    return StrictOutputCapabilityEvidence.verified(
        evidence_source="isolated_correlated_checkpoint_contract_audit",
        provider="deepseek",
        model=model,
        contracts=(VISIBLE_SOURCE_CLOSURE_CONTRACT,),
        observed_at="2026-08-10",
        evidence_revision="visible-beat-verdict-deepseek-v4-flash-20260810.3",
        audit_sample_count=100,
        audit_success_count=100,
        contract_schema_digests=(
            (VISIBLE_SOURCE_CLOSURE_CONTRACT, visible_source_verdict_schema_digest()),
        ),
    )


class VisibleSourceReviewModel:
    """One deep contract wrapper around an OpenAI-compatible tool transport.

    The wrapped transport owns HTTP and usage accounting.  This module owns the
    only tool/schema identity for the compact source role, so composition and
    callers never reproduce provider-dialect details.
    """

    VERSION = "visible-source-review-model.1"

    def __init__(
        self,
        *,
        transport_model: object,
        strict_output_capability_evidence: StrictOutputCapabilityEvidence,
        owns_transport: bool = True,
    ) -> None:
        self.transport_model = transport_model
        self.provider = str(getattr(transport_model, "provider", ""))
        self.base_url = str(getattr(transport_model, "base_url", ""))
        self.model = str(getattr(transport_model, "model", ""))
        self.usage_observer = getattr(transport_model, "usage_observer", None)
        self.owns_transport = bool(owns_transport)
        if strict_output_capability_evidence.provider.casefold() != self.provider.casefold():
            raise ValueError("visible verdict evidence provider must match transport")
        if strict_output_capability_evidence.model.casefold() != self.model.casefold():
            raise ValueError("visible verdict evidence model must match transport")
        expected = visible_source_verdict_schema_digest()
        for contract, digest in strict_output_capability_evidence.contract_schema_digests:
            if contract == VISIBLE_SOURCE_CLOSURE_CONTRACT and digest != expected:
                raise ValueError("visible verdict evidence schema digest is stale")
        self.strict_output_capability_evidence = strict_output_capability_evidence

    def supports_strict_output_contract(self, contract: str) -> bool:
        return (
            contract == VISIBLE_SOURCE_CLOSURE_CONTRACT
            and self.strict_output_capability_evidence.supports(contract)
        )

    def installs_strict_output_contract(self, contract: str) -> bool:
        return contract == VISIBLE_SOURCE_CLOSURE_CONTRACT

    @staticmethod
    def _tools() -> tuple[list[dict[str, object]], dict[str, object]]:
        tools = [
            {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": "Return exhaustive factual source verdicts for visible Beats.",
                    "strict": True,
                    "parameters": visible_source_closure_schema(),
                },
            }
        ]
        choice: dict[str, object] = {
            "type": "function",
            "function": {"name": _TOOL_NAME},
        }
        return tools, choice

    def provider_request_contract(self) -> dict[str, object]:
        """Expose the exact transport contract for local request identity."""

        tools, tool_choice = self._tools()
        return {
            "tools": deepcopy(tools),
            "tool_choice": deepcopy(tool_choice),
            "contract": VISIBLE_SOURCE_CLOSURE_CONTRACT,
            "schema_digest": visible_source_verdict_schema_digest(),
        }

    async def _call(
        self,
        method: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
    ) -> Any:
        call = getattr(self.transport_model, method, None)
        if not callable(call):
            raise TypeError(f"visible source transport does not expose {method}")
        tools, choice = self._tools()
        return await call(
            [dict(message) for message in messages],
            temperature=temperature,
            tools=deepcopy(tools),
            tool_choice=deepcopy(choice),
        )

    async def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        result = await self._call("complete_json", messages, temperature=temperature)
        if not isinstance(result, str):
            raise ValueError("visible source verdict must be text")
        return result

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> str:
        return await self.complete_json(messages, temperature=temperature)

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> tuple[str, object]:
        result = await self._call(
            "complete_json_with_usage",
            messages,
            temperature=temperature,
        )
        if not isinstance(result, tuple) or len(result) != 2 or not isinstance(result[0], str):
            raise ValueError("metered visible source verdict must be (text, usage)")
        return result

    async def complete_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> tuple[str, object]:
        return await self.complete_json_with_usage(messages, temperature=temperature)

    def wire_reselection_route(self) -> "VisibleSourceReviewModel":
        return self

    def strict_output_capability_snapshot(self) -> dict[str, object]:
        return self.strict_output_capability_evidence.health_snapshot()

    def health_snapshot(self) -> dict[str, object]:
        return {
            "contract": self.VERSION,
            "model": self.model,
            "route": self.base_url,
            "semantic_authority_relation": "correlated_same_checkpoint",
            "independent_semantic_authority": False,
            "strict_output": self.strict_output_capability_snapshot(),
        }

    async def aclose(self) -> None:
        if not self.owns_transport:
            return
        close = getattr(self.transport_model, "aclose", None)
        if callable(close):
            await close()


__all__ = [
    "VisibleSourceReviewModel",
    "audited_visible_source_verdict_capability_evidence",
    "visible_source_verdict_schema_digest",
]
