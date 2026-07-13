# Bare / Full 性能基线

## 目的和边界

`companion-eval-dialogue --baseline` 比较两条隔离的路径：

- `bare`：一次回复模型调用、Character Core 与该评测实例已经送达的本地聊天记录；它不是旧
  Engine。
- `full`：经 `CompanionTurn.respond/settle`、TurnFrame、World、Invariant Guard 和平台 capture
  transport 的实际路径。

每个 variant、场景和重复次数都有独立 SQLite 数据库与 transcript。故 full path 的 memory、
World 事实、Action 或工具副作用不能泄漏给 bare control。

## 记录内容

报告为 schema v3 JSON，必须通过 `--report` 保留。每一个 turn 都记录：

- variant、场景、`run_index`、`turn_index`、`cadence`（首轮为 cold，其后为 hot）；
- 可见状态、首次成功 transport dispatch 的时间、整轮完成时间；
- 已持久化的真实模型调用数、prompt/completion/reasoning/cache token、总 token 和错误状态；
- 自然度诊断 issue，以及硬事实/身份 issue 数；
- 场景集 SHA-256 和评测阈值，避免后来改语料却把结果当成同一基线。

该 provider 是非流式的，所以 `first_visible_delivery_ms` 是“首次成功 dispatch”，不是不可获得的
token 级 TTFT。报告会将缺失 dispatch 保留为缺失，不能把失败样本悄悄当作零延迟。

## 可复现实测

先运行一次本地 fake 冒烟，确认数据形状和隔离机制：

```bash
companion-eval-dialogue --baseline --max-cases 1 --report artifacts/baseline-fake.json
```

真实服务验收至少要有每 variant 20 个 hot 样本。标准五组语料每轮有 9 个 hot turn，因此三个独立
重复可得到 27 个 hot 样本：

```bash
mkdir -p artifacts
companion-eval-dialogue --baseline --live --repetitions 3 \
  --report artifacts/baseline-live-$(date +%F).json --assert-live-slo
```

`--live` 会要求存在 `DEEPSEEK_API_KEY`，拒绝把本地 fake fallback 标为真实服务样本。

不要把一次 `--live --max-cases 1` 的结果写成 P50/P95 结论；它只能用于排障。

## 自动判定和仍需人工证据

当且仅当为 live、两个 variant 都有至少 20 个可比 hot 样本、且两者均有可见 dispatch 时，报告的
`latency_status` 才会给出 `pass` 或 `fail`。自动门为：

- full hot P50 首次可见回复不高于 3 秒；
- full hot P95 不高于 5 秒；
- full hot P95 不高于 `max(5s, bare P95 + 1s, bare P95 × 1.5)`。

否则延迟状态为 `insufficient_evidence`，并明确列出缺少的条件。`--assert-live-slo` 会把非 `pass`
转成非零退出码，供可控的真实验收运行使用；普通 fake/小样本命令不会误报成功。

报告同时单列 `quality_status`：若缺可见投递或出现 heuristic hard issue，它会是
`heuristic_fail`；否则仍是 `requires_blind_evaluation`，而不是“质量通过”。自然度 issue、硬事实
issue、模型次数和 token 只作为可比诊断，不能替代盲评。完成架构文档中的
“full 不显著弱于 bare”仍需让评审者在不知道 variant 的情况下对同一输入评分；真实用户数周数据、
实际网络/平台队列分布和供应商真实 TTFT 同样是此仓库无法自行制造的外部证据。
