from __future__ import annotations

from pathlib import Path

import companion_daemon.world_v2.context_capsule as context_capsule_module
from companion_daemon.world_v2.context_capsule import ContextCapsuleCompiler
from companion_daemon.world_v2.context_resolver import (
    ContextCompileQuery,
    ResolvedContextResult,
    TrustedInternalContextResolver,
    context_query_hash,
)
from test_context_capsule import _bound, _request

import pytest


class TrustedFixtureResolver(TrustedInternalContextResolver):
    def __init__(self, resolved_context) -> None:
        super().__init__()
        self.resolved_context = resolved_context
        self.result_capability = self.capability
        self.result_query_hash: str | None = None

    def resolve(self, query: ContextCompileQuery) -> ResolvedContextResult:
        return ResolvedContextResult(
            query_hash=self.result_query_hash or context_query_hash(query),
            capability=self.result_capability,
            resolved_context=self.resolved_context,
        )


def _query(**updates) -> ContextCompileQuery:
    resolved = _request()
    values = {
        field: getattr(resolved, field)
        for field in (
            "world_id",
            "snapshot_id",
            "snapshot_hash",
            "actor_ref",
            "consumer_scope",
            "trigger_ref",
            "world_revision",
            "deliberation_revision",
            "ledger_sequence",
            "logical_time",
        )
    }
    values.update(updates)
    return ContextCompileQuery(**values)


def test_production_compiler_resolves_internally_and_preserves_authoritative_empty() -> None:
    resolved = _request(action_budget=_bound(()))
    compiler = ContextCapsuleCompiler(resolver=TrustedFixtureResolver(resolved))

    capsule = compiler.compile(_query())

    assert capsule.provenance_kind == "trusted_resolver_compiled"
    assert capsule.compiler_result_hash
    assert capsule.compiler_result_tag
    assert capsule.action_budget.availability == "available"
    assert capsule.action_budget.items == ()
    assert capsule.action_budget.source_refs == ()
    assert capsule.action_budget.resolver_proof is not None
    assert capsule.current_situation.items[0].source_bindings[0].source_kind == (
        "projection_snapshot"
    )
    assert capsule.current_situation.items[0].source_bindings[0].ref == "snapshot:7"


def test_compiler_rejects_query_result_swap() -> None:
    resolver = TrustedFixtureResolver(_request(trigger_ref="event:observation:other"))
    compiler = ContextCapsuleCompiler(resolver=resolver)

    with pytest.raises(ValueError, match="does not match the compile query"):
        compiler.compile(_query())

    resolver.resolved_context = _request()
    resolver.result_query_hash = "f" * 64
    with pytest.raises(ValueError, match="another query"):
        compiler.compile(_query())


def test_compiler_rejects_capability_swap_and_untrusted_duck_resolver() -> None:
    first = TrustedFixtureResolver(_request())
    second = TrustedFixtureResolver(_request())
    first.result_capability = second.capability

    with pytest.raises(ValueError, match="wrong resolver capability"):
        ContextCapsuleCompiler(resolver=first).compile(_query())

    class DuckResolver:
        capability = object()

    with pytest.raises(ValueError, match="trusted internal capability"):
        ContextCapsuleCompiler(resolver=DuckResolver())  # type: ignore[arg-type]


def test_trusted_resolver_cannot_return_fake_result_set_proof() -> None:
    resolved = _request()
    bad_situation = resolved.situation.model_copy(
        update={
            "resolver_proof": resolved.situation.resolver_proof.model_copy(
                update={"result_set_hash": "f" * 64}
            )
        }
    )
    resolver = TrustedFixtureResolver(resolved.model_copy(update={"situation": bad_situation}))

    with pytest.raises(ValueError, match="result set hash"):
        ContextCapsuleCompiler(resolver=resolver).compile(_query())


def test_resolved_test_harness_is_not_a_public_production_api_or_src_dependency() -> None:
    assert not hasattr(context_capsule_module, "compile_resolved_for_testing")
    source_root = Path(__file__).parents[2] / "src"
    offenders = []
    for path in source_root.rglob("*.py"):
        if path.name == "context_capsule.py":
            continue
        if "_compile_resolved_context" in path.read_text():
            offenders.append(path)
    assert offenders == []
