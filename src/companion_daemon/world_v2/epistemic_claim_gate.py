"""Classify model prose that must opt in to source-bound world claims.

The gate does not decide what the companion may say.  It only closes the
structured-output loophole where factual autobiography or shared history is
written in a visible beat while ``world_claims`` is left empty.  Subjective
inner life, hypotheticals, and honest statements about missing evidence remain
claim-free so ordinary conversation does not acquire an unnecessary reviewer
call or an impossible proof burden.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


GroundedClaimScope = str

_CLAUSE_BREAK = re.compile(r"[。！？!?；;，,\n]+")
_SPEAKER = r"(?:我|我们)"
_PAST_TIME = r"(?:上次|之前|以前|后来|刚才|刚刚|今天|今晚|昨天|昨晚|周末|前几天|最近|上周|上个月)"
_SHARED_TIME = r"(?:上次|之前|以前|刚才|刚刚|那次|那会儿|那时候|那时|当时)"
_EVENT_VERB = (
    r"(?:去|逛|搜|看|听|吃|喝|买|做|见|聊|读|写|玩|跑|走|到|参加|收到|发|拍|"
    r"睡|醒|回|收拾|整理|洗|等|忙)"
)

_EPISTEMIC_NON_ASSERTION = re.compile(
    r"(?:没有|没).{0,6}(?:可确认|能确认|确认过|记录|依据|证据)"
    r"|不(?:记得|确定|清楚|知道).{0,14}(?:聊过|说过|发生|做过|去过|见过)"
)
_SUBJECTIVE_OR_HYPOTHETICAL = re.compile(
    r"(?:如果|要是|也许|可能|假如|本来想|打算|希望)"
    r"|我.{0,8}(?:觉得|感觉|在想|想起|希望|担心|在意|难过|开心|生气|失望|走神|"
    r"犹豫|介意|好奇|喜欢|讨厌|不舒服)"
)
_SHARED_HISTORY = re.compile(
    r"你.{0,10}(?:上次|之前|以前|刚才|刚刚|说过|提过|推荐过|答应过|发过|给过|聊过)"
    r"|(?:上次|之前|以前|刚才|刚刚).{0,10}你"
    r"|我们.{0,10}(?:聊过|说过|见过|去过|做过|约过|答应过)"
)
_ELLIPTICAL_SHARED_HISTORY = re.compile(
    rf"{_SHARED_TIME}.{{0,20}}(?:聊过|讨论过|说过|见过|碰到过|约过|答应过|"
    r"推荐过|提过|加过好友|发过消息)"
    r"|(?:一起|彼此|互相).{0,14}(?:聊过|讨论过|说过|见过|去过|做过|约过|碰到过)"
)
_STABLE_BACKGROUND = re.compile(
    r"(?:我家(?:里)?|我们家|家里|家人|父母|我爸|我妈|爸爸|妈妈|爷爷|奶奶|外公|外婆)"
    r".{0,24}(?:有过|卖过|买过|开过|经营过|做过|从事过|住过|搬过|种过|养过|"
    r"工作过|当过|是做|来自|祖籍|老家)"
    r"|我.{0,16}(?:小学|初中|高中|大学|本科|研究生|学校|专业|毕业|上过学|读过书|"
    r"念过书|工作过|打过工|当过|经营过|开过店|卖过|住过|搬过|出生|长大|来自)"
)
_STABLE_OR_PAST = "stable_identity_or_past_world"
_CURRENT_AUTOBIOGRAPHY = re.compile(
    rf"(?:正在|刚在).{{0,10}}{_SPEAKER}|{_SPEAKER}.{{0,10}}(?:正在|刚在)"
    rf"|(?:现在|此刻|这会儿).{{0,10}}{_SPEAKER}.{{0,8}}(?:在)?{_EVENT_VERB}"
    rf"|{_SPEAKER}.{{0,10}}(?:现在|此刻|这会儿).{{0,8}}(?:在)?{_EVENT_VERB}"
)
_PAST_AUTOBIOGRAPHY = re.compile(
    rf"{_PAST_TIME}.{{0,12}}{_SPEAKER}.{{0,8}}{_EVENT_VERB}"
    rf"|{_SPEAKER}.{{0,12}}{_PAST_TIME}.{{0,8}}{_EVENT_VERB}"
    rf"|{_SPEAKER}.{{0,10}}{_EVENT_VERB}.{{0,4}}(?:了|过)"
)
_NEAR_FUTURE_SELF_ACTIVITY = re.compile(
    r"^(?:那)?我(?:正好)?(?:也)?(?:先|待会儿?|等会儿?|一会儿?|晚点|准备|打算|想|去)?"
    r"(?:翻翻?书|看(?:一会儿?|会儿?)?书|读(?:一会儿?|会儿?)?书|洗澡|洗漱|出门|"
    r"出去|散步|跑步|运动|做饭|吃饭|睡觉|睡了|收拾(?:一下)?|整理(?:一下)?|"
    r"忙(?:一会儿?|会儿?)?)"
)


def grounded_claim_scope_evidence(
    texts: Iterable[str],
) -> dict[GroundedClaimScope, tuple[str, ...]]:
    """Map each required scope to the exact clauses that demanded it.

    The clauses make a validation failure actionable: a corrective retry can
    point the model at the specific sentence that asserts an occurrence
    instead of only naming an abstract missing scope.
    """

    evidence: dict[str, list[str]] = {}

    def _add(scope: str, clause: str) -> None:
        clauses = evidence.setdefault(scope, [])
        if clause not in clauses and len(clauses) < 4:
            clauses.append(clause)

    for text in texts:
        for raw_clause in _CLAUSE_BREAK.split(text):
            clause = raw_clause.strip()
            if not clause or _EPISTEMIC_NON_ASSERTION.search(clause):
                continue
            # Inner experience is real expression, but not an externally
            # checkable occurrence.  It intentionally remains model-owned.
            if _SUBJECTIVE_OR_HYPOTHETICAL.search(clause):
                continue
            if _STABLE_BACKGROUND.search(clause):
                _add(_STABLE_OR_PAST, clause)
                continue
            if _SHARED_HISTORY.search(clause) or _ELLIPTICAL_SHARED_HISTORY.search(clause):
                _add("shared_history", clause)
            if _CURRENT_AUTOBIOGRAPHY.search(clause):
                _add("current_world", clause)
            elif _PAST_AUTOBIOGRAPHY.search(clause):
                _add("past_world", clause)
    return {scope: tuple(clauses) for scope, clauses in evidence.items()}


def required_grounded_claim_scopes(texts: Iterable[str]) -> frozenset[GroundedClaimScope]:
    """Return source-bound scopes required by visible factual prose.

    This is deliberately a small, conservative classification matrix.  It is
    run locally on every draft; no model call is added.  The existing claim
    authority validator remains responsible for proving that declared refs
    belong to the correct Context lane.
    """

    return frozenset(grounded_claim_scope_evidence(texts))


def require_grounded_claim_declarations(
    *, texts: Iterable[str], claims: object
) -> None:
    """Fail closed when a risky prose category omits its structured source lane."""

    scope_evidence = grounded_claim_scope_evidence(texts)
    if not scope_evidence:
        return
    declared: set[str] = set()
    if isinstance(claims, list):
        for claim in claims:
            if not isinstance(claim, Mapping):
                continue
            scope = claim.get("scope")
            refs = claim.get("source_refs")
            if (
                isinstance(scope, str)
                and scope in {
                    "current_world", "past_world", "shared_history", "stable_identity"
                }
                and isinstance(refs, list)
                and bool(refs)
            ):
                declared.add(scope)
    missing = sorted(frozenset(scope_evidence) - declared)
    if _STABLE_OR_PAST in missing:
        missing.remove(_STABLE_OR_PAST)
        if not declared.intersection({"stable_identity", "past_world"}):
            missing.append("stable_identity or past_world")
    if missing:
        # Name the exact clauses so a corrective retry can either bind the
        # right Context refs or rephrase that one sentence, instead of
        # guessing which part of the reply was classified as an occurrence.
        detail = "; ".join(
            f"{scope}: " + " / ".join(
                clause[:60]
                for clause in scope_evidence.get(
                    "stable_identity_or_past_world"
                    if scope == "stable_identity or past_world"
                    else scope,
                    (),
                )
            )
            for scope in missing
        )
        raise ValueError(
            "source-bound world claim declaration missing required scope(s): "
            + ", ".join(missing)
            + (f" [offending clauses -> {detail}]" if detail.strip(" ;:/") else "")
        )


def require_structured_life_intent(
    *, texts: Iterable[str], life_intent: object
) -> None:
    """Reject visible self-actions until a reviewed intent token is installed.

    This classifier runs only on companion-visible output. It never promotes a
    user's first-person statement into a companion plan. A future structured
    lane may accept a reviewed opaque activity token here; v1 deliberately
    fails closed because free text is not plan authority.
    """

    for text in texts:
        for raw_clause in _CLAUSE_BREAK.split(text):
            clause = raw_clause.strip()
            if clause and _NEAR_FUTURE_SELF_ACTIVITY.search(clause):
                raise ValueError(
                    "first-person near-future activity requires a structured life_intent "
                    "with a reviewed activity token"
                )


__all__ = [
    "grounded_claim_scope_evidence",
    "require_grounded_claim_declarations", "require_structured_life_intent",
    "required_grounded_claim_scopes",
]
