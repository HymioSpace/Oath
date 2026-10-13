# TODO

还没做的、但已经知道要做的。做完一条删一条。

| 分类 | 事项 | 状态 | 描述 |
| --- | --- | --- | --- |
| 事件通路 | Brain | 未开始 | context build → LLM → tool call → compress，现在只有 interface |
| 事件通路 | Intake | 未开始 | 接 command 落 journal 再入队 + 消费时逐条路由，现在只有 interface |
| 崩溃恢复 | 未取走的输入 | 未开始 | 队列是内存的，重启后清空；journal 里已落盘但没被消费的 `user.message` / `tool.result` 不会自动重投 |
| 事件通路 | 对外投递 | 未开始 | `AgentEventSink` + `OutboundTransport` 的实现：上行收 command、`QUEUE_FULL` 时怎么断、鉴权 |
| 审批 | 审批事件进 journal | 未开始 | 审批事件现在只在 `permissions.jsonl`，agent 看不到它们 |
| 崩溃恢复 | 悬挂调用的终态 | 未开始 | runtime 重启后没有 `tool.result` 的 `tool.call` 要补一条终态，否则投影一直发 `unresolved` 的 tool 消息给模型 |
| 崩溃恢复 | 审批过期重扫 | 未开始 | 重启后待审批的请求不会被判过期，一直挂着等于「随时可能被批准」 |
| 上下文 | tool result 来不齐 | 未开始 | 组下一次模型请求时，缺结果的 tool call 怎么补 |
| 对外投递 | 客户端漏收事件 | 未开始 | 投递是活的，断线期间的事件不补发。补发要么从 journal 重放，要么加一条「当前状态」查询 |
