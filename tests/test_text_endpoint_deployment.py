from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts" / "text_endpoint_watchdog.sh"


def _watchdog_environment(
    tmp_path: Path,
    *,
    models_exit: int,
    inference_exit: int,
) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    curl_log = tmp_path / "curl.log"
    launch_log = tmp_path / "launchctl.log"
    curl = fake_bin / "curl"
    curl.write_text(
        """#!/bin/sh
url=""
for argument in "$@"; do
    url="$argument"
done
printf '%s\\n' "$url" >> "$WATCHDOG_TEST_CURL_LOG"
case "$url" in
    */v1/models) exit "$WATCHDOG_TEST_MODELS_EXIT" ;;
    */v1/chat/completions) exit "$WATCHDOG_TEST_INFERENCE_EXIT" ;;
    *) exit 64 ;;
esac
"""
    )
    curl.chmod(0o755)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
printf '%s\\n' "$*" >> "$WATCHDOG_TEST_LAUNCH_LOG"
"""
    )
    launchctl.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "TMPDIR": str(tmp_path),
            "WORLD_V2_TEXT_ENDPOINT_WATCHDOG_LOG_FILE": str(tmp_path / "watchdog.log"),
            # Consecutive-failure tests invoke the 120-second launchd job
            # back-to-back. Production keeps the script's 120-second
            # post-timeout capacity lease.
            "WORLD_V2_TEXT_ENDPOINT_CAPACITY_COOLDOWN_SECONDS": "0",
            "WATCHDOG_TEST_CURL_LOG": str(curl_log),
            "WATCHDOG_TEST_LAUNCH_LOG": str(launch_log),
            "WATCHDOG_TEST_MODELS_EXIT": str(models_exit),
            "WATCHDOG_TEST_INFERENCE_EXIT": str(inference_exit),
        }
    )
    return environment


def _run_watchdog(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WATCHDOG)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=5,
    )


def test_local_mlx_server_disables_unsafe_multi_prompt_batching() -> None:
    command = (ROOT / "scripts" / "run_text_endpoint.sh").read_text()

    assert "--decode-concurrency 1" in command
    assert "--prompt-concurrency 1" in command
    assert "--prompt-cache-bytes 512M" in command


def test_launchd_installer_cannot_resurrect_retired_semantic_services() -> None:
    installer = (ROOT / "scripts" / "install_launchd.sh").read_text()
    uninstaller = (ROOT / "scripts" / "uninstall_launchd.sh").read_text()

    assert "com.girl-agent.text-endpoint" in installer
    assert "com.girl-agent.text-endpoint-watchdog" in installer
    assert 'cp "$ROOT/launchd/com.girl-agent.proactive.plist"' not in installer
    assert '\nlaunchctl load "$LAUNCH_AGENTS/com.girl-agent.proactive.plist"' not in installer
    assert '\nlaunchctl load "$LAUNCH_AGENTS/com.girl-agent.local-appraisal.plist"' not in installer
    assert (
        '\nlaunchctl load "$LAUNCH_AGENTS/com.girl-agent.local-appraisal-watchdog.plist"'
        not in installer
    )
    assert "com.girl-agent.text-endpoint.plist" in uninstaller
    assert "com.girl-agent.text-endpoint-watchdog.plist" in uninstaller
    assert not (ROOT / "scripts" / "run_proactive_scheduler.sh").exists()
    assert not (ROOT / "launchd" / "com.girl-agent.proactive.plist").exists()


def test_text_endpoint_watchdog_probes_generation_not_only_the_listener() -> None:
    watchdog = WATCHDOG.read_text()

    assert "/v1/chat/completions" in watchdog
    assert "max_completion_tokens" in watchdog
    assert ":1" in watchdog
    assert "--data-binary" in watchdog


def test_healthy_model_list_treats_one_inference_timeout_as_busy_not_wedged(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=0,
        inference_exit=28,
    )
    # Keep the old watchdog from mutating the real service while this
    # regression test is red; a recent throttle marker makes its eager restart
    # harmless without affecting the new consecutive-failure state assertion.
    (tmp_path / "girl-agent-text-endpoint-watchdog.last").write_text(str(int(time.time())))

    result = _run_watchdog(environment)

    assert result.returncode == 0
    assert "/v1/models" in (tmp_path / "curl.log").read_text()
    assert (tmp_path / "girl-agent-text-endpoint-watchdog.failures").read_text().strip() == "1"
    assert not (tmp_path / "launchctl.log").exists()


def test_known_daemon_capacity_busy_skips_generation_probe_and_restart(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=0,
        inference_exit=28,
    )
    capacity = tmp_path / "girl-agent-text-endpoint.capacity"
    capacity.mkdir()
    (capacity / "state").write_text(
        f"{time.time() + 300:.6f}\ndaemon:test:1\nactive\n"
    )
    (tmp_path / "girl-agent-text-endpoint-watchdog.failures").write_text("2")

    first = _run_watchdog(environment)
    second = _run_watchdog(environment)

    assert first.returncode == second.returncode == 0
    probes = (tmp_path / "curl.log").read_text()
    assert probes.count("/v1/models") == 2
    assert "/v1/chat/completions" not in probes
    assert not (tmp_path / "launchctl.log").exists()
    assert not (tmp_path / "girl-agent-text-endpoint-watchdog.failures").exists()
    assert "capacity busy" in (tmp_path / "watchdog.log").read_text()


def test_watchdog_timeout_keeps_a_busy_lease_before_any_new_probe(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=0,
        inference_exit=28,
    )
    environment["WORLD_V2_TEXT_ENDPOINT_CAPACITY_COOLDOWN_SECONDS"] = "300"

    first = _run_watchdog(environment)
    second = _run_watchdog(environment)

    assert first.returncode == second.returncode == 0
    probes = (tmp_path / "curl.log").read_text()
    assert probes.count("/v1/models") == 2
    assert probes.count("/v1/chat/completions") == 1
    state = (
        tmp_path / "girl-agent-text-endpoint.capacity" / "state"
    ).read_text()
    assert "watchdog_cooldown" in state
    assert (
        tmp_path / "girl-agent-text-endpoint-watchdog.failures"
    ).read_text().strip() == "1"
    assert not (tmp_path / "launchctl.log").exists()


def test_three_consecutive_inference_failures_restart_a_healthy_listener(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=0,
        inference_exit=28,
    )

    first = _run_watchdog(environment)
    second = _run_watchdog(environment)

    assert first.returncode == second.returncode == 0
    assert (tmp_path / "girl-agent-text-endpoint-watchdog.failures").read_text().strip() == "2"
    assert not (tmp_path / "launchctl.log").exists()

    third = _run_watchdog(environment)

    assert third.returncode == 0
    assert (tmp_path / "launchctl.log").read_text().count("kickstart -k") == 1
    assert not (tmp_path / "girl-agent-text-endpoint-watchdog.failures").exists()


def test_successful_inference_clears_the_consecutive_failure_counter(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=0,
        inference_exit=28,
    )
    _run_watchdog(environment)
    _run_watchdog(environment)

    environment["WATCHDOG_TEST_INFERENCE_EXIT"] = "0"
    success = _run_watchdog(environment)

    assert success.returncode == 0
    assert not (tmp_path / "girl-agent-text-endpoint-watchdog.failures").exists()

    environment["WATCHDOG_TEST_INFERENCE_EXIT"] = "28"
    one_new_failure = _run_watchdog(environment)

    assert one_new_failure.returncode == 0
    assert (tmp_path / "girl-agent-text-endpoint-watchdog.failures").read_text().strip() == "1"
    assert not (tmp_path / "launchctl.log").exists()


def test_unhealthy_model_list_restarts_without_masking_a_wedged_service(
    tmp_path: Path,
) -> None:
    environment = _watchdog_environment(
        tmp_path,
        models_exit=7,
        inference_exit=0,
    )

    result = _run_watchdog(environment)

    assert result.returncode == 0
    assert (tmp_path / "launchctl.log").read_text().count("kickstart -k") == 1
    probes = (tmp_path / "curl.log").read_text()
    assert "/v1/models" in probes
    assert "/v1/chat/completions" not in probes
