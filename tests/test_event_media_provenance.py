"""P0 provenance guards for the legacy image planner boundary."""

from companion_daemon.event_media import (
    MediaOpportunity,
    NotRenderable,
    _allowed_evidence_pointers,
    _freeze_proposal,
)


def _proposal(*, primary: str) -> dict[str, object]:
    return {
        "content_domain": "food_drink",
        "visual_form": "contextual_still_life",
        "share_intent": "show_and_tell",
        "capture_mode": "character_rear_camera",
        "character_visibility": "none",
        "other_people_visibility": "none",
        "polish": "casual",
        "tone": "bright",
        "privacy": "ordinary",
        "primary_evidence_ref": primary,
        "supporting_evidence_refs": [],
        "composition": "突出主证据细节的近景",
        "action": "自然地把{primary}带进画面",
        "camera_direction": "后摄正常透视且没有自拍臂",
        "sharing_motive": "把这个生活瞬间分享给熟悉的人",
        "constraints": ["不生成可读文字"],
        "route": "generate",
        "interaction_bid_id": "share_discovery",
    }


def test_explicit_evidence_allowlist_excludes_resolvable_containers_and_index_metadata() -> None:
    opportunity = MediaOpportunity(
        opportunity_id="opportunity:p0-provenance",
        family="life_share",
        privacy_ceiling="ordinary",
        delivery_mode="preview",
        event_snapshot={
            "event": {"event_id": "event:p0", "status": "committed", "summary": "coffee"},
            "objects": [{"description": "coffee"}],
            "evidence_index": {"/event/summary": {"source_event_ref": "event:p0"}},
        },
        allowed_evidence_refs=("/event/summary",),
    )

    assert _allowed_evidence_pointers(opportunity) == ("/event/summary",)

    rejected = _freeze_proposal(
        opportunity,
        _proposal(primary="/evidence_index"),
        (),
    )

    assert isinstance(rejected, NotRenderable)
    assert rejected.reason == "unapproved_evidence_ref"
