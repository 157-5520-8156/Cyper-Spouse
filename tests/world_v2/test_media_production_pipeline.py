"""Production media pipeline: real grants, durable transport, world-owned delivery.

Unlike the orchestration tests that patch ``require_provider_media_grant``,
this fixture provisions the actual enforcement chain with the test deployment
root, then drives candidate → selection → Acceptance → planning → render →
inspection → preview → world-owned delivery → QQ image dispatch through only
the production seams.  No human approves anything: the send decision is the
accepted media selection plus the composed guardrails.  The image provider
and planner model are doubles; every grant, budget, reducer and receipt check
is real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

import pytest

from companion_daemon import event_media
from companion_daemon.world_v2.event_ecology_media import EcologyPolicy
from companion_daemon.world_v2.activity_plan_runtime import (
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.image_evidence_contract import ImageEvidenceV1
from companion_daemon.world_v2.image_evidence_runtime import (
    ImageEvidenceDeclarationCommand,
)
from companion_daemon.world_v2.media_authority_provisioning import (
    MEDIA_INSPECTION_GRANT_ID,
    MEDIA_PLANNING_GRANT_ID,
    MEDIA_RENDER_GRANT_ID,
    MediaAuthorityProvisioner,
)
from companion_daemon.world_v2.media_auto_delivery import MediaAutoDeliveryComposition
from companion_daemon.world_v2.media_provider_transport import (
    SQLiteDurableMediaProviderTransport,
)
from companion_daemon.world_v2.media_v2 import (
    MediaPlan,
    MediaPlanningResult,
    StoredMediaPayload,
    media_payload_hash,
)
from companion_daemon.world_v2.production_turn_application import (
    MediaContinuationComposition,
    MediaSelectionAcceptanceComposition,
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_c2c_host import QQC2CPlatformTransport, qq_c2c_target
from companion_daemon.world_v2.schemas import ProviderMediaGrantBinding
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger
from companion_daemon.world_v2.world_turn_runtime import InboundTurn


NOW = datetime(2026, 7, 20, 4, 0, tzinfo=UTC)
WORLD_ID = "world:media-production-pipeline"
RECIPIENT = "10001"


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", qq_c2c_target(RECIPIENT))


class _NoModel:
    async def deliberate(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("pipeline test does not deliberate visible replies")


class _Router:
    async def route(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("pipeline test does not route")


class _SelectionModel:
    model = "test-media-selection"

    async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
        capsule = json.loads(messages[-1]["content"])
        return json.dumps({"decision": "select", "token": capsule["candidates"][0]["token"]})


class _PlanningDouble:
    """Return one frozen MediaPlan whose sidecar body the fake renderer accepts."""

    def __init__(self) -> None:
        self.calls = 0

    async def lookup(self, *, planning_request_id: str):  # type: ignore[no-untyped-def]
        return None

    async def plan(self, *, opportunity, planning_request_id: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        body = json.dumps({"plan_id": "plan:pipeline", "version": "test-double"})
        payload = StoredMediaPayload(
            payload_ref="sidecar:media-plan:pipeline",
            payload_hash=media_payload_hash(body),
            content_type="application/vnd.world-v2.media-plan+json",
            body=body,
        )
        return MediaPlanningResult(
            plan=MediaPlan(
                plan_id="plan:pipeline",
                planning_request_id=planning_request_id,
                opportunity_id=opportunity.opportunity_id,
                event_snapshot_hash=opportunity.event_snapshot_hash,
                family=opportunity.family,
                planner_version="test-double.1",
                schema_version="test-double.1",
                media_lane=opportunity.media_lane,
                plan_payload_ref=payload.payload_ref,
                plan_payload_hash=payload.payload_hash,
                frozen_at=NOW,
            ),
            plan_payload=payload,
        )


class _Renderer:
    def __init__(self, image: Path) -> None:
        self.image = image
        self.calls = 0

    async def render(self, plan):  # type: ignore[no-untyped-def]
        self.calls += 1
        return event_media.RenderedMedia(
            plan_id="plan:pipeline",
            path=self.image,
            artifact_hash=hashlib.sha256(self.image.read_bytes()).hexdigest(),
            prompt="frozen prompt",
            attempts=1,
            inspection=event_media.MediaInspection(
                passed=True,
                reason="accepted",
                observed_summary="傍晚公园小径的一张随手拍",
                observed_facts=("park",),
                deviations=(),
                inspector_model="test-inspector",
            ),
        )


class _Delivery:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.images: list[tuple[str, bytes]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.texts.append((recipient_id, text))
        return {"status": "ok", "data": {"message_id": f"qq-text-{len(self.texts)}"}}

    async def send_reaction(self, recipient_id: str, *, message_id: str, reaction_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError("pipeline test sends no reaction")

    async def send_sticker(self, recipient_id: str, *, sticker_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError("pipeline test sends no sticker")

    async def send_typing(self, recipient_id: str, *, state: str) -> dict[str, object]:
        return {"status": "ok"}

    async def send_image_message(self, recipient_id: str, *, image_path: Path) -> dict[str, object]:
        self.images.append((recipient_id, image_path.read_bytes()))
        return {"status": "ok", "data": {"message_id": f"qq-image-{len(self.images)}"}}


def _config() -> WorldV2TurnApplicationConfig:
    return WorldV2TurnApplicationConfig(
        world_id=WORLD_ID,
        companion_actor_ref="agent:companion",
        reply_target=qq_c2c_target(RECIPIENT),
        action_pump_owner="pump:media-production-pipeline",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=MediaSelectionAcceptanceComposition(
            grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_PLANNING_GRANT_ID, grant_revision=1
            ),
            account_id="account:world-v2:media-selection",
            account_window_id="window:world-v2:media-selection",
            account_limit=10_000,
            amount_limit=1,
        ),
        media_continuation=MediaContinuationComposition(
            render_grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_RENDER_GRANT_ID, grant_revision=1
            ),
            render_account_id="account:world-v2:media-render",
            render_window_id="window:world-v2:media-render",
            render_account_limit=10_000,
            render_amount_limit=0,
            inspection_grant=ProviderMediaGrantBinding(
                grant_id=MEDIA_INSPECTION_GRANT_ID, grant_revision=1
            ),
            inspection_account_id="account:world-v2:media-inspection",
            inspection_window_id="window:world-v2:media-inspection",
            inspection_account_limit=10_000,
            inspection_amount_limit=0,
        ),
        media_auto_delivery=MediaAutoDeliveryComposition(
            delivery_target_ref=qq_c2c_target(RECIPIENT),
            recipient_ref=f"user:{RECIPIENT}",
            account_id="account:world-v2:media-selection",
            amount_limit=0,
            max_deliveries_per_day=1,
        ),
    )


@pytest.mark.asyncio
async def test_full_media_pipeline_delivers_through_world_owned_policy_without_an_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")
    monkeypatch.setattr(
        event_media.MediaPlan,
        "from_payload",
        staticmethod(lambda payload: object()),
    )
    path = tmp_path / "media-production-pipeline.sqlite"
    image = tmp_path / "render.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\npipeline-image-bytes")
    delivery = _Delivery()
    planner = _PlanningDouble()
    renderer = _Renderer(image)

    def build():  # type: ignore[no-untyped-def]
        transport = SQLiteDurableMediaProviderTransport(
            path=str(path), world_id=WORLD_ID, renderer=renderer
        )
        app = build_sqlite_world_v2_turn_application(
            path=path,
            config=_config(),
            identities=_Identities(),
            router=_Router(),
            main_model=_NoModel(),
            quick_recovery=_NoModel(),
            transport=QQC2CPlatformTransport(
                delivery=delivery,
                recipients_by_target={qq_c2c_target(RECIPIENT): RECIPIENT},
                now=lambda: NOW,
            ),
            media_transport=transport,
            media_planner=planner,
            media_selection_model=_SelectionModel(),
            now=NOW,
        )
        return app, transport

    app, transport = build()
    try:
        await app.tick(
            tick_id="pipeline:clock",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:pipeline:clock",
            causation_id="scheduler:pipeline",
            correlation_id="correlation:pipeline",
            reason="test",
        )
    finally:
        app.close()
        transport.close()

    # Enforcement authority is provisioned exactly like production: a signed
    # chain in the same SQLite world, not a monkeypatched verifier.
    ledger = SQLiteWorldLedger(path=path, world_id=WORLD_ID)
    try:
        MediaAuthorityProvisioner(
            ledger=ledger,
            signing_key_hex="11" * 32,
            subject_ref=f"user:{RECIPIENT}",
        ).ensure()
    finally:
        ledger.close()

    app, transport = build()
    try:
        logical_time = await app.current_logical_time()
        assert logical_time is not None
        await app.respond(InboundTurn(
            platform="qq", platform_user_id=RECIPIENT,
            platform_message_id="message:pipeline",
            text="傍晚我想去公园走走。", observed_at=logical_time,
            trace_id="trace:pipeline:inbound",
        ))
        plan_commit = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:pipeline:plan", world_id=WORLD_ID,
                source_observation_id=f"observation:qq:{RECIPIENT}:message:pipeline",
                plan_id="plan:pipeline-activity", activity_id="activity:pipeline",
                activity_kind="walk", importance_bp=4_000, location_ref="location:park",
                participant_refs=("agent:companion",), privacy_class="shareable",
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:pipeline:plan", causation_id="cause:pipeline:plan",
            correlation_id="correlation:pipeline",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:pipeline:start", world_id=WORLD_ID,
                source_observation_id=f"observation:qq:{RECIPIENT}:message:pipeline",
                plan_id="plan:pipeline-activity", operation="start",
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:pipeline:start", causation_id=plan_commit.event_ids[-1],
            correlation_id="correlation:pipeline",
        )
        declaration = await app.declare_image_evidence(
            ImageEvidenceDeclarationCommand(
                command_id="command:pipeline:evidence",
                source_event_ref=started.event_ids[-1],
                image_evidence=ImageEvidenceV1(
                    visibility="shareable",
                    activity={
                        "evidence_visibility": "shareable",
                        "id": "activity:pipeline",
                        "kind": "walk",
                        "description": "傍晚在公园散步",
                        "phase": "active",
                    },
                ),
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:pipeline:evidence", correlation_id="correlation:pipeline",
        )
        ecology = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=logical_time,
            trace_id="trace:pipeline:ecology", correlation_id="correlation:pipeline",
        )
        assert ecology is not None and ecology.status == "created"

        # Candidate → selection → Acceptance → planning through the conductor,
        # with the real reducer-time grant verification in force.
        preview_run = await app.drain_media_preview_once(
            trace_id="trace:pipeline:conductor", correlation_id="correlation:pipeline",
        )
        assert preview_run.status == "planned", preview_run
        assert planner.calls == 1

        # plan → render Action → durable provider dispatch → artifact →
        # inspection Action → durable inspection replay → preview.
        statuses: list[str] = []
        for _ in range(12):
            continuation = await app.drain_media_continuation_once(
                logical_time=logical_time,
                trace_id="trace:pipeline:continuation",
                correlation_id="correlation:pipeline",
            )
            if continuation is not None:
                statuses.append("continuation:" + continuation)
            pumped = await app.drain_actions_once()
            if pumped is not None and getattr(pumped, "status", None) != "idle":
                statuses.append("action:" + str(pumped.status))
            materialized = await app.drain_media_results_once(logical_time=logical_time)
            if materialized is not None:
                statuses.append("result:" + materialized)
            projection = app._ledger.project()  # noqa: SLF001 - test assertion
            if projection.media_previews:
                break
        projection = app._ledger.project()  # noqa: SLF001 - test assertion
        assert renderer.calls == 1, statuses
        assert len(projection.media_artifacts) == 1
        assert len(projection.media_inspections) == 1
        assert projection.media_inspections[0].passed is True
        assert len(projection.media_previews) == 1, statuses
        assert projection.media_deliveries == ()
        assert delivery.images == []

        # The read-only observation surface lists the generated image and
        # materializes its PNG; it has no approval verb.
        observer = app.media_preview_operator(preview_dir=tmp_path / "preview-queue")
        queue = observer.queue()
        assert len(queue) == 1
        row = queue[0]
        assert row["delivered"] is False and row["awaiting_world_delivery"] is True
        assert row["observed_summary"] == "傍晚公园小径的一张随手拍"
        assert row["image_path"] is not None
        assert Path(str(row["image_path"])).read_bytes() == image.read_bytes()
        assert not hasattr(observer, "approve") and not hasattr(observer, "dismiss")

        # World-owned delivery: the composed policy freezes the standard
        # approval under a system authority ref and drives the delivery
        # Action.  No operator is involved anywhere.
        outcome = await app.drain_media_auto_delivery_once(
            trace_id="trace:pipeline:auto-delivery",
            correlation_id="correlation:pipeline",
        )
        assert outcome is not None and outcome.status == "delivered_attempted"
        assert outcome.action_id is not None

        # A second pass continues the same already-made decision (idempotent
        # redrive of the in-flight Action); it must not send a duplicate.
        second = await app.drain_media_auto_delivery_once(
            trace_id="trace:pipeline:auto-delivery-2",
            correlation_id="correlation:pipeline",
        )
        assert second is not None and second.status == "delivered_attempted"
        assert len(delivery.images) == 1
        after = app._ledger.project()  # noqa: SLF001 - test assertion
        observed = app.media_preview_operator(
            preview_dir=tmp_path / "preview-queue"
        ).queue()
    finally:
        app.close()
        transport.close()

    assert len(delivery.images) == 1
    assert delivery.images[0][0] == RECIPIENT
    assert delivery.images[0][1] == image.read_bytes()
    delivery_action = next(
        item for item in after.actions if item.kind == "media_delivery"
    )
    # NapCat's synchronous response proves provider acceptance, not terminal
    # delivery; MediaDeliveryShared may only materialize after a terminal
    # delivered receipt (via the existing verification/recovery lanes).
    assert delivery_action.state == "provider_accepted"
    assert (
        after.media_delivery_approvals[-1].operator_ref
        == "system:world-v2:media-delivery-policy"
    )
    assert observed[0]["delivery_decided_by"] == "system:world-v2:media-delivery-policy"
