# AstrBot 最新开源版本调研

日期：2026-07-27
资料范围：官方 GitHub org/repo、Release 标签、`pyproject.toml` / 源码树、官方文档站点；不采用二手博客作为证据。

## 结论

截至 2026-07-27，AstrBot 的**最新稳定开源版本是 `v4.26.7`（2026-07-18 发布）**。
规范上游仓库是 **[AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)**（AGPL-3.0），主页 [astrbot.app](https://astrbot.app)，文档 [docs.astrbot.app](https://docs.astrbot.app/)。

定位上，它是一个**多 IM 平台接入的 Agentic 聊天机器人框架 / 基础设施**：适配器 + Pipeline + LLM Provider + 插件（Star）+ Agent Runner。它面向个人助手、客服、自动化和知识库，而不是以单一角色主体性与事件账本为中心的 companion daemon。

## 规范上游与版本

| 项 | 事实 | 来源 |
| --- | --- | --- |
| Canonical repo | `https://github.com/AstrBotDevs/AstrBot` | [GitHub API](https://api.github.com/repos/AstrBotDevs/AstrBot)；[README](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md) |
| Org | AstrBotDevs；博客/主页 `https://astrbot.app` | [AstrBotDevs org](https://github.com/AstrBotDevs) |
| 历史作者关系 | 主要贡献者 Soulter；旧个人仓库路径 `Soulter/AstrBot` 现已 404，README 徽章仍可见 Trendshift 上的旧名痕迹 | [repo contributors](https://github.com/AstrBotDevs/AstrBot)；`GET /repos/Soulter/AstrBot` → 404 |
| License | AGPL-3.0（`pyproject` 写作 `AGPL-3.0-or-later`） | [LICENSE](https://github.com/AstrBotDevs/AstrBot/blob/master/LICENSE)；[`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml)；[FIRST_NOTICE.md](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/FIRST_NOTICE.md) |
| 最新稳定 tag | **`v4.26.7`**，published `2026-07-18T12:34:44Z` | [Releases](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.7) |
| 当前 minor 起点 | `v4.26.0`，published `2026-06-24T15:47:14Z` | [v4.26.0](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.0) |
| PyPI | 包名 `AstrBot`，版本 `4.26.7`，`requires-python >=3.12` | [PyPI AstrBot](https://pypi.org/project/AstrBot/)；[`astrbot/__init__.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/__init__.py) `__version__ = "4.26.7"` |
| 镜像/分发 | Docker Hub `soulter/astrbot`；README 另列 GitCode 徽章；官方文档与 clone URL 均指向 AstrBotDevs | [README](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md) |

**Fork 判定：** 应以 `AstrBotDevs/AstrBot` 为唯一上游。`Soulter/AstrBot` 已不存在；插件模板等仓库（如 `Soulter/helloworld`）明确指向 AstrBotDevs 主仓。

## 项目定位（one-liner）

官方中文 README：

> AstrBot 是一个开源的一站式 Agentic 个人和群聊助手，可在 QQ、Telegram、企业微信、飞书、钉钉、Slack 等数十款主流即时通讯软件上部署……为个人、开发者和团队打造可靠、可扩展的对话式智能基础设施。

英文 README 同义表述为 “all-in-one Agent chatbot platform”。`pyproject.toml` description 为：

> Easy-to-use multi-platform LLM chatbot and development framework

来源：[README_zh.md](https://github.com/AstrBotDevs/AstrBot/blob/master/README_zh.md)、[README.md](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md)、[`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml)。

官方文档首页强调：**多平台 / 1000+ 插件 / 通用 Agent 能力编排**，架构关键词为 “event bus and pipeline”。来源：[docs.astrbot.app](https://docs.astrbot.app/)、[docs EN](https://docs.astrbot.app/en/)。

## 技术栈

| 层 | 技术 | 来源 |
| --- | --- | --- |
| 语言 / 运行时 | Python ≥ 3.12（`main.py` 启动检查仍写 ≥3.10，但包装与文档要求 3.12） | [`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml)；[README_zh](https://github.com/AstrBotDevs/AstrBot/blob/master/README_zh.md)；[`main.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/main.py) |
| Web / Dashboard API | FastAPI（v4.26.0 从 Quart 迁入；依赖中仍保留 `quart`） | [v4.26.0 release](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.0)；[`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml) |
| 前端 WebUI | Vue 3 + Vite + Vuetify + Pinia（`dashboard/`） | [`dashboard/package.json`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/dashboard/package.json) |
| 持久化 | SQLite（`data_v4.db`）、SQLAlchemy/SQLModel、aiosqlite；向量侧 FAISS | [`astrbot/core/config/default.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/config/default.py)；[`astrbot/core/db/`](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/db) |
| LLM / Agent | openai、anthropic、google-genai、mcp（`<2`）、faiss-cpu、rank-bm25、jieba | [`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml) |
| 平台 SDK（节选） | `qq-botpy`、`aiocqhttp`、`python-telegram-bot`、`py-cord`、`slack-sdk`、`lark-oapi`、`dingtalk-stream`、`wechatpy` | 同上 |
| 沙盒 | `shipyard-python-sdk`、`shipyard-neo-sdk`；可选 CUA | 同上；[Agent Sandbox docs](https://docs.astrbot.app/use/astrbot-agent-sandbox.html) |
| 包管理 / CLI | `uv tool install astrbot`；入口 `astrbot = astrbot.cli.__main__:cli` | [README](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md)；[`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml) |
| 质量工具 | ruff、pytest、pre-commit | [`pyproject.toml`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/pyproject.toml) |

源码顶层结构（v4.26.7）：`astrbot/`（`api`、`builtin_stars`、`cli`、`core`、`dashboard`）、`dashboard/`、`docs/`、`openspec/`、`main.py`、`compose.yml`、`k8s/`。

`astrbot/core/` 关键子模块包括：`pipeline`、`platform`、`provider`、`agent`、`star`（插件）、`skills`、`knowledge_base`、`tools`、`config`、`db`、`computer`（沙盒）、`cron`、`message`。

## 核心架构：消息如何流动

官方文档与源码一致：平台消息 → 统一事件 → Pipeline 多阶段 → 插件/Agent → 装饰 → 回发。

### Pipeline 阶段顺序

```text
Platform adapter (sources/*)
        │  encapsulate as AstrMessageEvent / AstrBotMessage
        ▼
WakingCheckStage          # 是否唤醒
WhitelistCheckStage       # 群/私聊白名单
SessionStatusCheckStage   # 会话是否启用
RateLimitStage
ContentSafetyCheckStage
PreProcessStage
ProcessStage              # Stars（插件）或 LLM / Agent
ResultDecorateStage       # 前缀、t2i、TTS 等
RespondStage              # 发回平台
```

来源：[`astrbot/core/pipeline/stage_order.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/pipeline/stage_order.py)；事件封装说明见 [Handling Message Events](https://docs.astrbot.app/en/dev/star/guides/listen-message-event.html)。

### ProcessStage：插件 vs Agent

`ProcessStage` 下有两条主路径：

- `star_request.py`：交给 Star（插件）handler
- `agent_request.py`：按 `provider_settings.agent_runner_type` 选择
  - `local` → 内置 `InternalAgentSubStage`
  - 其他 → `ThirdPartyAgentSubStage`（Dify / Coze / Bailian / DeerFlow 等）

来源：[`process_stage/method/`](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/pipeline/process_stage/method)；[`agent_request.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/pipeline/process_stage/method/agent_request.py)。

### Agent Runner 分层（v4.7.0+）

文档明确区分：

- **Chat Provider**：单轮补全接口（prompt + history + tools → 回复 / tool call）
- **Agent Runner**：多轮 perceive → plan → act → observe 循环

内置 runner 默认；也可外挂 Dify、Coze、阿里云百炼应用、DeerFlow。源码 runners 目录：`tool_loop_agent_runner.py` 与 `coze/`、`dify/`、`dashscope/`、`deerflow/`。

来源：[Agent Runner](https://docs.astrbot.app/en/use/agent-runner.html)；[`astrbot/core/agent/runners/`](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/agent/runners)。

### 插件钩子与 Agent 生命周期

默认流程中，插件可挂接：

- `on_waiting_llm_request` / `on_llm_request` / `on_llm_response`
- `on_agent_begin` / `on_using_llm_tool` / `on_llm_tool_respond` / `on_agent_done`（需 > v4.23.1）
- `on_decorating_result` / `after_message_sent`

文档建议：稳定角色规则进 `system_prompt`；每轮变化的记忆片段等进 `extra_user_content_parts`；大体量长期记忆/知识库优先注册为按需 `llm_tool`。

来源：[listen-message-event](https://docs.astrbot.app/en/dev/star/guides/listen-message-event.html)。

## 支持的消息平台 / 适配器

README 官方维护表（节选）：QQ、OneBot v11、Telegram、企微、微信公众号、飞书、钉钉、Slack、Discord、LINE、Satori、KOOK、Misskey、Mattermost；WhatsApp 标注 Coming Soon；Matrix / Rocket.Chat / VoceChat 为社区插件。

源码 `astrbot/core/platform/sources/`（v4.26.7）对应目录：

`aiocqhttp`、`qqofficial`、`qqofficial_webhook`、`telegram`、`wecom`、`wecom_ai_bot`、`weixin_official_account`、`weixin_oc`、`lark`、`dingtalk`、`discord`、`slack`、`kook`、`line`、`satori`、`misskey`、`mattermost`、`webchat`

插件 `metadata.yaml` 的 `support_platforms` 可声明的 adapter key 还包括 `vocechat`、`matrix` 等。来源：[README_zh 平台表](https://github.com/AstrBotDevs/AstrBot/blob/master/README_zh.md)；[platform/sources](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/platform/sources)；[插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)。

## 插件 / 扩展系统（Star）

v4.0 起内部称插件为 **Star**，handler 为 `star_handler`。来源：[`astrbot/core/star/README.md`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/star/README.md)。

要点（官方插件文档）：

- 插件放在 `data/plugins/<name>/`，元数据依赖 `metadata.yaml`
- 支持命令、命令组、事件过滤、平台过滤、权限、优先级、`event.stop_event()`
- 可提供 `skills/`；可声明 `astrbot_version`（PEP 440）与 `support_platforms`
- 依赖用插件目录 `requirements.txt`；持久化应写到 `data/`，避免更新覆盖
- 市场声称 1000+ 一键安装插件（README 动态徽章）

来源：[插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)；[README](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md)。

## Agent / 工具 / Persona / 记忆相关能力

| 能力 | 现状（有源码/文档依据） | 来源 |
| --- | --- | --- |
| Tool calling | `FunctionTool`、`@filter.llm_tool`、`context.add_llm_tools`、`tool_loop_agent` | [AI 开发指南](https://docs.astrbot.app/en/dev/star/guides/ai.html) |
| MCP | v3.5.0+ 支持多 MCP Server，远程暴露函数工具 | [MCP](https://docs.astrbot.app/use/mcp.html) |
| Anthropic Skills | v4.13.0+；按需加载 SKILL.md；来源含本地 / 插件 / sandbox / workspace | [Skills](https://docs.astrbot.app/use/skills.html) |
| Agent Sandbox | v4.12.0+；Shipyard Neo（默认推荐）/ Shipyard / CUA；技术预览 | [Agent Sandbox](https://docs.astrbot.app/use/astrbot-agent-sandbox.html) |
| SubAgent handoff | v4.14.0+；`transfer_to_*`；实验性，子会话历史暂不保存 | [SubAgent](https://docs.astrbot.app/en/use/subagent.html)；`subagent_orchestrator` in [`default.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/config/default.py) |
| Persona | DB 表 `personas`：`system_prompt`、`begin_dialogs`、`tools`；`PersonaManager` CRUD；v4 弃用 `mood_imitation_dialogs` | [AI / Persona Manager](https://docs.astrbot.app/en/dev/star/guides/ai.html) |
| 会话历史 | `ConversationManager`：按 `unified_msg_origin` 管理 conversation、history、persona_id | 同上 |
| 上下文压缩 | 默认 `context_limit_reached_strategy: llm_compress`；可 `truncate_by_turns` | [`default.py` provider_settings](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/config/default.py) |
| 知识库 | ≥4.5.0 原生 KB；Embedding + 可选 Rerank；多库；FAISS/BM25 等检索组件 | [知识库](https://docs.astrbot.app/use/knowledge-base.html)；[`core/knowledge_base/`](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/knowledge_base) |
| 主动能力 | `proactive_capability.add_cron_tools`（默认 True）；cron 模块存在 | [`default.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/config/default.py)；[`core/cron/`](https://github.com/AstrBotDevs/AstrBot/tree/v4.26.7/astrbot/core/cron) |
| 角色扮演宣传 | README 展示 “Role-playing & Emotional Companionship” 用例图，属产品卖点，非独立记忆架构文档 | [README](https://github.com/AstrBotDevs/AstrBot/blob/master/README.md) |

**关于“记忆”：** 官方能力中心是**会话 history + LLM 摘要压缩 + 知识库 RAG + 插件注入/工具拉取**。文档明确建议长期记忆不要每轮塞进 system prompt，而用 `llm_tool` 或短摘要。这里没有 Girl-Agent 式的“唯一 World Ledger / source-bound recall authority”。

## 配置与部署

### 配置模型

- 默认配置在 [`astrbot/core/config/default.py`](https://github.com/AstrBotDevs/AstrBot/blob/v4.26.7/astrbot/core/config/default.py)
- 运行时修改：`data/cmd_config.json` 或 WebUI（文件头注释明文说明）
- `config_version: 2`；主要块包括 `platform_settings`、`provider_sources` / `provider`、`provider_settings`、`subagent_orchestrator`、`persona`（legacy 字段已 deprecated）等
- DB 默认路径：`data/data_v4.db`
- 启动可 `--reset-password` 重置 Dashboard 密码（v4.26.0 起）

### 部署选项（官方 README / docs）

1. **uv 一键**：`uv tool install astrbot --python 3.12` → `astrbot init` → `astrbot run`
2. **Docker / Compose**（推荐生产）
3. **雨云一键云**
4. **AstrBot-desktop**（ChatUI 桌面；非服务器）
5. **AstrBot Launcher**（桌面多实例隔离；社区仓库 Raven95676/astrbot-launcher）
6. **宝塔 / 1Panel / CasaOS / 手动 CLI / AUR / Replit（社区）**

来源：[README_zh 快速开始](https://github.com/AstrBotDevs/AstrBot/blob/master/README_zh.md)；[Docker 部署](https://docs.astrbot.app/deploy/astrbot/docker.html)。

## v4.26.x 近期重要变更

### v4.26.0（2026-06-24）— 本 minor 的主要跳跃

- **后端从 Quart 迁移到 FastAPI**，并增加多处 OpenAPI
- 全平台媒体处理统一；腾讯 Silk 语音不再依赖 pilk
- WebUI：工具级权限、主题模式、系统配置入口迁移到侧边栏 Settings
- QQ 官方 Bot WebSocket 扫码绑定；群聊能力增强
- Provider 请求可配置重试；workspace skills；Exa Web Search；ElevenLabs TTS
- 配置原子写入、若干工具/人格/MCP 兼容修复

来源：[v4.26.0 release notes](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.0)。

### 后续补丁至 v4.26.7（2026-07-18）

最新补丁侧重：ChatUI 内联 GenUI、流式 Agent 统计、TEI rerank、可配置 embedding 维度发送模式、异步 revision-aware 配置快照、重复 tool call 检测优化、知识库上传失败补偿、插件 schema BOM / handler 幂等等。

来源：[v4.26.7](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.7)。

## 与 Girl-Agent / companion daemon 的相关性

**可迁移的概念（基础设施层）：**

- **平台适配器边界**：统一 `AstrMessageEvent` / message chain，与具体 IM 解耦——Girl-Agent 已有 QQ C2C host，可对照其 adapter 目录与 webhook/ws 分型。
- **Pipeline 硬边界阶段**：唤醒、白名单、限流、内容安全、会话开关——对应“系统硬边界，不替角色作语义决定”。
- **工具按需调用与 MCP**：长期记忆/外部查询用 tool 拉取，避免污染 system prompt / 破坏 prompt cache——与 Girl-Agent 的 read-only `recall` 工具方向一致（文档层面）。
- **Persona 作为 prompt+tools 配置**：可作为“稳定人设载体”，但 AstrBot 的 Persona 不提供事件溯源与 supersession。

**取向差异（不宜照搬为 soul）：**

- AstrBot 是 **多平台 chatbot / agent 编排框架**；Girl-Agent 是 **受控高随机的角色主体性 + World Ledger 事实权威**（见 `AGENTS.md`、ADR 0010）。
- AstrBot 的“记忆”是会话压缩 + KB RAG + 插件；Girl-Agent 需要 source-bound episodic/semantic/reflective 分层与可重放账本。
- AstrBot 大量能力（分段回复 regex、默认安全 system prompt、人格工具列表）服务于通用助手可用性；Girl-Agent 明确拒绝用关键词/矩阵替角色作语义决定。
- 许可为 **AGPL-3.0**：若嵌入或网络提供修改版服务，合规成本高于典型宽松许可 companion 代码。

**实务建议：** 把 AstrBot 当作 **IM 适配与插件生态的参考实现 / 可选外围 runtime**，不要当作角色决策核心。若只需要 QQ/Telegram 等通道，优先抽取适配思路；角色动机、情绪、主动联系与事实回忆仍应留在 Girl-Agent World V2。

## 主要一手来源清单

- Repo：https://github.com/AstrBotDevs/AstrBot
- Latest release：https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.7
- Minor release：https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.26.0
- Docs：https://docs.astrbot.app/
- PyPI：https://pypi.org/project/AstrBot/
- 关键源码路径（tag `v4.26.7`）：`pyproject.toml`、`astrbot/core/pipeline/stage_order.py`、`astrbot/core/config/default.py`、`astrbot/core/platform/sources/`、`astrbot/core/agent/`、`astrbot/core/star/`
