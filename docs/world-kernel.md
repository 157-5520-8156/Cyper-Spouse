# 世界事务内核

`WorldKernel` 是虚拟世界唯一的写入入口。它使用 SQLite 的追加式 `world_events`
账本；`world_current_state`、实体、日程、行动、经历和事实索引都是可删除、可重建的投影。

## 不变量

1. 已发生的世界事实只能由事件追加；修正追加补偿事件，绝不改写历史。
2. 每个写入带预期 revision。过期 revision 会失败，调用方必须重新读取并决定。
3. LLM、随机数、时钟和投递结果均是记录过的外部结果；回放不会重新调用它们。
4. 计划、候选提议和未发送文本不能作为对话可引用经历。
5. 每个线上行动最终只能是 delivered、failed、cancelled 或 expired 之一。

## 启用与时间

默认 `WORLD_RUNTIME_ENABLED=false`，避免旧状态机与新世界混写。启用后运行时从
`configs/world_seed.yaml` 创建或恢复一个世界纪元，并只导入已验证的用户事实；旧生活
记录仍是只读归档，不进入新世界。

逻辑时间由世界命令推进。面板/HTTP 调用可以提交 `ClockModeChanged` 和 `ClockAdvanced`；
暂停、实时、1×、2×、4×、8× 都必须作为事件记录，不能由代码直接改当前时间。

## 运行检查

```bash
uv run pytest -q tests/test_world_kernel.py
uv run pytest -q
uv run ruff check src tests
```

启用真实聊天前，必须在当前数据库执行 `rebuild_projection` 并确认报告的
`matches_live=true`；任何 outbox、投递或适配器路径若未生成对应世界行动，不得启用。
