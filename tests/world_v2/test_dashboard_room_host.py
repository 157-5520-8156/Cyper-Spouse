"""Contract tests for the dashboard's embedded pixel-home room.

The World v2 panel hosts the pixel-home prototype in an iframe and relays
life-state to it via a versioned postMessage.  These tests pin the two sides
of that seam (host page and prototype bridge) plus the static mount, without
rendering anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

import companion_daemon.app as app_module
from companion_daemon.config import Settings
from companion_daemon.world_v2.world_v2_dashboard_ui import DASHBOARD_APP_JS, DASHBOARD_HTML


REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_SOURCE = (REPO_ROOT / "prototypes" / "pixel-home" / "js" / "bridge.js").read_text(
    encoding="utf-8"
)
PROTOTYPE_INDEX = (REPO_ROOT / "prototypes" / "pixel-home" / "index.html").read_text(
    encoding="utf-8"
)


def test_dashboard_html_embeds_pixel_home_instead_of_static_render() -> None:
    assert '<canvas id="stage" width="1120" height="640">' in PROTOTYPE_INDEX
    assert '<iframe id="roomVisual" src="/pixel-home/index.html?embed=1"' in DASHBOARD_HTML
    assert "zhizhi-room-isometric" not in DASHBOARD_HTML
    assert "zhizhi-room-isometric" not in DASHBOARD_APP_JS
    assert 'href="/pixel-home/index.html?edit=1"' in DASHBOARD_HTML
    assert 'aria-label="在独立页面编辑小屋"' in DASHBOARD_HTML
    assert 'title="知栀的小屋日常画面"' in DASHBOARD_HTML
    assert 'aspect-ratio:7/4' in DASHBOARD_HTML
    assert 'aspect-ratio:4/3' not in DASHBOARD_HTML
    assert "pointer-events:none" in DASHBOARD_HTML
    assert 'id="roomRoute"' not in DASHBOARD_HTML


def test_dashboard_script_posts_versioned_scene_state_to_the_iframe() -> None:
    assert "postMessage" in DASHBOARD_APP_JS
    assert "window.location.origin" in DASHBOARD_APP_JS
    assert "at_home:" in DASHBOARD_APP_JS
    # Home is her dorm room; anything else means she is out.
    assert "'location:ecnu-dorm-room'" in DASHBOARD_APP_JS
    assert "'/pixel-home/index.html?embed=1&hour='" in DASHBOARD_APP_JS


def test_host_and_bridge_agree_on_message_type_and_version() -> None:
    host_type = re.search(r"ROOM_SCENE_STATE_TYPE='([^']+)'", DASHBOARD_APP_JS)
    bridge_type = re.search(r"MESSAGE_TYPE = '([^']+)'", BRIDGE_SOURCE)
    assert host_type is not None and bridge_type is not None
    assert host_type.group(1) == bridge_type.group(1) == "zhizhi-scene-state"

    host_version = re.search(r"type:ROOM_SCENE_STATE_TYPE,v:(\d+)", DASHBOARD_APP_JS)
    bridge_version = re.search(r"MESSAGE_VERSION = (\d+)", BRIDGE_SOURCE)
    assert host_version is not None and bridge_version is not None
    assert host_version.group(1) == bridge_version.group(1) == "1"


def test_bridge_maps_daemon_activities_without_touching_engine_sources() -> None:
    assert "ACTIVITY_KEY_RULES" in BRIDGE_SOURCE
    # The bridge drives the engine only through its public actor commands.
    for public_call in ("dispatch(", "walkTo(", "endActivity(", "availableInteractions("):
        assert public_call in BRIDGE_SOURCE
    # index.html gains exactly one bridge script tag after main.js.
    assert PROTOTYPE_INDEX.index("js/main.js") < PROTOTYPE_INDEX.index("js/bridge.js")
    assert PROTOTYPE_INDEX.count("bridge.js") == 1
    assert "body.embed header" in PROTOTYPE_INDEX
    assert "body.embed .toolbar" in PROTOTYPE_INDEX
    assert "body.embed #iobox" in PROTOTYPE_INDEX
    assert "body.embed .stage-wrap{width:100%;border:0" in PROTOTYPE_INDEX


def test_pixel_home_prototype_is_mounted_read_only(tmp_path: Path) -> None:
    asgi_app = app_module.create_http_asgi_app(
        settings=Settings(_env_file=None, database_path=tmp_path / "pixel-home.sqlite")
    )

    with TestClient(asgi_app) as client:
        index = client.get("/pixel-home/index.html")
        bridge = client.get("/pixel-home/js/bridge.js")

    assert index.status_code == 200
    assert "js/bridge.js" in index.text
    assert bridge.status_code == 200
    assert "zhizhi-scene-state" in bridge.text
