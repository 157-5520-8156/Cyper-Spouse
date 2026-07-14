from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from functools import partial
import hashlib
import json
from typing import Any

from pydantic import model_validator

from .action_lifecycle import TERMINAL_ACTION_STATES, transition_action
from .errors import UnknownEventType
from .event_catalog import event_contract
from .life_events import (
    ActivityPlannedPayload,
    ExperienceCommittedPayload,
    NpcRegisteredPayload,
    OutcomeObservationRecordedPayload,
    OutcomeProposalRecordedPayload,
    WorldOccurrenceActivatedPayload,
    WorldOccurrenceCommittedPayload,
    WorldOccurrenceSettledPayload,
)
from .life_reducers import (
    activate_occurrence,
    commit_experience,
    commit_occurrence,
    plan_activity,
    record_outcome_observation,
    record_outcome_proposal,
    register_npc,
    settle_occurrence,
)
from .schemas import (
    Action,
    ActionDispatchClaim,
    ActionReconciliation,
    ActionState,
    BudgetAccount,
    BudgetReservation,
    BudgetSettlement,
    ClaimLease,
    CommittedWorldEventRef,
    ExecutionReceipt,
    ExternalObservation,
    FrozenModel,
    ExperienceProjection,
    LedgerProjection,
    NpcProjection,
    OutcomeObservationProjection,
    OutcomeProposalProjection,
    PlanStateProjection,
    TriggerProcess,
    WorldOccurrenceProjection,
    WorldEvent,
)


REDUCER_BUNDLE_VERSION = "world-v2-reducers.3"


class RevisionClass(StrEnum):
    WORLD = "world"
    DELIBERATION = "deliberation"


class ReducerState(FrozenModel):
    observation_refs: tuple[str, ...] = ()
    committed_world_event_refs: tuple[CommittedWorldEventRef, ...] = ()
    logical_time: datetime | None = None
    actions: tuple[Action, ...] = ()
    pending_actions: tuple[Action, ...] = ()
    budget_accounts: tuple[BudgetAccount, ...] = ()
    budget_reservations: tuple[BudgetReservation, ...] = ()
    trigger_processes: tuple[TriggerProcess, ...] = ()
    pending_external_observations: tuple[ExternalObservation, ...] = ()
    execution_receipts: tuple[ExecutionReceipt, ...] = ()
    budget_settlements: tuple[BudgetSettlement, ...] = ()
    reconciliations: tuple[ActionReconciliation, ...] = ()
    completed_trigger_ids: tuple[str, ...] = ()
    npcs: tuple[NpcProjection, ...] = ()
    plans: tuple[PlanStateProjection, ...] = ()
    world_occurrences: tuple[WorldOccurrenceProjection, ...] = ()
    outcome_observations: tuple[OutcomeObservationProjection, ...] = ()
    experiences: tuple[ExperienceProjection, ...] = ()
    outcome_proposals: tuple[OutcomeProposalProjection, ...] = ()

    @model_validator(mode="after")
    def pending_index_matches_actions(self) -> ReducerState:
        expected = tuple(
            action
            for action in self.actions
            if action.state not in TERMINAL_ACTION_STATES
        )
        if self.pending_actions != expected:
            raise ValueError("pending_actions must equal the non-terminal action index")
        return self

    def semantic_payload(
        self,
        *,
        world_id: str,
        world_revision: int,
        reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
    ) -> dict[str, Any]:
        return {
            "reducer_bundle_version": reducer_bundle_version,
            "schema_version": "world-v2.1",
            "world_id": world_id,
            "world_revision": world_revision,
            "observation_refs": self.observation_refs,
            "committed_world_event_refs": tuple(
                ref.model_dump(mode="json")
                for ref in self.committed_world_event_refs
            ),
            "logical_time": self.logical_time.isoformat() if self.logical_time else None,
            "actions": tuple(action.model_dump(mode="json") for action in self.actions),
            "pending_actions": tuple(
                action.model_dump(mode="json") for action in self.pending_actions
            ),
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
            "npcs": tuple(npc.model_dump(mode="json") for npc in self.npcs),
            "plans": tuple(plan.model_dump(mode="json") for plan in self.plans),
            "world_occurrences": tuple(
                occurrence.model_dump(mode="json")
                for occurrence in self.world_occurrences
            ),
            "outcome_observations": tuple(
                observation.model_dump(mode="json")
                for observation in self.outcome_observations
            ),
            "experiences": tuple(
                experience.model_dump(mode="json")
                for experience in self.experiences
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
    return state.model_copy(
        update={
            "actions": (*state.actions, action),
            "pending_actions": (*state.pending_actions, action),
        }
    )


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
            return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _action_claimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    if "claim_lease" not in event.payload():
        raise ValueError("ActionClaimed requires claim_lease")
    lease = _model_from_payload(event, "claim_lease", ClaimLease)
    if lease.acquired_at != event.created_at:
        raise ValueError("claim lease acquired_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        transitioned = transition_action(existing, "claimed")
        transitioned = transitioned.model_copy(update={"claim_lease": lease})
        return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _action_reclaimed(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    if "claim_lease" not in event.payload():
        raise ValueError("ActionReclaimed requires claim_lease")
    lease = _model_from_payload(event, "claim_lease", ClaimLease)
    if lease.acquired_at != event.created_at:
        raise ValueError("claim lease acquired_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        if existing.state != "claimed" or existing.claim_lease is None:
            raise ValueError(f"action {action_id!r} has no reclaimable claim lease")
        if lease.attempt_id == existing.claim_lease.attempt_id:
            raise ValueError("reclaimed action requires a new attempt_id")
        if lease.acquired_at < existing.claim_lease.expires_at:
            raise ValueError(f"action {action_id!r} claim lease has not expired")
        return _replace_action(
            state,
            index=index,
            action=existing.model_copy(update={"claim_lease": lease}),
        )
    raise ValueError(f"action {action_id!r} does not exist")


def _action_dispatch_started(state: ReducerState, event: WorldEvent) -> ReducerState:
    action_id = _required_action_id(event)
    payload = event.payload()
    proof = ActionDispatchClaim.model_validate_json(
        json.dumps(
            {
                "owner_id": payload.get("owner_id"),
                "attempt_id": payload.get("attempt_id"),
                "started_at": payload.get("started_at"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if proof.started_at != event.created_at:
        raise ValueError("dispatch started_at must equal event created_at")
    for index, existing in enumerate(state.actions):
        if existing.action_id != action_id:
            continue
        lease = existing.claim_lease
        if lease is None or (lease.owner_id, lease.attempt_id) != (
            proof.owner_id,
            proof.attempt_id,
        ):
            raise ValueError("ActionDispatchStarted requires the active claim lease")
        if proof.started_at < lease.acquired_at:
            raise ValueError("dispatch cannot start before the claim lease is acquired")
        if proof.started_at >= lease.expires_at:
            raise ValueError("dispatch cannot start after the claim lease expired")
        transitioned = transition_action(existing, "dispatch_started")
        return _replace_action(state, index=index, action=transitioned)
    raise ValueError(f"action {action_id!r} does not exist")


def _required_action_id(event: WorldEvent) -> str:
    action_id = event.payload().get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise ValueError(f"{event.event_type} requires action_id")
    return action_id


def _replace_action(
    state: ReducerState, *, index: int, action: Action
) -> ReducerState:
    actions = (
        *state.actions[:index],
        action,
        *state.actions[index + 1 :],
    )
    pending = tuple(
        candidate
        for candidate in actions
        if candidate.state not in TERMINAL_ACTION_STATES
    )
    return state.model_copy(
        update={
            "actions": actions,
            "pending_actions": pending,
        }
    )


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
    if process.process_kind == "npc_world_appraisal":
        if (
            state.logical_time is None
            or event.logical_time != state.logical_time
            or process.claim_lease is None
            or process.claim_lease.acquired_at != state.logical_time
        ):
            raise ValueError("npc appraisal claim lease must start at logical time")
    existing_index = next(
        (
            index
            for index, item in enumerate(state.trigger_processes)
            if item.trigger_id == process.trigger_id
        ),
        None,
    )
    if existing_index is not None:
        existing = state.trigger_processes[existing_index]
        if existing.state != "open":
            raise ValueError(f"trigger {process.trigger_id!r} is not open")
        if (
            existing.trigger_ref != process.trigger_ref
            or existing.process_kind != process.process_kind
        ):
            raise ValueError("claim cannot change opened trigger identity")
        return state.model_copy(
            update={
                "trigger_processes": (
                    *state.trigger_processes[:existing_index],
                    process,
                    *state.trigger_processes[existing_index + 1 :],
                )
            }
        )
    if process.process_kind == "npc_world_appraisal":
        raise ValueError("npc world appraisal must be opened before it is claimed")
    return state.model_copy(
        update={"trigger_processes": (*state.trigger_processes, process)}
    )


def _trigger_process_opened(state: ReducerState, event: WorldEvent) -> ReducerState:
    process = _model_from_payload(event, "process", TriggerProcess)
    if process.state != "open":
        raise ValueError("TriggerProcessOpened requires open state")
    if any(item.trigger_id == process.trigger_id for item in state.trigger_processes):
        raise ValueError(f"trigger {process.trigger_id!r} already exists")
    return state.model_copy(
        update={"trigger_processes": (*state.trigger_processes, process)}
    )


def _life_payload(event: WorldEvent, model_type):
    return model_type.model_validate_json(event.payload_json)


def _validated_life_payload(state: ReducerState, event: WorldEvent, model_type):
    payload = _life_payload(event, model_type)
    authority = {ref.event_id: ref for ref in state.committed_world_event_refs}
    for evidence in payload.evidence_refs:
        if evidence.evidence_type not in {
            "committed_world_event",
            "settled_world_event",
        }:
            continue
        committed = authority.get(evidence.ref_id)
        if (
            committed is None
            or evidence.source_world_revision != committed.world_revision
            or evidence.immutable_hash != committed.payload_hash
        ):
            raise ValueError("world-event evidence does not resolve to ledger authority")
        if (
            evidence.evidence_type == "settled_world_event"
            and committed.event_type != "WorldOccurrenceSettled"
        ):
            raise ValueError("settled-world evidence is not a settlement event")
    return payload


def _require_life_time(state: ReducerState, event: WorldEvent) -> datetime:
    if state.logical_time is None:
        raise ValueError("lived-world mutation requires authoritative logical time")
    if event.logical_time != state.logical_time:
        raise ValueError("lived-world event must be pinned to current logical time")
    return state.logical_time


def _npc_registered(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, NpcRegisteredPayload)
    return state.model_copy(update={"npcs": register_npc(state.npcs, payload)})


def _activity_planned(state: ReducerState, event: WorldEvent) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, ActivityPlannedPayload)
    return state.model_copy(
        update={"plans": plan_activity(state.plans, state.npcs, payload)}
    )


def _world_occurrence_committed(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, WorldOccurrenceCommittedPayload)
    return state.model_copy(
        update={
            "world_occurrences": commit_occurrence(
                state.world_occurrences,
                state.npcs,
                state.plans,
                payload,
            )
        }
    )


def _world_occurrence_activated(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, WorldOccurrenceActivatedPayload)
    return state.model_copy(
        update={
            "world_occurrences": activate_occurrence(
                state.world_occurrences, payload
            )
        }
    )


def _outcome_observation_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    payload = _validated_life_payload(
        state, event, OutcomeObservationRecordedPayload
    )
    occurrences, observations = record_outcome_observation(
        state.world_occurrences,
        state.outcome_observations,
        state.committed_world_event_refs,
        payload,
        logical_time=_require_life_time(state, event),
    )
    return state.model_copy(
        update={
            "world_occurrences": occurrences,
            "outcome_observations": observations,
        }
    )


def _world_occurrence_settled(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    payload = _validated_life_payload(state, event, WorldOccurrenceSettledPayload)
    return state.model_copy(
        update={
            "world_occurrences": settle_occurrence(
                state.world_occurrences,
                state.outcome_observations,
                state.outcome_proposals,
                payload,
                logical_time=_require_life_time(state, event),
            )
        }
    )


def _outcome_proposal_recorded(
    state: ReducerState, event: WorldEvent
) -> ReducerState:
    _require_life_time(state, event)
    payload = _validated_life_payload(state, event, OutcomeProposalRecordedPayload)
    return state.model_copy(
        update={
            "outcome_proposals": record_outcome_proposal(
                state.outcome_proposals,
                payload,
            )
        }
    )


def _experience_committed(state: ReducerState, event: WorldEvent) -> ReducerState:
    payload = _validated_life_payload(state, event, ExperienceCommittedPayload)
    return state.model_copy(
        update={
            "experiences": commit_experience(
                state.experiences,
                state.world_occurrences,
                state.execution_receipts,
                payload,
                logical_time=_require_life_time(state, event),
            )
        }
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
            "TriggerProcessOpened",
            RevisionClass.DELIBERATION,
            _trigger_process_opened,
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
            _action_claimed,
        ),
        EventDefinition(
            "ActionReclaimed",
            RevisionClass.WORLD,
            _action_reclaimed,
        ),
        EventDefinition(
            "ActionDispatchStarted",
            RevisionClass.WORLD,
            _action_dispatch_started,
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
        EventDefinition("NpcRegistered", RevisionClass.WORLD, _npc_registered),
        EventDefinition("ActivityPlanned", RevisionClass.WORLD, _activity_planned),
        EventDefinition(
            "WorldOccurrenceCommitted",
            RevisionClass.WORLD,
            _world_occurrence_committed,
        ),
        EventDefinition(
            "WorldOccurrenceActivated",
            RevisionClass.WORLD,
            _world_occurrence_activated,
        ),
        EventDefinition(
            "OutcomeObservationRecorded",
            RevisionClass.WORLD,
            _outcome_observation_recorded,
        ),
        EventDefinition(
            "OutcomeProposalRecorded",
            RevisionClass.DELIBERATION,
            _outcome_proposal_recorded,
        ),
        EventDefinition(
            "WorldOccurrenceSettled",
            RevisionClass.WORLD,
            _world_occurrence_settled,
        ),
        EventDefinition(
            "ExperienceCommitted", RevisionClass.WORLD, _experience_committed
        ),
    )
}


def event_definition(event_type: str) -> EventDefinition:
    try:
        return _EVENTS[event_type]
    except KeyError as exc:
        raise UnknownEventType(f"event type {event_type!r} is not registered") from exc


def event_types() -> frozenset[str]:
    """Return reducer event types for machine contract coverage checks."""

    return frozenset(_EVENTS)


def reduce_event(state: ReducerState, event: WorldEvent) -> ReducerState:
    event_contract(event.event_type).validate_payload(event.payload())
    definition = event_definition(event.event_type)
    reduced = definition.reducer(state, event)
    if definition.revision_class is RevisionClass.WORLD:
        return reduced.model_copy(
            update={
                "committed_world_event_refs": (
                    *reduced.committed_world_event_refs,
                    CommittedWorldEventRef(
                        event_id=event.event_id,
                        event_type=event.event_type,
                        world_revision=len(reduced.committed_world_event_refs) + 1,
                        payload_hash=event.payload_hash,
                        logical_time=event.logical_time,
                    ),
                )
            }
        )
    return reduced


def require_reducer_bundle(version: str) -> None:
    """Select an installed immutable reducer artifact or fail closed."""

    if version != REDUCER_BUNDLE_VERSION:
        raise ValueError(f"reducer bundle {version!r} is not installed")


def semantic_hash(
    *,
    world_id: str,
    world_revision: int,
    state: ReducerState,
    reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
) -> str:
    require_reducer_bundle(reducer_bundle_version)
    semantic_projection = state.semantic_payload(
        world_id=world_id,
        world_revision=world_revision,
        reducer_bundle_version=reducer_bundle_version,
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
    reducer_bundle_version: str = REDUCER_BUNDLE_VERSION,
) -> LedgerProjection:
    return LedgerProjection(
        world_id=world_id,
        world_revision=world_revision,
        deliberation_revision=deliberation_revision,
        ledger_sequence=ledger_sequence,
        logical_time=state.logical_time,
        observation_refs=state.observation_refs,
        committed_world_event_refs=state.committed_world_event_refs,
        actions=state.actions,
        pending_actions=state.pending_actions,
        budget_accounts=state.budget_accounts,
        budget_reservations=state.budget_reservations,
        trigger_processes=state.trigger_processes,
        pending_external_observations=state.pending_external_observations,
        execution_receipts=state.execution_receipts,
        budget_settlements=state.budget_settlements,
        reconciliations=state.reconciliations,
        completed_trigger_ids=state.completed_trigger_ids,
        npcs=state.npcs,
        plans=state.plans,
        world_occurrences=state.world_occurrences,
        outcome_observations=state.outcome_observations,
        experiences=state.experiences,
        outcome_proposals=state.outcome_proposals,
        semantic_hash=semantic_hash(
            world_id=world_id,
            world_revision=world_revision,
            state=state,
            reducer_bundle_version=reducer_bundle_version,
        ),
    )
