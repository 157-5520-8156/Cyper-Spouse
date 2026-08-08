"""QQ attachment perception is one source-bound CharacterInterior choice."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from companion_daemon.world_v2.character_interior.contracts import InnerDecision
from companion_daemon.world_v2.character_interior.qq_attachment_perception import (
    CharacterInteriorQQAttachmentPerceptionPort,
    QQAttachmentPerceptionTechnicalFailure,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.perception_proposal_compiler import perception_input_ref
from companion_daemon.world_v2.production_proposal_grammar import (
    ProductionProposalGrammar,
    SpecializedProposalCapability,
)
from companion_daemon.world_v2.proposal_envelope import (
    DecisionProposal,
    ProposalEvidenceRef,
    validate_proposal_envelope,
)
from companion_daemon.world_v2.qq_attachment_archive import QQAttachmentArchive
from companion_daemon.world_v2.schemas import ProjectionCursor


NOW = datetime(2026, 7, 20, 5, 0, tzinfo=UTC)
CURSOR = ProjectionCursor(
    world_revision=7,
    deliberation_revision=3,
    ledger_sequence=19,
)
IMAGE_REF = "qq-attachment:image:sha256:" + "a" * 64
AUDIO_REF = "qq-attachment:record:sha256:" + "b" * 64
SNAPSHOT_HASH = "4" * 64
SNAPSHOT_ID = f"inner-life-snapshot:sha256:{SNAPSHOT_HASH}"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"character-interior-perception"

GRAMMAR = ProductionProposalGrammar(
    lane_id="perception",  # type: ignore[arg-type]
    capabilities=(
        SpecializedProposalCapability(
            change_kind="perception_request",
            transition="request",
            compiler_ref="perception-proposal-compiler.2",
            manifest_ref="perception-acceptance.1",
            reverse_verifier_ref="perception-authorization.1",
            allows_actions=True,
            action_kinds=frozenset({"vision", "transcription"}),
        ),
    ),
    allows_no_change_decision=True,
)


class _Evidence:
    def __init__(
        self,
        *,
        count: int = 0,
        seen_hashes: frozenset[str] = frozenset(),
        broken: bool = False,
    ) -> None:
        self.count = count
        self.seen_hashes = seen_hashes
        self.broken = broken

    def dispatched_count_since(self, cutoff: datetime) -> int:
        del cutoff
        if self.broken:
            raise RuntimeError("evidence unavailable")
        return self.count

    def has_result_for_input(self, *, input_hash: str) -> bool:
        if self.broken:
            raise RuntimeError("evidence unavailable")
        return input_hash in self.seen_hashes


class _Interior:
    def __init__(self, terminal: str) -> None:
        self.terminal = terminal
        self.opportunities = []

    async def consider(self, opportunity):
        self.opportunities.append(opportunity)
        identity = dict(
            inner_turn_id="character-inner-turn:perception-test",
            opportunity_ref=opportunity.opportunity_ref,
            actor_ref=opportunity.actor_ref,
            cursor=opportunity.cursor,
        )
        if self.terminal == "technical":
            return InnerDecision(
                **identity,
                status="technical_failure",
                failure_code="provider_timeout",
            )
        author = {
            "model_id": "character-role:test",
            "model_version": "character-role:test.1",
            "model_call_id": "model-call:perception-test",
            "request_hash": "sha256:" + "6" * 64,
            "response_hash": "sha256:" + "7" * 64,
            "attempt_ordinal": 0,
        }
        if self.terminal == "silent":
            summary = "She does not choose to inspect an attachment this time."
            return InnerDecision(
                **identity,
                snapshot_id=SNAPSHOT_ID,
                snapshot_hash=SNAPSHOT_HASH,
                status="model_silent",
                summary=summary,
                instant_private_self={"summary": summary},
                private_self_lineage={
                    "relation": "single_pass",
                    "initial_private_self": {"summary": summary},
                    "initial_snapshot_id": SNAPSHOT_ID,
                    "initial_snapshot_hash": SNAPSHOT_HASH,
                    "initial_author_lineage": author,
                    "final_private_self": {"summary": summary},
                    "final_snapshot_id": SNAPSHOT_ID,
                    "final_snapshot_hash": SNAPSHOT_HASH,
                    "final_author_lineage": author,
                },
                author_lineage=author,
            )
        manifest = opportunity.capability_manifest
        assert manifest is not None
        summary = "She chooses one available attachment capability."
        return InnerDecision(
            **identity,
            snapshot_id=SNAPSHOT_ID,
            snapshot_hash=SNAPSHOT_HASH,
            status="decided",
            summary=summary,
            instant_private_self={"summary": summary},
            private_self_lineage={
                "relation": "single_pass",
                "initial_private_self": {"summary": summary},
                "initial_snapshot_id": SNAPSHOT_ID,
                "initial_snapshot_hash": SNAPSHOT_HASH,
                "initial_author_lineage": author,
                "final_private_self": {"summary": summary},
                "final_snapshot_id": SNAPSHOT_ID,
                "final_snapshot_hash": SNAPSHOT_HASH,
                "final_author_lineage": author,
            },
            author_lineage=author,
            decision={
                "contract": "character-interior-purpose-decision.1",
                "purpose": "qq_attachment_perception",
                "source_refs": list(manifest.source_refs),
                "capability_ref": manifest.capability_ref,
                "capability_payload_hash": manifest.payload_hash,
                "payload": {
                    "contract": ("character-interior-qq-attachment-perception-decision.1"),
                    "selected_token": IMAGE_REF,
                    "free_reason": "她自己想看",
                },
            },
        )


def _archive(tmp_path: Path, *, stored: bool = True) -> QQAttachmentArchive:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    if stored:
        archive.store(IMAGE_REF, PNG_BYTES)
    return archive


def _request(*, attachment_refs: tuple[str, ...] = (IMAGE_REF,)) -> ModelInput:
    media_types = tuple("image" if item == IMAGE_REF else "audio" for item in attachment_refs)
    content = {
        "world_id": "world:qq-perception",
        "actor_ref": "agent:companion",
        "logical_time": NOW.isoformat(),
        "inner_life_snapshot": {
            "contract": "inner-life-snapshot.1",
            "snapshot_id": SNAPSHOT_ID,
            "snapshot_hash": SNAPSHOT_HASH,
            "source_refs": ["observation:qq:1", "source:private-self"],
            "cursor": {
                **CURSOR.model_dump(mode="json"),
                "logical_time": NOW.isoformat(),
            },
        },
    }
    evidence = ProposalEvidenceRef(
        ref_id="observation:qq:1",
        evidence_kind="observed_message",
        source_world_revision=CURSOR.world_revision,
        immutable_hash="sha256:" + "9" * 64,
    )
    return ModelInput(
        call_id="model-call:qq-perception",
        attempt_id="attempt:qq-perception:1",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="c" * 64,
        trigger_ref="event:observation:qq:1",
        evaluated_world_revision=CURSOR.world_revision,
        evaluated_deliberation_revision=CURSOR.deliberation_revision,
        evaluated_ledger_sequence=CURSOR.ledger_sequence,
        model_content_json=json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        trigger_evidence=(evidence,),
        trigger_message=TriggerMessage(
            event_ref="event:observation:qq:1",
            event_payload_hash="sha256:" + "9" * 64,
            observation_ref="observation:qq:1",
            source_world_revision=CURSOR.world_revision,
            actor="user:geoff",
            channel="conversation:qq:c2c:10001",
            reply_target="conversation:qq:c2c:10001",
            text="给你看",
            attachment_refs=attachment_refs,
            attachment_media_types=media_types,
        ),
    )


def _port(
    tmp_path: Path,
    *,
    interior: _Interior,
    evidence: _Evidence | None = None,
    stored: bool = True,
) -> CharacterInteriorQQAttachmentPerceptionPort:
    return CharacterInteriorQQAttachmentPerceptionPort(
        character_interior=interior,  # type: ignore[arg-type]
        input_source=_archive(tmp_path, stored=stored),
        dispatch_evidence=evidence or _Evidence(),
        budget_account_id="account:world-v2:perception",
        budget_limit=12,
        daily_limit=12,
        local_timezone="Asia/Shanghai",
        now=lambda: NOW,
    )


def _proposal(raw: dict) -> DecisionProposal:
    proposal = validate_proposal_envelope(raw)
    assert isinstance(proposal, DecisionProposal)
    GRAMMAR.validate(proposal)
    return proposal


@pytest.mark.asyncio
async def test_selected_attachment_uses_public_character_interior_once(
    tmp_path: Path,
) -> None:
    interior = _Interior("decided")
    output = await _port(tmp_path, interior=interior).propose(_request())

    assert len(interior.opportunities) == 1
    assert output.character_interior_lineage is not None
    assert output.character_interior_lineage.purpose == "qq_attachment_perception"
    opportunity = interior.opportunities[0]
    assert opportunity.purpose == "qq_attachment_perception"
    assert opportunity.cursor == CURSOR
    assert opportunity.source_refs == ("event:observation:qq:1",)
    manifest = opportunity.capability_manifest
    assert manifest is not None
    assert manifest.capability_kind == "qq_attachment_perception"
    assert manifest.payload["offered_tokens"] == [IMAGE_REF]
    assert manifest.payload["attachments"] == [
        {
            "analysis_kind": "vision",
            "attachment_ref": IMAGE_REF,
            "attachment_token": IMAGE_REF,
            "input_hash": manifest.payload["attachments"][0]["input_hash"],
            "media_type": "image",
        }
    ]
    assert "inner_life_binding" not in manifest.payload
    assert manifest.payload["pinned_cursor"] == CURSOR.model_dump(mode="json")
    assert "should" not in manifest.payload_json.casefold()

    proposal = _proposal(output.raw_proposal)
    assert len(proposal.proposed_changes) == len(proposal.action_intents) == 1
    change = proposal.proposed_changes[0]
    intent = proposal.action_intents[0]
    assert change.payload.value()["attachment_ref"] == IMAGE_REF
    assert intent.payload_ref == perception_input_ref(
        proposal_id=proposal.proposal_id,
        change_id=change.change_id,
    )


@pytest.mark.asyncio
async def test_model_silence_is_a_real_no_change_but_technical_failure_is_not(
    tmp_path: Path,
) -> None:
    silent = _Interior("silent")
    silent_output = await _port(tmp_path / "silent", interior=silent).propose(_request())
    assert _proposal(silent_output.raw_proposal).proposed_changes == ()

    technical = _Interior("technical")
    with pytest.raises(QQAttachmentPerceptionTechnicalFailure) as raised:
        await _port(tmp_path / "technical", interior=technical).propose(_request())
    assert raised.value.code == "provider_timeout"


@pytest.mark.asyncio
async def test_hard_capability_gates_do_not_manufacture_a_character_choice(
    tmp_path: Path,
) -> None:
    cases = (
        (_request(attachment_refs=(AUDIO_REF,)), _Evidence(), True),
        (_request(), _Evidence(), False),
        (_request(), _Evidence(count=12), True),
    )
    for ordinal, (request, evidence, stored) in enumerate(cases):
        interior = _Interior("decided")
        output = await _port(
            tmp_path / str(ordinal),
            interior=interior,
            evidence=evidence,
            stored=stored,
        ).propose(request)
        assert _proposal(output.raw_proposal).proposed_changes == ()
        assert interior.opportunities == []


@pytest.mark.asyncio
async def test_broken_dispatch_evidence_is_technical_not_a_decline(tmp_path: Path) -> None:
    interior = _Interior("decided")
    with pytest.raises(QQAttachmentPerceptionTechnicalFailure) as raised:
        await _port(
            tmp_path,
            interior=interior,
            evidence=_Evidence(broken=True),
        ).propose(_request())
    assert raised.value.code == "perception_dispatch_evidence_unavailable"
    assert interior.opportunities == []
