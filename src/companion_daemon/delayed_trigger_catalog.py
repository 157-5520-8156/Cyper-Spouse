"""Read-only static declarations for delayed trigger mechanisms.

This module compares versioned declarations with explicit runtime-owned names.
It does not execute a public host scenario and therefore cannot establish host
qualification or production release readiness.  A separate qualification layer
must prove those properties through the real public host.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DelayedTriggerCatalogError(ValueError):
    """The matrix is malformed or disagrees with an explicit registry."""


class DueIdentity(_FrozenModel):
    coordinates: tuple[str, ...] = Field(min_length=1)
    logical_deadline: str = Field(min_length=1)
    merge_dedup_key: str = Field(min_length=1)


class ControlledInjection(_FrozenModel):
    public_seams: tuple[str, ...] = ()
    upstream_material: tuple[str, ...] = Field(min_length=1)


class ModelContract(_FrozenModel):
    purpose: str = Field(min_length=1)
    contract_identity: str = Field(min_length=1)


class RetryPolicy(_FrozenModel):
    policy_id: str = Field(min_length=1)
    seconds: tuple[int, ...] = ()

    @model_validator(mode="after")
    def positive_monotonic_delays(self) -> RetryPolicy:
        if any(value <= 0 for value in self.seconds):
            raise ValueError("retry delays must be positive")
        if tuple(sorted(self.seconds)) != self.seconds:
            raise ValueError("retry delays must be monotonic")
        return self


class DelayedTriggerMechanism(_FrozenModel):
    mechanism_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    release_status: Literal["active", "limited", "dormant"] = Field(
        description=(
            "Declarative inventory state only; this catalog never consumes it as "
            "host qualification or production release authority."
        )
    )
    trigger_mode: Literal["clock_due", "event_triggered", "derived_formula", "replay_only"] = (
        "clock_due"
    )
    upstream_authority: tuple[str, ...] = Field(min_length=1)
    due_identity: DueIdentity
    controlled_injection: ControlledInjection
    expected_path: tuple[str, ...] = Field(min_length=1)
    legal_terminals: tuple[str, ...] = Field(min_length=1)
    visible_effect: Literal["none", "qq", "media", "conditional"]
    observability: tuple[str, ...] = Field(min_length=1)
    fault_matrix: tuple[str, ...] = Field(min_length=1)
    vertical_lanes: tuple[str, ...] = ()
    closure_mechanisms: tuple[str, ...] = Field(min_length=1)
    projection_due_fields: tuple[str, ...] = ()
    action_kinds: tuple[str, ...] = ()
    model_contract: ModelContract | None = None
    retry_policy: RetryPolicy | None = None
    activation_note: str = Field(min_length=1)


class DelayedTriggerCatalog(_FrozenModel):
    schema_version: Literal[1]
    matrix_id: str = Field(min_length=1)
    qualification_layer: Literal["declaration_only"]
    mechanisms: tuple[DelayedTriggerMechanism, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_ids(self) -> DelayedTriggerCatalog:
        identities = [row.mechanism_id for row in self.mechanisms]
        if len(identities) != len(set(identities)):
            raise ValueError("delayed trigger mechanism ids must be unique")
        return self


class _VerticalRow(Protocol):
    lane_id: str
    delayed_trigger_ids: tuple[str, ...]


def load_delayed_trigger_catalog(path: Path) -> DelayedTriggerCatalog:
    """Load and strictly validate one versioned matrix without side effects."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return DelayedTriggerCatalog.model_validate(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise DelayedTriggerCatalogError(f"invalid delayed trigger catalog: {exc}") from exc


def verify_delayed_trigger_catalog(
    catalog: DelayedTriggerCatalog,
    *,
    vertical_registry: tuple[_VerticalRow, ...],
    mechanism_rows: tuple[dict[str, Any], ...],
    owner_registry: tuple[object, ...] | None = None,
) -> None:
    """Cross-check static declarations against explicit registry metadata.

    This intentionally does not inspect Python source or infer ownership from
    field names.  Both directions are checked so an unregistered matrix row and
    an orphan registry claim fail the same declaration check.  Success does not
    mean that a public host scenario has qualified the mechanism.
    """

    errors: list[str] = []
    from .world_v2.delayed_trigger_owner_registry import (
        DELAYED_TRIGGER_OWNERS,
        INSTALLED_DELAYED_ACTION_KINDS,
        INSTALLED_PROJECTION_DUE_FIELDS,
        PUBLIC_QUALIFICATION_SEAMS,
    )

    matrix_ids = {row.mechanism_id for row in catalog.mechanisms}
    installed_owners = DELAYED_TRIGGER_OWNERS if owner_registry is None else owner_registry
    owner_ids = [str(row.mechanism_id) for row in installed_owners]
    if len(owner_ids) != len(set(owner_ids)):
        duplicates = sorted({item for item in owner_ids if owner_ids.count(item) > 1})
        raise DelayedTriggerCatalogError(
            f"duplicate delayed-trigger owner ids: {duplicates}"
        )
    owners = {str(row.mechanism_id): row for row in installed_owners}
    vertical_by_lane = {row.lane_id: row for row in vertical_registry}
    vertical_claims = {
        delayed_id
        for row in vertical_registry
        for delayed_id in row.delayed_trigger_ids
    }
    vertical_pairs = {
        (delayed_id, row.lane_id)
        for row in vertical_registry
        for delayed_id in row.delayed_trigger_ids
    }
    closure_by_id = {str(row.get("id")): row for row in mechanism_rows}
    closure_claims = {
        str(delayed_id)
        for row in mechanism_rows
        for delayed_id in row.get("delayed_trigger_ids", ())
    }
    closure_pairs = {
        (str(delayed_id), str(row.get("id")))
        for row in mechanism_rows
        for delayed_id in row.get("delayed_trigger_ids", ())
    }
    expected_vertical_pairs = {
        (row.mechanism_id, lane)
        for row in catalog.mechanisms
        for lane in row.vertical_lanes
    }
    expected_closure_pairs = {
        (row.mechanism_id, closure)
        for row in catalog.mechanisms
        for closure in row.closure_mechanisms
    }

    for row in catalog.mechanisms:
        owner = owners.get(row.mechanism_id)
        if owner is None:
            errors.append(f"{row.mechanism_id}: no explicit delayed-trigger owner")
        else:
            if row.trigger_mode != owner.trigger_mode:
                errors.append(
                    f"{row.mechanism_id}: trigger mode {row.trigger_mode!r} does not "
                    f"match owner {owner.trigger_mode!r}"
                )
            if tuple(row.controlled_injection.public_seams) != owner.public_seams:
                errors.append(
                    f"{row.mechanism_id}: declared public seams do not match "
                    f"runtime owner {owner.public_seams!r}"
                )
            if row.release_status != "dormant" and (
                not owner.runtime_owners
                or not all(callable(item) for item in owner.runtime_owners)
            ):
                errors.append(
                    f"{row.mechanism_id}: released mechanism has no callable producer/consumer owner"
                )
            unknown_fields = set(row.projection_due_fields) - INSTALLED_PROJECTION_DUE_FIELDS
            if unknown_fields:
                errors.append(
                    f"{row.mechanism_id}: unknown Projection due/expiry fields "
                    f"{sorted(unknown_fields)}"
                )
            if tuple(row.projection_due_fields) != owner.projection_due_fields:
                errors.append(
                    f"{row.mechanism_id}: Projection due/expiry fields do not match owner "
                    f"{owner.projection_due_fields!r}"
                )
            unknown_actions = set(row.action_kinds) - INSTALLED_DELAYED_ACTION_KINDS
            if unknown_actions:
                errors.append(
                    f"{row.mechanism_id}: unknown Action kinds {sorted(unknown_actions)}"
                )
            if set(row.action_kinds) != set(owner.action_kinds):
                errors.append(
                    f"{row.mechanism_id}: Action kinds do not match owner {owner.action_kinds!r}"
                )
            action_owner_kinds = tuple(kind for kind, _ in owner.action_kind_owners)
            if len(action_owner_kinds) != len(set(action_owner_kinds)) or set(
                action_owner_kinds
            ) != set(row.action_kinds):
                errors.append(
                    f"{row.mechanism_id}: Action kinds have no exact callable owner binding"
                )
            elif not all(callable(item) for _, item in owner.action_kind_owners):
                errors.append(
                    f"{row.mechanism_id}: Action kind owner binding is not callable"
                )
            actual_model = (
                (row.model_contract.purpose, row.model_contract.contract_identity)
                if row.model_contract is not None
                else None
            )
            if actual_model != owner.model_contract:
                errors.append(
                    f"{row.mechanism_id}: model contract {actual_model!r} does not match "
                    f"owner {owner.model_contract!r}"
                )
            actual_retry = (
                (row.retry_policy.policy_id, row.retry_policy.seconds)
                if row.retry_policy is not None
                else None
            )
            if actual_retry != owner.retry_policy:
                errors.append(
                    f"{row.mechanism_id}: retry policy {actual_retry!r} does not match "
                    f"owner {owner.retry_policy!r}"
                )
            if (
                row.release_status != "dormant"
                and row.trigger_mode == "clock_due"
                and not row.projection_due_fields
            ):
                errors.append(
                    f"{row.mechanism_id}: active delayed trigger has no installed due field"
                )
            if row.release_status == "dormant" and (
                row.model_contract is not None
                or row.action_kinds
                or row.retry_policy is not None
            ):
                errors.append(
                    f"{row.mechanism_id}: dormant compatibility status cannot claim live model, Action, or retry ownership"
                )
        for lane in row.vertical_lanes:
            owner = vertical_by_lane.get(lane)
            if owner is None or row.mechanism_id not in owner.delayed_trigger_ids:
                errors.append(
                    f"{row.mechanism_id}: vertical lane {lane!r} does not explicitly claim it"
                )
        for mechanism_id in row.closure_mechanisms:
            owner = closure_by_id.get(mechanism_id)
            if owner is None or row.mechanism_id not in owner.get("delayed_trigger_ids", ()):
                errors.append(
                    f"{row.mechanism_id}: mechanism closure {mechanism_id!r} does not explicitly claim it"
                )
        for seam in row.controlled_injection.public_seams:
            lowered = seam.lower()
            if (
                "drain_one" in lowered
                or "advance_once" in lowered
                or "worker" in lowered
                or seam.startswith("_")
            ):
                errors.append(
                    f"{row.mechanism_id}: {seam!r} is not a public production seam"
                )
            elif seam not in PUBLIC_QUALIFICATION_SEAMS:
                errors.append(
                    f"{row.mechanism_id}: {seam!r} has no explicit public seam owner"
                )

    for missing in sorted(expected_vertical_pairs - vertical_pairs):
        errors.append(f"{missing[0]}: missing vertical pair {missing[1]!r}")
    for extra in sorted(vertical_pairs - expected_vertical_pairs):
        errors.append(f"{extra[0]}: undeclared vertical pair {extra[1]!r}")
    for missing in sorted(expected_closure_pairs - closure_pairs):
        errors.append(f"{missing[0]}: missing mechanism closure pair {missing[1]!r}")
    for extra in sorted(closure_pairs - expected_closure_pairs):
        errors.append(f"{extra[0]}: undeclared mechanism closure pair {extra[1]!r}")
    for orphan in sorted((vertical_claims | closure_claims) - matrix_ids):
        errors.append(f"{orphan}: explicit registry claim is absent from the matrix")
    for unclaimed in sorted(matrix_ids - closure_claims):
        errors.append(f"{unclaimed}: matrix identity has no mechanism closure claim")
    for orphan in sorted(set(owners) - matrix_ids):
        errors.append(f"{orphan}: delayed-trigger owner is absent from the matrix")

    if errors:
        raise DelayedTriggerCatalogError(
            "delayed trigger static declaration drift:\n- " + "\n- ".join(errors)
        )


__all__ = [
    "ControlledInjection",
    "DelayedTriggerCatalog",
    "DelayedTriggerCatalogError",
    "DelayedTriggerMechanism",
    "DueIdentity",
    "ModelContract",
    "RetryPolicy",
    "load_delayed_trigger_catalog",
    "verify_delayed_trigger_catalog",
]
