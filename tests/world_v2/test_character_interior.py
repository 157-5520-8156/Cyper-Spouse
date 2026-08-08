from __future__ import annotations

from datetime import UTC, datetime
import asyncio
import hashlib
import json

import pytest

from companion_daemon.world_v2.character_interior import (
    CharacterInterior,
    InnerDecision,
    InnerLifeSnapshot,
    InnerTransition,
    InteriorOpportunity,
    InteriorStimulus,
)
from companion_daemon.world_v2.schemas import ProjectionCursor
from companion_daemon.world_v2.character_interior.turn_store import (
    open_sqlite_character_interior_turn_store,
)


_NOW = datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
_CURSOR = ProjectionCursor(
    world_revision=17,
    deliberation_revision=9,
    ledger_sequence=42,
)
_FACETS = (
    "private_self",
    "selective_memory",
    "appraisal_affect",
    "emotional_continuity",
    "subjective_relationship",
    "aspirations_conflicts",
    "autonomous_impulses",
    "expression_stance",
)


def _author_lineage(request) -> dict[str, object]:  # type: ignore[no-untyped-def]
    identity = {
        "inner_turn_id": request.inner_turn_id,
        "phase": request.phase,
        "recall_completed": request.recall_completed,
        "correction_ordinal": request.correction_ordinal,
    }
    call_digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    parent_model_call_id = None
    if request.correction_ordinal == 1:
        parent = {**identity, "correction_ordinal": 0}
        parent_model_call_id = (
            "model-call:test:"
            + hashlib.sha256(json.dumps(parent, sort_keys=True).encode()).hexdigest()
        )
    return {
        "model_id": "character-role:test",
        "model_version": "character-role:test.1",
        "model_call_id": f"model-call:test:{call_digest}",
        "request_hash": "sha256:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
        "response_hash": "sha256:"
        + hashlib.sha256(("response:" + call_digest).encode()).hexdigest(),
        "attempt_ordinal": request.correction_ordinal,
        "parent_model_call_id": parent_model_call_id,
    }


def _facet(name: str) -> dict[str, object]:
    return {
        "availability": "available",
        "content": {"summary": f"{name} at the pinned head"},
        "source_refs": (f"source:{name}",),
    }


class _Projection:
    def __init__(self) -> None:
        self.calls = 0

    async def project(self, *, subject):
        self.calls += 1
        return {
            "world_id": subject.world_id,
            "actor_ref": subject.actor_ref,
            "cursor": subject.cursor,
            "logical_time": _NOW,
            "situation": {
                "availability": "available",
                "content": {"activity": "reading by the window"},
                "source_refs": ("source:situation",),
            },
            "continuity": {
                "availability": "available",
                "content": {"open_thread": "the promised photo"},
                "source_refs": ("source:continuity",),
            },
            "facets": {name: _facet(name) for name in _FACETS},
        }


class _UnavailableMemoryProjection(_Projection):
    async def project(self, *, subject):
        material = await super().project(subject=subject)
        material["facets"]["selective_memory"] = {
            "availability": "unavailable",
            "content": {},
            "source_refs": (),
        }
        return material


class _Recall:
    def __init__(self) -> None:
        self.requests = []

    async def recall(self, request):
        self.requests.append(request)
        return {
            "world_id": request.world_id,
            "actor_ref": request.actor_ref,
            "cursor": request.cursor,
            "content": {"recalled": ["the rainy walk"]},
            "source_refs": ("experience:rainy-walk",),
        }


class _AutomaticPrefetchRecall(_Recall):
    def __init__(self) -> None:
        super().__init__()
        self.prefetch_requests = []

    async def prefetch(self, request):
        self.prefetch_requests.append(request)
        return {
            "world_id": request.world_id,
            "actor_ref": request.actor_ref,
            "cursor": request.cursor,
            "content": {
                "items": [
                    {
                        "source_ref": "memory:afternoon-plan",
                        "memory_kind": "episodic",
                        "text": "They talked about the afternoon plan.",
                    }
                ]
            },
            "source_refs": ("memory:afternoon-plan",),
        }


class _DeferredAutomaticPrefetchRecall(_AutomaticPrefetchRecall):
    async def prefetch(self, request):
        raise AssertionError("a proactive first pass must not join automatic prefetch")

    async def recall(self, request):
        selected = await super().recall(request)
        selected["prefetch"] = {
            "world_id": request.world_id,
            "actor_ref": request.actor_ref,
            "cursor": request.cursor,
            "content": {
                "items": [
                    {
                        "source_ref": "memory:ready-after-choice",
                        "memory_kind": "episodic",
                        "text": "A scheduled candidate became ready.",
                    }
                ]
            },
            "source_refs": ("memory:ready-after-choice",),
        }
        return selected


class _Role:
    name = "character-role"
    purposes = ("external_perception_attention", "inbound_turn")

    def __init__(
        self,
        *,
        consider_status: str = "decision",
        raises: bool = False,
        recall_during_experience: bool = False,
    ) -> None:
        self.consider_status = consider_status
        self.raises = raises
        self.recall_during_experience = recall_during_experience
        self.experience_requests = []
        self.consider_requests = []

    async def experience(self, request):
        self.experience_requests.append(request)
        if self.recall_during_experience and not request.recall_completed:
            return {
                "status": "recall_request",
                "summary": "A related walk came to mind; she wants to retrieve it.",
                "attended_source_refs": ("source:appraisal_affect",),
                "recall_query": "the walk that felt like this rainy moment",
                "proposals": (),
                "author_lineage": _author_lineage(request),
            }
        return {
            "status": "transition",
            "summary": "She privately reinterpreted the moment.",
            "attended_source_refs": ("source:appraisal_affect",),
            "proposals": ({"proposal_type": "affect", "payload": {"valence": -120}},),
            "author_lineage": _author_lineage(request),
        }

    async def consider(self, request):
        self.consider_requests.append(request)
        if self.raises:
            raise TimeoutError("provider timed out")
        if self.consider_status == "silent":
            return {
                "status": "silent",
                "summary": "She decided this was not a moment to speak.",
                "attended_source_refs": ("source:private_self",),
                "proposals": (),
                "author_lineage": _author_lineage(request),
            }
        return {
            "status": "decision",
            "summary": "She wants to answer in her own voice.",
            "attended_source_refs": ("source:expression_stance",),
            "decision": {"expression_mode": "reply", "stance": "warm but distracted"},
            "proposals": (),
            "author_lineage": _author_lineage(request),
        }


class _InboundPrefetchRole(_Role):
    automatic_prefetch_join_seconds = 0.45


class _InboundPrefetchThenRecallRole(_InboundPrefetchRole):
    async def consider(self, request):
        self.consider_requests.append(request)
        if not request.recall_completed:
            return {
                "status": "recall_request",
                "summary": "She wants to remember one exact detail before deciding.",
                "attended_source_refs": ("memory:afternoon-plan",),
                "recall_query": "the exact afternoon plan",
                "proposals": (),
                "author_lineage": _author_lineage(request),
            }
        return {
            "status": "decision",
            "summary": "She remembered it and chose her response.",
            "attended_source_refs": ("experience:rainy-walk",),
            "decision": {"expression_mode": "reply"},
            "proposals": (),
            "author_lineage": _author_lineage(request),
        }


class _Authority:
    def __init__(self) -> None:
        self.submissions = []

    async def submit(self, request):
        self.submissions.append(request)
        return tuple(f"proposal:{index}" for index, _ in enumerate(request.proposals, start=1))


def _stimulus() -> InteriorStimulus:
    return InteriorStimulus(
        stimulus_ref="stimulus:incoming-message",
        inner_turn_ref="turn:qq:42",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="observation:42",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="inbound_turn",
        source_refs=("source:appraisal_affect",),
        context_note="A new, already recorded user message became available.",
    )


def _opportunity() -> InteriorOpportunity:
    return InteriorOpportunity(
        opportunity_ref="opportunity:reply:42",
        inner_turn_ref="turn:qq:42",
        world_id="world:test",
        actor_ref="character:zhizhi",
        trigger_ref="observation:42",
        cursor=_CURSOR,
        logical_time=_NOW,
        purpose="inbound_turn",
        source_refs=("source:expression_stance",),
        context_note="The character may decide how, whether, or when to express herself.",
    )


def test_public_module_exports_only_the_unified_character_interior_contract() -> None:
    import companion_daemon.world_v2.character_interior as module

    assert module.__all__ == [
        "CharacterInterior",
        "InteriorStimulus",
        "InteriorOpportunity",
        "InnerLifeSnapshot",
        "InnerTransition",
        "InnerDecision",
    ]


@pytest.mark.asyncio
async def test_project_builds_stable_actor_cursor_source_bound_eight_facet_snapshot() -> None:
    projection = _Projection()
    interior = CharacterInterior(projection=projection, role=_Role())

    first = await interior.project(_stimulus())
    second = await interior.project(_opportunity())

    assert isinstance(first, InnerLifeSnapshot)
    assert first == second
    assert projection.calls == 1
    assert first.actor_ref == "character:zhizhi"
    assert first.cursor == _CURSOR
    assert first.snapshot_id == f"inner-life-snapshot:sha256:{first.snapshot_hash}"
    assert set(first.facets) == set(_FACETS)
    assert first.source_refs == (
        "source:situation",
        "source:continuity",
        *(f"source:{name}" for name in _FACETS),
    )
    assert tuple(item.scope for item in first.source_inventory[:2]) == (
        "situation",
        "continuity",
    )
    assert InnerLifeSnapshot.model_validate(first.model_dump(mode="python")) == first
    with pytest.raises(Exception):
        first.facets["private_self"].content["summary"] = "mutated"


@pytest.mark.asyncio
async def test_experience_and_consider_keep_distinct_identity_and_one_recall_per_turn() -> None:
    projection = _Projection()
    recall = _Recall()
    role = _Role(recall_during_experience=True)
    authority = _Authority()
    interior = CharacterInterior(
        projection=projection,
        role=role,
        recall=recall,
        authority=authority,
    )

    transition = await interior.experience(_stimulus())
    decision = await interior.consider(_opportunity())

    assert isinstance(transition, InnerTransition)
    assert isinstance(decision, InnerDecision)
    assert transition.status == "transitioned"
    assert decision.status == "decided"
    assert transition.inner_turn_id != decision.inner_turn_id
    assert transition.snapshot_id != decision.snapshot_id
    assert len(recall.requests) == 1
    assert recall.requests[0].cursor == _CURSOR
    assert len(role.experience_requests) == 2
    assert role.experience_requests[0].recall_completed is False
    assert role.experience_requests[1].recall_completed is True
    assert role.experience_requests[1].snapshot.facets["selective_memory"].content["recalled"] == [
        "the rainy walk"
    ]
    assert "recalled" not in role.consider_requests[0].snapshot.facets["selective_memory"].content
    assert transition.proposal_refs == ("proposal:1",)
    assert len(authority.submissions) == 1
    assert interior.runtime_health()["recall"] == {
        "requests": 1,
        "hits": 1,
        "empty": 0,
        "adopted": 0,
        "reintegrated": 0,
        "source_rejections": 0,
    }


@pytest.mark.asyncio
async def test_inbound_first_pass_reads_scheduled_prefetch_through_selective_memory() -> None:
    recall = _AutomaticPrefetchRecall()
    role = _InboundPrefetchRole()
    interior = CharacterInterior(
        projection=_Projection(),
        role=role,
        recall=recall,
        authority=_Authority(),
    )

    result = await interior.consider(_opportunity())
    replay = await interior.consider(_opportunity())

    assert result.status == "decided"
    assert replay == result
    assert len(recall.prefetch_requests) == 1
    assert recall.prefetch_requests[0].join_seconds == 0.45
    assert recall.requests == []
    assert len(role.consider_requests) == 1
    first_snapshot = role.consider_requests[0].snapshot
    assert first_snapshot.facets["selective_memory"].content["material_keys"] == [
        "automatic_prefetch"
    ]
    assert first_snapshot.materials["automatic_prefetch"]["items"][0]["text"] == (
        "They talked about the afternoon plan."
    )
    # Prefetch augments this InnerTurn only; it must not mutate the canonical
    # cursor projection shared by unrelated opportunities.
    canonical = await interior.project(_opportunity())
    assert "automatic_prefetch" not in canonical.materials


@pytest.mark.asyncio
async def test_character_selected_recall_stays_in_same_turn_after_automatic_prefetch() -> None:
    recall = _AutomaticPrefetchRecall()
    role = _InboundPrefetchThenRecallRole()
    interior = CharacterInterior(
        projection=_Projection(),
        role=role,
        recall=recall,
    )

    result = await interior.consider(_opportunity())

    assert result.status == "decided"
    assert len(role.consider_requests) == 2
    assert role.consider_requests[0].inner_turn_id == role.consider_requests[1].inner_turn_id
    assert role.consider_requests[0].recall_completed is False
    assert role.consider_requests[1].recall_completed is True
    final_snapshot = role.consider_requests[1].snapshot
    assert final_snapshot.facets["selective_memory"].content["material_keys"] == [
        "automatic_prefetch",
        "selected_recall",
    ]
    assert final_snapshot.materials["automatic_prefetch"]["items"][0]["source_ref"] == (
        "memory:afternoon-plan"
    )
    assert final_snapshot.materials["selected_recall"]["content"]["recalled"] == ["the rainy walk"]


@pytest.mark.asyncio
async def test_proactive_first_pass_does_not_join_but_chosen_recall_absorbs_ready_prefetch() -> (
    None
):
    recall = _DeferredAutomaticPrefetchRecall()
    role = _Role(recall_during_experience=True)
    interior = CharacterInterior(
        projection=_Projection(),
        role=role,
        recall=recall,
        authority=_Authority(),
    )

    result = await interior.experience(_stimulus())

    assert result.status == "transitioned", result.failure_code
    assert recall.prefetch_requests == []
    assert len(role.experience_requests) == 2
    assert "automatic_prefetch" not in role.experience_requests[0].snapshot.materials
    final_snapshot = role.experience_requests[1].snapshot
    assert final_snapshot.materials["automatic_prefetch"]["items"][0]["source_ref"] == (
        "memory:ready-after-choice"
    )
    assert final_snapshot.materials["selected_recall"]["content"]["recalled"] == ["the rainy walk"]


@pytest.mark.asyncio
async def test_recall_hit_activates_an_initially_unavailable_memory_faculty() -> None:
    role = _Role(recall_during_experience=True)
    interior = CharacterInterior(
        projection=_UnavailableMemoryProjection(),
        role=role,
        recall=_Recall(),
        authority=_Authority(),
    )

    result = await interior.experience(_stimulus())

    assert result.status == "transitioned"
    final_snapshot = role.experience_requests[1].snapshot
    selective_memory = final_snapshot.facets["selective_memory"]
    assert selective_memory.availability == "available"
    assert selective_memory.content["material_keys"] == ["selected_recall"]
    assert final_snapshot.materials["selected_recall"] == {
        "content": {"recalled": ["the rainy walk"]},
        "source_refs": ["experience:rainy-walk"],
    }
    model_view = final_snapshot.model_view()
    assert model_view["faculties"]["selective_memory"] == {
        "availability": "available",
        "material_keys": ["selected_recall"],
    }
    assert interior.runtime_health()["faculty_state"]["selective_memory"] == {
        "availability": "available",
        "item_count": 1,
        "source_count": 1,
        "source_closed_count": 1,
        "truncation_reason": "truncation_not_requested",
    }


@pytest.mark.asyncio
async def test_ordinary_role_path_performs_zero_recall() -> None:
    recall = _Recall()
    interior = CharacterInterior(
        projection=_Projection(),
        role=_Role(),
        recall=recall,
    )

    result = await interior.consider(_opportunity())

    assert result.status == "decided"
    assert recall.requests == []


@pytest.mark.asyncio
async def test_repeated_recall_request_is_a_technical_failure_and_never_searches_twice() -> None:
    class _AlwaysRecallRole(_Role):
        async def consider(self, request):
            self.consider_requests.append(request)
            return {
                "status": "recall_request",
                "summary": "Still wants another memory search.",
                "attended_source_refs": ("source:private_self",),
                "recall_query": "another related memory",
                "proposals": (),
            }

    recall = _Recall()
    role = _AlwaysRecallRole()
    interior = CharacterInterior(
        projection=_Projection(),
        role=role,
        recall=recall,
    )

    result = await interior.consider(_opportunity())

    assert result.status == "technical_failure"
    assert result.failure_code == "repeated_recall_request"
    assert len(recall.requests) == 1
    assert len(role.consider_requests) == 2


@pytest.mark.asyncio
async def test_concurrent_same_opportunity_is_effect_once() -> None:
    role = _Role()
    projection = _Projection()
    interior = CharacterInterior(projection=projection, role=role)
    opportunity = _opportunity()

    first, second = await asyncio.gather(
        interior.consider(opportunity),
        interior.consider(opportunity),
    )

    assert first == second
    assert len(role.consider_requests) == 1
    assert projection.calls == 1
    health = interior.runtime_health()
    assert health["effect_once_join_count"] == 1
    assert health["parallel_character_author_conflicts"] == 0
    assert health["dual_write_conflicts"] == 0
    assert health["active_route"] == {
        "character_author": "character-role",
        "projection": "_Projection",
        "recall": None,
        "authority": None,
    }


    assert health["faculty_state"] == {
        name: {
            "availability": "available",
            "item_count": 1,
            "source_count": 1,
            "source_closed_count": 1,
            "truncation_reason": "truncation_not_requested",
        }
        for name in _FACETS
    }
    assert health["stale_cursor_rebuild_count"] == 0
    assert health["recall"] == {
        "requests": 0,
        "hits": 0,
        "empty": 0,
        "adopted": 0,
        "reintegrated": 0,
        "source_rejections": 0,
    }
    semantic_author_ids = health["topology_evidence"]["semantic_author_ids"]
    assert len(semantic_author_ids) == 1
    semantic_author_id = semantic_author_ids[0]
    assert semantic_author_id.startswith("unverified-character-semantic-author:sha256:")
    assert health["topology_evidence"] == {
        "public_role_entrypoints": ["experience", "consider"],
        "snapshot_entrypoint": "project",
        "purpose_owner_count": 2,
        "purpose_owner_counts": {
            "external_perception_attention": 1,
            "inbound_turn": 1,
        },
        "duplicate_purpose_owner_count": 0,
        "legacy_compatibility_route_installed": False,
        "legacy_compatibility_route_names": [],
        "semantic_author_ids": [semantic_author_id],
        "purpose_semantic_author_ids": {
            "external_perception_attention": semantic_author_id,
            "inbound_turn": semantic_author_id,
        },
        "unverified_author_faculty_names": ["character-role"],
        "semantic_author_scope": "character_purpose_faculties_only",
        "evidence_contract": "frozen-faculty-registry.1",
    }


@pytest.mark.asyncio
async def test_concurrent_callers_join_one_process_local_inner_turn() -> None:
    """Concurrent callers in one instance join one effect-once InnerTurn."""
    cursor = ProjectionCursor(
        world_revision=0,
        deliberation_revision=0,
        ledger_sequence=0,
    )
    opportunity = _opportunity().model_copy(update={"cursor": cursor})
    role = _Role()
    interior = CharacterInterior(projection=_Projection(), role=role)

    left, right = await asyncio.gather(
        interior.consider(opportunity),
        interior.consider(opportunity),
    )

    assert left == right
    assert len(role.consider_requests) == 1
    assert interior.runtime_health()["effect_once_join_count"] == 1


@pytest.mark.asyncio
async def test_two_character_interior_instances_join_terminal_sidecar_turn(tmp_path) -> None:
    path = tmp_path / "character-interior.sqlite"
    first_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:test")
    second_store = open_sqlite_character_interior_turn_store(path=path, world_id="world:test")
    first_role = _Role()
    second_role = _Role()
    first = CharacterInterior(
        projection=_Projection(),
        role=first_role,
        turn_store=first_store,
    )
    second = CharacterInterior(
        projection=_Projection(),
        role=second_role,
        turn_store=second_store,
    )

    first_result = await first.consider(_opportunity())
    second_result = await second.consider(_opportunity())

    assert second_result == first_result
    assert len(first_role.consider_requests) == 1
    assert second_role.consider_requests == []
    assert second.runtime_health()["durable_turn_store"]["terminal_turn_count"] == 1
    first_store.close()
    second_store.close()


def test_runtime_health_derives_topology_from_the_frozen_registry() -> None:
    class _PurposeFaculty:
        name = "bounded-purpose-faculty"
        purposes = ("media_selection", "proactive_contact")
        legacy_compatibility_route = False

    interior = CharacterInterior(
        projection=_Projection(),
        role=_Role(),
        faculties=(_PurposeFaculty(),),
    )

    evidence = interior.runtime_health()["topology_evidence"]

    assert evidence["purpose_owner_count"] == 4
    assert evidence["purpose_owner_counts"] == {
        "external_perception_attention": 1,
        "inbound_turn": 1,
        "media_selection": 1,
        "proactive_contact": 1,
    }
    assert evidence["duplicate_purpose_owner_count"] == 0
    assert evidence["legacy_compatibility_route_installed"] is False
    assert evidence["legacy_compatibility_route_names"] == []


def test_runtime_health_derives_distinct_semantic_authors_from_purpose_faculties() -> None:
    class _PrimaryAuthor(_Role):
        @property
        def author_identity(self) -> dict[str, object]:
            return {
                "contract": "character-semantic-author-identity.1",
                "semantic_author_id": "character-semantic-author:primary",
                "model_id": "character-primary",
                "model_version": "character-primary.1",
            }

    class _ForeignPurposeFaculty:
        name = "foreign-character-purpose"
        purposes = ("media_selection",)

        @property
        def author_identity(self) -> dict[str, object]:
            return {
                "contract": "character-semantic-author-identity.1",
                "semantic_author_id": "character-semantic-author:foreign",
                "model_id": "character-foreign",
                "model_version": "character-foreign.1",
            }

    interior = CharacterInterior(
        projection=_Projection(),
        role=_PrimaryAuthor(),
        faculties=(_ForeignPurposeFaculty(),),
    )

    health = interior.runtime_health()

    assert health["semantic_author_count"] == 2
    assert health["status"] == "not_ready"
    assert "multiple_semantic_authors" in health["topology_issues"]
    assert health["topology_evidence"]["semantic_author_ids"] == [
        "character-semantic-author:foreign",
        "character-semantic-author:primary",
    ]
    assert health["topology_evidence"]["purpose_semantic_author_ids"] == {
        "external_perception_attention": "character-semantic-author:primary",
        "inbound_turn": "character-semantic-author:primary",
        "media_selection": "character-semantic-author:foreign",
    }


def test_runtime_health_refuses_a_declared_legacy_compatibility_route() -> None:
    class _LegacyFaculty:
        name = "legacy-route"
        purposes = ("legacy_purpose",)
        legacy_compatibility_route = True

    interior = CharacterInterior(
        projection=_Projection(),
        role=_Role(),
        faculties=(_LegacyFaculty(),),
    )

    health = interior.runtime_health()

    assert health["status"] == "not_ready"
    assert "legacy_compatibility_route_installed" in health["topology_issues"]
    assert health["topology_evidence"]["legacy_compatibility_route_names"] == ["legacy-route"]


@pytest.mark.asyncio
async def test_unregistered_purpose_fails_before_projection_or_character_call() -> None:
    role = _Role()
    projection = _Projection()
    interior = CharacterInterior(projection=projection, role=role)
    opportunity = _opportunity().model_copy(update={"purpose": "typo_inbound_turn"})

    with pytest.raises(ValueError, match="unregistered character interior purpose"):
        await interior.consider(opportunity)

    assert projection.calls == 0
    assert role.consider_requests == []


@pytest.mark.asyncio
async def test_distinct_opportunity_identity_cannot_reuse_an_old_terminal_decision() -> None:
    role = _Role()
    interior = CharacterInterior(projection=_Projection(), role=role)
    first = _opportunity()
    second = first.model_copy(
        update={
            "opportunity_ref": "opportunity:reply:43",
            "context_note": "A distinct accepted opportunity at the same cursor.",
        }
    )

    first_result = await interior.consider(first)
    second_result = await interior.consider(second)

    assert first_result.inner_turn_id != second_result.inner_turn_id
    assert first_result.opportunity_ref == first.opportunity_ref
    assert second_result.opportunity_ref == second.opportunity_ref
    assert len(role.consider_requests) == 2


@pytest.mark.asyncio
async def test_distinct_source_closure_cannot_collide_on_one_inner_turn() -> None:
    role = _Role()
    interior = CharacterInterior(projection=_Projection(), role=role)
    first = _opportunity()
    second = first.model_copy(
        update={
            "source_refs": ("source:private_self",),
            "context_note": "The source closure changed while the cursor stayed pinned.",
        }
    )

    first_result = await interior.consider(first)
    second_result = await interior.consider(second)

    assert first_result.inner_turn_id != second_result.inner_turn_id
    assert len(role.consider_requests) == 2


@pytest.mark.asyncio
async def test_production_faculty_cannot_return_success_without_author_lineage() -> None:
    class _LineageRequiredRole(_Role):
        requires_author_lineage = True

        async def consider(self, request):  # type: ignore[no-untyped-def]
            result = await super().consider(request)
            result.pop("author_lineage", None)
            return result

    role = _LineageRequiredRole()
    interior = CharacterInterior(projection=_Projection(), role=role)

    result = await interior.consider(_opportunity())

    assert result.status == "technical_failure"
    assert result.failure_code == "invalid_role_result_after_correction"
    assert len(role.consider_requests) == 2


@pytest.mark.asyncio
async def test_recall_from_another_cursor_fails_closed_before_role_model() -> None:
    class _StaleRecall(_Recall):
        async def recall(self, request):
            result = await super().recall(request)
            result["cursor"] = request.cursor.model_copy(
                update={"ledger_sequence": request.cursor.ledger_sequence - 1}
            )
            return result

    role = _Role(recall_during_experience=True)
    interior = CharacterInterior(
        projection=_Projection(),
        role=role,
        recall=_StaleRecall(),
    )

    result = await interior.experience(_stimulus())

    assert result.status == "technical_failure"
    assert result.failure_code == "recall_cursor_mismatch"
    assert len(role.experience_requests) == 1


@pytest.mark.asyncio
async def test_valid_model_silence_is_not_confused_with_technical_failure() -> None:
    silent = CharacterInterior(
        projection=_Projection(),
        role=_Role(consider_status="silent"),
    )
    failed = CharacterInterior(
        projection=_Projection(),
        role=_Role(raises=True),
    )

    silent_result = await silent.consider(_opportunity())
    failed_result = await failed.consider(_opportunity())

    assert silent_result.status == "model_silent"
    assert silent_result.failure_code is None
    assert silent_result.summary == "She decided this was not a moment to speak."
    assert failed_result.status == "technical_failure"
    assert failed_result.failure_code == "role_faculty_unavailable"
    assert failed_result.summary is None
    assert failed_result.decision is None


@pytest.mark.asyncio
async def test_technical_failure_is_not_cached_as_an_effect_once_outcome() -> None:
    class _RetryingRole(_Role):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def consider(self, request):
            self.calls += 1
            if self.calls == 1:
                self.consider_requests.append(request)
                raise TimeoutError("provider timed out once")
            return await super().consider(request)

    role = _RetryingRole()
    interior = CharacterInterior(projection=_Projection(), role=role)
    opportunity = _opportunity()

    failed = await interior.consider(opportunity)
    recovered = await interior.consider(opportunity)

    assert failed.status == "technical_failure"
    assert recovered.status == "decided"
    assert role.calls == 2


@pytest.mark.asyncio
async def test_unpinned_attention_is_invalid_model_output_not_character_silence() -> None:
    class _InvalidRole(_Role):
        def __init__(self) -> None:
            super().__init__()
            self.requests = []

        async def consider(self, request):
            self.requests.append(request)
            return {
                "status": "silent",
                "summary": "invalid source",
                "attended_source_refs": ("unbound:model-invented-source",),
                "proposals": (),
            }

    role = _InvalidRole()
    interior = CharacterInterior(projection=_Projection(), role=role)

    result = await interior.consider(_opportunity())

    assert result.status == "technical_failure"
    assert result.failure_code == "invalid_role_result_after_correction"
    assert len(role.requests) == 2
    assert role.requests[0].correction_ordinal == 0
    assert role.requests[0].correction_failure_code is None
    assert role.requests[1].correction_ordinal == 1
    assert role.requests[1].correction_failure_code == "invalid_role_result"


@pytest.mark.asyncio
async def test_same_role_gets_one_bounded_correction_for_invalid_output() -> None:
    class _CorrectingRole(_Role):
        def __init__(self) -> None:
            super().__init__()
            self.requests = []

        async def consider(self, request):
            self.requests.append(request)
            if request.correction_ordinal == 0:
                return {
                    "status": "decision",
                    "summary": "first result forgot its typed decision",
                    "attended_source_refs": ("source:expression_stance",),
                    "proposals": (),
                }
            return {
                "status": "decision",
                "summary": "She made a valid choice after seeing the exact failure.",
                "attended_source_refs": ("source:expression_stance",),
                "decision": {"expression_mode": "reply", "stance": "direct"},
                "proposals": (),
                "author_lineage": _author_lineage(request),
            }

    role = _CorrectingRole()
    interior = CharacterInterior(projection=_Projection(), role=role)

    result = await interior.consider(_opportunity())

    assert result.status == "decided"
    assert result.decision == {"expression_mode": "reply", "stance": "direct"}
    assert len(role.requests) == 2
    assert role.requests[1].correction_ordinal == 1
    assert role.requests[1].correction_failure_code == "invalid_role_result"


@pytest.mark.asyncio
async def test_runtime_health_separates_character_silence_correction_and_technical_failure() -> (
    None
):
    class _MixedRole(_Role):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def consider(self, request):
            self.calls += 1
            if request.trigger_ref == "observation:silent":
                return {
                    "status": "silent",
                    "summary": "She chose not to answer this opportunity.",
                    "attended_source_refs": ("source:private_self",),
                    "proposals": (),
                    "author_lineage": _author_lineage(request),
                }
            raise TimeoutError("provider unavailable")

    interior = CharacterInterior(projection=_Projection(), role=_MixedRole())
    silent = _opportunity().model_copy(
        update={
            "inner_turn_ref": "turn:silent",
            "opportunity_ref": "opportunity:silent",
            "trigger_ref": "observation:silent",
            "source_refs": ("source:private_self",),
        }
    )
    failed = _opportunity().model_copy(
        update={
            "inner_turn_ref": "turn:failed",
            "opportunity_ref": "opportunity:failed",
            "trigger_ref": "observation:failed",
        }
    )

    assert (await interior.consider(silent)).status == "model_silent"
    assert (await interior.consider(failed)).status == "technical_failure"

    health = interior.runtime_health()
    assert health["status"] == "degraded"
    assert health["consideration_counts"] == {
        "decided": 0,
        "model_silent": 1,
        "technical_failure": 1,
    }
    assert health["correction_attempt_count"] == 0
    assert health["last_terminal_status"] == "technical_failure"
    assert health["last_failure_code"] == "role_faculty_unavailable"
    assert health["primary_author_faculty"] == "character-role"
    assert health["semantic_author_count"] == 1
    assert health["primary_author_model"] == "unknown"
    assert health["primary_author_route"]["name"] == "character-role"
    assert health["projection_bound"] is True
    assert health["recall_bound"] is False
    assert health["authority_bound"] is False
    assert health["legacy_interface_invocations"] == 0
    assert "summary" not in health
    assert "decision" not in health


def _capability_manifest(payload: dict[str, object]) -> dict[str, object]:
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "contract": "character-interior-capability-manifest.1",
        "capability_ref": "capability:external-attention:42",
        "capability_kind": "external_perception_attention",
        "payload_json": payload_json,
        "payload_hash": "sha256:" + hashlib.sha256(payload_json.encode()).hexdigest(),
        "source_refs": ("external-signal:42", "external-channel:weibo"),
    }


@pytest.mark.asyncio
async def test_opportunity_capability_is_identity_bound_and_visible_to_the_same_role() -> None:
    role = _Role()
    interior = CharacterInterior(projection=_Projection(), role=role)
    opportunity = InteriorOpportunity.model_validate(
        {
            **_opportunity().model_dump(mode="python"),
            "purpose": "external_perception_attention",
            "capability_manifest": _capability_manifest(
                {
                    "candidate_set_hash": "sha256:" + "a" * 64,
                    "channels": ["weibo"],
                }
            ),
        }
    )

    result = await interior.consider(opportunity)

    assert result.status == "decided"
    request = role.consider_requests[0]
    assert request.purpose == "external_perception_attention"
    assert request.capability_manifest is not None
    assert request.capability_manifest.payload == {
        "candidate_set_hash": "sha256:" + "a" * 64,
        "channels": ["weibo"],
    }
    snapshot = request.snapshot
    assert snapshot.capability_scope.availability == "available"
    assert snapshot.capability_scope.value["capability_ref"] == ("capability:external-attention:42")
    assert snapshot.capability_scope.value["payload_hash"] == (
        opportunity.capability_manifest.payload_hash
    )


def test_capability_manifest_rejects_tampered_sidecar_payload() -> None:
    manifest = _capability_manifest({"candidates": ["signal:1"]})
    manifest["payload_json"] = '{"candidates":["signal:2"]}'

    with pytest.raises(ValueError, match="capability manifest payload hash"):
        InteriorOpportunity.model_validate(
            {
                **_opportunity().model_dump(mode="python"),
                "purpose": "external_perception_attention",
                "capability_manifest": manifest,
            }
        )


def test_faculty_registry_rejects_duplicate_semantic_role_names() -> None:
    with pytest.raises(ValueError, match="duplicate character interior faculty"):
        CharacterInterior(
            projection=_Projection(),
            role=_Role(),
            faculties=(_Role(),),
        )
