from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_launchd.sh"
BASE_LABELS = {
    "com.girl-agent.qq-ws",
    "com.girl-agent.daemon",
    "com.girl-agent.napcat",
    "com.girl-agent.rsshub",
}
TEXT_LABELS = {
    "com.girl-agent.text-endpoint",
    "com.girl-agent.text-endpoint-watchdog",
}
RETIRED_LABELS = {
    "com.girl-agent.proactive",
    "com.girl-agent.local-appraisal-watchdog",
    "com.girl-agent.local-appraisal",
}
ALL_LABELS = BASE_LABELS | TEXT_LABELS | RETIRED_LABELS | {
    "com.girl-agent.sillytavern"
}


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _installer_environment(
    tmp_path: Path,
    *,
    text_endpoint_enabled: bool,
    inconsistent_endpoint: bool = False,
    fail_bootstrap_label: str | None = None,
    daemon_health_payload: dict[str, object] | None = None,
) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "launchctl-state"
    state.mkdir()
    launch_log = tmp_path / "launchctl.log"
    curl_log = tmp_path / "curl.log"
    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir()
    endpoint = tmp_path / "mlx_lm.server"
    _write_executable(endpoint, "#!/bin/sh\nexit 0\n")

    _write_executable(
        fake_bin / "launchctl",
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$INSTALLER_TEST_LAUNCH_LOG"
command_name="${1:-}"
case "$command_name" in
  getenv) exit 0 ;;
  print)
    label="${2##*/}"
    test -f "$INSTALLER_TEST_LAUNCH_STATE/$label"
    ;;
  bootout)
    label="${2##*/}"
    rm -f "$INSTALLER_TEST_LAUNCH_STATE/$label"
    ;;
  bootstrap)
    plist="$3"
    label="$(basename "$plist" .plist)"
    if test -n "${INSTALLER_TEST_FAIL_BOOTSTRAP_LABEL:-}" \
      && test "$label" = "$INSTALLER_TEST_FAIL_BOOTSTRAP_LABEL" \
      && test ! -f "$INSTALLER_TEST_FAILURE_USED"; then
        : > "$INSTALLER_TEST_FAILURE_USED"
        exit 70
    fi
    : > "$INSTALLER_TEST_LAUNCH_STATE/$label"
    ;;
  *) exit 64 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
set -eu
output=""
url=""
while test "$#" -gt 0; do
  case "$1" in
    -o) output="$2"; shift 2 ;;
    http://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
printf '%s\\n' "$url" >> "$INSTALLER_TEST_CURL_LOG"
case "$url" in
  */health)
    printf '%s\\n' "$INSTALLER_TEST_DAEMON_HEALTH_PAYLOAD" > "$output"
    ;;
  */v1/models)
    printf '{"data":[{"id":"%s"}]}\\n' "$INSTALLER_TEST_ENDPOINT_MODEL" > "$output"
    ;;
  *) exit 64 ;;
esac
""",
    )

    model = "fixture/Qwen-1.7B"
    healthy_character_interior = {
        "status": "ready",
        "installed": True,
        "semantic_author_count": 1,
        "legacy_interface_invocations": 0,
        "parallel_character_author_conflicts": 0,
        "dual_write_conflicts": 0,
        "topology_issues": [],
        "topology_evidence": {
            "duplicate_purpose_owner_count": 0,
            "legacy_compatibility_route_installed": False,
            "semantic_author_ids": ["character-semantic-author:primary"],
        },
    }
    health_payload = daemon_health_payload or {
        "status": "ok",
        "world_v2_capture": {"status": "ready"},
        "character_interior": healthy_character_interior,
    }
    port = 18188
    configured_port = port + 1 if inconsistent_endpoint else port
    env_file = tmp_path / "install.env"
    env_file.write_text(
        "\n".join(
            (
                f"WORLD_V2_TEXT_ENDPOINT_ENABLED={'true' if text_endpoint_enabled else 'false'}",
                f"WORLD_V2_TEXT_ENDPOINT_PORT={port}",
                f"WORLD_V2_TEXT_ENDPOINT_BASE_URL=http://127.0.0.1:{configured_port}/v1",
                f"WORLD_V2_TEXT_ENDPOINT_MODEL={model}",
                f"WORLD_V2_TEXT_ENDPOINT_EXECUTABLE={endpoint}",
                "INSTALL_SILLYTAVERN=0",
                "DEEPSEEK_API_KEY=deployment-secret-must-not-be-printed",
                "",
            )
        )
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "GIRL_AGENT_INSTALL_ENV_FILE": str(env_file),
            "GIRL_AGENT_LAUNCH_AGENTS_DIR": str(launch_agents),
            "GIRL_AGENT_INSTALL_PYTHON": str(ROOT / ".venv/bin/python"),
            "GIRL_AGENT_INSTALL_HEALTH_TIMEOUT_SECONDS": "0",
            "GIRL_AGENT_DAEMON_HEALTH_URL": "http://127.0.0.1:18765/health",
            "INSTALLER_TEST_LAUNCH_LOG": str(launch_log),
            "INSTALLER_TEST_LAUNCH_STATE": str(state),
            "INSTALLER_TEST_CURL_LOG": str(curl_log),
            "INSTALLER_TEST_ENDPOINT_MODEL": model,
            "INSTALLER_TEST_DAEMON_HEALTH_PAYLOAD": json.dumps(
                health_payload,
                separators=(",", ":"),
            ),
            "INSTALLER_TEST_FAIL_BOOTSTRAP_LABEL": fail_bootstrap_label or "",
            "INSTALLER_TEST_FAILURE_USED": str(tmp_path / "failure-used"),
        }
    )
    return environment, launch_agents, state, launch_log


def _seed_installation(
    launch_agents: Path,
    state: Path,
    *,
    installed: set[str],
    loaded: set[str],
) -> dict[str, bytes]:
    before: dict[str, bytes] = {}
    for label in installed:
        content = f"old deployment for {label}\n".encode()
        path = launch_agents / f"{label}.plist"
        path.write_bytes(content)
        before[label] = content
    for label in loaded:
        (state / label).touch()
    return before


def _run_installer(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(INSTALLER)],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _loaded_labels(state: Path) -> set[str]:
    return {path.name for path in state.iterdir()}


def test_installer_preflight_failure_never_unloads_or_replaces_plists(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, launch_log = _installer_environment(
        tmp_path,
        text_endpoint_enabled=True,
        inconsistent_endpoint=True,
    )
    initial = BASE_LABELS | RETIRED_LABELS
    before = _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "Settings preflight failed" in result.stderr
    launch_calls = launch_log.read_text() if launch_log.exists() else ""
    assert "bootout " not in launch_calls
    assert "bootstrap " not in launch_calls
    assert _loaded_labels(state) == initial
    assert {
        label: (launch_agents / f"{label}.plist").read_bytes() for label in initial
    } == before
    assert "deployment-secret-must-not-be-printed" not in result.stdout + result.stderr


def test_enabled_endpoint_missing_executable_fails_before_launchctl_mutation(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, launch_log = _installer_environment(
        tmp_path,
        text_endpoint_enabled=True,
    )
    missing_endpoint = tmp_path / "mlx_lm.server"
    missing_endpoint.unlink()
    initial = BASE_LABELS
    _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "Enabled text endpoint executable is unavailable" in result.stderr
    launch_calls = launch_log.read_text() if launch_log.exists() else ""
    assert "bootout " not in launch_calls
    assert "bootstrap " not in launch_calls
    assert _loaded_labels(state) == initial


def test_installer_switches_exact_labels_and_verifies_both_health_endpoints(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, _ = _installer_environment(
        tmp_path,
        text_endpoint_enabled=True,
    )
    initial = BASE_LABELS | TEXT_LABELS | RETIRED_LABELS
    _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode == 0, result.stderr
    desired = BASE_LABELS | TEXT_LABELS
    assert _loaded_labels(state) == desired
    assert {
        path.stem for path in launch_agents.glob("*.plist")
    } == desired
    for label in desired:
        assert (launch_agents / f"{label}.plist").read_bytes() == (
            ROOT / "launchd" / f"{label}.plist"
        ).read_bytes()
    curl_calls = (tmp_path / "curl.log").read_text()
    assert "http://127.0.0.1:18765/health" in curl_calls
    assert "http://127.0.0.1:18188/v1/models" in curl_calls
    assert "deployment-secret-must-not-be-printed" not in result.stdout + result.stderr


def test_installer_rolls_back_when_unified_character_interior_is_missing(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, _ = _installer_environment(
        tmp_path,
        text_endpoint_enabled=False,
        daemon_health_payload={
            "status": "ok",
            "world_v2_capture": {"status": "ready"},
        },
    )
    initial = BASE_LABELS | RETIRED_LABELS
    before = _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "Daemon health did not become ready" in result.stderr
    assert "restoring prior files and loaded state" in result.stderr
    assert _loaded_labels(state) == initial
    assert {
        label: (launch_agents / f"{label}.plist").read_bytes() for label in initial
    } == before


def test_installer_rolls_back_when_unified_character_interior_has_parallel_author(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, _ = _installer_environment(
        tmp_path,
        text_endpoint_enabled=False,
        daemon_health_payload={
            "status": "ok",
            "world_v2_capture": {"status": "ready"},
            "character_interior": {
                "status": "ready",
                "installed": True,
                "semantic_author_count": 1,
                "legacy_interface_invocations": 0,
                "parallel_character_author_conflicts": 1,
                "dual_write_conflicts": 0,
                "topology_issues": [],
                "topology_evidence": {
                    "duplicate_purpose_owner_count": 0,
                    "legacy_compatibility_route_installed": False,
                    "semantic_author_ids": ["character-semantic-author:primary"],
                },
            },
        },
    )
    initial = BASE_LABELS
    before = _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "Daemon health did not become ready" in result.stderr
    assert _loaded_labels(state) == initial
    assert {
        label: (launch_agents / f"{label}.plist").read_bytes() for label in initial
    } == before


def test_installer_failure_restores_exact_plists_and_loaded_state(
    tmp_path: Path,
) -> None:
    environment, launch_agents, state, _ = _installer_environment(
        tmp_path,
        text_endpoint_enabled=False,
        fail_bootstrap_label="com.girl-agent.daemon",
    )
    initial = BASE_LABELS | TEXT_LABELS | RETIRED_LABELS
    before = _seed_installation(
        launch_agents,
        state,
        installed=initial,
        loaded=initial,
    )

    result = _run_installer(environment)

    assert result.returncode != 0
    assert "restoring prior files and loaded state" in result.stderr
    assert _loaded_labels(state) == initial
    assert {
        path.stem for path in launch_agents.glob("*.plist")
    } == initial
    assert {
        label: (launch_agents / f"{label}.plist").read_bytes() for label in initial
    } == before
    assert not (tmp_path / "curl.log").exists()
    assert "deployment-secret-must-not-be-printed" not in result.stdout + result.stderr
