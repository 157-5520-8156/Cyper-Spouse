import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from companion_daemon.config import Settings
from companion_daemon.llm import (
    DeepSeekChatModel,
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
from companion_daemon.world_v2.visible_source_review_model import (
    VisibleSourceReviewModel,
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

    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "visible-beat-source-verdict.1"


class _MeasuredLatencyReview:
    def __init__(self, *, model: str, delay_seconds: float, fail: bool = False) -> None:
        self.model = model
        self.provider = "openrouter" if model.startswith("qwen/") else "openai"
        self.delay_seconds = delay_seconds
        self.fail = fail
        self.calls = 0

    async def complete_json_with_usage(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.0,
    ) -> tuple[str, object]:
        del messages, temperature
        self.calls += 1
        if self.fail:
            raise AssertionError("the reserve reviewer must not be selected")
        await asyncio.sleep(self.delay_seconds)
        return "{}", {"provider": self.provider}


@pytest.mark.asyncio
async def test_source_review_primary_budget_covers_the_observed_qualified_rtt() -> None:
    settings = Settings(_env_file=None)
    primary = _MeasuredLatencyReview(model="qwen/qwen-plus", delay_seconds=4.25)
    secondary = _MeasuredLatencyReview(
        model="gpt-4.1-mini",
        delay_seconds=0.0,
        fail=True,
    )
    authority = SourceReviewAuthority(
        primary=primary,  # type: ignore[arg-type]
        secondary=secondary,  # type: ignore[arg-type]
        hedge_after_seconds=settings.world_v2_source_review_hedge_after_seconds,
        deadline_seconds=10.0,
        caller_timeout_seconds=10.0,
    )

    raw, _usage = await authority.complete_json_with_usage(
        [{"role": "user", "content": "review"}],
        temperature=0.0,
    )

    assert raw == "{}"
    assert primary.calls == 1
    assert secondary.calls == 0


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


class _StrictInventoryInjectedModel(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-inventory.5"


class _StrictCoverageInjectedModel(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract == "candidate-external-proposition-coverage.5"


class _StrictLegacyFullReviewer(_InjectedModel):
    def supports_strict_output_contract(self, contract: str) -> bool:
        return contract in {
            "report-relative-entailment-adjudication.3",
            "source-closure-review.7",
        }


def test_remote_production_cannot_disable_the_visible_source_boundary() -> None:
    with pytest.raises(
        ValueError,
        match="provider-backed visible chat requires the Flash compact source guard",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
                OPENAI_API_KEY="openai-test-key",
                WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=False,
                WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=False,
                WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=False,
            ),
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_remote_production_keeps_chat_source_review_when_redundancy_is_disabled() -> None:
    """Legacy redundancy knobs cannot replace the compact visible guard."""

    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=False,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
            WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert composition.proactive_source_authority_health()["status"] == (
        "correlated_guard"
    )
    await composition.aclose()


def test_remote_production_rejects_unqualified_compact_guard_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No historical reviewer may rescue an unaudited Flash checkpoint."""

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
        match="exact audited DeepSeek route",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
                OPENAI_API_KEY="openai-test-key",
                OPENROUTER_API_KEY="openrouter-test-key",
                WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
                WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
                deepseek_model="unqualified-flash-checkpoint",
                WORLD_V2_SELECTIVE_SOURCE_REVIEW_MODEL=(
                    "unqualified-flash-checkpoint"
                ),
            ),
            model_id_prefix="test",
        )

    assert created_clients == []


@pytest.mark.asyncio
async def test_world_v2_composition_separates_compact_chat_from_independent_life_review() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        OPENAI_PROXY_URL="http://127.0.0.1:7890",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.character_author_model_id == "deepseek-v4-flash"
    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert composition.proactive_source_authority_health()["author_model"] == "deepseek-v4-flash"
    assert composition.source_closure_model.supports_strict_output_contract(
        "visible-beat-source-verdict.1"
    )
    assert isinstance(composition.recovery_source_closure_model, VisibleSourceReviewModel)
    assert composition.recovery_source_closure_model.supports_strict_output_contract(
        "visible-beat-source-verdict.1"
    )
    assert isinstance(composition.life_source_closure_model, SourceReviewAuthority)
    assert composition.source_closure_reselection_lane is None
    assert composition.proactive_source_authority_health()["status"] == (
        "correlated_guard"
    )
    await composition.aclose()


@pytest.mark.asyncio
async def test_flash_only_visible_guard_uses_one_correlated_checkpoint() -> None:
    base = {
        "_env_file": None,
        "DEEPSEEK_API_KEY": "deepseek-test-key",
        "OPENAI_API_KEY": None,
        "OPENROUTER_API_KEY": None,
        "QWEN_API_KEY": None,
        "WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED": False,
    }
    observed_usage: list[object] = []
    usage_observer = observed_usage.append
    composition = build_semantic_chat_composition(
        settings=Settings(
            **base,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_MODEL="deepseek-v4-flash",
        ),
        model_id_prefix="test",
        usage_observer=usage_observer,
    )
    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert composition.source_closure_model.provider == "deepseek"
    assert composition.source_closure_model.model == "deepseek-v4-flash"
    assert composition.source_closure_model.supports_strict_output_contract(
        "visible-beat-source-verdict.1"
    )
    assert isinstance(composition.recovery_source_closure_model, VisibleSourceReviewModel)
    assert composition.life_source_closure_model is None
    assert composition.source_closure_model.usage_observer is usage_observer
    compact_health = composition.proactive_source_authority_health()["selective_source_review"]
    assert compact_health["enabled"] is True
    assert compact_health["runtime"]["semantic_authority_relation"] == (
        "correlated_same_checkpoint"
    )
    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["independent_reviewer"] is False
    assert health["fact_effects_available"] is True
    assert health["source_guard_relation"] == "correlated_same_checkpoint"
    assert health["active_source_review_protocol"] == "visible_beat_source_verdict.1"
    assert health["redundancy_state"] == "single_active_correlated_lane"
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_deepseek_author_still_installs_compact_visible_guard() -> None:
    """CLI-style caller ownership must not restore the paid review route."""

    author = DeepSeekChatModel(
        api_key="deepseek-test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        thinking_enabled=False,
    )
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=False,
        ),
        flash_model=author,
        model_id_prefix="explicit-deepseek",
    )

    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert composition.proactive_source_authority_health()[
        "active_source_review_protocol"
    ] == "visible_beat_source_verdict.1"
    await composition.aclose()
    await author.aclose()


@pytest.mark.asyncio
async def test_default_production_chat_cannot_reinstall_full_source_review() -> None:
    """Unset selective config must not resurrect the historical paid chat lane."""

    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=False,
        ),
        model_id_prefix="test",
    )

    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    health = composition.proactive_source_authority_health()
    assert health["visible_review_strategy"] == "visible_beat_verdict"
    assert health["active_source_review_protocol"] == (
        "visible_beat_source_verdict.1"
    )
    assert health["source_review_authority"] is None
    await composition.aclose()


@pytest.mark.asyncio
async def test_independent_paid_reviewers_are_owned_only_by_life_runtime() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert isinstance(composition.recovery_source_closure_model, VisibleSourceReviewModel)
    assert isinstance(composition.life_source_closure_model, SourceReviewAuthority)
    assert composition.proactive_source_authority_health()["source_review_authority"] is None
    assert composition.life_source_authority_health()["runtime_isolation"] == (
        "dedicated_life_only"
    )
    await composition.aclose()


def test_production_chat_rejects_legacy_full_source_review_opt_out() -> None:
    with pytest.raises(
        ValueError,
        match="visible chat requires the Flash compact source guard",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
                OPENAI_API_KEY="openai-test-key",
                OPENROUTER_API_KEY="openrouter-test-key",
                WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=False,
                WORLD_V2_LIFE_SOURCE_REVIEW_ENABLED=False,
            ),
            model_id_prefix="test",
        )


def test_production_chat_rejects_injected_legacy_full_reviewer() -> None:
    with pytest.raises(
        ValueError,
        match="rejects the retired full-source-review route",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
            ),
            source_closure_model=_StrictLegacyFullReviewer("legacy-paid-review"),
            model_id_prefix="test",
        )


@pytest.mark.asyncio
async def test_provider_capture_can_preserve_deepseek_authority_for_isolated_preflight() -> None:
    """A loopback hash capture must not erase the underlying author identity."""

    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_base_url="http://127.0.0.1:32124",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="isolated-capture",
        test_only_provider_capture_authority_id=(
            "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
        ),
    )

    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["active_source_review_protocol"] == (
        "visible_beat_source_verdict.1"
    )
    assert "source_review_authority.test_only_capture_transit" in health[
        "warning_reasons"
    ]
    await composition.aclose()


@pytest.mark.parametrize(
    "settings_update",
    [
        {"deepseek_base_url": "https://api.deepseek.com"},
        {"deepseek_model": "unknown-checkpoint"},
    ],
)
def test_provider_capture_authority_is_fail_closed_outside_exact_loopback_route(
    settings_update: dict[str, str],
) -> None:
    settings_kwargs = {
        "_env_file": None,
        "DEEPSEEK_API_KEY": "deepseek-test-key",
        "deepseek_base_url": "http://127.0.0.1:32124",
        "deepseek_model": "deepseek-v4-flash",
        "OPENAI_API_KEY": "openai-test-key",
        "OPENROUTER_API_KEY": "openrouter-test-key",
        "WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED": True,
        "WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED": False,
        **settings_update,
    }
    settings = Settings(
        **settings_kwargs,
    )
    with pytest.raises(ValueError, match="test-only provider capture authority"):
        build_semantic_chat_composition(
            settings=settings,
            model_id_prefix="isolated-capture",
            test_only_provider_capture_authority_id=(
                "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
            ),
        )


def test_provider_capture_authority_rejects_caller_supplied_character_route() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_base_url="http://127.0.0.1:32124",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
    )

    with pytest.raises(ValueError, match="caller-supplied character models"):
        build_semantic_chat_composition(
            settings=settings,
            flash_model=SimpleNamespace(
                provider="deepseek",
                base_url="https://evil.example/v1",
                model="wrong-checkpoint",
            ),
            model_id_prefix="isolated-capture",
            test_only_provider_capture_authority_id=(
                "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
            ),
        )


def test_provider_capture_authority_rejects_unpinned_thinking_checkpoint() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_base_url="http://127.0.0.1:32124",
        deepseek_model="deepseek-v4-flash",
        DEEPSEEK_CHARACTER_THINKING_ENABLED=True,
        DEEPSEEK_CHARACTER_THINKING_MODEL="deepseek-v4-thinking",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
    )

    with pytest.raises(ValueError, match="thinking character route"):
        build_semantic_chat_composition(
            settings=settings,
            model_id_prefix="isolated-capture",
            test_only_provider_capture_authority_id=(
                "semantic-authority:2026-08-01.1:deepseek:deepseek-v4-flash"
            ),
        )


@pytest.mark.asyncio
async def test_production_composition_has_no_backup_character_author() -> None:

    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        deepseek_model="deepseek-v4-flash",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
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
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
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
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
        WORLD_V2_SOURCE_REVIEW_HEDGE_AFTER_SECONDS=3.5,
        WORLD_V2_SOURCE_REVIEW_DEADLINE_SECONDS=19.0,
        WORLD_V2_SOURCE_INVENTORY_TIMEOUT_SECONDS=5.0,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert isinstance(composition.source_closure_model, VisibleSourceReviewModel)
    assert isinstance(composition.recovery_source_closure_model, VisibleSourceReviewModel)
    assert composition.proactive_source_closure_model is composition.source_closure_model
    assert composition.candidate_external_proposition_inventory_model is None
    life_authority = composition.life_source_closure_model
    assert isinstance(life_authority, SourceReviewAuthority)
    assert life_authority.primary.model == "gpt-4.1-mini"
    assert life_authority.primary.base_url == "https://api.openai.com/v1"
    assert life_authority.primary.proxy_url == "http://127.0.0.1:7890"
    assert life_authority.secondary.model == "qwen/qwen-plus"
    assert life_authority.secondary.base_url == "https://openrouter.ai/api/v1"
    assert life_authority in composition._owned_task_owners
    # A background Life timeout cannot suppress the separate compact chat guard.
    life_authority._after_lane_failure("primary", "provider_timeout")
    life_health = life_authority.health_snapshot()
    assert life_health["route_suppression"]["primary"]["active"] is True
    deployment_health = composition.life_source_authority_health()
    assert deployment_health["status"] == "operational_unqualified"
    assert deployment_health["warning"] is True
    assert deployment_health["runtime_isolated"] is True
    assert deployment_health["route_suppression"]["primary"]["active"] is True
    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["redundancy_state"] == "single_active_correlated_lane"
    assert health["visible_review_strategy"] == "visible_beat_verdict"
    assert health["source_review_authority"] is None
    assert health["candidate_inventory_model"] is None
    assert health["requested_candidate_inventory_model"] is None
    assert health["inventory_capability_evidence"] is None
    assert health["inventory_transport"]["route_count"] == 0
    assert health["warning"] is True
    assert "source_review_authority.correlated_same_checkpoint" in health[
        "warning_reasons"
    ]
    await composition.aclose()


@pytest.mark.asyncio
async def test_life_source_review_prefers_the_stricter_live_winner() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    authority = composition.life_source_closure_model
    assert isinstance(authority, SourceReviewAuthority)
    assert authority.primary.model == "gpt-4.1-mini"
    assert authority.primary.provider == "openai"
    assert authority.secondary.model == "qwen/qwen-plus"
    assert authority.secondary.provider == "openrouter"
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
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["visible_review_strategy"] == "visible_beat_verdict"
    assert health["candidate_inventory_model"] is None
    assert health["requested_candidate_inventory_model"] is None
    assert health["inventory_capability_evidence"] is None
    assert health["inventory_call_timeout_seconds"] is None
    assert health["inventory_runtime"]["status"] == "unavailable"
    assert health["source_review_authority"] is None
    assert health["candidate_review_capabilities"]["ordinary"]["inventory_v5"] is False
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_compact_chat_does_not_construct_legacy_inventory_routes() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            OPENROUTER_BASE_URL="https://openrouter.ai/api/v1",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    assert composition.proactive_source_authority.inventory_runtime_model is None
    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["visible_review_strategy"] == "visible_beat_verdict"
    assert health["active_source_review_protocol"] == (
        "visible_beat_source_verdict.1"
    )
    assert health["inventory_runtime"]["status"] == "unavailable"
    assert health["inventory_transport"]["route_count"] == 0
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_inventory_flag_cannot_weaken_compact_review() -> None:
    composition = build_semantic_chat_composition(
        settings=Settings(
            _env_file=None,
            DEEPSEEK_API_KEY="deepseek-test-key",
            OPENAI_API_KEY="openai-test-key",
            OPENROUTER_API_KEY="openrouter-test-key",
            WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
            WORLD_V2_SOURCE_INVENTORY_ENABLED=False,
        ),
        model_id_prefix="test",
    )

    assert composition.candidate_external_proposition_inventory_model is None
    health = composition.proactive_source_authority_health()
    assert health["status"] == "correlated_guard"
    assert health["visible_review_strategy"] == "visible_beat_verdict"
    assert health["inventory_capability_evidence"] is None
    assert health["inventory_runtime"]["status"] == "unavailable"
    await composition.aclose()


@pytest.mark.asyncio
async def test_explicit_source_closure_injection_is_not_replaced_by_auto_wiring() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
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
async def test_compact_chat_reviewer_is_never_reused_as_the_life_runtime() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _ForkableInjectedReviewer("forkable-reviewer")

    composition = build_semantic_chat_composition(
        settings=Settings(_env_file=None),
        flash_model=author,
        source_closure_model=reviewer,
        model_id_prefix="test",
    )

    assert reviewer.forks == []
    assert composition.life_source_closure_model is None

    await composition.aclose()

    assert reviewer.closed is False


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
        "source_guard_relation": "unavailable",
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
        "visible_review_strategy": "unavailable",
        "inventory_qualification_state": "unavailable",
        "active_source_review_protocol": "unavailable",
        "source_review_qualification_transition": ("unavailable -> unavailable"),
        "selective_source_review": {"enabled": False, "runtime": None},
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


def test_remote_production_without_reviewer_keys_still_cannot_select_author_only() -> None:
    with pytest.raises(
        ValueError,
        match="provider-backed visible chat requires the Flash compact source guard",
    ):
        build_semantic_chat_composition(
            settings=Settings(
                _env_file=None,
                DEEPSEEK_API_KEY="deepseek-test-key",
                OPENAI_API_KEY=None,
                OPENROUTER_API_KEY=None,
                WORLD_V2_CHAT_SOURCE_REVIEW_ENABLED=False,
            ),
            model_id_prefix="test",
        )


def test_semantic_chat_rejects_retired_inventory_injection() -> None:
    author = _InjectedModel("injected-author")
    reviewer = _InjectedModel("injected-reviewer")
    inventory = _StrictInventoryInjectedModel("injected-inventory")
    with pytest.raises(ValueError, match="no longer accepts an Inventory reviewer"):
        build_semantic_chat_composition(
            settings=Settings(_env_file=None),
            flash_model=author,
            source_closure_model=reviewer,
            candidate_external_proposition_inventory_model=inventory,
            model_id_prefix="test",
        )


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
            WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
        ),
        model_id_prefix="test",
    )
    authority = composition.life_source_closure_model
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
async def test_life_source_authority_finishes_inside_its_22_second_caller() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
        WORLD_V2_SOURCE_REVIEW_HEDGE_AFTER_SECONDS=8.0,
        WORLD_V2_SOURCE_REVIEW_DEADLINE_SECONDS=30.0,
    )

    composition = build_semantic_chat_composition(
        settings=settings,
        model_id_prefix="test",
    )

    main_authority = composition.life_source_closure_model
    assert isinstance(main_authority, SourceReviewAuthority)
    assert main_authority.primary.model == "gpt-4.1-mini"
    assert main_authority.primary.reasoning_effort == ""
    assert main_authority.secondary.model == "qwen/qwen-plus"
    assert main_authority.configured_deadline_seconds == 30.0
    assert main_authority.caller_timeout_seconds == 22.0
    assert main_authority.terminal_reserve_seconds == 0.5
    assert main_authority.deadline_seconds == 21.5
    assert main_authority.health_snapshot()["configured_absolute_timeout_seconds"] == 30.0
    assert main_authority.health_snapshot()["absolute_timeout_seconds"] == 21.5
    assert main_authority.health_snapshot()["caller_timeout_seconds"] == 22.0
    assert main_authority.health_snapshot()["terminal_completion_reserve_seconds"] == 0.5

    assert isinstance(composition.recovery_source_closure_model, VisibleSourceReviewModel)
    await composition.aclose()


@pytest.mark.asyncio
async def test_production_proactive_authorship_has_no_post_authorship_binder() -> None:
    settings = Settings(
        _env_file=None,
        DEEPSEEK_API_KEY="deepseek-test-key",
        OPENAI_API_KEY="openai-test-key",
        OPENROUTER_API_KEY="openrouter-test-key",
        WORLD_V2_SOURCE_REVIEW_REDUNDANCY_ENABLED=True,
        WORLD_V2_SELECTIVE_SOURCE_REVIEW_ENABLED=True,
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
    assert settings.world_v2_source_review_hedge_after_seconds == 6.0
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
