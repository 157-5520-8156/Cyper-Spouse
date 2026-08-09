"""Ordinary inbound cognition through the sole CharacterInterior seam.

The provider-facing combined cognition implementation remains an internal
Faculty.  Application and Runtime code receive only a Deliberation-compatible
port whose semantic operation is :meth:`CharacterInterior.consider`.

One successful role result carries one unified ``DecisionProposal``.  Its
expression, Appraisal, and immediate Affect changes therefore share the same
cursor, provider invocation, audit, and effect-once identity.  Deterministic
authorities may consume their own typed changes; this module performs no
ledger write and invents no local fallback speech or emotion.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime
import hashlib
import json
from typing import Mapping

from .inbound_wire import _combine_usage
from ..deliberation import ModelInput, ModelOutput
from ..proposal_envelope import (
    validate_proposal_envelope,
)
from ..recall_runtime import (
    PREFETCH_FIRST_PASS_JOIN_SECONDS,
    PresentedPrefetchTrace,
    TrustedRecallTrace,
    append_presented_prefetch,
    augment_model_content_with_recall,
    mark_recall_budget_consumed,
    verify_trusted_recall_trace,
)
from ..schema_core import canonicalize_json_value
from ..schemas import ProjectionCursor
from .audit import recorded_character_interior_lineage
from .inbound_author import _InboundRecallRequested
from .contracts import (
    InteriorOpportunity,
    _InteriorAuthorLineage,
    _InteriorCapabilityManifest,
)
from .core import CharacterInterior
from .ports import _InteriorRoleRequest, _RoleResultContractError
from .run_result import CausalOpportunityIdentity


_CACHE_LIMIT = 128
_PURPOSE = "inbound_turn"
_CAPABILITY_KIND = "inbound_turn_cognition"
_DECISION_CONTRACT = "character-interior-inbound-turn-decision.1"


def _canonical(value: object) -> str:
    return json.dumps(
        canonicalize_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _logical_time(request: ModelInput) -> datetime:
    try:
        content = json.loads(request.model_content_json)
    except json.JSONDecodeError as exc:
        raise ValueError("inbound CharacterInterior Context is not JSON") from exc
    if not isinstance(content, dict):
        raise ValueError("inbound CharacterInterior Context is not an object")
    raw = content.get("logical_time")
    if not isinstance(raw, str) or not raw:
        raise ValueError("inbound CharacterInterior Context has no logical time")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("inbound CharacterInterior logical time is not timezone-aware")
    return parsed


def _model_input_material(
    request: ModelInput,
    *,
    transport_operation: str = "complete",
) -> dict[str, object]:
    """Bind the exact persisted/audited request coordinates.

    ``recorded_cadence_draws`` is deliberately process-local on ``ModelInput``;
    its immutable ``recorded_draw_refs`` remain in this material while the
    exact object is retained in the bounded bridge cache.
    """

    return {
        "contract": "character-interior-inbound-capability.1",
        "call_id": request.call_id,
        "attempt_id": request.attempt_id,
        "capsule_id": request.capsule_id,
        "trigger_ref": request.trigger_ref,
        "cursor": {
            "world_revision": request.evaluated_world_revision,
            "deliberation_revision": request.evaluated_deliberation_revision,
            "ledger_sequence": request.evaluated_ledger_sequence,
        },
        "model_input_hash": "sha256:" + _digest(request.model_dump(mode="json")),
        "recorded_draw_refs": list(request.recorded_draw_refs),
        "transport_operation": transport_operation,
    }


def _stream_key(request: ModelInput) -> tuple[str, str, int, int, int]:
    trigger = request.trigger_message
    source = trigger.event_payload_hash if trigger is not None else request.capsule_id
    return (
        request.trigger_ref,
        source,
        request.evaluated_world_revision,
        request.evaluated_deliberation_revision,
        request.evaluated_ledger_sequence,
    )


class InboundTurnFaculty:
    """Private purpose Faculty owning the sole inbound character author."""

    name = "inbound-turn-faculty"
    purposes = (_PURPOSE,)
    requires_author_lineage = True
    # Infrastructure latency policy only: this makes scheduled source-bound
    # candidates visible to the first inbound role call.  It does not decide
    # whether the character recalls, replies, interrupts, or stays silent.
    automatic_prefetch_join_seconds = PREFETCH_FIRST_PASS_JOIN_SECONDS

    def __init__(
        self,
        *,
        author: object,
    ) -> None:
        if not callable(getattr(author, "propose", None)):
            raise TypeError("inbound CharacterInterior author Faculty is invalid")
        if getattr(author, "_recall", None) is not None:
            raise ValueError("inbound author cannot own a second Recall lifecycle")
        delegate_recall = getattr(author, "delegate_recall_to_character_interior", None)
        if callable(delegate_recall):
            delegate_recall()
            owns_recall = getattr(author, "character_interior_owns_recall", None)
            if not callable(owns_recall) or not owns_recall():
                raise ValueError(
                    "inbound author did not delegate selective Recall to CharacterInterior"
                )
        self._author = author
        self._requests: OrderedDict[str, ModelInput] = OrderedDict()
        self._outputs: OrderedDict[str, ModelOutput] = OrderedDict()
        self._recall_parents: OrderedDict[str, _InboundRecallRequested] = OrderedDict()
        self._prefetch_presentations: OrderedDict[str, tuple[PresentedPrefetchTrace, ...]] = (
            OrderedDict()
        )
        self._decision_parents: OrderedDict[str, str] = OrderedDict()
        self._stream_inputs: OrderedDict[tuple[str, str, int, int, int], ModelInput] = OrderedDict()
        self._stream_ready: dict[
            tuple[str, str, int, int, int], asyncio.Future[ModelInput | None]
        ] = {}

    @property
    def author_identity(self) -> Mapping[str, object]:
        """Expose the inbound purpose's logical author to the frozen registry."""

        supplied = getattr(self._author, "author_identity", None)
        if callable(supplied):
            supplied = supplied()
        if not isinstance(supplied, Mapping):
            return {
                "name": self.name,
                "version": str(getattr(self._author, "VERSION", "unversioned")),
            }
        return {str(key): value for key, value in supplied.items()}

    def internal_recall_disabled(self) -> bool:
        """Architecture evidence: only CharacterInterior may own selective Recall."""

        owns_recall = getattr(self._author, "character_interior_owns_recall", None)
        if callable(owns_recall):
            return bool(owns_recall())
        return getattr(self._author, "_recall", None) is None

    def source_closure_review_enabled(self) -> bool:
        """Expose the reviewer phase owned by this same inbound author."""

        operation = getattr(self._author, "source_closure_review_enabled", None)
        return bool(callable(operation) and operation())

    @staticmethod
    def prefetch_presentation_phase(request: _InteriorRoleRequest) -> str:
        if request.recall_completed:
            return "recall_followup"
        manifest = request.capability_manifest
        if manifest is not None and isinstance(
            manifest.payload.get("technical_recovery_failure"), str
        ):
            return "recovery_initial"
        return "initial"

    def register_capability(
        self,
        request: object,
        *,
        recovery_failure: object | None = None,
        transport_operation: object = "complete",
    ):
        if not isinstance(request, ModelInput):
            raise TypeError("inbound CharacterInterior capability input is invalid")
        if recovery_failure is not None and not isinstance(recovery_failure, str):
            raise TypeError("inbound CharacterInterior recovery failure is invalid")
        if transport_operation not in {"complete", "stream_head"}:
            raise ValueError("inbound CharacterInterior transport operation is invalid")
        material = _model_input_material(
            request,
            transport_operation=str(transport_operation),
        )
        if recovery_failure is not None:
            material["technical_recovery_failure"] = recovery_failure[:384]
        capability_ref = "inbound-turn-capability:sha256:" + _digest(material)
        payload_json = _canonical(material)
        manifest = _InteriorCapabilityManifest(
            capability_ref=capability_ref,
            capability_kind=_CAPABILITY_KIND,
            payload_json=payload_json,
            payload_hash=_sha256(payload_json),
            source_refs=(request.trigger_ref,),
        )
        self._requests[capability_ref] = request
        self._requests.move_to_end(capability_ref)
        while len(self._requests) > _CACHE_LIMIT:
            self._requests.popitem(last=False)
        return manifest

    def transport_available(self, *, transport: str, payload: object) -> bool:
        if transport != "stream" or not isinstance(payload, ModelInput):
            return False
        operation = getattr(self._author, "stream_provider_available", None)
        if not callable(operation) or not bool(operation(payload)):
            return False
        key = _stream_key(payload)
        current = self._stream_ready.get(key)
        if current is None or current.done():
            self._stream_ready[key] = asyncio.get_running_loop().create_future()
        return True

    async def continue_transport(
        self,
        *,
        transport: str,
        payload: object,
    ) -> ModelOutput:
        if transport != "stream_tail" or not isinstance(payload, ModelInput):
            raise ValueError("inbound CharacterInterior continuation is invalid")
        key = _stream_key(payload)
        ready = self._stream_ready.get(key)
        if ready is None:
            raise RuntimeError("inbound CharacterInterior stream was not reserved")
        owned_head = await asyncio.shield(ready)
        if owned_head is None:
            # The initial head attempt failed before publishing a usable
            # head. CharacterInterior owns same-author correction and the
            # corrective slot is the only production recovery port (generic
            # Deliberation recovery is deliberately absent), so rerun a
            # fresh full Character Decision instead of abandoning the reply
            # to the durable 10/30/120-minute lifecycle.
            operation = getattr(self._author, "propose_stream_head", None)
            if not callable(operation):
                raise RuntimeError(
                    "inbound CharacterInterior stream head recovery is unavailable"
                )
            return await operation(payload)
        operation = getattr(self._author, "propose_stream_tail", None)
        if not callable(operation):
            raise RuntimeError("inbound CharacterInterior stream tail is unavailable")
        output = await operation(owned_head.model_copy(update={"call_id": payload.call_id}))
        if not isinstance(output, ModelOutput):
            raise TypeError("inbound CharacterInterior stream tail output is invalid")
        self._stream_inputs.pop(key, None)
        self._stream_ready.pop(key, None)
        return output

    def publish_transport(
        self,
        *,
        transport: str,
        payload: object,
        output: object | None,
    ) -> None:
        if transport != "stream_head" or not isinstance(payload, ModelInput):
            raise ValueError("inbound CharacterInterior stream publication is invalid")
        key = _stream_key(payload)
        ready = self._stream_ready.get(key)
        if ready is None:
            raise RuntimeError("inbound CharacterInterior stream publication was not reserved")
        if ready.done():
            return
        if (
            isinstance(output, ModelOutput)
            and output.semantic_stream_part == "head"
            and output.provider_parent_model_call_id is not None
        ):
            owned_head = self._stream_inputs.get(key)
            if owned_head is None:
                ready.set_result(None)
                return
            ready.set_result(owned_head)
            return
        self._stream_inputs.pop(key, None)
        ready.set_result(None)

    def advance_attention(self, *, attention_ref: str) -> None:
        operation = getattr(self._author, "advance_expression_attention", None)
        if callable(operation):
            operation(attention_ref)
        self._stream_inputs.clear()
        for ready in self._stream_ready.values():
            if not ready.done():
                ready.set_result(None)
        self._stream_ready.clear()

    def consume_output(self, *, output_ref: str, output_hash: str) -> ModelOutput:
        output = self._outputs.get(output_ref)
        if output is None:
            raise RuntimeError("inbound CharacterInterior output cache is unavailable")
        if "sha256:" + _digest(output.model_dump(mode="json")) != output_hash:
            raise RuntimeError("inbound CharacterInterior output cache changed identity")
        self._outputs.move_to_end(output_ref)
        return output

    async def experience(self, _request: _InteriorRoleRequest) -> Mapping[str, object]:
        raise RuntimeError("inbound_turn_does_not_support_experience_phase")

    async def consider(self, request: _InteriorRoleRequest) -> Mapping[str, object]:
        manifest = request.capability_manifest
        if (
            manifest is None
            or manifest.capability_kind != _CAPABILITY_KIND
            or request.purpose != _PURPOSE
        ):
            raise ValueError("inbound CharacterInterior capability is unavailable")
        model_input = self._requests.get(manifest.capability_ref)
        if model_input is None:
            raise RuntimeError("inbound CharacterInterior request cache is unavailable")
        transport_operation = manifest.payload.get("transport_operation", "complete")
        if transport_operation not in {"complete", "stream_head"}:
            raise ValueError("inbound CharacterInterior capability transport is invalid")
        expected = _model_input_material(
            model_input,
            transport_operation=str(transport_operation),
        )
        raw_failure = manifest.payload.get("technical_recovery_failure")
        recovery_failure = raw_failure if isinstance(raw_failure, str) else None
        if recovery_failure is not None:
            expected["technical_recovery_failure"] = recovery_failure
        if dict(manifest.payload) != expected:
            raise ValueError("inbound CharacterInterior capability changed its ModelInput")

        # Make the canonical, source-bound eight-facet snapshot—including the
        # subjective_relationship facet—the exact model-facing private state.
        content = json.loads(model_input.model_content_json)
        if not isinstance(content, dict):
            raise ValueError("inbound CharacterInterior Context is not an object")
        content["inner_life_snapshot"] = request.snapshot.model_view()
        if request.correction_ordinal == 1:
            # This is a hard-wire failure coordinate, not a semantic hint. It
            # stays inside the same InnerTurn snapshot view so the same role
            # author can replace its malformed result once.
            content["inner_life_snapshot"]["role_result_correction"] = {
                "contract": "character-interior-role-result-correction.1",
                "failure_code": request.correction_failure_code,
                "task": "return_one_fresh_complete_role_result",
            }
        model_content_json = _canonical(content)
        recall_trace: TrustedRecallTrace | None = None
        prefetch_trace: TrustedRecallTrace | None = None
        if request.snapshot.prefetch_trace_json is not None:
            prefetch_trace = TrustedRecallTrace.model_validate_json(
                request.snapshot.prefetch_trace_json
            )
            verify_trusted_recall_trace(prefetch_trace)
        if request.recall_completed:
            if request.snapshot.recall_trace_json is not None:
                recall_trace = TrustedRecallTrace.model_validate_json(
                    request.snapshot.recall_trace_json
                )
                model_content_json = augment_model_content_with_recall(
                    model_content_json,
                    verify_trusted_recall_trace(recall_trace),
                )
            model_content_json = mark_recall_budget_consumed(model_content_json)
        owned_input = model_input.model_copy(update={"model_content_json": model_content_json})
        if transport_operation == "stream_head":
            key = _stream_key(owned_input)
            self._stream_inputs[key] = owned_input
            self._stream_inputs.move_to_end(key)
            while len(self._stream_inputs) > 32:
                self._stream_inputs.popitem(last=False)

        try:
            if request.correction_ordinal == 1:
                correction = getattr(self._author, "correct_role_result", None)
                if not callable(correction):
                    raise RuntimeError(
                        "inbound CharacterInterior same-author correction is unavailable"
                    )
                output = await correction(
                    owned_input,
                    request.correction_failure_code,
                )
            elif recovery_failure is None:
                if transport_operation == "stream_head":
                    operation = getattr(self._author, "propose_stream_head", None)
                    if not callable(operation):
                        raise RuntimeError("inbound CharacterInterior stream head is unavailable")
                    output = await operation(owned_input)
                else:
                    output = await self._author.propose(owned_input)
            else:
                recovery = getattr(self._author, "recover", None)
                if not callable(recovery):
                    raise RuntimeError("inbound CharacterInterior recovery is unavailable")
                output = await recovery(owned_input, recovery_failure)
        except _InboundRecallRequested as recall_choice:
            if request.recall_completed:
                raise RuntimeError(
                    "inbound role requested Recall after its one bounded pull"
                ) from recall_choice
            previous = self._recall_parents.get(request.inner_turn_id)
            if previous is not None and (
                previous.model_id != recall_choice.model_id
                or previous.model_version != recall_choice.model_version
                or previous.model_call_id != recall_choice.model_call_id
                or previous.request_hash != recall_choice.request_hash
                or previous.response_hash != recall_choice.response_hash
                or previous.private_turn_state != recall_choice.private_turn_state
            ):
                raise RuntimeError(
                    "inbound Recall choice changed within one Inner Turn"
                ) from recall_choice
            private_state = recall_choice.private_turn_state
            if private_state is None:
                if request.correction_ordinal == 0:
                    self._decision_parents[request.inner_turn_id] = recall_choice.model_call_id
                    self._decision_parents.move_to_end(request.inner_turn_id)
                raise _RoleResultContractError(
                    "private_turn_state_missing",
                    detail="recall choice has no final model-owned private state",
                    response_hash="sha256:" + recall_choice.response_hash,
                ) from recall_choice
            self._recall_parents[request.inner_turn_id] = recall_choice
            self._recall_parents.move_to_end(request.inner_turn_id)
            while len(self._recall_parents) > _CACHE_LIMIT:
                self._recall_parents.popitem(last=False)
            presentations = append_presented_prefetch(
                self._prefetch_presentations.get(request.inner_turn_id, ()),
                phase=("recovery_initial" if recovery_failure is not None else "initial"),
                model_call_id=recall_choice.model_call_id,
                trace=prefetch_trace,
            )
            self._prefetch_presentations[request.inner_turn_id] = presentations
            self._prefetch_presentations.move_to_end(request.inner_turn_id)
            while len(self._prefetch_presentations) > _CACHE_LIMIT:
                self._prefetch_presentations.popitem(last=False)
            return {
                "status": "recall_request",
                "summary": private_state.inner_state_summary,
                "recall_query": recall_choice.query,
                "attended_source_refs": tuple(
                    ref
                    for ref in private_state.attended_source_refs
                    if ref in request.snapshot.source_refs
                ),
                "proposals": (),
                "author_lineage": _InteriorAuthorLineage(
                    model_id=recall_choice.model_id,
                    model_version=recall_choice.model_version,
                    model_call_id=recall_choice.model_call_id,
                    request_hash="sha256:" + recall_choice.request_hash,
                    response_hash="sha256:" + recall_choice.response_hash,
                    attempt_ordinal=0,
                ).model_dump(mode="python"),
            }
        if not isinstance(output, ModelOutput):
            raise TypeError("inbound character author output is invalid")
        if output.prefetch_trace is not None or output.presented_prefetch_traces:
            raise RuntimeError("inbound author attempted to own the retired prefetch lifecycle")
        recall_parent = self._recall_parents.get(request.inner_turn_id)
        if recall_parent is not None:
            usage = _combine_usage(
                recall_parent.usage,
                output.usage,
                owned_input.call_id,
            )
            output = output.model_copy(
                update={
                    "usage": usage,
                    "input_tokens": usage.input_tokens if usage is not None else None,
                    "output_tokens": usage.output_tokens if usage is not None else None,
                }
            )
        winning_call = output.winning_model_call_id or owned_input.call_id
        presentations = append_presented_prefetch(
            self._prefetch_presentations.get(request.inner_turn_id, ()),
            phase=(
                "recall_followup"
                if request.recall_completed
                else "recovery_initial"
                if recovery_failure is not None
                else "initial"
            ),
            model_call_id=winning_call,
            trace=prefetch_trace,
        )
        self._prefetch_presentations[request.inner_turn_id] = presentations
        self._prefetch_presentations.move_to_end(request.inner_turn_id)
        while len(self._prefetch_presentations) > _CACHE_LIMIT:
            self._prefetch_presentations.popitem(last=False)
        output = output.model_copy(
            update={
                "recall_trace": recall_trace,
                "presented_prefetch_traces": presentations,
            }
        )
        proposal = validate_proposal_envelope(output.raw_proposal)
        private = getattr(proposal, "private_turn_state", None)
        if private is None:
            if request.correction_ordinal == 0:
                self._decision_parents[request.inner_turn_id] = winning_call
                self._decision_parents.move_to_end(request.inner_turn_id)
                while len(self._decision_parents) > _CACHE_LIMIT:
                    self._decision_parents.popitem(last=False)
            raise _RoleResultContractError(
                "private_turn_state_missing",
                detail="expression decision has no final model-owned private state",
                response_hash="sha256:" + _digest(output.raw_proposal),
            )
        output_hash = "sha256:" + _digest(output.model_dump(mode="json"))
        output_ref = "inbound-turn-output:sha256:" + _digest(
            {
                "inner_turn_id": request.inner_turn_id,
                "output_hash": output_hash,
                "proposal_hash": proposal.proposal_hash,
            }
        )
        self._outputs[output_ref] = output
        self._outputs.move_to_end(output_ref)
        while len(self._outputs) > _CACHE_LIMIT:
            self._outputs.popitem(last=False)

        summary = private.inner_state_summary
        attended = tuple(
            ref
            for ref in getattr(private, "attended_source_refs", ())
            if ref in request.snapshot.source_refs
        )
        winning_request_hash = output.winning_request_hash or _digest(
            owned_input.model_dump(mode="json")
        )
        if request.correction_ordinal == 1:
            parent_model_call_id = self._decision_parents.pop(request.inner_turn_id, None)
        else:
            # Recall parentage belongs to the explicit private-self lineage.
            # ``_InteriorAuthorLineage.parent_model_call_id`` is reserved for
            # one bounded structural correction of this exact result.
            parent_model_call_id = None
            self._decision_parents[request.inner_turn_id] = winning_call
            self._decision_parents.move_to_end(request.inner_turn_id)
            while len(self._decision_parents) > _CACHE_LIMIT:
                self._decision_parents.popitem(last=False)
        if request.correction_ordinal == 1 and parent_model_call_id is None:
            raise RuntimeError("inbound role correction lost its parent author identity")
        lineage = _InteriorAuthorLineage(
            model_id=output.model_id,
            model_version=output.model_version,
            model_call_id=winning_call,
            request_hash="sha256:" + winning_request_hash,
            response_hash="sha256:" + _digest(output.raw_proposal),
            attempt_ordinal=1 if parent_model_call_id is not None else 0,
            parent_model_call_id=parent_model_call_id,
        )
        if recall_parent is not None:
            self._recall_parents.pop(request.inner_turn_id, None)
        self._prefetch_presentations.pop(request.inner_turn_id, None)
        return {
            "status": "decision",
            "summary": summary,
            "attended_source_refs": attended,
            "decision": {
                "contract": _DECISION_CONTRACT,
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "source_refs": list(manifest.source_refs),
                "output_ref": output_ref,
                "output_hash": output_hash,
                "proposal_hash": proposal.proposal_hash,
            },
            "proposals": (),
            "author_lineage": lineage.model_dump(mode="python"),
        }


class CharacterInteriorInboundDeliberationAdapter:
    """Deliberation port whose only semantic operation is ``consider``."""

    def __init__(
        self,
        *,
        interior: CharacterInterior,
        world_id: str,
        actor_ref: str,
    ) -> None:
        if not world_id or not actor_ref:
            raise ValueError("inbound CharacterInterior identity is required")
        self._interior = interior
        self._world_id = world_id
        self._actor_ref = actor_ref

    def source_closure_review_enabled(self) -> bool:
        # The combined internal Faculty owns the source-closure pass.  Surface
        # that fact so Deliberation opens its existing validation budget for
        # the same candidate; this does not create a second semantic author.
        faculty = self._interior._registry.for_purpose(_PURPOSE)  # noqa: SLF001
        operation = getattr(faculty, "source_closure_review_enabled", None)
        return bool(callable(operation) and operation())

    def has_hedge_provider(self, _request: ModelInput) -> bool:
        return False

    def provisional_provider_available(self, _request: ModelInput) -> bool:
        return False

    def shadow_observer_provider_available(self, _request: ModelInput) -> bool:
        return False

    def stream_provider_available(self, request: ModelInput) -> bool:
        return self._interior._purpose_transport_available(  # noqa: SLF001
            _PURPOSE,
            transport="stream",
            payload=request,
        )

    async def propose(self, request: ModelInput) -> ModelOutput:
        return await self._consider(request, recovery_failure=None)

    async def recover(self, request: ModelInput, failure_code: str) -> ModelOutput:
        return await self._consider(request, recovery_failure=failure_code)

    async def propose_stream_head(self, request: ModelInput) -> ModelOutput:
        try:
            return await self._consider(
                request,
                recovery_failure=None,
                transport_operation="stream_head",
            )
        except BaseException:
            self._interior._publish_purpose_transport(  # noqa: SLF001
                _PURPOSE,
                transport="stream_head",
                payload=request,
                output=None,
            )
            raise

    async def propose_stream_tail(self, request: ModelInput) -> ModelOutput:
        output = await self._interior._continue_purpose_transport(  # noqa: SLF001
            _PURPOSE,
            transport="stream_tail",
            payload=request,
        )
        if not isinstance(output, ModelOutput):
            raise RuntimeError("character_interior_inbound_stream_tail_invalid")
        return output

    def advance_expression_attention(self, attention_ref: str) -> None:
        self._interior._advance_purpose_attention(  # noqa: SLF001
            _PURPOSE,
            attention_ref=attention_ref,
        )

    async def recover_stream_head(self, request: ModelInput, failure_code: str) -> ModelOutput:
        return await self.recover(request, failure_code)

    async def _consider(
        self,
        request: ModelInput,
        *,
        recovery_failure: str | None,
        transport_operation: str = "complete",
    ) -> ModelOutput:
        manifest = self._interior._register_purpose_capability(  # noqa: SLF001
            _PURPOSE,
            request,
            recovery_failure=recovery_failure,
            transport_operation=transport_operation,
        )
        cursor = ProjectionCursor(
            world_revision=request.evaluated_world_revision,
            deliberation_revision=request.evaluated_deliberation_revision,
            ledger_sequence=request.evaluated_ledger_sequence,
        )
        identity = _digest(
            {
                "world_id": self._world_id,
                "actor_ref": self._actor_ref,
                "attempt_id": request.attempt_id,
                "capability_ref": manifest.capability_ref,
                "cursor": cursor.model_dump(mode="json"),
            }
        )
        # The inbound trigger is the accepted source epoch.  Keep the
        # provider-attempt identity above separate for transport/recovery, but
        # make every retry, recall continuation, and streamed head/tail of the
        # same observation share one durable source→opportunity identity.
        opportunity_identity = CausalOpportunityIdentity(
            world_id=self._world_id,
            actor_ref=self._actor_ref,
            purpose=_PURPOSE,
            source_refs=(request.trigger_ref,),
            epoch=request.trigger_ref,
        )
        decision = await self._interior.consider(
            InteriorOpportunity(
                inner_turn_ref=f"inbound-inner-turn:sha256:{identity}",
                world_id=self._world_id,
                actor_ref=self._actor_ref,
                trigger_ref=request.trigger_ref,
                cursor=cursor,
                logical_time=_logical_time(request),
                purpose=_PURPOSE,
                source_refs=(request.trigger_ref,),
                capability_manifest=manifest,
                opportunity_ref=opportunity_identity.opportunity_ref,
            )
        )
        if decision.status == "technical_failure":
            raise RuntimeError(
                "character_interior_inbound_" + (decision.failure_code or "technical_failure")
            )
        if decision.status != "decided" or not isinstance(decision.decision, dict):
            raise RuntimeError("character_interior_inbound_result_unavailable")
        payload = decision.decision
        if (
            payload.get("contract") != _DECISION_CONTRACT
            or payload.get("capability_ref") != manifest.capability_ref
            or payload.get("capability_payload_hash") != manifest.payload_hash
            or payload.get("source_refs") != list(manifest.source_refs)
        ):
            raise RuntimeError("character_interior_inbound_result_unbound")
        output_ref = payload.get("output_ref")
        output_hash = payload.get("output_hash")
        if not isinstance(output_ref, str) or not isinstance(output_hash, str):
            raise RuntimeError("character_interior_inbound_output_identity_missing")
        output = self._interior._consume_purpose_output(  # noqa: SLF001
            _PURPOSE,
            output_ref=output_ref,
            output_hash=output_hash,
        )
        if not isinstance(output, ModelOutput):
            raise RuntimeError("character_interior_inbound_output_invalid")
        proposal = validate_proposal_envelope(output.raw_proposal)
        if payload.get("proposal_hash") != proposal.proposal_hash:
            raise RuntimeError("character_interior_inbound_proposal_changed")
        lineage = recorded_character_interior_lineage(
            decision,
            purpose=_PURPOSE,
            subject_ref=opportunity_identity.opportunity_ref,
            capability_ref=manifest.capability_ref,
        )
        # The durable CharacterInterior lineage is the authoritative identity
        # of this provider result.  Carry it back onto the Deliberation
        # boundary as the winning invocation too; otherwise the outer audit
        # would combine the adapter's lineage with the Deliberation wrapper's
        # request hash and strict revalidation would (correctly) reject the
        # record as two different authors.
        output = output.model_copy(
            update={
                "character_interior_lineage": lineage,
                "winning_model_call_id": lineage.author_model_call_id,
                "winning_request_hash": lineage.author_request_hash.removeprefix("sha256:"),
            }
        )
        if transport_operation == "stream_head":
            self._interior._publish_purpose_transport(  # noqa: SLF001
                _PURPOSE,
                transport="stream_head",
                payload=request,
                output=output,
            )
        return output


def compose_character_interior_inbound_deliberation(
    *,
    interior: CharacterInterior,
    world_id: str,
    actor_ref: str,
) -> CharacterInteriorInboundDeliberationAdapter:
    """Return the one ordinary-inbound port from the frozen Interior module."""

    return CharacterInteriorInboundDeliberationAdapter(
        interior=interior,
        world_id=world_id,
        actor_ref=actor_ref,
    )


__all__: list[str] = []
