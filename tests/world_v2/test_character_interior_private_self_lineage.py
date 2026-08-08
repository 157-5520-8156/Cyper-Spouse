from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.character_interior import (
    CharacterInterior,
    InteriorOpportunity,
)
from companion_daemon.world_v2.character_interior.inbound_turn import InboundTurnFaculty
from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute
from companion_daemon.world_v2.private_turn_state import PrivateTurnState
from companion_daemon.world_v2.proposal_envelope import MinimalProposal
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.character_interior.inbound_author import (
    _InboundRecallRequested,
)


_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
_CURSOR = ProjectionCursor(
    world_revision=7,
    deliberation_revision=5,
    ledger_sequence=31,
)
_FACETS = (
    "private_self",
    "selective_memory",
    "appraisal_affect",
    "emotional_continuity",
    "subjective_relationship",
    "aspirations_conflicts",
    "autonomous_impulses",
    "expression_stance",
)


class _Projection:
    async def project(self, *, subject):
        return {
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "cursor": subject.cursor,
            "logical_time": subject.logical_time,
            "situation": {
                "availability": "available",
                "content": {"activity": "watching rain collect on the glass"},
                "source_refs": ("source:situation",),
            },
            "continuity": {
                "availability": "available",
                "content": {"open_thread": "the walk mentioned yesterday"},
                "source_refs": ("source:continuity",),
            },
            "facets": {
                name: {
                    "availability": "available",
                    "content": {"summary": name},
                    "source_refs": (f"source:{name}",),
                }
                for name in _FACETS
            },
        }


class _Recall:
    async def recall(self, request):
        return {
            "world_id": request.world_id,
            "actor_ref": request.actor_ref,
            "cursor": request.cursor,
            "content": {"recalled": "getting caught in the rain together"},
            "source_refs": ("experience:rain-walk",),
        }


def _author(call_id: str) -> dict[str, object]:
    return {
        "model_id": "role-model",
        "model_version": "role-model.1",
        "model_call_id": call_id,
        "request_hash": "sha256:" + "a" * 64,
        "response_hash": "sha256:" + "b" * 64,
        "attempt_ordinal": 0,
    }


class _RecallingRole:
    name = "recalling-role"
    purposes = ("inbound_turn",)
    requires_author_lineage = True

    async def experience(self, _request):
        raise AssertionError("this test uses consider")

    async def consider(self, request):
        if not request.recall_completed:
            return {
                "status": "recall_request",
                "summary": "The rain makes her reach for one particular shared walk.",
                "attended_source_refs": ("source:situation",),
                "recall_query": "the shared walk when rain changed the mood",
                "proposals": (),
                "author_lineage": _author("model-call:initial-private-self"),
            }
        return {
            "status": "decision",
            "summary": "Remembering it leaves her wanting to answer more softly.",
            "attended_source_refs": (
                "source:situation",
                "experience:rain-walk",
            ),
            "decision": {"expression_mode": "reply"},
            "proposals": (),
            "author_lineage": _author("model-call:final-private-self"),
        }


class _UnexpectedRoleFailure:
    name = "unexpected-role-failure"
    purposes = ("inbound_turn",)

    @property
    def requires_author_lineage(self):
        raise RuntimeError("broken Faculty metadata")

    async def experience(self, _request):
        raise AssertionError("this test uses consider")

    async def consider(self, _request):
        return {
            "status": "silent",
            "summary": "This otherwise valid result must not escape unaudited.",
            "proposals": (),
        }


class _CancelledRole:
    name = "cancelled-role"
    purposes = ("inbound_turn",)

    async def experience(self, _request):
        raise AssertionError("this test uses consider")

    async def consider(self, _request):
        raise asyncio.CancelledError


class _RecallingInboundCognition:
    _recall = None

    def __init__(self) -> None:
        self.calls = 0

    async def propose(self, request):
        self.calls += 1
        if self.calls == 1:
            raise _InboundRecallRequested(
                query="the walk this rain brought back",
                model_id="combined-role",
                model_version="combined-role.1",
                model_call_id="model-call:inbound-initial",
                request_hash="c" * 64,
                response_hash="d" * 64,
                usage=None,
                private_turn_state=PrivateTurnState(
                    inner_state_summary=(
                        "The current rain makes one particular shared walk salient."
                    ),
                    attended_source_refs=("source:situation",),
                ),
            )
        proposal = MinimalProposal(
            proposal_id="proposal:inbound-after-recall",
            trigger_ref="source:situation",
            evaluated_world_revision=_CURSOR.world_revision,
            confidence=7_200,
            brief_rationale="The selected memory changed how she wants to answer.",
            private_turn_state=PrivateTurnState(
                inner_state_summary="She now wants to answer with remembered tenderness.",
                attended_source_refs=("experience:rain-walk",),
            ),
            source_model_result="model-result:inbound-after-recall",
            response_text="I remember that walk.",
            stance="answer_without_world_claims",
        )
        return ModelOutput(
            model_id="combined-role",
            model_version="combined-role.1",
            raw_proposal=proposal.model_dump(mode="json"),
            winning_model_call_id="model-call:inbound-final",
            winning_request_hash="e" * 64,
        )


class _MissingPrivateSelfThenCorrectedInboundCognition:
    """A malformed first wire may only be replaced by the same role author."""

    _recall = None

    def __init__(self) -> None:
        self.propose_calls = 0
        self.recovery_failures: list[str] = []

    @staticmethod
    def _output(*, private_state: PrivateTurnState | None, call_id: str) -> ModelOutput:
        proposal = MinimalProposal(
            proposal_id=f"proposal:{call_id}",
            trigger_ref="source:situation",
            evaluated_world_revision=_CURSOR.world_revision,
            confidence=7_200,
            brief_rationale="This rationale must never substitute for private self.",
            private_turn_state=private_state,
            source_model_result=f"model-result:{call_id}",
            response_text="I am here.",
            stance="answer_without_world_claims",
        )
        return ModelOutput(
            model_id="combined-role",
            model_version="combined-role.1",
            raw_proposal=proposal.model_dump(mode="json"),
            winning_model_call_id=f"model-call:{call_id}",
            winning_request_hash=("c" if private_state is None else "d") * 64,
        )

    async def propose(self, _request: ModelInput) -> ModelOutput:
        self.propose_calls += 1
        return self._output(private_state=None, call_id="missing-private-self")

    async def correct_role_result(
        self,
        _request: ModelInput,
        failure_code: str,
    ) -> ModelOutput:
        self.recovery_failures.append(failure_code)
        return self._output(
            private_state=PrivateTurnState(
                inner_state_summary="She chooses to answer after forming her own present stance.",
                attended_source_refs=("source:situation",),
            ),
            call_id="corrected-private-self",
        )


class _MissingPrivateSelfAfterCorrectionInboundCognition(
    _MissingPrivateSelfThenCorrectedInboundCognition
):
    async def correct_role_result(
        self,
        _request: ModelInput,
        failure_code: str,
    ) -> ModelOutput:
        self.recovery_failures.append(failure_code)
        return self._output(
            private_state=None,
            call_id="still-missing-private-self",
        )


def _opportunity(*, suffix: str = "lineage") -> InteriorOpportunity:
    return InteriorOpportunity(
        opportunity_ref=f"opportunity:{suffix}",
        inner_turn_ref=f"inner-turn:{suffix}",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref=f"trigger:{suffix}",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="inbound_turn",
        source_refs=("source:situation",),
    )


@pytest.mark.asyncio
async def test_recall_keeps_initial_and_final_private_self_in_one_explicit_lineage() -> None:
    interior = CharacterInterior(
        projection=_Projection(),
        role=_RecallingRole(),
        recall=_Recall(),
    )

    result = await interior.consider(_opportunity())

    assert result.status == "decided"
    assert result.private_self_lineage is not None
    assert result.private_self_lineage.relation == "selective_recall"
    assert result.private_self_lineage.recall_query == (
        "the shared walk when rain changed the mood"
    )
    assert result.private_self_lineage.initial_private_self.summary == (
        "The rain makes her reach for one particular shared walk."
    )
    assert result.private_self_lineage.final_private_self.summary == (
        "Remembering it leaves her wanting to answer more softly."
    )
    assert result.private_self_lineage.initial_author_lineage is not None
    assert result.private_self_lineage.final_author_lineage is not None
    assert result.private_self_lineage.final_parent_model_call_id == (
        "model-call:initial-private-self"
    )
    assert result.private_self_lineage.initial_snapshot_id != (
        result.private_self_lineage.final_snapshot_id
    )
    assert result.instant_private_self == (
        result.private_self_lineage.final_private_self
    )
    assert result.author_lineage == result.private_self_lineage.final_author_lineage


@pytest.mark.asyncio
async def test_unexpected_consider_runtime_error_is_a_technical_failure() -> None:
    interior = CharacterInterior(
        projection=_Projection(),
        role=_UnexpectedRoleFailure(),
    )

    result = await interior.consider(_opportunity(suffix="runtime-failure"))

    assert result.status == "technical_failure"
    assert result.failure_code == "interior_runtime_failure"
    assert result.summary is None
    assert result.private_self_lineage is None


@pytest.mark.asyncio
async def test_consider_does_not_turn_task_cancellation_into_a_terminal_failure() -> None:
    interior = CharacterInterior(
        projection=_Projection(),
        role=_CancelledRole(),
    )

    with pytest.raises(asyncio.CancelledError):
        await interior.consider(_opportunity(suffix="cancelled"))


@pytest.mark.asyncio
async def test_inbound_recall_preserves_the_model_authored_initial_private_self() -> None:
    cognition = _RecallingInboundCognition()
    faculty = InboundTurnFaculty(author=cognition)
    interior = CharacterInterior(
        projection=_Projection(),
        role=faculty,
        recall=_Recall(),
    )
    model_input = ModelInput(
        call_id="model-input:inbound-lineage",
        attempt_id="attempt:inbound-lineage",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="f" * 64,
        trigger_ref="source:situation",
        evaluated_world_revision=_CURSOR.world_revision,
        evaluated_deliberation_revision=_CURSOR.deliberation_revision,
        evaluated_ledger_sequence=_CURSOR.ledger_sequence,
        model_content_json=json.dumps(
            {"logical_time": _NOW.isoformat(), "slices": {}},
            separators=(",", ":"),
        ),
    )
    manifest = faculty.register_capability(model_input)
    opportunity = _opportunity(suffix="inbound-lineage").model_copy(
        update={"capability_manifest": manifest}
    )

    result = await interior.consider(opportunity)

    assert result.status == "decided"
    assert cognition.calls == 2
    assert result.private_self_lineage is not None
    assert result.private_self_lineage.initial_private_self.summary == (
        "The current rain makes one particular shared walk salient."
    )
    assert result.private_self_lineage.final_private_self.summary == (
        "She now wants to answer with remembered tenderness."
    )
    assert result.private_self_lineage.final_parent_model_call_id == (
        "model-call:inbound-initial"
    )
    assert result.author_lineage is not None
    assert result.author_lineage.attempt_ordinal == 0
    assert result.author_lineage.parent_model_call_id is None


@pytest.mark.asyncio
async def test_inbound_expression_without_private_self_uses_same_author_correction() -> None:
    cognition = _MissingPrivateSelfThenCorrectedInboundCognition()
    faculty = InboundTurnFaculty(author=cognition)
    interior = CharacterInterior(
        projection=_Projection(),
        role=faculty,
    )
    model_input = ModelInput(
        call_id="model-input:missing-private-self",
        attempt_id="attempt:missing-private-self",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="f" * 64,
        trigger_ref="source:situation",
        evaluated_world_revision=_CURSOR.world_revision,
        evaluated_deliberation_revision=_CURSOR.deliberation_revision,
        evaluated_ledger_sequence=_CURSOR.ledger_sequence,
        model_content_json=json.dumps(
            {"logical_time": _NOW.isoformat(), "slices": {}},
            separators=(",", ":"),
        ),
    )
    manifest = faculty.register_capability(model_input)
    opportunity = _opportunity(suffix="missing-private-self").model_copy(
        update={"capability_manifest": manifest}
    )

    result = await interior.consider(opportunity)

    assert result.status == "decided"
    assert cognition.propose_calls == 1
    assert cognition.recovery_failures == ["private_turn_state_missing"]
    assert result.summary == (
        "She chooses to answer after forming her own present stance."
    )
    assert result.summary != "This rationale must never substitute for private self."
    assert result.instant_private_self is not None
    assert result.instant_private_self.summary == result.summary
    assert result.author_lineage is not None
    assert result.author_lineage.model_call_id == "model-call:corrected-private-self"
    assert result.author_lineage.parent_model_call_id == "model-call:missing-private-self"


@pytest.mark.asyncio
async def test_inbound_expression_still_without_private_self_is_technical_failure() -> None:
    cognition = _MissingPrivateSelfAfterCorrectionInboundCognition()
    faculty = InboundTurnFaculty(author=cognition)
    interior = CharacterInterior(projection=_Projection(), role=faculty)
    model_input = ModelInput(
        call_id="model-input:still-missing-private-self",
        attempt_id="attempt:still-missing-private-self",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="f" * 64,
        trigger_ref="source:situation",
        evaluated_world_revision=_CURSOR.world_revision,
        evaluated_deliberation_revision=_CURSOR.deliberation_revision,
        evaluated_ledger_sequence=_CURSOR.ledger_sequence,
        model_content_json=json.dumps(
            {"logical_time": _NOW.isoformat(), "slices": {}},
            separators=(",", ":"),
        ),
    )
    opportunity = _opportunity(suffix="still-missing-private-self").model_copy(
        update={"capability_manifest": faculty.register_capability(model_input)}
    )

    result = await interior.consider(opportunity)

    assert cognition.propose_calls == 1
    assert cognition.recovery_failures == ["private_turn_state_missing"]
    assert result.status == "technical_failure"
    assert result.failure_code == "invalid_role_result_after_correction"
    assert result.summary is None
    assert result.instant_private_self is None
    assert result.decision is None
