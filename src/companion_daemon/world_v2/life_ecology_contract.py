"""Pure, shared identities for one durable Life Ecology run.

This module is the Life Ecology seam.  A runtime may verify and schedule a
wake, a ledger adapter may persist its ownership, and reducers may validate a
stored process, but none of those layers owns the vocabulary independently.
The contract deliberately depends only on the schema-core value type and the
standard library; it never imports a runtime, ledger adapter, or reducer.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from .schema_core import FrozenModel


LIFE_ECOLOGY_PROCESS_KIND = "life_ecology"
"""The installed ``TriggerProcess`` kind for a durable ecology run."""

LIFE_ECOLOGY_WAKE_EVENT_TYPES = frozenset(
    {
        "ClockAdvanced",
        "ActivityStarted",
        "ActivityResumed",
        "ActivityCompleted",
        "ActivityAbandoned",
        "WorldOccurrenceSettled",
        "ExperienceCommitted",
        "FactCommitted",
        "FactCorrected",
        "NpcRegistered",
    }
)
"""Committed world events that are eligible to wake Life Ecology."""

_CATALOG_VERSION = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_MAX_WORLD_ID_LENGTH = 512
_MAX_WAKE_EVENT_REF_LENGTH = 512
_TRIGGER_REF_PREFIX = "life-ecology:"

LifeEcologyClaimState = Literal["owned", "joined", "completed"]


class LifeEcologyRunKey(FrozenModel):
    """Stable idempotency domain for exactly one wake/catalog pairing."""

    world_id: str
    wake_event_ref: str
    catalog_version: str


class LifeEcologyRunClaim(FrozenModel):
    """The durable ownership fact returned by a trigger-store adapter."""

    trigger_id: str
    state: LifeEcologyClaimState


def validate_life_ecology_run_key(key: LifeEcologyRunKey) -> None:
    """Reject keys outside the durable, canonical trigger namespace."""

    if not isinstance(key.world_id, str) or not key.world_id or len(key.world_id) > _MAX_WORLD_ID_LENGTH:
        raise ValueError("life ecology world_id is outside the storage contract")
    if (
        not isinstance(key.wake_event_ref, str)
        or not key.wake_event_ref
        or len(key.wake_event_ref) > _MAX_WAKE_EVENT_REF_LENGTH
    ):
        raise ValueError("life ecology wake_event_ref is outside the storage contract")
    if not isinstance(key.catalog_version, str) or not _CATALOG_VERSION.fullmatch(
        key.catalog_version
    ):
        raise ValueError("life ecology catalog_version is invalid")


def life_ecology_trigger_id(*, world_id: str, wake_event_ref: str, catalog_version: str) -> str:
    """Return the deterministic ``TriggerProcess`` ID for one durable wake."""

    key = LifeEcologyRunKey(
        world_id=world_id,
        wake_event_ref=wake_event_ref,
        catalog_version=catalog_version,
    )
    validate_life_ecology_run_key(key)
    return "trigger:life-ecology:" + _digest(
        {
            "contract": "life-ecology-trigger.1",
            "world_id": key.world_id,
            "wake_event_ref": key.wake_event_ref,
            "catalog_version": key.catalog_version,
        }
    )


def life_ecology_trigger_ref(*, wake_event_ref: str, catalog_version: str) -> str:
    """Return the canonical reversible trigger reference.

    The world ID does not belong in the reference because the ledger already
    scopes every ``TriggerProcess`` to one world.  Validate with a harmless
    sentinel world ID so reference construction and parsing share exactly the
    same catalog and wake constraints.
    """

    key = LifeEcologyRunKey(
        world_id="world:life-ecology-contract",
        wake_event_ref=wake_event_ref,
        catalog_version=catalog_version,
    )
    validate_life_ecology_run_key(key)
    return f"{_TRIGGER_REF_PREFIX}{key.catalog_version}:{key.wake_event_ref}"


def parse_life_ecology_trigger_ref(trigger_ref: str) -> tuple[str, str] | None:
    """Parse only a canonical ecology ref as ``(catalog_version, wake_ref)``.

    A parse must round-trip through :func:`life_ecology_trigger_ref`; accepting
    merely split-able strings would let multiple textual forms name one run.
    """

    if not isinstance(trigger_ref, str) or not trigger_ref.startswith(_TRIGGER_REF_PREFIX):
        return None
    catalog_version, separator, wake_event_ref = trigger_ref.removeprefix(
        _TRIGGER_REF_PREFIX
    ).partition(":")
    if not separator:
        return None
    try:
        canonical = life_ecology_trigger_ref(
            wake_event_ref=wake_event_ref,
            catalog_version=catalog_version,
        )
    except ValueError:
        return None
    if trigger_ref != canonical:
        return None
    return catalog_version, wake_event_ref


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


__all__ = [
    "LIFE_ECOLOGY_PROCESS_KIND",
    "LIFE_ECOLOGY_WAKE_EVENT_TYPES",
    "LifeEcologyClaimState",
    "LifeEcologyRunClaim",
    "LifeEcologyRunKey",
    "life_ecology_trigger_id",
    "life_ecology_trigger_ref",
    "parse_life_ecology_trigger_ref",
    "validate_life_ecology_run_key",
]
