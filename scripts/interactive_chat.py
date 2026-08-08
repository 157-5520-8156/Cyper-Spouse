#!/usr/bin/env python3
"""Interactive chat with the companion to test real conversation flow.

This is not an automated test. It's a REPL that lets you type messages and
see how the companion responds, so you can experience memory recall, emotional
state, latency, and other human-like qualities in a real conversation.
"""

import asyncio
import sys
from datetime import datetime, UTC
from pathlib import Path

# Add src to path so we can import companion_daemon
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from companion_daemon.config import Settings
from companion_daemon.world_v2.qq_c2c_host import build_qq_c2c_host


class ConsoleDelivery:
    """Delivery adapter that prints to console instead of sending to QQ."""

    async def send_message(self, recipient_id: str, message_parts: list) -> None:
        print("\n[角色回复]")
        for part in message_parts:
            if isinstance(part, dict):
                text = part.get("text", "")
                if text:
                    print(f"  {text}")
            elif isinstance(part, str):
                print(f"  {part}")
        print()

    async def send_typing(self, recipient_id: str, duration_ms: int) -> None:
        # Silent - we'll just see the delay in response time
        pass


async def main():
    settings = Settings()

    if not settings.deepseek_api_key:
        print("错误: 需要设置 DEEPSEEK_API_KEY 环境变量")
        print("或者在 .env 文件中配置")
        return 1

    print("初始化角色系统...")
    print(f"使用模型: {settings.deepseek_model}")
    print()

    delivery = ConsoleDelivery()
    host = build_qq_c2c_host(
        settings=settings,
        recipient_id="interactive-test-user",
        bootstrap_at=datetime.now(UTC),
        delivery=delivery,
    )

    print("=" * 60)
    print("交互式对话测试")
    print("=" * 60)
    print()
    print("提示:")
    print("  - 输入消息后按回车发送")
    print("  - 输入 'quit' 或 'exit' 退出")
    print("  - 输入 'help' 查看测试建议")
    print()
    print("=" * 60)
    print()

    conversation_count = 0

    while True:
        try:
            # Read user input
            user_input = input("[你] ").strip()

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "q"):
                print("\n再见！")
                break

            if user_input.lower() == "help":
                print("\n建议的测试场景:")
                print("  1. 记忆测试:")
                print("     - 告诉角色一些个人信息（名字、职业、喜好）")
                print("     - 几轮对话后用间接方式提起（'想喝点热的'）")
                print("     - 看角色能否回忆起之前说的'我喜欢乌龙茶'")
                print()
                print("  2. 情感状态测试:")
                print("     - 分享一些情绪化的事件（压力、焦虑、开心）")
                print("     - 观察角色的回复是否体现情感记忆")
                print()
                print("  3. 延迟观察:")
                print("     - 注意每次回复的等待时间")
                print("     - 记录明显的卡顿或超长等待")
                print()
                print("  4. 上下文连贯性:")
                print("     - 多轮对话中引用之前的话题")
                print("     - 看角色能否保持话题连贯")
                print()
                continue

            # Track timing
            start_time = datetime.now()

            # Send message to host
            conversation_count += 1
            print(f"\n[发送中... #{conversation_count}]")

            # Create a message event
            await host.handle_message(
                sender_id="interactive-test-user",
                text=user_input,
                received_at=datetime.now(UTC),
            )

            # Calculate response time
            elapsed = (datetime.now() - start_time).total_seconds()
            print(f"[响应耗时: {elapsed:.2f}s]")

        except KeyboardInterrupt:
            print("\n\n收到中断信号，退出...")
            break
        except Exception as e:
            print(f"\n错误: {e}")
            import traceback
            traceback.print_exc()
            break

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
