# Girl Agent

一个以 QQ 为主要交互入口、以连续世界与长期关系为核心的本地优先虚拟伴侣项目。

> [!IMPORTANT]
> 当前仓库处于持续集成阶段，不是已经完成生产资格的发行版。代码、自动化测试、隔离
> Provider 样本和一次真实发送都不能单独证明可上线；当前发布口径仍是
> `manual_only / qualification_incomplete`。

Girl Agent 不是用状态机或固定话术“扮演”角色。World V2 为角色提供有来源的世界事实、
记忆、关系、情绪、生活状态和可用能力；`CharacterInterior` 内的角色模型是主角唯一的
语义作者，决定她如何理解、是否回应、说什么、是否主动联系以及是否使用媒体等能力。
确定性代码只负责事实来源、隐私与同意、外部 Action 授权、CAS、effect-once、回执和
可重放性。

## 当前能力与边界

| 能力 | 当前代码状态 | 尚未关闭的发布门 |
| --- | --- | --- |
| World V2 账本与投影 | SQLite 不可变事件、Projection、CAS、迁移、冷重放和 Action 生命周期已进入主链 | 长期真实数据质量和多日增长仍需实测 |
| 文字对话 | HTTP、模拟入口及 QQ/NapCat C2C 可进入同一 `CharacterInterior` → Acceptance → Action 路径 | 真实 Provider 首次合法率、首 Beat 2–3 秒、单轮综合成本约 ¥0.03 的目标尚未资格化 |
| 内心状态与主动联系基础设施 | Appraisal、Affect、Relationship、Private Impression、Life/Proactive 等 typed lane 已存在 | 连续后台运行、真实主动联系、技术重试和自然可感知性仍需多日旅程验证 |
| 记忆与关系 | 来源绑定的事实、记忆候选、Recall 与关系投影已存在；承诺推动关系阶段的原子结算仍待集成 | 一周以上自然召回、跨轮动作与关系变化的完整真实对话证据仍不足 |
| 媒体与感知 | 媒体 planning/render/inspection/preview、附件感知与预算/授权 seam 已实现 | 真实生图 → 真实 QQ dispatch → receipt/get_msg 的最终链路尚未取得发布资格 |
| NPC 与外界感知 | 有 actor-scoped、source-bound 的实验/受限机制 | 默认生产模型调用面不因这些实验自动扩大，完整 producer/consumer 与体验资格仍未完成 |

设计文档描述的是完整产品目标，不代表其中每一项已经实现或启用。并行 worktree、未合并
分支和临时 SQLite 的结果也不属于当前主线能力。

## 核心调用图

```text
QQ / HTTP / local simulation
          │
          ▼
 Observation → append-only World Ledger → pinned Context / Inner Life Snapshot
                                                │
                                                ▼
                                      CharacterInterior
                                (内含唯一主角角色模型)
                                                │
                                                ▼
                            typed Proposal → Acceptance / CAS
                                                │
                                                ▼
                             Action → Provider/Platform → Receipt
                                                │
                                                ▼
                               deterministic Projection / replay
```

随机性可以产生机会、时机、注意力和候选环境，但不能替角色决定动机、态度、情绪表达、
措辞、主动联系或沉默。模型结果不合法时，只允许把精确失败原因交给同一角色模型做一次
受约束重选；仍失败就记录技术失败，不能由本地模板冒充角色。

## 快速开始

要求 Python 3.12+ 和 [`uv`](https://docs.astral.sh/uv/)。

```bash
uv sync
```

Provider 密钥通过环境变量或本机 `.env` 提供；不要提交 `.env`、数据库、聊天记录或验收
artifact。文字角色主路需要 `DEEPSEEK_API_KEY`。使用 OpenAI-compatible 媒体、视觉、语音
或 embedding 能力时才需要相应密钥。

部分客户端显式使用 `trust_env=False`，不会自动继承系统代理。需要代理时应明确配置，例如：

```bash
export OPENAI_PROXY_URL=http://127.0.0.1:7897
```

### 本地模拟

```bash
uv run companion-sim "我刚刚在忙，现在回来了"
```

### 启动基础 daemon

```bash
uv run companion-daemon
```

### NapCat / OneBot 私聊入口

```bash
scripts/run_napcat_adapter.sh
```

该脚本加载本机 `.env` 并启动 World V2 C2C 路径。NapCat WebUI、loopback、QQ 账号与
路由设置见 [`docs/napcat-setup.md`](docs/napcat-setup.md)。通用 OneBot 实现可使用：

```bash
scripts/run_onebot_adapter.sh
```

不要让 official、NapCat 和通用 OneBot 同时争用同一个 QQ outbound owner。真实 QQ 测试
必须使用明确的 allowlist、临时数据库和独立端口；未经人工确认不得替换正在运行的生产
daemon。

## 测试

日常开发使用确定性的分层入口，不必每次运行数千条完整回归：

```bash
# 最小 schema / appraisal smoke
uv run python scripts/test_fast.py --tier smoke

# 默认 CharacterInterior / LLM 主链
uv run python scripts/test_fast.py

# QQ host、调度、Delayed Trigger 与 Matrix
uv run python scripts/test_fast.py --tier host

# 仅运行 pytest 缓存中的上次失败；没有缓存时不会退化成全量
uv run python scripts/test_fast.py --last-failed

# 最终集成门禁
uv run python scripts/test_fast.py --tier full
```

`pytest-xdist` 在 SQLite、进程和冷重放 fixture 上不一定更快。优先按 seam 跑定向测试，
合并所有并行分支后只运行一次最终全量。测试绿色只证明对应范围，不证明真实 Provider、
QQ、自然度、成本或长期稳定性。

## 发布资格

满足以下证据前，不应把版本写成“生产完成”或自动替换现有 daemon：

1. 当前最终 revision 的完整回归、Ruff、diff check 和冻结场景均通过。
2. 真实角色 Provider 在每个启用的 required-tool purpose 上有足够样本，request identity、
   correction lineage、首次合法率、成本和延迟均可审计。
3. 真实 QQ staging 覆盖单气泡、多气泡、消息轰炸、插话、typing、System Notice、
   dispatch、receipt/get_msg、重复回执和未知回执恢复。
4. 重启与冷重放不会重写角色选择、重复发送、重复生图或丢失关系/记忆/生活状态。
5. 对话能持续感知到记忆、关系、情绪、生活、主动性和后果，而不是只有后台事件存在。
6. 媒体启用时，角色选择、授权、真实 render、inspection、QQ 投递和最终回执形成同一
   可重放链。
7. 经过 24 小时 soak 和多日自然生活旅程；最终生产替换仍由人工门决定。

当前性能目标是文字单轮综合成本约 ¥0.03、首个完整可见 Beat 2–3 秒。这些是待测的
release gate，不是 README 对当前版本的性能承诺。

## 设计与开发约束

开始修改 World V2 前先阅读：

- [`AGENTS.md`](AGENTS.md)：角色自主性与协作边界。
- [`CONTEXT.md`](CONTEXT.md)：领域词汇和当前权威模型。
- [`ADR-0010`](docs/adr/0010-controlled-high-variance-character-agency.md)：受控高随机，角色决定行为。
- [`设计总纲`](docs/design/girl-agent-design-intent.md)：目标体验与能力边界。
- [`长期耦合与执行计划`](docs/design/root-causes-and-long-coupling-luna-plan.md)：阶段、证据门与未完成项。
- [`成本控制`](docs/cost-control.md) 与 [`视觉身份`](docs/visual-identity.md)。

并行开发应使用独立分支/worktree、临时 SQLite 和独立端口；同一文件同时只能有一个写
owner。跨边界修改要先停下并重新分配所有权。一个 worktree 的绿色测试、一次短样本或
隔离验收不能自动升级其他分支或主线的资格状态。

## 仓库与部署说明

开发分支、远端 `main` 和正在运行的 daemon 可能处于不同 revision。发布前必须同时核对：

```bash
git status --short
git log --oneline --decorate -n 10
git branch -avv
```

README 描述的是仓库的设计与当前集成边界，不是某个旧进程的健康证明。进程 revision、
数据库路径、端口、Provider 路由和资格报告都需要在部署时重新记录。
