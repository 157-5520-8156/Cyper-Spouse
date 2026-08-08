from __future__ import annotations

from datetime import UTC, datetime
import json

import pytest

from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.character_interior.contracts import FACET_NAMES
from companion_daemon.world_v2.character_interior.structured_role import (
    StructuredCharacterRoleFaculty,
)
from companion_daemon.world_v2.deliberation import (
    ModelInput,
    ModelRoute,
    ValidationTechnicalFailure,
)
from companion_daemon.world_v2.expression_draft import TEXT_ONLY_EXPRESSION_CAPABILITIES
from companion_daemon.world_v2.proactive_action import (
    _CharacterInteriorProactiveTransport,
)
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    ProposalEvidenceRef,
    validate_proposal_envelope,
)
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)
SOURCE = "event:clock:ambient:1"
CURSOR = ProjectionCursor(
    world_revision=9,
    deliberation_revision=0,
    ledger_sequence=12,
)


class _Projection:
    async def project(self, *, subject):
        return {
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "cursor": subject.cursor,
            "logical_time": NOW,
            "situation": {
                "availability": "available",
                "content": {"activity": "sitting by the window"},
                "source_refs": (SOURCE,),
            },
            "continuity": {
                "availability": "available",
                "content": {"relationship": "ongoing"},
                "source_refs": (SOURCE,),
            },
            "facets": {
                name: {
                    "availability": "available",
                    "content": {"summary": name},
                    "source_refs": (SOURCE,),
                }
                for name in FACET_NAMES
            },
        }


class _Model:
    model = "character-model"

    def __init__(self, response: object) -> None:
        self.response = response
        self.calls = 0

    async def complete(self, messages, *, temperature=0.8):
        del messages, temperature
        self.calls += 1
        if isinstance(self.response, BaseException):
            raise self.response
        return json.dumps(self.response, ensure_ascii=False)


def _request() -> ModelInput:
    return ModelInput(
        call_id="call:proactive:interior:1",
        attempt_id="attempt:proactive:interior:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="a" * 64,
        trigger_ref=SOURCE,
        evaluated_world_revision=CURSOR.world_revision,
        evaluated_deliberation_revision=CURSOR.deliberation_revision,
        evaluated_ledger_sequence=CURSOR.ledger_sequence,
        trigger_evidence=(
            ProposalEvidenceRef(
                ref_id=SOURCE,
                evidence_kind="committed_world_event",
                source_world_revision=CURSOR.world_revision,
                immutable_hash="sha256:" + "b" * 64,
            ),
        ),
        model_content_json=json.dumps(
            {
                "logical_time": NOW.isoformat(),
                "slices": {
                    "advisories": {
                        "items": [
                            {
                                "value": {
                                    "kind": "proactive_opportunity",
                                    "candidate_refs": ["ambient_presence:epoch:1"],
                                    "source_refs": [SOURCE],
                                    "candidates": [{"value": "ambient context"}],
                                }
                            }
                        ]
                    }
                },
            },
            ensure_ascii=False,
        ),
    )


def _adapter(model: _Model) -> _CharacterInteriorProactiveTransport:
    interior = CharacterInterior(
        projection=_Projection(),
        role=StructuredCharacterRoleFaculty(model=model, model_id=model.model),
    )
    return _CharacterInteriorProactiveTransport(
        character_interior=interior,
        world_id="world:test",
        actor_ref="character:zhizhi",
        target="user:primary",
        expression_capabilities=TEXT_ONLY_EXPRESSION_CAPABILITIES,
    )


@pytest.mark.asyncio
async def test_proactive_business_opportunity_uses_character_interior_consider() -> None:
    model = _Model(
        {
            "status": "decision",
            "summary": "She noticed the impulse but wants to leave it private.",
            "attended_source_refs": [SOURCE],
            "decision": {
                "source_refs": [SOURCE],
                "payload": {
                    "timing_choice": "silent",
                    "beats": [],
                    "stance": "quietly keeping it to herself",
                    "brief_rationale": "she does not want to reach out this time",
                    "impulse_summary": "the other person crossed her mind",
                    "confidence": 7200,
                    "world_claims": [],
                },
            },
            "recall_query": None,
            "proposals": [],
        }
    )

    output = await _adapter(model).propose(_request())

    proposal = validate_proposal_envelope(output.raw_proposal)
    assert isinstance(proposal, DecisionProposal)
    assert proposal.timing_choice == "silent"
    assert proposal.private_turn_state.inner_state_summary.startswith("She noticed")
    assert proposal.proactive_opportunity_decision.disposition == (
        "silent_after_consideration"
    )
    assert model.calls == 1


@pytest.mark.asyncio
async def test_proactive_interior_technical_failure_is_not_materialized_as_silence() -> None:
    model = _Model(TimeoutError("provider unavailable"))

    with pytest.raises(ValidationTechnicalFailure):
        await _adapter(model).propose(_request())

    # Provider failure is already technical, so CharacterInterior neither
    # substitutes another author nor spends the structural-correction pass.
    assert model.calls == 1
