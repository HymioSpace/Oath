# Oath 接入手册

一条 WebSocket 发送 command、接收 event。协议机制见 [事件通路](feature-event-runtime.md)。

## 1 启动与连接

```bash
export MINIMAX_API_KEY=...  # 按配置的 api_key_env 设置
uv run python scripts/run_runtime.py --config oath.toml
```

默认监听 `ws://127.0.0.1:8765`；biz server 在 `http://127.0.0.1:8766`，整个 biz
一个端口，每条查询是其中一个 path。配置中的
credential 映射仅用于演示；部署由 Hymio 提供身份与权限策略。

先发送：

```json
{"v":1,"type":"session.open","id":"open-1","payload":{"credential":"synthetic-demo-token"}}
```

收到绑定回执后，所有 command 都带返回的 session_id：

```json
{"v":1,"type":"ack","id":"open-1","session_id":"default"}
{"v":1,"type":"user.message","id":"user-1","session_id":"default","payload":{"content":"帮我查天气","user_context":{"project":"hymio"}}}
```

user_id 由服务端认证决定。credential 不认则 close unauthorized，不回落为匿名用户。

## 2 Command

| type | payload | 回应 |
| --- | --- | --- |
| session.open | credential | 绑定 ACK |
| user.message | content: string，user_context: object（见下） | 可靠接纳 ACK；后续事件异步到达 |
| permission.decide | invocation_id, status: approved / denied, reason? | 可靠接纳 ACK |
| run.cancel | run_id?, reason? | 可靠接纳 ACK；未指定 run_id 时绑定处理时的当前 run |
| step.retry | 空对象 | 可靠接纳 ACK；重试失败或暂停的原 step |
| session.status | 空对象 | 当前 session.status 事件，不触发模型 |
| ping | 空对象 | pong，不落盘 |

ACK 表示已保存命令，不代表模型已回答、权限已生效或工具完成。写入失败回 error，不回成功 ACK。
命令 ID 使用唯一字符串；重发同一命令复用原 ID 和内容。同 ID 不同内容报错。
客户端不得替换审批对应的工具 / 参数；decided_by 即使携带也由服务端认证身份覆盖。
session.status 不产生输入、不改任何状态，可以随时重发。trace 查询不在 WebSocket 上，走 [§4 Trace 查询](#4-trace-查询) 的 HTTP。

`user_context` 是这一条消息带来的会话上下文，**必填**，没有新事实就传空对象
`{}`。它是一张 kv：同名 key 直接覆盖并更新时间，超过 24 小时没人再碰的作废，
服务端按 session 维护，不会带进模型上下文。形状不是对象、key 为空字符串时整帧拒掉
（`bad_payload`）。这份 kv 唯一的去处是 [§5 CLI 代理](#5-cli-代理) 里的业务命令，
要不要用它、怎么判权限由命令自己决定。

## 3 审批与重连

permission.requested 带 invocation_id、batch_id、tool、arguments、expires_at。展示原动作并提交裁定：

```json
{"v":1,"type":"permission.decide","id":"decision-1","session_id":"default","payload":{"invocation_id":"step-example-0","status":"approved"}}
```

批准后直接执行该原调用；UI 等 tool.terminal 或查询状态，不需要发送新的 user.message。
拒绝 / 过期同样产生真实终态。waiting_approval 是执行状态，不是终态 outcome。

断线后重新 session.open，再 session.status。响应包含：

- run_id、step_id、step_status、error、trace_id（trace_id 在 step 结束后不清，见 §4.1）。
- calls：原调用身份、工具 / 参数、审批 / 执行状态、期限与终态。
- messages：固定的对话视图；末尾可能是尚未获得响应的工具声明。
- input_errors：被隔离的无效内部输入。

按原 invocation_id 恢复授权卡；不要通过重新生成调用找回它。查询快照当前不分页。
step failed / paused 时提供显式“重试 / 继续”，发送 step.retry；新用户消息不会越过失败 step。

## 4 Trace 查询

一条 trace 是一段 ReAct 循环：一个 step 产出工具调用，工具结果唤醒下一个 step，
直到某个 step 不再要工具为止。

查询走 biz server 的 HTTP 接口，不在 WebSocket 上：它不产生 ACK、不改任何状态，
塞进 command/event 协议里是拧的。两个 server 共用一套 credential。

path 就是动作名，参数走 query string：`GET /list_traces`、`GET /query_trace`。

```bash
TOKEN=synthetic-demo-token
BASE=http://127.0.0.1:8766
```

### 4.1 拿 trace_id

`trace_id` 在 step 被认领时就定了，之后不变。当前运行的那个从 session.status 拿：

```json
{"v":1,"type":"session.status","id":"s1","session_id":"default","payload":{}}
{"v":1,"type":"event","session_id":"default","event_type":"session.status","payload":{"run_id":"...","step_id":"...","trace_id":"6f1c9b2d...","step_status":"prepared"}}
```

运行结束后 `step_id` 变 `null`，`trace_id` 留着最后开的那条 —— 刚跑完的这一轮
还能查。什么时候真的换 id：下一次认领 step 时。

### 4.2 列历史

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/list_traces?limit=20&offset=0"
```

```json
{"trace_ids":[{"trace_id":"6f1c9b2d...","span_count":4,"step_count":2,"duration_ms":812.4}],"total":37,"limit":20,"offset":0}
```

`span_count` 是这条 trace 的 span 总数，`step_count` 是它跑了几步。
最近写入的排前面。`duration_ms` 为 `null` 表示一个 span 都没结束 —— 见 4.4。

分页用 `limit` 与 `offset`，都不给时 `limit` 取 50、上限 200；`offset` 不限大小。
`total` 是全部条数，客户端靠它判断还有没有下一页；翻过头是空页，不是报错。
`limit` 超上限直接回 400，不静默截断 —— 截断会让客户端以为那就是全部。

**翻页之间不是快照。** 排序看文件的修改时间，所以翻页过程中若有 trace 被追加（比如
重试后补写的旧 trace 前移），同一页里的条目可能重复或漏掉。当成「最近活动过的
trace 大致是这样」来看，别当成一致性分页。

### 4.3 取一条

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/query_trace?trace_id=6f1c9b2d..."
```

```json
{"trace_id":"6f1c9b2d...","duration_ms":812.4,"spans":[
  {"trace_id":"6f1c9b2d...","span_id":"a1b2c3d4e5f60718","parent_span_id":null,"name":"invoke_agent","kind":"internal","start_unix_nano":1760000000000000000,"end_unix_nano":1760000000400000000,"status":"ok","duration_ms":400.0,"attributes":{"oath.step.id":"step-example-0","oath.step.status":"completed","oath.step.llm_calls":1,"oath.step.duration_ms":400.0},"events":[{"name":"oath.input.user","unix_nano":1760000000000000000,"attributes":{"body":"{...}"}},{"name":"oath.stage.tools_submitted","unix_nano":1760000000399000000,"attributes":{"oath.step.tool_calls":1}}],"links":[]},
  {"trace_id":"6f1c9b2d...","span_id":"a1b2c3d4f6071801","parent_span_id":"a1b2c3d4e5f60718","name":"chat m1","kind":"client","start_unix_nano":1760000000100000000,"end_unix_nano":1760000001200000000,"status":"ok","duration_ms":1100.0,"attributes":{"gen_ai.request.model":"m1","gen_ai.usage.input_tokens":812,"gen_ai.usage.output_tokens":64,"oath.llm.ttft_ms":420.0,"oath.llm.tpot_ms":12.1},"events":[{"name":"gen_ai.request","unix_nano":1760000000100000000,"attributes":{"body":"{...}"}},{"name":"gen_ai.response","unix_nano":1760000001200000000,"attributes":{"body":"{...}"}}],"links":[]}
]}
```

### 4.4 怎么读

**span 树只有两层，step 之间平级。** `parent_span_id` 为空的是 step span
（`invoke_agent`），它下面 `chat <模型>` 的是那一步的模型调用 span。第二步发生在
第一步结束之后，不嵌在第一步里。

**step 与上一步的因果看 `links`。** 每条工具结果各指向产出它的那一步的 span，
字段是 `trace_id` / `span_id` / `attributes`。跨 trace 时 `trace_id` 指的是**另一条**
trace —— 用户新消息会开新 trace，但和它一起被认领的旧工具结果仍指回原来那条。
按 `links` 串起来才是完整的因果链，光看 `trace_id` 分组会断。

**耗时直接读 `duration_ms`**，服务端算好的，不要用 `start_unix_nano` 自己减。

**原始输入输出在 `events` 里**，不在 `attributes`：
`oath.input.<队列名>` 是唤醒这一步的每一条输入（`user` / `tool` / `system`），
`gen_ai.request` 是发给模型的那一份，`gen_ai.response` 是模型回来的那一份。
`attributes.body` 是 JSON 文本，要 parse。记到 core 冻结的那一层为止，provider
内部的 wire 报文不在里面。

**阶段节点 `oath.stage.*`** 带时刻，把 step 里不属于模型调用的时间切开：
`request_prepared`（压缩判定与请求冻结完成，到这里之前算「模型之前」）、
`tools_submitted`、`completed`。慢 step 看这几个节点就知道慢在哪一段。

**字段缺席是有意义的。** 非流式调用没有 `oath.llm.ttft_ms`，也就没有
`oath.llm.tpot_ms` —— 别把缺席当 0。同理 step span 的 `oath.step.llm_calls` 为 0
表示还没调过模型（`events` 里也就只有输入、没有 request）。

**`end_unix_nano` 为 `null`** 表示这一步还没结束：进程崩在了步骤中途。
按 `status` 分 `ok` 与 `error`，`unset` 配 `null` 就是崩在半路，阶段节点也随之没有。

span 字段与 OpenTelemetry 的 span 模型一致，属性名清单见 [Trace](feature-trace.md)。
客户端按 `step_count` 展示循环轮数、按 `duration_ms` 展示耗时即可，
不要试图从 trace 还原成对话。

### 4.5 错误与空态

credential 不认回 `401 {"error":"unauthorized"}`，响应里不回显凭据本身。
参数不合法（非整数、负数、超上限、缺 trace_id）回 `400 {"error":"bad_query","message":...}`。
读 trace 失败回 `500 {"error":"internal_error"}` —— 不把故障说成空列表。

`trace_id` 不存在、或属于别的 session，都回 `spans: []` 与 `duration_ms: null`，
不报错，也不区分「不存在」和「无权查看」。按空态处理即可。

### 4.6 自动化检查

用替身模型和文件后端，不连真实模型、不起 socket：

```bash
uv run pytest -q tests/test_biz_server.py tests/test_biz_trace.py tests/test_trace_store.py tests/test_trace_brain.py
```

## 5 CLI 代理

业务命令挂在 biz server 上，与 trace 查询同一个端口、同一套 credential。模型在 bash 里
执行 `oath-cli <命令名> [参数...] [--key=value]` 调它们。

列出可用命令（`oath-cli` 不带参数时打的就是这条）：

```
GET /cli_commands
{"commands":[{"name":"echo","description":"Echo the text back.","parameters":{...}}]}
```

调用一条命令：**POST，参数放 JSON body**。命令是动作，GET 的缓存与重试语义不对。

```
POST /cli/echo
Authorization: Bearer <credential>
Content-Type: application/json

{"text": "工作区"}
```

`parameters` 是 JSON Schema，**属性声明的顺序就是位置参数的顺序**；缺失、拼错、
类型不对的参数一律 400，body 不是对象或不是 JSON 也是 400，认不出的命令名 404，
credential 不认 401。同一个 path 用 GET 打会回 405。命令自己抛错回 500，不带异常正文。

命令在 `<扩展目录>/cli/<名字>.py` 里声明（模块级 `COMMANDS`），启动时扫出来；路由固定在
`biz/cli/cli_server.py`。加命令就是加一个文件，路由不用动。命令收到的 `user_context` 是
该 session 当前的 kv（已裁掉过期的），框架不解释也不拿它判权限 —— 要鉴权就写在命令里。

`oath-cli` 从 `OATH_CLI_SERVER` / `OATH_CLI_TOKEN` 读端点与 credential，由执行环境注入
子进程；这两个变量没设就退出码 2（用法错），请求失败退出码 1，正文打到 stdout。

## 6 Event

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

## 7 错误与连接

error 带 id、code、message。帧结构 / 协议版本不合法时断开；已解析 command 的业务失败保留连接。
常见 code：bad_frame、bad_payload、not_bound、session_mismatch、already_bound、not_found、internal_error。

close reason：unauthorized、session_replaced、slow_consumer、protocol_error。
慢连接发送队列满会断开，重连应退避并查询状态。idle_timeout 尚未启用。

## 8 压缩事件与端到端验证

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
