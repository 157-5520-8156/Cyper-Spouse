# World-v2 新人关系回放与线上投影验收（2026-07-18）

本记录只记录本轮已经实际执行的证据，不把设计意图当成运行结果。QQ 适配器不在本记录的线上 HTTP 测量范围内；32 轮关系回放使用的是平台无关的 QQ C2C World-v2 composition 和隔离 SQLite。

## 回放范围

- fixture：`tests/world_v2/fixtures/new_acquaintance_32_turns.json`
- 覆盖：伴侣身份、禁止助手化、两条用户事实、跨轮回忆、负面情绪与同轮 Affect、关系边界、多 beat、当前生活/世界线索、沉默窗口和 response-gap 主动联系。
- 验收命令：

  ```text
  .venv/bin/python -m pytest -q tests/world_v2/test_new_acquaintance_journey.py
  ```

- 结果：`5 passed in 260.03s`。
- 单独运行完整 32 轮沉默/主动联系场景：`1 passed in 252.74s`。
- 修复同轮语义建议矩阵截断与 Thinking 路由重绑定后，定向回归：`5 passed in 8.40s`。
- 修复后完整 `tests/world_v2`：`2066 passed, 1 warning in 435.30s`；警告为
  Starlette `TestClient` 的 `httpx` 弃用提示，不是行为失败。
- 后续账本/调度/Context/附件恢复修复后再次完整回归：`2066 passed, 1 warning in
  326.69s`；无失败或错误。

## 本轮修复并由回放证明的链路

1. Fact 后台队列优先于普通 appraisal/NPC backlog，避免事实尚未落账就进入后续回忆轮。
2. Fact 接受事件在时钟推进后使用当前 World Clock；原始消息时间仍作为证据来源，不被覆盖。
3. Fact 接受信封的 `created_at` 不早于新的逻辑时间，避免延迟调度触发 Pydantic 拒绝并中断后续主动行为。
4. 同一 Context 的 observation proof 批量读取，并在已验证的不可变账本前缀内做局部缓存；locator 保持规范排序，未放宽证据校验。
5. QQ scheduler 严格遵守 `max_background_units=0`，并在 tick 后为 response-gap 做有限 preflight，再进入普通 Action recovery，避免已被 provider 接受的旧回复先变成 `unknown`。

## 真实 HTTP 投影

服务通过 LaunchAgent 重启后，`GET /health` 返回 `{"status":"ok"}`。使用唯一 message id 对 `/messages` 做了三次真实投影：

| 场景 | 结果 | 体感耗时 | 观察 |
| --- | --- | ---: | --- |
| 首条高信号失望/冒犯表达 | HTTP 200（首次 30 秒客户端超时后，以同 message id 重试命中已提交结果） | 服务端约 29.7s | 返回 `mood=hurt`，承认刚才没有接好，没有助手化措辞 |
| 普通生活分享 | HTTP 200 | 5.6s | 延续 hurt 状态，同时回应冰美式/按时下班，不回退成固定问答脚本 |
| 记忆询问 | HTTP 200 | 20.9s | 返回已验证的桂花乌龙；对当前数据库没有可靠证据的中文名明确说不装作记得 |

首次高信号请求的超时只是客户端设置为 30 秒；服务器随后完成了同一 Action，使用原 message id 重试在约 80ms 内返回已持久化结果，证明冷/热重试的幂等闭环成立。

## 尚未作为完成条件声称的项目

- 全仓回归的第一轮基线为 `3701 passed, 3 failed, 1 warning in 1028.98s`；其中两个
  `world_conversation` 失败已修复，并由 `tests/test_world_conversation_experience.py`,
  `tests/test_world_human_feel_emotion.py`, `tests/test_expression_plan.py` 共 `149 passed`
  定向复核。补充临时 Node v22 后发现并修复了等距投影的 1 ulp 差异；当前完整源码回归为
  `3704 passed, 0 failed, 1 warning in 647.03s`。
  `tests/js/room_runtime.test.js` + `tests/js/tile_runtime.test.js` 为 `28/28 passed`，
  `tests/test_room_runtime.py` 为 `1 passed`。唯一警告是 Starlette/httpx2 弃用提示，不是
  行为失败；默认 shell 仍未安装 Node，但不影响已用临时 Node 完成验收。
- 真实服务的高信号情绪回复仍有约 20–30 秒长尾；普通热回复已落在约 5–6 秒，但尚未宣称达到最终产品 SLA。
- 线上 HTTP 数据库中的历史事实取决于实际已输入的消息；没有证据的个人事实会被明确拒答，而不会凭空补全。
- 图片机 provider 的真实渲染不在本次文本 World-v2 回放中；事件来源与 media planner 的契约测试需单独查看。

本轮还修复了事件媒体候选的自持手机物理提示、由 workout/活动派生的身体与衣物证据闭合、
safe fallback 的来源/Action 保留，以及 Star Office 的纯读投影缺口；这些均有对应定向测试，
但真实图片 provider/QQ 推送仍不在本次线上验收范围。

## 最新修复后的线上投影

最新源码重启后，首次 `/messages` 请求按设计返回一次 `503 World-v2 is warming`；使用相同
message id 重试得到 HTTP 200，耗时约 6.6 秒，返回 `mood=hurt`，且没有回声式复述用户原话。
这验证了冷启动有界、同 message id 幂等重试以及当前情绪投影仍接入回复路径。

## 重启后再次投影

服务在应用最新源码后通过 LaunchAgent 重启，`GET /health` 返回 `{"status":"ok"}`。
普通生活分享实际返回 HTTP 200，耗时约 6.52 秒；身份边界确认实际返回 HTTP 200，耗时约
5.74 秒，明确回答“我是沈知栀，不是你的助手”。另一个带伤感关系余波的身份追问选择了
`202 observed_only`（约 5.45 秒），这属于当前受控沉默策略的可见长尾，不影响账本一致性，
但仍是后续体验校准项。
