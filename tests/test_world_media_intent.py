from companion_daemon.world_media_intent import WorldMediaIntentPolicy


def test_active_world_activity_selects_a_replayable_human_photo_intent() -> None:
    snapshot = {
        "agenda": {"show": {"activity_id": "show", "status": "active", "template_id": "photo_portfolio"}}
    }

    first = WorldMediaIntentPolicy().choose(snapshot, request_id="media:show")
    replay = WorldMediaIntentPolicy().choose(snapshot, request_id="media:show")

    assert first == replay
    assert first is not None
    assert first.intent in {"check_in_pose", "atmosphere_record", "playful_share"}


def test_registered_companion_selects_candid_instead_of_paparazzi_by_default() -> None:
    snapshot = {
        "agenda": {
            "show": {
                "activity_id": "show", "status": "active", "template_id": "photo_portfolio",
                "companions": ["friend:zhou"],
            }
        }
    }

    choice = WorldMediaIntentPolicy().choose(snapshot, request_id="media:show")

    assert choice is not None
    assert choice.intent == "companion_candid"


def test_no_active_world_activity_never_invents_a_photo_opportunity() -> None:
    assert WorldMediaIntentPolicy().choose({"agenda": {}}, request_id="media:none") is None


def test_pending_media_prevents_a_second_proactive_photo() -> None:
    snapshot = {
        "agenda": {"walk": {"activity_id": "walk", "status": "active", "template_id": "campus_walk"}},
        "media": {"already": {"status": "generated"}},
    }

    assert WorldMediaIntentPolicy().choose(snapshot, request_id="media:second") is None
