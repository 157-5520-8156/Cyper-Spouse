"""Generic expectation → feeling: the appraisal lanes see what she hoped for.

The expression contract already freezes a model-declared response
expectation ("I hope they come back and tell me how it went").  These tests
cover the deterministic resolver that reads it back from committed
projection state, and the two feeling mounts: the silence appraisal (being
left waiting) and the interaction appraisal (did this message land on what
she was waiting for).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
import hashlib
import json

import pytest

from companion_daemon.world_v2.chat_model_deliberation_adapter import (
    ChatModelDeliberationAdapter,
)
from companion_daemon.world_v2.deliberation import ModelInput, ModelOutput, ModelRoute, RouteRequest
from companion_daemon.world_v2.platform_action_executor import PlatformDispatchReceipt
from companion_daemon.world_v2.production_turn_application import (
    WorldV2TurnApplicationConfig,
    build_sqlite_world_v2_turn_application,
)
from companion_daemon.world_v2.proposal_envelope import DecisionProposal
from companion_daemon.world_v2.response_expectation_view import (
    pending_response_expectation,
    response_expectation_advisory,
)


NOW = datetime(2026, 7, 18, 21, 0, tzinfo=UTC)
HOPED = "对方忙完后回来继续聊天"


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- deterministic resolver ---------------------------------------------------


def _fake_projection(
    *,
    logical_time: datetime,
    expectation=None,
    receipt_state: str = "delivered",
    with_receipt: bool = True,
    receipt_revision: int = 5,
):
    action = SimpleNamespace(action_id="action:invite")
    manifest = SimpleNamespace(
        plan_id="plan:invite",
        acceptance_event_ref="event:acceptance:invite",
        recorded_at_world_revision=receipt_revision - 1,
        response_expectation=expectation,
        beats=(SimpleNamespace(beat_id="beat:invite", action=action),),
    )
    receipt_ref = SimpleNamespace(
        event_id="event:receipt:invite",
        event_type="ExecutionReceiptRecorded",
        world_revision=receipt_revision,
        logical_time=NOW,
    )
    receipt = SimpleNamespace(action_id="action:invite", observed_state=receipt_state)
    return SimpleNamespace(
        logical_time=logical_time,
        committed_world_event_refs=(receipt_ref,) if with_receipt else (),
        execution_receipts=(receipt,) if with_receipt else (),
        expression_plan_manifests=(manifest,),
    )


def _expectation(*, expires_at: datetime, pressure_bp: int = 2_000, importance_bp: int = 6_000):
    return SimpleNamespace(
        source_plan_id="plan:invite",
        source_beat_id="beat:invite",
        hoped_response=HOPED,
        pressure_bp=pressure_bp,
        importance_bp=importance_bp,
        not_before=NOW + timedelta(seconds=60),
        expires_at=expires_at,
    )


def test_resolver_binds_anchor_receipt_through_beat_to_manifest_expectation() -> None:
    projection = _fake_projection(
        logical_time=NOW + timedelta(minutes=10),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
    )

    view = pending_response_expectation(
        projection, anchor_event_ref="event:receipt:invite"
    )

    assert view is not None
    assert view.hoped_response == HOPED
    assert view.pressure == "low"
    assert view.importance == "medium"
    assert view.declared_seconds_ago == 600
    # Model-safe by construction: only semantic values, never authority refs.
    assert set(view.model_dump()) == {
        "hoped_response",
        "pressure",
        "importance",
        "declared_seconds_ago",
    }


def test_resolver_returns_none_when_the_expression_declared_no_expectation() -> None:
    projection = _fake_projection(
        logical_time=NOW + timedelta(minutes=10), expectation=None
    )

    assert (
        pending_response_expectation(projection, anchor_event_ref="event:receipt:invite")
        is None
    )
    assert pending_response_expectation(projection) is None


def test_resolver_never_returns_an_expired_expectation() -> None:
    projection = _fake_projection(
        logical_time=NOW + timedelta(hours=2),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
    )

    assert (
        pending_response_expectation(projection, anchor_event_ref="event:receipt:invite")
        is None
    )
    assert pending_response_expectation(projection) is None


def test_resolver_without_anchor_requires_a_delivered_invitation() -> None:
    delivered = _fake_projection(
        logical_time=NOW + timedelta(minutes=10),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
    )
    undelivered = _fake_projection(
        logical_time=NOW + timedelta(minutes=10),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
        with_receipt=False,
    )

    view = pending_response_expectation(delivered)
    assert view is not None and view.hoped_response == HOPED
    assert pending_response_expectation(undelivered) is None


def test_resolver_revision_bound_hides_expectations_declared_after_the_message() -> None:
    projection = _fake_projection(
        logical_time=NOW + timedelta(minutes=10),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
        receipt_revision=5,
    )

    # An inbound message committed at revision 5 or earlier predates the
    # delivery of the invitation, so the hope cannot explain it.
    assert pending_response_expectation(projection, before_world_revision=5) is None
    later = pending_response_expectation(projection, before_world_revision=6)
    assert later is not None and later.hoped_response == HOPED


def test_advisory_carries_the_semantic_summary_without_expectation_authority() -> None:
    projection = _fake_projection(
        logical_time=NOW + timedelta(minutes=10),
        expectation=_expectation(expires_at=NOW + timedelta(hours=1)),
    )
    view = pending_response_expectation(projection, anchor_event_ref="event:receipt:invite")
    assert view is not None

    advisory = response_expectation_advisory(
        view, source_ref="event:receipt:invite", logical_time=NOW + timedelta(minutes=10)
    )

    assert advisory.kind == "response_expectation"
    assert HOPED in advisory.candidates[0].value
    assert "pressure low" in advisory.candidates[0].value
    assert len(advisory.candidates[0].value) <= 256


# --- production wiring --------------------------------------------------------


class _Router:
    async def route(self, _request: RouteRequest) -> ModelRoute:
        return ModelRoute(tier="flash", reason_code="expectation-test", router_version="test.1")


class _Identities:
    def resolve(self, *, platform: str, platform_user_id: str) -> tuple[str, str]:
        assert platform == "http" and platform_user_id == "user.1"
        return "user:primary", "user:primary"


class _DeliveredTransport:
    provider = "platform:test"

    def __init__(self) -> None:
        self.bodies: list[str] = []

    async def send(self, request):  # type: ignore[no-untyped-def]
        self.bodies.append(request.body)
        return PlatformDispatchReceipt(
            provider_receipt_id=f"receipt:expectation:{len(self.bodies)}",
            provider_ref=f"message:expectation:{len(self.bodies)}",
            status="delivered",
            received_at=NOW + timedelta(seconds=1),
            raw_payload_hash="sha256:" + "b" * 64,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
        )

    async def lookup(self, **_kwargs):  # type: ignore[no-untyped-def]
        return None


class _ExpectingChat:
    """Every visible reply invites a response with a frozen expectation."""

    model = "test-expecting-chat"

    async def complete(self, _messages, *, temperature: float = 0.8):  # type: ignore[no-untyped-def]
        del temperature
        return json.dumps(
            {
                "timing_choice": "now",
                "beats": [{"modality": "text", "text": "你忙完跟我说一声呀。"}],
                "stance": "answer_without_world_claims",
                "brief_rationale": "自然地邀请对方回来继续聊",
                "confidence": 8_000,
                "response_expectation": {
                    "hoped_response": HOPED,
                    "pressure_bp": 2_000,
                    "importance_bp": 6_000,
                    "wait_seconds": 60,
                    "expires_after_seconds": 3_600,
                },
            },
            ensure_ascii=False,
        )


class _CapturingAppraisalModel:
    """No-change appraiser that records every ModelInput it was shown."""

    def __init__(self) -> None:
        self.requests: list[ModelInput] = []

    async def propose(self, request: ModelInput) -> ModelOutput:
        self.requests.append(request)
        proposal = DecisionProposal(
            proposal_id="proposal:expectation-test:" + _digest(request.trigger_ref)[:16],
            trigger_ref=request.trigger_ref,
            evaluated_world_revision=request.evaluated_world_revision,
            evidence_refs=(),
            proposed_changes=(),
            action_intents=(),
            confidence=4_000,
            brief_rationale="Nothing worth keeping from this pass.",
            affect_decision="no_change",
            behavior_tendency="observe",
            stance="wait",
            display_strategy="withhold",
        )
        return ModelOutput(
            model_id="test-capturing-appraiser",
            model_version="v1",
            raw_proposal=proposal.model_dump(mode="json"),
        )

    async def recover(self, request: ModelInput, _failure: str) -> ModelOutput:
        return await self.propose(request)

    def request_for(self, trigger_ref: str) -> ModelInput:
        return next(item for item in self.requests if item.trigger_ref == trigger_ref)


def _build_app(tmp_path, *, name: str, silence_idle_seconds: int | None):  # type: ignore[no-untyped-def]
    appraiser = _CapturingAppraisalModel()
    transport = _DeliveredTransport()
    chat = ChatModelDeliberationAdapter(model=_ExpectingChat())
    app = build_sqlite_world_v2_turn_application(
        path=tmp_path / f"{name}.sqlite3",
        config=WorldV2TurnApplicationConfig(
            world_id=f"world:{name}",
            companion_actor_ref="actor:companion",
            reply_target="user:primary",
            action_pump_owner="worker:actions",
            silence_appraisal_idle_seconds=silence_idle_seconds,
        ),
        identities=_Identities(),
        router=_Router(),
        main_model=chat,
        quick_recovery=chat,
        transport=transport,
        appraisal_model=appraiser,
        now=NOW,
    )
    return app, appraiser


def _event_refs(app, event_type: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [
        item.event_id
        for item in app._ledger.project().committed_world_event_refs  # noqa: SLF001
        if item.event_type == event_type
    ]


async def _drain_background(app, *, passes: int = 8) -> None:  # type: ignore[no-untyped-def]
    for _ in range(passes):
        await app.drain_background_once()


@pytest.mark.asyncio
async def test_silence_appraisal_material_names_what_she_hoped_for(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app, appraiser = _build_app(
        tmp_path, name="expectation-silence", silence_idle_seconds=90
    )
    try:
        outcome = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:1",
            text="我先去忙一会儿",
            observed_at=NOW,
            trace_id="trace:expectation-silence",
        )
        assert len(outcome.authorized_action_ids) == 1
        assert (await app.drain_actions_once()).status == "settled"
        receipt_ref = _event_refs(app, "ExecutionReceiptRecorded")[-1]
        first_observation_ref = _event_refs(app, "ObservationRecorded")[0]
        await app.tick(
            tick_id="tick:expectation-silence",
            logical_time_from=NOW,
            logical_time_to=NOW + timedelta(seconds=120),
            observed_at=NOW + timedelta(seconds=120),
            trace_id="trace:expectation-silence-tick",
            causation_id="scheduler:test",
            correlation_id="conversation:expectation-silence",
            reason="test_idle",
        )
        await _drain_background(app)

        silence_request = appraiser.request_for(receipt_ref)
        assert HOPED in silence_request.model_content_json
        assert '"response_expectation"' in silence_request.model_content_json
        # The message that started the conversation predates the declared
        # hope, so its own appraisal material must stay expectation-free.
        first_request = appraiser.request_for(first_observation_ref)
        assert HOPED not in first_request.model_content_json
        assert "response_expectation" not in first_request.model_content_json
    finally:
        app.close()


@pytest.mark.asyncio
async def test_interaction_appraisal_material_names_the_pending_expectation(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app, appraiser = _build_app(
        tmp_path, name="expectation-interaction", silence_idle_seconds=None
    )
    try:
        outcome = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:1",
            text="我先去忙一会儿",
            observed_at=NOW,
            trace_id="trace:expectation-interaction-1",
        )
        assert len(outcome.authorized_action_ids) == 1
        assert (await app.drain_actions_once()).status == "settled"
        answer = await app.inbound(
            platform="http",
            platform_user_id="user.1",
            platform_message_id="message:2",
            text="忙完啦，来继续聊。",
            observed_at=NOW + timedelta(seconds=120),
            trace_id="trace:expectation-interaction-2",
        )
        assert len(answer.authorized_action_ids) == 1
        assert (await app.drain_actions_once()).status == "settled"
        await _drain_background(app)

        observation_refs = _event_refs(app, "ObservationRecorded")
        assert len(observation_refs) >= 2
        # The answer arrived while her declared expectation was still open.
        second_request = appraiser.request_for(observation_refs[1])
        assert HOPED in second_request.model_content_json
        assert '"response_expectation"' in second_request.model_content_json
        # The first message predates the hope: no expectation field at all.
        first_request = appraiser.request_for(observation_refs[0])
        assert HOPED not in first_request.model_content_json
        assert "response_expectation" not in first_request.model_content_json
    finally:
        app.close()
