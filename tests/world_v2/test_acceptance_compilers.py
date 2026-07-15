from __future__ import annotations

from datetime import UTC, datetime
import copy
import hashlib
import json
import pickle

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.acceptance_compilers import (
    ACCEPTANCE_COMPILER_REGISTRY_VERSION,
    DOMAIN_COMPILER_COVERAGE_CATALOG,
    AcceptedEffectPlanner,
    AcceptedExecutionPlan,
    AcceptanceCompilationContext,
    AcceptanceCompilerError,
    PinnedProposalAuthorityHandle,
    CompiledDomainPayload,
    DependencyDigest,
    DomainPayloadDraft,
    DomainCompilerKey,
    DomainCompilerRegistration,
    DomainCompilerRegistry,
    PlannedEffect,
    PlannedEventProvenance,
    TrustedProposalAuthorityReader,
    UnsupportedDomainMutationAdapter,
)
from companion_daemon.world_v2.acceptance_manifest import EffectAuthorityRefV2
from companion_daemon.world_v2.proposal_envelope import (
    CanonicalTypedPayload,
    CHANGE_TRANSITION_REGISTRY,
    DecisionProposal,
    PROPOSAL_SCHEMA_REGISTRY_VERSION,
    TypedChange,
)
from companion_daemon.world_v2.proposal_audit_schemas import (
    ProposalAuditProjection,
    canonical_json,
)
from companion_daemon.world_v2.schemas import (
    CommitResult,
    LedgerProjection,
    ProjectionCursor,
    WorldEvent,
)


NOW = datetime(2026, 7, 15, 10, 0, tzinfo=UTC)


def _key(
    *, kind: str = "fact_transition", transition: str = "commit"
) -> DomainCompilerKey:
    return DomainCompilerKey(
        proposal_schema_registry=PROPOSAL_SCHEMA_REGISTRY_VERSION,
        change_kind=kind,
        transition=transition,
        payload_schema=f"{kind}.v1",
        payload_version=1,
    )


def _context(
    *,
    acceptance_event_id: str = "event:acceptance:1",
    cursor: ProjectionCursor | None = None,
) -> AcceptanceCompilationContext:
    return AcceptanceCompilationContext(
        acceptance_id="acceptance:1",
        acceptance_event_id=acceptance_event_id,
        cursor=cursor
        or ProjectionCursor(world_revision=12, deliberation_revision=7, ledger_sequence=31),
        world_id="world:1",
        logical_time=NOW,
        created_at=NOW,
        actor="acceptance",
        source="world-v2",
        trace_id="trace:1",
        correlation_id="correlation:1",
    )


def _payload(index: int = 1, *, padding: int = 0) -> CompiledDomainPayload:
    payload_json = json.dumps(
        {
            "change_id": f"change:{index}",
            "evidence_refs": [
                {
                    "claim_purpose": "private_hypothesis",
                    "evidence_type": "observed_message",
                    "immutable_hash": None,
                    "ref_id": "message:1",
                    "source_world_revision": None,
                }
            ],
            "expected_entity_revision": 0,
            "npc": {
                "current_location_ref": None,
                "entity_revision": 1,
                "known_trait_refs": (["x" * padding] if padding else []),
                "npc_id": f"npc:{index}",
                "privacy_class": "private",
                "stable_identity_ref": f"identity:{index}",
                "status": "active",
            },
            "policy_refs": [],
            "transition_id": f"transition:{index}",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    registry = DomainCompilerRegistry()
    return CompiledDomainPayload(
        event_type="NpcRegistered",
        payload_json=payload_json,
        authority_refs=(
            EffectAuthorityRefV2(
                proposal_id="proposal:1",
                authority_kind="change",
                authority_id=f"change:{index}",
                authority_hash="a" * 64,
            ),
        ),
        proposal_event_ref="event:proposal:1",
        proposal_event_payload_hash="b" * 64,
        proposal_hash="sha256:" + "c" * 64,
        compiler_key=_key(),
        compiler_ref="compiler:fact.1",
        compiler_digest="1" * 64,
        reverse_verifier_ref="verifier:fact.1",
        reverse_verifier_digest="2" * 64,
        output_payload_contract_ref="payload:fact.1",
        output_payload_contract_digest="3" * 64,
        dependency_digests=(DependencyDigest(name="schema", digest="4" * 64),),
        registry_version=registry.manifest_version,
        registry_digest=registry.manifest_digest,
    )


class _Adapter:
    def __init__(
        self,
        *,
        padding: int = 0,
        reverse_error: bool = False,
        compile_error: bool = False,
        omit_defaults: bool = False,
    ) -> None:
        self.padding = padding
        self.reverse_error = reverse_error
        self.compile_error = compile_error
        self.omit_defaults = omit_defaults

    def compile(self, **kwargs: object) -> DomainPayloadDraft:
        if self.compile_error:
            raise RuntimeError("hostile compiler")
        change = kwargs.get("change")
        index = int(str(getattr(change, "change_id", "change:1")).rsplit(":", 1)[-1])
        payload = _payload(index, padding=self.padding)
        if self.omit_defaults:
            decoded = json.loads(payload.payload_json)
            npc = decoded["npc"]
            for field in ("current_location_ref", "known_trait_refs", "status"):
                npc.pop(field)
            payload_json = json.dumps(
                decoded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            return DomainPayloadDraft(
                event_type=payload.event_type, payload_json=payload_json
            )
        return DomainPayloadDraft(event_type=payload.event_type, payload_json=payload.payload_json)

    def reverse_verify(self, actual: DomainPayloadDraft, **_: object) -> None:
        if self.reverse_error:
            raise RuntimeError("hostile verifier")
        if actual.event_type != "NpcRegistered":
            raise ValueError("payload mismatch")


def _registration(**overrides: object) -> DomainCompilerRegistration:
    values: dict[str, object] = {
        "key": _key(),
        "compiler_ref": "compiler:fact.1",
        "compiler_digest": "1" * 64,
        "reverse_verifier_ref": "verifier:fact.1",
        "reverse_verifier_digest": "2" * 64,
        "output_payload_contract_ref": "payload:fact.1",
        "output_payload_contract_digest": "3" * 64,
        "dependency_digests": (DependencyDigest(name="schema", digest="4" * 64),),
        "mutation_event_types": ("FactCommitted",),
        "adapter": _Adapter(),
    }
    values.update(overrides)
    return DomainCompilerRegistration(**values)  # type: ignore[arg-type]


def _change(index: int = 1) -> TypedChange:
    def binding(ref: str) -> dict[str, object]:
        return {
            "object_ref": ref,
            "schema_version": "test.1",
            "payload_hash": "sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
        }

    def source(ref: str) -> dict[str, object]:
        return {
            "ref_id": ref,
            "source_world_revision": 12,
            "immutable_hash": "sha256:" + hashlib.sha256(ref.encode()).hexdigest(),
        }

    return TypedChange(
        change_id=f"change:{index}",
        kind="fact_transition",
        target_id="fact:1",
        expected_entity_revision=0,
        transition="commit",
        payload=CanonicalTypedPayload.from_value(
            payload_schema="fact_transition.v1",
            value={
                "before_image": None,
                "after_image": binding("fact:after"),
                "subject": "character:1",
                "predicate": "likes",
                "cardinality": "one",
                "conflict_key": "likes:tea",
                "value_hash": "sha256:" + "a" * 64,
                "assertion_binding": binding("assertion:1"),
                "anchor_evidence": [source("event:anchor")],
                "source_evidence": [source("event:source")],
                "privacy": "private",
            },
        ),
    )


def _audit(
    change: TypedChange | None = None, *, proposal_id: str = "proposal:1"
) -> ProposalAuditProjection:
    proposal = DecisionProposal(
        proposal_id=proposal_id,
        trigger_ref="trigger:1",
        evaluated_world_revision=12,
        proposed_changes=((change or _change()),),
        confidence=8000,
        brief_rationale="Bounded test proposal.",
        behavior_tendency="observe",
        stance="quiet",
        display_strategy="none",
    )
    proposal_json = canonical_json(proposal.model_dump(mode="json"))
    return ProposalAuditProjection(
        proposal_id=proposal.proposal_id,
        proposal_kind=proposal.proposal_kind,
        model_result_ref="model-result:1",
        deliberation_result_id="deliberation:1",
        model_call_id="model-call:1",
        attempt_id="attempt:1",
        capsule_id="a" * 64,
        trigger_ref=proposal.trigger_ref,
        evaluated_world_revision=12,
        proposal_json=proposal_json,
        proposal_hash=proposal.proposal_hash,
        event_ref="event:proposal:1",
        event_payload_hash="b" * 64,
    )


class _ReaderLedger:
    blocks_event_loop = False

    def __init__(self, *, include_audit: bool = True) -> None:
        base = _audit()
        payload = base.model_dump(
            mode="json", exclude={"event_ref", "event_payload_hash"}
        )
        self.event = WorldEvent.from_payload(
            schema_version="world-v2.1",
            event_id=base.event_ref,
            world_id="world:1",
            event_type="ProposalRecorded",
            logical_time=NOW,
            created_at=NOW,
            actor="test",
            source="test",
            trace_id="trace:1",
            causation_id="cause:1",
            correlation_id="correlation:1",
            idempotency_key="proposal:1",
            payload=payload,
        )
        self.audit = base.model_copy(update={"event_payload_hash": self.event.payload_hash})
        self.include_audit = include_audit
        self.head = ProjectionCursor(
            world_revision=12, deliberation_revision=7, ledger_sequence=31
        )

    @property
    def world_id(self) -> str:
        return "world:1"

    def project(self) -> LedgerProjection:
        return self.project_at(self.head)

    def project_at(self, cursor: ProjectionCursor) -> LedgerProjection:
        return LedgerProjection(
            world_id=self.world_id,
            world_revision=cursor.world_revision,
            deliberation_revision=cursor.deliberation_revision,
            ledger_sequence=cursor.ledger_sequence,
            proposal_audits=((self.audit,) if self.include_audit else ()),
            semantic_hash="s" * 64,
        )

    def lookup_event_commit(
        self, event_id: str
    ) -> tuple[WorldEvent, CommitResult] | None:
        if event_id != self.event.event_id:
            return None
        return self.event, CommitResult(
            world_revision=12,
            deliberation_revision=7,
            ledger_sequence=31,
            event_ids=(event_id,),
        )
def _test_registry(*, adapter: _Adapter | None = None) -> DomainCompilerRegistry:
    return DomainCompilerRegistry._for_test(  # noqa: SLF001
        (_registration(mutation_event_types=("NpcRegistered",), adapter=adapter or _Adapter()),)
    )


def _compiled_handle(
    index: int = 1,
    *,
    registry: DomainCompilerRegistry,
    context: AcceptanceCompilationContext | None = None,
):
    context = context or _context()
    change = _change(index)
    audit = _audit(change, proposal_id=f"proposal:{index}")
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=context.cursor, world_id=context.world_id
    )
    return registry.compile(_key(), authority=authority, change=change, context=context)


def test_default_coverage_is_complete_and_explicitly_unsupported() -> None:
    expected = {
        (kind, transition)
        for kind, transitions in CHANGE_TRANSITION_REGISTRY.items()
        for transition in transitions
    }

    assert {
        (item.key.change_kind, item.key.transition)
        for item in DOMAIN_COMPILER_COVERAGE_CATALOG
    } == expected
    assert all(item.status == "unsupported" for item in DOMAIN_COMPILER_COVERAGE_CATALOG)

    registry = DomainCompilerRegistry()
    assert registry.coverage_for(_key()).status == "unsupported"


def test_registry_distinguishes_unknown_key_and_has_deterministic_manifest() -> None:
    first = DomainCompilerRegistry()
    second = DomainCompilerRegistry()

    assert first.manifest == second.manifest
    assert first.manifest_digest == second.manifest_digest
    assert first.manifest_version == ACCEPTANCE_COMPILER_REGISTRY_VERSION

    forged = DomainCompilerKey.model_construct(
        proposal_schema_registry=PROPOSAL_SCHEMA_REGISTRY_VERSION,
        change_kind="unknown_transition",
        transition="invent",
        payload_schema="unknown_transition.v1",
        payload_version=1,
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        first.coverage_for(forged)
    assert captured.value.code == "acceptance_compiler.unknown_key"


def test_registry_rejects_duplicate_keys_and_event_ownership() -> None:
    adapter = _Adapter()
    registration = _registration(adapter=adapter)
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry._for_test((registration, registration))  # noqa: SLF001
    assert captured.value.code == "acceptance_compiler.duplicate_key"

    other = _registration(
        key=_key(kind="experience_transition", transition="commit"),
        compiler_ref="compiler:experience.1",
        adapter=adapter,
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry._for_test((registration, other))  # noqa: SLF001
    assert captured.value.code == "acceptance_compiler.duplicate_event_owner"


def test_registry_cannot_install_fail_closed_placeholder_as_supported() -> None:
    registration = _registration(
        compiler_ref="compiler:placeholder.1",
        adapter=UnsupportedDomainMutationAdapter("not implemented"),
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry._for_test((registration,))  # noqa: SLF001
    assert captured.value.code == "acceptance_compiler.invalid_registration"


def test_production_ownership_contract_rejects_fact_to_npc_mapping() -> None:
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry(
            (_registration(mutation_event_types=("NpcRegistered",)),)
        )
    assert captured.value.code == "acceptance_compiler.invalid_event_owner"


def test_registry_manifest_binds_implementation_descriptors_and_dependencies() -> None:
    first = DomainCompilerRegistry._for_test((_registration(),))  # noqa: SLF001
    changed = DomainCompilerRegistry._for_test(  # noqa: SLF001
        (_registration(compiler_digest="9" * 64),)
    )
    assert first.manifest_digest != changed.manifest_digest

    for dependencies in (
        (
            DependencyDigest(name="z", digest="1" * 64),
            DependencyDigest(name="a", digest="2" * 64),
        ),
        (
            DependencyDigest(name="same", digest="1" * 64),
            DependencyDigest(name="same", digest="2" * 64),
        ),
    ):
        with pytest.raises(AcceptanceCompilerError) as captured:
            DomainCompilerRegistry._for_test(  # noqa: SLF001
                (_registration(dependency_digests=dependencies),)
            )
        assert captured.value.code == "acceptance_compiler.invalid_registration"


@pytest.mark.parametrize(
    "event_type",
    ["NotInstalled", "AcceptanceRecorded", "ProposalRecorded", "BudgetReserved"],
)
def test_registry_rejects_unknown_reserved_deliberation_or_identityless_events(
    event_type: str,
) -> None:
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry._for_test(  # noqa: SLF001
            (_registration(mutation_event_types=(event_type,)),)
        )
    assert captured.value.code == "acceptance_compiler.invalid_event_owner"


def test_pinned_proposal_authority_handle_is_private_cursor_bound_and_nonserializable() -> None:
    audit = _audit()
    with pytest.raises(ValueError):
        PinnedProposalAuthorityHandle(audit, _context().cursor, "world:1")

    registry = _test_registry()
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=_context().cursor, world_id="world:1"
    )
    assert authority.cursor == _context().cursor
    with pytest.raises(TypeError):
        authority.__reduce__()
    with pytest.raises(TypeError):
        copy.copy(authority)
    with pytest.raises(TypeError):
        pickle.dumps(authority)


def test_trusted_reader_rejects_cross_world_future_and_missing_proposal_audits() -> None:
    ledger = _ReaderLedger()
    reader = TrustedProposalAuthorityReader(ledger=ledger)  # type: ignore[arg-type]
    handle = reader.pin(
        world_id="world:1", cursor=ledger.head, proposal_id="proposal:1"
    )
    assert reader.owns(handle)
    assert handle.audit.event_payload_hash == ledger.event.payload_hash

    for world_id, cursor, proposal_id in (
        ("world:other", ledger.head, "proposal:1"),
        (
            "world:1",
            ProjectionCursor(
                world_revision=12, deliberation_revision=8, ledger_sequence=32
            ),
            "proposal:1",
        ),
        ("world:1", ledger.head, "proposal:missing"),
    ):
        with pytest.raises(AcceptanceCompilerError) as captured:
            reader.pin(world_id=world_id, cursor=cursor, proposal_id=proposal_id)
        assert captured.value.code == "acceptance_compiler.authority_mismatch"


def test_registry_wraps_adapter_output_and_rejects_unowned_event() -> None:
    change = _change()
    audit = _audit(change)
    registry = _test_registry()
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=_context().cursor, world_id="world:1"
    )

    compiled_handle = registry.compile(
        _key(), authority=authority, change=change, context=_context()
    )
    compiled = compiled_handle.payload
    assert compiled.compiler_digest == "1" * 64
    assert compiled.registry_digest == registry.manifest_digest
    assert compiled.authority_refs[0].authority_id == change.change_id
    registry.reverse_verify(
        compiled_handle, authority=authority, change=change, context=_context()
    )
    with pytest.raises(TypeError):
        copy.copy(compiled_handle)
    with pytest.raises(TypeError):
        pickle.dumps(compiled_handle)

    wrong_registry = DomainCompilerRegistry._for_test((_registration(),))  # noqa: SLF001
    wrong_authority = wrong_registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=_context().cursor, world_id="world:1"
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        wrong_registry.compile(
            _key(), authority=wrong_authority, change=change, context=_context()
        )
    assert captured.value.code == "acceptance_compiler.invalid_output"


def test_registry_rejects_authority_from_another_deliberation_cursor() -> None:
    change = _change()
    audit = _audit(change)
    registry = _test_registry()
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit,
        cursor=ProjectionCursor(
            world_revision=12, deliberation_revision=8, ledger_sequence=32
        ),
        world_id="world:1",
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        registry.compile(_key(), authority=authority, change=change, context=_context())
    assert captured.value.code == "acceptance_compiler.authority_mismatch"


def test_adapter_reverse_failure_is_a_stable_registry_error() -> None:
    registry = _test_registry(adapter=_Adapter(reverse_error=True))
    change = _change()
    audit = _audit(change)
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=_context().cursor, world_id="world:1"
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        registry.compile(_key(), authority=authority, change=change, context=_context())
    assert captured.value.code == "acceptance_compiler.reverse_verification_failed"


@pytest.mark.parametrize(
    ("adapter", "code"),
    [
        (_Adapter(compile_error=True), "acceptance_compiler.invalid_output"),
        (_Adapter(omit_defaults=True), "acceptance_compiler.invalid_event_identity"),
    ],
)
def test_adapter_exceptions_and_noncanonical_typed_event_bytes_are_stable(
    adapter: _Adapter, code: str
) -> None:
    registry = _test_registry(adapter=adapter)
    change = _change()
    audit = _audit(change)
    authority = registry._pin_test_authority(  # noqa: SLF001
        audit=audit, cursor=_context().cursor, world_id="world:1"
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        registry.compile(_key(), authority=authority, change=change, context=_context())
    assert captured.value.code == code


@pytest.mark.parametrize("nested_kind", ["authority_ref", "dependency"])
def test_public_reverse_revalidates_hostile_nested_constructs(nested_kind: str) -> None:
    registry = _test_registry()
    handle = _compiled_handle(registry=registry)
    payload = handle.payload
    updates: dict[str, object]
    if nested_kind == "authority_ref":
        updates = {
            "authority_refs": (
                EffectAuthorityRefV2.model_construct(
                    proposal_id="proposal:1",
                    authority_kind="forged",
                    authority_id="change:1",
                    authority_hash="a" * 64,
                ),
            )
        }
    else:
        updates = {
            "dependency_digests": (
                DependencyDigest.model_construct(name="", digest="forged"),
            )
        }
    hostile = CompiledDomainPayload.model_construct(**{**payload.__dict__, **updates})
    object.__setattr__(handle, "_CompiledDomainAuthorityHandle__payload", hostile)

    with pytest.raises(AcceptanceCompilerError) as captured:
        registry.reverse_verify(handle)
    assert captured.value.code == "acceptance_compiler.invalid_output"


def test_dependency_limit_and_production_constructor_are_fail_closed() -> None:
    dependencies = tuple(
        DependencyDigest(
            name=f"dependency:{index:02}", digest=format(index % 16, "x") * 64
        )
        for index in range(17)
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry._for_test(  # noqa: SLF001
            (_registration(dependency_digests=dependencies),)
        )
    assert captured.value.code == "acceptance_compiler.invalid_registration"

    with pytest.raises(AcceptanceCompilerError) as captured:
        DomainCompilerRegistry((_registration(),))
    assert captured.value.code == "acceptance_compiler.unsupported_key"


def test_domain_compiler_key_exactly_binds_registered_schema_and_transition() -> None:
    with pytest.raises(ValidationError):
        _key(kind="fact_transition", transition="open")
    with pytest.raises(ValidationError):
        DomainCompilerKey(
            proposal_schema_registry=PROPOSAL_SCHEMA_REGISTRY_VERSION,
            change_kind="fact_transition",
            transition="commit",
            payload_schema="fact_transition.v2",
            payload_version=1,
        )


def test_compiled_domain_payload_requires_canonical_bounded_payload_and_owns_no_envelope() -> None:
    payload = _payload()
    assert payload.payload_hash == hashlib.sha256(payload.payload_json.encode()).hexdigest()
    assert not hasattr(payload, "event_id")
    assert not hasattr(payload, "provenance")

    raw = payload.model_dump()
    raw["payload_json"] = '{"z":1, "a":2}'
    with pytest.raises(ValidationError):
        CompiledDomainPayload.model_validate(raw)

    hostile = CompiledDomainPayload.model_construct(**{
        **payload.model_dump(),
        "payload_json": '{"n":' + str(1 << 4096) + "}",
    })
    registry = _test_registry()
    with pytest.raises(AcceptanceCompilerError) as captured:
        AcceptedEffectPlanner(registry=registry).plan(
            context=_context(), authorities=(hostile,)  # type: ignore[arg-type]
        )
    assert captured.value.code == "acceptance_compiler.authority_mismatch"


def test_compiled_domain_payload_rejects_invalid_unicode_and_excessive_depth() -> None:
    raw = _payload().model_dump()
    raw["payload_json"] = json.dumps({"value": "\ud800"}, ensure_ascii=False)
    with pytest.raises((ValidationError, UnicodeError)):
        CompiledDomainPayload.model_validate(raw)

    nested: object = 0
    for _ in range(40):
        nested = {"x": nested}
    raw = _payload().model_dump()
    raw["payload_json"] = json.dumps(nested, separators=(",", ":"))
    with pytest.raises(ValidationError):
        CompiledDomainPayload.model_validate(raw)


def test_effect_planner_alone_derives_order_identity_and_causation_chain() -> None:
    registry = _test_registry()
    planner = AcceptedEffectPlanner(registry=registry)
    authorities = (
        _compiled_handle(1, registry=registry),
        _compiled_handle(2, registry=registry),
    )
    plan = planner.plan(context=_context(), authorities=authorities)

    effects = plan.ordered_effects
    assert effects[0].provenance.causation_id == "event:acceptance:1"
    assert effects[1].provenance.causation_id == effects[0].event_id
    assert effects[0].event_id != effects[1].event_id
    assert effects[0].provenance.idempotency_key != effects[1].provenance.idempotency_key
    assert not hasattr(effects[0], "to_world_event")
    assert not hasattr(plan, "world_events")
    assert planner.plan(context=_context(), authorities=authorities) == plan
    assert plan.authority_scope == "test_only"
    with pytest.raises(AttributeError):
        registry._test_scope = False  # type: ignore[attr-defined]  # noqa: SLF001

    with pytest.raises(ValidationError):
        AcceptedExecutionPlan.model_validate(
            plan.model_copy(update={"authority_scope": "production"}).model_dump()
        )

    with pytest.raises(AcceptanceCompilerError) as captured:
        planner.plan(context=_context(), authorities=(authorities[0], authorities[0]))
    assert captured.value.code == "acceptance_compiler.authority_reused"

    for effect in (
        plan.ordered_effects[0].model_copy(update={"ordinal": 1}),
        plan.ordered_effects[0].model_copy(update={"event_id": "event:forged"}),
        plan.ordered_effects[0].model_copy(
            update={
                "provenance": plan.ordered_effects[0].provenance.model_copy(
                    update={"causation_id": "event:wrong"}
                )
            }
        ),
    ):
        with pytest.raises(ValidationError):
            AcceptedExecutionPlan(
                pre_world_revision=12,
                cursor=_context().cursor,
                acceptance_id="acceptance:1",
                acceptance_event_id="event:acceptance:1",
                world_id="world:1",
                trace_id="trace:1",
                correlation_id="correlation:1",
                registry_version=plan.registry_version,
                registry_digest=plan.registry_digest,
                authority_scope=plan.authority_scope,
                ordered_effects=(effect,),
            )


def test_execution_plan_revalidates_hostile_constructed_effects() -> None:
    registry = _test_registry()
    effect = AcceptedEffectPlanner(registry=registry).plan(
        context=_context(), authorities=(_compiled_handle(registry=registry),)
    ).ordered_effects[0]
    hostile_effect = PlannedEffect.model_construct(
        **{**effect.model_dump(), "event_id": ""}
    )
    with pytest.raises(ValidationError):
        AcceptedExecutionPlan(
            pre_world_revision=12,
            cursor=_context().cursor,
            acceptance_id="acceptance:1",
            acceptance_event_id="event:acceptance:1",
            world_id="world:1",
            trace_id="trace:1",
            correlation_id="correlation:1",
            registry_version=effect.registry_version,
            registry_digest=effect.registry_digest,
            authority_scope="test_only",
            ordered_effects=(hostile_effect,),
        )

    hostile_provenance = PlannedEventProvenance.model_construct(
        **{**effect.provenance.__dict__, "world_id": ""}
    )
    hostile_nested = PlannedEffect.model_construct(
        **{**effect.__dict__, "provenance": hostile_provenance}
    )
    with pytest.raises(ValidationError):
        AcceptedExecutionPlan(
            pre_world_revision=12,
            cursor=_context().cursor,
            acceptance_id="acceptance:1",
            acceptance_event_id="event:acceptance:1",
            world_id="world:1",
            trace_id="trace:1",
            correlation_id="correlation:1",
            registry_version=effect.registry_version,
            registry_digest=effect.registry_digest,
            authority_scope="test_only",
            ordered_effects=(hostile_nested,),
        )


def test_planner_identity_binds_full_cursor_and_accepts_long_event_references() -> None:
    registry = _test_registry()
    planner = AcceptedEffectPlanner(registry=registry)
    long_ref = "event:" + "x" * 700
    first_context = _context(acceptance_event_id=long_ref)
    first = planner.plan(
        context=first_context,
        authorities=(_compiled_handle(registry=registry, context=first_context),),
    )
    changed_context = _context(
        acceptance_event_id=long_ref,
        cursor=ProjectionCursor(
            world_revision=12, deliberation_revision=8, ledger_sequence=31
        ),
    )
    changed = planner.plan(
        context=changed_context,
        authorities=(_compiled_handle(registry=registry, context=changed_context),),
    )
    assert first.ordered_effects[0].provenance.causation_id == long_ref
    assert first.ordered_effects[0].event_id != changed.ordered_effects[0].event_id


def test_planner_rejects_total_payload_amplification() -> None:
    registry = _test_registry(adapter=_Adapter(padding=20_000))
    authorities = tuple(
        _compiled_handle(index, registry=registry) for index in range(1, 65)
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        AcceptedEffectPlanner(registry=registry).plan(
            context=_context(), authorities=authorities
        )
    assert captured.value.code == "acceptance_compiler.plan_limit_exceeded"


def test_planner_rejects_compiler_metadata_amplification() -> None:
    dependencies = tuple(
        DependencyDigest(name=f"{'n' * 110}:{index:02}", digest="a" * 64)
        for index in range(16)
    )
    registry = DomainCompilerRegistry._for_test(  # noqa: SLF001
        (
            _registration(
                mutation_event_types=("NpcRegistered",),
                dependency_digests=dependencies,
            ),
        )
    )
    authorities = tuple(
        _compiled_handle(index, registry=registry) for index in range(1, 65)
    )
    with pytest.raises(AcceptanceCompilerError) as captured:
        AcceptedEffectPlanner(registry=registry).plan(
            context=_context(), authorities=authorities
        )
    assert captured.value.code == "acceptance_compiler.plan_limit_exceeded"
