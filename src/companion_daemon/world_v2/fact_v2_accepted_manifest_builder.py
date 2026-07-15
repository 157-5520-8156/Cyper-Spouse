"""ManifestBuilder for the first production Fact-v2 accepted-effect vertical.

It consumes only an opaque revalidated production-plan handle.  The builder
derives canonical manifest/effect/bundle DTOs and stores them behind another
opaque handle; it neither materializes a ``WorldEvent`` nor owns a ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from weakref import WeakKeyDictionary

from pydantic import Field, model_validator

from .accepted_effect_contracts import (
    EFFECT_AUTHORITY_VERSION,
    AcceptanceAuthorizedEffectV3,
    AcceptanceChangeAuthorityV3,
    AcceptanceManifestProposalV3,
    AcceptanceManifestV3,
    EffectAuthorityRefV3,
    canonical_acceptance_manifest_v3_hash,
)
from .fact_accepted_contracts import (
    FactCommitMaterializedPayloadV2,
    fact_commit_event_payload_hash,
)
from .event_identity import domain_idempotency_key
from .fact_v2_production_plan import (
    FactV2ProductionExecutionPlan,
    FactV2ProductionExecutionPlanHandle,
    FactV2ProductionPlanError,
    FactV2ProductionPlanIssuer,
)
from .proposal_envelope_v2 import (
    FactCommitProposalEnvelopeV2,
    canonical_full_change_authority_hash_v2,
    validate_fact_commit_proposal_v2,
)
from .schema_core import FrozenModel


FACT_V2_ACCEPTED_EVENT_TYPE = "FactCommittedV2"


class FactV2AcceptedManifestBuilderError(ValueError):
    """Stable failure at the Fact-v2 ManifestBuilder boundary."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
    )


class FactV2ProductionAcceptedBundle(FrozenModel):
    """Inspectable canonical bundle value; it has no event/write operation."""

    plan: FactV2ProductionExecutionPlan
    manifest: AcceptanceManifestV3
    effect_event_id: str = Field(min_length=1, max_length=512)
    effect_idempotency_key: str = Field(min_length=1, max_length=512)
    ordered_effect_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def bundle_binds_its_exact_manifest_and_effect(self) -> FactV2ProductionAcceptedBundle:
        if len(self.manifest.authorized_effects) != 1:
            raise ValueError("Fact v2 bundle requires exactly one effect")
        effect = self.manifest.authorized_effects[0]
        if (
            effect.event_type != FACT_V2_ACCEPTED_EVENT_TYPE
            or effect.event_id != self.effect_event_id
            or effect.payload_hash != fact_commit_event_payload_hash(self.plan.payload)
        ):
            raise ValueError("Fact v2 bundle effect does not bind its plan payload")
        if self.ordered_effect_digest != _ordered_effect_digest(self.manifest):
            raise ValueError("Fact v2 bundle effect digest is not canonical")
        if self.bundle_digest != _bundle_digest(
            plan=self.plan,
            manifest=self.manifest,
            ordered_effect_digest=self.ordered_effect_digest,
        ):
            raise ValueError("Fact v2 bundle digest is not canonical")
        return self


class FactV2ProductionAcceptedBundleHandle:
    """Opaque ManifestBuilder-issued capability for the future recorder."""

    __slots__ = ("__weakref__",)

    def __reduce__(self) -> object:
        raise TypeError("Fact v2 accepted bundle handles cannot be serialized")

    def __copy__(self) -> object:
        raise TypeError("Fact v2 accepted bundle handles cannot be copied")

    def __deepcopy__(self, memo: object) -> object:
        del memo
        raise TypeError("Fact v2 accepted bundle handles cannot be copied")


@dataclass(frozen=True, slots=True)
class _BundleMaterial:
    plan_handle: FactV2ProductionExecutionPlanHandle
    bundle: FactV2ProductionAcceptedBundle


class FactV2AcceptedManifestBuilder:
    """Rebuild canonical Fact-v2 accepted bundles from production plans only."""

    __slots__ = ("__plan_issuer", "__handles")

    def __init__(self, *, plan_issuer: FactV2ProductionPlanIssuer) -> None:
        if type(plan_issuer) is not FactV2ProductionPlanIssuer:
            raise TypeError("Fact v2 ManifestBuilder requires the exact plan issuer")
        self.__plan_issuer = plan_issuer
        self.__handles: WeakKeyDictionary[
            FactV2ProductionAcceptedBundleHandle, _BundleMaterial
        ] = WeakKeyDictionary()

    def build(
        self, *, plan_handle: FactV2ProductionExecutionPlanHandle
    ) -> FactV2ProductionAcceptedBundleHandle:
        try:
            plan = self.__plan_issuer.revalidate(handle=plan_handle)
        except FactV2ProductionPlanError as exc:
            raise FactV2AcceptedManifestBuilderError(str(exc)) from exc
        bundle = _derive_bundle(plan)
        handle = FactV2ProductionAcceptedBundleHandle()
        self.__handles[handle] = _BundleMaterial(plan_handle=plan_handle, bundle=bundle)
        return handle

    def inspect(
        self, *, handle: FactV2ProductionAcceptedBundleHandle
    ) -> FactV2ProductionAcceptedBundle:
        return self.__material(handle).bundle.model_copy(deep=True)

    def revalidate(
        self, *, handle: FactV2ProductionAcceptedBundleHandle
    ) -> FactV2ProductionAcceptedBundle:
        material = self.__material(handle)
        try:
            plan = self.__plan_issuer.revalidate(handle=material.plan_handle)
        except FactV2ProductionPlanError as exc:
            raise FactV2AcceptedManifestBuilderError(str(exc)) from exc
        rebuilt = _derive_bundle(plan)
        if rebuilt != material.bundle:
            raise FactV2AcceptedManifestBuilderError(
                "Fact v2 bundle no longer matches its production plan"
            )
        return rebuilt.model_copy(deep=True)

    def owns(self, value: object) -> bool:
        return type(value) is FactV2ProductionAcceptedBundleHandle and value in self.__handles

    def __material(self, handle: FactV2ProductionAcceptedBundleHandle) -> _BundleMaterial:
        if type(handle) is not FactV2ProductionAcceptedBundleHandle:
            raise FactV2AcceptedManifestBuilderError("Fact v2 bundle handle belongs to another builder")
        material = self.__handles.get(handle)
        if material is None:
            raise FactV2AcceptedManifestBuilderError("Fact v2 bundle handle belongs to another builder")
        return material


def _derive_bundle(plan: FactV2ProductionExecutionPlan) -> FactV2ProductionAcceptedBundle:
    if type(plan) is not FactV2ProductionExecutionPlan:
        raise FactV2AcceptedManifestBuilderError("Fact v2 bundle plan must use its exact contract")
    proposal = _proposal_from_audit(plan)
    audit = plan.proposal_audit
    envelope = plan.envelope
    if (
        plan.payload.acceptance_id != envelope.acceptance_id
        or plan.payload.proposal_id != proposal.proposal_id
        or plan.payload.evaluated_world_revision != envelope.cursor.world_revision
        or plan.payload.full_change_authority_hash == ""
    ):
        raise FactV2AcceptedManifestBuilderError("Fact v2 plan payload does not match envelope")
    changes = tuple(
        AcceptanceChangeAuthorityV3(
            change_id=change.change_id,
            kind=change.kind,
            target_id=change.target_id,
            transition=change.transition,
            expected_entity_revision=change.expected_entity_revision,
            evidence_refs=change.evidence_refs,
            preconditions=change.preconditions,
            policy_refs=change.policy_refs,
            payload_schema=change.payload.payload_schema,
            payload_version=change.payload.payload_version,
            payload_hash=change.payload.payload_hash,
            full_change_authority_hash=canonical_full_change_authority_hash_v2(change),
        )
        for change in proposal.proposed_changes
    )
    try:
        change = next(item for item in changes if item.change_id == plan.payload.change_id)
    except StopIteration as exc:
        raise FactV2AcceptedManifestBuilderError(
            "Fact v2 plan payload change is absent from its proposal"
        ) from exc
    if change.full_change_authority_hash != plan.payload.full_change_authority_hash:
        raise FactV2AcceptedManifestBuilderError("Fact v2 plan change authority is not exact")
    summary = AcceptanceManifestProposalV3(
        proposal_id=proposal.proposal_id,
        proposal_kind=proposal.proposal_kind,
        proposal_schema_registry=proposal.schema_registry_version,
        audit_contract=audit.audit_contract,
        proposal_event_ref=audit.event_ref,
        proposal_event_payload_hash=audit.event_payload_hash,
        proposal_hash=audit.proposal_hash,
        evaluated_world_revision=proposal.evaluated_world_revision,
        changes=changes,
        action_intents=(),
    )
    payload_hash = fact_commit_event_payload_hash(plan.payload)
    event_id = _effect_event_id(plan=plan, payload_hash=payload_hash, change=change)
    effect = AcceptanceAuthorizedEffectV3(
        effect_authority_version=EFFECT_AUTHORITY_VERSION,
        ordinal=0,
        role="domain_mutation",
        event_id=event_id,
        event_type=FACT_V2_ACCEPTED_EVENT_TYPE,
        payload_hash=payload_hash,
        authority_refs=(
            EffectAuthorityRefV3(
                proposal_id=summary.proposal_id,
                authority_kind="change",
                authority_id=change.change_id,
                authority_hash=change.full_change_authority_hash,
            ),
        ),
        domain_compiler_authority=plan.durable_authority,
    )
    manifest_data: dict[str, object] = {
        "manifest_version": "acceptance-manifest.3",
        "acceptance_id": envelope.acceptance_id,
        "status": "accepted",
        "evaluated_world_revision": envelope.cursor.world_revision,
        "proposals": (summary,),
        "authorized_effects": (effect,),
    }
    manifest_data["manifest_hash"] = canonical_acceptance_manifest_v3_hash(manifest_data)
    manifest = AcceptanceManifestV3.model_validate(manifest_data, strict=True)
    ordered_effect_digest = _ordered_effect_digest(manifest)
    return FactV2ProductionAcceptedBundle(
        plan=plan,
        manifest=manifest,
        effect_event_id=event_id,
        effect_idempotency_key=_effect_idempotency(plan=plan),
        ordered_effect_digest=ordered_effect_digest,
        bundle_digest=_bundle_digest(
            plan=plan, manifest=manifest, ordered_effect_digest=ordered_effect_digest
        ),
    )


def _proposal_from_audit(plan: FactV2ProductionExecutionPlan) -> FactCommitProposalEnvelopeV2:
    audit = plan.proposal_audit
    try:
        raw = json.loads(audit.proposal_json)
        if type(raw) is not dict:
            raise ValueError("proposal audit JSON must be an object")
        proposal = validate_fact_commit_proposal_v2(raw, world_id=plan.envelope.world_id)
    except Exception as exc:
        raise FactV2AcceptedManifestBuilderError("Fact v2 proposal audit is invalid") from exc
    if (
        proposal.proposal_id != audit.proposal_id
        or proposal.evaluated_world_revision != audit.evaluated_world_revision
        or plan.envelope.proposal_audit_event_ref != audit.event_ref
        or plan.envelope.proposal_audit_payload_hash != audit.event_payload_hash
        or plan.envelope.proposal_hash != audit.proposal_hash
    ):
        raise FactV2AcceptedManifestBuilderError("Fact v2 proposal audit binding is not exact")
    return proposal


def _effect_event_id(
    *,
    plan: FactV2ProductionExecutionPlan,
    payload_hash: str,
    change: AcceptanceChangeAuthorityV3,
) -> str:
    envelope = plan.envelope
    digest = hashlib.sha256(
        _canonical_json(
            {
                "contract": "fact-v2-accepted-effect-id.1",
                "world_id": envelope.world_id,
                "cursor": envelope.cursor.model_dump(mode="json"),
                "acceptance_event_id": envelope.acceptance_event_id,
                "ordinal": 0,
                "event_type": FACT_V2_ACCEPTED_EVENT_TYPE,
                "payload_hash": payload_hash,
                "proposal_id": plan.payload.proposal_id,
                "change_id": change.change_id,
                "full_change_authority_hash": change.full_change_authority_hash,
            }
        ).encode("utf-8")
    ).hexdigest()
    return f"event:accepted-effect-v3:{digest}"


def _effect_idempotency(*, plan: FactV2ProductionExecutionPlan) -> str:
    payload: FactCommitMaterializedPayloadV2 = plan.payload
    identity = domain_idempotency_key(
        event_type=FACT_V2_ACCEPTED_EVENT_TYPE,
        world_id=plan.envelope.world_id,
        payload=payload.model_dump(mode="json"),
    )
    if identity is None:
        raise FactV2AcceptedManifestBuilderError("Fact v2 event has no domain identity")
    return identity


def _ordered_effect_digest(manifest: AcceptanceManifestV3) -> str:
    return hashlib.sha256(
        _canonical_json(
            tuple(effect.model_dump(mode="json") for effect in manifest.authorized_effects)
        ).encode("utf-8")
    ).hexdigest()


def _bundle_digest(
    *,
    plan: FactV2ProductionExecutionPlan,
    manifest: AcceptanceManifestV3,
    ordered_effect_digest: str,
) -> str:
    envelope = plan.envelope
    return hashlib.sha256(
        _canonical_json(
            {
                "contract": "accepted-bundle.1",
                "world_id": envelope.world_id,
                "cursor": envelope.cursor.model_dump(mode="json"),
                "acceptance_event_id": envelope.acceptance_event_id,
                "manifest_hash": manifest.manifest_hash,
                "ordered_effect_digest": ordered_effect_digest,
                "registry_digest": plan.durable_authority.registry_digest,
            }
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "FACT_V2_ACCEPTED_EVENT_TYPE",
    "FactV2AcceptedManifestBuilder",
    "FactV2AcceptedManifestBuilderError",
    "FactV2ProductionAcceptedBundle",
    "FactV2ProductionAcceptedBundleHandle",
]
