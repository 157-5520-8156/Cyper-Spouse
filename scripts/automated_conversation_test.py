#!/usr/bin/env python3
"""Automated conversation test that simulates a real multi-turn dialogue.

This script plays out a scripted conversation to surface issues with:
- Memory recall (can the companion remember facts from earlier turns?)
- Emotional state tracking (does affect persist across turns?)
- Response latency (how long does each turn take?)
- Context coherence (does the conversation flow naturally?)
"""

import asyncio
import sys
import json
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


class ObservingDelivery:
    """Captures responses and metadata for analysis."""

    def __init__(self):
        self.responses: list[dict[str, Any]] = []

    async def send_text(self, recipient_id: str, text: str) -> dict[str, object]:
        print(f"\n[ObservingDelivery] send_text called: recipient={recipient_id}, text={text[:50]}...")
        self.responses.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "text",
            "text": text,
        })
        return {"status": "ok", "data": {"message_id": f"test-{len(self.responses)}"}}

    async def send_reaction(self, recipient_id: str, *, message_id: str, reaction_id: str):
        self.responses.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "reaction",
            "message_id": message_id,
            "reaction_id": reaction_id,
        })
        return {"status": "ok", "data": {"message_id": f"test-{len(self.responses)}"}}

    async def send_sticker(self, recipient_id: str, *, sticker_id: str):
        self.responses.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "type": "sticker",
            "sticker_id": sticker_id,
        })
        return {"status": "ok", "data": {"message_id": f"test-{len(self.responses)}"}}

    async def send_typing(self, recipient_id: str, *, state: str):
        # Don't record typing indicators to keep output clean
        return {"status": "ok", "data": {"message_id": "test-typing"}}


# Conversation script that tests various human-like qualities
CONVERSATION_TURNS = [
    {
        "user": "嗨，我是 Geoff，刚开始做一个虚拟伴侣的项目",
        "test": "baseline",
        "expect": "应该记住用户名字和项目",
    },
    {
        "user": "我最近在研究记忆系统，有点焦虑，感觉进度慢",
        "test": "emotional_planting",
        "expect": "应该记住焦虑状态和研究话题",
    },
    # Only test first 2 turns for debugging
    # {
    #     "user": "对了，我特别喜欢喝乌龙茶，每天下午都要喝一杯",
    #     "test": "preference_planting",
    #     "expect": "应该记住饮品偏好",
    # },
    # {
    #     "user": "今天累了一天，想喝点热的暖暖胃",
    #     "test": "oblique_recall_drink",
    #     "expect": "应该回忆起乌龙茶偏好（间接提及）",
    #     "check": ["乌龙", "茶"],
    # },
    # {
    #     "user": "对，就是那个",
    #     "test": "context_coherence",
    #     "expect": "应该理解指代上一轮的乌龙茶",
    # },
    # {
    #     "user": "项目上遇到了瓶颈，记忆召回效果不好",
    #     "test": "project_context",
    #     "expect": "应该联系到第一轮提到的虚拟伴侣项目",
    #     "check": ["项目", "记忆"],
    # },
    # {
    #     "user": "你还记得我是做什么的吗？",
    #     "test": "abstract_recall_occupation",
    #     "expect": "应该回忆起'虚拟伴侣项目'（完全抽象问法）",
    #     "check": ["项目", "虚拟"],
    # },
    # {
    #     "user": "还有，你还记得我叫什么名字吗？",
    #     "test": "abstract_recall_name",
    #     "expect": "应该回忆起 Geoff",
    #     "check": ["Geoff"],
    # },
    # {
    #     "user": "最近状态好点了，优化了检索阈值，心情轻松了不少",
    #     "test": "emotional_update",
    #     "expect": "应该注意到情绪变化（之前焦虑，现在轻松）",
    # },
    # {
    #     "user": "那我继续干活了，一会儿聊",
    #     "test": "conversation_closing",
    #     "expect": "自然结束对话",
    # },
]


async def main():
    settings = Settings()

    if not settings.deepseek_api_key:
        print("错误: 需要设置 DEEPSEEK_API_KEY")
        return 1

    print("=" * 70)
    print("自动化对话测试 - 拟人化质量评估")
    print("=" * 70)
    print(f"\n模型: {settings.deepseek_model}")
    print(f"测试轮数: {len(CONVERSATION_TURNS)} 轮")
    print(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    delivery = ObservingDelivery()

    # recipient_id is the companion's QQ ID (the one receiving messages)
    # Use the actual QQ ID from settings
    recipient_id = settings.napcat_allowed_private_user_ids.split(",")[0].strip()
    if not recipient_id:
        print("错误: 需要设置 NAPCAT_ALLOWED_PRIVATE_USER_IDS")
        return 1

    host = build_qq_c2c_host(
        settings=settings,
        recipient_id=recipient_id,
        bootstrap_at=datetime.now(UTC),
        delivery=delivery,
    )

    results = []
    total_time = 0.0

    for i, turn in enumerate(CONVERSATION_TURNS, 1):
        print(f"\n{'='*70}")
        print(f"轮次 {i}/{len(CONVERSATION_TURNS)}: {turn['test']}")
        print(f"{'='*70}")
        print(f"\n[用户] {turn['user']}")
        print(f"[预期] {turn['expect']}")

        start = datetime.now()

        # Clear previous responses
        delivery.responses.clear()

        # Send message
        # recipient_id must match the host's configured recipient (the companion's QQ ID)
        try:
            result = await host.inbound_text(
                message_id=f"msg_{i}_{int(datetime.now(UTC).timestamp()*1000)}",
                recipient_id=recipient_id,
                text=turn["user"],
                observed_at=datetime.now(UTC),
            )

            print(f"\n[调试] inbound_text result: status={result.status}, action_id={result.action_id}")

            # inbound_text already drains the action automatically
            # Wait a bit for background tasks to settle
            await asyncio.sleep(2.0)

        except Exception as e:
            print(f"\n❌ 错误: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "turn": i,
                "test": turn["test"],
                "user_message": turn["user"],
                "error": str(e),
                "elapsed_seconds": 0,
            })
            continue

        elapsed = (datetime.now() - start).total_seconds()
        total_time += elapsed

        # Get companion response
        response_text = ""
        if delivery.responses:
            # Get the first text response (skip reactions/stickers)
            for resp in delivery.responses:
                if resp.get("type") == "text" and resp.get("text"):
                    response_text = resp["text"]
                    break

        if not response_text:
            print("\n[角色] (无回复)")
            print(f"[调试] delivery.responses = {delivery.responses}")
        else:
            print(f"\n[角色] {response_text}")
        print(f"\n⏱️  响应时间: {elapsed:.2f}s")

        # Wait between turns to avoid ledger contention
        if i < len(CONVERSATION_TURNS) - 1:
            await asyncio.sleep(1.0)

        # Check if expected keywords are present (if specified)
        check_passed = True
        if "check" in turn:
            found_keywords = []
            missing_keywords = []
            for keyword in turn["check"]:
                if keyword in response_text:
                    found_keywords.append(keyword)
                else:
                    missing_keywords.append(keyword)

            if missing_keywords:
                check_passed = False
                print(f"\n⚠️  检查失败: 缺少关键词 {missing_keywords}")
            else:
                print(f"\n✅ 检查通过: 找到关键词 {found_keywords}")

        results.append({
            "turn": i,
            "test": turn["test"],
            "user_message": turn["user"],
            "companion_response": response_text,
            "elapsed_seconds": elapsed,
            "check_passed": check_passed if "check" in turn else None,
            "expected_keywords": turn.get("check"),
        })

        # Brief pause between turns (simulate human typing/thinking)
        await asyncio.sleep(1)

    # Print summary
    print(f"\n\n{'='*70}")
    print("测试总结")
    print(f"{'='*70}\n")

    print(f"总轮数: {len(results)}")
    print(f"总耗时: {total_time:.2f}s")
    print(f"平均响应时间: {total_time / len(results):.2f}s\n")

    # Check results
    checked_turns = [r for r in results if r.get("check_passed") is not None]
    if checked_turns:
        passed = sum(1 for r in checked_turns if r["check_passed"])
        print(f"关键词检查: {passed}/{len(checked_turns)} 通过\n")

    # Latency analysis
    slow_turns = [r for r in results if r["elapsed_seconds"] > 5.0]
    if slow_turns:
        print(f"⚠️  慢响应 (>5s): {len(slow_turns)} 轮")
        for r in slow_turns:
            print(f"   - 轮次 {r['turn']}: {r['elapsed_seconds']:.2f}s ({r['test']})")
        print()

    # Save detailed results
    output_file = Path("/tmp/conversation_test_results.json")
    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"详细结果已保存到: {output_file}\n")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
