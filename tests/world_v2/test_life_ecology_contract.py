from __future__ import annotations

import pytest

from companion_daemon.world_v2 import life_ecology_contract as contract
from companion_daemon.world_v2 import life_ecology_runtime as runtime
from companion_daemon.world_v2 import life_ecology_trigger_store as store


def test_life_ecology_contract_is_the_single_shared_runtime_store_surface() -> None:
    """Compatibility re-exports cannot become a second implementation."""

    assert runtime.LifeEcologyRunKey is contract.LifeEcologyRunKey
    assert runtime.LifeEcologyRunClaim is contract.LifeEcologyRunClaim
    assert runtime.LifeEcologyClaimState is contract.LifeEcologyClaimState
    assert store.LifeEcologyRunKey is contract.LifeEcologyRunKey
    assert store.LifeEcologyRunClaim is contract.LifeEcologyRunClaim
    assert store.LIFE_ECOLOGY_PROCESS_KIND == contract.LIFE_ECOLOGY_PROCESS_KIND
    assert store.LIFE_ECOLOGY_WAKE_EVENT_TYPES is contract.LIFE_ECOLOGY_WAKE_EVENT_TYPES
    assert store.life_ecology_trigger_id is contract.life_ecology_trigger_id
    assert store.life_ecology_trigger_ref is contract.life_ecology_trigger_ref
    assert store.parse_life_ecology_trigger_ref is contract.parse_life_ecology_trigger_ref


def test_life_ecology_contract_round_trips_only_canonical_catalog_and_wake_refs() -> None:
    wake_ref = "event:activity:complete:tea:1"
    trigger_ref = contract.life_ecology_trigger_ref(
        wake_event_ref=wake_ref,
        catalog_version="life-ecology.1",
    )

    assert trigger_ref == "life-ecology:life-ecology.1:event:activity:complete:tea:1"
    assert contract.parse_life_ecology_trigger_ref(trigger_ref) == ("life-ecology.1", wake_ref)
    assert contract.life_ecology_trigger_id(
        world_id="world:contract",
        wake_event_ref=wake_ref,
        catalog_version="life-ecology.1",
    ) == "trigger:life-ecology:2e6542b4c7f5e57cfb179e2f847a925c844983ef3737e1233b5396c75364c103"

    assert contract.parse_life_ecology_trigger_ref("life-ecology:Life.1:event:wake") is None
    assert contract.parse_life_ecology_trigger_ref("life-ecology:life.1:") is None
    assert contract.parse_life_ecology_trigger_ref("life-ecology:life.1") is None


@pytest.mark.parametrize(
    ("world_id", "wake_event_ref", "catalog_version"),
    [
        ("", "event:wake", "life.1"),
        ("world:contract", "", "life.1"),
        ("world:contract", "event:wake", "Life.1"),
    ],
)
def test_life_ecology_contract_rejects_noncanonical_run_keys(
    world_id: str, wake_event_ref: str, catalog_version: str
) -> None:
    with pytest.raises(ValueError):
        contract.life_ecology_trigger_id(
            world_id=world_id,
            wake_event_ref=wake_event_ref,
            catalog_version=catalog_version,
        )
