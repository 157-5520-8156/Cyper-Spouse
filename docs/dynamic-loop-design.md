# 动态闭环设计与演进方案

更新日期：2026-07-11

本文是沈知栀系统的长期设计合同。它关注“时间如何改变状态”和“每个事件如何
影响未来”，不是功能愿望清单。实现后将条目从“计划”改为“已实现”，并链接测试。

## 北极星

她的每次表现应来自同一条连续因果链：

```text
时间流逝 + 当前生活活动 + 用户事件 + 历史关系
  -> 情绪/印象/未完成事务
  -> 生活运行时（注意力、手机、余波、慢性轨迹）
  -> 是否读、何时回、怎么回、是否主动
  -> 成功或失败投递
  -> 新的历史、期待、记忆与下一轮状态
```

模型负责表达和少量受约束的选择；因果、时间、投递与事实归 daemon 负责。

## 时间是一等输入

任何动态状态必须定义以下至少两项：开始条件、持续时间、衰减方式、升级条件、
解除条件、对外显行为的影响。

| 时间层 | 典型跨度 | 例子 | 当前状态 |
| --- | --- | --- | --- |
| 瞬时 | 秒到分钟 | 通知、阅读、打字、分段消息、被打断 | 已实现 |
| 当前活动 | 分钟到数小时 | 上课、自习、吃饭、和同学、睡眠 | 已实现日计划初版 |
| 对话余波 | 数十分钟到一天 | 主动消息等待回应、没答完的问题、用户脆弱表达后的挂心 | 已实现初版 |
| 慢性轨迹 | 数天到数周 | 尊重感、回应印象、信任、情绪 affinity、关系阶段 | 已实现，需持续校准 |
| 长期事实 | 周到长期 | 用户偏好、共同事件、人物关系、自我核心 | 已实现检索与合并；事实账本进行中 |

禁止把“当前 15:00”直接等同于“她在上课”。时间只决定事件模板的候选和转场；
运行时记录才决定她此刻实际处于何种活动。

## 事实与记忆边界

从 2026-07-11 起，长期上下文分为四个职责块：角色事实账本、用户事实账本、
关系状态、以及检索型事件记忆。只有前两者可支撑具体断言；关系状态只改变行为，
事件记忆只帮助找回话题与语气。模型的临场扩写不会自动写回任何事实块。

用户事实账本是追加式的：每条事实保留消息来源、写入时间、有效期和状态。带明确
冲突键的事实（例如当前住处）会将旧值标记为 `superseded`，而不是删除。设计参考
见 [fact-memory-patterns.md](research/fact-memory-patterns.md)。

## 闭环矩阵

| 事件 | 即时状态 | 生活投影 | 后续影响 | 验收 |
| --- | --- | --- | --- | --- |
| 用户普通消息 | 意图、聊天历史 | 看手机或保持未读 | 下一条通知可提高读取概率 | 连续消息测试 |
| 用户脆弱表达 | worried/关怀 | 产生挂心余波、降低注意力门槛 | 更容易查看、后续主动可带关心 | 生活余波测试 |
| 冒犯/控制 | hurt/guarded/边界 | DND、注意力收回 | 降低主动与生活分享概率 | 边界回退测试 |
| 道歉/温和回应 | 修复/安全感 | 当天节奏放松 | 信任与可接近性恢复但不瞬间清零 | 修复曲线测试 |
| 她主动消息 | initiative 释放 | 进入期待反馈 | 冷却、等待、反馈分类 | 主动反馈测试 |
| 她分享生活 | 私人事件 + 投递记录 | 同样进入期待反馈 | 用户反应影响印象与下一次主动 | life-event 反馈测试 |
| 用户长期未回 | 安全感、回应印象下降 | 更少盯手机/更少追发 | 冷却变长、语气收住 | waiting 阶段测试 |
| 成功投递 | 出站历史、表达释放 | 手机回归活动 | 可被识别为下一条反馈的起点 | outbox 测试 |
| 投递失败 | outbox failed | 不生成共同历史 | 允许重试/后续重新决策 | failure 测试 |

## 统一生活投影层

`synchronize_life_runtime(store, user, mood_state)` 是唯一的持续状态到生活状态的
投影边界。

它合成：

```text
当前活动基础注意力
+ 有时效的用户事件注意力偏移
+ 慢性情绪/边界/回应印象/主动欲偏移
= 当前注意力与手机行为
```

投影的输出只能影响以下几类行为：

- 消息是否立即读取、延迟多久、第二条消息是否唤醒；
- 是否适合主动外发；
- 上下文中的当前生活状态与回复节奏；
- 活动结束后的转场与余波自然消退。

它不应直接命令模型说某句话，更不能把内部数值或状态名称泄漏给用户。

## 当前缺口与推进顺序

### P0：保持闭环正确性

- [已实现] 成功/失败投递与出站历史分离。
- [已实现] 生活分享与普通主动消息共享反馈链。
- [已实现] 状态改变后同步生活投影；用户消息处理的前置事件影响和最终状态保存都会回写生活运行时，避免后半段状态变化旁路生活层。
- [已实现] 生活分享部分失败的语义：只要第一条消息已送达，事件即算“已分享”（她已经说出口了），只记录实际送达的内容，避免下次对同一件事重复分享；见 `tests/test_life_event.py::test_life_event_partial_send_still_counts_as_shared`。
- [已实现] 纯主动消息投递失败也走 `reply_reconsider` 兜底（与回复失败路径一致），失败 outbox 保留为事实，稍后重新判断是否自然开口而不是重放原话；见 `tests/test_engine.py::test_failed_proactive_delivery_creates_reconsider_task`。
- [已实现] 等待心理不再依赖冷却放行：调度器每一轮先通过 `engine.refresh_waiting_state` 推进主动等待与未答问题的心理状态，即使本轮跳过主动决策；见 `tests/test_proactive_scheduler.py::test_scheduler_refreshes_waiting_state_even_when_cooldown_skips`。
- [已实现] 模型输出的 `cooldown_minutes` 由调度器真实消费：上一次主动决策（含忍住不发）声明的冷却会阻止下一轮重复评估，daemon 以 240 分钟为上限保留最终权威；到期社交事务（安抚/承诺/补句）可越过该冷却；见 `tests/test_proactive_scheduler.py`。
- [已实现] 为所有外部适配器补投递确认语义，尤其是未来微信。`/qq/webhook` 无出站通道，已改为只记录状态不生成不可投递的回复。
- [已实现] 面板快照可查看当前活动、生活账本、最近社交事务和工具请求提案；状态投影会列出活动、手机余波、慢性影响和挂起事务的具体原因（如待分享、轻微矛盾、安抚跟进等），不暴露完整提示词。

### P1：让日程真正连续

- [已实现] 基于时段的活动模板与起止时间。
- [已实现初版] 日计划：首次进入当天时生成固定私有时段计划，活动按计划推进而非每段重新抽取。计划不是记忆，也不会被写成已发生事实；见 `tests/test_life_runtime.py::test_daily_plan_is_stable_and_its_items_are_not_lived_facts_until_activated`。
- [已实现初版] 用户脆弱、边界、控制与修复等高显著事件会温和调整下一项尚未发生的安排，绝不改写当前或过去活动；见 `tests/test_life_runtime.py::test_salient_user_event_nudges_future_plan_without_rewriting_current_activity`。
- [已实现初版] “发生”和“分享”分离：活动完成时偶尔留下可追溯的私有事件和私有记忆；生活分享优先选择未分享的私有事件，成功投递才将其标记为已分享。没有候选时才生成当前活动中的微小新事件。失败投递不生成共同历史，也不消耗既有事件。
- [已实现] 事件结果：上课取消、临时邀约、天气、疲惫等非用户事件会写入 `life_event_result`，只影响当前余波和下一项未来计划，不把未来活动写成已发生事实；见 `tests/test_life_runtime.py::test_non_user_life_event_result_changes_future_plan_without_claiming_it_happened`。触发端已接入调度器：`plan_daily_life_result` 按用户+日期确定性地决定当天是否发生一件小事及其时段（多数日子什么都不发生），到点由 `maybe_apply_planned_life_result` 应用一次，日期化事件源保证不重复叠加，且用户事件余波优先；见 `tests/test_life_runtime.py::test_planned_life_result_fires_once_and_bends_the_day`。
- [已实现部分] 早间计划会读取慢性状态：疲惫降低次日高专注安排的强度，边界受损减少社交型晚间安排，强情绪让晚间收尾更低负荷；见 `tests/test_life_runtime.py::test_durable_state_changes_the_next_days_private_plan`。前一天事件和未完成安排已经有初步入口，仍待做一周级轨迹回放。

### P2：动态社交现实感

- [已实现初版] 未读/已读/打字/二次通知唤醒。
- [已实现] 看见但暂不回由 `social_tasks.reply_later` 持久化：保存原消息、原因、到期和过期时间；新消息可取消旧事务，成功投递才关闭。适配器重启后超过两分钟的逾期任务由调度器接管；见 `tests/test_social_tasks.py`。
- [已实现] 区分未读/阅读/打字/延迟回复；“被打断”会取消延迟事务并合并到新输入。读到但被手头事情岔开的情况创建 `reply_later` 事务，并以“读到了但被岔开”的原因进入同一补回管道；见 `tests/test_social_tasks.py::test_read_but_not_replied_task_can_be_created_and_claimed`。自动入口已接线：手机注意力判定“现在读”之后、回复决策仍然延迟的场景走 read-later 入口而非未读延迟；见 `tests/test_qq_websocket.py::test_reply_decision_defer_persists_a_read_later_task`。
- [已实现初版] 失败投递后创建 `reply_reconsider` 事务，不重放原入站消息；失败 outbox 保留为事实，稍后由主动决策重新判断是否自然补一句；见 `tests/test_engine.py::test_failed_deferred_reply_creates_reconsider_task_without_writing_history`。
- [已实现] 语义化未完成事务：用户脆弱表达会创建 `comfort_followup`；“晚点/明天跟你说”会创建 `promise_followup`；私有生活事件会创建 `life_share_followup`，到期后作为低压力分享候选，成功投递才标记已分享并收束；用户指出前后说法不一致会创建 `contradiction_followup`，新消息会取消挂起项，到期后允许轻描淡写地圆过去。见 `tests/test_social_tasks.py` 与 `tests/test_social_followups.py`。
- [已实现初版] 关系事件的滞后效应：最近一批互动里反复出现的温暖/修复/解释，或反复冒犯/控制/过早亲密，才会推动长期 `emotion_baseline` 与 `emotion_affinity`；单次事件只影响即时心情和印象，避免一次消息决定关系；见 `tests/test_advanced_state.py::test_repeated_interactions_change_affinity_but_one_event_does_not`。

### P3：评测与观测

- [已实现部分] 单元测试、上下文评测、消息时机测试。
- [已实现初版] 时间旅行测试：固定时钟跨越同一天，断言只完成已进入的活动而不把日计划写成事实；见 `tests/test_life_runtime.py::test_time_travel_across_a_day_keeps_continuity_and_completes_only_elapsed_activities`。
- [计划] 扩展时间旅行测试：固定时钟模拟一周，断言情绪、期待、冷却和恢复轨迹。
- [已实现初版] 增加会话回放评测：已有 `companion-eval-dialogue` / context regression；新增同一输入在不同生活状态和关系状态下必须得到不同上下文策略的确定性测试，见 `tests/test_context_orchestrator.py::test_same_input_gets_different_context_under_different_state_and_life`。`tests/test_dialogue_eval.py::test_context_regression_suite_passes` 已纳入默认 `pytest` 套件，GitHub Actions `.github/workflows/test.yml` 会在 push/PR 时自动跑。
- [已实现] 面板显示最近社交事务及其原因、到期和状态，并将活动、手机、状态余波和挂起事务汇总为“她为什么这样”的可读原因；不暴露完整提示词。视觉小屋在本机 daemon 面板中渲染当前生活投影。

## 新机制接入检查单

新增任何状态、记忆、工具、图像或平台能力前，必须回答：

1. 它的事件源、可靠性和事实所有者是什么？
2. 它影响瞬时、活动、余波、慢性轨迹还是长期事实？
3. 它何时衰减、升级、解除？
4. 它会改变读取、回复、主动、生活事件、记忆检索中的哪一个？
5. 投递失败或模型幻觉时，如何不让它变成假历史？
6. 哪个确定性测试能证明这条闭环？

若答案是“只改变 prompt”，则它不是状态机制；若答案是“完全不影响生活”，则必须
记录原因，以防以后误以为它已被集成。

## 维护节奏

- 每次功能变更：更新本文的状态标签和相关测试链接。
- 每次发现真人感问题：先把真实事件链写进本文，再决定修改模型、提示词还是状态机。
- 每周或每完成一批行为：回放固定场景，检查冷却是否机械、生活是否自相矛盾、记忆是否越权。
- 任何模型替换（如 Hermes）先跑同一评测集；模型更会表达不等于系统更闭环。
