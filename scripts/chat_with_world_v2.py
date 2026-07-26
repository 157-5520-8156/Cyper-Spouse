"""Interactively chat through the production World-v2 host with captured delivery."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import time

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


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
    delivery = ConsoleDelivery()
    settings = Settings(
        database_path=database,
        PRIMARY_USER_ID=args.primary_user_id,
    )
    started = time.perf_counter()
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=args.recipient_id,
        bootstrap_at=datetime.now(UTC),
        delivery=delivery,
    )
    print(
        f"READY database={database} startup_ms={(time.perf_counter() - started) * 1000:.1f}",
        flush=True,
    )
    turn = 0
    try:
        if args.burst_message:
            before = len(delivery.sent)
            started = time.perf_counter()
            tasks: list[asyncio.Task[object]] = []
            for text in args.burst_message:
                turn += 1
                tasks.append(
                    asyncio.create_task(
                        host.inbound_text(
                            message_id=f"world-v2-console-{turn}-{time.time_ns()}",
                            recipient_id=args.recipient_id,
                            text=text,
                            observed_at=datetime.now(UTC),
                        )
                    )
                )
                if args.burst_interval_seconds:
                    await asyncio.sleep(args.burst_interval_seconds)
            results = await asyncio.gather(*tasks)
            await host.drain(max_action_units=8, max_background_units=0)
            elapsed_ms = (time.perf_counter() - started) * 1000
            visible = delivery.sent[before:]
            for kind, _recipient, body in visible:
                print(f"ROLE[{kind}]> {body}", flush=True)
            print(
                "META> burst="
                f"{len(tasks)} statuses={[result.status for result in results]} "
                f"elapsed_ms={elapsed_ms:.1f} outputs={len(visible)}",
                flush=True,
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
            before = len(delivery.sent)
            started = time.perf_counter()
            result = await host.inbound_text(
                message_id=f"world-v2-console-{turn}-{time.time_ns()}",
                recipient_id=args.recipient_id,
                text=text,
                observed_at=datetime.now(UTC),
            )
            await host.drain(max_action_units=8, max_background_units=0)
            elapsed_ms = (time.perf_counter() - started) * 1000
            visible = delivery.sent[before:]
            for kind, _recipient, body in visible:
                print(f"ROLE[{kind}]> {body}", flush=True)
            print(
                f"META> status={result.status} elapsed_ms={elapsed_ms:.1f} "
                f"outputs={len(visible)}",
                flush=True,
            )
    finally:
        await host.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--clone", type=Path)
    parser.add_argument("--primary-user-id", default="geoff")
    parser.add_argument("--recipient-id", default="world-v2-console-user")
    parser.add_argument("--burst-message", action="append", default=[])
    parser.add_argument("--burst-interval-seconds", type=float, default=0.25)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
