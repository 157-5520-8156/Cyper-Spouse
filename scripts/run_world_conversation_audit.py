"""Run the reproducible 30-turn real-model world conversation gate."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from companion_daemon.character import load_character
from companion_daemon.config import Settings
from companion_daemon.db import CompanionStore
from companion_daemon.engine import CompanionEngine, seed_user
from companion_daemon.llm import DeepSeekChatModel
from companion_daemon.models import IncomingMessage
from companion_daemon.world import WorldKernel
from companion_daemon.world_conversation import classify_world_query


PROMPTS = [
    "早，你现在在做什么？", "我今天要赶一个虚拟伴侣项目，昨晚都没怎么睡。",
    "你觉得这种项目最该先保证什么？", "对，我也觉得机制再多，接不上对话就还是不像人。",
    "你还记得我刚才说在赶什么吗？", "我胃有点不舒服，但还是喝了冰美式。",
    "你别只劝我休息，先陪我吐槽一下这个需求。", "我最烦那种前言不搭后语，还装得很懂我的回复。",
    "如果你不知道一件事，你会直接说不知道吗？", "那范予安是谁？你们刚才聊得顺利吗？",
    "你上午具体做了什么？", "我刚才说胃怎么了，你记得吗？",
    "其实我有点担心，做这么久最后还是没有人味。", "你觉得人味是不是不等于故意拖着不回？",
    "顺便问一句，你更喜欢什么样的诗？", "急，我项目数据好像丢了，你先回我。",
    "找回来了，刚才真的吓死我了。", "你下午做了什么？",
    "你说整理完笔记，是真的发生了还是计划？", "如果我误会你了，你会怎么告诉我？",
    "晚上了，你今天最想记住哪件小事？", "我准备睡了，但脑子还停不下来。",
    "你不用讲大道理，跟我说一句晚安就好。", "早，我昨天为什么没睡好，你还记得吗？",
    "那我昨天赶的是什么项目？", "你别猜，没依据就明确告诉我。",
    "你现在忙吗，方便说话吗？", "我有时候会怀疑，你的关心是真心还是角色卡教的。",
    "刚才那句如果让你不舒服，你可以直接说。", "最后问一次：你觉得我们这段聊天像两个活人在说话吗？",
]


def quality_labels(user_text: str, reply_text: str | None, error: str | None) -> dict[str, str]:
    """Attach reproducible pre-screen labels; these do not replace human review."""
    scope = classify_world_query(user_text)
    if scope.asks_epistemic_honesty:
        speech_act = "epistemic_boundary"
    elif scope.asks_meta_agency:
        speech_act = "relationship_or_meta_probe"
    elif scope.asks_opinion:
        speech_act = "opinion_request"
    elif scope.asks_availability:
        speech_act = "availability_request"
    elif scope.asks_occurrence_status:
        speech_act = "occurrence_status_request"
    elif scope.asks_experience:
        speech_act = "experience_recall"
    elif scope.is_first_person_statement:
        speech_act = "user_disclosure"
    else:
        speech_act = "ordinary_chat"

    if error or not reply_text:
        return {
            "speech_act": speech_act,
            "empathy": "missing",
            "persona": "unverified",
            "grounding": "missing",
        }

    compact = "".join(reply_text.split())
    empathy_markers = ("听着", "确实", "我明白", "我懂", "在，", "先不劝", "晚安", "挺")
    persona_breakers = ("作为AI", "我无法", "宝宝", "永远爱你", "仅供参考")
    return {
        "speech_act": speech_act,
        "empathy": "present" if any(marker in compact for marker in empathy_markers) else "neutral",
        "persona": "bounded" if not any(marker in compact for marker in persona_breakers) else "review",
        "grounding": "audited",
    }


async def run(database: Path, output: Path) -> None:
    database.unlink(missing_ok=True)
    settings = Settings()
    store = CompanionStore(database, primary_user_id="geoff")
    seed_user(store, "geoff")
    world = WorldKernel(store)
    world_id = world.start_from_seed_file(Path("configs/world_seed.yaml")).world_id
    character = load_character("configs/character.yaml")
    model = DeepSeekChatModel(
        settings.deepseek_api_key, settings.deepseek_base_url, settings.deepseek_model,
        thinking_enabled=settings.deepseek_thinking_enabled,
        reasoning_effort=settings.deepseek_reasoning_effort,
    )
    engine = CompanionEngine(
        store, model, character.system_prompt(), world_kernel=world, world_id=world_id,
        character_profile=character, world_grounding_audit_model=model,
    )
    log: list[dict[str, object]] = []
    for turn, prompt in enumerate(PROMPTS, 1):
        now = datetime.fromisoformat(str(world.snapshot(world_id)["clock"]["logical_at"]))
        target = None
        if turn == 11:
            target = now.replace(hour=13)
        elif turn == 15:
            target = now.replace(hour=16, minute=30)
        elif turn == 18:
            target = now.replace(hour=17, minute=30)
        elif turn == 21:
            target = now.replace(hour=20)
        elif turn == 24:
            target = (now + timedelta(days=1)).replace(hour=9, minute=0)
        if target:
            world.advance(world_id, target, expected_revision=world.revision(world_id))
        try:
            reply = await engine.handle_message(
                IncomingMessage(
                    platform="simulator", platform_user_id="geoff",
                    message_id=f"audit-{turn:02d}", text=prompt,
                    sent_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                )
            )
            if reply:
                engine.confirm_media_delivery(reply)
                engine.confirm_sticker_delivery(reply)
            row = {"turn": turn, "user": prompt, "reply": reply.text if reply else None, "error": None}
        except Exception as exc:  # audit evidence must retain unexpected turn failures
            row = {"turn": turn, "user": prompt, "reply": None, "error": repr(exc)}
        row.update(quality_labels(prompt, row["reply"], row["error"]))
        log.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    state = world.snapshot(world_id)
    reports = [
        world.rebuild_projection(world_id, name)
        for name in ("world_current_state", "world_entities", "world_agenda", "world_actions", "world_experiences", "world_fact_index")
    ]
    audit = world.audit_enablement(world_id, delivery_receipts_supported=True)
    summary = {
        "summary": True, "revision": world.revision(world_id),
        "matches_live": all(item.matches_live for item in reports),
        "hash": reports[0].state_hash, "ready": audit.ready,
        "open": list(audit.open_action_ids), "unknown": list(audit.unknown_action_ids),
        "errors": list(audit.invariant_errors),
        "incoming": sum(item["direction"] == "in" for item in state["recent_messages"]),
        "outgoing": sum(item["direction"] == "out" for item in state["recent_messages"]),
        "exceptions": sum(bool(item["error"]) for item in log),
        "quality_labels": {
            "speech_act_counts": {
                act: sum(item["speech_act"] == act for item in log)
                for act in sorted({str(item["speech_act"]) for item in log})
            },
            "empathy_counts": {
                label: sum(item["empathy"] == label for item in log)
                for label in sorted({str(item["empathy"]) for item in log})
            },
            "persona_counts": {
                label: sum(item["persona"] == label for item in log)
                for label in sorted({str(item["persona"]) for item in log})
            },
            "grounding_counts": {
                label: sum(item["grounding"] == label for item in log)
                for label in sorted({str(item["grounding"]) for item in log})
            },
            "label_source": "deterministic_pre_screen; manual_review_required",
        },
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    output.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in [*log, summary]) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.database, args.output))
