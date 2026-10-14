# TODO

还没做的、但已经知道要做的。做完一条删一条。

| 分类 | 事项 | 描述 |
| --- | --- | --- |
| 崩溃恢复 | 未搬进 buffer 的输入 | 队列是内存的，重启后清空。那一批输入既没落盘也没持久化，直接丢 |
| 崩溃恢复 | 悬挂调用的终态 | runtime 重启后没有 `tool.result` 的 `tool.call` 要补一条终态，否则投影一直发 `unresolved` 的 tool 消息给模型 |
| 崩溃恢复 | 审批过期重扫 | 重启后待审批的请求不会被判过期，一直挂着等于「随时可能被批准」 |
| 存储 | buffer 换 SQLite | `EventBuffer` 现在是 per-scope 文件，单进程单写者。接口里没有路径，换实现契约不动 |
| 上下文 | 压缩 | `ContextCompressor` 是接口，v1 是 `NullCompressor`。`compress_at_ratio` 已经在配置里，没有触发它的实现 |
| 上下文 | 一步内的调用闸 | `max_step_tokens` 在配置里，没有东西读它 —— 模型与工具互相踢皮时没有闸 |
| LLM | 重试与 fallback | 模型调用失败就抛 `LlmError`，这一批还在 buffer 里不会丢，但没有东西重新触发它 |
| LLM | 多 provider | 配置里只有一个 provider，也没有按工具选模型的位置 |
| 对外投递 | 客户端漏收事件 | 投递是活的，断线期间的事件不补发。补发要么从 journal 重放，要么加一条「当前状态」查询 |
| 审批 | 审批事件进 journal | 审批事件现在只在 `permissions.jsonl`，agent 看不到它们 |
| 审批 | `permission.decide` | wire 上有这条命令，bootstrap 收到会回 `unsupported_command` |
| Supervisor | `system.invoke` | system 队列现在没有事件源。`run.cancel` 落成 `system.cancel` 之后要有人周期性地来问「有没有待中止的 run」 |
