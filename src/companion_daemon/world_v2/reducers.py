from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
import hashlib
import json
from typing import Any

from .action_lifecycle import transition_action
from .errors import UnknownEventType
from .schemas import (
    Action,
    ActionReconciliation,
    ActionState,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ExecutionReceipt,
    ExternalObservation,
    FrozenModel,
    LedgerProjection,
    TriggerProcess,
    WorldEvent,
)


REDUCER_BUNDLE_VERSION = "world-v2-reducers.1"


class RevisionClass(StrEnum):
    WORLD = "world"
    DELIBERATION = "deliberation"


class ReducerState(FrozenModel):
    observation_refs: tuple[str, ...] = ()
    logical_time: datetime | None = None
    actions: tuple[Action, ...] = ()
    budget_accounts: tuple[BudgetAccount, ...] = ()
    budget_reservations: tuple[BudgetReservation, ...] = ()
    trigger_processes: tuple[TriggerProcess, ...] = ()
    pending_external_observations: tuple[ExternalObservation, ...] = ()
    execution_receipts: tuple[ExecutionReceipt, ...] = ()
    budget_settlements: tuple[BudgetSettlement, ...] = ()
    reconciliations: tuple[ActionReconciliation, ...] = ()
    completed_trigger_ids: tuple[str, ...] = ()

    def semantic_payload(self, *, world_id: str, world_revision: int) -> dict[str, Any]:
        return {
            "reducer_bundle_version": REDUCER_BUNDLE_VERSION,
            "schema_version": "world-v2.1",
            "world_id": world_id,
            "world_revision": world_revision,
            "observation_refs": self.observation_refs,
            "logical_time": self.logical_time.isoformat() if self.logical_time else None,
            "actions": tuple(action.model_dump(mode="json") for action in self.actions),
            "budget_reservations": tuple(
                reservation.model_dump(mode="json")
                for reservation in self.budget_reservations
            ),
            "budget_accounts": tuple(
                account.model_dump(mode="json") for account in self.budget_accounts
            ),
            "execution_receipts": tuple(
                receipt.model_dump(mode="json") for receipt in self.execution_receipts
            ),
            "budget_settlements": tuple(
                settlement.model_dump(mode="json") for settlement in self.budget_settlements
            ),
            "reconciliations": tuple(
                reconciliation.model_dump(mode="json")
                for reconciliation in self.reconciliations
            ),
        }


Reducer = Callable[[ReducerState, WorldEvent], ReducerState]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    event_type: str
    revision_class: RevisionClass
    reducer: Reducer


def _audit_only(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _world_started(state: ReducerState, _event: WorldEvent) -> ReducerState:
    return state


def _observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    observation_id = event.payload().get("observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("ObservationRecorded requires observation_id")
    if observation_id in state.observation_refs:
        return state
    return state.model_copy(
        update={
            "observation_refs": (*state.observation_refs, observation_id),
            "logical_time": max(state.logical_time, event.logical_time)
            if state.logical_time is not None
            else event.logical_time,
        }
    )


def _clock_advanced(state: ReducerState, event: WorldEvent) -> ReducerState:
    logical_time_to = event.payload().get("logical_time_to")
    logical_time_from = event.payload().get("logical_time_from")
    if not isinstance(logical_time_from, str):
        raise ValueError("ClockAdvanced requires logical_time_from")
    if not isinstance(logical_time_to, str):
        raise ValueError("ClockAdvanced requires logical_time_to")
    origin = datetime.fromisoformat(logical_time_from)
    target = datetime.fromisoformat(logical_time_to)
    if target <= origin:
        raise ValueError("ClockAdvanced logical_time_to must follow logical_time_from")
    if state.logical_time is not None and origin != state.logical_time:
        raise ValueError("ClockAdvanced logical_time_from does not match current logical time")
    if state.logical_time is not None and target <= state.logical_time:
        raise ValueError("logical time cannot move backwards or remain unchanged")
    return state.model_copy(update={"logical_time": target})


def _action_authorized(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_payload = event.payload().get("action")
    action = Action.model_validate_json(
        json.dumps(action_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    if action.world_id != event.world_id:
        raise ValueError("ActionAuthorized action belongs to another world")
    if action.state != "authorized":
        raise ValueError("ActionAuthorized requires authorized state")
    if any(existing.action_id == action.action_id for existing in state.actions):
        raise ValueError(f"action {action.action_id!r} is already registered")
    if any(
        existing.idempotency_key == action.idempotency_key for existing in state.actions
    ):
        raise ValueError(f"action idempotency_key {action.idempotency_key!r} already exists")
    reservation = next(
        (
            item
            for item in state.budget_reservations
            if item.reservation_id == action.budget_reservation_id
        ),
        None,
    )
    if reservation is None or reservation.action_id != action.action_id:
        raise ValueError("ActionAuthorized requires its matching budget reservation")
    if reservation.state != "reserved":
        raise ValueError("ActionAuthorized budget reservation is not active")
    return state.model_copy(update={"actions": (*state.actions, action)})


def _budget_reserved(state: ReducerState, event: WorldEvent) -> ReducerState:
    reservation = _model_from_payload(event, "reservation", BudgetReservation)
    if any(
        item.reservation_id == reservation.reservation_id
        for item in state.budget_reservations
    ):
        raise ValueError(f"budget reservation {reservation.reservation_id!r} already exists")
    if reservation.state != "reserved":
        raise ValueError("BudgetReserved requires reserved state")
    account_index = next(
        (
            index
            for index, account in enumerate(state.budget_accounts)
            if account.account_id == reservation.account_id
        ),
        None,
    )
    if account_index is None:
        raise ValueError("BudgetReserved requires an active budget account")
    account = state.budget_accounts[account_index]
    if account.category != reservation.category:
        raise ValueError("budget reservation category does not match its account")
    if account.spent + account.reserved + reservation.amount_limit > account.limit:
        raise ValueError("budget account has insufficient available capacity")
    updated_account = account.model_copy(
        update={"reserved": account.reserved + reservation.amount_limit}
    )
    return state.model_copy(
        update={
            "budget_accounts": (
                *state.budget_accounts[:account_index],
                updated_account,
                *state.budget_accounts[account_index + 1 :],
            ),
            "budget_reservations": (*state.budget_reservations, reservation),
        }
    )


def _budget_account_configured(state: ReducerState, event: WorldEvent) -> ReducerState:
    account = _model_from_payload(event, "account", BudgetAccount)
    if any(item.account_id == account.account_id for item in state.budget_accounts):
        raise ValueError(f"budget account {account.account_id!r} already exists")
    if account.reserved != 0 or account.spent != 0 or account.overrun != 0:
        raise ValueError("new budget account must start with zero balances")
    return state.model_copy(update={"budget_accounts": (*state.budget_accounts, account)})


def _action_transitioned(
    state: ReducerState, event: WorldEvent, *, target: ActionState
) -> ReducerState:
    action_id = event.payload().get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{event.event_type} requires action_id")
    for index, existing in enumerate(state.actions):
        if existing.action_id == action_id:
            transitioned = transition_action(existing, target)
            return state.model_copy(
                update={
                    "actions": (
                        *state.actions[:index],
                        transitioned,
                        *state.actions[index + 1 :],
                    )
                }
            )
    raise ValueError(f"action {action_id!r} does not exist")


def _model_from_payload(event: WorldEvent, key: str, model_type: type[Any]) -> Any:
    value = event.payload().get(key)
    return model_type.model_validate_json(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _external_observation_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    result = _model_from_payload(event, "result", ExternalObservation)
    if any(item.result_id == result.result_id for item in state.pending_external_observations):
        raise ValueError(f"external result {result.result_id!r} is already pending")
    return state.model_copy(
        update={
            "pending_external_observations": (
                *state.pending_external_observations,
                result,
            )
        }
    )


def _external_observation_processed(state: ReducerState, event: WorldEvent) -> ReducerState:
    result_id = event.payload().get("result_id")
    if not isinstance(result_id, str) or not result_id:
        raise ValueError("ExternalObservationProcessed requires result_id")
    remaining = tuple(
        item for item in state.pending_external_observations if item.result_id != result_id
    )
    if len(remaining) == len(state.pending_external_observations):
        raise ValueError(f"external result {result_id!r} is not pending")
    return state.model_copy(update={"pending_external_observations": remaining})


def _execution_receipt_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    receipt = _model_from_payload(event, "receipt", ExecutionReceipt)
    if any(item.receipt_id == receipt.receipt_id for item in state.execution_receipts):
        raise ValueError(f"execution receipt {receipt.receipt_id!r} already exists")
    return state.model_copy(
        update={"execution_receipts": (*state.execution_receipts, receipt)}
    )


def _budget_settlement_recorded(state: ReducerState, event: WorldEvent) -> ReducerState:
    settlement = _model_from_payload(event, "settlement", BudgetSettlement)
    if any(
        item.settlement_id == settlement.settlement_id
        for item in state.budget_settlements
    ):
        raise ValueError(f"budget result {settlement.result_id!r} already exists")
    reservation_index = next(
        (
            index
            for index, item in enumerate(state.budget_reservations)
            if item.reservation_id == settlement.reservation_id
        ),
        None,
    )
    if reservation_index is None:
        raise ValueError("budget settlement requires an existing reservation")
    reservation = state.budget_reservations[reservation_index]
    if reservation.action_id != settlement.action_id:
        raise ValueError("budget reservation cannot be settled by this result")
    if settlement.previous_cost != reservation.settled_cost:
        raise ValueError("budget settlement previous_cost is stale")
    account_index = next(
        (
            index
            for index, account in enumerate(state.budget_accounts)
            if account.account_id == reservation.account_id
        ),
        None,
    )
    if account_index is None:
        raise ValueError("budget settlement account does not exist")
    account = state.budget_accounts[account_index]
    if settlement.settlement_kind == "reconciliation_adjustment":
        if reservation.state == "reserved":
            raise ValueError("budget adjustment requires an existing terminal settlement")
        reserved_after = account.reserved
    else:
        if reservation.state != "reserved":
            raise ValueError("budget reservation is already terminal")
        reserved_after = account.reserved - reservation.amount_limit
    spent_after = account.spent + settlement.cost_delta
    if reserved_after < 0 or spent_after < 0:
        raise ValueError("budget settlement would make account totals negative")
    updated_account = account.model_copy(
        update={
            "reserved": reserved_after,
            "spent": spent_after,
            "overrun": max(0, spent_after - account.limit),
        }
    )
    updated_reservation = reservation.model_copy(
        update={"state": settlement.state, "settled_cost": settlement.cost_actual}
    )
    return state.model_copy(
        update={
            "budget_accounts": (
                *state.budget_accounts[:account_index],
                updated_account,
                *state.budget_accounts[account_index + 1 :],
            ),
            "budget_reservations": (
                *state.budget_reservations[:reservation_index],
                updated_reservation,
                *state.budget_reservations[reservation_index + 1 :],
            ),
            "budget_settlements": (*state.budget_settlements, settlement),
        }
    )


def _reconciliation_required(state: ReducerState, event: WorldEvent) -> ReducerState:
    reconciliation = _model_from_payload(event, "reconciliation", ActionReconciliation)
    if any(
        item.reconciliation_id == reconciliation.reconciliation_id
        for item in state.reconciliations
    ):
        raise ValueError(f"reconciliation {reconciliation.result_id!r} already exists")
    return state.model_copy(
        update={"reconciliations": (*state.reconciliations, reconciliation)}
    )


def _trigger_process_completed(state: ReducerState, event: WorldEvent) -> ReducerState:
    trigger_id = event.payload().get("trigger_id")
    if not isinstance(trigger_id, str) or not trigger_id:
        raise ValueError("TriggerProcessCompleted requires trigger_id")
    process_index = next(
        (
            index
            for index, process in enumerate(state.trigger_processes)
            if process.trigger_id == trigger_id
        ),
        None,
    )
    if process_index is None:
        raise ValueError(f"trigger {trigger_id!r} was not claimed")
    process = state.trigger_processes[process_index]
    if process.state != "claimed":
        raise ValueError(f"trigger {trigger_id!r} is already completed")
    owner_id = event.payload().get("owner_id")
    attempt_id = event.payload().get("attempt_id")
    completed_at_raw = event.payload().get("completed_at")
    if owner_id != process.claim_lease.owner_id or attempt_id != process.claim_lease.attempt_id:
        raise ValueError("trigger completion does not own the active claim lease")
    if not isinstance(completed_at_raw, str):
        raise ValueError("TriggerProcessCompleted requires completed_at")
    completed_at = datetime.fromisoformat(completed_at_raw)
    if not (
        process.claim_lease.acquired_at
        <= completed_at
        <= process.claim_lease.expires_at
    ):
        raise ValueError("trigger completion occurred outside its claim lease")
    completed = process.model_copy(
        update={
            "state": "terminal",
            "runtime_outcome_ref": event.payload().get("runtime_outcome_ref"),
        }
    )
    return state.model_copy(
        update={
            "trigger_processes": (
                *state.trigger_processes[:process_index],
                completed,
                *state.trigger_processes[process_index + 1 :],
            ),
            "completed_trigger_ids": (*state.completed_trigger_ids, trigger_id),
        }
    )


def _trigger_process_reclaimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    replacement = _model_from_payload(event, "process", TriggerProcess)
    process_index = next(
        (
            index
            for index, process in enumerate(state.trigger_processes)
            if process.trigger_id == replacement.trigger_id
        ),
        None,
    )
    if process_index is None:
        raise ValueError("cannot reclaim an unknown trigger")
    existing = state.trigger_processes[process_index]
    if existing.state != "claimed":
        raise ValueError("cannot reclaim a terminal trigger")
    if replacement.state != "claimed":
        raise ValueError("reclaimed trigger must remain claimed")
    if (
        replacement.trigger_ref != existing.trigger_ref
        or replacement.process_kind != existing.process_kind
    ):
        raise ValueError("reclaim cannot change trigger identity")
    if replacement.claim_lease.acquired_at < existing.claim_lease.expires_at:
        raise ValueError("cannot reclaim before the active lease expires")
    if replacement.attempt_ids[:-1] != existing.attempt_ids:
        raise ValueError("reclaimed trigger must preserve attempt lineage")
    if len(replacement.attempt_ids) != len(existing.attempt_ids) + 1:
        raise ValueError("reclaim must append exactly one attempt")
    return state.model_copy(
        update={
            "trigger_processes": (
                *state.trigger_processes[:process_index],
                replacement,
                *state.trigger_processes[process_index + 1 :],
            )
        }
    )


def _trigger_process_claimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    process = _model_from_payload(event, "process", TriggerProcess)
    if process.state != "claimed":
        raise ValueError("TriggerProcessClaimed requires claimed state")
    if any(item.trigger_id == process.trigger_id for item in state.trigger_processes):
        raise ValueError(f"trigger {process.trigger_id!r} already exists")
    return state.model_copy(
        update={"trigger_processes": (*state.trigger_processes, process)}
    )


_EVENTS = {
    definition.event_type: definition
    for definition in (
        EventDefinition("WorldStarted", RevisionClass.WORLD, _world_started),
        EventDefinition("ObservationRecorded", RevisionClass.WORLD, _observation_recorded),
        EventDefinition("ClockAdvanced", RevisionClass.WORLD, _clock_advanced),
        EventDefinition(
            "ExternalObservationRecorded",
            RevisionClass.DELIBERATION,
            _external_observation_recorded,
        ),
        EventDefinition(
            "ExternalObservationProcessed",
            RevisionClass.DELIBERATION,
            _external_observation_processed,
        ),
        EventDefinition(
            "TriggerProcessClaimed",
            RevisionClass.DELIBERATION,
            _trigger_process_claimed,
        ),
        EventDefinition(
            "TriggerProcessReclaimed",
            RevisionClass.DELIBERATION,
            _trigger_process_reclaimed,
        ),
        EventDefinition(
            "BudgetAccountConfigured", RevisionClass.WORLD, _budget_account_configured
        ),
        EventDefinition("BudgetReserved", RevisionClass.WORLD, _budget_reserved),
        EventDefinition(
            "ExecutionReceiptRecorded",
            RevisionClass.WORLD,
            _execution_receipt_recorded,
        ),
        EventDefinition(
            "BudgetSettled", RevisionClass.WORLD, _budget_settlement_recorded
        ),
        EventDefinition(
            "BudgetReleased", RevisionClass.WORLD, _budget_settlement_recorded
        ),
        EventDefinition(
            "BudgetAdjusted", RevisionClass.WORLD, _budget_settlement_recorded
        ),
        EventDefinition(
            "ActionReconciliationRequired",
            RevisionClass.WORLD,
            _reconciliation_required,
        ),
        EventDefinition(
            "TriggerProcessCompleted",
            RevisionClass.DELIBERATION,
            _trigger_process_completed,
        ),
        EventDefinition("ActionAuthorized", RevisionClass.WORLD, _action_authorized),
        EventDefinition(
            "ActionScheduled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="scheduled"),
        ),
        EventDefinition(
            "ActionClaimed",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="claimed"),
        ),
        EventDefinition(
            "ActionDispatchStarted",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="dispatch_started"),
        ),
        EventDefinition(
            "ActionProviderAccepted",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="provider_accepted"),
        ),
        EventDefinition(
            "ActionDelivered",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="delivered"),
        ),
        EventDefinition(
            "ActionFailed",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="failed"),
        ),
        EventDefinition(
            "ActionUnknown",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="unknown"),
        ),
        EventDefinition(
            "ActionCancelled",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="cancelled"),
        ),
        EventDefinition(
            "ActionExpired",
            RevisionClass.WORLD,
            partial(_action_transitioned, target="expired"),
        ),
        EventDefinition("ProposalRecorded", RevisionClass.DELIBERATION, _audit_only),
        EventDefinition("AcceptanceRecorded", RevisionClass.WORLD, _audit_only),
    )
}


def event_definition(event_type: str) -> EventDefinition:
    try:
        return _EVENTS[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"event type {event_type!r} is not registered") from exc


def reduce_event(state: ReducerState, event: WorldEvent) -> ReducerState:
    return event_definition(event.event_type).reducer(state, event)


def semantic_hash(*, world_id: str, world_revision: int, state: ReducerState) -> str:
    semantic_projection = state.semantic_payload(
        world_id=world_id, world_revision=world_revision
    )
    encoded = json.dumps(
        semantic_projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def make_projection(
    *,
    world_id: str,
    world_revision: int,
    deliberation_revision: int,
    ledger_sequence: int,
    state: ReducerState,
) -> LedgerProjection:
    return LedgerProjection(
        world_id=world_id,
        world_revision=world_revision,
        deliberation_revision=deliberation_revision,
        ledger_sequence=ledger_sequence,
        logical_time=state.logical_time,
        observation_refs=state.observation_refs,
        actions=state.actions,
        budget_accounts=state.budget_accounts,
        budget_reservations=state.budget_reservations,
        trigger_processes=state.trigger_processes,
        pending_external_observations=state.pending_external_observations,
        execution_receipts=state.execution_receipts,
        budget_settlements=state.budget_settlements,
        reconciliations=state.reconciliations,
        completed_trigger_ids=state.completed_trigger_ids,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
        ),
    )
