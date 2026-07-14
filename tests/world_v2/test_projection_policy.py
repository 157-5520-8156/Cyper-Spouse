from __future__ import annotations

from datetime import UTC, datetime

from companion_daemon.world_v2 import (
    Action,
    BudgetAccount,
    BudgetReservation,
    ProjectionRequest,
    WorldRuntime,
)
from companion_daemon.world_v2.ledger import WorldLedger
from companion_daemon.world_v2.projection import (
    AuthenticatedProjectionPrincipal,
    InternalAuthorityReader,
    InternalProjectionReader,
    ProjectionAuthority,
    ProjectionCapabilityIssuer,
    ProjectionCompiler,
    ProjectionGrant,
    ProjectionLimits,
)
from companion_daemon.world_v2.reducers import ReducerState
from companion_daemon.world_v2.schemas import ProjectionCursor, WorldEvent
from companion_daemon.world_v2.sqlite_ledger import SQLiteWorldLedger


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
WORLD_ID = "world-v2-projection-test"


def event(event_id: str, event_type: str, payload: dict[str, object]) -> WorldEvent:
    return WorldEvent.from_payload(
        schema_version="world-v2.1",
        event_id=event_id,
        world_id=WORLD_ID,
        event_type=event_type,
        logical_time=NOW,
        created_at=NOW,
        actor="system:test",
        source="test",
        trace_id="trace-projection",
        causation_id="cause-projection",
        correlation_id="conversation-projection",
        idempotency_key=event_id,
        payload=payload,
    )


def ledger_with_private_action() -> WorldLedger:
    ledger = WorldLedger.in_memory(world_id=WORLD_ID)
    populate_ledger(ledger)
    return ledger


def populate_ledger(ledger: WorldLedger | SQLiteWorldLedger) -> None:
    account = BudgetAccount(
        account_id="account-chat",
        category="chat",
        window_id="window-private",
        limit=1_000,
    )
    reservation = BudgetReservation(
        reservation_id="reservation-private",
        account_id=account.account_id,
        action_id="action-private",
        category="chat",
        amount_limit=100,
    )
    action = Action(
        schema_version="world-v2.1",
        action_id="action-private",
        world_id=WORLD_ID,
        logical_time=NOW,
        created_at=NOW,
        trace_id="trace-projection",
        causation_id="acceptance-private",
        correlation_id="conversation-private",
        kind="reply",
        layer="external_action",
        intent_ref="intent-private",
        actor="companion:girl",
        target="user:private",
        payload_ref="encrypted:private-reply",
        payload_hash="sha256:private-reply",
        idempotency_key="private-effect-key",
        budget_reservation_id=reservation.reservation_id,
        state="authorized",
        recovery_policy="effect_once",
    )
    ledger.commit(
        [
            event(
                "event-account",
                "BudgetAccountConfigured",
                {"account": account.model_dump(mode="json")},
            ),
            event(
                "event-reservation",
                "BudgetReserved",
                {"reservation": reservation.model_dump(mode="json")},
            ),
            event("event-action", "ActionAuthorized", {"action": action.model_dump(mode="json")}),
            event(
                "event-observation",
                "ObservationRecorded",
                {"observation_id": "observation-private"},
            ),
        ],
        expected_world_revision=0,
        expected_deliberation_revision=0,
    )


def request(
    viewer_kind: str,
    *,
    permissions: frozenset[str] = frozenset(),
    include_debug_refs: bool = False,
) -> ProjectionRequest:
    redaction_policies = {
        "platform_adapter": "platform-v1",
        "dashboard_operator": "operator-default-v1",
        "room_renderer": "room-public-v1",
        "evaluator": "evaluator-redacted-v1",
    }
    return ProjectionRequest(
        schema_version="world-v2.1",
        request_id=f"request:{viewer_kind}",
        world_id=WORLD_ID,
        viewer_kind=viewer_kind,
        viewer_id=f"viewer:{viewer_kind}",
        permissions=permissions,
        trace_id="trace-project",
        include_debug_refs=include_debug_refs,
        redaction_policy=redaction_policies[viewer_kind],
    )


def authority(*grants: ProjectionGrant) -> ProjectionAuthority:
    return ProjectionAuthority(grants=grants)


_CREDENTIALS: dict[str, object] = {}


class StaticPrincipalVerifier:
    def authenticate(self, credential: object) -> AuthenticatedProjectionPrincipal:
        for viewer_id, expected in _CREDENTIALS.items():
            if credential is expected:
                return AuthenticatedProjectionPrincipal(
                    principal_id=viewer_id,
                    world_id=WORLD_ID,
                    authentication_context="test-fixture",
                )
        raise PermissionError("test credential is not authenticated")


def bind(access: ProjectionAuthority, projection_request: ProjectionRequest) -> ProjectionRequest:
    credential = _CREDENTIALS.setdefault(projection_request.viewer_id, object())
    return ProjectionCapabilityIssuer(
        authority=access,
        principal_verifier=StaticPrincipalVerifier(),
    ).bind(projection_request, credential=credential)


def test_projection_grants_reject_cross_viewer_permissions() -> None:
    try:
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="room:misconfigured",
            viewer_kind="room_renderer",
            permissions=frozenset({"projection:actions:status"}),
            redaction_policy="room-public-v1",
        )
    except ValueError as exc:
        assert "invalid for its viewer" in str(exc)
    else:
        raise AssertionError("cross-viewer permission grant must be rejected")


def test_projection_grant_rejects_mutable_scope_inputs() -> None:
    mutable_permissions: set[str] = set()
    try:
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:mutable",
            viewer_kind="room_renderer",
            permissions=mutable_permissions,  # type: ignore[arg-type]
            redaction_policy="room-public-v1",
        )
    except TypeError as exc:
        assert "frozenset" in str(exc)
    else:
        raise AssertionError("mutable grant scope must be rejected")


def test_capability_issuer_uses_authenticated_principal_not_requested_identity() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="operator:trusted",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:diagnostics"}),
            redaction_policy="operator-default-v1",
        )
    )
    attacker_credential = object()

    class AttackerVerifier:
        def authenticate(self, credential: object) -> AuthenticatedProjectionPrincipal:
            assert credential is attacker_credential
            return AuthenticatedProjectionPrincipal(
                principal_id="room:attacker",
                world_id=WORLD_ID,
                authentication_context="signed-session",
            )

    requested_operator = request(
        "dashboard_operator",
        permissions=frozenset({"projection:diagnostics"}),
    ).model_copy(update={"viewer_id": "operator:trusted"})

    try:
        ProjectionCapabilityIssuer(
            authority=access,
            principal_verifier=AttackerVerifier(),
        ).bind(requested_operator, credential=attacker_credential)
    except PermissionError as exc:
        assert "authenticated principal" in str(exc)
    else:
        raise AssertionError("attacker principal must not mint operator capability")


def test_platform_projection_requires_status_permission_and_never_exposes_dispatch_material() -> (
    None
):
    ledger = ledger_with_private_action()
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:platform_adapter",
            viewer_kind="platform_adapter",
            permissions=frozenset({"projection:actions:status"}),
            redaction_policy="platform-v1",
            action_targets=frozenset({"user:private"}),
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        projection_authority=access,
    )

    denied = runtime.project(bind(access, request("platform_adapter")))
    allowed = runtime.project(
        bind(
            access,
            request(
                "platform_adapter",
                permissions=frozenset({"projection:actions:status"}),
            ),
        )
    )

    assert denied.view.action_statuses == ()
    assert len(allowed.view.action_statuses) == 1
    pending = allowed.view.action_statuses[0]
    assert pending.action_id == "action-private"
    assert pending.target == "user:private"
    assert not hasattr(pending, "payload_ref")
    assert not hasattr(pending, "payload_hash")
    assert not hasattr(pending, "idempotency_key")
    assert not hasattr(pending, "intent_ref")
    assert not hasattr(allowed.view, "debug_observation_refs")
    assert allowed.semantic_hash is None
    assert "encrypted:private-reply" not in allowed.model_dump_json()
    assert "private-effect-key" not in allowed.model_dump_json()


def test_platform_projection_is_scoped_to_granted_action_targets() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:platform_adapter",
            viewer_kind="platform_adapter",
            permissions=frozenset({"projection:actions:status"}),
            redaction_policy="platform-v1",
            action_targets=frozenset({"user:someone-else"}),
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=access,
    )

    projection = runtime.project(
        bind(
            access,
            request(
                "platform_adapter",
                permissions=frozenset({"projection:actions:status"}),
            ),
        )
    )

    assert projection.view.action_statuses == ()


def test_dashboard_diagnostics_redact_payload_and_require_debug_permission_for_refs() -> None:
    ledger = ledger_with_private_action()
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset(
                {
                    "projection:actions:diagnostic",
                    "projection:diagnostics",
                    "projection:debug_refs",
                }
            ),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        projection_authority=access,
    )
    permissions = frozenset(
        {"projection:actions:diagnostic", "projection:diagnostics", "projection:debug_refs"}
    )

    projection = runtime.project(
        bind(
            access,
            request(
                "dashboard_operator",
                permissions=permissions,
                include_debug_refs=True,
            ),
        )
    )

    pending = projection.view.pending_actions[0]
    assert pending.action_id == "action-private"
    assert not hasattr(pending, "payload_ref")
    assert not hasattr(pending, "payload_hash")
    assert not hasattr(pending, "idempotency_key")
    assert projection.view.debug_observation_refs == ("observation-private",)
    assert projection.view.system_health.budget_account_count == 1
    assert projection.view.system_health.reserved_budget == 100
    assert projection.view.system_health.deliberation_revision == 0


def test_debug_reference_flag_without_requested_permission_is_rejected() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:debug_refs"}),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=access,
    )

    try:
        runtime.project(request("dashboard_operator", include_debug_refs=True))
    except PermissionError as exc:
        assert "debug refs" in str(exc)
    else:
        raise AssertionError("debug refs flag without permission must fail closed")


def test_room_and_unknown_viewers_cannot_escalate_by_self_declaring_permissions() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:room_renderer",
            viewer_kind="room_renderer",
            permissions=frozenset(),
            redaction_policy="room-public-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=access,
    )
    malicious = frozenset(
        {
            "projection:actions:status",
            "projection:actions:diagnostic",
            "projection:diagnostics",
            "projection:debug_refs",
        }
    )

    try:
        runtime.project(request("room_renderer", permissions=malicious, include_debug_refs=True))
    except PermissionError as exc:
        assert "exceeds principal permissions" in str(exc)
    else:
        raise AssertionError("room renderer must not self-grant projection permissions")
    room = runtime.project(bind(access, request("room_renderer")))

    assert not hasattr(room.view, "pending_actions")
    assert not hasattr(room.view, "debug_observation_refs")
    assert not hasattr(room.view, "system_health")
    serialized = room.model_dump_json()
    for canary in (
        "encrypted:private-reply",
        "private-effect-key",
        "observation-private",
        "reservation-private",
    ):
        assert canary not in serialized


def test_viewer_cannot_spoof_another_principal_or_redaction_profile() -> None:
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=authority(
            ProjectionGrant(
                world_id=WORLD_ID,
                viewer_id="operator:trusted",
                viewer_kind="dashboard_operator",
                permissions=frozenset({"projection:diagnostics"}),
                redaction_policy="operator-default-v1",
            )
        ),
    )
    spoofed = request(
        "dashboard_operator", permissions=frozenset({"projection:diagnostics"})
    ).model_copy(update={"viewer_id": "room:attacker"})

    try:
        runtime.project(spoofed)
    except PermissionError as exc:
        assert "projection principal" in str(exc)
    else:
        raise AssertionError("spoofed projection principal must fail closed")
    wrong_profile = request("dashboard_operator").model_copy(
        update={
            "viewer_id": "operator:trusted",
            "redaction_policy": "room-public-v1",
        }
    )
    try:
        runtime.project(wrong_profile)
    except PermissionError as exc:
        assert "does not match request" in str(exc)
    else:
        raise AssertionError("mismatched redaction profile must fail closed")


def test_bound_projection_capability_cannot_be_reused_for_larger_scope() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:diagnostics", "projection:actions:diagnostic"}),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=access,
    )
    bound = bind(
        access,
        request(
            "dashboard_operator",
            permissions=frozenset({"projection:diagnostics"}),
        ),
    )
    assert bound.authority_token is not None
    assert bound.authority_token not in repr(bound)
    assert "authority_token" not in bound.model_dump(mode="json")
    tampered = bound.model_copy(
        update={
            "permissions": frozenset({"projection:diagnostics", "projection:actions:diagnostic"})
        }
    )

    try:
        runtime.project(tampered)
    except PermissionError as exc:
        assert "token" in str(exc)
    else:
        raise AssertionError("bound projection capability must not authorize mutation")


def test_projection_authorization_precedes_historical_ledger_replay() -> None:
    class ReadTrapLedger(WorldLedger):
        def project_at(self, cursor: ProjectionCursor):
            raise AssertionError("historical ledger was read before authorization")

    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset(),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ReadTrapLedger(world_id=WORLD_ID),
        projection_authority=access,
    )
    unbound = request("dashboard_operator").model_copy(
        update={
            "at_world_revision": 1,
            "at_deliberation_revision": 0,
            "at_ledger_sequence": 1,
        }
    )

    try:
        runtime.project(unbound)
    except PermissionError as exc:
        assert "capability" in str(exc)
    else:
        raise AssertionError("unbound historical request must fail before ledger read")


def test_projection_capability_is_bound_to_world_audience() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:room_renderer",
            viewer_kind="room_renderer",
            permissions=frozenset(),
            redaction_policy="room-public-v1",
        )
    )
    signed = bind(access, request("room_renderer"))
    other_runtime = WorldRuntime(
        world_id="world-v2-other",
        projection_authority=access,
    )

    try:
        other_runtime.project(signed)
    except PermissionError as exc:
        assert "another world" in str(exc)
    else:
        raise AssertionError("projection capability must not cross world audiences")


def test_evaluator_receives_only_redacted_aggregate_state() -> None:
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:evaluator",
            viewer_kind="evaluator",
            permissions=frozenset({"projection:evaluator:trace"}),
            redaction_policy="evaluator-redacted-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger_with_private_action(),
        projection_authority=access,
    )

    projection = runtime.project(
        bind(
            access,
            request(
                "evaluator",
                permissions=frozenset({"projection:evaluator:trace"}),
            ),
        )
    )

    assert tuple((item.name, item.count) for item in projection.view.action_state_counts) == (
        ("authorized", 1),
    )
    serialized = projection.model_dump_json()
    assert "action-private" not in serialized
    assert "user:private" not in serialized
    assert "encrypted:private-reply" not in serialized


def test_projection_is_bounded_and_has_no_ledger_side_effects() -> None:
    ledger = ledger_with_private_action()
    for index in range(130):
        before = ledger.project()
        ledger.commit(
            [
                event(
                    f"event-observation-{index}",
                    "ObservationRecorded",
                    {"observation_id": f"observation-{index:03d}"},
                )
            ],
            expected_world_revision=before.world_revision,
            expected_deliberation_revision=before.deliberation_revision,
        )
    before_read = ledger.project()
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:debug_refs"}),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        projection_authority=access,
    )

    projection = runtime.project(
        bind(
            access,
            request(
                "dashboard_operator",
                permissions=frozenset({"projection:debug_refs"}),
                include_debug_refs=True,
            ),
        )
    )

    assert len(projection.view.debug_observation_refs) == 100
    assert projection.view.debug_observation_refs[0] == "observation-030"
    assert projection.view.debug_observation_refs[-1] == "observation-129"
    assert ledger.project() == before_read
    assert projection.semantic_hash is None
    assert len(projection.projection_hash) == 64
    debug_window = next(
        item
        for item in projection.view.slice_windows
        if item.slice_name == "debug_observation_refs"
    )
    assert debug_window.total_active == 131
    assert debug_window.returned_count == 100
    assert debug_window.truncated is True


def test_action_projection_truncates_deterministically_without_breaking_other_viewers() -> None:
    ledger_projection = ledger_with_private_action().project()
    source = ledger_projection.pending_actions[0]
    pending = tuple(
        source.model_copy(update={"action_id": f"action-{index}"}) for index in range(3)
    )
    ledger_projection = ledger_projection.model_copy(
        update={"actions": pending, "pending_actions": pending}
    )
    operator_access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:actions:diagnostic"}),
            redaction_policy="operator-default-v1",
        )
    )
    compiler = ProjectionCompiler(
        authority=operator_access,
        limits=ProjectionLimits(pending_actions=2),
    )
    operator_request = bind(
        operator_access,
        request(
            "dashboard_operator",
            permissions=frozenset({"projection:actions:diagnostic"}),
        ),
    )
    operator = compiler.compile(
        ledger_projection,
        operator_request,
    )

    assert tuple(item.action_id for item in operator.view.pending_actions) == (
        "action-1",
        "action-2",
    )
    assert operator.view.slice_windows[0].total_active == 3
    assert operator.view.slice_windows[0].returned_count == 2
    assert operator.view.slice_windows[0].truncated is True

    room_access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:room_renderer",
            viewer_kind="room_renderer",
            permissions=frozenset(),
            redaction_policy="room-public-v1",
        )
    )
    room_compiler = ProjectionCompiler(
        authority=room_access,
        limits=ProjectionLimits(pending_actions=1),
    )
    room_request = bind(room_access, request("room_renderer"))
    room = room_compiler.compile(
        ledger_projection,
        room_request,
    )
    assert room.view.view_kind == "room"


def test_context_truncation_never_limits_authority_queries_or_recovery_paging() -> None:
    base = ledger_with_private_action().project()
    source = base.actions[0]
    actions = tuple(
        source.model_copy(
            update={
                "action_id": f"action-authority-{index}",
                "idempotency_key": f"authority-key-{index}",
            }
        )
        for index in range(3)
    )
    projection = base.model_copy(update={"actions": actions, "pending_actions": actions})
    cursor = ProjectionCursor(
        world_revision=projection.world_revision,
        deliberation_revision=projection.deliberation_revision,
        ledger_sequence=projection.ledger_sequence,
    )

    class StaticLedger:
        world_id = WORLD_ID
        blocks_event_loop = False

        def project(self):
            return projection

        def project_at(self, requested: ProjectionCursor):
            assert requested == cursor
            return projection

    ledger = StaticLedger()
    context = InternalProjectionReader(
        ledger=ledger,
        limits=ProjectionLimits(pending_actions=1),
    ).snapshot(world_id=WORLD_ID, cursor=cursor)
    authority_reader = InternalAuthorityReader(ledger=ledger)

    assert tuple(action.action_id for action in context.pending_actions) == ("action-authority-2",)
    action_window = next(
        item for item in context.slice_windows if item.slice_name == "pending_actions"
    )
    assert action_window.truncated is True
    assert action_window.authority_query_ref == "internal-authority:pending_actions"
    assert (
        authority_reader.action_by_id(
            world_id=WORLD_ID,
            cursor=cursor,
            action_id="action-authority-0",
        )
        == actions[0]
    )

    first = authority_reader.pending_action_page(
        world_id=WORLD_ID,
        cursor=cursor,
        limit=2,
    )
    second = authority_reader.pending_action_page(
        world_id=WORLD_ID,
        cursor=cursor,
        after_action_id=first.next_after_action_id,
        limit=2,
    )
    assert first.complete is False
    assert first.next_after_action_id == "action-authority-1"
    assert tuple(action.action_id for action in (*first.actions, *second.actions)) == (
        "action-authority-0",
        "action-authority-1",
        "action-authority-2",
    )
    assert second.complete is True


def test_projection_hash_binds_reducer_bundle_and_projection_values_are_deeply_frozen() -> None:
    ledger_projection = ledger_with_private_action().project()
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:evaluator",
            viewer_kind="evaluator",
            permissions=frozenset({"projection:evaluator:trace"}),
            redaction_policy="evaluator-redacted-v1",
        )
    )
    compiler = ProjectionCompiler(authority=access)
    projection_request = bind(
        access, request("evaluator", permissions=frozenset({"projection:evaluator:trace"}))
    )
    first = compiler.compile(ledger_projection, projection_request)
    changed_bundle = compiler.compile(
        ledger_projection.model_copy(
            update={"reducer_bundle_version": "world-v2-reducers.test-next"}
        ),
        projection_request,
    )

    assert first.projection_hash != changed_bundle.projection_hash
    assert first.reducer_bundle_version == "world-v2-reducers.10"
    assert changed_bundle.reducer_bundle_version == "world-v2-reducers.test-next"
    try:
        first.view.action_state_counts[0].count = 99
    except Exception as exc:  # Pydantic's frozen-instance error is version-specific.
        assert "frozen" in str(exc).lower()
    else:
        raise AssertionError("projection values must be deeply immutable")


def test_internal_snapshot_is_revision_pinned_and_preserves_private_authority() -> None:
    ledger = ledger_with_private_action()
    reader = InternalProjectionReader(ledger=ledger)

    snapshot = reader.snapshot(
        world_id=WORLD_ID,
        cursor=ProjectionCursor(
            world_revision=4,
            deliberation_revision=0,
            ledger_sequence=4,
        ),
    )

    assert snapshot.world_revision == 4
    assert snapshot.deliberation_revision == 0
    assert snapshot.cursor.ledger_sequence == 4
    assert snapshot.snapshot_id.startswith(f"snapshot:{WORLD_ID}:4:")
    assert len(snapshot.snapshot_hash) == 64
    assert snapshot.pending_actions[0].payload_ref == "encrypted:private-reply"
    assert snapshot.budget_accounts[0].reserved == 100
    assert tuple((version.name, version.version) for version in snapshot.reducer_versions) == (
        ("schema", "world-v2.1"),
        ("reducer_bundle", "world-v2-reducers.10"),
    )
    assert snapshot.system_health.status == "degraded"
    affect_window = next(
        item for item in snapshot.slice_windows if item.slice_name == "affect_episodes"
    )
    assert affect_window.availability == "available"
    assert affect_window.returned_count == 0
    assert snapshot.affect_baselines == ()
    assert tuple(item.intensity_bp for item in snapshot.affect_aggregates) == (0,) * 8
    assert snapshot.current_situation is None
    assert snapshot.relationship_state is None


def test_pending_action_index_is_rejected_if_it_diverges_from_action_history() -> None:
    projection = ledger_with_private_action().project()
    state = ReducerState(
        actions=projection.actions,
        pending_actions=projection.pending_actions,
    )
    tampered = state.model_dump(mode="json")
    tampered["pending_actions"] = []

    try:
        ReducerState.model_validate(tampered)
    except ValueError as exc:
        assert "pending_actions" in str(exc)
    else:
        raise AssertionError("divergent pending action index must fail integrity checks")


def test_deliberation_audit_changes_snapshot_identity_not_world_semantics() -> None:
    ledger = ledger_with_private_action()
    reader = InternalProjectionReader(ledger=ledger)
    before = reader.snapshot(world_id=WORLD_ID)
    head = ledger.project()
    ledger.commit(
        [
            event(
                "event-proposal-audit",
                "ProposalRecorded",
                {"proposal_id": "proposal-audit"},
            )
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )

    after = reader.snapshot(world_id=WORLD_ID)

    assert after.semantic_hash == before.semantic_hash
    assert after.world_revision == before.world_revision
    assert after.deliberation_revision == before.deliberation_revision + 1
    assert after.snapshot_hash != before.snapshot_hash


def test_historical_snapshot_is_pinned_by_complete_cursor_across_later_audit_events() -> None:
    ledger = ledger_with_private_action()
    reader = InternalProjectionReader(ledger=ledger)
    cursor = ProjectionCursor(
        world_revision=4,
        deliberation_revision=0,
        ledger_sequence=4,
    )
    pinned_before = reader.snapshot(world_id=WORLD_ID, cursor=cursor)
    head = ledger.project()
    ledger.commit(
        [
            event(
                "event-later-audit",
                "ProposalRecorded",
                {"proposal_id": "proposal-later-audit"},
            )
        ],
        expected_world_revision=head.world_revision,
        expected_deliberation_revision=head.deliberation_revision,
    )

    pinned_after = reader.snapshot(world_id=WORLD_ID, cursor=cursor)
    current = reader.snapshot(world_id=WORLD_ID)

    assert pinned_after == pinned_before
    assert current.cursor == ProjectionCursor(
        world_revision=4,
        deliberation_revision=1,
        ledger_sequence=5,
    )
    assert current.snapshot_hash != pinned_before.snapshot_hash


def test_internal_snapshot_matches_across_memory_and_sqlite_adapters(tmp_path) -> None:
    memory = ledger_with_private_action()
    sqlite = SQLiteWorldLedger(path=tmp_path / "world-v2-projection.sqlite3", world_id=WORLD_ID)
    populate_ledger(sqlite)

    memory_snapshot = InternalProjectionReader(ledger=memory).snapshot(world_id=WORLD_ID)
    sqlite_snapshot = InternalProjectionReader(ledger=sqlite).snapshot(world_id=WORLD_ID)

    assert sqlite_snapshot == memory_snapshot
    sqlite.close()


def test_historical_snapshot_uses_requested_world_revision_with_current_authority() -> None:
    ledger = ledger_with_private_action()
    before = ledger.project()
    ledger.commit(
        [
            event(
                "event-observation-later",
                "ObservationRecorded",
                {"observation_id": "observation-later"},
            )
        ],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    reader = InternalProjectionReader(ledger=ledger)

    historical = reader.snapshot(
        world_id=WORLD_ID,
        cursor=ProjectionCursor(
            world_revision=4,
            deliberation_revision=0,
            ledger_sequence=4,
        ),
    )
    current = reader.snapshot(world_id=WORLD_ID)

    assert historical.world_revision == 4
    assert historical.ledger_sequence == 4
    assert current.world_revision == 5
    assert historical.snapshot_hash != current.snapshot_hash


def test_viewer_historical_projection_is_redacted_by_current_principal_grant() -> None:
    ledger = ledger_with_private_action()
    before = ledger.project()
    ledger.commit(
        [
            event(
                "event-observation-later",
                "ObservationRecorded",
                {"observation_id": "observation-later"},
            )
        ],
        expected_world_revision=before.world_revision,
        expected_deliberation_revision=before.deliberation_revision,
    )
    access = authority(
        ProjectionGrant(
            world_id=WORLD_ID,
            viewer_id="viewer:dashboard_operator",
            viewer_kind="dashboard_operator",
            permissions=frozenset({"projection:debug_refs"}),
            redaction_policy="operator-default-v1",
        )
    )
    runtime = WorldRuntime(
        world_id=WORLD_ID,
        ledger=ledger,
        projection_authority=access,
    )
    historical_request = request(
        "dashboard_operator",
        permissions=frozenset({"projection:debug_refs"}),
        include_debug_refs=True,
    ).model_copy(
        update={
            "at_world_revision": 4,
            "at_deliberation_revision": 0,
            "at_ledger_sequence": 4,
        }
    )

    historical = runtime.project(bind(access, historical_request))

    assert historical.world_revision == 4
    assert historical.view.debug_observation_refs == ("observation-private",)
    assert "encrypted:private-reply" not in historical.model_dump_json()
