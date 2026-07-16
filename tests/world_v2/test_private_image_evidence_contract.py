from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.event_catalog import event_contract
from companion_daemon.world_v2.image_evidence_contract import CharacterMediaEvidenceV1
from companion_daemon.world_v2.private_image_evidence_contract import (
    RecipientScopedImageEvidenceDeclaredPayload,
    RecipientScopedImageEvidenceV1,
)
from companion_daemon.world_v2.character_media_fact_binder import CharacterMediaFactBinder


def _evidence(*, visibility: str = "private") -> RecipientScopedImageEvidenceV1:
    return RecipientScopedImageEvidenceV1.model_validate(
        {
            "visibility": visibility,
            "activity": {"id": "activity:wind-down", "kind": "wind_down"},
        }
    )


def test_recipient_scoped_evidence_is_a_separate_catalogued_p3_wire() -> None:
    payload = RecipientScopedImageEvidenceDeclaredPayload(
        source_event_ref="event:activity-completed:1",
        source_event_payload_hash="a" * 64,
        source_event_type="ActivityCompleted",
        source_privacy_ceiling="private",
        recipient_ref="user:recipient",
        image_evidence=_evidence(),
        declared_at=datetime(2026, 7, 16, tzinfo=UTC),
    )

    assert event_contract("RecipientScopedImageEvidenceDeclared").payload_model is type(payload)
    assert payload.image_evidence.planner_payload() == {
        "visibility": "private",
        "activity": {"id": "activity:wind-down", "kind": "wind_down"},
        "participants": [],
        "objects": [],
        "existing_media": [],
        "requires_readable_text": False,
    }


def test_recipient_scoped_evidence_cannot_relabel_source_privacy() -> None:
    with pytest.raises(ValueError, match="visibility must equal source privacy"):
        RecipientScopedImageEvidenceDeclaredPayload(
            source_event_ref="event:activity-completed:1",
            source_event_payload_hash="a" * 64,
            source_event_type="ActivityCompleted",
            source_privacy_ceiling="personal",
            recipient_ref="user:recipient",
            image_evidence=_evidence(visibility="private"),
            declared_at=datetime(2026, 7, 16, tzinfo=UTC),
        )


def test_personal_recipient_scoped_evidence_is_not_silently_promoted_to_p3_candidate() -> None:
    evidence = RecipientScopedImageEvidenceV1(
        visibility="personal",
        activity={"id": "activity:quiet", "kind": "quiet_time"},
        character_media=CharacterMediaEvidenceV1(
            character_ref="agent:companion",
            present=True,
            capture_capabilities=("character_front_camera",),
        ),
    )

    assert CharacterMediaFactBinder._contracts(evidence=evidence) == ()
