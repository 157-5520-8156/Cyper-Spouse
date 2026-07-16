from __future__ import annotations

import pytest
from pydantic import ValidationError

from companion_daemon.world_v2.activity_lifecycle_acceptance_manifest import (
    ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION,
    build_activity_lifecycle_acceptance_manifest,
)


def _manifest():
    return build_activity_lifecycle_acceptance_manifest(
        acceptance_id="acceptance:activity-lifecycle:1",
        acceptance_event_ref="event:acceptance-recorded:1",
        acceptance_event_payload_hash="a" * 64,
        proposal_id="proposal:activity-lifecycle:1",
        proposal_event_ref="event:activity-lifecycle-proposal:1",
        proposal_event_payload_hash="b" * 64,
        evaluated_world_revision=7,
        accepted_change_id="change:activity-lifecycle:1",
        accepted_change_hash="c" * 64,
        ecology_trigger_id="trigger:life-ecology:1",
        wake_event_ref="event:clock:1",
        wake_event_payload_hash="d" * 64,
        catalog_version="activity-opening.1",
        catalog_hash="e" * 64,
        opening_token="f" * 64,
        effect_event_id="event:activity-started:1",
        effect_event_type="ActivityStarted",
        effect_event_payload_hash="0" * 64,
        policy_digest="1" * 64,
    )


def test_manifest_binds_every_lifecycle_authority_coordinate_with_stable_hash() -> None:
    first = _manifest()
    assert first.manifest_version == ACTIVITY_LIFECYCLE_ACCEPTANCE_MANIFEST_VERSION
    assert _manifest() == first
    assert _manifest().manifest_hash == first.manifest_hash


@pytest.mark.parametrize(
    "field,value",
    [
        ("acceptance_event_ref", "event:forged-acceptance"),
        ("proposal_event_payload_hash", "2" * 64),
        ("wake_event_ref", "event:forged-wake"),
        ("catalog_hash", "3" * 64),
        ("opening_token", "4" * 64),
        ("effect_event_type", "ActivityCompleted"),
    ],
)
def test_manifest_rejects_tampered_authority_or_effect_binding(field: str, value: str) -> None:
    serialized = _manifest().model_dump(mode="json")
    serialized[field] = value
    with pytest.raises(ValidationError, match="manifest hash"):
        type(_manifest()).model_validate(serialized)


def test_manifest_requires_all_closed_authority_coordinates() -> None:
    material = _manifest().model_dump(mode="json")
    material.pop("ecology_trigger_id")
    with pytest.raises(ValidationError, match="ecology_trigger_id"):
        type(_manifest()).model_validate(material)


def test_manifest_rejects_an_effect_outside_the_activity_lifecycle_family() -> None:
    material = _manifest().model_dump(mode="json")
    material["effect_event_type"] = "ActivityPlanned"
    material["manifest_hash"] = "0" * 64
    with pytest.raises(ValidationError, match="effect_event_type"):
        type(_manifest()).model_validate(material)
