# Oath 接入手册

一条 WebSocket 发送 command、接收 event。协议机制见 [事件通路](feature-event-runtime.md)。

## 1 启动与连接

```bash
export MINIMAX_API_KEY=...  # 按配置的 api_key_env 设置
uv run python scripts/run_runtime.py --config oath.toml
```

默认监听 `ws://127.0.0.1:8765`。配置中的 credential 映射仅用于演示；部署由 Hymio 提供身份与权限策略。

先发送：

```json
{"v":1,"type":"session.open","id":"open-1","payload":{"credential":"synthetic-demo-token"}}
```

收到绑定回执后，所有 command 都带返回的 session_id：

```json
{"v":1,"type":"ack","id":"open-1","session_id":"default"}
{"v":1,"type":"user.message","id":"user-1","session_id":"default","payload":{"content":"帮我查天气"}}
```

user_id 由服务端认证决定。credential 不认则 close unauthorized，不回落为匿名用户。

## 2 Command

| type | payload | 回应 |
| --- | --- | --- |
| session.open | credential | 绑定 ACK |
| user.message | content: string | 可靠接纳 ACK；后续事件异步到达 |
| permission.decide | invocation_id, status: approved / denied, reason? | 可靠接纳 ACK |
| run.cancel | run_id?, reason? | 可靠接纳 ACK；未指定 run_id 时绑定处理时的当前 run |
| step.retry | 空对象 | 可靠接纳 ACK；重试失败或暂停的原 step |
| session.status | 空对象 | 当前 session.status 事件，不触发模型 |
| ping | 空对象 | pong，不落盘 |

ACK 表示已保存命令，不代表模型已回答、权限已生效或工具完成。写入失败回 error，不回成功 ACK。
命令 ID 使用唯一字符串；重发同一命令复用原 ID 和内容。同 ID 不同内容报错。
客户端不得替换审批对应的工具 / 参数；decided_by 即使携带也由服务端认证身份覆盖。

## 3 审批与重连

permission.requested 带 invocation_id、batch_id、tool、arguments、expires_at。展示原动作并提交裁定：

```json
{"v":1,"type":"permission.decide","id":"decision-1","session_id":"default","payload":{"invocation_id":"step-example-0","status":"approved"}}
```

批准后直接执行该原调用；UI 等 tool.terminal 或查询状态，不需要发送新的 user.message。
拒绝 / 过期同样产生真实终态。waiting_approval 是执行状态，不是终态 outcome。

断线后重新 session.open，再 session.status。响应包含：

- run_id、step_id、step_status、error。
- calls：原调用身份、工具 / 参数、审批 / 执行状态、期限与终态。
- messages：固定的对话视图；末尾可能是尚未获得响应的工具声明。
- input_errors：被隔离的无效内部输入。

按原 invocation_id 恢复授权卡；不要通过重新生成调用找回它。查询快照当前不分页。
step failed / paused 时提供显式“重试 / 继续”，发送 step.retry；新用户消息不会越过失败 step。

## 4 Event

事件信封：

```json
{"v":1,"type":"event","session_id":"default","event_type":"assistant.message","occur_dt":"2026-10-14T00:00:00Z","payload":{"step_id":"step-example","content":"好的","reasoning":null,"tool_calls":[],"usage":null}}
```

| event_type | 关键字段 |
| --- | --- |
| assistant.delta | step_id, channel, text, seq |
| assistant.message | step_id, content, reasoning, tool_calls, usage |
| permission.requested | invocation_id, batch_id, tool, arguments, expires_at |
| tool.terminal | event_id, invocation_id, tool_call_id, batch_id, run_id, source, outcome, content? / error?, detail_key?, original_chars? |
| step.status | status, error, step_id? |
| intake.rejected | input_id, error |
| session.status | 当前快照 |

tool.terminal outcome 为 succeeded / failed / timeout / cancelled / outcome_unknown。
结果正文超过阈值时 content 是截断后的文本，detail_key 指到全文，original_chars 是原长度；
助手用内置工具 detail_read 取回全文，它的结果不再截断。
source=system 表示原调用经 system 裁定 / 控制产生；不意味着该结果应立即独自唤醒模型。
unknown / timeout 不证明外部动作没有发生，客户端不能据此自动重做副作用。

assistant.delta 的 channel 为 content / reasoning，只用于草稿展示；同一步网络重试可能重新生成。
用完整 assistant.message 或重连查询替换草稿。tool.terminal 通知可重复，按 event_id 去重。
在线事件不补发，occur_dt 不用于消费排序。持久化事务日志是 runtime.commit，客户端不需要解析它。

## 5 错误与连接

error 带 id、code、message。帧结构 / 协议版本不合法时断开；已解析 command 的业务失败保留连接。
常见 code：bad_frame、bad_payload、not_bound、session_mismatch、already_bound、not_found、internal_error。

close reason：unauthorized、session_replaced、slow_consumer、protocol_error。
慢连接发送队列满会断开，重连应退避并查询状态。idle_timeout 尚未启用。
