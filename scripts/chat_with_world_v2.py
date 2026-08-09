"""Interactively chat through the production World-v2 host with captured delivery."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import time
from typing import TextIO

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


TRANSCRIPT_CONTRACT = "chat-world-v2-transcript.1"


def json_safe(value: object) -> object:
    """Convert runtime evidence into JSON without dropping observability fields."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return json_safe(model_dump(mode="json"))
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)


class JsonlTranscript:
    """Flush each manual-chat evidence record so a killed session remains useful."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream: TextIO = self.path.open("a", encoding="utf-8")

    def write(self, record: dict[str, object]) -> None:
        self._stream.write(
            json.dumps(json_safe(record), ensure_ascii=False, sort_keys=True) + "\n"
        )
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


def _replay_snapshot(host: object) -> dict[str, object]:
    evidence = host.export_replay_evidence()  # type: ignore[attr-defined]
    projection = evidence.projection
    replay = evidence.replay
    actions = getattr(projection, "actions", ())
    return {
        "world_id": evidence.world_id,
        "cursor": json_safe(evidence.cursor),
        "event_count": len(evidence.events),
        "action_count": len(actions) if hasattr(actions, "__len__") else None,
        "semantic_hash": getattr(projection, "semantic_hash", None),
        "replay_semantic_hash": getattr(replay, "semantic_hash", None),
        "replay_matches_live": (
            getattr(projection, "semantic_hash", None)
            == getattr(replay, "semantic_hash", None)
        ),
    }


def build_chat_turn_record(
    *,
    turn_index: int,
    message_id: str,
    user_text: str,
    started_at: datetime,
    finished_at: datetime,
    result: object | None,
    drain_result: object | None,
    visible: tuple[tuple[str, str, str], ...],
    evidence: dict[str, object] | None,
    health: dict[str, object] | None,
    error: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one auditable user/role turn record for manual free dialogue."""

    result_status = getattr(result, "status", None)
    action_id = getattr(result, "action_id", None)
    record: dict[str, object] = {
        "contract": TRANSCRIPT_CONTRACT,
        "record_type": "turn",
        "turn_index": turn_index,
        "started_at": started_at,
        "finished_at": finished_at,
        "latency_ms": round((finished_at - started_at).total_seconds() * 1000, 3),
        "user": {"message_id": message_id, "text": user_text},
        "result": {"status": result_status, "action_id": action_id},
        "role_units": [
            {"kind": kind, "recipient_id": recipient_id, "body": body}
            for kind, recipient_id, body in visible
        ],
        "drain": (
            {
                "action_statuses": list(getattr(drain_result, "action_statuses", ())),
                "background_statuses": list(getattr(drain_result, "background_statuses", ())),
            }
            if drain_result is not None
            else None
        ),
        "evidence": evidence,
        "health": health,
    }
    if error is not None:
        record["error"] = error
    return json_safe(record)  # type: ignore[return-value]


def build_burst_result_records(
    *, burst_users: list[dict[str, object]], results: list[object]
) -> list[dict[str, object]]:
    """Keep each concurrent input's identity even when one task fails."""

    records: list[dict[str, object]] = []
    for index, result in enumerate(results, start=1):
        user = burst_users[index - 1]
        record: dict[str, object] = {
            "turn_index": user["turn_index"],
            "message_id": user["message_id"],
        }
        if isinstance(result, BaseException):
            record.update(
                {
                    "status": "error",
                    "error": {
                        "type": type(result).__name__,
                        "message": str(result)[:2_000],
                    },
                }
            )
        else:
            record.update(
                {
                    "status": getattr(result, "status", None),
                    "action_id": getattr(result, "action_id", None),
                }
            )
        records.append(record)
    return records


def build_session_failure_record(
    *, finished_at: datetime, error: BaseException
) -> dict[str, object]:
    """Build a durable startup failure record before re-raising the error."""

    return {
        "contract": TRANSCRIPT_CONTRACT,
        "record_type": "session_failed",
        "finished_at": finished_at,
        "error": {"type": type(error).__name__, "message": str(error)[:2_000]},
    }


def build_burst_drain_record(
    *, drain_result: object | None, error: BaseException | None = None
) -> dict[str, object]:
    """Serialize the burst drain outcome, including a failed drain attempt."""

    if error is not None:
        return {
            "status": "error",
            "error": {"type": type(error).__name__, "message": str(error)[:2_000]},
        }
    if drain_result is None:
        return {"status": "not_run"}
    return {
        "status": "ok",
        "action_statuses": list(getattr(drain_result, "action_statuses", ())),
        "background_statuses": list(getattr(drain_result, "background_statuses", ())),
    }


class ConsoleDelivery:
    """Capture the exact provider-visible output without sending it to QQ."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        self.sent.append(("text", recipient_id, text))
        return {
            "status": "ok",
            "data": {"message_id": f"world-v2-console-{len(self.sent)}"},
        }

    async def send_reaction(
        self, recipient_id: str, *, message_id: str, reaction_id: str
    ) -> dict[str, object]:
        self.sent.append(("reaction", recipient_id, f"{message_id}:{reaction_id}"))
        return {
            "status": "ok",
            "data": {"message_id": f"world-v2-console-{len(self.sent)}"},
        }

    async def send_sticker(
        self, recipient_id: str, *, sticker_id: str
    ) -> dict[str, object]:
        self.sent.append(("sticker", recipient_id, sticker_id))
        return {
            "status": "ok",
            "data": {"message_id": f"world-v2-console-{len(self.sent)}"},
        }

    async def send_typing(
        self, recipient_id: str, *, state: str
    ) -> dict[str, object]:
        self.sent.append(("typing", recipient_id, state))
        return {
            "status": "ok",
            "data": {"message_id": f"world-v2-console-{len(self.sent)}"},
        }


def _clone_database(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        raise ValueError("source and target databases must differ")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        with sqlite3.connect(target) as target_db:
            source_db.backup(target_db)


async def _run(args: argparse.Namespace) -> None:
    database = args.database.resolve()
    if args.clone is not None:
        _clone_database(args.clone.resolve(), database)
    jsonl_path = getattr(args, "jsonl", None)
    recorder = JsonlTranscript(jsonl_path) if jsonl_path is not None else None
    background_units = int(getattr(args, "background_units", 0))
    disable_recall_embedding = bool(getattr(args, "disable_recall_embedding", False))
    delivery = ConsoleDelivery()
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID=args.primary_user_id,
    )
    started = time.perf_counter()
    try:
        host = build_qq_c2c_host(
            settings=settings,
            recipient_id=args.recipient_id,
            bootstrap_at=datetime.now(UTC),
            delivery=delivery,
            use_configured_recall_embedding=not disable_recall_embedding,
        )
    except Exception as exc:
        if recorder is not None:
            try:
                recorder.write(
                    build_session_failure_record(
                        finished_at=datetime.now(UTC), error=exc
                    )
                )
            finally:
                recorder.close()
        raise
    session_started_at = datetime.now(UTC)
    print(
        f"READY database={database} startup_ms={(time.perf_counter() - started) * 1000:.1f}",
        flush=True,
    )
    if recorder is not None:
        recorder.write(
            {
                "contract": TRANSCRIPT_CONTRACT,
                "record_type": "session_started",
                "started_at": session_started_at,
                "database": database,
                "clone": args.clone,
                "primary_user_id": args.primary_user_id,
                "recipient_id": args.recipient_id,
                "provider": {
                    "model": settings.deepseek_model,
                    "api_key_configured": bool(settings.deepseek_api_key),
                    "base_url": settings.deepseek_base_url,
                },
                "recall_embedding_disabled": disable_recall_embedding,
                "background_units": background_units,
            }
        )

    async def _observability() -> tuple[dict[str, object] | None, dict[str, object] | None]:
        try:
            world_health = await host.world_health_diagnostics()
            health: dict[str, object] = {
                "world": world_health,
                "usage_budget": host.usage_budget_health(),
                "latency_samples": host.latency_samples(),
                "external_perception": host.external_world_perception_health(),
            }
            return _replay_snapshot(host), json_safe(health)  # type: ignore[return-value]
        except Exception as exc:  # pragma: no cover - evidence must not hide a turn error
            return None, {
                "observability_error": {
                    "type": type(exc).__name__,
                    "message": str(exc)[:2_000],
                }
            }

    async def _process_turn(
        *, turn_index: int, message_id: str, user_text: str
    ) -> tuple[object | None, object | None, tuple[tuple[str, str, str], ...], float, dict[str, object] | None]:
        before = len(delivery.sent)
        started_at = datetime.now(UTC)
        started_perf = time.perf_counter()
        result: object | None = None
        drain_result: object | None = None
        error: dict[str, object] | None = None
        try:
            result = await host.inbound_text(
                message_id=message_id,
                recipient_id=args.recipient_id,
                text=user_text,
                observed_at=started_at,
            )
            drain_result = await host.drain(
                max_action_units=8,
                max_background_units=background_units,
            )
        except Exception as exc:  # keep the failure visible and in the transcript
            error = {"type": type(exc).__name__, "message": str(exc)[:2_000]}
            print(f"ERROR> {error['type']}: {error['message']}", flush=True)
        finished_at = datetime.now(UTC)
        elapsed_ms = (time.perf_counter() - started_perf) * 1000
        visible = tuple(delivery.sent[before:])
        evidence, health = await _observability()
        if recorder is not None:
            recorder.write(
                build_chat_turn_record(
                    turn_index=turn_index,
                    message_id=message_id,
                    user_text=user_text,
                    started_at=started_at,
                    finished_at=finished_at,
                    result=result,
                    drain_result=drain_result,
                    visible=visible,
                    evidence=evidence,
                    health=health,
                    error=error,
                )
            )
        return result, drain_result, visible, elapsed_ms, error

    turn = 0
    try:
        if args.burst_message:
            before = len(delivery.sent)
            started = time.perf_counter()
            tasks: list[asyncio.Task[object]] = []
            burst_users: list[dict[str, object]] = []
            for text in args.burst_message:
                turn += 1
                message_id = f"world-v2-console-{turn}-{time.time_ns()}"
                observed_at = datetime.now(UTC)
                burst_users.append(
                    {
                        "text": text,
                        "turn_index": turn,
                        "message_id": message_id,
                        "observed_at": observed_at,
                    }
                )
                tasks.append(
                    asyncio.create_task(
                        host.inbound_text(
                            message_id=message_id,
                            recipient_id=args.recipient_id,
                            text=text,
                            observed_at=observed_at,
                        )
                    )
                )
                if args.burst_interval_seconds:
                    await asyncio.sleep(args.burst_interval_seconds)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            drain_error: BaseException | None = None
            try:
                drain_result = await host.drain(
                    max_action_units=8, max_background_units=background_units
                )
            except Exception as exc:
                drain_error = exc
                print(f"ERROR> {type(exc).__name__}: {str(exc)[:2_000]}", flush=True)
            elapsed_ms = (time.perf_counter() - started) * 1000
            visible = delivery.sent[before:]
            for kind, _recipient, body in visible:
                print(f"ROLE[{kind}]> {body}", flush=True)
            result_records = build_burst_result_records(
                burst_users=burst_users, results=list(results)
            )
            print(
                "META> burst="
                f"{len(tasks)} statuses={[item['status'] for item in result_records]} "
                f"elapsed_ms={elapsed_ms:.1f} outputs={len(visible)}",
                flush=True,
            )
            if recorder is not None:
                evidence, health = await _observability()
                recorder.write(
                    {
                        "contract": TRANSCRIPT_CONTRACT,
                        "record_type": "burst",
                        "finished_at": datetime.now(UTC),
                        "latency_ms": round(elapsed_ms, 3),
                        "users": burst_users,
                        "results": result_records,
                        "role_units": [
                            {
                                "kind": kind,
                                "recipient_id": recipient_id,
                                "body": body,
                            }
                            for kind, recipient_id, body in visible
                        ],
                        "drain": build_burst_drain_record(
                            drain_result=drain_result, error=drain_error
                        ),
                        "evidence": evidence,
                        "health": health,
                    }
                )
            return
        while True:
            try:
                text = input("YOU> ").strip()
            except EOFError:
                break
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                break
            turn += 1
            message_id = f"world-v2-console-{turn}-{time.time_ns()}"
            result, _drain_result, visible, elapsed_ms, error = await _process_turn(
                turn_index=turn,
                message_id=message_id,
                user_text=text,
            )
            for kind, _recipient, body in visible:
                print(f"ROLE[{kind}]> {body}", flush=True)
            print(
                f"META> status={getattr(result, 'status', 'error')} elapsed_ms={elapsed_ms:.1f} "
                f"outputs={len(visible)}",
                flush=True,
            )
            if error is not None:
                continue
    finally:
        if recorder is not None:
            try:
                evidence, health = await _observability()
                recorder.write(
                    {
                        "contract": TRANSCRIPT_CONTRACT,
                        "record_type": "session_closed",
                        "finished_at": datetime.now(UTC),
                        "turn_count": turn,
                        "evidence": evidence,
                        "health": health,
                    }
                )
            finally:
                recorder.close()
        await host.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--clone", type=Path)
    parser.add_argument("--primary-user-id", default="geoff")
    parser.add_argument("--recipient-id", default="world-v2-console-user")
    parser.add_argument("--burst-message", action="append", default=[])
    parser.add_argument("--burst-interval-seconds", type=float, default=0.25)
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="append session, turn, evidence, latency, usage, and error records to JSONL",
    )
    parser.add_argument(
        "--background-units",
        type=int,
        default=0,
        help="bounded background drain units after each manual turn (default: 0)",
    )
    parser.add_argument(
        "--disable-recall-embedding",
        action="store_true",
        help="disable configured semantic recall embedding for an isolated qualification run",
    )
    args = parser.parse_args()
    if not 0 <= args.background_units <= 64:
        parser.error("background units must be between 0 and 64")
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
