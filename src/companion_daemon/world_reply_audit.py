"""Independent semantic audit for world-mode reply candidates."""

from __future__ import annotations

from dataclasses import dataclass
import json

from companion_daemon.llm import ChatModel


@dataclass(frozen=True)
class WorldReplyAudit:
    supported: bool
    unsupported_spans: tuple[str, ...]
    reason: str


class WorldReplyAuditor:
    """Judge claim coverage without trusting the reply model's self-labelled claims."""

    async def evaluate(
        self,
        model: ChatModel,
        *,
        user_text: str,
        reply_text: str,
        grounding_context: dict[str, object],
    ) -> tuple[str, WorldReplyAudit]:
        raw = await model.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "你是严格的虚拟世界事实审计器。只判断候选回复是否加入了授权来源中不存在的事实。"
                        "角色的动作、位置细节、周围环境、持有物、过去经历、用户历史、NPC 身份/话语、"
                        "NPC 性别代词、现实世界趋势都属于事实；来源未提供 gender 时不能把姓名改成他/她。"
                        "猜测、建议、观点、比喻和问题不属于事实。"
                        "自然转述可以通过，但不能增加新的对象、动作、原因、结果、数量或时间。"
                        "同时必须回应用户本轮的言语行为；即使每句话都有来源，若只是罗列与本轮问题或情绪无关的旧事实，"
                        "也判 supported=false。简短安慰、观点、建议或诚实说不知道可以通过。"
                        "只返回 JSON："
                        '{"supported":true,"unsupported_spans":[],"reason":"..."}'
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "user_message": user_text,
                            "candidate_reply": reply_text,
                            "authorized_grounding": grounding_context,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            temperature=0.0,
        )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw, WorldReplyAudit(False, (reply_text,), "audit_output_not_json")
        if not isinstance(payload, dict) or not isinstance(payload.get("supported"), bool):
            return raw, WorldReplyAudit(False, (reply_text,), "audit_output_invalid")
        spans = payload.get("unsupported_spans", [])
        if not isinstance(spans, list) or any(not isinstance(item, str) for item in spans):
            return raw, WorldReplyAudit(False, (reply_text,), "audit_spans_invalid")
        supported = bool(payload["supported"])
        if supported and spans:
            supported = False
        return raw, WorldReplyAudit(
            supported,
            tuple(item.strip() for item in spans if item.strip()),
            str(payload.get("reason") or "")[:300],
        )
