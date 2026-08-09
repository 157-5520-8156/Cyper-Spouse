import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import (
    FailoverChatModel,
    FakeCompanionModel,
    OpenAICompatibleChatModel,
    ProviderCapacityGate,
    ProviderCircuitBreaker,
)
from companion_daemon.world_v2.semantic_chat_composition import (
    build_semantic_chat_composition,
)
from companion_daemon.world_v2.character_interior.inbound_wire import (
    SourceClosureReselectionLane,
)
from companion_daemon.world_v2.model_authority_identity import (
    provider_lane_sets_are_independent,
    semantic_authority_id,
    transport_route_id,
)
from companion_daemon.world_v2.source_review_authority import (
    SourceReviewAttemptsExhausted,
    SourceReviewAuthority,
)
from companion_daemon.world_v2.structured_source_review_model import (
    InventoryAvailabilityAuthority,
    StructuredSourceReviewModel,
)


class _InjectedModel:
    def __init__(self, model: str) -> None:
        self.model = model
        # Composition fakes declare checkpoint authority explicitly. A model
        # display name alone is deliberately insufficient in production.
        self.semantic_authority_id = f"semantic-authority:test:{model.casefold()}"
        self.closed = False

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        del messages, temperature
        return "{}"

    async def aclose(self) -> None:
        self.closed = True


class _ForkableInjectedReviewer(_InjectedModel):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.forks: list[_ForkableInjectedReviewer] = []

    def fork_isolated_runtime(self) -> "_ForkableInjectedReviewer":
        fork = _ForkableInjectedReviewer(self.model)
        self.forks.append(fork)
        return fork

    async def wait_for_shutdown_quiescence(self) -> None:
        return None


class _SharedCircuitForkableInjectedReviewer(_InjectedModel):
    """A dishonest fork seam returning a new wrapper over shared failure state."""

    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.circuit_breaker = ProviderCircuitBreaker(
            failure_threshold=2,
            cooldown_seconds=60,
        )

    def fork_isolated_runtime(self) -> _InjectedModel:
        fork = _InjectedModel(self.model)
        fork.circuit_breaker = self.circuit_breaker
        return fork


class _CloseOnlyForkableInjectedReviewer(_InjectedModel):
    def __init__(self, model: str) -> None:
        super().__init__(model)
        self.forks: list[_InjectedModel] = []

    def fork_isolated_runtime(self) -> _InjectedModel:
        fork = _InjectedModel(self.model)
        self.forks.append(fork)
        return fork


class _UncloseableInjectedReviewer:
    def __init__(self, model: str) -> None:
        self.model = model
        self.semantic_authority_id = f"semantic-authority:test:{model.casefold()}"

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> str:
        del messages, temperature
        return "{}"


class _UncloseableForkableInjectedReviewer(_InjectedModel):
    def fork_isolated_runtime(self) -> _UncloseableInjectedReviewer:
        return _UncloseableInjectedReviewer(self.model)


class _StrictInventoryInjectedModel(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-inventory.5"


class _StrictCoverageInjectedModel(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"


class _StrictInventoryAndCoverageInjectedModel(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "candidate-external-proposition-inventory.5",
            "candidate-external-proposition-coverage.5",
        }


@pytest.mark.asyncio
async def test_remote_production_composes_without_reviewer_when_redundancy_is_disabled() -> None:
    """The unsafe author-only route is available only by explicit opt-out."""

    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=False,
            WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=False,
        ),
        model_id_prefix="test",
    )
    await composition.aclose()


@pytest.mark.asyncio
async def test_remote_production_keeps_chat_source_review_when_redundancy_is_disabled() -> None:
    """A single review lane still guards visible World-bound expression facts."""

    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=False,
            WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    assert isinstance(composition.source_closure_model, SourceReviewAuthority)
    assert composition.proactive_source_authority_health()["status"] == "ready"
    await composition.aclose()


def test_remote_production_rejects_unqualified_redundant_review_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two independent transports remain unusable without exact contract audits."""

    created_clients: list[object] = []

    class _TrackingClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            created_clients.append(self)

        async def aclose(self) -> None:
            return None

        async def post(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("preflight must not reach a provider")

    monkeypatch.setattr(httpx, "AsyncClient", _TrackingClient)

    with pytest.raises(
        ValueError,
        match="independently qualified source-closure reviewers for ordinary and recovery",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
                OPENAI_API_KEY="openai-test-key",
                OPENROUTER_API_KEY="openrouter-test-key",
                WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
                WORLD_V2_SOURCE_REVIEW_SECONDARY_MODEL="openai/gpt-5.4-mini",
                WORLD_V2_SOURCE_REVIEW_FALLBACK_MODEL="gpt-5.4-nano",
                WORLD_V2_SOURCE_REVIEW_RECOVERY_MODEL="openai/gpt-5.4-mini",
                WORLD_V2_SOURCE_REVIEW_RECOVERY_FALLBACK_MODEL=("gpt-5.4-nano"),
            ),
            model_id_prefix="test",
        )

    assert created_clients == []


@pytest.mark.asyncio
async def test_world_v2_composition_uses_one_character_provider_and_independent_reviewers() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        OPENAI_PROXY_URL="http://127.0.0.1:7890",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.character_author_model_id == "deepseek-v4-flash"
    assert isinstance(composition.source_closure_model, SourceReviewAuthority)
    assert composition.proactive_source_authority_health()["author_model"] == "deepseek-v4-flash"
    assert composition.source_closure_model.supports_strict_output_contract(
        "report-relative-entailment-adjudication.3"
    )
    assert composition.source_closure_model.supports_strict_output_contract(
        "source-closure-review.7"
    )
    assert isinstance(composition.recovery_source_closure_model, SourceReviewAuthority)
    assert composition.recovery_source_closure_model.supports_strict_output_contract(
        "report-relative-entailment-adjudication.3"
    )
    assert composition.recovery_source_closure_model.supports_strict_output_contract(
        "source-closure-review.7"
    )
    assert composition.source_closure_reselection_lane is None
    assert composition.proactive_source_authority_health()["status"] == "ready"
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_composition_has_no_backup_character_author() -> None:

    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.source_closure_reselection_lane is None
    assert composition.expression_episode_observer_model is None
    assert (
        composition.character_interior.runtime_health()["parallel_character_author_conflicts"] == 0
    )
    await composition.aclose()


@pytest.mark.asyncio
async def test_shadow_composition_does_not_install_a_backup_character_observer() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.character_author_model_id == "deepseek-v4-flash"
    assert composition.expression_episode_observer_model is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_author_does_not_implicitly_enable_shadow_observer() -> None:
    author = _InjectedModel("injected-author")
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            OPENAI_API_KEY="openai-test-key",
            WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        ),
        flash_model=author,
        model_id_prefix="test",
    )

    assert composition.expression_episode_observer_model is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_shadow_observer_remains_caller_owned() -> None:
    author = _InjectedModel("injected-author")
    observer = _InjectedModel("injected-observer")
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            WORLD_V2_EXPRESSION_EPISODE_MODE="shadow",
        ),
        flash_model=author,
        expression_episode_observer_model=observer,
        model_id_prefix="test",
    )

    assert composition.expression_episode_observer_model is observer
    await composition.aclose()
    assert author.closed is False
    assert observer.closed is False


@pytest.mark.asyncio
async def test_production_composition_keeps_unverified_inventory_out_of_every_candidate() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENAI_PROXY_URL="http://127.0.0.1:7890",
        OPENROUTER_API_KEY="openrouter-test-key",
        OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
        WORLD_V2_SOURCE_INVENTORY_MODEL="nousresearch/hermes-test-inventory",
        WORLD_V2_SOURCE_REVIEW_SECONDARY_MODEL="qwen/qwen-plus",
        WORLD_V2_SOURCE_REVIEW_FALLBACK_MODEL="gpt-4.1-mini",
        WORLD_V2_SOURCE_REVIEW_RECOVERY_MODEL="qwen/qwen-plus",
        WORLD_V2_SOURCE_REVIEW_RECOVERY_FALLBACK_MODEL="gpt-4.1-mini",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SOURCE_REVIEW_HEDGE_AFTER_SECONDS=3.5,
        WORLD_V2_SOURCE_REVIEW_DEADLINE_SECONDS=19.0,
        WORLD_V2_SOURCE_INVENTORY_TIMEOUT_SECONDS=5.0,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    main_authority = composition.source_closure_model
    assert isinstance(main_authority, SourceReviewAuthority)
    assert isinstance(main_authority.primary, StructuredSourceReviewModel)
    assert main_authority.primary.model == "gpt-4.1-mini"
    assert main_authority.primary.base_url == "https://api.openai.com/v1"
    assert main_authority.primary.proxy_url == "http://127.0.0.1:7890"
    assert isinstance(main_authority.secondary, OpenAICompatibleChatModel)
    assert main_authority.secondary.model == "qwen/qwen-plus"
    assert main_authority.secondary.reasoning_effort == ""
    assert main_authority.hedge_after_seconds == 3.5
    assert main_authority.deadline_seconds == 19.0
    assert composition.proactive_source_closure_model is main_authority
    inventory = composition.candidate_external_proposition_inventory_model
    assert inventory is None

    life_authority = composition.life_source_closure_model
    assert isinstance(life_authority, SourceReviewAuthority)
    assert life_authority is not main_authority
    assert life_authority.primary is not main_authority.primary
    assert life_authority.secondary is not main_authority.secondary
    assert life_authority.primary.client is main_authority.primary.client
    assert life_authority.secondary.client is main_authority.secondary.client
    assert life_authority.primary.circuit_breaker is not main_authority.primary.circuit_breaker
    assert life_authority.secondary.circuit_breaker is not main_authority.secondary.circuit_breaker
    assert life_authority in composition._owned_task_owners
    # A background Life timeout must not suppress the interactive/proactive
    # fact-review route that happens to use the same configured providers.
    life_authority._after_lane_failure("primary", "provider_timeout")
    life_health = life_authority.health_snapshot()
    interactive_health = main_authority.health_snapshot()
    assert life_health["route_suppression"]["primary"]["active"] is True
    assert interactive_health["route_suppression"]["primary"] == {
        "active": False,
        "reason": None,
        "retry_after_seconds": 0.0,
        "skipped_calls": 0,
    }
    # Life schemas are locally installed and parser-enforced, but they are not
    # silently upgraded to release-qualified strict-route evidence.
    assert (
        life_authority.supports_strict_output_contract("life-development-source-closure-review.1")
        is False
    )
    assert (
        life_authority.supports_strict_output_contract("life-development-novel-origin-review.2")
        is False
    )
    deployment_health = composition.life_source_authority_health()
    assert deployment_health["status"] == "operational_unqualified"
    assert deployment_health["warning"] is True
    assert deployment_health["runtime_isolated"] is True
    assert deployment_health["contracts"] == {
        "life-development-source-closure-review.1": {
            "schema_installed": True,
            "parser_fail_closed": True,
            "release_qualified": False,
        },
        "life-development-novel-origin-review.2": {
            "schema_installed": True,
            "parser_fail_closed": True,
            "release_qualified": False,
        },
    }
    assert deployment_health["last_transport_winner"] is None
    assert deployment_health["route_suppression"]["primary"]["active"] is True

    recovery_authority = composition.recovery_source_closure_model
    assert isinstance(recovery_authority, SourceReviewAuthority)
    assert recovery_authority is not main_authority
    assert recovery_authority.primary is not main_authority.primary
    assert recovery_authority.primary.model == "gpt-4.1-mini"
    assert recovery_authority.primary.provider == "openai"
    assert isinstance(recovery_authority.secondary, StructuredSourceReviewModel)
    assert recovery_authority.secondary.model == "qwen/qwen-plus"
    assert (
        recovery_authority.supports_strict_output_contract(
            "candidate-external-proposition-coverage.5"
        )
        is False
    )
    reselection_lane = composition.source_closure_reselection_lane
    assert reselection_lane is None

    health = composition.proactive_source_authority_health()
    assert health["status"] == "ready"
    assert health["redundancy_state"] == "redundant"
    assert health["visible_review_strategy"] == "full_source_review"
    assert health["candidate_review_capabilities"] == {
        "ordinary": {
            "inventory_v5": False,
            "coverage_v5": False,
            "roles_independent": False,
        },
        "recovery": {
            "inventory_v5": False,
            "coverage_v5": False,
            "roles_independent": False,
        },
        "reselection": {
            "inventory_v5": False,
            "coverage_v5": False,
            "roles_independent": False,
        },
    }
    assert health["candidate_inventory_model"] is None
    assert health["requested_candidate_inventory_model"] == ("nousresearch/hermes-test-inventory")
    assert health["inventory_capability_evidence"]["status"] == "unverified"
    assert health["inventory_call_timeout_seconds"] == 11.75
    assert health["inventory_transport"]["route_count"] == 0
    assert health["inventory_transport"]["single_transport"] is False
    assert health["warning"] is True
    assert "source_inventory.strict_output_capability_unverified" in health["warning_reasons"]
    assert health["source_review_authority"]["configured_lanes"] == (
        "primary",
        "secondary",
    )
    assert health["source_review_authority"]["lane_models"] == {
        "primary": "gpt-4.1-mini",
        "secondary": "qwen/qwen-plus",
    }
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_composition_does_not_install_unverified_openrouter_inventory() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
        WORLD_V2_SOURCE_INVENTORY_ENABLED=True,
        WORLD_V2_SOURCE_INVENTORY_MODEL="nousresearch/hermes-4-70b",
        WORLD_V2_SOURCE_REVIEW_SECONDARY_MODEL="qwen/qwen-plus",
        WORLD_V2_SOURCE_REVIEW_FALLBACK_MODEL="gpt-4.1-mini",
        WORLD_V2_SOURCE_REVIEW_RECOVERY_MODEL="qwen/qwen-plus",
        WORLD_V2_SOURCE_REVIEW_RECOVERY_FALLBACK_MODEL="gpt-4.1-mini",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    health = composition.proactive_source_authority_health()
    assert health["status"] == "ready"
    assert health["visible_review_strategy"] == "full_source_review"
    assert health["candidate_inventory_model"] is None
    assert health["requested_candidate_inventory_model"] == ("nousresearch/hermes-4-70b")
    assert health["inventory_capability_evidence"] == {
        "status": "unverified",
        "evidence_source": "openrouter_model_metadata",
        "reason_code": "source_inventory.structured_outputs_not_advertised",
        "provider": "openrouter",
        "model": "nousresearch/hermes-4-70b",
        "contracts": (),
        "observed_at": "2026-08-01",
        "qualified_at": None,
        "evidence_revision": None,
        "audit_sample_count": None,
        "audit_success_count": None,
        "contract_schema_digests": {},
    }
    assert health["inventory_call_timeout_seconds"] == 11.75
    assert "source_inventory.structured_outputs_not_advertised" in health["warning_reasons"]
    assert health["candidate_review_capabilities"]["ordinary"]["inventory_v5"] is False
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_composition_installs_inventory_guard_with_qualified_reserve() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    assert isinstance(
        composition.candidate_external_proposition_inventory_model,
        InventoryAvailabilityAuthority,
    )
    requested_inventory = composition.proactive_source_authority.inventory_runtime_model
    assert isinstance(requested_inventory, InventoryAvailabilityAuthority)
    assert composition.candidate_external_proposition_inventory_model is requested_inventory
    assert requested_inventory.primary.model == "openai/gpt-5.4-nano"
    assert requested_inventory.secondary.model == "gpt-5.4-mini"
    assert requested_inventory.primary.reasoning_effort == "none"
    assert requested_inventory.secondary.reasoning_effort == "none"
    assert requested_inventory.inventory_attempt_timeout_seconds == 3.0
    assert requested_inventory.inventory_secondary_reserved_seconds == 8.0
    assert requested_inventory.inventory_call_timeout_seconds == 11.75
    assert requested_inventory.primary.circuit_breaker is None
    assert requested_inventory.secondary.circuit_breaker is None
    health = composition.proactive_source_authority_health()
    assert health["visible_review_strategy"] == ("inventory_v5_guard_then_full_source_review")
    assert health["inventory_qualification_state"] == "verified"
    assert health["active_source_review_protocol"] == (
        "inventory_v5_guard_then_full_source_review.7"
    )
    assert health["source_review_qualification_transition"] == (
        "verified -> inventory_v5_guard_then_full_source_review.7"
    )
    assert health["candidate_review_capabilities"]["ordinary"] == {
        "inventory_v5": True,
        "coverage_v5": False,
        "roles_independent": True,
    }
    evidence = health["inventory_capability_evidence"]
    assert evidence["status"] == "verified"
    assert evidence["evidence_revision"] == ("inventory-v5-openrouter-gpt54nano-20260801.1")
    assert evidence["audit_sample_count"] == 14
    assert evidence["audit_success_count"] == 13
    assert evidence["qualified_at"] == "2026-08-01"
    assert evidence["contract_schema_digests"] == {
        "candidate-external-proposition-inventory.5": (
            "cd55ce09687b5b4e68b1a6805244f76e9c43d4e286b3bee5bb183715a38519fb"
        )
    }
    assert health["inventory_runtime"]["status"] == "qualified_unprobed"
    assert health["inventory_runtime"]["last_winner_lane"] is None
    assert set(health["inventory_runtime"]["lanes"]) == {"primary", "secondary"}
    assert health["inventory_runtime"]["route_rejection_cooldown_seconds"] == 600.0
    assert health["inventory_runtime"]["provider_timeout_cooldown_seconds"] == 600.0
    assert "source_inventory.contract_response_unverified" not in health["warning_reasons"]
    assert health["inventory_transport"]["route_count"] == 2
    assert health["inventory_transport"]["provider_count"] == 2
    assert health["inventory_transport"]["single_provider"] is False
    assert health["inventory_transport"]["attempt_timeout_seconds"] == 3.0
    assert health["inventory_transport"]["secondary_reserved_seconds"] == 8.0
    route_evidence = health["inventory_transport"]["capability_evidence"]
    assert [item["model"] for item in route_evidence] == [
        "openai/gpt-5.4-nano",
        "gpt-5.4-mini",
    ]
    assert [item["provider"] for item in route_evidence] == [
        "openrouter",
        "openai",
    ]
    assert route_evidence[1]["status"] == "verified"
    assert route_evidence[1]["evidence_source"] == "production_contract_audit"
    assert route_evidence[1]["reason_code"] == ("strict_output.endpoint_capability_verified")
    assert route_evidence[1]["evidence_revision"] == ("inventory-v5-openai-gpt54mini-20260801.2")
    assert route_evidence[1]["audit_sample_count"] == 12
    assert route_evidence[1]["audit_success_count"] == 11
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_inventory_can_be_disabled_without_weakening_full_review() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
            WORLD_V2_SOURCE_INVENTORY_ENABLED=False,
        ),
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    health = composition.proactive_source_authority_health()
    assert health["status"] == "ready"
    assert health["visible_review_strategy"] == "full_source_review"
    assert health["inventory_capability_evidence"]["status"] == "disabled"
    assert "source_inventory.disabled_by_configuration" in health["warning_reasons"]
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_source_closure_injection_is_not_replaced_by_auto_wiring() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
        ),
        flash_model=author,
        source_closure_model=reviewer,
        model_id_prefix="test",
    )

    assert composition.character_author_model_id == "injected-author"
    assert composition.source_closure_model is reviewer
    assert composition.proactive_source_closure_model is reviewer
    assert composition.life_source_closure_model is None
    unavailable_health = composition.life_source_authority_health()
    assert unavailable_health["runtime_isolation"] == "unavailable"
    assert all(
        contract["schema_installed"] is False
        for contract in unavailable_health["contracts"].values()
    )
    # A full-review injection does not silently acquire a second wire
    # responsibility. Without explicit Inventory V5 and Coverage V5 support,
    # production retains the full source-review boundary instead of sending
    # either strict contract to a generic model.
    assert composition.candidate_external_proposition_inventory_model is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_life_source_reviewer_uses_a_distinct_runtime_seam() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    life_reviewer = _InjectedModel("injected-life-reviewer")

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        life_source_closure_model=life_reviewer,
        model_id_prefix="test",
    )

    assert composition.proactive_source_closure_model is reviewer
    assert composition.life_source_closure_model is life_reviewer
    life_health = composition.life_source_authority_health()
    assert life_health["runtime_isolated"] is False
    assert life_health["runtime_isolation"] == ("caller_provided_distinct_unverified")
    assert all(
        contract["schema_installed"] is False for contract in life_health["contracts"].values()
    )
    await composition.aclose()


def test_explicit_life_source_reviewer_rejects_a_shared_runtime() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")

    with pytest.raises(ValueError, match="distinct runtime instance"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=reviewer,
            life_source_closure_model=reviewer,
            model_id_prefix="test",
        )


def test_explicit_life_source_reviewer_rejects_a_shared_circuit() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    life_reviewer = _InjectedModel("injected-life-reviewer")
    shared_circuit = ProviderCircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    reviewer.circuit_breaker = shared_circuit
    life_reviewer.circuit_breaker = shared_circuit

    with pytest.raises(ValueError, match="share mutable reviewer runtime"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=reviewer,
            life_source_closure_model=life_reviewer,
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_explicit_reviewer_fork_is_owned_as_the_life_runtime() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _ForkableInjectedReviewer("forkable-reviewer")

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        model_id_prefix="test",
    )

    assert len(reviewer.forks) == 1
    life_reviewer = reviewer.forks[0]
    assert composition.life_source_closure_model is life_reviewer
    assert life_reviewer.closed is False

    await composition.aclose()

    assert life_reviewer.closed is True
    assert reviewer.closed is False


def test_reviewer_fork_rejects_a_distinct_wrapper_over_the_source_circuit() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _SharedCircuitForkableInjectedReviewer("forkable-reviewer")

    with pytest.raises(ValueError, match="share mutable reviewer runtime"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=reviewer,
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_close_only_reviewer_fork_is_owned_as_the_life_runtime() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _CloseOnlyForkableInjectedReviewer("forkable-reviewer")

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        model_id_prefix="test",
    )

    assert len(reviewer.forks) == 1
    life_reviewer = reviewer.forks[0]
    assert composition.life_source_closure_model is life_reviewer
    assert life_reviewer.closed is False

    await composition.aclose()

    assert life_reviewer.closed is True
    assert reviewer.closed is False


def test_uncloseable_reviewer_fork_is_rejected() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _UncloseableForkableInjectedReviewer("forkable-reviewer")

    with pytest.raises(ValueError, match="fork must provide an async close lifecycle"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=reviewer,
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_schema_capable_reviewer_is_not_reused_as_its_own_inventory() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _StrictInventoryAndCoverageInjectedModel("injected-reviewer")
    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        model_id_prefix="test",
    )

    assert composition.proactive_source_closure_model is reviewer
    assert composition.candidate_external_proposition_inventory_model is None
    assert composition.proactive_source_authority_health()["candidate_inventory_model"] is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_offline_fixture_never_installs_character_self_review() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
        ),
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert composition.character_author_model_id == "FakeCompanionModel"
    assert composition.source_closure_model is None
    assert composition.proactive_source_closure_model is None
    assert composition.proactive_source_authority_health() == {
        "status": "fact_effects_fail_closed",
        "warning": True,
        "warning_reasons": [
            "proactive_source_authority.independent_reviewer_unavailable",
        ],
        "independent_reviewer": False,
        "fact_effects_available": False,
        "subjective_expression_available": True,
        "author_model": "FakeCompanionModel",
        "reviewer_model": None,
        "candidate_inventory_model": None,
        "requested_candidate_inventory_model": None,
        "inventory_capability_evidence": None,
        "inventory_runtime": {
            "status": "unavailable",
            "successful_calls": 0,
            "failed_calls": 0,
            "last_checked_at": None,
            "last_failure_code": None,
        },
        "inventory_call_timeout_seconds": None,
        "visible_review_strategy": "full_source_review",
        "inventory_qualification_state": "unavailable",
        "active_source_review_protocol": "full_source_review.7",
        "source_review_qualification_transition": ("unavailable -> full_source_review.7"),
        "candidate_review_capabilities": {
            "ordinary": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
            "recovery": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
            "reselection": {
                "inventory_v5": False,
                "coverage_v5": False,
                "roles_independent": False,
            },
        },
        "inventory_transport": {
            "route_count": 0,
            "routes": (),
            "single_transport": False,
            "provider_count": 0,
            "single_provider": False,
            "capability_evidence": [],
            "attempt_timeout_seconds": None,
            "secondary_reserved_seconds": None,
        },
        "redundancy_state": "unavailable",
        "source_review_authority": None,
    }
    await composition.aclose()


def test_production_composition_without_character_provider_fails_closed() -> None:
    with pytest.raises(
        ValueError,
        match="requires an explicit character model or DEEPSEEK_API_KEY",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY=None,
                OPENAI_API_KEY=None,
                OPENROUTER_API_KEY=None,
            ),
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_proactive_source_authority_refuses_an_explicit_author_self_review() -> None:
    author = _InjectedModel("same-role-model")
    same_model_reviewer = _InjectedModel("same-role-model")
    with pytest.raises(ValueError, match="independent source-closure reviewer"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=same_model_reviewer,
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_remote_production_composition_runs_pure_deterministic_without_reviewer_keys() -> None:
    """No review keys require an explicit degraded author-only opt-out."""

    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY=None,
            OPENROUTER_API_KEY=None,
            WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=False,
        ),
        model_id_prefix="test",
    )
    await composition.aclose()


@pytest.mark.asyncio
async def test_inventory_v5_is_not_installed_without_qualified_followup_review() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    inventory = _StrictInventoryInjectedModel("injected-inventory")
    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    assert composition.proactive_source_authority_health()["candidate_inventory_model"] is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_inventory_v5_is_installed_with_coverage_v5_authority() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _StrictCoverageInjectedModel("injected-reviewer")
    inventory = _StrictInventoryInjectedModel("injected-inventory")
    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is inventory
    assert composition.proactive_source_authority_health()["candidate_inventory_model"] == (
        "injected-inventory"
    )
    await composition.aclose()


def test_openrouter_and_official_routes_share_one_semantic_authority() -> None:
    openrouter = _StrictCoverageInjectedModel("openai/gpt-5.6-terra")
    openrouter.provider = "openrouter"
    official = _StrictInventoryInjectedModel("gpt-5.6-terra")
    official.provider = "openai"
    openrouter.semantic_authority_id = official.semantic_authority_id = (
        "semantic-authority:test:openai:gpt-5.6-terra"
    )

    assert semantic_authority_id(openrouter) == semantic_authority_id(official)
    assert transport_route_id(openrouter) != transport_route_id(official)
    with pytest.raises(ValueError, match="inventory model.*reviewer"):
        SourceClosureReselectionLane(
            author=_InjectedModel("gpt-5.6-luna"),
            inventory_model=official,
            reviewer=openrouter,
        )


def test_release_registry_closes_non_openai_cross_route_checkpoint_aliases() -> None:
    official_deepseek = SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    openrouter_deepseek = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-v4-flash",
    )
    dashscope_qwen = SimpleNamespace(
        # The generic OpenAI-compatible adapter labels the wire protocol, not
        # the actual semantic provider. The exact endpoint closes that gap.
        provider="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
    )
    openrouter_qwen = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="qwen/qwen-plus",
    )

    assert semantic_authority_id(official_deepseek) == semantic_authority_id(openrouter_deepseek)
    assert semantic_authority_id(dashscope_qwen) == semantic_authority_id(openrouter_qwen)
    assert not provider_lane_sets_are_independent(
        official_deepseek,
        openrouter_deepseek,
    )
    assert not provider_lane_sets_are_independent(dashscope_qwen, openrouter_qwen)


def test_unknown_model_identity_fails_closed_but_registered_checkpoints_remain_independent() -> (
    None
):
    unknown = SimpleNamespace(
        provider="custom-proxy",
        base_url="https://models.example.invalid/v1",
        model="friendly-alias",
    )
    another_unknown = SimpleNamespace(
        provider="another-proxy",
        base_url="https://other.example.invalid/v1",
        model="different-friendly-alias",
    )
    malformed_declaration = SimpleNamespace(
        semantic_authority_id=object(),
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    deepseek = SimpleNamespace(
        provider="deepseek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
    )
    qwen = SimpleNamespace(
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        model="qwen/qwen-plus",
    )

    assert semantic_authority_id(unknown) is None
    assert semantic_authority_id(malformed_declaration) is None
    assert not provider_lane_sets_are_independent(unknown, another_unknown)
    assert provider_lane_sets_are_independent(deepseek, qwen)


@pytest.mark.asyncio
async def test_inventory_v5_rejects_cross_transport_same_semantic_reviewer() -> None:
    author = _InjectedModel("deepseek-v4-flash")
    inventory = _StrictInventoryInjectedModel("gpt-5.6-terra")
    inventory.provider = "openai"
    reviewer = _StrictCoverageInjectedModel("openai/gpt-5.6-terra")
    reviewer.provider = "openrouter"
    inventory.semantic_authority_id = reviewer.semantic_authority_id = (
        "semantic-authority:test:openai:gpt-5.6-terra"
    )

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_composition_defers_owned_reviewer_close_until_authority_is_quiescent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        ),
        model_id_prefix="test",
    )
    authority = composition.source_closure_model
    assert isinstance(authority, SourceReviewAuthority)
    reviewer = authority.primary
    started = asyncio.Event()
    ignored_cancellation = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_suppressing_review(
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.8,
    ) -> tuple[str, object]:
        del messages, temperature
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            ignored_cancellation.set()
            await release.wait()
            return "late-review", {"lane": "primary"}

    monkeypatch.setattr(
        reviewer,
        "complete_json_with_usage",
        cancellation_suppressing_review,
    )
    authority.hedge_after_seconds = 0.001
    authority.deadline_seconds = 0.3

    try:
        with pytest.raises(SourceReviewAttemptsExhausted):
            await authority.complete_json_with_usage(
                [{"role": "user", "content": "review"}],
                temperature=0.0,
            )
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(ignored_cancellation.wait(), timeout=1)

        await asyncio.wait_for(composition.aclose(), timeout=0.2)

        assert reviewer.client.is_closed is False
        assert composition.shutdown_pending_task_count == 1
    finally:
        release.set()

    await asyncio.wait_for(composition.wait_for_shutdown_quiescence(), timeout=1)
    assert reviewer.client.is_closed is True
    assert composition.shutdown_pending_task_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overlap_pair",
    ("author_inventory", "author_reviewer", "inventory_reviewer"),
)
@pytest.mark.asyncio
async def test_inventory_v5_rejects_overlap_in_any_possible_provider_lane(
    overlap_pair: str,
) -> None:
    shared_identity = f"shared-{overlap_pair}"
    author: object = _InjectedModel("independent-author")
    inventory: object = _StrictInventoryInjectedModel("independent-inventory")
    reviewer: object = _StrictCoverageInjectedModel("independent-reviewer")

    if overlap_pair == "author_inventory":
        author = SourceReviewAuthority(
            primary=_InjectedModel("author-primary"),
            secondary=_InjectedModel(shared_identity),
            hedge_after_seconds=1.0,
            deadline_seconds=2.0,
        )
        inventory = _StrictInventoryInjectedModel(shared_identity)
    elif overlap_pair == "author_reviewer":
        author = SourceReviewAuthority(
            primary=_InjectedModel("author-primary"),
            secondary=_InjectedModel(shared_identity),
            hedge_after_seconds=1.0,
            deadline_seconds=2.0,
        )
        reviewer = _StrictCoverageInjectedModel(shared_identity)
    else:
        inventory = SourceReviewAuthority(
            primary=_StrictInventoryInjectedModel("inventory-primary"),
            secondary=_StrictInventoryInjectedModel(shared_identity),
            hedge_after_seconds=1.0,
            deadline_seconds=2.0,
        )
        reviewer = _StrictCoverageInjectedModel(shared_identity)

    if overlap_pair == "author_reviewer":
        with pytest.raises(ValueError, match="independent source-closure reviewer"):
            build_semantic_chat_composition(
                settings=Settings(_env_file=None),
                flash_model=author,  # type: ignore[arg-type]
                source_closure_model=reviewer,  # type: ignore[arg-type]
                candidate_external_proposition_inventory_model=inventory,  # type: ignore[arg-type]
                model_id_prefix="test",
            )
        return

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,  # type: ignore[arg-type]
        source_closure_model=reviewer,  # type: ignore[arg-type]
        candidate_external_proposition_inventory_model=inventory,  # type: ignore[arg-type]
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    assert composition.proactive_source_authority_health()["candidate_inventory_model"] is None
    await composition.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_route", ("flash", "thinking"))
@pytest.mark.asyncio
async def test_inventory_v5_ignores_dormant_provider_fallback_as_a_character_author(
    recovery_route: str,
) -> None:
    primary_author = _InjectedModel("primary-author")
    recovery_author_and_inventory = _StrictInventoryInjectedModel("recovery-author")
    fallback_route = FailoverChatModel(
        primary=primary_author,
        fallback=recovery_author_and_inventory,
        implicit_failover=False,
    )
    reviewer = _StrictCoverageInjectedModel("independent-reviewer")
    flash_model: object = fallback_route
    thinking_model: object | None = None
    if recovery_route == "thinking":
        flash_model = _InjectedModel("independent-flash-author")
        thinking_model = fallback_route

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=flash_model,  # type: ignore[arg-type]
        thinking_model=thinking_model,  # type: ignore[arg-type]
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=recovery_author_and_inventory,
        model_id_prefix="test",
    )

    assert composition.proactive_source_closure_model is reviewer
    assert (
        composition.candidate_external_proposition_inventory_model is recovery_author_and_inventory
    )
    health = composition.proactive_source_authority_health()
    assert health["candidate_inventory_model"] == "recovery-author"
    assert health["candidate_review_capabilities"]["recovery"] == {
        "inventory_v5": False,
        "coverage_v5": False,
        "roles_independent": False,
    }
    await composition.aclose()


def test_composition_rejects_an_implicit_backup_character_author() -> None:
    primary_author = _InjectedModel("primary-author")
    backup_author = _InjectedModel("backup-author")
    route = FailoverChatModel(
        primary=primary_author,
        fallback=backup_author,
        implicit_failover=True,
    )

    with pytest.raises(ValueError, match="implicit backup character author"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=route,
            source_closure_model=_StrictCoverageInjectedModel("independent-reviewer"),
            model_id_prefix="test",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("overlap_author", ("flash", "thinking"))
@pytest.mark.asyncio
async def test_inventory_v5_checks_every_expression_author_when_background_author_differs(
    overlap_author: str,
) -> None:
    expression_author = _InjectedModel(
        "shared-expression-author" if overlap_author == "flash" else "independent-flash-author"
    )
    thinking_author = (
        _InjectedModel("shared-expression-author") if overlap_author == "thinking" else None
    )
    background_author = _InjectedModel("independent-background-author")
    inventory = _StrictInventoryInjectedModel("shared-expression-author")
    reviewer = _StrictCoverageInjectedModel("independent-reviewer")

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=expression_author,
        thinking_model=thinking_author,
        world_support_model=background_author,
        source_closure_model=reviewer,
        candidate_external_proposition_inventory_model=inventory,
        model_id_prefix="test",
    )

    assert composition.world_support_model is background_author
    assert composition.candidate_external_proposition_inventory_model is None
    assert composition.proactive_source_authority_health()["candidate_inventory_model"] is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_source_authority_finishes_inside_its_22_second_caller() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SOURCE_REVIEW_HEDGE_AFTER_SECONDS=8.0,
        WORLD_V2_SOURCE_REVIEW_DEADLINE_SECONDS=30.0,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    main_authority = composition.source_closure_model
    assert isinstance(main_authority, SourceReviewAuthority)
    assert main_authority.primary.model == "gpt-4.1-mini"
    assert main_authority.primary.reasoning_effort == ""
    assert main_authority.secondary.model == "qwen/qwen-plus"
    assert main_authority.secondary.reasoning_effort == ""
    assert main_authority.configured_deadline_seconds == 30.0
    assert main_authority.caller_timeout_seconds == 22.0
    assert main_authority.terminal_reserve_seconds == 0.5
    assert main_authority.deadline_seconds == 21.5
    assert main_authority.health_snapshot()["configured_absolute_timeout_seconds"] == 30.0
    assert main_authority.health_snapshot()["absolute_timeout_seconds"] == 21.5
    assert main_authority.health_snapshot()["caller_timeout_seconds"] == 22.0
    assert main_authority.health_snapshot()["terminal_completion_reserve_seconds"] == 0.5

    recovery_authority = composition.recovery_source_closure_model
    assert isinstance(recovery_authority, SourceReviewAuthority)
    assert recovery_authority.configured_deadline_seconds == 30.0
    assert recovery_authority.caller_timeout_seconds == 22.0
    assert recovery_authority.deadline_seconds == 21.5
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_proactive_authorship_has_no_post_authorship_binder() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert not hasattr(composition, "proactive_claim_binder_model")
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_fake_composition_does_not_install_a_claim_binder() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY="unused-openai-test-key",
            OPENROUTER_API_KEY=None,
        ),
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert isinstance(composition.world_support_model, FakeCompanionModel)
    assert not hasattr(composition, "proactive_claim_binder_model")
    await composition.aclose()


def test_world_v2_has_no_configured_backup_character_model() -> None:
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "world_v2_fallback_model")
    assert settings.world_v2_source_review_redundancy_enabled is False
    assert settings.world_v2_chat_source_review_enabled is True
    assert settings.world_v2_source_review_secondary_model == "qwen/qwen-plus"
    assert settings.world_v2_source_review_fallback_model == "gpt-4.1-mini"
    assert settings.world_v2_source_review_recovery_model == "qwen/qwen-plus"
    assert settings.world_v2_source_review_recovery_fallback_model == "gpt-4.1-mini"
    assert settings.world_v2_source_inventory_enabled is True
    assert settings.world_v2_source_inventory_model == "openai/gpt-5.4-nano"
    assert settings.world_v2_source_inventory_fallback_model == "openai/gpt-5.4-mini"
    assert settings.world_v2_source_inventory_timeout_seconds == 10.0
    assert settings.world_v2_source_review_hedge_after_seconds == 8.0
    assert settings.world_v2_source_review_deadline_seconds == 30.0


def test_world_v2_has_no_configured_contextual_backup_character() -> None:
    settings = Settings(_env_file=None)

    assert not hasattr(settings, "world_v2_contextual_failsafe_enabled")


@pytest.mark.asyncio
async def test_production_identity_leaves_current_relationship_to_the_world_projection() -> None:
    """Stable identity must not freeze the deployment's initial relationship stage."""

    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY=None,
        OPENAI_API_KEY=None,
        OPENROUTER_API_KEY=None,
    )
    composition = build_semantic_chat_composition(
        settings=settings,
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert "relationship_frame" not in type(composition.identity_frame).model_fields
    assert composition.identity_frame.style_rules == (
        "像手机私聊；消息长度、条数和间隔由她当下真正想怎样表达决定，不固定成一两句。",
        "你的消息就是纯粹的私聊文字。",
        "语气平实，偶尔俏皮就好。",
        "先真实，再可爱；先自然，再浪漫。",
    )
    assert "刚认识" not in json.dumps(
        composition.identity_frame.model_dump(mode="json"),
        ensure_ascii=False,
    )
    assert composition.identity_frame.shared_history_facts == (
        "她与用户在 QQ 的读书/城市漫游兴趣群相识。",
    )
    assert composition.identity_frame.counterpart_history_facts == ()
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_identity_does_not_invent_absent_relationship_or_style(
    tmp_path,
) -> None:
    character_path = tmp_path / "minimal-character.yaml"
    character_path.write_text(
        "name: 无预设角色\nbase_prompt: 只保留这个测试角色明确写下的资料。\n",
        encoding="utf-8",
    )
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            character_path=character_path,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
            OPENROUTER_API_KEY=None,
        ),
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert "relationship_frame" not in type(composition.identity_frame).model_fields
    assert composition.identity_frame.style_rules == ()
    await composition.aclose()


@pytest.mark.asyncio
async def test_local_endpoint_model_only_predicts_user_continuation() -> None:
    settings = Settings(
        _env_file=None,
        WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
        DEEPSEEK_API_KEY=None,
        OPENAI_API_KEY=None,
        OPENROUTER_API_KEY=None,
    )
    composition = build_semantic_chat_composition(
        settings=settings,
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert composition.text_endpoint_controller is not None
    endpoint = composition.text_endpoint_controller._model  # noqa: SLF001
    provider = endpoint._model  # noqa: SLF001
    assert isinstance(provider, OpenAICompatibleChatModel)
    assert provider.max_completion_tokens == 96
    assert not hasattr(composition, "advisory_compiler")
    await composition.aclose()


@pytest.mark.asyncio
async def test_local_endpoint_owns_one_non_queueing_capacity_gate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            WORLD_V2_TEXT_ENDPOINT_ENABLED=True,
            DEEPSEEK_API_KEY=None,
            OPENAI_API_KEY=None,
            OPENROUTER_API_KEY=None,
        ),
        flash_model=FakeCompanionModel(),
        model_id_prefix="test",
    )

    assert composition.text_endpoint_controller is not None
    endpoint = composition.text_endpoint_controller._model  # noqa: SLF001
    provider = endpoint._model  # noqa: SLF001
    assert isinstance(provider, OpenAICompatibleChatModel)
    assert isinstance(composition.local_provider_capacity, ProviderCapacityGate)
    assert provider.capacity_gate is composition.local_provider_capacity
    assert composition.local_provider_capacity.health_snapshot()["status"] == "idle"
    await composition.aclose()
