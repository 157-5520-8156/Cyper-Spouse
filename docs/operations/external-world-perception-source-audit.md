# 外界感知来源与许可审计

状态：Phase 0 登记表。当前没有任何生产来源或 RSSHub route 获准；外界感知 sidecar
不得进入生产调度，也不得向角色模型、World V2、Life Ecology 或消息发送链提供数据。

每个来源上线前必须完成一行登记，并把完整证据链接到变更记录。技术上能抓取不等于拥有
缓存、派生、embedding、模型处理、引用或永久冻结的权利。

## 来源登记

| source_id | Adapter / gateway | 上游发布者 | 固定 endpoint / route allowlist | 身份或登录依赖 | 权威范围 | 条款与许可证据 | policy revision | 状态 |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | 暂无获准生产来源 |

## Source policy 必填项

每个不可变 `policy_revision` 必须明确：

- 是否允许 fetch；
- 是否允许缓存 exact raw evidence，以及最大保留时间；
- 是否允许保存来源许可的 normalized summary，以及最大信号 TTL 和 normalized retention；
- 是否允许 embedding；
- 是否允许把材料暴露给角色模型；
- 是否允许逐字引用；
- 是否允许冻结进 World V2 的 durable snapshot；
- 删除、撤回或许可变化时的处置流程与责任人。

同名 policy revision 的内容不得变化；权限变化必须创建新 revision。若不能合法冻结足够的
冷重放证据，该来源最多用于 shadow 质量研究，不能进入 live perception。

## 上线检查

- endpoint 和 RSSHub route 由部署配置固定；模型输出不能参与 URL 或 route 拼接。
- RSSHub 必须是本机/私网隔离的独立进程，并完成 AGPL-3.0 部署和修改分发审查。
- transport gateway、上游平台/发布者和 item identity 分开保存；RSSHub 不是事实权威。
- 记录限流、登录失效、反爬变化、HTML/XML 变化、预期新鲜度和故障告警方式。
- 不向来源发送用户、角色或 NPC 的精确位置、对话、记忆、关系或其他私人状态。
- Cookie、token 和授权材料不得写入 sidecar、World 账本、模型上下文或普通日志。
- 普通 RSSHub/social 来源不能作为地震、气象或公共安全预警的唯一权威来源。
- 明确测试 payload、录制日期、预期解析字段、纠错/retraction 表达方式和 TTL。

## 首次生产接源前仍需用户确认

1. 首批 route/source 及其许可证据；
2. 公共预警的权威结构化来源；
3. 位置匹配的默认粒度、用途授权和有效期；
4. shadow 是否完全禁用用户位置；
5. raw、normalized signal 与 attention attempt 的容量预算；
6. 首批有 World 证据支持的 Perception Channels；
7. live snapshot 的 V2 事件形状。
