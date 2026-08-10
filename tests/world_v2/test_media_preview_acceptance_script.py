"""Unit coverage for the isolated World v2 media qualification harness.

These tests import the harness and exercise its pure report/safety seams.  No
provider client is constructed and no acceptance run is started here.
"""

from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from companion_daemon.image_generation import ImageGenerationProviderError
from companion_daemon.world_v2.media_provider_transport import (
    sanitize_media_provider_error,
)


def _harness():
    module_name = "_girl_agent_media_preview_acceptance_under_test"
    loaded = sys.modules.get(module_name)
    if loaded is not None:
        return loaded
    script_path = Path(__file__).resolve().parents[2] / "scripts" / (
        "run_world_v2_media_preview_acceptance.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load acceptance harness")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _settings(tmp_path: Path):
    from companion_daemon.config import Settings

    return Settings(
        database_path=tmp_path / "isolated.sqlite",
        NAPCAT_ALLOWED_PRIVATE_USER_IDS="acceptance",
        PRIMARY_USER_ID="acceptance",
    )


def test_import_does_not_construct_real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the harness must not instantiate a model or HTTP client."""

    import companion_daemon.image_generation as image_generation
    import companion_daemon.llm as llm

    module_name = "_girl_agent_media_preview_acceptance_under_test"
    importlib.invalidate_caches()
    sys.modules.pop(module_name, None)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider construction during harness import")

    monkeypatch.setattr(llm, "DeepSeekChatModel", fail)
    monkeypatch.setattr(image_generation.httpx, "AsyncClient", fail)
    module = _harness()
    assert callable(module.main)
    assert module.MAX_REAL_RENDER_ATTEMPTS == 2


def test_new_scratch_root_is_fresh_tmp_and_production_paths_are_rejected() -> None:
    harness = _harness()

    first = harness._new_scratch_root()
    second = harness._new_scratch_root()
    assert first != second
    assert first.parent == Path("/tmp")
    assert second.parent == Path("/tmp")
    assert first.name.startswith("girl-agent-wt-e.")
    assert harness._require_scratch_path(first / "media.sqlite", first) == (
        first / "media.sqlite"
    ).resolve()
    with pytest.raises(ValueError, match="isolated scratch"):
        harness._require_scratch_path(Path("data/companion.sqlite"), first)


def test_base_report_contains_git_sha_and_starts_unqualified(tmp_path: Path) -> None:
    harness = _harness()
    report = harness._base_report(scratch_root=tmp_path, settings=_settings(tmp_path))

    assert isinstance(report["git"]["sha"], str)
    assert len(report["git"]["sha"]) == 40
    assert report["qualification_scope"] == "not_started"
    assert report["character_selection_qualified"] is False
    assert report["deterministic_selection_double"] is False


def test_failed_render_cannot_claim_qualification_complete() -> None:
    harness = _harness()

    failed = harness._qualification_fields(
        status="preview_not_generated", deterministic_selection_double=False
    )
    downstream = harness._qualification_fields(
        status="qualification_complete", deterministic_selection_double=True
    )
    assert failed["qualification_complete"] is False
    assert failed["qualification_scope"] == "character_selection_only"
    assert failed["character_selection_qualified"] is False
    assert downstream["qualification_complete"] is False
    assert downstream["qualification_scope"] == "downstream_provider_stages_only"
    assert downstream["deterministic_selection_double"] is True


def test_real_selection_no_op_is_reported_as_no_op_not_select() -> None:
    harness = _harness()
    role_model = SimpleNamespace(
        media_attempts=[{"decision": "no_op", "status": "decision"}],
        provider="deepseek",
        model="test-model",
        base_url=None,
    )
    result = SimpleNamespace(
        status="no_op", reason_code="character_attention_not_selected", proposal_event_ref=None
    )
    report = harness._media_selection_report(
        result=result,
        role_model=role_model,
        evidence=SimpleNamespace(events=()),
        usage=(),
        usage_start=0,
    )

    assert report["status"] == "no_op"
    assert report["decision"] == "no_op"
    assert report["decision"] != "select"
    assert report["first_legal"] is True


def test_deterministic_double_is_downstream_only_and_provider_failure_is_not_role_no_op() -> None:
    harness = _harness()

    assert harness._provider_failure_did_not_become_role_no_op(
        {"status": "proposed", "decision": "select"}, ["MediaPreviewFailed"]
    ) is True
    assert harness._provider_failure_did_not_become_role_no_op(
        {"status": "no_op", "decision": "no_op"}, ["MediaPreviewFailed"]
    ) is False
    assert harness._qualification_fields(
        status="preview_not_generated", deterministic_selection_double=True
    )["qualification_scope"] == "downstream_provider_stages_only"


def test_sanitized_provider_error_keeps_stage_and_timeout_without_secrets() -> None:
    try:
        try:
            raise httpx.ReadTimeout("Authorization: Bearer sk-test-secret private prompt")
        except httpx.ReadTimeout as cause:
            raise ImageGenerationProviderError(
                provider="openai_image",
                kind="transport",
                detail="sk-test-secret private prompt",
            ) from cause
    except ImageGenerationProviderError as error:
        diagnostic = sanitize_media_provider_error(
            stage="image_generation",
            exception=error,
            endpoint="https://api.openai.com/v1",
            model="gpt-image-2",
            proxy_configured=True,
            elapsed_ms=150_556.7,
            secret_values=("sk-test-secret",),
        )

    assert diagnostic["stage"] == "image_generation"
    assert diagnostic["exception_class"] == "ImageGenerationProviderError"
    assert diagnostic["cause_class"] == "ReadTimeout"
    assert diagnostic["timeout_class"] == "read"
    assert diagnostic["endpoint_hostname"] == "api.openai.com"
    assert diagnostic["model"] == "gpt-image-2"
    assert diagnostic["elapsed_ms"] == 150_556.7
    encoded = str(diagnostic)
    assert "sk-test-secret" not in encoded
    assert "Authorization" not in encoded
    assert "private prompt" not in encoded


def test_restart_report_declares_and_checks_required_fields() -> None:
    harness = _harness()
    complete = {key: True for key in harness.RESTART_REPORT_FIELDS}
    assert harness._restart_report_is_complete(complete) is True
    incomplete = dict(complete)
    incomplete.pop(harness.RESTART_REPORT_FIELDS[0])
    assert harness._restart_report_is_complete(incomplete) is False


def test_artifact_path_and_hash_are_verified_in_scratch(tmp_path: Path) -> None:
    harness = _harness()
    artifact_path = tmp_path / "artifacts" / "event-media" / "render.png"
    artifact_path.parent.mkdir(parents=True)
    data = bytearray(b"\x89PNG\r\n\x1a\n")
    data.extend(b"\x00" * 8)
    data.extend((1).to_bytes(4, "big"))
    data.extend((1).to_bytes(4, "big"))
    artifact_path.write_bytes(bytes(data))
    digest = "sha256:" + hashlib.sha256(bytes(data)).hexdigest()
    artifact = {
        "path": str(artifact_path),
        "bytes": len(data),
        "sha256": digest,
        "mime_type": "image/png",
        "dimensions": {"width": 1, "height": 1},
    }

    assert harness._artifact_report_is_valid(artifact, scratch_root=tmp_path) is True
    artifact["sha256"] = "sha256:" + "0" * 64
    assert harness._artifact_report_is_valid(artifact, scratch_root=tmp_path) is False


@pytest.mark.asyncio
async def test_qualification_delivery_sink_can_never_call_qq() -> None:
    harness = _harness()

    with pytest.raises(AssertionError, match="must not deliver"):
        await harness._NoDeliveryTransport().send(SimpleNamespace(kind="qq_message"))


def test_real_render_attempt_limit_is_two_and_third_is_rejected() -> None:
    harness = _harness()

    assert harness._render_attempt_within_limit(0) is True
    assert harness._render_attempt_within_limit(1) is True
    assert harness._render_attempt_within_limit(2) is False
    assert harness._render_attempt_within_limit(3) is False
