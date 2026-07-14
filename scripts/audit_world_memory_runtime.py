"""Audit whether World facts and memory-like context reach live turn generation.

This is a deterministic replay harness, not a quality benchmark.  It uses a
probe model that only references the user's prior fact when that fact appears
in the actual prompt payload sent by the World-backed CompanionTurn path.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import tempfile

from companion_daemon.companion_turn import (
    CompanionTurn,
    ResponseBudget,
    TurnEnvelope,
    TurnOptions,
)
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.models import IncomingMessage
from companion_daemon.turn_transports import CaptureTurnTransport
from companion_daemon.world import WorldKernel


BASE = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


@dataclass
class TurnResult:
    text: str
    action_ids: tuple[str, ...]


class WorldMemoryProbeModel:
    """Reply only from facts visible in the real World prompt."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []
        self.outputs: list[str] = []
        self.selected_sources: list[str | None] = []

    async def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.8) -> str:
        return await self.complete_json(messages, temperature=temperature)

    async def complete_json(
        self, messages: list[dict[str, str]], *, temperature: float = 0.8
    ) -> str:
        self.calls.append(messages)
        joined = "\n".join(item["content"] for item in messages)
        if "严格的虚拟世界事实审计器" in joined:
            output = json.dumps(
                {"supported": True, "unsupported_spans": [], "reason": "probe audit pass"},
                ensure_ascii=False,
            )
            self.outputs.append(output)
            return output
        fact_source = _source_id_near(joined, "安徽") or _source_id_near(joined, "床很软")
        self.selected_sources.append(fact_source)
        if fact_source:
            reply_text = "你刚睡醒的话，安徽老家那张很软的床确实会让人更想赖一会儿。"
            output = json.dumps(
                {
                    "reply_text": reply_text,
                    "expression_beats": [{"text": reply_text, "delay_ms": 0}],
                    "display_strategy": "自然承接，不把记忆显摆成客服式复述",
                    "private_impression": None,
                    "private_commitment": None,
                    "mentioned_event_ids": [fact_source],
                    "proposed_action_ids": [],
                    "claims": [
                        {
                            "source_id": fact_source,
                            "text": "我人在安徽老家，家里的床很软",
                            "assertion": "安徽老家那张很软的床",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            self.outputs.append(output)
            return output
        reply_text = "我这边没拿到你前面说的事，只能先按你刚睡醒来接。"
        output = json.dumps(
            {
                "reply_text": reply_text,
                "expression_beats": [{"text": reply_text, "delay_ms": 0}],
                "display_strategy": "缺少上下文时明确承认",
                "private_impression": None,
                "private_commitment": None,
                "mentioned_event_ids": [],
                "proposed_action_ids": [],
                "claims": [],
            },
            ensure_ascii=False,
        )
        self.outputs.append(output)
        return output


def _source_id_near(prompt: str, needle: str) -> str | None:
    idx = prompt.find(needle)
    if idx < 0:
        return None
    window = prompt[max(0, idx - 600) : idx + 600]
    matches = re.findall(r'"source_id":"([^"]+)"|"source_id": "([^"]+)"', window)
    source_ids = [left or right for left, right in matches if left or right]
    for source_id in source_ids:
        if source_id.startswith("message:"):
            return source_id
    for source_id in source_ids:
        if source_id:
            return source_id
    return None


def _message(text: str, index: int) -> IncomingMessage:
    return IncomingMessage(
        platform="qq",
        platform_user_id="geoff",
        message_id=f"world-memory-audit-{index}",
        text=text,
        sent_at=BASE + timedelta(minutes=index),
    )


async def _respond(engine: CompanionEngine, incoming: IncomingMessage) -> TurnResult:
    context = engine.freeze_turn_context(incoming)
    transport = CaptureTurnTransport(receipt_namespace="world-memory-audit")
    turn = CompanionTurn(engine, transport, cadence_delay_seconds=0)
    envelope = TurnEnvelope.from_message(
        incoming,
        idempotency_key=f"{incoming.platform}:{incoming.platform_user_id}:{incoming.message_id}",
        world_id=engine.world_id,
        canonical_user_id=engine.store.resolve_user(
            incoming.platform, incoming.platform_user_id
        ),
        frozen_cadence=context.cadence.heat,
    )
    outcome = await turn.respond(
        envelope,
        budget=ResponseBudget(first_visible_by_ms=8_000, complete_by_ms=12_000),
        options=TurnOptions(turn_context=context),
    )
    await turn.wait_for_delivery_continuations()
    return TurnResult(text=transport.text, action_ids=tuple(outcome.action_ids))


async def run_audit(*, keep_db: Path | None = None) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = keep_db or Path(tmp_dir) / "world-memory-audit.sqlite"
        store = CompanionStore(db_path)
        seed_user(store)
        world = WorldKernel(store)
        world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
        model = WorldMemoryProbeModel()
        engine = CompanionEngine(
            store,
            model,
            "你是沈知栀。",
            world_kernel=world,
            world_id=world_id,
            visual_identity_path=None,
        )
        first = await _respond(engine, _message("我人在安徽老家，家里的床很软。", 1))
        second = await _respond(engine, _message("我刚睡醒，还有点想赖床。", 2))
        snapshot = world.snapshot(world_id)
        fact_values = [
            str(item.get("value") or "")
            for item in snapshot.get("facts", {}).values()
            if isinstance(item, dict)
        ]
        second_prompt = "\n".join(item["content"] for item in model.calls[-1])
        report = {
            "ok": (
                any("安徽" in value for value in fact_values)
                and "安徽" in second_prompt
                and "安徽" in second.text
            ),
            "world_id": world_id,
            "db_path": str(db_path),
            "fact_values": fact_values,
            "first_reply": first.text,
            "second_reply": second.text,
            "second_prompt_contains_anhui": "安徽" in second_prompt,
            "second_prompt_contains_soft_bed": "床很软" in second_prompt,
            "second_action_ids": list(second.action_ids),
            "model_call_count": len(model.calls),
            "selected_sources": model.selected_sources,
            "model_outputs": model.outputs[-4:],
        }
        await engine.aclose()
        return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-db", type=Path)
    args = parser.parse_args()
    report = asyncio.run(run_audit(keep_db=args.keep_db))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
