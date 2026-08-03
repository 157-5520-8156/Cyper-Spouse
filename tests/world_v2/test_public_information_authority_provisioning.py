from __future__ import annotations

from datetime import UTC, datetime

import pytest

from companion_daemon.world_v2.external_world_perception.production_attention import (
    public_information_capability_id,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.media_authority_provisioning import (
    MediaAuthorityProvisioner,
)
from companion_daemon.world_v2.public_information_authority_provisioning import (
    PublicInformationAuthorityProvisioner,
)
from companion_daemon.world_v2.schemas import WorldEvent


NOW = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)
TEST_ROOT_SEED = "11" * 32
REGISTRY_HASH = "sha256:" + "b" * 64


def _clocked_world() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id="world:public-information-authority")
    ledger.commit(
        (
            WorldEvent.from_payload(
                schema_version="world-v2.1",
                event_id="event:world-started:public-information-authority",
                event_type="WorldStarted",
                world_id=ledger.world_id,
                logical_time=NOW,
                created_at=NOW,
                actor="system:test",
                source="test",
                trace_id="trace:public-information-authority",
                causation_id="cause:public-information-authority",
                correlation_id="correlation:public-information-authority",
                idempotency_key="identity:public-information-authority",
                payload={},
            ),
        ),
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )
    return ledger


def test_public_information_authority_is_root_signed_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = _clocked_world()
    provisioner = PublicInformationAuthorityProvisioner(
        ledger=ledger,
        signing_key_hex=TEST_ROOT_SEED,
        companion_actor_ref="character:zhizhi",
        registry_content_hash=REGISTRY_HASH,
    )

    first = provisioner.ensure()
    rerun = provisioner.ensure()
    projection = ledger.project()

    assert len(first.committed_event_ids) == 2
    assert rerun.committed_event_ids == ()
    capability_id = public_information_capability_id(REGISTRY_HASH)
    assert capability_id in rerun.already_present
    grant = next(item for item in projection.capability_grants if item.grant_id == capability_id)
    assert grant.values.capability_kind == "public_information_read"
    assert grant.values.target_scope_refs == ("channel:public_information",)
    assert grant.values.constraint_refs == ("constraint:read-only",)
    assert grant.values.actor_ref == "character:zhizhi"
    assert grant.origin.enforcement_eligible is True


def test_public_information_reuses_existing_operator_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    ledger = _clocked_world()
    MediaAuthorityProvisioner(
        ledger=ledger,
        signing_key_hex=TEST_ROOT_SEED,
        subject_ref="user:test",
    ).ensure()
    authority_count = len(ledger.project().actor_authorities)

    result = PublicInformationAuthorityProvisioner(
        ledger=ledger,
        signing_key_hex=TEST_ROOT_SEED,
        companion_actor_ref="character:zhizhi",
        registry_content_hash=REGISTRY_HASH,
    ).ensure()

    assert len(result.committed_event_ids) == 1
    assert len(ledger.project().actor_authorities) == authority_count
