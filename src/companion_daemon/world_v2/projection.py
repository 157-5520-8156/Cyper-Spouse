"""Deterministic internal and viewer-specific projections for World v2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import secrets
from typing import Protocol

from .affect_math import relative_baseline_saturation_bp

from .ledger import LedgerPort
from .proposal_audit_schemas import ModelResultAuditProjection, ProposalAuditProjection
from .schemas import (
    Action,
    AffectAggregateProjection,
    ActionAuthorityPage,
    BudgetAccount,
    BudgetReservation,
    InternalWorldSnapshot,
    LedgerProjection,
    DiagnosticActionProjection,
    EvaluatorProjectionView,
    ExecutionReceipt,
    ExternalObservation,
    NamedCount,
    OperatorProjectionView,
    PlatformActionStatusProjection,
    PlatformProjectionView,
    ProjectionCursor,
    ProjectionRequest,
    ProjectionSliceWindow,
    ProjectionSystemHealth,
    RoomProjectionView,
    VersionRef,
    WorldProjection,
)


@dataclass(frozen=True, slots=True)
class ProjectionLimits:
    pending_actions: int = 128
    debug_observation_refs: int = 100
    budget_accounts: int = 32
    budget_reservations: int = 128
    pending_external_observations: int = 128
    npcs: int = 128
    plans: int = 64
    world_occurrences: int = 128
    outcome_observations: int = 128
    experiences: int = 128
    appraisals: int = 128
    affect_episodes: int = 128
    relationship_boundaries: int = 128

    def __post_init__(self) -> None:
        if any(
            limit <= 0
            for limit in (
                self.pending_actions,
                self.debug_observation_refs,
                self.budget_accounts,
                self.budget_reservations,
                self.pending_external_observations,
                self.npcs,
                self.plans,
                self.world_occurrences,
                self.outcome_observations,
                self.experiences,
                self.appraisals,
                self.affect_episodes,
                self.relationship_boundaries,
            )
        ):
            raise ValueError("projection limits must be positive")


@dataclass(frozen=True, slots=True)
class ProjectionGrant:
    world_id: str
    viewer_id: str
    viewer_kind: str
    permissions: frozenset[str]
    redaction_policy: str
    action_targets: frozenset[str] = frozenset()
    grant_revision: int = 1

    def __post_init__(self) -> None:
        if not self.world_id or not self.viewer_id:
            raise ValueError("projection grant identity must not be empty")
        if type(self.permissions) is not frozenset or type(self.action_targets) is not frozenset:
            raise TypeError("projection grant scopes must be immutable frozensets")
        if self.grant_revision <= 0:
            raise ValueError("projection grant revision must be positive")
        profiles = {
            "platform_adapter": "platform-v1",
            "dashboard_operator": "operator-default-v1",
            "room_renderer": "room-public-v1",
            "evaluator": "evaluator-redacted-v1",
        }
        allowed_permissions = {
            "platform_adapter": frozenset({"projection:actions:status"}),
            "dashboard_operator": frozenset(
                {
                    "projection:actions:diagnostic",
                    "projection:diagnostics",
                    "projection:debug_refs",
                    "projection:internal_hash",
                }
            ),
            "room_renderer": frozenset(),
            "evaluator": frozenset({"projection:evaluator:trace"}),
        }
        if self.viewer_kind not in profiles:
            raise ValueError(f"unsupported projection viewer kind {self.viewer_kind!r}")
        if self.redaction_policy != profiles[self.viewer_kind]:
            raise ValueError("projection grant has an incompatible redaction policy")
        if not self.permissions <= allowed_permissions[self.viewer_kind]:
            raise ValueError("projection grant contains permissions invalid for its viewer")
        if self.viewer_kind == "platform_adapter":
            if "projection:actions:status" in self.permissions and not self.action_targets:
                raise ValueError("platform action-status grant requires target scope")
        elif self.action_targets:
            raise ValueError("only platform grants may declare action target scope")


@dataclass(frozen=True, slots=True)
class AuthorizedProjection:
    world_id: str
    permissions: frozenset[str]
    action_targets: frozenset[str]
    grant_revision: int


@dataclass(frozen=True, slots=True)
class AuthenticatedProjectionPrincipal:
    principal_id: str
    world_id: str
    authentication_context: str

    def __post_init__(self) -> None:
        if not self.principal_id or not self.world_id or not self.authentication_context:
            raise ValueError("authenticated projection principal must be complete")


class ProjectionPrincipalVerifier(Protocol):
    def authenticate(self, credential: object) -> AuthenticatedProjectionPrincipal: ...


class ProjectionAuthority:
    """Verify short-lived projection capabilities; it does not authenticate ingress."""

    def __init__(
        self,
        *,
        grants: tuple[ProjectionGrant, ...] = (),
        signing_key: bytes | None = None,
        key_version: str = "projection-key.1",
        clock: Callable[[], datetime] | None = None,
        capability_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        by_id: dict[tuple[str, str], ProjectionGrant] = {}
        for grant in grants:
            identity = (grant.world_id, grant.viewer_id)
            if identity in by_id:
                raise ValueError(f"duplicate projection principal {grant.viewer_id!r}")
            by_id[identity] = grant
        if signing_key is not None and (type(signing_key) is not bytes or len(signing_key) < 32):
            raise ValueError("projection signing key must be at least 32 immutable bytes")
        if not key_version:
            raise ValueError("projection key version must not be empty")
        if capability_ttl <= timedelta(0):
            raise ValueError("projection capability ttl must be positive")
        self._grants = by_id
        self._signing_key = signing_key or secrets.token_bytes(32)
        self._key_version = key_version
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capability_ttl = capability_ttl

    def _bind_authenticated(
        self,
        request: ProjectionRequest,
        principal: AuthenticatedProjectionPrincipal,
    ) -> ProjectionRequest:
        if principal.principal_id != request.viewer_id:
            raise PermissionError("authenticated principal does not match projection viewer")
        if principal.world_id != request.world_id:
            raise PermissionError("authenticated principal does not match projection world")
        unsigned = request.model_copy(
            update={
                "authority_token": None,
                "capability_issued_at": None,
                "capability_expires_at": None,
            }
        )
        grant = self._matching_grant(unsigned)
        self._validate_scope(unsigned, grant)
        issued_at = self._clock()
        if issued_at.tzinfo is None or issued_at.utcoffset() is None:
            raise ValueError("projection authority clock must be timezone-aware")
        stamped = unsigned.model_copy(
            update={
                "capability_issued_at": issued_at,
                "capability_expires_at": issued_at + self._capability_ttl,
            }
        )
        return stamped.model_copy(update={"authority_token": self._token(stamped, grant)})

    def authorize(self, request: ProjectionRequest) -> AuthorizedProjection:
        grant = self._matching_grant(request)
        self._validate_scope(request, grant)
        if request.capability_expires_at is None or request.capability_issued_at is None:
            raise PermissionError("projection capability timestamps are missing")
        now = self._clock()
        if now < request.capability_issued_at or now >= request.capability_expires_at:
            raise PermissionError("projection capability is outside its validity window")
        expected = self._token(request.model_copy(update={"authority_token": None}), grant)
        if request.authority_token is None or not hmac.compare_digest(
            request.authority_token, expected
        ):
            raise PermissionError("projection authority token is missing or invalid")
        return AuthorizedProjection(
            world_id=request.world_id,
            permissions=request.permissions,
            action_targets=grant.action_targets,
            grant_revision=grant.grant_revision,
        )

    def _matching_grant(self, request: ProjectionRequest) -> ProjectionGrant:
        grant = self._grants.get((request.world_id, request.viewer_id))
        if grant is None:
            raise PermissionError("projection principal is not authorized")
        if (
            grant.viewer_kind != request.viewer_kind
            or grant.redaction_policy != request.redaction_policy
        ):
            raise PermissionError("projection principal grant does not match request")
        return grant

    @staticmethod
    def _validate_scope(request: ProjectionRequest, grant: ProjectionGrant) -> None:
        if not request.permissions <= grant.permissions:
            raise PermissionError("projection request exceeds principal permissions")

    def _token(self, request: ProjectionRequest, grant: ProjectionGrant) -> str:
        intent = {
            "schema_version": request.schema_version,
            "request_id": request.request_id,
            "world_id": request.world_id,
            "viewer_id": request.viewer_id,
            "viewer_kind": request.viewer_kind,
            "permissions": sorted(request.permissions),
            "at_world_revision": request.at_world_revision,
            "at_deliberation_revision": request.at_deliberation_revision,
            "at_ledger_sequence": request.at_ledger_sequence,
            "trace_id": request.trace_id,
            "include_debug_refs": request.include_debug_refs,
            "redaction_policy": request.redaction_policy,
            "capability_issued_at": request.capability_issued_at.isoformat()
            if request.capability_issued_at
            else None,
            "capability_expires_at": request.capability_expires_at.isoformat()
            if request.capability_expires_at
            else None,
            "grant_revision": grant.grant_revision,
            "key_version": self._key_version,
        }
        encoded = json.dumps(intent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._signing_key, encoded, hashlib.sha256).hexdigest()


class ProjectionCapabilityIssuer:
    """Trusted-ingress component; never pass it to Runtime or viewer code."""

    def __init__(
        self,
        *,
        authority: ProjectionAuthority,
        principal_verifier: ProjectionPrincipalVerifier,
    ) -> None:
        self._authority = authority
        self._principal_verifier = principal_verifier

    def bind(self, request: ProjectionRequest, *, credential: object) -> ProjectionRequest:
        principal = self._principal_verifier.authenticate(credential)
        return self._authority._bind_authenticated(request, principal)


class InternalProjectionReader:
    """Bounded situation-context reader; never use for Acceptance or recovery."""

    def __init__(self, *, ledger: LedgerPort, limits: ProjectionLimits | None = None) -> None:
        self._ledger = ledger
        self._limits = limits or ProjectionLimits()

    def snapshot(
        self, *, world_id: str, cursor: ProjectionCursor | None = None
    ) -> InternalWorldSnapshot:
        if world_id != self._ledger.world_id:
            raise ValueError("requested snapshot belongs to another world")
        projection = self._ledger.project() if cursor is None else self._ledger.project_at(cursor)
        active_reservations = tuple(
            reservation
            for reservation in projection.budget_reservations
            if reservation.state == "reserved"
        )
        pending_actions = projection.pending_actions[-self._limits.pending_actions :]
        budget_accounts = projection.budget_accounts[-self._limits.budget_accounts :]
        active_reservations = active_reservations[-self._limits.budget_reservations :]
        pending_external = projection.pending_external_observations[
            -self._limits.pending_external_observations :
        ]
        npcs = self._bounded_relevant(
            projection.npcs,
            limit=self._limits.npcs,
            is_relevant=lambda item: item.status == "active",
        )
        plans = self._bounded_relevant(
            projection.plans,
            limit=self._limits.plans,
            is_relevant=lambda item: item.status in {"planned", "active", "paused"},
        )
        occurrences = self._bounded_relevant(
            projection.world_occurrences,
            limit=self._limits.world_occurrences,
            is_relevant=lambda item: item.status in {"committed", "active"},
        )
        outcome_observations = projection.outcome_observations[-self._limits.outcome_observations :]
        experiences = projection.experiences[-self._limits.experiences :]
        active_appraisals = tuple(item for item in projection.appraisals if item.status == "active")
        appraisals = active_appraisals[-self._limits.appraisals :]
        active_affect_episodes = tuple(
            item for item in projection.affect_episodes if item.status == "active"
        )
        affect_episodes = active_affect_episodes[-self._limits.affect_episodes :]
        relationship_state = (
            projection.relationship_states[0] if projection.relationship_states else None
        )
        active_relationship_boundaries = tuple(
            item
            for item in projection.boundaries
            if item.status == "active"
            and (
                relationship_state is None
                or item.subject_ref == relationship_state.subject_ref
            )
        )
        relationship_boundaries = active_relationship_boundaries[
            -self._limits.relationship_boundaries :
        ]
        baseline_by_dimension = {
            item.dimension: item.baseline_bp for item in projection.affect_baselines
        }
        affect_aggregates = tuple(
            AffectAggregateProjection(
                dimension=dimension,
                intensity_bp=relative_baseline_saturation_bp(
                    baseline_by_dimension.get(dimension, 0),
                    [
                        component.intensity_bp
                        for episode in active_affect_episodes
                        for component in episode.components
                        if component.dimension == dimension
                    ],
                ),
                active_component_count=sum(
                    component.dimension == dimension
                    for episode in active_affect_episodes
                    for component in episode.components
                ),
            )
            for dimension in (
                "hurt",
                "anger",
                "sadness",
                "loneliness",
                "anxiety",
                "resentment",
                "warmth",
                "joy",
            )
        )
        slice_windows = (
            self._window(
                "pending_actions",
                total=len(projection.pending_actions),
                returned=len(pending_actions),
            ),
            self._window(
                "budget_accounts",
                total=len(projection.budget_accounts),
                returned=len(budget_accounts),
            ),
            self._window(
                "active_budget_reservations",
                total=sum(
                    reservation.state == "reserved"
                    for reservation in projection.budget_reservations
                ),
                returned=len(active_reservations),
            ),
            self._window(
                "pending_external_observations",
                total=len(projection.pending_external_observations),
                returned=len(pending_external),
            ),
            self._window(
                "npcs",
                total=len(projection.npcs),
                returned=len(npcs),
                ordering_policy="active-first-then-ledger-recency-v1",
            ),
            self._window(
                "plans",
                total=len(projection.plans),
                returned=len(plans),
                ordering_policy="open-first-then-ledger-recency-v1",
            ),
            self._window(
                "world_occurrences",
                total=len(projection.world_occurrences),
                returned=len(occurrences),
                ordering_policy="unsettled-first-then-ledger-recency-v1",
            ),
            self._window(
                "outcome_observations",
                total=len(projection.outcome_observations),
                returned=len(outcome_observations),
            ),
            self._window(
                "experiences",
                total=len(projection.experiences),
                returned=len(experiences),
            ),
            self._window(
                "appraisals",
                total=len(active_appraisals),
                returned=len(appraisals),
                ordering_policy="active-first-then-ledger-recency-v1",
            ),
            self._window(
                "affect_episodes",
                total=len(active_affect_episodes),
                returned=len(affect_episodes),
                ordering_policy="active-first-then-ledger-recency-v1",
            ),
            self._window(
                "relationship_state",
                total=len(projection.relationship_states),
                returned=1 if relationship_state is not None else 0,
                ordering_policy="ledger-recency-v1",
            ),
            self._window(
                "relationship_boundaries",
                total=len(active_relationship_boundaries),
                returned=len(relationship_boundaries),
                ordering_policy="active-ledger-recency-v1",
            ),
            *(self._unavailable_window(name) for name in self._UNAVAILABLE_SLICES),
        )
        updated_at = projection.logical_time or datetime(1970, 1, 1, tzinfo=UTC)
        health = ProjectionCompiler._health(projection, diagnostics=True).model_copy(
            update={
                "status": "degraded",
                "unavailable_slices": self._UNAVAILABLE_SLICES,
            }
        )
        draft = InternalWorldSnapshot(
            snapshot_id="snapshot:pending",
            snapshot_hash="0" * 64,
            world_id=projection.world_id,
            cursor=ProjectionCursor(
                world_revision=projection.world_revision,
                deliberation_revision=projection.deliberation_revision,
                ledger_sequence=projection.ledger_sequence,
            ),
            semantic_hash=projection.semantic_hash,
            logical_time=projection.logical_time,
            updated_at=updated_at,
            projection_policy_version="world-v2-internal-projection.1",
            pending_actions=pending_actions,
            budget_accounts=budget_accounts,
            budget_reservations=active_reservations,
            pending_external_observations=pending_external,
            npcs=npcs,
            plans=plans,
            world_occurrences=occurrences,
            outcome_observations=outcome_observations,
            experiences=experiences,
            appraisals=appraisals,
            affect_episodes=affect_episodes,
            affect_baselines=projection.affect_baselines,
            affect_aggregates=affect_aggregates,
            relationship_state=relationship_state,
            relationship_boundaries=relationship_boundaries,
            reducer_versions=(
                VersionRef(name="schema", version=projection.schema_version),
                VersionRef(
                    name="reducer_bundle",
                    version=projection.reducer_bundle_version,
                ),
            ),
            slice_windows=slice_windows,
            system_health=health,
        )
        snapshot_payload = draft.model_dump(mode="json", exclude={"snapshot_id", "snapshot_hash"})
        snapshot_hash = hashlib.sha256(
            json.dumps(
                snapshot_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return draft.model_copy(
            update={
                "snapshot_id": (
                    f"snapshot:{projection.world_id}:{projection.ledger_sequence}:"
                    f"{snapshot_hash[:16]}"
                ),
                "snapshot_hash": snapshot_hash,
            }
        )

    _UNAVAILABLE_SLICES = (
        "character_core",
        "facts",
        "current_situation",
        "commitments",
        "private_impressions",
        "conversation_threads",
        "capabilities",
        "consents",
        "privacy_policy",
        "media_candidates",
    )

    @staticmethod
    def _window(
        slice_name: str,
        *,
        total: int,
        returned: int,
        ordering_policy: str = "ledger-identity-order-v1",
    ) -> ProjectionSliceWindow:
        return ProjectionSliceWindow(
            slice_name=slice_name,
            total_active=total,
            returned_count=returned,
            truncated=returned < total,
            ordering_policy=ordering_policy,
            retention_policy_version="world-v2-retention.1",
            authority_query_ref=f"internal-authority:{slice_name}",
        )

    @staticmethod
    def _bounded_relevant(values, *, limit: int, is_relevant):
        relevant = tuple(value for value in values if is_relevant(value))
        if len(relevant) >= limit:
            return relevant[-limit:]
        remaining = tuple(value for value in values if not is_relevant(value))
        return (*relevant, *remaining[-(limit - len(relevant)) :])

    @staticmethod
    def _unavailable_window(slice_name: str) -> ProjectionSliceWindow:
        return ProjectionSliceWindow(
            slice_name=slice_name,
            total_active=0,
            returned_count=0,
            truncated=False,
            availability="unavailable",
            unavailable_reason="reducer_not_implemented",
            ordering_policy="not-applicable-v1",
            retention_policy_version="world-v2-retention.1",
        )


class InternalAuthorityReader:
    """Exact and paginated authority reads for Acceptance and recovery."""

    def __init__(self, *, ledger: LedgerPort) -> None:
        self._ledger = ledger

    def current_cursor(self, *, world_id: str) -> ProjectionCursor:
        projection = self._current(world_id=world_id)
        return self._cursor(projection)

    def model_result_audit_by_ref(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        model_result_ref: str,
    ) -> ModelResultAuditProjection | None:
        if not model_result_ref:
            raise ValueError("model_result_ref must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (
                audit
                for audit in projection.model_result_audits
                if audit.model_result_ref == model_result_ref
            ),
            None,
        )

    def proposal_audit_by_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        proposal_id: str,
    ) -> ProposalAuditProjection | None:
        if not proposal_id:
            raise ValueError("proposal_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (audit for audit in projection.proposal_audits if audit.proposal_id == proposal_id),
            None,
        )

    def action_by_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        action_id: str,
    ) -> Action | None:
        if not action_id:
            raise ValueError("action_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (action for action in projection.actions if action.action_id == action_id),
            None,
        )

    def budget_account_by_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        account_id: str,
    ) -> BudgetAccount | None:
        if not account_id:
            raise ValueError("account_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (account for account in projection.budget_accounts if account.account_id == account_id),
            None,
        )

    def budget_reservation_by_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        reservation_id: str,
    ) -> BudgetReservation | None:
        if not reservation_id:
            raise ValueError("reservation_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (
                reservation
                for reservation in projection.budget_reservations
                if reservation.reservation_id == reservation_id
            ),
            None,
        )

    def external_observation_by_result_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        result_id: str,
    ) -> ExternalObservation | None:
        if not result_id:
            raise ValueError("result_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (
                result
                for result in projection.pending_external_observations
                if result.result_id == result_id
            ),
            None,
        )

    def execution_receipt_by_id(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        receipt_id: str,
    ) -> ExecutionReceipt | None:
        if not receipt_id:
            raise ValueError("receipt_id must not be empty")
        projection = self._at(world_id=world_id, cursor=cursor)
        return next(
            (
                receipt
                for receipt in projection.execution_receipts
                if receipt.receipt_id == receipt_id
            ),
            None,
        )

    def pending_action_page(
        self,
        *,
        world_id: str,
        cursor: ProjectionCursor,
        after_action_id: str | None = None,
        limit: int = 100,
    ) -> ActionAuthorityPage:
        if limit <= 0 or limit > 500:
            raise ValueError("authority page limit must be between 1 and 500")
        projection = self._at(world_id=world_id, cursor=cursor)
        start = 0
        if after_action_id is not None:
            match = next(
                (
                    index
                    for index, action in enumerate(projection.pending_actions)
                    if action.action_id == after_action_id
                ),
                None,
            )
            if match is None:
                raise ValueError("authority page cursor action is not pending")
            start = match + 1
        actions = projection.pending_actions[start : start + limit]
        complete = start + len(actions) >= len(projection.pending_actions)
        return ActionAuthorityPage(
            world_id=world_id,
            cursor=cursor,
            actions=actions,
            next_after_action_id=None if complete or not actions else actions[-1].action_id,
            complete=complete,
        )

    def _current(self, *, world_id: str) -> LedgerProjection:
        self._validate_world(world_id)
        return self._ledger.project()

    def _at(self, *, world_id: str, cursor: ProjectionCursor) -> LedgerProjection:
        self._validate_world(world_id)
        return self._ledger.project_at(cursor)

    def _validate_world(self, world_id: str) -> None:
        if world_id != self._ledger.world_id:
            raise ValueError("authority query belongs to another world")

    @staticmethod
    def _cursor(projection: LedgerProjection) -> ProjectionCursor:
        return ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )


class ProjectionCompiler:
    """Apply viewer ceilings and bounded read policies behind one interface."""

    def __init__(
        self,
        *,
        authority: ProjectionAuthority | None = None,
        limits: ProjectionLimits | None = None,
    ) -> None:
        self._authority = authority or ProjectionAuthority()
        self._limits = limits or ProjectionLimits()

    def authorize(self, request: ProjectionRequest) -> AuthorizedProjection:
        """Authorize before any ledger read, especially historical replay."""

        if request.include_debug_refs and "projection:debug_refs" not in request.permissions:
            raise PermissionError("debug refs require explicit requested permission")
        return self._authority.authorize(request)

    def compile(
        self,
        projection: LedgerProjection,
        request: ProjectionRequest,
    ) -> WorldProjection:
        access = self.authorize(request)
        if projection.world_id != request.world_id or access.world_id != request.world_id:
            raise PermissionError("projection world does not match authorized audience")
        actual_cursor = ProjectionCursor(
            world_revision=projection.world_revision,
            deliberation_revision=projection.deliberation_revision,
            ledger_sequence=projection.ledger_sequence,
        )
        if request.at_cursor not in (None, actual_cursor):
            raise ValueError("requested projection cursor is not available")
        permissions = access.permissions
        action_mode = self._action_mode(request.viewer_kind, permissions)
        visible_actions = self._visible_actions(
            projection=projection,
            viewer_kind=request.viewer_kind,
            action_mode=action_mode,
            action_targets=access.action_targets,
        )
        bounded_actions = visible_actions[-self._limits.pending_actions :]
        action_window = self._action_window(
            total=len(visible_actions), returned=len(bounded_actions)
        )
        pending = tuple(self._action_view(action, mode=action_mode) for action in bounded_actions)

        diagnostics = (
            request.viewer_kind == "dashboard_operator" and "projection:diagnostics" in permissions
        )
        health = self._health(projection, diagnostics=diagnostics)
        debug_allowed = (
            request.viewer_kind == "dashboard_operator" and "projection:debug_refs" in permissions
        )
        debug_refs = ()
        debug_window = None
        if request.include_debug_refs and debug_allowed:
            debug_refs = projection.observation_refs[-self._limits.debug_observation_refs :]
            debug_window = ProjectionSliceWindow(
                slice_name="debug_observation_refs",
                total_active=len(projection.observation_refs),
                returned_count=len(debug_refs),
                truncated=len(debug_refs) < len(projection.observation_refs),
                ordering_policy="ledger-sequence-v1",
                retention_policy_version="world-v2-retention.1",
            )

        slice_windows = ()
        if action_mode is not None:
            slice_windows = (*slice_windows, action_window)
        if debug_window is not None:
            slice_windows = (*slice_windows, debug_window)

        view = self._view(
            request=request,
            permissions=permissions,
            pending=pending,
            health=health,
            debug_refs=debug_refs,
            projection=projection,
            slice_windows=slice_windows,
        )
        internal_hash = None
        if (
            request.viewer_kind == "dashboard_operator"
            and "projection:internal_hash" in permissions
        ):
            internal_hash = projection.semantic_hash
        visible = {
            "schema_version": projection.schema_version,
            "reducer_bundle_version": projection.reducer_bundle_version,
            "world_id": projection.world_id,
            "world_revision": projection.world_revision,
            "ledger_sequence": projection.ledger_sequence,
            "viewer_kind": request.viewer_kind,
            "redaction_policy": request.redaction_policy,
            "projection_policy_version": "world-v2-projection-policy.1",
            "semantic_hash": internal_hash,
            "logical_time": projection.logical_time.isoformat()
            if projection.logical_time
            else None,
            "view": view.model_dump(mode="json"),
        }
        projection_hash = hashlib.sha256(
            json.dumps(
                visible,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return WorldProjection(
            schema_version=projection.schema_version,
            world_id=projection.world_id,
            world_revision=projection.world_revision,
            ledger_sequence=projection.ledger_sequence,
            viewer_kind=request.viewer_kind,
            redaction_policy=request.redaction_policy,
            reducer_bundle_version=projection.reducer_bundle_version,
            projection_hash=projection_hash,
            semantic_hash=internal_hash,
            logical_time=projection.logical_time,
            view=view,
        )

    @staticmethod
    def _action_mode(viewer: str, permissions: frozenset[str]) -> str | None:
        if viewer == "platform_adapter" and "projection:actions:status" in permissions:
            return "status"
        if viewer == "dashboard_operator" and "projection:actions:diagnostic" in permissions:
            return "diagnostic"
        return None

    @staticmethod
    def _visible_actions(
        *,
        projection: LedgerProjection,
        viewer_kind: str,
        action_mode: str | None,
        action_targets: frozenset[str],
    ) -> tuple[Action, ...]:
        if action_mode is None:
            return ()
        actions = projection.pending_actions
        if viewer_kind == "platform_adapter":
            actions = tuple(action for action in actions if action.target in action_targets)
        return actions

    @staticmethod
    def _action_window(*, total: int, returned: int) -> ProjectionSliceWindow:
        return ProjectionSliceWindow(
            slice_name="pending_actions",
            total_active=total,
            returned_count=returned,
            truncated=returned < total,
            ordering_policy="authorization-ledger-order-v1",
            retention_policy_version="world-v2-retention.1",
        )

    @staticmethod
    def _view(
        *,
        request: ProjectionRequest,
        permissions: frozenset[str],
        pending: tuple[PlatformActionStatusProjection | DiagnosticActionProjection, ...],
        health: ProjectionSystemHealth,
        debug_refs: tuple[str, ...],
        projection: LedgerProjection,
        slice_windows: tuple[ProjectionSliceWindow, ...],
    ):
        if request.viewer_kind == "platform_adapter":
            return PlatformProjectionView(
                action_statuses=pending,
                slice_windows=slice_windows,
            )
        if request.viewer_kind == "dashboard_operator":
            return OperatorProjectionView(
                pending_actions=pending,
                system_health=health,
                debug_observation_refs=debug_refs,
                slice_windows=slice_windows,
            )
        if request.viewer_kind == "room_renderer":
            return RoomProjectionView()
        counts: dict[str, int] = {}
        if "projection:evaluator:trace" in permissions:
            for action in projection.actions:
                counts[action.state] = counts.get(action.state, 0) + 1
        return EvaluatorProjectionView(
            action_state_counts=tuple(
                NamedCount(name=name, count=count) for name, count in sorted(counts.items())
            )
        )

    @staticmethod
    def _action_view(
        action, *, mode: str | None
    ) -> PlatformActionStatusProjection | DiagnosticActionProjection:
        shared = {
            "action_id": action.action_id,
            "kind": action.kind,
            "layer": action.layer,
            "state": action.state,
            "not_before": action.not_before,
            "expires_at": action.expires_at,
            "dependencies": action.dependencies,
        }
        if mode == "diagnostic":
            return DiagnosticActionProjection(**shared)
        if mode == "status":
            return PlatformActionStatusProjection(**shared, target=action.target)
        raise ValueError("action projection mode is not visible")

    @staticmethod
    def _health(projection: LedgerProjection, *, diagnostics: bool) -> ProjectionSystemHealth:
        if not diagnostics:
            return ProjectionSystemHealth()
        return ProjectionSystemHealth(
            reducer_bundle_version=projection.reducer_bundle_version,
            deliberation_revision=projection.deliberation_revision,
            action_count=len(projection.actions),
            pending_action_count=len(projection.pending_actions),
            budget_account_count=len(projection.budget_accounts),
            reserved_budget=sum(account.reserved for account in projection.budget_accounts),
            spent_budget=sum(account.spent for account in projection.budget_accounts),
            pending_external_result_count=len(projection.pending_external_observations),
            reconciliation_count=len(projection.reconciliations),
        )
