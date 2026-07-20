# QQ/NapCat 链路恢复记录（2026-07-18）

## 故障

- `.env` 使用 `QQ_ADAPTER=napcat`，但 official WebSocket LaunchAgent 仍启动
  `companion-qq-ws --sandbox`，进程因 outbound owner 不匹配退出。
- NapCat OneBot 适配器启动时读取 `data/companion.sqlite`，QQ 世界
  `world:companion-v2:qq-c2c:geoff` 的 `state_hash`/`semantic_hash` 是旧派生值，
  与当前 reducer 状态不一致，启动被 `LedgerIntegrityError` 拒绝。
- NapCat 自身 API（3000）在线，但适配器监听端口（8787）未启动，所以消息无法进入
  World-v2 并发送回复。

## 处理

1. 备份原始数据库：`data/companion.sqlite.pre-qq-repair-20260718-213951`。
2. 通过当前 reducer 重新计算 QQ head 的两个派生哈希，并用旧值条件更新；没有改写事件历史。
3. 先在数据库副本上验证完整 replay 与 head 一致，再修复正式数据库。
4. `scripts/run_qq_ws.sh` 增加 adapter owner guard：`QQ_ADAPTER` 不是 `official` 时
   official 进程正常退出，不再进入 launchd 崩溃循环。
5. 停用冲突的 official LaunchAgent，只保留 NapCat/OneBot 出站 owner。

## 验证

- `GET http://127.0.0.1:8787/health`：`adapter=napcat`、`world_v2=true`、scheduler
  `failures=0`。
- QQ API `get_status`：`online=true`、`good=true`。
- 唯一 ID 的 OneBot 私聊回放返回 `action_authorized`；NapCat `get_friend_msg_history`
  确认已发送 `收到～QQ链路正常。`。
- 账本重新打开并 replay：`replay_equal=True`。
- QQ/NapCat 定向回归：`37 passed, 20 deselected`；仅有既存 Starlette/httpx 弃用警告。
