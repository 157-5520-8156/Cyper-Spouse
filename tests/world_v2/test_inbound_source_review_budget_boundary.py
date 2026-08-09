from __future__ import annotations

import pytest

from companion_daemon.world_v2.character_interior.inbound_turn import (
    CharacterInteriorInboundDeliberationAdapter,
    InboundTurnFaculty,
)


class _ReviewAwareAuthor:
    def propose(self, _request: object) -> object:
        raise AssertionError("the transport capability test must not author a turn")

    def source_closure_review_enabled(self) -> bool:
        return True


class _PurposeRegistry:
    def __init__(self, faculty: object) -> None:
        self._faculty = faculty

    def for_purpose(self, purpose: str) -> object:
        assert purpose == "inbound_turn"
        return self._faculty


class _InteriorWithInboundFaculty:
    def __init__(self, faculty: object) -> None:
        self._registry = _PurposeRegistry(faculty)


def test_inbound_faculty_exposes_its_installed_source_review_boundary() -> None:
    faculty = InboundTurnFaculty(author=_ReviewAwareAuthor())

    assert faculty.source_closure_review_enabled() is True


def test_inbound_deliberation_surfaces_internal_source_review_to_budget_owner() -> None:
    faculty = InboundTurnFaculty(author=_ReviewAwareAuthor())
    adapter = CharacterInteriorInboundDeliberationAdapter(
        interior=_InteriorWithInboundFaculty(faculty),  # type: ignore[arg-type]
        world_id="world:test",
        actor_ref="agent:companion",
    )

    assert adapter.source_closure_review_enabled() is True
