from __future__ import annotations

from .errors import InvalidActionTransition
from .schemas import Action, ActionState

_SETTLEMENT_EVENTS: dict[ActionState, str] = {
    "provider_accepted": "ActionProviderAccepted",
    "delivered": "ActionDelivered",
    "failed": "ActionFailed",
    "unknown": "ActionUnknown",
    "cancelled": "ActionCancelled",
    "expired": "ActionExpired",
}

TERMINAL_ACTION_STATES: frozenset[ActionState] = frozenset(
    {"delivered", "failed", "unknown", "cancelled", "expired"}
)

_ALLOWED_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    "authorized": frozenset({"scheduled", "cancelled", "expired"}),
    "scheduled": frozenset({"claimed", "cancelled", "expired"}),
    "claimed": frozenset({"dispatch_started", "cancelled", "expired"}),
    "dispatch_started": frozenset(
        {"provider_accepted", "delivered", "failed", "unknown"}
    ),
    "provider_accepted": frozenset({"delivered", "failed", "unknown"}),
    "delivered": frozenset(),
    "failed": frozenset(),
    "unknown": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
}


def transition_action(action: Action, target: ActionState) -> Action:
    current = action.state
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidActionTransition(
            f"action {action.action_id!r} cannot transition from {current!r} to {target!r}"
        )
    return action.model_copy(update={"state": target})


def settlement_event_type(status: ActionState) -> str:
    try:
        return _SETTLEMENT_EVENTS[status]
    except KeyError as exc:
        raise ValueError(f"{status!r} is not an external settlement state") from exc
