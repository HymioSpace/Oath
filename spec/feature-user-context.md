# Feature — user_context

状态：实现中

## 1 背景与目标

客户端知道一些会话当下成立、但不该由用户逐句说出来的事实：正在哪个项目、连的是哪套环境、
这次请求属于哪个工单。这些事实过去只有一个去处 —— 让用户自己写进消息，于是每一句都要
重复。

`user_context` 让客户端在每条 `user.message` 上带一份 kv。runtime 按 scope 增量维护它：
同名 key 覆盖并更新时间，很久没人碰的 key 自动作废。它**只做透传** —— 模型看不到它，
框架也不解释它。

不包含：把 user_context 注入模型上下文；让它参与鉴权。

## 2 前置依赖

- [Intake](feature-intake.md)：user 队列的输入就是 `user.message`。
- [Journal](feature-journal.md)：维护动作是 scope 状态里的一笔事务。
- [Bash 与 CLI](feature-bash-cli.md)：它是 CLI 命令唯一的额外入参。

## 3 总览

### 3.1 一句话

**每条用户消息带一份 kv，runtime 按 scope 增量覆盖维护、24 小时不用就作废，需要它的命令
自己从 server 拿到。**

### 3.2 流程

```mermaid flowchart TD
    C[客户端] -->|user.message 带 user_context| UC[UserConsumer.apply]
    UC -->|merge 覆盖同名 key| ST[(meta.user_context)]
    CLI[oath-cli 命令] -->|GET /cli/xxx| S[biz server]
    S -->|current 裁掉过期项| ST
    S -->|整份透传| H[业务命令]
```

贯穿全文的规则：

- **一份消息给的是增量，不是全量。** 客户端没带的 key 原样留着，不会被这次覆盖掉。
- **每个 key 自带时间。** 覆盖同一个 key 就更新时间；没有更新的 key 时钟照走，24 小时后
  读不到。
- **只有一条维护路径。** 合并发生在 user 队列的 consumer 里，别的地方只读。
- **合并是覆盖，重放无害。** 合并与「这条输入已消费」是两笔事务：崩在两者之间，重启后
  这条输入会被重新消费、重新合并一次 —— 值不变，只有 `at` 被刷新到更晚的时刻。
- **框架不解释内容。** 值的形状、要不要拿它鉴权，都是业务命令自己的事。

### 3.3 一个完整的例子

客户端连上 `u1/s1`，发两条消息：

```json
{"type": "user.message", "payload": {"content": "看看 hymio", "user_context": {"project": "hymio", "env": "dev"}}}
{"type": "user.message", "payload": {"content": "那 oath 呢", "user_context": {"project": "oath"}}}
```

第二条之后 scope 状态里是：

```
meta.user_context = {
  "project": {"value": "oath", "at": 1760000600.0},
  "env":     {"value": "dev",  "at": 1760000000.0},
}
```

`env` 没被清掉 —— 这次没提它。模型两次都看不到这份 map。

之后模型在 bash 里执行 `oath-cli search docs`，server 交给业务命令的是
`{"project": "oath", "env": "dev"}`：`project` 是这次的值，`env` 是两条消息之前写的、
还没过期的那一份。

## 4 接口与数据

### 4.1 协议字段

`user.message` 的 `payload`：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `content` | 是 | 用户这一句 |
| `user_context` | 是 | object，key 是非空字符串，value 是 JSON 值 |

不合规则整帧被拒（`bad_payload`），不做部分接纳。

### 4.2 状态里的形状

`meta.user_context` 是一张表，每项记值与最后一次被写入的时刻：

```
{"project": {"value": "oath", "at": 1760000600.0}}
```

**整张表装在一个 meta key 里**，不是每个 user_context 的 key 各占一行：`RuntimeStore`
的事务不允许删 key（[Journal](feature-journal.md)），而过期就是删。覆盖写同一个 key 合法，
过期项在 map 内部被裁掉。

### 4.3 不变量

- 读到的永远是裁剪后的：`current()` 不会返回超过 24 小时的项。
- 写进 journal 的永远是覆盖后的：进程在两条消息之间重启，读回来的是最后一次写入的值。
- key 不跨 scope：一个 session 的 user_context 不会被另一个 session 读到。

## 5 实现方案

`core/context/user_context.py`：

| 函数 | 作用 |
| --- | --- |
| `merge(existing, incoming, now)` | 逐 key 覆盖并更新时间，顺带裁掉过期项 |
| `current(existing, now)` | 裁掉过期项，返回纯 kv |
| `table_of(state)` | 从 scope 状态里取出那张表；形状不对就当空的 |

维护点在 `core/consumer.py` 的 `UserConsumer.apply`：user 队列的输入就是用户消息，
在这里合并。消费一条输入分两笔事务 —— `apply` 自己提交合并，`receive` 提交「这条已经
处理过」；崩在中间就是重放，重放对值没有影响。没有后台清理线程 —— 裁剪在写入时和读取时
各做一次就够了，一个没人读的 key 留在 journal 里不占运行时任何东西。

## 6 失败与恢复

- **消息被拒。** `user_context` 形状不对时整帧拒掉，客户端重发时带上合法的值即可；这一轮
  没有产生任何状态。
- **进程在合并前崩溃。** 这条输入还在接纳记录里，重启后重新消费，合并照做。
- **重复消费。** 合并是覆盖，同一条消息被消费两次得到同一个值，只是 `at` 被刷新到更晚的
  时刻 —— 那个 key 的保质期从头开始算。要严格「客户端说多少次算一次」就得把合并并进
  `receive` 那一笔，代价是 core/context 要挂到 buffer 的写入路径上。
- **没有清理线程。** 一个从不再被碰、也不被读到的 key 会一直留在 journal 里。要回收只能
  靠它所在的 scope 被删；这是已知的缺口，不影响正确性。
- **过期是惰性的。** 裁剪只在写与读两处发生，不需要单独触发一次清理。

## 7 跨仓库协议变更

- **客户端（Hymio / 客户端）**：`user.message` 的 `payload` 新增必填 `user_context`。老客户端
  发的帧会被拒，属于破坏性变更。
- **Hymio**：决定 user_context 里放什么 key —— 这一层不解释内容，Hymio 与 Nostalgia 之间
  的约定不进 Oath。
- **Nostalgia**：不改协议。若某个 context 来源要按 key 提供值，由客户端取好后放进
  `user_context`。

## 8 待决事项

- 单条消息里 kv 的条数与单个 value 的大小上限还没定；先按 payload 通用上限兜着。
- 作废阈值 24 小时写死在 core。等实际场景给出更合适的值再让它可配。
