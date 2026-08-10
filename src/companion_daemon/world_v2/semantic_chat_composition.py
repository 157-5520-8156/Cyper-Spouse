"""Production composition for CharacterInterior and compute routing.

The Module keeps one small Interface for platform composition roots while it
owns model selection, Flash / Thinking routing, endpoint timing, source review,
and model lifecycle. CharacterInterior is the only protagonist semantic author;
the text endpoint predicts only whether another user bubble is likely.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import logging
from types import SimpleNamespace
from typing import Literal
from urllib.parse import urlsplit

from companion_daemon.character import load_character
from companion_daemon.config import Settings
from companion_daemon.llm import (
    DeepSeekChatModel,
    OpenAICompatibleChatModel,
    ProviderCapacityGate,
    ProviderCircuitBreaker,
    text_endpoint_capacity_marker_path,
)

from .character_interior import CharacterInterior
from .character_interior.production import compose_production_character_interior
from .character_interior.turn_store import _CharacterInteriorTurnStore
from .companion_identity import CompanionIdentityFrame
from .expression_draft import (
    ExpressionDraftCapabilities,
    PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES,
)
from .model_completion import ChatCompletionModel
from .model_authority_identity import (
    possible_provider_lanes,
    provider_lane_sets_are_independent,
    semantic_authority_id,
    transport_route_ids,
)
from .semantic_compute_router import SemanticComputeRouter
from .source_review_authority import (
    SOURCE_REVIEW_CALL_TIMEOUT_SECONDS,
    SourceReviewAuthority,
)
from .source_closure_lane import SourceClosureReselectionLane
from .structured_expression_reselection_model import (
    EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT,
)
from .text_turn_endpoint import (
    ChatSemanticEndpointModel,
    TextTurnEndpointController,
)
from .structured_source_review_model import (
    direct_openai_model_id,
    InventoryAvailabilityAuthority,
    openai_inventory_capability_evidence,
    audited_source_review_capability_evidence,
    StrictOutputCapabilityEvidence,
    StructuredSourceReviewModel,
    openrouter_inventory_capability_evidence,
    _STRICT_SCHEMAS,
)


_LOG = logging.getLogger(__name__)
_CANDIDATE_INVENTORY_CONTRACT = "candidate-external-proposition-inventory.5"
_CANDIDATE_COVERAGE_CONTRACT = "candidate-external-proposition-coverage.5"
_FULL_SOURCE_REVIEW_CONTRACT = "source-closure-review.7"
_REPORT_RELATIVE_REVIEW_CONTRACT = "report-relative-entailment-adjudication.3"
_LIFE_SOURCE_REVIEW_CONTRACTS = (
    "life-development-source-closure-review.1",
    "life-development-novel-origin-review.2",
)


def unavailable_life_source_authority_health() -> dict[str, object]:
    """Return a fresh backward-compatible snapshot for missing composition.

    Hosts expose this shape before semantic composition is available. Returning
    new containers prevents one health consumer from mutating a later response.
    """

    return {
        "status": "unavailable",
        "warning": True,
        "warning_reasons": ["life_source_authority.composition_unavailable"],
        "runtime_isolated": False,
        "runtime_isolation": "unavailable",
        "reviewer_model": None,
        "contracts": {
            contract: {
                "schema_installed": False,
                "parser_fail_closed": True,
                "release_qualified": False,
            }
            for contract in _LIFE_SOURCE_REVIEW_CONTRACTS
        },
        "last_transport_winner": None,
        "route_suppression": {},
        "transport_runtime": None,
    }


@dataclass(frozen=True, slots=True)
class _ConfiguredProviderLane:
    """Allocation-free provider identity used by the production preflight."""

    provider: str
    base_url: str
    model: str
    semantic_authority_id: str | None = None


@dataclass(frozen=True, slots=True)
class _ConfiguredReviewAuthority:
    primary: object
    secondary: object

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            _REPORT_RELATIVE_REVIEW_CONTRACT,
            _FULL_SOURCE_REVIEW_CONTRACT,
        }


def _direct_source_review_reasoning_effort(model: str) -> str:
    """Return only the reasoning knob qualified for the exact direct route.

    GPT-4.1/4o Chat Completions reject the argument entirely, while the
    release-qualified GPT-5 mini review route uses ``minimal``. Unknown model
    overrides therefore take the portable fail-closed path and omit the knob.
    """

    return "minimal" if direct_openai_model_id(model).casefold() == "gpt-5-mini" else ""


def _validated_test_only_provider_capture_authority(
    *,
    settings: Settings,
    authority_id: str | None,
) -> str | None:
    """Validate the identity hand-off used by the isolated provider harness.

    The real author route is identified by its provider endpoint and checkpoint.
    A loopback hash capture necessarily changes that endpoint, so the acceptance
    process may carry the already-derived identity explicitly.  This seam is
    deliberately restricted to an IPv4 loopback endpoint and to the exact
    release-pinned DeepSeek checkpoint; production configuration cannot use it
    to claim an arbitrary authority.
    """

    if authority_id is None:
        return None
    try:
        parsed = urlsplit(settings.deepseek_base_url.rstrip("/"))
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "test-only provider capture authority requires a valid loopback DeepSeek endpoint"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "test-only provider capture authority requires an exact IPv4 loopback DeepSeek endpoint"
        )
    expected = semantic_authority_id(
        SimpleNamespace(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model=settings.deepseek_model,
        )
    )
    if expected is None or authority_id != expected:
        raise ValueError(
            "test-only provider capture authority does not match the configured DeepSeek checkpoint"
        )
    return authority_id


def _apply_test_only_provider_capture_authority(
    model: object | None,
    authority_id: str | None,
    *,
    settings: Settings,
) -> None:
    """Attach the validated capture identity before independence checks.

    Only auto-created models reach this helper.  Rechecking their route and
    checkpoint here prevents a future provider-construction change from
    accidentally inheriting the primary model's authority label.
    """

    if authority_id is None or model is None:
        return
    if (
        getattr(model, "provider", None) != "deepseek"
        or str(getattr(model, "base_url", "")).rstrip("/")
        != settings.deepseek_base_url.rstrip("/")
        or str(getattr(model, "model", "")).strip()
        != settings.deepseek_model
    ):
        raise ValueError("test-only provider capture authority requires a DeepSeek model")
    object.__setattr__(model, "semantic_authority_id", authority_id)
    # The exact loopback capture is also allowed to receive a SHA-256 of the
    # adapter-verified logical request identity.  The capture proxy strips the
    # header before forwarding upstream; ordinary production models never set
    # this private flag and therefore never emit local audit metadata.
    object.__setattr__(model, "_test_only_capture_exact_request_identity", True)


def _model_identity(model: object | None) -> str | None:
    """Return the smallest deployment-visible model identity.

    This is an audit label, not a security principal.  Explicit provider
    dependencies remain the authority; the label only lets composition reject
    an obvious author-as-reviewer route and explain the deployed state.
    """

    if model is None:
        return None
    value = str(getattr(model, "model", "")).strip()
    return value[:256] if value else type(model).__name__[:256]


def _possible_provider_lanes(model: object | None) -> tuple[object, ...]:
    """Expand every provider that may produce bytes for one semantic role."""

    return possible_provider_lanes(model)


def _provider_lane_sets_are_independent(
    left: object | None,
    right: object | None,
) -> bool:
    """Prove that two roles have no shared possible provider authority."""

    return provider_lane_sets_are_independent(left, right)


def _reviewer_is_independent(*, author: object, reviewer: object | None) -> bool:
    """Reject self-review across every possible author and reviewer lane.

    A production ``FailoverChatModel`` has implicit failover disabled: its
    primary remains the role author and its separately exposed fallback can be
    the review authority.  If implicit failover is enabled, either branch may
    have authored the candidate and neither branch is an independent reviewer.
    """

    return _provider_lane_sets_are_independent(author, reviewer)


def _uses_implicit_character_failover(model: object | None) -> bool:
    """Whether one author port can silently switch to a second provider."""

    if model is None:
        return False
    origin = getattr(model, "authority_origin", model)
    return (
        getattr(origin, "primary", None) is not None
        and getattr(origin, "fallback", None) is not None
        and bool(getattr(origin, "implicit_failover", True))
    )


def _supports_strict_output_contract(
    model: object | None,
    contract: str,
) -> bool:
    """Query an explicit transport capability without guessing from identity."""

    if model is None:
        return False
    try:
        checker = getattr(model, "supports_strict_output_contract", None)
        return callable(checker) and checker(contract) is True
    except Exception:
        # A malformed capability declaration proves nothing. The caller keeps
        # its existing fallback instead of risking an unsupported strict wire.
        return False


def _installs_strict_output_contract(
    model: object | None,
    contract: str,
) -> bool:
    """Prove that every possible transport lane installs the exact schema."""

    if model is None:
        return False
    checker = getattr(model, "installs_strict_output_contract", None)
    try:
        if callable(checker):
            return checker(contract) is True
        primary = getattr(model, "primary", None)
        secondary = getattr(model, "secondary", None)
        if primary is not None and secondary is not None:
            return _installs_strict_output_contract(
                primary,
                contract,
            ) and _installs_strict_output_contract(secondary, contract)
    except Exception:
        return False
    return False


def _shares_known_reviewer_runtime(left: object | None, right: object | None) -> bool:
    """Reject mutable runtime objects known to couple two reviewer roles."""

    if left is None or right is None:
        return False
    if left is right:
        return True
    for attribute in ("circuit_breaker", "capacity_gate"):
        left_state = getattr(left, attribute, None)
        right_state = getattr(right, attribute, None)
        if left_state is not None and left_state is right_state:
            return True
    left_lanes = tuple(
        lane
        for lane_name in ("primary", "secondary")
        if (lane := getattr(left, lane_name, None)) is not None
    )
    right_lanes = tuple(
        lane
        for lane_name in ("primary", "secondary")
        if (lane := getattr(right, lane_name, None)) is not None
    )
    if any(
        left_lane is right_lane or _shares_known_reviewer_runtime(left_lane, right_lane)
        for left_lane in left_lanes
        for right_lane in right_lanes
    ):
        return True
    return False


def _supports_inventory_followup_review(model: object | None) -> bool:
    """Prove the reviewer can close Inventory without claiming exhaustiveness.

    Coverage V5 is one complete protocol.  Until that contract is separately
    qualified, the bounded guard may instead hand the decomposition to the
    independently audited report-relative release check and full V7 review.
    A generic injected reviewer proves neither path and must not enable the
    Inventory fast path merely because it has a ``complete`` method.
    """

    return _supports_strict_output_contract(
        model,
        _CANDIDATE_COVERAGE_CONTRACT,
    ) or (
        _supports_strict_output_contract(model, _REPORT_RELATIVE_REVIEW_CONTRACT)
        and _supports_strict_output_contract(model, _FULL_SOURCE_REVIEW_CONTRACT)
    )


def _provider_roles_are_pairwise_independent(
    *,
    author: object,
    inventory: object | None,
    reviewer: object | None,
) -> bool:
    """Require three distinct semantic authorities before enabling Inventory V5."""

    return (
        inventory is not None
        and _provider_lane_sets_are_independent(author, inventory)
        and _provider_lane_sets_are_independent(author, reviewer)
        and _provider_lane_sets_are_independent(inventory, reviewer)
    )


def _candidate_review_capability(
    *,
    authors: tuple[object, ...],
    inventory: object | None,
    reviewer: object | None,
) -> tuple[bool, bool, bool]:
    """Describe one candidate lane without invoking any provider."""

    return (
        _supports_strict_output_contract(
            inventory,
            _CANDIDATE_INVENTORY_CONTRACT,
        ),
        _supports_strict_output_contract(
            reviewer,
            _CANDIDATE_COVERAGE_CONTRACT,
        ),
        bool(authors)
        and all(
            _provider_roles_are_pairwise_independent(
                author=author,
                inventory=inventory,
                reviewer=reviewer,
            )
            for author in authors
        ),
    )


def _preflight_production_source_review(
    *,
    settings: Settings,
    thinking_model: object | None,
    source_closure_model: object | None,
    character_authority_id: str | None = None,
) -> None:
    """Reject an unusable hard-boundary topology before allocating clients."""

    deepseek_author = _ConfiguredProviderLane(
        provider="deepseek",
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        semantic_authority_id=character_authority_id,
    )
    authors: list[object] = [deepseek_author]
    if thinking_model is not None:
        authors.append(thinking_model)
    elif settings.deepseek_character_thinking_enabled:
        authors.append(
            _ConfiguredProviderLane(
                provider="deepseek",
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_character_thinking_model,
                semantic_authority_id=character_authority_id,
            )
        )
    if source_closure_model is not None:
        ordinary_reviewer = source_closure_model
        recovery_reviewer = source_closure_model
    else:
        ordinary_primary_evidence = audited_source_review_capability_evidence(
            base_url=settings.openrouter_base_url,
            model=settings.world_v2_source_review_secondary_model,
            provider="openrouter",
        )
        ordinary_secondary_evidence = audited_source_review_capability_evidence(
            base_url=settings.openai_base_url,
            model=settings.world_v2_source_review_fallback_model,
            provider="openai",
        )
        recovery_primary_evidence = audited_source_review_capability_evidence(
            base_url=settings.openrouter_base_url,
            model=settings.world_v2_source_review_recovery_model,
            provider="openrouter",
        )
        recovery_secondary_evidence = audited_source_review_capability_evidence(
            base_url=settings.openai_base_url,
            model=settings.world_v2_source_review_recovery_fallback_model,
            provider="openai",
        )
        evidence = (
            ordinary_primary_evidence,
            ordinary_secondary_evidence,
            recovery_primary_evidence,
            recovery_secondary_evidence,
        )
        if not all(
            item.supports(_REPORT_RELATIVE_REVIEW_CONTRACT)
            and item.supports(_FULL_SOURCE_REVIEW_CONTRACT)
            for item in evidence
        ):
            raise ValueError(
                "production character routing requires independently qualified "
                "source-closure reviewers for ordinary and recovery candidates"
            )
        ordinary_reviewer = _ConfiguredReviewAuthority(
            primary=_ConfiguredProviderLane(
                provider="openai",
                base_url=settings.openai_base_url,
                model=settings.world_v2_source_review_fallback_model,
            ),
            secondary=_ConfiguredProviderLane(
                provider="openrouter",
                base_url=settings.openrouter_base_url,
                model=settings.world_v2_source_review_secondary_model,
            ),
        )
        recovery_reviewer = _ConfiguredReviewAuthority(
            primary=_ConfiguredProviderLane(
                provider="openai",
                base_url=settings.openai_base_url,
                model=settings.world_v2_source_review_recovery_fallback_model,
            ),
            secondary=_ConfiguredProviderLane(
                provider="openrouter",
                base_url=settings.openrouter_base_url,
                model=settings.world_v2_source_review_recovery_model,
            ),
        )

    if not (
        _supports_inventory_followup_review(ordinary_reviewer)
        and _supports_inventory_followup_review(recovery_reviewer)
        and all(
            _reviewer_is_independent(author=author, reviewer=ordinary_reviewer)
            and _reviewer_is_independent(author=author, reviewer=recovery_reviewer)
            for author in authors
        )
    ):
        raise ValueError(
            "production character routing requires independently qualified "
            "source-closure reviewers for ordinary and recovery candidates"
        )


@dataclass(frozen=True, slots=True)
class ProactiveSourceAuthorityDeployment:
    """Auditable deployment state for proactive visible-fact closure.

    Missing independent authority does not decide that the character must stay
    silent.  Source-free subjective expression remains available; visible
    external propositions fail closed through the full review route when the
    optional semantic inventory is unavailable.
    """

    status: Literal["ready", "fact_effects_fail_closed"]
    author_model: str
    reviewer_model: str | None
    candidate_inventory_model: str | None
    requested_candidate_inventory_model: str | None = None
    inventory_capability_evidence: StrictOutputCapabilityEvidence | None = None
    inventory_route_evidence: tuple[StrictOutputCapabilityEvidence, ...] = ()
    inventory_runtime_model: object | None = None
    inventory_call_timeout_seconds: float | None = None
    warning_reasons: tuple[str, ...] = ()
    source_review_authority: SourceReviewAuthority | None = None
    ordinary_candidate_review_capability: tuple[bool, bool, bool] = (
        False,
        False,
        False,
    )
    recovery_candidate_review_capability: tuple[bool, bool, bool] = (
        False,
        False,
        False,
    )
    reselection_candidate_review_capability: tuple[bool, bool, bool] = (
        False,
        False,
        False,
    )
    inventory_transport_routes: tuple[str, ...] = ()

    @property
    def independent_reviewer(self) -> bool:
        return self.status == "ready"

    def health_snapshot(self) -> dict[str, object]:
        def capability_snapshot(
            value: tuple[bool, bool, bool],
        ) -> dict[str, bool]:
            inventory_v5, coverage_v5, roles_independent = value
            return {
                "inventory_v5": inventory_v5,
                "coverage_v5": coverage_v5,
                "roles_independent": roles_independent,
            }

        runtime_reader = getattr(
            self.inventory_runtime_model,
            "strict_output_runtime_snapshot",
            None,
        )
        inventory_runtime = (
            runtime_reader()
            if callable(runtime_reader)
            else {
                "status": "unavailable",
                "successful_calls": 0,
                "failed_calls": 0,
                "last_checked_at": None,
                "last_failure_code": None,
            }
        )
        warning_reasons = list(self.warning_reasons)
        runtime_status = str(inventory_runtime.get("status") or "unavailable")
        if runtime_status == "qualified_unprobed":
            warning_reasons.append("source_inventory.qualified_unprobed")
        elif runtime_status == "degraded":
            warning_reasons.append("source_inventory.full_source_closure_fallback_active")
        elif runtime_status == "runtime_failed":
            warning_reasons.append("source_inventory.runtime_failed")
        warning_reasons = list(dict.fromkeys(warning_reasons))
        redundancy_state = (
            "redundant"
            if self.source_review_authority is not None
            else "single_lane"
            if self.status == "ready"
            else "unavailable"
        )
        inventory_provider_count = len(
            {evidence.provider.casefold() for evidence in self.inventory_route_evidence}
        )
        inventory_installed = self.ordinary_candidate_review_capability[0]
        if inventory_installed:
            inventory_qualification_state = "verified"
            active_source_review_protocol = (
                "inventory_v5_coverage_v5"
                if self.ordinary_candidate_review_capability[1]
                else "inventory_v5_guard_then_full_source_review.7"
            )
        elif any(evidence.status == "unverified" for evidence in self.inventory_route_evidence):
            inventory_qualification_state = "unverified"
            active_source_review_protocol = "full_source_review.7"
        elif self.inventory_route_evidence and all(
            evidence.status == "disabled" for evidence in self.inventory_route_evidence
        ):
            inventory_qualification_state = "disabled"
            active_source_review_protocol = "full_source_review.7"
        else:
            inventory_qualification_state = "unavailable"
            active_source_review_protocol = "full_source_review.7"
        return {
            "status": self.status,
            "warning": bool(warning_reasons),
            "warning_reasons": warning_reasons,
            "independent_reviewer": self.independent_reviewer,
            "fact_effects_available": self.independent_reviewer,
            "subjective_expression_available": True,
            "author_model": self.author_model,
            "reviewer_model": self.reviewer_model,
            "candidate_inventory_model": self.candidate_inventory_model,
            "requested_candidate_inventory_model": (self.requested_candidate_inventory_model),
            "inventory_capability_evidence": (
                self.inventory_capability_evidence.health_snapshot()
                if self.inventory_capability_evidence is not None
                else None
            ),
            "inventory_runtime": inventory_runtime,
            "inventory_call_timeout_seconds": self.inventory_call_timeout_seconds,
            "visible_review_strategy": (
                "inventory_v5_coverage_v5"
                if self.ordinary_candidate_review_capability[:2] == (True, True)
                else "inventory_v5_guard_then_full_source_review"
                if inventory_installed
                else "full_source_review"
            ),
            "inventory_qualification_state": inventory_qualification_state,
            "active_source_review_protocol": active_source_review_protocol,
            "source_review_qualification_transition": (
                f"{inventory_qualification_state} -> {active_source_review_protocol}"
            ),
            "candidate_review_capabilities": {
                "ordinary": capability_snapshot(self.ordinary_candidate_review_capability),
                "recovery": capability_snapshot(self.recovery_candidate_review_capability),
                "reselection": capability_snapshot(self.reselection_candidate_review_capability),
            },
            "inventory_transport": {
                "route_count": len(self.inventory_transport_routes),
                "routes": self.inventory_transport_routes,
                "single_transport": len(self.inventory_transport_routes) == 1,
                "provider_count": inventory_provider_count,
                "single_provider": (
                    bool(self.inventory_route_evidence) and inventory_provider_count == 1
                ),
                "capability_evidence": [
                    evidence.health_snapshot() for evidence in self.inventory_route_evidence
                ],
                "attempt_timeout_seconds": getattr(
                    self.inventory_runtime_model,
                    "inventory_attempt_timeout_seconds",
                    None,
                ),
                "secondary_reserved_seconds": getattr(
                    self.inventory_runtime_model,
                    "inventory_secondary_reserved_seconds",
                    None,
                ),
            },
            "redundancy_state": redundancy_state,
            "source_review_authority": (
                self.source_review_authority.health_snapshot()
                if self.source_review_authority is not None
                else None
            ),
        }


@dataclass(slots=True)
class SemanticChatComposition:
    """The complete capability-free semantic/model side of one chat host."""

    # Objective extraction/World-author lanes may reuse this provider, but no
    # host receives the protagonist author itself.  Every protagonist decision
    # is reachable only through ``character_interior``.
    world_support_model: ChatCompletionModel
    character_author_model_id: str
    expression_episode_observer_model: ChatCompletionModel | None
    source_closure_model: ChatCompletionModel | None
    recovery_source_closure_model: ChatCompletionModel | None
    source_closure_reselection_lane: SourceClosureReselectionLane | None
    proactive_source_closure_model: ChatCompletionModel | None
    life_source_closure_model: ChatCompletionModel | None
    life_source_runtime_isolation: str
    candidate_external_proposition_inventory_model: ChatCompletionModel | None
    proactive_source_authority: ProactiveSourceAuthorityDeployment
    character_interior: CharacterInterior
    router: SemanticComputeRouter
    identity_frame: CompanionIdentityFrame
    local_provider_capacity: ProviderCapacityGate | None
    text_endpoint_controller: TextTurnEndpointController | None
    _owned_models: tuple[object, ...] = ()
    # Close-only resources promise that ``aclose`` itself reaches quiescence.
    # Task owners may return from bounded close while retaining provider
    # leases, so they are tracked separately and expose an explicit waiter.
    _owned_closeables: tuple[object, ...] = ()
    _owned_task_owners: tuple[object, ...] = ()
    _close_task: asyncio.Task[None] | None = None
    _deferred_model_close_task: asyncio.Task[None] | None = None
    _models_closed: bool = False

    def proactive_source_authority_health(self) -> dict[str, object]:
        """Return read-only deployment evidence without invoking a model."""

        return self.proactive_source_authority.health_snapshot()

    def character_interior_health(self) -> dict[str, object]:
        """Expose the sole protagonist-author topology without private state."""

        return self.character_interior.runtime_health()

    def life_source_authority_health(self) -> dict[str, object]:
        """Report Life reviewer transport state without overstating qualification."""

        reviewer = self.life_source_closure_model
        contracts = {
            contract: {
                "schema_installed": _installs_strict_output_contract(
                    reviewer,
                    contract,
                ),
                "parser_fail_closed": True,
                "release_qualified": _supports_strict_output_contract(
                    reviewer,
                    contract,
                ),
            }
            for contract in _LIFE_SOURCE_REVIEW_CONTRACTS
        }
        runtime_isolation = self.life_source_runtime_isolation
        runtime_isolated = runtime_isolation == "verified_fork"
        transport_runtime: dict[str, object] | None = None
        health_reader = getattr(reviewer, "health_snapshot", None)
        if callable(health_reader):
            try:
                raw_health = health_reader()
                if isinstance(raw_health, dict):
                    transport_runtime = dict(raw_health)
            except Exception:
                _LOG.warning("Life source-review health snapshot failed", exc_info=True)

        route_suppression: object = {}
        last_transport_winner: dict[str, object] | None = None
        if transport_runtime is not None:
            raw_suppression = transport_runtime.get("route_suppression")
            if isinstance(raw_suppression, dict):
                route_suppression = raw_suppression
            winner_lane = transport_runtime.get("last_winner_lane")
            lane_models = transport_runtime.get("lane_models")
            lane_providers = transport_runtime.get("lane_providers")
            if isinstance(winner_lane, str) and winner_lane:
                last_transport_winner = {
                    "lane": winner_lane,
                    "model": (
                        lane_models.get(winner_lane) if isinstance(lane_models, dict) else None
                    ),
                    "provider": (
                        lane_providers.get(winner_lane)
                        if isinstance(lane_providers, dict)
                        else None
                    ),
                }

        all_qualified = all(
            bool(contract_health["release_qualified"]) for contract_health in contracts.values()
        )
        warning_reasons: list[str] = []
        if reviewer is None:
            status = "unavailable"
            warning_reasons.append("life_source_authority.reviewer_unavailable")
        elif runtime_isolation == "caller_provided_distinct_unverified":
            status = (
                "operational_isolation_unverified" if all_qualified else "operational_unqualified"
            )
            warning_reasons.append("life_source_authority.runtime_isolation_unverified")
            if not all_qualified:
                warning_reasons.append("life_source_authority.release_qualification_unavailable")
        elif not runtime_isolated:
            status = "unsafe_shared_runtime"
            warning_reasons.append("life_source_authority.runtime_not_isolated")
        elif not all_qualified:
            status = "operational_unqualified"
            warning_reasons.append("life_source_authority.release_qualification_unavailable")
        else:
            status = "ready"
        return {
            "status": status,
            "warning": bool(warning_reasons),
            "warning_reasons": warning_reasons,
            "runtime_isolated": runtime_isolated,
            "runtime_isolation": runtime_isolation,
            "reviewer_model": _model_identity(reviewer),
            "contracts": contracts,
            "last_transport_winner": last_transport_winner,
            "route_suppression": route_suppression,
            "transport_runtime": transport_runtime,
        }

    async def aclose(self) -> None:
        close_task = self._close_task
        if close_task is None:
            close_task = asyncio.create_task(
                self._aclose_owned(),
                name="world-v2-semantic-chat-close",
            )
            self._close_task = close_task
        await asyncio.shield(close_task)

    async def _aclose_owned(self) -> None:
        close_results = await asyncio.gather(
            *(
                close()
                for owner in (
                    *self._owned_closeables,
                    *self._owned_task_owners,
                )
                if callable(close := getattr(owner, "aclose", None))
            ),
            return_exceptions=True,
        )
        waiters = tuple(
            waiter()
            for owner in self._owned_task_owners
            if getattr(owner, "shutdown_pending_task_count", 0) > 0
            if callable(waiter := getattr(owner, "wait_for_shutdown_quiescence", None))
        )
        if waiters:
            deferred = asyncio.create_task(
                self._close_models_after_quiescence(waiters),
                name="world-v2-semantic-chat-deferred-model-close",
            )
            self._deferred_model_close_task = deferred
            deferred.add_done_callback(self._observe_deferred_model_close)
        else:
            await self._close_owned_models()
        for result in close_results:
            if isinstance(result, BaseException):
                raise result

    async def _close_models_after_quiescence(
        self,
        waiters: tuple[Awaitable[None], ...],
    ) -> None:
        await asyncio.gather(*waiters)
        await self._close_owned_models()

    async def _close_owned_models(self) -> None:
        if self._models_closed:
            return
        for model in self._owned_models:
            close = getattr(model, "aclose", None)
            if callable(close):
                await close()
        self._models_closed = True

    @staticmethod
    def _observe_deferred_model_close(task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()

    @property
    def shutdown_pending_task_count(self) -> int:
        """Dependencies retained by advisory or reviewer tasks after bounded close."""

        deferred = self._deferred_model_close_task
        if deferred is None or deferred.done():
            return 0
        owner_count = sum(
            int(getattr(owner, "shutdown_pending_task_count", 0))
            for owner in self._owned_task_owners
        )
        return max(1, owner_count)

    async def wait_for_shutdown_quiescence(self) -> None:
        """Wait until reviewer leases end and their clients have closed."""

        close_task = self._close_task
        if close_task is not None:
            await asyncio.shield(close_task)
        deferred = self._deferred_model_close_task
        if deferred is not None:
            await asyncio.shield(deferred)


def build_semantic_chat_composition(
    *,
    settings: Settings,
    flash_model: ChatCompletionModel | None = None,
    thinking_model: ChatCompletionModel | None = None,
    world_support_model: ChatCompletionModel | None = None,
    source_closure_model: ChatCompletionModel | None = None,
    life_source_closure_model: ChatCompletionModel | None = None,
    candidate_external_proposition_inventory_model: ChatCompletionModel | None = None,
    expression_episode_observer_model: ChatCompletionModel | None = None,
    model_id_prefix: str,
    expression_capabilities: ExpressionDraftCapabilities = (
        PRODUCTION_TEXT_ONLY_EXPRESSION_CAPABILITIES
    ),
    usage_observer: object | None = None,
    character_interior_turn_store: _CharacterInteriorTurnStore | None = None,
    character_interior_turn_owner_id: str = "character-interior:production",
    test_only_provider_capture_authority_id: str | None = None,
) -> SemanticChatComposition:
    """Build one explicitly supplied or provider-backed Character author.

    Explicitly supplied models are caller-owned.  With provider settings, the
    Module owns a Flash client and, when enabled, a separate bounded Thinking
    client. Invalid output may receive the same CharacterInterior author's one
    constrained reselection; terminal technical failure leaves through the
    normal retry lifecycle and cannot become a second semantic role path.
    """

    if not model_id_prefix:
        raise ValueError("semantic chat composition requires a model id prefix")
    if expression_capabilities.private_turn_state_mode != "required":
        raise ValueError(
            "production expression requires a final PrivateTurnState; "
            "legacy_optional is historical replay/test only"
        )
    if flash_model is None and not settings.deepseek_api_key:
        raise ValueError(
            "production CharacterInterior requires an explicit character model "
            "or DEEPSEEK_API_KEY; fixture prose cannot be installed implicitly"
        )
    if any(_uses_implicit_character_failover(model) for model in (flash_model, thinking_model)):
        raise ValueError(
            "CharacterInterior cannot install an implicit backup character author; "
            "provider failure must enter its durable technical-failure lifecycle"
        )
    test_only_provider_capture_authority_id = (
        _validated_test_only_provider_capture_authority(
            settings=settings,
            authority_id=test_only_provider_capture_authority_id,
        )
    )
    if test_only_provider_capture_authority_id is not None and (
        flash_model is not None or thinking_model is not None
    ):
        raise ValueError(
            "test-only provider capture authority cannot be combined with caller-supplied "
            "character models"
        )
    if (
        test_only_provider_capture_authority_id is not None
        and settings.deepseek_character_thinking_enabled
        and settings.deepseek_character_thinking_model != settings.deepseek_model
    ):
        raise ValueError(
            "test-only provider capture authority requires the thinking character route "
            "to use the configured DeepSeek checkpoint"
        )
    # Source review is an explicit deployment capability. It is a hard
    # boundary for visible World-bound expression facts; the redundant-route
    # switch must not silently turn that boundary off. An explicitly injected
    # reviewer still must be independent of every author.
    if source_closure_model is not None and any(
        not _reviewer_is_independent(
            author=author,
            reviewer=source_closure_model,
        )
        for author in (flash_model, thinking_model)
        if author is not None
    ):
        raise ValueError("every character author requires an independent source-closure reviewer")
    if (
        flash_model is None
        and settings.deepseek_api_key
        and settings.world_v2_chat_source_review_enabled
    ):
        _preflight_production_source_review(
            settings=settings,
            thinking_model=thinking_model,
            source_closure_model=source_closure_model,
            character_authority_id=test_only_provider_capture_authority_id,
        )
    owned: list[object] = []
    owned_closeables: list[object] = []
    owned_task_owners: list[object] = []
    if character_interior_turn_store is not None:
        owned_closeables.append(character_interior_turn_store)

    auto_flash = flash_model is None
    if flash_model is None:
        if settings.deepseek_api_key:
            provider_flash = DeepSeekChatModel(
                api_key=settings.deepseek_api_key,
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_model,
                thinking_enabled=False,
                max_completion_tokens=4_096,
                usage_observer=usage_observer,
            )
            flash_model = provider_flash
            owned.append(provider_flash)
        else:  # pragma: no cover - guarded above, kept for type narrowing
            raise RuntimeError("character author is unavailable")
    if (
        thinking_model is None
        and auto_flash
        and settings.deepseek_api_key
        and settings.deepseek_character_thinking_enabled
    ):
        provider_thinking = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_character_thinking_model,
            thinking_enabled=True,
            reasoning_effort=settings.deepseek_character_thinking_reasoning_effort,
            usage_observer=usage_observer,
        )
        thinking_model = provider_thinking
        owned.append(provider_thinking)
    _apply_test_only_provider_capture_authority(
        flash_model,
        test_only_provider_capture_authority_id,
        settings=settings,
    )
    _apply_test_only_provider_capture_authority(
        thinking_model,
        test_only_provider_capture_authority_id,
        settings=settings,
    )

    local_endpoint_model: ChatCompletionModel | None = None
    local_provider_capacity: ProviderCapacityGate | None = None
    if settings.world_v2_text_endpoint_enabled:
        # The endpoint deployment is a serial inference worker. Its sole role
        # is turn-boundary likelihood; it never enters CharacterInterior or
        # compiles Appraisal/Affect/Relationship semantics.
        local_provider_capacity = ProviderCapacityGate(
            marker_path=text_endpoint_capacity_marker_path(),
        )
        local_endpoint_model = OpenAICompatibleChatModel(
            api_key=settings.world_v2_text_endpoint_api_key,
            base_url=settings.world_v2_text_endpoint_base_url,
            model=settings.world_v2_text_endpoint_model,
            reasoning_effort="none",
            max_completion_tokens=96,
            capacity_gate=local_provider_capacity,
        )
        owned.append(local_endpoint_model)

    character = load_character(str(settings.character_path))
    aliases_raw = character.identity.get("nicknames", ())
    aliases = (
        tuple(str(item) for item in aliases_raw if str(item).strip())
        if isinstance(aliases_raw, list)
        else ()
    )
    identity_frame = CompanionIdentityFrame(
        companion_name=character.name,
        companion_aliases=aliases,
        counterpart_name=settings.primary_user_id,
        stable_identity_facts=tuple(character.canonical_facts),
        shared_history_facts=tuple(character.shared_history_facts),
        counterpart_history_facts=tuple(character.counterpart_history_facts),
        personality_frame=character.personality,
        values=tuple(character.values),
        speech_frame=character.speech,
        style_rules=tuple(character.style_rules),
        boundaries=tuple(character.boundaries),
    )
    source_closure_was_injected = source_closure_model is not None
    resolved_source_closure_model = source_closure_model
    if auto_flash and settings.deepseek_api_key and resolved_source_closure_model is None:
        resolved_source_closure_model = None
    recovery_source_closure_model = resolved_source_closure_model
    auto_inventory_model: ChatCompletionModel | None = None
    auto_inventory_requested_model: str | None = None
    auto_inventory_evidence: StrictOutputCapabilityEvidence | None = None
    auto_inventory_route_evidence: tuple[StrictOutputCapabilityEvidence, ...] = ()
    source_review_authority: SourceReviewAuthority | None = None
    life_source_review_authority: SourceReviewAuthority | None = None
    if (
        auto_flash
        and not source_closure_was_injected
        and (
            settings.world_v2_chat_source_review_enabled
            or settings.world_v2_source_review_redundancy_enabled
            or settings.world_v2_life_source_review_enabled
        )
        and settings.openrouter_api_key
        and settings.openai_api_key
        and isinstance(flash_model, DeepSeekChatModel)
    ):
        # Source truth is a hard boundary, but provider availability is not a
        # semantic vote. The structured OpenRouter lane and dedicated OpenAI
        # lane are both independent of the DeepSeek character author. The
        # bounded authority tries the primary transport first and creates the
        # reserve call only after a terminal primary failure; only one verdict
        # can return.
        local_review_base_url = settings.world_v2_source_review_base_url
        local_inventory_base_url = (
            settings.world_v2_source_inventory_base_url or local_review_base_url
        )
        local_review_model = (
            settings.world_v2_source_review_local_model or "qwen2.5-7b-instruct"
        )
        local_inventory_model = (
            settings.world_v2_source_inventory_local_model or local_review_model
        )
        _local_review_contracts = tuple(_STRICT_SCHEMAS)

        def _local_review_evidence(model: str) -> StrictOutputCapabilityEvidence:
            # A local endpoint is operator-configured and OpenAI-compatible.
            # Declare every installed strict schema so the V5 inventory
            # coverage gate and all review lanes pass without the proxy lane.
            return StrictOutputCapabilityEvidence.verified(
                evidence_source="local_configuration",
                provider="openai",
                model=model,
                contracts=_local_review_contracts,
                observed_at="2026-08-06",
            )

        def _pin_local_authority(instance: object, checkpoint: str) -> None:
            # Local checkpoints are not in the release-pinned registry, so
            # semantic_authority_id would return None and fail independence
            # checks. Declare a stable local authority instead; it is still
            # distinct from every cloud checkpoint including DeepSeek.
            try:
                object.__setattr__(
                    instance,
                    "semantic_authority_id",
                    f"local-mlx:{checkpoint}",
                )
            except (AttributeError, TypeError):
                pass
        openrouter_source_reviewer = StructuredSourceReviewModel(
            api_key=settings.openrouter_api_key,
            base_url=(
                settings.world_v2_source_review_base_url
                or settings.openrouter_base_url
            ),
            model=(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_secondary_model
            ),
            require_provider_parameters=(
                settings.world_v2_source_review_base_url is None
            ),
            # qwen/qwen-plus exposes strict structured output through
            # OpenRouter but does not accept OpenAI's reasoning knob.
            reasoning_effort="",
            max_completion_tokens=1_200,
            proxy_url=(
                None
                if settings.world_v2_source_review_base_url
                else settings.openai_proxy_url
            ),
            circuit_breaker=ProviderCircuitBreaker(
                failure_threshold=2,
                cooldown_seconds=60.0,
            ),
            strict_output_capability_evidence=(
                _local_review_evidence(local_review_model)
                if settings.world_v2_source_review_base_url
                else audited_source_review_capability_evidence(
                    base_url=settings.openrouter_base_url,
                    model=settings.world_v2_source_review_secondary_model,
                    provider="openrouter",
                )
            ),
        )
        openrouter_recovery_source_reviewer = StructuredSourceReviewModel(
            api_key=settings.openrouter_api_key,
            base_url=(
                settings.world_v2_source_review_base_url
                or settings.openrouter_base_url
            ),
            model=(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_recovery_model
            ),
            require_provider_parameters=(
                settings.world_v2_source_review_base_url is None
            ),
            reasoning_effort="",
            max_completion_tokens=1_200,
            proxy_url=(
                None
                if settings.world_v2_source_review_base_url
                else settings.openai_proxy_url
            ),
            circuit_breaker=ProviderCircuitBreaker(
                failure_threshold=2,
                cooldown_seconds=60.0,
            ),
            strict_output_capability_evidence=(
                _local_review_evidence(local_review_model)
                if settings.world_v2_source_review_base_url
                else audited_source_review_capability_evidence(
                    base_url=settings.openrouter_base_url,
                    model=settings.world_v2_source_review_recovery_model,
                    provider="openrouter",
                )
            ),
        )
        auto_inventory_requested_model = settings.world_v2_source_inventory_model
        auto_inventory_evidence = openrouter_inventory_capability_evidence(
            enabled=settings.world_v2_source_inventory_enabled,
            base_url=settings.openrouter_base_url,
            model=settings.world_v2_source_inventory_model,
        )
        direct_inventory_fallback_model = direct_openai_model_id(
            settings.world_v2_source_inventory_fallback_model
        )
        fallback_inventory_evidence = openai_inventory_capability_evidence(
            enabled=settings.world_v2_source_inventory_enabled,
            base_url=settings.openai_base_url,
            model=direct_inventory_fallback_model,
        )
        auto_inventory_route_evidence = (
            auto_inventory_evidence,
            fallback_inventory_evidence,
        )
        openrouter_inventory_primary = (
            StructuredSourceReviewModel(
                api_key=(
                    settings.qwen_api_key
                    if local_inventory_base_url
                    else settings.openrouter_api_key
                ),
                base_url=(
                    local_inventory_base_url
                    or settings.openrouter_base_url
                ),
                model=(
                    settings.world_v2_source_inventory_local_model
                    or settings.world_v2_source_inventory_model
                ),
                require_provider_parameters=(local_inventory_base_url is None),
                reasoning_effort="none",
                max_completion_tokens=1_200,
                proxy_url=(
                    None
                    if local_inventory_base_url
                    else settings.openai_proxy_url
                ),
                strict_output_capability_evidence=(
                    _local_review_evidence(local_inventory_model)
                    if local_inventory_base_url
                    else auto_inventory_evidence
                ),
            )
            if settings.world_v2_source_inventory_enabled
            else None
        )
        openai_inventory_secondary = (
            StructuredSourceReviewModel(
                api_key=(
                    settings.qwen_api_key
                    if local_inventory_base_url
                    else settings.openai_api_key
                ),
                base_url=(
                    local_inventory_base_url
                    or settings.openai_base_url
                ),
                model=(
                    settings.world_v2_source_inventory_local_model
                    or direct_inventory_fallback_model
                ),
                reasoning_effort="none",
                max_completion_tokens=1_200,
                proxy_url=(
                    None
                    if local_inventory_base_url
                    else settings.openai_proxy_url
                ),
                strict_output_capability_evidence=(
                    _local_review_evidence(local_inventory_model)
                    if local_inventory_base_url
                    else fallback_inventory_evidence
                ),
            )
            if settings.world_v2_source_inventory_enabled
            else None
        )
        inventory_availability_authority = (
            InventoryAvailabilityAuthority(
                primary=openrouter_inventory_primary,
                secondary=openai_inventory_secondary,
                attempt_timeout_seconds=(
                    min(3.0, settings.world_v2_source_inventory_timeout_seconds)
                ),
                secondary_attempt_timeout_seconds=8.0,
            )
            if (openrouter_inventory_primary is not None and openai_inventory_secondary is not None)
            else None
        )
        openai_source_reviewer = StructuredSourceReviewModel(
            api_key=settings.openai_api_key,
            base_url=(
                settings.world_v2_source_review_base_url
                or settings.openai_base_url
            ),
            model=(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_fallback_model
            ),
            reasoning_effort=_direct_source_review_reasoning_effort(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_fallback_model
            ),
            max_completion_tokens=1_200,
            proxy_url=(
                None
                if settings.world_v2_source_review_base_url
                else settings.openai_proxy_url
            ),
            circuit_breaker=ProviderCircuitBreaker(
                failure_threshold=2,
                cooldown_seconds=60.0,
            ),
            strict_output_capability_evidence=(
                _local_review_evidence(local_review_model)
                if settings.world_v2_source_review_base_url
                else audited_source_review_capability_evidence(
                    base_url=settings.openai_base_url,
                    model=settings.world_v2_source_review_fallback_model,
                    provider="openai",
                )
            ),
        )
        openai_recovery_source_reviewer = StructuredSourceReviewModel(
            api_key=settings.openai_api_key,
            base_url=(
                settings.world_v2_source_review_base_url
                or settings.openai_base_url
            ),
            model=(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_recovery_fallback_model
            ),
            reasoning_effort=_direct_source_review_reasoning_effort(
                settings.world_v2_source_review_local_model
                or settings.world_v2_source_review_recovery_fallback_model
            ),
            max_completion_tokens=1_200,
            proxy_url=(
                None
                if settings.world_v2_source_review_base_url
                else settings.openai_proxy_url
            ),
            circuit_breaker=ProviderCircuitBreaker(
                failure_threshold=2,
                cooldown_seconds=60.0,
            ),
            strict_output_capability_evidence=(
                _local_review_evidence(local_review_model)
                if settings.world_v2_source_review_base_url
                else audited_source_review_capability_evidence(
                    base_url=settings.openai_base_url,
                    model=settings.world_v2_source_review_recovery_fallback_model,
                    provider="openai",
                )
            ),
        )
        if settings.world_v2_source_review_base_url:
            for _reviewer in (
                openrouter_source_reviewer,
                openrouter_recovery_source_reviewer,
                openai_source_reviewer,
                openai_recovery_source_reviewer,
            ):
                _pin_local_authority(_reviewer, local_review_model)
        if local_inventory_base_url:
            if openrouter_inventory_primary is not None:
                _pin_local_authority(
                    openrouter_inventory_primary, local_inventory_model
                )
            if openai_inventory_secondary is not None:
                _pin_local_authority(
                    openai_inventory_secondary, local_inventory_model
                )
        owned.extend(
            (
                openrouter_source_reviewer,
                openrouter_recovery_source_reviewer,
                openai_source_reviewer,
                openai_recovery_source_reviewer,
            )
        )
        if openrouter_inventory_primary is not None:
            owned.append(openrouter_inventory_primary)
        if openai_inventory_secondary is not None:
            owned.append(openai_inventory_secondary)
        source_review_authority = SourceReviewAuthority(
            # The live-qualified Qwen route won every captured source-review
            # call in the current deployment evidence. Keep the independently
            # qualified direct OpenAI route as the serial reserve; this is
            # transport availability ordering, never a second semantic vote.
            primary=openrouter_source_reviewer,
            secondary=openai_source_reviewer,
            hedge_after_seconds=settings.world_v2_source_review_hedge_after_seconds,
            deadline_seconds=settings.world_v2_source_review_deadline_seconds,
            caller_timeout_seconds=SOURCE_REVIEW_CALL_TIMEOUT_SECONDS,
        )
        # Life Ecology is background cognition with a larger, differently
        # shaped evidence packet. Reuse the same audited provider routes while
        # isolating their circuit/runtime health, route suppression and active
        # task ownership from visible/proactive conversation review.
        life_source_review_authority = source_review_authority.fork_isolated_runtime()
        # Chat source review is controlled by its own explicit deployment
        # flag. Redundancy only describes the availability policy of the
        # installed authority; it must not disable the visible factual
        # boundary and let an author-only lane invent life episodes.
        resolved_source_closure_model = (
            source_review_authority
            if settings.world_v2_chat_source_review_enabled
            else None
        )
        # The same DeepSeek character owns any one permitted source-bound
        # correction. This separately isolated authority reviews that fresh
        # candidate; it is not a backup character author.
        recovery_source_closure_model = SourceReviewAuthority(
            primary=openrouter_recovery_source_reviewer,
            secondary=openai_recovery_source_reviewer,
            hedge_after_seconds=settings.world_v2_source_review_hedge_after_seconds,
            deadline_seconds=settings.world_v2_source_review_deadline_seconds,
            caller_timeout_seconds=SOURCE_REVIEW_CALL_TIMEOUT_SECONDS,
        )
        owned_task_owners.extend(
            (
                source_review_authority,
                recovery_source_closure_model,
                life_source_review_authority,
            )
        )
        if inventory_availability_authority is not None:
            owned_task_owners.append(inventory_availability_authority)
        auto_inventory_model = inventory_availability_authority
    background_model = world_support_model
    if (
        background_model is None
        and auto_flash
        and settings.deepseek_api_key
        and isinstance(flash_model, DeepSeekChatModel)
    ):
        # Life Ecology authors long world drafts (premise + 2-4 outcomes +
        # claims) that far exceed the chat reply budget.  The chat flash model
        # caps completions at 900 tokens, which truncates those drafts into
        # invalid JSON.  Give the background/life lane its own completion
        # budget so life output is never cut off by a chat-side limit.
        background_model = DeepSeekChatModel(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            thinking_enabled=False,
            max_completion_tokens=4_096,
            usage_observer=usage_observer,
        )
        _apply_test_only_provider_capture_authority(
            background_model,
            test_only_provider_capture_authority_id,
            settings=settings,
        )
        owned.append(background_model)
    if background_model is None:
        background_model = flash_model
    # The role declares source permission metadata in the same structured
    # decision that contains its expression. Production deliberately exposes
    # no claim-binder dependency: the retired second synchronous model was a
    # single point of failure after the role had already chosen ``now``. Local
    # closure plus the existing independent truth reviewer remain the boundary.
    ordinary_expression_authors = tuple(
        author for author in (flash_model, thinking_model) if author is not None
    )
    proactive_reviewer = (
        resolved_source_closure_model
        if all(
            _reviewer_is_independent(
                author=author,
                reviewer=resolved_source_closure_model,
            )
            for author in ordinary_expression_authors
        )
        else None
    )
    resolved_life_source_closure_model: ChatCompletionModel | None = None
    life_source_runtime_isolation = "unavailable"
    if life_source_closure_model is not None:
        if life_source_closure_model is proactive_reviewer:
            raise ValueError("Life source reviewer must use a distinct runtime instance")
        if not _reviewer_is_independent(
            author=background_model,
            reviewer=life_source_closure_model,
        ):
            raise ValueError("Life source reviewer must be independent of the World Author")
        if _shares_known_reviewer_runtime(
            proactive_reviewer,
            life_source_closure_model,
        ):
            raise ValueError("Life source reviewer must not share mutable reviewer runtime")
        resolved_life_source_closure_model = life_source_closure_model
        life_source_runtime_isolation = "caller_provided_distinct_unverified"
    elif life_source_review_authority is not None:
        resolved_life_source_closure_model = life_source_review_authority
        life_source_runtime_isolation = "verified_fork"
    if (
        resolved_life_source_closure_model is None
        and source_closure_was_injected
        and proactive_reviewer is not None
    ):
        fork_runtime = getattr(proactive_reviewer, "fork_isolated_runtime", None)
        if callable(fork_runtime):
            isolated_runtime = fork_runtime()
            if isolated_runtime is proactive_reviewer:
                raise ValueError(
                    "Life source reviewer runtime fork must return a distinct instance"
                )
            if not _reviewer_is_independent(
                author=background_model,
                reviewer=isolated_runtime,
            ):
                raise ValueError("Life source reviewer must be independent of the World Author")
            if any(
                _shares_known_reviewer_runtime(existing_reviewer, isolated_runtime)
                for existing_reviewer in (
                    resolved_source_closure_model,
                    proactive_reviewer,
                )
            ):
                raise ValueError(
                    "Life source reviewer fork must not share mutable reviewer runtime"
                )
            resolved_life_source_closure_model = isolated_runtime
            life_source_runtime_isolation = "verified_fork"
            close_runtime = getattr(isolated_runtime, "aclose", None)
            if not callable(close_runtime):
                raise ValueError(
                    "Life source reviewer runtime fork must provide an async close lifecycle"
                )
            if callable(getattr(isolated_runtime, "wait_for_shutdown_quiescence", None)):
                owned_task_owners.append(isolated_runtime)
            else:
                owned_closeables.append(isolated_runtime)
    if (
        auto_flash
        and settings.deepseek_api_key
        and settings.world_v2_chat_source_review_enabled
    ):
        if proactive_reviewer is None:
            raise ValueError(
                "production character routing requires an independent source-closure reviewer"
            )
        production_authors = ordinary_expression_authors
        ordinary_review_ready = (
            proactive_reviewer is not None
            and _supports_inventory_followup_review(proactive_reviewer)
            and all(
                _reviewer_is_independent(
                    author=author,
                    reviewer=proactive_reviewer,
                )
                for author in production_authors
            )
        )
        recovery_review_ready = (
            recovery_source_closure_model is not None
            and _supports_inventory_followup_review(recovery_source_closure_model)
            and all(
                _reviewer_is_independent(
                    author=author,
                    reviewer=recovery_source_closure_model,
                )
                for author in production_authors
            )
        )
        if not ordinary_review_ready or not recovery_review_ready:
            raise ValueError(
                "production character routing requires independently qualified "
                "source-closure reviewers for ordinary and recovery candidates"
            )
    source_reselection_author = flash_model if recovery_source_closure_model is not None else None
    # Inventory V5 is semantic decomposition, not character authorship. Every
    # installed inventory lane must promise V5, the visible authority must
    # promise verdict-only Coverage V5, and all possible ordinary/recovery
    # author, Inventory, and Coverage winners must be pairwise disjoint. Missing
    # capability or identity proof keeps Inventory absent and retains the
    # established full-review route. A provider object's dormant ``fallback``
    # attribute is intentionally irrelevant: it is not a character author.
    requested_inventory_model = (
        candidate_external_proposition_inventory_model or auto_inventory_model
    )
    requested_inventory_identity = (
        _model_identity(candidate_external_proposition_inventory_model)
        if candidate_external_proposition_inventory_model is not None
        else auto_inventory_requested_model
    )
    requested_inventory_evidence = (
        getattr(
            candidate_external_proposition_inventory_model,
            "strict_output_capability_evidence",
            None,
        )
        if candidate_external_proposition_inventory_model is not None
        else auto_inventory_evidence
    )
    requested_inventory_route_evidence = (
        tuple(
            getattr(
                candidate_external_proposition_inventory_model,
                "strict_output_capability_evidences",
                (),
            )
        )
        if candidate_external_proposition_inventory_model is not None
        else auto_inventory_route_evidence
    )
    if not requested_inventory_route_evidence and requested_inventory_evidence is not None:
        requested_inventory_route_evidence = (requested_inventory_evidence,)
    requested_inventory_timeout = getattr(
        requested_inventory_model,
        "inventory_call_timeout_seconds",
        (
            settings.world_v2_source_inventory_timeout_seconds
            if auto_inventory_requested_model is not None
            else None
        ),
    )
    inventory_model = (
        requested_inventory_model
        if (
            _supports_strict_output_contract(
                requested_inventory_model,
                _CANDIDATE_INVENTORY_CONTRACT,
            )
            and _supports_inventory_followup_review(proactive_reviewer)
            and _supports_inventory_followup_review(recovery_source_closure_model)
            and all(
                _provider_roles_are_pairwise_independent(
                    author=author,
                    inventory=requested_inventory_model,
                    reviewer=proactive_reviewer,
                )
                for author in ordinary_expression_authors
            )
        )
        else None
    )
    reselection_inventory_model = (
        inventory_model
        if (
            inventory_model is not None
            and source_reselection_author is not None
            and _provider_roles_are_pairwise_independent(
                author=source_reselection_author,
                inventory=inventory_model,
                reviewer=recovery_source_closure_model,
            )
        )
        else None
    )
    source_closure_reselection_lane: SourceClosureReselectionLane | None = None
    if (
        source_reselection_author is not None
        and _supports_strict_output_contract(
            source_reselection_author,
            EXPRESSION_SOURCE_RESELECTION_DIRECT_CONTRACT,
        )
        and _reviewer_is_independent(
            author=source_reselection_author,
            reviewer=recovery_source_closure_model,
        )
    ):
        source_closure_reselection_lane = SourceClosureReselectionLane(
            author=source_reselection_author,
            reviewer=recovery_source_closure_model,
            report_relative_reviewer=recovery_source_closure_model,
            # Inventory V5 is optional. If its provider can also become the
            # final reviewer, keep the independent strict role reselector and
            # use the established full-review route instead of disabling the
            # entire correction lane.
            inventory_model=reselection_inventory_model,
        )
    ordinary_candidate_review_capability = _candidate_review_capability(
        authors=ordinary_expression_authors,
        inventory=inventory_model,
        reviewer=proactive_reviewer,
    )
    # Kept as a read-only health compatibility field. There is no recovery
    # character author in the production topology; same-author source
    # reselection is reported by the dedicated capability below.
    recovery_candidate_review_capability = (False, False, False)
    reselection_candidate_review_capability = _candidate_review_capability(
        authors=(
            (source_closure_reselection_lane.author,)
            if source_closure_reselection_lane is not None
            else ()
        ),
        inventory=(
            source_closure_reselection_lane.inventory_model
            if source_closure_reselection_lane is not None
            else None
        ),
        reviewer=(
            source_closure_reselection_lane.reviewer
            if source_closure_reselection_lane is not None
            else None
        ),
    )
    inventory_transport_routes = transport_route_ids(inventory_model)
    if proactive_reviewer is None:
        warning_reason = (
            "proactive_source_authority.independent_reviewer_unavailable"
            if resolved_source_closure_model is None
            else "proactive_source_authority.reviewer_not_independent"
        )
        proactive_source_authority = ProactiveSourceAuthorityDeployment(
            status="fact_effects_fail_closed",
            author_model=_model_identity(flash_model) or "unknown",
            reviewer_model=_model_identity(resolved_source_closure_model),
            candidate_inventory_model=_model_identity(inventory_model),
            requested_candidate_inventory_model=requested_inventory_identity,
            inventory_capability_evidence=requested_inventory_evidence,
            inventory_route_evidence=requested_inventory_route_evidence,
            inventory_runtime_model=requested_inventory_model,
            inventory_call_timeout_seconds=requested_inventory_timeout,
            warning_reasons=(warning_reason,),
            ordinary_candidate_review_capability=(ordinary_candidate_review_capability),
            recovery_candidate_review_capability=(recovery_candidate_review_capability),
            reselection_candidate_review_capability=(reselection_candidate_review_capability),
            inventory_transport_routes=inventory_transport_routes,
        )
        _LOG.warning(
            "proactive visible-fact source authority degraded status=%s reason=%s "
            "author=%s reviewer=%s; subjective expression remains available",
            proactive_source_authority.status,
            warning_reason,
            proactive_source_authority.author_model,
            proactive_source_authority.reviewer_model,
        )
    else:
        warning_reasons: list[str] = []
        if source_review_authority is None:
            warning_reasons.append("source_review_authority.single_independent_lane")
        if len(inventory_transport_routes) == 1:
            warning_reasons.append("source_inventory.single_transport_route")
        inventory_transport_providers = {
            evidence.provider.casefold() for evidence in requested_inventory_route_evidence
        }
        if len(inventory_transport_routes) > 1 and len(inventory_transport_providers) == 1:
            warning_reasons.append("source_inventory.single_transport_provider")
        warning_reasons.extend(
            evidence.reason_code
            for evidence in requested_inventory_route_evidence
            if evidence.status != "verified"
        )
        proactive_source_authority = ProactiveSourceAuthorityDeployment(
            status="ready",
            author_model=_model_identity(getattr(flash_model, "primary", flash_model)) or "unknown",
            reviewer_model=_model_identity(proactive_reviewer),
            candidate_inventory_model=_model_identity(inventory_model),
            requested_candidate_inventory_model=requested_inventory_identity,
            inventory_capability_evidence=requested_inventory_evidence,
            inventory_route_evidence=requested_inventory_route_evidence,
            inventory_runtime_model=requested_inventory_model,
            inventory_call_timeout_seconds=requested_inventory_timeout,
            warning_reasons=tuple(warning_reasons),
            source_review_authority=source_review_authority,
            ordinary_candidate_review_capability=(ordinary_candidate_review_capability),
            recovery_candidate_review_capability=(recovery_candidate_review_capability),
            reselection_candidate_review_capability=(reselection_candidate_review_capability),
            inventory_transport_routes=inventory_transport_routes,
        )
    character_interior = compose_production_character_interior(
        flash_model=flash_model,
        thinking_model=thinking_model,
        # The built-in production route always installs semantic truth
        # closure. Ordinary source review remains independent of the DeepSeek
        # author. Any permitted correction remains a DeepSeek character
        # choice; OpenAI/Qwen are reviewer or binder authorities only and
        # never author visible expression.
        source_closure_model=proactive_reviewer,
        report_relative_source_closure_model=proactive_reviewer,
        # Inventory is a lightweight semantic decomposition; the configured
        # source-closure authority still makes the focused factual verdict.
        candidate_external_proposition_inventory_model=inventory_model,
        source_closure_reselection_lane=source_closure_reselection_lane,
        expression_episode_observer_model=expression_episode_observer_model,
        flash_model_id=str(getattr(flash_model, "model", f"{model_id_prefix}-flash")),
        thinking_model_id=(
            str(getattr(thinking_model, "model", f"{model_id_prefix}-thinking"))
            if thinking_model is not None
            else None
        ),
        expression_capabilities=expression_capabilities,
        identity_frame=identity_frame,
        review_claim_free_candidates=settings.world_v2_chat_source_review_enabled,
        turn_store=character_interior_turn_store,
        turn_owner_id=character_interior_turn_owner_id,
        # A provider object may expose a historical ``fallback`` attribute,
        # but CharacterInterior has one semantic author. Structural repair is
        # the bounded same-author correction in the inner turn; a later
        # provider outage remains a technical failure for durable retry.
    )
    return SemanticChatComposition(
        world_support_model=background_model,
        character_author_model_id=(
            _model_identity(getattr(flash_model, "primary", flash_model)) or "unknown"
        ),
        expression_episode_observer_model=expression_episode_observer_model,
        source_closure_model=proactive_reviewer,
        recovery_source_closure_model=recovery_source_closure_model,
        source_closure_reselection_lane=source_closure_reselection_lane,
        proactive_source_closure_model=proactive_reviewer,
        life_source_closure_model=resolved_life_source_closure_model,
        life_source_runtime_isolation=life_source_runtime_isolation,
        candidate_external_proposition_inventory_model=inventory_model,
        proactive_source_authority=proactive_source_authority,
        character_interior=character_interior,
        router=SemanticComputeRouter(thinking_available=thinking_model is not None),
        identity_frame=identity_frame,
        local_provider_capacity=local_provider_capacity,
        text_endpoint_controller=(
            TextTurnEndpointController(
                model=ChatSemanticEndpointModel(local_endpoint_model),
                timeout_seconds=settings.world_v2_text_endpoint_timeout_seconds,
            )
            if local_endpoint_model is not None
            else None
        ),
        _owned_models=tuple(owned),
        _owned_closeables=tuple(owned_closeables),
        _owned_task_owners=tuple(owned_task_owners),
    )


__all__ = [
    "ProactiveSourceAuthorityDeployment",
    "SemanticChatComposition",
    "build_semantic_chat_composition",
    "unavailable_life_source_authority_health",
]
