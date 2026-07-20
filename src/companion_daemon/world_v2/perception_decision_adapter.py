"""Production deliberation adapter for the optional QQ perception lane.

The perception vertical lets an injected model grammar decide whether one
inbound attachment deserves a provider look.  This adapter is that model for
the QQ deployment.  It keeps the lane deliberately restrained instead of
"analyze every image":

* deterministic deployment gates run first — only image attachments whose
  bytes are actually archived qualify, exact re-sent bytes are never analyzed
  twice, and a durable per-local-day dispatch cap bounds provider volume;
* one bounded chat-model confirmation then decides whether *this* moment in
  the conversation warrants looking, and which attachment matters most;
* every outcome is an ordinary audited proposal: a valid no-change
  ``DecisionProposal`` when she does not look, or one closed
  ``perception_request`` change whose payload and action intent bind the
  deployment budget account exactly as the perception compiler verifies.

The adapter never touches attachment bytes beyond hashing (`describe`), and
it holds no acceptance, budget, or ledger authority of its own.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import logging
from typing import Callable, Protocol

from .chat_model_deliberation_adapter import ChatCompletionModel
from .deliberation import ModelInput, ModelOutput
from .perception_input_source import PerceptionInputSource
from .perception_proposal_compiler import perception_input_ref
from .proposal_envelope import (
    CanonicalTypedPayload,
    DecisionProposal,
    ProposalActionIntent,
    TypedChange,
)


_LOG = logging.getLogger(__name__)

_DECISION_SYSTEM_PROMPT = (
    "你在替一位虚拟伴侣做一个小决定：对方刚在私聊里发来图片，"
    "她要不要现在仔细看这张图？看一次要花一点点额度，所以只在图片"
    "看起来和对话相关、或她自然会好奇时才看；纯装饰、重复刷屏、"
    "无意义的图可以不看。只输出 JSON："
    '{"look": true或false, "attachment_index": 从0开始的整数, "reason": "不超过40字"}'
)


class PerceptionDispatchEvidence(Protocol):
    """Read-only durable dispatch evidence used for deployment restraint."""

    def dispatched_count_since(self, cutoff: datetime) -> int: ...
    def has_result_for_input(self, *, input_hash: str) -> bool: ...


class QQPerceptionDecisionModel:
    """DeliberationModelAdapter for the injected perception deliberation lane."""

    def __init__(
        self,
        *,
        model: ChatCompletionModel,
        input_source: PerceptionInputSource,
        dispatch_evidence: PerceptionDispatchEvidence,
        budget_account_id: str,
        budget_limit: int,
        daily_limit: int,
        local_timezone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not budget_account_id or budget_limit <= 0 or daily_limit <= 0:
            raise ValueError("perception decision adapter needs deployment budget policy")
        self._model = model
        self._inputs = input_source
        self._evidence = dispatch_evidence
        self._budget_account_id = budget_account_id
        self._budget_limit = budget_limit
        self._daily_limit = daily_limit
        self._zone = ZoneInfo(local_timezone)
        self._now = now or (lambda: datetime.now(self._zone))

    async def propose(self, request: ModelInput) -> ModelOutput:
        trigger = request.trigger_message
        if trigger is None:
            return self._decline(request, "no trigger message")
        candidates = self._eligible_candidates(request)
        if not candidates:
            return self._decline(request, "no analyzable archived image")
        if self._daily_budget_exhausted():
            return self._decline(request, "daily perception budget reached")
        selection = await self._confirm(trigger.text, len(candidates))
        if selection is None:
            return self._decline(request, "not worth a provider look now")
        index, reason = selection
        attachment_ref = candidates[min(index, len(candidates) - 1)]
        return self._request_proposal(request, attachment_ref=attachment_ref, reason=reason)

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        # The perception lane's grammar cannot accept any recovery proposal
        # (recovery is minimal-only while minimal replies are unreachable
        # here), so a failed main attempt always terminates as no-change.
        # Returning inert output without a provider call keeps that cheap.
        del request, failure_code
        return ModelOutput(
            model_id="qq-perception-decision",
            model_version="qq-perception-decision.1",
            raw_proposal={},
        )

    # -- deterministic deployment gates ---------------------------------------

    def _eligible_candidates(self, request: ModelInput) -> tuple[str, ...]:
        trigger = request.trigger_message
        assert trigger is not None
        eligible: list[str] = []
        for ref, media_type in zip(trigger.attachment_refs, trigger.attachment_media_types):
            if media_type != "image":
                continue
            try:
                descriptor = self._inputs.describe(attachment_ref=ref, analysis_kind="vision")
            except ValueError:
                # Bytes were never archived (download failed or predates the
                # archive) or are not a supported image: nothing to perceive.
                continue
            if self._evidence.has_result_for_input(input_hash=descriptor.content_hash):
                continue
            eligible.append(ref)
        return tuple(eligible)

    def _daily_budget_exhausted(self) -> bool:
        local_now = self._now()
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            local_now = local_now.replace(tzinfo=self._zone)
        local_now = local_now.astimezone(self._zone)
        midnight = datetime(
            local_now.year, local_now.month, local_now.day, tzinfo=self._zone
        )
        try:
            return self._evidence.dispatched_count_since(midnight) >= self._daily_limit
        except Exception:  # noqa: BLE001 - evidence store failure must fail closed
            _LOG.warning("perception dispatch evidence unavailable; declining analysis")
            return True

    # -- bounded model confirmation --------------------------------------------

    async def _confirm(self, text: str | None, candidate_count: int) -> tuple[int, str] | None:
        described = text.strip()[:500] if text else "（没有文字，只发了图片）"
        messages = [
            {"role": "system", "content": _DECISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"对方这条消息的文字：{described}\n"
                    f"随消息发来的可看图片数量：{candidate_count}\n"
                    "她要现在看吗？"
                ),
            },
        ]
        try:
            complete_json = getattr(self._model, "complete_json", None)
            raw = (
                await complete_json(messages, temperature=0.2)
                if callable(complete_json)
                else await self._model.complete(messages, temperature=0.2)
            )
            decision = json.loads(_extract_json_object(raw))
        except Exception as exc:  # noqa: BLE001 - decline instead of failing the turn
            _LOG.warning("perception decision model failed (%s); declining", type(exc).__name__)
            return None
        if not isinstance(decision, dict) or decision.get("look") is not True:
            return None
        index = decision.get("attachment_index")
        index = index if isinstance(index, int) and 0 <= index < candidate_count else 0
        reason = str(decision.get("reason") or "值得看一眼")[:120]
        return index, reason

    # -- audited proposal shapes -------------------------------------------------

    def _decline(self, request: ModelInput, reason: str) -> ModelOutput:
        proposal = DecisionProposal(
            proposal_id="proposal:perception:"
            + _digest({"capsule": request.capsule_id, "attempt": request.attempt_id, "kind": "no-change"}),
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=request.trigger_evidence,
            proposed_changes=(),
            action_intents=(),
            confidence=9_000,
            brief_rationale=("She leaves this attachment unexamined: " + reason)[:240],
            behavior_tendency="maintain",
            stance="reserved",
            display_strategy="private",
        )
        return self._output(proposal)

    def _request_proposal(
        self, request: ModelInput, *, attachment_ref: str, reason: str
    ) -> ModelOutput:
        identity = {
            "capsule": request.capsule_id,
            "attempt": request.attempt_id,
            "attachment": attachment_ref,
        }
        proposal_id = "proposal:perception:" + _digest(identity)
        change_id = "change:perception:" + _digest(identity)
        change = TypedChange(
            change_id=change_id,
            kind="perception_request",
            target_id="perception:vision",
            transition="request",
            evidence_refs=tuple(ref.ref_id for ref in request.trigger_evidence),
            payload=CanonicalTypedPayload.from_value(
                payload_schema="perception_request.v1",
                value={
                    "analysis_kind": "vision",
                    "attachment_ref": attachment_ref,
                    "content_privacy_class": "private",
                    "budget_account_id": self._budget_account_id,
                    "budget_limit": self._budget_limit,
                },
            ),
        )
        proposal = DecisionProposal(
            proposal_id=proposal_id,
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=request.trigger_evidence,
            proposed_changes=(change,),
            action_intents=(
                ProposalActionIntent(
                    intent_id="intent:perception:" + _digest(identity),
                    kind="vision",
                    layer="perception_tool",
                    target="perception:vision",
                    payload_ref=perception_input_ref(
                        proposal_id=proposal_id, change_id=change_id
                    ),
                    payload_hash="sha256:"
                    + hashlib.sha256(attachment_ref.encode()).hexdigest(),
                    causal_change_id=change_id,
                ),
            ),
            confidence=7_500,
            brief_rationale=("She wants to actually see this image: " + reason)[:240],
            behavior_tendency="explore",
            stance="curious",
            display_strategy="private",
        )
        return self._output(proposal)

    @staticmethod
    def _output(proposal: DecisionProposal) -> ModelOutput:
        return ModelOutput(
            model_id="qq-perception-decision",
            model_version="qq-perception-decision.1",
            raw_proposal=proposal.model_dump(mode="json"),
        )


def _extract_json_object(raw: str) -> str:
    """Accept plain JSON or one fenced/object-embedded JSON body."""

    stripped = raw.strip()
    if stripped.startswith("{"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model decision is not a JSON object")
    return stripped[start : end + 1]


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


__all__ = ["PerceptionDispatchEvidence", "QQPerceptionDecisionModel"]
