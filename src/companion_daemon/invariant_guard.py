"""One hard-only guard between a dialogue proposal and World Action staging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from companion_daemon.world import WorldError, WorldKernel


GuardDisposition = Literal[
    "accept",
    "accept_with_local_redaction",
    "requires_action_settlement",
    "hard_reject",
]


@dataclass(frozen=True)
class GuardResolution:
    """A bounded hard-invariant verdict; it never scores conversational style."""

    disposition: GuardDisposition
    candidate: dict[str, object] | None = None
    reason: str | None = None
    action_ids: tuple[str, ...] = ()


class InvariantGuard:
    """Centralize fact and external-action authorization for reply candidates.

    Human-feel concerns deliberately do not appear here. They belong to the
    model, fallible Advisory context, and offline evaluation. The World
    validator remains the source of truth for committed facts, delivery, and
    Action references.
    """

    def resolve(
        self,
        world: WorldKernel,
        world_id: str,
        candidate: dict[str, object],
        *,
        user_id: str,
    ) -> GuardResolution:
        try:
            accepted = world.validate_reply_candidate(
                world_id, candidate, user_id=user_id
            )
        except WorldError as exc:
            return GuardResolution("hard_reject", reason=str(exc))
        proposed_actions = accepted.get("proposed_action_ids", [])
        if proposed_actions:
            try:
                action_ids = world.require_settleable_reply_actions(
                    world_id,
                    tuple(str(action_id) for action_id in proposed_actions),
                    user_id=user_id,
                )
            except WorldError as exc:
                return GuardResolution("hard_reject", reason=str(exc))
            return GuardResolution(
                "requires_action_settlement", candidate=accepted, action_ids=action_ids
            )
        return GuardResolution("accept", candidate=accepted)
