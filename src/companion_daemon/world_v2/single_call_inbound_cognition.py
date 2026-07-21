"""One provider round trip for immediate appraisal and visible expression drafts.

The Module deliberately stops at the existing deliberation seam.  It returns
two inert, independently materialized proposals: Appraisal/Affect first and
Expression second.  WorldRuntime therefore keeps its existing acceptance and
Action ordering; this module merely avoids asking the same provider to read the
same inbound evidence twice.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
from time import monotonic
from typing import Any

from .appraisal_chat_model_adapter import (
    AppraisalDraftDeliberationAdapter,
    FastAppraisalDraftDeliberationAdapter,
    _no_change_proposal,
    _proposal_from_draft as materialize_appraisal_draft,
)
from .chat_model_deliberation_adapter import (
    ChatCompletionModel,
    ChatModelDeliberationAdapter,
    CompanionIdentityFrame,
    RoutedChatModelDeliberationAdapter,
    _proposal_from_model_text as materialize_expression_draft,
    claim_repair_instruction,
    shape_repair_instruction,
)
from .deliberation import (
    ModelInput,
    ModelOutput,
    ModelUsageProvenance,
    fit_secondary_call_timeout,
)
from .expression_draft import (
    ExpressionDraftCapabilities,
    TEXT_ONLY_EXPRESSION_CAPABILITIES,
    is_world_claim_violation as _is_world_claim_violation,
)
from .immediate_emotion_gate import SemanticImmediateEmotionGate
from .model_facing_context import compact_chat_model_facing_context
from .no_world_evidence_recovery import (
    claim_free_reply_already_given,
    is_companion_world_evidence_probe,
    recent_companion_texts,
    recover_without_world_evidence,
)
from .production_reliability_metrics import (
    record_backup_recovery,
    record_claim_repair,
    record_failsafe,
    record_shape_repair,
)


_MAX_PENDING_DRAFTS = 64
_RECOVERY_MODEL_TIMEOUT_SECONDS = 2.5
# One corrective completion for a claim-bookkeeping near-miss.  A repaired
# genuine reply a few seconds late reads far more human than an instant
# canned acknowledgement, but the wait stays bounded.
_CLAIM_REPAIR_TIMEOUT_SECONDS = 8.0
_REMOTE_APPRAISAL_CUES = (
    "失望",
    "敷衍",
    "不高兴",
    "生气",
    "愤怒",
    "难过",
    "伤心",
    "委屈",
    "冒犯",
    "讨厌",
    "不想聊",
    "不想理",
    "别理我",
    "滚",
    "骗子",
    "背叛",
    "算了",
    "没认真听",
    "当我没说",
    "不舒服",
    "程序",
    "复读",
    "只会",
    "原谅",
    "信任",
    "对不起",
    "抱歉",
    "喜欢你",
    "想你",
    "在乎",
    "你还记得",
    "你是不是",
    "为什么不回",
    "怎么不回",
    "不找我",
    "不理我",
    "忽略我",
    "冷落我",
    "一直不",
    "怎么都",
)
_VISIBLE_TEXT_KEYS = ("response_text", "text", "reply", "message")
_VISIBLE_TEXT_LIST_KEYS = ("beats", "messages", "responses")
_UNSAFE_VISIBLE_KEYS = ("role", "tool", "tool_calls", "function_call", "arguments")


logger = logging.getLogger(__name__)


def _cache_key(request: ModelInput) -> tuple[str, str, str]:
    trigger = request.trigger_message
    if trigger is None:
        raise ValueError("single-call inbound cognition requires a verified current message")
    return (request.trigger_ref, trigger.observation_ref, trigger.event_payload_hash)


def _discover_recovery_model(
    *,
    flash_model: ChatCompletionModel,
    thinking_model: ChatCompletionModel | None,
) -> ChatCompletionModel | None:
    """Find the explicitly installed provider fallback without adding a route.

    ``FailoverChatModel`` exposes its secondary provider as ``fallback``.  The
    cognition module deliberately discovers only that existing seam; it never
    creates a new provider, silently upgrades a normal turn, or treats the
    primary model as its own backup.
    """

    # Recovery is latency-sensitive and follows the existing production rule:
    # fall back through Flash first, even when the failed normal route was a
    # Thinking pass.
    for candidate in (flash_model, thinking_model):
        fallback = getattr(candidate, "fallback", None)
        if fallback is None or fallback is candidate:
            continue
        if callable(getattr(fallback, "complete_json", None)) or callable(
            getattr(fallback, "complete", None)
        ):
            return fallback
    return None


# One live turn — main attempt plus its bounded recovery — comfortably fits
# in this window.  A fallback use older than this belongs to another turn.
_RECENT_FALLBACK_WINDOW_SECONDS = 30.0
_MISSING = object()


def _provider_already_used_fallback(provider: object) -> bool:
    """Avoid re-calling a FailoverChatModel's fallback in the same turn.

    The production FailoverChatModel is shared by every background cognition
    lane, so its boolean ``last_attempt_used_fallback`` can stay ``True`` for
    minutes after an unrelated lane's availability failover.  Trusting that
    stale flag here silently skipped a legitimate backup attempt and turned a
    recoverable failure into a canned failsafe (observed in production).  The
    timestamped ``last_fallback_used_at`` restricts the skip to fallback use
    recent enough to belong to the current turn; providers without the
    timestamp keep the conservative boolean semantics.
    """

    used_at = getattr(provider, "last_fallback_used_at", _MISSING)
    if used_at is _MISSING:
        return bool(getattr(provider, "last_attempt_used_fallback", False))
    return (
        isinstance(used_at, (int, float))
        and not isinstance(used_at, bool)
        and monotonic() - float(used_at) <= _RECENT_FALLBACK_WINDOW_SECONDS
    )


def _requires_remote_appraisal(text: str | None) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    normalized = "".join(text.lower().split())
    return any(cue in normalized for cue in _REMOTE_APPRAISAL_CUES)


class _PendingExpression:
    __slots__ = ("raw", "model_id", "route_tier", "usage")

    def __init__(
        self,
        *,
        raw: str,
        model_id: str,
        route_tier: str,
        usage: ModelUsageProvenance | None,
    ) -> None:
        self.raw = raw
        self.model_id = model_id
        self.route_tier = route_tier
        self.usage = usage


class _FailedExpressionDetail:
    """The exact provider conversation and violation of one structural reject.

    Retained so the post-acceptance expression pass can spend one corrective
    retry that names the concrete violation before it falls back to a local
    canned line.  This is bounded evidence for a retry, never accepted state.
    """

    __slots__ = ("messages", "raw", "violation")

    def __init__(
        self, *, messages: list[dict[str, str]], raw: str, violation: str
    ) -> None:
        self.messages = messages
        self.raw = raw
        self.violation = violation


class _BoundedKeySet:
    """Small insertion-ordered set for same-trigger recovery markers."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def add(self, key: tuple[str, str, str]) -> None:
        self._items.pop(key, None)
        self._items[key] = None
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def discard(self, key: tuple[str, str, str]) -> None:
        self._items.pop(key, None)

    def __contains__(self, key: object) -> bool:
        return key in self._items


class SingleCallAppraisalAdapter:
    """Appraisal side of the paired deliberation seam."""

    supports_immediate_emotion = True

    def __init__(self, owner: "SingleCallInboundCognition") -> None:
        self._owner = owner

    @property
    def immediate_emotion_gate(self) -> SemanticImmediateEmotionGate | None:
        """Expose the owner's semantic scheduling gate on the adapter seam.

        Production composition roots pass this adapter (not the cognition
        module) into the application builder, so the same local appraisal
        model instance can serve the same-turn scheduling decision without a
        second client or configuration lane.
        """

        return self._owner.immediate_emotion_gate

    @property
    def local_appraisal_model(self) -> ChatCompletionModel | None:
        """Expose the bounded local checkpoint for other same-turn micro-gates.

        The quick-reaction lane makes one strict-JSON classification per
        selected turn; sharing the already-owned local client avoids a second
        configuration lane exactly like ``immediate_emotion_gate`` above.
        """

        return self._owner._appraisal_model

    async def propose(self, request: ModelInput) -> ModelOutput:
        return await self._owner._propose_appraisal(request)

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        if request.trigger_message is not None:
            self._owner._pending.pop(_cache_key(request), None)
        return ModelOutput(
            model_id=self._owner._model_id_for(request),
            model_version=self._owner.VERSION,
            raw_proposal=self._owner._local_appraisal_recovery(request, failure_code),
        )


class SingleCallExpressionAdapter:
    def __init__(self, owner: "SingleCallInboundCognition") -> None:
        self._owner = owner

    def has_precomputed_advisory(
        self,
        *,
        trigger_ref: str,
        observation_ref: str,
        event_payload_hash: str,
    ) -> bool:
        pending = self._owner._pending.get((trigger_ref, observation_ref, event_payload_hash))
        # A Thinking route is itself an effect of semantic advice. Re-run the
        # bounded classifier after acceptance so the new route audit retains
        # that evidence; ordinary Flash turns can safely reuse the advice
        # already incorporated into the cached expression.
        return pending is not None and pending.route_tier == "flash"

    def has_precomputed_semantic_advisory(
        self,
        *,
        trigger_ref: str,
        observation_ref: str,
        event_payload_hash: str,
    ) -> bool:
        """Include a valid paired appraisal even when expression bytes failed."""

        key = (trigger_ref, observation_ref, event_payload_hash)
        pending = self._owner._pending.get(key)
        # A cached Flash expression was deliberately compiled against the
        # same advisory-bearing route and can be rebound without another
        # classifier call.  Thinking is different: its route is itself a
        # consequence of semantic advice and acceptance may change the route
        # hints.  Re-run the bounded advisory slice for that post-acceptance
        # cursor so the visible pass cannot silently downgrade to Flash and
        # then reject the cached Thinking bytes as a route mismatch.
        if pending is not None:
            return pending.route_tier == "flash"
        return key in self._owner._precomputed_advisory

    async def propose(self, request: ModelInput) -> ModelOutput:
        key = _cache_key(request)
        pending = self._owner._pending.pop(key, None)
        self._owner._precomputed_advisory.discard(key)
        if pending is None:
            if key in self._owner._failed_combined:
                self._owner._failed_combined.discard(key)
                if self._owner._should_use_grounded_provider_recovery(request):
                    # A verified memory question already exhausted the paired
                    # pass and its one backup attempt.  Force the normal
                    # Deliberation recovery audit before the local, claim-free
                    # boundary response rather than silently treating it as a
                    # successful visible expression.
                    raise ValueError("paired_expression_requires_grounded_recovery")
                repaired = await self._owner._retry_failed_expression_before_failsafe(
                    request, key
                )
                if repaired is not None:
                    return repaired
                return self._owner._local_expression_failsafe(request, "combined_cognition_failed")
            try:
                return await self._owner._fallback_expression.propose(request)
            except asyncio.CancelledError:
                raise
            except Exception:
                recovery = self._owner._recovery_expression
                if recovery is None or _provider_already_used_fallback(
                    self._owner._selected_provider(request)
                ):
                    raise
                self._owner._recovery_attempted.add(key)
                async with asyncio.timeout(_RECOVERY_MODEL_TIMEOUT_SECONDS):
                    output = await recovery.propose(request)
                record_backup_recovery()
                return output
        if pending.route_tier != request.route.tier:
            # The post-acceptance capsule may legitimately route differently.
            # Never attribute bytes produced by one tier to another tier's
            # independent proposal audit merely to preserve the one-call fast
            # path.
            return self._owner._local_expression_failsafe(request, "combined_route_changed")
        try:
            selected = self._owner._selected_expression(request)
            reviewed_raw = await selected._review_world_grounding_if_needed(  # noqa: SLF001
                request=request,
                raw=pending.raw,
            )
            proposal = materialize_expression_draft(
                raw=reviewed_raw,
                request=request,
                capabilities=self._owner._capabilities,
                quick_recovery=False,
            )
        except (TypeError, ValueError):
            # A paired draft can become invalid when acceptance advances the
            # world revision or changes the available evidence.  Give the
            # configured backup model one fresh, source-bound expression pass
            # before Deliberation invokes its local recovery lane.
            fallback = self._owner._recovery_expression
            if fallback is None:
                raise
            self._owner._recovery_attempted.add(_cache_key(request))
            try:
                async with asyncio.timeout(_RECOVERY_MODEL_TIMEOUT_SECONDS):
                    output = await fallback.propose(request)
            except (TimeoutError, TypeError, ValueError):
                raise
            record_backup_recovery()
            return output
        return ModelOutput(
            model_id=pending.model_id,
            model_version=self._owner.VERSION,
            raw_proposal=proposal,
            input_tokens=pending.usage.input_tokens if pending.usage is not None else None,
            output_tokens=pending.usage.output_tokens if pending.usage is not None else None,
            usage=pending.usage,
        )

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        key = _cache_key(request)
        self._owner._pending.pop(key, None)
        # Deliberation invokes this method after a main timeout/exception. If
        # the paired pass has not already consumed the configured backup, this
        # is the one remaining model-owned recovery opportunity. It uses the
        # adapter's quick-recovery prompt, so the backup receives the same
        # bounded world/emotion/relationship context without adding a second
        # provider lane.
        recovery = self._owner._recovery_expression
        if (
            recovery is not None
            and key not in self._owner._recovery_attempted
            and not _provider_already_used_fallback(self._owner._selected_provider(request))
        ):
            self._owner._recovery_attempted.add(key)
            try:
                output = await recovery.recover(request, failure_code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "expression backup recovery failed: %s: %s",
                    type(exc).__name__,
                    str(exc)[:240],
                )
            else:
                record_backup_recovery()
                return output
        # The paired pass and the one configured backup model have now spent
        # their provider attempts. Local recovery is deliberately claim-free
        # and only handles identity, evidence, modality, and other safety
        # boundaries.
        return self._owner._local_expression_failsafe(request, failure_code)


class SingleCallInboundCognition:
    """Deep Module exposing the two unchanged deliberation adapter interfaces.

    A normal text turn performs one provider call during the appraisal pass and
    stores only the untrusted ExpressionDraft bytes.  The later expression pass
    materializes those bytes against its *post-acceptance* ModelInput, producing
    a distinct source-bound proposal and audit.  Missing/invalid cache entries
    fall back to the ordinary routed expression adapter.

    Current-world evidence questions retain their dedicated grounding review
    lane and intentionally use the established two-pass path.
    """

    VERSION = "single-call-inbound-cognition.1"

    def __init__(
        self,
        *,
        flash_model: ChatCompletionModel,
        thinking_model: ChatCompletionModel | None = None,
        appraisal_model: ChatCompletionModel | None = None,
        recovery_model: ChatCompletionModel | None = None,
        flash_model_id: str | None = None,
        thinking_model_id: str | None = None,
        temperature: float = 0.7,
        expression_capabilities: ExpressionDraftCapabilities = TEXT_ONLY_EXPRESSION_CAPABILITIES,
        identity_frame: CompanionIdentityFrame | None = None,
    ) -> None:
        self._flash_model = flash_model
        self._thinking_model = thinking_model
        self._appraisal_model = appraisal_model
        self._recovery_model = recovery_model or _discover_recovery_model(
            flash_model=flash_model,
            thinking_model=thinking_model,
        )
        self._flash_id = (
            flash_model_id or str(getattr(flash_model, "model", "single-call-flash"))
        )[:256]
        self._thinking_id = thinking_model_id or (
            str(getattr(thinking_model, "model", "single-call-thinking"))
            if thinking_model
            else None
        )
        self._temperature = temperature
        self._capabilities = expression_capabilities
        self._identity_frame = identity_frame
        self._flash_expression = ChatModelDeliberationAdapter(
            model=flash_model,
            model_id=self._flash_id,
            temperature=temperature,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
            world_grounding_reviewer=flash_model,
        )
        self._thinking_expression = (
            ChatModelDeliberationAdapter(
                model=thinking_model,
                model_id=self._thinking_id,
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                world_grounding_reviewer=flash_model,
            )
            if thinking_model is not None
            else None
        )
        self._fallback_expression = RoutedChatModelDeliberationAdapter(
            flash_model=flash_model,
            thinking_model=thinking_model,
            flash_model_id=self._flash_id,
            thinking_model_id=self._thinking_id,
            temperature=temperature,
            expression_capabilities=expression_capabilities,
            identity_frame=identity_frame,
        )
        self._recovery_expression = (
            ChatModelDeliberationAdapter(
                model=self._recovery_model,
                model_id=str(getattr(self._recovery_model, "model", "recovery-model")),
                temperature=temperature,
                expression_capabilities=expression_capabilities,
                identity_frame=identity_frame,
                # One backup completion is the recovery budget.  The normal
                # materializer still enforces source-bound claims, so a
                # second semantic reviewer is unnecessary here.
                world_grounding_reviewer=None,
            )
            if self._recovery_model is not None
            else None
        )
        self._fallback_appraisal = AppraisalDraftDeliberationAdapter(model=flash_model)
        self._separate_appraisal = (
            FastAppraisalDraftDeliberationAdapter(
                model=appraisal_model,
                model_id=str(getattr(appraisal_model, "model", "local-appraisal")),
            )
            if appraisal_model is not None
            else None
        )
        # The same local small model that drafts the fast appraisal also
        # answers the same-turn scheduling question ("does this message need
        # emotion work before the reply?").  It is a scheduling gate only:
        # the durable interaction-appraisal trigger stays unconditionally
        # open at ingress, so gate failures merely defer emotion work to the
        # background drain instead of losing it.
        self.immediate_emotion_gate = (
            SemanticImmediateEmotionGate(model=appraisal_model)
            if appraisal_model is not None
            else None
        )
        self._pending: OrderedDict[tuple[str, str, str], _PendingExpression] = OrderedDict()
        self._failed_combined = _BoundedKeySet(_MAX_PENDING_DRAFTS)
        self._failed_details: OrderedDict[
            tuple[str, str, str], _FailedExpressionDetail
        ] = OrderedDict()
        self._recovery_attempted = _BoundedKeySet(_MAX_PENDING_DRAFTS)
        self._precomputed_advisory: set[tuple[str, str, str]] = set()
        self.appraisal = SingleCallAppraisalAdapter(self)
        self.expression = SingleCallExpressionAdapter(self)

    def _selected_expression(self, request: ModelInput) -> ChatModelDeliberationAdapter:
        if request.route.tier == "thinking":
            if self._thinking_expression is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_expression
        return self._flash_expression

    def _selected_provider(self, request: ModelInput) -> ChatCompletionModel:
        if request.route.tier == "thinking":
            if self._thinking_model is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_model
        return self._flash_model

    def _local_appraisal_recovery(
        self, request: ModelInput, failure_code: str
    ) -> dict[str, object]:
        """Fail closed without inventing an emotional interpretation.

        Affect and relationship meaning belong to the model-backed appraisal
        lane.  Once both provider attempts are exhausted, local code must not
        turn keywords into durable emotion or relationship state.
        """

        return _no_change_proposal(
            request=request,
            rationale=f"Provider recovery exhausted; appraisal withheld ({failure_code[:96]}).",
        )

    @staticmethod
    def _should_use_grounded_provider_recovery(request: ModelInput) -> bool:
        trigger = request.trigger_message
        if trigger is None or not _has_grounded_recovery_material(request.model_content_json):
            return False
        normalized = "".join(trigger.text.lower().split())
        return any(
            marker in normalized for marker in ("记得", "还记得", "喜欢什么", "之前说", "说过什么")
        )

    def _model_id_for(self, request: ModelInput) -> str:
        if request.route.tier == "thinking":
            if self._thinking_id is None:
                raise RuntimeError("thinking deliberation route is not configured")
            return self._thinking_id[:256]
        return self._flash_id

    def _model_id_for_provider(
        self, request: ModelInput, provider: ChatCompletionModel
    ) -> str:
        inferred = str(getattr(provider, "model", "")).strip()
        return (inferred or self._model_id_for(request))[:256]

    async def _repair_expression_claims(
        self,
        *,
        request: ModelInput,
        provider: ChatCompletionModel,
        messages: list[dict[str, str]],
        raw: str,
        violation: str,
        combined: bool = True,
        timeout_seconds: float = _CLAIM_REPAIR_TIMEOUT_SECONDS,
    ) -> str | None:
        """Spend one corrective call naming the exact structural violation.

        Handles both claim-bookkeeping near-misses and non-claim draft-shape
        rejects (the measured second failure class).  Returns validated
        expression bytes, or ``None`` when the correction itself fails.  This
        never loosens any gate: the corrected draft still passes the full
        materializer, and only one attempt is made.
        """

        shape = (
            "the same JSON object shape (appraisal_draft and expression_draft)"
            if combined
            else "one corrected ExpressionDraft JSON object only"
        )
        is_claim = _is_world_claim_violation(violation)
        instruction = (
            claim_repair_instruction(violation, shape_line=shape)
            if is_claim
            else shape_repair_instruction(violation, shape_line=shape)
        )
        corrective = [
            *messages,
            {"role": "assistant", "content": raw},
            {"role": "user", "content": instruction},
        ]
        try:
            async with asyncio.timeout(timeout_seconds):
                complete_json = getattr(provider, "complete_json", None)
                corrected_raw = await (
                    complete_json(corrective, temperature=self._temperature)
                    if callable(complete_json)
                    else provider.complete(corrective, temperature=self._temperature)
                )
            if combined:
                corrected = _parse_combined(corrected_raw)
                expression_raw = json.dumps(
                    corrected["expression_draft"], ensure_ascii=False, separators=(",", ":")
                )
            else:
                expression_raw = corrected_raw
            materialize_expression_draft(
                raw=expression_raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "%s corrective retry failed: %s: %s",
                "world-claim" if is_claim else "draft-shape",
                type(exc).__name__,
                str(exc)[:240],
            )
            return None
        if is_claim:
            logger.warning("world-claim corrective retry repaired the expression draft")
            record_claim_repair()
        else:
            logger.warning("draft-shape corrective retry repaired the expression draft")
            record_shape_repair()
        return expression_raw

    async def _retry_failed_expression_before_failsafe(
        self, request: ModelInput, key: tuple[str, str, str]
    ) -> ModelOutput | None:
        """One violation-quoting main-provider retry before any canned line.

        The paired pass failed structurally and its bounded in-attempt repair
        either did not fit the appraisal-lane budget or itself failed once.
        The person is now already waiting on the failure path, so spending a
        few more seconds on one corrective completion that names the exact
        violation is a better trade than an instant canned acknowledgement.
        Timeout-class failures never reach here: they leave no remembered
        violation, so this method returns ``None`` immediately for them.
        """

        detail = self._failed_details.pop(key, None)
        if detail is None:
            return None
        repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
        if repair_timeout is None:
            return None
        provider = self._selected_provider(request)
        repaired = await self._repair_expression_claims(
            request=request,
            provider=provider,
            messages=detail.messages,
            raw=detail.raw,
            violation=detail.violation,
            combined=True,
            timeout_seconds=repair_timeout,
        )
        if repaired is None:
            return None
        logger.warning(
            "pre-failsafe corrective retry recovered a genuine expression trigger=%s",
            request.trigger_message.observation_ref
            if request.trigger_message is not None
            else request.trigger_ref,
        )
        return ModelOutput(
            model_id=self._model_id_for_provider(request, provider),
            model_version=self.VERSION,
            raw_proposal=materialize_expression_draft(
                raw=repaired,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
            ),
        )

    async def _retry_with_recovery_provider(self, request: ModelInput) -> ModelOutput:
        """Run exactly one bounded structural recovery against the backup model."""

        if self._recovery_model is None:
            raise RuntimeError("no recovery model is configured")
        key = _cache_key(request)
        self._pending.pop(key, None)
        self._precomputed_advisory.discard(key)
        self._failed_combined.discard(key)
        self._failed_details.pop(key, None)
        self._recovery_attempted.add(key)
        try:
            async with asyncio.timeout(_RECOVERY_MODEL_TIMEOUT_SECONDS):
                return await self._propose_appraisal(
                    request,
                    provider_override=self._recovery_model,
                    allow_recovery=False,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "paired cognition backup failed: %s: %s",
                type(exc).__name__,
                str(exc)[:240],
            )
            raise

    async def _propose_appraisal(
        self,
        request: ModelInput,
        *,
        provider_override: ChatCompletionModel | None = None,
        allow_recovery: bool = True,
    ) -> ModelOutput:
        trigger = request.trigger_message
        if trigger is None:
            return await self._fallback_appraisal.propose(request)

        # An opt-in local appraiser is intentionally a separate, structured
        # call. It only owns the Appraisal draft; the visible Expression still
        # uses the configured Flash/Thinking model on its normal lane. This
        # keeps a small local model from becoming a voice/persona generator,
        # while making same-turn emotional triage independent of the remote
        # paired-completion latency.
        if (
            provider_override is None
            and self._separate_appraisal is not None
            and not _requires_remote_appraisal(trigger.text)
        ):
            try:
                return await self._separate_appraisal.propose(request)
            except (TypeError, ValueError):
                # A small local checkpoint is a latency optimization, not a
                # reason to lose an appraisal. If it misses the strict typed
                # contract, spend the normal provider path for this turn.
                logger.warning("local appraisal output rejected; using the main appraisal provider")

        expression_adapter = self._selected_expression(request)
        provider_request = request.model_copy(
            update={
                "model_content_json": compact_chat_model_facing_context(request.model_content_json)
            }
        )
        appraisal_messages = AppraisalDraftDeliberationAdapter._messages(provider_request)
        expression_messages = expression_adapter._messages(  # noqa: SLF001 - paired internal seam
            request=provider_request, quick_recovery=False, failure_code=None
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Return exactly one JSON object with exactly two keys: appraisal_draft and "
                    "expression_draft. Both values must be JSON objects. This is one simultaneous "
                    "inner cognition pass: the expression may be shaped by the appraisal and optional "
                    "affect it returns, but neither draft is accepted authority yet. The application "
                    "will independently validate and accept Appraisal/Affect before it can authorize "
                    "the Expression. Do not weaken, erase, or prematurely repair a material feeling "
                    "merely to sound agreeable. The expression_draft's timing_choice (now/later/"
                    "silent) is part of this same inner pass: read any phone_attention advisory "
                    "(【手机注意力：…】) and the appraisal you just made before deciding whether she "
                    "answers now, defers with later (the host delivers the deferred text after "
                    "delay_seconds and keeps it as her private commitment), or stays silent; "
                    "appraising a message honestly does not oblige an instant visible reply."
                    "\n\nAPPRAISAL DRAFT CONTRACT:\n"
                    + appraisal_messages[0]["content"]
                    + "\n\nEXPRESSION DRAFT CONTRACT:\n"
                    + expression_messages[0]["content"]
                ),
            },
            expression_messages[1],
        ]
        provider = provider_override or self._selected_provider(request)
        model_id = self._model_id_for_provider(request, provider)
        metered = getattr(provider, "complete_with_usage", None)
        usage: ModelUsageProvenance | None = None
        try:
            if callable(metered):
                result = await metered(messages, temperature=self._temperature)
                if (
                    not isinstance(result, tuple)
                    or len(result) != 2
                    or not isinstance(result[0], str)
                ):
                    raise ValueError("metered combined provider result must be (text, usage)")
                raw, usage_raw = result
                usage = ModelUsageProvenance.model_validate(usage_raw)
            else:
                complete_json = getattr(provider, "complete_json", None)
                raw = await (
                    complete_json(messages, temperature=self._temperature)
                    if callable(complete_json)
                    else provider.complete(messages, temperature=self._temperature)
                )
        except asyncio.CancelledError:
            # Deliberation cancels the paired provider task when its deadline
            # expires.  Preserve the same-trigger marker so the later
            # expression pass does not launch a duplicate provider call.
            self._failed_combined.add(_cache_key(request))
            raise
        except Exception:
            if (
                allow_recovery
                and self._recovery_model is not None
                and not _provider_already_used_fallback(provider)
            ):
                return await self._retry_with_recovery_provider(request)
            self._failed_combined.add(_cache_key(request))
            raise
        try:
            value = _parse_combined(raw)
        except (TypeError, ValueError) as exc:
            if (
                allow_recovery
                and self._recovery_model is not None
                and not _provider_already_used_fallback(provider)
            ):
                return await self._retry_with_recovery_provider(request)
            self._failed_combined.add(_cache_key(request))
            self._remember_failed_expression(
                _cache_key(request), messages=messages, raw=raw, violation=str(exc)
            )
            raise
        key = _cache_key(request)
        # Even when the paired expression later fails structural validation,
        # this successful provider response already contains the semantic
        # advice used by the interaction-appraisal lane.  Mark it so the
        # post-acceptance expression lane does not compile/run the advisory
        # classifier a second time for the same trigger.
        self._precomputed_advisory.add(key)
        if len(self._precomputed_advisory) > _MAX_PENDING_DRAFTS:
            self._precomputed_advisory.pop()
        appraisal_raw = json.dumps(
            value["appraisal_draft"], ensure_ascii=False, separators=(",", ":")
        )
        expression_value = value["expression_draft"]
        expression_raw = json.dumps(expression_value, ensure_ascii=False, separators=(",", ":"))
        # The provider creates two fallible drafts in one transport response,
        # but they remain independent proposal candidates.  A malformed inner
        # appraisal must not erase a valid, separately auditable expression.
        # Conversely, never cache expression bytes that cannot pass the normal
        # ExpressionDraft materializer even at the source cursor.
        violation: str | None = None
        try:
            materialize_expression_draft(
                raw=expression_raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
            )
        except (TypeError, ValueError) as exc:
            violation = str(exc)
            normalized = _normalize_visible_expression(expression_value)
            if normalized is None:
                logger.warning(
                    "combined expression structural normalization rejected: shape=%s error=%s",
                    _visible_expression_shape(expression_value),
                    violation[:300],
                )
                expression_valid = False
            else:
                expression_raw = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
                try:
                    # Normalization is only a structural repair.  The normal
                    # epistemic-claim and capability gates remain authoritative.
                    materialize_expression_draft(
                        raw=expression_raw,
                        request=request,
                        capabilities=self._capabilities,
                        quick_recovery=False,
                    )
                except (TypeError, ValueError) as retry_exc:
                    violation = str(retry_exc)
                    expression_valid = False
                else:
                    expression_valid = True
        else:
            expression_valid = True
        corrective_spent = False
        if not expression_valid and violation is not None:
            # A structural near-miss (claim bookkeeping, beat shape, later
            # contract) regularly arrives attached to a perfectly good visible
            # reply.  Rerunning the identical contract on the backup provider
            # tends to repeat the same mistake, so spend one corrective call
            # that names the exact violation before falling back.  The retry
            # is deadline-aware: when the Deliberation attempt budget cannot
            # fit another completion, defer the correction to the
            # post-acceptance expression pass instead of timing out the whole
            # attempt after the repair already succeeded.
            repair_timeout = fit_secondary_call_timeout(_CLAIM_REPAIR_TIMEOUT_SECONDS)
            if repair_timeout is None:
                logger.warning(
                    "paired corrective retry deferred: attempt budget exhausted "
                    "violation=%s",
                    violation[:200],
                )
            else:
                corrective_spent = True
                repaired = await self._repair_expression_claims(
                    request=request,
                    provider=provider,
                    messages=messages,
                    raw=raw,
                    violation=violation,
                    timeout_seconds=repair_timeout,
                )
                if repaired is not None:
                    expression_raw = repaired
                    expression_valid = True
        try:
            appraisal_proposal = materialize_appraisal_draft(raw=appraisal_raw, request=request)
        except (TypeError, ValueError):
            appraisal_proposal = _no_change_proposal(
                request=request,
                rationale="Combined appraisal was invalid; emotional state failed closed.",
            )
        if (
            not expression_valid
            and allow_recovery
            and self._recovery_model is not None
            and not _provider_already_used_fallback(provider)
        ):
            return await self._retry_with_recovery_provider(request)
        if expression_valid:
            self._pending[key] = _PendingExpression(
                raw=expression_raw,
                model_id=model_id,
                route_tier=request.route.tier,
                usage=usage,
            )
            self._pending.move_to_end(key)
            while len(self._pending) > _MAX_PENDING_DRAFTS:
                self._pending.popitem(last=False)
        else:
            self._pending.pop(key, None)
            # The appraisal bytes may still be valid even when the paired
            # expression draft is not.  Preserve a same-trigger marker plus
            # the exact violation so the post-acceptance expression lane can
            # spend one corrective retry that names the concrete problem
            # before it falls back to a local canned line.  When the
            # in-attempt corrective was already spent (and failed once), do
            # not queue the same correction again: repeating an identical
            # failed repair only delays the bounded local recovery.
            self._failed_combined.add(key)
            if violation is not None and not corrective_spent:
                self._remember_failed_expression(
                    key, messages=messages, raw=raw, violation=violation
                )
        return ModelOutput(
            model_id=model_id,
            model_version=self.VERSION,
            raw_proposal=appraisal_proposal,
        )

    def _remember_failed_expression(
        self,
        key: tuple[str, str, str],
        *,
        messages: list[dict[str, str]],
        raw: str,
        violation: str,
    ) -> None:
        self._failed_details.pop(key, None)
        self._failed_details[key] = _FailedExpressionDetail(
            messages=messages, raw=raw, violation=violation
        )
        while len(self._failed_details) > _MAX_PENDING_DRAFTS:
            self._failed_details.popitem(last=False)

    def _local_expression_failsafe(self, request: ModelInput, failure_code: str) -> ModelOutput:
        trigger = request.trigger_message
        if trigger is None:
            raise ValueError("local expression failsafe requires a verified message")
        trigger_text = trigger.text or ""
        intent = _classify_local_failsafe_intent(trigger_text)
        logger.warning(
            "local expression failsafe engaged trigger=%s failure=%s intent=%s",
            trigger.observation_ref,
            failure_code[:120],
            intent,
        )
        record_failsafe()
        name = self._identity_frame.companion_name if self._identity_frame is not None else None
        if intent == "world_evidence":
            try:
                context: object = json.loads(request.model_content_json)
            except (TypeError, json.JSONDecodeError):
                context = {}
            recent_texts = recent_companion_texts(context)
            if claim_free_reply_already_given(
                trigger_text=trigger_text, recent_visible_texts=recent_texts
            ):
                # Every claim-free variant for this probe was already said;
                # parroting one verbatim reads as a script.  Fall to the
                # bounded acknowledgement that names the hand-off instead.
                return self._local_expression_recovery_ack(
                    request=request,
                    failure_code=failure_code,
                    intent=intent,
                )
            text = recover_without_world_evidence(
                trigger_text=trigger_text,
                source_ref=trigger.observation_ref,
                recent_visible_texts=recent_texts,
            )
        elif intent == "identity":
            text = f"我是{name}。" if name else "我在。"
        elif intent == "role_boundary":
            subject = f"我是{name}，是" if name else "我是"
            if self._identity_frame is None or self._identity_frame.not_an_assistant:
                text = f"不是。{subject}和你聊天、相处的人，不是你的助手或工具。"
            else:
                text = f"{subject}和你聊天、相处的人。"
        elif intent == "emotion":
            # Preserve the relational meaning of a hurt/repair bid even when
            # both model attempts are unavailable.  This owns only the
            # runtime's failed hand-off; it does not infer why the counterpart
            # feels that way or claim a new world event.
            text = "我听到了你的情绪。刚才那句我确实没接好，先不装作没事。"
        elif intent == "user_fact":
            # The disclosure itself is part of the immutable trigger, but the
            # durable Fact/Memory workers may still be pending.  Acknowledge
            # hearing it without falsely promising that persistence already
            # succeeded.
            text = "我看到你是在告诉我一件关于自己的事。刚才没接好，不想装作没听见。"
        elif intent == "unavailable_action":
            # Never authorize a text substitute for an unavailable modality.
            raise ValueError("local failsafe cannot replace an unavailable action modality")
        elif intent == "greeting":
            # A provider outage must not turn a first hello into an unexplained
            # disappearance.  This is deliberately a tiny, claim-free social
            # acknowledgement: it does not invent a current activity or a
            # feeling, but keeps the live conversation continuous until the
            # normal model lane is available again.
            text = (
                f"你好，第一次见。我是{name}。"
                if name
                else "你好，第一次见。先认识一下，我在。"
            )
        else:
            # Ordinary conversation, emotion, relationship, memory, and
            # attachment content remain model-owned.  Local recovery cannot
            # invent a reply for them after both provider attempts fail; a
            # bounded acknowledgement is still preferable to silently losing
            # an otherwise normal live message, so keep that policy in the
            # typed fallback below rather than pretending to answer its topic.
            return self._local_expression_recovery_ack(
                request=request,
                failure_code=failure_code,
                intent=intent,
            )
        raw = json.dumps(
            {
                "response_text": text,
                "stance": "answer_without_world_claims",
                "brief_rationale": (f"local_structural_failsafe:{intent}:{failure_code[:64]}"),
                "confidence": 0,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ModelOutput(
            model_id="local-expression-failsafe",
            model_version="local-expression-failsafe.1",
            raw_proposal=materialize_expression_draft(
                raw=raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=True,
            ),
        )

    def _local_expression_recovery_ack(
        self, *, request: ModelInput, failure_code: str, intent: str
    ) -> ModelOutput:
        """Keep a provider failure visible without inventing topic content.

        Deliberate silence remains model-owned (a valid ``timing_choice`` in a
        normal proposal).  This path is only entered after the provider and
        its bounded recovery both failed, so silently settling a live inbound
        as ``observed_only`` would make the companion look as if it vanished.
        The acknowledgement names the failure, makes no world claim, and
        leaves the original topic for a later model-owned answer.
        """

        text = (
            "我刚才没接好这句，不想装作已经回答了；但我看到你说了什么。"
            if intent in {"acknowledgement", "relationship"}
            else "我刚才没接好这句，不想敷衍你；这句话我先收到了。"
        )
        raw = json.dumps(
            {
                "response_text": text,
                "stance": "acknowledge_briefly",
                "brief_rationale": (
                    f"local_expression_recovery_ack:{intent}:{failure_code[:64]}"
                ),
                "confidence": 0,
                "world_claims": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return ModelOutput(
            model_id="local-expression-failsafe",
            model_version="local-expression-failsafe.1",
            raw_proposal=materialize_expression_draft(
                raw=raw,
                request=request,
                capabilities=self._capabilities,
                quick_recovery=False,
            ),
        )


def _normalize_visible_expression(
    value: dict[str, Any],
) -> dict[str, object] | None:
    """Repair only an explicit, user-visible reply shape.

    This intentionally does not recurse: rationale, tool results, metadata, or
    arbitrary nested ``text`` fields must never become a companion utterance.
    Conflicting top-level aliases are rejected instead of guessed between.
    """

    if any(key in value for key in _UNSAFE_VISIBLE_KEYS):
        return None

    candidates: list[tuple[str, ...]] = []
    for key in _VISIBLE_TEXT_KEYS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            candidates.append((raw.strip(),))
        elif isinstance(raw, list):
            texts = _normalize_visible_text_list(raw)
            if texts is None:
                return None
            candidates.append(texts)
    for key in _VISIBLE_TEXT_LIST_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        if not isinstance(raw, list):
            return None
        texts = _normalize_visible_text_list(raw)
        if texts is None:
            return None
        candidates.append(texts)

    distinct = set(candidates)
    if len(distinct) != 1:
        return None
    texts = next(iter(distinct))
    if not texts or any(len(text) > 4_096 for text in texts):
        return None
    stance = value.get("stance")
    rationale = value.get("brief_rationale")
    confidence = value.get("confidence")
    claims = value.get("world_claims")
    return {
        "timing_choice": "now",
        "beats": [{"modality": "text", "text": text} for text in texts],
        "stance": (stance if isinstance(stance, str) and 1 <= len(stance) <= 128 else "open"),
        "brief_rationale": (
            rationale
            if isinstance(rationale, str) and 1 <= len(rationale) <= 240
            else "Normalized an explicit visible-text response."
        ),
        "confidence": (
            confidence
            if isinstance(confidence, int)
            and not isinstance(confidence, bool)
            and 0 <= confidence <= 10_000
            else 5_000
        ),
        "world_claims": claims if isinstance(claims, list) else [],
    }


def _normalize_visible_text_list(value: list[object]) -> tuple[str, ...] | None:
    """Normalize one declared visible-text list without recursive extraction."""

    if not value:
        return None
    texts: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            if not set(item).issubset({"text", "modality"}):
                return None
            if item.get("modality", "text") != "text":
                return None
            raw_text = item.get("text")
            if not isinstance(raw_text, str):
                return None
            text = raw_text.strip()
        else:
            return None
        if not text:
            return None
        texts.append(text)
    return tuple(texts)


def _visible_expression_shape(value: dict[str, Any]) -> str:
    """Return bounded structural diagnostics without logging proposed prose."""

    parts: list[str] = []
    for key in sorted(value)[:16]:
        item = value[key]
        if isinstance(item, list):
            item_types = ",".join(type(child).__name__ for child in item[:8])
            kind = f"list[{item_types}]"
        else:
            kind = type(item).__name__
        parts.append(f"{key}:{kind}")
    if len(value) > 16:
        parts.append(f"+{len(value) - 16}-keys")
    return ";".join(parts)


def _has_grounded_recovery_material(raw_context: str) -> bool:
    try:
        context = json.loads(raw_context)
    except (TypeError, json.JSONDecodeError):
        return False
    slices = context.get("slices") if isinstance(context, dict) else None
    if not isinstance(slices, dict):
        return False
    for name in (
        "relevant_facts",
        "active_memory_candidates",
        "recent_experiences",
        "current_situation",
        "world_life",
    ):
        lane = slices.get(name)
        if (
            isinstance(lane, dict)
            and lane.get("availability") == "available"
            and isinstance(lane.get("items"), list)
            and bool(lane["items"])
        ):
            return True
    return False


def _classify_local_failsafe_intent(text: str) -> str:
    normalized = "".join(text.lower().split())
    if any(marker in normalized for marker in ("表情", "贴纸", "reaction", "sticker")):
        return "unavailable_action"
    identity_markers = ("你是谁", "你叫什么", "你的名字", "whoareyou", "yourname")
    role_boundary_markers = (
        "助手",
        "助理",
        "秘书",
        "工具",
        "机器人",
        "人工智能",
        "ai",
        "程序",
        "模型",
        "assistant",
        "robot",
        "bot",
        "program",
    )
    relationship_markers = (
        "什么关系",
        "你是我的什么",
        "我们算什么",
        "是什么关系",
        "ourrelationship",
        "whatamitoyou",
        "whatrelationship",
    )
    # A direct self-disclosure is already observable in the trigger.  Keep a
    # provider outage from asking the person to repeat a name or preference;
    # the recovery may acknowledge the disclosure without claiming that the
    # asynchronous fact/memory workers have already persisted it.
    user_fact_markers = (
        "我叫",
        "我的名字",
        "英文名",
        "我喜欢",
        "我最喜欢",
        "我平时最喜欢",
        "我不喜欢",
        "我平时喝",
        "我平时喜欢",
    )
    greeting_markers = ("你好", "嗨", "哈喽", "初次见", "第一次见", "hello", "hey")
    # Relational/emotional probes must win over the broader "recent world
    # evidence" detector (e.g. "刚才你真的不高兴吗？").  Do not treat a
    # third-person topic as a bid for repair, though: "电影里的人生气了"
    # should not receive "刚才我没接好".  The local lane is only a failsafe,
    # so its emotion detector intentionally accepts a small set of explicit
    # first/second-person constructions instead of every emotion keyword.
    emotional_markers = (
        "失望",
        "敷衍",
        "冒犯",
        "怪话",
        "攻击",
        "生气",
        "不高兴",
        "不爽",
        "讨厌",
        "没在听",
        "走流程",
        "不满意",
        "没用",
        "废物",
        "垃圾",
        "语气有点冲",
        "道歉",
        "抱歉",
        "对不起",
    )
    direct_emotion_phrases = (
        "我失望",
        "我很失望",
        "我有点失望",
        "我生气",
        "我很生气",
        "我有点生气",
        "我不高兴",
        "我不爽",
        "我讨厌你",
        "你失望",
        "你生气",
        "你真的生气",
        "你不高兴",
        "你不爽",
        "你是不是在敷衍",
        "你在敷衍",
        "你回得",
        "你没在听",
        "你让我失望",
        "你让我生气",
        "你就是个没用",
        "你就是个垃圾",
    )
    is_direct_apology = any(
        marker in normalized for marker in ("道歉", "抱歉", "对不起")
    )
    is_explicit_relational_emotion = any(
        phrase in normalized for phrase in direct_emotion_phrases
    )
    is_contextual_relational_emotion = any(
        prefix in normalized and any(marker in normalized for marker in emotional_markers)
        for prefix in ("你刚才", "你这")
    )
    if (
        is_direct_apology
        or is_explicit_relational_emotion
        or is_contextual_relational_emotion
    ):
        return "emotion"
    if any(marker in normalized for marker in user_fact_markers):
        return "user_fact"
    if is_companion_world_evidence_probe(text):
        return "world_evidence"
    if any(marker in normalized for marker in role_boundary_markers):
        return "role_boundary"
    if any(marker in normalized for marker in relationship_markers):
        return "relationship"
    if any(marker in normalized for marker in identity_markers):
        return "identity"
    if normalized == "hi" or any(marker in normalized for marker in greeting_markers):
        return "greeting"
    return "acknowledgement"


def _parse_combined(raw: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, str):
        raise ValueError("combined cognition model did not return text")
    candidate = raw.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if len(lines) < 3 or not lines[-1].strip().startswith("```"):
            raise ValueError("combined cognition model returned an unclosed JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError("combined cognition model did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("combined cognition model must return an object")
    if len(value) == 2:
        aliases: dict[str, object] = {}
        for key, item in value.items():
            normalized = "".join(character for character in key.lower() if character.isalpha())
            if normalized in {"appraisal", "appraisaldraft"}:
                canonical = "appraisal_draft"
            elif normalized in {"expression", "expressiondraft"}:
                canonical = "expression_draft"
            else:
                break
            if canonical in aliases:
                break
            aliases[canonical] = item
        if set(aliases) == {"appraisal_draft", "expression_draft"}:
            value = aliases
    if set(value) != {"appraisal_draft", "expression_draft"}:
        # Compatibility for a provider that obeys the embedded ExpressionDraft
        # contract but misses the new two-key wrapper.  State fails closed to a
        # source-bound no-change appraisal; the provider bytes are still
        # validated independently as an expression after the acceptance seam.
        return {
            "appraisal_draft": {
                "appraise": False,
                "brief_rationale": "No material appraisal was supplied in the combined response.",
                "behavior_tendency": "observe",
                "stance": "wait",
                "display_strategy": "withhold",
                "confidence": 0,
            },
            "expression_draft": value,
        }
    if not all(isinstance(value[key], dict) for key in value):
        raise ValueError("combined cognition drafts must be objects")
    return value  # type: ignore[return-value]


__all__ = [
    "SingleCallAppraisalAdapter",
    "SingleCallExpressionAdapter",
    "SingleCallInboundCognition",
]
