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


@dataclass(frozen=True)
class HardEvidenceContext:
    """Turn-local evidence needed for hard claims about the active user."""

    user_text: str = ""
    recent_user_texts: tuple[str, ...] = ()
    meta_agency_query: bool = False
    epistemic_honesty_requested: bool = False
    known_npc_interaction_required: bool = False


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
        evidence: HardEvidenceContext | None = None,
    ) -> GuardResolution:
        try:
            accepted = world.validate_reply_candidate(
                world_id, candidate, user_id=user_id
            )
        except WorldError as exc:
            return GuardResolution("hard_reject", reason=str(exc))
        semantic_error = _hard_semantic_claim_error(accepted, evidence=evidence)
        if semantic_error:
            return GuardResolution("hard_reject", reason=semantic_error)
        if evidence and evidence.known_npc_interaction_required and re.search(
            r"(?:没听过|没聊过|没有聊过|不认识|没见过|没有互动)",
            str(accepted.get("reply_text") or ""),
        ):
            return GuardResolution("hard_reject", reason="reply denies a known NPC interaction")
        if _uncommitted_companion_affect_claim(accepted):
            return GuardResolution("hard_reject", reason="uncommitted_companion_affect")
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


def _hard_semantic_claim_error(
    candidate: dict[str, object], *, evidence: HardEvidenceContext | None
) -> str | None:
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
    if evidence is None:
        return None
    if evidence.epistemic_honesty_requested and not re.search(
        r"(?:没|没有)[^。！？]{0,8}(?:依据|把握|记录|能确认)"
        r"|不知道|不确定|不(?:再|继续|会)?猜|不乱说",
        reply,
    ):
        return "explicit_epistemic_instruction_not_acknowledged"
    recent_context = "\n".join((evidence.user_text, *evidence.recent_user_texts))
    if "没怎么睡" in recent_context and any(
        marker in reply for marker in ("通宵", "彻夜", "整晚没睡", "一夜没睡")
    ):
        return "sleep_degree_escalated_beyond_user_statement"
    if (
        "为什么" in evidence.user_text
        and any(marker in evidence.user_text for marker in ("没睡", "睡不好", "失眠"))
        and has_grounded_claim
        and not re.search(
            r"不能确定|不确定|不知道|没依据|不清楚|没有能确认|不想乱说",
            reply,
        )
    ):
        return "causal_user_recall_without_uncertainty"
    if evidence.meta_agency_query and not has_grounded_claim and re.search(
        r"(?:我感觉得到|我看得出来|这说明|因为).{0,10}你(?:也|其实|一直)?"
        r"(?:很|挺)?(?:真诚|在乎|认真(?:在听)?)"
        r"|你(?:对我)?(?:也|其实|一直)?(?:很|挺)?(?:真诚|在乎|认真(?:在听)?).{0,10}"
        r"(?:我感觉得到|我看得出来)",
        reply,
    ):
        return "unsupported_user_sincerity_inference"
    if evidence.meta_agency_query and not has_grounded_claim and re.search(
        r"你(?:是不是)?[^。！？]{0,16}(?:被[^。！？]{0,8}(?:搞烦|敷衍)|"
        r"区分得出来|分得出来|认真在听)",
        reply,
    ):
        return "unsupported_user_history_or_ability_inference"
    return None


def _uncommitted_companion_affect_claim(candidate: dict[str, object]) -> bool:
    """Keep only explicit new companion-affect assertions on the hard boundary.

    A broad emotion lexicon would misread a quoted user symptom (for example
    ``我胃有点不舒服``) as the companion's feeling.  This conservative shape
    catches direct first-person/new-state assertions and leaves expression
    nuance to the Advisory/telemetry path.
    """
    reply = str(candidate.get("reply_text") or "")
    return bool(
        re.search(
            r"(?:^|[。！？])\s*(?:是有一点儿?|我(?:这会儿|刚才)?(?:有一点|有点)?|"
            r"你这么说让我有一点)(?:不舒服|介意|生气|难过|委屈|不高兴|烦|压着火|"
            r"闷着|不想理|失落|孤独|心里发紧)",
            reply,
        )
    )
