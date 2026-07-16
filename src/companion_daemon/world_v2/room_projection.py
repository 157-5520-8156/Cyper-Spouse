"""Public, read-only materialization for a World v2 room renderer.

The room is deliberately a *viewer* of the ledger, not another world-state
owner.  This module is the single privacy boundary between the rich authority
projection and the small ``RoomProjectionView`` contract.  In particular it
does not expose affect episodes, participants, focus references, plans that
are not explicitly publishable, or media previews (a preview is never a
delivery approval).
"""

from __future__ import annotations

from .schemas import (
    DashboardPublicProjectionView,
    LedgerProjection,
    PublicAgendaProjection,
    PublicSituationProjection,
    RoomProjectionView,
)


_ROOM_VISIBLE_PRIVACY = frozenset({"public", "shareable"})
_ATTENTION_VISIBLE_STATUS = {
    "available": "available",
    "glancing": "available",
    "occupied": "busy",
    "deep_focus": "busy",
    "do_not_disturb": "do_not_disturb",
    "recovering_attention": "recovering",
}
_ACTIVITY_PHASE_ORDER = {"active": 0, "paused": 1, "planned": 2}


class RoomProjectionMaterializer:
    """Compile the public room view from one immutable ledger projection.

    The materializer is intentionally pure.  It has no ledger port, cache,
    executor, or side effect, which means a historical projection and a replay
    produce exactly the same room representation.  Missing or ambiguous
    authority is redacted rather than guessed.
    """

    @classmethod
    def materialize(cls, projection: LedgerProjection) -> RoomProjectionView:
        actor_ref = cls._companion_actor_ref(projection)
        if actor_ref is None:
            return RoomProjectionView()

        location_ref = cls._location_ref(projection, actor_ref=actor_ref)
        activity, activity_phase = cls._activity(projection, actor_ref=actor_ref)
        attention = cls._attention(projection, actor_ref=actor_ref)
        visible_status = cls._visible_status(
            activity_phase=activity_phase,
            attention=attention,
        )

        # ``PublicAffectProjection`` remains deliberately empty.  Felt affect
        # is private authority, and there is not yet a separately accepted
        # public display-strategy projection from which it could be derived.
        # Likewise, media previews are not delivery approval and must not be
        # surfaced as ``approved_media_refs``.
        return RoomProjectionView(
            situation=PublicSituationProjection(
                location_ref=location_ref,
                activity=activity,
                activity_phase=activity_phase,
                attention=attention,
                visible_status=visible_status,
            )
        )

    @staticmethod
    def _companion_actor_ref(projection: LedgerProjection) -> str | None:
        if projection.character_core is not None:
            return projection.character_core.actor_ref

        # The bootstrap identity is an explicit convention, not inference from
        # arbitrary actors.  In its absence we fail closed instead of rendering
        # an NPC or user state as the companion's state.
        companion = "actor:companion"
        known_actors = {
            *(
                item.actor_ref
                for item in projection.locations
            ),
            *(item.actor_ref for item in projection.attentions),
            *(item.owner_actor_ref for item in projection.plans if item.owner_actor_ref),
        }
        return companion if companion in known_actors else None

    @staticmethod
    def _location_ref(projection: LedgerProjection, *, actor_ref: str) -> str | None:
        candidates = tuple(
            item
            for item in projection.locations
            if item.actor_ref == actor_ref
            and item.values.privacy_class in _ROOM_VISIBLE_PRIVACY
            and item.values.scene_visibility in _ROOM_VISIBLE_PRIVACY
        )
        # A correct authority projection has one head per actor.  Treat a
        # malformed/mixed projection as unavailable rather than choosing a
        # potentially stale location.
        return candidates[0].values.location_ref if len(candidates) == 1 else None

    @staticmethod
    def _activity(
        projection: LedgerProjection, *, actor_ref: str
    ) -> tuple[str | None, str | None]:
        candidates = tuple(
            item
            for item in projection.plans
            if item.owner_actor_ref == actor_ref
            and item.status in _ACTIVITY_PHASE_ORDER
            and item.privacy_class in _ROOM_VISIBLE_PRIVACY
        )
        if not candidates:
            return None, None
        selected = min(
            candidates,
            key=lambda item: (
                _ACTIVITY_PHASE_ORDER[item.status],
                -item.importance_bp,
                item.plan_id,
            ),
        )
        # The plan's status is the only public lifecycle phase authority.  Do
        # not invent a more detailed progress description from private plan
        # evidence or participant data.
        return selected.activity_kind, selected.status

    @staticmethod
    def _attention(projection: LedgerProjection, *, actor_ref: str) -> str | None:
        candidates = tuple(
            item
            for item in projection.attentions
            if item.actor_ref == actor_ref
            and item.values.privacy_class in _ROOM_VISIBLE_PRIVACY
        )
        return candidates[0].values.mode if len(candidates) == 1 else None

    @staticmethod
    def _visible_status(*, activity_phase: str | None, attention: str | None) -> str | None:
        if attention is not None:
            return _ATTENTION_VISIBLE_STATUS[attention]
        if activity_phase == "active":
            return "active"
        if activity_phase == "paused":
            return "paused"
        if activity_phase == "planned":
            return "scheduled"
        return None


class DashboardPublicProjectionMaterializer:
    """Materialize the small Dashboard-only public facts at one cursor.

    The browser never receives this intermediate model.  It exists so that a
    dashboard capture cannot join a room view to a separately read plan list
    (and accidentally mix cursors), while preserving the same public/privacy
    ceilings used by the minimal room renderer.
    """

    @classmethod
    def materialize(cls, projection: LedgerProjection) -> DashboardPublicProjectionView:
        room = RoomProjectionMaterializer.materialize(projection)
        actor_ref = RoomProjectionMaterializer._companion_actor_ref(projection)
        if actor_ref is None:
            return DashboardPublicProjectionView(situation=room.situation)
        agenda = tuple(
            PublicAgendaProjection(
                activity=item.activity_kind,
                status="active" if item.status == "active" else "scheduled",
                starts_at=item.scheduled_window.opens_at,
            )
            for item in sorted(
                (
                    plan
                    for plan in projection.plans
                    if plan.owner_actor_ref == actor_ref
                    and plan.privacy_class in _ROOM_VISIBLE_PRIVACY
                    and plan.status in {"planned", "active"}
                    and plan.scheduled_window is not None
                ),
                key=lambda plan: (plan.scheduled_window.opens_at, plan.activity_kind, plan.plan_id),
            )[:8]
        )
        return DashboardPublicProjectionView(situation=room.situation, agenda=agenda)


__all__ = ["DashboardPublicProjectionMaterializer", "RoomProjectionMaterializer"]
