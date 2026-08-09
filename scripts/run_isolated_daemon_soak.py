"""Run an explicitly opt-in, capture-only World V2 daemon soak.

This is an acceptance harness, not a production supervisor.  It always uses a
temporary SQLite database and a loopback OneBot capture.  Real provider traffic
requires ``--allow-real-provider``; a full-day run additionally requires
``--confirm-24h``.  The script deliberately has no production-daemon restart or
real-QQ transport path.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Literal

import httpx


_ROOT = Path(__file__).resolve().parents[1]
_ACCEPTANCE_SCRIPT = _ROOT / "scripts" / "run_isolated_daemon_acceptance.py"
SoakModelMode = Literal["loopback-stub", "real-provider"]
SOAK_CONTRACT = "isolated-daemon-soak.1"
DAY_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class SoakOptions:
    output: Path
    duration_seconds: float
    model_mode: SoakModelMode
    allow_real_provider: bool
    confirm_24h: bool
    turn_interval_seconds: float = 3_600.0
    restart_interval_seconds: float = 21_600.0
    snapshot_interval_seconds: float = 600.0
    max_turns: int | None = 24
    startup_timeout_seconds: float = 120.0


def _event_path(output: Path) -> Path:
    return output.with_suffix(output.suffix + ".events.jsonl")


def validate_options(options: SoakOptions) -> None:
    """Fail closed before importing or starting any daemon process."""

    if options.duration_seconds <= 0 or options.duration_seconds > DAY_SECONDS:
        raise ValueError("duration must be greater than 0 and no more than 86400 seconds")
    if options.model_mode not in {"loopback-stub", "real-provider"}:
        raise ValueError("soak model mode must be loopback-stub or real-provider")
    if options.model_mode == "real-provider" and not options.allow_real_provider:
        raise ValueError("real-provider soak requires --allow-real-provider")
    if options.model_mode != "real-provider" and options.allow_real_provider:
        raise ValueError("--allow-real-provider is valid only with real-provider")
    if options.duration_seconds >= DAY_SECONDS and not options.confirm_24h:
        raise ValueError("24h soak requires --confirm-24h")
    if options.turn_interval_seconds < 1:
        raise ValueError("turn interval must be at least 1 second")
    if options.restart_interval_seconds < 0:
        raise ValueError("restart interval cannot be negative")
    if options.duration_seconds >= DAY_SECONDS and options.restart_interval_seconds == 0:
        raise ValueError("24h soak requires at least one planned restart interval")
    if options.snapshot_interval_seconds < 1:
        raise ValueError("snapshot interval must be at least 1 second")
    if options.max_turns is not None and options.max_turns < 1:
        raise ValueError("max turns must be positive when supplied")
    if not 5 <= options.startup_timeout_seconds <= 120:
        raise ValueError("startup timeout must be between 5 and 120 seconds")
    output = options.output.expanduser().resolve()
    if output.exists() or _event_path(output).exists():
        raise ValueError("soak output must not already exist")


def restart_due(*, started_at: float, now: float, interval_seconds: float) -> bool:
    """Return whether a planned restart boundary has been crossed."""

    return interval_seconds > 0 and now - started_at >= interval_seconds


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _json_safe(model_dump(mode="json"))
    return str(value)


class _EventJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        self._stream.write(
            json.dumps(_json_safe(record), ensure_ascii=False, sort_keys=True) + "\n"
        )
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


def build_soak_report(
    *,
    options: SoakOptions,
    started_at: datetime,
    finished_at: datetime,
    interrupted: bool,
    health_samples: list[dict[str, object]],
    turns: list[dict[str, object]],
    restarts: list[dict[str, object]],
    duplicate_effect_deltas: list[int],
    final_replay: dict[str, object],
    captured_effect_count: int,
    captured_provider_request_count: int,
    usage_budget: dict[str, object] | None,
    provenance: dict[str, object] | None = None,
    failures: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Build a report that can never be mistaken for release qualification."""

    report = {
        "contract": SOAK_CONTRACT,
        "started_at": started_at,
        "finished_at": finished_at,
        "configuration": asdict(options),
        "qualification_status": "manual_only",
        "provenance": provenance,
        "safety": {
            "temporary_database": True,
            "onebot_loopback_only": True,
            "real_qq_send_possible": False,
            "production_database_touched": False,
            "production_daemon_restarted": False,
            "external_provider": options.model_mode == "real-provider",
        },
        "scope_exclusions": [
            "real QQ transport",
            "production daemon restart or replacement",
            "wording or character-choice quality gate",
            "100-sample provider qualification",
        ],
        "interrupted": interrupted,
        "health_samples": health_samples,
        "turns": turns,
        "restarts": restarts,
        "continuity": {
            "duplicate_effect_deltas": duplicate_effect_deltas,
            "duplicate_provider_request_deltas": [
                int(item.get("duplicate_provider_request_delta", 0))
                for item in restarts
            ],
            "duplicate_authoritative_role_request_deltas": [
                int(item.get("duplicate_authoritative_role_request_delta", 0))
                for item in restarts
            ],
            "captured_effect_count": captured_effect_count,
            "captured_provider_request_count": captured_provider_request_count,
            "final_replay": final_replay,
        },
        "usage_budget": usage_budget,
        "failures": failures or [],
    }
    return _json_safe(report)  # type: ignore[return-value]


def _load_acceptance_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "girl_agent_isolated_daemon_acceptance", _ACCEPTANCE_SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load isolated daemon acceptance harness")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _health(acceptance: Any, daemon: Any) -> dict[str, object]:
    try:
        with httpx.Client(base_url=daemon.base_url, timeout=5, trust_env=False) as client:
            response = client.get("/health")
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("health response is not an object")
        return dict(payload)
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": {"type": type(exc).__name__, "message": str(exc)[:2_000]},
            "daemon_alive": daemon.process.poll() is None,
        }


def _error_record(*, phase: str, error: BaseException) -> dict[str, object]:
    return {
        "phase": phase,
        "type": type(error).__name__,
        "message": str(error)[:2_000],
    }


def _wait_for_provider_capture_quiet(
    provider_capture: Any,
    *,
    timeout_seconds: float = 5.0,
    quiet_seconds: float = 0.2,
) -> tuple[dict[str, object], ...]:
    """Return a snapshot after the capture boundary has stopped growing."""

    deadline = time.monotonic() + timeout_seconds
    previous = provider_capture.snapshot()
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        current = provider_capture.snapshot()
        if len(current) != len(previous):
            previous = current
            stable_since = time.monotonic()
            continue
        if time.monotonic() - stable_since >= quiet_seconds:
            return current
    raise TimeoutError("provider capture did not become quiet")


def _authoritative_role_request_count(
    records: tuple[dict[str, object], ...],
) -> int:
    """Count only character-authority invocations, excluding reviewer lanes."""

    return sum(record.get("authoritative_role_request") is True for record in records)


def run_soak(options: SoakOptions) -> dict[str, object]:
    """Run one temporary, loopback-only soak after all safety checks pass."""

    validate_options(options)
    acceptance = _load_acceptance_module()
    settings = acceptance._validated_provider_settings(  # noqa: SLF001 - acceptance-only seam
        model_mode=options.model_mode,
        allow_real_provider=options.allow_real_provider,
        production_source_authority=False,
    )
    ambient_database = Path(settings.database_path).expanduser().resolve()
    output = options.output.expanduser().resolve()
    event_path = _event_path(output)
    started_at = datetime.now(UTC)
    health_samples: list[dict[str, object]] = []
    turns: list[dict[str, object]] = []
    restarts: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    duplicate_effect_deltas: list[int] = []
    interrupted = False
    final_replay: dict[str, object] = {}
    usage_budget: dict[str, object] | None = None
    journal = _EventJournal(event_path)
    daemon: Any | None = None
    capture: Any | None = None
    provider_capture: Any | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="girl-agent-daemon-soak-") as raw_temp:
            temporary_root = Path(raw_temp).resolve()
            database = temporary_root / "isolated-world.sqlite"
            if database.resolve() == ambient_database:
                raise ValueError("soak database resolved to production")
            attachment_cache = temporary_root / "attachments"
            log_path = temporary_root / "daemon.log"
            upstream_base_url = (
                settings.deepseek_base_url
                if options.model_mode == "real-provider"
                else None
            )
            provider_manager = acceptance._provider_capture_server(  # noqa: SLF001
                mode=options.model_mode,
                upstream_base_url=upstream_base_url,
            )
            with acceptance._capture_server() as capture_pair:  # noqa: SLF001
                capture, capture_url = capture_pair
                with provider_manager as provider_pair:
                    provider_capture, provider_capture_url = provider_pair
                    daemon = acceptance._start_daemon(  # noqa: SLF001
                        database=database,
                        capture_url=capture_url,
                        attachment_cache=attachment_cache,
                        log_path=log_path,
                        model_mode=options.model_mode,
                        provider_capture_url=provider_capture_url,
                        production_source_authority=False,
                    )
                    health = acceptance._wait_for_health(  # noqa: SLF001
                        daemon, timeout_seconds=options.startup_timeout_seconds
                    )
                    health_samples.append(
                        {
                            "observed_at": datetime.now(UTC),
                            "reason": "startup",
                            "health": health,
                        }
                    )
                    journal.write(
                        {
                            "contract": SOAK_CONTRACT,
                            "record_type": "started",
                            "observed_at": datetime.now(UTC),
                            "model_mode": options.model_mode,
                        }
                    )
                    started_monotonic = time.monotonic()
                    deadline = started_monotonic + options.duration_seconds
                    next_turn = started_monotonic
                    next_snapshot = started_monotonic
                    next_restart = (
                        started_monotonic + options.restart_interval_seconds
                        if options.restart_interval_seconds > 0
                        else float("inf")
                    )
                    turn_index = 0
                    restart_index = 0
                    while time.monotonic() < deadline:
                        now = time.monotonic()
                        if now >= next_snapshot:
                            sample = _health(acceptance, daemon)
                            health_samples.append(
                                {
                                    "observed_at": datetime.now(UTC),
                                    "reason": "periodic",
                                    "health": sample,
                                }
                            )
                            usage = sample.get("usage_budget")
                            if isinstance(usage, dict):
                                usage_budget = dict(usage)
                            journal.write(
                                {
                                    "contract": SOAK_CONTRACT,
                                    "record_type": "health",
                                    "observed_at": datetime.now(UTC),
                                    "health": sample,
                                }
                            )
                            next_snapshot += options.snapshot_interval_seconds
                        if now >= next_turn and (
                            options.max_turns is None or turn_index < options.max_turns
                        ):
                            turn_index += 1
                            source_event_id = f"isolated-soak-turn-{turn_index}"
                            request_text = f"隔离 soak 样本 {turn_index}：请简短回应。"
                            started_turn = time.perf_counter()
                            try:
                                turn = acceptance._post_turn(  # noqa: SLF001
                                    daemon=daemon,
                                    source_event_id=source_event_id,
                                    text=request_text,
                                )
                                turn["turn_index"] = turn_index
                                turn["request_text"] = request_text
                                turn["elapsed_ms"] = round(
                                    (time.perf_counter() - started_turn) * 1_000, 3
                                )
                                acceptance._wait_for_durable_provider_acceptance_count(  # noqa: SLF001
                                    database,
                                    expected_count=turn_index,
                                )
                                turns.append(turn)
                                journal.write(
                                    {
                                        "contract": SOAK_CONTRACT,
                                        "record_type": "turn",
                                        "observed_at": datetime.now(UTC),
                                        "turn": turn,
                                    }
                                )
                            except Exception as exc:
                                failure = _error_record(phase="turn", error=exc)
                                failure["turn_index"] = turn_index
                                failures.append(failure)
                                journal.write(
                                    {
                                        "contract": SOAK_CONTRACT,
                                        "record_type": "failure",
                                        "observed_at": datetime.now(UTC),
                                        "failure": failure,
                                    }
                                )
                            next_turn += options.turn_interval_seconds
                        if now >= next_restart:
                            restart_index += 1
                            _wait_for_provider_capture_quiet(provider_capture)
                            before_effects = capture.visible_count()
                            provider_records_before_duplicate = provider_capture.snapshot()
                            before_requests = len(provider_records_before_duplicate)
                            before_authoritative_role_requests = (
                                _authoritative_role_request_count(provider_records_before_duplicate)
                            )
                            duplicate_turn: dict[str, object] | None = None
                            if turns:
                                previous = turns[-1]
                                duplicate_text = str(
                                    previous.get("request_text")
                                    or f"隔离 soak 样本 {previous['turn_index']}：请简短回应。"
                                )
                                duplicate_turn = acceptance._post_turn(  # noqa: SLF001
                                    daemon=daemon,
                                    source_event_id=str(previous["source_event_id"]),
                                    text=duplicate_text,
                                )
                                # The ingress response only acknowledges the
                                # observation.  Give the asynchronous
                                # expression/action path the same bounded
                                # observation window as the acceptance harness
                                # before stopping the process or measuring
                                # effect-once deltas.
                                time.sleep(0.2)
                            if daemon is not None:
                                daemon.stop()
                            replay_before_restart = acceptance._cold_replay(  # noqa: SLF001
                                database
                            )
                            daemon = acceptance._start_daemon(  # noqa: SLF001
                                database=database,
                                capture_url=capture_url,
                                attachment_cache=attachment_cache,
                                log_path=log_path,
                                model_mode=options.model_mode,
                                provider_capture_url=provider_capture_url,
                                production_source_authority=False,
                            )
                            restarted_health = acceptance._wait_for_health(  # noqa: SLF001
                                daemon, timeout_seconds=options.startup_timeout_seconds
                            )
                            _wait_for_provider_capture_quiet(provider_capture)
                            after_effects = capture.visible_count()
                            provider_records_after_duplicate = provider_capture.snapshot()
                            after_requests = len(provider_records_after_duplicate)
                            after_authoritative_role_requests = _authoritative_role_request_count(
                                provider_records_after_duplicate
                            )
                            delta = after_effects - before_effects
                            duplicate_provider_request_delta = after_requests - before_requests
                            duplicate_authoritative_role_request_delta = (
                                after_authoritative_role_requests
                                - before_authoritative_role_requests
                            )
                            duplicate_effect_deltas.append(delta)
                            if duplicate_authoritative_role_request_delta:
                                failure = _error_record(
                                    phase="duplicate",
                                    error=RuntimeError(
                                        "duplicate source caused character-authority reauthoring: "
                                        f"{duplicate_authoritative_role_request_delta}"
                                    ),
                                )
                                failure["restart_index"] = restart_index
                                failures.append(failure)
                                journal.write(
                                    {
                                        "contract": SOAK_CONTRACT,
                                        "record_type": "failure",
                                        "observed_at": datetime.now(UTC),
                                        "failure": failure,
                                    }
                                )
                            restart_record = {
                                "restart_index": restart_index,
                                "observed_at": datetime.now(UTC),
                                "healthy": True,
                                "health": restarted_health,
                                "duplicate_turn": duplicate_turn,
                                "duplicate_effect_delta": delta,
                                "duplicate_provider_request_delta": duplicate_provider_request_delta,
                                "duplicate_authoritative_role_request_delta": (
                                    duplicate_authoritative_role_request_delta
                                ),
                                "duplicate_provider_request_evidence": list(
                                    provider_records_after_duplicate[before_requests:]
                                ),
                                "provider_request_evidence_before_duplicate": list(
                                    provider_records_before_duplicate
                                ),
                                "replay_before_restart": replay_before_restart,
                            }
                            restarts.append(restart_record)
                            health_samples.append(
                                {
                                    "observed_at": datetime.now(UTC),
                                    "reason": "restart",
                                    "health": restarted_health,
                                }
                            )
                            journal.write(
                                {
                                    "contract": SOAK_CONTRACT,
                                    "record_type": "restart",
                                    "observed_at": datetime.now(UTC),
                                    "restart": restart_record,
                                }
                            )
                            next_restart += options.restart_interval_seconds
                        due_times = [next_snapshot, next_turn, next_restart, deadline]
                        sleep_for = max(0.1, min(30.0, min(due_times) - time.monotonic()))
                        time.sleep(sleep_for)
            if daemon is not None and daemon.process.poll() is None:
                daemon.stop()
            final_replay = acceptance._cold_replay(database)  # noqa: SLF001
            if capture is not None:
                captured_effect_count = len(capture.snapshot())
            else:
                captured_effect_count = 0
            if provider_capture is not None:
                captured_provider_request_count = len(provider_capture.snapshot())
            else:
                captured_provider_request_count = 0
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        failures.append(_error_record(phase="soak", error=exc))
    finally:
        if daemon is not None:
            try:
                daemon.stop()
            except Exception as exc:
                failures.append(_error_record(phase="shutdown", error=exc))
        if capture is not None and "captured_effect_count" not in locals():
            captured_effect_count = len(capture.snapshot())
        if provider_capture is not None and "captured_provider_request_count" not in locals():
            captured_provider_request_count = len(provider_capture.snapshot())
        finished_at = datetime.now(UTC)
        report = build_soak_report(
            options=options,
            started_at=started_at,
            finished_at=finished_at,
            interrupted=interrupted,
            health_samples=health_samples,
            turns=turns,
            restarts=restarts,
            duplicate_effect_deltas=duplicate_effect_deltas,
            final_replay=final_replay,
            captured_effect_count=locals().get("captured_effect_count", 0),
            captured_provider_request_count=locals().get(
                "captured_provider_request_count", 0
            ),
            usage_budget=usage_budget,
            provenance=(
                acceptance._acceptance_provenance()  # noqa: SLF001
                if "acceptance" in locals()
                else None
            ),
            failures=failures,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        journal.write(
            {
                "contract": SOAK_CONTRACT,
                "record_type": "finished",
                "observed_at": finished_at,
                "report": output,
                "failure_count": len(failures),
            }
        )
        journal.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run an explicitly authorized temporary, loopback-only World V2 soak; "
            "never starts or replaces the production daemon."
        )
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument(
        "--model-mode", choices=("loopback-stub", "real-provider"), default="loopback-stub"
    )
    parser.add_argument("--allow-real-provider", action="store_true")
    parser.add_argument(
        "--confirm-24h",
        action="store_true",
        help="required for a duration of at least 86400 seconds",
    )
    parser.add_argument("--turn-interval-seconds", type=float, default=3_600.0)
    parser.add_argument("--restart-interval-seconds", type=float, default=21_600.0)
    parser.add_argument("--snapshot-interval-seconds", type=float, default=600.0)
    parser.add_argument("--max-turns", type=int, default=24)
    parser.add_argument("--startup-timeout-seconds", type=float, default=120.0)
    args = parser.parse_args()
    options = SoakOptions(
        output=args.output,
        duration_seconds=args.duration_seconds,
        model_mode=args.model_mode,
        allow_real_provider=args.allow_real_provider,
        confirm_24h=args.confirm_24h,
        turn_interval_seconds=args.turn_interval_seconds,
        restart_interval_seconds=args.restart_interval_seconds,
        snapshot_interval_seconds=args.snapshot_interval_seconds,
        max_turns=args.max_turns,
        startup_timeout_seconds=args.startup_timeout_seconds,
    )
    try:
        report = run_soak(options)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
