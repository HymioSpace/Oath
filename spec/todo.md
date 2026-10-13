# TODO

还没做的、但已经知道要做的。做完一个删一条。

- queue 的实现：事件从 core 内部消费出去的那条通路。
- eventsink 的实现：Oath 对外暴露 event 的出口（HTTP / WebSocket / 客户端推送）。
- context 如何处理 tool result 来不齐的情况：`tool.call` 没有对应 `tool.result` 时，
  组下一次模型请求该怎么补。

## 崩溃恢复要补的两件事

现状：runtime 重启后执行表清空，悬挂的 `tool.call` 永远收不了尾，pending 的审批也永远
不会被判过期。两个模块各自有各自的持久化真相（journal 与 permission store），
`journal.recover()` 只看得到前者。

- **悬挂的 `tool.call` 补终态。** 工具跑在 runtime 进程内的线程池里，进程死了线程也死了，
  不存在可以续的执行 —— 能做的只有补一条终态事件填上对话历史里的空位。记
  `outcome_unknown` 而不是 `timeout`：进程死时工具可能已经落过外部副作用，
  `timeout` 会让上层以为没跑过而重试。复用这个取值是协议决定，下游要同步。
- **pending 的审批重扫。** `PermissionManager` 的 deadline 堆只在本次进程内攒，重启后
  `heap` 是空的。要一个 `sweep_expired(scope)`：pending 且 `expires_dt` 已过的发
  `permission.resolved{expired}`，幂等。

两者都做成**显式且幂等**的方法（`ToolSystem.settle(scope, ...)` /
`PermissionManager.sweep_expired(scope)`），由 runtime 在 `journal.recover()` 之后调用，
不在构造函数里隐式扫 —— 这两个动作都要发事件，隐式做的话投递时机不受调用方控制，
而且「恢复时补发了哪些终态」应该是上层看得见的一等返回值。

**timer 不需要持久化。** deadline 与 `expires_dt` 都是「一个已落盘的事实 + 一个墙钟
时刻」，重启后重算即可。墙钟回拨会让过期判定偏晚，方向是安全的：宁可多等一会儿人批，
也不要把已经同意的请求判成过期，或把跑了一半的调用当成没跑过。
