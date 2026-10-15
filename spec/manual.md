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
| trace.list | 空对象 | 当前 session 的 trace.list 事件，不触发模型 |
| trace.get | trace_id: string | trace.get 事件，不触发模型 |
| ping | 空对象 | pong，不落盘 |

ACK 表示已保存命令，不代表模型已回答、权限已生效或工具完成。写入失败回 error，不回成功 ACK。
命令 ID 使用唯一字符串；重发同一命令复用原 ID 和内容。同 ID 不同内容报错。
客户端不得替换审批对应的工具 / 参数；decided_by 即使携带也由服务端认证身份覆盖。
session.status / trace.list / trace.get 不产生输入、不改任何状态，可以随时重发。

## 3 审批与重连

permission.requested 带 invocation_id、batch_id、tool、arguments、expires_at。展示原动作并提交裁定：

```json
{"v":1,"type":"permission.decide","id":"decision-1","session_id":"default","payload":{"invocation_id":"step-example-0","status":"approved"}}
```

批准后直接执行该原调用；UI 等 tool.terminal 或查询状态，不需要发送新的 user.message。
拒绝 / 过期同样产生真实终态。waiting_approval 是执行状态，不是终态 outcome。

断线后重新 session.open，再 session.status。响应包含：

- run_id、step_id、step_status、error、trace_id。
- calls：原调用身份、工具 / 参数、审批 / 执行状态、期限与终态。
- messages：固定的对话视图；末尾可能是尚未获得响应的工具声明。
- input_errors：被隔离的无效内部输入。

按原 invocation_id 恢复授权卡；不要通过重新生成调用找回它。查询快照当前不分页。
step failed / paused 时提供显式“重试 / 继续”，发送 step.retry；新用户消息不会越过失败 step。

## 4 Trace 查询

一条 trace 是一段 ReAct 循环：一个 step 产出工具调用，工具结果唤醒下一个 step，
直到某个 step 不再要工具为止。trace.list 返回当前 session 的全部 trace，
trace.get 返回其中一条的 span 列表。两者都只读，不触发模型。

### 4.1 拿 trace_id

`trace_id` 在 step 被认领时就定了，之后不变。当前运行的那个从 session.status 拿：

```json
{"v":1,"type":"session.status","id":"s1","session_id":"default","payload":{}}
{"v":1,"type":"event","session_id":"default","event_type":"session.status","payload":{"run_id":"...","step_id":"...","trace_id":"6f1c9b2d...","step_status":"prepared"}}
```

运行结束后 `step_id` 变 `null`，`trace_id` 随之变 `null` —— 这一轮已经不在飞了。
要留住 id 就自己存，没有别的来源。

### 4.2 列历史

```json
{"v":1,"type":"trace.list","id":"t1","session_id":"default","payload":{}}
{"v":1,"type":"event","session_id":"default","event_type":"trace.list","payload":{"trace_ids":[{"trace_id":"6f1c9b2d...","span_count":4,"step_count":2,"duration_ms":812.4}]}}
```

`span_count` 是这条 trace 的 span 总数，`step_count` 是它跑了几步。
`duration_ms` 为 `null` 表示一个 span 都没结束 —— 见 4.4。

### 4.3 取一条

```json
{"v":1,"type":"trace.get","id":"t2","session_id":"default","payload":{"trace_id":"6f1c9b2d..."}}
{"v":1,"type":"event","session_id":"default","event_type":"trace.get","payload":{"trace_id":"6f1c9b2d...","duration_ms":812.4,"spans":[
  {"trace_id":"6f1c9b2d...","span_id":"a1b2c3d4e5f60718","parent_span_id":null,"name":"invoke_agent","kind":"internal","start_unix_nano":1760000000000000000,"end_unix_nano":1760000000400000000,"status":"ok","duration_ms":400.0,"attributes":{"oath.step.id":"step-example-0","oath.step.status":"completed","oath.step.llm_calls":1,"oath.step.duration_ms":400.0},"events":[{"name":"oath.input.user","unix_nano":1760000000000000000,"attributes":{"body":"{...}"}}]},
  {"trace_id":"6f1c9b2d...","span_id":"a1b2c3d4f6071801","parent_span_id":"a1b2c3d4e5f60718","name":"chat m1","kind":"client","start_unix_nano":1760000000100000000,"end_unix_nano":1760000001200000000,"status":"ok","duration_ms":1100.0,"attributes":{"gen_ai.request.model":"m1","gen_ai.usage.input_tokens":812,"gen_ai.usage.output_tokens":64,"oath.llm.ttft_ms":420.0,"oath.llm.tpot_ms":12.1},"events":[{"name":"gen_ai.request","unix_nano":1760000000100000000,"attributes":{"body":"{...}"}},{"name":"gen_ai.response","unix_nano":1760000001200000000,"attributes":{"body":"{...}"}}]}
]}}
```

### 4.4 怎么读

**span 树只有两层。** `parent_span_id` 为空的是 step span（`invoke_agent`），
它下面 `chat <模型>` 的是那一步的模型调用 span。step 之间是平级的 —— 第二步发生在
第一步结束之后，不嵌在第一步里。

**耗时直接读 `duration_ms`**，服务端算好的，不要用 `start_unix_nano` 自己减。

**原始输入输出在 `events` 里**，不在 `attributes`：
`oath.input.<队列名>` 是唤醒这一步的每一条输入（`user` / `tool` / `system`），
`gen_ai.request` 是发给模型的那一份，`gen_ai.response` 是模型回来的那一份。
`attributes.body` 是 JSON 文本，要 parse。

**字段缺席是有意义的。** 非流式调用没有 `oath.llm.ttft_ms`，也就没有
`oath.llm.tpot_ms` —— 别把缺席当 0。同理 step span 的 `oath.step.llm_calls` 为 0
表示还没调过模型（`events` 里也就只有输入、没有 request）。

**`end_unix_nano` 为 `null`** 表示这一步还没结束：进程崩在了步骤中途。
按 `status` 分 `ok` 与 `error`，`unset` 配 `null` 就是崩在半路。

span 字段与 OpenTelemetry 一致，属性名清单见 [Trace](feature-trace.md)。
客户端按 `step_count` 展示循环轮数、按 `duration_ms` 展示耗时即可，
不要试图从 trace 还原成对话。

### 4.5 找不到的时候

`trace_id` 不存在、或属于别的 session，都返回 `spans: []` 与 `duration_ms: null`，
不报错，也不区分「不存在」和「无权查看」。按空态处理即可。

### 4.6 手工试

当前终端客户端只发聊天消息。用支持原始帧的客户端连上后依次发：

```json
{"v":1,"type":"session.status","id":"q1","session_id":"default","payload":{}}
{"v":1,"type":"trace.list","id":"q2","session_id":"default","payload":{}}
{"v":1,"type":"trace.get","id":"q3","session_id":"default","payload":{"trace_id":"<4.1 拿到的>"}}
```

自动化链路检查使用替身模型和文件后端，不调用真实模型：

```bash
uv run pytest -q tests/test_biz_trace.py tests/test_trace_store.py tests/test_trace_brain.py
```

## 5 Event

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
| trace.list | trace_id, span_count, step_count, duration_ms |
| trace.get | trace_id, duration_ms, spans |

tool.terminal outcome 为 succeeded / failed / timeout / cancelled / outcome_unknown。
结果正文超过阈值时 content 是截断后的文本，detail_key 指到全文，original_chars 是原长度；
助手用内置工具 detail_read 取回全文，它的结果不再截断。
source=system 表示原调用经 system 裁定 / 控制产生；不意味着该结果应立即独自唤醒模型。
unknown / timeout 不证明外部动作没有发生，客户端不能据此自动重做副作用。

assistant.delta 的 channel 为 content / reasoning，只用于草稿展示；同一步网络重试可能重新生成。
用完整 assistant.message 或重连查询替换草稿。tool.terminal 通知可重复，按 event_id 去重。
在线事件不补发，occur_dt 不用于消费排序。持久化事务日志是 runtime.commit，客户端不需要解析它。

## 6 错误与连接

error 带 id、code、message。帧结构 / 协议版本不合法时断开；已解析 command 的业务失败保留连接。
常见 code：bad_frame、bad_payload、not_bound、session_mismatch、already_bound、not_found、internal_error。

close reason：unauthorized、session_replaced、slow_consumer、protocol_error。
慢连接发送队列满会断开，重连应退避并查询状态。idle_timeout 尚未启用。

## 7 压缩事件与端到端验证

`context.compaction` 走同一条 WebSocket，下行 payload 为：

```json
{"compression_id":"opaque-id","status":"started","mode":"async","covers":8,"reason":null}
```

status 为 started / ready / applied / failed / skipped。ready 表示异步摘要已保存但未生效；
applied 表示已替换模型视图。mode 为 async / sync / pending（应用已有产物）。
compression_id 关联同一次摘要的生命周期；covers 是原始历史覆盖条数。
事件不携带原文或摘要；失败 reason 为固定分类，不含模型或存储异常正文。
推送失败不改变保存结果。重连通过 session.status.compaction 查询 pending / applied 的身份和覆盖范围。

### 配置

在本地 oath.toml **已有的 `[agent]` 段**添加或修改以下字段，不要重复创建该段：

```toml
context_window = 8000
max_output_tokens = 512
compress_async_at_ratio = 0.2
compress_sync_at_ratio = 0.6
compress_keep_recent = 4
compress_summary_tokens = 128
tool_result_detail_chars = 200
```

这是低阈值测试配置；修改后重启服务。system prompt、工具定义和输出预算也占用计量。
保留 `[agent.provider]` 的有效模型配置，API key 从环境变量读取。
建议修改顶层 data_root 使用独立测试目录，避免旧历史导致启动后的首次请求直接进入同步压缩。

### 操作与观察

在仓库目录启动服务，另开终端连接：

```bash
uv run python scripts/run_runtime.py --config oath.toml
PYTHONPATH=. uv run python scripts/terminal_client.py --url ws://127.0.0.1:8765 --token demo-token
```

token 使用本地配置里对应的凭据。终端客户端会打印 context.compaction 事件。

1. 连续发送几段各约 500 汉字的合成资料，带不同编号，要求助手只回复“已记录”。
   逐条等回复；消息数量需超过保留的 4 条，且可压前缀大于摘要预算。
2. 超过 20% 后看到 async started → ready；此时只有 pending，没有 applied。
3. 等 ready 后继续发送资料，超过 60% 时看到 pending applied；产物不足以降到阈值下时，
   还会看到 sync started → applied。若直接跨过同步阈值，直接同步压缩也是预期行为。
4. 询问早先资料中的编号或约束，验证摘要保留关键信息。failed / skipped 表示此次没有有效摘要，
   不能按“压缩成功”验收；LLM 摘要质量需要实际模型验证。
5. 要求调用 echo 返回超过 200 字符的合成文本，查看 tool.terminal 的截断提示与 detail_key；
   再要求调用 detail_read 读取该 key，确认取回完整正文且未再次截断。
6. 重启服务并用同一凭据连接；通过 session.status 查询产物，再发送一句短消息，验证恢复后继续使用摘要。

session.status 是 WebSocket command；当前终端客户端仅发送聊天消息。可使用支持原始帧的客户端发送：

```json
{"v":1,"type":"session.status","id":"status-unique-id","session_id":"default","payload":{}}
```

自动化链路检查使用替身模型和客户端通道，不调用真实模型：

```bash
uv run pytest -q tests/test_compaction_events.py tests/test_compaction_regressions.py tests/test_compaction_runtime.py
```
