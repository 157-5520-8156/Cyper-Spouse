"""Production factory for the QQ World v2 media deployment.

This is the one place that turns deployment ``Settings`` into a complete,
explicit :class:`MediaPreviewDeployment` plus its durable provider transport.
It composes only already-reviewed seams:

- bounded candidate selection uses the flash chat model;
- planning goes through ``EventMediaPlannerAdapter`` with a durable SQLite
  terminal-result store, wrapping the image machine's v5 ``MediaPlanner``
  with the module flags passed explicitly (no environment switches);
- render/inspection wrap ``MediaRenderer``/``OpenAIMediaInspector`` behind
  :class:`SQLiteDurableMediaProviderTransport` so restart recovery replays
  the exact stored bytes;
- grant bindings reference the identities written by
  :mod:`media_authority_provisioning`; this factory never manufactures
  enforcement authority;
- delivery is world-owned: the send decision is the accepted media
  selection itself, and the composed :class:`MediaAutoDeliveryComposition`
  adds only conservative operational guardrails (daily cap, minimum gap).
  No human approval step exists anywhere in this lane.

Missing prerequisites disable the whole lane with exactly one log line.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from pathlib import Path
import sqlite3

import httpx

from companion_daemon.config import Settings

from .event_media_planner_adapter import (
    EventMediaPlannerAdapter,
    SQLiteEventMediaPlanningResultStore,
)
from .media_authority_provisioning import (
    MEDIA_INSPECTION_GRANT_ID,
    MEDIA_PLANNING_GRANT_ID,
    MEDIA_RENDER_GRANT_ID,
)
from .media_auto_delivery import MediaAutoDeliveryComposition
from .media_provider_transport import SQLiteDurableMediaProviderTransport
from .media_v2 import SQLiteImmutableMediaPayloadStore
from .production_turn_application import (
    MediaContinuationComposition,
    MediaPreviewDeployment,
    MediaSelectionAcceptanceComposition,
)
from .qq_c2c_host import qq_c2c_target
from .schemas import ProviderMediaGrantBinding


_LOG = logging.getLogger(__name__)

MEDIA_SELECTION_ACCOUNT_ID = "account:world-v2:media-selection"
MEDIA_RENDER_ACCOUNT_ID = "account:world-v2:media-render"
MEDIA_INSPECTION_ACCOUNT_ID = "account:world-v2:media-inspection"

_REQUIRED_GRANT_IDS = (
    MEDIA_PLANNING_GRANT_ID,
    MEDIA_RENDER_GRANT_ID,
    MEDIA_INSPECTION_GRANT_ID,
)


@dataclass(frozen=True, slots=True)
class QQMediaDeploymentBundle:
    """Everything ``create_qq_c2c_onebot_app`` needs to install the media lane."""

    deployment: MediaPreviewDeployment
    transport: SQLiteDurableMediaProviderTransport


class InspectorHardeningTransport(httpx.AsyncBaseTransport):
    """Deployment-level HTTP hardening for the visual inspection provider.

    Two live failure modes observed through the proxy route are absorbed here
    without touching the image machine:

    - the vision model occasionally answers a descriptive *list* field
      (``observed_facts`` etc.) as an object or bare string, which crashes
      the inspector's parse outside its own error net.  Shape is normalized
      to a list of strings; verdict fields (``passed``/``reason``/booleans)
      are deliberately never rewritten;
    - the default 45s client read timeout is too tight for a multi-megabyte
      image upload through the proxy, so the per-request timeout is widened.
    """

    _LIST_KEYS = (
        "observed_facts",
        "deviations",
        "salient_expression_cues",
        "forbidden_expression_cues",
        "observed_physical_cues",
        "unsupported_physical_cues",
    )

    def __init__(
        self,
        *,
        proxy_url: str | None,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if inner is not None:
            self._inner = inner
        elif proxy_url:
            self._inner = httpx.AsyncHTTPTransport(proxy=proxy_url)
        else:
            self._inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.extensions["timeout"] = httpx.Timeout(
            connect=30.0, read=150.0, write=150.0, pool=30.0
        ).as_dict()
        # Ask for an uncompressed body so the JSON below is inspectable at
        # the transport seam.
        request.headers["accept-encoding"] = "identity"
        response = await self._inner.handle_async_request(request)
        raw = await response.aread()
        if response.status_code == 200:
            try:
                envelope = json.loads(raw)
                content = json.loads(envelope["choices"][0]["message"]["content"])
                changed = False
                if isinstance(content, dict):
                    for key in self._LIST_KEYS:
                        value = content.get(key)
                        if isinstance(value, list):
                            continue
                        if key in content and value is None:
                            # An explicit JSON null defeats ``.get(key, [])``
                            # in the parser and crashes its slice.
                            content[key] = []
                        elif value is None:
                            continue
                        elif isinstance(value, dict):
                            content[key] = [f"{k}: {v}" for k, v in value.items()]
                        else:
                            content[key] = [str(value)]
                        changed = True
                if changed:
                    envelope["choices"][0]["message"]["content"] = json.dumps(
                        content, ensure_ascii=False
                    )
                    raw = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
            except (KeyError, IndexError, TypeError, ValueError):
                pass
        headers = [
            (name, value)
            for name, value in response.headers.raw
            if name.lower() not in {b"content-length", b"content-encoding"}
        ]
        return httpx.Response(
            status_code=response.status_code,
            headers=headers,
            content=raw,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()


def _provisioned_grants_present(*, database_path: Path, world_id: str) -> bool:
    """Cheap preflight: are the enforcement grants recorded for this world?

    This intentionally avoids a full ledger replay at composition time.  It
    is only a gate for enabling the lane; the reducers and the ActionPump
    remain the enforcement authority for every dispatch.
    """

    if not database_path.exists():
        return False
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        for grant_id in _REQUIRED_GRANT_IDS:
            row = connection.execute(
                "SELECT COUNT(*) FROM world_v2_events WHERE world_id = ? "
                "AND json_extract(event_json, '$.event_type') = 'ProviderMediaGrantRecorded' "
                "AND event_json LIKE ?",
                (world_id, f"%{grant_id}%"),
            ).fetchone()
            if row is None or int(row[0]) < 1:
                return False
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def build_qq_media_preview_deployment(
    *, settings: Settings, world_id: str
) -> QQMediaDeploymentBundle | None:
    """Assemble the world-delivered media deployment, or disable it with one log line."""

    missing: list[str] = []
    if not settings.world_v2_media_preview_enabled:
        missing.append("WORLD_V2_MEDIA_PREVIEW_ENABLED")
    if not settings.deepseek_api_key:
        missing.append("DEEPSEEK_API_KEY")
    if not settings.openai_api_key:
        missing.append("OPENAI_API_KEY")
    recipient_ids = tuple(
        item.strip()
        for item in settings.napcat_allowed_private_user_ids.split(",")
        if item.strip()
    )
    if len(recipient_ids) != 1:
        missing.append("NAPCAT_ALLOWED_PRIVATE_USER_IDS (exactly one recipient)")
    database_path = Path(settings.database_path)
    if not missing and not _provisioned_grants_present(
        database_path=database_path, world_id=world_id
    ):
        missing.append("provider media grants (scripts/provision_world_v2_media_authority.py)")
    if missing:
        _LOG.warning(
            "world v2 media lane disabled for %s; missing: %s",
            world_id,
            ", ".join(missing),
        )
        return None

    from companion_daemon import event_media
    from companion_daemon.image_generation import OpenAIImageGenerator
    from companion_daemon.llm import (
        DeepSeekChatModel,
        FailoverChatModel,
        OpenAICompatibleChatModel,
    )

    def routed_model(*, model: str, fallback_max_completion_tokens: int):
        """Mirror the chat lane's Flash provider route (DeepSeek → proxy)."""

        primary = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=model,
            thinking_enabled=False,
        )
        fallback = OpenAICompatibleChatModel(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            model=settings.world_v2_fallback_model,
            reasoning_effort="none",
            max_completion_tokens=fallback_max_completion_tokens,
            proxy_url=settings.openai_proxy_url,
        )
        return FailoverChatModel(primary=primary, fallback=fallback)

    selection_model = routed_model(
        model=settings.deepseek_model, fallback_max_completion_tokens=200
    )
    planner_model = routed_model(
        model=settings.world_v2_media_planner_model or settings.deepseek_model,
        fallback_max_completion_tokens=3_000,
    )
    # Module switches are explicit constructor facts here, not process
    # environment probes: this composition is the deployment decision.
    legacy_planner = event_media.MediaPlanner(
        planner_model, enabled=True, v5_enabled=True
    )
    sidecar = SQLiteImmutableMediaPayloadStore(
        path=str(database_path), world_id=world_id
    )
    planner = EventMediaPlannerAdapter(
        sidecar=sidecar,
        legacy_planner=legacy_planner,
        result_store=SQLiteEventMediaPlanningResultStore(
            path=str(database_path), world_id=world_id
        ),
    )
    generator = OpenAIImageGenerator(
        settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.image_model,
        proxy_url=settings.openai_proxy_url,
    )
    inspector = event_media.OpenAIMediaInspector(
        settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.world_v2_media_inspection_model,
        # The hardening transport owns the proxy route, a proxy-sized read
        # timeout, and normalization of malformed descriptive list fields.
        transport=InspectorHardeningTransport(proxy_url=settings.openai_proxy_url),
    )
    # Composed directly rather than through the archived runtime module (the
    # platform reverse-architecture guard forbids that import).  No
    # specialized high-private generators and no private prompt author are
    # installed: this deployment serves the ordinary life lane, and frozen
    # high-lane plans fail closed exactly as the renderer contract requires.
    renderer = event_media.MediaRenderer(
        generator=generator,
        inspector=inspector,
        output_dir=Path("output/event-media"),
        visual_identity_path=settings.visual_identity_path,
    )
    transport = SQLiteDurableMediaProviderTransport(
        path=str(database_path), world_id=world_id, renderer=renderer
    )
    deployment = MediaPreviewDeployment(
        selection_model=selection_model,
        planner=planner,
        acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_PLANNING_GRANT_ID, grant_revision=1
            ),
            account_id=MEDIA_SELECTION_ACCOUNT_ID,
            account_window_id="window:world-v2:media-selection",
            account_limit=10_000,
            amount_limit=1,
        ),
        continuation=MediaContinuationComposition(
            render_grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_RENDER_GRANT_ID, grant_revision=1
            ),
            render_account_id=MEDIA_RENDER_ACCOUNT_ID,
            render_window_id="window:world-v2:media-render",
            render_account_limit=10_000,
            # Zero-amount reservations mark these as deployment-paid provider
            # operations (billed by OpenAI directly); the ledger still tracks
            # effect-once dispatch.  Per-image cost is structurally fixed by
            # the renderer profile (one gpt-image call, 1024x1536/medium, no
            # fan-out), and volume is bounded by the ecology's daily candidate
            # caps plus the delivery guardrails below.
            render_amount_limit=0,
            inspection_grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_INSPECTION_GRANT_ID, grant_revision=1
            ),
            inspection_account_id=MEDIA_INSPECTION_ACCOUNT_ID,
            inspection_window_id="window:world-v2:media-inspection",
            inspection_account_limit=10_000,
            inspection_amount_limit=0,
        ),
        # World-owned delivery: the send decision is the accepted selection;
        # these are conservative operational guardrails, not an approval.
        auto_delivery=MediaAutoDeliveryComposition(
            delivery_target_ref=qq_c2c_target(recipient_ids[0]),
            recipient_ref=f"user:{settings.primary_user_id}",
            account_id=MEDIA_SELECTION_ACCOUNT_ID,
            amount_limit=0,
        ),
    )
    _LOG.warning(
        "world v2 media lane enabled for %s (world-owned delivery, guardrails on)",
        world_id,
    )
    return QQMediaDeploymentBundle(deployment=deployment, transport=transport)


__all__ = [
    "InspectorHardeningTransport",
    "MEDIA_INSPECTION_ACCOUNT_ID",
    "MEDIA_RENDER_ACCOUNT_ID",
    "MEDIA_SELECTION_ACCOUNT_ID",
    "QQMediaDeploymentBundle",
    "build_qq_media_preview_deployment",
]
