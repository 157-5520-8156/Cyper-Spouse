#!/usr/bin/env python
"""End-to-end media preview acceptance on a scratch World v2 ledger.

Drives the full production chain — declared visual evidence → ecology
candidate → bounded selection → Acceptance (real enforcement grants) → v5
planning (real DeepSeek call) → render (real OpenAI image backend, proxy
honoured) → visual inspection → ``MediaPreviewGenerated`` — and then STOPS.
The image stays in the operator approval queue as acceptance evidence; this
script never approves or delivers anything.

Usage (repository root; uses .env credentials):

    WORLD_V2_ENABLE_INSECURE_TEST_ROOT=1 \
    .venv/bin/python scripts/run_world_v2_media_preview_acceptance.py

The scratch world lives under output/media-preview/acceptance-world.sqlite
and is recreated on every run; the production ledger is never touched.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("WORLD_V2_ENABLE_INSECURE_TEST_ROOT", "1")

from companion_daemon.config import Settings  # noqa: E402
from companion_daemon.world_v2.activity_plan_runtime import (  # noqa: E402
    ActivityPlanCommand,
    ActivityPlanTransitionCommand,
)
from companion_daemon.world_v2.event_ecology_media import EcologyPolicy  # noqa: E402
from companion_daemon.world_v2.image_evidence_contract import ImageEvidenceV1  # noqa: E402
from companion_daemon.world_v2.image_evidence_runtime import (  # noqa: E402
    ImageEvidenceDeclarationCommand,
)
from companion_daemon.world_v2.media_authority_provisioning import (  # noqa: E402
    MediaAuthorityProvisioner,
)
from companion_daemon.world_v2.production_turn_application import (  # noqa: E402
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.qq_media_deployment import (  # noqa: E402
    build_qq_media_preview_deployment,
)
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger  # noqa: E402
from companion_daemon.world_v2.world_turn_runtime import InboundTurn  # noqa: E402


WORLD_ID = "world:media-preview-acceptance"
SCRATCH = Path("output/media-preview/acceptance-world.sqlite")
PREVIEW_DIR = Path("output/media-preview")
NOW = datetime.now(UTC)


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        return (f"user:{platform_user_id}", "user:acceptance")


class _NoModel:
    async def deliberate(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("acceptance run does not deliberate visible replies")


class _Router:
    async def route(self, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("acceptance run does not route")


class _NoDeliveryTransport:
    provider = "platform:acceptance-null"

    async def send(self, _request):  # type: ignore[no-untyped-def]
        raise AssertionError("acceptance run must never deliver")

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


def _config(*, media_bundle=None) -> WorldV2TurnApplicationConfig:  # type: ignore[no-untyped-def]
    return WorldV2TurnApplicationConfig(
        world_id=WORLD_ID,
        companion_actor_ref="agent:companion",
        reply_target="user:acceptance",
        action_pump_owner="pump:media-preview-acceptance",
        event_ecology_policy=EcologyPolicy(max_candidates_per_drain=1),
        media_selection_acceptance=(
            media_bundle.deployment.acceptance if media_bundle is not None else None
        ),
        media_continuation=(
            media_bundle.deployment.continuation if media_bundle is not None else None
        ),
    )


def _build(*, settings: Settings, media_bundle=None):  # type: ignore[no-untyped-def]
    return build_sqlite_world_v2_turn_application(
        path=SCRATCH,
        config=_config(media_bundle=media_bundle),
        identities=_Identities(),
        router=_Router(),
        main_model=_NoModel(),
        quick_recovery=_NoModel(),
        transport=_NoDeliveryTransport(),
        media_transport=(media_bundle.transport if media_bundle is not None else None),
        media_planner=(media_bundle.deployment.planner if media_bundle is not None else None),
        media_selection_model=(
            media_bundle.deployment.selection_model if media_bundle is not None else None
        ),
        now=NOW,
    )


async def main() -> int:
    SCRATCH.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(SCRATCH) + suffix)
        if candidate.exists():
            candidate.unlink()

    settings = Settings(
        database_path=SCRATCH,
        WORLD_V2_MEDIA_PREVIEW_ENABLED="1",
    )
    if not settings.deepseek_api_key or not settings.openai_api_key:
        print("DEEPSEEK_API_KEY and OPENAI_API_KEY are required (.env)", file=sys.stderr)
        return 2

    # 1. Establish the world clock, then provision the enforcement chain.
    app = _build(settings=settings)
    try:
        await app.tick(
            tick_id="acceptance:clock",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(minutes=1),
            observed_at=NOW + timedelta(minutes=1),
            trace_id="trace:acceptance:clock",
            causation_id="scheduler:acceptance",
            correlation_id="correlation:acceptance",
            reason="media_preview_acceptance",
        )
    finally:
        app.close()
    signing_key = os.environ.get("WORLD_V2_ROOT_SIGNING_KEY_HEX", "11" * 32)
    ledger = SQLiteWorldLedger(path=SCRATCH, world_id=WORLD_ID)
    try:
        MediaAuthorityProvisioner(
            ledger=ledger, signing_key_hex=signing_key, subject_ref="user:acceptance",
        ).ensure()
    finally:
        ledger.close()

    # 2. Compose the real production media deployment against the scratch world.
    media_bundle = build_qq_media_preview_deployment(settings=settings, world_id=WORLD_ID)
    if media_bundle is None:
        print("media deployment factory reported disabled; see log line above", file=sys.stderr)
        return 3
    if os.environ.get("ACCEPTANCE_DETERMINISTIC_SELECTION") == "1":
        # The bounded selection layer's durability is covered by unit tests;
        # this acceptance run targets planning/render/inspection with real
        # providers.  The flag substitutes only the select-or-decline token
        # choice so provider mood cannot starve the run.  Production keeps
        # the real model.
        from companion_daemon.world_v2.production_turn_application import (
            MediaPreviewDeployment,
        )

        class _AlwaysSelect:
            model = "acceptance-deterministic-selection"

            async def complete(self, messages, *, temperature: float = 0.2):  # type: ignore[no-untyped-def]
                capsule = json.loads(messages[-1]["content"])
                token = capsule["candidates"][0]["token"]
                return json.dumps({"decision": "select", "token": token})

        media_bundle = type(media_bundle)(
            deployment=MediaPreviewDeployment(
                selection_model=_AlwaysSelect(),
                planner=media_bundle.deployment.planner,
                acceptance=media_bundle.deployment.acceptance,
                continuation=media_bundle.deployment.continuation,
            ),
            transport=media_bundle.transport,
        )
        print("selection layer: deterministic acceptance double (flagged)")

    app = _build(settings=settings, media_bundle=media_bundle)
    try:
        logical_time = await app.current_logical_time()
        assert logical_time is not None
        await app.respond(InboundTurn(
            platform="acceptance", platform_user_id="acceptance",
            platform_message_id="message:acceptance",
            text="傍晚我想去公园走走，看看晚霞。", observed_at=logical_time,
            trace_id="trace:acceptance:inbound",
        ))
        plan = await app.plan_activity(
            ActivityPlanCommand(
                command_id="command:acceptance:plan", world_id=WORLD_ID,
                source_observation_id="observation:acceptance:acceptance:message:acceptance",
                plan_id="plan:acceptance-walk", activity_id="activity:acceptance-walk",
                activity_kind="walk", importance_bp=4_000, location_ref="location:park",
                participant_refs=("agent:companion",), privacy_class="shareable",
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:acceptance:plan", causation_id="cause:acceptance:plan",
            correlation_id="correlation:acceptance",
        )
        started = await app.transition_activity(
            ActivityPlanTransitionCommand(
                command_id="command:acceptance:start", world_id=WORLD_ID,
                source_observation_id="observation:acceptance:acceptance:message:acceptance",
                plan_id="plan:acceptance-walk", operation="start",
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:acceptance:start", causation_id=plan.event_ids[-1],
            correlation_id="correlation:acceptance",
        )
        declaration = await app.declare_image_evidence(
            ImageEvidenceDeclarationCommand(
                command_id="command:acceptance:evidence",
                source_event_ref=started.event_ids[-1],
                image_evidence=ImageEvidenceV1(
                    visibility="shareable",
                    summary="傍晚的公园散步，晚霞正好",
                    activity={
                        "evidence_visibility": "shareable",
                        "id": "activity:acceptance-walk",
                        "kind": "walk",
                        "description": "傍晚在公园的小径散步",
                        "phase": "active",
                    },
                    location={
                        "evidence_visibility": "shareable",
                        "id": "location:park",
                        "kind": "park",
                        "publicness": "public",
                    },
                    environment={
                        "evidence_visibility": "shareable",
                        "light": "golden_hour",
                    },
                ),
            ),
            logical_time=logical_time, created_at=logical_time,
            trace_id="trace:acceptance:evidence", correlation_id="correlation:acceptance",
        )
        ecology = await app.drain_media_ecology_once(
            wake_event_ref=declaration.event_ids[-1], logical_time=logical_time,
            trace_id="trace:acceptance:ecology", correlation_id="correlation:acceptance",
        )
        print(f"ecology: {ecology.status} candidates={list(ecology.candidate_ids)}")

        # The production scheduler retries the bounded selection on every
        # pass at a fresh logical time; a model decline is not terminal.
        # Mirror that here with a few tick+conductor rounds.
        conductor = None
        for selection_round in range(6):
            conductor = await app.drain_media_preview_once(
                trace_id="trace:acceptance:conductor", correlation_id="correlation:acceptance",
            )
            print(
                f"conductor[{selection_round}]: {conductor.status} "
                f"reason={conductor.reason_code}"
            )
            if conductor.status in {"planned", "not_renderable", "blocked"}:
                break
            next_time = logical_time + timedelta(minutes=1 + selection_round)
            await app.tick(
                tick_id=f"acceptance:retry:{selection_round}",
                logical_time_from=logical_time,
                logical_time_to=next_time,
                observed_at=next_time,
                trace_id=f"trace:acceptance:retry:{selection_round}",
                causation_id="scheduler:acceptance",
                correlation_id="correlation:acceptance",
                reason="media_preview_acceptance_retry",
            )
            logical_time = next_time
        if conductor is None or conductor.status != "planned":
            print("planning did not produce a renderable plan; nothing rendered")
            return 4

        for round_index in range(12):
            continuation = await app.drain_media_continuation_once(
                logical_time=logical_time,
                trace_id="trace:acceptance:continuation",
                correlation_id="correlation:acceptance",
            )
            if continuation is not None:
                print(f"continuation[{round_index}]: {continuation}")
            pumped = await app.drain_actions_once()
            if pumped is not None and getattr(pumped, "status", None) != "idle":
                print(f"action[{round_index}]: {pumped.status} {pumped.action_id}")
            materialized = await app.drain_media_results_once(logical_time=logical_time)
            if materialized is not None:
                print(f"result[{round_index}]: {materialized}")
            projection = app._ledger.project()  # noqa: SLF001 - acceptance evidence
            if projection.media_previews:
                break

        operator = app.media_preview_operator(preview_dir=PREVIEW_DIR)
        queue = operator.queue()
        print(json.dumps({"pending_previews": list(queue)}, ensure_ascii=False, indent=2))
        if not queue:
            projection = app._ledger.project()  # noqa: SLF001 - acceptance evidence
            failed = [
                (item.event_id, item.event_type)
                for item in projection.committed_world_event_refs
                if item.event_type in {"MediaPreviewFailed", "MediaNotRenderableRecorded"}
            ]
            print(f"no preview generated; terminal media events: {failed}")
            import sqlite3 as _sqlite3

            diagnostics = _sqlite3.connect(SCRATCH)
            try:
                for key, receipt_json in diagnostics.execute(
                    "SELECT idempotency_key, receipt_json FROM world_v2_media_provider_dispatch"
                ):
                    receipt = json.loads(receipt_json)
                    print(
                        f"provider dispatch {key[:44]} -> {receipt['status']} "
                        f"{receipt.get('error_class')}"
                    )
            finally:
                diagnostics.close()
            return 5
        print(
            "acceptance evidence ready; this scratch world composes no delivery "
            "policy, so the image stays undelivered (production's world-owned "
            "policy decides delivery on its own schedule)"
        )
        return 0
    finally:
        app.close()
        media_bundle.transport.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
