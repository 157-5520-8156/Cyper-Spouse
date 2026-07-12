import ast
from datetime import datetime, timedelta
from pathlib import Path
import tomllib

import pytest

from companion_daemon.config import Settings, get_settings
from companion_daemon.db import CompanionStore
from companion_daemon.runtime import build_companion_engine, require_flash_model
from companion_daemon.world import WorldKernel


def test_runtime_builds_engine_with_fake_model() -> None:
    engine = build_companion_engine(use_fake_model=True)

    assert engine.companion_system_prompt
    assert engine.world_kernel is not None
    assert engine.world_id is not None


def test_daemon_prompt_core_is_default_without_env() -> None:
    settings = Settings(_env_file=None)

    assert settings.conversation_core == "prompt"
    assert settings.deepseek_model == "deepseek-v4-flash"
    assert settings.deepseek_thinking_enabled is False
    assert settings.deepseek_deep_appraisal_model == "deepseek-v4-flash"
    assert settings.deepseek_deep_appraisal_thinking_enabled is True
    assert settings.deepseek_repair_model == "deepseek-v4-flash"
    assert settings.deepseek_repair_thinking_enabled is True
    assert not hasattr(settings, "world_runtime_enabled")


def test_runtime_rejects_v4_pro_override() -> None:
    with pytest.raises(ValueError, match="disabled"):
        require_flash_model("deepseek-v4-pro", setting="DEEPSEEK_MODEL")


@pytest.mark.asyncio
async def test_runtime_models_share_provider_circuit_breaker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runtime-breaker.sqlite"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        engine = build_companion_engine()
    finally:
        get_settings.cache_clear()

    assert engine.interaction_appraisal_model is not None
    assert engine.interaction_deep_appraisal_model is not None
    assert engine.interaction_appraisal_model.thinking_enabled is False
    assert engine.interaction_deep_appraisal_model.thinking_enabled is True
    assert engine.model.circuit_breaker is not None
    assert (
        engine.model.circuit_breaker
        is engine.interaction_appraisal_model.circuit_breaker
    )
    assert (
        engine.model.circuit_breaker
        is engine.interaction_deep_appraisal_model.circuit_breaker
    )
    assert engine.model.client is engine.interaction_appraisal_model.client
    assert engine.model.client is engine.interaction_deep_appraisal_model.client
    await engine.aclose()


@pytest.mark.asyncio
async def test_runtime_routes_daily_reply_to_flash_and_exposes_task_level_deep_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runtime-routing.sqlite"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_DEEP_APPRAISAL_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_DEEP_APPRAISAL_THINKING_ENABLED", "true")
    monkeypatch.setenv("DEEPSEEK_REPAIR_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_REPAIR_THINKING_ENABLED", "false")
    get_settings.cache_clear()
    try:
        engine = build_companion_engine()
    finally:
        get_settings.cache_clear()

    assert engine.model.model == "deepseek-v4-flash"
    assert engine.model.thinking_enabled is False
    assert engine.interaction_appraisal_model.model == "deepseek-v4-flash"
    assert engine.interaction_appraisal_model.thinking_enabled is False
    assert engine.interaction_deep_appraisal_model.model == "deepseek-v4-pro"
    assert engine.interaction_deep_appraisal_model.thinking_enabled is True
    assert engine.reply_repair_model.model == "deepseek-v4-pro"
    assert engine.reply_repair_model.thinking_enabled is False
    assert engine.reply_repair_model.client is engine.model.client
    assert engine.reply_repair_model.circuit_breaker is engine.model.circuit_breaker
    await engine.aclose()


@pytest.mark.asyncio
async def test_runtime_engine_closes_its_shared_model_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runtime-close.sqlite"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    get_settings.cache_clear()
    try:
        engine = build_companion_engine()
        client = engine.model.client
        await engine.aclose()
    finally:
        get_settings.cache_clear()

    assert client.is_closed is True


def test_removed_world_runtime_switch_cannot_disable_the_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "runtime.sqlite"))
    monkeypatch.setenv("WORLD_RUNTIME_ENABLED", "false")
    get_settings.cache_clear()
    try:
        engine = build_companion_engine(use_fake_model=True)
    finally:
        get_settings.cache_clear()

    assert engine.world_kernel is not None
    assert engine.world_id is not None
    assert engine.store.world_mode_enabled is True
    assert engine.store.has_mood_state("geoff") is False


def test_runtime_fails_closed_against_legacy_behavior_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "runtime.sqlite"
    monkeypatch.setenv("DATABASE_PATH", str(database))
    get_settings.cache_clear()
    try:
        engine = build_companion_engine(use_fake_model=True)
    finally:
        get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        engine.store.upsert_memory(
            "geoff", kind="profile", content="legacy bypass", source="test"
        )

    # The guard is persisted, so another process cannot reopen the same
    # database with a false in-memory flag and write through the old model.
    reopened = CompanionStore(database)
    with pytest.raises(RuntimeError, match="forbids legacy behaviour write"):
        reopened.save_mood_state("geoff", reopened.get_mood_state("geoff"))


def test_runtime_startup_marks_abandoned_outgoing_claim_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "runtime-recovery.sqlite"
    store = CompanionStore(database)
    store.enable_world_mode()
    store.resolve_user("qq", "geoff")
    kernel = WorldKernel(store)
    started = kernel.start_from_seed_file(Path("configs/world_seed.yaml"))
    delivery_id, _, action_id = kernel.queue_outgoing_action(
        canonical_user_id="geoff",
        platform="qq",
        text="进程在回执落库前崩溃。",
        kind="reply",
        expires_at=datetime.now().astimezone() + timedelta(hours=1),
        trace={
            "world_id": started.world_id,
            "appraisal": "test",
            "expression_policy": "test",
            "allowed_facts": [],
            "short_lived_constraint": None,
            "observable_reason": "test",
        },
    )
    kernel.begin_outgoing_action(
        delivery_id,
        expected_revision=kernel.revision(started.world_id),
        lease_seconds=0,
    )
    monkeypatch.setenv("DATABASE_PATH", str(database))
    get_settings.cache_clear()
    try:
        engine = build_companion_engine(use_fake_model=True)
    finally:
        get_settings.cache_clear()

    action = engine.world_kernel.snapshot(engine.world_id)["actions"][action_id]
    assert action["status"] == "unknown"
    assert engine.store.outbox_message(delivery_id)["status"] == "unknown"


def test_all_production_entrypoints_use_the_world_runtime_factory() -> None:
    project_root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((project_root / "pyproject.toml").read_text())
    scripts = project["project"]["scripts"]
    offline_tools = {"companion-eval-experience"}

    for script_name, target in scripts.items():
        module_name = target.split(":", 1)[0]
        source_path = project_root / "src" / Path(*module_name.split(".")).with_suffix(".py")
        tree = ast.parse(source_path.read_text(), filename=str(source_path))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        if script_name not in offline_tools:
            assert "build_companion_engine" in calls, (
                f"production entrypoint {script_name!r} must use the single world runtime factory"
            )
        assert "CompanionEngine" not in calls, (
            f"production entrypoint {script_name!r} must not construct a bypass engine"
        )


def test_world_runtime_factory_does_not_call_legacy_behavior_writers() -> None:
    project_root = Path(__file__).resolve().parents[1]
    runtime_path = project_root / "src" / "companion_daemon" / "runtime.py"
    tree = ast.parse(runtime_path.read_text(), filename=str(runtime_path))
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    forbidden = {
        "seed_user",
        "save_mood_state",
        "save_life_runtime",
        "create_social_task",
        "create_calendar_event",
        "upsert_memory",
        "queue_outgoing",
        "save_incoming",
    }
    assert calls.isdisjoint(forbidden), f"legacy startup bypasses: {sorted(calls & forbidden)}"


def test_removed_world_switch_has_no_production_source_or_config_residue() -> None:
    project_root = Path(__file__).resolve().parents[1]
    roots = [project_root / "src", project_root / "configs", project_root / "pyproject.toml"]
    forbidden = ("WORLD_RUNTIME_ENABLED", "world_runtime_enabled")
    violations: list[str] = []
    for root in roots:
        files = root.rglob("*") if root.is_dir() else (root,)
        for path in files:
            if not path.is_file() or path.suffix in {".pyc", ".sqlite"}:
                continue
            content = path.read_text(errors="ignore")
            if any(token in content for token in forbidden):
                violations.append(str(path.relative_to(project_root)))
    assert violations == []
