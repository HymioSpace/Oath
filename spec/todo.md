# TODO

还没做的、但已经知道要做的。做完一条删一条。

| 分类 | 事项 | 描述 |
| --- | --- | --- |
| 崩溃恢复 | 未取走的输入 | 队列是内存的，重启后清空；journal 里已落盘但没被消费的 `user.message` / `tool.result` 不会自动重投 |
| 崩溃恢复 | 悬挂调用的终态 | runtime 重启后没有 `tool.result` 的 `tool.call` 要补一条终态，否则投影一直发 `unresolved` 的 tool 消息给模型 |
| 崩溃恢复 | 审批过期重扫 | 重启后待审批的请求不会被判过期，一直挂着等于「随时可能被批准」 |
| 上下文 | 压缩 | `ContextCompressor` 是接口，v1 是 `NullCompressor`。`compress_at_ratio` 已经在配置里，没有触发它的实现 |
| 上下文 | 一步内的调用闸 | `max_step_tokens` 在配置里，没有东西读它 —— 模型与工具互相踢皮时没有闸 |
| 上下文 | tool result 来不齐 | 组下一次模型请求时，缺结果的 tool call 怎么补 |
| LLM | 重试与 fallback | 模型调用失败就抛 `LlmError`，队列里那条已经出队，不会自动重试。`outcome_unknown` 在工具侧有，模型侧还没有 |
| LLM | 多 provider | 配置里只有一个 provider，也没有按工具选模型的位置 |
| 对外投递 | 客户端漏收事件 | 投递是活的，断线期间的事件不补发。补发要么从 journal 重放，要么加一条「当前状态」查询 |
| 审批 | 审批事件进 journal | 审批事件现在只在 `permissions.jsonl`，agent 看不到它们 |
| 审批 | `permission.decide` | wire 上有这条命令，`JournalIntake` 收到会抛 `WireProtocolError` |
