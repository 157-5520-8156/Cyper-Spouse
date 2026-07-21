"""The QQ perception decision adapter keeps the lane audited and restrained."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
from pathlib import Path

import pytest

from companion_daemon.world_v2.deliberation import ModelInput, ModelRoute, TriggerMessage
from companion_daemon.world_v2.perception_decision_adapter import QQPerceptionDecisionModel
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


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"decision-test-png"
IMAGE_REF = "qq-attachment:image:sha256:" + "a" * 64
AUDIO_REF = "qq-attachment:record:sha256:" + "b" * 64

# The exact closed grammar installed by compose_injected_perception_deliberation.
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


class _Decision:
    """Scripted decision model; records whether it was consulted."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.calls = 0

    async def complete(self, messages, *, temperature: float = 0.8) -> str:
        self.calls += 1
        assert messages[0]["role"] == "system"
        return self.raw


class _Evidence:
    def __init__(self, *, count: int = 0, seen_hashes: frozenset[str] = frozenset()) -> None:
        self.count = count
        self.seen_hashes = seen_hashes

    def dispatched_count_since(self, cutoff: datetime) -> int:
        return self.count

    def has_result_for_input(self, *, input_hash: str) -> bool:
        return input_hash in self.seen_hashes


def _archive(tmp_path: Path, *, store_image: bool = True) -> QQAttachmentArchive:
    archive = QQAttachmentArchive(tmp_path / "attachments")
    if store_image:
        archive.store(IMAGE_REF, PNG_BYTES)
    return archive


def _request(
    *,
    text: str | None = "你看这张图",
    attachment_refs: tuple[str, ...] = (IMAGE_REF,),
) -> ModelInput:
    media_types = tuple(
        "image" if ":image:" in ref else "audio" for ref in attachment_refs
    )
    evidence = (
        ProposalEvidenceRef(
            ref_id="observation:qq:1",
            evidence_kind="observed_message",
            source_world_revision=7,
            immutable_hash="sha256:" + "9" * 64,
        ),
    )
    return ModelInput(
        call_id="model-call:test",
        attempt_id="attempt:test",
        route=ModelRoute(tier="flash", reason_code="test", router_version="test.1"),
        capsule_id="c" * 64,
        trigger_ref="event:observation:qq:1",
        evaluated_world_revision=7,
        model_content_json="{}",
        trigger_evidence=evidence,
        trigger_message=TriggerMessage(
            event_ref="event:observation:qq:1",
            event_payload_hash="sha256:" + "9" * 64,
            observation_ref="observation:qq:1",
            source_world_revision=7,
            actor="user:geoff",
            channel="conversation:qq:c2c:10001",
            reply_target="conversation:qq:c2c:10001",
            text=text,
            attachment_refs=attachment_refs,
            attachment_media_types=media_types,
        ),
    )


def _adapter(
    tmp_path: Path,
    *,
    decision: _Decision,
    evidence: _Evidence | None = None,
    store_image: bool = True,
    daily_limit: int = 12,
) -> QQPerceptionDecisionModel:
    return QQPerceptionDecisionModel(
        model=decision,
        input_source=_archive(tmp_path, store_image=store_image),
        dispatch_evidence=evidence or _Evidence(),
        budget_account_id="account:world-v2:perception",
        budget_limit=12,
        daily_limit=daily_limit,
        local_timezone="Asia/Shanghai",
        now=lambda: datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
    )


def _validated(raw: dict) -> DecisionProposal:
    proposal = validate_proposal_envelope(raw)
    assert isinstance(proposal, DecisionProposal)
    GRAMMAR.validate(proposal)
    return proposal


@pytest.mark.asyncio
async def test_selected_image_becomes_one_closed_perception_request(tmp_path: Path) -> None:
    decision = _Decision('{"look": true, "attachment_index": 0, "reason": "像是她的猫"}')
    adapter = _adapter(tmp_path, decision=decision)
    output = await adapter.propose(_request())
    proposal = _validated(output.raw_proposal)
    assert decision.calls == 1
    assert len(proposal.proposed_changes) == 1 and len(proposal.action_intents) == 1
    change, intent = proposal.proposed_changes[0], proposal.action_intents[0]
    payload = change.payload.value()
    assert change.kind == "perception_request" and change.transition == "request"
    assert change.target_id == "perception:vision"
    assert payload == {
        "analysis_kind": "vision",
        "attachment_ref": IMAGE_REF,
        "content_privacy_class": "private",
        "budget_account_id": "account:world-v2:perception",
        "budget_limit": 12,
    }
    assert intent.kind == "vision" and intent.layer == "perception_tool"
    assert intent.payload_ref == perception_input_ref(
        proposal_id=proposal.proposal_id, change_id=change.change_id
    )
    assert intent.payload_hash == (
        "sha256:" + hashlib.sha256(IMAGE_REF.encode()).hexdigest()
    )
    assert intent.causal_change_id == change.change_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    ['{"look": false, "reason": "只是表情包"}', "not json at all", '{"look": "maybe"}'],
)
async def test_model_decline_or_garbage_is_an_audited_no_change(
    tmp_path: Path, raw: str
) -> None:
    adapter = _adapter(tmp_path, decision=_Decision(raw))
    output = await adapter.propose(_request())
    proposal = _validated(output.raw_proposal)
    assert proposal.proposed_changes == () and proposal.action_intents == ()


@pytest.mark.asyncio
async def test_deterministic_gates_decline_without_consulting_the_model(
    tmp_path: Path,
) -> None:
    always_look = '{"look": true, "attachment_index": 0, "reason": "x"}'

    # No image attachment at all (audio-only).
    decision = _Decision(always_look)
    output = await _adapter(tmp_path / "audio", decision=decision).propose(
        _request(attachment_refs=(AUDIO_REF,))
    )
    assert _validated(output.raw_proposal).proposed_changes == ()
    assert decision.calls == 0

    # Image ref present but bytes were never archived.
    decision = _Decision(always_look)
    output = await _adapter(
        tmp_path / "unarchived", decision=decision, store_image=False
    ).propose(_request())
    assert _validated(output.raw_proposal).proposed_changes == ()
    assert decision.calls == 0

    # Durable daily cap reached.
    decision = _Decision(always_look)
    output = await _adapter(
        tmp_path / "capped", decision=decision, evidence=_Evidence(count=12), daily_limit=12
    ).propose(_request())
    assert _validated(output.raw_proposal).proposed_changes == ()
    assert decision.calls == 0

    # Exact bytes already analyzed once (re-sent image dedupe).
    archive = _archive(tmp_path / "dedupe")
    seen = archive.describe(attachment_ref=IMAGE_REF, analysis_kind="vision").content_hash
    decision = _Decision(always_look)
    output = await _adapter(
        tmp_path / "dedupe",
        decision=decision,
        evidence=_Evidence(seen_hashes=frozenset({seen})),
    ).propose(_request())
    assert _validated(output.raw_proposal).proposed_changes == ()
    assert decision.calls == 0


@pytest.mark.asyncio
async def test_model_failure_declines_instead_of_failing_the_turn(tmp_path: Path) -> None:
    class _Broken:
        async def complete(self, messages, *, temperature: float = 0.8) -> str:
            raise RuntimeError("provider down")

    adapter = QQPerceptionDecisionModel(
        model=_Broken(),
        input_source=_archive(tmp_path),
        dispatch_evidence=_Evidence(),
        budget_account_id="account:world-v2:perception",
        budget_limit=12,
        daily_limit=12,
    )
    output = await adapter.propose(_request())
    assert _validated(output.raw_proposal).proposed_changes == ()


@pytest.mark.asyncio
async def test_recover_is_inert_and_provider_free(tmp_path: Path) -> None:
    decision = _Decision('{"look": true}')
    adapter = _adapter(tmp_path, decision=decision)
    output = await adapter.recover(_request(), "main_timeout")
    assert output.raw_proposal == {}
    assert decision.calls == 0


@pytest.mark.asyncio
async def test_fenced_json_and_out_of_range_index_are_tolerated(tmp_path: Path) -> None:
    fenced = "```json\n{\"look\": true, \"attachment_index\": 9, \"reason\": \"想看\"}\n```"
    adapter = _adapter(tmp_path, decision=_Decision(fenced))
    output = await adapter.propose(_request())
    proposal = _validated(output.raw_proposal)
    assert len(proposal.proposed_changes) == 1
    payload = proposal.proposed_changes[0].payload.value()
    assert payload["attachment_ref"] == IMAGE_REF
