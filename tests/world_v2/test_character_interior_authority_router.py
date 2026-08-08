from __future__ import annotations

import pytest

from companion_daemon.world_v2.character_interior.authority import (
    _DeferredInteriorAuthority,
)
from companion_daemon.world_v2.character_interior import CharacterInterior
from companion_daemon.world_v2.character_interior.ports import _AuthorityRequest
from companion_daemon.world_v2.schemas import ProjectionCursor


def _request(*proposals: dict[str, object]) -> _AuthorityRequest:
    return _AuthorityRequest(
        inner_turn_id="character-inner-turn:test",
        world_id="world:test",
        actor_ref="character:zhizhi",
        purpose="test_transition",
        subject_ref="stimulus:test",
        trigger_ref="event:test",
        subject_source_refs=("event:test",),
        cursor=ProjectionCursor(
            world_revision=3,
            deliberation_revision=2,
            ledger_sequence=8,
        ),
        snapshot_id="inner-life:test",
        snapshot_hash="a" * 64,
        private_self_lineage_hash="sha256:" + "b" * 64,
        decision_hash="sha256:" + "c" * 64,
        proposals=proposals,
    )


class _Handler:
    proposal_type = "test_transition"

    def __init__(self) -> None:
        self.prepared: list[str] = []
        self.commits = 0

    async def prepare(self, request, proposal):
        del request
        value = proposal.get("value")
        if not isinstance(value, str) or not value:
            raise ValueError("invalid test transition")
        self.prepared.append(value)
        return value

    async def submit(self, request, prepared):
        del request
        self.commits += 1
        return tuple(f"accepted:{item}" for item in prepared)


class _Projection:
    async def project(self, **kwargs):  # pragma: no cover - health is read-only
        raise AssertionError(kwargs)


class _Role:
    name = "test-role"

    async def experience(self, request):  # pragma: no cover
        raise AssertionError(request)

    async def consider(self, request):  # pragma: no cover
        raise AssertionError(request)


@pytest.mark.asyncio
async def test_deferred_authority_fails_closed_until_exact_registry_is_bound() -> None:
    authority = _DeferredInteriorAuthority()

    with pytest.raises(RuntimeError, match="not bound"):
        await authority.submit(_request({"proposal_type": "test_transition", "value": "one"}))


@pytest.mark.asyncio
async def test_second_invalid_proposal_prevents_the_first_authority_commit() -> None:
    handler = _Handler()
    authority = _DeferredInteriorAuthority()
    authority.bind((handler,))

    with pytest.raises(ValueError, match="invalid test transition"):
        await authority.submit(
            _request(
                {"proposal_type": "test_transition", "value": "valid-first"},
                {"proposal_type": "test_transition", "value": None},
            )
        )

    assert handler.prepared == ["valid-first"]
    assert handler.commits == 0


@pytest.mark.asyncio
async def test_unknown_proposal_type_never_reaches_a_registered_handler() -> None:
    handler = _Handler()
    authority = _DeferredInteriorAuthority()
    authority.bind((handler,))

    with pytest.raises(ValueError, match="unregistered interior proposal type"):
        await authority.submit(_request({"proposal_type": "unknown", "value": "x"}))

    assert handler.prepared == []
    assert handler.commits == 0


def test_runtime_health_reports_deferred_authority_unbound_until_registry_bind() -> None:
    handler = _Handler()
    authority = _DeferredInteriorAuthority()
    interior = CharacterInterior(
        projection=_Projection(),
        role=_Role(),
        authority=authority,
    )

    before = interior.runtime_health()
    assert before["authority_bound"] is False
    assert "authority_unbound" in before["topology_issues"]

    authority.bind((handler,))
    after = interior.runtime_health()
    assert after["authority_bound"] is True
    assert "authority_unbound" not in after["topology_issues"]
