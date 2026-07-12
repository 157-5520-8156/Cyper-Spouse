"""One hard-only guard between a dialogue proposal and World Action staging."""

from __future__ import annotations

from dataclasses import dataclass
import re
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
        semantic_error = _hard_semantic_claim_error(accepted)
        if semantic_error:
            return GuardResolution("hard_reject", reason=semantic_error)
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


def _hard_semantic_claim_error(candidate: dict[str, object]) -> str | None:
    """Reject factual, identity, and external-capability claims outside World.

    This intentionally excludes relevance, warmth, relationship wording, and
    expression quality.  Those are fallible soft concerns, not hard facts.
    """
    reply = str(candidate.get("reply_text") or "")
    claims = candidate.get("claims")
    has_grounded_claim = isinstance(claims, list) and bool(claims)
    proposed_actions = candidate.get("proposed_action_ids")
    has_proposed_action = isinstance(proposed_actions, list) and bool(proposed_actions)
    if re.search(
        r"你(?:是不是|是否|可能|大概|会不会)?[^。！？]{0,8}"
        r"(?:以前|过去|从小|一直)[^。！？]{0,18}(?:被|经历过|遇到过|受过)",
        reply,
    ) and not has_grounded_claim:
        return "unsupported_user_history_or_psychology_inference"
    if re.search(
        r"我[^。！？]{0,12}(?:没|没有|不)[^。！？]{0,8}(?:说|告诉)"
        r"[^。！？]{0,8}是因为我(?:觉得|以为|担心|怕)",
        reply,
    ) and not has_grounded_claim:
        return "uncommitted_companion_inner_reason"
    if re.search(
        r"每(?:一)?句[^。！？]{0,10}(?:都是|完全)[^。！？]{0,10}(?:我自己|自己想说)"
        r"|没有(?:谁|人)[^。！？]{0,8}(?:教|控制)"
        r"|(?:关心|在意|想回应|想陪着你)[^。！？]{0,12}"
        r"(?:不是程序|不是角色卡|不是设定|完全是我)",
        reply,
    ):
        return "absolute_meta_agency_guarantee"
    if re.search(
        r"(?:要不要)?我(?:来|可以|能|帮你|替你)[^。！？]{0,10}"
        r"(?:点单|点杯|下单|购买|支付|预订|联系|发给)",
        reply,
    ) and not has_proposed_action:
        return "external_execution_offer_without_action"
    if re.search(
        r"我[^。！？]{0,8}(?:看|读|写|做|听)[^。！？]{0,8}"
        r"(?:多了|久了|好多年|惯了)",
        reply,
    ) and not has_grounded_claim:
        return "uncommitted_accumulated_personal_experience"
    return None
