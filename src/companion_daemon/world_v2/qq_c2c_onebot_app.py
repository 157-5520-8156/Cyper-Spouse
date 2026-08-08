"""NapCat/OneBot HTTP ingress for the normalized World v2 QQ C2C lane.

The module owns only provider-envelope validation and lifecycle scheduling. It
does not import the legacy engine, conversation turn, or coalescer modules.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
import secrets
import time

from fastapi import FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse

from companion_daemon.config import Settings
from companion_daemon.llm import FakeCompanionModel
from companion_daemon.ledger_storage_health import ledger_storage_snapshot
from companion_daemon.onebot_adapter import (
    event_token_is_valid,
    get_onebot_friend_msg_history,
)

from .platform_action_executor import MediaProviderTransport
from .model_completion import ChatCompletionModel
from .production_latency_health import production_latency_health_snapshot
from .production_reliability_metrics import reliability_snapshot
from .durable_reliability import durable_reliability_snapshot
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
        elif self.last_error is not None and (
            self.last_success_at is None or self.last_completed_at > self.last_success_at
        ):
            status = "failing"
        else:
            status = "running"
        world = world or {}
        raw_initiative_warning_reasons = world.get("initiative_warning_reasons", [])
        initiative_warning_reasons = [
            item
            for item in (
                raw_initiative_warning_reasons
                if isinstance(raw_initiative_warning_reasons, (list, tuple))
                else ()
            )
            if isinstance(item, str) and item != "consideration_overdue"
        ]
        next_consideration_at = world.get("initiative_next_consideration_at")
        due_at = None
        if isinstance(next_consideration_at, str):
            try:
                parsed_due_at = datetime.fromisoformat(next_consideration_at)
            except ValueError:
                parsed_due_at = None
            if (
                parsed_due_at is not None
                and parsed_due_at.tzinfo is not None
                and parsed_due_at.utcoffset() is not None
            ):
                due_at = parsed_due_at
        # "Two scheduler cycles" is an adapter liveness promise, so its
        # threshold must come from this process's actual scheduler cadence.
        # The application projection has no authority to invent a platform
        # polling interval, and wall time lets this warning fire even when a
        # failed scheduler has stopped advancing the World clock itself.
        if (
            world.get("initiative_state") == "consideration_due"
            and due_at is not None
            and now - due_at > timedelta(seconds=self.interval_seconds * 2)
        ):
            initiative_warning_reasons.append("consideration_overdue")
        unexplained_initiative_warning = (
            bool(world.get("initiative_warning", False)) and not raw_initiative_warning_reasons
        )
        return {
            "status": status,
            "task_running": task_running,
            "interval_seconds": self.interval_seconds,
            "passes_started": self.passes_started,
            "passes_completed": self.passes_completed,
            "failures": self.failures,
            "last_started_at": (self.last_started_at.isoformat() if self.last_started_at else None),
            "last_completed_at": (
                self.last_completed_at.isoformat() if self.last_completed_at else None
            ),
            "last_success_at": (self.last_success_at.isoformat() if self.last_success_at else None),
            "last_duration_ms": self.last_duration_ms,
            "last_error": self.last_error,
            "initiative": {
                "last_status": world.get("initiative_last_status"),
                "last_reason": world.get("initiative_last_reason"),
                "pending_opportunity_count": world.get("pending_proactive_opportunity_count", 0),
                "pending_process_count": world.get("pending_proactive_process_count", 0),
                "pending_action_count": world.get("pending_proactive_action_count", 0),
                "spontaneous_candidate_due": world.get("spontaneous_candidate_due", False),
                "state": world.get("initiative_state", "waiting_context"),
                "last_considered_at": world.get("initiative_last_considered_at"),
                "last_model_decision": world.get("initiative_last_model_decision"),
                "last_decision_reason": world.get("initiative_last_decision_reason"),
                "last_impulse_summary": world.get("initiative_last_impulse_summary"),
                "last_grounding_outcome": world.get("initiative_last_grounding_outcome"),
                "grounding_corrected_count": world.get("initiative_grounding_corrected_count", 0),
                "grounding_rejected_count": world.get("initiative_grounding_rejected_count", 0),
                "stimulus_source_count": world.get("initiative_stimulus_source_count", 0),
                "stimulus_merge_window_seconds": world.get(
                    "initiative_stimulus_merge_window_seconds", 600
                ),
                "pending_expectation_count": world.get("initiative_pending_expectation_count", 0),
                "expectation_status_counts": world.get("initiative_expectation_status_counts", {}),
                "next_consideration_at": world.get("initiative_next_consideration_at"),
                "cadence_reason_codes": world.get("initiative_cadence_reason_codes", []),
                "consecutive_technical_failures": world.get(
                    "initiative_consecutive_technical_failures", 0
                ),
                "retry_ordinal": world.get("initiative_retry_ordinal", 0),
                "last_failure_code": world.get("initiative_last_failure_code"),
                "reliability_24h": world.get(
                    "initiative_reliability_24h",
                    {
                        "window_hours": 24,
                        "as_of": None,
                        "attempt_count": 0,
                        "consideration_count": 0,
                        "technical_failure_attempt_count": 0,
                        "technical_failure_consideration_count": 0,
                        "model_silent_count": 0,
                        "grounding_rejected_count": 0,
                        "authorized_count": 0,
                        "delivered_count": 0,
                        "delivery_pending_count": 0,
                        "delivery_non_delivered_terminal_count": 0,
                        "model_decision_success_rate": None,
                        "technical_failure_rate": None,
                        "technical_failure_attempt_rate": None,
                        "visible_authorization_rate": None,
                        "visible_delivery_rate": None,
                        "delivery_success_rate": None,
                        "technical_failure_codes": {},
                        "warning": False,
                        "warning_reasons": [],
                    },
                ),
                "warning": bool(initiative_warning_reasons) or unexplained_initiative_warning,
                "warning_reasons": initiative_warning_reasons,
            },
            "world_activity": {
                "life_event_count": world.get("life_event_count", 0),
                "occurrence_count": world.get("occurrence_count", 0),
                "experience_count": world.get("experience_count", 0),
                "starved": world.get("starved", True),
            },
            "expression_episode": world.get("expression_episode", {}),
            "expression_retry": world.get("expression_retry", {}),
            "character_interior": world.get(
                "character_interior",
                {
                    "status": "unavailable",
                    "installed": False,
                    "semantic_author_count": 0,
                    "primary_author_model": None,
                    "primary_author_route": None,
                    "parallel_character_author_conflicts": 0,
                    "legacy_interface_invocations": 0,
                    "dual_write_conflicts": 0,
                },
            ),
            # Keep the legacy ``world_activity`` contract stable while
            # exposing the per-mechanism evidence needed to diagnose a live
            # companion.  These values are read-only projection counts; they
            # do not claim that a model used a slice merely because it exists.
            "mechanisms": world.get("mechanisms", {}),
            "recall_semantic": world.get(
                "recall_semantic",
                {"enabled": False},
            ),
        }


def create_qq_c2c_onebot_app(
    *,
    adapter: str,
    settings: Settings,
    use_fake_model: bool = False,
    _test_only_model: ChatCompletionModel | None = None,
    _test_only_world_support_model: ChatCompletionModel | None = None,
    _test_only_source_closure_model: ChatCompletionModel | None = None,
    _test_only_life_source_closure_model: ChatCompletionModel | None = None,
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
        item.strip() for item in settings.napcat_allowed_private_user_ids.split(",") if item.strip()
    )
    if len(recipient_ids) != 1:
        raise ValueError(
            "World v2 QQ C2C requires exactly one NAPCAT_ALLOWED_PRIVATE_USER_IDS entry"
        )
    recipient_id = recipient_ids[0]
    test_authorities_injected = any(
        model is not None
        for model in (
            _test_only_model,
            _test_only_world_support_model,
            _test_only_source_closure_model,
            _test_only_life_source_closure_model,
        )
    )
    if use_fake_model and test_authorities_injected:
        raise ValueError("fake-model mode cannot also inject test authorities")
    if test_authorities_injected:
        if _test_only_model is None or _test_only_source_closure_model is None:
            raise ValueError(
                "test authority injection requires a character author and source reviewer"
            )
        strict_checker = getattr(
            _test_only_source_closure_model,
            "supports_strict_output_contract",
            None,
        )
        if not callable(strict_checker) or not all(
            strict_checker(contract) is True
            for contract in (
                "report-relative-entailment-adjudication.3",
                "source-closure-review.7",
            )
        ):
            raise ValueError("test source reviewer requires exact RR.3/V7 qualification")
    if (
        not use_fake_model
        and _test_only_model is None
        and not (settings.deepseek_api_key or "").strip()
    ):
        raise RuntimeError(
            "World v2 production QQ C2C requires a configured real character provider; "
            "set DEEPSEEK_API_KEY or explicitly enable the fake-model test mode"
        )
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
            api_url=(settings.napcat_api_url if adapter == "napcat" else settings.onebot_api_url),
            access_token=access_token,
        )
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=recipient_id,
        model=FakeCompanionModel() if use_fake_model else _test_only_model,
        world_support_model=_test_only_world_support_model,
        source_closure_model=_test_only_source_closure_model,
        life_source_closure_model=_test_only_life_source_closure_model,
        media_preview=media_preview,
        media_transport=media_transport,
        perception_input_source=(
            perception_bundle.input_source if perception_bundle is not None else None
        ),
        perception_transport=(
            perception_bundle.transport if perception_bundle is not None else None
        ),
        perception_budget_limit=(
            perception_bundle.budget_limit if perception_bundle is not None else 0
        ),
        scheduler_interval_seconds=scheduler_interval_seconds,
    )
    scheduler = QQC2CSchedulerDiagnostics(interval_seconds=scheduler_interval_seconds)

    api_url = settings.napcat_api_url if adapter == "napcat" else settings.onebot_api_url

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
            wait_for_quiescence = getattr(
                host,
                "wait_for_shutdown_quiescence",
                None,
            )
            if callable(wait_for_quiescence):
                await wait_for_quiescence()
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
            return JSONResponse({"status": "rejected_invalid_qq_ingress"}, status_code=400)
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
        scheduler_view["local_provider_capacity"] = host.local_provider_capacity_health()
        scheduler_view["text_turn_endpoint"] = host.text_endpoint_health()
        scheduler_view["proactive_source_authority"] = host.proactive_source_authority_health()
        scheduler_view["life_source_authority"] = host.life_source_authority_health()
        scheduler_view["budget"] = host.usage_budget_health()
        external_perception_health = host.external_world_perception_health()
        downstream = world.get("external_perception_downstream")
        if isinstance(downstream, dict):
            external_perception_health["downstream"] = downstream
        scheduler_view["external_world_perception"] = external_perception_health
        scheduler_view["performance"] = production_latency_health_snapshot(host.latency_samples())
        # Rolling process-local reliability counters (24h window): provider
        # dispatch ACKs are reported separately from strongly evidenced
        # visible replies, alongside failsafe engagements and repairs.  The
        # ledger stays the durable audit; this makes the failsafe rate
        # checkable at a glance without a ledger scan.
        scheduler_view["reliability"] = reliability_snapshot()
        try:
            scheduler_view["reliability_ledger"] = durable_reliability_snapshot(
                settings.database_path
            )
        except Exception as exc:  # health must stay available
            scheduler_view["reliability_ledger"] = {
                "source": "ledger",
                "error": type(exc).__name__,
            }
        try:
            scheduler_view["storage"] = ledger_storage_snapshot(settings.database_path)
        except Exception as exc:  # health must stay available
            scheduler_view["storage"] = {"status": "error", "error": type(exc).__name__}
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
                {
                    "error": "media observation surface is disabled until an operator token is configured"
                },
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

    # One model-backed background unit can fan out into a Life/NPC/Memory
    # settlement and therefore take seconds or minutes.  Starting with the
    # historical budget of eight units made a restart spend an unbounded
    # foreground-looking burst on backlog recovery: /health remained in
    # ``starting`` and the user-visible lane competed with stale cognition.
    # Durable claims already provide fairness across wakes, so one unit per
    # pass is the safer production quantum.  Action recovery remains at its
    # normal bounded budget and inbound turns can preempt this lane.
    background_units_per_pass = 1
    while True:
        started_at = datetime.now(UTC)
        started = time.monotonic()
        diagnostics.passes_started += 1
        diagnostics.last_started_at = started_at
        try:
            await host.scheduler_once(
                observed_at=datetime.now(UTC),
                max_action_units=8,
                max_background_units=background_units_per_pass,
            )
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
