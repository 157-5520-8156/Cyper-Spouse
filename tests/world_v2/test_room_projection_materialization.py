from __future__ import annotations

from datetime import UTC, datetime

from companion_daemon.world_v2.attention_authority_schemas import (
    V2AttentionOrigin,
    V2AttentionProjection,
    V2AttentionValues,
    v2_attention_semantic_fingerprint,
)
from companion_daemon.world_v2.location_authority_schemas import (
    V2LocationOrigin,
    V2LocationProjection,
    V2LocationValues,
    v2_location_semantic_fingerprint,
)
from companion_daemon.world_v2.projection import (
    AuthenticatedProjectionPrincipal,
    ProjectionAuthority,
    ProjectionCompiler,
    ProjectionGrant,
)
from companion_daemon.world_v2.room_projection import RoomProjectionMaterializer
from companion_daemon.world_v2.schema_core import EvidenceRef
from companion_daemon.world_v2.schemas import (
    LedgerProjection,
    MediaPreview,
    PlanStateProjection,
    ProjectionRequest,
)


NOW = datetime(2026, 7, 16, 10, 30, tzinfo=UTC)
WORLD_ID = "world:room-projection"
ACTOR = "actor:companion"


def _location(*, privacy_class: str = "public") -> V2LocationProjection:
    values = V2LocationValues(
        location_ref="location:studio",
        zone_ref="zone:window",
        scene_visibility="public" if privacy_class == "public" else "private",
        privacy_class=privacy_class,  # type: ignore[arg-type]
        since=NOW,
    )
    policy_refs = ("policy:room-location",)
    return V2LocationProjection(
        actor_ref=ACTOR,
        entity_revision=1,
        semantic_fingerprint=v2_location_semantic_fingerprint(
            actor_ref=ACTOR, values=values, policy_refs=policy_refs
        ),
        values=values,
        origin=V2LocationOrigin(
            change_id="change:room-location",
            transition_id="transition:room-location",
            policy_refs=policy_refs,
            accepted_event_ref="event:room-location",
        ),
        updated_at=NOW,
    )


def _attention(*, privacy_class: str = "public") -> V2AttentionProjection:
    values = V2AttentionValues(
        mode="do_not_disturb",
        allocation_bp=8_000,
        interruptibility_bp=0,
        since=NOW,
        privacy_class=privacy_class,  # type: ignore[arg-type]
    )
    policy_refs = ("policy:room-attention",)
    return V2AttentionProjection(
        actor_ref=ACTOR,
        entity_revision=1,
        semantic_fingerprint=v2_attention_semantic_fingerprint(
            actor_ref=ACTOR, values=values, policy_refs=policy_refs
        ),
        values=values,
        origin=V2AttentionOrigin(
            change_id="change:room-attention",
            transition_id="transition:room-attention",
            policy_refs=policy_refs,
            accepted_event_ref="event:room-attention",
        ),
        updated_at=NOW,
    )


def _plan(
    *,
    plan_id: str = "plan:studio-work",
    status: str = "active",
    privacy_class: str = "shareable",
    importance_bp: int = 8_000,
) -> PlanStateProjection:
    return PlanStateProjection(
        plan_id=plan_id,
        activity_id=f"activity:{plan_id}",
        entity_revision=1,
        activity_kind="focused_work",
        evidence_refs=(
            EvidenceRef(
                ref_id=f"evidence:{plan_id}",
                evidence_type="active_plan",
                claim_purpose="future_plan",
            ),
        ),
        status=status,  # type: ignore[arg-type]
        importance_bp=importance_bp,
        location_ref="location:studio",
        privacy_class=privacy_class,  # type: ignore[arg-type]
        owner_actor_ref=ACTOR,
    )


def _projection(**changes: object) -> LedgerProjection:
    base = LedgerProjection(
        world_id=WORLD_ID,
        world_revision=9,
        deliberation_revision=3,
        ledger_sequence=12,
        logical_time=NOW,
        semantic_hash="a" * 64,
    )
    # Tests exercise the pure viewer materializer against a ledger snapshot.
    # Its input is read-only; authority lifecycle validation is covered by the
    # domain reducer suites rather than duplicated here.
    return base.model_copy(update=changes)


def test_room_materializes_only_public_location_activity_and_attention() -> None:
    view = RoomProjectionMaterializer.materialize(
        _projection(
            locations=(_location(),),
            attentions=(_attention(),),
            plans=(_plan(),),
        )
    )

    assert view.situation.location_ref == "location:studio"
    assert view.situation.activity == "focused_work"
    assert view.situation.activity_phase == "active"
    assert view.situation.attention == "do_not_disturb"
    assert view.situation.visible_status == "do_not_disturb"
    assert view.affect_display.display_state is None
    assert view.approved_media_refs == ()


def test_room_redacts_private_situation_and_preview_media() -> None:
    preview = MediaPreview(
        preview_id="preview:private",
        plan_id="plan:private",
        artifact_id="artifact:private",
        inspection_id="inspection:private",
        recipient_ref="user:private",
    )
    view = RoomProjectionMaterializer.materialize(
        _projection(
            locations=(_location(privacy_class="private"),),
            attentions=(_attention(privacy_class="private"),),
            plans=(_plan(privacy_class="private"),),
            media_previews=(preview,),
        )
    )

    assert view.situation.model_dump() == {
        "location_ref": None,
        "activity": None,
        "activity_phase": None,
        "attention": None,
        "visible_status": None,
    }
    serialized = view.model_dump_json()
    for private_value in ("user:private", "preview:private", "artifact:private", "location:studio"):
        assert private_value not in serialized
    assert view.approved_media_refs == ()


def test_room_fails_closed_for_ambiguous_location_and_uses_deterministic_activity_order() -> None:
    alternate = _location().model_copy(
        update={"entity_revision": 2, "updated_at": NOW.replace(minute=31)}
    )
    view = RoomProjectionMaterializer.materialize(
        _projection(
            locations=(_location(), alternate),
            plans=(
                _plan(plan_id="plan:planned", status="planned", importance_bp=10_000),
                _plan(plan_id="plan:active-low", status="active", importance_bp=1),
                _plan(plan_id="plan:active-high", status="active", importance_bp=9_000),
            ),
        )
    )

    assert view.situation.location_ref is None
    assert view.situation.activity == "focused_work"
    assert view.situation.activity_phase == "active"
    assert view.situation.visible_status == "active"


def test_projection_compiler_uses_room_materializer() -> None:
    authority = ProjectionAuthority(
        grants=(
            ProjectionGrant(
                world_id=WORLD_ID,
                viewer_id="room:trusted",
                viewer_kind="room_renderer",
                permissions=frozenset(),
                redaction_policy="room-public-v1",
            ),
        ),
        clock=lambda: NOW,
    )
    request = ProjectionRequest(
        schema_version="world-v2.1",
        request_id="request:room-public",
        world_id=WORLD_ID,
        viewer_id="room:trusted",
        viewer_kind="room_renderer",
        permissions=frozenset(),
        trace_id="trace:room-public",
        redaction_policy="room-public-v1",
    )
    signed = authority._bind_authenticated(
        request,
        AuthenticatedProjectionPrincipal(
            principal_id="room:trusted",
            world_id=WORLD_ID,
            authentication_context="test",
        ),
    )

    output = ProjectionCompiler(authority=authority).compile(
        _projection(locations=(_location(),), plans=(_plan(),)), signed
    )

    assert output.view.view_kind == "room"
    assert output.view.situation.location_ref == "location:studio"
    assert output.view.situation.activity_phase == "active"
