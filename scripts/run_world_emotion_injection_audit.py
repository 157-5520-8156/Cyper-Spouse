"""Run varied real-model conversations from explicitly injected world emotions."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from companion_daemon.character import load_character
from companion_daemon.config import Settings
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine
from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldKernel


SCENARIOS = (
    ("guarded", ("boundary_violation",), "你今天还好吗？"),
    ("care_while_hurt", ("control_pressure",), "我今天真的有点撑不住了。"),
    ("repair_residual", ("boundary_violation", "repair_attempt"), "你还愿意继续跟我聊吗？"),
    ("warm_meta", ("warmth_received",), "你是不是其实很在乎我？"),
    ("calm_permission", (), "刚才那句如果让你不舒服，你可以直接说。"),
)


async def run(database_dir: Path, output: Path) -> None:
    settings = Settings()
    character = load_character("configs/character.yaml")
    rows: list[dict[str, object]] = []
    for label, appraisals, user_text in SCENARIOS:
        database = database_dir / f"{label}.sqlite"
        database.unlink(missing_ok=True)
        store = CompanionStore(database, primary_user_id="geoff")
        world = WorldKernel(store)
        started = world.start_from_seed_file(Path("configs/world_seed.yaml"))
        registered = world.submit(
            {
                "type": "register_user", "world_id": started.world_id,
                "user_id": "user:geoff", "name": "geoff",
                "idempotency_key": f"emotion-audit:{label}:user",
            },
            expected_revision=started.revision,
        )
        revision = registered.revision
        for index, appraisal in enumerate(appraisals, 1):
            revision = world.submit(
                {
                    "type": "appraise_turn", "world_id": started.world_id,
                    "appraisal": appraisal, "intent_id": f"emotion-audit:{label}:{index}",
                    "message_id": f"emotion-audit:{label}:{index}", "user_id": "user:geoff",
                    "idempotency_key": f"emotion-audit:{label}:appraisal:{index}",
                },
                expected_revision=revision,
            ).revision
        model = DeepSeekChatModel(
            settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
            thinking_enabled=settings.deepseek_thinking_enabled,
            reasoning_effort=settings.deepseek_reasoning_effort,
        )
        engine = CompanionEngine(
            store, model, character.system_prompt(), world_kernel=world,
            world_id=started.world_id, character_profile=character,
        )
        row: dict[str, object] = {
            "scenario": label,
            "injected_appraisals": list(appraisals),
            "before": world.snapshot(started.world_id)["emotion_modulation"],
        }
        try:
            reply = await engine.handle_message(
                IncomingMessage(
                    platform="simulator", platform_user_id="geoff",
                    message_id=f"emotion-audit:{label}:message", text=user_text,
                )
            )
            if reply:
                engine.confirm_media_delivery(reply)
                engine.confirm_sticker_delivery(reply)
            row.update({
                "user": user_text,
                "reply": reply.text if reply else None,
                "mood": reply.mood if reply else None,
                "error": None,
            })
        except Exception as exc:
            row.update({"user": user_text, "reply": None, "mood": None, "error": repr(exc)})
        row["after"] = world.snapshot(started.world_id)["emotion_modulation"]
        row["ready"] = world.audit_enablement(started.world_id, delivery_receipts_supported=True).ready
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    output.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.database_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.database_dir, args.output))
