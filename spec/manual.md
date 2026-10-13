# Oath 接入手册

客户端连上 Oath，发一句 `user.message`，收一串事件。**一条 WebSocket 双向复用**，没有
别的协议。

这份文档只讲「怎么连、帧长什么样」。为什么这么设计看
[feature-event-runtime](feature-event-runtime.md)。

## 1 跑起来

```bash
export MINIMAX_API_KEY=...            # 或配置里 api_key_env 指的那个变量
uv run python scripts/run_runtime.py --config oath.toml
```

监听地址取 `oath.toml` 的 `[server]`，默认 `ws://127.0.0.1:8765`。可用的 `credential`
在 `[[credentials]]` 里配。

想看一遍真实交互，`scripts/terminal_client.py` 是一个完整客户端，几十行。

## 2 连上

第一步必须是 `session.open`：

```json
{"v":1,"type":"session.open","id":"c-1","payload":{"credential":"demo-token"}}
```

回一帧 `ack`，`session_id` 在里面：

```json
{"v":1,"type":"ack","id":"c-1","session_id":"default"}
```

**`user_id` 不在上行帧里**，服务端从 `credential` 推导。之后每条 command 都要带这个
`session_id`。`ack` 是「凭据过了、session 绑上了」，不是「你这条消息到了」。

credential 不认：回一帧 `close{reason:"unauthorized"}` 然后断开，不回落到匿名。

## 3 上行

信封五个字段，多一个就报错：

```json
{"v":1,"type":"user.message","id":"c-2","session_id":"default","payload":{"content":"北京天气"}}
```

| type | payload | 说明 |
| --- | --- | --- |
| `session.open` | `{credential}` | 绑定 session，只能是第一条 |
| `user.message` | `{content}` | 用户输入 |
| `permission.decide` | `{invocation_id, status, decided_by?, reason?}` | 还没接 |
| `run.cancel` | `{reason?}` | 目前只记一笔，没有可中止的 run |
| `ping` | `{}` | 心跳，回 `pong`，不落盘 |

**`id` 就是幂等键，唯一性范围是 session。** 用随机 id；重发同一条消息复用同一个 id，
journal 会去重。从 1 递增会出事：重连后新消息撞上旧消息的键，服务端只能按「同键异
payload」报错。

`v` 不认识就断开重协商，别猜 —— 猜出来的结果是「按旧版解析成功、按新版执行失败」，
失败点会落在某个看起来无关的地方。

## 4 下行

五种帧，`type` 分开：

| type | 什么时候 |
| --- | --- |
| `event` | 发生了什么 |
| `ack` | `session.open` 成了 |
| `pong` | `ping` 的回声 |
| `error` | 这条 command 收到但没办成 |
| `close` | 服务端要断开，附 `reason` |

`event` 帧：

```json
{
  "v": 1,
  "type": "event",
  "session_id": "default",
  "occur_dt": "2026-10-13T13:52:52.979Z",
  "event_type": "assistant.message",
  "payload": {"content": "晴，25 度", "reasoning": null, "model": "MiniMax-M2.7"}
}
```

**下行是活的投递，不是 journal 的转发。** 没有游标，断线期间发生的事件不补发。
`occur_dt` 只用来显示，客户端不要拿它排序或判定。

### 事件

| `event_type` | payload | 说明 |
| --- | --- | --- |
| `assistant.delta` | `{step_id, channel, text, seq}` | 边生成边发，见下 |
| `assistant.message` | `{content, reasoning, tool_calls?, usage?, model?}` | 这一轮说完的话 |
| `tool.call` | `{tool_call_id, invocation_id, tool, arguments}` | 要调工具了 |
| `tool.terminal` | `{invocation_id, tool_call_id, outcome, content?, error?}` | 工具跑完了 |

事件叫 `tool.terminal`，journal 里那条记录叫 `tool.result` —— 两个名字指同一件事。

`assistant.message` 里的 `tool_calls` 是 `{id, name, arguments}` 的数组。投影靠它给每个
调用占位，所以它和紧随其后的 `tool.call` 事件一一对应。

`tool.terminal` 的 `outcome` 取 `succeeded` / `failed` / `timeout` / `cancelled` /
`outcome_unknown`。`outcome_unknown` 的意思是「跑过了但结果丢了」—— 别当成失败重试，
第三方系统不一定去重。`awaiting_approval` 是第六个取值，权限还没接，暂时不会出现。

**`assistant.delta` 只在生成过程中出现，不补发。** `channel` 是 `content` 或 `reasoning`，
两路分开，客户端可以一边显示「正在想」一边打「正在答」。按 `text` 追加渲染就好，完整的
`assistant.message` 随后就到 —— 断线重连时以它为准。

发出去的 `user.message` **没有回声**。要显示用户说了什么，客户端自己存。

## 5 一次完整交互

真实跑一遍录的帧，省略了大部分 delta。问的是「请用 echo 工具回显这句话：链路通了」：

```
C→S  {"v":1,"type":"session.open","id":"c-7d55d4","payload":{"credential":"demo-token"}}
S→C  {"v":1,"type":"ack","id":"c-7d55d4","session_id":"default"}

C→S  {"v":1,"type":"user.message","id":"c-609125","session_id":"default","payload":{"content":"请用 echo 工具回显这句话：链路通了"}}

S→C  event assistant.delta   {"channel":"reasoning","seq":1,"step_id":"00000001","text":"用户"}
S→C  event assistant.delta   {"channel":"reasoning","seq":2,"step_id":"00000001","text":"要求我"}
S→C  event assistant.message {"content":"\n\n\n","model":"MiniMax-M2.7","reasoning":"用户要求我使用 echo 工具来回显…","tool_calls":[{"arguments":{"text":"链路通了"},"id":"call_be1996c1","name":"echo"}]}
S→C  event tool.call         {"arguments":{"text":"链路通了"},"invocation_id":"00000002","tool":"echo","tool_call_id":"call_be1996c1"}
S→C  event tool.terminal     {"content":"链路通了","invocation_id":"00000002","outcome":"succeeded","tool_call_id":"call_be1996c1"}
S→C  event assistant.delta   {"channel":"content","seq":1,"step_id":"00000003","text":"链路"}
S→C  event assistant.message {"content":"\n\n链路通了","model":"MiniMax-M2.7","reasoning":"The echo tool returned…"}
```

一轮里先有 `assistant.message` + `tool_calls`，工具跑完再来第二轮 —— 收到第二个
`assistant.message` 且没有 `tool_calls` 时，这一轮对话就完了。

配对靠两个 id：`tool_call_id` 是模型给的（assistant 轮 ↔ tool 消息的配对值，原样透传），
`invocation_id` 是 Oath 生成的（事件之间配对、查状态用）。**别拿 `tool_call_id` 去查状态。**

## 6 出错

`error` 帧的 `code` 才是给逻辑用的，`message` 是给人看的：

| code | 什么情况 |
| --- | --- |
| `bad_frame` | 信封字段不对 |
| `bad_payload` | payload 缺字段或有未知字段 |
| `unsupported_version` / `bad_version` | 协议版本不对 |
| `unknown_command` | 不认识的 command type |
| `not_bound` | 没先 `session.open` |
| `session_mismatch` | `session_id` 与绑定的不是一个 |
| `already_bound` | 这条连接已经绑过 session |
| `unsupported_command` | command 在协议里，但还没接进来（`permission.decide` 现在是这一支） |
| `internal_error` | 兜底 |

`close` 帧的 `reason`：

| reason | 客户端该怎么办 |
| --- | --- |
| `unauthorized` | credential 不对，别重试 |
| `session_replaced` | 同一 session 来了新连接，本条让位 |
| `slow_consumer` | 自己读太慢，发队列满了，或者连接数已满。重连后要能续上 |
| `idle_timeout` | 协议里留了，服务端还没启用超时断开 |
| `protocol_error` | 两端认知分叉了，重新协商版本 |

**帧本身不合法时服务端会回一帧 `error` 再断开** —— 不能只回错误继续收。command 解析
出来、业务没办成时只回 `error`，连接继续。

## 7 接的时候容易踩的

- **重连要走一遍 `session.open`。** session 是绑在连接上的，新连接要重新绑。
- **慢消费者会被断。** 发送队列默认 256 条，读不动就别堆积。收到 `slow_consumer` 要退避
  重连，不要立刻重连。
- **不要等 `ack` 来确认 `user.message` 生效** —— 它没有回执。结果要么作为事件出现，要么
  来一帧 `error`。
- **不认识的事件 type 记下来别报错。** 协议只保证 command 集合闭合，事件会随功能增加。
- **不认识的上行 command type 必须断开**，不能跳过 —— command 有副作用，跳过等于让两端
  的认知分叉。