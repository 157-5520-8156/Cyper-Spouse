"""Read-only observation surface for the media lane.

Delivery is decided by the world itself: bounded candidate selection,
Acceptance (grant/budget/relationship gating) and the composed auto-delivery
guardrails.  A human never approves or vetoes an image here.  This service
only answers "what did she generate and send?" — it lists previews with their
inspection summary and delivery state, and materializes the PNG bytes under
``output/media-preview/`` so a person can look at them.

It holds no application reference and cannot write any world event.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path


_LOG = logging.getLogger(__name__)


class MediaPreviewOperatorService:
    """List generated/delivered media and materialize preview images."""

    def __init__(
        self,
        *,
        ledger,
        sidecar,
        preview_dir: Path = Path("output/media-preview"),
    ) -> None:
        self._ledger = ledger
        self._sidecar = sidecar
        self._preview_dir = preview_dir

    def queue(self, *, materialize: bool = True) -> tuple[dict[str, object], ...]:
        projection = self._ledger.project()
        plans = {item.plan_id: item for item in projection.media_plans}
        inspections = {item.inspection_id: item for item in projection.media_inspections}
        artifacts = {item.artifact_id: item for item in projection.media_artifacts}
        approvals_by_id: dict[str, list] = {}
        for approval in projection.media_delivery_approvals:
            approvals_by_id.setdefault(approval.approval_id, []).append(approval)
        delivered_plan_ids = {item.plan_id for item in projection.media_deliveries}
        rows: list[dict[str, object]] = []
        for preview in projection.media_previews:
            plan = plans.get(preview.plan_id)
            inspection = inspections.get(preview.inspection_id)
            artifact = artifacts.get(preview.artifact_id)
            if plan is None or inspection is None or artifact is None:
                continue
            revisions = approvals_by_id.get(f"approval:media:{preview.preview_id}", [])
            latest = max(revisions, key=lambda item: item.entity_revision, default=None)
            delivered = preview.plan_id in delivered_plan_ids
            image_path = (
                self._materialize(preview_id=preview.preview_id, artifact=artifact)
                if materialize
                else None
            )
            rows.append(
                {
                    "preview_id": preview.preview_id,
                    "plan_id": preview.plan_id,
                    "family": plan.family,
                    "media_lane": plan.media_lane,
                    "frozen_at": plan.frozen_at.isoformat(),
                    "recipient_ref": preview.recipient_ref,
                    "observed_summary": inspection.observed_summary,
                    "inspection_reason": inspection.reason_code,
                    "artifact_id": artifact.artifact_id,
                    "artifact_hash": artifact.artifact_hash,
                    "image_path": str(image_path) if image_path is not None else None,
                    "delivery_decided_by": (
                        latest.operator_ref if latest is not None else None
                    ),
                    "approval_revision": latest.entity_revision if latest is not None else None,
                    "approval_expires_at": (
                        latest.expires_at.isoformat() if latest is not None else None
                    ),
                    "delivered": delivered,
                    "awaiting_world_delivery": not delivered and latest is None,
                }
            )
        rows.sort(key=lambda item: str(item["frozen_at"]), reverse=True)
        return tuple(rows)

    def _materialize(self, *, preview_id: str, artifact) -> Path | None:
        record = self._sidecar.read_exact(payload_ref=artifact.artifact_ref)
        if record is None or record.payload_hash != artifact.artifact_hash:
            return None
        try:
            body = json.loads(record.body)
            image = base64.b64decode(str(body.get("bytes") or ""), validate=True)
        except (ValueError, json.JSONDecodeError):
            return None
        if not image:
            return None
        self._preview_dir.mkdir(parents=True, exist_ok=True)
        path = self._preview_dir / (preview_id.replace(":", "-") + ".png")
        if not path.exists():
            path.write_bytes(image)
        return path


__all__ = ["MediaPreviewOperatorService"]
