# TODO

还没做的、但已经知道要做的。做完一个删一条。

- queue 的实现：事件从 core 内部消费出去的那条通路。
- eventsink 的实现：Oath 对外暴露 event 的出口（HTTP / WebSocket / 客户端推送）。
- context 如何处理 tool result 来不齐的情况：`tool.call` 没有对应 `tool.result` 时，
  组下一次模型请求该怎么补。