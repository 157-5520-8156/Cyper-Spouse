"""NapCat/OneBot HTTP ingress for the normalized World v2 QQ C2C lane.

The module owns only provider-envelope validation and lifecycle scheduling. It
does not import the legacy engine, conversation turn, or coalescer modules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import secrets
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.onebot_adapter import (
    event_token_is_valid,
    get_onebot_friend_msg_history,
)

from .platform_action_executor import MediaProviderTransport
from .production_reliability_metrics import reliability_snapshot
from .production_turn_application import MediaPreviewDeployment
from .qq_attachment_archive import QQOneBotAttachmentArchiver
from .qq_c2c_host import QQC2CHost, build_qq_c2c_host, qq_c2c_world_id
from .qq_media_deployment import build_qq_media_preview_deployment
from .qq_perception_deployment import build_qq_perception_deployment
from .qq_history_backfill import (
    DEFAULT_BACKFILL_COUNT,
    backfill_missed_private_messages,
)
from .qq_ingress_policy import normalize_onebot_qq_ingress


logger = logging.getLogger(__name__)


@dataclass
class QQC2CSchedulerDiagnostics:
    """Process-local evidence that the QQ scheduler is actually making progress."""

    interval_seconds: float
    task: asyncio.Task[None] | None = None
    passes_started: int = 0
    passes_completed: int = 0
    failures: int = 0
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_success_at: datetime | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None

    def snapshot(
        self, *, now: datetime, world: dict[str, object] | None = None
    ) -> dict[str, object]:
        task_running = self.task is not None and not self.task.done()
        stale_after_seconds = max(60.0, self.interval_seconds * 4)
        stale = (
            self.last_completed_at is not None
            and (now - self.last_completed_at).total_seconds() > stale_after_seconds
        )
        if not task_running:
            status = "stopped"
        elif self.last_completed_at is None:
            status = "starting"
        elif stale:
            status = "stale"
        elif self.last_success_at is None:
            status = "failing"
        else:
            status = "running"
        world = world or {}
        return {
            "status": status,
            "task_running": task_running,
            "interval_seconds": self.interval_seconds,
            "passes_started": self.passes_started,
            "passes_completed": self.passes_completed,
            "failures": self.failures,
            "last_started_at": (
                self.last_started_at.isoformat() if self.last_started_at else None
            ),
            "last_completed_at": (
                self.last_completed_at.isoformat() if self.last_completed_at else None
            ),
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "initiative": {
                "last_status": world.get("initiative_last_status"),
                "last_reason": world.get("initiative_last_reason"),
                "pending_opportunity_count": world.get(
                    "pending_proactive_opportunity_count", 0
                ),
                "pending_process_count": world.get(
                    "pending_proactive_process_count", 0
                ),
                "pending_action_count": world.get(
                    "pending_proactive_action_count", 0
                ),
                "spontaneous_candidate_due": world.get(
                    "spontaneous_candidate_due", False
                ),
            },
            "world_activity": {
                "life_event_count": world.get("life_event_count", 0),
                "occurrence_count": world.get("occurrence_count", 0),
                "experience_count": world.get("experience_count", 0),
                "starved": world.get("starved", True),
            },
            # Keep the legacy ``world_activity`` contract stable while
            # exposing the per-mechanism evidence needed to diagnose a live
            # companion.  These values are read-only projection counts; they
            # do not claim that a model used a slice merely because it exists.
            "mechanisms": world.get("mechanisms", {}),
        }


def create_qq_c2c_onebot_app(
    *,
    adapter: str,
    settings: Settings,
    use_fake_model: bool = False,
    scheduler_interval_seconds: float = 15.0,
    media_preview: MediaPreviewDeployment | None = None,
    media_transport: MediaProviderTransport | None = None,
) -> FastAPI:
    """Create the opt-in v2 OneBot service for exactly one private QQ user.

    ``NAPCAT_ALLOWED_PRIVATE_USER_IDS`` is intentionally required to contain
    one id.  A missing or multi-user allowlist would create ambiguous target
    ownership and must not silently map several relationships into one world.
    """

    if adapter not in {"napcat", "onebot"}:
        raise ValueError(f"unsupported OneBot adapter: {adapter}")
    if scheduler_interval_seconds <= 0:
        raise ValueError("QQ C2C v2 scheduler interval must be positive")
    if (media_preview is None) != (media_transport is None):
        raise ValueError(
            "QQ media preview deployment and durable transport must be supplied together"
        )
    recipient_ids = tuple(
        item.strip()
        for item in settings.napcat_allowed_private_user_ids.split(",")
        if item.strip()
    )
    if len(recipient_ids) != 1:
        raise ValueError(
            "World v2 QQ C2C requires exactly one NAPCAT_ALLOWED_PRIVATE_USER_IDS entry"
        )
    recipient_id = recipient_ids[0]
    media_bundle = None
    if media_preview is None and not use_fake_model:
        # The production entry composes its own complete media deployment
        # from Settings.  Missing credentials, an explicit off-switch, or an
        # unprovisioned enforcement grant chain disable the lane with one log
        # line inside the factory; an explicit injected deployment wins.
        # Delivery is world-owned (selection Acceptance + composed
        # guardrails); there is no human approval step.
        media_bundle = build_qq_media_preview_deployment(
            settings=settings, world_id=qq_c2c_world_id(settings.primary_user_id)
        )
        if media_bundle is not None:
            media_preview = media_bundle.deployment
            media_transport = media_bundle.transport
    access_token = (
        settings.napcat_access_token if adapter == "napcat" else settings.onebot_access_token
    ) or None
    perception_bundle = None
    if not use_fake_model:
        # Perception is likewise composed from Settings: a zero budget,
        # missing credentials, or an unprovisioned perception enforcement
        # chain disables the lane with one log line inside the factory.
        perception_bundle = build_qq_perception_deployment(
            settings=settings,
            world_id=qq_c2c_world_id(settings.primary_user_id),
            api_url=(
                settings.napcat_api_url if adapter == "napcat" else settings.onebot_api_url
            ),
            access_token=access_token,
        )
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=recipient_id,
        model=FakeCompanionModel() if use_fake_model else None,
        media_preview=media_preview,
        media_transport=media_transport,
        perception_model=(
            perception_bundle.model if perception_bundle is not None else None
        ),
        perception_input_source=(
            perception_bundle.input_source if perception_bundle is not None else None
        ),
        perception_transport=(
            perception_bundle.transport if perception_bundle is not None else None
        ),
        perception_budget_limit=(
            perception_bundle.budget_limit if perception_bundle is not None else 0
        ),
    )
    scheduler = QQC2CSchedulerDiagnostics(
        interval_seconds=scheduler_interval_seconds
    )

    api_url = (
        settings.napcat_api_url if adapter == "napcat" else settings.onebot_api_url
    )

    async def _fetch_recent_history() -> list[dict[str, object]]:
        return await get_onebot_friend_msg_history(
            api_url,
            user_id=recipient_id,
            count=DEFAULT_BACKFILL_COUNT,
            access_token=access_token,
        )

    def _start_attachment_archive(raw_event: dict[str, object]) -> asyncio.Task | None:
        """Pull inbound image bytes concurrently with the ingress composure wait.

        The archiver owns its failures (a miss degrades to "no bytes to
        perceive"); this hook only decides *when* it runs so a provider
        download never delays accepting the message itself.
        """

        if perception_bundle is None:
            return None
        if not QQOneBotAttachmentArchiver.image_segments(raw_event):
            return None
        return asyncio.create_task(
            perception_bundle.archiver.archive_from_event(raw_event),
            name="world-v2-qq-attachment-archive",
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Restart-window compensation: messages the user sent while this
        # process was down exist only in provider history.  Replay them
        # through the ordinary dedup ingress before live traffic resumes;
        # the pass runs as a background task so startup and push ingress
        # never block on a slow or absent provider history API.
        backfill_task = asyncio.create_task(
            backfill_missed_private_messages(
                host=host,
                fetch_history=_fetch_recent_history,
                recipient_id=recipient_id,
                archive_event=(
                    perception_bundle.archiver.archive_from_event
                    if perception_bundle is not None
                    else None
                ),
            ),
            name="world-v2-qq-c2c-history-backfill",
        )
        task = asyncio.create_task(
            _scheduler_loop(
                host,
                interval_seconds=scheduler_interval_seconds,
                diagnostics=scheduler,
            ),
            name="world-v2-qq-c2c-scheduler",
        )
        scheduler.task = task
        try:
            yield
        finally:
            backfill_task.cancel()
            task.cancel()
            await asyncio.gather(backfill_task, task, return_exceptions=True)
            await host.aclose()
            if media_bundle is not None:
                media_bundle.transport.close()
            if perception_bundle is not None:
                perception_bundle.close()

    app = FastAPI(title=f"Girl-Agent {adapter.title()} World v2 C2C", lifespan=lifespan)
    app.state.qq_c2c_host = host

    @app.post("/onebot/event")
    async def onebot_event(
        request: Request,
        authorization: str | None = Header(None),
        x_signature: str | None = Header(None),
    ):
        if not _event_request_is_authorized(
            request,
            access_token,
            authorization=authorization,
            x_signature=x_signature,
            accept_unauthenticated_local=settings.napcat_accept_unauthenticated_local_events,
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        raw_event = await request.json()
        if raw_event.get("post_type") == "message" and raw_event.get("message_type") == "group":
            return {"status": "ignored_group_v2_unsupported"}
        try:
            fragment = normalize_onebot_qq_ingress(raw_event)
        except (TypeError, ValueError):
            return JSONResponse(
                {"status": "rejected_invalid_qq_ingress"}, status_code=400
            )
        if fragment is None:
            return {"status": "ignored_qq_shape_v2_unsupported"}
        if fragment.recipient_id != recipient_id:
            return {"status": "ignored_private"}
        archive_task = _start_attachment_archive(raw_event)
        try:
            result = await host.inbound_fragment(fragment)
        finally:
            if archive_task is not None:
                await archive_task
        return {
            "status": result.status,
            "world_action_id": result.action_id,
            "canonical_user_id": result.canonical_user_id,
        }

    @app.get("/health")
    async def health():
        world = await host.world_health_diagnostics()
        scheduler_view = scheduler.snapshot(now=datetime.now(UTC), world=world)
        # Rolling process-local reliability counters (24h window): visible
        # inbound replies, local failsafe engagements, corrective repairs,
        # claim-free boundary lines, and backup-provider recoveries.  The
        # ledger stays the durable audit; this makes the failsafe rate
        # checkable at a glance without a ledger scan.
        scheduler_view["reliability"] = reliability_snapshot()
        return {
            "status": "running",
            "adapter": adapter,
            "world_v2": True,
            "mode": "c2c-normalized-ingress",
            "scheduler": scheduler_view,
        }

    def _media_observer_access(token: str | None) -> JSONResponse | None:
        """Gate the read-only media observation surface behind the operator secret.

        This mirrors the daemon's ``/internal/world-v2/*`` discipline: the
        surface stays disabled until ``DELIVERY_RECONCILIATION_TOKEN`` exists,
        and a wrong token is rejected without leaking media contents.  The
        surface is deliberately observation-only — delivery is decided by the
        world's own selection/acceptance chain and its composed guardrails.
        """

        configured = (settings.delivery_reconciliation_token or "").strip()
        if not configured:
            return JSONResponse(
                {"error": "media observation surface is disabled until an operator token is configured"},
                status_code=503,
            )
        if not token or not secrets.compare_digest(token, configured):
            return JSONResponse({"error": "invalid operator token"}, status_code=403)
        return None

    @app.get("/internal/world-v2/media/previews")
    async def media_previews(
        x_world_v2_internal_token: str | None = Header(None),
    ):
        denied = _media_observer_access(x_world_v2_internal_token)
        if denied is not None:
            return denied
        observer = host.media_preview_operator()
        return {"previews": list(observer.queue(materialize=True))}

    @app.get("/internal/world-v2/media/previews/{preview_id}/image")
    async def media_preview_image(
        preview_id: str,
        x_world_v2_internal_token: str | None = Header(None),
    ):
        denied = _media_observer_access(x_world_v2_internal_token)
        if denied is not None:
            return denied
        observer = host.media_preview_operator()
        row = next(
            (item for item in observer.queue(materialize=True) if item["preview_id"] == preview_id),
            None,
        )
        if row is None or not row.get("image_path"):
            return JSONResponse({"error": "preview image is unavailable"}, status_code=404)
        return FileResponse(str(row["image_path"]), media_type="image/png")

    return app


async def _scheduler_loop(
    host: QQC2CHost,
    *,
    interval_seconds: float,
    diagnostics: QQC2CSchedulerDiagnostics,
) -> None:
    """Bounded recovery loop; each pass resumes from the durable v2 clock."""

    while True:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        diagnostics.passes_started += 1
        diagnostics.last_started_at = started_at
        try:
            await host.scheduler_once(observed_at=datetime.now(UTC))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            diagnostics.failures += 1
            diagnostics.last_error = type(exc).__name__
            logger.exception("World v2 QQ C2C scheduler pass failed")
        else:
            diagnostics.last_success_at = datetime.now(UTC)
            diagnostics.last_error = None
        diagnostics.passes_completed += 1
        diagnostics.last_completed_at = datetime.now(UTC)
        diagnostics.last_duration_ms = round((time.monotonic() - started) * 1_000)
        try:
            # Passive WAL compaction is scheduler upkeep, never reply work.
            # The ledger self-throttles by WAL size and a minimum interval,
            # and yields immediately to any active writer.
            result = await host.maintain_wal_once()
            if result is not None and getattr(result, "status", "skipped") != "skipped":
                logger.info(
                    "world v2 QQ WAL maintenance status=%s before_bytes=%s after_bytes=%s "
                    "log_frames=%s checkpointed_frames=%s",
                    result.status,
                    result.wal_bytes_before,
                    result.wal_bytes_after,
                    result.log_frames,
                    result.checkpointed_frames,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("World v2 QQ WAL maintenance failed")
        await asyncio.sleep(interval_seconds)


def _event_request_is_authorized(
    request: Request,
    expected_token: str | None,
    *,
    authorization: str | None,
    x_signature: str | None,
    accept_unauthenticated_local: bool,
) -> bool:
    if event_token_is_valid(expected_token, authorization=authorization, x_signature=x_signature):
        return True
    client_host = request.client.host if request.client else None
    return bool(accept_unauthenticated_local and client_host in {"127.0.0.1", "::1"})


__all__ = ["QQC2CSchedulerDiagnostics", "create_qq_c2c_onebot_app"]
