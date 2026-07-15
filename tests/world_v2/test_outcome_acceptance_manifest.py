from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.outcome_acceptance_manifest import (
    build_outcome_acceptance_manifest,
)


def _manifest():
    return build_outcome_acceptance_manifest(
        acceptance_id="acceptance:outcome:1",
        proposal_id="proposal:outcome:1",
        proposal_event_ref="event:outcome-proposal:1",
        proposal_event_payload_hash="a" * 64,
        evaluated_world_revision=7,
        accepted_change_id="change:outcome:1",
        accepted_change_hash="b" * 64,
        deliberation_trigger_id="trigger:outcome:1",
        settlement_event_id="event:outcome-settlement:1",
        settlement_payload_hash="c" * 64,
        npc_appraisal_trigger_id="appraisal:occurrence:result",
        npc_appraisal_trigger_event_id="event:npc-appraisal:1",
        npc_appraisal_trigger_payload_hash="d" * 64,
        policy_digest="e" * 64,
    )


def test_manifest_binds_all_outcome_effects_with_a_stable_hash() -> None:
    first = _manifest()
    assert _manifest() == first
    assert first.manifest_hash == _manifest().manifest_hash


def test_manifest_rejects_a_tampered_settlement_or_npc_trigger_binding() -> None:
    serialized = _manifest().model_dump(mode="json")
    serialized["settlement_event_id"] = "event:forged"
    with pytest.raises(ValidationError, match="manifest hash"):
        type(_manifest()).model_validate(serialized)
