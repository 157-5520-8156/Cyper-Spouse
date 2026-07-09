import re
from dataclasses import dataclass
from typing import Literal


ToolRisk = Literal["read_only", "confirmation_required", "blocked"]


@dataclass(frozen=True)
class ToolRequest:
    kind: str
    risk: ToolRisk
    summary: str

    @property
    def requires_confirmation(self) -> bool:
        return self.risk in {"confirmation_required", "blocked"}


def detect_tool_request(text: str) -> ToolRequest | None:
    normalized = text.strip().lower()
    if not normalized:
        return None
    if re.search(r"(删除|清空|覆盖|改掉|写入|保存到|运行|执行|终端|shell|发给|转发|登录|付款|下单)", normalized):
        return ToolRequest(
            kind="risky_action",
            risk="confirmation_required",
            summary="用户可能在请求执行会改变电脑、账号、文件或第三方消息的操作。",
        )
    if re.search(r"(打开|点开|帮我看|看看电脑|截图|屏幕|浏览器|网页|文件夹|app|应用)", normalized):
        return ToolRequest(
            kind="computer_assist",
            risk="confirmation_required",
            summary="用户可能在请求电脑/浏览器辅助操作；执行前需要明确范围和确认。",
        )
    if re.search(r"(查一下|搜一下|找一下|读取|读一下|总结这个文件|分析这个文件)", normalized):
        return ToolRequest(
            kind="read_only",
            risk="read_only",
            summary="用户可能在请求只读检索或分析。",
        )
    return None


def tool_prompt_line(request: ToolRequest) -> str:
    if request.risk == "read_only":
        return f"工具请求: {request.summary} 可以提出只读操作计划；如果当前环境没有对应工具，就先说明限制。"
    return (
        f"工具请求: {request.summary} 不要假装已经执行。"
        "涉及文件写入、shell、账号、付款、发送第三方消息或控制电脑前，必须先请求用户明确确认。"
    )
