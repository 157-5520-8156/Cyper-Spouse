"""Durable media provider transport around the legacy image machine.

The World v2 media lane needs one provider adapter that satisfies two
contracts at once:

- :class:`~.platform_action_executor.MediaProviderTransport` — dispatch one
  authorized ``media_render`` / ``media_repair`` / ``media_inspection``
  Action and answer recovery ``lookup`` calls with the exact original
  receipt;
- :class:`~.media_provider_results.MediaProviderResultTransport` — return
  the idempotency-keyed opaque result bytes after a restart, so
  ``MediaExecutionWorker`` can materialize artifacts and inspections without
  a second paid provider call.

The in-process ``EventMediaExecutionAdapter`` deliberately cannot be that
transport: its render→inspection cache dies with the process.  This adapter
persists every terminal receipt and result in the same SQLite file as the
world ledger before the receipt is returned, so a crash between dispatch and
settlement recovers to the identical bytes instead of re-rendering.

The legacy ``MediaRenderer.render`` performs generation, visual acceptance
and at most one internal targeted repair in one call, and only returns a
``RenderedMedia`` whose inspection passed.  The transport therefore stores
that passed inspection alongside the artifact, keyed by the artifact sidecar
ref; the later ``media_inspection`` Action replays it as its own durable
provider result.  A render failure becomes a terminal ``failed`` receipt and
the preview lane fails closed downstream.
"""

from __future__ import annotations

from datetime import datetime, UTC
import json
import logging
import sqlite3
from threading import RLock
from typing import Callable

from .media_provider_results import (
    MediaProviderArtifactResult,
    MediaProviderExecutionResult,
    MediaProviderInspectionResult,
    media_provider_result_hash,
)
from .media_v2 import media_digest, media_payload_hash
from .platform_action_executor import (
    MediaProviderDispatchRequest,
    PlatformDispatchReceipt,
)
from .sqlite_coordination import configure_shared_sqlite_connection, sqlite_write_lock


_LOG = logging.getLogger(__name__)

_PLAN_CONTENT_TYPE = "application/vnd.world-v2.media-plan+json"
_ARTIFACT_CONTENT_TYPE = "application/vnd.world-v2.media-artifact+json"
_INSPECTION_CONTENT_TYPE = "application/vnd.world-v2.media-inspection+json"


class SQLiteDurableMediaProviderTransport:
    """Effect-once render/inspection provider bound to durable SQLite rows.

    One idempotency key maps to at most one terminal receipt and result.
    Re-dispatch and recovery lookups return the stored bytes; they never call
    the image provider again for the same frozen request.
    """

    provider = "provider:event-media"

    def __init__(
        self,
        *,
        path: str,
        world_id: str,
        renderer,  # companion_daemon.event_media.MediaRenderer (structural)
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not path or not world_id:
            raise ValueError("durable media provider transport needs path and world id")
        if renderer is None:
            raise ValueError("durable media provider transport needs the image renderer")
        self._world_id = world_id
        self._renderer = renderer
        self._now = now or (lambda: datetime.now(UTC))
        self._lock = RLock()
        self._database_write_lock = sqlite_write_lock(path)
        # Autocommit like every other sidecar on this file: an implicit open
        # transaction would pin the shared WAL checkpoint.
        self._connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        with self._database_write_lock:
            configure_shared_sqlite_connection(self._connection)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_v2_media_provider_dispatch (
                    world_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    result_type TEXT,
                    result_json TEXT,
                    PRIMARY KEY (world_id, idempotency_key)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS world_v2_media_pending_inspection (
                    world_id TEXT NOT NULL,
                    artifact_payload_ref TEXT NOT NULL,
                    inspection_json TEXT NOT NULL,
                    PRIMARY KEY (world_id, artifact_payload_ref)
                )
                """
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- MediaProviderTransport ------------------------------------------

    async def send(
        self, request: MediaProviderDispatchRequest
    ) -> PlatformDispatchReceipt:
        stored = self._stored_receipt(
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        if stored is not None:
            return stored
        if request.kind in {"media_render", "media_repair"}:
            return await self._render(request)
        if request.kind == "media_inspection":
            return self._inspect(request)
        raise ValueError(
            "durable media provider transport does not dispatch this Action kind"
        )

    async def lookup(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        return self._stored_receipt(
            idempotency_key=idempotency_key, request_fingerprint=request_fingerprint
        )

    # -- MediaProviderResultTransport --------------------------------------

    async def lookup_execution_result(
        self, *, action_id: str, idempotency_key: str, request_fingerprint: str
    ) -> MediaProviderExecutionResult | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT request_fingerprint, result_type, result_json "
                "FROM world_v2_media_provider_dispatch WHERE world_id=? AND idempotency_key=?",
                (self._world_id, idempotency_key),
            ).fetchone()
        if row is None or row[1] is None or row[2] is None:
            return None
        if row[0] != request_fingerprint:
            raise ValueError("durable media result fingerprint conflicts with dispatched request")
        model = {
            "MediaProviderArtifactResult": MediaProviderArtifactResult,
            "MediaProviderInspectionResult": MediaProviderInspectionResult,
        }.get(str(row[1]))
        if model is None:
            raise ValueError("durable media result row has an unknown result type")
        result = model.model_validate_json(str(row[2]))
        if result.action_id != action_id or result.idempotency_key != idempotency_key:
            raise ValueError("durable media result does not bind the recovering Action")
        return result

    # -- render / repair ----------------------------------------------------

    async def _render(
        self, request: MediaProviderDispatchRequest
    ) -> PlatformDispatchReceipt:
        from companion_daemon.event_media import (
            MediaPlan as LegacyMediaPlan,
            MediaRenderFailure,
        )

        if request.content_type == _INSPECTION_CONTENT_TYPE and request.kind == "media_repair":
            # The repair Action's authorized payload is the failed inspection
            # sidecar.  The current renderer already spends its single bounded
            # repair inside ``render``, so a second external repair pass would
            # exceed the one-repair contract.  Fail closed instead.
            return self._persist_failure(
                request, error_class="repair_route_not_composed"
            )
        if request.content_type != _PLAN_CONTENT_TYPE:
            return self._persist_failure(request, error_class="unexpected_render_payload")
        try:
            plan = LegacyMediaPlan.from_payload(json.loads(request.body))
        except Exception:
            return self._persist_failure(request, error_class="invalid_frozen_plan")
        try:
            rendered = await self._renderer.render(plan)
        except Exception as exc:  # provider/transport faults become terminal evidence
            _LOG.warning(
                "media render dispatch failed action=%s error=%s",
                request.action_id,
                type(exc).__name__,
            )
            return self._persist_failure(
                request, error_class=f"render_exception:{type(exc).__name__}"[:120]
            )
        if isinstance(rendered, MediaRenderFailure):
            return self._persist_failure(
                request, error_class=str(rendered.reason)[:120] or "render_failed"
            )
        import base64

        image_bytes = rendered.path.read_bytes()
        artifact_body = json.dumps(
            {
                "encoding": "base64",
                "artifact_hash": rendered.artifact_hash,
                "bytes": base64.b64encode(image_bytes).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        artifact_ref = "sidecar:media-artifact:" + media_digest(
            {"request": request.idempotency_key, "hash": rendered.artifact_hash}
        )
        result = MediaProviderArtifactResult(
            action_id=request.action_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
            artifact_payload_ref=artifact_ref,
            artifact_payload_hash=media_payload_hash(artifact_body),
            artifact_content_type=_ARTIFACT_CONTENT_TYPE,
            artifact_body=artifact_body,
        )
        inspection_payload = rendered.inspection.to_payload()
        receipt = PlatformDispatchReceipt(
            provider_receipt_id="receipt:event-media:" + media_digest(
                {"key": request.idempotency_key, "artifact": artifact_ref}
            ),
            provider_ref="event-media:render:" + rendered.artifact_hash[:32],
            status="delivered",
            artifact_refs=(artifact_ref,),
            cost_actual=0,
            received_at=self._now(),
            raw_payload_hash=media_provider_result_hash(result),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._persist(
            request=request,
            receipt=receipt,
            result=result,
            pending_inspection=(artifact_ref, inspection_payload),
        )
        return receipt

    # -- inspection ---------------------------------------------------------

    def _inspect(self, request: MediaProviderDispatchRequest) -> PlatformDispatchReceipt:
        if request.content_type != _ARTIFACT_CONTENT_TYPE:
            return self._persist_failure(request, error_class="unexpected_inspection_payload")
        with self._lock:
            row = self._connection.execute(
                "SELECT inspection_json FROM world_v2_media_pending_inspection "
                "WHERE world_id=? AND artifact_payload_ref=?",
                (self._world_id, request.payload_ref),
            ).fetchone()
        if row is None:
            # The paired render never persisted an acceptance record for this
            # artifact (foreign artifact or partial write).  Never invent a
            # visual verdict: fail this Action and let the preview fail closed.
            return self._persist_failure(request, error_class="inspection_record_unavailable")
        payload = json.loads(str(row[0]))
        inspection_body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        passed = bool(payload.get("passed"))
        reason = str(payload.get("reason") or ("accepted" if passed else "inspection_failed"))
        observed = payload.get("observed_summary")
        result = MediaProviderInspectionResult(
            action_id=request.action_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
            passed=passed,
            reason_code=reason[:256],
            observed_summary=str(observed)[:4_000] if isinstance(observed, str) else None,
            inspection_payload_ref="sidecar:media-inspection:" + media_digest(
                {"request": request.idempotency_key, "artifact": request.payload_hash}
            ),
            inspection_payload_hash=media_payload_hash(inspection_body),
            inspection_content_type=_INSPECTION_CONTENT_TYPE,
            inspection_body=inspection_body,
            # The legacy renderer has already spent its one bounded repair
            # before returning an artifact, so a failed replayed inspection is
            # terminal rather than opening a second repair.
            repairable=False,
            repair_scope=(),
        )
        receipt = PlatformDispatchReceipt(
            provider_receipt_id="receipt:event-media:" + media_digest(
                {"key": request.idempotency_key, "inspection": request.payload_ref}
            ),
            provider_ref="event-media:inspection:" + request.payload_hash[7:39],
            status="delivered",
            artifact_refs=(),
            cost_actual=0,
            received_at=self._now(),
            raw_payload_hash=media_provider_result_hash(result),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._persist(request=request, receipt=receipt, result=result)
        return receipt

    # -- durable rows ---------------------------------------------------------

    def _persist_failure(
        self, request: MediaProviderDispatchRequest, *, error_class: str
    ) -> PlatformDispatchReceipt:
        identity = media_digest(
            {"key": request.idempotency_key, "error": error_class, "fp": request.fingerprint}
        )
        receipt = PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:event-media:failed:{identity}",
            provider_ref=f"event-media:failed:{identity}",
            status="failed",
            error_class=error_class,
            received_at=self._now(),
            raw_payload_hash="sha256:" + media_digest({"failure": identity}),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )
        self._persist(request=request, receipt=receipt, result=None)
        return receipt

    def _persist(
        self,
        *,
        request: MediaProviderDispatchRequest,
        receipt: PlatformDispatchReceipt,
        result: MediaProviderExecutionResult | None,
        pending_inspection: tuple[str, dict[str, object]] | None = None,
    ) -> None:
        with self._database_write_lock, self._lock:
            existing = self._connection.execute(
                "SELECT receipt_json FROM world_v2_media_provider_dispatch "
                "WHERE world_id=? AND idempotency_key=?",
                (self._world_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                # A concurrent dispatcher won the durable insert; keep its bytes.
                return
            self._connection.execute(
                "INSERT INTO world_v2_media_provider_dispatch VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._world_id,
                    request.idempotency_key,
                    request.fingerprint,
                    receipt.model_dump_json(),
                    type(result).__name__ if result is not None else None,
                    result.model_dump_json() if result is not None else None,
                ),
            )
            if pending_inspection is not None:
                self._connection.execute(
                    "INSERT OR IGNORE INTO world_v2_media_pending_inspection VALUES (?, ?, ?)",
                    (
                        self._world_id,
                        pending_inspection[0],
                        json.dumps(
                            pending_inspection[1],
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            self._connection.commit()

    def _stored_receipt(
        self, *, idempotency_key: str, request_fingerprint: str
    ) -> PlatformDispatchReceipt | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT request_fingerprint, receipt_json "
                "FROM world_v2_media_provider_dispatch WHERE world_id=? AND idempotency_key=?",
                (self._world_id, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != request_fingerprint:
            raise ValueError(
                "media provider idempotency key is already bound to a different request"
            )
        return PlatformDispatchReceipt.model_validate_json(str(row[1]))


__all__ = ["SQLiteDurableMediaProviderTransport"]
